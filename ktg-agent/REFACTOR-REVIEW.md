# Agent framework migration — full candidate checkpoint

This branch (`refactor/pydantic-ai`) contains the completed FastAPI + Pydantic AI + unified SQLite refactor candidate as of 2026-11-26, synced from `ktg-agent-refactor`. Legacy custom agent infrastructure (custom loop/events/queues/budget, custom context compaction/spill, custom providers, legacy discovery/coordinator) has been removed.

## Scope

- `http_app.py`, `http_rest.py`, `http_run_rest.py`, `http_runs.py`, `http_sse.py`, `server.py`: FastAPI application, canonical `ktg_agent.server` entry point, session REST interfaces, asynchronous run control and native SSE.
- `core/native_core.py`, `core/native_execution.py`, `core/native_persistence.py`: native Pydantic AI execution, cancellation/steer/follow-up, archive cursor and StepPersistence.
- `sessions/database.py`, `sessions/conversation_store.py`, `sessions/repository.py`, `sessions/native_planning.py`: unified WAL SQLite (`APP_SCHEMA_VERSION=100`), official `ConversationStore` backend, Planning and atomic terminal settlement.
- `context/native_context.py`, `tools/native.py`, `tools/mes_catalog.py`: native compaction and direct registration of the eight reviewed MES tools with fail-closed live permission checks.
- `pyproject.toml` and `uv.lock`: locked Python 3.12 dependencies (`fastapi==0.165.0`, `pydantic-ai-slim[openai]==2.75.0`, `pydantic-ai-harness==0.75.0`).
- `ktg-mes-ui`: assistant streaming, `plan_changed` and settlement compatibility updates.

## Verification and boundaries

- Isolated HTTP-01 through HTTP-06 and SQL-01 through SQL-06 verification passed (127 Python tests, strict mypy across 80 files, locked `uv` check, UI tests and staging build).
- Formal local and cloud demonstration deployments remain on `main` until explicitly approved for cutover. The candidate refuses to open databases inside `ktg_agent_v1`.
