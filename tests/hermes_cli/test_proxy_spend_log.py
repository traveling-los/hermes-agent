"""Tests for proxy spend attribution (usage tee + JSONL record)."""

from __future__ import annotations

import json

import pytest

from hermes_cli.auth import _is_loopback_host, _xai_validate_inference_base_url
from hermes_cli.proxy import spend_log

DEFAULT_XAI = "https://api.x.ai/v1"


def _feed(sniffer: spend_log.UsageSniffer, payload: bytes, *, chunk: int) -> None:
    """Feed a body in fixed-size chunks to exercise partial-line handling."""
    for i in range(0, len(payload), chunk):
        sniffer.feed(payload[i : i + chunk])
    sniffer.finish()


CHAT_SSE = (
    b'data: {"id":"1","model":"grok-4.5","choices":[{"delta":{"content":"he"}}]}\n\n'
    b'data: {"id":"1","model":"grok-4.5","choices":[{"delta":{"content":"llo"}}]}\n\n'
    b'data: {"id":"1","model":"grok-4.5","choices":[],'
    b'"usage":{"prompt_tokens":40213,"completion_tokens":812,"total_tokens":41025,'
    b'"prompt_tokens_details":{"cached_tokens":38912},'
    b'"completion_tokens_details":{"reasoning_tokens":256}}}\n\n'
    b"data: [DONE]\n\n"
)

RESPONSES_SSE = (
    b'event: response.output_text.delta\n'
    b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
    b'event: response.completed\n'
    b'data: {"type":"response.completed","response":{"model":"grok-4.5",'
    b'"usage":{"input_tokens":1200,"output_tokens":34,"total_tokens":1234,'
    b'"input_tokens_details":{"cached_tokens":1024},'
    b'"output_tokens_details":{"reasoning_tokens":8}}}}\n\n'
    b"data: [DONE]\n\n"
)


@pytest.mark.parametrize("chunk", [1, 7, 64, 4096])
def test_chat_completions_sse_usage_survives_any_chunking(chunk):
    sniffer = spend_log.UsageSniffer(is_sse=True)
    _feed(sniffer, CHAT_SSE, chunk=chunk)
    assert sniffer.usage == {
        "input_tokens": 40213,
        "output_tokens": 812,
        "cached_tokens": 38912,
        "reasoning_tokens": 256,
        "total_tokens": 41025,
    }
    assert sniffer.model == "grok-4.5"
    assert sniffer.bytes_seen == len(CHAT_SSE)


@pytest.mark.parametrize("chunk", [1, 13, 4096])
def test_responses_sse_usage_is_read_from_nested_response(chunk):
    sniffer = spend_log.UsageSniffer(is_sse=True)
    _feed(sniffer, RESPONSES_SSE, chunk=chunk)
    assert sniffer.usage["input_tokens"] == 1200
    assert sniffer.usage["output_tokens"] == 34
    assert sniffer.usage["cached_tokens"] == 1024
    assert sniffer.usage["reasoning_tokens"] == 8


def test_non_streamed_json_body():
    body = json.dumps(
        {
            "model": "grok-4.5",
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }
    ).encode()
    sniffer = spend_log.UsageSniffer(is_sse=False)
    _feed(sniffer, body, chunk=5)
    assert sniffer.usage["input_tokens"] == 10
    assert sniffer.usage["output_tokens"] == 2
    assert sniffer.usage["cached_tokens"] is None


def test_missing_usage_degrades_quietly():
    sniffer = spend_log.UsageSniffer(is_sse=True)
    _feed(sniffer, b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n', chunk=3)
    assert sniffer.usage is None


def test_malformed_json_never_raises():
    sniffer = spend_log.UsageSniffer(is_sse=True)
    _feed(sniffer, b'data: {"usage": NOT JSON\n\ndata: [DONE]\n\n', chunk=4)
    assert sniffer.usage is None


def test_oversized_json_body_does_not_buffer():
    sniffer = spend_log.UsageSniffer(is_sse=False)
    blob = b"x" * (spend_log._MAX_JSON_BUFFER + 10)
    sniffer.feed(blob)
    sniffer.finish()
    assert sniffer.usage is None
    assert sniffer.bytes_seen == len(blob)


def test_build_record_shape_and_unknown_agent_fallback():
    record = spend_log.build_record(
        agent_id=None,
        upstream_base_url="https://api.x.ai/v1",
        path="/chat/completions",
        model="grok-4.5",
        stream=True,
        status=200,
        credential_label="some-account",
        usage={
            "input_tokens": 5,
            "output_tokens": 6,
            "cached_tokens": 1,
            "reasoning_tokens": 2,
            "total_tokens": 11,
        },
        duration_ms=42,
    )
    assert record["agent_id"] == "unknown"
    assert record["upstream_host"] == "api.x.ai"
    assert record["usage_found"] is True
    assert record["input_tokens"] == 5
    assert record["credential_label"] == "some-account"
    # The record must be JSON-serializable on one line.
    assert "\n" not in json.dumps(record)


def test_write_record_appends_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv(spend_log.SPEND_DIR_ENV, str(tmp_path / "spend"))
    spend_log.write_record({"ts": "now", "agent_id": "super_agente"})
    spend_log.write_record({"ts": "now", "agent_id": "tooly"})
    files = list((tmp_path / "spend").glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text().strip().splitlines()
    assert [json.loads(x)["agent_id"] for x in lines] == ["super_agente", "tooly"]


# --- loopback exemption ----------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.1.2.3", "localhost", "LOCALHOST", "::1", "[::1]"],
)
def test_loopback_hosts_recognized(host):
    assert _is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "",
        None,
        "0.0.0.0",
        "192.168.1.10",
        "10.0.0.1",
        "127.0.0.1.evil.com",
        "localhost.evil.com",
        "api.x.ai",
    ],
)
def test_non_loopback_hosts_rejected(host):
    assert _is_loopback_host(host) is False


def test_local_proxy_base_url_is_accepted_over_plain_http():
    assert (
        _xai_validate_inference_base_url(
            "http://127.0.0.1:8645/v1", fallback=DEFAULT_XAI
        )
        == "http://127.0.0.1:8645/v1"
    )


def test_remote_http_override_still_refused():
    assert (
        _xai_validate_inference_base_url(
            "http://attacker.example/v1", fallback=DEFAULT_XAI
        )
        == DEFAULT_XAI
    )
    assert (
        _xai_validate_inference_base_url(
            "https://attacker.example/v1", fallback=DEFAULT_XAI
        )
        == DEFAULT_XAI
    )
    # A private-LAN address is NOT loopback and must not be exempted.
    assert (
        _xai_validate_inference_base_url(
            "http://192.168.1.5:8645/v1", fallback=DEFAULT_XAI
        )
        == DEFAULT_XAI
    )
