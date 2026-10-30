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

from plugins.platforms.slack.adapter import SlackAdapter, _WorkspaceConnection  # noqa: E402
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
    # Socket Mode state lives per workspace on ``_WorkspaceConnection`` now;
    # ``_app`` is a read-only property over ``_connections``.
    a._connections = [
        _WorkspaceConnection(
            bot_token="xoxb-fake-token", app_token="xapp-fake", app=MagicMock()
        )
    ]
    a._proxy_url = None
    a._running = True
    a.handle_message = AsyncMock()
    return a


def _conn(adapter):
    return adapter._connections[0]


def _attach(adapter, handler):
    """Wire a handler into the adapter the way _start_socket_mode_handler does."""
    conn = _conn(adapter)
    conn.handler = handler
    task = asyncio.create_task(handler.start_async())
    conn.socket_task = task
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

        await adapter._stop_socket_mode_handler(_conn(adapter))
        # Give anything that survived a chance to make itself known.
        await asyncio.sleep(0.03)

        session = handler.client.aiohttp_client_session
        assert session.ws_connect_after_close == 0, (
            "the old socket task retried against a closed session "
            f"{session.ws_connect_after_close} time(s) after close_async()"
        )
        assert task.done(), "the old socket task outlived teardown"

    @pytest.mark.asyncio
    async def test_sdk_background_tasks_do_not_outlive_teardown(self, adapter):
        """Old-client background tasks must be cancelled even if close_async() fails.

        SocketModeClient.close() cancels message_processor,
        current_session_monitor and message_receiver only after disconnect()
        returns, so a raising disconnect() leaves all three running. The adapter
        logs and moves on, so it has to clean them up itself.
        """
        handler = _FakeHandler()
        client = handler.client
        client.close_should_raise = True
        client.message_processor = asyncio.create_task(_spin())
        client.current_session_monitor = asyncio.create_task(_spin())
        client.message_receiver = asyncio.create_task(_spin())

        _attach(adapter, handler)
        await asyncio.sleep(0.01)

        await adapter._stop_socket_mode_handler(_conn(adapter))
        await asyncio.sleep(0.03)

        for name in (
            "message_processor",
            "current_session_monitor",
            "message_receiver",
        ):
            assert getattr(client, name).done(), f"{name} outlived teardown"

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

        await adapter._stop_socket_mode_handler(_conn(adapter))
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

    @pytest.mark.asyncio
    async def test_stop_clears_adapter_state(self, adapter):
        """Teardown always drops its references, even when close_async() raises."""
        handler = _FakeHandler()
        handler.client.close_should_raise = True
        _attach(adapter, handler)
        await asyncio.sleep(0.01)

        await adapter._stop_socket_mode_handler(_conn(adapter))

        assert _conn(adapter).handler is None
        assert _conn(adapter).socket_task is None


