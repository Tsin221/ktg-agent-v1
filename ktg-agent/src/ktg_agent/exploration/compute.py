"""Docker-only analysis execution. There is deliberately no host Python fallback."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import shutil
from collections.abc import Sequence
from typing import Protocol, cast
from uuid import uuid4

from ktg_agent.core.messages import JsonObject
from ktg_agent.ktg.client import KTGError

MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024


class Cancellation(Protocol):
    @property
    def is_cancelled(self) -> bool: ...


def _error(code: str, message: str) -> KTGError:
    return KTGError(code, message, status=503)


def _reject_constant(value: str) -> object:
    raise ValueError("Non-finite JSON value")


async def _read_limited(reader: asyncio.StreamReader, limit: int, *, discard: bool = False) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := await reader.read(65536):
        size += len(chunk)
        if size > limit:
            if not discard:
                raise _error("COMPUTE_OUTPUT_LIMIT", "Analysis output exceeded the byte limit.")
        elif not discard:
            chunks.append(chunk)
    return b"".join(chunks)


async def _exchange(proc: asyncio.subprocess.Process, payload: bytes) -> tuple[int, bytes]:
    if proc.stdin is None or proc.stdout is None or proc.stderr is None:
        raise _error("COMPUTE_UNAVAILABLE", "Analysis pipes are unavailable.")
    out = asyncio.create_task(_read_limited(proc.stdout, MAX_OUTPUT_BYTES))
    err = asyncio.create_task(_read_limited(proc.stderr, 8192, discard=True))
    try:
        proc.stdin.write(payload)
        await proc.stdin.drain()
        proc.stdin.close()
        output = await out
        await err
        return await proc.wait(), output
    finally:
        for task in (out, err):
            if not task.done():
                task.cancel()
        await asyncio.gather(out, err, return_exceptions=True)


class ComputeRunner:
    def __init__(self, image: str = "ktg-analysis:local", *, timeout_seconds: float = 15) -> None:
        if not image or image.startswith("-") or any(ch.isspace() for ch in image):
            raise ValueError("Invalid analysis image")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Analysis timeout must be positive and finite")
        self.image = image
        self.timeout_seconds = timeout_seconds
        self._slots = asyncio.Semaphore(2)

    def command(self, name: str) -> list[str]:
        return ["docker", "run", "--rm", "--name", name,
                "--network=none", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true", "--user=65532:65532",
                "--memory=128m", "--memory-swap=128m", "--cpus=1", "--pids-limit=16",
                "--ulimit=nofile=64:64", "--ulimit=fsize=1048576:1048576",
                "--tmpfs=/tmp:rw,noexec,nosuid,size=32m,uid=65532,gid=65532,mode=700",
                "--workdir=/tmp", "--pull=never", "-i", self.image]

    async def _cleanup(self, docker: str, name: str, proc: asyncio.subprocess.Process) -> None:
        cleanup: asyncio.subprocess.Process | None = None
        try:
            cleanup = await asyncio.create_subprocess_exec(
                docker, "rm", "-f", name, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(cleanup.wait(), timeout=3)
        except (OSError, TimeoutError):
            if cleanup is not None and cleanup.returncode is None:
                cleanup.kill()
                await cleanup.wait()
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def analyze(self, datasets: JsonObject, code: str,
                      cancellation: Cancellation | None = None) -> JsonObject:
        if cancellation is not None and cancellation.is_cancelled:
            raise _error("RUN_CANCELLED", "Analysis was cancelled.")
        if not isinstance(code, str) or not code.strip() or len(code) > 65536:
            raise KTGError("VALIDATION_ERROR", "Analysis code is empty or too long.", status=400)
        try:
            payload = json.dumps({"datasets": datasets, "code": code},
                                 ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise KTGError("VALIDATION_ERROR", "Analysis inputs must be JSON.", status=400) from exc
        if len(payload) > MAX_INPUT_BYTES:
            raise _error("COMPUTE_INPUT_LIMIT", "Analysis input exceeded the byte limit.")
        docker = shutil.which("docker")
        if docker is None:
            raise _error("COMPUTE_UNAVAILABLE", "Docker isolation is unavailable; analysis was not executed.")
        async with self._slots:
            if cancellation is not None and cancellation.is_cancelled:
                raise _error("RUN_CANCELLED", "Analysis was cancelled.")
            return await self._run(docker, datasets, payload, cancellation)

    async def _run(self, docker: str, datasets: JsonObject, payload: bytes,
                   cancellation: Cancellation | None) -> JsonObject:
        name = "ktg-analysis-" + uuid4().hex
        args: Sequence[str] = self.command(name)[1:]
        try:
            proc = await asyncio.create_subprocess_exec(
                docker, *args, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as exc:
            raise _error("COMPUTE_UNAVAILABLE", "Docker isolation could not start.") from exc
        exchange = asyncio.create_task(_exchange(proc, payload))
        try:
            async with asyncio.timeout(self.timeout_seconds):
                while not exchange.done():
                    if cancellation is not None and cancellation.is_cancelled:
                        raise _error("RUN_CANCELLED", "Analysis was cancelled.")
                    await asyncio.wait({exchange}, timeout=0.05)
                returncode, output = await exchange
            if returncode != 0:
                raise _error("COMPUTE_FAILED", "Isolated analysis failed or Docker isolation is unavailable.")
            result = self._result(output)
            coverages = [value.get("coverage", "unknown") for value in datasets.values() if isinstance(value, dict)]
            result["coverage"] = ("unknown" if not coverages or "unknown" in coverages
                                  else "partial" if "partial" in coverages else "complete")
            result["input_sources"] = list(datasets)
            return result
        except TimeoutError as exc:
            raise _error("COMPUTE_TIMEOUT", "Analysis timed out; container cleanup was requested.") from exc
        except (OSError, ConnectionError) as exc:
            raise _error("COMPUTE_UNAVAILABLE", "Docker isolation communication failed.") from exc
        finally:
            if not exchange.done():
                exchange.cancel()
            await asyncio.gather(exchange, return_exceptions=True)
            await self._cleanup(docker, name, proc)

    @staticmethod
    def _result(output: bytes) -> JsonObject:
        try:
            raw: object = json.loads(output, parse_constant=_reject_constant)
            if not isinstance(raw, dict) or set(raw) != {"summary", "tables", "artifacts", "warnings"}:
                raise ValueError("Invalid result envelope")
            result = cast(JsonObject, raw)
            if not isinstance(result["summary"], str) or not isinstance(result["tables"], list):
                raise ValueError("Invalid summary or tables")
            for table in result["tables"]:
                if not isinstance(table, dict):
                    raise ValueError("Invalid table")
                columns, rows = table.get("columns"), table.get("rows")
                if not isinstance(columns, list) or not isinstance(rows, list):
                    raise ValueError("Invalid table columns or rows")
                if not all(isinstance(col, dict) and isinstance(col.get("name"), str)
                           and isinstance(col.get("type"), str) for col in columns):
                    raise ValueError("Invalid column definitions")
                if not all(isinstance(row, list) and len(row) == len(columns) for row in rows):
                    raise ValueError("Invalid table row width")
            if not isinstance(result["warnings"], list) or not all(isinstance(v, str) for v in result["warnings"]):
                raise ValueError("Invalid warnings")
            artifacts = result["artifacts"]
            if not isinstance(artifacts, list):
                raise ValueError("Invalid artifacts")
            for item in artifacts:
                if not isinstance(item, dict) or set(item) != {"name", "mime_type", "data_base64"}:
                    raise ValueError("Invalid artifact")
                name, mime, data = item["name"], item["mime_type"], item["data_base64"]
                if not isinstance(name, str) or not name or "/" in name or "\\" in name:
                    raise ValueError("Invalid artifact name")
                if mime not in ("image/png", "image/jpeg", "text/csv", "application/json") or not isinstance(data, str):
                    raise ValueError("Invalid artifact type")
                if len(base64.b64decode(data, validate=True)) > MAX_OUTPUT_BYTES // 2:
                    raise ValueError("Artifact limit exceeded")
            return result
        except (ValueError, TypeError, RecursionError) as exc:
            raise _error("COMPUTE_INVALID_RESULT", "Isolated analysis returned an invalid result.") from exc


_runner = ComputeRunner()


async def analyze(datasets: JsonObject, code: str, cancellation: Cancellation | None = None) -> JsonObject:
    return await _runner.analyze(datasets, code, cancellation)
