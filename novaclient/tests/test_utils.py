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

import sys

import mock
import six

from novaclient import base
from novaclient import exceptions
from novaclient.tests import utils as test_utils
from novaclient import utils

UUID = '8e8ec658-c7b0-4243-bdf8-6f7f2952c0d0'


class FakeResource(object):
    NAME_ATTR = 'name'

    def __init__(self, _id, properties):
        self.id = _id
        try:
            self.name = properties['name']
        except KeyError:
            pass


class FakeManager(base.ManagerWithFind):

    resource_class = FakeResource

    resources = [
        FakeResource('1234', {'name': 'entity_one'}),
        FakeResource(UUID, {'name': 'entity_two'}),
        FakeResource('5678', {'name': '9876'}),
        FakeResource('01234', {'name': 'entity_three'})
    ]

    is_alphanum_id_allowed = None

    def __init__(self, alphanum_id_allowed=False):
        self.is_alphanum_id_allowed = alphanum_id_allowed

    def get(self, resource_id):
        for resource in self.resources:
            if resource.id == str(resource_id):
                return resource
        raise exceptions.NotFound(resource_id)

    def list(self):
        return self.resources


class FakeDisplayResource(object):
    NAME_ATTR = 'display_name'

    def __init__(self, _id, properties):
        self.id = _id
        try:
            self.display_name = properties['display_name']
        except KeyError:
            pass


class FakeDisplayManager(FakeManager):

    resource_class = FakeDisplayResource

    resources = [
        FakeDisplayResource('4242', {'display_name': 'entity_three'}),
    ]


class FindResourceTestCase(test_utils.TestCase):

    def setUp(self):
        super(FindResourceTestCase, self).setUp()
        self.manager = FakeManager(None)

    def test_find_none(self):
        """Test a few non-valid inputs."""
        self.assertRaises(exceptions.CommandError,
                          utils.find_resource,
                          self.manager,
                          'asdf')
        self.assertRaises(exceptions.CommandError,
                          utils.find_resource,
                          self.manager,
                          None)
        self.assertRaises(exceptions.CommandError,
                          utils.find_resource,
                          self.manager,
                          {})

    def test_find_by_integer_id(self):
        output = utils.find_resource(self.manager, 1234)
        self.assertEqual(output, self.manager.get('1234'))

    def test_find_by_str_id(self):
        output = utils.find_resource(self.manager, '1234')
        self.assertEqual(output, self.manager.get('1234'))

    def test_find_by_uuid(self):
        output = utils.find_resource(self.manager, UUID)
        self.assertEqual(output, self.manager.get(UUID))

    def test_find_by_str_name(self):
        output = utils.find_resource(self.manager, 'entity_one')
        self.assertEqual(output, self.manager.get('1234'))

    def test_find_by_str_displayname(self):
        display_manager = FakeDisplayManager(None)
        output = utils.find_resource(display_manager, 'entity_three')
        self.assertEqual(output, display_manager.get('4242'))

    def test_find_in_alphanum_allowd_manager_by_str_id_(self):
        alphanum_manager = FakeManager(True)
        output = utils.find_resource(alphanum_manager, '01234')
        self.assertEqual(output, alphanum_manager.get('01234'))


class _FakeResult(object):
    def __init__(self, name, value):
        self.name = name
        self.value = value


