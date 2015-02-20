# commandline.py - commandline parsing functions
#
# Copyright (C) 2012 Red Hat Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Author: Will Woods <wwoods@redhat.com>

import os, argparse, platform

from . import media
from .sysprep import reset_boot, remove_boot, remove_cache, misc_cleanup
from . import _

import logging
log = logging.getLogger(__package__)

def parse_args(gui=False):
    p = argparse.ArgumentParser(
        description=_('Prepare system for upgrade.'),
        # Translators: This is the CLI's "usage:" string
        usage=_('%(prog)s <SOURCE> [options]'),
    )

    # === basic options ===
    p.add_argument('-v', '--verbose', action='store_const', dest='loglevel',
        const=logging.INFO, help=_('print more info'))
    p.add_argument('-d', '--debug', action='store_const', dest='loglevel',
        const=logging.DEBUG, help=_('print lots of debugging info'))
    p.set_defaults(loglevel=logging.WARNING)

    p.add_argument('--debuglog', default='/var/log/%s.log' % __package__,
        help=_('write lots of debugging output to the given file'))

    p.add_argument('--reboot', action='store_true', default=False,
        help=_('automatically reboot to start the upgrade when ready'))


    # === hidden options. FOR DEBUGGING ONLY. ===
    p.add_argument('--skippkgs', action='store_true', default=False,
        help=argparse.SUPPRESS)
    p.add_argument('--skipkernel', action='store_true', default=False,
        help=argparse.SUPPRESS)
    p.add_argument('--skipbootloader', action='store_true', default=False,
        help=argparse.SUPPRESS)
    p.add_argument('-C', '--cacheonly', action='store_true', default=False,
        help=argparse.SUPPRESS)
    p.add_argument('--logtraceback', action='store_true', default=False,
        help=argparse.SUPPRESS)


    # === yum options ===
    yumopts = p.add_argument_group(_('yum options'))
    yumopts.add_argument('--enableplugin', metavar='PLUGIN',
        action='append', dest='enable_plugins', default=[],
        help=_('enable yum plugins by name'))
    yumopts.add_argument('--disableplugin', metavar='PLUGIN',
        action='append', dest='disable_plugins', default=[],
        help=_('disable yum plugins by name'))
    yumopts.add_argument('--nogpgcheck', action='store_true', default=False,
        help=_('disable GPG signature checking'))
    yumopts.add_argument('--add-install', metavar='PKG-OR-GROUP',
        action='append', dest='add_install', default=[],
        help=_('add extra item to be installed during upgrade'))


    # Magical --product option only used for upgrading to Fedora 21
    legacy_fedora = False
    distro, version, id = platform.linux_distribution(supported_dists='fedora')
    if distro.lower() == 'fedora' and int(version) < 21:
        legacy_fedora = True
        yumopts.add_argument('--product', metavar='PRODUCTNAME', default=None,
            choices=('server','cloud','workstation','nonproduct'),
            help=_('Fedora product to install (for upgrades to F21)'))


    # === <SOURCE> options ===
    req = p.add_argument_group(_('options for <SOURCE>'),
                               _('Location to search for upgrade data.'))
    req.add_argument('--device', metavar='DEV', nargs='?',
        type=device_or_mnt, const='auto',
        help=_('device or mountpoint. default: check mounted devices'))
    req.add_argument('--iso', type=isofile,
        help=_('installation image file'))
    # Translators: This is for '--network [VERSION]' in --help output
    req.add_argument('--network', metavar=_('VERSION'), type=VERSION,
        help=_('online repos matching VERSION (a number or "rawhide")'))


    # === options for --network ===
    net = p.add_argument_group(_('additional options for --network'))
    net.add_argument('--enablerepo', metavar='REPOID', action=RepoAction,
        dest='repos', help=_('enable one or more repos (wildcards allowed)'))
    net.add_argument('--disablerepo', metavar='REPOID', action=RepoAction,
        dest='repos', help=_('disable one or more repos (wildcards allowed)'))
    net.add_argument('--addrepo', metavar='REPOID=[@]URL',
        action=RepoAction, dest='repos',
        help=_('add the repo at URL (@URL for mirrorlist)'))
    net.add_argument('--instrepo', metavar='[@]URL', type=str,
        help=_('get upgrader boot images from the given URL (default: auto)'))
    net.add_argument('--instrepokey', metavar='GPGKEY', type=gpgkeyfile,
        help=_('use this GPG key to verify upgrader boot images'))
    net.add_argument('--noverifyssl', action='store_true', default=False,
        help=_('do not verify the SSL certificate for HTTPS connections'))
    p.set_defaults(repos=[])

    if not gui:
        clean = p.add_argument_group(_('cleanup commands'))

        clean.add_argument('--resetbootloader', action='store_const',
            dest='clean', const='bootloader', default=None,
            help=_('remove any modifications made to bootloader'))
        clean.add_argument('--clean', action='store_const', const='all',
            help=_('clean up everything written by %s') % __package__)
        p.add_argument('--expire-cache', action='store_true', default=False,
            help=argparse.SUPPRESS)
        p.add_argument('--clean-metadata', action='store_true', default=False,
            help=argparse.SUPPRESS)

    args = p.parse_args()

    if args.clean:
        args.resetbootloader = True
        return args

    if not (gui or args.network or args.device or args.iso or args.clean):
        p.error(_('SOURCE is required (--network, --device, --iso)'))

    # handle --instrepo URL (default is to interpret it as a repoid)
    if args.instrepo and '://' in args.instrepo:
        args.repos.append(('add', 'instrepo=%s' % args.instrepo))
        args.instrepo = 'instrepo'

    if args.instrepo and args.instrepokey:
        args.repos.append(('gpgkey', 'instrepo=%s' % args.instrepokey))

    # Fedora.next: upgrades to F21 require --product
    if legacy_fedora:
        if args.product is None:
            # fail early if we can detect that you need a product
            if int(version) == 20 or args.network in ('21', '22', 'rawhide'):
                p.error(fedora_next_error)
        elif args.product == 'nonproduct':
            args.add_install.append('fedora-release-nonproduct')
        else:
            args.add_install.append('@^%s-product-environment' % args.product)

    # save this so we can check it later
    args.legacy_fedora = legacy_fedora

    return args

