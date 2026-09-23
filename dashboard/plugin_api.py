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
import sqlite3
import time
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

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


def _fetch_account_usage(provider: str) -> Any:
    module = importlib.import_module("agent.account_usage")
    token = _active_pool_token(provider) if provider == "openai-codex" else None
    result = module.fetch_account_usage(provider, api_key=token) if token else module.fetch_account_usage(provider)
    return asyncio.run(result) if inspect.isawaitable(result) else result


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


def _read_deepseek_key() -> str | None:
    environment_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if environment_key:
        return environment_key

    root = _hermes_home()
    root_key = _read_env_key(root / ".env", "DEEPSEEK_API_KEY")
    if root_key:
        return root_key

    try:
        profile_envs = sorted((root / "profiles").glob("*/.env"), key=lambda path: path.parent.name)
    except OSError:
        return None
    for path in profile_envs:
        profile_key = _read_env_key(path, "DEEPSEEK_API_KEY")
        if profile_key:
            return profile_key
    return None


def _fetch_deepseek_balance() -> Any:
    key = _read_deepseek_key()
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
    anthropic, codex, deepseek = await asyncio.gather(
        _limited_call(_fetch_account_usage, "anthropic"),
        _limited_call(_fetch_account_usage, "openai-codex"),
        _limited_call(_fetch_deepseek_balance),
    )
    return {"anthropic": anthropic, "openai-codex": codex, "deepseek": deepseek}


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
