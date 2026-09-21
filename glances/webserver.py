#
# This file is part of Glances.
#
# SPDX-FileCopyrightText: 2024 Nicolas Hennion <nicolas@nicolargo.com>
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""Glances Restful/API and Web based interface."""

from glances.globals import WINDOWS
from glances.logger import logger
from glances.outputs.glances_restful_api import GlancesRestfulApi
from glances.processes import glances_processes
from glances.stats import GlancesStats


class GlancesWebServer:
    """This class creates and manages the Glances Web server session."""

    def __init__(self, config=None, args=None):
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

    def serve_forever(self):
        """Main loop for the Web server."""
        self.web.start(self.stats)

    def flush_exports(self):
        """Flush export buffers synchronously (shutdown manager phase 3)."""
        for e in self.stats.getExportsList():
            try:
                self.stats._exports[e].update(self.stats)
            except Exception as e:
                logger.error(f"Can not flush export buffer for the {e} module: {e}")

    def register_shutdown_hooks(self, manager):
        """Register the Web server mode hooks in the GlancesShutdownManager.

        Phase 1 asks uvicorn to stop accepting new connections (it drains
        in-flight HTTP requests and WebSocket connections itself); phase 2
        tracks the remaining open connections.
        """
        manager.register_stop_accepting(self.web.stop, name='webserver')
        manager.register_connection_tracker(lambda: self.web.active_connections, name='webserver')
        manager.register_flush(self.flush_exports, name='webserver-exports')

    def end(self):
        """End of the Web server."""
        self.web.end()
        self.stats.end()
