#    Copyright 2013 - 2014 Mirantis, Inc.
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

import argparse
import os
import sys

import ipaddr

import devops
from devops.helpers.helpers import _get_file_size
from devops.helpers.helpers import sync_node_time
from devops.helpers import node_manager
from devops.models import Environment
from devops.models.network import Network
from devops import settings


class Shell(object):
    def __init__(self):
        self.params = self.get_params()
        if (getattr(self.params, 'name', None) and
                getattr(self.params, 'command', None) != 'create'):
            self.env = Environment.get(name=self.params.name)

    def execute(self):
        self.commands.get(self.params.command)(self)

    def do_list(self):
        env_list = Environment.list().values('name', 'created')
        for env in env_list:
            if self.params.list_ips:
                cur_env = Environment.get(name=env['name'])
                admin_ip = ''
                if 'admin' in [node.name for node in cur_env.get_nodes()]:
                    admin_ip = (cur_env.get_node(name='admin').
                                get_ip_address_by_network_name('admin'))
                print('{0}\t{1}'.format(env['name'], admin_ip))
            elif self.params.timestamps:
                created_text = env['created'].strftime('%Y-%m-%d_%H:%M:%S')
                print('{0} {1}'.format(env['name'], created_text))
            else:
                print(env['name'])

        return env_list

    def node_dict(self, node):
        return {'name': node.name,
                'vnc': node.get_vnc_port()}

    def do_show(self):
        print('%5s %25s' % ("VNC", "NODE-NAME"))
        for item in map(lambda x: self.node_dict(x), self.env.get_nodes()):
            print ('%5s %25s' % (item['vnc'], item['name']))

    def do_erase(self):
        self.env.erase()

    def do_start(self):
        self.env.start()

    def do_destroy(self):
        self.env.destroy(verbose=False)

    def do_suspend(self):
        self.env.suspend(verbose=False)

    def do_resume(self):
        self.env.resume(verbose=False)

    def do_revert(self):
        self.env.revert(self.params.snapshot_name, flag=False)

    def do_snapshot(self):
        self.env.snapshot(self.params.snapshot_name)

    def do_synchronize(self):
        Environment.synchronize_all()

    def do_snapshot_list(self):
        snap_nodes = {}
        max_len = 0
        for node in self.env.get_nodes():
            snaps = sorted(node.get_snapshots())
            for snap in snaps:
                if len(snap) > max_len:
                    max_len = len(snap)
                if snap in snap_nodes:
                    snap_nodes[snap].append(node.name)
                else:
                    snap_nodes[snap] = [node.name, ]

        print("%*s     %50s" % (max_len, "SNAPSHOT", "NODES-NAME"))
        for snap in snap_nodes:
            print("%*s     %50s" % (max_len, snap,
                                    ', '.join(snap_nodes[snap])))

    def do_snapshot_delete(self):
        for node in self.env.get_nodes():
            snaps = sorted(node.get_snapshots())
            if self.params.snapshot_name in snaps:
                node.erase_snapshot(name=self.params.snapshot_name)

    def do_net_list(self):
        print("%15s   %10s" % ("NETWORK NAME", "IP NET"))
        for network in self.env.get_networks():
            print("%15s  %10s" % (network.name, network.ip_network))

    def do_timesync(self):
        if not self.params.node_name:
            for node in self.env.get_nodes():
                if node.driver.node_active(node):
                    datetime = sync_node_time(self.env, node.name)
                    print('Node [{0}]: {1}'.format(node.name, datetime))
        else:
            datetime = sync_node_time(self.env, self.params.node_name)
            print('Node [{0}]: {1}'.format(self.params.node_name, datetime))

    def do_revert_resume(self):
        self.env.revert(self.params.snapshot_name, flag=False)
        self.env.resume(verbose=False)
        if not self.params.no_timesync:
            print('Time synchronization is starting')
            self.do_timesync()

    def do_version(self):
        print(devops.__version__)

    def do_create(self):
        env_name = self.params.name
        for env in Environment.list():
            if env.name == env_name:
                print("Please, set another environment name")
                raise SystemExit()
        self.env = Environment.create(env_name)
        networks, prefix = self.params.net_pool.split(':')
        Network.default_pool = Network.create_network_pool(
            networks=[ipaddr.IPNetwork(networks)],
            prefix=int(prefix))
        networks = Network.create_networks(environment=self.env)
        admin_node = self.admin_add(networks=networks)
        self.do_slave_add(force_define=False)
        self.env.define()
        admin_node.disk_devices.get(device='cdrom').volume.upload(
            self.params.iso_path)
        for net in self.env.get_networks():
            net.start()

    def do_slave_add(self, force_define=True):
        vcpu = self.params.vcpu_count
        memory = self.params.ram_size
        created_nodes = len(self.env.get_nodes())
        node_count = self.params.node_count

        for node in xrange(created_nodes, created_nodes + node_count):
            node_name = "slave-%i" % (node)
            node = self.env.add_node(name=node_name, vcpu=vcpu, memory=memory)
            disknames_capacity = {
                'system': 50 * 1024 ** 3
            }
            if self.params.second_disk_size > 0:
                disknames_capacity[
                    'cinder'] = self.params.second_disk_size * 1024 ** 3
            if self.params.third_disk_size > 0:
                disknames_capacity[
                    'swift'] = self.params.third_disk_size * 1024 ** 3
            node.attach_disks(
                disknames_capacity=disknames_capacity,
                force_define=force_define)
            node.attach_to_networks()
            if force_define is True:
                node.define()

    def do_slave_remove(self):
        volumes = []
        for drive in self.env.get_node(
                name=self.params.node_name).disk_devices:
            volumes.append(drive.volume)
        self.env.get_node(name=self.params.node_name).remove()
        for volume in volumes:
            volume.erase()

    def do_slave_change(self):
        node = self.env.get_node(name=self.params.node_name)
        node.set_vcpu(vcpu=self.params.vcpu_count)
        node.set_memory(memory=self.params.ram_size)

    def do_admin_change(self):
        node = self.env.get_node(name="admin")
        node.set_vcpu(vcpu=self.params.vcpu_count)
        node.set_memory(memory=self.params.ram_size)

    def do_admin_setup(self):
        admin_node = self.env.get_node(name='admin')
        admin_node.destroy()
        node_manager.admin_prepare_disks(node=admin_node,
                                         disk_size=self.params.admin_disk_size)
        admin_node.start()
        node_manager.admin_change_config(admin_node)
        admin_node.await("admin", timeout=10 * 60)
        node_manager.admin_wait_bootstrap(3000, self.env)

    def admin_add(self, networks=None):
        vcpu = self.params.admin_vcpu_count
        ram = self.params.admin_ram_size
        iso_path = self.params.iso_path
        iso_size = _get_file_size(iso_path)

        if not (iso_size > 0):
            print("Please, set correct ISO file")
            sys.exit(1)
        if networks is None:
            networks = []
            interfaces = settings.INTERFACE_ORDER
            for name in interfaces:
                networks.append(self.env.create_networks(name))
        return self.env.describe_admin_node(name="admin",
                                            vcpu=vcpu,
                                            networks=networks,
                                            memory=ram,
                                            iso_path=iso_path)

    def do_node_start(self):
        self.env.get_node(name=self.params.node_name).start()

    def do_node_destroy(self):
        self.env.get_node(name=self.params.node_name).destroy()

    def do_node_reset(self):
        self.env.get_node(name=self.params.node_name).node_reset()

    commands = {
        'list': do_list,
        'show': do_show,
        'erase': do_erase,
        'start': do_start,
        'destroy': do_destroy,
        'suspend': do_suspend,
        'resume': do_resume,
        'revert': do_revert,
        'snapshot': do_snapshot,
        'sync': do_synchronize,
        'snapshot-list': do_snapshot_list,
        'snapshot-delete': do_snapshot_delete,
        'net-list': do_net_list,
        'time-sync': do_timesync,
        'revert-resume': do_revert_resume,
        'version': do_version,
        'create': do_create,
        'slave-add': do_slave_add,
        'slave-change': do_slave_change,
        'slave-remove': do_slave_remove,
        'admin-setup': do_admin_setup,
        'admin-change': do_admin_change,
        'node-start': do_node_start,
        'node-destroy': do_node_destroy,
        'node-reset': do_node_reset
    }

    def get_params(self):
        name_parser = argparse.ArgumentParser(add_help=False)
        name_parser.add_argument('name', help='environment name',
                                 default=os.environ.get('ENV_NAME'),
                                 metavar='ENV_NAME')
        snapshot_name_parser = argparse.ArgumentParser(add_help=False)
        snapshot_name_parser.add_argument('--snapshot-name',
                                          help='snapshot name',
                                          default=os.environ.get(
                                              'SNAPSHOT_NAME'))
        node_name_parser = argparse.ArgumentParser(add_help=False)
        node_name_parser.add_argument('--node-name',
                                      help='node name',
                                      default=None)
        no_timesync_parser = argparse.ArgumentParser(add_help=False)
        no_timesync_parser.add_argument('--no-timesync', dest='no_timesync',
                                        action='store_const', const=True,
                                        help='revert without timesync',
                                        default=False)
        list_ips_parser = argparse.ArgumentParser(add_help=False)
        list_ips_parser.add_argument('--ips', dest='list_ips',
                                     action='store_const', const=True,
                                     help='show admin node ip addresses',
                                     default=False)
        timestamps_parser = argparse.ArgumentParser(add_help=False)
        timestamps_parser.add_argument('--timestamps', dest='timestamps',
                                       action='store_const', const=True,
                                       help='show creation timestamps',
                                       default=False)
        iso_path_parser = argparse.ArgumentParser(add_help=False)
        iso_path_parser.add_argument('--iso-path', dest='iso_path',
                                     help='Set Fuel ISO path',
                                     required=True)
        admin_ram_parser = argparse.ArgumentParser(add_help=False)
        admin_ram_parser.add_argument('--admin-ram', dest='admin_ram_size',
                                      help='Select admin node RAM size (MB)',
                                      default=1536, type=int)
        admin_vcpu_parser = argparse.ArgumentParser(add_help=False)
        admin_vcpu_parser.add_argument('--admin-vcpu', dest='admin_vcpu_count',
                                       help='Select admin node VCPU count',
                                       default=2, type=int)
        admin_disk_size_parser = argparse.ArgumentParser(add_help=False)
        admin_disk_size_parser.add_argument('--admin-disk-size',
                                            dest='admin_disk_size',
                                            help='Set admin node disk '
                                                 'size (GB)',
                                            default=50, type=int)
        ram_parser = argparse.ArgumentParser(add_help=False)
        ram_parser.add_argument('--ram', dest='ram_size',
                                help='Set node RAM size',
                                default=1024, type=int)
        vcpu_parser = argparse.ArgumentParser(add_help=False)
        vcpu_parser.add_argument('--vcpu', dest='vcpu_count',
                                 help='Set node VCPU count',
                                 default=1, type=int)
        node_count = argparse.ArgumentParser(add_help=False)
        node_count.add_argument('--node-count', dest='node_count',
                                help='How many nodes will be created',
                                default=1, type=int)
        net_pool = argparse.ArgumentParser(add_help=False)
        net_pool.add_argument('--net-pool', dest='net_pool',
                              help='Set ip network pool (cidr)',
                              default="10.21.0.0/16:24", type=str)
        second_disk_size = argparse.ArgumentParser(add_help=False)
        second_disk_size.add_argument('--second-disk-size',
                                      dest='second_disk_size',
                                      help='Allocate second disk for node '
                                           'with selected size(GB). '
                                           'If set to 0, the disk will not be '
                                           'allocated',
                                      default=50, type=int)
        third_disk_size = argparse.ArgumentParser(add_help=False)
        third_disk_size.add_argument('--third-disk-size',
                                     dest='third_disk_size',
                                     help='Allocate the third disk for node '
                                          'with selected size(GB). '
                                          'If set to 0, the disk will not be '
                                          'allocated',
                                     default=50, type=int)
        parser = argparse.ArgumentParser(
            description="Manage virtual environments. "
                        "For additional help, use with -h/--help option")
        subparsers = parser.add_subparsers(title="Operation commands",
                                           help='available commands',
                                           dest='command')
        subparsers.add_parser('list',
                              parents=[list_ips_parser, timestamps_parser],
                              help="Show virtual environments",
                              description="Show virtual environments on host")
        subparsers.add_parser('show', parents=[name_parser],
                              help="Show VMs in environment",
                              description="Show VMs in environment")
        subparsers.add_parser('erase', parents=[name_parser],
                              help="Delete environment",
                              description="Delete environment and VMs on it")
        subparsers.add_parser('start', parents=[name_parser],
                              help="Start VMs",
                              description="Start VMs in selected environment")
        subparsers.add_parser('destroy', parents=[name_parser],
                              help="Destroy(stop) VMs",
                              description="Stop VMs in selected environment")
        subparsers.add_parser('suspend', parents=[name_parser],
                              help="Suspend VMs",
                              description="Suspend VMs in selected "
                              "environment")
        subparsers.add_parser('resume', parents=[name_parser],
                              help="Resume VMs",
                              description="Resume VMs in selected environment")
        subparsers.add_parser('revert',
                              parents=[name_parser, snapshot_name_parser],
                              help="Apply snapshot to environment",
                              description="Apply selected snapshot to "
                              "environment")
        subparsers.add_parser('snapshot',
                              parents=[name_parser, snapshot_name_parser],
                              help="Make environment snapshot",
                              description="Make environment snapshot")
        subparsers.add_parser('sync',
                              help="Synchronization environment and devops",
                              description="Synchronization environment "
                              "and devops"),
        subparsers.add_parser('snapshot-list',
                              parents=[name_parser],
                              help="Show snapshots in environment",
                              description="Show snapshots in selected "
                              "environment")
        subparsers.add_parser('snapshot-delete',
                              parents=[name_parser, snapshot_name_parser],
                              help="Delete snapshot from environment",
                              description="Delete snapshot from selected "
                              "environment")
        subparsers.add_parser('net-list',
                              parents=[name_parser],
                              help="Show networks in environment",
                              description="Display allocated networks for "
                              "environment")
        subparsers.add_parser('time-sync',
                              parents=[name_parser, node_name_parser],
                              help="Sync time on all env nodes",
                              description="Sync time on all active nodes "
                                          "of environment starting from "
                                          "admin")
        subparsers.add_parser('revert-resume',
                              parents=[name_parser, snapshot_name_parser,
                                       node_name_parser, no_timesync_parser],
                              help="Revert, resume, sync time on VMs",
                              description="Revert and resume VMs in selected"
                                          "environment, then"
                                          " sync time on VMs")
        subparsers.add_parser('version',
                              help="Show devops version")
        subparsers.add_parser('create',
                              parents=[name_parser, vcpu_parser,
                                       node_count, ram_parser,
                                       net_pool, iso_path_parser,
                                       admin_disk_size_parser,
                                       admin_ram_parser,
                                       admin_vcpu_parser,
                                       second_disk_size,
                                       third_disk_size],
                              help="Create a new environment",
                              description="Create an environment with "
                              "the Fuel Master node and slaves"),
        subparsers.add_parser('slave-add',
                              parents=[name_parser, node_count,
                                       ram_parser, vcpu_parser,
                                       second_disk_size, third_disk_size],
                              help="Add a node",
                              description="Add a new node to environment")
        subparsers.add_parser('slave-change',
                              parents=[name_parser, node_name_parser,
                                       ram_parser, vcpu_parser],
                              help="Change node VCPU and memory config",
                              description="Change count of VCPUs and memory")
        subparsers.add_parser('slave-remove',
                              parents=[name_parser, node_name_parser],
                              help="Remove node from environment",
                              description="Remove selected node from "
                              "environment")
        subparsers.add_parser('admin-setup',
                              parents=[name_parser, admin_disk_size_parser],
                              help="Setup admin node",
                              description="Setup admin node from ISO")
        subparsers.add_parser('admin-change',
                              parents=[name_parser, admin_ram_parser,
                                       admin_vcpu_parser],
                              help="Change admin node VCPU and memory config",
                              description="Change count of VCPUs and memory "
                                          "for admin node")
        subparsers.add_parser('node-start',
                              parents=[name_parser, node_name_parser],
                              help="Start node in environment",
                              description="Start a separate node in "
                                          "environment")
        subparsers.add_parser('node-destroy',
                              parents=[name_parser, node_name_parser],
                              help="Destroy (power off) node in environment",
                              description="Destroy a separate node in "
                                          "environment")
        subparsers.add_parser('node-reset',
                              parents=[name_parser, node_name_parser],
                              help="Reset (restart) node in environment",
                              description="Reset a separate node in "
                                          "environment")
        if len(sys.argv) == 1:
            sys.argv.append("-h")
        return parser.parse_args()
