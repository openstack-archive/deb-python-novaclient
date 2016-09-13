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

from novaclient import base


class QuotaSet(base.Resource):

    @property
    def id(self):
        """QuotaSet does not have a 'id' attribute but base.Resource needs it
        to self-refresh and QuotaSet is indexed by tenant_id.
        """
        return self.tenant_id

    def update(self, *args, **kwargs):
        return self.manager.update(self.tenant_id, *args, **kwargs)


class QuotaSetManager(base.Manager):
    resource_class = QuotaSet

    def get(self, tenant_id, user_id=None, detail=False):
        url = '/os-quota-sets/%(tenant_id)s'
        if detail:
            url += '/detail'

        if hasattr(tenant_id, 'tenant_id'):
            tenant_id = tenant_id.tenant_id
        if user_id:
            params = {'tenant_id': tenant_id, 'user_id': user_id}
            url += '?user_id=%(user_id)s'
        else:
            params = {'tenant_id': tenant_id}

        return self._get(url % params, "quota_set")

    def update(self, tenant_id, **kwargs):

        user_id = kwargs.pop('user_id', None)
        body = {'quota_set': kwargs}

        for key in list(body['quota_set']):
            if body['quota_set'][key] is None:
                body['quota_set'].pop(key)

        if user_id:
            url = '/os-quota-sets/%s?user_id=%s' % (tenant_id, user_id)
        else:
            url = '/os-quota-sets/%s' % tenant_id
        return self._update(url, body, 'quota_set')

    def defaults(self, tenant_id):
        return self._get('/os-quota-sets/%s/defaults' % tenant_id,
                         'quota_set')

    def delete(self, tenant_id, user_id=None):
        """
        Delete quota for a tenant or for a user.

        :param tenant_id: A tenant for which quota is to be deleted
        :param user_id: A user for which quota is to be deleted
        :returns: An instance of novaclient.base.TupleWithMeta
        """
        if user_id:
            url = '/os-quota-sets/%s?user_id=%s' % (tenant_id, user_id)
        else:
            url = '/os-quota-sets/%s' % tenant_id
        return self._delete(url)
