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
import struct
import logging
from .callback import BaseTsCallback
from .treeinfo import Treeinfo, TreeinfoError
from .conf import Config
from yum.Errors import YumBaseError
from yum.parser import varReplace
from yum.constants import TS_REMOVE_STATES
from yum.misc import gpgme

enabled_plugins = ['blacklist', 'whiteout']
disabled_plugins = ['rpm-warm-cache', 'remove-with-leaves', 'presto',
                    'auto-update-debuginfo', 'refresh-packagekit']

from . import _
from . import cachedir, upgradeconf, kernelpath, initrdpath, defaultkey
from . import mirrormanager
from .util import listdir, mkdir_p, rm_rf, isxen
from shutil import copy2

log = logging.getLogger(__package__+".yum") # maybe I should rename this..

# TODO: add --urlgrabdebug to enable this... or something
#yum.urlgrabber.grabber.set_logger(logging.getLogger(__package__+".urlgrab"))

def mirrorlist(repo, arch='$basearch'):
    return mirrormanager + '?repo=%s&arch=%s' % (repo, arch)

def raise_exception(failobj):
    raise failobj.exception

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

def init_keyring(gpgdir):
    # set up gpgdir
    if not os.path.isdir(gpgdir):
        log.debug("creating gpgdir %s", gpgdir)
        os.makedirs(gpgdir, 0o700)
    else:
        os.chmod(gpgdir, 0o700)
    os.environ['GNUPGHOME'] = gpgdir

def import_key(keydata, hexkeyid, gpgdir):
    log.debug("importing key %s", hexkeyid.lower())
    yum.misc.import_key_to_pubring(keydata, hexkeyid,
                                   gpgdir=gpgdir, make_ro_copy=False)

