from pathlib import Path

import pytest

from ktg_agent.server import Settings, create_service, create_session_repository


def _set_valid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_ID", "test-model")
    monkeypatch.setenv("MODEL_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("KTG_BASE_URL", "http://127.0.0.1:8080")


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_environment(monkeypatch)

    service = create_service(Settings.from_env())

    assert service._model_config.temperature is None


def test_session_database_uses_separate_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _set_valid_environment(monkeypatch)
    session_path = tmp_path / "sessions.sqlite3"
    monkeypatch.setenv("AGENT_SESSION_DB", str(session_path))
    monkeypatch.setenv("AGENT_INVENTORY_DB", str(tmp_path / "inventory.sqlite3"))
    settings = Settings.from_env()
    assert create_session_repository(settings).path == session_path
    assert session_path.exists()

    monkeypatch.setenv("AGENT_INVENTORY_DB", str(session_path))
    with pytest.raises(RuntimeError, match="must differ"):
        create_session_repository(Settings.from_env())

    monkeypatch.setenv("AGENT_SESSION_DB", "relative.sqlite3")
    with pytest.raises(RuntimeError, match="absolute"):
        create_session_repository(Settings.from_env())
