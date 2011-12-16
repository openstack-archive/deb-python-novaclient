from novaclient import client
from novaclient.v1_0 import accounts
from novaclient.v1_0 import backup_schedules
from novaclient.v1_0 import flavors
from novaclient.v1_0 import images
from novaclient.v1_0 import ipgroups
from novaclient.v1_0 import servers
from novaclient.v1_0 import zones


class Client(object):
    """
    Top-level object to access the OpenStack Compute API.

    Create an instance with your creds::

        >>> client = Client(USERNAME, PASSWORD, PROJECT_ID, AUTH_URL)

    Then call methods on its managers::

        >>> client.servers.list()
        ...
        >>> client.flavors.list()
        ...

    """

    def __init__(self, username, api_key, project_id, auth_url=None,
                 insecure=False, timeout=None, token=None, region_name=None,
                 endpoint_name='publicURL'):

        # FIXME(comstud): Rename the api_key argument above when we
        # know it's not being used as keyword argument
        password = api_key
        self.accounts = accounts.AccountManager(self)
        self.backup_schedules = backup_schedules.BackupScheduleManager(self)
        self.flavors = flavors.FlavorManager(self)
        self.images = images.ImageManager(self)
        self.ipgroups = ipgroups.IPGroupManager(self)
        self.servers = servers.ServerManager(self)
        self.zones = zones.ZoneManager(self)

        _auth_url = auth_url or 'https://auth.api.rackspacecloud.com/v1.0'

        self.client = client.HTTPClient(username,
                                        password,
                                        project_id,
                                        _auth_url,
                                        insecure=insecure,
                                        timeout=timeout,
                                        token=token,
                                        region_name=region_name,
                                        endpoint_name=endpoint_name)

    def authenticate(self):
        """
        Authenticate against the server.

        Normally this is called automatically when you first access the API,
        but you can call this method to force authentication right now.

        Returns on success; raises :exc:`exceptions.Unauthorized` if the
        credentials are wrong.
        """
        self.client.authenticate()
