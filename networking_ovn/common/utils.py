# encoding:utf-8
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os

import netaddr
from neutron_lib.api.definitions import extra_dhcp_opt as edo_ext
from neutron_lib.api.definitions import l3
from neutron_lib.api import validators
from neutron_lib import constants as const
from neutron_lib import context as n_context
from neutron_lib import exceptions as n_exc
from neutron_lib.plugins import directory
from neutron_lib.utils import net as n_utils
from oslo_utils import netutils
from oslo_utils import strutils

from networking_ovn._i18n import _
from networking_ovn.common import constants
from networking_ovn.common import exceptions as ovn_exc


def ovn_name(id):
    # The name of the OVN entry will be neutron-<UUID>
    # This is due to the fact that the OVN application checks if the name
    # is a UUID. If so then there will be no matches.
    # We prefix the UUID to enable us to use the Neutron UUID when
    # updating, deleting etc.
    # ovn路由器名称及交换机名称
    return 'neutron-%s' % id


def ovn_lrouter_port_name(id):
    # The name of the OVN lrouter port entry will be lrp-<UUID>
    # This is to distinguish with the name of the connected lswitch patch port,
    # which is named with neutron port uuid, so that OVS patch ports are
    # generated properly. The pairing patch port names will be:
    #   - patch-lrp-<UUID>-to-<UUID>
    #   - patch-<UUID>-to-lrp-<UUID>
    # lrp stands for Logical Router Port
    # 路由器接口名称
    return 'lrp-%s' % id


def ovn_provnet_port_name(network_id):
    # The name of OVN lswitch provider network port entry will be
    # provnet-<Network-UUID>. The port is created for network having
    # provider:physical_network attribute.
    #创建provider network port,用于与物理网络实现基于segment id的共享
    return constants.OVN_PROVNET_PORT_NAME_PREFIX + '%s' % network_id


def ovn_vhu_sockpath(sock_dir, port_id):
    # Frame the socket path of a virtio socket
    #生成vhostuser对应的socketpath
    return os.path.join(
        sock_dir,
        # this parameter will become the virtio port name,
        # so it should not exceed IFNAMSIZ(16).
        (const.VHOST_USER_DEVICE_PREFIX + port_id)[:14])


def ovn_addrset_name(sg_id, ip_version):
    #生成ovn的安全组名称
    # The name of the address set for the given security group id and ip
    # version. The format is:
    #   as-<ip version>-<security group uuid>
    # with all '-' replaced with '_'. This replacement is necessary
    # because OVN doesn't support '-' in an address set name.
    return ('as-%s-%s' % (ip_version, sg_id)).replace('-', '_')


def is_network_device_port(port):
    return port.get('device_owner', '').startswith(
        const.DEVICE_OWNER_PREFIXES)


def get_lsp_dhcp_opts(port, ip_version):
    # Get dhcp options from Neutron port, for setting DHCP_Options row
    # in OVN.
    lsp_dhcp_disabled = False
    lsp_dhcp_opts = {}
    if is_network_device_port(port):
        lsp_dhcp_disabled = True
    else:
        for edo in port.get(edo_ext.EXTRADHCPOPTS, []):
            if edo['ip_version'] != ip_version:
                continue

            if edo['opt_name'] == 'dhcp_disabled' and (
                    edo['opt_value'] in ['True', 'true']):
                # OVN native DHCP is disabled on this port
                #主动禁用dhcp
                lsp_dhcp_disabled = True
                # Make sure return value behavior not depends on the order and
                # content of the extra DHCP options for the port
                lsp_dhcp_opts.clear()
                break

            if edo['opt_name'] not in (
                    constants.SUPPORTED_DHCP_OPTS[ip_version]):
                #不接受不认识的选项
                continue

            opt = edo['opt_name'].replace('-', '_')
            lsp_dhcp_opts[opt] = edo['opt_value']

    return (lsp_dhcp_disabled, lsp_dhcp_opts)


def is_lsp_trusted(port):
    #检查接口是否为可信接口
    return n_utils.is_port_trusted(port) if port.get('device_owner') else False


