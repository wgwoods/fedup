#!/usr/bin/python
#
# fedup-gtk - GTK+ frontend for fedup, the Fedora Upgrader
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
#
# TODO:
# - Do device_setup()
# - Actually set up yum object and, like, get packages
# - Wire up progress

import os
import signal

from fedup import _
from fedup.commandline import parse_args, do_cleanup

from gi.repository import GObject
GObject.threads_init()
from gi.repository import Gtk, GLib, Gio, Soup

from fedup.media import mounts, isblock, isloop, iscd
from fedup.treeinfo import Treeinfo, TreeinfoError

import logging, fedup.logutils
log = logging.getLogger("fedup.gtk")
fedup.logutils.consolelog(level=logging.INFO)

srcicon = dict(net="network-server-symbolic",
               dvd="media-optical-symbolic",
               usb="media-removable-symbolic",
               default="drive-removable-media-symbolic")

from collections import namedtuple
FedupSourceBase = namedtuple('FedupSource', 'id icon label available')

# FIXME DUMB
testing = True

class FedupSource(FedupSourceBase):
    def __new__(cls, id, icon=None, label=None, available=True):
        if icon not in srcicon.values():
            icon = srcicon.get(icon) or srcicon.get('default')
        if label is None:
            label = id
        return FedupSourceBase.__new__(cls, id, icon, label, available)

# FIXME!!
def mirrorlist(ver):
    return "https://mirrors.fedoraproject.org/metalink?repo=fedora-install-%i&arch=x86_64" % ver

