#!/usr/bin/env python
#
# Glances - An eye on your system
#
# SPDX-FileCopyrightText: 2024 Nicolas Hennion <nicolas@nicolargo.com>
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""Glances unitary tests suite for the unified graceful shutdown."""

import json
import threading
import time
import types
import unittest
from unittest import mock

from glances import GlancesShutdownManager, flush_stats_exports, shutdown_manager
from glances.outputs.glances_restful_api import GlancesRestfulApi
from glances.server import GlancesXMLRPCHandler, GlancesXMLRPCServer
from glances.standalone import GlancesStandalone


class TestGlancesShutdownManager(unittest.TestCase):
    """Test the GlancesShutdownManager phased shutdown."""

    def setUp(self):
        self.manager = GlancesShutdownManager()

    def test_001_initial_state(self):
        """Initial state should be ready."""
        self.assertEqual(self.manager.state, 'ready')
        self.assertFalse(self.manager.is_shutting_down())
        self.assertEqual(self.manager.drain_timeout, GlancesShutdownManager.DEFAULT_DRAIN_TIMEOUT)

    def test_002_request_shutdown_runs_stop_accepting_hooks(self):
        """request_shutdown should switch state and run stop-accepting hooks."""
        calls = []
        self.manager.register_stop_accepting(lambda: calls.append('stop'))
        self.manager.request_shutdown()
        self.assertEqual(self.manager.state, 'shutting_down')
        self.assertTrue(self.manager.is_shutting_down())
        self.assertEqual(calls, ['stop'])
        # A second request should be a no-op
        self.manager.request_shutdown()
        self.assertEqual(calls, ['stop'])

    def test_003_phased_shutdown_order(self):
        """shutdown should execute hooks in phase order: stop, flush, close."""
        calls = []
        self.manager.register_stop_accepting(lambda: calls.append('stop_accepting'))
        self.manager.register_inflight_check(lambda: 0)
        self.manager.register_flusher(lambda: calls.append('flush'))
        self.manager.register_closer(lambda: calls.append('close'))
        self.manager.shutdown()
        self.assertEqual(calls, ['stop_accepting', 'flush', 'close'])
        self.assertEqual(self.manager.state, 'shutdown')

    def test_004_shutdown_is_idempotent(self):
        """shutdown should only run the phases once."""
        calls = []
        self.manager.register_flusher(lambda: calls.append('flush'))
        self.manager.shutdown()
        self.manager.shutdown()
        self.assertEqual(calls, ['flush'])

    def test_005_wait_for_drain_success(self):
        """wait_for_drain should return True once in-flight requests reach 0."""
        inflight = {'count': 2}
        self.manager.register_inflight_check(lambda: inflight['count'])

        def drain():
            time.sleep(0.3)
            inflight['count'] = 0

        threading.Thread(target=drain, daemon=True).start()
        self.assertTrue(self.manager.wait_for_drain(timeout=5))

    def test_006_wait_for_drain_timeout(self):
        """wait_for_drain should return False when the timeout is reached."""
        self.manager.register_inflight_check(lambda: 1)
        start = time.monotonic()
        self.assertFalse(self.manager.wait_for_drain(timeout=0.5))
        self.assertLess(time.monotonic() - start, 2)

    def test_007_hook_exception_does_not_break_shutdown(self):
        """A failing hook should not prevent the other phases to run."""
        calls = []

        def bad_hook():
            raise RuntimeError('boom')

        self.manager.register_stop_accepting(bad_hook)
        self.manager.register_flusher(bad_hook)
        self.manager.register_closer(lambda: calls.append('close'))
        self.manager.shutdown()
        self.assertEqual(calls, ['close'])
        self.assertEqual(self.manager.state, 'shutdown')

    def test_008_configure_drain_timeout(self):
        """configure should read the timeout from args then config."""
        args = types.SimpleNamespace(shutdown_timeout=42)
        self.manager.configure(config=None, args=args)
        self.assertEqual(self.manager.drain_timeout, 42.0)

        manager = GlancesShutdownManager()
        config = mock.Mock()
        config.get_value.return_value = '7'
        manager.configure(config=config, args=types.SimpleNamespace())
        self.assertEqual(manager.drain_timeout, 7.0)

        manager = GlancesShutdownManager()
        config = mock.Mock()
        config.get_value.return_value = 'not_a_number'
        manager.configure(config=config, args=None)
        self.assertEqual(manager.drain_timeout, GlancesShutdownManager.DEFAULT_DRAIN_TIMEOUT)

    def test_009_reset(self):
        """reset should restore the initial state."""
        self.manager.register_flusher(lambda: None)
        self.manager.shutdown()
        self.assertEqual(self.manager.state, 'shutdown')
        self.manager.reset()
        self.assertEqual(self.manager.state, 'ready')


class TestFlushStatsExports(unittest.TestCase):
    """Test the synchronous export flush helper."""

    def test_010_flush_calls_update_synchronously(self):
        """flush_stats_exports should call update() on each enabled export."""
        export_module = mock.Mock()
        stats = mock.Mock()
        stats.getExportsList.return_value = ['influxdb']
        stats._exports = {'influxdb': export_module}
        flush_stats_exports(stats)
        export_module.update.assert_called_once_with(stats)

    def test_011_flush_survives_export_error(self):
        """flush_stats_exports should not raise if an export fails."""
        export_module = mock.Mock()
        export_module.update.side_effect = RuntimeError('boom')
        stats = mock.Mock()
        stats.getExportsList.return_value = ['influxdb']
        stats._exports = {'influxdb': export_module}
        flush_stats_exports(stats)  # Should not raise

    def test_012_flush_none_stats(self):
        """flush_stats_exports should accept None stats."""
        flush_stats_exports(None)  # Should not raise


class TestStandaloneGracefulShutdown(unittest.TestCase):
    """Test the standalone mode graceful exit flag."""

    def test_020_request_shutdown_sets_flag(self):
        """request_shutdown should set the graceful exit flag."""
        standalone = GlancesStandalone.__new__(GlancesStandalone)
        standalone._shutdown_requested = False
        standalone.request_shutdown()
        self.assertTrue(standalone._shutdown_requested)

    def test_021_serve_forever_exits_on_shutdown_flag(self):
        """serve_forever should not loop when the shutdown flag is set."""
        standalone = GlancesStandalone.__new__(GlancesStandalone)
        standalone._shutdown_requested = True
        standalone.args = types.SimpleNamespace(stop_after=None)
        # serve_forever should return immediately without calling __serve_once
        standalone.serve_forever()

    def test_022_register_shutdown_hooks(self):
        """The standalone mode should register its hooks in the manager."""
        manager = GlancesShutdownManager()
        standalone = GlancesStandalone.__new__(GlancesStandalone)
        standalone._shutdown_requested = False
        standalone._stats_closed = False
        standalone.stats = mock.Mock()
        standalone.register_shutdown_hooks(manager)
        # Phase 1 hook should set the exit flag
        manager.request_shutdown()
        self.assertTrue(standalone._shutdown_requested)
        # Phase 4 hook should close the stats (only once)
        manager.shutdown()
        standalone.stats.end.assert_called_once()

    def test_023_end_is_idempotent(self):
        """end should only run the cleanup once."""
        standalone = GlancesStandalone.__new__(GlancesStandalone)
        standalone._ended = False
        standalone._stats_closed = False
        standalone._quiet = True
        standalone.stats = mock.Mock()
        standalone.outdated = mock.Mock()
        standalone.outdated.is_outdated.return_value = False
        standalone.end()
        standalone.end()
        standalone.stats.end.assert_called_once()


class TestXMLRPCServerShutdown(unittest.TestCase):
    """Test the XML-RPC server shutdown method and connection tracking."""

    def _make_server(self):
        try:
            return GlancesXMLRPCServer('127.0.0.1', 0, config=None)
        except OSError as e:
            self.skipTest(f"Cannot bind a local socket in this environment: {e}")

    def test_030_connection_tracking(self):
        """connection_opened/closed should track in-flight connections."""
        server = self._make_server()
        try:
            self.assertEqual(server.inflight_connections, 0)
            server.connection_opened()
            server.connection_opened()
            self.assertEqual(server.inflight_connections, 2)
            server.connection_closed()
            self.assertEqual(server.inflight_connections, 1)
            server.connection_closed()
            # Counter should not go below 0
            server.connection_closed()
            self.assertEqual(server.inflight_connections, 0)
        finally:
            server.end()

    def test_031_shutdown_stops_serve_forever(self):
        """shutdown should make serve_forever return."""
        server = self._make_server()
        self.assertFalse(server.finished)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.2)
        server.shutdown()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(server.finished)
        server.end()

    def test_032_handler_tracks_connections(self):
        """The XML-RPC handler should increment/decrement the server counter."""
        server = self._make_server()
        try:
            handler = mock.Mock()
            handler.server = server
            # Simulate the setup/finish calls made by socketserver
            GlancesXMLRPCHandler.setup(handler)
            self.assertEqual(server.inflight_connections, 1)
            GlancesXMLRPCHandler.finish(handler)
            self.assertEqual(server.inflight_connections, 0)
        finally:
            server.end()


class TestRestfulApiHealth(unittest.TestCase):
    """Test the /health endpoint payload."""

    def setUp(self):
        shutdown_manager.reset()

    def tearDown(self):
        shutdown_manager.reset()

    def _make_api(self):
        api = GlancesRestfulApi.__new__(GlancesRestfulApi)
        stats = mock.Mock()
        stats.getPluginsList.return_value = ['cpu', 'mem', 'processcount']
        export_module = mock.Mock()
        export_module.export_enable = True
        stats.getExportsList.return_value = ['json']
        stats._exports = {'json': export_module}
        stats.get_plugin.return_value.get_raw.return_value = {'total': 123}
        api.stats = stats
        return api

    def test_040_health_ready(self):
        """Health endpoint should return ready with components info."""
        api = self._make_api()
        response = api._api_health()
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.body)
        self.assertEqual(data['status'], 'ready')
        self.assertIn('version', data)
        self.assertEqual(data['components']['plugins']['status'], 'ok')
        self.assertEqual(data['components']['plugins']['count'], 3)
        self.assertEqual(data['components']['exports']['status'], 'ok')
        self.assertEqual(data['components']['exports']['connections'], {'json': 'connected'})
        self.assertEqual(data['components']['processlist']['status'], 'ok')
        self.assertEqual(data['components']['processlist']['total'], 123)

    def test_041_health_shutting_down(self):
        """Health endpoint should return 503 while shutting down."""
        api = self._make_api()
        shutdown_manager.request_shutdown()
        response = api._api_health()
        self.assertEqual(response.status_code, 503)
        data = json.loads(response.body)
        self.assertEqual(data['status'], 'shutting_down')

    def test_042_health_no_exports(self):
        """Health endpoint should report disabled exports when none is enabled."""
        api = self._make_api()
        api.stats.getExportsList.return_value = []
        api.stats._exports = {}
        response = api._api_health()
        data = json.loads(response.body)
        self.assertEqual(data['components']['exports']['status'], 'disabled')

    def test_043_initiate_shutdown_and_inflight(self):
        """initiate_shutdown should set the shutdown flag and uvicorn should_exit."""
        api = GlancesRestfulApi.__new__(GlancesRestfulApi)
        api._shutting_down = False
        api.uvicorn_server = mock.Mock()
        api.uvicorn_server.should_exit = False
        api.uvicorn_server.server_state = types.SimpleNamespace(connections={'a', 'b'})
        self.assertEqual(api.inflight_connections(), 2)
        api.initiate_shutdown()
        self.assertTrue(api._shutting_down)
        self.assertTrue(api.uvicorn_server.should_exit)

    def test_044_inflight_without_server(self):
        """inflight_connections should return 0 when the server is not started."""
        api = GlancesRestfulApi.__new__(GlancesRestfulApi)
        api.uvicorn_server = None
        self.assertEqual(api.inflight_connections(), 0)


if __name__ == '__main__':
    unittest.main()
