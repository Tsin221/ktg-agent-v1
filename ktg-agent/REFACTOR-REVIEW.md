# Agent framework migration — review checkpoint

This branch is based on `26e93f5` and contains the independently developed FastAPI HTTP adapter and Pydantic AI core as of 2026-11-26. It is intended for architecture and implementation review. The legacy service remains available for comparison.

## Review scope

- `http_app.py`, `http_rest.py`, `http_run_rest.py`, `http_runs.py`, `http_sse.py`: FastAPI application, session REST interfaces, asynchronous execution control and SSE.
- `core/native_core.py`, `core/native_execution.py`, `core/native_persistence.py`: native Pydantic AI execution and framework step persistence.
- `context/native_context.py`, `sessions/native_planning.py`, `tools/native.py`: context processing, planning and the eight existing MES tools.
- `pyproject.toml` and `uv.lock`: locked Python 3.12 dependencies, including Pydantic AI Slim 2.75.0 and Harness 0.75.0.
- `ktg-mes-ui`: assistant streaming and settlement compatibility changes.

## Verification and limits

The candidate core handoff reports 105 offline tests passing and strict type checking across 76 source files. These tests use model and MES test doubles, temporary SQLite databases, and real HTTP/SSE adapter behavior. The original repository's legacy tests remain in this review branch.

The application-level SQLite contract and default native-core assembly, real DeepSeek/MES integration, and final deployment acceptance remain outstanding. This branch is not a replacement for the existing demonstration deployment. The candidate must not be started against the existing demonstration SQLite database.

Reviewers should focus on how much of the original infrastructure the frameworks can replace, the execution and persistence boundaries, cancellation and input semantics, event settlement, and the existing authorization and confirmation guarantees.
