# Copyright 2010 Jacob Kaplan-Moss

# Copyright 2011 OpenStack Foundation
# Copyright 2013 IBM Corp.
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

from __future__ import print_function

import argparse
import copy
import datetime
import functools
import getpass
import locale
import logging
import os
import sys
import time
import warnings

from oslo_utils import encodeutils
from oslo_utils import netutils
from oslo_utils import strutils
from oslo_utils import timeutils
from oslo_utils import uuidutils
import six

import novaclient
from novaclient import api_versions
from novaclient import base
from novaclient import client
from novaclient import exceptions
from novaclient.i18n import _
from novaclient.i18n import _LE
from novaclient import shell
from novaclient import utils
from novaclient.v2 import availability_zones
from novaclient.v2 import quotas
from novaclient.v2 import servers


logger = logging.getLogger(__name__)


CLIENT_BDM2_KEYS = {
    'id': 'uuid',
    'source': 'source_type',
    'dest': 'destination_type',
    'bus': 'disk_bus',
    'device': 'device_name',
    'size': 'volume_size',
    'format': 'guest_format',
    'bootindex': 'boot_index',
    'type': 'device_type',
    'shutdown': 'delete_on_termination',
    'tag': 'tag',
}


# NOTE(mriedem): Remove this along with the deprecated commands in the first
# python-novaclient release AFTER the nova server 15.0.0 'O' release.
def emit_image_deprecation_warning(command_name):
    print('WARNING: Command %s is deprecated and will be removed after Nova '
          '15.0.0 is released. Use python-glanceclient or openstackclient '
          'instead.' % command_name, file=sys.stderr)


def deprecated_network(fn):
    @functools.wraps(fn)
    def wrapped(cs, *args, **kwargs):
        command_name = '-'.join(fn.__name__.split('_')[1:])
        print('WARNING: Command %s is deprecated and will be removed '
              'after Nova 15.0.0 is released. Use python-neutronclient '
              'or python-openstackclient instead.' % command_name,
              file=sys.stderr)
        # The network proxy API methods were deprecated in 2.36 and will return
        # a 404 so we fallback to 2.35 to maintain a transition for CLI users.
        want_version = api_versions.APIVersion('2.35')
        cur_version = cs.api_version
        if cs.api_version > want_version:
            cs.api_version = want_version
        try:
            return fn(cs, *args, **kwargs)
        finally:
            cs.api_version = cur_version
    wrapped.__doc__ = 'DEPRECATED: ' + fn.__doc__
    return wrapped


def _key_value_pairing(text):
    try:
        (k, v) = text.split('=', 1)
        return (k, v)
    except ValueError:
        msg = _LE("'%s' is not in the format of 'key=value'") % text
        raise argparse.ArgumentTypeError(msg)


def _meta_parsing(metadata):
    return dict(v.split('=', 1) for v in metadata)


def _match_image(cs, wanted_properties):
    image_list = cs.images.list()
    images_matched = []
    match = set(wanted_properties)
    for img in image_list:
        try:
            if match == match.intersection(set(img.metadata.items())):
                images_matched.append(img)
        except AttributeError:
            pass
    return images_matched


def _parse_block_device_mapping_v2(args, image):
    bdm = []

    if args.boot_volume:
        bdm_dict = {'uuid': args.boot_volume, 'source_type': 'volume',
                    'destination_type': 'volume', 'boot_index': 0,
                    'delete_on_termination': False}
        bdm.append(bdm_dict)

    if args.snapshot:
        bdm_dict = {'uuid': args.snapshot, 'source_type': 'snapshot',
                    'destination_type': 'volume', 'boot_index': 0,
                    'delete_on_termination': False}
        bdm.append(bdm_dict)

    for device_spec in args.block_device:
        spec_dict = dict(v.split('=') for v in device_spec.split(','))
        bdm_dict = {}

        for key, value in six.iteritems(spec_dict):
            bdm_dict[CLIENT_BDM2_KEYS[key]] = value

        # Convert the delete_on_termination to a boolean or set it to true by
        # default for local block devices when not specified.
        if 'delete_on_termination' in bdm_dict:
            action = bdm_dict['delete_on_termination']
            if action not in ['remove', 'preserve']:
                raise exceptions.CommandError(
                    _("The value of shutdown key of --block-device shall be "
                      "either 'remove' or 'preserve' but it was '%(action)s'")
                    % {'action': action})

            bdm_dict['delete_on_termination'] = (action == 'remove')
        elif bdm_dict.get('destination_type') == 'local':
            bdm_dict['delete_on_termination'] = True

        bdm.append(bdm_dict)

    for ephemeral_spec in args.ephemeral:
        bdm_dict = {'source_type': 'blank', 'destination_type': 'local',
                    'boot_index': -1, 'delete_on_termination': True}
        try:
            eph_dict = dict(v.split('=') for v in ephemeral_spec.split(','))
        except ValueError:
            err_msg = (_("Invalid ephemeral argument '%s'.") % args.ephemeral)
            raise argparse.ArgumentTypeError(err_msg)
        if 'size' in eph_dict:
            bdm_dict['volume_size'] = eph_dict['size']
        if 'format' in eph_dict:
            bdm_dict['guest_format'] = eph_dict['format']

        bdm.append(bdm_dict)

    if args.swap:
        bdm_dict = {'source_type': 'blank', 'destination_type': 'local',
                    'boot_index': -1, 'delete_on_termination': True,
                    'guest_format': 'swap', 'volume_size': args.swap}
        bdm.append(bdm_dict)

    return bdm


def _parse_nics(cs, args):
    supports_auto_alloc = cs.api_version >= api_versions.APIVersion('2.37')
    if supports_auto_alloc:
        err_msg = (_("Invalid nic argument '%s'. Nic arguments must be of "
                     "the form --nic <auto,none,net-id=net-uuid,"
                     "net-name=network-name,v4-fixed-ip=ip-addr,"
                     "v6-fixed-ip=ip-addr,port-id=port-uuid,tag=tag>, "
                     "with only one of net-id, net-name or port-id "
                     "specified. Specifying a --nic of auto or none cannot "
                     "be used with any other --nic value."))
    elif cs.api_version >= api_versions.APIVersion('2.32'):
        err_msg = (_("Invalid nic argument '%s'. Nic arguments must be of "
                     "the form --nic <net-id=net-uuid,"
                     "net-name=network-name,v4-fixed-ip=ip-addr,"
                     "v6-fixed-ip=ip-addr,port-id=port-uuid,tag=tag>, "
                     "with only one of net-id, net-name or port-id "
                     "specified."))
    else:
        err_msg = (_("Invalid nic argument '%s'. Nic arguments must be of "
                     "the form --nic <net-id=net-uuid,"
                     "net-name=network-name,v4-fixed-ip=ip-addr,"
                     "v6-fixed-ip=ip-addr,port-id=port-uuid>, "
                     "with only one of net-id, net-name or port-id "
                     "specified."))
    auto_or_none = False
    nics = []
    for nic_str in args.nics:
        nic_info = {"net-id": "", "v4-fixed-ip": "", "v6-fixed-ip": "",
                    "port-id": "", "net-name": "", "tag": ""}

        for kv_str in nic_str.split(","):
            try:
                # handle the special auto/none cases
                if kv_str in ('auto', 'none'):
                    if not supports_auto_alloc:
                        raise exceptions.CommandError(err_msg % nic_str)
                    nics.append(kv_str)
                    auto_or_none = True
                    continue
                k, v = kv_str.split("=", 1)
            except ValueError:
                raise exceptions.CommandError(err_msg % nic_str)

            if k in nic_info:
                # if user has given a net-name resolve it to network ID
                if k == 'net-name':
                    k = 'net-id'
                    v = _find_network_id(cs, v)
                # if some argument was given multiple times
                if nic_info[k]:
                    raise exceptions.CommandError(err_msg % nic_str)
                nic_info[k] = v
            else:
                raise exceptions.CommandError(err_msg % nic_str)

        if auto_or_none:
            continue

        if nic_info['v4-fixed-ip'] and not netutils.is_valid_ipv4(
                nic_info['v4-fixed-ip']):
            raise exceptions.CommandError(_("Invalid ipv4 address."))

        if nic_info['v6-fixed-ip'] and not netutils.is_valid_ipv6(
                nic_info['v6-fixed-ip']):
            raise exceptions.CommandError(_("Invalid ipv6 address."))

        if bool(nic_info['net-id']) == bool(nic_info['port-id']):
            raise exceptions.CommandError(err_msg % nic_str)

        nics.append(nic_info)

    if nics:
        if auto_or_none:
            if len(nics) > 1:
                raise exceptions.CommandError(err_msg % nic_str)
            # change the single list entry to a string
            nics = nics[0]
    else:
        # Default to 'auto' if API version >= 2.37 and nothing was specified
        if supports_auto_alloc:
            nics = 'auto'

    return nics


def _boot(cs, args):
    """Boot a new server."""
    if not args.flavor:
        raise exceptions.CommandError(_("you need to specify a Flavor ID."))

    if args.image:
        image = _find_image(cs, args.image)
    else:
        image = None

    if not image and args.image_with:
        images = _match_image(cs, args.image_with)
        if images:
            # TODO(harlowja): log a warning that we
            # are selecting the first of many?
            image = images[0]

    min_count = 1
    max_count = 1
    if args.min_count is not None:
        if args.min_count < 1:
            raise exceptions.CommandError(_("min_count should be >= 1"))
        min_count = args.min_count
        max_count = min_count
    if args.max_count is not None:
        if args.max_count < 1:
            raise exceptions.CommandError(_("max_count should be >= 1"))
        max_count = args.max_count
    if (args.min_count is not None and
            args.max_count is not None and
            args.min_count > args.max_count):
        raise exceptions.CommandError(_("min_count should be <= max_count"))

    flavor = _find_flavor(cs, args.flavor)

    meta = _meta_parsing(args.meta)

    files = {}
    for f in args.files:
        try:
            dst, src = f.split('=', 1)
            files[dst] = open(src)
        except IOError as e:
            raise exceptions.CommandError(_("Can't open '%(src)s': %(exc)s") %
                                          {'src': src, 'exc': e})
        except ValueError:
            raise exceptions.CommandError(_("Invalid file argument '%s'. "
                                            "File arguments must be of the "
                                            "form '--file "
                                            "<dst-path=src-path>'") % f)

    # use the os-keypair extension
    key_name = None
    if args.key_name is not None:
        key_name = args.key_name

    if args.user_data:
        try:
            userdata = open(args.user_data)
        except IOError as e:
            raise exceptions.CommandError(_("Can't open '%(user_data)s': "
                                            "%(exc)s") %
                                          {'user_data': args.user_data,
                                           'exc': e})
    else:
        userdata = None

    if args.availability_zone:
        availability_zone = args.availability_zone
    else:
        availability_zone = None

    if args.security_groups:
        security_groups = args.security_groups.split(',')
    else:
        security_groups = None

    block_device_mapping = {}
    for bdm in args.block_device_mapping:
        device_name, mapping = bdm.split('=', 1)
        block_device_mapping[device_name] = mapping

    block_device_mapping_v2 = _parse_block_device_mapping_v2(args, image)

    n_boot_args = len(list(filter(
        bool, (image, args.boot_volume, args.snapshot))))
    have_bdm = block_device_mapping_v2 or block_device_mapping

    # Fail if more than one boot devices are present
    # or if there is no device to boot from.
    if n_boot_args > 1 or n_boot_args == 0 and not have_bdm:
        raise exceptions.CommandError(
            _("you need to specify at least one source ID (Image, Snapshot, "
              "or Volume), a block device mapping or provide a set of "
              "properties to match against an image"))

    if block_device_mapping and block_device_mapping_v2:
        raise exceptions.CommandError(
            _("you can't mix old block devices (--block-device-mapping) "
              "with the new ones (--block-device, --boot-volume, --snapshot, "
              "--ephemeral, --swap)"))

    nics = _parse_nics(cs, args)

    hints = {}
    if args.scheduler_hints:
        for hint in args.scheduler_hints:
            key, _sep, value = hint.partition('=')
            # NOTE(vish): multiple copies of the same hint will
            #             result in a list of values
            if key in hints:
                if isinstance(hints[key], six.string_types):
                    hints[key] = [hints[key]]
                hints[key] += [value]
            else:
                hints[key] = value
    boot_args = [args.name, image, flavor]

    if str(args.config_drive).lower() in ("true", "1"):
        config_drive = True
    elif str(args.config_drive).lower() in ("false", "0", "", "none"):
        config_drive = None
    else:
        config_drive = args.config_drive

    boot_kwargs = dict(
        meta=meta,
        files=files,
        key_name=key_name,
        min_count=min_count,
        max_count=max_count,
        userdata=userdata,
        availability_zone=availability_zone,
        security_groups=security_groups,
        block_device_mapping=block_device_mapping,
        block_device_mapping_v2=block_device_mapping_v2,
        nics=nics,
        scheduler_hints=hints,
        config_drive=config_drive,
        admin_pass=args.admin_pass,
        access_ip_v4=args.access_ip_v4,
        access_ip_v6=args.access_ip_v6)

    if 'description' in args:
        boot_kwargs["description"] = args.description

    return boot_args, boot_kwargs


@utils.arg(
    '--flavor',
    default=None,
    metavar='<flavor>',
    help=_("Name or ID of flavor (see 'nova flavor-list')."))
@utils.arg(
    '--image',
    default=None,
    metavar='<image>',
    help=_("Name or ID of image (see 'glance image-list'). "))
@utils.arg(
    '--image-with',
    default=[],
    type=_key_value_pairing,
    action='append',
    metavar='<key=value>',
    help=_("Image metadata property (see 'glance image-show'). "))
@utils.arg(
    '--boot-volume',
    default=None,
    metavar="<volume_id>",
    help=_("Volume ID to boot from."))
@utils.arg(
    '--snapshot',
    default=None,
    metavar="<snapshot_id>",
    help=_("Snapshot ID to boot from (will create a volume)."))
@utils.arg(
    '--min-count',
    default=None,
    type=int,
    metavar='<number>',
    help=_("Boot at least <number> servers (limited by quota)."))
@utils.arg(
    '--max-count',
    default=None,
    type=int,
    metavar='<number>',
    help=_("Boot up to <number> servers (limited by quota)."))
@utils.arg(
    '--meta',
    metavar="<key=value>",
    action='append',
    default=[],
    help=_("Record arbitrary key/value metadata to /meta_data.json "
           "on the metadata server. Can be specified multiple times."))
@utils.arg(
    '--file',
    metavar="<dst-path=src-path>",
    action='append',
    dest='files',
    default=[],
    help=_("Store arbitrary files from <src-path> locally to <dst-path> "
           "on the new server. Limited by the injected_files quota value."))
@utils.arg(
    '--key-name',
    default=os.environ.get('NOVACLIENT_DEFAULT_KEY_NAME'),
    metavar='<key-name>',
    help=_("Key name of keypair that should be created earlier with \
           the command keypair-add."))
@utils.arg('name', metavar='<name>', help=_('Name for the new server.'))
@utils.arg(
    '--user-data',
    default=None,
    metavar='<user-data>',
    help=_("user data file to pass to be exposed by the metadata server."))
@utils.arg(
    '--availability-zone',
    default=None,
    metavar='<availability-zone>',
    help=_("The availability zone for server placement."))
@utils.arg(
    '--security-groups',
    default=None,
    metavar='<security-groups>',
    help=_("Comma separated list of security group names."))
@utils.arg(
    '--block-device-mapping',
    metavar="<dev-name=mapping>",
    action='append',
    default=[],
    help=_("Block device mapping in the format "
           "<dev-name>=<id>:<type>:<size(GB)>:<delete-on-terminate>."))
@utils.arg(
    '--block-device',
    metavar="key1=value1[,key2=value2...]",
    action='append',
    default=[],
    start_version='2.0',
    end_version='2.31',
    help=_("Block device mapping with the keys: "
           "id=UUID (image_id, snapshot_id or volume_id only if using source "
           "image, snapshot or volume) "
           "source=source type (image, snapshot, volume or blank), "
           "dest=destination type of the block device (volume or local), "
           "bus=device's bus (e.g. uml, lxc, virtio, ...; if omitted, "
           "hypervisor driver chooses a suitable default, "
           "honoured only if device type is supplied) "
           "type=device type (e.g. disk, cdrom, ...; defaults to 'disk') "
           "device=name of the device (e.g. vda, xda, ...; "
           "if omitted, hypervisor driver chooses suitable device "
           "depending on selected bus; note the libvirt driver always "
           "uses default device names), "
           "size=size of the block device in MB(for swap) and in "
           "GB(for other formats) "
           "(if omitted, hypervisor driver calculates size), "
           "format=device will be formatted (e.g. swap, ntfs, ...; optional), "
           "bootindex=integer used for ordering the boot disks "
           "(for image backed instances it is equal to 0, "
           "for others need to be specified) and "
           "shutdown=shutdown behaviour (either preserve or remove, "
           "for local destination set to remove)."))
@utils.arg(
    '--block-device',
    metavar="key1=value1[,key2=value2...]",
    action='append',
    default=[],
    start_version='2.32',
    help=_("Block device mapping with the keys: "
           "id=UUID (image_id, snapshot_id or volume_id only if using source "
           "image, snapshot or volume) "
           "source=source type (image, snapshot, volume or blank), "
           "dest=destination type of the block device (volume or local), "
           "bus=device's bus (e.g. uml, lxc, virtio, ...; if omitted, "
           "hypervisor driver chooses a suitable default, "
           "honoured only if device type is supplied) "
           "type=device type (e.g. disk, cdrom, ...; defaults to 'disk') "
           "device=name of the device (e.g. vda, xda, ...; "
           "tag=device metadata tag (optional) "
           "if omitted, hypervisor driver chooses suitable device "
           "depending on selected bus; note the libvirt driver always "
           "uses default device names), "
           "size=size of the block device in MB(for swap) and in "
           "GB(for other formats) "
           "(if omitted, hypervisor driver calculates size), "
           "format=device will be formatted (e.g. swap, ntfs, ...; optional), "
           "bootindex=integer used for ordering the boot disks "
           "(for image backed instances it is equal to 0, "
           "for others need to be specified) and "
           "shutdown=shutdown behaviour (either preserve or remove, "
           "for local destination set to remove)."))
@utils.arg(
    '--swap',
    metavar="<swap_size>",
    default=None,
    help=_("Create and attach a local swap block device of <swap_size> MB."))
@utils.arg(
    '--ephemeral',
    metavar="size=<size>[,format=<format>]",
    action='append',
    default=[],
    help=_("Create and attach a local ephemeral block device of <size> GB "
           "and format it to <format>."))
@utils.arg(
    '--hint',
    action='append',
    dest='scheduler_hints',
    default=[],
    metavar='<key=value>',
    help=_("Send arbitrary key/value pairs to the scheduler for custom "
           "use."))
@utils.arg(
    '--nic',
    metavar="<net-id=net-uuid,net-name=network-name,v4-fixed-ip=ip-addr,"
            "v6-fixed-ip=ip-addr,port-id=port-uuid>",
    action='append',
    dest='nics',
    default=[],
    start_version='2.0',
    end_version='2.31',
    help=_("Create a NIC on the server. "
           "Specify option multiple times to create multiple NICs. "
           "net-id: attach NIC to network with this UUID "
           "net-name: attach NIC to network with this name "
           "(either port-id or net-id or net-name must be provided), "
           "v4-fixed-ip: IPv4 fixed address for NIC (optional), "
           "v6-fixed-ip: IPv6 fixed address for NIC (optional), "
           "port-id: attach NIC to port with this UUID "
           "(either port-id or net-id must be provided)."))
@utils.arg(
    '--nic',
    metavar="<net-id=net-uuid,net-name=network-name,v4-fixed-ip=ip-addr,"
            "v6-fixed-ip=ip-addr,port-id=port-uuid>",
    action='append',
    dest='nics',
    default=[],
    start_version='2.32',
    end_version='2.36',
    help=_("Create a NIC on the server. "
           "Specify option multiple times to create multiple nics. "
           "net-id: attach NIC to network with this UUID "
           "net-name: attach NIC to network with this name "
           "(either port-id or net-id or net-name must be provided), "
           "v4-fixed-ip: IPv4 fixed address for NIC (optional), "
           "v6-fixed-ip: IPv6 fixed address for NIC (optional), "
           "port-id: attach NIC to port with this UUID "
           "tag: interface metadata tag (optional) "
           "(either port-id or net-id must be provided)."))
