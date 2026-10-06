import pytest

from ktg_agent.server import Settings, create_service


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
