# fedup.callback - callback functions for the Fedora Upgrader
# vim: set fileencoding=UTF-8:
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

import rpm
import logging
from rpmUtils.miscutils import formatRequire
from yum.callbacks import ProcessTransBaseCallback

# callback objects for RPM transactions

class BaseTsCallback(object):
    '''Basic RPMTransaction Callback class. You need one of these to actually
       make a transaction work.
       If you subclass it, make sure you preserve the behavior of
       inst_open_file and inst_close_file, or nothing will actually happen.'''
    callback_map = dict((rpm.__dict__[k], k[12:].lower())
                         for k in rpm.__dict__
                         if k.startswith('RPMCALLBACK_'))

    def __init__(self):
        self._openfds = dict()
        self.log = logging.getLogger("fedup.rpm")

    def callback(self, what, amount, total, key, data):
        if what not in self.callback_map:
            self.log.info("Ignoring unknown callback number %i", what)
            return
        name = self.callback_map[what]
        #self.log.debug("%s(%s, %s, %s, %s)", name, amount, total, key, data)
        func = getattr(self, name, None)
        if callable(func):
            return func(amount, total, key, data)

    def openfile(self, filename):
        f = open(filename, 'r')
        self._openfds[filename] = f
        return f.fileno()

    def closefile(self, filename, unlink=False):
        f = self._openfds.pop(filename)
        f.close()
        if unlink:
            os.unlink(filename)

    def inst_open_file(self, amount, total, key, data):
        '''Called whenever RPM wants to open a package file.
           The key will be a filename, or txmbr object, or whatever you passed
           as the 'key' argument to ts.addInstall().
           Must return a fd for the now-open file.'''
        raise NotImplementedError

    def inst_close_file(self, amount, total, key, data):
        '''Called when RPM wants to close a previously-opened file.
        'key' will be the same as the 'inst_open_file' call.'''
        raise NotImplementedError

class RPMTsCallback(BaseTsCallback):
    '''Minimal RPM transaction callback that opens/closes files as required'''
    def inst_open_file(self, amount, total, filename, data):
        return self.openfile(filename)

    def inst_close_file(self, amount, total, filename, data):
        self.closefile(filename)

class DownloadCallbackBase(ProcessTransBaseCallback):
    def __init__(self):
        ProcessTransBaseCallback.__init__(self)
        self.log = logging.getLogger("fedup.download")

    # for Yum transaction callbacks (i.e. YumBase.processTransaction stuff)
    def event(self, state, data=None):
        ProcessTransBaseCallback.event(self, state, data)

    # our 'verify' callback in download_packages (yum doesn't have one :/)
    def verify(self, amount, total, filename, data):
        shortname = filename.split('/')[-1]
        self.log.debug("verifying %u/%u %s", amount, total, shortname)

# callback object for depsolving

class DepsolveCallbackBase(object):
    modedict = {
        'i': 'install',
        'u': 'update',
        'e': 'erase',
        'r': 'reinstall',
        'd': 'downgrade',
        'o': 'obsolete',
        'ud': 'updated',
        'od': 'obsoleted',
    }
    def __init__(self, yumobj=None):
        if yumobj:
            self.yum_setup(yumobj)
        self.log = logging.getLogger("fedup.depsolve")
        self.mode_counter = dict((m, 0) for m in self.modedict)
        self.missingreqs = set()
    def yum_setup(self, yumobj):
        pkglist = yumobj.doPackageLists(pkgnarrow='installed')
        self.installed_packages = len(pkglist.installed)
    def start(self):
        self.log.debug('starting depsolve')
    def tscheck(self):
        self.log.debug('running transaction check')
    def restartLoop(self):
        self.log.debug('restarting depsolve')
    def end(self):
        self.log.debug('finished depsolve')
        self.log.debug('%u updates for %u packages',
                      self.mode_counter['u'], self.installed_packages)
        self.log.debug('%s', self.mode_counter)
        self.log.debug("%u missing reqs", len(self.missingreqs))
        for tup in self.missingreqs:
            self.log.debug("missing: %s", formatRequire(*tup))
    def pkgAdded(self, tup, mode):
        (n,a,e,v,r) = tup
        pkg = "%s.%s" % (n,a)
        self.log.debug('added %s.%s for %s', n, a, mode)
        self.mode_counter[mode] += 1
        if mode in ('e', 'i', 'od', 'o'):
            self.log.debug("%s %s", self.modedict[mode], pkg)
    def procReqPo(self, po, formatted_req):
        self.log.debug('req po:   %s → %s', formatted_req, po)
    def procConflictPo(self, name, formatted_conflict):
        self.log.debug('CONFLICT: %s → %s', name, formatted_conflict)
    def unresolved(self, msg):
        self.log.debug('UNRESOLVED DEP: %s', msg)
    def format_missing_requires(self, po, tup):
        req = formatRequire(*tup)
        self.log.debug('MISSING REQ: %s requires %s', po, req)
