#
# SchoolTool - common information systems platform for school administration
# Copyright (c) 2003 Shuttleworth Foundation
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
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
#
"""
Schooltool HTTP server.

$Id$
"""

import os
import sys
import errno
import getopt
import libxml2
import logging

import ZConfig
import transaction
from zope.interface import moduleProvides
from zope.app.traversing.api import traverse
from twisted.internet import reactor
from twisted.internet.ssl import DefaultOpenSSLContextFactory
from twisted.python import threadable
import twisted.python.runtime

from schooltool.app import create_application
from schooltool.component import getView
from schooltool.component import FacetManager, getRelatedObjects
from schooltool.interfaces import IModuleSetup
from schooltool.interfaces import AuthenticationError
from schooltool.interfaces import IApplicationObject
from schooltool.common import StreamWrapper, UnicodeAwareException
from schooltool.translation import setCatalog, ugettext, TranslatableString
from schooltool.browser import BrowserRequest
from schooltool.browser.app import RootView
from schooltool.http import Site, SnapshottableDB
from schooltool.uris import URIMembership, URIMember, URIGroup
from schooltool.uris import URICalendarSubscription, URICalendarProvider
from schooltool.uris import URICalendarSubscriber
from schooltool.membership import Membership
from schooltool.component import getOptions, relate
from schooltool.pathconfig import DATADIR



__metaclass__ = type


moduleProvides(IModuleSetup)


#
# Misc
#

def profile(fn, extension='prof'):
    """Profiling hook.

    To profile a function call, wrap it in a call to this function.
    For example, to profile
      self.foo(bar, baz)
    write
      profile(lambda: self.foo(bar, baz))

    The 'extension' argument gives the extension of the filename to use for
    saving the profiling data.
    """
    import hotshot, random, time
    filename = '%s_%03d' % (time.strftime('%DT%T'), random.randint(0, 1000))
    filename = filename.replace('/', '-').replace(':', '-')
    prof = hotshot.Profile('%s.%s' % (filename, extension))
    result = []

    def doit():
        result.append(fn())

    prof.runcall(doit)
    prof.close()
    return result[0]


#
# Main loop
#

_ = TranslatableString  # postpone actual translations

no_storage_error_msg = _("""\
No storage defined in the configuration file.  Unable to start the server.

If you're using the default configuration file, please edit it now and
uncomment one of the ZODB storage sections.""")

incompatible_version_msg = _("""\
Old database format is incompatible with the new SchoolTool version.
Please remove your Data.fs and try again.  Note that you will lose all
the data.""")

usage_msg = _("""\
Usage: %s [options]
Options:

  -c, --config xxx  use this configuration file instead of the default
  -h, --help        show this help message
  -d, --daemon      go to background after starting""")


_ = ugettext        # go back to immediate translations


class ConfigurationError(UnicodeAwareException):
    pass


class SchoolToolError(UnicodeAwareException):
    pass