@utils.arg(
    '--nic',
    metavar="<auto,none,"
            "net-id=net-uuid,net-name=network-name,port-id=port-uuid,"
            "v4-fixed-ip=ip-addr,v6-fixed-ip=ip-addr,tag=tag>",
    action='append',
    dest='nics',
    default=[],
    start_version='2.37',
    help=_("Create a NIC on the server. "
           "Specify option multiple times to create multiple nics unless "
           "using the special 'auto' or 'none' values. "
           "auto: automatically allocate network resources if none are "
           "available. This cannot be specified with any other nic value and "
           "cannot be specified multiple times. "
           "none: do not attach a NIC at all. This cannot be specified "
           "with any other nic value and cannot be specified multiple times. "
           "net-id: attach NIC to network with a specific UUID. "
           "net-name: attach NIC to network with this name "
           "(either port-id or net-id or net-name must be provided), "
           "v4-fixed-ip: IPv4 fixed address for NIC (optional), "
           "v6-fixed-ip: IPv6 fixed address for NIC (optional), "
           "port-id: attach NIC to port with this UUID "
           "tag: interface metadata tag (optional) "
           "(either port-id or net-id must be provided)."))
@utils.arg(
    '--config-drive',
    metavar="<value>",
    dest='config_drive',
    default=False,
    help=_("Enable config drive."))
@utils.arg(
    '--poll',
    dest='poll',
    action="store_true",
    default=False,
    help=_('Report the new server boot progress until it completes.'))
@utils.arg(
    '--admin-pass',
    dest='admin_pass',
    metavar='<value>',
    default=None,
    help=_('Admin password for the instance.'))
@utils.arg(
    '--access-ip-v4',
    dest='access_ip_v4',
    metavar='<value>',
    default=None,
    help=_('Alternative access IPv4 of the instance.'))
@utils.arg(
    '--access-ip-v6',
    dest='access_ip_v6',
    metavar='<value>',
    default=None,
    help=_('Alternative access IPv6 of the instance.'))
@utils.arg(
    '--description',
    metavar='<description>',
    dest='description',
    default=None,
    help=_('Description for the server.'),
    start_version="2.19")
def do_boot(cs, args):
    """Boot a new server."""
    boot_args, boot_kwargs = _boot(cs, args)

    extra_boot_kwargs = utils.get_resource_manager_extra_kwargs(do_boot, args)
    boot_kwargs.update(extra_boot_kwargs)

    server = cs.servers.create(*boot_args, **boot_kwargs)
    _print_server(cs, args, server)

    if args.poll:
        _poll_for_status(cs.servers.get, server.id, 'building', ['active'])


def do_cloudpipe_list(cs, _args):
    """Print a list of all cloudpipe instances."""
    cloudpipes = cs.cloudpipe.list()
    columns = ['Project Id', "Public IP", "Public Port", "Internal IP"]
    utils.print_list(cloudpipes, columns)


@utils.arg(
    'project',
    metavar='<project_id>',
    help=_('UUID of the project to create the cloudpipe for.'))
def do_cloudpipe_create(cs, args):
    """Create a cloudpipe instance for the given project."""
    cs.cloudpipe.create(args.project)


@utils.arg('address', metavar='<ip address>', help=_('New IP Address.'))
@utils.arg('port', metavar='<port>', help=_('New Port.'))
def do_cloudpipe_configure(cs, args):
    """Update the VPN IP/port of a cloudpipe instance."""
    cs.cloudpipe.update(args.address, args.port)


def _poll_for_status(poll_fn, obj_id, action, final_ok_states,
                     poll_period=5, show_progress=True,
                     status_field="status", silent=False):
    """Block while an action is being performed, periodically printing
    progress.
    """
    def print_progress(progress):
        if show_progress:
            msg = (_('\rServer %(action)s... %(progress)s%% complete')
                   % dict(action=action, progress=progress))
        else:
            msg = _('\rServer %(action)s...') % dict(action=action)

        sys.stdout.write(msg)
        sys.stdout.flush()

    if not silent:
        print()

    while True:
        obj = poll_fn(obj_id)

        status = getattr(obj, status_field)

        if status:
            status = status.lower()

        progress = getattr(obj, 'progress', None) or 0
        if status in final_ok_states:
            if not silent:
                print_progress(100)
                print(_("\nFinished"))
            break
        elif status == "error":
            if not silent:
                print(_("\nError %s server") % action)
            raise exceptions.ResourceInErrorState(obj)
        elif status == "deleted":
            if not silent:
                print(_("\nDeleted %s server") % action)
            raise exceptions.InstanceInDeletedState(obj.fault["message"])

        if not silent:
            print_progress(progress)

        time.sleep(poll_period)


def _translate_keys(collection, convert):
    for item in collection:
        keys = item.__dict__.keys()
        for from_key, to_key in convert:
            if from_key in keys and to_key not in keys:
                setattr(item, to_key, item._info[from_key])


def _translate_extended_states(collection):
    power_states = [
        'NOSTATE',      # 0x00
        'Running',      # 0x01
        '',             # 0x02
        'Paused',       # 0x03
        'Shutdown',     # 0x04
        '',             # 0x05
        'Crashed',      # 0x06
        'Suspended'     # 0x07
    ]

    for item in collection:
        try:
            setattr(item, 'power_state',
                    power_states[getattr(item, 'power_state')])
        except AttributeError:
            setattr(item, 'power_state', "N/A")
        try:
            getattr(item, 'task_state')
        except AttributeError:
            setattr(item, 'task_state', "N/A")


def _translate_flavor_keys(collection):
    _translate_keys(collection, [('ram', 'memory_mb')])


def _print_flavor_extra_specs(flavor):
    try:
        return flavor.get_keys()
    except exceptions.NotFound:
        return "N/A"


def _print_flavor_list(flavors, show_extra_specs=False):
    _translate_flavor_keys(flavors)

    headers = [
        'ID',
        'Name',
        'Memory_MB',
        'Disk',
        'Ephemeral',
        'Swap',
        'VCPUs',
        'RXTX_Factor',
        'Is_Public',
    ]

    if show_extra_specs:
        formatters = {'extra_specs': _print_flavor_extra_specs}
        headers.append('extra_specs')
    else:
        formatters = {}

    utils.print_list(flavors, headers, formatters)


@utils.arg(
    '--extra-specs',
    dest='extra_specs',
    action='store_true',
    default=False,
    help=_('Get extra-specs of each flavor.'))
@utils.arg(
    '--all',
    dest='all',
    action='store_true',
    default=False,
    help=_('Display all flavors (Admin only).'))
@utils.arg(
    '--marker',
    dest='marker',
    metavar='<marker>',
    default=None,
    help=_('The last flavor ID of the previous page; displays list of flavors'
           ' after "marker".'))
@utils.arg(
    '--limit',
    dest='limit',
    metavar='<limit>',
    type=int,
    default=None,
    help=_("Maximum number of flavors to display. If limit == -1, all flavors "
           "will be displayed. If limit is bigger than 'osapi_max_limit' "
           "option of Nova API, limit 'osapi_max_limit' will be used "
           "instead."))
def do_flavor_list(cs, args):
    """Print a list of available 'flavors' (sizes of servers)."""
    if args.all:
        flavors = cs.flavors.list(is_public=None)
    else:
        flavors = cs.flavors.list(marker=args.marker, limit=args.limit)
    _print_flavor_list(flavors, args.extra_specs)


@utils.arg(
    'flavor',
    metavar='<flavor>',
    help=_("Name or ID of the flavor to delete."))
def do_flavor_delete(cs, args):
    """Delete a specific flavor"""
    flavorid = _find_flavor(cs, args.flavor)
    cs.flavors.delete(flavorid)
    _print_flavor_list([flavorid])


@utils.arg(
    'flavor',
    metavar='<flavor>',
    help=_("Name or ID of flavor."))
def do_flavor_show(cs, args):
    """Show details about the given flavor."""
    flavor = _find_flavor(cs, args.flavor)
    _print_flavor(flavor)


@utils.arg(
    'name',
    metavar='<name>',
    help=_("Unique name of the new flavor."))
@utils.arg(
    'id',
    metavar='<id>',
    help=_("Unique ID of the new flavor."
           " Specifying 'auto' will generated a UUID for the ID."))
@utils.arg(
    'ram',
    metavar='<ram>',
    help=_("Memory size in MB."))
@utils.arg(
    'disk',
    metavar='<disk>',
    help=_("Disk size in GB."))
@utils.arg(
    '--ephemeral',
    metavar='<ephemeral>',
    help=_("Ephemeral space size in GB (default 0)."),
    default=0)
@utils.arg(
    'vcpus',
    metavar='<vcpus>',
    help=_("Number of vcpus"))
@utils.arg(
    '--swap',
    metavar='<swap>',
    help=_("Swap space size in MB (default 0)."),
    default=0)
@utils.arg(
    '--rxtx-factor',
    metavar='<factor>',
    help=_("RX/TX factor (default 1)."),
    default=1.0)
@utils.arg(
    '--is-public',
    metavar='<is-public>',
    help=_("Make flavor accessible to the public (default true)."),
    type=lambda v: strutils.bool_from_string(v, True),
    default=True)
def do_flavor_create(cs, args):
    """Create a new flavor."""
    f = cs.flavors.create(args.name, args.ram, args.vcpus, args.disk, args.id,
                          args.ephemeral, args.swap, args.rxtx_factor,
                          args.is_public)
    _print_flavor_list([f])


@utils.arg(
    'flavor',
    metavar='<flavor>',
    help=_("Name or ID of flavor."))
@utils.arg(
    'action',
    metavar='<action>',
    choices=['set', 'unset'],
    help=_("Actions: 'set' or 'unset'."))
@utils.arg(
    'metadata',
    metavar='<key=value>',
    nargs='+',
    action='append',
    default=[],
    help=_('Extra_specs to set/unset (only key is necessary on unset).'))
def do_flavor_key(cs, args):
    """Set or unset extra_spec for a flavor."""
    flavor = _find_flavor(cs, args.flavor)
    keypair = _extract_metadata(args)

    if args.action == 'set':
        flavor.set_keys(keypair)
    elif args.action == 'unset':
        flavor.unset_keys(keypair.keys())


@utils.arg(
    '--flavor',
    metavar='<flavor>',
    help=_("Filter results by flavor name or ID."))
@utils.arg(
    '--tenant', metavar='<tenant_id>',
    help=_('Filter results by tenant ID.'),
    action=shell.DeprecatedAction,
    real_action='nothing',
    use=_('this option is not supported, and will be '
          'removed in version 5.0.0.'))
def do_flavor_access_list(cs, args):
    """Print access information about the given flavor."""
    if args.flavor:
        flavor = _find_flavor(cs, args.flavor)
        if flavor.is_public:
            raise exceptions.CommandError(_("Access list not available "
                                            "for public flavors."))
        kwargs = {'flavor': flavor}
    else:
        raise exceptions.CommandError(_("Unable to get all access lists. "
                                        "Specify --flavor"))

    try:
        access_list = cs.flavor_access.list(**kwargs)
    except NotImplementedError as e:
        raise exceptions.CommandError("%s" % str(e))

    columns = ['Flavor_ID', 'Tenant_ID']
    utils.print_list(access_list, columns)


@utils.arg(
    'flavor',
    metavar='<flavor>',
    help=_("Flavor name or ID to add access for the given tenant."))
@utils.arg(
    'tenant', metavar='<tenant_id>',
    help=_('Tenant ID to add flavor access for.'))
def do_flavor_access_add(cs, args):
    """Add flavor access for the given tenant."""
    flavor = _find_flavor(cs, args.flavor)
    access_list = cs.flavor_access.add_tenant_access(flavor, args.tenant)
    columns = ['Flavor_ID', 'Tenant_ID']
    utils.print_list(access_list, columns)


@utils.arg(
    'flavor',
    metavar='<flavor>',
    help=_("Flavor name or ID to remove access for the given tenant."))
@utils.arg(
    'tenant', metavar='<tenant_id>',
    help=_('Tenant ID to remove flavor access for.'))
def do_flavor_access_remove(cs, args):
    """Remove flavor access for the given tenant."""
    flavor = _find_flavor(cs, args.flavor)
    access_list = cs.flavor_access.remove_tenant_access(flavor, args.tenant)
    columns = ['Flavor_ID', 'Tenant_ID']
    utils.print_list(access_list, columns)


@utils.arg(
    'project_id', metavar='<project_id>',
    help=_('The ID of the project.'))
@deprecated_network
def do_scrub(cs, args):
    """Delete networks and security groups associated with a project."""
    networks_list = cs.networks.list()
    networks_list = [network for network in networks_list
                     if getattr(network, 'project_id', '') == args.project_id]
    search_opts = {'all_tenants': 1}
    groups = cs.security_groups.list(search_opts)
    groups = [group for group in groups
              if group.tenant_id == args.project_id]
    for network in networks_list:
        cs.networks.disassociate(network)
    for group in groups:
        cs.security_groups.delete(group)


@utils.arg(
    '--fields',
    default=None,
    metavar='<fields>',
    help=_('Comma-separated list of fields to display. '
           'Use the show command to see which fields are available.'))
@deprecated_network
def do_network_list(cs, args):
    """Print a list of available networks."""
    network_list = cs.networks.list()
    columns = ['ID', 'Label', 'Cidr']
    columns += _get_list_table_columns_and_formatters(
        args.fields, network_list,
        exclude_fields=(c.lower() for c in columns))[0]
    utils.print_list(network_list, columns)


@utils.arg(
    'network',
    metavar='<network>',
    help=_("UUID or label of network."))
@deprecated_network
def do_network_show(cs, args):
    """Show details about the given network."""
    network = utils.find_resource(cs.networks, args.network)
    utils.print_dict(network._info)


@utils.arg(
    'network',
    metavar='<network>',
    help=_("UUID or label of network."))
@deprecated_network
def do_network_delete(cs, args):
    """Delete network by label or id."""
    network = utils.find_resource(cs.networks, args.network)
    network.delete()


@utils.arg(
    '--host-only',
    dest='host_only',
    metavar='<0|1>',
    nargs='?',
    type=int,
    const=1,
    default=0)
@utils.arg(
    '--project-only',
    dest='project_only',
    metavar='<0|1>',
    nargs='?',
    type=int,
    const=1,
    default=0)
@utils.arg(
    'network',
    metavar='<network>',
    help=_("UUID of network."))
@deprecated_network
def do_network_disassociate(cs, args):
    """Disassociate host and/or project from the given network."""
    if args.host_only:
        cs.networks.disassociate(args.network, True, False)
    elif args.project_only:
        cs.networks.disassociate(args.network, False, True)
    else:
        cs.networks.disassociate(args.network, True, True)


@utils.arg(
    'network',
    metavar='<network>',
    help=_("UUID of network."))
@utils.arg(
    'host',
    metavar='<host>',
    help=_("Name of host"))
@deprecated_network
def do_network_associate_host(cs, args):
    """Associate host with network."""
    cs.networks.associate_host(args.network, args.host)


@utils.arg(
    'network',
    metavar='<network>',
    help=_("UUID of network."))
@deprecated_network
def do_network_associate_project(cs, args):
    """Associate project with network."""
    cs.networks.associate_project(args.network)


def _filter_network_create_options(args):
    valid_args = ['label', 'cidr', 'vlan_start', 'vpn_start', 'cidr_v6',
                  'gateway', 'gateway_v6', 'bridge', 'bridge_interface',
                  'multi_host', 'dns1', 'dns2', 'uuid', 'fixed_cidr',
                  'project_id', 'priority', 'vlan', 'mtu', 'dhcp_server',
                  'allowed_start', 'allowed_end']
    kwargs = {}
    for k, v in args.__dict__.items():
        if k in valid_args and v is not None:
            kwargs[k] = v

    return kwargs


@utils.arg(
    'label',
    metavar='<network_label>',
    help=_("Label for network"))
@utils.arg(
    '--fixed-range-v4',
    dest='cidr',
    metavar='<x.x.x.x/yy>',
    help=_("IPv4 subnet (ex: 10.0.0.0/8)"))
@utils.arg(
    '--fixed-range-v6',
    dest="cidr_v6",
    help=_('IPv6 subnet (ex: fe80::/64'))
@utils.arg(
    '--vlan',
    dest='vlan',
    type=int,
    metavar='<vlan id>',
    help=_("The vlan ID to be assigned to the project."))
@utils.arg(
    '--vlan-start',
    dest='vlan_start',
    type=int,
    metavar='<vlan start>',
    help=_('First vlan ID to be assigned to the project. Subsequent vlan '
           'IDs will be assigned incrementally.'))
@utils.arg(
    '--vpn',
    dest='vpn_start',
    type=int,
    metavar='<vpn start>',
    help=_("vpn start"))
@utils.arg(
    '--gateway',
    dest="gateway",
    help=_('gateway'))
@utils.arg(
    '--gateway-v6',
    dest="gateway_v6",
    help=_('IPv6 gateway'))
@utils.arg(
    '--bridge',
    dest="bridge",
    metavar='<bridge>',
    help=_('VIFs on this network are connected to this bridge.'))
@utils.arg(
    '--bridge-interface',
    dest="bridge_interface",
    metavar='<bridge interface>',
    help=_('The bridge is connected to this interface.'))
@utils.arg(
    '--multi-host',
    dest="multi_host",
    metavar="<'T'|'F'>",
    help=_('Multi host'))
@utils.arg(
    '--dns1',
    dest="dns1",
    metavar="<DNS Address>", help=_('First DNS.'))
@utils.arg(
    '--dns2',
    dest="dns2",
    metavar="<DNS Address>",
    help=_('Second DNS.'))
@utils.arg(
    '--uuid',
    dest="uuid",
    metavar="<network uuid>",
    help=_('Network UUID.'))
@utils.arg(
    '--fixed-cidr',
    dest="fixed_cidr",
    metavar='<x.x.x.x/yy>',
    help=_('IPv4 subnet for fixed IPs (ex: 10.20.0.0/16).'))
@utils.arg(
    '--project-id',
    dest="project_id",
    metavar="<project id>",
    help=_('Project ID.'))
@utils.arg(
    '--priority',
    dest="priority",
    metavar="<number>",
    help=_('Network interface priority.'))
@utils.arg(
    '--mtu',
    dest="mtu",
    type=int,
    help=_('MTU for network.'))
@utils.arg(
    '--enable-dhcp',
    dest="enable_dhcp",
    metavar="<'T'|'F'>",
    help=_('Enable DHCP.'))
@utils.arg(
    '--dhcp-server',
    dest="dhcp_server",
    help=_('DHCP-server address (defaults to gateway address)'))
@utils.arg(
    '--share-address',
    dest="share_address",
    metavar="<'T'|'F'>",
    help=_('Share address'))
@utils.arg(
    '--allowed-start',
    dest="allowed_start",
    help=_('Start of allowed addresses for instances.'))
@utils.arg(
    '--allowed-end',
    dest="allowed_end",
    help=_('End of allowed addresses for instances.'))
@deprecated_network
def do_network_create(cs, args):
    """Create a network."""

    if not (args.cidr or args.cidr_v6):
        raise exceptions.CommandError(
            _("Must specify either fixed_range_v4 or fixed_range_v6"))
    kwargs = _filter_network_create_options(args)
    if args.multi_host is not None:
        kwargs['multi_host'] = bool(args.multi_host == 'T' or
                                    strutils.bool_from_string(args.multi_host))
    if args.enable_dhcp is not None:
        kwargs['enable_dhcp'] = bool(
            args.enable_dhcp == 'T' or
            strutils.bool_from_string(args.enable_dhcp))
    if args.share_address is not None:
        kwargs['share_address'] = bool(
            args.share_address == 'T' or
            strutils.bool_from_string(args.share_address))

    cs.networks.create(**kwargs)


@utils.arg(
    '--limit',
    dest="limit",
    metavar="<limit>",
    help=_('Number of images to return per request.'))
def do_image_list(cs, _args):
    """DEPRECATED: Print a list of available images to boot from."""
    emit_image_deprecation_warning('image-list')
    limit = _args.limit
    image_list = cs.images.list(limit=limit)

    def parse_server_name(image):
        try:
            return image.server['id']
        except (AttributeError, KeyError):
            return ''

    fmts = {'Server': parse_server_name}
    utils.print_list(image_list, ['ID', 'Name', 'Status', 'Server'],
                     fmts, sortby_index=1)


@utils.arg(
    'image',
    metavar='<image>',
    help=_("Name or ID of image."))
