# Copyright 2010 Jacob Kaplan-Moss

# Copyright 2011 OpenStack LLC.
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

import getpass
import os
import uuid

from novaclient import exceptions
from novaclient import utils
from novaclient.v1_0 import client
from novaclient.v1_0 import backup_schedules
from novaclient.v1_0 import servers


CLIENT_CLASS = client.Client

# Choices for flags.
DAY_CHOICES = [getattr(backup_schedules, i).lower()
               for i in dir(backup_schedules)
               if i.startswith('BACKUP_WEEKLY_')]
HOUR_CHOICES = [getattr(backup_schedules, i).lower()
                for i in dir(backup_schedules)
                if i.startswith('BACKUP_DAILY_')]


# Sentinal for boot --key
AUTO_KEY = object()


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
@utils.arg('--enable', dest='enabled', default=None, action='store_true',
                                               help='Enable backups.')
@utils.arg('--disable', dest='enabled', action='store_false',
                                  help='Disable backups.')
@utils.arg('--weekly', metavar='<day>', choices=DAY_CHOICES,
     help='Schedule a weekly backup for <day> (one of: %s).' %
                              utils.pretty_choice_list(DAY_CHOICES))
@utils.arg('--daily', metavar='<time-window>', choices=HOUR_CHOICES,
     help='Schedule a daily backup during <time-window> (one of: %s).' %
                                       utils.pretty_choice_list(HOUR_CHOICES))
def do_backup_schedule(cs, args):
    """
    Show or edit the backup schedule for a server.

    With no flags, the backup schedule will be shown. If flags are given,
    the backup schedule will be modified accordingly.
    """
    server = _find_server(cs, args.server)

    # If we have some flags, update the backup
    backup = {}
    if args.daily:
        backup['daily'] = getattr(backup_schedules, 'BACKUP_DAILY_%s' %
                                                    args.daily.upper())
    if args.weekly:
        backup['weekly'] = getattr(backup_schedules, 'BACKUP_WEEKLY_%s' %
                                                     args.weekly.upper())
    if args.enabled is not None:
        backup['enabled'] = args.enabled
    if backup:
        server.backup_schedule.update(**backup)
    else:
        utils.print_dict(server.backup_schedule._info)


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
def do_backup_schedule_delete(cs, args):
    """
    Delete the backup schedule for a server.
    """
    server = _find_server(cs, args.server)
    server.backup_schedule.delete()


def _boot(cs, args, reservation_id=None, min_count=None, max_count=None):
    """Boot a new server."""
    if min_count is None:
        min_count = 1
    if max_count is None:
        max_count = min_count
    if min_count > max_count:
        raise exceptions.CommandError("min_instances should be"
                                      "<= max_instances")
    if not min_count or not max_count:
        raise exceptions.CommandError("min_instances nor max_instances"
                                      "should be 0")

    flavor = args.flavor or cs.flavors.find(ram=256)
    image = args.image or cs.images.find(name="Ubuntu 10.04 LTS "\
                                                   "(lucid)")

    # Map --ipgroup <name> to an ID.
    # XXX do this for flavor/image?
    if args.ipgroup:
        ipgroup = _find_ipgroup(cs, args.ipgroup)
    else:
        ipgroup = None

    metadata = dict(v.split('=') for v in args.meta)

    files = {}
    for f in args.files:
        dst, src = f.split('=', 1)
        try:
            files[dst] = open(src)
        except IOError, e:
            raise exceptions.CommandError("Can't open '%s': %s" % (src, e))

    if args.key is AUTO_KEY:
        possible_keys = [os.path.join(os.path.expanduser('~'), '.ssh', k)
                         for k in ('id_dsa.pub', 'id_rsa.pub')]
        for k in possible_keys:
            if os.path.exists(k):
                keyfile = k
                break
        else:
            raise exceptions.CommandError("Couldn't find a key file: tried "
                               "~/.ssh/id_dsa.pub or ~/.ssh/id_rsa.pub")
    elif args.key:
        keyfile = args.key
    else:
        keyfile = None

    if keyfile:
        try:
            files['/root/.ssh/authorized_keys2'] = open(keyfile)
        except IOError, e:
            raise exceptions.CommandError("Can't open '%s': %s" % (keyfile, e))

    return (args.name, image, flavor, ipgroup, metadata, files,
            reservation_id, min_count, max_count)


