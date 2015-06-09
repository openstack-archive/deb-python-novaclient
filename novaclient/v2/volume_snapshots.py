# Copyright 2011 Denali Systems, Inc.
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
DEPRECATED: Volume snapshot interface (1.1 extension).
"""

import warnings

from novaclient import base


class Snapshot(base.Resource):
    """
    DEPRECATED: A Snapshot is a point-in-time snapshot of an openstack volume.
    """
    NAME_ATTR = 'display_name'

    def __repr__(self):
        return "<Snapshot: %s>" % self.id

    def delete(self):
        """
        DEPRECATED: Delete this snapshot.
        """
        self.manager.delete(self)


class SnapshotManager(base.ManagerWithFind):
    """
    DEPRECATED: Manage :class:`Snapshot` resources.
    """
    resource_class = Snapshot

    def create(self, volume_id, force=False, display_name=None,
               display_description=None):

        """
        DEPRECATED: Create a snapshot of the given volume.

        :param volume_id: The ID of the volume to snapshot.
        :param force: If force is True, create a snapshot even if the volume is
        attached to an instance. Default is False.
        :param display_name: Name of the snapshot
        :param display_description: Description of the snapshot
        :rtype: :class:`Snapshot`
        """
        warnings.warn('The novaclient.v2.volume_snapshots module is '
                      'deprecated and will be removed after Nova 2016.1 is '
                      'released. Use python-cinderclient or '
                      'python-openstacksdk instead.', DeprecationWarning)
        with self.alternate_service_type('volume'):
            body = {'snapshot': {'volume_id': volume_id,
                                 'force': force,
                                 'display_name': display_name,
                                 'display_description': display_description}}
            return self._create('/snapshots', body, 'snapshot')

    def get(self, snapshot_id):
        """
        DEPRECATED: Get a snapshot.

        :param snapshot_id: The ID of the snapshot to get.
        :rtype: :class:`Snapshot`
        """
        warnings.warn('The novaclient.v2.volume_snapshots module is '
                      'deprecated and will be removed after Nova 2016.1 is '
                      'released. Use python-cinderclient or '
                      'python-openstacksdk instead.', DeprecationWarning)
        with self.alternate_service_type('volume'):
            return self._get("/snapshots/%s" % snapshot_id, "snapshot")

    def list(self, detailed=True):
        """
        DEPRECATED: Get a list of all snapshots.

        :rtype: list of :class:`Snapshot`
        """
        warnings.warn('The novaclient.v2.volume_snapshots module is '
                      'deprecated and will be removed after Nova 2016.1 is '
                      'released. Use python-cinderclient or '
                      'python-openstacksdk instead.', DeprecationWarning)
        with self.alternate_service_type('volume'):
            if detailed is True:
                return self._list("/snapshots/detail", "snapshots")
            else:
                return self._list("/snapshots", "snapshots")

    def delete(self, snapshot):
        """
        DEPRECATED: Delete a snapshot.

        :param snapshot: The :class:`Snapshot` to delete.
        """
        warnings.warn('The novaclient.v2.volume_snapshots module is '
                      'deprecated and will be removed after Nova 2016.1 is '
                      'released. Use python-cinderclient or '
                      'python-openstacksdk instead.', DeprecationWarning)
        with self.alternate_service_type('volume'):
            self._delete("/snapshots/%s" % base.getid(snapshot))
