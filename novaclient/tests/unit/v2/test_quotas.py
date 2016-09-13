# Copyright 2011 OpenStack Foundation
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

from novaclient.tests.unit.fixture_data import client
from novaclient.tests.unit.fixture_data import quotas as data
from novaclient.tests.unit import utils
from novaclient.tests.unit.v2 import fakes


class QuotaSetsTest(utils.FixturedTestCase):

    client_fixture_class = client.V1
    data_fixture_class = data.V1

    def test_tenant_quotas_get(self):
        tenant_id = 'test'
        q = self.cs.quotas.get(tenant_id)
        self.assert_request_id(q, fakes.FAKE_REQUEST_ID_LIST)
        self.assert_called('GET', '/os-quota-sets/%s' % tenant_id)

    def test_user_quotas_get(self):
        tenant_id = 'test'
        user_id = 'fake_user'
        q = self.cs.quotas.get(tenant_id, user_id=user_id)
        self.assert_request_id(q, fakes.FAKE_REQUEST_ID_LIST)
        url = '/os-quota-sets/%s?user_id=%s' % (tenant_id, user_id)
        self.assert_called('GET', url)

    def test_tenant_quotas_get_detail(self):
        tenant_id = 'test'
        q = self.cs.quotas.get(tenant_id, detail=True)
        self.assert_request_id(q, fakes.FAKE_REQUEST_ID_LIST)
        self.assert_called('GET', '/os-quota-sets/%s/detail' % tenant_id)

    def test_user_quotas_get_detail(self):
        tenant_id = 'test'
        user_id = 'fake_user'
        q = self.cs.quotas.get(tenant_id, user_id=user_id, detail=True)
        self.assert_request_id(q, fakes.FAKE_REQUEST_ID_LIST)
        url = '/os-quota-sets/%s/detail?user_id=%s' % (tenant_id, user_id)
        self.assert_called('GET', url)

    def test_tenant_quotas_defaults(self):
        tenant_id = '97f4c221bff44578b0300df4ef119353'
        q = self.cs.quotas.defaults(tenant_id)
        self.assert_request_id(q, fakes.FAKE_REQUEST_ID_LIST)
        self.assert_called('GET', '/os-quota-sets/%s/defaults' % tenant_id)

    def test_force_update_quota(self):
        q = self.cs.quotas.get('97f4c221bff44578b0300df4ef119353')
        qu = q.update(cores=2, force=True)
        self.assert_request_id(qu, fakes.FAKE_REQUEST_ID_LIST)
        self.assert_called(
            'PUT', '/os-quota-sets/97f4c221bff44578b0300df4ef119353',
            {'quota_set': {'force': True, 'cores': 2}})

    def test_quotas_delete(self):
        tenant_id = 'test'
        ret = self.cs.quotas.delete(tenant_id)
        self.assert_request_id(ret, fakes.FAKE_REQUEST_ID_LIST)
        self.assert_called('DELETE', '/os-quota-sets/%s' % tenant_id)

    def test_user_quotas_delete(self):
        tenant_id = 'test'
        user_id = 'fake_user'
        ret = self.cs.quotas.delete(tenant_id, user_id=user_id)
        self.assert_request_id(ret, fakes.FAKE_REQUEST_ID_LIST)
        url = '/os-quota-sets/%s?user_id=%s' % (tenant_id, user_id)
        self.assert_called('DELETE', url)
