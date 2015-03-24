# Copyright 2015 IBM Corp.
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

import fixtures
import mock
from oslo_config import cfg

from nova import test
import os
import pypowervm.adapter as pvm_adp
from pypowervm.tests.wrappers.util import pvmhttp
from pypowervm.wrappers import storage as pvm_stg

from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm.disk import ssp


SSP = 'fake_ssp.txt'


class SSPFixture(fixtures.Fixture):
    """Patch out PyPowerVM SSP EntryWrapper search and refresh."""

    def __init__(self):
        pass

    def setUp(self):
        super(SSPFixture, self).setUp()
        self._search_patcher = mock.patch(
            'pypowervm.wrappers.storage.SSP.search')
        self._refresh_patcher = mock.patch(
            'pypowervm.wrappers.storage.SSP.refresh')
        self.mock_search = self._search_patcher.start()
        self.mock_refresh = self._refresh_patcher.start()

        self.addCleanup(self._search_patcher.stop)
        self.addCleanup(self._refresh_patcher.stop)


class TestSSPDiskAdapter(test.TestCase):
    """Unit Tests for the LocalDisk storage driver."""

    def setUp(self):
        super(TestSSPDiskAdapter, self).setUp()

        # Find directory for response file(s)
        data_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(data_dir, "..", 'data')

        def resp(file_name):
            file_path = os.path.join(data_dir, file_name)
            return pvmhttp.load_pvm_resp(file_path).get_response()

        self.ssp_resp = resp(SSP)
        self.pypvm = self.useFixture(fx.PyPowerVM())
        self.apt = self.pypvm.apt

        self.sspfx = self.useFixture(SSPFixture())

        self.mock_search = self.sspfx.mock_search
        # EntryWrapper.search always returns a feed.
        self.mock_search.return_value = self._bld_resp(
            entry_or_list=[self.ssp_resp.entry])

        self.mock_refresh = self.sspfx.mock_refresh
        self.mock_refresh.return_value = pvm_stg.SSP.wrap(self.ssp_resp)

        # By default, assume the config supplied an SSP name
        cfg.CONF.set_override('ssp_name', 'ssp1')

    def _get_ssp_stor(self):
        with mock.patch('nova_powervm.virt.powervm.vios.get_vios_name_map') as\
                mock_vio_name_map:
            mock_vio_name_map.return_value = {'vio_name': 'vio_uuid'}
            ssp_stor = ssp.SSPDiskAdapter({'adapter': self.apt,
                                           'host_uuid': 'host_uuid'})
        return ssp_stor

    def _bld_resp(self, status=200, entry_or_list=None):
        """Build a pypowervm.adapter.Response for mocking Adapter.search/read.

        :param status: HTTP status of the Response.
        :param entry_or_list: pypowervm.adapter.Entry or list thereof.  If
                              None, the Response has no content.  If an Entry,
                              the Response looks like it got back <entry/>.  If
                              a list of Entry, the Response looks like it got
                              back <feed/>.
        :return: pypowervm.adapter.Response suitable for mocking
                 pypowervm.adapter.Adapter.search or read.
        """
        resp = pvm_adp.Response('meth', 'path', status, 'reason', {})
        resp.entry = None
        resp.feed = None
        if entry_or_list is None:
            resp.feed = pvm_adp.Feed({}, [])
        else:
            if isinstance(entry_or_list, list):
                resp.feed = pvm_adp.Feed({}, entry_or_list)
            else:
                resp.entry = entry_or_list
        return resp

    def test_init_green_with_config(self):
        """Bootstrap SSPStorage, testing first call to _fetch_ssp_wrap.

        Driver init should search for SSP by name.
        """
        # Invoke __init__ => initial _fetch_ssp_wrap()
        self._get_ssp_stor()
        # Init should call _fetch_ssp_wrap() once.  First _fetch_ssp_wrap()
        # WITH a configured name does a search, but not a read.
        # Refresh shouldn't be invoked.
        self.assertEqual(1, self.mock_search.call_count)
        self.assertEqual(0, self.apt.read.call_count)
        self.assertEqual(0, self.mock_refresh.call_count)

    def test_init_green_no_config(self):
        """No SSP name specified in config; one SSP on host - success."""
        cfg.CONF.clear_override('ssp_name')
        self.apt.read.return_value = self._bld_resp(
            entry_or_list=[self.ssp_resp.entry])
        self._get_ssp_stor()
        # Init should call _fetch_ssp_wrap() once.  First _fetch_ssp_wrap()
        # WITHOUT a configured name does a feed GET (read), not a search.
        # Refresh shouldn't be invoked.
        self.assertEqual(0, self.mock_search.call_count)
        self.assertEqual(1, self.apt.read.call_count)
        self.assertEqual(0, self.mock_refresh.call_count)
        self.apt.read.assert_called_with('SharedStoragePool')

    def test_init_SSPNotFoundByName(self):
        """Empty feed comes back from search - no SSP by that name."""
        self.mock_search.return_value = self._bld_resp(status=204)
        self.assertRaises(ssp.SSPNotFoundByName, self._get_ssp_stor)

    def test_init_TooManySSPsFound(self):
        """Search-by-name returns more than one result."""
        ssp1 = pvm_stg.SSP.bld('newssp1', [])
        ssp2 = pvm_stg.SSP.bld('newssp2', [])
        self.mock_search.return_value = self._bld_resp(
            entry_or_list=[ssp1.entry, ssp2.entry])
        self.assertRaises(ssp.TooManySSPsFound, self._get_ssp_stor)

    def test_init_NoConfigNoSSPFound(self):
        """No SSP name specified in config, no SSPs on host."""
        cfg.CONF.clear_override('ssp_name')
        self.apt.read.return_value = self._bld_resp(status=204)
        self.assertRaises(ssp.NoConfigNoSSPFound, self._get_ssp_stor)

    def test_init_NoConfigTooManySSPs(self):
        """No SSP name specified in config, more than one SSP on host."""
        cfg.CONF.clear_override('ssp_name')
        ssp1 = pvm_stg.SSP.bld('newssp1', [])
        ssp2 = pvm_stg.SSP.bld('newssp2', [])
        self.apt.read.return_value = self._bld_resp(
            entry_or_list=[ssp1.entry, ssp2.entry])
        self.assertRaises(ssp.NoConfigTooManySSPs, self._get_ssp_stor)

    def test_fetch_ssp_wrap_refresh(self):
        """_fetch_ssp_wrap with cached wrapper."""
        # Save original SSP wrapper for later comparison
        orig_ssp_wrap = pvm_stg.SSP.wrap(self.ssp_resp)
        # Prime _ssp_wrap
        ssp_stor = self._get_ssp_stor()
        # Verify baseline call counts
        self.assertEqual(1, self.mock_search.call_count)
        self.assertEqual(0, self.mock_refresh.call_count)
        ssp_wrap = ssp_stor._fetch_ssp_wrap()
        # This second _fetch_ssp_wrap() should call refresh, not read or search
        self.assertEqual(1, self.mock_search.call_count)
        self.assertEqual(0, self.apt.read.call_count)
        self.assertEqual(1, self.mock_refresh.call_count)
        self.assertEqual(ssp_wrap.name, orig_ssp_wrap.name)

    def test_capacity(self):
        ssp_stor = self._get_ssp_stor()
        self.assertEqual(49.88, ssp_stor.capacity)

    def test_capacity_used(self):
        ssp_stor = self._get_ssp_stor()
        self.assertEqual((49.88 - 48.98), ssp_stor.capacity_used)