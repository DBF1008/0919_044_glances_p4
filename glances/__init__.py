#
# This file is part of Glances.
#
# SPDX-FileCopyrightText: 2024 Nicolas Hennion <nicolas@nicolargo.com>
#
# SPDX-License-Identifier: LGPL-3.0-only
#
#

"""Init the Glances software."""

# Import system libs
import locale
import platform
import signal
import sys
import threading
import time
import tracemalloc

# Global name
# Version should start and end with a numerical char
# See https://packaging.python.org/specifications/core-metadata/#version
# Examples: 1.0.0, 1.0.0rc1, 1.1.0_dev1
__version__ = "4.5.5_dev1"
__apiversion__ = '4'
__author__ = 'Nicolas Hennion <nicolas@nicolargo.com>'
__license__ = 'LGPLv3'

# Import psutil
try:
    from psutil import __version__ as psutil_version
except ImportError:
    print('psutil library not found. Glances cannot start.')
    sys.exit(1)

# Import Glances libs
# Note: others Glances libs will be imported optionally
from glances.logger import logger
from glances.main import GlancesMain
from glances.timer import Counter


def flush_stats_exports(stats):
    """Synchronously flush a final stats batch to all enabled export modules.

    Unlike GlancesStats.export() (which spawns one thread per export module
    and returns immediately), this helper runs each export update in the
    current thread so that, when it returns, every export buffer has been
    pushed (best effort) to the remote server. It is used during the
    graceful shutdown 'flush' phase to avoid losing the last batch of data
    (e.g. an InfluxDB bulk write).

    :param stats: a GlancesStats instance
    """
    if stats is None:
        return
    exports_registry = getattr(stats, '_exports', {})
    for export_name in stats.getExportsList():
        export_module = exports_registry.get(export_name)
        if export_module is None:
            continue
        try:
            export_module.update(stats)
        except Exception as e:
            logger.error(f"Graceful shutdown: failed to flush export module {export_name}: {e}")
        else:
            logger.debug(f"Graceful shutdown: export module {export_name} flushed")


