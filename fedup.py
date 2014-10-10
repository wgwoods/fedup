#!/usr/bin/python
#
# fedup.py - commandline frontend for fedup, the Fedora Upgrader.
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

import os, sys, time, atexit
from subprocess import call

from fedup.download import UpgradeDownloader, YumBaseError, yum_plugin_for_exc
from fedup.sysprep import prep_upgrade, prep_boot, setup_media_mount
from fedup.upgrade import RPMUpgrade, TransactionError

from fedup.commandline import parse_args, do_cleanup, device_setup
from fedup import textoutput as output

import fedup.logutils as logutils
import fedup.media as media

import logging
log = logging.getLogger("fedup")
def message(m):
    print m
    log.info(m)

from fedup import _, kernelpath, initrdpath
from fedup import version as fedupversion

def setup_downloader(version, instrepo=None, cacheonly=False, repos=[],
                     enable_plugins=[], disable_plugins=[]):
    log.debug("setup_downloader(version=%s, repos=%s)", version, repos)
    f = UpgradeDownloader(version=version, cacheonly=cacheonly)
    f.preconf.enabled_plugins += enable_plugins
    f.preconf.disabled_plugins += disable_plugins
    f.instrepoid = instrepo
    repo_cb = output.RepoCallback()
    repo_prog = output.RepoProgress(fo=sys.stdout)
    multi_prog = output.RepoMultiProgress(fo=sys.stdout)
    disabled_repos = f.setup_repos(callback=repo_cb,
                                   progressbar=repo_prog,
                                   multi_progressbar=multi_prog,
                                   repos=repos)
    disabled_repos = filter(lambda id: id != f.instrepoid, disabled_repos)
    if disabled_repos:
        print _("No upgrade available for the following repos") + ": " + \
                " ".join(disabled_repos)
        log.info("disabled repos: " + " ".join(disabled_repos))
    return f

def install_product_group(product, f):
    product_groups = {
        'workstation': '@^workstation-product-environment',
        'server':      '@^server-product-environment',
        'cloud':       '@^cloud-server-environment',
        'nonproduct':  '@fedora-release-standard',
    }

    f.install_group(product_groups[product], callback=output.DepsolveCallback(f))

def download_packages(f):
    updates = f.build_update_transaction(callback=output.DepsolveCallback(f))
    # check for empty upgrade transaction
    if not updates:
        print _('Your system is already upgraded!')
        print _('Finished. Nothing to do.')
        raise SystemExit(0)
    # print dependency problems before we start the upgrade
    # TODO: let users skip this with --force
    transprobs = f.describe_transaction_problems()
    if transprobs:
        print "WARNING: potential problems with upgrade"
        for p in transprobs:
            print "  " + p
    # clean out any unneeded packages from the cache
    f.clean_cache(keepfiles=(p.localPkg() for p in updates))
    # download packages
    f.download_packages(updates, callback=output.DownloadCallback())

    return updates

def transaction_test(pkgs):
    print _("testing upgrade transaction")
    pkgfiles = set(po.localPkg() for po in pkgs)
    fu = RPMUpgrade()
    probs = fu.setup_transaction(pkgfiles=pkgfiles, check_fatal=False)
    rv = fu.test_transaction(callback=output.TransactionCallback(numpkgs=len(pkgfiles)))
    return (probs, rv)

def reboot():
    call(['systemctl', 'reboot'])

