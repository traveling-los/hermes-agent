"""
Tests for Slack Socket Mode teardown (issue #46990).

slack_sdk's SocketModeClient.connect() is an unconditional retry loop that
swallows connection errors and never checks the client's ``closed`` flag. If a
task is still inside that loop when the client's shared aiohttp session is
closed, it keeps retrying forever and logs
``Failed to connect (error: Session is closed); Retrying...`` against a session
that can never work again.

These tests pin the ordering and cleanup that keep old-client background work
from outliving a teardown.
"""

import argparse
import asyncio
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Mock the slack-bolt package if it's not installed
# ---------------------------------------------------------------------------


def _ensure_slack_mock():
    """Install mock slack modules so SlackAdapter can be imported."""
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return  # Real library installed

    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock

    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock

    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        (
            "slack_bolt.adapter.socket_mode.async_handler",
            slack_bolt.adapter.socket_mode.async_handler,
        ),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)

    sys.modules.setdefault("aiohttp", MagicMock())


_ensure_slack_mock()

import plugins.platforms.slack.adapter as _slack_mod  # noqa: E402

_slack_mod.SLACK_AVAILABLE = True

from plugins.platforms.slack.adapter import SlackAdapter  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms.base import Platform  # noqa: E402
from gateway.run import GatewayRunner  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal stand-ins for the slack_sdk objects involved in teardown
# ---------------------------------------------------------------------------


class _FakeSession:
    """Stands in for the ``aiohttp.ClientSession`` SocketModeClient holds."""

    def __init__(self, client=None) -> None:
        self.closed = False
        self.reachable = False
        self.ws_connect_after_close = 0
        self._client = client
        self.live_tasks_at_close: list = []

    async def ws_connect(self):
        if self.closed:
            # This is the exact failure recorded in #46990.
            self.ws_connect_after_close += 1
            raise RuntimeError("Session is closed")
        if not self.reachable:
            raise ConnectionError("connection refused")
        return object()

    async def close(self) -> None:
        # Record which client tasks were still alive at the instant the shared
        # session went away. Anything listed here could be inside connect().
        if self._client is not None:
            self.live_tasks_at_close = self._client.live_task_names()
        self.closed = True
        # Closing a real session performs I/O and yields control back to the
        # loop, which is what gives a surviving retry task a chance to run.
        await asyncio.sleep(0.01)


class _FakeSocketModeClient:
    """Mirrors the parts of SocketModeClient that matter during teardown."""

    _TASK_ATTRS = ("message_processor", "current_session_monitor", "message_receiver")

    def __init__(self) -> None:
        self.aiohttp_client_session = _FakeSession(self)
        self.closed = False
        self.close_should_raise = False
        self.message_processor = None
        self.current_session_monitor = None
        self.message_receiver = None

    def live_task_names(self) -> list:
        return [
            attr
            for attr in self._TASK_ATTRS
            if getattr(self, attr) is not None and not getattr(self, attr).done()
        ]

    async def connect_to_new_endpoint(self) -> None:
        # monitor_current_session() (on staleness) and receive_messages() (on a
        # CLOSE frame) both reach connect() through here, independently.
        await self.connect()

    async def monitor_current_session(self) -> None:
        while not self.closed:
            await asyncio.sleep(0.001)
            await self.connect_to_new_endpoint()

    async def connect(self) -> None:
        # Mirrors SocketModeClient.connect(): ``while True`` with a broad
        # ``except Exception``, so neither the closed flag nor a closed session
        # ends the loop.
        while True:
            try:
                await self.aiohttp_client_session.ws_connect()
                return
            except Exception:
                await asyncio.sleep(0.001)

    async def close(self) -> None:
        self.closed = True
        if self.close_should_raise:
            # SocketModeClient.close() calls disconnect() before it cancels its
            # background tasks. A broken session makes disconnect() raise, so
            # the SDK never reaches those cancel() calls at all.
            raise RuntimeError("Session is closed")
        for task in (
            self.message_processor,
            self.current_session_monitor,
            self.message_receiver,
        ):
            if task is not None:
                # The SDK requests cancellation but never awaits it.
                task.cancel()
        await self.aiohttp_client_session.close()


class _FakeHandler:
    """Stands in for AsyncSocketModeHandler."""

    def __init__(self) -> None:
        self.client = _FakeSocketModeClient()

    async def start_async(self) -> None:
        await self.client.connect()
        await asyncio.sleep(float("inf"))

    async def close_async(self) -> None:
        await self.client.close()


async def _spin() -> None:
    while True:
        await asyncio.sleep(0.001)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def adapter():
    config = PlatformConfig(enabled=True, token="xoxb-fake-token")
    a = SlackAdapter(config)
    a._app = MagicMock()
    a._app_token = "xapp-fake"
    a._proxy_url = None
    a._running = True
    a.handle_message = AsyncMock()
    return a


