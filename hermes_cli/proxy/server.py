"""HTTP server that forwards OpenAI-compatible requests to a configured upstream.

Listens on ``http://<host>:<port>/v1/<path>`` and forwards each request to
``<upstream-base-url>/<path>`` with the client's ``Authorization`` header
replaced by a freshly-resolved bearer from the configured adapter. The
response is streamed back unmodified, preserving SSE.

The server is intentionally minimal: it does NOT mediate, transform, or
rewrite request/response bodies. It's a credential-attaching forwarder.

The one thing it *does* observe is spend: each forwarded chunk is teed into a
:class:`~hermes_cli.proxy.spend_log.UsageSniffer` (after it has been written to
the client, so the stream is never stalled) to recover the trailing ``usage``
block, and one JSONL record per request is appended to the spend log. The
caller is identified by the ``X-Hermes-Agent-Id`` request header, which is
stripped before forwarding upstream. See ``hermes_cli/proxy/spend_log.py``.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Optional

try:
    import aiohttp
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential
from hermes_cli.proxy import spend_log

logger = logging.getLogger(__name__)

# Headers we strip when forwarding to the upstream. ``host``/``content-length``
# are recomputed by aiohttp; ``authorization`` is replaced with our bearer.
# Everything else (content-type, accept, user-agent, x-* headers) passes through.
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "authorization",  # we replace this one
    }
)

# Hermes-internal headers consumed by the proxy and NOT forwarded upstream.
# ``X-Hermes-Agent-Id`` is how a caller names itself for spend attribution;
# xAI has no use for it and it is not part of any provider API.
_HERMES_INTERNAL_HEADERS = frozenset({spend_log.AGENT_ID_HEADER_LOWER})

# Endpoints whose responses carry a ``usage`` block worth recording. Sniffing
# /models or /embeddings would just be overhead.
_SPEND_TRACKED_PATHS = frozenset({"/chat/completions", "/responses", "/completions"})

DEFAULT_PORT = 8645
DEFAULT_HOST = "127.0.0.1"
# Body cap for forwarded requests. Chat-completion payloads with long agent
# conversations can be large; mirror api_server's MAX_REQUEST_BYTES (10 MB).
# client_max_size bounds every read path, including chunked bodies.
MAX_REQUEST_BYTES = 10_000_000


def _json_error(status: int, message: str, code: str = "proxy_error") -> "web.Response":
    """Return an OpenAI-style error JSON response."""
    body = {"error": {"message": message, "type": code, "code": code}}
    return web.json_response(body, status=status)


def _filter_request_headers(headers: "aiohttp.typedefs.LooseHeaders") -> dict:
    """Strip hop-by-hop + auth headers from the inbound request."""
    out = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in _HOP_BY_HOP_HEADERS or lowered in _HERMES_INTERNAL_HEADERS:
            continue
        out[key] = value
    return out


def _filter_response_headers(headers) -> dict:
    """Strip hop-by-hop headers from the upstream response."""
    out = {}
    for key, value in headers.items():
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        # aiohttp recomputes Content-Encoding/Content-Length on stream — let it.
        if key.lower() in {"content-encoding", "content-length"}:
            continue
        out[key] = value
    return out


def create_app(adapter: UpstreamAdapter) -> "web.Application":
    """Build the aiohttp application bound to a specific upstream adapter."""
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError(
            "aiohttp is required for `hermes proxy`. Run `hermes setup` to install it."
        )

    app = web.Application(client_max_size=MAX_REQUEST_BYTES)
    # AppKey ensures forward-compat with future aiohttp versions that strip
    # bare-string keys.
    _adapter_key = web.AppKey("adapter", UpstreamAdapter)
    app[_adapter_key] = adapter

    async def handle_health(request: "web.Request") -> "web.Response":
        return web.json_response(
            {
                "status": "ok",
                "upstream": adapter.display_name,
                "authenticated": adapter.is_authenticated(),
            }
        )

    async def handle_proxy(request: "web.Request") -> "web.StreamResponse":
        # Extract the path *after* /v1
        rel_path = request.match_info.get("tail", "")
        rel_path = "/" + rel_path.lstrip("/")

        started = time.monotonic()
        agent_id = (
            request.headers.get(spend_log.AGENT_ID_HEADER)
            or spend_log.UNKNOWN_AGENT
        )
        track_spend = rel_path in _SPEND_TRACKED_PATHS

        def _log_spend(
            *,
            status: int,
            cred_used: Optional[UpstreamCredential],
            model: Optional[str] = None,
            stream: Optional[bool] = None,
            usage=None,
            response_bytes: Optional[int] = None,
        ) -> None:
            """Append one spend record. Best-effort -- must never raise."""
            if not track_spend:
                return
            try:
                spend_log.write_record(
                    spend_log.build_record(
                        agent_id=agent_id,
                        upstream_base_url=(
                            cred_used.base_url if cred_used is not None else ""
                        ),
                        path=rel_path,
                        model=model,
                        stream=stream,
                        status=status,
                        credential_label=(
                            getattr(cred_used, "label", None)
                            if cred_used is not None
                            else None
                        ),
                        usage=usage,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        response_bytes=response_bytes,
                    )
                )
            except Exception:  # pragma: no cover - defensive
                logger.debug("proxy: spend logging failed", exc_info=True)

        if rel_path not in adapter.allowed_paths:
            allowed = ", ".join(sorted(adapter.allowed_paths))
            return _json_error(
                404,
                f"Path /v1{rel_path} is not forwarded by this proxy. "
                f"Allowed: {allowed}",
                code="path_not_allowed",
            )

        try:
            cred = adapter.get_credential()
        except Exception as exc:
            logger.warning("proxy: credential resolution failed: %s", exc)
            _log_spend(status=401, cred_used=None)
            return _json_error(401, str(exc), code="upstream_auth_failed")

        # Forward body verbatim. Read into memory once — request bodies for
        # chat/completions/embeddings are small (<1MB typically). If we ever
        # need to forward large multipart uploads we'll switch to streaming
        # the request body too.
        body = await request.read()

        req_model = spend_log.request_model(body) if track_spend else None
        req_stream = spend_log.request_is_stream(body) if track_spend else None

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=300)

        async def _send_upstream(active_cred: UpstreamCredential):
            upstream_url = f"{active_cred.base_url.rstrip('/')}{rel_path}"
            # Preserve query string verbatim.
            if request.query_string:
                upstream_url = f"{upstream_url}?{request.query_string}"

            fwd_headers = _filter_request_headers(request.headers)
            fwd_headers["Authorization"] = f"{active_cred.token_type} {active_cred.bearer}"

            logger.debug(
                "proxy: forwarding %s %s -> %s (body=%d bytes)",
                request.method, rel_path, upstream_url, len(body),
            )

            try:
                session = aiohttp.ClientSession(timeout=timeout)
            except Exception as exc:  # pragma: no cover - aiohttp setup issue
                raise RuntimeError(f"proxy session init failed: {exc}") from exc

            try:
                upstream_resp = await session.request(
                    request.method,
                    upstream_url,
                    data=body if body else None,
                    headers=fwd_headers,
                    allow_redirects=False,
                )
            except Exception:
                await session.close()
                raise
            return session, upstream_resp

        async def _open_upstream(active_cred: UpstreamCredential):
            try:
                return await _send_upstream(active_cred)
            except RuntimeError as exc:
                return _json_error(500, str(exc)), None
            except aiohttp.ClientError as exc:
                logger.warning("proxy: upstream connection failed: %s", exc)
                return (
                    _json_error(
                        502,
                        f"upstream connection failed: {exc}",
                        code="upstream_unreachable",
                    ),
                    None,
                )
            except asyncio.TimeoutError:
                return (
                    _json_error(
                        504,
                        "upstream request timed out",
                        code="upstream_timeout",
                    ),
                    None,
                )

        active_cred = cred
        session_or_response, upstream_resp = await _open_upstream(cred)
        if upstream_resp is None:
            _log_spend(
                status=session_or_response.status,
                cred_used=cred,
                model=req_model,
                stream=req_stream,
            )
            return session_or_response
        session = session_or_response

        if upstream_resp.status in {401, 403, 429}:  # 403: xAI spending-limit block
            try:
                retry_cred = adapter.get_retry_credential(
                    failed_credential=cred,
                    status_code=upstream_resp.status,
                )
            except Exception as exc:
                logger.warning("proxy: retry credential resolution failed: %s", exc)
                retry_cred = None

            if retry_cred is not None:
                # The first credential's failure is spend-relevant on its own:
                # it is how an account silently draining shows up in the log.
                _log_spend(
                    status=upstream_resp.status,
                    cred_used=active_cred,
                    model=req_model,
                    stream=req_stream,
                )
                upstream_resp.release()
                await session.close()
                active_cred = retry_cred
                session_or_response, upstream_resp = await _open_upstream(retry_cred)
                if upstream_resp is None:
                    _log_spend(
                        status=session_or_response.status,
                        cred_used=retry_cred,
                        model=req_model,
                        stream=req_stream,
                    )
                    return session_or_response
                session = session_or_response

        # Stream response back. Headers first, then chunked body.
        resp = web.StreamResponse(
            status=upstream_resp.status,
            headers=_filter_response_headers(upstream_resp.headers),
        )
        await resp.prepare(request)

        # Tee for spend attribution. Chunks are handed to the sniffer *after*
        # they have been written to the client, so observation can never add
        # latency to (or stall) the forwarded stream.
        sniffer = None
        if track_spend:
            content_type = (upstream_resp.headers.get("Content-Type") or "").lower()
            sniffer = spend_log.UsageSniffer(
                is_sse="text/event-stream" in content_type
            )

        try:
            async for chunk in upstream_resp.content.iter_any():
                if chunk:
                    await resp.write(chunk)
                    if sniffer is not None:
                        sniffer.feed(chunk)
        except (aiohttp.ClientError, asyncio.CancelledError) as exc:
            logger.warning("proxy: streaming interrupted: %s", exc)
        finally:
            upstream_resp.release()
            await session.close()

        # Finish the client response first; only then do the (blocking, tiny)
        # spend-log append, so the caller never waits on our bookkeeping.
        await resp.write_eof()

        if sniffer is not None:
            sniffer.finish()
            _log_spend(
                status=upstream_resp.status,
                cred_used=active_cred,
                model=sniffer.model or req_model,
                stream=req_stream,
                usage=sniffer.usage,
                response_bytes=sniffer.bytes_seen,
            )

        return resp

    # /health doesn't go through the upstream
    app.router.add_get("/health", handle_health)
    # Catch-all under /v1 — forwards if the path is allowed.
    app.router.add_route("*", "/v1/{tail:.*}", handle_proxy)

    return app


async def run_server(
    adapter: UpstreamAdapter,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    shutdown_event: Optional[asyncio.Event] = None,
) -> None:
    """Run the proxy in the current event loop until shutdown_event is set.

    If shutdown_event is None, runs until cancelled (Ctrl+C or SIGTERM).
    """
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError(
            "aiohttp is required for `hermes proxy`. Run `hermes setup` to install it."
        )

    app = create_app(adapter)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    logger.info(
        "proxy: listening on http://%s:%d/v1 -> %s",
        host, port, adapter.display_name,
    )

    stop_event = shutdown_event or asyncio.Event()

    # Wire signal handlers when we own the loop's lifetime.
    if shutdown_event is None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)  # windows-footgun: ok
            except NotImplementedError:
                # Windows / restricted environments — Ctrl+C will still
                # raise KeyboardInterrupt and unwind us.
                pass

    try:
        await stop_event.wait()
    finally:
        logger.info("proxy: shutting down")
        await runner.cleanup()


__all__ = [
    "create_app",
    "run_server",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "AIOHTTP_AVAILABLE",
]
