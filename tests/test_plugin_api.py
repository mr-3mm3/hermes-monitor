import asyncio
import json
import unittest
from unittest.mock import patch

from dashboard import plugin_api


NOW = 1_700_000_000
SECRET = "fake-secret-never-return"


def test_routes_are_relative_and_registered():
    paths = {route.path for route in plugin_api.router.routes}
    assert paths == {"/workers", "/quotas"}


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
        return {"anthropic": RuntimeError(SECRET), "openai-codex": RuntimeError(SECRET), "deepseek": RuntimeError(SECRET)}

    with patch.object(plugin_api, "_collect_quota_sources", fake_sources), patch.object(plugin_api.time, "time", lambda: NOW):
        plugin_api._quotas_cache.clear()
        result = asyncio.run(plugin_api.get_quotas(fresh=1))

    assert [provider["status"] for provider in result["providers"]] == ["n/d", "n/d", "n/d"]
    assert SECRET not in json.dumps(result)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name, value in globals().items():
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value, description=name))
    return suite
