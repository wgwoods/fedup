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
'''
Configuration file parser / writer for .treeinfo files.

The .treeinfo file lives in the top directory of an installable distribution
'tree' and describes the contents - boot images/media, packages, etc.

The file syntax is the normal ConfigParser .ini-style format.

A typical .treeinfo file looks like this:

    [general]
    family = Fedora
    timestamp = 1337720130.41
    variant = Fedora
    version = 17
    packagedir = 
    arch = x86_64

    [stage2]
    mainimage = LiveOS/squashfs.img

    [images-x86_64]
    kernel = images/pxeboot/vmlinuz
    initrd = images/pxeboot/initrd.img
    boot.iso = images/boot.iso

    [images-xen]
    kernel = images/pxeboot/vmlinuz
    initrd = images/pxeboot/initrd.img

    [checksums]
    images/boot.iso = sha256:[long hex string]
    images/macboot.img = sha256:[long hex string]
    images/efiboot.img = sha256:[long hex string]
    images/pxeboot/initrd.img = sha256:[long hex string]
    images/pxeboot/vmlinuz = sha256:[long hex string]
    repodata/repomd.xml = sha256:[long hex string]

(That's the .treeinfo for Fedora 17 for x86_64 if you couldn't guess.)

Some details about the file format:

* [general] section: general info about this distro tree.

  - 'timestamp', 'arch', and 'version' are required.

  - 'timestamp' is the build time, in seconds since the Epoch.
     It may be a floating point number.

  - 'timestamp' should match the timestamp in other .treeinfo files for other
     arches/variants built as part of the same "compose".
     (e.g. Fedora 17 x86_64 and Fedora 17 i386 have the same timestamp.)

  - 'version' is a string, not an integer - "17-Beta" is valid.
  - 'family' is the OS/distro family name, 'variant' is name of the distro
    variant, like "Server" or "Client" or "Workstation".

  - 'packagedir' usually points to the directory containing packages, but in
     Fedora this is ignored in favor of reading repodata/repomd.xml.

  - 'discnum' and 'totaldiscs' can be used when writing a tree to multiple
     CD/DVD images.


* [stage2] section: installer runtime images.

  - 'mainimage' will refer to the main installer runtime image, if one exists.
     In Fedora 17 and later the installer uses this to automatically find
     its runtime if you boot with 'inst.repo=url://to/os/tree'.


* [images-*] sections: list the boot images (kernel, initramfs, boot.iso, etc.)

  - There should be an [images-$arch] section for the arch in [general].
    There may be more sections for other major arches the tree supports.

  - [images-xen], by convention, contains the images that should be used when
    installing into a virtualized guest.

  - Standard keys are 'kernel', 'initrd', and 'boot.iso'.


* [checksums] section: checksums of various files in the tree

  - Everything listed in all the [images-*] sections should have a checksum.
  - Each line is of the form:
    relative/path = hash_algorithm:hex_digest

'''

import ConfigParser
from ConfigParser import RawConfigParser
import hashlib
import time
from os.path import join, normpath
import logging
from StringIO import StringIO
import rpm

# TODO: release this separately so it can be used by other stuff
#       (pungi, libvirt, etc.)
#log = logging.getLogger('treeinfo')
#log.addHandler(logging.NullHandler())
log = logging.getLogger(__package__+".treeinfo")

def hexdigest(filename, algo, blocksize=8192):
    hasher = hashlib.new(algo)
    with open(filename, 'rb') as fobj:
        while True:
            data = fobj.read(blocksize)
            if not data:
                break
            hasher.update(data)
    return hasher.hexdigest()

__all__ = ['Treeinfo', 'TreeinfoError']

# Base class for Treeinfo errors.
TreeinfoError = ConfigParser.Error

