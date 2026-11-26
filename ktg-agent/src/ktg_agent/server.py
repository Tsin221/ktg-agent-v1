"""Candidate configuration and canonical service entry point."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from ktg_agent.sessions import SessionRepository


@dataclass(frozen=True, slots=True)
class Settings:
    model_api_key: str
    model_base_url: str
    model_id: str
    ktg_base_url: str
    host: str = "127.0.0.1"
    port: int = 8001
    scope_by_token: bool = False
    session_db_path: str = ""
    max_total_tokens: int | None = None
    max_tool_calls: int = 24
    max_model_turns: int = 16
    context_window: int | None = None
    context_threshold_ratio: float = 0.8
    context_retain_ratio: float = 0.16
    max_output_tokens: int = 1024

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.environ.get("MODEL_API_KEY", "").strip()
        model_id = os.environ.get("MODEL_ID", "").strip()
        if not api_key or api_key == "CHANGE_ME":
            raise RuntimeError("MODEL_API_KEY must be configured")
        if not model_id or model_id == "CHANGE_ME":
            raise RuntimeError("MODEL_ID must be configured")
        model_base_url = os.environ.get(
            "MODEL_BASE_URL", "https://api.openai.com/v1"
        ).strip()
        ktg_base_url = os.environ.get(
            "KTG_BASE_URL", "http://127.0.0.1:8080"
        ).strip()
        host = os.environ.get("AGENT_HOST", "127.0.0.1").strip()
        for name, value in (
            ("MODEL_BASE_URL", model_base_url),
            ("KTG_BASE_URL", ktg_base_url),
            ("AGENT_HOST", host),
        ):
            if not value:
                raise RuntimeError(f"{name} must not be blank")
        raw_port = os.environ.get("AGENT_PORT", "8001")
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise RuntimeError("AGENT_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise RuntimeError("AGENT_PORT must be between 1 and 65535")
        raw_tokens = os.environ.get("AGENT_MAX_TOTAL_TOKENS")
        try:
            max_total_tokens = int(raw_tokens) if raw_tokens is not None else None
        except ValueError as exc:
            raise RuntimeError("AGENT_MAX_TOTAL_TOKENS must be a positive integer") from exc
        if max_total_tokens is not None and max_total_tokens <= 0:
            raise RuntimeError("AGENT_MAX_TOTAL_TOKENS must be a positive integer")

        def positive_integer(name: str, default: int | None = None) -> int | None:
            raw = os.environ.get(name)
            try:
                value = int(raw) if raw is not None else default
            except ValueError as exc:
                raise RuntimeError(f"{name} must be a positive integer") from exc
            if value is not None and value <= 0:
                raise RuntimeError(f"{name} must be a positive integer")
            return value

        def ratio(name: str, default: float) -> float:
            raw = os.environ.get(name)
            try:
                value = float(raw) if raw is not None else default
            except ValueError as exc:
                raise RuntimeError(f"{name} must be a finite ratio") from exc
            if not 0 < value <= 1:
                raise RuntimeError(f"{name} must be a finite ratio greater than 0 and at most 1")
            return value

        context_window = positive_integer("AGENT_CONTEXT_WINDOW")
        context_threshold_ratio = ratio("AGENT_CONTEXT_THRESHOLD_RATIO", 0.8)
        context_retain_ratio = ratio("AGENT_CONTEXT_RETAIN_RATIO", 0.16)
        if context_retain_ratio >= context_threshold_ratio:
            raise RuntimeError("AGENT_CONTEXT_RETAIN_RATIO must be less than AGENT_CONTEXT_THRESHOLD_RATIO")
        return cls(
            model_api_key=api_key,
            model_base_url=model_base_url,
            model_id=model_id,
            ktg_base_url=ktg_base_url,
            host=host,
            port=port,
            scope_by_token=os.environ.get("AGENT_SCOPE_BY_TOKEN", "false").lower() == "true",
            session_db_path=os.environ.get("AGENT_SESSION_DB", "").strip(),
            max_total_tokens=max_total_tokens,
            max_tool_calls=positive_integer("AGENT_MAX_TOOL_CALLS", 24) or 24,
            max_model_turns=positive_integer("AGENT_MAX_MODEL_TURNS", 16) or 16,
            max_output_tokens=positive_integer("AGENT_MAX_OUTPUT_TOKENS", 1024) or 1024,
            context_window=context_window,
            context_threshold_ratio=context_threshold_ratio,
            context_retain_ratio=context_retain_ratio,
        )


def create_session_repository(settings: Settings) -> SessionRepository:
    """Open the separately configured, versioned conversation database."""
    session_path = Path(settings.session_db_path)
    if not settings.session_db_path or not session_path.is_absolute():
        raise RuntimeError("AGENT_SESSION_DB must be an absolute path")
    return SessionRepository(session_path)



def main() -> None:
    from ktg_agent.http_candidate import run

    run()


if __name__ == '__main__':
    main()

