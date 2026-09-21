#
# This file is part of Glances.
#
# SPDX-FileCopyrightText: 2024 Nicolas Hennion <nicolas@nicolargo.com>
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""Glances Restful/API and Web based interface."""

from glances import flush_stats_exports
from glances.globals import WINDOWS
from glances.outputs.glances_restful_api import GlancesRestfulApi
from glances.processes import glances_processes
from glances.stats import GlancesStats


class GlancesWebServer:
    """This class creates and manages the Glances Web server session."""

    def __init__(self, config=None, args=None):
        # Graceful shutdown flags
        self._ended = False
        self._stats_closed = False

        # Init stats
        self.stats = GlancesStats(config, args)

        if not WINDOWS and args.no_kernel_threads:
            # Ignore kernel threads in process list
            glances_processes.disable_kernel_threads()

        # Set the args for the glances_processes instance
        glances_processes.set_args(args)

        # Initial system information update
        self.stats.update()

        # Init the Web server
        self.web = GlancesRestfulApi(config=config, args=args)

    def register_shutdown_hooks(self, manager):
        """Register the graceful shutdown hooks in the GlancesShutdownManager."""
        # Phase 1: stop accepting new HTTP/WebSocket connections
        manager.register_stop_accepting(self.web.initiate_shutdown)
        # Phase 2: wait for the in-flight HTTP requests to drain
        manager.register_inflight_check(self.web.inflight_connections)
        # Phase 3: flush the export buffers (do not lose the last batch)
        manager.register_flusher(self.flush_exports)
        # Phase 4: close the export/storage connections
        manager.register_closer(self.close)

    def flush_exports(self):
        """Synchronously flush a final stats batch to all the export modules."""
        if self._stats_closed:
            return
        flush_stats_exports(self.stats)

    def close(self):
        """Close the stats (export connections and plugins). Idempotent."""
        if not self._stats_closed:
            self._stats_closed = True
            self.stats.end()

    def serve_forever(self):
        """Main loop for the Web server."""
        self.web.start(self.stats)

    def end(self):
        """End of the Web server."""
        if self._ended:
            return
        self._ended = True
        self.web.end()
        self.close()