class Treeinfo(RawConfigParser):
    '''
    A subclass of RawConfigParser with some extra bits for handling .treeinfo
    files, such as are written by pungi and friends.
    '''
    def __init__(self, fromfile=None, topdir=None):
        '''
        fromfile can be a file-like object (anything with a .readline method)
        or a filename, or a list of filenames.

        topdir specifies the default topdir that any 'relpath' arguments are
        assumed to be relative to (see add_image, add_checksum, etc.)
        '''
        RawConfigParser.__init__(self, allow_no_value=True)
        self._fullpath = dict() # save relpath -> filename mappings here
        if hasattr(fromfile, 'readline'):
            self.readfp(fromfile)
        elif fromfile is not None:
            self.read(fromfile)
        self.topdir = topdir

    def _path(self, relpath, topdir=None):
        if relpath not in self._fullpath:
            if topdir is None:
                topdir = self.topdir or '.'
            self._fullpath[relpath] = normpath(join(topdir, relpath))
        return self._fullpath[relpath]

    def read_str(self, data):
        self.readfp(StringIO(data)) # guh

    def setopt(self, section, option, value):
        '''Sets the given option, creating the section if it doesn't exist'''
        if not self.has_section(section):
            self.add_section(section)
        self.set(section, option, value)

    def get_image(self, arch, imgtype):
        '''return the relative path to the image type for the given arch.
        typical imgtypes are: kernel, initrd, boot.iso'''
        return self.get('images-%s' % arch, imgtype)

    def image_arches(self):
        '''return a list of arches mentioned in 'images-*' sections'''
        return [s[7:] for s in self.sections() if s.startswith('images-')]

    def checkvalues(self):
        '''Check the .treeinfo to make sure it has all required elements.'''
        for f in ('version', 'arch'):
            self.get('general', f)
        # TODO check for checksums for all images

    def checkarch(self):
        '''Check that the .treeinfo correspond to the arch of the system'''
        if not rpm.expandMacro("%_arch") == self.get('general', 'arch'):
            raise TreeinfoError("Incorrect architecture in .treeinfo file")

    def checkfile(self, filename, relpath):
        '''
        Check the given file against the info in [checksum].

        filename must be a full path to the file to be checked.
        relpath is the relative path that was used to fetch the file,
        i.e. the value from the [images-*] section (and the key in the
        [checksums] section)
        '''
        val = self.get('checksums', relpath)
        algo, checksum = val.split(':',1)
        try:
            return checksum == hexdigest(filename, algo)
        except IOError:
            return False

    def add_image(self, arch, imgtype, relpath, topdir=None, algo='sha256'):
        '''
        Add an image to the .treeinfo file: adds an entry to the [images-$arch]
        section with imgtype as the key and relpath as the value.

        If algo is not none, also checksums the file and adds a corresponding
        entry to the [checksums] section, of the form:
          relpath = algo:checksum_in_hex

        If topdir is None, the Treeinfo.topdir value is used.
        '''
        log.debug("adding %s %s: %s", arch, imgtype, relpath)
        section = 'images-%s' % arch
        self.setopt(section, imgtype, relpath)
        if algo:
            self.add_checksum(relpath, topdir, algo)

    def add_checksum(self, relpath, topdir=None, algo='sha256'):
        '''
        Add an item to the [checksums] section for the given file.

        relpath is the path to the file, relative to .treeinfo.
        topdir is the directory that filename is relative to.
        algo is the checksum algorithm.
        '''
        fullpath = self._path(relpath, topdir)
        log.debug("add_checksum(%s)", fullpath)
        self.setopt('checksums', relpath, algo+':'+hexdigest(fullpath, algo))
        log.debug("%s = %s" % (relpath, self.get('checksums',relpath)))

    def add_timestamp(self, timestamp=None):
        '''
        Add a 'timestamp' entry to the [general] section.
        Uses the current time if timestamp is not set.
        '''
        self.setopt('general', 'timestamp', timestamp or time.time())

    def writetreeinfo(self, topdir=None, strict=True, add_timestamp=False):
        if not (topdir or self.topdir):
            raise TypeError("writetreeinfo() requires topdir")
        if strict:
            self.checkvals()
        if add_timestamp:
            self.add_timestamp()
        # TODO sort checksums to end
        with open(self._path('.treeinfo'), 'w') as fp:
            RawConfigParser.write(fp)


# TODO: unit tests