@utils.arg(
    'action',
    metavar='<action>',
    choices=['set', 'delete'],
    help=_("Actions: 'set' or 'delete'."))
@utils.arg(
    'metadata',
    metavar='<key=value>',
    nargs='+',
    action='append',
    default=[],
    help=_('Metadata to add/update or delete (only key is necessary on '
           'delete).'))
def do_image_meta(cs, args):
    """DEPRECATED: Set or delete metadata on an image."""
    emit_image_deprecation_warning('image-meta')
    image = _find_image(cs, args.image)
    metadata = _extract_metadata(args)

    if args.action == 'set':
        cs.images.set_meta(image, metadata)
    elif args.action == 'delete':
        cs.images.delete_meta(image, metadata.keys())


def _extract_metadata(args):
    metadata = {}
    for metadatum in args.metadata[0]:
        # Can only pass the key in on 'delete'
        # So this doesn't have to have '='
        if metadatum.find('=') > -1:
            (key, value) = metadatum.split('=', 1)
        else:
            key = metadatum
            value = None

        metadata[key] = value
    return metadata


def _print_image(image):
    info = image._info.copy()

    # ignore links, we don't need to present those
    info.pop('links', None)

    # try to replace a server entity to just an id
    server = info.pop('server', None)
    try:
        info['server'] = server['id']
    except (KeyError, TypeError):
        pass

    # break up metadata and display each on its own row
    metadata = info.pop('metadata', {})
    try:
        for key, value in metadata.items():
            _key = 'metadata %s' % key
            info[_key] = value
    except AttributeError:
        pass

    utils.print_dict(info)


def _print_flavor(flavor):
    info = flavor._info.copy()
    # ignore links, we don't need to present those
    info.pop('links')
    info.update({"extra_specs": _print_flavor_extra_specs(flavor)})
    utils.print_dict(info)


@utils.arg(
    'image',
    metavar='<image>',
    help=_("Name or ID of image."))
def do_image_show(cs, args):
    """DEPRECATED: Show details about the given image."""
    emit_image_deprecation_warning('image-show')
    image = _find_image(cs, args.image)
    _print_image(image)


@utils.arg(
    'image', metavar='<image>', nargs='+',
    help=_('Name or ID of image(s).'))
def do_image_delete(cs, args):
    """DEPRECATED: Delete specified image(s)."""
    emit_image_deprecation_warning('image-delete')
    for image in args.image:
        try:
            # _find_image is using the GlanceManager which doesn't implement
            # the delete() method so use the ImagesManager for that.
            image = _find_image(cs, image)
            cs.images.delete(image)
        except Exception as e:
            print(_("Delete for image %(image)s failed: %(e)s") %
                  {'image': image, 'e': e})


@utils.arg(
    '--reservation-id',
    dest='reservation_id',
    metavar='<reservation-id>',
    default=None,
    help=_('Only return servers that match reservation-id.'))
@utils.arg(
    '--ip',
    dest='ip',
    metavar='<ip-regexp>',
    default=None,
    help=_('Search with regular expression match by IP address.'))
@utils.arg(
    '--ip6',
    dest='ip6',
    metavar='<ip6-regexp>',
    default=None,
    help=_('Search with regular expression match by IPv6 address.'))
@utils.arg(
    '--name',
    dest='name',
    metavar='<name-regexp>',
    default=None,
    help=_('Search with regular expression match by name.'))
@utils.arg(
    '--instance-name',
    dest='instance_name',
    metavar='<name-regexp>',
    default=None,
    help=_('Search with regular expression match by server name.'))
@utils.arg(
    '--status',
    dest='status',
    metavar='<status>',
    default=None,
    help=_('Search by server status.'))
@utils.arg(
    '--flavor',
    dest='flavor',
    metavar='<flavor>',
    default=None,
    help=_('Search by flavor name or ID.'))
@utils.arg(
    '--image',
    dest='image',
    metavar='<image>',
    default=None,
    help=_('Search by image name or ID.'))
@utils.arg(
    '--host',
    dest='host',
    metavar='<hostname>',
    default=None,
    help=_('Search servers by hostname to which they are assigned (Admin '
           'only).'))
@utils.arg(
    '--all-tenants',
    dest='all_tenants',
    metavar='<0|1>',
    nargs='?',
    type=int,
    const=1,
    default=int(strutils.bool_from_string(
        os.environ.get("ALL_TENANTS", 'false'), True)),
    help=_('Display information from all tenants (Admin only).'))
@utils.arg(
    '--tenant',
    # nova db searches by project_id
    dest='tenant',
    metavar='<tenant>',
    nargs='?',
    help=_('Display information from single tenant (Admin only).'))
@utils.arg(
    '--user',
    dest='user',
    metavar='<user>',
    nargs='?',
    help=_('Display information from single user (Admin only).'))
@utils.arg(
    '--deleted',
    dest='deleted',
    action="store_true",
    default=False,
    help=_('Only display deleted servers (Admin only).'))
@utils.arg(
    '--fields',
    default=None,
    metavar='<fields>',
    help=_('Comma-separated list of fields to display. '
           'Use the show command to see which fields are available.'))
@utils.arg(
    '--minimal',
    dest='minimal',
    action="store_true",
    default=False,
    help=_('Get only UUID and name.'))
@utils.arg(
    '--sort',
    dest='sort',
    metavar='<key>[:<direction>]',
    help=_('Comma-separated list of sort keys and directions in the form '
           'of <key>[:<asc|desc>]. The direction defaults to descending if '
           'not specified.'))
@utils.arg(
    '--marker',
    dest='marker',
    metavar='<marker>',
    default=None,
    help=_('The last server UUID of the previous page; displays list of '
           'servers after "marker".'))
@utils.arg(
    '--limit',
    dest='limit',
    metavar='<limit>',
    type=int,
    default=None,
    help=_("Maximum number of servers to display. If limit == -1, all servers "
           "will be displayed. If limit is bigger than 'osapi_max_limit' "
           "option of Nova API, limit 'osapi_max_limit' will be used "
           "instead."))
@utils.arg(
    '--changes-since',
    dest='changes_since',
    metavar='<changes_since>',
    default=None,
    help=_("List only servers changed after a certain point of time."
           "The provided time should be an ISO 8061 formatted time."
           "ex 2016-03-04T06:27:59Z ."))
@utils.arg(
    '--tags',
    dest='tags',
    metavar='<tags>',
    default=None,
    help=_("The given tags must all be present for a server to be included in "
           "the list result. Boolean expression in this case is 't1 AND t2'. "
           "Tags must be separated by commas: --tags <tag1,tag2>"),
    start_version="2.26")
@utils.arg(
    '--tags-any',
    dest='tags-any',
    metavar='<tags-any>',
    default=None,
    help=_("If one of the given tags is present the server will be included "
           "in the list result. Boolean expression in this case is "
           "'t1 OR t2'. Tags must be separated by commas: "
           "--tags-any <tag1,tag2>"),
    start_version="2.26")
@utils.arg(
    '--not-tags',
    dest='not-tags',
    metavar='<not-tags>',
    default=None,
    help=_("Only the servers that do not have any of the given tags will"
           "be included in the list results. Boolean expression in this case "
           "is 'NOT(t1 AND t2)'. Tags must be separated by commas: "
           "--not-tags <tag1,tag2>"),
    start_version="2.26")
@utils.arg(
    '--not-tags-any',
    dest='not-tags-any',
    metavar='<not-tags-any>',
    default=None,
    help=_("Only the servers that do not have at least one of the given tags"
           "will be included in the list result. Boolean expression in this "
           "case is 'NOT(t1 OR t2)'. Tags must be separated by commas: "
           "--not-tags-any <tag1,tag2>"),
    start_version="2.26")
def do_list(cs, args):
    """List active servers."""
    imageid = None
    flavorid = None
    if args.image:
        imageid = _find_image(cs, args.image).id
    if args.flavor:
        flavorid = _find_flavor(cs, args.flavor).id
    # search by tenant or user only works with all_tenants
    if args.tenant or args.user:
        args.all_tenants = 1
    search_opts = {
        'all_tenants': args.all_tenants,
        'reservation_id': args.reservation_id,
        'ip': args.ip,
        'ip6': args.ip6,
        'name': args.name,
        'image': imageid,
        'flavor': flavorid,
        'status': args.status,
        'tenant_id': args.tenant,
        'user_id': args.user,
        'host': args.host,
        'deleted': args.deleted,
        'instance_name': args.instance_name,
        'changes-since': args.changes_since}

    for arg in ('tags', "tags-any", 'not-tags', 'not-tags-any'):
        if arg in args:
            search_opts[arg] = getattr(args, arg)

    filters = {'flavor': lambda f: f['id'],
               'security_groups': utils.format_security_groups}

    id_col = 'ID'

    detailed = not args.minimal

    sort_keys = []
    sort_dirs = []
    if args.sort:
        for sort in args.sort.split(','):
            sort_key, _sep, sort_dir = sort.partition(':')
            if not sort_dir:
                sort_dir = 'desc'
            elif sort_dir not in ('asc', 'desc'):
                raise exceptions.CommandError(_(
                    'Unknown sort direction: %s') % sort_dir)
            sort_keys.append(sort_key)
            sort_dirs.append(sort_dir)

    if search_opts['changes-since']:
        try:
            timeutils.parse_isotime(search_opts['changes-since'])
        except ValueError:
            raise exceptions.CommandError(_('Invalid changes-since value: %s')
                                          % search_opts['changes-since'])

    servers = cs.servers.list(detailed=detailed,
                              search_opts=search_opts,
                              sort_keys=sort_keys,
                              sort_dirs=sort_dirs,
                              marker=args.marker,
                              limit=args.limit)
    convert = [('OS-EXT-SRV-ATTR:host', 'host'),
               ('OS-EXT-STS:task_state', 'task_state'),
               ('OS-EXT-SRV-ATTR:instance_name', 'instance_name'),
               ('OS-EXT-STS:power_state', 'power_state'),
               ('hostId', 'host_id')]
    _translate_keys(servers, convert)
    _translate_extended_states(servers)

    formatters = {}

    cols, fmts = _get_list_table_columns_and_formatters(
        args.fields, servers, exclude_fields=('id',), filters=filters)

    if args.minimal:
        columns = [
            id_col,
            'Name']
    elif cols:
        columns = [id_col] + cols
        formatters.update(fmts)
    else:
        columns = [
            id_col,
            'Name',
            'Status',
            'Task State',
            'Power State',
            'Networks'
        ]
        # If getting the data for all tenants, print
        # Tenant ID as well
        if search_opts['all_tenants']:
            columns.insert(2, 'Tenant ID')
        if search_opts['changes-since']:
            columns.append('Updated')
    formatters['Networks'] = utils.format_servers_list_networks
    sortby_index = 1
    if args.sort:
        sortby_index = None
    utils.print_list(servers, columns,
                     formatters, sortby_index=sortby_index)


def _get_list_table_columns_and_formatters(fields, objs, exclude_fields=(),
                                           filters=None):
    """Check and add fields to output columns.

    If there is any value in fields that not an attribute of obj,
    CommandError will be raised.

    If fields has duplicate values (case sensitive), we will make them unique
    and ignore duplicate ones.

    If exclude_fields is specified, any field both in fields and
    exclude_fields will be ignored.

    :param fields: A list of string contains the fields to be printed.
    :param objs: An list of object which will be used to check if field is
                 valid or not. Note, we don't check fields if obj is None or
                 empty.
    :param exclude_fields: A tuple of string which contains the fields to be
                           excluded.
    :param filters: A dictionary defines how to get value from fields, this
                    is useful when field's value is a complex object such as
                    dictionary.

    :return: columns, formatters.
             columns is a list of string which will be used as table header.
             formatters is a dictionary specifies how to display the value
             of the field.
             They can be [], {}.
    :raise: novaclient.exceptions.CommandError
    """
    if not fields:
        return [], {}

    if not objs:
        obj = None
    elif isinstance(objs, list):
        obj = objs[0]
    else:
        obj = objs

    columns = []
    formatters = {}

    non_existent_fields = []
    exclude_fields = set(exclude_fields)

    for field in fields.split(','):
        if not hasattr(obj, field):
            non_existent_fields.append(field)
            continue
        if field in exclude_fields:
            continue
        field_title, formatter = utils.make_field_formatter(field,
                                                            filters)
        columns.append(field_title)
        formatters[field_title] = formatter
        exclude_fields.add(field)

    if non_existent_fields:
        raise exceptions.CommandError(
            _("Non-existent fields are specified: %s") % non_existent_fields)

    return columns, formatters


@utils.arg(
    '--hard',
    dest='reboot_type',
    action='store_const',
    const=servers.REBOOT_HARD,
    default=servers.REBOOT_SOFT,
    help=_('Perform a hard reboot (instead of a soft one). '
           'Note: Ironic does not currently support soft reboot; '
           'consequently, bare metal nodes will always do a hard '
           'reboot, regardless of the use of this option.'))
@utils.arg(
    'server',
    metavar='<server>', nargs='+',
    help=_('Name or ID of server(s).'))
@utils.arg(
    '--poll',
    dest='poll',
    action="store_true",
    default=False,
    help=_('Poll until reboot is complete.'))
def do_reboot(cs, args):
    """Reboot a server."""
    servers = [_find_server(cs, s) for s in args.server]
    utils.do_action_on_many(
        lambda s: s.reboot(args.reboot_type),
        servers,
        _("Request to reboot server %s has been accepted."),
        _("Unable to reboot the specified server(s)."))

    if args.poll:
        utils.do_action_on_many(
            lambda s: _poll_for_status(cs.servers.get, s.id, 'rebooting',
                                       ['active'], show_progress=False),
            servers,
            _("Wait for server %s reboot."),
            _("Wait for specified server(s) failed."))


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg('image', metavar='<image>', help=_("Name or ID of new image."))
@utils.arg(
    '--rebuild-password',
    dest='rebuild_password',
    metavar='<rebuild-password>',
    default=False,
    help=_("Set the provided admin password on the rebuilt server."))
@utils.arg(
    '--poll',
    dest='poll',
    action="store_true",
    default=False,
    help=_('Report the server rebuild progress until it completes.'))
@utils.arg(
    '--minimal',
    dest='minimal',
    action="store_true",
    default=False,
    help=_('Skips flavor/image lookups when showing servers.'))
@utils.arg(
    '--preserve-ephemeral',
    action="store_true",
    default=False,
    help=_('Preserve the default ephemeral storage partition on rebuild.'))
@utils.arg(
    '--name',
    metavar='<name>',
    default=None,
    help=_('Name for the new server.'))
@utils.arg(
    '--description',
    metavar='<description>',
    dest='description',
    default=None,
    help=_('New description for the server.'),
    start_version="2.19")
@utils.arg(
    '--meta',
    metavar="<key=value>",
    action='append',
    default=[],
    help=_("Record arbitrary key/value metadata to /meta_data.json "
           "on the metadata server. Can be specified multiple times."))
@utils.arg(
    '--file',
    metavar="<dst-path=src-path>",
    action='append',
    dest='files',
    default=[],
    help=_("Store arbitrary files from <src-path> locally to <dst-path> "
           "on the new server. You may store up to 5 files."))
def do_rebuild(cs, args):
    """Shutdown, re-image, and re-boot a server."""
    server = _find_server(cs, args.server)
    image = _find_image(cs, args.image)

    if args.rebuild_password is not False:
        _password = args.rebuild_password
    else:
        _password = None

    kwargs = utils.get_resource_manager_extra_kwargs(do_rebuild, args)
    kwargs['preserve_ephemeral'] = args.preserve_ephemeral
    kwargs['name'] = args.name
    if 'description' in args:
        kwargs['description'] = args.description
    meta = _meta_parsing(args.meta)
    kwargs['meta'] = meta

    files = {}
    for f in args.files:
        try:
            dst, src = f.split('=', 1)
            with open(src, 'r') as s:
                files[dst] = s.read()
        except IOError as e:
            raise exceptions.CommandError(_("Can't open '%(src)s': %(exc)s") %
                                          {'src': src, 'exc': e})
        except ValueError:
            raise exceptions.CommandError(_("Invalid file argument '%s'. "
                                            "File arguments must be of the "
                                            "form '--file "
                                            "<dst-path=src-path>'") % f)
    kwargs['files'] = files
    server = server.rebuild(image, _password, **kwargs)
    _print_server(cs, args, server)

    if args.poll:
        _poll_for_status(cs.servers.get, server.id, 'rebuilding', ['active'])


@utils.arg(
    'server', metavar='<server>',
    help=_('Name (old name) or ID of server.'))
@utils.arg('name', metavar='<name>', help=_('New name for the server.'))
def do_rename(cs, args):
    """DEPRECATED, use update instead."""
    do_update(cs, args)


@utils.arg(
    'server', metavar='<server>',
    help=_('Name (old name) or ID of server.'))
@utils.arg(
    '--name',
    metavar='<name>',
    dest='name',
    default=None,
    help=_('New name for the server.'))
@utils.arg(
    '--description',
    metavar='<description>',
    dest='description',
    default=None,
    help=_('New description for the server. If it equals to empty string '
           '(i.g. ""), the server description will be removed.'),
    start_version="2.19")
def do_update(cs, args):
    """Update the name or the description for a server."""
    update_kwargs = {}
    if args.name:
        update_kwargs["name"] = args.name
    # NOTE(andreykurilin): `do_update` method is used by `do_rename` method,
    # which do not have description argument at all. When `do_rename` will be
    # removed after deprecation period, feel free to change the check below to:
    #     `if args.description:`
    if "description" in args and args.description is not None:
        update_kwargs["description"] = args.description
    _find_server(cs, args.server).update(**update_kwargs)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg(
    'flavor',
    metavar='<flavor>',
    help=_("Name or ID of new flavor."))
@utils.arg(
    '--poll',
    dest='poll',
    action="store_true",
    default=False,
    help=_('Report the server resize progress until it completes.'))
def do_resize(cs, args):
    """Resize a server."""
    server = _find_server(cs, args.server)
    flavor = _find_flavor(cs, args.flavor)
    kwargs = utils.get_resource_manager_extra_kwargs(do_resize, args)
    server.resize(flavor, **kwargs)
    if args.poll:
        _poll_for_status(cs.servers.get, server.id, 'resizing',
                         ['active', 'verify_resize'])


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_resize_confirm(cs, args):
    """Confirm a previous resize."""
    _find_server(cs, args.server).confirm_resize()


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_resize_revert(cs, args):
    """Revert a previous resize (and return to the previous VM)."""
    _find_server(cs, args.server).revert_resize()


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg(
    '--poll',
    dest='poll',
    action="store_true",
    default=False,
    help=_('Report the server migration progress until it completes.'))
def do_migrate(cs, args):
    """Migrate a server. The new host will be selected by the scheduler."""
    server = _find_server(cs, args.server)
    server.migrate()

    if args.poll:
        _poll_for_status(cs.servers.get, server.id, 'migrating',
                         ['active', 'verify_resize'])


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_pause(cs, args):
    """Pause a server."""
    _find_server(cs, args.server).pause()


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_unpause(cs, args):
    """Unpause a server."""
    _find_server(cs, args.server).unpause()


@utils.arg(
    '--all-tenants',
    action='store_const',
    const=1,
    default=0,
    help=_('Stop server(s) in another tenant by name (Admin only).'))
@utils.arg(
    'server',
    metavar='<server>', nargs='+',
    help=_('Name or ID of server(s).'))
def do_stop(cs, args):
    """Stop the server(s)."""
    find_args = {'all_tenants': args.all_tenants}
    utils.do_action_on_many(
        lambda s: _find_server(cs, s, **find_args).stop(),
        args.server,
        _("Request to stop server %s has been accepted."),
        _("Unable to stop the specified server(s)."))


@utils.arg(
    '--all-tenants',
    action='store_const',
    const=1,
    default=0,
    help=_('Start server(s) in another tenant by name (Admin only).'))
@utils.arg(
    'server',
    metavar='<server>', nargs='+',
    help=_('Name or ID of server(s).'))
def do_start(cs, args):
    """Start the server(s)."""
    find_args = {'all_tenants': args.all_tenants}
    utils.do_action_on_many(
        lambda s: _find_server(cs, s, **find_args).start(),
        args.server,
        _("Request to start server %s has been accepted."),
        _("Unable to start the specified server(s)."))


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_lock(cs, args):
    """Lock a server. A normal (non-admin) user will not be able to execute
    actions on a locked server.
    """
    _find_server(cs, args.server).lock()


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_unlock(cs, args):
    """Unlock a server."""
    _find_server(cs, args.server).unlock()


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_suspend(cs, args):
    """Suspend a server."""
    _find_server(cs, args.server).suspend()


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_resume(cs, args):
    """Resume a server."""
    _find_server(cs, args.server).resume()


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg(
    '--password',
    metavar='<password>',
    dest='password',
    help=_('The admin password to be set in the rescue environment.'))