def main(args):
    if args.clean:
        do_cleanup(args)
        return

    if args.device or args.iso:
        mnt = device_setup(args)

        if args.iso:
            if not mnt:
                message('--iso: '+_('Unable to open %s') % args.iso)
                raise SystemExit(2)
            log.debug("iso is %s", os.path.realpath(args.iso))
            def isocleanup():
                log.debug("unmounting %s", args.device.mnt)
                media.umount(args.device.mnt)
                os.rmdir(args.device.mnt)
            atexit.register(isocleanup)

    # Get our packages set up where we can use 'em
    print _("setting up repos...")
    f = setup_downloader(version=args.network,
                         cacheonly=args.cacheonly,
                         instrepo=args.instrepo,
                         repos=args.repos,
                         enable_plugins=args.enable_plugins,
                         disable_plugins=args.disable_plugins)

    if args.nogpgcheck:
        f._override_sigchecks = True

    if args.expire_cache:
        print "expiring cache files"
        f.cleanExpireCache()
        return
    if args.clean_metadata:
        print "cleaning metadata"
        f.cleanMetadata()
        return

    # TODO: error msg generation should be shared between CLI and GUI
    if args.skipkernel:
        message("skipping kernel/initrd download")
    elif f.instrepoid is None or f.instrepoid in f.disabled_repos:
        print _("Error: can't get boot images.")
        if args.instrepo:
            print _("The '%s' repo was rejected by yum as invalid.") % args.instrepo
            if args.iso:
                print _("The given ISO probably isn't an install DVD image.")
            elif args.device:
                print _("The media doesn't contain a valid install DVD image.")
        else:
            print _("The installation repo isn't currently available.")
            print _("Try again later, or specify a repo using --instrepo.")
        raise SystemExit(1)
    else:
        print _("getting boot images...")
        kernel, initrd = f.download_boot_images() # TODO: force arch?

    if args.skippkgs:
        message("skipping package download")
    else:
        print _("setting up update...")
        if len(f.pkgSack) == 0:
            print("no updates available in configured repos!")
            raise SystemExit(1)
        if args.product:
            install_product_group(args.product, f)
        pkgs = download_packages(f)
        # Run a test transaction
        probs, rv = transaction_test(pkgs)


    # And prepare for upgrade
    # TODO: use polkit to get root privs for these things
    print _("setting up system for upgrade")
    if not args.skippkgs:
        prep_upgrade(pkgs)

    if not args.skipbootloader:
        if args.skipkernel:
            print "warning: --skipkernel without --skipbootloader"
            print "using default paths: %s %s" % (kernelpath, initrdpath)
            kernel = kernelpath
            initrd = initrdpath
        prep_boot(kernel, initrd)

    if args.device:
        setup_media_mount(args.device)

    if args.reboot:
        reboot()
    else:
        print _('Finished. Reboot to start upgrade.')

    if args.skippkgs:
        return

    # --- Here's where we summarize potential problems. ---

    # list packages without updates, if any
    missing = sorted(f.find_packages_without_updates(), key=lambda p:p.envra)
    if missing:
        message(_('Packages without updates:'))
        for p in missing:
            message("  %s" % p)

    if not any(p.name in f.conf.kernelpkgnames for p in pkgs):
        message(_('WARNING: no new kernel will be installed during upgrade'))

    # warn if the "important" repos are disabled
    if f.disabled_repos:
        # NOTE: I hate having a hardcoded list of Important Repos here.
        # This information should be provided by the system, somehow..
        important = ("fedora", "updates")
        if any(i in f.disabled_repos for i in important):
            msg = _("WARNING: Some important repos could not be contacted: %s")
        else:
            msg = _("NOTE: Some repos could not be contacted: %s")
        print msg % ", ".join(f.disabled_repos)
        print _("If you start the upgrade now, packages from these repos will not be installed.")

    # warn about broken dependencies etc.
    if probs:
        print
        print _("WARNING: problems were encountered during transaction test:")
        for s in probs.summaries:
            print "  "+s.desc
            for line in s.format_details():
                print "    "+line
        print _("Continue with the upgrade at your own risk.")

if __name__ == '__main__':
    args = parse_args()

    # TODO: use polkit to get privs for modifying bootloader stuff instead
    if os.getuid() != 0:
        print _("you must be root to run this program.")
        raise SystemExit(1)

    # set up logging
    if args.debuglog:
        logutils.debuglog(args.debuglog)
    logutils.consolelog(level=args.loglevel)
    log.info("%s %s starting at %s", sys.argv[0], fedupversion, time.asctime())

    try:
        exittype = "cleanly"
        main(args)
    except KeyboardInterrupt as e:
        print
        log.info("exiting on keyboard interrupt")
        if e.message:
            message(_("Exiting on keyboard interrupt (%s)") % e.message)
        if args.logtraceback:
            log.debug("Traceback (for debugging purposes):", exc_info=True)
        raise SystemExit(1)
    except YumBaseError as e:
        print
        if isinstance(e.value, list):
            err = e.value.pop(0)
            message(_("Downloading failed: %s") % err)
            for p in e.value:
                message("  %s" % p)
        else:
            message(_("Downloading failed: %s") % e)
        log.debug("Traceback (for debugging purposes):", exc_info=True)
        raise SystemExit(2)
    except TransactionError as e:
        print
        message(_("Upgrade test failed with the following problems:"))
        for s in e.summaries:
            message(s)
        log.debug("Detailed transaction problems:")
        for p in e.problems:
            log.debug(p)
        log.error(_("Upgrade test failed."))
        if args.logtraceback:
            log.debug("Traceback (for debugging purposes):", exc_info=True)
        raise SystemExit(3)
    except Exception as e:
        pluginfile = yum_plugin_for_exc()
        if pluginfile:
            plugin, ext = os.path.splitext(os.path.basename(pluginfile))
            log.error(_("The '%s' yum plugin has crashed.") % plugin)
            log.error(_("Please report this problem to the plugin developers:"),
                      exc_info=True)
            raise SystemExit(1)
        log.info("Exception:", exc_info=True)
        exittype = "with unhandled exception"
        raise
    finally:
        log.info("%s exiting %s at %s", sys.argv[0], exittype, time.asctime())