@utils.arg('--flavor',
     default=None,
     type=int,
     metavar='<flavor>',
     help="Flavor ID (see 'nova flavors'). "\
          "Defaults to 256MB RAM instance.")
@utils.arg('--image',
     default=None,
     type=int,
     metavar='<image>',
     help="Image ID (see 'nova images'). "\
          "Defaults to Ubuntu 10.04 LTS.")
@utils.arg('--ipgroup',
     default=None,
     metavar='<group>',
     help="IP group name or ID (see 'nova ipgroup-list').")
@utils.arg('--meta',
     metavar="<key=value>",
     action='append',
     default=[],
     help="Record arbitrary key/value metadata. "\
          "May be give multiple times.")
@utils.arg('--file',
     metavar="<dst-path=src-path>",
     action='append',
     dest='files',
     default=[],
     help="Store arbitrary files from <src-path> locally to <dst-path> "\
          "on the new server. You may store up to 5 files.")
@utils.arg('--key',
     metavar='<path>',
     nargs='?',
     const=AUTO_KEY,
     help="Key the server with an SSH keypair. "\
          "Looks in ~/.ssh for a key, "\
          "or takes an explicit <path> to one.")
@utils.arg('name', metavar='<name>', help='Name for the new server')
def do_boot(cs, args):
    """Boot a new server."""
    name, image, flavor, ipgroup, metadata, files, reservation_id, \
                min_count, max_count = _boot(cs, args)

    server = cs.servers.create(args.name, image, flavor,
                                    ipgroup=ipgroup,
                                    meta=metadata,
                                    files=files,
                                    min_count=min_count,
                                    max_count=max_count)
    utils.print_dict(server._info)


@utils.arg('--flavor',
     default=None,
     type=int,
     metavar='<flavor>',
     help="Flavor ID (see 'nova flavors'). "\
          "Defaults to 256MB RAM instance.")
@utils.arg('--image',
     default=None,
     type=int,
     metavar='<image>',
     help="Image ID (see 'nova images'). "\
          "Defaults to Ubuntu 10.04 LTS.")
@utils.arg('--ipgroup',
     default=None,
     metavar='<group>',
     help="IP group name or ID (see 'nova ipgroup-list').")
@utils.arg('--meta',
     metavar="<key=value>",
     action='append',
     default=[],
     help="Record arbitrary key/value metadata. "\
          "May be give multiple times.")
@utils.arg('--file',
     metavar="<dst-path=src-path>",
     action='append',
     dest='files',
     default=[],
     help="Store arbitrary files from <src-path> locally to <dst-path> "\
          "on the new server. You may store up to 5 files.")
@utils.arg('--key',
     metavar='<path>',
     nargs='?',
     const=AUTO_KEY,
     help="Key the server with an SSH keypair. "\
          "Looks in ~/.ssh for a key, "\
          "or takes an explicit <path> to one.")
@utils.arg('account', metavar='<account>', help='Account to build this'\
     ' server for')
@utils.arg('name', metavar='<name>', help='Name for the new server')
def do_boot_for_account(cs, args):
    """Boot a new server in an account."""
    name, image, flavor, ipgroup, metadata, files, reservation_id, \
            min_count, max_count = _boot(cs, args)

    server = cs.accounts.create_instance_for(args.account, args.name,
                image, flavor,
                ipgroup=ipgroup,
                meta=metadata,
                files=files)
    utils.print_dict(server._info)


@utils.arg('--flavor',
     default=None,
     type=int,
     metavar='<flavor>',
     help="Flavor ID (see 'nova flavors'). "\
          "Defaults to 256MB RAM instance.")
@utils.arg('--image',
     default=None,
     type=int,
     metavar='<image>',
     help="Image ID (see 'nova images'). "\
          "Defaults to Ubuntu 10.04 LTS.")