class TestSocketModeRestart:
    @pytest.mark.asyncio
    async def test_closed_session_replaces_old_client(self, adapter):
        """A closed aiohttp session must not leave Slack retrying forever.

        The SDK's ``is_connected()`` can still report true while a reconnect
        task loops against a manually closed ClientSession. The adapter must
        recognize the closed session directly, retire every old task, then
        create a handler with a fresh session.
        """
        old = _FakeHandler()
        old.client.aiohttp_client_session.reachable = True
        old.client.ping_interval = 5
        old.client.last_ping_pong_time = time.time()
        old.client.is_connected = AsyncMock(return_value=True)
        _attach(adapter, old)
        await asyncio.sleep(0.01)

        retry_task = asyncio.create_task(old.client.connect())
        old.client.message_receiver = retry_task
        await old.client.aiohttp_client_session.close()
        await asyncio.sleep(0.01)
        retries_before_watchdog = (
            old.client.aiohttp_client_session.ws_connect_after_close
        )

        started: list[_FakeHandler] = []
        retries_at_replacement: list[int] = []

        def _start_fresh(conn) -> None:
            assert retry_task.done(), "old retry task survived into handler replacement"
            retries_at_replacement.append(
                old.client.aiohttp_client_session.ws_connect_after_close
            )
            fresh = _FakeHandler()
            fresh.client.aiohttp_client_session.reachable = True
            conn.handler = fresh
            conn.socket_task = asyncio.create_task(fresh.start_async())
            started.append(fresh)

        with patch.object(adapter, "_start_socket_mode_handler", _start_fresh):

            async def _stop_after_probe() -> None:
                await asyncio.sleep(0.04)
                adapter._running = False

            stop_task = asyncio.create_task(_stop_after_probe())
            adapter._socket_watchdog_interval_s = 0.005
            await adapter._socket_watchdog_loop()
            await stop_task

        await asyncio.sleep(0.02)
        assert len(started) == 1, (
            "closed-session client was not replaced; old SDK retry count="
            f"{old.client.aiohttp_client_session.ws_connect_after_close}"
        )
        assert (
            started[0].client.aiohttp_client_session
            is not old.client.aiohttp_client_session
        ), "replacement handler reused the closed aiohttp session"
        assert retry_task.done(), "old client retry task outlived its session"
        assert retries_before_watchdog > 0, "test did not enter the SDK's bad loop"
        assert (
            old.client.aiohttp_client_session.ws_connect_after_close
            == retries_at_replacement[0]
        ), (
            "old client continued retrying after handler replacement "
            f"({retries_at_replacement[0]} -> "
            f"{old.client.aiohttp_client_session.ws_connect_after_close})"
        )

    @pytest.mark.asyncio
    async def test_restart_stops_old_handler_before_starting_new_one(self, adapter):
        """A reconnect must fully retire the old handler before replacing it."""
        old = _FakeHandler()
        old_task = _attach(adapter, old)
        await asyncio.sleep(0.01)

        started: list[str] = []

        def _fake_start(conn) -> None:
            started.append("started")
            assert old_task.done(), (
                "the replacement handler was created while the old socket task "
                "was still running"
            )

        with patch.object(adapter, "_start_socket_mode_handler", _fake_start):
            await adapter._restart_socket_mode(_conn(adapter), "transport disconnected")

        await asyncio.sleep(0.03)

        assert started == ["started"]
        assert old.client.aiohttp_client_session.ws_connect_after_close == 0

    @pytest.mark.asyncio
    async def test_reconnect_retries_use_exponential_backoff(self, adapter):
        conn = _conn(adapter)
        starts: list[str] = []

        async def _stop(_conn) -> None:
            _conn.handler = None
            _conn.socket_task = None

        def _start(_conn) -> None:
            starts.append("start")
            _conn.handler = MagicMock()

        with (
            patch.object(adapter, "_stop_socket_mode_handler", _stop),
            patch.object(adapter, "_start_socket_mode_handler", _start),
        ):
            await adapter._restart_socket_mode(conn, "session closed")
            first_deadline = conn.next_reconnect_monotonic
            await adapter._restart_socket_mode(conn, "session closed")
            assert len(starts) == 1, "retry ignored its backoff window"

            conn.next_reconnect_monotonic = time.monotonic() - 1
            await adapter._restart_socket_mode(conn, "session closed")

        assert len(starts) == 2
        assert conn.reconnect_attempts == 2
        assert first_deadline is not None
        assert conn.next_reconnect_monotonic - time.monotonic() > 29

    @pytest.mark.asyncio
    async def test_five_minute_outage_requests_process_restart(self, adapter):
        conn = _conn(adapter)
        handler = _FakeHandler()
        handler.client.aiohttp_client_session.closed = True
        conn.handler = handler
        live_task = MagicMock()
        live_task.done.return_value = False
        conn.socket_task = live_task
        conn.reconnect_started_monotonic = time.monotonic() - 301
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
        runner.stop = AsyncMock()
        adapter = SimpleNamespace(
            fatal_error_requires_process_restart=True,
            fatal_error_message="Slack Socket Mode did not reconnect",
            platform=SimpleNamespace(value="slack"),
        )

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
        platform = Platform.SLACK
        adapter = SimpleNamespace(
            fatal_error_requires_process_restart=True,
            fatal_error_message="Slack Socket Mode did not reconnect",
        )
        runner._profile_adapters = {"ady": {platform: adapter}}

        await runner._handle_profile_adapter_fatal_error("ady", platform, adapter)

        assert runner.should_exit_with_failure is True
        assert runner.exit_reason == "Slack Socket Mode did not reconnect"
        runner.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_watchdog_restarts_when_socket_task_stops(self, adapter):
        """The existing watchdog triggers still fire after the teardown change."""
        done_task = MagicMock()
        done_task.done.return_value = True
        _conn(adapter).socket_task = done_task

        reasons: list[str] = []

        async def _fake_restart(conn, reason: str) -> None:
            reasons.append(reason)
            adapter._running = False

        adapter._restart_socket_mode = _fake_restart
        adapter._socket_transport_connected = AsyncMock(return_value=None)
        adapter._socket_watchdog_interval_s = 0.01

        await adapter._socket_watchdog_loop()

        assert reasons == ["socket task stopped"]

    @pytest.mark.asyncio
    async def test_watchdog_restarts_when_transport_disconnected(self, adapter):
        """A transport that reports itself down still triggers a reconnect."""
        live_task = MagicMock()
        live_task.done.return_value = False
        _conn(adapter).socket_task = live_task
        _conn(adapter).handler = MagicMock()

        reasons: list[str] = []

        async def _fake_restart(conn, reason: str) -> None:
            reasons.append(reason)
            adapter._running = False

        adapter._restart_socket_mode = _fake_restart
        adapter._socket_transport_connected = AsyncMock(return_value=False)
        adapter._socket_watchdog_interval_s = 0.01

        await adapter._socket_watchdog_loop()

        assert reasons == ["transport disconnected"]