def _attach(adapter, handler):
    """Wire a handler into the adapter the way _start_socket_mode_handler does."""
    adapter._handler = handler
    task = asyncio.create_task(handler.start_async())
    adapter._socket_mode_task = task
    return task


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSocketModeTeardown:
    @pytest.mark.asyncio
    async def test_socket_task_stops_before_session_is_closed(self, adapter):
        """The socket task must be stopped before close_async() kills the session.

        The task is parked in the SDK's connect() retry loop, which is the state
        #46990 describes. If teardown closes the shared session first, that loop
        wakes up and retries against a session that is already gone.
        """
        handler = _FakeHandler()
        task = _attach(adapter, handler)
        # Let the task settle into the retry loop.
        await asyncio.sleep(0.01)

        await adapter._stop_socket_mode_handler()
        # Give anything that survived a chance to make itself known.
        await asyncio.sleep(0.03)

        session = handler.client.aiohttp_client_session
        assert session.ws_connect_after_close == 0, (
            "the old socket task retried against a closed session "
            f"{session.ws_connect_after_close} time(s) after close_async()"
        )
        assert task.done(), "the old socket task outlived teardown"


    @pytest.mark.asyncio
    async def test_client_tasks_are_dead_before_the_session_closes(self, adapter):
        """Nothing may still be inside connect() when the shared session closes.

        monitor_current_session() and receive_messages() each reach
        connect_to_new_endpoint() on their own, and connect() rebinds
        current_session_monitor and message_receiver to fresh tasks on success.
        The live task set therefore changes across the awaits inside
        SocketModeClient.close(), so cancelling from a snapshot taken partway
        through races a moving target. Everything has to be stopped before the
        session is closed. See slackapi/python-slack-sdk#1913.
        """
        handler = _FakeHandler()
        client = handler.client
        client.message_processor = asyncio.create_task(_spin())
        client.current_session_monitor = asyncio.create_task(
            client.monitor_current_session()
        )
        client.message_receiver = asyncio.create_task(client.monitor_current_session())

        _attach(adapter, handler)
        # Let both reconnect loops settle inside connect().
        await asyncio.sleep(0.01)

        await adapter._stop_socket_mode_handler()
        await asyncio.sleep(0.03)

        session = client.aiohttp_client_session
        assert session.live_tasks_at_close == [], (
            "client tasks were still running when the shared session was closed: "
            f"{session.live_tasks_at_close}"
        )
        assert session.ws_connect_after_close == 0, (
            "a client task retried against a closed session "
            f"{session.ws_connect_after_close} time(s)"
        )


