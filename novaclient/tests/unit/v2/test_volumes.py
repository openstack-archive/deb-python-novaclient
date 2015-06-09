# Copyright 2013 IBM Corp.
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

import warnings

import mock

from novaclient.tests.unit import utils
from novaclient.tests.unit.v2 import fakes
from novaclient.v2 import volumes


cs = fakes.FakeClient()


class VolumesTest(utils.TestCase):

    @mock.patch.object(warnings, 'warn')
    def test_list_volumes(self, mock_warn):
        vl = cs.volumes.list()
        cs.assert_called('GET', '/volumes/detail')
        for v in vl:
            self.assertIsInstance(v, volumes.Volume)
        self.assertEqual(1, mock_warn.call_count)

    @mock.patch.object(warnings, 'warn')
    def test_list_volumes_undetailed(self, mock_warn):
        vl = cs.volumes.list(detailed=False)
        cs.assert_called('GET', '/volumes')
        for v in vl:
            self.assertIsInstance(v, volumes.Volume)
        self.assertEqual(1, mock_warn.call_count)

    @mock.patch.object(warnings, 'warn')
    def test_get_volume_details(self, mock_warn):
        vol_id = '15e59938-07d5-11e1-90e3-e3dffe0c5983'
        v = cs.volumes.get(vol_id)
        cs.assert_called('GET', '/volumes/%s' % vol_id)
        self.assertIsInstance(v, volumes.Volume)
        self.assertEqual(v.id, vol_id)
        self.assertEqual(1, mock_warn.call_count)

    @mock.patch.object(warnings, 'warn')
    def test_create_volume(self, mock_warn):
        v = cs.volumes.create(
            size=2,
            display_name="My volume",
            display_description="My volume desc",
        )
        cs.assert_called('POST', '/volumes')
        self.assertIsInstance(v, volumes.Volume)
        self.assertEqual(1, mock_warn.call_count)

    @mock.patch.object(warnings, 'warn')
    def test_delete_volume(self, mock_warn):
        vol_id = '15e59938-07d5-11e1-90e3-e3dffe0c5983'
        v = cs.volumes.get(vol_id)
        v.delete()
        cs.assert_called('DELETE', '/volumes/%s' % vol_id)
        cs.volumes.delete(vol_id)
        cs.assert_called('DELETE', '/volumes/%s' % vol_id)
        cs.volumes.delete(v)
        cs.assert_called('DELETE', '/volumes/%s' % vol_id)
        self.assertEqual(4, mock_warn.call_count)

    def test_create_server_volume(self):
        v = cs.volumes.create_server_volume(
            server_id=1234,
            volume_id='15e59938-07d5-11e1-90e3-e3dffe0c5983',
            device='/dev/vdb'
        )
        cs.assert_called('POST', '/servers/1234/os-volume_attachments')
        self.assertIsInstance(v, volumes.Volume)

    def test_update_server_volume(self):
        vol_id = '15e59938-07d5-11e1-90e3-e3dffe0c5983'
        v = cs.volumes.update_server_volume(
            server_id=1234,
            attachment_id='Work',
            new_volume_id=vol_id
        )
        cs.assert_called('PUT', '/servers/1234/os-volume_attachments/Work')
        self.assertIsInstance(v, volumes.Volume)

    def test_get_server_volume(self):
        v = cs.volumes.get_server_volume(1234, 'Work')
        cs.assert_called('GET', '/servers/1234/os-volume_attachments/Work')
        self.assertIsInstance(v, volumes.Volume)

    def test_list_server_volumes(self):
        vl = cs.volumes.get_server_volumes(1234)
        cs.assert_called('GET', '/servers/1234/os-volume_attachments')
        for v in vl:
            self.assertIsInstance(v, volumes.Volume)

    def test_delete_server_volume(self):
        cs.volumes.delete_server_volume(1234, 'Work')
        cs.assert_called('DELETE', '/servers/1234/os-volume_attachments/Work')
