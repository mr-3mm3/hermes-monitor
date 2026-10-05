import asyncio
import builtins
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dashboard import plugin_api

try:
    from agent import credential_pool as _real_credential_pool
    import agent.anthropic_credentials as _real_anthropic_credentials
except Exception:  # pragma: no cover - run outside the Hermes venv
    _real_credential_pool = None
    _real_anthropic_credentials = None


NOW = 1_700_000_000
CANARY = "canary-value-must-not-appear"

# Single hermetic profile so cross-profile collection tests never touch the real ~/.hermes.
_FAKE_PROFILES = [("default", Path("/tmp/fake-hermes"))]


def _result_provider(result, provider_id):
    return next(provider for provider in result["providers"] if provider["id"] == provider_id)


def _empty_runtime_import(name):
    if name == "agent.credential_pool":
        return SimpleNamespace(PROVIDER_REGISTRY={}, load_pool=lambda provider: _FakePool([], None))
    if name == "agent.account_usage":
        return SimpleNamespace(_USAGE_FETCHERS={}, fetch_account_usage=lambda *args, **kwargs: None)
    raise AssertionError(name)


def _read_key_with_home(root: Path, env_key: str | None = None):
    environment = {"HERMES_HOME": str(root)}
    if env_key is not None:
        environment["DEEPSEEK_API_KEY"] = env_key
    with patch.dict(os.environ, environment, clear=True):
        return plugin_api._read_deepseek_key()


def _write_env(path: Path, value: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"DEEPSEEK_API_KEY={value}\n", encoding="utf-8")


def _write_auth_json(home: Path, credential_pool: dict) -> None:
    """Write a minimal but valid Hermes auth.json holding *credential_pool* rows."""
    home.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "providers": {},
        "active_provider": None,
        "credential_pool": credential_pool,
    }
    (home / "auth.json").write_text(json.dumps(payload), encoding="utf-8")


def test_routes_are_relative_and_registered():
    paths = {route.path for route in plugin_api.router.routes}
    assert paths == {"/workers", "/quotas"}


def test_deepseek_key_environment_wins_over_files():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _write_env(root / ".env", "root-value")
        _write_env(root / "profiles" / "alpha" / ".env", "profile-value")

        assert _read_key_with_home(root, "environment-value") == "environment-value"
        with patch.dict(os.environ, {"HERMES_HOME": str(root), "DEEPSEEK_API_KEY": "environment-value"}, clear=True):
            assert plugin_api._read_deepseek_credential()[1] == "env"


def test_deepseek_key_falls_back_to_root_env():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _write_env(root / ".env", "root-value")
        _write_env(root / "profiles" / "alpha" / ".env", "profile-value")

        assert _read_key_with_home(root) == "root-value"
        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True):
            assert plugin_api._read_deepseek_credential()[1] == "root"


def test_deepseek_key_uses_first_profile_alphabetically():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _write_env(root / "profiles" / "zulu" / ".env", "zulu-value")
        _write_env(root / "profiles" / "alpha" / ".env", "alpha-value")

        assert _read_key_with_home(root) == "alpha-value"


def test_deepseek_key_is_none_when_unconfigured():
    with tempfile.TemporaryDirectory() as directory:
        assert _read_key_with_home(Path(directory)) is None


def test_sqlite_connections_are_enforced_read_only():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.db"
        with sqlite3.connect(path) as writable:
            writable.execute("CREATE TABLE sample (value TEXT)")
        with plugin_api._readonly_connection(path) as readonly:
            try:
                readonly.execute("INSERT INTO sample VALUES ('forbidden')")
            except sqlite3.OperationalError as error:
                assert "readonly" in str(error).lower()
            else:
                raise AssertionError("read-only connection unexpectedly accepted a write")


def test_workers_endpoint_exact_shape_and_fresh_bypasses_cache():
    calls = []

    def fake_build():
        calls.append(1)
        return {"generated_at": NOW, "count": 0, "workers": []}

    with patch.object(plugin_api, "_build_workers_payload", fake_build):
        plugin_api._workers_cache.clear()
        first = asyncio.run(plugin_api.get_workers(fresh=0))
        cached = asyncio.run(plugin_api.get_workers(fresh=0))
        refreshed = asyncio.run(plugin_api.get_workers(fresh=1))

    assert first == cached == refreshed == {"generated_at": NOW, "count": 0, "workers": []}
    assert len(calls) == 2


def test_quotas_endpoint_source_failure_is_nd_and_secret_free():
    async def fake_sources():
        return {"anthropic": RuntimeError(CANARY), "openai-codex": RuntimeError(CANARY), "deepseek": RuntimeError(CANARY)}

    with patch.object(plugin_api, "_collect_quota_sources", fake_sources), patch.object(plugin_api.time, "time", lambda: NOW):
        plugin_api._quotas_cache.clear()
        result = asyncio.run(plugin_api.get_quotas(fresh=1))

    assert [provider["status"] for provider in result["providers"]] == ["n/a", "n/a", "n/a"]
    assert CANARY not in json.dumps(result)


