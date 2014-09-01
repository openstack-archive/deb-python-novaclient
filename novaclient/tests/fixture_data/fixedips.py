# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import httpretty

from novaclient.openstack.common import jsonutils
from novaclient.tests.fixture_data import base


class Fixture(base.Fixture):

    base_url = 'os-fixed-ips'

    def setUp(self):
        super(Fixture, self).setUp()

        get_os_fixed_ips = {
            "fixed_ip": {
                'cidr': '192.168.1.0/24',
                'address': '192.168.1.1',
                'hostname': 'foo',
                'host': 'bar'
            }
        }
        httpretty.register_uri(httpretty.GET, self.url('192.168.1.1'),
                               body=jsonutils.dumps(get_os_fixed_ips),
                               content_type='application/json')

        httpretty.register_uri(httpretty.POST,
                               self.url('192.168.1.1', 'action'),
                               content_type='application/json',
                               status=202)
