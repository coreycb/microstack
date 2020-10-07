"""questions.py

All of our subclasses of Question live here.

We might break this file up into multiple pieces at some point, but
for now, we're keeping things simple (if a big lengthy)

----------------------------------------------------------------------

Copyright 2020 Canonical Ltd

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

 http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

"""

import json
from time import sleep
from os import path

from init import shell
from init.shell import (check, call, check_output, sql, nc_wait, log_wait,
                        start, restart, download, disable, enable)
from init.config import Env, log
from init.questions.question import Question
from init.questions import clustering, network, uninstall  # noqa F401


_env = Env().get_env()


class ConfigError(Exception):
    """Suitable error to raise in case there is an issue with the snapctl
    config or environment vars.

    """


class Clustering(Question):
    """Possibly setup clustering."""

    _type = 'boolean'
    _question = 'Do you want to setup clustering?'
    config_key = 'config.clustered'
    interactive = True

    def yes(self, answer: bool):

        log.info('Configuring clustering ...')

        questions = [
            clustering.Role(),
            clustering.Password(),
            clustering.ControlIp(),
            clustering.ComputeIp(),  # Automagically skipped role='control'
        ]
        for question in questions:
            if not self.interactive:
                question.interactive = False
            question.ask()

        role = check_output('snapctl', 'get', 'config.cluster.role')
        control_ip = check_output('snapctl', 'get',
                                  'config.network.control-ip')
        password = check_output('snapctl', 'get', 'config.cluster.password')

        log.debug('Role: {}, IP: {}, Password: {}'.format(
            role, control_ip, password))

        # TODO: raise an exception if any of the above are None (can
        # happen if we're automatig and mess up our params.)

        if role == 'compute':
            log.info('I am a compute node.')
            # Gets config info and sets local env vals.
            check_output('microstack_join')

            # Set default question answers.
            check('snapctl', 'set', 'config.services.control-plane=false')
            check('snapctl', 'set', 'config.services.hypervisor=true')

        if role == 'control':
            log.info('I am a control node.')
            check('snapctl', 'set', 'config.services.control-plane=true')
            # We want to run a hypervisor on our control plane nodes
            # -- this is essentially a hyper converged cloud.
            check('snapctl', 'set', 'config.services.hypervisor=true')

        # TODO: if this is run after init has already been called,
        # need to restart services.

        # Write templates
        check('snap-openstack', 'setup')

    def no(self, answer: bool):
        # Turn off cluster server
        # TODO: it would be more secure to reverse this -- only enable
        # to service if we are doing clustering.
        disable('cluster-server')


class ConfigQuestion(Question):
    """Question class that simply asks for and sets a config value.

    All we need to do is run 'snap-openstack setup' after we have saved
    off the value. The value to be set is specified by the name of the
    question class.

    """
    def after(self, answer):
        """Our value has been saved.

        Run 'snap-openstack setup' to write it out, and load any changes to
        microstack.rc.

        # TODO this is a bit messy and redundant. Come up with a clean
        way of loading and writing config after the run of
        ConfigQuestions have been asked.

        """
        check('snap-openstack', 'setup')

        # TODO: get rid of this? (I think that it has become redundant)
        mstackrc = '{SNAP_COMMON}/etc/microstack.rc'.format(**_env)
        with open(mstackrc, 'r') as rc_file:
            for line in rc_file.readlines():
                if not line.startswith('export'):
                    continue
                key, val = line[7:].split('=')
                _env[key.strip()] = val.strip()


class DnsServers(ConfigQuestion):
    """Provide default DNS forwarders for MicroStack to use."""

    _type = 'string'
    _question = 'Upstream DNS servers to be used by instances (VMs)'
    config_key = 'config.network.dns-servers'

    def yes(self, answer: str):
        # Neutron is not actually started at this point, so we don't
        # need to restart.
        # TODO: This isn't idempotent, because it will behave
        # differently if we re-run this script when neutron *is*
        # started. Need to figure that out.
        pass


class DnsDomain(ConfigQuestion):
    """An internal DNS domain to be used for ML2 DNS."""

    _type = 'string'
    _question = 'An internal DNS domain to be used for ML2 DNS'
    config_key = 'config.network.dns-domain'

    def yes(self, answer: str):
        # Neutron is not actually started at this point, so we don't
        # need to restart.
        # TODO: This isn't idempotent, because it will behave
        # differently if we re-run this script when neutron *is*
        # started. Need to figure that out.
        pass