class _FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.body


def test_deepseek_balance_valid_response_is_mapped_without_exposing_key():
    response = _FakeResponse(b'{"is_available":true,"balance_infos":[{"currency":"USD","total_balance":"4.50"}]}')
    with patch.object(plugin_api, "_read_deepseek_key", lambda: CANARY), patch.object(
        plugin_api.urllib.request, "urlopen", return_value=response
    ):
        source = plugin_api._fetch_deepseek_balance()

    result = plugin_api.build_quotas_response({"deepseek": source}, now=NOW)
    assert _result_provider(result, "deepseek")["balance"] == {"currency": "USD", "amount": 4.5}
    assert CANARY not in json.dumps(result)


def test_deepseek_missing_key_is_unavailable_without_network_or_secret_output():
    output = io.StringIO()
    with patch.object(plugin_api, "_read_deepseek_key", return_value=None), patch.object(
        plugin_api.urllib.request, "urlopen"
    ) as urlopen, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        source = asyncio.run(plugin_api._limited_call(plugin_api._fetch_deepseek_balance))

    result = plugin_api.build_quotas_response({"deepseek": source}, now=NOW)
    deepseek = _result_provider(result, "deepseek")
    assert deepseek["status"] == "n/a"
    assert deepseek["balance"] is None
    assert deepseek["accounts"][0]["status"] == "n/a"
    urlopen.assert_not_called()
    assert CANARY not in output.getvalue()


def test_deepseek_http_error_timeout_and_malformed_json_are_secret_free_and_unavailable():
    failures = (
        urllib.error.URLError(CANARY),
        TimeoutError(CANARY),
        _FakeResponse(b"not-json"),
    )
    for failure in failures:
        output = io.StringIO()
        urlopen = patch.object(plugin_api.urllib.request, "urlopen", side_effect=failure) if isinstance(
            failure, BaseException
        ) else patch.object(plugin_api.urllib.request, "urlopen", return_value=failure)
        with patch.object(plugin_api, "_read_deepseek_key", return_value=CANARY), urlopen, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            source = asyncio.run(plugin_api._limited_call(plugin_api._fetch_deepseek_balance))

        result = plugin_api.build_quotas_response({"deepseek": source}, now=NOW)
        assert _result_provider(result, "deepseek")["status"] == "n/a"
        assert _result_provider(result, "deepseek")["balance"] is None
        assert CANARY not in json.dumps(result)
        assert CANARY not in output.getvalue()