@utils.arg(
    '--image',
    metavar='<image>',
    dest='image',
    help=_('The image to rescue with.'))
def do_rescue(cs, args):
    """Reboots a server into rescue mode, which starts the machine
    from either the initial image or a specified image, attaching the current
    boot disk as secondary.
    """
    kwargs = {}
    if args.image:
        kwargs['image'] = _find_image(cs, args.image)
    if args.password:
        kwargs['password'] = args.password
    utils.print_dict(_find_server(cs, args.server).rescue(**kwargs)[1])


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_unrescue(cs, args):
    """Restart the server from normal boot disk again."""
    _find_server(cs, args.server).unrescue()


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_shelve(cs, args):
    """Shelve a server."""
    _find_server(cs, args.server).shelve()


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_shelve_offload(cs, args):
    """Remove a shelved server from the compute node."""
    _find_server(cs, args.server).shelve_offload()


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_unshelve(cs, args):
    """Unshelve a server."""
    _find_server(cs, args.server).unshelve()


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_diagnostics(cs, args):
    """Retrieve server diagnostics."""
    server = _find_server(cs, args.server)
    utils.print_dict(cs.servers.diagnostics(server)[1], wrap=80)


@utils.arg(
    'server', metavar='<server>',
    help=_('Name or ID of a server for which the network cache should '
           'be refreshed from neutron (Admin only).'))
def do_refresh_network(cs, args):
    """Refresh server network information."""
    server = _find_server(cs, args.server)
    cs.server_external_events.create([{'server_uuid': server.id,
                                       'name': 'network-changed'}])


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_root_password(cs, args):
    """DEPRECATED, use set-password instead."""
    do_set_password(cs, args)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_set_password(cs, args):
    """
    Change the admin password for a server.
    """
    server = _find_server(cs, args.server)
    p1 = getpass.getpass('New password: ')
    p2 = getpass.getpass('Again: ')
    if p1 != p2:
        raise exceptions.CommandError(_("Passwords do not match."))
    server.change_password(p1)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg('name', metavar='<name>', help=_('Name of snapshot.'))
@utils.arg(
    '--metadata',
    metavar="<key=value>",
    action='append',
    default=[],
    help=_("Record arbitrary key/value metadata to /meta_data.json "
           "on the metadata server. Can be specified multiple times."))
@utils.arg(
    '--show',
    dest='show',
    action="store_true",
    default=False,
    help=_('Print image info.'))
@utils.arg(
    '--poll',
    dest='poll',
    action="store_true",
    default=False,
    help=_('Report the snapshot progress and poll until image creation is '
           'complete.'))
def do_image_create(cs, args):
    """Create a new image by taking a snapshot of a running server."""
    server = _find_server(cs, args.server)
    meta = _meta_parsing(args.metadata) or None
    image_uuid = cs.servers.create_image(server, args.name, meta)

    if args.poll:
        _poll_for_status(cs.glance.find_image, image_uuid, 'snapshotting',
                         ['active'])

        # NOTE(sirp):  A race-condition exists between when the image finishes
        # uploading and when the servers's `task_state` is cleared. To account
        # for this, we need to poll a second time to ensure the `task_state` is
        # cleared before returning, ensuring that a snapshot taken immediately
        # after this function returns will succeed.
        #
        # A better long-term solution will be to separate 'snapshotting' and
        # 'image-uploading' in Nova and clear the task-state once the VM
        # snapshot is complete but before the upload begins.
        task_state_field = "OS-EXT-STS:task_state"
        if hasattr(server, task_state_field):
            _poll_for_status(cs.servers.get, server.id, 'image_snapshot',
                             [None], status_field=task_state_field,
                             show_progress=False, silent=True)

    if args.show:
        _print_image(_find_image(cs, image_uuid))


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg('name', metavar='<name>', help=_('Name of the backup image.'))
@utils.arg(
    'backup_type', metavar='<backup-type>',
    help=_('The backup type, like "daily" or "weekly".'))
@utils.arg(
    'rotation', metavar='<rotation>',
    help=_('Int parameter representing how many backups to keep '
           'around.'))
def do_backup(cs, args):
    """Backup a server by creating a 'backup' type snapshot."""
    _find_server(cs, args.server).backup(args.name,
                                         args.backup_type,
                                         args.rotation)


@utils.arg(
    'server',
    metavar='<server>',
    help=_("Name or ID of server."))
@utils.arg(
    'action',
    metavar='<action>',
    choices=['set', 'delete'],
    help=_("Actions: 'set' or 'delete'."))
@utils.arg(
    'metadata',
    metavar='<key=value>',
    nargs='+',
    action='append',
    default=[],
    help=_('Metadata to set or delete (only key is necessary on delete).'))
def do_meta(cs, args):
    """Set or delete metadata on a server."""
    server = _find_server(cs, args.server)
    metadata = _extract_metadata(args)

    if args.action == 'set':
        cs.servers.set_meta(server, metadata)
    elif args.action == 'delete':
        cs.servers.delete_meta(server, sorted(metadata.keys(), reverse=True))


def _print_server(cs, args, server=None):
    # By default when searching via name we will do a
    # findall(name=blah) and due a REST /details which is not the same
    # as a .get() and doesn't get the information about flavors and
    # images. This fix it as we redo the call with the id which does a
    # .get() to get all information.
    if not server:
        server = _find_server(cs, args.server)

    minimal = getattr(args, "minimal", False)

    networks = server.networks
    info = server._info.copy()
    for network_label, address_list in networks.items():
        info['%s network' % network_label] = ', '.join(address_list)

    flavor = info.get('flavor', {})
    flavor_id = flavor.get('id', '')
    if minimal:
        info['flavor'] = flavor_id
    else:
        try:
            info['flavor'] = '%s (%s)' % (_find_flavor(cs, flavor_id).name,
                                          flavor_id)
        except Exception:
            info['flavor'] = '%s (%s)' % (_("Flavor not found"), flavor_id)

    if 'security_groups' in info:
        # when we have multiple nics the info will include the
        # security groups N times where N == number of nics. Be nice
        # and only display it once.
        info['security_groups'] = ', '.join(
            sorted(set(group['name'] for group in info['security_groups'])))

    image = info.get('image', {})
    if image:
        image_id = image.get('id', '')
        if minimal:
            info['image'] = image_id
        else:
            try:
                info['image'] = '%s (%s)' % (_find_image(cs, image_id).name,
                                             image_id)
            except Exception:
                info['image'] = '%s (%s)' % (_("Image not found"), image_id)
    else:  # Booted from volume
        info['image'] = _("Attempt to boot from volume - no image supplied")

    info.pop('links', None)
    info.pop('addresses', None)

    utils.print_dict(info)


@utils.arg(
    '--minimal',
    dest='minimal',
    action="store_true",
    default=False,
    help=_('Skips flavor/image lookups when showing servers.'))
@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_show(cs, args):
    """Show details about the given server."""
    _print_server(cs, args)


@utils.arg(
    '--all-tenants',
    action='store_const',
    const=1,
    default=0,
    help=_('Delete server(s) in another tenant by name (Admin only).'))
@utils.arg(
    'server', metavar='<server>', nargs='+',
    help=_('Name or ID of server(s).'))
def do_delete(cs, args):
    """Immediately shut down and delete specified server(s)."""
    find_args = {'all_tenants': args.all_tenants}
    utils.do_action_on_many(
        lambda s: _find_server(cs, s, **find_args).delete(),
        args.server,
        _("Request to delete server %s has been accepted."),
        _("Unable to delete the specified server(s)."))


def _find_server(cs, server, raise_if_notfound=True, **find_args):
    """Get a server by name or ID.

    :param cs: NovaClient's instance
    :param server: identifier of server
    :param raise_if_notfound: raise an exception if server is not found
    :param find_args: argument to search server
    """
    if raise_if_notfound:
        return utils.find_resource(cs.servers, server, **find_args)
    else:
        try:
            return utils.find_resource(cs.servers, server,
                                       wrap_exception=False)
        except exceptions.NoUniqueMatch as e:
            raise exceptions.CommandError(six.text_type(e))
        except exceptions.NotFound:
            # The server can be deleted
            return server


def _find_image(cs, image):
    """Get an image by name or ID."""
    try:
        return cs.glance.find_image(image)
    except (exceptions.NotFound, exceptions.NoUniqueMatch) as e:
        raise exceptions.CommandError(six.text_type(e))


def _find_flavor(cs, flavor):
    """Get a flavor by name, ID, or RAM size."""
    try:
        return utils.find_resource(cs.flavors, flavor, is_public=None)
    except exceptions.NotFound:
        return cs.flavors.find(ram=flavor)


def _find_network_id_neutron(cs, net_name):
    """Get unique network ID from network name from neutron"""
    try:
        return cs.neutron.find_network(net_name).id
    except (exceptions.NotFound, exceptions.NoUniqueMatch) as e:
        raise exceptions.CommandError(six.text_type(e))


def _find_network_id(cs, net_name):
    """Find the network id for a network name.

    If we have access to neutron in the service catalog, use neutron
    for this lookup, otherwise use nova. This ensures that we do the
    right thing in the future.

    Once nova network support is deleted, we can delete this check and
    the has_neutron function.
    """
    if cs.has_neutron():
        return _find_network_id_neutron(cs, net_name)
    else:
        # The network proxy API methods were deprecated in 2.36 and will return
        # a 404 so we fallback to 2.35 to maintain a transition for CLI users.
        want_version = api_versions.APIVersion('2.35')
        cur_version = cs.api_version
        if cs.api_version > want_version:
            cs.api_version = want_version
        try:
            return _find_network_id_novanet(cs, net_name)
        finally:
            cs.api_version = cur_version


def _find_network_id_novanet(cs, net_name):
    """Get unique network ID from network name."""
    network_id = None
    for net_info in cs.networks.list():
        if net_name == net_info.label:
            if network_id is not None:
                msg = (_("Multiple network name matches found for name '%s', "
                         "use network ID to be more specific.") % net_name)
                raise exceptions.NoUniqueMatch(msg)
            else:
                network_id = net_info.id

    if network_id is None:
        msg = (_("No network name match for name '%s'") % net_name)
        raise exceptions.ResourceNotFound(msg % {'network': net_name})
    else:
        return network_id


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg(
    'network_id',
    metavar='<network-id>',
    help=_('Network ID.'))
def do_add_fixed_ip(cs, args):
    """Add new IP address on a network to server."""
    server = _find_server(cs, args.server)
    server.add_fixed_ip(args.network_id)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg('address', metavar='<address>', help=_('IP Address.'))
def do_remove_fixed_ip(cs, args):
    """Remove an IP address from a server."""
    server = _find_server(cs, args.server)
    server.remove_fixed_ip(args.address)


def _find_volume(cs, volume):
    """Get a volume by name or ID."""
    return utils.find_resource(cs.volumes, volume)


def _find_volume_snapshot(cs, snapshot):
    """Get a volume snapshot by name or ID."""
    return utils.find_resource(cs.volume_snapshots, snapshot)


def _print_volume(volume):
    utils.print_dict(volume._info)


def _print_volume_snapshot(snapshot):
    utils.print_dict(snapshot._info)


def _translate_volume_keys(collection):
    _translate_keys(collection,
                    [('displayName', 'display_name'),
                     ('volumeType', 'volume_type')])


def _translate_volume_snapshot_keys(collection):
    _translate_keys(collection,
                    [('displayName', 'display_name'),
                     ('volumeId', 'volume_id')])


def _translate_availability_zone_keys(collection):
    _translate_keys(collection,
                    [('zoneName', 'name'), ('zoneState', 'status')])


def _translate_volume_attachments_keys(collection):
    _translate_keys(collection,
                    [('serverId', 'server_id'),
                     ('volumeId', 'volume_id')])


@utils.arg(
    'server',
    metavar='<server>',
    help=_('Name or ID of server.'))
@utils.arg(
    'volume',
    metavar='<volume>',
    help=_('ID of the volume to attach.'))
@utils.arg(
    'device', metavar='<device>', default=None, nargs='?',
    help=_('Name of the device e.g. /dev/vdb. '
           'Use "auto" for autoassign (if supported). '
           'Libvirt driver will use default device name.'))
def do_volume_attach(cs, args):
    """Attach a volume to a server."""
    if args.device == 'auto':
        args.device = None

    volume = cs.volumes.create_server_volume(_find_server(cs, args.server).id,
                                             args.volume,
                                             args.device)
    _print_volume(volume)


@utils.arg(
    'server',
    metavar='<server>',
    help=_('Name or ID of server.'))
@utils.arg(
    'attachment_id',
    metavar='<attachment>',
    help=_('Attachment ID of the volume.'))
@utils.arg(
    'new_volume',
    metavar='<volume>',
    help=_('ID of the volume to attach.'))
def do_volume_update(cs, args):
    """Update volume attachment."""
    cs.volumes.update_server_volume(_find_server(cs, args.server).id,
                                    args.attachment_id,
                                    args.new_volume)


@utils.arg(
    'server',
    metavar='<server>',
    help=_('Name or ID of server.'))
@utils.arg(
    'attachment_id',
    metavar='<volume>',
    help=_('ID of the volume to detach.'))
def do_volume_detach(cs, args):
    """Detach a volume from a server."""
    cs.volumes.delete_server_volume(_find_server(cs, args.server).id,
                                    args.attachment_id)


@utils.arg(
    'server',
    metavar='<server>',
    help=_('Name or ID of server.'))
def do_volume_attachments(cs, args):
    """List all the volumes attached to a server."""
    volumes = cs.volumes.get_server_volumes(_find_server(cs, args.server).id)
    _translate_volume_attachments_keys(volumes)
    utils.print_list(volumes, ['ID', 'DEVICE', 'SERVER ID', 'VOLUME ID'])


@api_versions.wraps('2.0', '2.5')
def console_dict_accessor(cs, data):
    return data['console']


@api_versions.wraps('2.6')
def console_dict_accessor(cs, data):
    return data['remote_console']


class Console(object):
    def __init__(self, console_dict):
        self.type = console_dict['type']
        self.url = console_dict['url']


def print_console(cs, data):
    utils.print_list([Console(console_dict_accessor(cs, data))],
                     ['Type', 'Url'])


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg(
    'console_type',
    metavar='<console-type>',
    help=_('Type of vnc console ("novnc" or "xvpvnc").'))
def do_get_vnc_console(cs, args):
    """Get a vnc console to a server."""
    server = _find_server(cs, args.server)
    data = server.get_vnc_console(args.console_type)

    print_console(cs, data)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg(
    'console_type',
    metavar='<console-type>',
    help=_('Type of spice console ("spice-html5").'))
def do_get_spice_console(cs, args):
    """Get a spice console to a server."""
    server = _find_server(cs, args.server)
    data = server.get_spice_console(args.console_type)

    print_console(cs, data)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg(
    'console_type',
    metavar='<console-type>',
    help=_('Type of rdp console ("rdp-html5").'))
def do_get_rdp_console(cs, args):
    """Get a rdp console to a server."""
    server = _find_server(cs, args.server)
    data = server.get_rdp_console(args.console_type)

    print_console(cs, data)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg(
    '--console-type',
    default='serial',
    help=_('Type of serial console, default="serial".'))
def do_get_serial_console(cs, args):
    """Get a serial console to a server."""
    if args.console_type not in ('serial',):
        raise exceptions.CommandError(
            _("Invalid parameter value for 'console_type', "
              "currently supported 'serial'."))

    server = _find_server(cs, args.server)
    data = server.get_serial_console(args.console_type)

    print_console(cs, data)


@api_versions.wraps('2.8')
@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_get_mks_console(cs, args):
    """Get an MKS console to a server."""
    server = _find_server(cs, args.server)
    data = server.get_mks_console()

    print_console(cs, data)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg(
    'private_key',
    metavar='<private-key>',
    help=_('Private key (used locally to decrypt password) (Optional). '
           'When specified, the command displays the clear (decrypted) VM '
           'password. When not specified, the ciphered VM password is '
           'displayed.'),
    nargs='?',
    default=None)
def do_get_password(cs, args):
    """Get the admin password for a server. This operation calls the metadata
    service to query metadata information and does not read password
    information from the server itself.
    """
    server = _find_server(cs, args.server)
    data = server.get_password(args.private_key)
    print(data)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_clear_password(cs, args):
    """Clear the admin password for a server from the metadata server.
    This action does not actually change the instance server password.
    """
    server = _find_server(cs, args.server)
    server.clear_password()


def _print_floating_ip_list(floating_ips):
    convert = [('instance_id', 'server_id')]
    _translate_keys(floating_ips, convert)

    utils.print_list(floating_ips,
                     ['Id', 'IP', 'Server Id', 'Fixed IP', 'Pool'])


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg(
    '--length',
    metavar='<length>',
    default=None,
    help=_('Length in lines to tail.'))
def do_console_log(cs, args):
    """Get console log output of a server."""
    server = _find_server(cs, args.server)
    data = server.get_console_output(length=args.length)
    print(data)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg('address', metavar='<address>', help=_('IP Address.'))
@utils.arg(
    '--fixed-address',
    metavar='<fixed_address>',
    default=None,
    help=_('Fixed IP Address to associate with.'))
def do_add_floating_ip(cs, args):
    """DEPRECATED, use floating-ip-associate instead."""
    _associate_floating_ip(cs, args)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg('address', metavar='<address>', help=_('IP Address.'))
@utils.arg(
    '--fixed-address',
    metavar='<fixed_address>',
    default=None,
    help=_('Fixed IP Address to associate with.'))
def do_floating_ip_associate(cs, args):
    """Associate a floating IP address to a server."""
    _associate_floating_ip(cs, args)


def _associate_floating_ip(cs, args):
    server = _find_server(cs, args.server)
    server.add_floating_ip(args.address, args.fixed_address)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg('address', metavar='<address>', help=_('IP Address.'))
def do_remove_floating_ip(cs, args):
    """DEPRECATED, use floating-ip-disassociate instead."""
    _disassociate_floating_ip(cs, args)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg('address', metavar='<address>', help=_('IP Address.'))
def do_floating_ip_disassociate(cs, args):
    """Disassociate a floating IP address from a server."""
    _disassociate_floating_ip(cs, args)


def _disassociate_floating_ip(cs, args):
    server = _find_server(cs, args.server)
    server.remove_floating_ip(args.address)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg(
    'secgroup',
    metavar='<secgroup>',
    help=_('Name or ID of Security Group.'))
def do_add_secgroup(cs, args):
    """Add a Security Group to a server."""
    server = _find_server(cs, args.server)
    server.add_security_group(args.secgroup)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg(
    'secgroup',
    metavar='<secgroup>',
    help=_('Name of Security Group.'))
def do_remove_secgroup(cs, args):
    """Remove a Security Group from a server."""
    server = _find_server(cs, args.server)
    server.remove_security_group(args.secgroup)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_list_secgroup(cs, args):
    """List Security Group(s) of a server."""
    server = _find_server(cs, args.server)
    groups = server.list_security_group()
    _print_secgroups(groups)


@utils.arg(
    'pool',
    metavar='<floating-ip-pool>',
    help=_('Name of Floating IP Pool. (Optional)'),
    nargs='?',
    default=None)
@deprecated_network
def do_floating_ip_create(cs, args):
    """Allocate a floating IP for the current tenant."""
    _print_floating_ip_list([cs.floating_ips.create(pool=args.pool)])


@utils.arg('address', metavar='<address>', help=_('IP of Floating IP.'))
@deprecated_network
def do_floating_ip_delete(cs, args):
    """De-allocate a floating IP."""
    floating_ips = cs.floating_ips.list()
    for floating_ip in floating_ips:
        if floating_ip.ip == args.address:
            return cs.floating_ips.delete(floating_ip.id)
    raise exceptions.CommandError(_("Floating IP %s not found.") %
                                  args.address)


@deprecated_network
def do_floating_ip_list(cs, _args):
    """List floating IPs."""
    _print_floating_ip_list(cs.floating_ips.list())


@deprecated_network
def do_floating_ip_pool_list(cs, _args):
    """List all floating IP pools."""
    utils.print_list(cs.floating_ip_pools.list(), ['name'])


