from __future__ import annotations

from pathlib import Path

import pytest

from ktg_agent.server import Settings


def _base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_ID", "test-model")
    for name in (
        "AGENT_CONTEXT_WINDOW", "AGENT_CONTEXT_RESERVE_TOKENS",
        "AGENT_CONTEXT_THRESHOLD_RATIO", "AGENT_CONTEXT_RETAIN_RATIO",
        "AGENT_CONTEXT_SUMMARY_MAX_TOKENS", "AGENT_SPILL_MAX_INLINE_TOKENS",
        "AGENT_SPILL_DIR", "AGENT_SESSION_DB", "AGENT_INVENTORY_DB",
    ):
        monkeypatch.delenv(name, raising=False)


def test_context_defaults_are_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _base(monkeypatch)
    settings = Settings.from_env()
    assert settings.context_window is None
    assert settings.context_reserve_tokens is None
    assert settings.context_threshold_ratio == 0.8
    assert settings.context_retain_ratio == 0.16
    assert settings.context_summary_max_tokens == 1024
    assert settings.spill_max_inline_tokens is None
    assert settings.spill_dir == ""


def test_context_settings_parse_valid_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _base(monkeypatch)
    monkeypatch.setenv("AGENT_CONTEXT_WINDOW", "4096")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVE_TOKENS", "512")
    monkeypatch.setenv("AGENT_CONTEXT_THRESHOLD_RATIO", "0.9")
    monkeypatch.setenv("AGENT_CONTEXT_RETAIN_RATIO", "0.2")
    monkeypatch.setenv("AGENT_CONTEXT_SUMMARY_MAX_TOKENS", "256")
    monkeypatch.setenv("AGENT_SPILL_MAX_INLINE_TOKENS", "100")
    monkeypatch.setenv("AGENT_SPILL_DIR", str(tmp_path))
    settings = Settings.from_env()
    assert (settings.context_window, settings.context_reserve_tokens) == (4096, 512)
    assert (settings.context_threshold_ratio, settings.context_retain_ratio) == (0.9, 0.2)
    assert (settings.context_summary_max_tokens, settings.spill_max_inline_tokens) == (256, 100)
    assert settings.spill_dir == str(tmp_path)


@pytest.mark.parametrize("name,value", [
    ("AGENT_CONTEXT_WINDOW", "0"), ("AGENT_CONTEXT_WINDOW", "-1"),
    ("AGENT_CONTEXT_WINDOW", "abc"),
    ("AGENT_CONTEXT_RESERVE_TOKENS", "0"),
    ("AGENT_CONTEXT_THRESHOLD_RATIO", "0"),
    ("AGENT_CONTEXT_THRESHOLD_RATIO", "1.1"),
    ("AGENT_CONTEXT_THRESHOLD_RATIO", "nan"),
    ("AGENT_CONTEXT_RETAIN_RATIO", "0"),
    ("AGENT_CONTEXT_RETAIN_RATIO", "0.8"),
    ("AGENT_CONTEXT_SUMMARY_MAX_TOKENS", "0"),
    ("AGENT_SPILL_MAX_INLINE_TOKENS", "-1"),
    ("AGENT_SPILL_DIR", "relative/path"),
])
def test_invalid_context_settings_fail_at_startup(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str,
) -> None:
    _base(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=name):
        Settings.from_env()


def test_reserve_must_fit_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _base(monkeypatch)
    monkeypatch.setenv("AGENT_CONTEXT_WINDOW", "100")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVE_TOKENS", "100")
    with pytest.raises(RuntimeError, match="AGENT_CONTEXT_RESERVE_TOKENS"):
        Settings.from_env()


@pytest.mark.parametrize("db_name", ("AGENT_SESSION_DB", "AGENT_INVENTORY_DB"))
def test_spill_directory_cannot_be_a_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db_name: str,
) -> None:
    _base(monkeypatch)
    same_path = str(tmp_path / "store.sqlite3")
    monkeypatch.setenv("AGENT_SPILL_DIR", same_path)
    monkeypatch.setenv(db_name, same_path)
    with pytest.raises(RuntimeError, match="AGENT_SPILL_DIR"):
        Settings.from_env()


def test_spill_requires_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    _base(monkeypatch)
    monkeypatch.setenv("AGENT_SPILL_MAX_INLINE_TOKENS", "128")
    with pytest.raises(RuntimeError, match="AGENT_SPILL_DIR"):
        Settings.from_env()