def test_collection_isolates_failed_provider_and_preserves_two_healthy_sources():
    def fake_account_usage(provider, api_key=None, use_pool=True):
        if provider == "anthropic":
            raise TimeoutError(CANARY)
        return {"windows": [{"label": "primary", "used_percent": 20, "reset_at": NOW}]}

    deepseek = {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "3"}]}
    entry = _pool_entry("active", "primary", "runtime-token", 0)
    pools = {provider: _FakePool([entry], entry) for provider in ("anthropic", "openai-codex")}
    registry = {
        provider: SimpleNamespace(name=provider, inference_base_url="https://example.invalid") for provider in pools
    }

    def fake_import(name):
        if name == "agent.credential_pool":
            return _fake_credential_module(registry, pools)
        if name == "agent.account_usage":
            return SimpleNamespace(_USAGE_FETCHERS=dict.fromkeys(pools), fetch_account_usage=lambda *args, **kwargs: None)
        raise AssertionError(name)

    output = io.StringIO()
    with patch.object(plugin_api.importlib, "import_module", side_effect=fake_import), patch.object(
        plugin_api, "_enumerate_profiles", return_value=_FAKE_PROFILES
    ), patch.object(
        plugin_api, "_fetch_account_usage", fake_account_usage
    ), patch.object(plugin_api, "_read_deepseek_credential", return_value=("deepseek-key", "env")), patch.object(
        plugin_api, "_fetch_deepseek_balance", return_value=deepseek
    ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        sources = asyncio.run(plugin_api._collect_quota_sources())

    result = plugin_api.build_quotas_response(sources, now=NOW)
    assert [provider["status"] for provider in result["providers"]] == ["n/a", "ok", "ok"]
    assert CANARY not in json.dumps(result)
    assert CANARY not in output.getvalue()


class _FakePool:
    def __init__(self, entries, active=None):
        self._entries = entries
        self._active = active

    def entries(self):
        return list(self._entries)

    def peek(self):
        return self._active

    def select(self):
        return self._active


def _pool_entry(identifier, label, token, priority):
    return SimpleNamespace(
        id=identifier,
        label=label,
        priority=priority,
        runtime_api_key=token,
        access_token=token,
    )


def _fake_credential_module(registry, pools):
    def read_credential_pool(provider):
        pool = pools.get(provider, _FakePool([]))
        return [
            {
                "id": entry.id,
                "label": entry.label,
                "priority": entry.priority,
                "source": "manual",
                "auth_type": "api_key",
                "access_token": entry.access_token,
            }
            for entry in pool.entries()
        ]

    return SimpleNamespace(
        PROVIDER_REGISTRY=registry,
        load_pool=lambda provider: pools.get(provider, _FakePool([])),
        read_credential_pool=read_credential_pool,
    )


def _usage_snapshot(percent=10):
    return {
        "plan": "Pro",
        "windows": [
            {"label": "five_hour", "used_percent": percent, "reset_at": NOW},
            {"label": "seven_day", "used_percent": percent + 10, "reset_at": NOW},
        ],
    }


def test_anthropic_fetch_isolates_active_token_from_legacy_singleton_resolver():
    def legacy_resolver():
        return "legacy-token"

    def anthropic_fetcher(base_url=None, api_key=None):
        return {"resolved": globals()["resolve_anthropic_token"](), "api_key": api_key}

    anthropic_fetcher.__globals__["resolve_anthropic_token"] = legacy_resolver
    module = SimpleNamespace(
        _fetch_anthropic_account_usage=anthropic_fetcher,
        fetch_account_usage=lambda provider, api_key=None: {"resolved": "wrong", "api_key": api_key},
    )
    with patch.object(plugin_api.importlib, "import_module", return_value=module):
        result = plugin_api._fetch_account_usage("anthropic", "active-token", False)

    assert result == {"resolved": "active-token", "api_key": "active-token"}


def test_anthropic_active_oauth_token_populates_both_windows():
    active = _pool_entry("active", "primary", "valid-oauth-token", 0)
    pool = _FakePool([active], active)
    calls = []

    def fake_import(name):
        if name == "agent.credential_pool":
            return SimpleNamespace(load_pool=lambda provider: pool)
        if name == "agent.account_usage":
            def fetch(provider, api_key=None):
                calls.append((provider, api_key))
                return _usage_snapshot()
            return SimpleNamespace(fetch_account_usage=fetch)
        raise AssertionError(name)

    with patch.object(plugin_api.importlib, "import_module", side_effect=fake_import):
        source = asyncio.run(plugin_api._collect_usage_provider("anthropic"))
    claude = plugin_api.build_quotas_response({"anthropic": source}, now=NOW)["providers"][0]

    assert calls == [("anthropic", "valid-oauth-token")]
    assert claude["status"] == "ok"
    assert [window["label"] for window in claude["windows"]] == ["5h", "week"]


def test_usage_collection_never_selects_an_expiring_token():
    active = SimpleNamespace(
        id="oauth-own", label="claude", priority=0, access_token="old-access",
        refresh_token="old-refresh", expires_at_ms=(NOW - 3600) * 1000,
    )
    select_calls = []

    class Pool(_FakePool):
        def select(self):
            select_calls.append(1)
            return self._active

    with patch.object(plugin_api, "_fetch_account_usage", return_value=_usage_snapshot()):
        envelope = asyncio.run(
            plugin_api._collect_usage_provider("anthropic", Pool([active], active), "Anthropic", None)
        )

    assert select_calls == []
    assert envelope["accounts"][0]["source"]["windows"]


def test_dynamic_provider_enumeration_excludes_local_only_and_keeps_unknown_as_nd():
    entries = {
        "anthropic": _pool_entry("a", "primary", "anthropic-token", 0),
        "openai-codex": _pool_entry("c", "primary", "codex-token", 0),
        "mystery": _pool_entry("m", "primary", "mystery-token", 0),
        "lmstudio": _pool_entry("l", "local", "local-token", 0),
    }
    registry = {
        "anthropic": SimpleNamespace(display_name="Anthropic", inference_base_url="https://api.anthropic.com"),
        "openai-codex": SimpleNamespace(display_name="OpenAI Codex", inference_base_url="https://chatgpt.com"),
        "mystery": SimpleNamespace(display_name="Mystery Plan", inference_base_url="https://example.invalid"),
        "lmstudio": SimpleNamespace(display_name="LM Studio", inference_base_url="http://127.0.0.1:1234/v1"),
    }
    pools = {provider: _FakePool([entry], entry) for provider, entry in entries.items()}

    def fake_import(name):
        if name == "agent.credential_pool":
            return _fake_credential_module(registry, pools)
        if name == "agent.account_usage":
            return SimpleNamespace(
                _USAGE_FETCHERS={"anthropic": object(), "openai-codex": object()},
                fetch_account_usage=lambda provider, api_key=None: _usage_snapshot() if provider != "mystery" else None,
            )
        raise AssertionError(name)

    with patch.object(plugin_api.importlib, "import_module", side_effect=fake_import), patch.object(
        plugin_api, "_enumerate_profiles", return_value=_FAKE_PROFILES
    ), patch.object(
        plugin_api, "_read_deepseek_credential", return_value=(None, None)
    ):
        sources = asyncio.run(plugin_api._collect_quota_sources())
    result = plugin_api.build_quotas_response(sources, now=NOW)

    assert [provider["id"] for provider in result["providers"]] == ["claude", "codex", "mystery"]
    assert [provider["status"] for provider in result["providers"]] == ["ok", "ok", "n/a"]
    assert result["providers"][2]["account_label"] == "primary"
    assert "mystery-token" not in json.dumps(result)


def test_collection_uses_active_pool_account_and_isolates_each_account():
    first = _pool_entry("first", "primary", "pool-token-one", 0)
    second = _pool_entry("second", "backup", "pool-token-two", 1)
    pools = {
        "anthropic": _FakePool([first, second], first),
        "openai-codex": _FakePool([first], first),
    }

    def fake_import(name):
        if name == "agent.credential_pool":
            registry = {
                provider: SimpleNamespace(name=provider, inference_base_url="https://example.invalid")
                for provider in pools
            }
            return _fake_credential_module(registry, pools)
        if name == "agent.account_usage":
            def fetch(provider, api_key=None):
                if api_key == "pool-token-two":
                    raise TimeoutError(CANARY)
                return {
                    "plan": "Plus" if api_key else "Default",
                    "windows": [
                        {"label": "five_hour", "used_percent": 10, "reset_at": NOW},
                        {"label": "seven_day", "used_percent": 20, "reset_at": NOW},
                    ],
                }
            return SimpleNamespace(_USAGE_FETCHERS=dict.fromkeys(pools), fetch_account_usage=fetch)
        raise AssertionError(name)

    with patch.object(plugin_api.importlib, "import_module", side_effect=fake_import), patch.object(
        plugin_api, "_enumerate_profiles", return_value=_FAKE_PROFILES
    ), patch.object(
        plugin_api, "_fetch_deepseek_balance", side_effect=RuntimeError(CANARY)
    ):
        sources = asyncio.run(plugin_api._collect_quota_sources())
    result = plugin_api.build_quotas_response(sources, now=NOW)
    claude = result["providers"][0]

    assert claude["account_label"] == "primary"
    assert claude["plan"] == "Plus"
    assert claude["pool_size"] == 2
    assert [account["active"] for account in claude["accounts"]] == [True, False]
    assert [account["status"] for account in claude["accounts"]] == ["ok", "n/a"]
    codex = result["providers"][1]
    assert codex["account_label"] == "primary"
    assert codex["pool_size"] == 1
    assert codex["accounts"][0]["active"] is True
    assert "pool-token-one" not in json.dumps(result)
    assert "pool-token-two" not in json.dumps(result)
    assert CANARY not in json.dumps(result)


def test_empty_pool_is_not_reported_as_an_active_provider():
    calls = []

    def fake_import(name):
        if name == "agent.credential_pool":
            registry = {
                "anthropic": SimpleNamespace(name="Anthropic", inference_base_url="https://api.anthropic.com"),
                "openai-codex": SimpleNamespace(name="Codex", inference_base_url="https://chatgpt.com"),
            }
            return _fake_credential_module(registry, {})
        if name == "agent.account_usage":
            def fetch(provider, **kwargs):
                calls.append((provider, kwargs))
                return None
            return SimpleNamespace(
                _USAGE_FETCHERS={"anthropic": object(), "openai-codex": object()}, fetch_account_usage=fetch
            )
        raise AssertionError(name)

    with patch.object(plugin_api.importlib, "import_module", side_effect=fake_import), patch.object(
        plugin_api, "_enumerate_profiles", return_value=_FAKE_PROFILES
    ), patch.object(
        plugin_api, "_read_deepseek_credential", return_value=(None, None)
    ):
        sources = asyncio.run(plugin_api._collect_quota_sources())
    result = plugin_api.build_quotas_response(sources, now=NOW)

    assert result["providers"] == []
    assert calls == []


def test_unavailable_pool_falls_back_to_singleton_without_retrying_pool_token():
    entry = _pool_entry("active", "primary", "pool-token", 0)
    calls = []

    class BrokenPool:
        def entries(self):
            raise RuntimeError(CANARY)

        def peek(self):
            return entry

    def fake_import(name):
        if name == "agent.credential_pool":
            return SimpleNamespace(load_pool=lambda provider: BrokenPool())
        if name == "agent.account_usage":
            def fetch(provider, **kwargs):
                calls.append((provider, kwargs))
                return {"plan": "Pro", "windows": [{"label": "primary", "used_percent": 5, "reset_at": NOW}]}
            return SimpleNamespace(fetch_account_usage=fetch)
        raise AssertionError(name)

    with patch.object(plugin_api.importlib, "import_module", side_effect=fake_import):
        source = asyncio.run(plugin_api._collect_usage_provider("openai-codex"))

    assert source["pool_size"] == 0
    assert calls == [("openai-codex", {})]


def test_deepseek_key_source_is_reported_without_key_and_missing_key_is_unavailable():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _write_env(root / "profiles" / "alpha" / ".env", "deepseek-secret-value")
        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True):
            key, source = plugin_api._read_deepseek_credential()
        assert key == "deepseek-secret-value"
        assert source == "alpha"

        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True), patch.object(
            plugin_api.importlib, "import_module", side_effect=_empty_runtime_import
        ), patch.object(
            plugin_api, "_enumerate_profiles", return_value=_FAKE_PROFILES
        ), patch.object(
            plugin_api.urllib.request, "urlopen", return_value=_FakeResponse(
                b'{"is_available":true,"balance_infos":[{"currency":"USD","total_balance":"2"}]}'
            )
        ):
            sources = asyncio.run(plugin_api._collect_quota_sources())
        deepseek = _result_provider(plugin_api.build_quotas_response(sources, now=NOW), "deepseek")
        assert deepseek["key_source"] == "alpha"
        assert deepseek["accounts"][0]["active"] is True
        assert "deepseek-secret-value" not in json.dumps(deepseek)

    with tempfile.TemporaryDirectory() as directory, patch.dict(
        os.environ, {"HERMES_HOME": directory}, clear=True
    ), patch.object(plugin_api.importlib, "import_module", side_effect=_empty_runtime_import), patch.object(
        plugin_api, "_enumerate_profiles", return_value=_FAKE_PROFILES
    ):
        sources = asyncio.run(plugin_api._collect_quota_sources())
    assert plugin_api.build_quotas_response(sources, now=NOW)["providers"] == []