def is_lsp_ignored(port):
    # Since the floating IP port is not bound to any chassis, packets from vm
    # destined to floating IP will be dropped. To overcome this, we do not
    # create/update floating IP port in OVN.
    return port.get('device_owner') in [const.DEVICE_OWNER_FLOATINGIP]


def get_lsp_security_groups(port, skip_trusted_port=True):
    # In other agent link OVS, skipping trusted port is processed in security
    # groups RPC.  We haven't that step, so we do it here.
    return [] if (skip_trusted_port and is_lsp_trusted(port)
                  ) else port.get('security_groups', [])


def is_snat_enabled(router):
    return router.get(l3.EXTERNAL_GW_INFO, {}).get('enable_snat', True)


def validate_and_get_data_from_binding_profile(port):
    if (constants.OVN_PORT_BINDING_PROFILE not in port or
            not validators.is_attr_set(
                port[constants.OVN_PORT_BINDING_PROFILE])):
        #如果接口没有配置profile
        return {}
    param_set = {}
    param_dict = {}
    for param_set in constants.OVN_PORT_BINDING_PROFILE_PARAMS:
        param_keys = param_set.keys()
        for param_key in param_keys:
            try:
                param_dict[param_key] = (port[
                    constants.OVN_PORT_BINDING_PROFILE][param_key])
            except KeyError:
                #如果无此配置，跳continue
                pass
        if len(param_dict) == 0:
            continue
        if len(param_dict) != len(param_keys):
            #存在不认识的key
            msg = _('Invalid binding:profile. %s are all '
                    'required.') % param_keys
            raise n_exc.InvalidInput(error_message=msg)
        if (len(port[constants.OVN_PORT_BINDING_PROFILE]) != len(
                param_keys)):
            #存在过多的key
            msg = _('Invalid binding:profile. too many parameters')
            raise n_exc.InvalidInput(error_message=msg)
        break

    if not param_dict:
        return {}

    #检查获得的值是否是期待的类型，如果不是，报错
    for param_key, param_type in param_set.items():
        if param_type is None:
            continue
        param_value = param_dict[param_key]
        if not isinstance(param_value, param_type):
            msg = _('Invalid binding:profile. %(key)s %(value)s '
                    'value invalid type') % {'key': param_key,
                                             'value': param_value}
            raise n_exc.InvalidInput(error_message=msg)

    # Make sure we can successfully look up the port indicated by
    # parent_name.  Just let it raise the right exception if there is a
    # problem.
    if 'parent_name' in param_set:
        plugin = directory.get_plugin() #取核心插件
        plugin.get_port(n_context.get_admin_context(),
                        param_dict['parent_name']) #确认port存在，如果port不存在，将扔异常

    if 'tag' in param_set:
        #tag值必须是合法的vlan
        tag = int(param_dict['tag'])
        if tag < 0 or tag > 4095:
            msg = _('Invalid binding:profile. tag "%s" must be '
                    'an integer between 0 and 4095, inclusive') % tag
            raise n_exc.InvalidInput(error_message=msg)

    return param_dict


def is_dhcp_options_ignored(subnet):
    # Don't insert DHCP_Options entry for v6 subnet with 'SLAAC' as
    # 'ipv6_address_mode', since DHCPv6 shouldn't work for this mode.
    return (subnet['ip_version'] == const.IP_VERSION_6 and
            subnet.get('ipv6_address_mode') == const.IPV6_SLAAC)


def get_ovn_ipv6_address_mode(address_mode):
    return constants.OVN_IPV6_ADDRESS_MODES[address_mode]


def get_revision_number(resource, resource_type):
    """Get the resource's revision number based on its type."""
    if resource_type in (constants.TYPE_NETWORKS,
                         constants.TYPE_PORTS,
                         constants.TYPE_SECURITY_GROUP_RULES,
                         constants.TYPE_ROUTERS,
                         constants.TYPE_ROUTER_PORTS,
                         constants.TYPE_SECURITY_GROUPS,
                         constants.TYPE_FLOATINGIPS, constants.TYPE_SUBNETS):
        return resource['revision_number']
    else:
        raise ovn_exc.UnknownResourceType(resource_type=resource_type)


