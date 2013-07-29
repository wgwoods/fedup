# download.py - yum-based download/depsolver for system upgrades
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
import yum
import logging
from .callback import BaseTsCallback
from .treeinfo import Treeinfo, TreeinfoError
from .conf import Config
from yum.Errors import YumBaseError
from yum.parser import varReplace
import urlgrabber

enabled_plugins = ['blacklist', 'whiteout']
disabled_plugins = ['rpm-warm-cache', 'remove-with-leaves', 'presto',
                    'auto-update-debuginfo', 'refresh-packagekit']

from . import _
from . import cachedir, upgradeconf, kernelpath, initrdpath
from . import mirrormanager
from .util import listdir, mkdir_p
from shutil import copy2

log = logging.getLogger(__package__+".yum") # maybe I should rename this..

# TODO: add --urlgrabdebug to enable this... or something
#yum.urlgrabber.grabber.set_logger(logging.getLogger(__package__+".urlgrab"))

def mirrorlist(repo, arch='$basearch'):
    return mirrormanager + '?repo=%s&arch=%s' % (repo, arch)

def raise_exception(failobj):
    e = failobj.exception

    # Catch URLGrabber "HTTP errors" and have it retry:
    if type(e) is urlgrabber.grabber.URLGrabError:
        if e.errno in (12, 14, -1):
            e.errno = -1
            return {}

    log.debug("URLGrabber error: %d: %r" % (e.errno, e))
    raise e

pluginpath = []
def yum_plugin_for_exc():
    import sys, traceback
    tb_files = [i[0] for i in traceback.extract_tb(sys.exc_info()[2])]
    log.debug("checking traceback files: %s", tb_files)
    log.debug("plugin path is %s", pluginpath)
    for f in tb_files:
        for p in pluginpath:
            if f.startswith(p):
                return f
    return None