def test_deepseek_key_source_never_exposes_key_from_unsafe_profile_name():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        key = "credential-like-canary"
        _write_env(root / "profiles" / key / ".env", key)

        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True):
            resolved_key, source = plugin_api._read_deepseek_credential()

    assert resolved_key == key
    assert source == "profile"
    assert key not in source


def test_pool_labels_never_expose_email_or_token_in_response_or_logs():
    runtime_token = "sensitive-runtime-token"
    access_token = "sensitive-access-token"
    foreign_token = "foreign-account-token"
    refresh_token = "sensitive-refresh-token"
    entry = _pool_entry("unsafe", access_token, runtime_token, 0)
    entry.access_token = access_token
    foreign_entry = _pool_entry("foreign", f"backup-{refresh_token}", foreign_token, 1)
    foreign_entry.refresh_token = refresh_token

    def fake_import(name):
        if name == "agent.credential_pool":
            registry = {
                "anthropic": SimpleNamespace(name="Anthropic", inference_base_url="https://api.anthropic.com"),
                "openai-codex": SimpleNamespace(name="Codex", inference_base_url="https://chatgpt.com"),
            }
            return SimpleNamespace(
                PROVIDER_REGISTRY=registry,
                load_pool=lambda provider: _FakePool([entry, foreign_entry], entry),
            )
        if name == "agent.account_usage":
            return SimpleNamespace(
                _USAGE_FETCHERS={"anthropic": object(), "openai-codex": object()},
                fetch_account_usage=lambda provider, api_key=None: {
                    "plan": "Plus",
                    "windows": [{"label": "primary", "used_percent": 1, "reset_at": NOW}],
                },
            )
        raise AssertionError(name)

    output = io.StringIO()
    with patch.object(plugin_api.importlib, "import_module", side_effect=fake_import), patch.object(
        plugin_api, "_enumerate_profiles", return_value=_FAKE_PROFILES
    ), patch.object(
        plugin_api, "_fetch_deepseek_balance", side_effect=RuntimeError(CANARY)
    ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        result = plugin_api.build_quotas_response(asyncio.run(plugin_api._collect_quota_sources()), now=NOW)

    serialized = json.dumps(result)
    assert runtime_token not in serialized
    assert access_token not in serialized
    assert foreign_token not in serialized
    assert refresh_token not in serialized
    assert CANARY not in serialized
    assert runtime_token not in output.getvalue()
    assert access_token not in output.getvalue()
    assert foreign_token not in output.getvalue()
    assert refresh_token not in output.getvalue()
    assert CANARY not in output.getvalue()


def test_worker_tool_events_expose_only_name_hash_and_timestamp():
    raw = [{"function": {"name": "terminal", "arguments": {"command": CANARY, "token": CANARY}}}]

    events = plugin_api._extract_calls(raw, 123.0)

    assert len(events) == 1
    assert set(events[0]) == {"tool_name", "arguments_hash", "timestamp"}
    assert events[0]["tool_name"] == "terminal"
    assert len(events[0]["arguments_hash"]) == 16
    assert CANARY not in json.dumps(events)


def test_enumerate_profiles_lists_default_then_sorted_sibling_dirs():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "profiles" / "zulu").mkdir(parents=True)
        (root / "profiles" / "alpha").mkdir(parents=True)
        (root / "profiles" / "default").mkdir(parents=True)  # duplicate of the root home → skipped
        (root / "profiles" / "bad name").mkdir(parents=True)  # space → rejected by the name filter
        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True):
            profiles = plugin_api._enumerate_profiles()

    assert profiles[0] == ("default", root)
    assert [name for name, _ in profiles[1:]] == ["alpha", "zulu"]