class NetworkSettings(Question):
    """Write network settings, and """
    _type = 'auto'
    _question = 'Network settings'

    def yes(self, answer):
        log.info('Configuring networking ...')

        role = check_output('snapctl', 'get', 'config.cluster.role')

        # Enable and start the services.
        enable('ovsdb-server')
        enable('ovs-vswitchd')
        enable('ovn-ovsdb-server-sb')
        enable('ovn-ovsdb-server-nb')

        network.ExtGateway().ask()
        network.ExtCidr().ask()

        control_ip = check_output('snapctl', 'get',
                                  'config.network.control-ip')
        if role == 'control':
            nb_conn = 'unix:{SNAP_COMMON}/run/ovn/ovnnb_db.sock'.format(**_env)
            sb_conn = 'unix:{SNAP_COMMON}/run/ovn/ovnsb_db.sock'.format(**_env)
            check_output('ovs-vsctl', 'set', 'open', '.',
                         f'external-ids:ovn-encap-ip={control_ip}')
        elif role == 'compute':
            sb_conn = f'tcp:{control_ip}:6642'
            # Not used by any compute node services.
            nb_conn = ''
            compute_ip = check_output('snapctl', 'get',
                                      'config.network.compute-ip')
            # Set the IP address to be used for a tunnel endpoint.
            check_output('ovs-vsctl', 'set', 'open', '.',
                         f'external-ids:ovn-encap-ip={compute_ip}')
        else:
            raise Exception(f'Unexpected node role: {role}')

        # ovn-controller does not start unless both the ovn-encap-ip and the
        # ovn-encap-type are set.
        check_output('ovs-vsctl', 'set', 'open', '.',
                     'external-ids:ovn-encap-type=geneve')

        # Configure OVN SB and NB sockets based on the role node. For
        # single-node deployments there is no need to use a TCP socket.
        check_output('snapctl', 'set',
                     f'config.network.ovn-nb-connection={nb_conn}')
        check_output('snapctl', 'set',
                     f'config.network.ovn-sb-connection={sb_conn}')

        # Set SB database connection details for ovn-controller to pick up.
        check_output(
                'ovs-vsctl', 'set', 'open', '.',
                f'external-ids:ovn-remote={sb_conn}'
        )
        check_output(
                'ovs-vsctl', 'set', 'open', '.',
                'external-ids:ovn-cms-options=enable-chassis-as-gw'
        )

        # Now that we have default or overriden values, setup the
        # bridge and write all the proper values into our config
        # files.
        check('setup-br-ex')
        check('snap-openstack', 'setup')

        if role == 'control':

            enable('ovn-northd')
            enable('ovn-controller')

        network.IpForwarding().ask()


class OsPassword(ConfigQuestion):
    _type = 'string'
    _question = 'Openstack Admin Password'
    config_key = 'config.credentials.keystone-password'

    def yes(self, answer):
        _env['keystone_password'] = answer


class ForceQemu(Question):
    _type = 'boolean'
    config_key = 'config.host.check-qemu'

    def yes(self, answer: str) -> None:
        """Possibly force us to use qemu emulation rather than kvm."""

        cpuinfo = check_output('cat', '/proc/cpuinfo')
        if 'vmx' in cpuinfo or 'svm' in cpuinfo:
            # We have processor extensions installed. No need to Force
            # Qemu emulation.
            return

        _path = '{SNAP_COMMON}/etc/nova/nova.conf.d/hypervisor.conf'.format(
            **_env)

        with open(_path, 'w') as _file:
            _file.write("""\
[DEFAULT]
compute_driver = libvirt.LibvirtDriver

[workarounds]
disable_rootwrap = True

[libvirt]
virt_type = qemu
cpu_mode = host-model
""")

        # TODO: restart nova services when re-running this after init.


class VmSwappiness(Question):

    _type = 'boolean'
    _question = 'Do you wish to set vm swappiness to 1? (recommended)'

    def yes(self, answer: str) -> None:
        # TODO
        pass


class FileHandleLimits(Question):

    _type = 'boolean'
    _question = 'Do you wish to increase file handle limits? (recommended)'

    def yes(self, answer: str) -> None:
        # TODO
        pass


