import json
import unittest

from dashboard.quotas import build_quotas_response


NOW = 1_700_000_000
CANARY = "canary-value-must-not-appear"


def test_quota_response_has_exact_provider_order_and_shape():
    sources = {
        "anthropic": {
            "windows": [
                {"label": "five_hour", "used_percent": 12.5, "reset_at": NOW + 10},
                {"label": "seven_day", "used_percent": 33, "reset_at": None},
            ]
        },
        "openai-codex": {
            "windows": [{"label": "5 hour", "used_percent": 40, "reset_at": NOW + 20}]
        },
        "deepseek": {
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "7.25"}],
            "request_key": CANARY,
        },
    }

    result = build_quotas_response(sources, now=NOW)

    assert result == {
        "generated_at": NOW,
        "providers": [
            {"id": "claude", "label": "Claude", "status": "ok", "windows": [
                {"label": "5h", "used_percent": 12.5, "reset_at": NOW + 10},
                {"label": "week", "used_percent": 33.0, "reset_at": None},
            ]},
            {"id": "codex", "label": "Codex", "status": "ok", "windows": [
                {"label": "5 hour", "used_percent": 40.0, "reset_at": NOW + 20},
            ]},
            {"id": "deepseek", "label": "DeepSeek", "status": "ok",
             "balance": {"currency": "USD", "amount": 7.25}, "windows": []},
        ],
    }
    assert CANARY not in json.dumps(result)


def test_each_failed_or_malformed_source_degrades_independently():
    result = build_quotas_response(
        {"anthropic": RuntimeError(CANARY), "openai-codex": None, "deepseek": {"error": CANARY}},
        now=NOW,
    )
    assert result == {
        "generated_at": NOW,
        "providers": [
            {"id": "claude", "label": "Claude", "status": "n/a", "windows": []},
            {"id": "codex", "label": "Codex", "status": "n/a", "windows": []},
            {"id": "deepseek", "label": "DeepSeek", "status": "n/a", "balance": None, "windows": []},
        ],
    }
    assert CANARY not in json.dumps(result)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name, value in globals().items():
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value, description=name))
    return suite