def test_named_profile_quota_read_never_writes_or_copies_root_credentials():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "root"
        work = root / "profiles" / "work"
        _write_auth_json(root, {
            "anthropic": [
                {
                    "id": "root-oauth", "label": "root", "source": "manual",
                    "auth_type": "oauth", "priority": 0, "access_token": "root-access",
                    "refresh_token": "root-refresh", "expires_at_ms": (NOW - 3600) * 1000,
                }
            ]
        })
        _write_auth_json(work, {
            "anthropic": [
                {
                    "id": "work-oauth", "label": "work", "source": "manual",
                    "auth_type": "oauth", "priority": 0, "access_token": "work-access",
                    "refresh_token": "work-refresh", "expires_at_ms": (NOW + 3600) * 1000,
                }
            ]
        })
        root_before = (root / "auth.json").read_bytes()
        work_before = (work / "auth.json").read_bytes()
        override: dict[str, Path | None] = {"home": None}

        def set_home(value):
            previous = override["home"]
            override["home"] = Path(value)
            return previous

        def reset_home(previous):
            override["home"] = previous

        constants = SimpleNamespace(
            get_hermes_home=lambda: Path(os.environ["HERMES_HOME"]),
            set_hermes_home_override=set_home,
            reset_hermes_home_override=reset_home,
        )

        def read_credential_pool(provider):
            home = override["home"] or constants.get_hermes_home()
            payload = json.loads((home / "auth.json").read_text(encoding="utf-8"))
            return payload["credential_pool"].get(provider, [])

        credential_module = SimpleNamespace(read_credential_pool=read_credential_pool)

        def fake_import(name):
            if name == "hermes_constants":
                return constants
            if name == "agent.credential_pool":
                return credential_module
            raise AssertionError(name)

        with patch.dict(os.environ, {"HERMES_HOME": str(work)}, clear=True), \
                patch.object(plugin_api.importlib, "import_module", side_effect=fake_import), \
                patch.object(plugin_api, "_fetch_account_usage", return_value=_usage_snapshot()):
            profiles = plugin_api._enumerate_profiles()
            source = asyncio.run(
                plugin_api._collect_usage_provider_cross_profile("anthropic", profiles, "Anthropic")
            )

        assert source is not None
        assert profiles[:2] == [("work", work), ("default", root)]
        assert (root / "auth.json").read_bytes() == root_before
        assert (work / "auth.json").read_bytes() == work_before
        stored_work = json.loads((work / "auth.json").read_text(encoding="utf-8"))
        assert {row["id"] for row in stored_work["credential_pool"]["anthropic"]} == {"work-oauth"}
        assert [account["profile"] for account in source["accounts"]] == ["work", "default"]