class DashboardAccess(ConfigQuestion):

    _type = 'string'
    _question = 'Dashboard allowed hosts.'
    config_key = 'config.network.dashboard-allowed-hosts'

    def yes(self, answer):
        log.info("Opening horizon dashboard up to {hosts}".format(
            hosts=answer))


class RabbitMq(Question):
    """Wait for Rabbit to start, then setup permissions."""

    _type = 'boolean'
    config_key = 'config.services.control-plane'

    def _wait(self) -> None:
        restart('rabbitmq-server')  # Restart server for plugs
        rabbit_port = check_output(
            'snapctl', 'get', 'config.network.ports.rabbit')
        nc_wait(_env['control_ip'], rabbit_port)
        log_file = '{SNAP_COMMON}/log/rabbitmq/startup_log'.format(**_env)
        log_wait(log_file, 'completed')

    def _configure(self) -> None:
        """Configure RabbitMQ

        (actions may have already been run, in which case we fail silently).
        """
        # Configure RabbitMQ
        check('{SNAP}/bin/setup-rabbit'.format(**_env))

    def yes(self, answer: str) -> None:
        log.info('Waiting for RabbitMQ to start ...')
        self._wait()
        log.info('RabbitMQ started!')
        log.info('Configuring RabbitMQ ...')
        self._configure()
        log.info('RabbitMQ Configured!')

    def no(self, answer: str):
        log.info('Disabling local rabbit ...')
        disable('rabbitmq-server')


class DatabaseSetup(Question):
    """Setup keystone permissions, then setup all databases."""

    _type = 'boolean'
    config_key = 'config.services.control-plane'

    def _wait(self) -> None:
        mysql_port = check_output(
            'snapctl', 'get', 'config.network.ports.mysql')
        nc_wait(_env['control_ip'], mysql_port)
        log_wait('{SNAP_COMMON}/log/mysql/error.log'.format(**_env),
                 'mysqld: ready for connections.')

    def _create_dbs(self) -> None:
        db_creds = shell.config_get('config.credentials')
        for service_user, db_name in (
            ('neutron', 'neutron'),
            ('nova', 'nova'),
            ('nova', 'nova_api'),
            ('nova', 'nova_cell0'),
            ('cinder', 'cinder'),
            ('glance', 'glance'),
            ('keystone', 'keystone'),
            ('placement', 'placement')
        ):
            db_password = db_creds[f'{service_user}-password']
            sql("CREATE USER IF NOT EXISTS '{user}'@'{control_ip}'"
                " IDENTIFIED BY '{db_password}';".format(
                    user=service_user, db_password=db_password, **_env))
            sql("CREATE DATABASE IF NOT EXISTS `{db}`;".format(db=db_name))
            sql("GRANT ALL PRIVILEGES ON {db}.* TO '{user}'@'{control_ip}';"
                "".format(db=db_name, user=service_user, **_env))

    def _bootstrap(self) -> None:

        if call('openstack', 'user', 'show', 'admin'):
            return

        bootstrap_url = 'http://{control_ip}:5000/v3/'.format(**_env)

        check('snap-openstack', 'launch', 'keystone-manage', 'bootstrap',
              '--bootstrap-password', _env['keystone_password'],
              '--bootstrap-admin-url', bootstrap_url,
              '--bootstrap-internal-url', bootstrap_url,
              '--bootstrap-public-url', bootstrap_url,
              '--bootstrap-region-id', 'microstack')

    def yes(self, answer: str) -> None:
        """Setup Databases.

        Create all the MySQL databases we require, then setup the
        fernet keys and create the service project.

        """
        log.info('Waiting for MySQL server to start ...')
        self._wait()
        log.info('Mysql server started! Creating databases ...')
        self._create_dbs()

        check('snapctl', 'set', 'database.ready=true')

        # Start keystone-uwsgi. We use snapctl, because systemd
        # doesn't yet know about the service.
        start('nginx')
        start('keystone-uwsgi')

        log.info('Configuring Keystone Fernet Keys ...')
        check('snap-openstack', 'launch', 'keystone-manage',
              'fernet_setup', '--keystone-user', 'root',
              '--keystone-group', 'root')
        check('snap-openstack', 'launch', 'keystone-manage', 'db_sync')

        restart('keystone-uwsgi')

        log.info('Bootstrapping Keystone ...')
        self._bootstrap()

        log.info('Creating service project ...')
        if not call('openstack', 'project', 'show', 'service'):
            check('openstack', 'project', 'create', '--domain',
                  'default', '--description', 'Service Project',
                  'service')

        log.info('Keystone configured!')

    def no(self, answer: str):
        # We assume that the control node has a connection setup for us.
        check('snapctl', 'set', 'database.ready=true')

        log.info('Disabling local MySQL ...')
        disable('mysqld')