class FedupUI(object):
    def __init__(self, args):
        self.args = args
        self.builder = Gtk.Builder()
        self.builder.add_from_file(self.findUIFile("fedup.glade"))
        self.builder.connect_signals(self)
        self.window = self.builder.get_object("assistant")
        self.searchdialog = self.builder.get_object("searchdialog")
        self.soup = None
        self.did_srclist = False
        self.network = None

        self.srclist = self.builder.get_object("srcliststore")
        self.missingsrc = list(self.srclist_objs)

    @property
    def srclist_objs(self):
        for row in self.srclist:
            yield FedupSource._make(row)

    def findUIFile(self, filename):
        path = os.environ.get("UIPATH", ".:ui:/usr/share/fedup")
        for ui_dir in path.split(":"):
            uifile = os.path.normpath(os.path.join(ui_dir, filename))
            if os.path.isfile(uifile) and os.access(uifile, os.R_OK):
                return uifile
        raise IOError("Can't find UI file '%s'" % filename)

    def run(self):
        self.window.show_all()
        Gtk.main()

    def cancel(self, *args):
        Gtk.main_quit()

    def close(self, *args):
        Gtk.main_quit()

    def prepare(self, asst, *args):
        pageid = asst.get_current_page()
        page = asst.get_nth_page(asst.get_current_page())
        log.info("preparing page #%s", pageid)
        if pageid == 1 and not self.did_srclist:
            self.populate_srclist()
            # FIXME: monitor for new devices
            # FIXME: monitor for network up
        if pageid == 2:
            if testing:
                GLib.timeout_add_seconds(3, self.reposetup_finished, page, True)

    def reposetup_finished(self, page, ok):
        progspin = self.builder.get_object("repoprogspin")
        progspin.stop()
        progbox = self.builder.get_object("repoprogbox")
        progbox.hide()
        self.window.set_page_complete(page, ok)

    def populate_srclist(self):
        log.info("showing search dialog")
        self.searchdialog.show_all()
        # Start with a list that contains two placeholder items
        self.srclist.clear()
        self.srclist.append(self.missingsrc[0])
        self.srclist.append(self.missingsrc[1])
        self.to_check = list()

        self.checkmirror(18) # FIXME: system_version()+1

        for m in mounts():
            if isblock(m.dev) and m.mnt not in ("/", "/boot", "/boot/efi"):
                self.checkdev(m)

    def checkmirror(self, ver):
        self.checkuri(mirrorlist(ver),
                      callback=self.checkmirror_cb, version=ver)

    def checkuri(self, uri, callback, **kwargs):
        if not self.soup:
            self.soup = Soup.SessionAsync()
        msg = Soup.Message.new("GET", uri)
        self.to_check.append(uri)
        kwargs['uri'] = uri
        self.soup.queue_message(msg, callback, kwargs)

    def checkmirror_cb(self, sess, msg, kwargs):
        host = msg.props.uri.host
        uri = kwargs['uri']
        if self.network is None and msg.props.status_code == 200:
            self.network = True

        respdata = msg.props.response_body.data
        if respdata and "</url>" in respdata:
            log.info("checkmirror for %s succeeded", uri)
            self.srclist.insert(0, FedupSource(uri, icon='net', label=host))
            # Success - try the next mirror
            self.checkmirror(kwargs['version']+1)
        else:
            log.info("checkmirror for %s failed", uri)

        self.to_check.remove(uri)
        if not self.to_check:
            self.populate_srclist_finished()

    def checkdev(self, mnt):
        f = Gio.file_new_for_path(os.path.join(mnt.mnt, ".treeinfo"))
        self.to_check.append(mnt)
        f.load_contents_async(None, self.checkdev_cb, mnt)

    def checkdev_cb(self, f, result, mnt):
        treeinfo = Treeinfo()

        try:
            tdata = f.load_contents_finish(result)
            treeinfo.read_str(tdata)
            treeinfo.checkvalues()
        except GLib.GError as e:
            log.info("can't read .treeinfo in %s: %s", mnt.mnt, str(e))
        except TreeinfoError as e:
            log.info("invalid .treeinfo in %s: %s", mnt.mnt, str(e))
        else:
            log.info("%s is install media", mnt.dev)
            # figure out what kind of device it is
            if isloop(mnt.dev):
                devtype = 'loop'
            if iscd(mnt.dev):
                devtype = 'dvd'
            else:
                devtype = 'usb'
            # remove placeholder, if it exists
            for row in self.srclist:
                if row[0] == devtype:
                    self.srclist.remove(row.iter)
            # add item to srclist
            self.srclist.insert(0, FedupSource(mnt.dev, icon=devtype))

        # remove the item from the work queue
        self.to_check.remove(mnt)
        if not self.to_check:
            self.populate_srclist_finished()

    def populate_srclist_finished(self):
        log.info("finished populating srclist. rows: %i", len(self.srclist))
        for row in self.srclist:
            log.info(", ".join(str(i) for i in row[:]))

        # TODO: handle this in a property or with a signal or something
        if self.network:
            updates = self.builder.get_object("updatesbox")
            updates.props.sensitive = True

        self.searchdialog.hide()

        if any(row.available for row in self.srclist_objs):
            self.did_srclist = True
            combo = self.builder.get_object("sourcecombo")
            combo.set_active(0)
        else:
            log.info("no valid instrepos found")
            nosrc = self.builder.get_object("nosourcedialog")
            nosrc.show_all()

    def nosource_response(self, w, responseid):
        if responseid == 0:
            self.close()
        elif responseid == 1:
            w.hide()
            self.populate_srclist()

    def disable_delete(self, w, event):
        return True # ignore delete signal from Esc keypress

    def sourcecombo_changed(self, w, *args):
        idx = w.get_active()
        if idx < 0:
            return
        src = FedupSource._make(self.srclist[idx])
        page = self.builder.get_object("sourcebox")
        self.window.set_page_complete(page, src.available)


if __name__ == '__main__':
    args = parse_args(gui=True)
    try:
        # Make ^C work. See https://bugzilla.gnome.org/show_bug.cgi?id=622084
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        ui = FedupUI(args)
        ui.run()
    except IOError as e:
        print e
        raise SystemExit(1)
    except KeyboardInterrupt:
        print "KeyboardInterrupt"
        raise SystemExit(2)