def test_sibling_quota_read_is_strictly_read_only_and_never_mutates_stores():
    if _real_credential_pool is None:
        raise unittest.SkipTest("real agent package required")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "root"
        alpha = root / "profiles" / "alpha"
        # alpha holds an aged DEAD manual entry (past the 24h prune window) and an expiring
        # Anthropic OAuth entry — exactly the rows the old peek()/select() path mutated.
        alpha_pool = {
            "anthropic": [
                {
                    "id": "manual-dead", "label": "stale", "source": "manual",
                    "auth_type": "api_key", "priority": 0, "access_token": "manual-secret-token",
                    "last_status": "dead", "last_status_at": NOW - 3 * 86400,
                    "last_error_reason": "invalid_token",
                },
                {
                    "id": "oauth-exp", "label": "claude", "source": "manual",
                    "auth_type": "oauth", "priority": 1, "access_token": "oauth-access-token",
                    "refresh_token": "oauth-refresh-token", "expires_at_ms": (NOW - 3600) * 1000,
                },
            ]
        }
        _write_auth_json(root, {})
        _write_auth_json(alpha, alpha_pool)

        root_before = (root / "auth.json").read_bytes()
        alpha_before = (alpha / "auth.json").read_bytes()

        refresh_calls = []

        def _record_refresh(*args, **kwargs):
            refresh_calls.append(1)
            return {"access_token": "x", "refresh_token": "y", "expires_at_ms": 0}

        with patch.object(_real_anthropic_credentials, "refresh_anthropic_oauth_pure", side_effect=_record_refresh), \
                patch.object(plugin_api, "_fetch_account_usage", return_value=_usage_snapshot()):
            with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True):
                snapshots = plugin_api._snapshot_pool_for_profile(_real_credential_pool, "anthropic", alpha)
                assert snapshots is not None
                assert {snapshot.entry_id for snapshot in snapshots} == {"manual-dead", "oauth-exp"}
                envelope = asyncio.run(plugin_api._collect_usage_from_snapshots("anthropic", snapshots, "Anthropic", {}))

        # Both stores byte-identical: the sibling was read, never written.
        assert (root / "auth.json").read_bytes() == root_before
        assert (alpha / "auth.json").read_bytes() == alpha_before
        root_text = (root / "auth.json").read_text(encoding="utf-8")
        assert "manual-secret-token" not in root_text
        assert "oauth-refresh-token" not in root_text
        assert refresh_calls == []
        assert [account["active"] for account in envelope["accounts"]] == [False, False]


