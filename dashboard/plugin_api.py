"""Hermes Monitor dashboard API.

All databases are opened read-only. External usage calls are isolated behind
small wrappers and individually limited to 15 seconds.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import re
import sqlite3
import time
import urllib.request
from contextlib import closing
from pathlib import Path
from types import FunctionType
from typing import Any, Callable
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Query

try:
    from .quotas import build_quotas_response
    from .workers import build_workers_response, hash_arguments
except ImportError:  # Loaded by file path by the Hermes plugin loader: no package, no sys.path entry.
    import importlib.util as _ilu

    def _load_sibling(name: str) -> Any:
        spec = _ilu.spec_from_file_location(f"hermes_monitor_{name}", Path(__file__).with_name(f"{name}.py"))
        module = _ilu.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    build_quotas_response = _load_sibling("quotas").build_quotas_response
    _workers = _load_sibling("workers")
    build_workers_response, hash_arguments = _workers.build_workers_response, _workers.hash_arguments


router = APIRouter()
TIMEOUT_S = 15
CACHE_TTL_S = 300


class TTLCache:
    def __init__(self, ttl: int = CACHE_TTL_S) -> None:
        self.ttl = ttl
        self._value: dict[str, Any] | None = None
        self._stored_at = 0.0

    def clear(self) -> None:
        self._value = None
        self._stored_at = 0.0

    def get(self, *, now: float) -> dict[str, Any] | None:
        if self._value is None or now - self._stored_at >= self.ttl:
            return None
        return self._value

    def set(self, value: dict[str, Any], *, now: float) -> dict[str, Any]:
        self._value = value
        self._stored_at = now
        return value


_workers_cache = TTLCache()
_quotas_cache = TTLCache()
_workers_lock = asyncio.Lock()
_quotas_lock = asyncio.Lock()


def _readonly_connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=True)
    return sqlite3.connect(f"file:{quote(str(resolved), safe='/')}?mode=ro", uri=True)


def _hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".hermes"
    # A named profile points HERMES_HOME at ``<root>/profiles/<name>`` while
    # kanban and the profile roster remain rooted at ``<root>``.
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def _load_running_tasks() -> list[dict[str, Any]]:
    database = _hermes_home() / "kanban.db"
    with closing(_readonly_connection(database)) as connection:
        rows = connection.execute(
            "SELECT id, title, assignee, started_at FROM tasks WHERE status = ? ORDER BY started_at, id",
            ("running",),
        ).fetchall()
    return [{"id": row[0], "title": row[1], "assignee": row[2], "started_at": row[3], "kanban_url": None} for row in rows]


def _safe_profile_database(assignee: str) -> Path | None:
    if not assignee or assignee in {".", ".."} or Path(assignee).name != assignee:
        return None
    profiles = (_hermes_home() / "profiles").resolve()
    candidate = (profiles / assignee / "state.db").resolve()
    return candidate if candidate.is_relative_to(profiles) and candidate.is_file() else None


def _extract_calls(tool_calls: Any, timestamp: float) -> list[dict[str, Any]]:
    if isinstance(tool_calls, str):
        try:
            tool_calls = json.loads(tool_calls)
        except (TypeError, ValueError):
            return []
    if not isinstance(tool_calls, list):
        return []
    events = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else call
        name = function.get("name") or call.get("tool_name")
        if not name:
            continue
        events.append({"tool_name": str(name), "arguments_hash": hash_arguments(function.get("arguments", {})), "timestamp": timestamp})
    return events


def _load_tool_events(task: dict[str, Any]) -> list[dict[str, Any]]:
    database = _safe_profile_database(str(task.get("assignee") or ""))
    if database is None:
        return []
    started = float(task.get("started_at") or 0)
    with closing(_readonly_connection(database)) as connection:
        session = connection.execute(
            "SELECT id FROM sessions WHERE source = ? AND started_at >= ? ORDER BY ABS(started_at - ?) LIMIT 1",
            ("kanban", started - 60, started),
        ).fetchone()
        if session is None:
            return []
        rows = connection.execute(
            "SELECT tool_calls, timestamp FROM messages WHERE session_id = ? AND tool_calls IS NOT NULL "
            "AND active = 1 AND compacted = 0 ORDER BY timestamp DESC, id DESC LIMIT 200",
            (session[0],),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for tool_calls, timestamp in reversed(rows):
        events.extend(_extract_calls(tool_calls, float(timestamp)))
    return events


def _build_workers_payload() -> dict[str, Any]:
    now = int(time.time())
    tasks = _load_running_tasks()
    activity = {str(task["id"]): _load_tool_events(task) for task in tasks}
    return build_workers_response(tasks, activity, now=now)


def _active_pool_token(provider: str) -> str | None:
    """Token of the credential Hermes will use next for *provider* (pool order), if any.

    Without it the usage API reads the legacy singleton login, which is stale once the
    user adds a second account and moves it to priority 0.
    """
    try:
        entry = importlib.import_module("agent.credential_pool").load_pool(provider).peek()
    except Exception:
        return None
    return getattr(entry, "runtime_api_key", None) or getattr(entry, "access_token", None) if entry else None


def _fetch_account_usage(provider: str, api_key: str | None = None, use_pool: bool = True) -> Any:
    module = importlib.import_module("agent.account_usage")
    token = api_key or (_active_pool_token(provider) if use_pool else None)
    if provider == "anthropic" and token:
        # Hermes <=0.21 accepts api_key here but its Anthropic fetcher ignores it
        # and resolves the legacy singleton instead. Clone the Hermes fetcher with
        # an isolated resolver so concurrent gateway requests are never mutated.
        fetcher = getattr(module, "_fetch_anthropic_account_usage", None)
        if isinstance(fetcher, FunctionType):
            namespace = dict(fetcher.__globals__)
            namespace["resolve_anthropic_token"] = lambda: token
            isolated = FunctionType(
                fetcher.__code__, namespace, fetcher.__name__, fetcher.__defaults__, fetcher.__closure__
            )
            isolated.__kwdefaults__ = fetcher.__kwdefaults__
            result = isolated(api_key=token)
            return asyncio.run(result) if inspect.isawaitable(result) else result
    result = module.fetch_account_usage(provider, api_key=token) if token else module.fetch_account_usage(provider)
    return asyncio.run(result) if inspect.isawaitable(result) else result


def _entry_token(entry: Any) -> str | None:
    return getattr(entry, "runtime_api_key", None) or getattr(entry, "access_token", None)


def _safe_account_label(entry: Any, index: int, secrets: tuple[str, ...]) -> str:
    label = str(getattr(entry, "label", "") or "").strip()
    if not label or "@" in label or any(secret in label for secret in secrets):
        return f"account {index + 1}"
    return label[:80]


async def _collect_usage_provider(provider: str, pool: Any = None, label: str | None = None) -> dict[str, Any]:
    try:
        pool = pool or importlib.import_module("agent.credential_pool").load_pool(provider)
        active = pool.peek()
        expires_at_ms = getattr(active, "expires_at_ms", None) if active else None
        if (
            provider == "anthropic"
            and active is not None
            and getattr(active, "refresh_token", None)
            and isinstance(expires_at_ms, (int, float))
            and expires_at_ms <= time.time() * 1000 + 120_000
            and callable(getattr(pool, "select", None))
        ):
            active = pool.select()
        entries = sorted(pool.entries(), key=lambda entry: getattr(entry, "priority", 0))
    except Exception:
        entries, active = [], None

    if not entries:
        source = await _limited_call(_fetch_account_usage, provider, None, False)
        return {
            "pool_size": 0,
            "provider_label": label,
            "accounts": [{"account_label": "default", "active": True, "source": source}],
        }

    active_id = getattr(active, "id", None)
    secrets = tuple(
        secret
        for entry in entries
        for secret in (
            getattr(entry, "runtime_api_key", None),
            getattr(entry, "access_token", None),
            getattr(entry, "refresh_token", None),
        )
        if isinstance(secret, str) and secret
    )
    calls = []
    for entry in entries:
        token = _entry_token(entry)
        calls.append(_limited_call(_fetch_account_usage, provider, token, False) if token else asyncio.sleep(0, result=None))
    snapshots = await asyncio.gather(*calls)
    return {
        "pool_size": len(entries),
        "provider_label": label,
        "accounts": [
            {
                "account_label": _safe_account_label(entry, index, secrets),
                "active": bool(active is entry or (active_id is not None and getattr(entry, "id", None) == active_id)),
                "source": snapshots[index],
            }
            for index, entry in enumerate(entries)
        ],
    }


def _is_local_only_provider(config: Any) -> bool:
    urls = (
        getattr(config, "inference_base_url", None),
        getattr(config, "base_url", None),
        getattr(config, "portal_base_url", None),
    )
    remote_seen = False
    for value in urls:
        if not isinstance(value, str) or not value.strip():
            continue
        host = (urlparse(value).hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "::1"}:
            return True
        remote_seen = True
    return not remote_seen and getattr(config, "auth_type", None) == "external_process"


def _provider_label(config: Any, provider: str) -> str:
    for attribute in ("display_name", "name"):
        value = getattr(config, attribute, None)
        if isinstance(value, str) and value.strip() and "@" not in value:
            return value.strip()[:80]
    return provider.replace("-", " ").title()[:80]


def _read_env_key(path: Path, name: str) -> str | None:
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            candidate, value = line.split("=", 1)
            if candidate.strip() == name:
                return value.strip().strip("\"'") or None
    except OSError:
        return None
    return None


def _read_deepseek_credential() -> tuple[str | None, str | None]:
    environment_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if environment_key:
        return environment_key, "env"

    root = _hermes_home()
    root_key = _read_env_key(root / ".env", "DEEPSEEK_API_KEY")
    if root_key:
        return root_key, "root"

    try:
        profile_envs = sorted((root / "profiles").glob("*/.env"), key=lambda path: path.parent.name)
    except OSError:
        return None, None
    for path in profile_envs:
        profile_key = _read_env_key(path, "DEEPSEEK_API_KEY")
        if profile_key:
            profile = path.parent.name
            safe_profile = (
                profile
                if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", profile)
                and "@" not in profile
                and profile_key not in profile
                and profile not in profile_key
                else "profile"
            )
            return profile_key, safe_profile
    return None, None


def _read_deepseek_key() -> str | None:
    return _read_deepseek_credential()[0]


def _fetch_deepseek_balance(key: str | None = None) -> Any:
    key = key or _read_deepseek_key()
    if not key:
        raise RuntimeError("DeepSeek credentials unavailable")
    request = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


async def _limited_call(function: Callable[..., Any], *args: Any) -> Any:
    try:
        return await asyncio.wait_for(asyncio.to_thread(function, *args), timeout=TIMEOUT_S)
    except Exception:
        return None


async def _collect_quota_sources() -> dict[str, Any]:
    credential_module = importlib.import_module("agent.credential_pool")
    account_usage_module = importlib.import_module("agent.account_usage")
    registry = getattr(credential_module, "PROVIDER_REGISTRY", {})
    supported = set(getattr(account_usage_module, "_USAGE_FETCHERS", {}))
    deepseek_key, deepseek_source = _read_deepseek_credential()
    active: list[tuple[str, Any, str]] = []
    for provider in sorted(set(registry) | supported):
        config = registry.get(provider)
        if config is not None and _is_local_only_provider(config):
            continue
        try:
            pool = credential_module.load_pool(provider)
            if not pool.entries() and provider not in supported:
                continue
        except Exception:
            continue
        active.append((provider, pool, _provider_label(config, provider)))

    if deepseek_key and all(provider != "deepseek" for provider, _, _ in active):
        active.append(("deepseek", None, "DeepSeek"))

    known_rank = {"anthropic": 0, "openai-codex": 1, "deepseek": 2}
    active.sort(key=lambda item: (known_rank.get(item[0], 3), item[0]))
    sources: dict[str, Any] = {}
    for provider, pool, label in active:
        if provider == "deepseek":
            entries = sorted(pool.entries(), key=lambda entry: getattr(entry, "priority", 0)) if pool else []
            active_entry = pool.peek() if pool else None
            active_id = getattr(active_entry, "id", None)
            if entries:
                snapshots = await asyncio.gather(
                    *[_limited_call(_fetch_deepseek_balance, _entry_token(entry)) for entry in entries]
                )
                secrets = tuple(filter(None, (_entry_token(entry) for entry in entries)))
                accounts = [
                    {
                        "account_label": _safe_account_label(entry, index, secrets),
                        "active": bool(entry is active_entry or getattr(entry, "id", None) == active_id),
                        "source": snapshots[index],
                    }
                    for index, entry in enumerate(entries)
                ]
            else:
                snapshot = await _limited_call(_fetch_deepseek_balance, deepseek_key)
                accounts = [{"account_label": "default", "active": True, "source": snapshot}]
            sources[provider] = {
                "provider_label": label,
                "pool_size": len(entries) or (1 if deepseek_key else 0),
                "key_source": deepseek_source or ("pool" if entries else None),
                "accounts": accounts,
            }
        else:
            source = await _collect_usage_provider(provider, pool, label)
            accounts = source.get("accounts", [])
            if source.get("pool_size") == 0 and not any(account.get("source") is not None for account in accounts):
                continue
            sources[provider] = source
    return sources


async def _cached(cache: TTLCache, lock: asyncio.Lock, fresh: int, builder: Callable[[], Any]) -> dict[str, Any]:
    now = time.monotonic()
    if not fresh:
        cached = cache.get(now=now)
        if cached is not None:
            return cached
    async with lock:
        now = time.monotonic()
        if not fresh:
            cached = cache.get(now=now)
            if cached is not None:
                return cached
        value = builder()
        if inspect.isawaitable(value):
            value = await value
        return cache.set(value, now=time.monotonic())


@router.get("/workers")
async def get_workers(fresh: int = Query(default=0, ge=0, le=1)) -> dict[str, Any]:
    return await _cached(_workers_cache, _workers_lock, fresh, lambda: asyncio.to_thread(_build_workers_payload))


@router.get("/quotas")
async def get_quotas(fresh: int = Query(default=0, ge=0, le=1)) -> dict[str, Any]:
    async def build() -> dict[str, Any]:
        sources = await _collect_quota_sources()
        return build_quotas_response(sources, now=int(time.time()))

    return await _cached(_quotas_cache, _quotas_lock, fresh, build)