@utils.arg('--ipgroup',
     default=None,
     metavar='<group>',
     help="IP group name or ID (see 'nova ipgroup-list').")
@utils.arg('--meta',
     metavar="<key=value>",
     action='append',
     default=[],
     help="Record arbitrary key/value metadata. "\
          "May be give multiple times.")
@utils.arg('--file',
     metavar="<dst-path=src-path>",
     action='append',
     dest='files',
     default=[],
     help="Store arbitrary files from <src-path> locally to <dst-path> "\
          "on the new server. You may store up to 5 files.")
@utils.arg('--key',
     metavar='<path>',
     nargs='?',
     const=AUTO_KEY,
     help="Key the server with an SSH keypair. "\
          "Looks in ~/.ssh for a key, "\
          "or takes an explicit <path> to one.")
@utils.arg('--reservation_id',
     default=None,
     metavar='<reservation_id>',
     help="Reservation ID (a UUID). "\
          "If unspecified will be generated by the server.")
@utils.arg('--min_instances',
     default=None,
     type=int,
     metavar='<number>',
     help="The minimum number of instances to build. "\
             "Defaults to 1.")
@utils.arg('--max_instances',
     default=None,
     type=int,
     metavar='<number>',
     help="The maximum number of instances to build. "\
             "Defaults to 'min_instances' setting.")
@utils.arg('name', metavar='<name>', help='Name for the new server')
def do_zone_boot(cs, args):
    """Boot a new server, potentially across Zones."""
    reservation_id = args.reservation_id
    min_count = args.min_instances
    max_count = args.max_instances
    name, image, flavor, ipgroup, metadata, \
            files, reservation_id, min_count, max_count = \
                             _boot(cs, args,
                                        reservation_id=reservation_id,
                                        min_count=min_count,
                                        max_count=max_count)

    reservation_id = cs.zones.boot(args.name, image, flavor,
                                        ipgroup=ipgroup,
                                        meta=metadata,
                                        files=files,
                                        reservation_id=reservation_id,
                                        min_count=min_count,
                                        max_count=max_count)
    print "Reservation ID=", reservation_id


def _translate_flavor_keys(collection):
    convert = [('ram', 'memory_mb'), ('disk', 'local_gb')]
    for item in collection:
        keys = item.__dict__.keys()
        for from_key, to_key in convert:
            if from_key in keys and to_key not in keys:
                setattr(item, to_key, item._info[from_key])


def do_flavor_list(cs, args):
    """Print a list of available 'flavors' (sizes of servers)."""
    flavors = cs.flavors.list()
    _translate_flavor_keys(flavors)
    utils.print_list(flavors, [
        'ID',
        'Name',
        'Memory_MB',
        'Swap',
        'Local_GB',
        'VCPUs',
        'RXTX_Factor'])


def do_image_list(cs, args):
    """Print a list of available images to boot from."""
    server_list = {}
    for server in cs.servers.list():
        server_list[server.id] = server.name
    image_list = cs.images.list()
    for i in range(len(image_list)):
        if hasattr(image_list[i], 'serverId'):
            image_list[i].serverId = server_list[image_list[i].serverId] + \
            ' (' + str(image_list[i].serverId) + ')'
    utils.print_list(image_list, ['ID', 'Name', 'serverId', 'Status'])


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
@utils.arg('name', metavar='<name>', help='Name of snapshot.')
def do_image_create(cs, args):
    """Create a new image by taking a snapshot of a running server."""
    server = _find_server(cs, args.server)
    image = cs.images.create(server, args.name)
    utils.print_dict(image._info)


@utils.arg('image', metavar='<image>', help='Name or ID of image.')
def do_image_delete(cs, args):
    """
    Delete an image.

    It should go without saying, but you can only delete images you
    created.
    """
    image = _find_image(cs, args.image)
    image.delete()


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
@utils.arg('group', metavar='<group>', help='Name or ID of group.')
@utils.arg('address', metavar='<address>', help='IP address to share.')
def do_ip_share(cs, args):
    """Share an IP address from the given IP group onto a server."""
    server = _find_server(cs, args.server)
    group = _find_ipgroup(cs, args.group)
    server.share_ip(group, args.address)


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
@utils.arg('address', metavar='<address>',
                help='Shared IP address to remove from the server.')