def test_cross_profile_readonly_snapshots_dedup_and_label_by_profile():
    if _real_credential_pool is None:
        raise unittest.SkipTest("real agent package required")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "root"
        alpha = root / "profiles" / "alpha"
        _write_auth_json(root, {
            "openai-codex": [
                {"id": "id-a", "label": "primary", "source": "manual", "auth_type": "api_key", "priority": 0, "access_token": "token-a"},
                {"id": "id-b", "label": "backup", "source": "manual", "auth_type": "api_key", "priority": 1, "access_token": "token-b"},
            ]
        })
        _write_auth_json(alpha, {
            "openai-codex": [
                {"id": "id-c", "label": "alpha-primary", "source": "manual", "auth_type": "api_key", "priority": 0, "access_token": "token-c"},
            ]
        })
        profiles = [("default", root), ("alpha", alpha)]

        def fake_usage(provider, api_key=None, use_pool=True):
            return {"plan": "Pro", "windows": [{"label": "primary", "used_percent": 10, "reset_at": NOW}]}

        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True), \
                patch.object(plugin_api, "_fetch_account_usage", side_effect=fake_usage):
            source = asyncio.run(plugin_api._collect_usage_provider_cross_profile("openai-codex", profiles, "Codex"))

        codex = _result_provider(plugin_api.build_quotas_response({"openai-codex": source}, now=NOW), "codex")

        assert [account["account_label"] for account in codex["accounts"]] == ["primary", "backup", "alpha-primary"]
        assert [account["profile"] for account in codex["accounts"]] == ["default", "default", "alpha"]
        assert [account["active"] for account in codex["accounts"]] == [True, False, False]
        assert codex["pool_size"] == 3
        assert "token-a" not in json.dumps(source)
        assert "token-c" not in json.dumps(source)


def test_cross_profile_reuses_one_fetch_per_unique_credential():
    root = Path("/tmp/hermes-root")
    profiles = [
        ("default", root),
        ("profile-alpha", root / "profiles" / "profile-alpha"),
        ("profile-beta", root / "profiles" / "profile-beta"),
    ]
    entry_a = _pool_entry("id-a", "primary", "token-a", 0)
    entry_b = _pool_entry("id-b", "backup", "token-b", 1)
    calls = []

    def load_pool(provider):
        # A shared (global/Keychain) store resolves the same two credentials for every home.
        return _FakePool([entry_a, entry_b], entry_a)

    def fake_import(name):
        if name == "agent.credential_pool":
            registry = {"anthropic": SimpleNamespace(name="Anthropic", inference_base_url="https://api.anthropic.com")}
            return _fake_credential_module(registry, {"anthropic": load_pool("anthropic")})
        if name == "agent.account_usage":
            return SimpleNamespace(_USAGE_FETCHERS={"anthropic": object()}, fetch_account_usage=lambda *a, **k: None)
        raise AssertionError(name)

    def fake_usage(provider, api_key=None, use_pool=True):
        calls.append((provider, api_key))
        return _usage_snapshot()

    with patch.object(plugin_api.importlib, "import_module", side_effect=fake_import), patch.object(
        plugin_api, "_enumerate_profiles", return_value=profiles
    ), patch.object(plugin_api, "_fetch_account_usage", side_effect=fake_usage), patch.object(
        plugin_api, "_read_deepseek_credential", return_value=(None, None)
    ):
        sources = asyncio.run(plugin_api._collect_quota_sources())

    claude = _result_provider(plugin_api.build_quotas_response(sources, now=NOW), "claude")

    # Three profiles share one credential store: each credential must be fetched once, not once per profile.
    assert calls == [("anthropic", "token-a"), ("anthropic", "token-b")]
    assert [account["account_label"] for account in claude["accounts"]] == ["primary", "backup"]
    assert [account["profile"] for account in claude["accounts"]] == ["default", "default"]
    assert claude["pool_size"] == 2
    assert claude["status"] == "ok"


