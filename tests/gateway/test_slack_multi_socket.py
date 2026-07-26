"""Tests for multi-workspace Socket Mode support in the Slack adapter."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack import adapter as slack_adapter
from plugins.platforms.slack.adapter import SlackAdapter, parse_socket_pairs


def test_single_pair_legacy():
    assert parse_socket_pairs("xoxb-a", "xapp-1") == [("xoxb-a", "xapp-1")]


def test_two_pairs_positional():
    assert parse_socket_pairs("xoxb-a, xoxb-b", "xapp-1, xapp-2") == [
        ("xoxb-a", "xapp-1"),
        ("xoxb-b", "xapp-2"),
    ]


def test_extra_bot_tokens_stay_outbound_only():
    # 3 bot tokens, 1 app token → one socket on the first pair.
    assert parse_socket_pairs("xoxb-a,xoxb-b,xoxb-c", "xapp-1") == [
        ("xoxb-a", "xapp-1")
    ]


def test_more_app_than_bot_tokens_is_config_error():
    assert parse_socket_pairs("xoxb-a", "xapp-1,xapp-2") is None


def test_empty_app_tokens_is_config_error():
    assert parse_socket_pairs("xoxb-a", "") is None
    assert parse_socket_pairs("xoxb-a", " , ") is None


def test_whitespace_and_trailing_commas_tolerated():
    assert parse_socket_pairs(" xoxb-a ,", " xapp-1 ,") == [("xoxb-a", "xapp-1")]


# --- connect(): N sockets, per-token locks, partial degradation ---------


class _FakeAuthResp(dict):
    headers: dict = {}


def _make_adapter(bot_tokens: str) -> SlackAdapter:
    cfg = PlatformConfig(enabled=True, token=bot_tokens)
    return SlackAdapter(cfg)


class _FakeApp:
    def __init__(self, token=None):
        self.token = token
        self.client = SimpleNamespace(token=token)

    def event(self, _name):
        return lambda fn: fn

    def command(self, _pat):
        return lambda fn: fn

    def action(self, _aid):
        return lambda fn: fn


class _FakeHandler:
    def __init__(self, app, app_token, proxy=None):
        self.app, self.app_token = app, app_token
        self.client = SimpleNamespace(is_connected=lambda: True)

    async def start_async(self):
        await asyncio.Event().wait()  # park forever like a live socket

    async def close_async(self):
        pass


def _fake_web_client_factory(auth_by_token):
    class _FakeWebClient:
        def __init__(self, token=None):
            self.token = token

        async def auth_test(self):
            resp = _FakeAuthResp(auth_by_token[self.token])
            return resp

    return _FakeWebClient


# A single reused event loop, not ``asyncio.get_event_loop()`` (deprecated
# with no running loop in 3.12+) and not a fresh loop per call (a fresh loop
# per ``_run()`` would orphan tasks — e.g. the socket watchdog and Socket
# Mode handler tasks created during ``connect()`` — created on a prior,
# now-closed loop, which breaks the paired ``disconnect()`` call in the same
# test).
_loop = asyncio.new_event_loop()


def _run(coro):
    return _loop.run_until_complete(coro)


def _patched_env(app_tokens: str, auth_by_token):
    return [
        patch.object(slack_adapter, "SLACK_AVAILABLE", True),
        patch.object(slack_adapter, "AsyncApp", _FakeApp),
        patch.object(slack_adapter, "AsyncSocketModeHandler", _FakeHandler),
        patch.object(
            slack_adapter, "AsyncWebClient", _fake_web_client_factory(auth_by_token)
        ),
        patch.dict("os.environ", {"SLACK_APP_TOKEN": app_tokens}),
        patch.object(SlackAdapter, "_acquire_platform_lock", return_value=True),
        patch.object(SlackAdapter, "_release_platform_lock"),
    ]


def test_connect_opens_one_socket_per_pair():
    auth = {
        "xoxb-a": {"team_id": "T1", "user_id": "U1", "user": "mia", "team": "careflow"},
        "xoxb-b": {"team_id": "T2", "user_id": "U2", "user": "mia", "team": "iamia"},
    }
    ad = _make_adapter("xoxb-a,xoxb-b")
    patches = _patched_env("xapp-1,xapp-2", auth)
    for p in patches:
        p.start()
    try:
        ok = _run(ad.connect())
        assert ok is True
        assert len(ad._connections) == 2
        assert {c.team_id for c in ad._connections} == {"T1", "T2"}
        assert ad._team_clients["T1"].token == "xoxb-a"
        assert ad._team_clients["T2"].token == "xoxb-b"
        _run(ad.disconnect())
    finally:
        for p in patches:
            p.stop()


def test_connect_partial_degradation_one_bad_token():
    class _Boom(Exception):
        pass

    auth = {
        "xoxb-a": {"team_id": "T1", "user_id": "U1", "user": "mia", "team": "careflow"},
    }

    base_factory = _fake_web_client_factory(auth)

    class _FailingSecond(base_factory):
        async def auth_test(self):
            if self.token == "xoxb-bad":
                raise _Boom("invalid_auth")
            return await super().auth_test()

    ad = _make_adapter("xoxb-a,xoxb-bad")
    patches = _patched_env("xapp-1,xapp-2", auth)
    patches[3] = patch.object(slack_adapter, "AsyncWebClient", _FailingSecond)
    for p in patches:
        p.start()
    try:
        ok = _run(ad.connect())
        assert ok is True                      # ≥1 workspace up → success
        assert len(ad._connections) == 1
        assert ad._connections[0].team_id == "T1"
        _run(ad.disconnect())
    finally:
        for p in patches:
            p.stop()


def test_connect_config_error_more_app_tokens():
    ad = _make_adapter("xoxb-a")
    patches = _patched_env("xapp-1,xapp-2", {})
    for p in patches:
        p.start()
    try:
        assert _run(ad.connect()) is False
    finally:
        for p in patches:
            p.stop()


def test_connect_watchdog_failure_after_running_tears_down_and_releases_locks():
    """A failure after ``_running`` flips True (e.g. watchdog start) must not
    leave live sockets behind a False connect(): ``_running`` resets, every
    started handler is stopped, connections are cleared, and the ``finally``
    block releases the acquired app-token locks."""
    auth = {
        "xoxb-a": {"team_id": "T1", "user_id": "U1", "user": "mia", "team": "careflow"},
    }
    ad = _make_adapter("xoxb-a")
    patches = _patched_env("xapp-1", auth)
    # Use the real base-class lock helpers so lock release is observable,
    # patching the gateway.status primitives they delegate to instead.
    patches[5] = patch("gateway.status.acquire_scoped_lock", return_value=(True, None))
    patches[6] = patch("gateway.status.release_scoped_lock")
    watchdog_boom = patch.object(
        SlackAdapter,
        "_ensure_socket_watchdog",
        side_effect=RuntimeError("watchdog boom"),
    )
    started = [p.start() for p in patches]
    mock_release = started[6]
    watchdog_boom.start()
    try:
        ok = _run(ad.connect())
        assert ok is False
        assert ad._running is False
        assert ad._connections == []  # started handler stopped and cleared
        assert ad._socket_lock_identities == []  # lock bookkeeping cleared
        mock_release.assert_called_once_with("slack-app-token", "xapp-1")
    finally:
        watchdog_boom.stop()
        for p in patches:
            p.stop()