class NovaHypervisor(Question):
    """Run the nova compute hypervisor."""

    _type = 'boolean'
    config_key = 'config.services.hypervisor'

    def yes(self, answer):
        log.info('Configuring nova compute hypervisor ...')
        start('nova-compute')

    def no(self, answer):
        log.info('Disabling nova compute service ...')
        disable('nova-compute')


class NovaSpiceConsoleSetup(Question):
    """Run the Spice HTML5 console proxy service"""

    _type = 'boolean'
    config_key = 'config.services.spice-console'

    def yes(self, answer):
        log.info('Configuring the Spice HTML5 console service...')
        start('nova-spicehtml5proxy')

    def no(self, answer):
        log.info('Disabling nova compute service ...')
        disable('nova-spicehtml5proxy')


class PlacementSetup(Question):
    """Setup Placement services."""

    _type = 'boolean'
    config_key = 'config.services.control-plane'

    def yes(self, answer: str) -> None:
        log.info('Configuring the Placement service...')

        if not call('openstack', 'user', 'show', 'placement'):
            check(
                'openstack', 'user', 'create', '--domain', 'default',
                '--password',
                shell.config_get('config.credentials.placement-password'),
                'placement',
            )
            check('openstack', 'role', 'add', '--project', 'service',
                  '--user', 'placement', 'admin')

        if not call('openstack', 'service', 'show', 'placement'):
            check('openstack', 'service', 'create', '--name',
                  'placement', '--description', '"Placement API"',
                  'placement')

            for endpoint in ['public', 'internal', 'admin']:
                call('openstack', 'endpoint', 'create', '--region',
                     'microstack', 'placement', endpoint,
                     'http://{control_ip}:8778'.format(**_env))

        start('placement-uwsgi')

        log.info('Running Placement DB migrations...')
        check('snap-openstack', 'launch', 'placement-manage', 'db', 'sync')

        restart('placement-uwsgi')

    def no(self, answer):
        log.info('Disabling the Placement service...')
        disable('placement-uwsgi')


class NovaControlPlane(Question):
    """Create all control plane nova users and services."""

    _type = 'boolean'
    config_key = 'config.services.control-plane'

    def _flavors(self) -> None:
        """Create default flavors."""

        if not call('openstack', 'flavor', 'show', 'm1.tiny'):
            check('openstack', 'flavor', 'create', '--id', '1',
                  '--ram', '512', '--disk', '1', '--vcpus', '1', 'm1.tiny')
        if not call('openstack', 'flavor', 'show', 'm1.small'):
            check('openstack', 'flavor', 'create', '--id', '2',
                  '--ram', '2048', '--disk', '20', '--vcpus', '1', 'm1.small')
        if not call('openstack', 'flavor', 'show', 'm1.medium'):
            check('openstack', 'flavor', 'create', '--id', '3',
                  '--ram', '4096', '--disk', '20', '--vcpus', '2', 'm1.medium')
        if not call('openstack', 'flavor', 'show', 'm1.large'):
            check('openstack', 'flavor', 'create', '--id', '4',
                  '--ram', '8192', '--disk', '20', '--vcpus', '4', 'm1.large')
        if not call('openstack', 'flavor', 'show', 'm1.xlarge'):
            check('openstack', 'flavor', 'create', '--id', '5',
                  '--ram', '16384', '--disk', '20', '--vcpus', '8',
                  'm1.xlarge')

    def yes(self, answer: str) -> None:
        log.info('Configuring nova control plane services ...')

        if not call('openstack', 'user', 'show', 'nova'):
            check(
                'openstack', 'user', 'create', '--domain', 'default',
                '--password',
                shell.config_get('config.credentials.nova-password'),
                'nova'
            )
            check('openstack', 'role', 'add', '--project',
                  'service', '--user', 'nova', 'admin')

        # Use snapctl to start nova services.  We need to call them
        # out manually, because systemd doesn't know about them yet.
        # TODO: parse the output of `snapctl services` to get this
        # list automagically.
        start('nova-api')

        log.info('Running Nova API DB migrations'
                 ' (this may take a lot of time)...')
        check('snap-openstack', 'launch', 'nova-manage', 'api_db', 'sync')

        if 'cell0' not in check_output('snap-openstack', 'launch',
                                       'nova-manage', 'cell_v2',
                                       'list_cells'):
            check('snap-openstack', 'launch', 'nova-manage',
                  'cell_v2', 'map_cell0')

        if 'cell1' not in check_output('snap-openstack', 'launch',
                                       'nova-manage', 'cell_v2', 'list_cells'):

            check('snap-openstack', 'launch', 'nova-manage', 'cell_v2',
                  'create_cell', '--name=cell1', '--verbose')

        log.info('Running Nova DB migrations'
                 ' (this may take a lot of time)...')
        check('snap-openstack', 'launch', 'nova-manage', 'db', 'sync')

        restart('nova-api')
        restart('nova-compute')

        for service in [
                'nova-api-metadata',
                'nova-conductor',
                'nova-scheduler',
        ]:
            start(service)

        nc_wait(_env['compute_ip'], '8774')

        sleep(5)  # TODO: log_wait

        if not call('openstack', 'service', 'show', 'compute'):
            check('openstack', 'service', 'create', '--name', 'nova',
                  '--description', '"Openstack Compute"', 'compute')
            for endpoint in ['public', 'internal', 'admin']:
                call('openstack', 'endpoint', 'create', '--region',
                     'microstack', 'compute', endpoint,
                     'http://{control_ip}:8774/v2.1'.format(**_env))

        log.info('Creating default flavors...')

        self._flavors()

    def no(self, answer):
        log.info('Disabling nova control plane services ...')

        for service in [
                'nova-api',
                'nova-conductor',
                'nova-scheduler',
                'nova-api-metadata']:
            disable(service)