class Server:
    """SchoolTool HTTP server."""

    # hooks for unit tests
    threadable_hook = threadable
    reactor_hook = reactor
    transaction_hook = transaction
    setCatalog_hook = staticmethod(setCatalog)
    OpenSSLContextFactory_hook = DefaultOpenSSLContextFactory

    def __init__(self, stdout=sys.stdout, stderr=sys.stderr):
        self.stdout = StreamWrapper(stdout)
        self.stderr = StreamWrapper(stderr)
        self.logger = logging.getLogger('schooltool.server')

    def main(self, args):
        """Start the SchoolTool HTTP server.

        args contains command line arguments, usually it is sys.argv[1:].

        Returns zero on normal exit, nonzero on error.  Return value should
        be passed to sys.exit.
        """
        try:
            self.configure(args)
            self.run()
        except ConfigurationError, e:
            print >> self.stderr, u"schooltool: %s" % unicode(e)
            print >> self.stderr, _("run schooltool -h for help")
            return 1
        except SchoolToolError, e:
            print >> self.stderr, unicode(e)
            return 1
        except SystemExit, e:
            return e.args[0]
        else:
            return 0

    def configure(self, args):
        """Process command line arguments and configuration files.

        This is called automatically from run.

        The following attributes define server configuration and are set by
        this method:
          appname       name of the application instance in ZODB
          viewFactory   root view class
          appFactory    application object factory
          config_file   file name of the config file
          config        configuration loaded from a config file, contains the
                        following attributes (see schema.xml for the definitive
                        list):
                            thread_pool_size
                            listen
                            database
                            event_logging
                            test_mode
                            pid_file
                            error_log_file
                            web_access_log_file
                            app_log_file
        """
        # Defaults
        config_file = self.findDefaultConfigFile()
        self.appname = 'schooltool'
        self.viewFactory = getView
        self.appFactory = create_application
        self.daemon = False

        # Process command line arguments
        try:
            opts, args = getopt.getopt(args, 'c:hmd',
                                       ['config=', 'help', 'daemon'])
        except getopt.error, e:
            raise ConfigurationError(str(e))

        for k, v in opts:
            if k in ('-h', '--help'):
                self.help()
                raise SystemExit(0)

        if args:
            raise ConfigurationError(_("too many arguments"))

        # Read configuration file
        for k, v in opts:
            if k in ('-c', '--config'):
                config_file = v
        self.config_file = config_file
        self.config = self.loadConfig(config_file)

        # Check for missing ssl_certificate
        if self.config.ssl_certificate is None and (
                self.config.listen_ssl != [] or self.config.web_ssl != []):
            raise ConfigurationError("ssl_certificate must be specified"
                    " when web_ssl or listen_ssl is used")

        db_configuration = self.config.database
        if db_configuration.config.storage is None:
            self.noStorage()
            raise SystemExit(1)

        # Insert the metadefault for 'modules'
        self.config.module.insert(0, 'schooltool.main')

        # Set up logging
        self.setUpLogger('schooltool.server', self.config.error_log_file,
                         "%(asctime)s %(message)s")
        self.setUpLogger('schooltool.error', self.config.error_log_file,
                         "--\n%(asctime)s\n%(message)s")
        self.setUpLogger('schooltool.rest_access',
                         self.config.rest_access_log_file)
        self.setUpLogger('schooltool.web_access',
                         self.config.web_access_log_file)
        self.setUpLogger('schooltool.app', self.config.app_log_file,
                         "%(asctime)s %(levelname)s %(message)s")

        # Set up the message catalog
        self.setCatalog_hook(self.config.domain, self.config.lang)

        # Shut up ZODB lock_file, because it logs tracebacks when unable
        # to lock the database file, and we don't want that.
        logging.getLogger('ZODB.lock_file').disabled = True

        # ZODB and libxml2 should have a way to complain in case of trouble
        for logger_name in ['ZODB', 'txn', 'libxml2']:
            self.setUpLogger(logger_name, self.config.error_log_file,
                             "%(asctime)s [%(name)s] %(message)s")

        # Process any command line arguments that may override config file
        # settings here.

        for k, v in opts:
            if k in ('-d', '--daemon'):
                if twisted.python.runtime.platformType == 'posix':
                    self.daemon = True
                else:
                    sys.exit(_("Daemon mode is not supported on your"
                               " operating system"))

    def setUpLogger(self, name, filenames, format=None):
        """Set up a named logger.

        Sets up a named logger to log into filenames with the given format.
        Two filenames are special: 'STDOUT' means sys.stdout and 'STDERR'
        means sys.stderr.
        """
        formatter = logging.Formatter(format)
        logger = logging.getLogger(name)
        logger.propagate = False
        logger.setLevel(logging.INFO)
        for filename in filenames:
            if filename == 'STDOUT':
                handler = logging.StreamHandler(self.stdout)
            elif filename == 'STDERR':
                handler = logging.StreamHandler(self.stderr)
            else:
                handler = UnicodeFileHandler(filename)
            handler.setFormatter(formatter)
            logger.addHandler(handler)

    def help(self):
        """Print a help message."""
        progname = os.path.basename(sys.argv[0])
        print >> self.stdout, unicode(usage_msg) % progname

    def noStorage(self):
        """Print an informative message when the config file does not define a
        storage."""
        print >> self.stderr, unicode(no_storage_error_msg)

    def findDefaultConfigFile(self):
        """Return the default config file pathname.

        Looks for a file called 'schooltool.conf' in the directory two levels
        above the location of this module.  In the extracted source archive
        this will be the project root.
        """
        dirname = os.path.dirname(__file__)
        dirname = os.path.normpath(os.path.join(dirname, '..', '..'))
        config_file = os.path.join(dirname, 'schooltool.conf')
        if not os.path.exists(config_file):
            config_file = os.path.join(dirname, 'schooltool.conf.in')
        return config_file

    def loadConfig(self, config_file):
        """Load configuration from a given config file."""
        schema = ZConfig.loadSchema(os.path.join(DATADIR, 'schema.xml'))
        self.notifyConfigFile(config_file)
        config, handler = ZConfig.loadConfig(schema, config_file)
        return config

    def run(self):
        """Start the HTTP server.

        Must be called after configure.
        """
        # Add directories to the pythonpath
        path = self.config.path[:]
        path.reverse()
        for dir in path:
            sys.path.insert(0, dir)

        setUpModules(self.config.module)

        # Log libxml2 complaints
        libxml2.registerErrorHandler(
                lambda logger, error: logger.error(error.strip()),
                logging.getLogger('libxml2'))

        # This must be called here because we use threads
        libxml2.initParser()

        db_configuration = self.config.database
        try:
            self.db = db_configuration.open()
        except IOError, e:
            msg = _("Could not initialize the database:\n  ") + unicode(e)
            if e.errno == errno.EAGAIN: # Resource temporarily unavailable
                msg += "\n"
                msg += _("Perhaps another SchoolTool instance is using it?")
            raise SchoolToolError(msg)
        self.prepareDatabase()

        if self.config.test_mode:
            self.db = SnapshottableDB(self.db)

        self.threadable_hook.init()

        # Setup RESTive interfaces
        site = Site(self.db, self.appname, self.viewFactory, self.authenticate,
                    self.getApplicationLogPath())
        for interface, port in self.config.listen:
            self.reactor_hook.listenTCP(port, site, interface=interface)
            self.notifyServerStarted(interface, port)
        for interface, port in self.config.listen_ssl:
            self.reactor_hook.listenSSL(port, site,
                                        self.OpenSSLContextFactory_hook(
                                            self.config.ssl_certificate,
                                            self.config.ssl_certificate
                                        ),
                                        interface=interface)
            self.notifyServerStarted(interface, port, ssl=True)

        # Setup web interfaces
        site = Site(self.db, self.appname, RootView, self.authenticate,
                    self.getApplicationLogPath(), BrowserRequest)
        for interface, port in self.config.web:
            self.reactor_hook.listenTCP(port, site, interface=interface)
            self.notifyWebServerStarted(interface, port)
        for interface, port in self.config.web_ssl:
            self.reactor_hook.listenSSL(port, site,
                                        self.OpenSSLContextFactory_hook(
                                            self.config.ssl_certificate,
                                            self.config.ssl_certificate
                                        ),
                                        interface=interface)
            self.notifyWebServerStarted(interface, port, ssl=True)


        if self.daemon:
            self.daemonize()

        if self.config.pid_file:
            pidfile = file(self.config.pid_file, "w")
            print >> pidfile, os.getpid()
            pidfile.close()

        # Call suggestThreadPoolSize at the last possible moment, because it
        # will create a number of non-daemon threads and will prevent the
        # application from exitting on errors.
        self.reactor_hook.suggestThreadPoolSize(self.config.thread_pool_size)
        self.reactor_hook.run()

        # Cleanup on signals TERM, INT and BREAK
        self.notifyShutdown()
        if self.config.pid_file:
            os.unlink(self.config.pid_file)

    def daemonize(self):
        """Daemonize with a double fork and close the standard IO."""
        pid = os.fork()
        if pid:
            sys.exit(0)
        os.setsid()
        os.umask(077)

        pid = os.fork()
        if pid:
            self.notifyDaemonized(pid)
            sys.exit(0)

        os.close(0)
        os.close(1)
        os.close(2)
        os.open('/dev/null', os.O_RDWR)
        os.dup(0)
        os.dup(0)

    def prepareDatabase(self):
        """Prepare the database.

        Makes sure the database has an application instance.

        Creates the application if necessary.

        This is the place to perform object schema upgrades, if necessary.
        """
        conn = self.db.open()
        root = conn.root()

        # Create the application if it does not yet exist
        if root.get(self.appname) is None:
            root[self.appname] = self.appFactory()
            self.transaction_hook.commit()
        app = root[self.appname]

        # Database schema change from 0.6 to 0.7
        # TODO: this should really check for version 0.7
        if not hasattr(app, 'ticketService'):
            self.transaction_hook.abort()
            conn.close()
            raise SchoolToolError(unicode(incompatible_version_msg))

        # Database schema change from 0.8 to 0.9
        if hasattr(app['groups'], 'keys'):
            if not 'community' in app['groups'].keys():
                self.notifyUpgrade('0.8', '0.9')
                self.migrate08to09(root)

        # Enable or disable global event logging
        eventlog = app.utilityService['eventlog']
        eventlog.enabled = self.config.event_logging
        self.transaction_hook.commit()

        conn.close()

    def authenticate(context, username, password):
        """See IAuthenticator."""
        try:
            persons = traverse(context, '/persons')
        except TraversalError:
            # Perhaps log somewhere that authentication is not possible in
            # this context, otherwise it might be hard to debug
            raise AuthenticationError(_("Invalid login"))
        try:
            person = persons[username]
        except KeyError:
            pass
        else:
            if person.checkPassword(password):
                return person
        raise AuthenticationError(_("Invalid login"))

    authenticate = staticmethod(authenticate)

    def notifyConfigFile(self, config_file):
        # Note that the following message will be translated to the language
        # specified by the system locale, instead of the language specified
        # in the config file.  The reason for that should be obvious (chicken
        # and egg problem).
        self.logger.info(_("Reading configuration from %s"), config_file)

    def notifyServerStarted(self, network_interface, port, ssl=False):
        if ssl:
            msg = _("Started HTTPS server for RESTive API on %s:%s")
        else:
            msg = _("Started HTTP server for RESTive API on %s:%s")
        self.logger.info(msg, network_interface or "*", port)

    def notifyWebServerStarted(self, network_interface, port, ssl=False):
        if ssl:
            msg = _("Started HTTPS server for web UI on %s:%s")
        else:
            msg = _("Started HTTP server for web UI on %s:%s")
        self.logger.info(msg, network_interface or "*", port)

    def notifyDaemonized(self, pid):
        self.logger.info(_("Going to background, daemon pid %d"), pid)

    def notifyShutdown(self):
        self.logger.info(_("Shutting down"))

    def getApplicationLogPath(self):
        for name in self.config.app_log_file:
            if name not in ('STDOUT', 'STDERR'):
                return name
        return None

    def notifyUpgrade(self, v1, v2):
        self.logger.info(_("Upgrading Data.fs from version %s to version %s"),
                            v1, v2)

    def migrate08to09(self, root):
        """Partial database migration from 0.8 to 0.9

        Create a new application and new IApplicationObjects, copy calendars and
        relationships, copy PersonInfoFacets from old Person objects to new ones.
        """

        def get08RelatedObjects(obj, role):
            return [link.relationship.traverse(link).__parent__ for link \
                    in obj.listLinks(role) if IApplicationObject.providedBy(
                            link.relationship.traverse(link).__parent__)]

        old_app = root[self.appname]
        new_app = self.appFactory()

        old_groups = old_app['groups']
        old_persons = old_app['persons']
        old_resources = old_app['resources']

        new_groups = new_app['groups']
        new_persons = new_app['persons']
        new_resources = new_app['resources']

        # probably shouldn't assume that manager still exists
        old_new_map = {old_groups['root']: new_groups['community'],
                       old_groups['locations']: new_groups['locations'],
                       old_groups['managers']: new_groups['managers'],
                       old_groups['teachers']: new_groups['teachers'],
                       old_groups['pupils']: new_groups['pupils'],
                       old_persons['manager']: new_persons['manager']}

        for group in old_groups.keys():
            if group not in new_groups.keys() and group != 'root':
                g = new_groups.new(old_groups[group].__name__,
                                    title = old_groups[group].title)
                old_new_map[old_groups[group]] = g

        for person in old_persons.keys():
            if person not in new_persons.keys():
                p = new_persons.new(old_persons[person].__name__,
                                    title = old_persons[person].title)
                old_new_map[old_persons[person]] = p

                if old_persons[person].hasPassword():
                    p._pwhash = old_persons[person]._pwhash

        for person in new_persons.keys():
            old_info = FacetManager(old_persons[person]).facetByName(
                                                            'person_info')
            new_info = FacetManager(new_persons[person]).facetByName(
                                                            'person_info')
            new_info.first_name = old_info.first_name
            new_info.last_name = old_info.last_name
            new_info.date_of_birth = old_info.date_of_birth
            new_info.comment = old_info.comment
            new_info.photo = old_info.photo

        for resource in old_resources.keys():
            if resource not in new_resources.keys():
                r = new_resources.new(old_resources[resource].__name__,
                                    title = old_resources[resource].title)
                old_new_map[old_resources[resource]] = r

        for group in old_groups.keys():
            if group in new_groups.keys() and group != 'root':
                for event in old_groups[group].calendar:
                    if event.owner:
                        event = event.replace(
                                        owner = old_new_map[event.owner])
                    if event.context:
                        event = event.replace(
                                        context = old_new_map[event.context])
                    new_groups[group].calendar.addEvent(event)

        for resource in old_resources.keys():
            if resource in new_resources.keys():
                for event in old_resources[resource].calendar:
                    if event.owner:
                        event = event.replace(
                                        owner = old_new_map[event.owner])
                    if event.context:
                        event = event.replace(
                                        context = old_new_map[event.context])
                    new_resources[resource].calendar.addEvent(event)

        for person in old_persons.keys():
            if person in new_persons.keys():
                for event in old_persons[person].calendar:
                    if event.owner:
                        event = event.replace(owner = old_new_map[event.owner])
                    if event.context:
                        event = event.replace(
                                        context = old_new_map[event.context])
                    new_persons[person].calendar.addEvent(event)

        # If the application objects don't match up migration will be
        # unreliable, fail here and recommend removing Data.fs
        if len(old_groups.keys()) - len(new_groups.keys()):
            raise SchoolToolError(unicode(incompatible_version_msg))

        if len(old_persons.keys()) - len(new_persons.keys()):
            raise SchoolToolError(unicode(incompatible_version_msg))

        if len(old_resources.keys()) - len(new_resources.keys()):
            raise SchoolToolError(unicode(incompatible_version_msg))

        for old, new in old_new_map.items():
            for group in get08RelatedObjects(old, URIGroup):
                if old_new_map[group] not in \
                        getRelatedObjects(old_new_map[old], URIGroup):
                    Membership(group=old_new_map[group],
                               member=old_new_map[old])

            for member in get08RelatedObjects(old, URIMember):
                if old_new_map[member] not in \
                        getRelatedObjects(old_new_map[old], URIMember):
                    Membership(group=old_new_map[old],
                               member=old_new_map[member])

            for provider in get08RelatedObjects(old, URICalendarProvider):
                relate(URICalendarSubscription,
                   (old_new_map[old], URICalendarSubscriber),
                   (old_new_map[provider], URICalendarProvider))

        root[self.appname] = new_app

