# Copyright (c) 2014 VMware, Inc.
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
Server group interface.
"""

from novaclient import base


class ServerGroup(base.Resource):
    """
    A server group.
    """

    def __repr__(self):
        return '<ServerGroup: %s>' % self.id

    def delete(self):
        """
        Delete this server group.

        :returns: An instance of novaclient.base.TupleWithMeta
        """
        return self.manager.delete(self.id)


class ServerGroupsManager(base.ManagerWithFind):
    """
    Manage :class:`ServerGroup` resources.
    """
    resource_class = ServerGroup

    def list(self, all_projects=False):
        """Get a list of all server groups.

        :rtype: list of :class:`ServerGroup`.
        """
        all = '?all_projects' if all_projects else ''
        return self._list('/os-server-groups%s' % all, 'server_groups')

    def get(self, id):
        """Get a specific server group.

        :param id: The ID of the :class:`ServerGroup` to get.
        :rtype: :class:`ServerGroup`
        """
        return self._get('/os-server-groups/%s' % id,
                         'server_group')

    def delete(self, id):
        """Delete a specific server group.

        :param id: The ID of the :class:`ServerGroup` to delete.
        :returns: An instance of novaclient.base.TupleWithMeta
        """
        return self._delete('/os-server-groups/%s' % id)

    def create(self, **kwargs):
        """Create (allocate) a server group.

        :rtype: list of :class:`ServerGroup`
        """
        body = {'server_group': kwargs}
        return self._create('/os-server-groups', body, 'server_group')