class TestSocketModeRestart:
    @pytest.mark.asyncio
    async def test_closed_session_replaces_old_client(self, adapter):
        """A closed aiohttp session must be replaced even if Slack says connected."""
        old = _FakeHandler()
        old.client.aiohttp_client_session.reachable = True
        old.client.ping_interval = 5
        old.client.last_ping_pong_time = time.time()
        old.client.is_connected = AsyncMock(return_value=True)
        old_socket_task = _attach(adapter, old)
        await asyncio.sleep(0.01)

        await old.client.aiohttp_client_session.close()
        retry_task = asyncio.create_task(old.client.connect())
        old.client.message_receiver = retry_task
        await asyncio.sleep(0.01)
        retries_before_watchdog = (
            old.client.aiohttp_client_session.ws_connect_after_close
        )

        started: list[_FakeHandler] = []
        retries_at_replacement: list[int] = []

        def _start_fresh() -> None:
            assert retry_task.done(), "old retry task survived into handler replacement"
            assert old_socket_task.done(), "old socket task survived into replacement"
            retries_at_replacement.append(
                old.client.aiohttp_client_session.ws_connect_after_close
            )
            fresh = _FakeHandler()
            fresh.client.aiohttp_client_session.reachable = True
            adapter._handler = fresh
            adapter._socket_mode_task = asyncio.create_task(fresh.start_async())
            started.append(fresh)

        async def _stop_after_probe() -> None:
            await asyncio.sleep(0.04)
            adapter._running = False

        adapter._start_socket_mode_handler = _start_fresh
        adapter._socket_watchdog_interval_s = 0.005
        stop_task = asyncio.create_task(_stop_after_probe())
        try:
            await adapter._socket_watchdog_loop()
        finally:
            adapter._running = False
            await stop_task
            await adapter._stop_socket_mode_handler()

        session = old.client.aiohttp_client_session
        assert len(started) == 1, (
            "closed-session client was not replaced; old SDK retry count="
            f"{session.ws_connect_after_close}"
        )
        assert retry_task.done(), "old client retry task outlived its session"
        assert retries_before_watchdog > 0, "test did not enter the SDK's bad loop"
        assert session.ws_connect_after_close == retries_at_replacement[0], (
            "old client continued retrying after handler replacement "
            f"({retries_at_replacement[0]} -> {session.ws_connect_after_close})"
        )

    @pytest.mark.asyncio
    async def test_reconnect_retries_use_exponential_backoff(self, adapter):
        starts: list[str] = []
        adapter._stop_socket_mode_handler = AsyncMock()
        adapter._start_socket_mode_handler = lambda: starts.append("started")

        await adapter._restart_socket_mode("session closed")
        first_deadline = adapter._socket_next_reconnect_monotonic
        await adapter._restart_socket_mode("session closed")
        assert starts == ["started"], "retry ignored its backoff window"

        adapter._socket_next_reconnect_monotonic = time.monotonic() - 1
        await adapter._restart_socket_mode("session closed")

        assert starts == ["started", "started"]
        assert adapter._socket_reconnect_attempts == 2
        assert first_deadline is not None
        assert adapter._socket_next_reconnect_monotonic - time.monotonic() > 29

    @pytest.mark.asyncio
    async def test_five_minute_outage_requests_process_restart(self, adapter):
        handler = _FakeHandler()
        handler.client.aiohttp_client_session.closed = True
        adapter._handler = handler
        live_task = MagicMock()
        live_task.done.return_value = False
        adapter._socket_mode_task = live_task
        adapter._socket_reconnect_started_monotonic = time.monotonic() - 301
        adapter._notify_fatal_error = AsyncMock()
        adapter._socket_watchdog_interval_s = 0.001

        await adapter._socket_watchdog_loop()

        assert adapter.fatal_error_code == "slack_reconnect_timeout"
        assert adapter.fatal_error_requires_process_restart is True
        adapter._notify_fatal_error.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gateway_exits_with_failure_for_process_restart_request(self):
        runner = object.__new__(GatewayRunner)
        runner._exit_reason = None
        runner._exit_with_failure = False
        runner.adapters = {}
        adapter = SimpleNamespace(
            fatal_error_requires_process_restart=True,
            fatal_error_message="Slack Socket Mode did not reconnect",
            platform=Platform.SLACK,
        )
        runner.adapters[Platform.SLACK] = adapter
        runner.stop = AsyncMock()

        await runner._handle_adapter_fatal_error_impl(adapter)

        assert runner.should_exit_with_failure is True
        assert runner.exit_reason == "Slack Socket Mode did not reconnect"
        runner.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_profile_gateway_exits_for_process_restart_request(self):
        runner = object.__new__(GatewayRunner)
        runner._exit_reason = None
        runner._exit_with_failure = False
        runner.stop = AsyncMock()
        adapter = SimpleNamespace(
            fatal_error_requires_process_restart=True,
            fatal_error_message="Slack Socket Mode did not reconnect",
        )
        runner._profile_adapters = {"ady": {Platform.SLACK: adapter}}

        await runner._handle_profile_adapter_fatal_error("ady", Platform.SLACK, adapter)

        assert runner.should_exit_with_failure is True
        assert runner.exit_reason == "Slack Socket Mode did not reconnect"
        runner.stop.assert_awaited_once()

    def test_main_passes_failure_exit_code_to_s6(self, monkeypatch):
        import gateway.run as gateway_run

        exit_codes: list[int] = []

        def _fake_run(coro):
            coro.close()
            return False

        monkeypatch.setattr(
            argparse.ArgumentParser,
            "parse_args",
            lambda _parser: SimpleNamespace(config=None),
        )
        monkeypatch.setattr(gateway_run.asyncio, "run", _fake_run)
        monkeypatch.setattr(
            gateway_run, "_exit_after_graceful_shutdown", exit_codes.append
        )
        monkeypatch.setattr(
            gateway_run, "_ensure_windows_gateway_venv_imports", lambda: None
        )
        monkeypatch.setitem(
            sys.modules,
            "hermes_cli.process_identity",
            SimpleNamespace(
                register_self=lambda *_args, **_kwargs: None,
                attach_self_to_kill_on_close_job=lambda: None,
            ),
        )
        monkeypatch.setitem(
            sys.modules,
            "hermes_cli.stdio",
            SimpleNamespace(configure_windows_stdio=lambda: None),
        )
        monkeypatch.delenv("AI_AGENT", raising=False)
        monkeypatch.delenv("HERMES_AGENT", raising=False)

        gateway_run.main()

        assert exit_codes == [1]

    @pytest.mark.asyncio
    async def test_watchdog_restarts_when_transport_disconnected(self, adapter):
        """A transport that reports itself down still triggers a reconnect."""
        live_task = MagicMock()
        live_task.done.return_value = False
        adapter._socket_mode_task = live_task
        adapter._handler = MagicMock()

        reasons: list[str] = []

        async def _fake_restart(reason: str) -> None:
            reasons.append(reason)
            adapter._running = False

        adapter._restart_socket_mode = _fake_restart
        adapter._socket_transport_connected = AsyncMock(return_value=False)
        adapter._socket_watchdog_interval_s = 0.01

        await adapter._socket_watchdog_loop()

        assert reasons == ["transport disconnected"]
