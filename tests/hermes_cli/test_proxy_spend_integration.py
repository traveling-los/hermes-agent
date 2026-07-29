"""End-to-end: proxy tees SSE usage without altering what the client receives.

Mirrors the harness style of ``tests/hermes_cli/test_proxy.py`` (real aiohttp
servers on ephemeral ports, driven by ``asyncio.run``) so no extra pytest
plugin is required.
"""

from __future__ import annotations

import asyncio
import json

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402

from hermes_cli.proxy import spend_log  # noqa: E402
from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential  # noqa: E402
from hermes_cli.proxy.server import create_app  # noqa: E402

SSE_BODY = (
    b'data: {"id":"1","model":"grok-4.5","choices":[{"delta":{"content":"a"}}]}\n\n'
    b'data: {"id":"1","model":"grok-4.5","choices":[{"delta":{"content":"b"}}]}\n\n'
    b'data: {"id":"1","model":"grok-4.5","choices":[],"usage":'
    b'{"prompt_tokens":1234,"completion_tokens":56,"total_tokens":1290,'
    b'"prompt_tokens_details":{"cached_tokens":1000}}}\n\n'
    b"data: [DONE]\n\n"
)


class _FakeAdapter(UpstreamAdapter):
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    @property
    def name(self):
        return "fake"

    @property
    def display_name(self):
        return "Fake Upstream"

    @property
    def allowed_paths(self):
        return frozenset({"/chat/completions", "/models"})

    def is_authenticated(self):
        return True

    def get_credential(self):
        return UpstreamCredential(
            bearer="not-a-real-token",
            base_url=self._base_url,
            label="test-account",
        )

    def get_retry_credential(self, *, failed_credential, status_code):
        return None


async def _start(app):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = list(site._server.sockets)[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


def _upstream_app(seen: dict):
    async def stream(request):
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        resp = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream"}
        )
        await resp.prepare(request)
        # Many small writes: the tee must survive usage split across chunks.
        for i in range(0, len(SSE_BODY), 9):
            await resp.write(SSE_BODY[i : i + 9])
        await resp.write_eof()
        return resp

    async def models(request):
        return web.json_response({"data": []})

    app = web.Application()
    app.router.add_route("*", "/v1/chat/completions", stream)
    app.router.add_route("*", "/v1/models", models)
    return app


def _spend_lines(tmp_path):
    files = list((tmp_path / "spend").glob("*.jsonl"))
    if not files:
        return []
    return files[0].read_text().strip().splitlines()


def _run(tmp_path, monkeypatch, coro_factory):
    monkeypatch.setenv(spend_log.SPEND_DIR_ENV, str(tmp_path / "spend"))

    async def run():
        seen: dict = {}
        up_runner, up_base = await _start(_upstream_app(seen))
        adapter = _FakeAdapter(f"{up_base}/v1")
        proxy_runner, proxy_base = await _start(create_app(adapter))
        try:
            return await coro_factory(proxy_base, seen)
        finally:
            await proxy_runner.cleanup()
            await up_runner.cleanup()

    return asyncio.run(run())


def test_sse_passthrough_is_byte_identical_and_usage_is_logged(tmp_path, monkeypatch):
    async def body(proxy_base, seen):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{proxy_base}/v1/chat/completions",
                data=json.dumps({"model": "grok-4.5", "stream": True}),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer client-side-value",
                    spend_log.AGENT_ID_HEADER: "super_agente",
                },
            ) as resp:
                assert resp.status == 200
                return await resp.read(), seen

    received, seen = _run(tmp_path, monkeypatch, body)

    # 1. The client gets exactly what the upstream sent.
    assert received == SSE_BODY

    # 2. The Hermes-internal header never reaches the upstream, and the
    #    inbound Authorization was replaced by the adapter's credential.
    assert spend_log.AGENT_ID_HEADER_LOWER not in seen["headers"]
    assert seen["headers"]["authorization"] == "Bearer not-a-real-token"

    # 3. Exactly one spend record, with real token counts and attribution.
    lines = _spend_lines(tmp_path)
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["agent_id"] == "super_agente"
    assert rec["model"] == "grok-4.5"
    assert rec["status"] == 200
    assert rec["credential_label"] == "test-account"
    assert rec["usage_found"] is True
    assert rec["input_tokens"] == 1234
    assert rec["output_tokens"] == 56
    assert rec["cached_tokens"] == 1000
    assert rec["total_tokens"] == 1290
    assert rec["path"] == "/chat/completions"
    assert rec["stream"] is True
    assert isinstance(rec["duration_ms"], int)
    assert rec["response_bytes"] == len(SSE_BODY)
    # No secret ever lands in the log.
    assert "not-a-real-token" not in lines[0]


def test_missing_agent_header_falls_back_to_unknown(tmp_path, monkeypatch):
    async def body(proxy_base, seen):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{proxy_base}/v1/chat/completions",
                data=json.dumps({"model": "grok-4.5", "stream": True}),
                headers={"Content-Type": "application/json"},
            ) as resp:
                await resp.read()

    _run(tmp_path, monkeypatch, body)
    rec = json.loads(_spend_lines(tmp_path)[0])
    assert rec["agent_id"] == "unknown"


def test_untracked_path_writes_no_record(tmp_path, monkeypatch):
    """/models must not generate spend noise."""

    async def body(proxy_base, seen):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{proxy_base}/v1/models") as resp:
                await resp.read()

    _run(tmp_path, monkeypatch, body)
    assert _spend_lines(tmp_path) == []


def test_spend_log_failure_never_breaks_the_response(tmp_path, monkeypatch):
    """A broken log destination must not cost the caller their response."""

    def _boom(record):
        raise OSError("disk on fire")

    monkeypatch.setattr(spend_log, "write_record", _boom)

    async def body(proxy_base, seen):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{proxy_base}/v1/chat/completions",
                data=json.dumps({"model": "grok-4.5", "stream": True}),
                headers={"Content-Type": "application/json"},
            ) as resp:
                assert resp.status == 200
                return await resp.read()

    assert _run(tmp_path, monkeypatch, body) == SSE_BODY
