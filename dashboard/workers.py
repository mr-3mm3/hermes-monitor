"""Pure worker-state transformations for Hermes Monitor."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any


def hash_arguments(arguments: Any) -> str:
    """Return a stable, non-reversible identifier for tool arguments."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            pass
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def is_repeated_tool_loop(events: Iterable[Mapping[str, Any]], minimum: int = 4) -> bool:
    """Detect an identical run at the tail of the recent call sequence."""
    recent = list(events)
    if len(recent) < minimum:
        return False
    last = recent[-1]
    name = str(last.get("tool_name") or "")
    digest = str(last.get("arguments_hash") or "")
    if not name or not digest:
        return False
    signature = (name, digest)
    run_length = 0
    for event in reversed(recent):
        current = (str(event.get("tool_name") or ""), str(event.get("arguments_hash") or ""))
        if current != signature:
            break
        run_length += 1
    return run_length >= minimum


def build_workers_response(
    tasks: Iterable[Mapping[str, Any]],
    activity_by_card: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    now: int,
) -> dict[str, Any]:
    """Build the public workers payload without exposing event contents."""
    workers: list[dict[str, Any]] = []
    for task in tasks:
        card_id = str(task["id"])
        started_at = int(task.get("started_at") or now)
        events = sorted(activity_by_card.get(card_id, ()), key=lambda event: float(event.get("timestamp") or 0))
        last_timestamp = float(events[-1].get("timestamp") or 0) if events else None
        last_activity_s = max(0, int(now - last_timestamp)) if last_timestamp is not None else None
        if is_repeated_tool_loop(events):
            state = "loop"
        elif last_activity_s is not None and last_activity_s > 300:
            state = "stalled"
        else:
            state = "active"
        workers.append(
            {
                "card_id": card_id,
                "title": str(task.get("title") or ""),
                "assignee": str(task.get("assignee") or ""),
                "running_since": started_at,
                "duration_s": max(0, now - started_at),
                "last_activity_s": last_activity_s,
                "state": state,
                "kanban_url": task.get("kanban_url") if isinstance(task.get("kanban_url"), str) else None,
            }
        )
    return {"generated_at": int(now), "count": len(workers), "workers": workers}
