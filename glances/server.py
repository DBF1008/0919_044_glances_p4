#
# This file is part of Glances.
#
# SPDX-FileCopyrightText: 2022 Nicolas Hennion <nicolas@nicolargo.com>
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""Manage the Glances server."""

import fnmatch
import json
import socket
import sys
import threading
from base64 import b64decode

from defusedxml import xmlrpc

from glances import __version__, flush_stats_exports
from glances.logger import logger
from glances.processes import glances_processes
from glances.servers_list_dynamic import GlancesAutoDiscoverClient
from glances.stats_server import GlancesStatsServer
from glances.timer import Timer

# Correct issue #1025 by monkey path the xmlrpc lib
xmlrpc.monkey_patch()


class GlancesXMLRPCHandler(xmlrpc.xmlrpc_server.SimpleXMLRPCRequestHandler):
    """Main XML-RPC handler."""

    rpc_paths = ('/RPC2',)

    def setup(self):
        # Track the connection for the graceful shutdown drain phase
        super().setup()
        self.server.connection_opened()

    def finish(self):
        try:
            super().finish()
        finally:
            self.server.connection_closed()

    def end_headers(self):
        # Hack to add a specific header
        # Thk to: https://gist.github.com/rca/4063325
        self.send_my_headers()
        super().end_headers()

    def send_my_headers(self):
        # CORS Access-Control-Allow-Origin (issue #227, GHSA-87qc-fj39-wccr).
        # Per-request reflection against the configured allowlist:
        #   - "*" in the allowlist (default)  -> echo "*"
        #   - request Origin in the allowlist -> reflect it + Vary: Origin
        #   - otherwise                        -> omit ACAO entirely so browsers
        #     block cross-origin JS reads. The XML-RPC call itself still runs;
        #     non-browser clients (curl, custom XML-RPC libs) are unaffected.
        cors_origins = getattr(self.server, 'cors_origins', ['*'])
        if '*' in cors_origins:
            self.send_header("Access-Control-Allow-Origin", "*")
            return
        request_origin = self.headers.get('Origin', '')
        if request_origin and request_origin in cors_origins:
            self.send_header("Access-Control-Allow-Origin", request_origin)
            self.send_header("Vary", "Origin")

    def authenticate(self, headers):
        # auth = headers.get('Authorization')
        try:
            (basic, _, encoded) = headers.get('Authorization').partition(' ')
        except Exception:
            # Client did not ask for authentication
            # If server need it then exit
            return not self.server.isAuth
        else:
            # Client authentication
            (basic, _, encoded) = headers.get('Authorization').partition(' ')
            assert basic == 'Basic', 'Only basic authentication supported'
            # Encoded portion of the header is a string
            # Need to convert to byte-string
            encoded_byte_string = encoded.encode()
            # Decode base64 byte string to a decoded byte string
            decoded_bytes = b64decode(encoded_byte_string)
            # Convert from byte string to a regular string
            decoded_string = decoded_bytes.decode()
            # Get the username and password from the string
            (username, _, password) = decoded_string.partition(':')
            # Check that username and password match internal global dictionary
            return self.check_user(username, password)

    def check_user(self, username, password):
        # Check username and password in the dictionary
        if username in self.server.user_dict:
            from glances.password import GlancesPassword

            # TODO: config is not taken into account: it may be a problem ?
            pwd = GlancesPassword(username=username, config=None)
            return pwd.check_password(self.server.user_dict[username], password)
        return False

    def parse_request(self):
        if not xmlrpc.xmlrpc_server.SimpleXMLRPCRequestHandler.parse_request(self):
            return False
        # Host header validation (DNS rebinding protection, GHSA-w856-8p3r-p338).
        # Runs before authentication so a spoofed Host is rejected regardless of
        # credentials — avoids leaking valid hostnames via auth-error differentials.
        allowed_hosts = getattr(self.server, 'allowed_hosts', None)
        if allowed_hosts:
            host = self.headers.get('Host', '').split(':')[0]
            if not any(fnmatch.fnmatchcase(host, pat) for pat in allowed_hosts):
                self.send_error(400, 'Bad Request: invalid Host header')
                return False
        if self.authenticate(self.headers):
            return True
        # if authentication fails, tell the client
        self.send_error(401, 'Authentication failed')
        return False

    def log_message(self, log_format, *args):
        # No message displayed on the server side
        pass


class GlancesXMLRPCServer(xmlrpc.xmlrpc_server.SimpleXMLRPCServer):
    """Init a SimpleXMLRPCServer instance (IPv6-ready)."""

    finished = False

    def __init__(self, bind_address, bind_port=61209, requestHandler=GlancesXMLRPCHandler, config=None):
        self.bind_address = bind_address
        self.bind_port = bind_port
        self.config = config

        # In-flight connections tracking (used by the graceful shutdown)
        self._active_connections = 0
        self._connections_lock = threading.Lock()

        # CORS origins (GHSA-87qc-fj39-wccr / CVE-2026-46608).
        # Stored as a list; the handler reflects the request Origin against it
        # per request. Default ["*"] preserves backward compatibility for the
        # majority of users who run on a private network without configuration.
        if config is not None:
            self.cors_origins = config.get_list_value('outputs', 'cors_origins', default=["*"])
        else:
            self.cors_origins = ["*"]

        # DNS rebinding protection (GHSA-w856-8p3r-p338 / CVE-2026-46611).
        # When xmlrpc_allowed_hosts is set, the handler rejects requests whose
        # Host header does not match any of the listed patterns. Default is
        # None (permissive) for backward compatibility — a startup warning is
        # emitted by GlancesServer when the allowlist is empty.
        if config is not None:
            self.allowed_hosts = config.get_list_value('outputs', 'xmlrpc_allowed_hosts', default=None)
        else:
            self.allowed_hosts = None

        try:
            self.address_family = socket.getaddrinfo(bind_address, bind_port)[0][0]
        except OSError as e:
            logger.error(f"Couldn't open socket: {e}")
            sys.exit(1)

        super().__init__((bind_address, bind_port), requestHandler)

        # Poll interval (in seconds) so that serve_forever() can notice a
        # shutdown request even when no request is received
        self.timeout = 1

    def connection_opened(self):
        """Track a new in-flight connection."""
        with self._connections_lock:
            self._active_connections += 1

    def connection_closed(self):
        """Track the end of an in-flight connection."""
        with self._connections_lock:
            self._active_connections = max(0, self._active_connections - 1)

    @property
    def inflight_connections(self):
        """Return the number of connections currently being served."""
        with self._connections_lock:
            return self._active_connections

    def shutdown(self):
        """Stop accepting new requests (graceful shutdown phase 1).

        The serve_forever loop will return once the request currently
        being processed (if any) is finished.
        """
        self.finished = True

    def end(self):
        """Stop the server"""
        self.shutdown()
        self.server_close()

    def serve_forever(self):
        """Main loop"""
        while not self.finished:
            self.handle_request()


class GlancesInstance:
    """All the methods of this class are published as XML-RPC methods."""

    def __init__(self, config=None, args=None):
        # Init stats
        self.stats = GlancesStatsServer(config=config, args=args)

        # Initial update
        self.stats.update()

        # cached_time is the minimum time interval between stats updates
        # i.e. XML/RPC calls will not retrieve updated info until the time
        # since last update is passed (will retrieve old cached info instead)
        self.timer = Timer(0)
        self.cached_time = args.cached_time

    def __update__(self):
        # Never update more than 1 time per cached_time
        if self.timer.finished():
            self.stats.update()
            self.timer = Timer(self.cached_time)

    def init(self):
        # Return the Glances version
        return __version__

    def getAll(self):
        # Update and return all the stats
        self.__update__()
        return json.dumps(self.stats.getAll())

    def getAllPlugins(self):
        # Return the plugins list
        return json.dumps(self.stats.getPluginsList())

    def getAllLimits(self):
        # Return all the plugins limits
        return json.dumps(self.stats.getAllLimitsAsDict())

    def getAllViews(self):
        # Return all the plugins views
        return json.dumps(self.stats.getAllViewsAsDict())

    def getPlugin(self, plugin):
        # Update and return the plugin stat
        self.__update__()
        return json.dumps(self.stats.get_plugin(plugin).get_raw())

    def getPluginView(self, plugin):
        # Update and return the plugin view
        return json.dumps(self.stats.get_plugin(plugin).get_views())


class GlancesServer:
    """This class creates and manages the TCP server."""

    def __init__(self, requestHandler=GlancesXMLRPCHandler, config=None, args=None):
        # Args
        self.args = args

        # Graceful shutdown flags
        self._ended = False
        self._stats_closed = False

        # Set the args for the glances_processes instance
        glances_processes.set_args(args)

        # Init the XML RPC server
        try:
            self.server = GlancesXMLRPCServer(args.bind_address, args.port, requestHandler, config=config)
        except Exception as e:
            logger.critical(f"Cannot start Glances server: {e}")
            sys.exit(2)
        else:
            print(f'Glances XML-RPC server is running on {args.bind_address}:{args.port}')

        # The users dict
        # username / password couple
        # By default, no auth is needed
        self.server.user_dict = {}
        self.server.isAuth = False

        # Warn if running unauthenticated with wildcard CORS
        if args.password == "" and "*" in self.server.cors_origins:
            print(
                "WARNING: XML-RPC server is running without authentication and with CORS Allow-Origin: *.\n"
                "         Mitigations: set a password (-P/--password) and/or restrict\n"
                "         cors_origins in glances.conf [outputs] section."
            )
            logger.warning(
                "XML-RPC server is running without authentication and with CORS Access-Control-Allow-Origin: *. "
            )

        # Warn if running without Host header validation (DNS rebinding protection)
        if not getattr(self.server, 'allowed_hosts', None):
            print(
                "WARNING: Glances XML-RPC server is running without Host header validation.\n"
                "         DNS rebinding attacks may allow untrusted pages to read the XML-RPC API.\n"
                "         Set xmlrpc_allowed_hosts in glances.conf [outputs] to restrict access:\n"
                "           xmlrpc_allowed_hosts=localhost,127.0.0.1,<your-hostname>"
            )
            logger.warning("XML-RPC server is running without Host header validation (DNS rebinding protection)")

        # Register functions
        self.server.register_introspection_functions()
        self.glances_instance = GlancesInstance(config, args)
        self.server.register_instance(self.glances_instance)

        if not self.args.disable_autodiscover:
            # Note: The Zeroconf service name will be based on the hostname
            # Correct issue: Zeroconf problem with zeroconf service name #889
            logger.info('Autodiscover is enabled with service name {}'.format(socket.gethostname().split('.', 1)[0]))
            self.autodiscover_client = GlancesAutoDiscoverClient(socket.gethostname().split('.', 1)[0], args)
        else:
            logger.info("Glances autodiscover announce is disabled")

    def add_user(self, username, password):
        """Add an user to the dictionary."""
        self.server.user_dict[username] = password
        self.server.isAuth = True

    def register_shutdown_hooks(self, manager):
        """Register the graceful shutdown hooks in the GlancesShutdownManager."""
        # Phase 1: stop accepting new XML-RPC requests
        manager.register_stop_accepting(self.server.shutdown)
        # Phase 2: wait for the in-flight XML-RPC connections to drain
        manager.register_inflight_check(lambda: self.server.inflight_connections)
        # Phase 3: flush the export buffers (do not lose the last batch)
        manager.register_flusher(self.flush_exports)
        # Phase 4: close the export/storage connections
        manager.register_closer(self.close)

    def flush_exports(self):
        """Synchronously flush a final stats batch to all the export modules."""
        if self._stats_closed:
            return
        flush_stats_exports(self.glances_instance.stats)

    def close(self):
        """Close the stats (export connections and plugins). Idempotent."""
        if not self._stats_closed:
            self._stats_closed = True
            self.glances_instance.stats.end()

    def serve_forever(self):
        """Call the main loop."""
        # Set the server login/password (if -P/--password tag)
        if self.args.password != "":
            self.add_user(self.args.username, self.args.password)
        # Serve forever
        self.server.serve_forever()

    def end(self):
        """End of the Glances server session."""
        if self._ended:
            return
        self._ended = True
        if not self.args.disable_autodiscover:
            self.autodiscover_client.close()
        self.server.end()
        self.close()
