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
from glances.timer import Counter


class GlancesShutdownManager:
    """Coordinate a unified graceful shutdown across all Glances run modes.

    The shutdown is split in ordered phases:
      1. stop accepting new connections (HTTP / XML-RPC) or stop the
         standalone main loop (via the mode stop flag)
      2. wait for in-flight requests to drain (bounded by drain_timeout)
      3. flush all export buffers (e.g. InfluxDB batch writer)
      4. close storage connections

    Modes register their own hooks through register_* methods. The manager
    is a process-wide singleton (shutdown_manager) so that the REST API
    /health endpoint can expose the current readiness state.
    """

    READY = 'ready'
    SHUTTING_DOWN = 'shutting_down'
    SHUTDOWN = 'shutdown'

    def __init__(self, drain_timeout=5.0):
        self.drain_timeout = float(drain_timeout)
        self._state = self.READY
        self._state_lock = threading.Lock()
        self._stop_accepting_hooks = []
        self._connection_trackers = []
        self._flush_hooks = []
        self._close_hooks = []

    @property
    def state(self):
        """Current readiness state: ready / shutting_down / shutdown."""
        return self._state

    @property
    def is_shutting_down(self):
        return self._state != self.READY

    def configure(self, drain_timeout=None):
        """Configure the manager (called once from the start function)."""
        if drain_timeout is not None:
            self.drain_timeout = float(drain_timeout)

    def register_stop_accepting(self, hook, name=''):
        """Phase 1 hook: stop accepting new connections / stop the main loop."""
        self._stop_accepting_hooks.append((name, hook))

    def register_connection_tracker(self, tracker, name=''):
        """Register a callable returning the number of in-flight connections."""
        self._connection_trackers.append((name, tracker))

    def register_flush(self, hook, name=''):
        """Phase 3 hook: flush export buffers."""
        self._flush_hooks.append((name, hook))

    def register_close(self, hook, name=''):
        """Phase 4 hook: close storage connections."""
        self._close_hooks.append((name, hook))

    def _run_hooks(self, hooks, phase):
        for name, hook in hooks:
            try:
                hook()
            except Exception as e:
                logger.error(f"Graceful shutdown: error in {phase} hook '{name}': {e}")

    def _active_connections(self):
        active = 0
        for name, tracker in self._connection_trackers:
            try:
                active += tracker()
            except Exception as e:
                logger.debug(f"Graceful shutdown: connection tracker '{name}' failed: {e}")
        return active

    def _wait_for_drain(self):
        """Wait for in-flight requests to complete, bounded by drain_timeout."""
        deadline = time.monotonic() + self.drain_timeout
        while True:
            active = self._active_connections()
            if active <= 0:
                return
            if time.monotonic() >= deadline:
                logger.warning(
                    f"Graceful shutdown: {active} connection(s) still active "
                    f"after {self.drain_timeout}s drain timeout, forcing shutdown"
                )
                return
            logger.debug(f"Graceful shutdown: waiting for {active} in-flight connection(s)")
            time.sleep(0.1)

    def request_shutdown(self):
        """Run the phased graceful shutdown. Idempotent."""
        with self._state_lock:
            if self._state != self.READY:
                # Shutdown already in progress (or done)
                return False
            self._state = self.SHUTTING_DOWN

        logger.info("Graceful shutdown: phase 1/4 - stop accepting new connections")
        self._run_hooks(self._stop_accepting_hooks, 'stop_accepting')

        logger.info(f"Graceful shutdown: phase 2/4 - drain in-flight requests (timeout {self.drain_timeout}s)")
        self._wait_for_drain()

        logger.info("Graceful shutdown: phase 3/4 - flush export buffers")
        self._run_hooks(self._flush_hooks, 'flush')

        logger.info("Graceful shutdown: phase 4/4 - close storage connections")
        self._run_hooks(self._close_hooks, 'close')

        with self._state_lock:
            self._state = self.SHUTDOWN
        return True


# Process-wide shutdown manager singleton
shutdown_manager = GlancesShutdownManager()

# Deferred import: glances.main (and the modules it pulls in) may rely on the
# shutdown_manager singleton defined above.
from glances.main import GlancesMain  # noqa: E402

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


# Trac malloc is only available on Python 3.4 or higher
def __signal_handler(sig, frame):
    logger.debug(f"Signal {sig} caught")
    # Avoid Glances hang when killing process with muliple CTRL-C See #3264
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # Unified graceful shutdown: stop accepting new connections, drain
    # in-flight requests, flush export buffers and close storage connections
    shutdown_manager.request_shutdown()
    end()


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

    # Configure the unified graceful shutdown manager and let the current
    # mode register its phase hooks (stop accepting / drain / flush / close)
    drain_timeout = None
    if config is not None:
        try:
            drain_timeout = config.get_float_value('outputs', 'shutdown_drain_timeout', default=None)
        except (TypeError, ValueError):
            drain_timeout = None
    shutdown_manager.configure(drain_timeout=drain_timeout)
    if hasattr(mode, 'register_shutdown_hooks'):
        mode.register_shutdown_hooks(shutdown_manager)

    start_main_loop(args, start_duration)
    snapshot_begin = check_memleak(args, mode)
    setup_server_mode(args, mode)
    maybe_trace_memleak(args, snapshot_begin)

    # Shutdown
    mode.end()


def main():
    """Main entry point for Glances.

    Select the mode (standalone, client or server)
    Run it...
    """
    # SIGHUP not available on Windows (see issue #2408)
    if sys.platform.startswith('win'):
        signal_list = (signal.SIGTERM, signal.SIGINT)
    else:
        signal_list = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    # Catch the kill signal
    for sig in signal_list:
        signal.signal(sig, __signal_handler)

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