def list_keyring(gpgdir):
    return [yum.misc.keyIdToRPMVer(int(k, 16))
            for k in yum.misc.return_keyids_from_pubring(gpgdir)]


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
        # TODO: locking to prevent multiple instances
        self.verbose_logger = log

    def _getConfig(self):
        firstrun = hasattr(self, 'preconf')
        conf = yum.YumBase._getConfig(self)
        if firstrun:
            # override some of yum's defaults
            conf.disable_excludes = ['all']
            conf.cache = self.cacheonly
            conf.deltarpm = 0
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
        r.failovermethod = 'priority'
        r.baseurl = [varReplace(u, self.conf.yumvar) for u in baseurls if u]
        if mirrorlist:
            r.mirrorlist = varReplace(mirrorlist, self.conf.yumvar)
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

        # Add default instrepo (and its key) if needed
        if self.instrepoid is None:
            self.instrepoid = 'default-installrepo'
            # FIXME: hardcoded and Fedora-specific
            mirrorurl = mirrorlist('fedora-install-$releasever')
            repos.append(('add', '%s=@%s' % (self.instrepoid, mirrorurl)))
            repos.append(('gpgkey', '%s=%s' % (self.instrepoid, defaultkey)))

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

        # add GPG keys *after* the repos are created
        for action, repo in repos:
            if action == 'gpgkey':
                (repoid, keyurl) = repo.split('=',1)
                repo = self.repos.getRepo(repoid)
                repo.gpgkey.append(varReplace(keyurl, self.conf.yumvar))
                repo.gpgcheck = True

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

            # Disable gpg key checking for the repos, if requested
            if self._override_sigchecks:
                repo._override_sigchecks = True

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
        if self.extra_pkgs:
            searchlist = ['name']
            self.extra_pkgs = self.extra_pkgs[0].split(',')
            matches = self.searchGenerator(searchlist, self.extra_pkgs)
            for (pkg, matched_value) in matches:
                if pkg.name in self.extra_pkgs:
                    self.install(pkg)
        self.update()
        (rv, msgs) = self.buildTransaction(unfinished_transactions_check=False)
        # NOTE: self.po_with_problems is now a list of (po1, po2, errmsg) tuples
        log.info("buildTransaction returned %i", rv)
        for m in msgs:
            log.info("    %s", m)
        # NOTE: we ignore errors, as anaconda did before us.
        self.dsCallback = None
        return [t.po for t in self.tsInfo.getMembers()
                     if t.ts_state in ("i", "u")]

    def find_packages_without_updates(self):
        '''packages on the local system that aren't being updated/obsoleted'''
        remove = self.tsInfo.getMembersWithState(output_states=TS_REMOVE_STATES)
        return set(p for p in self.rpmdb if p not in remove)

    def describe_transaction_problems(self):
        problems = []

        def find_replacement(po):
            for tx in self.tsInfo.getMembers(po.pkgtup):
                # XXX multiple replacers?
                for otherpo, rel in tx.relatedto:
                    if rel in ('obsoletedby', 'updatedby'):
                        return po, otherpo
                    if rel in ('obsoletes', 'updates'):
                        return otherpo, po
            if po in self.rpmdb:
                return po, None
            else:
                return None, po

        def format_replacement(po):
            oldpkg, newpkg = find_replacement(po)
            if oldpkg and newpkg:
                return "%s (replaced by %s)" % (oldpkg, newpkg)
            elif oldpkg:
                return "%s (no replacement)" % oldpkg
            elif newpkg:
                return "%s (new package)" % newpkg

        done = set()
        for pkg1, pkg2, err in self.po_with_problems:
            if (pkg1,pkg2) not in done:
                problems.append("%s requires %s" % (format_replacement(pkg1),
                                                    format_replacement(pkg2)))
                done.add((pkg1,pkg2))

        return problems

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
        # check signatures of downloaded packages
        if updates:
            self._checkSignatures(updates, callback)

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

    def _get_treeinfo(self):
        mkdir_p(cachedir)
        outfile = os.path.join(cachedir, '.treeinfo')

        if self.cacheonly:
            log.debug("using cached .treeinfo %s", outfile)
            return outfile

        if self.instrepo.gpgcheck and not self._override_sigchecks:
            log.debug("fetching .treeinfo.signed from '%s'", self.instrepoid)
            fn = self.instrepo.grab.urlgrab('.treeinfo.signed',
                                            outfile+'.signed',
                                            reget=None)

            try:
                log.info("verifying .treeinfo.signed")
                # verify file and write plaintext to outfile
                errs = self.check_signed_file(fn, outfile)
            except gpgme.GpgmeError as e:
                raise yum.Errors.YumGPGCheckError(e.strerror)
            if errs:
                raise yum.Errors.YumGPGCheckError(', '.join(errs))
            else:
                log.info(".treeinfo.signed was signed with a trusted key")
            return outfile

        else:
            log.debug("fetching .treeinfo from '%s'", self.instrepoid)
            fn = self.instrepo.grab.urlgrab('.treeinfo', outfile,
                                            reget=None)
            return fn

    @property
    def instrepo(self):
        return self.repos.getRepo(self.instrepoid)

    @property
    def treeinfo(self):
        if self._treeinfo is None:
            self._treeinfo = Treeinfo(self._get_treeinfo())
            log.debug("validating .treeinfo")
            self._treeinfo.checkvalues()
            if self._treeinfo.get("general", "arch") != self.arch.basearch:
                raise TreeinfoError("arch doesn't match system")
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

        # handle special cases for downloading kernel images
        def get_image_arch():
            if isxen():
                return "xen"
            else:
                return self.treeinfo.get("general", "arch")

        # download the images
        try:
            # pick which arch of image(s) to use
            arch = arch or get_image_arch()
            # grab the kernel
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
        except yum.Errors.YumGPGCheckError as e:
            raise YumBaseError(_("could not verify GPG signature: %s") % str(e))
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

    def _checkSignatures(self, pkgs, callback):
        '''check the package signatures and get keys if needed.
           works like YumBase._checkSignatures() except it only uses our
           special automatic _GPGKeyCheck to import untrusted keys.'''
        for po in pkgs:
            result, errmsg = self.sigCheckPkg(po)
            if result == 0:
                continue
            elif result == 1:
                keycheck = lambda info: self._GPGKeyCheck(info, callback)
                self.getKeyForPackage(po, fullaskcb=keycheck)
            else:
                raise yum.Errors.YumGPGCheckError(errmsg)

    def _GPGKeyCheck(self, info, callback=None):
        '''special key importer: import trusted keys automatically'''
        if info['keyurl'].startswith("file://"):
            keyfile = info['keyurl'][7:]
        else:
            return False
        po = info['po']
        log.info("repo '%s' wants to import key %s", po.repoid, keyfile)
        if self.check_keyfile(keyfile):
            log.info("key was installed by signed, trusted package - importing")
            return True
        else:
            log.info("no automatic trust for key %s")
            return False

    def check_keyfile(self, keyfile):
        '''
        If a keyfile was installed by a package that was signed with a trusted
        key (and the key hasn't been modified or tampered with), we can assume
        that the key is trustworthy.

        This is kind of a roundabout way to establish trust between the two
        keys. It'd be a lot more straightforward if we just signed the new
        release key with the old release key - "If you trust this, you can
        trust this too.."
        '''
        if keyfile.startswith('file://'):
            keyfile = keyfile[7:]
        # did the key come from a package?
        keypkgs = self.rpmdb.searchFiles(keyfile)
        log.info("checking keyfile %s", keyfile)
        if keypkgs:
            keypkg = sorted(keypkgs)[-1]
            log.debug("keyfile owned by package %s", keypkg.nevr)
        if not keypkgs:
            log.info("REJECTED: %s does not belong to any package")
            return False

        # was that package signed?
        hdr = keypkg.returnLocalHeader()
        if hdr.sigpgp or hdr.siggpg:
            sigdata = hdr.sigpgp or hdr.siggpg
            siginfo = yum.pgpmsg.decode(sigdata)[0]
            (keyid,) = struct.unpack('>Q', siginfo.key_id())
            hexkeyid = yum.misc.keyIdToRPMVer(keyid)
            log.debug("package was signed with key %s", hexkeyid)
        else:
            log.info("REJECTED: %s was unsigned", keypkg.nevr)
            return False

        # do we trust the key that signed it?
        if yum.misc.keyInstalled(self.ts, keyid, 0) >= 0:
            log.debug("key %s is trusted by rpm", hexkeyid)
        else:
            log.info("REJECTED: key %s is not trusted by rpm", hexkeyid)
            return False

        # has the key been tampered with?
        problems = keypkg.verify([keyfile]).get(keyfile, [])
        if problems:
            log.info("REJECTED: keyfile does not match packaged file (%s)",
                     keyfile, " ".join(p.type for p in problems))
            return False

        # everything checks out OK!
        return True

    def _setup_keyring(self, gpgdir):
        # set up a fresh gpgdir
        rm_rf(gpgdir)
        init_keyring(gpgdir)

        # import trusted keys from rpmdb
        log.debug("checking rpmdb trusted keys")
        pubring_keys = list_keyring(gpgdir)
        for hdr in self.ts.dbMatch('name', 'gpg-pubkey'):
            if hdr.version not in pubring_keys:
                import_key(hdr.description, hdr.version, gpgdir)
            else:
                log.debug("key %s is already in keyring", hdr.version)

        # check instrepo keys to see if they're trustworthy
        log.info("checking GPG keys for instrepo")
        for k in self.instrepo.gpgkey:
            if self.check_keyfile(k):
                keys = self._retrievePublicKey(k) # XXX getSig?
                for info in keys:
                    import_key(info['raw_key'], info['hexkeyid'], gpgdir)

    def check_signed_file(self, signedfile, outfile, gpgdir=cachedir+'/gpgdir'):
        '''
        uses the keys trusted by RPM to verify signedfile.
        writes the resulting plaintext to outfile.
        returns a list of unicode strings describing any errors in verification.
        if the list is empty, the verification was successful.

        It'd be great if RPM could do this for us, since it already has all the
        keys imported and has its own signature verification code, but AFAICT
        it doesn't (at least not in any way reachable from Python), so..
        '''
        if self._override_sigchecks:
            return []

        # set up our own GPG keyring containing all the trusted keys
        self._setup_keyring(gpgdir)

        # verify the signed file, writing plaintext to outfile
        with open(signedfile) as inf, open(outfile, 'w') as outf:
            ctx = gpgme.Context()
            sigresults = ctx.verify(inf, None, outf)
        # return a list of error messages. if it's empty, we're OK.
        # NOTE: this is enough detail for current use cases, but it's very
        # possible we'll want/need to just return sigresults and let the caller
        # sort out the details..
        return [sig.status.message for sig in sigresults if not (
                     sig.summary & gpgme.SIGSUM_VALID and
                     sig.validity >= gpgme.VALIDITY_FULL)]