def remove_macs_from_lsp_addresses(addresses):
    """Remove the mac addreses from the Logical_Switch_Port addresses column.

    :param addresses: The list of addresses from the Logical_Switch_Port.
        Example: ["80:fa:5b:06:72:b7 158.36.44.22",
                  "ff:ff:ff:ff:ff:ff 10.0.0.2"]
    :returns: A list of IP addesses (v4 and v6)
    """
    ip_list = []
    for addr in addresses:
        ip_list.extend([x for x in addr.split() if
                       (netutils.is_valid_ipv4(x) or
                        netutils.is_valid_ipv6(x))])
    return ip_list


def get_allowed_address_pairs_ip_addresses(port):
    """Return a list of IP addresses from port's allowed_address_pairs.

    :param port: A neutron port
    :returns: A list of IP addesses (v4 and v6)
    """
    return [x['ip_address'] for x in port.get('allowed_address_pairs', [])
            if 'ip_address' in x]


def get_allowed_address_pairs_ip_addresses_from_ovn_port(ovn_port):
    """Return a list of IP addresses from ovn port.

    Return a list of IP addresses equivalent of Neutron's port
    allowed_address_pairs column using the data in the OVN port.

    :param ovn_port: A OVN port
    :returns: A list of IP addesses (v4 and v6)
    """
    addresses = remove_macs_from_lsp_addresses(ovn_port.addresses)
    port_security = remove_macs_from_lsp_addresses(ovn_port.port_security)
    return [x for x in port_security if x not in addresses]


def get_ovn_port_security_groups(ovn_port, skip_trusted_port=True):
    info = {'security_groups': ovn_port.external_ids.get(
            constants.OVN_SG_IDS_EXT_ID_KEY, '').split(),
            'device_owner': ovn_port.external_ids.get(
            constants.OVN_DEVICE_OWNER_EXT_ID_KEY, '')}
    return get_lsp_security_groups(info, skip_trusted_port=skip_trusted_port)


def get_ovn_port_addresses(ovn_port):
    addresses = remove_macs_from_lsp_addresses(ovn_port.addresses)
    port_security = remove_macs_from_lsp_addresses(ovn_port.port_security)
    return list(set(addresses + port_security))


def sort_ips_by_version(addresses):
    ip_map = {'ip4': [], 'ip6': []}
    for addr in addresses:
        ip_version = netaddr.IPNetwork(addr).version
        ip_map['ip%d' % ip_version].append(addr)
    return ip_map


def is_lsp_router_port(port):
    return port.get('device_owner') in [const.DEVICE_OWNER_ROUTER_INTF,
                                        const.DEVICE_OWNER_ROUTER_GW]


def get_lrouter_ext_gw_static_route(ovn_router):
    # TODO(lucasagomes): Remove the try...except block after OVS 2.8.2
    # is tagged.
    try:
        for route in getattr(ovn_router, 'static_routes', []):
            external_ids = getattr(route, 'external_ids', {})
            if strutils.bool_from_string(
                external_ids.get(constants.OVN_ROUTER_IS_EXT_GW, 'false')):
                return route
    except KeyError:
        pass


def get_lrouter_snats(ovn_router):
    return [n for n in getattr(ovn_router, 'nat', []) if n.type == 'snat']


def get_lrouter_non_gw_routes(ovn_router):
    routes = []
    # TODO(lucasagomes): Remove the try...except block after OVS 2.8.2
    # is tagged.
    try:
        for route in getattr(ovn_router, 'static_routes', []):
            external_ids = getattr(route, 'external_ids', {})
            if strutils.bool_from_string(
                external_ids.get(constants.OVN_ROUTER_IS_EXT_GW, 'false')):
                continue

            routes.append({'destination': route.ip_prefix,
                           'nexthop': route.nexthop})
    except KeyError:
        pass
    return routes


def is_ovn_l3(l3_plugin):
    return hasattr(l3_plugin, '_ovn_client_inst')