@utils.arg(
    '--host', dest='host', metavar='<host>', default=None,
    help=_('Filter by host.'))
@deprecated_network
def do_floating_ip_bulk_list(cs, args):
    """List all floating IPs (nova-network only)."""
    utils.print_list(cs.floating_ips_bulk.list(args.host), ['project_id',
                                                            'address',
                                                            'instance_uuid',
                                                            'pool',
                                                            'interface'])


@utils.arg('ip_range', metavar='<range>',
           help=_('Address range to create.'))
@utils.arg(
    '--pool', dest='pool', metavar='<pool>', default=None,
    help=_('Pool for new Floating IPs.'))
@utils.arg(
    '--interface', metavar='<interface>', default=None,
    help=_('Interface for new Floating IPs.'))
@deprecated_network
def do_floating_ip_bulk_create(cs, args):
    """Bulk create floating IPs by range (nova-network only)."""
    cs.floating_ips_bulk.create(args.ip_range, args.pool, args.interface)


@utils.arg('ip_range', metavar='<range>',
           help=_('Address range to delete.'))
@deprecated_network
def do_floating_ip_bulk_delete(cs, args):
    """Bulk delete floating IPs by range (nova-network only)."""
    cs.floating_ips_bulk.delete(args.ip_range)


def _print_dns_list(dns_entries):
    utils.print_list(dns_entries, ['ip', 'name', 'domain'])


def _print_domain_list(domain_entries):
    utils.print_list(domain_entries, ['domain', 'scope',
                                      'project', 'availability_zone'])


@deprecated_network
def do_dns_domains(cs, args):
    """Print a list of available dns domains."""
    domains = cs.dns_domains.domains()
    _print_domain_list(domains)


@utils.arg('domain', metavar='<domain>', help=_('DNS domain.'))
@utils.arg('--ip', metavar='<ip>', help=_('IP address.'), default=None)
@utils.arg('--name', metavar='<name>', help=_('DNS name.'), default=None)
@deprecated_network
def do_dns_list(cs, args):
    """List current DNS entries for domain and IP or domain and name."""
    if not (args.ip or args.name):
        raise exceptions.CommandError(
            _("You must specify either --ip or --name"))
    if args.name:
        entry = cs.dns_entries.get(args.domain, args.name)
        _print_dns_list([entry])
    else:
        entries = cs.dns_entries.get_for_ip(args.domain,
                                            ip=args.ip)
        _print_dns_list(entries)


@utils.arg('ip', metavar='<ip>', help=_('IP address.'))
@utils.arg('name', metavar='<name>', help=_('DNS name.'))
@utils.arg('domain', metavar='<domain>', help=_('DNS domain.'))
@utils.arg(
    '--type',
    metavar='<type>',
    help=_('DNS type (e.g. "A")'),
    default='A')
@deprecated_network
def do_dns_create(cs, args):
    """Create a DNS entry for domain, name, and IP."""
    cs.dns_entries.create(args.domain, args.name, args.ip, args.type)


@utils.arg('domain', metavar='<domain>', help=_('DNS domain.'))
@utils.arg('name', metavar='<name>', help=_('DNS name.'))
@deprecated_network
def do_dns_delete(cs, args):
    """Delete the specified DNS entry."""
    cs.dns_entries.delete(args.domain, args.name)


@utils.arg('domain', metavar='<domain>', help=_('DNS domain.'))
@deprecated_network
def do_dns_delete_domain(cs, args):
    """Delete the specified DNS domain."""
    cs.dns_domains.delete(args.domain)


@utils.arg('domain', metavar='<domain>', help=_('DNS domain.'))
@utils.arg(
    '--availability-zone',
    metavar='<availability-zone>',
    default=None,
    help=_('Limit access to this domain to servers '
           'in the specified availability zone.'))
@deprecated_network
def do_dns_create_private_domain(cs, args):
    """Create the specified DNS domain."""
    cs.dns_domains.create_private(args.domain,
                                  args.availability_zone)


@utils.arg('domain', metavar='<domain>', help=_('DNS domain.'))
@utils.arg(
    '--project', metavar='<project>',
    help=_('Limit access to this domain to users '
           'of the specified project.'),
    default=None)
@deprecated_network
def do_dns_create_public_domain(cs, args):
    """Create the specified DNS domain."""
    cs.dns_domains.create_public(args.domain,
                                 args.project)


def _print_secgroup_rules(rules, show_source_group=True):
    class FormattedRule(object):
        def __init__(self, obj):
            items = (obj if isinstance(obj, dict) else obj._info).items()
            for k, v in items:
                if k == 'ip_range':
                    v = v.get('cidr')
                elif k == 'group':
                    k = 'source_group'
                    v = v.get('name')
                if v is None:
                    v = ''

                setattr(self, k, v)

    rules = [FormattedRule(rule) for rule in rules]
    headers = ['IP Protocol', 'From Port', 'To Port', 'IP Range']
    if show_source_group:
        headers.append('Source Group')
    utils.print_list(rules, headers)


def _print_secgroups(secgroups):
    utils.print_list(secgroups, ['Id', 'Name', 'Description'])


def _get_secgroup(cs, secgroup):
    # Check secgroup is an ID (nova-network) or UUID (neutron)
    if (utils.is_integer_like(encodeutils.safe_encode(secgroup)) or
            uuidutils.is_uuid_like(secgroup)):
        try:
            return cs.security_groups.get(secgroup)
        except exceptions.NotFound:
            pass

    # Check secgroup as a name
    match_found = False
    for s in cs.security_groups.list():
        encoding = (
            locale.getpreferredencoding() or sys.stdin.encoding or 'UTF-8')
        if not six.PY3:
            s.name = s.name.encode(encoding)
        if secgroup == s.name:
            if match_found is not False:
                msg = (_("Multiple security group matches found for name '%s'"
                         ", use an ID to be more specific.") % secgroup)
                raise exceptions.NoUniqueMatch(msg)
            match_found = s
    if match_found is False:
        raise exceptions.CommandError(_("Secgroup ID or name '%s' not found.")
                                      % secgroup)
    return match_found


@utils.arg(
    'secgroup',
    metavar='<secgroup>',
    help=_('ID or name of security group.'))
@utils.arg(
    'ip_proto',
    metavar='<ip-proto>',
    help=_('IP protocol (icmp, tcp, udp).'))
@utils.arg(
    'from_port',
    metavar='<from-port>',
    help=_('Port at start of range.'))
@utils.arg(
    'to_port',
    metavar='<to-port>',
    help=_('Port at end of range.'))
@utils.arg('cidr', metavar='<cidr>', help=_('CIDR for address range.'))
@deprecated_network
def do_secgroup_add_rule(cs, args):
    """Add a rule to a security group."""
    secgroup = _get_secgroup(cs, args.secgroup)
    rule = cs.security_group_rules.create(secgroup.id,
                                          args.ip_proto,
                                          args.from_port,
                                          args.to_port,
                                          args.cidr)
    _print_secgroup_rules([rule])


@utils.arg(
    'secgroup',
    metavar='<secgroup>',
    help=_('ID or name of security group.'))
@utils.arg(
    'ip_proto',
    metavar='<ip-proto>',
    help=_('IP protocol (icmp, tcp, udp).'))
@utils.arg(
    'from_port',
    metavar='<from-port>',
    help=_('Port at start of range.'))
@utils.arg(
    'to_port',
    metavar='<to-port>',
    help=_('Port at end of range.'))
@utils.arg('cidr', metavar='<cidr>', help=_('CIDR for address range.'))
@deprecated_network
def do_secgroup_delete_rule(cs, args):
    """Delete a rule from a security group."""
    secgroup = _get_secgroup(cs, args.secgroup)
    for rule in secgroup.rules:
        if (rule['ip_protocol'] and
                rule['ip_protocol'].upper() == args.ip_proto.upper() and
                rule['from_port'] == int(args.from_port) and
                rule['to_port'] == int(args.to_port) and
                rule['ip_range']['cidr'] == args.cidr):
            _print_secgroup_rules([rule])
            return cs.security_group_rules.delete(rule['id'])

    raise exceptions.CommandError(_("Rule not found"))


@utils.arg('name', metavar='<name>', help=_('Name of security group.'))
@utils.arg(
    'description', metavar='<description>',
    help=_('Description of security group.'))
@deprecated_network
def do_secgroup_create(cs, args):
    """Create a security group."""
    secgroup = cs.security_groups.create(args.name, args.description)
    _print_secgroups([secgroup])


@utils.arg(
    'secgroup',
    metavar='<secgroup>',
    help=_('ID or name of security group.'))
@utils.arg('name', metavar='<name>', help=_('Name of security group.'))
@utils.arg(
    'description', metavar='<description>',
    help=_('Description of security group.'))
@deprecated_network
def do_secgroup_update(cs, args):
    """Update a security group."""
    sg = _get_secgroup(cs, args.secgroup)
    secgroup = cs.security_groups.update(sg, args.name, args.description)
    _print_secgroups([secgroup])


@utils.arg(
    'secgroup',
    metavar='<secgroup>',
    help=_('ID or name of security group.'))
@deprecated_network
def do_secgroup_delete(cs, args):
    """Delete a security group."""
    secgroup = _get_secgroup(cs, args.secgroup)
    cs.security_groups.delete(secgroup)
    _print_secgroups([secgroup])


@utils.arg(
    '--all-tenants',
    dest='all_tenants',
    metavar='<0|1>',
    nargs='?',
    type=int,
    const=1,
    default=int(strutils.bool_from_string(
        os.environ.get("ALL_TENANTS", 'false'), True)),
    help=_('Display information from all tenants (Admin only).'))
@deprecated_network
def do_secgroup_list(cs, args):
    """List security groups for the current tenant."""
    search_opts = {'all_tenants': args.all_tenants}
    columns = ['Id', 'Name', 'Description']
    if args.all_tenants:
        columns.append('Tenant_ID')
    groups = cs.security_groups.list(search_opts=search_opts)
    utils.print_list(groups, columns)


@utils.arg(
    'secgroup',
    metavar='<secgroup>',
    help=_('ID or name of security group.'))
@deprecated_network
def do_secgroup_list_rules(cs, args):
    """List rules for a security group."""
    secgroup = _get_secgroup(cs, args.secgroup)
    _print_secgroup_rules(secgroup.rules)


@utils.arg(
    'secgroup',
    metavar='<secgroup>',
    help=_('ID or name of security group.'))
@utils.arg(
    'source_group',
    metavar='<source-group>',
    help=_('ID or name of source group.'))
@utils.arg(
    'ip_proto',
    metavar='<ip-proto>',
    help=_('IP protocol (icmp, tcp, udp).'))
@utils.arg(
    'from_port',
    metavar='<from-port>',
    help=_('Port at start of range.'))
@utils.arg(
    'to_port',
    metavar='<to-port>',
    help=_('Port at end of range.'))
@deprecated_network
def do_secgroup_add_group_rule(cs, args):
    """Add a source group rule to a security group."""
    secgroup = _get_secgroup(cs, args.secgroup)
    source_group = _get_secgroup(cs, args.source_group)
    params = {'group_id': source_group.id}

    if args.ip_proto or args.from_port or args.to_port:
        if not (args.ip_proto and args.from_port and args.to_port):
            raise exceptions.CommandError(_("ip_proto, from_port, and to_port"
                                            " must be specified together"))
        params['ip_protocol'] = args.ip_proto.upper()
        params['from_port'] = args.from_port
        params['to_port'] = args.to_port

    rule = cs.security_group_rules.create(secgroup.id, **params)
    _print_secgroup_rules([rule])


@utils.arg(
    'secgroup',
    metavar='<secgroup>',
    help=_('ID or name of security group.'))
@utils.arg(
    'source_group',
    metavar='<source-group>',
    help=_('ID or name of source group.'))
@utils.arg(
    'ip_proto',
    metavar='<ip-proto>',
    help=_('IP protocol (icmp, tcp, udp).'))
@utils.arg(
    'from_port',
    metavar='<from-port>',
    help=_('Port at start of range.'))
@utils.arg(
    'to_port',
    metavar='<to-port>',
    help=_('Port at end of range.'))
@deprecated_network
def do_secgroup_delete_group_rule(cs, args):
    """Delete a source group rule from a security group."""
    secgroup = _get_secgroup(cs, args.secgroup)
    source_group = _get_secgroup(cs, args.source_group)
    params = {'group_name': source_group.name}

    if args.ip_proto or args.from_port or args.to_port:
        if not (args.ip_proto and args.from_port and args.to_port):
            raise exceptions.CommandError(_("ip_proto, from_port, and to_port"
                                            " must be specified together"))
        params['ip_protocol'] = args.ip_proto.upper()
        params['from_port'] = int(args.from_port)
        params['to_port'] = int(args.to_port)

    for rule in secgroup.rules:
        if (rule.get('ip_protocol') and
                rule['ip_protocol'].upper() == params.get(
                    'ip_protocol').upper() and
                rule.get('from_port') == params.get('from_port') and
                rule.get('to_port') == params.get('to_port') and
                rule.get('group', {}).get('name') == params.get('group_name')):
            return cs.security_group_rules.delete(rule['id'])

    raise exceptions.CommandError(_("Rule not found"))


@api_versions.wraps("2.0", "2.1")
def _keypair_create(cs, args, name, pub_key):
    return cs.keypairs.create(name, pub_key)


@api_versions.wraps("2.2", "2.9")
def _keypair_create(cs, args, name, pub_key):
    return cs.keypairs.create(name, pub_key, key_type=args.key_type)


@api_versions.wraps("2.10")
def _keypair_create(cs, args, name, pub_key):
    return cs.keypairs.create(name, pub_key, key_type=args.key_type,
                              user_id=args.user)


@utils.arg('name', metavar='<name>', help=_('Name of key.'))
@utils.arg(
    '--pub-key',
    metavar='<pub-key>',
    default=None,
    help=_('Path to a public ssh key.'))
@utils.arg(
    '--key-type',
    metavar='<key-type>',
    default='ssh',
    help=_('Keypair type. Can be ssh or x509.'),
    start_version="2.2")
@utils.arg(
    '--user',
    metavar='<user-id>',
    default=None,
    help=_('ID of user to whom to add key-pair (Admin only).'),
    start_version="2.10")
def do_keypair_add(cs, args):
    """Create a new key pair for use with servers."""
    name = args.name
    pub_key = args.pub_key
    if pub_key:
        if pub_key == '-':
            pub_key = sys.stdin.read()
        else:
            try:
                with open(os.path.expanduser(pub_key)) as f:
                    pub_key = f.read()
            except IOError as e:
                raise exceptions.CommandError(
                    _("Can't open or read '%(key)s': %(exc)s")
                    % {'key': pub_key, 'exc': e}
                )

    keypair = _keypair_create(cs, args, name, pub_key)

    if not pub_key:
        private_key = keypair.private_key
        print(private_key)


@api_versions.wraps("2.0", "2.9")
@utils.arg('name', metavar='<name>', help=_('Keypair name to delete.'))
def do_keypair_delete(cs, args):
    """Delete keypair given by its name."""
    name = _find_keypair(cs, args.name)
    cs.keypairs.delete(name)


@api_versions.wraps("2.10")
@utils.arg('name', metavar='<name>', help=_('Keypair name to delete.'))
@utils.arg(
    '--user',
    metavar='<user-id>',
    default=None,
    help=_('ID of key-pair owner (Admin only).'))
def do_keypair_delete(cs, args):
    """Delete keypair given by its name."""
    cs.keypairs.delete(args.name, args.user)


@api_versions.wraps("2.0", "2.1")
def _get_keypairs_list_columns(cs, args):
    return ['Name', 'Fingerprint']


@api_versions.wraps("2.2")
def _get_keypairs_list_columns(cs, args):
    return ['Name', 'Type', 'Fingerprint']


@api_versions.wraps("2.0", "2.9")
def do_keypair_list(cs, args):
    """Print a list of keypairs for a user"""
    keypairs = cs.keypairs.list()
    columns = _get_keypairs_list_columns(cs, args)
    utils.print_list(keypairs, columns)


@api_versions.wraps("2.10", "2.34")
@utils.arg(
    '--user',
    metavar='<user-id>',
    default=None,
    help=_('List key-pairs of specified user ID (Admin only).'))
def do_keypair_list(cs, args):
    """Print a list of keypairs for a user"""
    keypairs = cs.keypairs.list(args.user)
    columns = _get_keypairs_list_columns(cs, args)
    utils.print_list(keypairs, columns)


@api_versions.wraps("2.35")
@utils.arg(
    '--user',
    metavar='<user-id>',
    default=None,
    help=_('List key-pairs of specified user ID (Admin only).'))
@utils.arg(
    '--marker',
    dest='marker',
    metavar='<marker>',
    default=None,
    help=_('The last keypair of the previous page; displays list of keypairs '
           'after "marker".'))
@utils.arg(
    '--limit',
    dest='limit',
    metavar='<limit>',
    type=int,
    default=None,
    help=_("Maximum number of keypairs to display. If limit == -1, all "
           "keypairs will be displayed. If limit is bigger than "
           "'osapi_max_limit' option of Nova API, limit 'osapi_max_limit' "
           "will be used instead."))
def do_keypair_list(cs, args):
    """Print a list of keypairs for a user"""
    keypairs = cs.keypairs.list(args.user, args.marker, args.limit)
    columns = _get_keypairs_list_columns(cs, args)
    utils.print_list(keypairs, columns)


def _print_keypair(keypair):
    kp = keypair._info.copy()
    pk = kp.pop('public_key')
    utils.print_dict(kp)
    print(_("Public key: %s") % pk)


@api_versions.wraps("2.0", "2.9")
@utils.arg(
    'keypair',
    metavar='<keypair>',
    help=_("Name of keypair."))
def do_keypair_show(cs, args):
    """Show details about the given keypair."""
    keypair = _find_keypair(cs, args.keypair)
    _print_keypair(keypair)


@api_versions.wraps("2.10")
@utils.arg(
    'keypair',
    metavar='<keypair>',
    help=_("Name of keypair."))
@utils.arg(
    '--user',
    metavar='<user-id>',
    default=None,
    help=_('ID of key-pair owner (Admin only).'))
def do_keypair_show(cs, args):
    """Show details about the given keypair."""
    keypair = cs.keypairs.get(args.keypair, args.user)
    _print_keypair(keypair)


def _find_keypair(cs, keypair):
    """Get a keypair by name."""
    return utils.find_resource(cs.keypairs, keypair)


@utils.arg(
    '--tenant',
    # nova db searches by project_id
    dest='tenant',
    metavar='<tenant>',
    nargs='?',
    help=_('Display information from single tenant (Admin only).'))
@utils.arg(
    '--reserved',
    dest='reserved',
    action='store_true',
    default=False,
    help=_('Include reservations count.'))
def do_absolute_limits(cs, args):
    """DEPRECATED, use limits instead."""
    limits = cs.limits.get(args.reserved, args.tenant).absolute
    _print_absolute_limits(limits)


def _print_absolute_limits(limits):
    """Prints absolute limits."""
    class Limit(object):
        def __init__(self, name, used, max, other):
            self.name = name
            self.used = used
            self.max = max
            self.other = other

    limit_map = {
        'maxServerMeta': {'name': 'Server Meta', 'type': 'max'},
        'maxPersonality': {'name': 'Personality', 'type': 'max'},
        'maxPersonalitySize': {'name': 'Personality Size', 'type': 'max'},
        'maxImageMeta': {'name': 'ImageMeta', 'type': 'max'},
        'maxTotalKeypairs': {'name': 'Keypairs', 'type': 'max'},
        'totalCoresUsed': {'name': 'Cores', 'type': 'used'},
        'maxTotalCores': {'name': 'Cores', 'type': 'max'},
        'totalRAMUsed': {'name': 'RAM', 'type': 'used'},
        'maxTotalRAMSize': {'name': 'RAM', 'type': 'max'},
        'totalInstancesUsed': {'name': 'Instances', 'type': 'used'},
        'maxTotalInstances': {'name': 'Instances', 'type': 'max'},
        'totalFloatingIpsUsed': {'name': 'FloatingIps', 'type': 'used'},
        'maxTotalFloatingIps': {'name': 'FloatingIps', 'type': 'max'},
        'totalSecurityGroupsUsed': {'name': 'SecurityGroups', 'type': 'used'},
        'maxSecurityGroups': {'name': 'SecurityGroups', 'type': 'max'},
        'maxSecurityGroupRules': {'name': 'SecurityGroupRules', 'type': 'max'},
        'maxServerGroups': {'name': 'ServerGroups', 'type': 'max'},
        'totalServerGroupsUsed': {'name': 'ServerGroups', 'type': 'used'},
        'maxServerGroupMembers': {'name': 'ServerGroupMembers', 'type': 'max'},
    }

    max = {}
    used = {}
    other = {}
    limit_names = []
    columns = ['Name', 'Used', 'Max']
    for l in limits:
        map = limit_map.get(l.name, {'name': l.name, 'type': 'other'})
        name = map['name']
        if map['type'] == 'max':
            max[name] = l.value
        elif map['type'] == 'used':
            used[name] = l.value
        else:
            other[name] = l.value
            columns.append('Other')
        if name not in limit_names:
            limit_names.append(name)

    limit_names.sort()

    limit_list = []
    for name in limit_names:
        l = Limit(name,
                  used.get(name, "-"),
                  max.get(name, "-"),
                  other.get(name, "-"))
        limit_list.append(l)

    utils.print_list(limit_list, columns)


