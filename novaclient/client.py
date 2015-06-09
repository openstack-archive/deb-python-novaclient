# Copyright 2010 Jacob Kaplan-Moss
# Copyright 2011 OpenStack Foundation
# Copyright 2011 Piston Cloud Computing, Inc.

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
OpenStack Client interface. Handles the REST calls and responses.
"""

import copy
import functools
import glob
import hashlib
import imp
import itertools
import logging
import os
import pkgutil
import re
import socket
import warnings

from keystoneclient import adapter
from oslo_utils import importutils
from oslo_utils import netutils
import pkg_resources
import requests
from requests import adapters

try:
    import json
except ImportError:
    import simplejson as json

from six.moves.urllib import parse

from novaclient import exceptions
from novaclient import extension as ext
from novaclient.i18n import _, _LW
from novaclient import service_catalog
from novaclient import utils


# key is a deprecated version and value is an alternative version.
DEPRECATED_VERSIONS = {"1.1": "2"}


class TCPKeepAliveAdapter(adapters.HTTPAdapter):
    """The custom adapter used to set TCP Keep-Alive on all connections."""
    def init_poolmanager(self, *args, **kwargs):
        kwargs.setdefault('socket_options', [
            (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        ])
        super(TCPKeepAliveAdapter, self).init_poolmanager(*args, **kwargs)


class _ClientConnectionPool(object):

    def __init__(self):
        self._adapters = {}

    def get(self, url):
        """
        Store and reuse HTTP adapters per Service URL.
        """
        if url not in self._adapters:
            self._adapters[url] = TCPKeepAliveAdapter()

        return self._adapters[url]


class SessionClient(adapter.LegacyJsonAdapter):

    def __init__(self, *args, **kwargs):
        self.times = []
        self.timings = kwargs.pop('timings', False)
        super(SessionClient, self).__init__(*args, **kwargs)

    def request(self, url, method, **kwargs):
        # NOTE(jamielennox): The standard call raises errors from
        # keystoneclient, where we need to raise the novaclient errors.
        raise_exc = kwargs.pop('raise_exc', True)
        with utils.record_time(self.times, self.timings, method, url):
            resp, body = super(SessionClient, self).request(url,
                                                            method,
                                                            raise_exc=False,
                                                            **kwargs)
        if raise_exc and resp.status_code >= 400:
            raise exceptions.from_response(resp, body, url, method)

        return resp, body

    def get_timings(self):
        return self.times

    def reset_timings(self):
        self.times = []


def _original_only(f):
    """Indicates and enforces that this function can only be used if we are
    using the original HTTPClient object.

    We use this to specify that if you use the newer Session HTTP client then
    you are aware that the way you use your client has been updated and certain
    functions are no longer allowed to be used.
    """
    @functools.wraps(f)
    def wrapper(self, *args, **kwargs):
        if isinstance(self.client, SessionClient):
            msg = ('This call is no longer available. The operation should '
                   'be performed on the session object instead.')
            raise exceptions.InvalidUsage(msg)

        return f(self, *args, **kwargs)

    return wrapper


class HTTPClient(object):
    USER_AGENT = 'python-novaclient'

    def __init__(self, user, password, projectid=None, auth_url=None,
                 insecure=False, timeout=None, proxy_tenant_id=None,
                 proxy_token=None, region_name=None,
                 endpoint_type='publicURL', service_type=None,
                 service_name=None, volume_service_name=None,
                 timings=False, bypass_url=None,
                 os_cache=False, no_cache=True,
                 http_log_debug=False, auth_system='keystone',
                 auth_plugin=None, auth_token=None,
                 cacert=None, tenant_id=None, user_id=None,
                 connection_pool=False):
        self.user = user
        self.user_id = user_id
        self.password = password
        self.projectid = projectid
        self.tenant_id = tenant_id

        self._connection_pool = (_ClientConnectionPool()
                                 if connection_pool else None)

        # This will be called by #_get_password if self.password is None.
        # EG if a password can only be obtained by prompting the user, but a
        # token is available, you don't want to prompt until the token has
        # been proven invalid
        self.password_func = None

        if auth_system and auth_system != 'keystone' and not auth_plugin:
            raise exceptions.AuthSystemNotFound(auth_system)

        if not auth_url and auth_system and auth_system != 'keystone':
            auth_url = auth_plugin.get_auth_url()
            if not auth_url:
                raise exceptions.EndpointNotFound()
        self.auth_url = auth_url.rstrip('/') if auth_url else auth_url
        self.version = 'v1.1'
        self.region_name = region_name
        self.endpoint_type = endpoint_type
        self.service_type = service_type
        self.service_name = service_name
        self.volume_service_name = volume_service_name
        self.timings = timings
        self.bypass_url = bypass_url.rstrip('/') if bypass_url else bypass_url
        self.os_cache = os_cache or not no_cache
        self.http_log_debug = http_log_debug
        if timeout is not None:
            self.timeout = float(timeout)
        else:
            self.timeout = None

        self.times = []  # [("item", starttime, endtime), ...]

        self.management_url = self.bypass_url or None
        self.auth_token = auth_token
        self.proxy_token = proxy_token
        self.proxy_tenant_id = proxy_tenant_id
        self.keyring_saver = None
        self.keyring_saved = False

        if insecure:
            self.verify_cert = False
        else:
            if cacert:
                self.verify_cert = cacert
            else:
                self.verify_cert = True

        self.auth_system = auth_system
        self.auth_plugin = auth_plugin
        self._session = None
        self._current_url = None
        self._logger = logging.getLogger(__name__)

        if self.http_log_debug and not self._logger.handlers:
            # Logging level is already set on the root logger
            ch = logging.StreamHandler()
            self._logger.addHandler(ch)
            self._logger.propagate = False
            if hasattr(requests, 'logging'):
                rql = requests.logging.getLogger(requests.__name__)
                rql.addHandler(ch)
                # Since we have already setup the root logger on debug, we
                # have to set it up here on WARNING (its original level)
                # otherwise we will get all the requests logging messages
                rql.setLevel(logging.WARNING)

        self.service_catalog = None
        self.services_url = {}

    def use_token_cache(self, use_it):
        self.os_cache = use_it

    def unauthenticate(self):
        """Forget all of our authentication information."""
        self.management_url = None
        self.auth_token = None

    def set_management_url(self, url):
        self.management_url = url

    def get_timings(self):
        return self.times

    def reset_timings(self):
        self.times = []

    def _redact(self, target, path, text=None):
        """Replace the value of a key in `target`.

        The key can be at the top level by specifying a list with a single
        key as the path. Nested dictionaries are also supported by passing a
        list of keys to be navigated to find the one that should be replaced.
        In this case the last one is the one that will be replaced.

        :param dict target: the dictionary that may have a key to be redacted;
                            modified in place
        :param list path: a list representing the nested structure in `target`
                          that should be redacted; modified in place
        :param string text: optional text to use as a replacement for the
                            redacted key. if text is not specified, the
                            default text will be sha1 hash of the value being
                            redacted
        """

        key = path.pop()

        # move to the most nested dict
        for p in path:
            try:
                target = target[p]
            except KeyError:
                return

        if key in target:
            if text:
                target[key] = text
            else:
                # because in python3 byte string handling is ... ug
                value = target[key].encode('utf-8')
                sha1sum = hashlib.sha1(value)
                target[key] = "{SHA1}%s" % sha1sum.hexdigest()

    def http_log_req(self, method, url, kwargs):
        if not self.http_log_debug:
            return

        string_parts = ['curl -g -i']

        if not kwargs.get('verify', True):
            string_parts.append(' --insecure')

        string_parts.append(" '%s'" % url)
        string_parts.append(' -X %s' % method)

        headers = copy.deepcopy(kwargs['headers'])
        self._redact(headers, ['X-Auth-Token'])
        # because dict ordering changes from 2 to 3
        keys = sorted(headers.keys())
        for name in keys:
            value = headers[name]
            header = ' -H "%s: %s"' % (name, value)
            string_parts.append(header)

        if 'data' in kwargs:
            data = json.loads(kwargs['data'])
            self._redact(data, ['auth', 'passwordCredentials', 'password'])
            string_parts.append(" -d '%s'" % json.dumps(data))
        self._logger.debug("REQ: %s" % "".join(string_parts))

    def http_log_resp(self, resp):
        if not self.http_log_debug:
            return

        if resp.text and resp.status_code != 400:
            try:
                body = json.loads(resp.text)
                self._redact(body, ['access', 'token', 'id'])
            except ValueError:
                body = None
        else:
            body = None

        self._logger.debug("RESP: [%(status)s] %(headers)s\nRESP BODY: "
                           "%(text)s\n", {'status': resp.status_code,
                                          'headers': resp.headers,
                                          'text': json.dumps(body)})

    def open_session(self):
        if not self._connection_pool:
            self._session = requests.Session()

    def close_session(self):
        if self._session and not self._connection_pool:
            self._session.close()
            self._session = None

    def _get_session(self, url):
        if self._connection_pool:
            magic_tuple = parse.urlsplit(url)
            scheme, netloc, path, query, frag = magic_tuple
            service_url = '%s://%s' % (scheme, netloc)
            if self._current_url != service_url:
                # Invalidate Session object in case the url is somehow changed
                if self._session:
                    self._session.close()
                self._current_url = service_url
                self._logger.debug(
                    "New session created for: (%s)" % service_url)
                self._session = requests.Session()
                self._session.mount(service_url,
                                    self._connection_pool.get(service_url))
            return self._session
        elif self._session:
            return self._session

    def request(self, url, method, **kwargs):
        kwargs.setdefault('headers', kwargs.get('headers', {}))
        kwargs['headers']['User-Agent'] = self.USER_AGENT
        kwargs['headers']['Accept'] = 'application/json'
        if 'body' in kwargs:
            kwargs['headers']['Content-Type'] = 'application/json'
            kwargs['data'] = json.dumps(kwargs['body'])
            del kwargs['body']
        if self.timeout is not None:
            kwargs.setdefault('timeout', self.timeout)
        kwargs['verify'] = self.verify_cert

        self.http_log_req(method, url, kwargs)

        request_func = requests.request
        session = self._get_session(url)
        if session:
            request_func = session.request

        resp = request_func(
            method,
            url,
            **kwargs)

        self.http_log_resp(resp)

        if resp.text:
            # TODO(dtroyer): verify the note below in a requests context
            # NOTE(alaski): Because force_exceptions_to_status_code=True
            # httplib2 returns a connection refused event as a 400 response.
            # To determine if it is a bad request or refused connection we need
            # to check the body.  httplib2 tests check for 'Connection refused'
            # or 'actively refused' in the body, so that's what we'll do.
            if resp.status_code == 400:
                if ('Connection refused' in resp.text or
                        'actively refused' in resp.text):
                    raise exceptions.ConnectionRefused(resp.text)
            try:
                body = json.loads(resp.text)
            except ValueError:
                body = None
        else:
            body = None

        if resp.status_code >= 400:
            raise exceptions.from_response(resp, body, url, method)

        return resp, body

    def _time_request(self, url, method, **kwargs):
        with utils.record_time(self.times, self.timings, method, url):
            resp, body = self.request(url, method, **kwargs)
        return resp, body

    def _cs_request(self, url, method, **kwargs):
        if not self.management_url:
            self.authenticate()
        if url is None:
            # To get API version information, it is necessary to GET
            # a nova endpoint directly without "v2/<tenant-id>".
            magic_tuple = parse.urlsplit(self.management_url)
            scheme, netloc, path, query, frag = magic_tuple
            path = re.sub(r'v[1-9]/[a-z0-9]+$', '', path)
            url = parse.urlunsplit((scheme, netloc, path, None, None))
        else:
            if self.service_catalog and not self.bypass_url:
                url = self.get_service_url(self.service_type) + url
            else:
                url = self.management_url + url

        # Perform the request once. If we get a 401 back then it
        # might be because the auth token expired, so try to
        # re-authenticate and try again. If it still fails, bail.
        try:
            kwargs.setdefault('headers', {})['X-Auth-Token'] = self.auth_token
            if self.projectid:
                kwargs['headers']['X-Auth-Project-Id'] = self.projectid

            resp, body = self._time_request(url, method, **kwargs)
            return resp, body
        except exceptions.Unauthorized as e:
            try:
                # first discard auth token, to avoid the possibly expired
                # token being re-used in the re-authentication attempt
                self.unauthenticate()
                # overwrite bad token
                self.keyring_saved = False
                self.authenticate()
                kwargs['headers']['X-Auth-Token'] = self.auth_token
                resp, body = self._time_request(url, method, **kwargs)
                return resp, body
            except exceptions.Unauthorized:
                raise e

    def _get_password(self):
        if not self.password and self.password_func:
            self.password = self.password_func()
        return self.password

    def get(self, url, **kwargs):
        return self._cs_request(url, 'GET', **kwargs)

    def post(self, url, **kwargs):
        return self._cs_request(url, 'POST', **kwargs)

    def put(self, url, **kwargs):
        return self._cs_request(url, 'PUT', **kwargs)

    def delete(self, url, **kwargs):
        return self._cs_request(url, 'DELETE', **kwargs)

    def get_service_url(self, service_type):
        if service_type not in self.services_url:
            url = self.service_catalog.url_for(
                attr='region',
                filter_value=self.region_name,
                endpoint_type=self.endpoint_type,
                service_type=service_type,
                service_name=self.service_name,
                volume_service_name=self.volume_service_name,)
            url = url.rstrip('/')
            self.services_url[service_type] = url
        return self.services_url[service_type]

    def _extract_service_catalog(self, url, resp, body, extract_token=True):
        """See what the auth service told us and process the response.
        We may get redirected to another site, fail or actually get
        back a service catalog with a token and our endpoints.
        """

        # content must always present
        if resp.status_code == 200 or resp.status_code == 201:
            try:
                self.auth_url = url
                self.service_catalog = \
                    service_catalog.ServiceCatalog(body)
                if extract_token:
                    self.auth_token = self.service_catalog.get_token()
                    self.tenant_id = self.service_catalog.get_tenant_id()

                self.management_url = self.get_service_url(self.service_type)
                return None
            except exceptions.AmbiguousEndpoints:
                print(_("Found more than one valid endpoint. Use a more "
                      "restrictive filter"))
                raise
            except KeyError:
                raise exceptions.AuthorizationFailure()
            except exceptions.EndpointNotFound:
                print(_("Could not find any suitable endpoint. Correct "
                        "region?"))
                raise

        elif resp.status_code == 305:
            return resp.headers['location']
        else:
            raise exceptions.from_response(resp, body, url)

    def _fetch_endpoints_from_auth(self, url):
        """We have a token, but don't know the final endpoint for
        the region. We have to go back to the auth service and
        ask again. This request requires an admin-level token
        to work. The proxy token supplied could be from a low-level enduser.

        We can't get this from the keystone service endpoint, we have to use
        the admin endpoint.

        This will overwrite our admin token with the user token.
        """

        # GET ...:5001/v2.0/tokens/#####/endpoints
        url = '/'.join([url, 'tokens', '%s?belongsTo=%s'
                        % (self.proxy_token, self.proxy_tenant_id)])
        self._logger.debug("Using Endpoint URL: %s" % url)
        resp, body = self._time_request(
            url, "GET", headers={'X-Auth-Token': self.auth_token})
        return self._extract_service_catalog(url, resp, body,
                                             extract_token=False)

    def authenticate(self):
        if not self.auth_url:
            msg = _("Authentication requires 'auth_url', which should be "
                    "specified in '%s'") % self.__class__.__name__
            raise exceptions.AuthorizationFailure(msg)
        magic_tuple = netutils.urlsplit(self.auth_url)
        scheme, netloc, path, query, frag = magic_tuple
        port = magic_tuple.port
        if port is None:
            port = 80
        path_parts = path.split('/')
        for part in path_parts:
            if len(part) > 0 and part[0] == 'v':
                self.version = part
                break

        if self.auth_token and self.management_url:
            self._save_keys()
            return

        # TODO(sandy): Assume admin endpoint is 35357 for now.
        # Ideally this is going to have to be provided by the service catalog.
        new_netloc = netloc.replace(':%d' % port, ':%d' % (35357,))
        admin_url = parse.urlunsplit(
            (scheme, new_netloc, path, query, frag))

        auth_url = self.auth_url
        if self.version == "v2.0":  # FIXME(chris): This should be better.
            while auth_url:
                if not self.auth_system or self.auth_system == 'keystone':
                    auth_url = self._v2_auth(auth_url)
                else:
                    auth_url = self._plugin_auth(auth_url)

            # Are we acting on behalf of another user via an
            # existing token? If so, our actual endpoints may
            # be different than that of the admin token.
            if self.proxy_token:
                if self.bypass_url:
                    self.set_management_url(self.bypass_url)
                else:
                    self._fetch_endpoints_from_auth(admin_url)
                # Since keystone no longer returns the user token
                # with the endpoints any more, we need to replace
                # our service account token with the user token.
                self.auth_token = self.proxy_token
        else:
            try:
                while auth_url:
                    auth_url = self._v1_auth(auth_url)
            # In some configurations nova makes redirection to
            # v2.0 keystone endpoint. Also, new location does not contain
            # real endpoint, only hostname and port.
            except exceptions.AuthorizationFailure:
                if auth_url.find('v2.0') < 0:
                    auth_url = auth_url + '/v2.0'
                self._v2_auth(auth_url)

        if self.bypass_url:
            self.set_management_url(self.bypass_url)
        elif not self.management_url:
            raise exceptions.Unauthorized('Nova Client')

        self._save_keys()

    def _save_keys(self):
        # Store the token/mgmt url in the keyring for later requests.
        if (self.keyring_saver and self.os_cache and not self.keyring_saved
                and self.auth_token and self.management_url
                and self.tenant_id):
            self.keyring_saver.save(self.auth_token,
                                    self.management_url,
                                    self.tenant_id)
            # Don't save it again
            self.keyring_saved = True

    def _v1_auth(self, url):
        if self.proxy_token:
            raise exceptions.NoTokenLookupException()

        headers = {'X-Auth-User': self.user,
                   'X-Auth-Key': self._get_password()}
        if self.projectid:
            headers['X-Auth-Project-Id'] = self.projectid

        resp, body = self._time_request(url, 'GET', headers=headers)
        if resp.status_code in (200, 204):  # in some cases we get No Content
            try:
                mgmt_header = 'x-server-management-url'
                self.management_url = resp.headers[mgmt_header].rstrip('/')
                self.auth_token = resp.headers['x-auth-token']
                self.auth_url = url
            except (KeyError, TypeError):
                raise exceptions.AuthorizationFailure()
        elif resp.status_code == 305:
            return resp.headers['location']
        else:
            raise exceptions.from_response(resp, body, url)

    def _plugin_auth(self, auth_url):
        return self.auth_plugin.authenticate(self, auth_url)

    def _v2_auth(self, url):
        """Authenticate against a v2.0 auth service."""
        if self.auth_token:
            body = {"auth": {
                    "token": {"id": self.auth_token}}}
        elif self.user_id:
            body = {"auth": {
                    "passwordCredentials": {"userId": self.user_id,
                                            "password": self._get_password()}}}
        else:
            body = {"auth": {
                    "passwordCredentials": {"username": self.user,
                                            "password": self._get_password()}}}

        if self.tenant_id:
            body['auth']['tenantId'] = self.tenant_id
        elif self.projectid:
            body['auth']['tenantName'] = self.projectid

        return self._authenticate(url, body)

    def _authenticate(self, url, body, **kwargs):
        """Authenticate and extract the service catalog."""
        method = "POST"
        token_url = url + "/tokens"

        # Make sure we follow redirects when trying to reach Keystone
        resp, respbody = self._time_request(
            token_url,
            method,
            body=body,
            allow_redirects=True,
            **kwargs)

        return self._extract_service_catalog(url, resp, respbody)


def _construct_http_client(username=None, password=None, project_id=None,
                           auth_url=None, insecure=False, timeout=None,
                           proxy_tenant_id=None, proxy_token=None,
                           region_name=None, endpoint_type='publicURL',
                           extensions=None, service_type='compute',
                           service_name=None, volume_service_name=None,
                           timings=False, bypass_url=None, os_cache=False,
                           no_cache=True, http_log_debug=False,
                           auth_system='keystone', auth_plugin=None,
                           auth_token=None, cacert=None, tenant_id=None,
                           user_id=None, connection_pool=False, session=None,
                           auth=None, user_agent='python-novaclient',
                           interface=None, **kwargs):
    if session:
        return SessionClient(session=session,
                             auth=auth,
                             interface=interface or endpoint_type,
                             service_type=service_type,
                             region_name=region_name,
                             service_name=service_name,
                             user_agent=user_agent,
                             timings=timings,
                             **kwargs)
    else:
        # FIXME(jamielennox): username and password are now optional. Need
        # to test that they were provided in this mode.
        return HTTPClient(username,
                          password,
                          user_id=user_id,
                          projectid=project_id,
                          tenant_id=tenant_id,
                          auth_url=auth_url,
                          auth_token=auth_token,
                          insecure=insecure,
                          timeout=timeout,
                          auth_system=auth_system,
                          auth_plugin=auth_plugin,
                          proxy_token=proxy_token,
                          proxy_tenant_id=proxy_tenant_id,
                          region_name=region_name,
                          endpoint_type=endpoint_type,
                          service_type=service_type,
                          service_name=service_name,
                          volume_service_name=volume_service_name,
                          timings=timings,
                          bypass_url=bypass_url,
                          os_cache=os_cache,
                          http_log_debug=http_log_debug,
                          cacert=cacert,
                          connection_pool=connection_pool)


def discover_extensions(version):
    extensions = []
    for name, module in itertools.chain(
            _discover_via_python_path(),
            _discover_via_contrib_path(version),
            _discover_via_entry_points()):

        extension = ext.Extension(name, module)
        extensions.append(extension)

    return extensions


def _discover_via_python_path():
    for (module_loader, name, _ispkg) in pkgutil.iter_modules():
        if name.endswith('_python_novaclient_ext'):
            if not hasattr(module_loader, 'load_module'):
                # Python 2.6 compat: actually get an ImpImporter obj
                module_loader = module_loader.find_module(name)

            module = module_loader.load_module(name)
            if hasattr(module, 'extension_name'):
                name = module.extension_name

            yield name, module


def _discover_via_contrib_path(version):
    module_path = os.path.dirname(os.path.abspath(__file__))
    version_str = "v%s" % version.replace('.', '_')
    # NOTE(andreykurilin): v1.1 uses implementation of v2, so we should
    # discover contrib modules in novaclient.v2 dir.
    if version_str == "v1_1":
        version_str = "v2"
    ext_path = os.path.join(module_path, version_str, 'contrib')
    ext_glob = os.path.join(ext_path, "*.py")

    for ext_path in glob.iglob(ext_glob):
        name = os.path.basename(ext_path)[:-3]

        if name == "__init__":
            continue

        module = imp.load_source(name, ext_path)
        yield name, module


def _discover_via_entry_points():
    for ep in pkg_resources.iter_entry_points('novaclient.extension'):
        name = ep.name
        module = ep.load()

        yield name, module


def get_client_class(version):
    version = str(version)
    if version in DEPRECATED_VERSIONS:
        warnings.warn(_LW(
            "Version %(deprecated_version)s is deprecated, using "
            "alternative version %(alternative)s instead.") %
            {"deprecated_version": version,
             "alternative": DEPRECATED_VERSIONS[version]})
        version = DEPRECATED_VERSIONS[version]
    try:
        return importutils.import_class(
            "novaclient.v%s.client.Client" % version)
    except ImportError:
        # NOTE(andreykurilin): available clients version should not be
        # hardcoded, so let's discover them.
        matcher = re.compile(r"v[0-9_]*$")
        submodules = pkgutil.iter_modules(['novaclient'])
        available_versions = [
            name[1:].replace("_", ".") for loader, name, ispkg in submodules
            if matcher.search(name)]
        msg = _("Invalid client version '%(version)s'. must be one of: "
                "%(keys)s") % {'version': version,
                               'keys': ', '.join(available_versions)}
        raise exceptions.UnsupportedVersion(msg)


def Client(version, *args, **kwargs):
    client_class = get_client_class(version)
    return client_class(*args, **kwargs)
