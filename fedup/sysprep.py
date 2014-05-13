# sysprep.py - utility functions for system prep
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

import os
from shutil import copy2

from . import _
from . import cachedir, packagedir, packagelist, update_img_dir
from . import upgradeconf, upgradelink, upgraderoot
from .media import write_systemd_unit
from .util import listdir, mkdir_p, rm_f, rm_rf, is_selinux_enabled, kernelver
from .conf import Config
from . import boot

import logging
log = logging.getLogger(__package__+".sysprep")

upgrade_target_requires = "/lib/systemd/system/system-upgrade.target.requires"

def link_pkgs(pkgs):
    '''link the named pkgs into packagedir, overwriting existing files.
       also removes any .rpm files in packagedir that aren't in pkgs.
       finally, write a list of packages to upgrade and a list of dirs
       to clean up after successful upgrade.'''

    log.info("linking required packages into packagedir")
    log.info("packagedir = %s", packagedir)
    mkdir_p(packagedir)

    pkgbasenames = set()
    for pkg in pkgs:
        pkgpath = pkg.localPkg()
        if pkg.remote_url.startswith("file://"):
            pkgbasename = "media/%s" % pkg.relativepath
            pkgbasenames.add(pkgbasename)
            continue
        if not os.path.exists(pkgpath):
            log.warning("%s missing", pkgpath)
            continue
        pkgbasename = os.path.basename(pkgpath)
        pkgbasenames.add(pkgbasename)
        target = os.path.join(packagedir, pkgbasename)
        if os.path.exists(target) and os.lstat(pkgpath) == os.lstat(target):
            log.info("%s already in packagedir", pkgbasename)
            continue
        else:
            if os.path.isdir(target):
                log.info("deleting weirdo directory named %s", pkgbasename)
                rm_rf(target)
            elif os.path.exists(target):
                os.remove(target)
            try:
                os.link(pkgpath, target)
            except OSError as e:
                if e.errno == 18:
                    copy2(pkgpath, target)
                else:
                    raise

    # remove spurious / leftover RPMs
    for f in os.listdir(packagedir):
        if f.endswith(".rpm") and f not in pkgbasenames:
            os.remove(os.path.join(packagedir, f))

    # write packagelist
    with open(packagelist, 'w') as outf:
        outf.writelines(p+'\n' for p in pkgbasenames)

    # write cleanup data
    with Config(upgradeconf) as conf:
        # packagedir should probably be last, since it contains upgradeconf
        cleanupdirs = [cachedir, packagedir]
        conf.set("cleanup", "dirs", ';'.join(cleanupdirs))

def setup_upgradelink():
    log.info("setting up upgrade symlink: %s->%s", upgradelink, packagedir)
    try:
        os.remove(upgradelink)
    except OSError:
        pass
    os.symlink(packagedir, upgradelink)

def setup_media_mount(mnt):
    # make a "media" subdir where all the packages are
    mountpath = os.path.join(upgradelink, "media")
    log.info("setting up mount for %s at %s", mnt.dev, mountpath)
    mkdir_p(mountpath)
    # make a directory to place a unit
    mkdir_p(upgrade_target_requires)
    # make a modified mnt entry that puts it at mountpath
    mediamnt = mnt._replace(rawmnt=mountpath)
    # finally, write out a systemd unit to mount media there
    unit = write_systemd_unit(mediamnt, upgrade_target_requires)
    log.info("wrote %s", unit)

def setup_upgraderoot():
    if os.path.isdir(upgraderoot):
        log.info("upgrade root dir %s already exists", upgraderoot)
        return
    else:
        log.info("creating upgraderoot dir: %s", upgraderoot)
        os.makedirs(upgraderoot, 0755)

def prep_upgrade(pkgs):
    # put packages in packagedir (also writes packagelist)
    link_pkgs(pkgs)
    # make magic symlink
    setup_upgradelink()
    # make dir for upgraderoot
    setup_upgraderoot()

def modify_bootloader(kernel, initrd):
    log.info("adding new boot entry")
    boot.add_entry(kernel, initrd, banner=_("System Upgrade"))

def prep_boot(kernel, initrd):
    # check for systems that need mdadm.conf
    if boot.need_mdadmconf():
        log.info("appending /etc/mdadm.conf to initrd")
        boot.initramfs_append_files(initrd, "/etc/mdadm.conf")

    # check if the system is encrypted and need /etc/cryppttab
    # to be present for systemd to mount it
    if boot.need_crypttab():
        log.info("appending /etc/crypttab to initrd")
        boot.initramfs_append_files(initrd, "/etc/crypttab")

    # look for updates, and add them to initrd if found
    updates = []
    try:
        updates = list(listdir(update_img_dir))
    except (IOError, OSError) as e:
        log.info("can't list update img dir %s: %s", update_img_dir, e.strerror)
    if updates:
        log.info("found updates in %s, appending to initrd", update_img_dir)
        boot.initramfs_append_images(initrd, updates)

    # make a dir in /lib/modules to hold a copy of the new kernel's modules
    # (the initramfs will copy/bind them into place when we reboot)
    kv = kernelver(kernel)
    if kv:
        moddir = os.path.join("/lib/modules", kv)
        log.info("creating module dir %s", moddir)
        mkdir_p(moddir)
    else:
        log.warn("can't determine version of kernel image '%s'", kernel)

    # set up the boot args
    modify_bootloader(kernel, initrd)

def reset_boot():
    '''reset bootloader to previous default and remove our boot entry'''
    conf = Config(upgradeconf)
    kernel = conf.get("boot", "kernel")
    if kernel:
        boot.remove_entry(kernel)

def remove_boot():
    '''remove boot images'''
    conf = Config(upgradeconf)
    kernel = conf.get("boot", "kernel")
    initrd = conf.get("boot", "initrd")
    if kernel:
        rm_f(kernel)
    if initrd:
        rm_f(initrd)

def remove_cache():
    '''remove our cache dirs'''
    conf = Config(upgradeconf)
    cleanup = conf.get("cleanup", "dirs") or ''
    cleanup = cleanup.split(';')
    cleanup += [cachedir, packagedir] # just to be sure
    for d in cleanup:
        log.info("removing %s", d)
        rm_rf(d)

def misc_cleanup():
    log.info("removing symlink %s", upgradelink)
    rm_f(upgradelink)
    for d in (upgraderoot, upgrade_target_requires):
        log.info("removing %s", d)
        rm_rf(d)
