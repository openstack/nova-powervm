# Copyright 2015, 2018 IBM Corp.
#
# All Rights Reserved.
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

from __future__ import absolute_import

import fixtures
import mock
from nova import test
from oslo_utils.fixture import uuidsentinel
from pypowervm import const as pvm_const
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.wrappers import storage as pvm_stg
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.virt.powervm import media as m


class TestConfigDrivePowerVM(test.NoDBTestCase):
    """Unit Tests for the ConfigDrivePowerVM class."""

    def setUp(self):
        super(TestConfigDrivePowerVM, self).setUp()

        self.apt = self.useFixture(pvm_fx.AdapterFx()).adpt

        self.validate_vopt = self.useFixture(fixtures.MockPatch(
            'pypowervm.tasks.vopt.validate_vopt_repo_exists')).mock
        self.validate_vopt.return_value = None, None

    @mock.patch('nova.api.metadata.base.InstanceMetadata', autospec=True)
    @mock.patch('nova.virt.configdrive.ConfigDriveBuilder.make_drive',
                autospec=True)
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid', autospec=True)
    def test_crt_cfg_dr_iso(self, mock_pvm_uuid, mock_mkdrv, mock_meta):
        """Validates that the image creation method works."""
        cfg_dr_builder = m.ConfigDrivePowerVM(self.apt)
        self.assertTrue(self.validate_vopt.called)
        mock_instance = mock.MagicMock()
        mock_instance.uuid = '1e46bbfd-73b6-3c2a-aeab-a1d3f065e92f'
        mock_files = mock.MagicMock()
        mock_net = mock.MagicMock()
        iso_path = '/tmp/cfgdrv.iso'
        cfg_dr_builder._create_cfg_dr_iso(mock_instance, mock_files, mock_net,
                                          iso_path)
        self.assertTrue(mock_pvm_uuid.called)
        self.assertEqual(mock_mkdrv.call_count, 1)

        # Test retry iso create
        mock_mkdrv.reset_mock()
        mock_mkdrv.side_effect = [OSError, mock_mkdrv]
        cfg_dr_builder._create_cfg_dr_iso(mock_instance, mock_files, mock_net,
                                          iso_path)
        self.assertEqual(mock_mkdrv.call_count, 2)

    def test_get_cfg_drv_name(self):
        cfg_dr_builder = m.ConfigDrivePowerVM(self.apt)
        mock_instance = mock.MagicMock()
        mock_instance.uuid = uuidsentinel.inst_id

        # calculate expected file name
        expected_file_name = 'cfg_' + mock_instance.uuid.replace('-', '')
        allowed_len = pvm_const.MaxLen.VOPT_NAME - 4  # '.iso' is 4 chars
        expected_file_name = expected_file_name[:allowed_len] + '.iso'

        name = cfg_dr_builder.get_cfg_drv_name(mock_instance)
        self.assertEqual(name, expected_file_name)

    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                'get_cfg_drv_name')
    @mock.patch('tempfile.NamedTemporaryFile', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_attach_vopt')
    @mock.patch('os.path.getsize', autospec=True)
    @mock.patch('pypowervm.tasks.storage.upload_vopt', autospec=True)
    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                '_create_cfg_dr_iso', autospec=True)
    def test_crt_cfg_drv_vopt(self, mock_ccdi, mock_upl, mock_getsize,
                              mock_attach, mock_ntf, mock_name):
        # Mock Returns
        cfg_dr_builder = m.ConfigDrivePowerVM(self.apt)
        cfg_dr_builder.vios_uuid = 'vios_uuid'
        mock_instance = mock.MagicMock()
        mock_instance.uuid = uuidsentinel.inst_id
        mock_upl.return_value = 'vopt', 'f_uuid'
        fh = mock_ntf.return_value.__enter__.return_value
        fh.name = 'iso_path'
        mock_name.return_value = 'fake-name'

        # Run
        cfg_dr_builder.create_cfg_drv_vopt(mock_instance, 'files', 'netinfo',
                                           'fake_lpar', admin_pass='pass')
        mock_ntf.assert_called_once_with(mode='rb')
        mock_ccdi.assert_called_once_with(mock_instance,
                                          'files', 'netinfo', 'iso_path',
                                          admin_pass='pass')
        mock_getsize.assert_called_once_with('iso_path')
        mock_upl.assert_called_once_with(self.apt, 'vios_uuid', fh,
                                         'fake-name',
                                         mock_getsize.return_value)
        mock_attach.assert_called_once_with(mock_instance, 'fake_lpar',
                                            'vopt', None)

    @mock.patch('pypowervm.tasks.scsi_mapper.add_map', autospec=True)
    @mock.patch('pypowervm.tasks.scsi_mapper.build_vscsi_mapping',
                autospec=True)
    @mock.patch('pypowervm.utils.transaction.WrapperTask', autospec=True)
    def test_attach_vopt(self, mock_class_wrapper_task, mock_build_map,
                         mock_add_map):
        # Create objects to test with
        mock_instance = mock.MagicMock(name='fake-instance')
        cfg_dr_builder = m.ConfigDrivePowerVM(self.apt)
        vopt = mock.Mock()
        mock_vios = mock.Mock(spec=pvm_vios.VIOS)
        mock_vios.configure_mock(name='vios name')

        # Mock methods not currently under test
        mock_wrapper_task = mock.MagicMock()
        mock_class_wrapper_task.return_value = mock_wrapper_task

        def call_param(param):
            param(mock_vios)
        mock_wrapper_task.add_functor_subtask.side_effect = call_param

        def validate_build(host_uuid, vios_w, lpar_uuid, vopt_elem):
            self.assertEqual(None, host_uuid)
            self.assertIsInstance(vios_w, pvm_vios.VIOS)
            self.assertEqual('lpar_uuid', lpar_uuid)
            self.assertEqual(vopt, vopt_elem)
            return 'map'
        mock_build_map.side_effect = validate_build

        def validate_add(vios_w, mapping):
            self.assertIsInstance(vios_w, pvm_vios.VIOS)
            self.assertEqual(mapping, 'map')
            return 'added'
        mock_add_map.side_effect = validate_add

        # Run the actual test
        cfg_dr_builder._attach_vopt(mock_instance, 'lpar_uuid', vopt)

        # Make sure they were called and validated
        self.assertTrue(mock_wrapper_task.execute.called)
        self.assertEqual(1, mock_build_map.call_count)
        self.assertEqual(1, mock_add_map.call_count)
        self.assertTrue(self.validate_vopt.called)

    def test_sanitize_network_info(self):
        network_info = [{'type': 'lbr'}, {'type': 'pvm_sea'},
                        {'type': 'ovs'}]

        cfg_dr_builder = m.ConfigDrivePowerVM(self.apt)

        resp = cfg_dr_builder._sanitize_network_info(network_info)
        expected_ret = [{'type': 'vif'}, {'type': 'vif'},
                        {'type': 'ovs'}]
        self.assertEqual(resp, expected_ret)

    def test_mgmt_cna_to_vif(self):
        mock_cna = mock.MagicMock()
        mock_cna.mac = "FAD4433ED120"

        # Run
        cfg_dr_builder = m.ConfigDrivePowerVM(self.apt)
        vif = cfg_dr_builder._mgmt_cna_to_vif(mock_cna)

        # Validate
        self.assertEqual(vif.get('address'), "fa:d4:43:3e:d1:20")
        self.assertEqual(vif.get('id'), 'mgmt_vif')
        self.assertIsNotNone(vif.get('network'))
        self.assertEqual(1, len(vif.get('network').get('subnets')))
        subnet = vif.get('network').get('subnets')[0]
        self.assertEqual(6, subnet.get('version'))
        self.assertEqual('fe80::/64', subnet.get('cidr'))
        ip = subnet.get('ips')[0]
        self.assertEqual('fe80::f8d4:43ff:fe3e:d120', ip.get('address'))

    def test_mac_to_link_local(self):
        mac = 'fa:d4:43:3e:d1:20'
        self.assertEqual('fe80::f8d4:43ff:fe3e:d120',
                         m.ConfigDrivePowerVM._mac_to_link_local(mac))

        mac = '00:00:00:00:00:00'
        self.assertEqual('fe80::0200:00ff:fe00:0000',
                         m.ConfigDrivePowerVM._mac_to_link_local(mac))

        mac = 'ff:ff:ff:ff:ff:ff'
        self.assertEqual('fe80::fdff:ffff:feff:ffff',
                         m.ConfigDrivePowerVM._mac_to_link_local(mac))

    @mock.patch('nova_powervm.virt.powervm.media.ConfigDrivePowerVM.'
                'add_dlt_vopt_tasks')
    @mock.patch('pypowervm.wrappers.virtual_io_server.VIOS.wrap',
                new=mock.MagicMock())
    @mock.patch('pypowervm.tasks.scsi_mapper.find_maps')
    @mock.patch('pypowervm.utils.transaction.FeedTask')
    @mock.patch('pypowervm.utils.transaction.FeedTask.execute')
    def test_dlt_vopt_no_map(self, mock_execute, mock_class_feed_task,
                             mock_add_dlt_vopt_tasks, mock_find_maps):
        # Init objects to test with
        mock_feed_task = mock.MagicMock()
        mock_class_feed_task.return_value = mock_feed_task
        mock_find_maps.return_value = []

        # Invoke the operation
        cfg_dr = m.ConfigDrivePowerVM(self.apt)
        cfg_dr.dlt_vopt('2', remove_mappings=False)

        # Verify expected methods were called
        mock_add_dlt_vopt_tasks.assert_not_called()
        self.assertTrue(mock_feed_task.execute.called)

    @mock.patch('nova_powervm.virt.powervm.vm.get_vm_id', autospec=True)
    @mock.patch('pypowervm.tasks.scsi_mapper.gen_match_func', autospec=True)
    @mock.patch('pypowervm.tasks.scsi_mapper.find_maps', autospec=True)
    def test_add_dlt_vopt_tasks(self, mock_find_maps, mock_gen_match_func,
                                mock_vm_id):
        # Init objects to test with
        cfg_dr = m.ConfigDrivePowerVM(self.apt)
        stg_ftsk = mock.MagicMock()
        cfg_dr.vios_uuid = 'vios_uuid'
        lpar_uuid = 'lpar_uuid'
        mock_find_maps.return_value = [mock.Mock(backing_storage='stor')]
        mock_vm_id.return_value = '2'

        # Run
        cfg_dr.add_dlt_vopt_tasks(lpar_uuid, stg_ftsk)

        # Validate
        mock_gen_match_func.assert_called_with(pvm_stg.VOptMedia)
        mock_find_maps.assert_called_with(
            stg_ftsk.get_wrapper().scsi_mappings, client_lpar_id='2',
            match_func=mock_gen_match_func.return_value)
        self.assertTrue(stg_ftsk.add_post_execute.called)
        self.assertTrue(
            stg_ftsk.wrapper_tasks['vios_uuid'].add_functor_subtask.called)