def test_pool_resolution_failure_is_reported_as_nd_instead_of_hidden():
    def read_credential_pool(provider):
        raise RuntimeError(CANARY)

    def fake_import(name):
        if name == "agent.credential_pool":
            return SimpleNamespace(
                PROVIDER_REGISTRY={"anthropic": SimpleNamespace(name="Anthropic", inference_base_url="https://api.anthropic.com")},
                read_credential_pool=read_credential_pool,
            )
        if name == "agent.account_usage":
            return SimpleNamespace(_USAGE_FETCHERS={"anthropic": object()}, fetch_account_usage=lambda provider, **kwargs: None)
        raise AssertionError(name)

    output = io.StringIO()
    with patch.object(plugin_api.importlib, "import_module", side_effect=fake_import), patch.object(
        plugin_api, "_enumerate_profiles", return_value=_FAKE_PROFILES
    ), patch.object(plugin_api, "_read_deepseek_credential", return_value=(None, None)), \
            contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        result = plugin_api.build_quotas_response(asyncio.run(plugin_api._collect_quota_sources()), now=NOW)

    claude = _result_provider(result, "claude")
    assert claude["status"] == "n/a"
    assert claude["failures"] == ["RuntimeError"]
    assert CANARY not in json.dumps(result)
    assert CANARY not in output.getvalue()


def test_unconfigured_provider_without_credential_is_hidden_from_collection():
    # A provider with no pool entries, no singleton snapshot and no failure is
    # "not configured": the filter must drop it so the footer only shows real accounts.
    def load_pool(provider):
        return _FakePool([], None)  # empty everywhere → no credential at all

    def fake_import(name):
        if name == "agent.credential_pool":
            registry = {"gemini": SimpleNamespace(name="Gemini", inference_base_url="https://generativelanguage.googleapis.com")}
            return _fake_credential_module(registry, {"gemini": load_pool("gemini")})
        if name == "agent.account_usage":
            return SimpleNamespace(_USAGE_FETCHERS={"gemini": object()}, fetch_account_usage=lambda *a, **k: None)
        raise AssertionError(name)

    with patch.object(plugin_api.importlib, "import_module", side_effect=fake_import), patch.object(
        plugin_api, "_enumerate_profiles", return_value=_FAKE_PROFILES
    ), patch.object(plugin_api, "_fetch_account_usage", return_value=None), patch.object(
        plugin_api, "_read_deepseek_credential", return_value=(None, None)
    ):
        sources = asyncio.run(plugin_api._collect_quota_sources())

    assert "gemini" not in sources
    assert plugin_api.build_quotas_response(sources, now=NOW)["providers"] == []


def test_configured_provider_with_credential_but_failed_fetch_stays_visible_as_nd():
    # A provider with a pool credential whose usage fetch fails (rate-limit/refresh) must
    # stay visible with status n/a, not vanish like an unconfigured provider.
    entry = _pool_entry("id-x", "primary", "token-x", 0)

    def load_pool(provider):
        return _FakePool([entry], entry)

    def fake_import(name):
        if name == "agent.credential_pool":
            registry = {"openrouter": SimpleNamespace(name="OpenRouter", inference_base_url="https://openrouter.ai/api")}
            return _fake_credential_module(registry, {"openrouter": load_pool("openrouter")})
        if name == "agent.account_usage":
            return SimpleNamespace(_USAGE_FETCHERS={"openrouter": object()}, fetch_account_usage=lambda *a, **k: None)
        raise AssertionError(name)

    with patch.object(plugin_api.importlib, "import_module", side_effect=fake_import), patch.object(
        plugin_api, "_enumerate_profiles", return_value=_FAKE_PROFILES
    ), patch.object(plugin_api, "_fetch_account_usage", return_value=None), patch.object(
        plugin_api, "_read_deepseek_credential", return_value=(None, None)
    ):
        sources = asyncio.run(plugin_api._collect_quota_sources())

    assert "openrouter" in sources
    provider = _result_provider(plugin_api.build_quotas_response(sources, now=NOW), "openrouter")
    assert provider["status"] == "n/a"
    assert provider["pool_size"] == 1


def test_secret_scope_helpers_degrade_without_agent_package():
    # Outside the Hermes serve process there is no agent.secret_scope module; the helpers
    # must return None without breaking read-only credential snapshots.
    # The Hermes venv used to run these tests ships the real agent package, so force the
    # degradation path by making the agent.secret_scope import fail.
    real_import = builtins.__import__

    def _no_secret_scope(name, *args, **kwargs):
        if name == "agent.secret_scope":
            raise ImportError("agent.secret_scope unavailable (simulated)")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_no_secret_scope):
        scope_token = plugin_api._install_secret_scope(Path("/tmp/not-hermes"))
        assert scope_token is None
        plugin_api._reset_secret_scope(scope_token)  # must not raise


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name, value in globals().items():
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value, description=name))
    return suite