def do_ip_unshare(cs, args):
    """Stop sharing an given address with a server."""
    server = _find_server(cs, args.server)
    server.unshare_ip(args.address)


def do_ipgroup_list(cs, args):
    """Show IP groups."""
    def pretty_server_list(ipgroup):
        return ", ".join(cs.servers.get(id).name
                         for id in ipgroup.servers)

    utils.print_list(cs.ipgroups.list(),
               fields=['ID', 'Name', 'Server List'],
               formatters={'Server List': pretty_server_list})


@utils.arg('group', metavar='<group>', help='Name or ID of group.')
def do_ipgroup_show(cs, args):
    """Show details about a particular IP group."""
    group = _find_ipgroup(cs, args.group)
    utils.print_dict(group._info)


@utils.arg('name', metavar='<name>', help='What to name this new group.')
@utils.arg('server', metavar='<server>', nargs='?',
     help='Server (name or ID) to make a member of this new group.')
def do_ipgroup_create(cs, args):
    """Create a new IP group."""
    if args.server:
        server = _find_server(cs, args.server)
    else:
        server = None
    group = cs.ipgroups.create(args.name, server)
    utils.print_dict(group._info)


@utils.arg('group', metavar='<group>', help='Name or ID of group.')
def do_ipgroup_delete(cs, args):
    """Delete an IP group."""
    _find_ipgroup(cs, args.group).delete()


@utils.arg('--fixed_ip',
    dest='fixed_ip',
    metavar='<fixed_ip>',
    default=None,
    help='Only match against fixed IP.')
@utils.arg('--reservation_id',
    dest='reservation_id',
    metavar='<reservation_id>',
    default=None,
    help='Only return instances that match reservation_id.')
@utils.arg('--recurse_zones',
    dest='recurse_zones',
    metavar='<0|1>',
    nargs='?',
    type=int,
    const=1,
    default=0,
    help='Recurse through all zones if set.')
@utils.arg('--ip',
    dest='ip',
    metavar='<ip_regexp>',
    default=None,
    help='Search with regular expression match by IP address')
@utils.arg('--ip6',
    dest='ip6',
    metavar='<ip6_regexp>',
    default=None,
    help='Search with regular expression match by IPv6 address')
@utils.arg('--name',
    dest='name',
    metavar='<name_regexp>',
    default=None,
    help='Search with regular expression match by name')
@utils.arg('--instance_name',
    dest='instance_name',
    metavar='<name_regexp>',
    default=None,
    help='Search with regular expression match by instance name')
@utils.arg('--status',
    dest='status',
    metavar='<status>',
    default=None,
    help='Search by server status')
@utils.arg('--flavor',
    dest='flavor',
    metavar='<flavor>',
    type=int,
    default=None,
    help='Search by flavor ID')
@utils.arg('--image',
    dest='image',
    type=int,
    metavar='<image>',
    default=None,
    help='Search by image ID')
@utils.arg('--host',
    dest='host',
    metavar='<hostname>',
    default=None,
    help="Search by instances by hostname to which they are assigned")
def do_list(cs, args):
    """List active servers."""
    recurse_zones = args.recurse_zones
    search_opts = {
            'reservation_id': args.reservation_id,
            'fixed_ip': args.fixed_ip,
            'recurse_zones': recurse_zones,
            'ip': args.ip,
            'ip6': args.ip6,
            'name': args.name,
            'image': args.image,
            'flavor': args.flavor,
            'status': args.status,
            'host': args.host,
            'instance_name': args.instance_name}
    if recurse_zones:
        to_print = ['UUID', 'Name', 'Status', 'Public IP', 'Private IP']
    else:
        to_print = ['ID', 'Name', 'Status', 'Public IP', 'Private IP']
    utils.print_list(cs.servers.list(search_opts=search_opts),
            to_print)


