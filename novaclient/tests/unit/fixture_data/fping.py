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

from novaclient.tests.unit.fixture_data import base


class Fixture(base.Fixture):

    base_url = 'os-fping'

    def setUp(self):
        super(Fixture, self).setUp()

        get_os_fping_1 = {
            'server': {
                "id": "1",
                "project_id": "fake-project",
                "alive": True,
            }
        }
        self.requests.register_uri('GET', self.url(1),
                                   json=get_os_fping_1,
                                   headers=self.json_headers)

        get_os_fping = {
            'servers': [
                get_os_fping_1['server'],
                {
                    "id": "2",
                    "project_id": "fake-project",
                    "alive": True,
                },
            ]
        }
        self.requests.register_uri('GET', self.url(),
                                   json=get_os_fping,
                                   headers=self.json_headers)
