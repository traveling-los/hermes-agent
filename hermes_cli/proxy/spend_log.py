"""Per-request spend attribution for the credential proxy.

The proxy is a credential-attaching forwarder: it never buffered or parsed
response bodies, so xAI's trailing ``usage`` block was discarded and spend
could not be attributed to a caller. This module adds a *tee* that observes
the bytes already being streamed to the client and, at end-of-stream, writes
one structured JSONL record per request.

Design constraints (see docs/xai-spend-remediation.md):

  - **Never stall or alter the stream.** The server writes each chunk to the
    client first, then hands the same bytes here. Everything in this module is
    best-effort and wrapped so an exception can never propagate into the
    forwarding path.
  - **Bounded memory.** SSE responses are scanned line-by-line and only lines
    that actually contain ``"usage"`` are JSON-parsed; nothing is retained.
    Non-streamed JSON bodies are buffered only up to ``_MAX_JSON_BUFFER``.
  - **No secrets.** Only the credential *label* (account name) is recorded,
    never a bearer, and no request/response content is stored.

Record shape (one JSON object per line, file ``<dir>/YYYY-MM-DD.jsonl``)::

    {"ts": "2026-07-28T21:04:11.512Z", "agent_id": "super_agente",
     "upstream_host": "api.x.ai", "path": "/chat/completions",
     "model": "grok-4.5", "stream": true, "status": 200,
     "credential_label": "carlos-personal", "input_tokens": 40213,
     "output_tokens": 812, "cached_tokens": 38912, "reasoning_tokens": 256,
     "total_tokens": 41025, "duration_ms": 9412, "usage_found": true}
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Header each Hermes agent sets so the proxy can attribute the call. Stripped
# before forwarding upstream — xAI has no use for it and it is not part of any
# provider API.
AGENT_ID_HEADER = "X-Hermes-Agent-Id"
AGENT_ID_HEADER_LOWER = AGENT_ID_HEADER.lower()

UNKNOWN_AGENT = "unknown"

# Env override for the log directory (containers bind-mount elsewhere).
SPEND_DIR_ENV = "HERMES_PROXY_SPEND_DIR"
_DEFAULT_SPEND_DIR = "~/.hermes/proxy-spend"

# Non-streamed JSON responses are buffered so the body can be parsed once at
# EOF. 4 MB is far above any completion payload; past the cap we stop
# retaining bytes and simply report ``usage_found: false`` rather than grow
# without bound.
_MAX_JSON_BUFFER = 4_000_000
# Guard against a pathological SSE stream with no newlines.
_MAX_PENDING_LINE = 2_000_000

_write_lock = threading.Lock()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def spend_dir() -> Path:
    raw = (os.environ.get(SPEND_DIR_ENV) or "").strip() or _DEFAULT_SPEND_DIR
    return Path(raw).expanduser()


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def normalize_usage(usage: Any) -> Optional[Dict[str, Optional[int]]]:
    """Flatten an OpenAI/xAI ``usage`` block into a stable shape.

    Handles both dialects the proxy forwards:

      - ``/chat/completions``: ``prompt_tokens`` / ``completion_tokens`` with
        ``prompt_tokens_details.cached_tokens``
      - ``/responses``: ``input_tokens`` / ``output_tokens`` with
        ``input_tokens_details.cached_tokens``

    Returns ``None`` when the block carries no token counts at all.
    """
    if not isinstance(usage, dict):
        return None

    def _details(*keys: str) -> Dict[str, Any]:
        for key in keys:
            val = usage.get(key)
            if isinstance(val, dict):
                return val
        return {}

    in_details = _details("prompt_tokens_details", "input_tokens_details")
    out_details = _details("completion_tokens_details", "output_tokens_details")

    input_tokens = _as_int(usage.get("input_tokens"))
    if input_tokens is None:
        input_tokens = _as_int(usage.get("prompt_tokens"))
    output_tokens = _as_int(usage.get("output_tokens"))
    if output_tokens is None:
        output_tokens = _as_int(usage.get("completion_tokens"))
    total_tokens = _as_int(usage.get("total_tokens"))

    cached_tokens = _as_int(in_details.get("cached_tokens"))
    if cached_tokens is None:
        cached_tokens = _as_int(usage.get("cached_tokens"))
    reasoning_tokens = _as_int(out_details.get("reasoning_tokens"))
    if reasoning_tokens is None:
        reasoning_tokens = _as_int(usage.get("reasoning_tokens"))

    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }


def _extract_usage(payload: Any) -> Optional[Dict[str, Optional[int]]]:
    """Pull a usage block out of a decoded event/response payload."""
    if not isinstance(payload, dict):
        return None
    direct = normalize_usage(payload.get("usage"))
    if direct is not None:
        return direct
    # /responses SSE wraps the final object: {"type": "response.completed",
    # "response": {..., "usage": {...}}}
    nested = payload.get("response")
    if isinstance(nested, dict):
        return normalize_usage(nested.get("usage"))
    return None


class UsageSniffer:
    """Observes streamed response bytes and recovers the trailing usage block.

    Feed every chunk the client receives via :meth:`feed` (after it has been
    written to the client), then call :meth:`finish` once at end-of-stream.
    All methods swallow their own errors: a malformed body must degrade to
    ``usage_found: false``, never to a broken response.
    """

    def __init__(self, *, is_sse: bool) -> None:
        self._is_sse = is_sse
        self._pending = b""
        self._json_buf = bytearray()
        self._json_overflow = False
        self._usage: Optional[Dict[str, Optional[int]]] = None
        self._model: Optional[str] = None
        self.bytes_seen = 0

    # -- ingestion ----------------------------------------------------------
    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        try:
            self.bytes_seen += len(chunk)
            if self._is_sse:
                self._feed_sse(chunk)
            else:
                if not self._json_overflow:
                    if len(self._json_buf) + len(chunk) > _MAX_JSON_BUFFER:
                        self._json_overflow = True
                        self._json_buf = bytearray()
                    else:
                        self._json_buf.extend(chunk)
        except Exception:  # pragma: no cover - defensive
            logger.debug("proxy spend: usage sniff failed on chunk", exc_info=True)

    def _feed_sse(self, chunk: bytes) -> None:
        self._pending += chunk
        if b"\n" not in self._pending:
            if len(self._pending) > _MAX_PENDING_LINE:
                self._pending = b""
            return
        lines = self._pending.split(b"\n")
        self._pending = lines.pop()
        for line in lines:
            self._consume_sse_line(line)

    def _consume_sse_line(self, line: bytes) -> None:
        # Only lines that mention usage are worth decoding — this keeps the
        # tee to a substring check for the overwhelming majority of deltas.
        if b'"usage"' not in line:
            return
        stripped = line.strip()
        if stripped.startswith(b"data:"):
            stripped = stripped[5:].strip()
        if not stripped or stripped == b"[DONE]":
            return
        try:
            payload = json.loads(stripped.decode("utf-8", "replace"))
        except Exception:
            return
        self._absorb(payload)

    def _absorb(self, payload: Any) -> None:
        usage = _extract_usage(payload)
        if usage is not None:
            # Later blocks win: /responses emits cumulative usage and the
            # completed event carries the final totals.
            self._usage = usage
        if isinstance(payload, dict):
            model = payload.get("model")
            if isinstance(model, str) and model:
                self._model = model

    # -- completion ---------------------------------------------------------
    def finish(self) -> None:
        try:
            if self._is_sse:
                if self._pending:
                    self._consume_sse_line(self._pending)
                    self._pending = b""
                return
            if self._json_overflow or not self._json_buf:
                return
            payload = json.loads(bytes(self._json_buf).decode("utf-8", "replace"))
            self._absorb(payload)
        except Exception:
            logger.debug("proxy spend: usage sniff failed at EOF", exc_info=True)
        finally:
            self._json_buf = bytearray()

    @property
    def usage(self) -> Optional[Dict[str, Optional[int]]]:
        return self._usage

    @property
    def model(self) -> Optional[str]:
        return self._model


def request_model(body: bytes) -> Optional[str]:
    """Best-effort ``model`` from the request body (cheap, always available)."""
    if not body or len(body) > _MAX_JSON_BUFFER:
        return None
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    return model if isinstance(model, str) and model else None


def request_is_stream(body: bytes) -> Optional[bool]:
    if not body or len(body) > _MAX_JSON_BUFFER:
        return None
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("stream")
    return bool(value) if isinstance(value, bool) else None


def upstream_host(base_url: str) -> Optional[str]:
    try:
        return (urlparse(base_url).hostname or "").lower() or None
    except Exception:
        return None


def write_record(record: Dict[str, Any]) -> None:
    """Append one JSONL record. Never raises."""
    try:
        directory = spend_dir()
        directory.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = directory / f"{day}.jsonl"
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _write_lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
    except Exception:
        logger.debug("proxy spend: failed to write spend record", exc_info=True)


def build_record(
    *,
    agent_id: Optional[str],
    upstream_base_url: str,
    path: str,
    model: Optional[str],
    stream: Optional[bool],
    status: int,
    credential_label: Optional[str],
    usage: Optional[Dict[str, Optional[int]]],
    duration_ms: Optional[int],
    response_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "ts": _utcnow_iso(),
        "agent_id": (agent_id or UNKNOWN_AGENT).strip() or UNKNOWN_AGENT,
        "upstream_host": upstream_host(upstream_base_url),
        "path": path,
        "model": model,
        "stream": stream,
        "status": status,
        "credential_label": credential_label,
        "usage_found": usage is not None,
        "input_tokens": None,
        "output_tokens": None,
        "cached_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
        "duration_ms": duration_ms,
    }
    if usage:
        record.update(usage)
    if response_bytes is not None:
        record["response_bytes"] = response_bytes
    return record


__all__ = [
    "AGENT_ID_HEADER",
    "AGENT_ID_HEADER_LOWER",
    "UNKNOWN_AGENT",
    "SPEND_DIR_ENV",
    "UsageSniffer",
    "build_record",
    "normalize_usage",
    "request_is_stream",
    "request_model",
    "spend_dir",
    "upstream_host",
    "write_record",
]