class GlancesShutdownManager:
    """Coordinate a unified graceful shutdown across all Glances run modes.

    The shutdown is split in phases:
      1. stop accepting new connections (HTTP/WebSocket/XML-RPC) or, for the
         standalone mode, request the main loop to exit gracefully
      2. wait for in-flight requests to drain (configurable timeout)
      3. flush all export buffers (so the last batch of stats is not lost)
      4. close the storage/export connections

    Run modes register their own hooks through the register_*() methods.
    The manager installs SIGINT/SIGTERM (and SIGHUP when available) signal
    handlers; a first signal triggers the graceful shutdown, a second one
    forces the legacy immediate exit.
    """

    STATE_READY = 'ready'
    STATE_SHUTTING_DOWN = 'shutting_down'
    STATE_SHUTDOWN = 'shutdown'

    # Default time (in seconds) to wait for in-flight requests to drain
    DEFAULT_DRAIN_TIMEOUT = 10.0

    def __init__(self, drain_timeout=None):
        self._state = self.STATE_READY
        self._drain_timeout = float(drain_timeout) if drain_timeout is not None else self.DEFAULT_DRAIN_TIMEOUT
        # Hooks (lists of callables), one per shutdown phase
        self._stop_accepting_hooks = []
        self._inflight_checks = []
        self._flush_hooks = []
        self._close_hooks = []
        # Event set as soon as a shutdown has been requested
        self._shutdown_event = threading.Event()

    # State management
    # ================

    @property
    def state(self):
        """Return the current shutdown state: ready / shutting_down / shutdown."""
        return self._state

    @property
    def drain_timeout(self):
        """Return the configured drain timeout (in seconds)."""
        return self._drain_timeout

    def is_shutting_down(self):
        """Return True if a shutdown has been requested (or completed)."""
        return self._state != self.STATE_READY

    def configure(self, config=None, args=None):
        """Configure the drain timeout from the command line args or the
        configuration file ([outputs] shutdown_timeout option, in seconds)."""
        timeout = getattr(args, 'shutdown_timeout', None) if args is not None else None
        if timeout is None and config is not None:
            timeout = config.get_value('outputs', 'shutdown_timeout', default=None)
        if timeout is None:
            return
        try:
            self._drain_timeout = float(timeout)
        except (TypeError, ValueError):
            logger.warning(f"Invalid shutdown_timeout value {timeout!r}, keep {self._drain_timeout} seconds")
        else:
            logger.debug(f"Graceful shutdown drain timeout set to {self._drain_timeout} seconds")

    def reset(self):
        """Reset the manager to its initial state (mainly used by unit tests)."""
        self._state = self.STATE_READY
        self._stop_accepting_hooks = []
        self._inflight_checks = []
        self._flush_hooks = []
        self._close_hooks = []
        self._shutdown_event.clear()

    # Hooks registration
    # ==================

    def register_stop_accepting(self, hook):
        """Register a callable that stops accepting new connections/iterations.

        These hooks are called first (directly from the signal handler), so
        they should only flip flags and return immediately.
        """
        self._stop_accepting_hooks.append(hook)

    def register_inflight_check(self, hook):
        """Register a callable returning the number of in-flight requests."""
        self._inflight_checks.append(hook)

    def register_flusher(self, hook):
        """Register a callable that flushes an export buffer."""
        self._flush_hooks.append(hook)

    def register_closer(self, hook):
        """Register a callable that closes a storage/export connection."""
        self._close_hooks.append(hook)

    # Signal handling
    # ===============

    def install_signal_handlers(self):
        """Register SIGINT/SIGTERM (and SIGHUP when available) handlers.

        Should be called from the main thread.
        """
        # SIGHUP not available on Windows (see issue #2408)
        if sys.platform.startswith('win'):
            signal_list = (signal.SIGTERM, signal.SIGINT)
        else:
            signal_list = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
        for sig in signal_list:
            signal.signal(sig, self._handle_signal)

    def _handle_signal(self, sig, frame):
        logger.debug(f"Signal {sig} caught")
        if self._state == self.STATE_READY:
            # First signal: start the graceful shutdown
            self.request_shutdown()
        else:
            # Second signal: force the legacy immediate exit
            # Avoid Glances hang when killing process with multiple CTRL-C (see #3264)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            end()

    # Shutdown phases
    # ===============

    def request_shutdown(self):
        """Phase 1: stop accepting new connections/iterations.

        Called from the signal handler context: only fast, non-blocking
        flag-flipping hooks should be executed here.
        """
        if self._state != self.STATE_READY:
            return
        self._state = self.STATE_SHUTTING_DOWN
        self._shutdown_event.set()
        for hook in self._stop_accepting_hooks:
            try:
                hook()
            except Exception as e:
                logger.error(f"Graceful shutdown: stop-accepting hook failed: {e}")

    def inflight_count(self):
        """Return the total number of in-flight requests across all trackers."""
        inflight = 0
        for check in self._inflight_checks:
            try:
                inflight += check()
            except Exception as e:
                logger.error(f"Graceful shutdown: in-flight check failed: {e}")
        return inflight

    def wait_for_drain(self, timeout=None):
        """Phase 2: wait until all in-flight requests have been processed.

        :param timeout: maximum time to wait (default: the configured drain timeout)
        :return: True if all in-flight requests drained, False on timeout
        """
        timeout = self._drain_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while True:
            inflight = self.inflight_count()
            if inflight <= 0:
                return True
            if time.monotonic() >= deadline:
                logger.warning(
                    f"Graceful shutdown: drain timeout ({timeout}s) reached "
                    f"with {inflight} in-flight request(s) remaining"
                )
                return False
            time.sleep(0.1)

    def shutdown(self):
        """Execute the phased graceful shutdown (idempotent)."""
        if self._state == self.STATE_SHUTDOWN:
            return
        if self._state == self.STATE_READY:
            # No signal received (e.g. --stop-after reached): request it now
            self.request_shutdown()

        logger.info(f"Graceful shutdown: waiting for in-flight requests to drain (timeout: {self._drain_timeout}s)")
        self.wait_for_drain()

        logger.info("Graceful shutdown: flushing export buffers")
        for hook in self._flush_hooks:
            try:
                hook()
            except Exception as e:
                logger.error(f"Graceful shutdown: flush hook failed: {e}")

        logger.info("Graceful shutdown: closing connections")
        for hook in self._close_hooks:
            try:
                hook()
            except Exception as e:
                logger.error(f"Graceful shutdown: close hook failed: {e}")

        self._state = self.STATE_SHUTDOWN
        self._shutdown_event.set()
        logger.info("Graceful shutdown complete")


# Global (unique) shutdown manager instance, shared by all the run modes
shutdown_manager = GlancesShutdownManager()

# Check locale
try:
    locale.setlocale(locale.LC_ALL, '')
except locale.Error:
    print("Warning: Unable to set locale. Expect encoding problems.")

# Check psutil version
psutil_min_version = (5, 3, 0)
psutil_version_info = tuple([int(num) for num in psutil_version.split('.')])
if psutil_version_info < psutil_min_version:
    print('psutil 5.3.0 or higher is needed. Glances cannot start.')
    sys.exit(1)


