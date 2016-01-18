Python bindings to the OpenStack Nova API
=========================================

.. image:: https://img.shields.io/pypi/v/python-novaclient.svg
    :target: https://pypi.python.org/pypi/python-novaclient/
    :alt: Latest Version

.. image:: https://img.shields.io/pypi/dm/python-novaclient.svg
    :target: https://pypi.python.org/pypi/python-novaclient/
    :alt: Downloads

This is a client for the OpenStack Nova API. There's a Python API (the
``novaclient`` module), and a command-line script (``nova``). Each
implements 100% of the OpenStack Nova API.

See the `OpenStack CLI guide`_ for information on how to use the ``nova``
command-line tool. You may also want to look at the
`OpenStack API documentation`_.

.. _OpenStack CLI Guide: http://docs.openstack.org/cli-reference/content/novaclient_commands.html
.. _OpenStack API documentation: http://docs.openstack.org/api/quick-start/content/

python-novaclient is licensed under the Apache License like the rest of
OpenStack.

* License: Apache License, Version 2.0
* `PyPi`_ - package installation
* `Online Documentation`_
* `Blueprints`_ - feature specifications
* `Bugs`_ - issue tracking
* `Source`_
* `Specs`_
* `How to Contribute`_

.. _PyPi: https://pypi.python.org/pypi/python-novaclient
.. _Online Documentation: http://docs.openstack.org/developer/python-novaclient
.. _Blueprints: https://blueprints.launchpad.net/python-novaclient
.. _Bugs: https://bugs.launchpad.net/python-novaclient
.. _Source: https://git.openstack.org/cgit/openstack/python-novaclient
.. _How to Contribute: http://docs.openstack.org/infra/manual/developers.html
.. _Specs: http://specs.openstack.org/openstack/nova-specs/


.. contents:: Contents:
   :local:

Command-line API
----------------

Installing this package gets you a shell command, ``nova``, that you
can use to interact with any OpenStack cloud.

You'll need to provide your OpenStack username and password. You can do this
with the ``--os-username``, ``--os-password`` and  ``--os-tenant-name``
params, but it's easier to just set them as environment variables::

    export OS_USERNAME=openstack
    export OS_PASSWORD=yadayada
    export OS_TENANT_NAME=myproject

You will also need to define the authentication url with ``--os-auth-url``
and the version of the API with ``--os-compute-api-version``.  Or set them as
an environment variables as well::

    export OS_AUTH_URL=http://example.com:8774/v2/
    export OS_COMPUTE_API_VERSION=2

If you are using Keystone, you need to set the OS_AUTH_URL to the keystone
endpoint::

    export OS_AUTH_URL=http://example.com:5000/v2.0/

Since Keystone can return multiple regions in the Service Catalog, you
can specify the one you want with ``--os-region-name`` (or
``export OS_REGION_NAME``). It defaults to the first in the list returned.

You'll find complete documentation on the shell by running
``nova help``

Python API
----------

There's also a complete Python API, with documentation linked below.


To use with keystone as the authentication system::

    >>> from novaclient import client
    >>> nt = client.Client(VERSION, USER, PASSWORD, TENANT, AUTH_URL)
    >>> nt.flavors.list()
    [...]
    >>> nt.servers.list()
    [...]
    >>> nt.keypairs.list()
    [...]


Testing
-------

There are multiple test targets that can be run to validate the code.

* tox -e pep8 - style guidelines enforcement
* tox -e py27 - traditional unit testing
* tox -e functional - live functional testing against an existing
  openstack

Functional testing assumes the existence of a `clouds.yaml` file as supported
by `os-client-config` (http://docs.openstack.org/developer/os-client-config)
It assumes the existence of a cloud named `devstack` that behaves like a normal
devstack installation with a demo and an admin user/tenant - or clouds named
`functional_admin` and `functional_nonadmin`.