class UpgradeDownloader(yum.YumBase):
    '''Yum-based downloader class. Based roughly on AnacondaYum.'''
    def __init__(self, version=None, cachedir=cachedir, cacheonly=False):
        # TODO: special handling for version='test' where we just synthesize
        #       a bunch of fake RPMs with interesting properties
        log.info("UpgradeDownloader(version=%s,cachedir=%s)",version,cachedir)
        yum.YumBase.__init__(self)
        self.use_txmbr_in_callback = True
        self.preconf.debuglevel = -1
        self.preconf.enabled_plugins = enabled_plugins
        self.preconf.disabled_plugins = disabled_plugins
        self.version = version
        if version:
            self.preconf.releasever = version
        self.cacheonly = cacheonly
        self.prerepoconf.cachedir = cachedir
        self.prerepoconf.cache = cacheonly
        log.debug("prerepoconf.cache=%i", self.prerepoconf.cache)
        self.instrepoid = None
        self.disabled_repos = []
        self._treeinfo = None
        self.prerepoconf.failure_callback = raise_exception
        self._repoprogressbar = None
        self.retries = 5
        # TODO: locking to prevent multiple instances
        # TODO: override logging objects so we get yum logging

    def _getConfig(self):
        firstrun = hasattr(self, 'preconf')
        conf = yum.YumBase._getConfig(self)
        if firstrun:
            # override some of yum's defaults
            conf.disable_excludes = ['all']
            conf.cache = self.cacheonly
            log.debug("conf.cache=%i", conf.cache)
        return conf

    def doPluginSetup(self, *args, **kwargs):
        yum.YumBase.doPluginSetup(self, *args, **kwargs)
        # Now that plugins have been set up, let's save some info about them
        global pluginpath
        pluginpath = self.plugins.searchpath
        log.info("enabled plugins: %s", self.plugins._plugins.keys())

    def add_repo(self, repoid, baseurls=[], mirrorlist=None, **kwargs):
        '''like add_enable_repo, but doesn't do initial repo setup and doesn't
        make unnecessary changes'''
        r = yum.yumRepo.YumRepository(repoid)
        r.name = repoid
        r.base_persistdir = cachedir
        r.basecachedir = cachedir
        r.cache = self.cacheonly
        r.callback = kwargs.get('callback') or self._repoprogressbar
        r.failure_obj = raise_exception
        r.mirror_failure_obj = raise_exception
        r.baseurl = [varReplace(u, self.conf.yumvar) for u in baseurls if u]
        if mirrorlist:
            r.mirrorlist = varReplace(mirrorlist, self.conf.yumvar)
        r._async = True
        self._repos.add(r)
        self._repos.enableRepo(repoid)

    def setup_repos(self, callback=None, progressbar=None, repos=[]):
        '''Return a list of repos that had problems setting up.'''
        # These will set up progressbar and callback when we actually do setup
        self.prerepoconf.progressbar = progressbar
        self.prerepoconf.callback = callback
        self._repoprogressbar = progressbar

        # TODO invalidate cache if the version doesn't match previous version
        log.info("checking repos")

        # Add default instrepo if needed
        if self.instrepoid is None:
            self.instrepoid = 'default-installrepo'
            # FIXME: hardcoded and Fedora-specific
            mirrorurl = mirrorlist('fedora-install-$releasever')
            repos.append(('add', '%s=@%s' % (self.instrepoid, mirrorurl)))

        # We need to read .repo files before we can enable/disable them, so:
        self.repos # implicit repo setup! ha ha! what fun!

        if self.version is None: # i.e. no --network arg
            self.repos.disableRepo('*')

        # user overrides to enable/disable repos.
        # NOTE: will raise YumBaseError if there are problems
        for action, repo in repos:
            if action == 'enable':
                self.repos.enableRepo(repo)
            elif action == 'disable':
                self.repos.disableRepo(repo)
            elif action == 'add':
                (repoid, url) = repo.split('=',1)
                if url[0] == '@':
                    self.add_repo(repoid, mirrorlist=url[1:])
                else:
                    self.add_repo(repoid, baseurls=[url])
                if self.conf.proxy:
                    repo = self.repos.getRepo(repoid)
                    repo.proxy = self.conf.proxy
                    repo.proxy_username = self.conf.proxy_username
                    repo.proxy_password = self.conf.proxy_password

        # check enabled repos
        for repo in self.repos.listEnabled():
            try:
                md_types = repo.repoXML.fileTypes()
            except yum.Errors.RepoError:
                log.info("can't find valid repo metadata for %s", repo.id)
                repo.disable()
                self.disabled_repos.append(repo.id)
            else:
                log.info("repo %s seems OK" % repo.id)

        log.debug("repos.cache=%i", self.repos.cache)

        return self.disabled_repos

    # XXX currently unused
    def save_repo_configs():
        '''save repo configuration files for later use'''
        repodir = os.path.join(cachedir, 'yum.repos.d')
        mkdir_p(repodir)
        for repo in self.repos.listEnabled():
            repofile = os.path.join(repodir, "%s.repo" % repo.id)
            try:
                repo.write(open(repofile), 'w')
            except IOError as e:
                log.warn("couldn't write repofile for %s: %s", repo.id, str(e))

    # NOTE: could raise RepoError if metadata is missing/busted
    def build_update_transaction(self, callback=None):
        log.info("looking for updates")
        self.dsCallback = callback
        self.update()
        (rv, msgs) = self.buildTransaction(unfinished_transactions_check=False)
        log.info("buildTransaction returned %i", rv)
        for m in msgs:
            log.info("    %s", m)
        # NOTE: we ignore errors, as anaconda did before us.
        self.dsCallback = None
        return [t.po for t in self.tsInfo.getMembers()
                     if t.ts_state in ("i", "u")]

    def download_packages(self, pkgs, callback=None):
        # Verifying a full upgrade payload of ~2000 pkgs takes a good 90-120
        # seconds with no callback. Unacceptable!
        # So: here we have our own verifyPkg loop, with callback.
        # The results get cached, so when yum does it again in the real
        # _downloadPackages function it's a negligible delay.
        localpkgs = [p for p in pkgs if os.path.exists(p.localPkg())]
        total = len(localpkgs)
        # XXX: multithreading?
        for num, p in enumerate(localpkgs, 1):
            local = p.localPkg()
            if hasattr(callback, "verify") and callable(callback.verify):
                callback.verify(num, total, local, None)
            ok = self.verifyPkg(local, p, False) # result will be cached by yum
        log.info("beginning package download...")
        updates = self._downloadPackages(callback)

        # Handle _downloadPackages returning None instead of an empty list
        if updates is None:
            updates = []

        if set(updates) != set(pkgs):
            log.debug("differences between requested pkg set and downloaded:")
            for p in set(pkgs).difference(updates):
                log.debug("  -%s", p)
            for p in set(updates).difference(pkgs):
                log.debug("  +%s", p)
        # TODO check signatures of downloaded packages

    def clean_cache(self, keepfiles):
        log.info("checking for unneeded rpms in cache")
        # Find all the packages in the caches (not on media though)
        localpkgs = set(f for r in self.repos.listEnabled() if not r.mediaid
                          for f in listdir(r.pkgdir) if f.endswith(".rpm"))
        for f in localpkgs.difference(keepfiles):
            try:
                log.debug("removing %s", f)
                os.remove(f)
            except IOError as e:
                log.info("failed to remove %s", f)
        # TODO remove dirs that don't belong to any repo

    @property
    def instrepo(self):
        return self.repos.getRepo(self.instrepoid)

    @property
    def treeinfo(self):
        if self._treeinfo is None:
            mkdir_p(cachedir)
            outfile = os.path.join(cachedir, '.treeinfo')
            if self.cacheonly:
                log.debug("using cached .treeinfo %s", outfile)
                self._treeinfo = Treeinfo(outfile)
            else:
                log.debug("fetching .treeinfo from repo '%s'", self.instrepoid)
                if os.path.exists(outfile):
                    os.remove(outfile)
                fn = self.instrepo.grab.urlgrab('.treeinfo', outfile,
                                                reget=None)
                self._treeinfo = Treeinfo(fn)
                log.debug(".treeinfo saved at %s", fn)
            self._treeinfo.checkvalues()
        return self._treeinfo

    def download_boot_images(self, arch=None):
        # helper function to grab and checksum image files listed in .treeinfo
        def grab_and_check(imgarch, imgtype, outpath):
            relpath = self.treeinfo.get_image(imgarch, imgtype)
            log.debug("grabbing %s %s", imgarch, imgtype)
            log.info("downloading %s to %s", relpath, outpath)
            if self.treeinfo.checkfile(outpath, relpath):
                log.debug("file already exists and checksum OK")
                return outpath
            def checkfile(cb):
                log.debug("checking %s", relpath)
                if not self.treeinfo.checkfile(cb.filename, relpath):
                    log.info("checksum doesn't match - retrying")
                    raise yum.URLGrabError(-1)
            return self.instrepo.grab.urlgrab(relpath, outpath,
                                              checkfunc=checkfile,
                                              reget=None,
                                              copy_local=True)

        # download the images
        try:
            if not arch:
                arch = self.treeinfo.get('general', 'arch')
            kernel = grab_and_check(arch, 'kernel', kernelpath)
            # cache the initrd somewhere so we don't have to fetch it again
            # if it gets modified later.
            cacheinitrd = os.path.join(cachedir, os.path.basename(initrdpath))
            initrd = grab_and_check(arch, 'upgrade', cacheinitrd)
            # copy the downloaded initrd to the target path
            copy2(initrd, initrdpath)
            initrd = initrdpath
        except TreeinfoError as e:
            raise YumBaseError(_("invalid data in .treeinfo: %s") % str(e))
        except yum.URLGrabError as e:
            err = e.strerror
            if e.errno == 256:
                err += "\n" + _("Last error was: %s") % e.errors[-1][1]
            raise YumBaseError(_("couldn't get boot images: %s") % err)
        except KeyboardInterrupt:
            # if an IOError occurs while writing the file to disk, F17
            # urlgrabber actually raises *KeyboardInterrupt* for some reason.
            # But urlgrabber.__version__ hasn't been changed since F12, so:
            if not hasattr(yum.urlgrabber.grabber, 'exception2msg'): # <=F17
                raise KeyboardInterrupt(_("or possible error writing file"))
            else:
                # The exception actually was a KeyBoardInterrupt, re-raise it
                raise

        # Save kernel/initrd info so we can clean it up later
        mkdir_p(os.path.dirname(upgradeconf))
        with Config(upgradeconf) as conf:
            conf.set("boot", "kernel", kernel)
            conf.set("boot", "initrd", initrd)

        return kernel, initrd