class UnicodeFileHandler(logging.StreamHandler):
    """A handler class which writes records to disk files.

    This class differs from logging.FileHandler in that it can handle Unicode
    strings with graceful degradation.
    """

    def __init__(self, filename):
        stm = StreamWrapper(open(filename, 'a'))
        logging.StreamHandler.__init__(self, stm)

    def close(self):
        self.stream.close()


def setUpModules(module_names):
    """Set up the modules named in the given list."""
    for name in module_names:
        assert isinstance(name, basestring)
        module = __import__(name)
        components = name.split('.')
        for component in components[1:]:
            module = getattr(module, component)
        if IModuleSetup.providedBy(module):
            module.setUp()
        else:
            raise TypeError('Cannot set up module because it does not'
                            ' provide IModuleSetup', module)


def setUp():
    """Set up the SchoolTool application."""
    setUpModules([
        'schooltool.component',
        'schooltool.relationship',
        'schooltool.membership',
        'schooltool.absence',
        'schooltool.rest',
        'schooltool.eventlog',
        'schooltool.uris',
        'schooltool.teaching',
        'schooltool.timetable',
        'schooltool.booking',
        ])


def main():
    """Start the SchoolTool HTTP server."""
    retval = Server().main(sys.argv[1:])
    sys.exit(retval)


if __name__ == '__main__':
    main()