class PrintResultTestCase(test_utils.TestCase):
    @mock.patch('sys.stdout', six.StringIO())
    def test_print_dict(self):
        dict = {'key': 'value'}
        utils.print_dict(dict)
        self.assertEqual(sys.stdout.getvalue(),
                         '+----------+-------+\n'
                         '| Property | Value |\n'
                         '+----------+-------+\n'
                         '| key      | value |\n'
                         '+----------+-------+\n')

    @mock.patch('sys.stdout', six.StringIO())
    def test_print_dict_wrap(self):
        dict = {'key1': 'not wrapped',
                'key2': 'this will be wrapped'}
        utils.print_dict(dict, wrap=16)
        self.assertEqual(sys.stdout.getvalue(),
                         '+----------+--------------+\n'
                         '| Property | Value        |\n'
                         '+----------+--------------+\n'
                         '| key1     | not wrapped  |\n'
                         '| key2     | this will be |\n'
                         '|          | wrapped      |\n'
                         '+----------+--------------+\n')

    @mock.patch('sys.stdout', six.StringIO())
    def test_print_list_sort_by_str(self):
        objs = [_FakeResult("k1", 1),
                _FakeResult("k3", 2),
                _FakeResult("k2", 3)]

        utils.print_list(objs, ["Name", "Value"], sortby_index=0)

        self.assertEqual(sys.stdout.getvalue(),
                         '+------+-------+\n'
                         '| Name | Value |\n'
                         '+------+-------+\n'
                         '| k1   | 1     |\n'
                         '| k2   | 3     |\n'
                         '| k3   | 2     |\n'
                         '+------+-------+\n')

    @mock.patch('sys.stdout', six.StringIO())
    def test_print_list_sort_by_integer(self):
        objs = [_FakeResult("k1", 1),
                _FakeResult("k3", 2),
                _FakeResult("k2", 3)]

        utils.print_list(objs, ["Name", "Value"], sortby_index=1)

        self.assertEqual(sys.stdout.getvalue(),
                         '+------+-------+\n'
                         '| Name | Value |\n'
                         '+------+-------+\n'
                         '| k1   | 1     |\n'
                         '| k3   | 2     |\n'
                         '| k2   | 3     |\n'
                         '+------+-------+\n')

    # without sorting
    @mock.patch('sys.stdout', six.StringIO())
    def test_print_list_sort_by_none(self):
        objs = [_FakeResult("k1", 1),
                _FakeResult("k3", 3),
                _FakeResult("k2", 2)]

        utils.print_list(objs, ["Name", "Value"], sortby_index=None)

        self.assertEqual(sys.stdout.getvalue(),
                         '+------+-------+\n'
                         '| Name | Value |\n'
                         '+------+-------+\n'
                         '| k1   | 1     |\n'
                         '| k3   | 3     |\n'
                         '| k2   | 2     |\n'
                         '+------+-------+\n')

    @mock.patch('sys.stdout', six.StringIO())
    def test_print_dict_dictionary(self):
        dict = {'k': {'foo': 'bar'}}
        utils.print_dict(dict)
        self.assertEqual(sys.stdout.getvalue(),
                         '+----------+----------------+\n'
                         '| Property | Value          |\n'
                         '+----------+----------------+\n'
                         '| k        | {"foo": "bar"} |\n'
                         '+----------+----------------+\n')

    @mock.patch('sys.stdout', six.StringIO())
    def test_print_dict_list_dictionary(self):
        dict = {'k': [{'foo': 'bar'}]}
        utils.print_dict(dict)
        self.assertEqual(sys.stdout.getvalue(),
                         '+----------+------------------+\n'
                         '| Property | Value            |\n'
                         '+----------+------------------+\n'
                         '| k        | [{"foo": "bar"}] |\n'
                         '+----------+------------------+\n')

    @mock.patch('sys.stdout', six.StringIO())
    def test_print_dict_list(self):
        dict = {'k': ['foo', 'bar']}
        utils.print_dict(dict)
        self.assertEqual(sys.stdout.getvalue(),
                         '+----------+----------------+\n'
                         '| Property | Value          |\n'
                         '+----------+----------------+\n'
                         '| k        | ["foo", "bar"] |\n'
                         '+----------+----------------+\n')


class FlattenTestCase(test_utils.TestCase):
    def test_flattening(self):
        squashed = utils.flatten_dict(
            {'a1': {'b1': 1234,
                    'b2': 'string',
                    'b3': set((1, 2, 3)),
                    'b4': {'c1': ['l', 'l', ['l']],
                           'c2': 'string'}},
             'a2': ['l'],
             'a3': ('t',)})

        self.assertEqual({'a1_b1': 1234,
                          'a1_b2': 'string',
                          'a1_b3': set([1, 2, 3]),
                          'a1_b4_c1': ['l', 'l', ['l']],
                          'a1_b4_c2': 'string',
                          'a2': ['l'],
                          'a3': ('t',)},
                         squashed)

    def test_pretty_choice_list(self):
        l = []
        r = utils.pretty_choice_list(l)
        self.assertEqual(r, "")

        l = ["v1", "v2", "v3"]
        r = utils.pretty_choice_list(l)
        self.assertEqual(r, "'v1', 'v2', 'v3'")

    def test_pretty_choice_dict(self):
        d = {}
        r = utils.pretty_choice_dict(d)
        self.assertEqual(r, "")

        d = {"k1": "v1",
             "k2": "v2",
             "k3": "v3"}
        r = utils.pretty_choice_dict(d)
        self.assertEqual(r, "'k1=v1', 'k2=v2', 'k3=v3'")


class ValidationsTestCase(test_utils.TestCase):
    def test_validate_flavor_metadata_keys_with_valid_keys(self):
        valid_keys = ['key1', 'month.price', 'I-Am:AK-ey.01-', 'spaces and _']
        utils.validate_flavor_metadata_keys(valid_keys)

    def test_validate_flavor_metadata_keys_with_invalid_keys(self):
        invalid_keys = ['/1', '?1', '%1', '<', '>', '\1']
        for key in invalid_keys:
            try:
                utils.validate_flavor_metadata_keys([key])
                self.fail("Invalid key passed validation: %s" % key)
            except exceptions.CommandError as ce:
                self.assertTrue(key in str(ce))
