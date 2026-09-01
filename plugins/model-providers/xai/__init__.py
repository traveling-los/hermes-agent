"""xAI (Grok) provider profile."""

import os

from hermes_cli import __version__ as _HERMES_VERSION
from providers import register_provider
from providers.base import ProviderProfile


def _agent_id_headers() -> dict:
    """Caller-identity header for xAI spend attribution.

    Every Hermes agent on a host sends an identical ``User-Agent``/``X-Title``,
    so neither xAI nor the local Hermes credential proxy could tell which
    profile issued a request — which is why the 2026-07-28 xAI burn could not
    be attributed to any agent after the fact (docs/xai-spend-remediation.md).

    ``X-Hermes-Agent-Id`` carries the active profile name. The Hermes proxy
    logs it and **strips it before forwarding upstream**
    (``hermes_cli/proxy/server.py``), so xAI never receives it; when an agent
    talks to ``api.x.ai`` directly the header is inert metadata.

    Scoped to this provider profile on purpose: it reaches every xAI client via
    the ``profile.default_headers`` fallback that ``agent/agent_init.py``,
    ``run_agent._apply_client_headers_for_base_url`` and
    ``agent/auxiliary_client.resolve_provider_client`` already share, without
    changing header behaviour for any other provider.

    Resolved at import time, which is correct here: ``HERMES_HOME`` is set by
    launchd/systemd/docker before the process starts and a profile does not
    change within a process.

    Kill switch: ``HERMES_AGENT_ID_HEADER=0``. Never raises — attribution is a
    diagnostic and must not be able to break client construction.
    """
    try:
        if os.environ.get("HERMES_AGENT_ID_HEADER", "").strip().lower() in {
            "0",
            "false",
            "no",
            "off",
        }:
            return {}
        from hermes_cli.profiles import get_active_profile_name

        name = (get_active_profile_name() or "default").strip()
        # Header values must be single-line latin-1 tokens; profile names are
        # filesystem-derived, so sanitize defensively.
        safe = "".join(c for c in name if c.isalnum() or c in "-_.")[:64]
        return {"X-Hermes-Agent-Id": safe or "default"}
    except Exception:
        return {}


xai = ProviderProfile(
    name="xai",
    aliases=("grok", "x-ai", "x.ai"),
    api_mode="codex_responses",
    env_vars=("XAI_API_KEY",),
    base_url="https://api.x.ai/v1",
    auth_type="api_key",
    default_headers={
        "User-Agent": f"Hermes-Agent/{_HERMES_VERSION}",
        **_agent_id_headers(),
    },
)

register_provider(xai)