@utils.arg('--hard',
    dest='reboot_type',
    action='store_const',
    const=servers.REBOOT_HARD,
    default=servers.REBOOT_SOFT,
    help='Perform a hard reboot (instead of a soft one).')
@utils.arg('server', metavar='<server>', help='Name or ID of server.')
def do_reboot(cs, args):
    """Reboot a server."""
    _find_server(cs, args.server).reboot(args.reboot_type)


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
@utils.arg('image', metavar='<image>', help="Name or ID of new image.")
def do_rebuild(cs, args):
    """Shutdown, re-image, and re-boot a server."""
    server = _find_server(cs, args.server)
    image = _find_image(cs, args.image)
    server.rebuild(image)


@utils.arg('server', metavar='<server>',
           help='Name (old name) or ID of server.')
@utils.arg('name', metavar='<name>', help='New name for the server.')
def do_rename(cs, args):
    """Rename a server."""
    _find_server(cs, args.server).update(name=args.name)


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
@utils.arg('flavor', metavar='<flavor>', help="Name or ID of new flavor.")
def do_resize(cs, args):
    """Resize a server."""
    server = _find_server(cs, args.server)
    flavor = _find_flavor(cs, args.flavor)
    server.resize(flavor)


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
@utils.arg('name', metavar='<name>', help='Name of snapshot.')
@utils.arg('backup_type', metavar='<daily|weekly>', help='type of backup')
@utils.arg('rotation', type=int, metavar='<rotation>',
     help="Number of backups to retain. Used for backup image_type.")
def do_backup(cs, args):
    """Backup a server."""
    server = _find_server(cs, args.server)
    server.backup(args.name, args.backup_type, args.rotation)


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
def do_migrate(cs, args):
    """Migrate a server."""
    _find_server(cs, args.server).migrate()


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
def do_pause(cs, args):
    """Pause a server."""
    _find_server(cs, args.server).pause()


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
def do_unpause(cs, args):
    """Unpause a server."""
    _find_server(cs, args.server).unpause()


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
def do_suspend(cs, args):
    """Suspend a server."""
    _find_server(cs, args.server).suspend()


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
def do_resume(cs, args):
    """Resume a server."""
    _find_server(cs, args.server).resume()


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
def do_rescue(cs, args):
    """Rescue a server."""
    _find_server(cs, args.server).rescue()


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
def do_unrescue(cs, args):
    """Unrescue a server."""
    _find_server(cs, args.server).unrescue()


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
def do_diagnostics(cs, args):
    """Retrieve server diagnostics."""
    utils.print_dict(cs.servers.diagnostics(args.server)[1])


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
def do_actions(cs, args):
    """Retrieve server actions."""
    utils.print_list(
        cs.servers.actions(args.server),
        ["Created_At", "Action", "Error"])


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
def do_resize_confirm(cs, args):
    """Confirm a previous resize."""
    _find_server(cs, args.server).confirm_resize()


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
def do_resize_revert(cs, args):
    """Revert a previous resize (and return to the previous VM)."""
    _find_server(cs, args.server).revert_resize()


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
def do_root_password(cs, args):
    """
    Change the root password for a server.
    """
    server = _find_server(cs, args.server)
    p1 = getpass.getpass('New password: ')
    p2 = getpass.getpass('Again: ')
    if p1 != p2:
        raise exceptions.CommandError("Passwords do not match.")
    server.update(password=p1)


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
def do_show(cs, args):
    """Show details about the given server."""
    s = _find_server(cs, args.server)

    info = s._info.copy()
    addresses = info.pop('addresses')
    for addrtype in addresses:
        info['%s ip' % addrtype] = ', '.join(addresses[addrtype])

    flavorId = info.get('flavorId', None)
    if flavorId:
        info['flavor'] = _find_flavor(cs, info.pop('flavorId')).name
    imageId = info.get('imageId', None)
    if imageId:
        info['image'] = _find_image(cs, info.pop('imageId')).name

    utils.print_dict(info)


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
def do_delete(cs, args):
    """Immediately shut down and delete a server."""
    _find_server(cs, args.server).delete()