def do_rate_limits(cs, args):
    """DEPRECATED, use limits instead."""
    limits = cs.limits.get().rate
    _print_rate_limits(limits)


def _print_rate_limits(limits):
    """print rate limits."""
    columns = ['Verb', 'URI', 'Value', 'Remain', 'Unit', 'Next_Available']
    utils.print_list(limits, columns)


@utils.arg(
    '--tenant',
    # nova db searches by project_id
    dest='tenant',
    metavar='<tenant>',
    nargs='?',
    help=_('Display information from single tenant (Admin only).'))
@utils.arg(
    '--reserved',
    dest='reserved',
    action='store_true',
    default=False,
    help=_('Include reservations count.'))
def do_limits(cs, args):
    """Print rate and absolute limits."""
    limits = cs.limits.get(args.reserved, args.tenant)
    _print_rate_limits(limits.rate)
    _print_absolute_limits(limits.absolute)


@utils.arg(
    '--start',
    metavar='<start>',
    help=_('Usage range start date ex 2012-01-20. (default: 4 weeks ago)'),
    default=None)
@utils.arg(
    '--end',
    metavar='<end>',
    help=_('Usage range end date, ex 2012-01-20. (default: tomorrow)'),
    default=None)
def do_usage_list(cs, args):
    """List usage data for all tenants."""
    dateformat = "%Y-%m-%d"
    rows = ["Tenant ID", "Servers", "RAM MB-Hours", "CPU Hours",
            "Disk GB-Hours"]

    now = timeutils.utcnow()

    if args.start:
        start = datetime.datetime.strptime(args.start, dateformat)
    else:
        start = now - datetime.timedelta(weeks=4)

    if args.end:
        end = datetime.datetime.strptime(args.end, dateformat)
    else:
        end = now + datetime.timedelta(days=1)

    def simplify_usage(u):
        simplerows = [x.lower().replace(" ", "_") for x in rows]

        setattr(u, simplerows[0], u.tenant_id)
        setattr(u, simplerows[1], "%d" % len(u.server_usages))
        setattr(u, simplerows[2], "%.2f" % u.total_memory_mb_usage)
        setattr(u, simplerows[3], "%.2f" % u.total_vcpus_usage)
        setattr(u, simplerows[4], "%.2f" % u.total_local_gb_usage)

    usage_list = cs.usage.list(start, end, detailed=True)

    print(_("Usage from %(start)s to %(end)s:") %
          {'start': start.strftime(dateformat),
           'end': end.strftime(dateformat)})

    for usage in usage_list:
        simplify_usage(usage)

    utils.print_list(usage_list, rows)


@utils.arg(
    '--start',
    metavar='<start>',
    help=_('Usage range start date ex 2012-01-20. (default: 4 weeks ago)'),
    default=None)
@utils.arg(
    '--end', metavar='<end>',
    help=_('Usage range end date, ex 2012-01-20. (default: tomorrow)'),
    default=None)
@utils.arg(
    '--tenant',
    metavar='<tenant-id>',
    default=None,
    help=_('UUID of tenant to get usage for.'))
def do_usage(cs, args):
    """Show usage data for a single tenant."""
    dateformat = "%Y-%m-%d"
    rows = ["Servers", "RAM MB-Hours", "CPU Hours", "Disk GB-Hours"]

    now = timeutils.utcnow()

    if args.start:
        start = datetime.datetime.strptime(args.start, dateformat)
    else:
        start = now - datetime.timedelta(weeks=4)

    if args.end:
        end = datetime.datetime.strptime(args.end, dateformat)
    else:
        end = now + datetime.timedelta(days=1)

    def simplify_usage(u):
        simplerows = [x.lower().replace(" ", "_") for x in rows]

        setattr(u, simplerows[0], "%d" % len(u.server_usages))
        setattr(u, simplerows[1], "%.2f" % u.total_memory_mb_usage)
        setattr(u, simplerows[2], "%.2f" % u.total_vcpus_usage)
        setattr(u, simplerows[3], "%.2f" % u.total_local_gb_usage)

    if args.tenant:
        usage = cs.usage.get(args.tenant, start, end)
    else:
        if isinstance(cs.client, client.SessionClient):
            auth = cs.client.auth
            project_id = auth.get_auth_ref(cs.client.session).project_id
            usage = cs.usage.get(project_id, start, end)
        else:
            usage = cs.usage.get(cs.client.tenant_id, start, end)

    print(_("Usage from %(start)s to %(end)s:") %
          {'start': start.strftime(dateformat),
           'end': end.strftime(dateformat)})

    if getattr(usage, 'total_vcpus_usage', None):
        simplify_usage(usage)
        utils.print_list([usage], rows)
    else:
        print(_('None'))


@utils.arg(
    'pk_filename',
    metavar='<private-key-filename>',
    nargs='?',
    default='pk.pem',
    help=_('Filename for the private key. [Default: pk.pem]'))
@utils.arg(
    'cert_filename',
    metavar='<x509-cert-filename>',
    nargs='?',
    default='cert.pem',
    help=_('Filename for the X.509 certificate. [Default: cert.pem]'))
def do_x509_create_cert(cs, args):
    """Create x509 cert for a user in tenant."""

    if os.path.exists(args.pk_filename):
        raise exceptions.CommandError(_("Unable to write privatekey - %s "
                                        "exists.") % args.pk_filename)
    if os.path.exists(args.cert_filename):
        raise exceptions.CommandError(_("Unable to write x509 cert - %s "
                                        "exists.") % args.cert_filename)

    certs = cs.certs.create()

    try:
        old_umask = os.umask(0o377)
        with open(args.pk_filename, 'w') as private_key:
            private_key.write(certs.private_key)
            print(_("Wrote private key to %s") % args.pk_filename)
    finally:
        os.umask(old_umask)

    with open(args.cert_filename, 'w') as cert:
        cert.write(certs.data)
        print(_("Wrote x509 certificate to %s") % args.cert_filename)


@utils.arg(
    'filename',
    metavar='<filename>',
    nargs='?',
    default='cacert.pem',
    help=_('Filename to write the x509 root cert.'))
def do_x509_get_root_cert(cs, args):
    """Fetch the x509 root cert."""
    if os.path.exists(args.filename):
        raise exceptions.CommandError(_("Unable to write x509 root cert - \
                                      %s exists.") % args.filename)

    with open(args.filename, 'w') as cert:
        cacert = cs.certs.get()
        cert.write(cacert.data)
        print(_("Wrote x509 root cert to %s") % args.filename)


@utils.arg(
    '--hypervisor',
    metavar='<hypervisor>',
    default=None,
    help=_('Type of hypervisor.'))
def do_agent_list(cs, args):
    """List all builds."""
    result = cs.agents.list(args.hypervisor)
    columns = ["Agent_id", "Hypervisor", "OS", "Architecture", "Version",
               'Md5hash', 'Url']
    utils.print_list(result, columns)


@utils.arg('os', metavar='<os>', help=_('Type of OS.'))
@utils.arg(
    'architecture',
    metavar='<architecture>',
    help=_('Type of architecture.'))
@utils.arg('version', metavar='<version>', help=_('Version.'))
@utils.arg('url', metavar='<url>', help=_('URL.'))
@utils.arg('md5hash', metavar='<md5hash>', help=_('MD5 hash.'))
@utils.arg(
    'hypervisor',
    metavar='<hypervisor>',
    default='xen',
    help=_('Type of hypervisor.'))
def do_agent_create(cs, args):
    """Create new agent build."""
    result = cs.agents.create(args.os, args.architecture,
                              args.version, args.url,
                              args.md5hash, args.hypervisor)
    utils.print_dict(result._info.copy())


@utils.arg('id', metavar='<id>', help=_('ID of the agent-build.'))
def do_agent_delete(cs, args):
    """Delete existing agent build."""
    cs.agents.delete(args.id)


@utils.arg('id', metavar='<id>', help=_('ID of the agent-build.'))
@utils.arg('version', metavar='<version>', help=_('Version.'))
@utils.arg('url', metavar='<url>', help=_('URL'))
@utils.arg('md5hash', metavar='<md5hash>', help=_('MD5 hash.'))
def do_agent_modify(cs, args):
    """Modify existing agent build."""
    result = cs.agents.update(args.id, args.version,
                              args.url, args.md5hash)
    utils.print_dict(result._info)


def _find_aggregate(cs, aggregate):
    """Get an aggregate by name or ID."""
    return utils.find_resource(cs.aggregates, aggregate)


def do_aggregate_list(cs, args):
    """Print a list of all aggregates."""
    aggregates = cs.aggregates.list()
    columns = ['Id', 'Name', 'Availability Zone']
    utils.print_list(aggregates, columns)


@utils.arg('name', metavar='<name>', help=_('Name of aggregate.'))
@utils.arg(
    'availability_zone',
    metavar='<availability-zone>',
    default=None,
    nargs='?',
    help=_('The availability zone of the aggregate (optional).'))
def do_aggregate_create(cs, args):
    """Create a new aggregate with the specified details."""
    aggregate = cs.aggregates.create(args.name, args.availability_zone)
    _print_aggregate_details(aggregate)


@utils.arg(
    'aggregate',
    metavar='<aggregate>',
    help=_('Name or ID of aggregate to delete.'))
def do_aggregate_delete(cs, args):
    """Delete the aggregate."""
    aggregate = _find_aggregate(cs, args.aggregate)
    cs.aggregates.delete(aggregate)
    print(_("Aggregate %s has been successfully deleted.") % aggregate.id)


@utils.arg(
    'aggregate',
    metavar='<aggregate>',
    help=_('Name or ID of aggregate to update.'))
@utils.arg(
    'name',
    nargs='?',
    action=shell.DeprecatedAction,
    use=_('use "%s"; this option will be removed in '
          'novaclient 5.0.0.') % '--name',
    help=argparse.SUPPRESS)
@utils.arg(
    '--name',
    dest='name',
    help=_('Name of aggregate.'))
@utils.arg(
    'availability_zone',
    metavar='<availability-zone>',
    nargs='?',
    default=None,
    action=shell.DeprecatedAction,
    use=_('use "%s"; this option will be removed in '
          'novaclient 5.0.0.') % '--availability_zone',
    help=argparse.SUPPRESS)
@utils.arg(
    '--availability-zone',
    metavar='<availability-zone>',
    dest='availability_zone',
    help=_('The availability zone of the aggregate.'))
def do_aggregate_update(cs, args):
    """Update the aggregate's name and optionally availability zone."""
    aggregate = _find_aggregate(cs, args.aggregate)
    updates = {}
    if args.name:
        updates["name"] = args.name
    if args.availability_zone:
        updates["availability_zone"] = args.availability_zone

    aggregate = cs.aggregates.update(aggregate.id, updates)
    print(_("Aggregate %s has been successfully updated.") % aggregate.id)
    _print_aggregate_details(aggregate)


@utils.arg(
    'aggregate', metavar='<aggregate>',
    help=_('Name or ID of aggregate to update.'))
@utils.arg(
    'metadata',
    metavar='<key=value>',
    nargs='+',
    action='append',
    default=[],
    help=_('Metadata to add/update to aggregate. '
           'Specify only the key to delete a metadata item.'))
def do_aggregate_set_metadata(cs, args):
    """Update the metadata associated with the aggregate."""
    aggregate = _find_aggregate(cs, args.aggregate)
    metadata = _extract_metadata(args)
    currentmetadata = getattr(aggregate, 'metadata', {})
    if set(metadata.items()) & set(currentmetadata.items()):
        raise exceptions.CommandError(_("metadata already exists"))
    for key, value in metadata.items():
        if value is None and key not in currentmetadata:
            raise exceptions.CommandError(_("metadata key %s does not exist"
                                          " hence can not be deleted")
                                          % key)
    aggregate = cs.aggregates.set_metadata(aggregate.id, metadata)
    print(_("Metadata has been successfully updated for aggregate %s.") %
          aggregate.id)
    _print_aggregate_details(aggregate)


@utils.arg(
    'aggregate', metavar='<aggregate>',
    help=_('Name or ID of aggregate.'))
@utils.arg(
    'host', metavar='<host>',
    help=_('The host to add to the aggregate.'))
def do_aggregate_add_host(cs, args):
    """Add the host to the specified aggregate."""
    aggregate = _find_aggregate(cs, args.aggregate)
    aggregate = cs.aggregates.add_host(aggregate.id, args.host)
    print(_("Host %(host)s has been successfully added for aggregate "
            "%(aggregate_id)s ") % {'host': args.host,
                                    'aggregate_id': aggregate.id})
    _print_aggregate_details(aggregate)


@utils.arg(
    'aggregate', metavar='<aggregate>',
    help=_('Name or ID of aggregate.'))
@utils.arg(
    'host', metavar='<host>',
    help=_('The host to remove from the aggregate.'))
def do_aggregate_remove_host(cs, args):
    """Remove the specified host from the specified aggregate."""
    aggregate = _find_aggregate(cs, args.aggregate)
    aggregate = cs.aggregates.remove_host(aggregate.id, args.host)
    print(_("Host %(host)s has been successfully removed from aggregate "
            "%(aggregate_id)s ") % {'host': args.host,
                                    'aggregate_id': aggregate.id})
    _print_aggregate_details(aggregate)


@utils.arg(
    'aggregate', metavar='<aggregate>',
    help=_('Name or ID of aggregate.'))
def do_aggregate_details(cs, args):
    """DEPRECATED, use aggregate-show instead."""
    do_aggregate_show(cs, args)


@utils.arg(
    'aggregate', metavar='<aggregate>',
    help=_('Name or ID of aggregate.'))
def do_aggregate_show(cs, args):
    """Show details of the specified aggregate."""
    aggregate = _find_aggregate(cs, args.aggregate)
    _print_aggregate_details(aggregate)


def _print_aggregate_details(aggregate):
    columns = ['Id', 'Name', 'Availability Zone', 'Hosts', 'Metadata']

    def parser_metadata(fields):
        return utils.pretty_choice_dict(getattr(fields, 'metadata', {}) or {})

    def parser_hosts(fields):
        return utils.pretty_choice_list(getattr(fields, 'hosts', []))

    formatters = {
        'Metadata': parser_metadata,
        'Hosts': parser_hosts,
    }
    utils.print_list([aggregate], columns, formatters=formatters)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg(
    'host', metavar='<host>', default=None, nargs='?',
    help=_('Destination host name.'))
@utils.arg(
    '--block-migrate',
    action='store_true',
    dest='block_migrate',
    default=False,
    help=_('True in case of block_migration. (Default=False:live_migration)'),
    start_version="2.0", end_version="2.24")
@utils.arg(
    '--block-migrate',
    action='store_true',
    dest='block_migrate',
    default="auto",
    help=_('True in case of block_migration. (Default=auto:live_migration)'),
    start_version="2.25")
@utils.arg(
    '--disk-over-commit',
    action='store_true',
    dest='disk_over_commit',
    default=False,
    help=_('Allow overcommit. (Default=False)'),
    start_version="2.0", end_version="2.24")
@utils.arg(
    '--force',
    dest='force',
    action='store_true',
    default=False,
    help=_('Force to not verify the scheduler if a host is provided.'),
    start_version='2.30')
def do_live_migration(cs, args):
    """Migrate running server to a new machine."""

    update_kwargs = {}
    if 'disk_over_commit' in args:
        update_kwargs['disk_over_commit'] = args.disk_over_commit
    if 'force' in args and args.force:
        update_kwargs['force'] = args.force

    _find_server(cs, args.server).live_migrate(args.host, args.block_migrate,
                                               **update_kwargs)


@api_versions.wraps("2.22")
@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg('migration', metavar='<migration>', help=_('ID of migration.'))
def do_live_migration_force_complete(cs, args):
    """Force on-going live migration to complete."""
    server = _find_server(cs, args.server)
    cs.server_migrations.live_migrate_force_complete(server, args.migration)


@api_versions.wraps("2.23")
@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_server_migration_list(cs, args):
    """Get the migrations list of specified server."""
    server = _find_server(cs, args.server)
    migrations = cs.server_migrations.list(server)

    fields = ['Id', 'Source Node', 'Dest Node', 'Source Compute',
              'Dest Compute', 'Dest Host', 'Status', 'Server UUID',
              'Created At', 'Updated At']

    format_name = ["Total Memory Bytes", "Processed Memory Bytes",
                   "Remaining Memory Bytes", "Total Disk Bytes",
                   "Processed Disk Bytes", "Remaining Disk Bytes"]

    format_key = ["memory_total_bytes", "memory_processed_bytes",
                  "memory_remaining_bytes", "disk_total_bytes",
                  "disk_processed_bytes", "disk_remaining_bytes"]

    formatters = map(lambda field: utils.make_field_formatter(field)[1],
                     format_key)
    formatters = dict(zip(format_name, formatters))

    utils.print_list(migrations, fields + format_name, formatters)


@api_versions.wraps("2.23")
@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg('migration', metavar='<migration>', help=_('ID of migration.'))
def do_server_migration_show(cs, args):
    """Get the migration of specified server."""
    server = _find_server(cs, args.server)
    migration = cs.server_migrations.get(server, args.migration)
    utils.print_dict(migration._info)


@api_versions.wraps("2.24")
@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg('migration', metavar='<migration>', help=_('ID of migration.'))
def do_live_migration_abort(cs, args):
    """Abort an on-going live migration."""
    server = _find_server(cs, args.server)
    cs.server_migrations.live_migration_abort(server, args.migration)


@utils.arg(
    '--all-tenants',
    action='store_const',
    const=1,
    default=0,
    help=_('Reset state server(s) in another tenant by name (Admin only).'))
@utils.arg(
    'server', metavar='<server>', nargs='+',
    help=_('Name or ID of server(s).'))
@utils.arg(
    '--active', action='store_const', dest='state',
    default='error', const='active',
    help=_('Request the server be reset to "active" state instead '
           'of "error" state (the default).'))
def do_reset_state(cs, args):
    """Reset the state of a server."""
    failure_flag = False
    find_args = {'all_tenants': args.all_tenants}

    for server in args.server:
        try:
            _find_server(cs, server, **find_args).reset_state(args.state)
            msg = "Reset state for server %s succeeded; new state is %s"
            print(msg % (server, args.state))
        except Exception as e:
            failure_flag = True
            msg = "Reset state for server %s failed: %s" % (server, e)
            print(msg)

    if failure_flag:
        msg = "Unable to reset the state for the specified server(s)."
        raise exceptions.CommandError(msg)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_reset_network(cs, args):
    """Reset network of a server."""
    _find_server(cs, args.server).reset_network()


@utils.arg(
    '--host',
    metavar='<hostname>',
    default=None,
    help=_('Name of host.'))
@utils.arg(
    '--binary',
    metavar='<binary>',
    default=None,
    help=_('Service binary.'))
def do_service_list(cs, args):
    """Show a list of all running services. Filter by host & binary."""
    result = cs.services.list(host=args.host, binary=args.binary)
    columns = ["Binary", "Host", "Zone", "Status", "State", "Updated_at"]
    # NOTE(sulo): we check if the response has disabled_reason
    # so as not to add the column when the extended ext is not enabled.
    if result and hasattr(result[0], 'disabled_reason'):
        columns.append("Disabled Reason")

    # NOTE(gtt): After https://review.openstack.org/#/c/39998/ nova will
    # show id in response.
    if result and hasattr(result[0], 'id'):
        columns.insert(0, "Id")

    utils.print_list(result, columns)


