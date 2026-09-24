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
        return {"id": "claude", "label": "Claude", "status": "n/a", "windows": []}
    indexed = {str(_value(item, "label", "")).lower().replace("-", "_").replace(" ", "_"): item for item in _windows(source)}
    # agent.account_usage labels Claude windows "Current session" (five_hour) and "Current week" (seven_day)
    five = next((item for key, item in indexed.items() if key in {"five_hour", "5_hour", "5h", "current_session", "session"}), None)
    week = next((item for key, item in indexed.items() if key in {"seven_day", "7_day", "week", "weekly", "current_week"}), None)
    normalized = [_window(five, "5h"), _window(week, "week")]
    if any(item is None for item in normalized):
        return {"id": "claude", "label": "Claude", "status": "n/a", "windows": []}
    return {"id": "claude", "label": "Claude", "status": "ok", "windows": normalized}


def _codex(source: Any) -> dict[str, Any]:
    if source is None or isinstance(source, BaseException):
        return {"id": "codex", "label": "Codex", "status": "n/a", "windows": []}
    normalized = [_window(item) for item in _windows(source)]
    if not normalized or any(item is None for item in normalized):
        return {"id": "codex", "label": "Codex", "status": "n/a", "windows": []}
    return {"id": "codex", "label": "Codex", "status": "ok", "windows": normalized}


def _deepseek(source: Any) -> dict[str, Any]:
    unavailable = {"id": "deepseek", "label": "DeepSeek", "status": "n/a", "balance": None, "windows": []}
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


def _generic(source: Any, provider_id: str, label: str) -> dict[str, Any]:
    unavailable = {"id": provider_id, "label": label, "status": "n/a", "windows": []}
    if source is None or isinstance(source, BaseException):
        return unavailable
    normalized = [_window(item) for item in _windows(source)]
    if not normalized or any(item is None for item in normalized):
        return unavailable
    return {"id": provider_id, "label": label, "status": "ok", "windows": normalized}


def _account(raw: Any, transform: Any, index: int) -> dict[str, Any]:
    metadata = raw if isinstance(raw, Mapping) and "source" in raw else {"source": raw, "account_label": "default", "active": True}
    source = metadata.get("source")
    normalized = transform(source)
    label = metadata.get("account_label")
    if not isinstance(label, str) or not label.strip() or "@" in label:
        label = f"account {index + 1}"
    plan = _value(source, "plan")
    if not isinstance(plan, str) or "@" in plan or len(plan) > 80:
        plan = None
    return {
        "account_label": label,
        "plan": plan,
        "active": bool(metadata.get("active")),
        "status": normalized["status"],
        "windows": normalized.get("windows", []),
        "balance": normalized.get("balance"),
    }


def _provider(source: Any, transform: Any, *, key_source: bool = False) -> dict[str, Any]:
    envelope = source if isinstance(source, Mapping) and isinstance(source.get("accounts"), list) else None
    raw_accounts = envelope["accounts"] if envelope is not None else [source]
    accounts = [_account(raw, transform, index) for index, raw in enumerate(raw_accounts)]
    active = next((account for account in accounts if account["active"]), None)
    identity = transform(None)
    result = {
        "id": identity["id"],
        "label": identity["label"],
        "status": active["status"] if active else "n/a",
        "account_label": active["account_label"] if active else None,
        "plan": active["plan"] if active else None,
        "pool_size": int(envelope.get("pool_size", 0)) if envelope is not None else 0,
        "windows": active["windows"] if active else [],
        "accounts": accounts,
    }
    if identity["id"] == "deepseek":
        result["balance"] = active["balance"] if active else None
    if key_source:
        source_name = envelope.get("key_source") if envelope is not None else None
        result["key_source"] = source_name if isinstance(source_name, str) and "@" not in source_name else None
    return result


def build_quotas_response(sources: Mapping[str, Any], *, now: int) -> dict[str, Any]:
    """Build a secret-free payload for every active remote provider."""
    known = {
        "anthropic": (_claude, False),
        "openai-codex": (_codex, False),
        "deepseek": (_deepseek, True),
    }
    known_rank = {"anthropic": 0, "openai-codex": 1, "deepseek": 2}
    providers = []
    for provider_id in sorted(sources, key=lambda item: (known_rank.get(item, 3), item)):
        source = sources.get(provider_id)
        envelope = source if isinstance(source, Mapping) else {}
        if provider_id in known:
            transform, include_key_source = known[provider_id]
        else:
            raw_label = envelope.get("provider_label")
            label = (
                raw_label
                if isinstance(raw_label, str) and raw_label.strip() and "@" not in raw_label
                else provider_id.replace("-", " ").title()
            )
            transform = lambda value, item=provider_id, name=label[:80]: _generic(value, item, name)
            include_key_source = False
        providers.append(_provider(source, transform, key_source=include_key_source))
    return {
        "generated_at": int(now),
        "providers": providers,
    }
