import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import plugin_api


NOW = 1_700_000_000
CANARY = "canary-value-must-not-appear"


def _read_key_with_home(root: Path, env_key: str | None = None):
    environment = {"HERMES_HOME": str(root)}
    if env_key is not None:
        environment["DEEPSEEK_API_KEY"] = env_key
    with patch.dict(os.environ, environment, clear=True):
        return plugin_api._read_deepseek_key()


def _write_env(path: Path, value: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"DEEPSEEK_API_KEY={value}\n", encoding="utf-8")


def test_routes_are_relative_and_registered():
    paths = {route.path for route in plugin_api.router.routes}
    assert paths == {"/workers", "/quotas"}


def test_deepseek_key_environment_wins_over_files():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _write_env(root / ".env", "root-value")
        _write_env(root / "profiles" / "alpha" / ".env", "profile-value")

        assert _read_key_with_home(root, "environment-value") == "environment-value"


def test_deepseek_key_falls_back_to_root_env():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _write_env(root / ".env", "root-value")
        _write_env(root / "profiles" / "alpha" / ".env", "profile-value")

        assert _read_key_with_home(root) == "root-value"


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

    assert [provider["status"] for provider in result["providers"]] == ["n/d", "n/d", "n/d"]
    assert CANARY not in json.dumps(result)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name, value in globals().items():
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value, description=name))
    return suite