@utils.arg('host', metavar='<hostname>', help=_('Name of host.'))
@utils.arg('binary', metavar='<binary>', help=_('Service binary.'))
def do_service_enable(cs, args):
    """Enable the service."""
    result = cs.services.enable(args.host, args.binary)
    utils.print_list([result], ['Host', 'Binary', 'Status'])


@utils.arg('host', metavar='<hostname>', help=_('Name of host.'))
@utils.arg('binary', metavar='<binary>', help=_('Service binary.'))
@utils.arg(
    '--reason',
    metavar='<reason>',
    help=_('Reason for disabling service.'))
def do_service_disable(cs, args):
    """Disable the service."""
    if args.reason:
        result = cs.services.disable_log_reason(args.host, args.binary,
                                                args.reason)
        utils.print_list([result], ['Host', 'Binary', 'Status',
                         'Disabled Reason'])
    else:
        result = cs.services.disable(args.host, args.binary)
        utils.print_list([result], ['Host', 'Binary', 'Status'])


@api_versions.wraps("2.11")
@utils.arg('host', metavar='<hostname>', help=_('Name of host.'))
@utils.arg('binary', metavar='<binary>', help=_('Service binary.'))
@utils.arg(
    '--unset',
    dest='force_down',
    help=_("Unset the force state down of service."),
    action='store_false',
    default=True)
def do_service_force_down(cs, args):
    """Force service to down."""
    result = cs.services.force_down(args.host, args.binary, args.force_down)
    utils.print_list([result], ['Host', 'Binary', 'Forced down'])


@utils.arg('id', metavar='<id>', help=_('ID of service.'))
def do_service_delete(cs, args):
    """Delete the service."""
    cs.services.delete(args.id)


@api_versions.wraps("2.0", "2.3")
def _print_fixed_ip(cs, fixed_ip):
    fields = ['address', 'cidr', 'hostname', 'host']
    utils.print_list([fixed_ip], fields)


@api_versions.wraps("2.4")
def _print_fixed_ip(cs, fixed_ip):
    fields = ['address', 'cidr', 'hostname', 'host', 'reserved']
    utils.print_list([fixed_ip], fields)


@utils.arg('fixed_ip', metavar='<fixed_ip>', help=_('Fixed IP Address.'))
@deprecated_network
def do_fixed_ip_get(cs, args):
    """Retrieve info on a fixed IP."""
    result = cs.fixed_ips.get(args.fixed_ip)
    _print_fixed_ip(cs, result)


@utils.arg('fixed_ip', metavar='<fixed_ip>', help=_('Fixed IP Address.'))
@deprecated_network
def do_fixed_ip_reserve(cs, args):
    """Reserve a fixed IP."""
    cs.fixed_ips.reserve(args.fixed_ip)


@utils.arg('fixed_ip', metavar='<fixed_ip>', help=_('Fixed IP Address.'))
@deprecated_network
def do_fixed_ip_unreserve(cs, args):
    """Unreserve a fixed IP."""
    cs.fixed_ips.unreserve(args.fixed_ip)


@utils.arg('host', metavar='<hostname>', help=_('Name of host.'))
def do_host_describe(cs, args):
    """Describe a specific host."""
    result = cs.hosts.get(args.host)
    columns = ["HOST", "PROJECT", "cpu", "memory_mb", "disk_gb"]
    utils.print_list(result, columns)


@utils.arg(
    '--zone',
    metavar='<zone>',
    default=None,
    help=_('Filters the list, returning only those hosts in the availability '
           'zone <zone>.'))
def do_host_list(cs, args):
    """List all hosts by service."""
    columns = ["host_name", "service", "zone"]
    result = cs.hosts.list(args.zone)
    utils.print_list(result, columns)


@utils.arg('host', metavar='<hostname>', help=_('Name of host.'))
@utils.arg(
    '--status', metavar='<enable|disable>', default=None, dest='status',
    help=_('Either enable or disable a host.'))
@utils.arg(
    '--maintenance',
    metavar='<enable|disable>',
    default=None,
    dest='maintenance',
    help=_('Either put or resume host to/from maintenance.'))
def do_host_update(cs, args):
    """Update host settings."""
    updates = {}
    columns = ["HOST"]
    if args.status:
        updates['status'] = args.status
        columns.append("status")
    if args.maintenance:
        updates['maintenance_mode'] = args.maintenance
        columns.append("maintenance_mode")
    result = cs.hosts.update(args.host, updates)
    utils.print_list([result], columns)


@utils.arg('host', metavar='<hostname>', help=_('Name of host.'))
@utils.arg(
    '--action', metavar='<action>', dest='action',
    choices=['startup', 'shutdown', 'reboot'],
    help=_('A power action: startup, reboot, or shutdown.'))
def do_host_action(cs, args):
    """Perform a power action on a host."""
    result = cs.hosts.host_action(args.host, args.action)
    utils.print_list([result], ['HOST', 'power_action'])


def _find_hypervisor(cs, hypervisor):
    """Get a hypervisor by name or ID."""
    return utils.find_resource(cs.hypervisors, hypervisor)


def _do_hypervisor_list(cs, matching=None, limit=None, marker=None):
    columns = ['ID', 'Hypervisor hostname', 'State', 'Status']
    if matching:
        utils.print_list(cs.hypervisors.search(matching), columns)
    else:
        params = {}
        if limit is not None:
            params['limit'] = limit
        if marker is not None:
            params['marker'] = marker
        # Since we're not outputting detail data, choose
        # detailed=False for server-side efficiency
        utils.print_list(cs.hypervisors.list(False, **params), columns)


@api_versions.wraps("2.0", "2.32")
@utils.arg(
    '--matching',
    metavar='<hostname>',
    default=None,
    help=_('List hypervisors matching the given <hostname>.'))
def do_hypervisor_list(cs, args):
    """List hypervisors."""
    _do_hypervisor_list(cs, matching=args.matching)


@api_versions.wraps("2.33")
@utils.arg(
    '--matching',
    metavar='<hostname>',
    default=None,
    help=_('List hypervisors matching the given <hostname>. '
           'If matching is used limit and marker options will be ignored.'))
@utils.arg(
    '--marker',
    dest='marker',
    metavar='<marker>',
    default=None,
    help=_('The last hypervisor of the previous page; displays list of '
           'hypervisors after "marker".'))
@utils.arg(
    '--limit',
    dest='limit',
    metavar='<limit>',
    type=int,
    default=None,
    help=_("Maximum number of hypervisors to display. If limit == -1, all "
           "hypervisors will be displayed. If limit is bigger than "
           "'osapi_max_limit' option of Nova API, limit 'osapi_max_limit' "
           "will be used instead."))
def do_hypervisor_list(cs, args):
    """List hypervisors."""
    _do_hypervisor_list(
        cs, matching=args.matching, limit=args.limit, marker=args.marker)


@utils.arg(
    'hostname',
    metavar='<hostname>',
    help=_('The hypervisor hostname (or pattern) to search for.'))
def do_hypervisor_servers(cs, args):
    """List servers belonging to specific hypervisors."""
    hypers = cs.hypervisors.search(args.hostname, servers=True)

    class InstanceOnHyper(object):
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    # Massage the result into a list to be displayed
    instances = []
    for hyper in hypers:
        hyper_host = hyper.hypervisor_hostname
        hyper_id = hyper.id
        if hasattr(hyper, 'servers'):
            instances.extend([InstanceOnHyper(id=serv['uuid'],
                                              name=serv['name'],
                                              hypervisor_hostname=hyper_host,
                                              hypervisor_id=hyper_id)
                              for serv in hyper.servers])

    # Output the data
    utils.print_list(instances, ['ID', 'Name', 'Hypervisor ID',
                                 'Hypervisor Hostname'])


@utils.arg(
    'hypervisor',
    metavar='<hypervisor>',
    help=_('Name or ID of the hypervisor to show the details of.'))
@utils.arg(
    '--wrap', dest='wrap', metavar='<integer>', default=40,
    help=_('Wrap the output to a specified length. '
           'Default is 40 or 0 to disable'))
def do_hypervisor_show(cs, args):
    """Display the details of the specified hypervisor."""
    hyper = _find_hypervisor(cs, args.hypervisor)
    utils.print_dict(utils.flatten_dict(hyper._info), wrap=int(args.wrap))


@utils.arg(
    'hypervisor',
    metavar='<hypervisor>',
    help=_('Name or ID of the hypervisor to show the uptime of.'))
def do_hypervisor_uptime(cs, args):
    """Display the uptime of the specified hypervisor."""
    hyper = _find_hypervisor(cs, args.hypervisor)
    hyper = cs.hypervisors.uptime(hyper)

    # Output the uptime information
    utils.print_dict(hyper._info.copy())


def do_hypervisor_stats(cs, args):
    """Get hypervisor statistics over all compute nodes."""
    stats = cs.hypervisor_stats.statistics()
    utils.print_dict(stats._info.copy())


def ensure_service_catalog_present(cs):
    if not hasattr(cs.client, 'service_catalog'):
        # Turn off token caching and re-auth
        cs.client.unauthenticate()
        cs.client.use_token_cache(False)
        cs.client.authenticate()


def do_endpoints(cs, _args):
    """Discover endpoints that get returned from the authenticate services."""
    warnings.warn(
        "nova endpoints is deprecated, use openstack catalog list instead")
    if isinstance(cs.client, client.SessionClient):
        access = cs.client.auth.get_access(cs.client.session)
        for service in access.service_catalog.catalog:
            _print_endpoints(service, cs.client.region_name)
    else:
        ensure_service_catalog_present(cs)

        catalog = cs.client.service_catalog.catalog
        region = cs.client.region_name
        for service in catalog['access']['serviceCatalog']:
            _print_endpoints(service, region)


def _print_endpoints(service, region):
    name, endpoints = service["name"], service["endpoints"]

    try:
        endpoint = _get_first_endpoint(endpoints, region)
        utils.print_dict(endpoint, name)
    except LookupError:
        print(_("WARNING: %(service)s has no endpoint in %(region)s! "
                "Available endpoints for this service:") %
              {'service': name, 'region': region})
        for other_endpoint in endpoints:
            utils.print_dict(other_endpoint, name)


def _get_first_endpoint(endpoints, region):
    """Find the first suitable endpoint in endpoints.

    If there is only one endpoint, return it. If there is more than
    one endpoint, return the first one with the given region. If there
    are no endpoints, or there is more than one endpoint but none of
    them match the given region, raise KeyError.

    """
    if len(endpoints) == 1:
        return endpoints[0]
    else:
        for candidate_endpoint in endpoints:
            if candidate_endpoint["region"] == region:
                return candidate_endpoint

    raise LookupError("No suitable endpoint found")


@utils.arg(
    '--wrap', dest='wrap', metavar='<integer>', default=64,
    help=_('Wrap PKI tokens to a specified length, or 0 to disable.'))
def do_credentials(cs, _args):
    """Show user credentials returned from auth."""
    warnings.warn(
        "nova credentials is deprecated, use openstack client instead")
    if isinstance(cs.client, client.SessionClient):
        access = cs.client.auth.get_access(cs.client.session)
        utils.print_dict(access._user, 'User Credentials',
                         wrap=int(_args.wrap))
        if hasattr(access, '_token'):
            utils.print_dict(access._token, 'Token', wrap=int(_args.wrap))
    else:
        ensure_service_catalog_present(cs)
        catalog = cs.client.service_catalog.catalog
        utils.print_dict(catalog['access']['user'], "User Credentials",
                         wrap=int(_args.wrap))
        utils.print_dict(catalog['access']['token'], "Token",
                         wrap=int(_args.wrap))


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg(
    '--port',
    dest='port',
    action='store',
    type=int,
    default=22,
    help=_('Optional flag to indicate which port to use for ssh. '
           '(Default=22)'))
@utils.arg(
    '--private',
    dest='private',
    action='store_true',
    default=False,
    help=argparse.SUPPRESS)
@utils.arg(
    '--address-type',
    dest='address_type',
    action='store',
    type=str,
    default='floating',
    help=_('Optional flag to indicate which IP type to use. Possible values  '
           'includes fixed and floating (the Default).'))
@utils.arg(
    '--network', metavar='<network>',
    help=_('Network to use for the ssh.'), default=None)
@utils.arg(
    '--ipv6',
    dest='ipv6',
    action='store_true',
    default=False,
    help=_('Optional flag to indicate whether to use an IPv6 address '
           'attached to a server. (Defaults to IPv4 address)'))
@utils.arg(
    '--login', metavar='<login>', help=_('Login to use.'),
    default="root")
@utils.arg(
    '-i', '--identity',
    dest='identity',
    help=_('Private key file, same as the -i option to the ssh command.'),
    default='')
@utils.arg(
    '--extra-opts',
    dest='extra',
    help=_('Extra options to pass to ssh. see: man ssh.'),
    default='')
def do_ssh(cs, args):
    """SSH into a server."""
    if '@' in args.server:
        user, server = args.server.split('@', 1)
        args.login = user
        args.server = server

    addresses = _find_server(cs, args.server).addresses
    address_type = "fixed" if args.private else args.address_type
    version = 6 if args.ipv6 else 4
    pretty_version = 'IPv%d' % version

    # Select the network to use.
    if args.network:
        network_addresses = addresses.get(args.network)
        if not network_addresses:
            msg = _("Server '%(server)s' is not attached to network "
                    "'%(network)s'")
            raise exceptions.ResourceNotFound(
                msg % {'server': args.server, 'network': args.network})
    else:
        if len(addresses) > 1:
            msg = _("Server '%(server)s' is attached to more than one network."
                    " Please pick the network to use.")
            raise exceptions.CommandError(msg % {'server': args.server})
        elif not addresses:
            msg = _("Server '%(server)s' is not attached to any network.")
            raise exceptions.CommandError(msg % {'server': args.server})
        else:
            network_addresses = list(six.itervalues(addresses))[0]

    # Select the address in the selected network.
    # If the extension is not present, we assume the address to be floating.
    match = lambda addr: all((
        addr.get('version') == version,
        addr.get('OS-EXT-IPS:type', 'floating') == address_type))
    matching_addresses = [address.get('addr')
                          for address in network_addresses if match(address)]
    if not any(matching_addresses):
        msg = _("No address that would match network '%(network)s'"
                " and type '%(address_type)s' of version %(pretty_version)s "
                "has been found for server '%(server)s'.")
        raise exceptions.ResourceNotFound(msg % {
            'network': args.network, 'address_type': address_type,
            'pretty_version': pretty_version, 'server': args.server})
    elif len(matching_addresses) > 1:
        msg = _("More than one %(pretty_version)s %(address_type)s address "
                "found.")
        raise exceptions.CommandError(msg % {'pretty_version': pretty_version,
                                             'address_type': address_type})
    else:
        ip_address = matching_addresses[0]

    identity = '-i %s' % args.identity if len(args.identity) else ''

    cmd = "ssh -%d -p%d %s %s@%s %s" % (version, args.port, identity,
                                        args.login, ip_address, args.extra)
    logger.debug("Executing cmd '%s'", cmd)
    os.system(cmd)


_quota_resources = ['instances', 'cores', 'ram',
                    'floating_ips', 'fixed_ips', 'metadata_items',
                    'injected_files', 'injected_file_content_bytes',
                    'injected_file_path_bytes', 'key_pairs',
                    'security_groups', 'security_group_rules',
                    'server_groups', 'server_group_members']


def _quota_show(quotas):
    class FormattedQuota(object):
        def __init__(self, key, value):
            setattr(self, 'quota', key)
            setattr(self, 'limit', value)

    quota_list = []
    for resource in _quota_resources:
        try:
            quota = FormattedQuota(resource, getattr(quotas, resource))
            quota_list.append(quota)
        except AttributeError:
            pass
    columns = ['Quota', 'Limit']
    utils.print_list(quota_list, columns)


def _quota_update(manager, identifier, args):
    updates = {}
    for resource in _quota_resources:
        val = getattr(args, resource, None)
        if val is not None:
            updates[resource] = val

    if updates:
        # default value of force is None to make sure this client
        # will be compatible with old nova server
        force_update = getattr(args, 'force', None)
        user_id = getattr(args, 'user', None)
        if isinstance(manager, quotas.QuotaSetManager):
            manager.update(identifier, force=force_update, user_id=user_id,
                           **updates)
        else:
            manager.update(identifier, **updates)


@utils.arg(
    '--tenant',
    metavar='<tenant-id>',
    default=None,
    help=_('ID of tenant to list the quotas for.'))
@utils.arg(
    '--user',
    metavar='<user-id>',
    default=None,
    help=_('ID of user to list the quotas for.'))
@utils.arg(
    '--detail',
    action='store_true',
    default=False,
    help=_('Show detailed info (limit, reserved, in-use).'))
def do_quota_show(cs, args):
    """List the quotas for a tenant/user."""

    if args.tenant:
        project_id = args.tenant
    elif isinstance(cs.client, client.SessionClient):
        auth = cs.client.auth
        project_id = auth.get_auth_ref(cs.client.session).project_id
    else:
        project_id = cs.client.tenant_id

    _quota_show(cs.quotas.get(project_id, user_id=args.user,
                              detail=args.detail))


@utils.arg(
    '--tenant',
    metavar='<tenant-id>',
    default=None,
    help=_('ID of tenant to list the default quotas for.'))
def do_quota_defaults(cs, args):
    """List the default quotas for a tenant."""

    if args.tenant:
        project_id = args.tenant
    elif isinstance(cs.client, client.SessionClient):
        auth = cs.client.auth
        project_id = auth.get_auth_ref(cs.client.session).project_id
    else:
        project_id = cs.client.tenant_id

    _quota_show(cs.quotas.defaults(project_id))


@api_versions.wraps("2.0", "2.35")
@utils.arg(
    'tenant',
    metavar='<tenant-id>',
    help=_('ID of tenant to set the quotas for.'))
@utils.arg(
    '--user',
    metavar='<user-id>',
    default=None,
    help=_('ID of user to set the quotas for.'))
@utils.arg(
    '--instances',
    metavar='<instances>',
    type=int, default=None,
    help=_('New value for the "instances" quota.'))
@utils.arg(
    '--cores',
    metavar='<cores>',
    type=int, default=None,
    help=_('New value for the "cores" quota.'))
@utils.arg(
    '--ram',
    metavar='<ram>',
    type=int, default=None,
    help=_('New value for the "ram" quota.'))
@utils.arg(
    '--floating-ips',
    metavar='<floating-ips>',
    type=int,
    default=None,
    action=shell.DeprecatedAction,
    help=_('New value for the "floating-ips" quota.'))
@utils.arg(
    '--fixed-ips',
    metavar='<fixed-ips>',
    type=int,
    default=None,
    action=shell.DeprecatedAction,
    help=_('New value for the "fixed-ips" quota.'))
@utils.arg(
    '--metadata-items',
    metavar='<metadata-items>',
    type=int,
    default=None,
    help=_('New value for the "metadata-items" quota.'))
@utils.arg(
    '--injected-files',
    metavar='<injected-files>',
    type=int,
    default=None,
    help=_('New value for the "injected-files" quota.'))
@utils.arg(
    '--injected-file-content-bytes',
    metavar='<injected-file-content-bytes>',
    type=int,
    default=None,
    help=_('New value for the "injected-file-content-bytes" quota.'))
@utils.arg(
    '--injected-file-path-bytes',
    metavar='<injected-file-path-bytes>',
    type=int,
    default=None,
    help=_('New value for the "injected-file-path-bytes" quota.'))
@utils.arg(
    '--key-pairs',
    metavar='<key-pairs>',
    type=int,
    default=None,
    help=_('New value for the "key-pairs" quota.'))
@utils.arg(
    '--security-groups',
    metavar='<security-groups>',
    type=int,
    default=None,
    action=shell.DeprecatedAction,
    help=_('New value for the "security-groups" quota.'))
@utils.arg(
    '--security-group-rules',
    metavar='<security-group-rules>',
    type=int,
    default=None,
    action=shell.DeprecatedAction,
    help=_('New value for the "security-group-rules" quota.'))
@utils.arg(
    '--server-groups',
    metavar='<server-groups>',
    type=int,
    default=None,
    help=_('New value for the "server-groups" quota.'))
@utils.arg(
    '--server-group-members',
    metavar='<server-group-members>',
    type=int,
    default=None,
    help=_('New value for the "server-group-members" quota.'))
@utils.arg(
    '--force',
    dest='force',
    action="store_true",
    default=None,
    help=_('Whether force update the quota even if the already used and '
           'reserved exceeds the new quota.'))
