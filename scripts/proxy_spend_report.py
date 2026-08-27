#!/usr/bin/env python3
"""Aggregate Hermes proxy spend JSONL: tokens by agent, by hour, by model.

The proxy writes one record per forwarded request to
``$HERMES_PROXY_SPEND_DIR`` (default ``~/.hermes/proxy-spend``) as
``YYYY-MM-DD.jsonl``. This reads those files and answers the question the
2026-07-28 xAI incident could not: *which agent burned the tokens, and when?*

Usage:
    proxy_spend_report.py                        # today, by agent
    proxy_spend_report.py --by agent,hour        # the incident view
    proxy_spend_report.py --by model
    proxy_spend_report.py --by agent,credential
    proxy_spend_report.py --date 2026-07-28
    proxy_spend_report.py --since 2026-07-25 --until 2026-07-28
    proxy_spend_report.py --dir /var/log/hermes/proxy-spend --json

Group keys: agent, hour, day, model, credential, host, status, path.

Only reads. Stdlib only, so it runs under the same interpreter as the proxy on
every host (mini launchd venv, Sales systemd venv, Ops container python3.11).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

DEFAULT_DIR = "~/.hermes/proxy-spend"

_KEY_FIELDS = {
    "agent": lambda r: r.get("agent_id") or "unknown",
    "model": lambda r: r.get("model") or "-",
    "credential": lambda r: r.get("credential_label") or "-",
    "host": lambda r: r.get("upstream_host") or "-",
    "status": lambda r: str(r.get("status") if r.get("status") is not None else "-"),
    "path": lambda r: r.get("path") or "-",
    "hour": lambda r: (r.get("ts") or "")[:13],  # YYYY-MM-DDTHH
    "day": lambda r: (r.get("ts") or "")[:10],
}


def _iter_records(paths: Iterable[Path]) -> Iterable[Dict[str, Any]]:
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except Exception:
                        continue
        except OSError as exc:
            print(f"warn: cannot read {path}: {exc}", file=sys.stderr)


def _select_files(directory: Path, args) -> List[Path]:
    if args.date:
        return [directory / f"{args.date}.jsonl"]
    if args.since or args.until:
        since = args.since or "0000-00-00"
        until = args.until or "9999-99-99"
        return sorted(
            p for p in directory.glob("*.jsonl") if since <= p.stem <= until
        )
    if args.all:
        return sorted(directory.glob("*.jsonl"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    candidate = directory / f"{today}.jsonl"
    if candidate.exists():
        return [candidate]
    # Fall back to the newest file so an empty "today" is not a dead end.
    files = sorted(directory.glob("*.jsonl"))
    return files[-1:] if files else []


def _num(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def aggregate(records: Iterable[Dict[str, Any]], group_by: List[str]):
    buckets: Dict[Tuple[str, ...], Dict[str, int]] = {}
    for rec in records:
        key = tuple(_KEY_FIELDS[g](rec) for g in group_by)
        row = buckets.setdefault(
            key,
            {
                "calls": 0,
                "input": 0,
                "output": 0,
                "cached": 0,
                "reasoning": 0,
                "total": 0,
                "no_usage": 0,
                "errors": 0,
            },
        )
        row["calls"] += 1
        row["input"] += _num(rec.get("input_tokens"))
        row["output"] += _num(rec.get("output_tokens"))
        row["cached"] += _num(rec.get("cached_tokens"))
        row["reasoning"] += _num(rec.get("reasoning_tokens"))
        row["total"] += _num(rec.get("total_tokens"))
        if not rec.get("usage_found"):
            row["no_usage"] += 1
        status = rec.get("status")
        if isinstance(status, int) and status >= 400:
            row["errors"] += 1
    return buckets


def _print_table(buckets, group_by: List[str], sort_key: str, limit: int) -> None:
    if not buckets:
        print("(no records)")
        return
    rows = sorted(
        buckets.items(), key=lambda kv: kv[1].get(sort_key, 0), reverse=True
    )
    if limit:
        rows = rows[:limit]

    headers = list(group_by) + [
        "calls",
        "input",
        "cached",
        "output",
        "reasoning",
        "total",
        "errs",
        "nousage",
    ]
    table = [headers]
    for key, row in rows:
        table.append(
            list(key)
            + [
                f"{row['calls']:,}",
                f"{row['input']:,}",
                f"{row['cached']:,}",
                f"{row['output']:,}",
                f"{row['reasoning']:,}",
                f"{row['total']:,}",
                str(row["errors"]),
                str(row["no_usage"]),
            ]
        )
    widths = [max(len(r[i]) for r in table) for i in range(len(headers))]
    for idx, row in enumerate(table):
        line = "  ".join(
            cell.ljust(widths[i]) if i < len(group_by) else cell.rjust(widths[i])
            for i, cell in enumerate(row)
        )
        print(line)
        if idx == 0:
            print("  ".join("-" * w for w in widths))

    totals = {
        k: sum(v[k] for v in buckets.values())
        for k in ("calls", "input", "cached", "output", "total")
    }
    print(
        f"\nTOTAL  calls={totals['calls']:,}  input={totals['input']:,}  "
        f"cached={totals['cached']:,}  output={totals['output']:,}  "
        f"grand_total={totals['total']:,}"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--dir", default=None, help=f"spend log directory (default {DEFAULT_DIR})"
    )
    parser.add_argument("--by", default="agent", help="comma-separated group keys")
    parser.add_argument("--date", help="single YYYY-MM-DD file")
    parser.add_argument("--since", help="inclusive YYYY-MM-DD lower bound")
    parser.add_argument("--until", help="inclusive YYYY-MM-DD upper bound")
    parser.add_argument("--all", action="store_true", help="every day on disk")
    parser.add_argument("--agent", help="filter to one agent id")
    parser.add_argument("--model", help="filter to one model")
    parser.add_argument(
        "--last-hours", type=float, help="only records newer than N hours"
    )
    parser.add_argument(
        "--sort", default="total", help="sort column (total/input/output/calls)"
    )
    parser.add_argument("--limit", type=int, default=40, help="max rows (0 = all)")
    parser.add_argument("--json", action="store_true", help="emit JSON, not a table")
    args = parser.parse_args(argv)

    group_by = [g.strip() for g in args.by.split(",") if g.strip()]
    unknown = [g for g in group_by if g not in _KEY_FIELDS]
    if unknown:
        parser.error(
            f"unknown group key(s): {', '.join(unknown)}. "
            f"Valid: {', '.join(sorted(_KEY_FIELDS))}"
        )

    import os

    directory = Path(
        args.dir or os.environ.get("HERMES_PROXY_SPEND_DIR") or DEFAULT_DIR
    ).expanduser()
    if not directory.is_dir():
        print(f"no spend log directory at {directory}", file=sys.stderr)
        return 1

    files = _select_files(directory, args)
    if not files:
        print(f"no spend log files in {directory}", file=sys.stderr)
        return 1

    cutoff = None
    if args.last_hours:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=args.last_hours)
        ).isoformat()

    def _filtered():
        for rec in _iter_records(files):
            if args.agent and rec.get("agent_id") != args.agent:
                continue
            if args.model and rec.get("model") != args.model:
                continue
            if cutoff and (rec.get("ts") or "") < cutoff:
                continue
            yield rec

    buckets = aggregate(_filtered(), group_by)

    if args.json:
        print(
            json.dumps(
                [
                    {**dict(zip(group_by, key)), **row}
                    for key, row in sorted(
                        buckets.items(),
                        key=lambda kv: kv[1].get(args.sort, 0),
                        reverse=True,
                    )
                ],
                indent=2,
            )
        )
        return 0

    print(f"# {', '.join(str(f) for f in files)}")
    print(f"# grouped by: {' / '.join(group_by)}\n")
    _print_table(buckets, group_by, args.sort, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