# --zone_username is required since --username is already used.
@utils.arg('zone', metavar='<zone_id>', help='ID of the zone', default=None)
@utils.arg('--api_url', dest='api_url', default=None, help='New URL.')
@utils.arg('--zone_username', dest='zone_username', default=None,
                        help='New zone username.')
@utils.arg('--zone_password', dest='zone_password', default=None,
                        help='New password.')
@utils.arg('--weight_offset', dest='weight_offset', default=None,
                        help='Child Zone weight offset.')
@utils.arg('--weight_scale', dest='weight_scale', default=None,
                        help='Child Zone weight scale.')
def do_zone(cs, args):
    """Show or edit a child zone. No zone arg for this zone."""
    zone = cs.zones.get(args.zone)

    # If we have some flags, update the zone
    zone_delta = {}
    if args.api_url:
        zone_delta['api_url'] = args.api_url
    if args.zone_username:
        zone_delta['username'] = args.zone_username
    if args.zone_password:
        zone_delta['password'] = args.zone_password
    if args.weight_offset:
        zone_delta['weight_offset'] = args.weight_offset
    if args.weight_scale:
        zone_delta['weight_scale'] = args.weight_scale
    if zone_delta:
        zone.update(**zone_delta)
    else:
        utils.print_dict(zone._info)


def do_zone_info(cs, args):
    """Get this zones name and capabilities."""
    zone = cs.zones.info()
    utils.print_dict(zone._info)


@utils.arg('zone_name', metavar='<zone_name>',
            help='Name of the child zone being added.')
@utils.arg('api_url', metavar='<api_url>', help="URL for the Zone's Auth API")
@utils.arg('--zone_username', metavar='<zone_username>',
            help='Optional Authentication username. (Default=None)',
            default=None)
@utils.arg('--zone_password', metavar='<zone_password>',
           help='Authentication password. (Default=None)',
           default=None)
@utils.arg('--weight_offset', metavar='<weight_offset>',
           help='Child Zone weight offset (Default=0.0))',
           default=0.0)
@utils.arg('--weight_scale', metavar='<weight_scale>',
           help='Child Zone weight scale (Default=1.0).',
           default=1.0)
def do_zone_add(cs, args):
    """Add a new child zone."""
    zone = cs.zones.create(args.zone_name, args.api_url,
                           args.zone_username, args.zone_password,
                           args.weight_offset, args.weight_scale)
    utils.print_dict(zone._info)


@utils.arg('zone', metavar='<zone>', help='Name or ID of the zone')
def do_zone_delete(cs, args):
    """Delete a zone."""
    cs.zones.delete(args.zone)


def do_zone_list(cs, args):
    """List the children of a zone."""
    utils.print_list(cs.zones.list(), ['ID', 'Name', 'Is Active', \
                        'API URL', 'Weight Offset', 'Weight Scale'])


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
@utils.arg('network_id', metavar='<network_id>', help='Network ID.')
def do_add_fixed_ip(cs, args):
    """Add new IP address to network."""
    server = _find_server(cs, args.server)
    server.add_fixed_ip(args.network_id)


@utils.arg('server', metavar='<server>', help='Name or ID of server.')
@utils.arg('address', metavar='<address>', help='IP Address.')
def do_remove_fixed_ip(cs, args):
    """Remove an IP address from a server."""
    server = _find_server(cs, args.server)
    server.remove_fixed_ip(args.address)


def _find_server(cs, server):
    """Get a server by name or ID."""
    return utils.find_resource(cs.servers, server)


def _find_ipgroup(cs, group):
    """Get an IP group by name or ID."""
    return utils.find_resource(cs.ipgroups, group)


def _find_image(cs, image):
    """Get an image by name or ID."""
    return utils.find_resource(cs.images, image)


def _find_flavor(cs, flavor):
    """Get a flavor by name, ID, or RAM size."""
    try:
        return utils.find_resource(cs.flavors, flavor)
    except exceptions.NotFound:
        return cs.flavors.find(ram=flavor)
