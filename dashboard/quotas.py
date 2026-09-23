"""Pure account-usage transformations for Hermes Monitor."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _epoch(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp())
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _window(raw: Any, label: str | None = None) -> dict[str, Any] | None:
    source_label = _value(raw, "label")
    used = _value(raw, "used_percent")
    try:
        percent = float(used)
    except (TypeError, ValueError, OverflowError):
        return None
    return {"label": label or str(source_label or ""), "used_percent": percent, "reset_at": _epoch(_value(raw, "reset_at"))}


def _windows(snapshot: Any) -> list[Any]:
    raw = _value(snapshot, "windows", [])
    if isinstance(raw, Mapping):
        return [dict(value, label=_value(value, "label", key)) if isinstance(value, Mapping) else value for key, value in raw.items()]
    return list(raw) if isinstance(raw, (list, tuple)) else []


def _claude(source: Any) -> dict[str, Any]:
    if source is None or isinstance(source, BaseException):
        return {"id": "claude", "label": "Claude", "status": "n/d", "windows": []}
    indexed = {str(_value(item, "label", "")).lower().replace("-", "_").replace(" ", "_"): item for item in _windows(source)}
    # agent.account_usage labels Claude windows "Current session" (five_hour) and "Current week" (seven_day)
    five = next((item for key, item in indexed.items() if key in {"five_hour", "5_hour", "5h", "current_session", "session"}), None)
    week = next((item for key, item in indexed.items() if key in {"seven_day", "7_day", "week", "weekly", "current_week"}), None)
    normalized = [_window(five, "5h"), _window(week, "settimana")]
    if any(item is None for item in normalized):
        return {"id": "claude", "label": "Claude", "status": "n/d", "windows": []}
    return {"id": "claude", "label": "Claude", "status": "ok", "windows": normalized}


def _codex(source: Any) -> dict[str, Any]:
    if source is None or isinstance(source, BaseException):
        return {"id": "codex", "label": "Codex", "status": "n/d", "windows": []}
    normalized = [_window(item) for item in _windows(source)]
    if not normalized or any(item is None for item in normalized):
        return {"id": "codex", "label": "Codex", "status": "n/d", "windows": []}
    return {"id": "codex", "label": "Codex", "status": "ok", "windows": normalized}


def _deepseek(source: Any) -> dict[str, Any]:
    unavailable = {"id": "deepseek", "label": "DeepSeek", "status": "n/d", "balance": None, "windows": []}
    if source is None or isinstance(source, BaseException) or not isinstance(source, Mapping):
        return unavailable
    balances = source.get("balance_infos")
    if source.get("is_available") is False or not isinstance(balances, list) or not balances:
        return unavailable
    first = balances[0]
    if not isinstance(first, Mapping):
        return unavailable
    try:
        amount = float(first["total_balance"])
        currency = str(first["currency"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return unavailable
    return {"id": "deepseek", "label": "DeepSeek", "status": "ok", "balance": {"currency": currency, "amount": amount}, "windows": []}


def build_quotas_response(sources: Mapping[str, Any], *, now: int) -> dict[str, Any]:
    """Build the fixed public provider payload, dropping all source extras."""
    return {
        "generated_at": int(now),
        "providers": [_claude(sources.get("anthropic")), _codex(sources.get("openai-codex")), _deepseek(sources.get("deepseek"))],
    }