class CinderSetup(Question):
    """Setup Placement services."""

    _type = 'boolean'
    config_key = 'config.services.control-plane'

    def yes(self, answer: str) -> None:
        log.info('Configuring the Cinder services...')

        if not call('openstack', 'user', 'show', 'cinder'):
            check(
                'openstack', 'user', 'create', '--domain', 'default',
                '--password',
                shell.config_get('config.credentials.cinder-password'),
                'cinder'
            )
            check('openstack', 'role', 'add', '--project', 'service',
                  '--user', 'cinder', 'admin')

        control_ip = _env['control_ip']
        for endpoint in ['public', 'internal', 'admin']:
            for api_version in ['v2', 'v3']:
                if not call('openstack', 'service', 'show',
                            f'cinder{api_version}'):
                    check('openstack', 'service', 'create', '--name',
                          f'cinder{api_version}', '--description',
                          f'"Cinder {api_version} API"',
                          f'volume{api_version}')
                if not check_output(
                        'openstack', 'endpoint', 'list',
                        '--service', f'volume{api_version}', '--interface',
                        endpoint):
                    check(
                            'openstack', 'endpoint', 'create', '--region',
                            'microstack', f'volume{api_version}', endpoint,
                            f'http://{control_ip}:8776/{api_version}/'
                            '$(project_id)s'
                    )
        restart('cinder-uwsgi')

        log.info('Running Cinder DB migrations...')
        check('snap-openstack', 'launch', 'cinder-manage', 'db', 'sync')

        restart('cinder-uwsgi')
        restart('cinder-scheduler')

    def no(self, answer):
        log.info('Disabling Cinder services...')

        for service in [
                'cinder-uwsgi',
                'cinder-scheduler',
                'cinder-volume',
                'cinder-backup']:
            disable(service)


class CinderVolumeLVMSetup(Question):
    """Setup cinder-volume with LVM."""

    _type = 'boolean'
    config_key = 'config.cinder.setup-loop-based-cinder-lvm-backend'
    _question = ('(experimental) Do you want to setup a loop device-backed LVM'
                 ' volume backend for Cinder?')
    interactive = True

    def yes(self, answer: bool) -> None:
        check('snapctl', 'set',
              f'config.cinder.setup-loop-based-cinder-lvm-backend'
              f'={str(answer).lower()}')
        log.info('Setting up cinder-volume service with the LVM backend...')
        enable('setup-lvm-loopdev')
        enable('cinder-volume')
        enable('target')
        enable('iscsid')

    def no(self, answer: bool) -> None:
        check('snapctl', 'set', f'config.cinder.lvm.setup-file-backed-lvm='
                                f'{str(answer).lower()}')
        disable('setup-lvm-loopdev')
        disable('cinder-volume')
        disable('iscsid')
        disable('target')


