#
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
#

from neutron_lib.api.definitions import external_net
from neutron_lib.api.definitions import l3
from neutron_lib.api.definitions import portbindings
from neutron_lib.api.definitions import provider_net as pnet
from neutron_lib.callbacks import events
from neutron_lib.callbacks import registry
from neutron_lib.callbacks import resources
from neutron_lib import constants as n_const
from neutron_lib import context as n_context
from neutron_lib import exceptions as n_exc
from neutron_lib.plugins import constants as plugin_constants
from neutron_lib.plugins import directory
from neutron_lib.services import base as service_base
from oslo_log import log
from oslo_utils import excutils

from neutron.db import common_db_mixin
from neutron.db import dns_db
from neutron.db import extraroute_db
from neutron.db import l3_gwmode_db
from neutron.db.models import l3 as l3_models
from neutron.quota import resource_registry

from networking_ovn.common import constants as ovn_const
from networking_ovn.common import extensions
from networking_ovn.common import ovn_client
from networking_ovn.common import utils
from networking_ovn.db import revision as db_rev
from networking_ovn.l3 import l3_ovn_scheduler
from networking_ovn.ovsdb import impl_idl_ovn


LOG = log.getLogger(__name__)


@registry.has_registry_receivers
class OVNL3RouterPlugin(service_base.ServicePluginBase,
                        common_db_mixin.CommonDbMixin,
                        extraroute_db.ExtraRoute_dbonly_mixin,
                        l3_gwmode_db.L3_NAT_db_mixin,
                        dns_db.DNSDbMixin):
    """Implementation of the OVN L3 Router Service Plugin.

    This class implements a L3 service plugin that provides
    router and floatingip resources and manages associated
    request/response.
    """
    supported_extension_aliases = \
        extensions.ML2_SUPPORTED_API_EXTENSIONS_OVN_L3

    @resource_registry.tracked_resources(router=l3_models.Router,
                                         floatingip=l3_models.FloatingIP)
    def __init__(self):
        LOG.info("Starting OVNL3RouterPlugin")
        super(OVNL3RouterPlugin, self).__init__()
        self._nb_ovn_idl = None
        self._sb_ovn_idl = None
        self._plugin_property = None
        self._ovn_client_inst = None
        self.scheduler = l3_ovn_scheduler.get_scheduler()
        self._register_precommit_callbacks()

    def _register_precommit_callbacks(self):
        registry.subscribe(
            self.create_router_precommit, resources.ROUTER,
            events.PRECOMMIT_CREATE)
        registry.subscribe(
            self.create_floatingip_precommit, resources.FLOATING_IP,
            events.PRECOMMIT_CREATE)

    @property
    def _ovn_client(self):
        #传入北向接口，南向接口来创建OVNClient
        if self._ovn_client_inst is None:
            self._ovn_client_inst = ovn_client.OVNClient(self._ovn,
                                                         self._sb_ovn)
        return self._ovn_client_inst

    @property
    def _ovn(self):
        #创建北向接口
        if self._nb_ovn_idl is None:
            LOG.info("Getting OvsdbNbOvnIdl")
            conn = impl_idl_ovn.get_connection(impl_idl_ovn.OvsdbNbOvnIdl)
            self._nb_ovn_idl = impl_idl_ovn.OvsdbNbOvnIdl(conn)
        return self._nb_ovn_idl

    @property
    def _sb_ovn(self):
        #创建南向接口
        if self._sb_ovn_idl is None:
            LOG.info("Getting OvsdbSbOvnIdl")
            conn = impl_idl_ovn.get_connection(impl_idl_ovn.OvsdbSbOvnIdl)
            self._sb_ovn_idl = impl_idl_ovn.OvsdbSbOvnIdl(conn)
        return self._sb_ovn_idl

    @property
    def _plugin(self):
        #取核心插件
        if self._plugin_property is None:
            self._plugin_property = directory.get_plugin()
        return self._plugin_property

    def get_plugin_type(self):
        return plugin_constants.L3

    def get_plugin_description(self):
        """returns string description of the plugin."""
        return ("L3 Router Service Plugin for basic L3 forwarding"
                " using OVN")

    def create_router_precommit(self, resource, event, trigger, context,
                                router, router_id, router_db):
        db_rev.create_initial_revision(
            router_id, ovn_const.TYPE_ROUTERS, context.session)

    def create_router(self, context, router):
        #创建路由器
        router = super(OVNL3RouterPlugin, self).create_router(context, router)
        try:
            self._ovn_client.create_router(router)
        except Exception:
            with excutils.save_and_reraise_exception():
                # Delete the logical router
                LOG.error('Unable to create lrouter for %s', router['id'])
                super(OVNL3RouterPlugin, self).delete_router(context,
                                                             router['id'])
        return router

    def update_router(self, context, id, router):
        #路由器更新，做snat下发
        original_router = self.get_router(context, id)
        result = super(OVNL3RouterPlugin, self).update_router(context, id,
                                                              router)
        try:
            self._ovn_client.update_router(result, original_router)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception('Unable to update lrouter for %s', id)
                revert_router = {'router': original_router}
                super(OVNL3RouterPlugin, self).update_router(context, id,
                                                             revert_router)
        return result

    def delete_router(self, context, id):
        #路由器删除
        original_router = self.get_router(context, id)
        super(OVNL3RouterPlugin, self).delete_router(context, id)
        try:
            self._ovn_client.delete_router(id)
        except Exception:
            with excutils.save_and_reraise_exception():
                super(OVNL3RouterPlugin, self).create_router(
                    context, {'router': original_router})

    def add_router_interface(self, context, router_id, interface_info):
        #路由器接口添加，当接口增加时，做snat时它的源ip的网段增加了，故需要更新
        router_interface_info = \
            super(OVNL3RouterPlugin, self).add_router_interface(
                context, router_id, interface_info)
        port = self._plugin.get_port(context, router_interface_info['port_id'])

        multi_prefix = False
        if (len(router_interface_info['subnet_ids']) == 1 and
                len(port['fixed_ips']) > 1):
            # NOTE(lizk) It's adding a subnet onto an already existing router
            # interface port, try to update lrouter port 'networks' column.
            self._ovn_client.update_router_port(port,
                                                bump_db_rev=False)
            multi_prefix = True
        else:
            self._ovn_client.create_router_port(router_id, port)

        router = self.get_router(context, router_id)
        if not router.get(l3.EXTERNAL_GW_INFO):
            #如果无gateway,则直接返回
            return router_interface_info

        #如果有gateway，则需要增加nat规则
        cidr = None
        for fixed_ip in port['fixed_ips']:
            subnet = self._plugin.get_subnet(context, fixed_ip['subnet_id'])
            if multi_prefix:
                if 'subnet_id' in interface_info:
                    if subnet['id'] is not interface_info['subnet_id']:
                        continue
            if subnet['ip_version'] == 4:
                cidr = subnet['cidr']

        if utils.is_snat_enabled(router) and cidr:
            try:
                #snat更新
                self._ovn_client.update_nat_rules(router, networks=[cidr],
                                                  enable_snat=True)
            except Exception:
                with excutils.save_and_reraise_exception():
                    self._ovn.delete_lrouter_port(
                        utils.ovn_lrouter_port_name(port['id']),
                        utils.ovn_name(router_id)).execute(check_error=True)
                    super(OVNL3RouterPlugin, self).remove_router_interface(
                        context, router_id, router_interface_info)
                    LOG.error('Error updating snat for subnet %(subnet)s in '
                              'router %(router)s',
                              {'subnet': router_interface_info['subnet_id'],
                               'router': router_id})

        db_rev.bump_revision(port, ovn_const.TYPE_ROUTER_PORTS)
        return router_interface_info

    def remove_router_interface(self, context, router_id, interface_info):
        #路由器接口移除，接口移除会导致做snat的源ip网段减少，故需要更新snat规则
        router_interface_info = \
            super(OVNL3RouterPlugin, self).remove_router_interface(
                context, router_id, interface_info)
        router = self.get_router(context, router_id)
        port_id = router_interface_info['port_id']
        multi_prefix = False
        port_removed = False
        try:
            port = self._plugin.get_port(context, port_id)
            # The router interface port still exists, call ovn to update it.
            self._ovn_client.update_router_port(port,
                                                bump_db_rev=False)
            multi_prefix = True
        except n_exc.PortNotFound:
            # The router interface port doesn't exist any more,
            # we will call ovn to delete it once we remove the snat
            # rules in the router itself if we have to
            port_removed = True

        if not router.get(l3.EXTERNAL_GW_INFO):
            if port_removed:
                self._ovn_client.delete_router_port(port_id, router_id)
            return router_interface_info

        #有external_gateway时需要移除nat
        try:
            cidr = None
            if multi_prefix:
                subnet = self._plugin.get_subnet(context,
                                                 interface_info['subnet_id'])
                if subnet['ip_version'] == 4:
                    cidr = subnet['cidr']
            else:
                subnet_ids = router_interface_info.get('subnet_ids')
                for subnet_id in subnet_ids:
                    subnet = self._plugin.get_subnet(context, subnet_id)
                    if subnet['ip_version'] == 4:
                        cidr = subnet['cidr']
                        break

            if utils.is_snat_enabled(router) and cidr:
                self._ovn_client.update_nat_rules(
                    router, networks=[cidr], enable_snat=False)
        except Exception:
            with excutils.save_and_reraise_exception():
                super(OVNL3RouterPlugin, self).add_router_interface(
                    context, router_id, interface_info)
                LOG.error('Error is deleting snat')

        # NOTE(mangelajo): If the port doesn't exist anymore, we delete the
        # router port as the last operation and update the revision database
        # to ensure consistency
        if port_removed:
            self._ovn_client.delete_router_port(port_id, router_id)
        else:
            # otherwise, we just update the revision database
            db_rev.bump_revision(port, ovn_const.TYPE_ROUTER_PORTS)

        return router_interface_info

    def create_floatingip_precommit(self, resource, event, trigger, context,
                                    floatingip, floatingip_id, floatingip_db):
        db_rev.create_initial_revision(
            floatingip_id, ovn_const.TYPE_FLOATINGIPS, context.session)

    def create_floatingip(self, context, floatingip,
                          initial_status=n_const.FLOATINGIP_STATUS_DOWN):
        #添加floatingip,需要增加一对一nat规则
        fip = super(OVNL3RouterPlugin, self).create_floatingip(
            context, floatingip, initial_status)
        self._ovn_client.create_floatingip(fip)
        return fip

    def delete_floatingip(self, context, id):
        # TODO(lucasagomes): Passing ``original_fip`` object as a
        # parameter to the OVNClient's delete_floatingip() method is done
        # for backward-compatible reasons. Remove it in the Rocky release
        # of OpenStack.
        original_fip = self.get_floatingip(context, id)
        super(OVNL3RouterPlugin, self).delete_floatingip(context, id)
        self._ovn_client.delete_floatingip(id, fip_object=original_fip)

    def update_floatingip(self, context, id, floatingip):
        # TODO(lucasagomes): Passing ``original_fip`` object as a
        # parameter to the OVNClient's update_floatingip() method is done
        # for backward-compatible reasons. Remove it in the Rocky release
        # of OpenStack.
        original_fip = self.get_floatingip(context, id)
        fip = super(OVNL3RouterPlugin, self).update_floatingip(context, id,
                                                               floatingip)
        self._ovn_client.update_floatingip(fip, fip_object=original_fip)
        return fip

    def update_floatingip_status(self, context, floatingip_id, status):
        fip = super(OVNL3RouterPlugin, self).update_floatingip_status(
            context, floatingip_id, status)
        self._ovn_client.update_floatingip_status(fip)
        return fip

    def disassociate_floatingips(self, context, port_id, do_notify=True):
        #解除floating-ip的关联，实现nat的移除
        fips = self.get_floatingips(context.elevated(),
                                    filters={'port_id': [port_id]})
        router_ids = super(OVNL3RouterPlugin, self).disassociate_floatingips(
            context, port_id, do_notify)
        for fip in fips:
            router_id = fip.get('router_id')
            fixed_ip_address = fip.get('fixed_ip_address')
            if router_id and fixed_ip_address:
                update_fip = {'logical_ip': fixed_ip_address,
                              'external_ip': fip['floating_ip_address']}
                try:
                    self._ovn_client.disassociate_floatingip(update_fip,
                                                             router_id)
                    self.update_floatingip_status(
                        context, fip['id'], n_const.FLOATINGIP_STATUS_DOWN)
                except Exception as e:
                    LOG.error('Error in disassociating floatingip %(id)s: '
                              '%(error)s', {'id': fip['id'], 'error': e})
        return router_ids

    def _get_gateway_port_physnet_mapping(self):
        # This function returns all gateway ports with corresponding
        # external network's physnet
        net_physnet_dict = {}
        port_physnet_dict = {}
        l3plugin = directory.get_plugin(plugin_constants.L3)
        if not l3plugin:
            return port_physnet_dict
        context = n_context.get_admin_context()
        for net in l3plugin._plugin.get_networks(
            context, {external_net.EXTERNAL: [True]}):
            if net.get(pnet.NETWORK_TYPE) in [n_const.TYPE_FLAT,
                                              n_const.TYPE_VLAN]:
                net_physnet_dict[net['id']] = net.get(pnet.PHYSICAL_NETWORK)
        for port in l3plugin._plugin.get_ports(context, filters={
            'device_owner': [n_const.DEVICE_OWNER_ROUTER_GW]}):
            port_physnet_dict[port['id']] = net_physnet_dict.get(
                port['network_id'])
        return port_physnet_dict

    def update_router_gateway_port_bindings(self, router, host):
        context = n_context.get_admin_context()
        filters = {'device_id': [router],
                   'device_owner': [n_const.DEVICE_OWNER_ROUTER_GW]}
        for port in self._plugin.get_ports(context, filters=filters):
            self._plugin.update_port(
                context, port['id'], {'port': {portbindings.HOST_ID: host}})

    def schedule_unhosted_gateways(self):
        port_physnet_dict = self._get_gateway_port_physnet_mapping()
        chassis_physnets = self._sb_ovn.get_chassis_and_physnets()
        cms = self._sb_ovn.get_gateway_chassis_from_cms_options()
        unhosted_gateways = self._ovn.get_unhosted_gateways(
            port_physnet_dict, chassis_physnets, cms)
        with self._ovn.transaction(check_error=True) as txn:
            for g_name in unhosted_gateways:
                physnet = port_physnet_dict.get(g_name[len('lrp-'):])
                candidates = self._ovn_client.get_candidates_for_scheduling(
                    physnet, cms=cms, chassis_physnets=chassis_physnets)
                chassis = self.scheduler.select(
                    self._ovn, self._sb_ovn, g_name, candidates=candidates)
                txn.add(self._ovn.update_lrouter_port(
                    g_name, gateway_chassis=chassis))

    @staticmethod
    @registry.receives(resources.SUBNET, [events.AFTER_UPDATE])
    def _subnet_update(resource, event, trigger, **kwargs):
        #subnet更新可能会导致路由发生变化，故检查是否external-network发生变化
        #如变换更新默认路由（我之前也在这种情况下出过一次bug)
        l3plugin = directory.get_plugin(plugin_constants.L3)
        if not l3plugin:
            return
        context = kwargs['context']
        orig = kwargs['original_subnet']
        current = kwargs['subnet']
        orig_gw_ip = orig['gateway_ip']
        current_gw_ip = current['gateway_ip']
        if orig_gw_ip == current_gw_ip:
            return
        gw_ports = l3plugin._plugin.get_ports(context, filters={
            'network_id': [orig['network_id']],
            'device_owner': [n_const.DEVICE_OWNER_ROUTER_GW],
            'fixed_ips': {'subnet_id': [orig['id']]},
        })
        router_ids = set([port['device_id'] for port in gw_ports])
        remove = [{'destination': '0.0.0.0/0', 'nexthop': orig_gw_ip}
                  ] if orig_gw_ip else []
        add = [{'destination': '0.0.0.0/0', 'nexthop': current_gw_ip}
               ] if current_gw_ip else []
        with l3plugin._ovn.transaction(check_error=True) as txn:
            for router_id in router_ids:
                l3plugin._ovn_client.update_router_routes(
                    context, router_id, add, remove, txn=txn)

    @staticmethod
    @registry.receives(resources.PORT, [events.AFTER_UPDATE])
    def _port_update(resource, event, trigger, **kwargs):
        l3plugin = directory.get_plugin(plugin_constants.L3)
        if not l3plugin:
            return

        current = kwargs['port']

        if utils.is_lsp_router_port(current):
            # We call the update_router port with if_exists, because neutron,
            # internally creates the port, and then calls update, which will
            # trigger this callback even before we had the chance to create
            # the OVN NB DB side
            l3plugin._ovn_client.update_router_port(current, if_exists=True)
