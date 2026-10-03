import json
import unittest

from dashboard.quotas import build_quotas_response


NOW = 1_700_000_000
CANARY = "canary-value-must-not-appear"


def _claude_snapshot():
    return {
        "windows": [
            {"label": "five_hour", "used_percent": 12.5, "reset_at": NOW + 10},
            {"label": "seven_day", "used_percent": 33, "reset_at": None},
        ]
    }


def _codex_snapshot():
    return {"windows": [{"label": "5 hour", "used_percent": 40, "reset_at": NOW + 20}]}


def _deepseek_snapshot():
    return {
        "is_available": True,
        "balance_infos": [{"currency": "USD", "total_balance": "7.25"}],
    }


def _provider(sources, provider_id):
    result = build_quotas_response(sources, now=NOW)
    provider = next(provider for provider in result["providers"] if provider["id"] == provider_id)
    keys = {"id", "label", "status", "windows"}
    if provider_id == "deepseek":
        keys.add("balance")
    return {key: provider[key] for key in keys}


def test_quota_response_has_exact_provider_order_and_shape():
    sources = {
        "anthropic": _claude_snapshot(),
        "openai-codex": _codex_snapshot(),
        "deepseek": {**_deepseek_snapshot(), "request_key": CANARY},
    }

    result = build_quotas_response(sources, now=NOW)

    assert result["generated_at"] == NOW
    assert [provider["id"] for provider in result["providers"]] == ["claude", "codex", "deepseek"]
    assert [provider["status"] for provider in result["providers"]] == ["ok", "ok", "ok"]
    assert all(set(provider) >= {"account_label", "plan", "pool_size", "accounts"} for provider in result["providers"])
    assert all(set(provider["accounts"][0]) == {
        "account_label", "plan", "active", "status", "windows", "balance"
    } for provider in result["providers"])
    assert result["providers"][2]["key_source"] is None
    assert CANARY not in json.dumps(result)


def test_claude_ok_maps_five_hour_and_seven_day_windows():
    assert _provider({"anthropic": _claude_snapshot()}, "claude") == {
        "id": "claude",
        "label": "Claude",
        "status": "ok",
        "windows": [
            {"label": "5h", "used_percent": 12.5, "reset_at": NOW + 10},
            {"label": "week", "used_percent": 33.0, "reset_at": None},
        ],
    }


def test_claude_absent_error_and_timeout_are_unavailable():
    expected = {"id": "claude", "label": "Claude", "status": "n/a", "windows": []}
    for source in (None, RuntimeError(CANARY), TimeoutError(CANARY)):
        assert _provider({"anthropic": source}, "claude") == expected


def test_codex_ok_maps_all_plan_windows():
    source = {
        "windows": [
            {"label": "primary", "used_percent": 40, "reset_at": NOW + 20},
            {"label": "secondary", "used_percent": 70.5, "reset_at": NOW + 30},
        ]
    }
    assert _provider({"openai-codex": source}, "codex") == {
        "id": "codex",
        "label": "Codex",
        "status": "ok",
        "windows": [
            {"label": "primary", "used_percent": 40.0, "reset_at": NOW + 20},
            {"label": "secondary", "used_percent": 70.5, "reset_at": NOW + 30},
        ],
    }


def test_codex_absent_error_and_timeout_are_unavailable():
    expected = {"id": "codex", "label": "Codex", "status": "n/a", "windows": []}
    for source in (None, RuntimeError(CANARY), TimeoutError(CANARY)):
        assert _provider({"openai-codex": source}, "codex") == expected


def test_deepseek_ok_maps_only_currency_and_amount():
    assert _provider({"deepseek": {**_deepseek_snapshot(), "secret": CANARY}}, "deepseek") == {
        "id": "deepseek",
        "label": "DeepSeek",
        "status": "ok",
        "balance": {"currency": "USD", "amount": 7.25},
        "windows": [],
    }


def test_deepseek_absent_error_timeout_and_malformed_payload_are_unavailable():
    expected = {
        "id": "deepseek",
        "label": "DeepSeek",
        "status": "n/a",
        "balance": None,
        "windows": [],
    }
    malformed = ({}, {"balance_infos": []}, {"balance_infos": [{}]}, {"balance_infos": "invalid"})
    for source in (None, RuntimeError(CANARY), TimeoutError(CANARY), *malformed):
        assert _provider({"deepseek": source}, "deepseek") == expected


def test_one_failed_provider_does_not_hide_two_healthy_providers():
    result = build_quotas_response(
        {
            "anthropic": RuntimeError(CANARY),
            "openai-codex": _codex_snapshot(),
            "deepseek": _deepseek_snapshot(),
        },
        now=NOW,
    )

    assert [provider["id"] for provider in result["providers"]] == ["claude", "codex", "deepseek"]
    assert [provider["status"] for provider in result["providers"]] == ["n/a", "ok", "ok"]
    assert CANARY not in json.dumps(result)


def test_each_failed_or_malformed_source_degrades_independently():
    result = build_quotas_response(
        {"anthropic": RuntimeError(CANARY), "openai-codex": None, "deepseek": {"error": CANARY}},
        now=NOW,
    )
    assert result["generated_at"] == NOW
    assert [provider["id"] for provider in result["providers"]] == ["claude", "codex", "deepseek"]
    assert [provider["status"] for provider in result["providers"]] == ["n/a", "n/a", "n/a"]
    assert all(provider["accounts"][0]["status"] == "n/a" for provider in result["providers"])
    assert CANARY not in json.dumps(result)


def test_single_active_provider_yields_exactly_one_provider():
    result = build_quotas_response({"openai-codex": _codex_snapshot()}, now=NOW)

    assert [provider["id"] for provider in result["providers"]] == ["codex"]


def test_account_profile_surfaces_from_envelope_and_is_absent_for_plain_snapshot():
    envelope = {
        "accounts": [
            {"account_label": "primary", "active": True, "profile": "profile-alpha", "source": _codex_snapshot()},
        ]
    }
    codex = next(p for p in build_quotas_response({"openai-codex": envelope}, now=NOW)["providers"] if p["id"] == "codex")
    assert codex["accounts"][0]["profile"] == "profile-alpha"

    plain = next(p for p in build_quotas_response({"openai-codex": _codex_snapshot()}, now=NOW)["providers"] if p["id"] == "codex")
    assert "profile" not in plain["accounts"][0]


def test_provider_failures_are_sanitized_to_class_names_and_bounded():
    envelope = {
        "provider_label": "Anthropic",
        "pool_size": 0,
        "accounts": [{"account_label": "default", "active": True, "source": None}],
        "failures": [
            "RuntimeError",
            "../../etc/passwd",
            "sk-live-token",
            "TimeoutError",
            "ClientError",
            "ValueError",
            CANARY,
        ],
    }
    claude = next(p for p in build_quotas_response({"anthropic": envelope}, now=NOW)["providers"] if p["id"] == "claude")

    assert claude["status"] == "n/a"
    assert claude["failures"] == ["ClientError", "RuntimeError", "TimeoutError", "ValueError"]
    assert CANARY not in json.dumps(claude)


def test_provider_without_failures_has_no_failures_key():
    claude = next(
        p for p in build_quotas_response({"anthropic": {"accounts": [{"account_label": "a", "active": True, "source": _claude_snapshot()}]}}, now=NOW)["providers"]
        if p["id"] == "claude"
    )

    assert "failures" not in claude


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name, value in globals().items():
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value, description=name))
    return suite