def do_quota_update(cs, args):
    """Update the quotas for a tenant/user."""

    _quota_update(cs.quotas, args.tenant, args)


# 2.36 does not support updating quota for floating IPs, fixed IPs, security
# groups or security group rules.
@api_versions.wraps("2.36")
@utils.arg(
    'tenant',
    metavar='<tenant-id>',
    help=_('ID of tenant to set the quotas for.'))
@utils.arg(
    '--user',
    metavar='<user-id>',
    default=None,
    help=_('ID of user to set the quotas for.'))
@utils.arg(
    '--instances',
    metavar='<instances>',
    type=int, default=None,
    help=_('New value for the "instances" quota.'))
@utils.arg(
    '--cores',
    metavar='<cores>',
    type=int, default=None,
    help=_('New value for the "cores" quota.'))
@utils.arg(
    '--ram',
    metavar='<ram>',
    type=int, default=None,
    help=_('New value for the "ram" quota.'))
@utils.arg(
    '--metadata-items',
    metavar='<metadata-items>',
    type=int,
    default=None,
    help=_('New value for the "metadata-items" quota.'))
@utils.arg(
    '--injected-files',
    metavar='<injected-files>',
    type=int,
    default=None,
    help=_('New value for the "injected-files" quota.'))
@utils.arg(
    '--injected-file-content-bytes',
    metavar='<injected-file-content-bytes>',
    type=int,
    default=None,
    help=_('New value for the "injected-file-content-bytes" quota.'))
@utils.arg(
    '--injected-file-path-bytes',
    metavar='<injected-file-path-bytes>',
    type=int,
    default=None,
    help=_('New value for the "injected-file-path-bytes" quota.'))
@utils.arg(
    '--key-pairs',
    metavar='<key-pairs>',
    type=int,
    default=None,
    help=_('New value for the "key-pairs" quota.'))
@utils.arg(
    '--server-groups',
    metavar='<server-groups>',
    type=int,
    default=None,
    help=_('New value for the "server-groups" quota.'))
@utils.arg(
    '--server-group-members',
    metavar='<server-group-members>',
    type=int,
    default=None,
    help=_('New value for the "server-group-members" quota.'))
@utils.arg(
    '--force',
    dest='force',
    action="store_true",
    default=None,
    help=_('Whether force update the quota even if the already used and '
           'reserved exceeds the new quota.'))
def do_quota_update(cs, args):
    """Update the quotas for a tenant/user."""

    _quota_update(cs.quotas, args.tenant, args)


@utils.arg(
    '--tenant',
    metavar='<tenant-id>',
    required=True,
    help=_('ID of tenant to delete quota for.'))
@utils.arg(
    '--user',
    metavar='<user-id>',
    help=_('ID of user to delete quota for.'))
def do_quota_delete(cs, args):
    """Delete quota for a tenant/user so their quota will Revert
       back to default.
    """

    cs.quotas.delete(args.tenant, user_id=args.user)


@utils.arg(
    'class_name',
    metavar='<class>',
    help=_('Name of quota class to list the quotas for.'))
def do_quota_class_show(cs, args):
    """List the quotas for a quota class."""

    _quota_show(cs.quota_classes.get(args.class_name))


@api_versions.wraps("2.0", "2.35")
@utils.arg(
    'class_name',
    metavar='<class>',
    help=_('Name of quota class to set the quotas for.'))
@utils.arg(
    '--instances',
    metavar='<instances>',
    type=int, default=None,
    help=_('New value for the "instances" quota.'))
@utils.arg(
    '--cores',
    metavar='<cores>',
    type=int, default=None,
    help=_('New value for the "cores" quota.'))
@utils.arg(
    '--ram',
    metavar='<ram>',
    type=int, default=None,
    help=_('New value for the "ram" quota.'))
@utils.arg(
    '--floating-ips',
    metavar='<floating-ips>',
    type=int,
    default=None,
    action=shell.DeprecatedAction,
    help=_('New value for the "floating-ips" quota.'))
@utils.arg(
    '--fixed-ips',
    metavar='<fixed-ips>',
    type=int,
    default=None,
    action=shell.DeprecatedAction,
    help=_('New value for the "fixed-ips" quota.'))
@utils.arg(
    '--metadata-items',
    metavar='<metadata-items>',
    type=int,
    default=None,
    help=_('New value for the "metadata-items" quota.'))
@utils.arg(
    '--injected-files',
    metavar='<injected-files>',
    type=int,
    default=None,
    help=_('New value for the "injected-files" quota.'))
@utils.arg(
    '--injected-file-content-bytes',
    metavar='<injected-file-content-bytes>',
    type=int,
    default=None,
    help=_('New value for the "injected-file-content-bytes" quota.'))
@utils.arg(
    '--injected-file-path-bytes',
    metavar='<injected-file-path-bytes>',
    type=int,
    default=None,
    help=_('New value for the "injected-file-path-bytes" quota.'))
@utils.arg(
    '--key-pairs',
    metavar='<key-pairs>',
    type=int,
    default=None,
    help=_('New value for the "key-pairs" quota.'))
@utils.arg(
    '--security-groups',
    metavar='<security-groups>',
    type=int,
    default=None,
    action=shell.DeprecatedAction,
    help=_('New value for the "security-groups" quota.'))
@utils.arg(
    '--security-group-rules',
    metavar='<security-group-rules>',
    type=int,
    default=None,
    action=shell.DeprecatedAction,
    help=_('New value for the "security-group-rules" quota.'))
@utils.arg(
    '--server-groups',
    metavar='<server-groups>',
    type=int,
    default=None,
    help=_('New value for the "server-groups" quota.'))
@utils.arg(
    '--server-group-members',
    metavar='<server-group-members>',
    type=int,
    default=None,
    help=_('New value for the "server-group-members" quota.'))
def do_quota_class_update(cs, args):
    """Update the quotas for a quota class."""

    _quota_update(cs.quota_classes, args.class_name, args)


# 2.36 does not support updating quota for floating IPs, fixed IPs, security
# groups or security group rules.
@api_versions.wraps("2.36")
@utils.arg(
    'class_name',
    metavar='<class>',
    help=_('Name of quota class to set the quotas for.'))
@utils.arg(
    '--instances',
    metavar='<instances>',
    type=int, default=None,
    help=_('New value for the "instances" quota.'))
@utils.arg(
    '--cores',
    metavar='<cores>',
    type=int, default=None,
    help=_('New value for the "cores" quota.'))
@utils.arg(
    '--ram',
    metavar='<ram>',
    type=int, default=None,
    help=_('New value for the "ram" quota.'))
@utils.arg(
    '--metadata-items',
    metavar='<metadata-items>',
    type=int,
    default=None,
    help=_('New value for the "metadata-items" quota.'))
@utils.arg(
    '--injected-files',
    metavar='<injected-files>',
    type=int,
    default=None,
    help=_('New value for the "injected-files" quota.'))
@utils.arg(
    '--injected-file-content-bytes',
    metavar='<injected-file-content-bytes>',
    type=int,
    default=None,
    help=_('New value for the "injected-file-content-bytes" quota.'))
@utils.arg(
    '--injected-file-path-bytes',
    metavar='<injected-file-path-bytes>',
    type=int,
    default=None,
    help=_('New value for the "injected-file-path-bytes" quota.'))
@utils.arg(
    '--key-pairs',
    metavar='<key-pairs>',
    type=int,
    default=None,
    help=_('New value for the "key-pairs" quota.'))
@utils.arg(
    '--server-groups',
    metavar='<server-groups>',
    type=int,
    default=None,
    help=_('New value for the "server-groups" quota.'))
@utils.arg(
    '--server-group-members',
    metavar='<server-group-members>',
    type=int,
    default=None,
    help=_('New value for the "server-group-members" quota.'))
def do_quota_class_update(cs, args):
    """Update the quotas for a quota class."""

    _quota_update(cs.quota_classes, args.class_name, args)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg(
    'host', metavar='<host>', nargs='?',
    help=_("Name or ID of the target host.  "
           "If no host is specified, the scheduler will choose one."))
@utils.arg(
    '--password',
    dest='password',
    metavar='<password>',
    help=_("Set the provided admin password on the evacuated server. Not"
            " applicable if the server is on shared storage."))
@utils.arg(
    '--on-shared-storage',
    dest='on_shared_storage',
    action="store_true",
    default=False,
    help=_('Specifies whether server files are located on shared storage.'),
    start_version='2.0',
    end_version='2.13')
@utils.arg(
    '--force',
    dest='force',
    action='store_true',
    default=False,
    help=_('Force to not verify the scheduler if a host is provided.'),
    start_version='2.29')
def do_evacuate(cs, args):
    """Evacuate server from failed host."""

    server = _find_server(cs, args.server)
    on_shared_storage = getattr(args, 'on_shared_storage', None)
    force = getattr(args, 'force', None)
    update_kwargs = {}
    if on_shared_storage is not None:
        update_kwargs['on_shared_storage'] = on_shared_storage
    if force:
        update_kwargs['force'] = force
    res = server.evacuate(host=args.host, password=args.password,
                          **update_kwargs)[1]
    if isinstance(res, dict):
        utils.print_dict(res)


def _print_interfaces(interfaces):
    columns = ['Port State', 'Port ID', 'Net ID', 'IP addresses',
               'MAC Addr']

    class FormattedInterface(object):
        def __init__(self, interface):
            for col in columns:
                key = col.lower().replace(" ", "_")
                if hasattr(interface, key):
                    setattr(self, key, getattr(interface, key))
            self.ip_addresses = ",".join([fip['ip_address']
                                          for fip in interface.fixed_ips])
    utils.print_list([FormattedInterface(i) for i in interfaces], columns)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_interface_list(cs, args):
    """List interfaces attached to a server."""
    server = _find_server(cs, args.server)

    res = server.interface_list()
    if isinstance(res, list):
        _print_interfaces(res)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg(
    '--port-id',
    metavar='<port_id>',
    help=_('Port ID.'),
    dest="port_id")
@utils.arg(
    '--net-id',
    metavar='<net_id>',
    help=_('Network ID'),
    default=None, dest="net_id")
@utils.arg(
    '--fixed-ip',
    metavar='<fixed_ip>',
    help=_('Requested fixed IP.'),
    default=None, dest="fixed_ip")
def do_interface_attach(cs, args):
    """Attach a network interface to a server."""
    server = _find_server(cs, args.server)

    res = server.interface_attach(args.port_id, args.net_id, args.fixed_ip)
    if isinstance(res, dict):
        utils.print_dict(res)


@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg('port_id', metavar='<port_id>', help=_('Port ID.'))
def do_interface_detach(cs, args):
    """Detach a network interface from a server."""
    server = _find_server(cs, args.server)

    res = server.interface_detach(args.port_id)
    if isinstance(res, dict):
        utils.print_dict(res)


@api_versions.wraps("2.17")
@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_trigger_crash_dump(cs, args):
    """Trigger crash dump in an instance."""
    server = _find_server(cs, args.server)

    server.trigger_crash_dump()


def _treeizeAvailabilityZone(zone):
    """Build a tree view for availability zones."""
    AvailabilityZone = availability_zones.AvailabilityZone

    az = AvailabilityZone(zone.manager,
                          copy.deepcopy(zone._info), zone._loaded)
    result = []

    # Zone tree view item
    az.zoneName = zone.zoneName
    az.zoneState = ('available'
                    if zone.zoneState['available'] else 'not available')
    az._info['zoneName'] = az.zoneName
    az._info['zoneState'] = az.zoneState
    result.append(az)

    if zone.hosts is not None:
        zone_hosts = sorted(zone.hosts.items(), key=lambda x: x[0])
        for (host, services) in zone_hosts:
            # Host tree view item
            az = AvailabilityZone(zone.manager,
                                  copy.deepcopy(zone._info), zone._loaded)
            az.zoneName = '|- %s' % host
            az.zoneState = ''
            az._info['zoneName'] = az.zoneName
            az._info['zoneState'] = az.zoneState
            result.append(az)

            for (svc, state) in services.items():
                # Service tree view item
                az = AvailabilityZone(zone.manager,
                                      copy.deepcopy(zone._info), zone._loaded)
                az.zoneName = '| |- %s' % svc
                az.zoneState = '%s %s %s' % (
                               'enabled' if state['active'] else 'disabled',
                               ':-)' if state['available'] else 'XXX',
                               state['updated_at'])
                az._info['zoneName'] = az.zoneName
                az._info['zoneState'] = az.zoneState
                result.append(az)
    return result


@utils.service_type('compute')
def do_availability_zone_list(cs, _args):
    """List all the availability zones."""
    try:
        availability_zones = cs.availability_zones.list()
    except exceptions.Forbidden as e:  # policy doesn't allow probably
        try:
            availability_zones = cs.availability_zones.list(detailed=False)
        except Exception:
            raise e

    result = []
    for zone in availability_zones:
        result += _treeizeAvailabilityZone(zone)
    _translate_availability_zone_keys(result)
    utils.print_list(result, ['Name', 'Status'],
                     sortby_index=None)


@api_versions.wraps("2.0", "2.12")
def _print_server_group_details(cs, server_group):
    columns = ['Id', 'Name', 'Policies', 'Members', 'Metadata']
    utils.print_list(server_group, columns)


@api_versions.wraps("2.13")
def _print_server_group_details(cs, server_group):    # noqa
    columns = ['Id', 'Name', 'Project Id', 'User Id',
               'Policies', 'Members', 'Metadata']
    utils.print_list(server_group, columns)


@utils.arg(
    '--all-projects',
    dest='all_projects',
    action='store_true',
    default=False,
    help=_('Display server groups from all projects (Admin only).'))
def do_server_group_list(cs, args):
    """Print a list of all server groups."""
    server_groups = cs.server_groups.list(args.all_projects)
    _print_server_group_details(cs, server_groups)


@deprecated_network
def do_secgroup_list_default_rules(cs, args):
    """List rules that will be added to the 'default' security group for
    new tenants.
    """
    _print_secgroup_rules(cs.security_group_default_rules.list(),
                          show_source_group=False)


@utils.arg(
    'ip_proto',
    metavar='<ip-proto>',
    help=_('IP protocol (icmp, tcp, udp).'))
@utils.arg(
    'from_port',
    metavar='<from-port>',
    help=_('Port at start of range.'))
@utils.arg(
    'to_port',
    metavar='<to-port>',
    help=_('Port at end of range.'))
@utils.arg('cidr', metavar='<cidr>', help=_('CIDR for address range.'))
@deprecated_network
def do_secgroup_add_default_rule(cs, args):
    """Add a rule to the set of rules that will be added to the 'default'
    security group for new tenants (nova-network only).
    """
    rule = cs.security_group_default_rules.create(args.ip_proto,
                                                  args.from_port,
                                                  args.to_port,
                                                  args.cidr)
    _print_secgroup_rules([rule], show_source_group=False)


@utils.arg(
    'ip_proto',
    metavar='<ip-proto>',
    help=_('IP protocol (icmp, tcp, udp).'))
@utils.arg(
    'from_port',
    metavar='<from-port>',
    help=_('Port at start of range.'))
@utils.arg(
    'to_port',
    metavar='<to-port>',
    help=_('Port at end of range.'))
@utils.arg('cidr', metavar='<cidr>', help=_('CIDR for address range.'))
@deprecated_network
def do_secgroup_delete_default_rule(cs, args):
    """Delete a rule from the set of rules that will be added to the
    'default' security group for new tenants (nova-network only).
    """
    for rule in cs.security_group_default_rules.list():
        if (rule.ip_protocol and
                rule.ip_protocol.upper() == args.ip_proto.upper() and
                rule.from_port == int(args.from_port) and
                rule.to_port == int(args.to_port) and
                rule.ip_range['cidr'] == args.cidr):
            _print_secgroup_rules([rule], show_source_group=False)
            return cs.security_group_default_rules.delete(rule.id)

    raise exceptions.CommandError(_("Rule not found"))


@utils.arg('name', metavar='<name>', help=_('Server group name.'))
# NOTE(wingwj): The '--policy' way is still reserved here for preserving
# the backwards compatibility of CLI, even if a user won't get this usage
# in '--help' description. It will be deprecated after a suitable deprecation
# period(probably 2 coordinated releases or so).
#
# Moreover, we imagine that a given user will use only positional parameters or
# only the "--policy" option. So we don't need to properly handle
# the possibility that they might mix them here. That usage is unsupported.
# The related discussion can be found in
# https://review.openstack.org/#/c/96382/2/.
@utils.arg(
    'policy',
    metavar='<policy>',
    default=argparse.SUPPRESS,
    nargs='*',
    help=_('Policies for the server groups.'))
def do_server_group_create(cs, args):
    """Create a new server group with the specified details."""
    if not args.policy:
        raise exceptions.CommandError(_("at least one policy must be "
                                        "specified"))
    kwargs = {'name': args.name,
              'policies': args.policy}
    server_group = cs.server_groups.create(**kwargs)
    _print_server_group_details(cs, [server_group])


@utils.arg(
    'id',
    metavar='<id>',
    nargs='+',
    help=_("Unique ID(s) of the server group to delete."))
def do_server_group_delete(cs, args):
    """Delete specific server group(s)."""
    failure_count = 0

    for sg in args.id:
        try:
            cs.server_groups.delete(sg)
            print(_("Server group %s has been successfully deleted.") % sg)
        except Exception as e:
            failure_count += 1
            print(_("Delete for server group %(sg)s failed: %(e)s") %
                  {'sg': sg, 'e': e})
    if failure_count == len(args.id):
        raise exceptions.CommandError(_("Unable to delete any of the "
                                        "specified server groups."))


@utils.arg(
    'id',
    metavar='<id>',
    help=_("Unique ID of the server group to get."))
def do_server_group_get(cs, args):
    """Get a specific server group."""
    server_group = cs.server_groups.get(args.id)
    _print_server_group_details(cs, [server_group])


def do_version_list(cs, args):
    """List all API versions."""
    result = cs.versions.list()
    if 'min_version' in dir(result[0]):
        columns = ["Id", "Status", "Updated", "Min Version", "Version"]
    else:
        columns = ["Id", "Status", "Updated"]

    print(_("Client supported API versions:"))
    print(_("Minimum version %(v)s") %
          {'v': novaclient.API_MIN_VERSION.get_string()})
    print(_("Maximum version %(v)s") %
          {'v': novaclient.API_MAX_VERSION.get_string()})

    print(_("\nServer supported API versions:"))
    utils.print_list(result, columns)


@api_versions.wraps("2.0", "2.11")
def _print_virtual_interface_list(cs, interface_list):
    columns = ['Id', 'Mac address']
    utils.print_list(interface_list, columns)


@api_versions.wraps("2.12")
def _print_virtual_interface_list(cs, interface_list):
    columns = ['Id', 'Mac address', 'Network ID']
    formatters = {"Network ID": lambda o: o.net_id}
    utils.print_list(interface_list, columns, formatters)


@utils.arg('server', metavar='<server>', help=_('ID of server.'))
def do_virtual_interface_list(cs, args):
    """Show virtual interface info about the given server."""
    server = _find_server(cs, args.server)
    interface_list = cs.virtual_interfaces.list(base.getid(server))
    _print_virtual_interface_list(cs, interface_list)


@api_versions.wraps("2.26")
@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_server_tag_list(cs, args):
    """Get list of tags from a server."""
    server = _find_server(cs, args.server)
    tags = server.tag_list()
    formatters = {'Tag': lambda o: o}
    utils.print_list(tags, ['Tag'], formatters=formatters)


@api_versions.wraps("2.26")
@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg('tag', metavar='<tag>', help=_('Tag to add.'))
def do_server_tag_add(cs, args):
    """Add single tag to a server."""
    server = _find_server(cs, args.server)
    server.add_tag(args.tag)


@api_versions.wraps("2.26")
@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg('tags', metavar='<tags>', nargs='+', help=_('Tag(s) to set.'))
def do_server_tag_set(cs, args):
    """Set list of tags to a server."""
    server = _find_server(cs, args.server)
    server.set_tags(args.tags)


@api_versions.wraps("2.26")
@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
@utils.arg('tag', metavar='<tag>', help=_('Tag to delete.'))
def do_server_tag_delete(cs, args):
    """Delete single tag from a server."""
    server = _find_server(cs, args.server)
    server.delete_tag(args.tag)


@api_versions.wraps("2.26")
@utils.arg('server', metavar='<server>', help=_('Name or ID of server.'))
def do_server_tag_delete_all(cs, args):
    """Delete all tags from a server."""
    server = _find_server(cs, args.server)
    server.delete_all_tags()
