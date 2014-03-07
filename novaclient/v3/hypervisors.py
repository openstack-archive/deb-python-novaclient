# Copyright 2013 IBM Corp
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

"""
Hypervisors interface
"""

from novaclient.openstack.common.py3kcompat import urlutils
from novaclient.v1_1 import hypervisors


class Hypervisor(hypervisors.Hypervisor):
    pass


class HypervisorManager(hypervisors.HypervisorManager):
    resource_class = Hypervisor

    def search(self, hypervisor_match):
        """
        Get a list of matching hypervisors.

        :param servers: If True, server information is also retrieved.
        """
        url = ('/os-hypervisors/search?query=%s' %
               urlutils.quote(hypervisor_match, safe=''))
        return self._list(url, 'hypervisors')

    def servers(self, hypervisor):
        """
        Get servers for a specific hypervisor

        :param hypervisor: ID of hypervisor to get list of servers for.
        """
        return self._get('/os-hypervisors/%s/servers' % hypervisor,
                         'hypervisor')