def end():
    """Stop Glances."""
    try:
        mode.end()
    except (NameError, KeyError):
        # NameError: name 'mode' is not defined in case of interrupt shortly...
        # ...after starting the server mode (issue #1175)
        pass

    logger.info("Glances stopped gracefully")

    # The end...
    sys.exit(0)


def start_main_loop(args, start_duration):
    logger.debug(f"Glances started in {start_duration.get()} seconds")
    if args.stop_after:
        logger.info(f'Glances will be stopped in ~{args.stop_after * args.time} seconds')


def check_memleak(args, mode):
    if args.memory_leak:
        wait = args.stop_after * args.time * args.memory_leak * 2
        print(f'Memory leak detection, please wait ~{wait} seconds...')
        # First run without dump to fill the memory
        mode.serve_n(args.stop_after)
        # Then start the memory-leak loop
        snapshot_begin = tracemalloc.take_snapshot()
    else:
        snapshot_begin = None

    return snapshot_begin


def setup_server_mode(args, mode):
    if args.stdout_issue or args.stdout_api_restful_doc or args.stdout_api_doc:
        # Serve once for issue and API documentation modes
        mode.serve_issue()
    else:
        # Serve forever
        mode.serve_forever()


def maybe_trace_memleak(args, snapshot_begin):
    if args.trace_malloc or args.memory_leak:
        snapshot_end = tracemalloc.take_snapshot()
    if args.memory_leak:
        snapshot_diff = snapshot_end.compare_to(snapshot_begin, 'filename')
        memory_leak = sum([s.size_diff for s in snapshot_diff])
        print(f"Memory consumption: {memory_leak / 1000:.1f}KB (see log for details)")
        logger.info("Memory consumption (top 5):")
        for stat in snapshot_diff[:5]:
            logger.info(stat)
    if args.trace_malloc:
        # See more options here: https://docs.python.org/3/library/tracemalloc.html
        top_stats = snapshot_end.statistics("filename")
        print("[ Trace malloc - Top 10 ]")
        for stat in top_stats[:10]:
            print(stat)


def start(config, args):
    """Start Glances."""

    # Load mode
    global mode

    if args.trace_malloc or args.memory_leak:
        tracemalloc.start()

    start_duration = Counter()

    if core.is_standalone():
        from glances.standalone import GlancesStandalone as GlancesMode
    elif core.is_client():
        if core.is_client_browser():
            from glances.client_browser import GlancesClientBrowser as GlancesMode
        else:
            from glances.client import GlancesClient as GlancesMode
    elif core.is_server():
        from glances.server import GlancesServer as GlancesMode
    elif core.is_webserver():
        from glances.webserver import GlancesWebServer as GlancesMode

    # Init the mode
    logger.info(f"Start {GlancesMode.__name__} mode")
    mode = GlancesMode(config=config, args=args)

    # Unified graceful shutdown: configure the drain timeout, (re)install the
    # SIGINT/SIGTERM handlers and let the run mode register its hooks
    # (stop accepting new connections, in-flight tracking, export flush,
    # connection close)
    shutdown_manager.configure(config=config, args=args)
    shutdown_manager.install_signal_handlers()
    if hasattr(mode, 'register_shutdown_hooks'):
        mode.register_shutdown_hooks(shutdown_manager)

    start_main_loop(args, start_duration)
    snapshot_begin = check_memleak(args, mode)
    setup_server_mode(args, mode)
    maybe_trace_memleak(args, snapshot_begin)

    # Phased graceful shutdown: drain in-flight requests, flush the export
    # buffers then close the storage connections
    shutdown_manager.shutdown()

    # Shutdown
    mode.end()


def main():
    """Main entry point for Glances.

    Select the mode (standalone, client or server)
    Run it...
    """
    # Catch the kill signals with the unified shutdown manager
    # (re-installed and configured in the start function)
    shutdown_manager.install_signal_handlers()

    # Log Glances and psutil version
    logger.info(f'Start Glances {__version__}')
    python_impl = platform.python_implementation()
    python_ver = platform.python_version()
    logger.info(f'{python_impl} {python_ver} ({sys.executable}) and psutil {psutil_version} detected')

    # Share global var
    global core

    # Create the Glances main instance
    # Glances options from the command line are read first (in __init__)
    # then the options from the config file (in parse_args)
    core = GlancesMain()

    # Glances can be ran in standalone, client or server mode
    start(config=core.get_config(), args=core.get_args())


# End of glances/__init__.py