class NeutronControlPlane(Question):
    """Create all relevant neutron services and users."""

    _type = 'boolean'
    config_key = 'config.services.control-plane'

    def yes(self, answer: str) -> None:
        log.info('Configuring Neutron')

        if not call('openstack', 'user', 'show', 'neutron'):
            check(
                'openstack', 'user', 'create', '--domain', 'default',
                '--password',
                shell.config_get('config.credentials.neutron-password'),
                'neutron')
            check('openstack', 'role', 'add', '--project', 'service',
                  '--user', 'neutron', 'admin')

        if not call('openstack', 'service', 'show', 'network'):
            check('openstack', 'service', 'create', '--name', 'neutron',
                  '--description', '"OpenStack Network"', 'network')
            for endpoint in ['public', 'internal', 'admin']:
                call('openstack', 'endpoint', 'create', '--region',
                     'microstack', 'network', endpoint,
                     'http://{control_ip}:9696'.format(**_env))

        start('neutron-api')

        check('snap-openstack', 'launch', 'neutron-db-manage', 'upgrade',
              'head')

        for service in [
                'neutron-api',
                'neutron-ovn-metadata-agent',
        ]:
            restart(service)

        nc_wait(_env['control_ip'], '9696')

        sleep(5)  # TODO: log_wait

        if not call('openstack', 'network', 'show', 'test'):
            check('openstack', 'network', 'create', 'test')

        if not call('openstack', 'subnet', 'show', 'test-subnet'):
            check('openstack', 'subnet', 'create', '--network', 'test',
                  '--subnet-range', '192.168.222.0/24', 'test-subnet')

        if not call('openstack', 'network', 'show', 'external'):
            check('openstack', 'network', 'create', '--external',
                  '--provider-physical-network=physnet1',
                  '--provider-network-type=flat', 'external')
        if not call('openstack', 'subnet', 'show', 'external-subnet'):
            check('openstack', 'subnet', 'create', '--network', 'external',
                  '--subnet-range', _env['extcidr'], '--no-dhcp',
                  'external-subnet')

        if not call('openstack', 'router', 'show', 'test-router'):
            check('openstack', 'router', 'create', 'test-router')
            check('openstack', 'router', 'add', 'subnet', 'test-router',
                  'test-subnet')
            check('openstack', 'router', 'set', '--external-gateway',
                  'external', 'test-router')

    def no(self, answer):
        """Create endpoints pointed at control node if we're not setting up
        neutron on this machine.

        """
        # Make sure the necessary services are enabled and started.
        for service in [
                'ovs-vswitchd',
                'ovsdb-server',
                'ovn-controller',
                'neutron-ovn-metadata-agent'
        ]:
            enable(service)

        # Disable the other services.
        for service in [
                'neutron-api',
                'ovn-northd',
                'ovn-ovsdb-server-sb',
                'ovn-ovsdb-server-nb',
        ]:
            disable(service)


class GlanceSetup(Question):
    """Setup glance, and download an initial Cirros image."""

    _type = 'boolean'
    config_key = 'config.services.control-plane'

    def _fetch_cirros(self) -> None:

        if call('openstack', 'image', 'show', 'cirros'):
            return

        log.info('Adding cirros image ...')

        env = dict(**_env)
        env['VER'] = '0.4.0'
        env['IMG'] = 'cirros-{VER}-x86_64-disk.img'.format(**env)

        cirros_path = '{SNAP_COMMON}/images/{IMG}'.format(**env)

        if not path.exists(cirros_path):
            check('mkdir', '-p', '{SNAP_COMMON}/images'.format(**env))
            log.info('Downloading cirros image ...')
            download(
                'http://download.cirros-cloud.net/{VER}/{IMG}'.format(**env),
                '{SNAP_COMMON}/images/{IMG}'.format(**env))

        check('openstack', 'image', 'create', '--file',
              '{SNAP_COMMON}/images/{IMG}'.format(**env),
              '--public', '--container-format=bare',
              '--disk-format=qcow2', 'cirros')

    def yes(self, answer: str) -> None:

        log.info('Configuring Glance ...')

        if not call('openstack', 'user', 'show', 'glance'):
            check(
                'openstack', 'user', 'create', '--domain', 'default',
                '--password',
                shell.config_get('config.credentials.glance-password'),
                'glance'
            )
            check('openstack', 'role', 'add', '--project', 'service',
                  '--user', 'glance', 'admin')

        if not call('openstack', 'service', 'show', 'image'):
            check('openstack', 'service', 'create', '--name', 'glance',
                  '--description', '"OpenStack Image"', 'image')
            for endpoint in ['internal', 'admin', 'public']:
                check('openstack', 'endpoint', 'create', '--region',
                      'microstack', 'image', endpoint,
                      'http://{compute_ip}:9292'.format(**_env))

        for service in [
                'glance-api',
                'registry',  # TODO rename to glance-registery
        ]:
            start(service)

        check('snap-openstack', 'launch', 'glance-manage', 'db_sync')

        restart('glance-api')
        restart('registry')

        nc_wait(_env['compute_ip'], '9292')

        sleep(5)  # TODO: log_wait

        self._fetch_cirros()

    def no(self, answer):
        disable('glance-api')
        disable('registry')