class RepoAction(argparse.Action):
    '''Hold a list of repo actions so we can apply them in the order given.'''
    def __call__(self, parser, namespace, value, opt=None):
        curval = getattr(namespace, self.dest, [])
        action = ''
        if opt.startswith('--enable'):
            action = 'enable'
        elif opt.startswith('--disable'):
            action = 'disable'
        elif opt.startswith('--addrepo'):
            action = 'add'
            # validate the argument
            repoid, eq, url = value.partition("=")
            if not (repoid and eq and "://" in url):
                raise argparse.ArgumentError(self,
                                        _("value should be REPOID=[@]URL"))
        curval.append((action, value))
        setattr(namespace, self.dest, curval)

# check the argument to '--device' to see if it refers to install media
def device_or_mnt(arg):
    if arg == 'auto':
        localmedia = media.find()
    else:
        # Canonicalize the device or mountpoint argument
        arg = os.path.realpath(arg)

        localmedia = [m for m in media.find() if arg in (m.dev, m.mnt)]

    if len(localmedia) == 1:
        return localmedia.pop()

    if not localmedia:
        msg = _("no install media found - please mount install media first")
        if arg != 'auto':
            msg = "%s: %s" % (arg, msg)
    else:
        devs = ", ".join(m.dev for m in localmedia)
        msg = _("multiple devices found. please choose one of (%s)") % devs
    raise argparse.ArgumentTypeError(msg)

# check the argument to '--iso' to make sure it's somewhere we can use it
def isofile(arg):
    if not os.path.exists(arg):
        raise argparse.ArgumentTypeError(_("File not found: %s") % arg)
    if not os.path.isfile(arg):
        raise argparse.ArgumentTypeError(_("Not a regular file: %s") % arg)
    if not media.isiso(arg):
        raise argparse.ArgumentTypeError(_("Not an ISO 9660 image: %s") % arg)
    if any(media.fileondev(arg, d.dev) for d in media.removable()):
        raise argparse.ArgumentTypeError(_("ISO image on removable media\n"
            "Sorry, but this isn't supported yet.\n"
            "Copy the image to your hard drive or burn it to a disk."))
    return arg

# validate a GPGKEY argument and return a URI ('file:///...')
def gpgkeyfile(arg):
    if arg.startswith('file://'):
        arg = arg[7:]
    gpghead = '-----BEGIN PGP PUBLIC KEY BLOCK-----'
    try:
        with open(arg) as keyfile:
            keyhead = keyfile.read(len(gpghead))
    except (IOError, OSError) as e:
        raise argparse.ArgumentTypeError(e.strerror)
    if keyhead != gpghead:
        raise argparse.ArgumentTypeError(_("File is not a GPG key"))
    return 'file://' + os.path.abspath(arg)

def VERSION(arg):
    if arg.lower() == 'rawhide':
        return 'rawhide'

    _distros=('fedora', 'redhat', 'centos')
    distro, version, id = platform.linux_distribution(supported_dists=_distros)

    try:
        floatver = float(version)
    except ValueError:
        raise argparse.ArgumentTypeError(_("can't determine system version?"))
    if float(arg) <= floatver:
        raise argparse.ArgumentTypeError(_("version must be higher than %s")
                                         % version)
    return arg

def do_cleanup(args):
    if not args.skipbootloader:
        print "resetting bootloader config"
        reset_boot()
    if args.clean == 'bootloader':
        return
    if not args.skipkernel:
        print "removing boot images"
        remove_boot()
    if not args.skippkgs:
        print "removing downloaded packages"
        remove_cache()
    print "removing miscellaneous files"
    misc_cleanup()

def device_setup(args):
    # treat --device like --addrepo REPO=file://$MOUNTPOINT
    if args.device:
        args.repos.append(('add', 'upgradedevice=file://%s' % args.device.mnt))
        if not args.instrepo:
            args.instrepo = 'upgradedevice'
    elif args.iso:
        try:
            args.device = media.loopmount(args.iso)
        except media.CalledProcessError as e:
            log.info("mount failure: %s", e.output)
            return
        else:
            args.repos.append(('add', 'upgradeiso=file://%s' % args.device.mnt))
            if not args.instrepo:
                args.instrepo = 'upgradeiso'
    return args.device.mnt

# special Fedora-21-specific error message
fedora_next_error = '\n' + _('''
This installation of Fedora does not belong to a product, so you
must provide the --product=PRODUCTNAME option to specify what product
you want to upgrade to. PRODUCTNAME should be one of:

 workstation: the default Fedora experience for laptops and desktops,
   powered by GNOME.
 server: the default Fedora experience for servers
 cloud: a base image for use on public and private clouds
 nonproduct: choose this if none of the above apply; in particular,
   choose this if you are using an alternate-desktop spin of Fedora

Selecting a product will also install its standard package-set in
addition to upgrading the packages already on your system. If you
prefer to maintain your current set of packages, select 'nonproduct'.

See https://fedoraproject.org/wiki/Upgrading for more information.
''')
