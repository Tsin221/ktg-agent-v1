from pathlib import Path

import pytest

from ktg_agent.server import Settings, create_session_repository
from ktg_agent.http_app import usage_limits


def _set_valid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_ID", "test-model")
    monkeypatch.setenv("MODEL_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("KTG_BASE_URL", "http://127.0.0.1:8080")


@pytest.mark.parametrize("name", ["AGENT_MAX_TOOL_CALLS", "AGENT_MAX_MODEL_TURNS"])
def test_execution_budget_configuration(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    _set_valid_environment(monkeypatch)
    monkeypatch.setenv(name, "17")
    limits = usage_limits(Settings.from_env())
    assert getattr(limits, "tool_calls_limit" if name.endswith("CALLS") else "request_limit") == 17
    for invalid in ("0", "-1", "abc"):
        monkeypatch.setenv(name, invalid)
        with pytest.raises(RuntimeError, match=name):
            Settings.from_env()


@pytest.mark.parametrize("name", ("MODEL_API_KEY", "MODEL_ID"))
def test_settings_rejects_unchanged_example_placeholders(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _set_valid_environment(monkeypatch)
    monkeypatch.setenv(name, "CHANGE_ME")

    with pytest.raises(RuntimeError, match=name):
        Settings.from_env()


@pytest.mark.parametrize("name", ("MODEL_BASE_URL", "KTG_BASE_URL", "AGENT_HOST"))
def test_settings_rejects_blank_service_addresses(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _set_valid_environment(monkeypatch)
    monkeypatch.setenv(name, "   ")

    with pytest.raises(RuntimeError, match=name):
        Settings.from_env()


def test_service_does_not_force_optional_temperature(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _set_valid_environment(monkeypatch)
    from ktg_agent.core.native_core import build_deepseek_agent
    from ktg_agent.operations.store import OperationPlanStore
    from ktg_agent.sessions.repository import SessionRepository
    agent = build_deepseek_agent(Settings.from_env(), OperationPlanStore(SessionRepository(tmp_path / "settings.sqlite")), instructions="test")
    assert isinstance(agent.model_settings, dict) and agent.model_settings.get("temperature") is None


def test_token_limit_configuration_reaches_both_service_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_environment(monkeypatch)
    monkeypatch.delenv("AGENT_MAX_TOTAL_TOKENS", raising=False)
    assert usage_limits(Settings.from_env()).total_tokens_limit is None
    monkeypatch.setenv("AGENT_MAX_TOTAL_TOKENS", "123")
    assert usage_limits(Settings.from_env()).total_tokens_limit == 123


@pytest.mark.parametrize("raw", ("", "0", "-1", "1.5", "invalid"))
def test_invalid_token_limit_configuration_fails(
    monkeypatch: pytest.MonkeyPatch, raw: str,
) -> None:
    _set_valid_environment(monkeypatch)
    monkeypatch.setenv("AGENT_MAX_TOTAL_TOKENS", raw)
    with pytest.raises(RuntimeError, match="AGENT_MAX_TOTAL_TOKENS"):
        Settings.from_env()


def test_session_database_uses_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _set_valid_environment(monkeypatch)
    session_path = tmp_path / "sessions.sqlite3"
    monkeypatch.setenv("AGENT_SESSION_DB", str(session_path))
    assert create_session_repository(Settings.from_env()).path == session_path
    assert session_path.exists()
    monkeypatch.setenv("AGENT_SESSION_DB", "relative.sqlite3")
    with pytest.raises(RuntimeError, match="absolute"):
        create_session_repository(Settings.from_env())


def test_retired_configuration_cannot_restore_business_tools(monkeypatch: pytest.MonkeyPatch) -> None:

    _set_valid_environment(monkeypatch)
    monkeypatch.setenv("AGENT_ENABLE_EXPLORATION", "true")
    monkeypatch.setenv("AGENT_ENABLE_ITEM_RECEIPT", "true")
    monkeypatch.setenv("AGENT_ENABLE_DV_CHECKPLAN_WRITE", "true")
    settings = Settings.from_env()
    assert not hasattr(settings, "enable_exploration")
    assert not hasattr(settings, "enable_item_receipt")
    from ktg_agent.tools.mes_catalog import build_mes_catalog, default_switches, mes_definitions
    available = build_mes_catalog(mes_definitions()).available(
        frozenset({"*:*:*"}), default_switches(operation_enabled=False))
    assert set(available) == {
        "ktg_count_products", "ktg_query_stock_summary", "ktg_query_workorder_overview",
        "ktg_query_workorder_materials", "ktg_query_workorder_quality", "ktg_query_production_trace", "ktg_resolve_business_reference",
    }