class SecurityRules(Question):
    """Setup default security rules."""

    _type = 'boolean'
    config_key = 'config.network.security-rules'

    def yes(self, answer: str) -> None:
        # Create security group rules
        log.info('Creating security group rules ...')
        group_id = check_output('openstack', 'security', 'group', 'list',
                                '--project', 'admin', '-f', 'value',
                                '-c', 'ID')
        rules = check_output('openstack', 'security', 'group', 'rule', 'list',
                             '--format', 'json')
        ping_rule = False
        ssh_rule = False

        for rule in json.loads(rules):
            if rule['Security Group'] == group_id:
                if rule['IP Protocol'] == 'icmp':
                    ping_rule = True
                if rule['IP Protocol'] == 'tcp':
                    ssh_rule = True

        if not ping_rule:
            check('openstack', 'security', 'group', 'rule', 'create',
                  group_id, '--proto', 'icmp')
        if not ssh_rule:
            check('openstack', 'security', 'group', 'rule', 'create',
                  group_id, '--proto', 'tcp', '--dst-port', '22')


class PostSetup(Question):
    """Sneak in any additional cleanup, then set the initialized state."""

    config_key = 'config.post-setup'

    def yes(self, answer: str) -> None:

        log.info('restarting libvirt and virtlogd ...')
        # This fixes an issue w/ logging not getting set.
        # TODO: fix issue.
        restart('libvirtd')
        restart('virtlogd')
        restart('nova-compute')

        restart('horizon-uwsgi')

        check('snapctl', 'set', 'initialized=true')
        log.info('Complete. Marked microstack as initialized!')


class SimpleServiceQuestion(Question):

    def yes(self, answer: str) -> None:
        log.info('enabling and starting ' + self.__class__.__name__)

        for service in self.services:
            enable(service)

        log.info(self.__class__.__name__ + ' enabled')

    def no(self, answer):
        for service in self.services:
            disable(service)


class ExtraServicesQuestion(Question):

    _type = 'boolean'
    _question = 'Do you want to setup extra services?'
    config_key = 'config.services.extra.enabled'
    interactive = True

    def yes(self, answer: bool):
        questions = [
            Filebeat(),
            Telegraf(),
            Nrpe(),
        ]

        for question in questions:
            if not self.interactive:
                question.interactive = False
            question.ask()

    def no(self, answer: bool):
        pass


class Filebeat(SimpleServiceQuestion):
    _type = 'boolean'
    _question = 'Do you want to enable Filebeat?'
    config_key = 'config.services.extra.filebeat'
    interactive = True

    @property
    def services(self):
        return [
            '{SNAP_INSTANCE_NAME}.filebeat'.format(**_env)
        ]


class Telegraf(SimpleServiceQuestion):
    _type = 'boolean'
    _question = 'Do you want to enable Telegraf?'
    config_key = 'config.services.extra.telegraf'
    interactive = True

    @property
    def services(self):
        return [
            '{SNAP_INSTANCE_NAME}.telegraf'.format(**_env)
        ]


class Nrpe(SimpleServiceQuestion):
    _type = 'boolean'
    _question = 'Do you want to enable NRPE?'
    config_key = 'config.services.extra.nrpe'
    interactive = True

    @property
    def services(self):
        return [
            '{SNAP_INSTANCE_NAME}.nrpe'.format(**_env)
        ]
