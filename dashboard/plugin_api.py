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
from dataclasses import dataclass
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


async def _entry_snapshot(provider: str, entry: Any, shared: dict[str, Any] | None = None) -> Any:
    """Usage snapshot for one pool entry, fetched at most once per credential.

    A shared (global/Keychain) credential store resolves the same entries for every profile
    home, so a per-profile fetch would issue N identical calls to the provider's usage API.
    Those endpoints rate-limit aggressively (Anthropic answers 429 with a ~3 minute
    ``Retry-After``), which turned every window into "n/a"; ``shared`` carries the snapshot —
    including a failed one — across profiles.
    """
    key = getattr(entry, "id", None)
    if shared is not None and key is not None and key in shared:
        return shared[key]
    token = _entry_token(entry)
    if not token:
        return None
    result = await _limited_call(_fetch_account_usage, provider, token, False)
    if shared is not None and key is not None:
        shared[key] = result
    return result


def _failure_name(error: BaseException) -> str:
    """Exception class name only.

    Exception messages can embed tokens, absolute home paths or URLs, so a failure is
    identified by its class name and nothing else.
    """
    name = type(error).__name__
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,59}", name) else "Error"


async def _collect_usage_provider(
    provider: str,
    pool: Any = None,
    label: str | None = None,
    shared: dict[str, Any] | None = None,
) -> dict[str, Any]:
    failure: str | None = None
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
    except Exception as error:
        entries, active = [], None
        failure = _failure_name(error)

    if not entries:
        source = await _limited_call(_fetch_account_usage, provider, None, False)
        return {
            "pool_size": 0,
            "provider_label": label,
            "failure": failure,
            "accounts": [{"entry_id": None, "account_label": "default", "active": True, "source": source}],
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
    calls = [_entry_snapshot(provider, entry, shared) for entry in entries]
    snapshots = await asyncio.gather(*calls)
    return {
        "pool_size": len(entries),
        "provider_label": label,
        "failure": failure,
        "accounts": [
            {
                "entry_id": getattr(entry, "id", None),
                "account_label": _safe_account_label(entry, index, secrets),
                "active": bool(active is entry or (active_id is not None and getattr(entry, "id", None) == active_id)),
                "source": snapshots[index],
            }
            for index, entry in enumerate(entries)
        ],
    }


async def _snapshot_fetch(provider: str, snapshot: _PoolEntrySnapshot, shared: dict[str, Any] | None) -> Any:
    """Fetch usage for one sibling snapshot entry; never refreshes the sibling's token."""
    key = snapshot.entry_id
    if shared is not None and key is not None and key in shared:
        return shared[key]
    if not snapshot.access_token:
        return None
    result = await _limited_call(_fetch_account_usage, provider, snapshot.access_token, False)
    if shared is not None and key is not None:
        shared[key] = result
    return result


async def _collect_usage_from_snapshots(
    provider: str,
    snapshots: list[_PoolEntrySnapshot],
    label: str | None,
    shared: dict[str, Any] | None,
) -> dict[str, Any]:
    """Collect usage from a SIBLING profile's immutable snapshots, strictly read-only.

    An expired sibling OAuth token is fetched as-is (and reports ``n/a`` on failure); it is never
    refreshed. Sibling accounts are always ``active=False`` — only the backend's own profile can
    mark an account active.
    """
    if not snapshots:
        return {"pool_size": 0, "provider_label": label, "failure": None, "accounts": []}
    secrets = tuple(
        secret for snapshot in snapshots for secret in (snapshot.access_token, snapshot.refresh_token) if secret
    )
    calls = [_snapshot_fetch(provider, snapshot, shared) for snapshot in snapshots]
    results = await asyncio.gather(*calls)
    return {
        "pool_size": len(snapshots),
        "provider_label": label,
        "failure": None,
        "accounts": [
            {
                "entry_id": snapshot.entry_id,
                "account_label": _safe_account_label(snapshot, index, secrets),
                "active": False,
                "source": results[index],
            }
            for index, snapshot in enumerate(snapshots)
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


def _enumerate_profiles() -> list[tuple[str, Path]]:
    """Return ``(profile_name, home_path)`` for the backend's own home and every named profile.

    The backend's own home is first (labelled ``default``); it carries the pool the current
    session actually resolves against. Remaining entries are the sibling profiles under
    ``<root>/profiles/``, in alphabetical order. Only plain directory names survive (no
    separators, ``@``, or control characters), so a profile name can never smuggle a path or
    a credential-shaped label into the response.
    """
    root = _hermes_home()
    profiles: list[tuple[str, Path]] = [("default", root)]
    profiles_dir = root / "profiles"
    try:
        children = sorted((entry for entry in profiles_dir.iterdir() if entry.is_dir()), key=lambda p: p.name)
    except OSError:
        return profiles
    for child in children:
        name = child.name
        if name == "default":
            # The root home is already labelled "default"; a sibling directory of the same name
            # is either a stray or a duplicate of the root profile, so it is not enumerated.
            continue
        if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
            profiles.append((name, child))
    return profiles


def _install_secret_scope(home_path: Path) -> Any:
    """Bind *home_path*'s profile secret scope; returns a reset token (or ``None``).

    The Desktop serve multiplexes profiles and fails closed with ``UnscopedSecretError``
    on any ``get_secret`` read with no scope installed. Pool loading resolves singletons
    (the Anthropic OAuth/Keychain token, env API keys) through ``get_secret``, so the read
    must run inside the profile's own scope. Degrades to no scope when the module is absent
    (e.g. under tests), where ``get_secret`` falls back to the process env anyway.
    """
    try:
        from agent.secret_scope import build_profile_secret_scope, set_secret_scope
    except Exception:
        return None
    try:
        scope = build_profile_secret_scope(home_path)
    except Exception:
        scope = {}
    return set_secret_scope(scope, profile_home=str(home_path))


def _reset_secret_scope(token: Any) -> None:
    if token is None:
        return
    try:
        from agent.secret_scope import reset_secret_scope
    except Exception:
        return
    reset_secret_scope(token)


def _home_override_api() -> tuple[Any, Any]:
    """Context-local home override pair (set/reset), or ``(None, None)`` when unavailable.

    ``hermes_constants.set_hermes_home_override`` binds the home in a ``ContextVar``, so it
    never mutates the process-global ``HERMES_HOME`` env. This is the only supported seam for
    reading another profile's store; there is deliberately no env-swap fallback.
    """
    try:
        constants = importlib.import_module("hermes_constants")
        set_override = getattr(constants, "set_hermes_home_override", None)
        reset_override = getattr(constants, "reset_hermes_home_override", None)
    except Exception:
        return None, None
    if callable(set_override) and callable(reset_override):
        return set_override, reset_override
    return None, None


def _load_pool_for_profile(credential_module: Any, provider: str, home_path: Path) -> Any:
    """Load *provider*'s pool resolved against the backend's OWN home.

    Only the backend's own profile is loaded this way: it is the one pool the plugin is allowed
    to mutate (``select()`` refresh of its own token). The read runs inside the profile's secret
    scope and the context-local home override; the override is reset before the pool is handed
    back, so the live pool is always scoped to the process home — which is the own home. When the
    override API is unavailable the pool simply loads against the process home (correct here, and
    never used for siblings — those go through ``_snapshot_pool_for_profile``).
    """
    set_override, reset_override = _home_override_api()
    secret_token = _install_secret_scope(home_path)
    try:
        if set_override is not None:
            token = set_override(str(home_path))
            try:
                return credential_module.load_pool(provider)
            finally:
                reset_override(token)
        return credential_module.load_pool(provider)
    finally:
        _reset_secret_scope(secret_token)


@dataclass(frozen=True)
class _PoolEntrySnapshot:
    """Immutable read-only view of one credential-pool row.

    Carries only the identity and token material needed to fetch a sibling's usage. It never
    holds a live ``CredentialPool`` — whose ``peek``/``select``/``reclaim``/``_available_entries``
    prune, refresh and persist — so a sibling's store cannot be mutated from outside its own scope.
    """

    entry_id: str | None
    label: str
    priority: int
    source: str
    auth_type: str
    access_token: str
    refresh_token: str | None
    expires_at_ms: int | None
    provider: str


def _snapshot_from_row(provider: str, row: dict[str, Any]) -> _PoolEntrySnapshot:
    refresh_token = row.get("refresh_token")
    expires_at_ms = row.get("expires_at_ms")

    def _priority() -> int:
        try:
            return int(row.get("priority") or 0)
        except (TypeError, ValueError):
            return 0

    return _PoolEntrySnapshot(
        entry_id=row.get("id") if isinstance(row.get("id"), str) else None,
        label=str(row.get("label") or ""),
        priority=_priority(),
        source=str(row.get("source") or ""),
        auth_type=str(row.get("auth_type") or ""),
        access_token=str(row.get("access_token") or ""),
        refresh_token=refresh_token if isinstance(refresh_token, str) and refresh_token else None,
        expires_at_ms=int(expires_at_ms) if isinstance(expires_at_ms, (int, float)) else None,
        provider=provider,
    )


def _snapshot_pool_for_profile(
    credential_module: Any, provider: str, home_path: Path
) -> list[_PoolEntrySnapshot] | None:
    """Read-only immutable snapshot of a SIBLING profile's pool entries.

    Built entirely inside the sibling's home + secret scope using ``read_credential_pool`` (a
    plain auth.json read). Never ``load_pool`` (which seeds/prunes and persists), ``peek``,
    ``select``, ``reclaim`` or ``_available_entries``. Returns ``None`` when the context-local
    home override is unavailable, so the caller skips that sibling instead of falling back to a
    process-global env swap.
    """
    set_override, reset_override = _home_override_api()
    if set_override is None:
        return None
    read_credential_pool = getattr(credential_module, "read_credential_pool", None)
    if not callable(read_credential_pool):
        return None
    secret_token = _install_secret_scope(home_path)
    try:
        token = set_override(str(home_path))
        try:
            rows = read_credential_pool(provider)
        finally:
            reset_override(token)
    finally:
        _reset_secret_scope(secret_token)
    if not isinstance(rows, list):
        return []
    return [_snapshot_from_row(provider, row) for row in rows if isinstance(row, dict)]


async def _collect_usage_provider_cross_profile(provider: str, profiles: list[tuple[str, Path]], label: str) -> dict[str, Any] | None:
    """Collect *provider* usage across every profile, deduplicating accounts by entry id.

    Accounts are tagged with their ``profile`` (the profile whose own auth.json holds the
    entry). A shared singleton/Keychain fallback (empty pool, ``entry_id is None``) is reported
    once, from the first profile that resolves it. Only the backend's own home can mark an
    account ``active``; accounts surfaced from sibling profiles are always ``active=False``.
    """
    credential_module = importlib.import_module("agent.credential_pool")
    accounts_by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    entry_profile: dict[str, str] = {}
    entry_active: dict[str, bool] = {}
    singleton_account: dict[str, Any] | None = None
    singleton_profile = "default"
    singleton_active = True
    shared: dict[str, Any] = {}
    failures: set[str] = set()
    for profile_index, (profile_name, home_path) in enumerate(profiles):
        if profile_index == 0:
            # Backend's OWN home: the one pool the plugin may mutate (refresh its own token).
            pool = None
            try:
                pool = _load_pool_for_profile(credential_module, provider, home_path)
            except Exception as error:
                # A broken home must not erase the provider: fall back to the singleton
                # resolver (``_collect_usage_provider`` retries the pool, then the singleton)
                # and remember why, so the failure is reported instead of hidden.
                failures.add(_failure_name(error))
            try:
                envelope = await _collect_usage_provider(provider, pool, label, shared)
            except Exception as error:
                failures.add(_failure_name(error))
                continue
        else:
            # SIBLING profile: strictly read-only immutable snapshot. A sibling whose
            # override API is unavailable (``None``) is skipped — no env swap, ever.
            try:
                snapshots = _snapshot_pool_for_profile(credential_module, provider, home_path)
            except Exception as error:
                failures.add(_failure_name(error))
                continue
            if snapshots is None:
                continue
            try:
                envelope = await _collect_usage_from_snapshots(provider, snapshots, label, shared)
            except Exception as error:
                failures.add(_failure_name(error))
                continue
        if envelope.get("failure"):
            failures.add(str(envelope["failure"]))
        for account in envelope.get("accounts", []):
            entry_id = account.get("entry_id")
            if entry_id is None:
                # Shared singleton/Keychain fallback: report once, from the backend's own home.
                if profile_index == 0 and singleton_account is None:
                    singleton_account = account
                    singleton_profile = profile_name
                    singleton_active = bool(account.get("active"))
                continue
            if entry_id in accounts_by_id:
                continue
            accounts_by_id[entry_id] = account
            order.append(entry_id)
            entry_profile[entry_id] = profile_name
            entry_active[entry_id] = profile_index == 0 and bool(account.get("active"))
    accounts: list[dict[str, Any]] = []
    for entry_id in order:
        account = dict(accounts_by_id[entry_id])
        account.pop("entry_id", None)
        account["profile"] = entry_profile[entry_id]
        account["active"] = entry_active[entry_id]
        accounts.append(account)
    if singleton_account is not None:
        account = dict(singleton_account)
        account.pop("entry_id", None)
        account["profile"] = singleton_profile
        account["active"] = singleton_active
        accounts.insert(0, account)
    if not accounts and not failures:
        return None
    return {
        "pool_size": len(order),
        "provider_label": label,
        "accounts": accounts,
        "failures": sorted(failures)[:4],
    }


async def _collect_quota_sources() -> dict[str, Any]:
    credential_module = importlib.import_module("agent.credential_pool")
    account_usage_module = importlib.import_module("agent.account_usage")
    registry = getattr(credential_module, "PROVIDER_REGISTRY", {})
    supported = set(getattr(account_usage_module, "_USAGE_FETCHERS", {}))
    deepseek_key, deepseek_source = _read_deepseek_credential()
    profiles = _enumerate_profiles()

    providers: list[tuple[str, str]] = []
    for provider in sorted(set(registry) | supported):
        config = registry.get(provider)
        if config is not None and _is_local_only_provider(config):
            continue
        providers.append((provider, _provider_label(config, provider)))

    if deepseek_key and all(provider != "deepseek" for provider, _ in providers):
        providers.append(("deepseek", "DeepSeek"))

    known_rank = {"anthropic": 0, "openai-codex": 1, "deepseek": 2}
    providers.sort(key=lambda item: (known_rank.get(item[0], 3), item[0]))

    sources: dict[str, Any] = {}
    for provider, label in providers:
        if provider == "deepseek":
            # DeepSeek is key-based (DEEPSEEK_API_KEY resolved from env → root .env → the first
            # profile .env that holds it), not a credential-pool provider, so there is no pool to
            # aggregate across profiles here.
            snapshot = await _limited_call(_fetch_deepseek_balance, deepseek_key)
            sources[provider] = {
                "provider_label": label,
                "pool_size": 1 if deepseek_key else 0,
                "key_source": deepseek_source,
                "accounts": [{"account_label": "default", "active": True, "source": snapshot}],
            }
            continue
        source = await _collect_usage_provider_cross_profile(provider, profiles, label)
        if source is None:
            continue
        accounts = source.get("accounts", [])
        resolved = any(account.get("source") is not None for account in accounts)
        # Keep dropping a provider that is simply unconfigured (empty pool, no snapshot,
        # no failure). A provider whose resolution *failed* stays in the payload as n/a
        # with its failure names, so a footer "n/a" is never indistinguishable from a
        # silently swallowed error.
        if source.get("pool_size") == 0 and not resolved and not source.get("failures"):
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
