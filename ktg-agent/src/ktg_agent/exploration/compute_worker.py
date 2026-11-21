"""JSON analysis protocol for the isolated container. Never execute this on host data."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import math
from pathlib import Path
import sys
from typing import cast

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
MAX_INPUT = 4 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024
_ARTIFACT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".csv": "text/csv", ".json": "application/json"}


class _Diagnostics(io.StringIO):
    def write(self, value: str) -> int:
        # User print output is intentionally discarded, not mixed into the JSON channel.
        return len(value)


def _json_check(value: object, depth: int = 0) -> None:
    if depth > 48:
        raise ValueError("Analysis result is too deeply nested.")
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json_check(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json_check(item, depth + 1)
        return
    raise ValueError("Analysis result must be finite JSON values.")


def _artifacts(items: list[JsonValue]) -> list[JsonValue]:
    root = Path("/tmp/artifacts").resolve()
    exported: list[JsonValue] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid artifact descriptor.")
        relative = item.get("path")
        name = item.get("name")
        if not isinstance(relative, str) or not isinstance(name, str):
            raise ValueError("Artifact requires a relative path and a name.")
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or Path(name).name != name or not name:
            raise ValueError("Invalid artifact path.")
        candidate = (root / path).resolve()
        mime = _ARTIFACT_TYPES.get(candidate.suffix.lower())
        if not candidate.is_relative_to(root) or mime is None or not candidate.is_file():
            raise ValueError("Artifact is outside the allowed output directory or type.")
        if item.get("mime_type") != mime or candidate.stat().st_size > MAX_OUTPUT // 2:
            raise ValueError("Artifact type or size is invalid.")
        with candidate.open("rb") as stream:
            data = stream.read(MAX_OUTPUT // 2 + 1)
        if len(data) > MAX_OUTPUT // 2:
            raise ValueError("Artifact exceeds the byte limit.")
        exported.append({"name": name, "mime_type": mime,
                         "data_base64": base64.b64encode(data).decode("ascii")})
    return exported


def run_job(request: JsonObject) -> JsonObject:
    """Execute only within the runner's container; direct calls are protocol tests."""
    datasets, code = request.get("datasets"), request.get("code")
    if not isinstance(datasets, dict) or not isinstance(code, str) or len(code) > 65536:
        raise ValueError("Invalid analysis request.")
    namespace: dict[str, object] = {"__name__": "analysis"}
    try:
        with contextlib.redirect_stdout(_Diagnostics()), contextlib.redirect_stderr(_Diagnostics()):
            exec(compile(code, "<analysis>", "exec"), namespace)
            analyze_function = namespace.get("analyze")
            if not callable(analyze_function):
                raise ValueError("Define analyze(datasets).")
            raw: object = analyze_function(datasets)
    except Exception:
        raise ValueError("Analysis code did not produce a result.") from None
    _json_check(raw)
    if not isinstance(raw, dict) or set(raw) != {"summary", "tables", "artifacts", "warnings"}:
        raise ValueError("Analysis must return summary, tables, artifacts and warnings.")
    result = cast(JsonObject, raw)
    if not isinstance(result["summary"], str) or not isinstance(result["tables"], list):
        raise ValueError("Invalid summary or tables.")
    if not isinstance(result["warnings"], list) or not all(isinstance(item, str) for item in result["warnings"]):
        raise ValueError("Invalid warnings.")
    if not isinstance(result["artifacts"], list):
        raise ValueError("Invalid artifacts.")
    for table in result["tables"]:
        if not isinstance(table, dict) or not isinstance(table.get("columns"), list) or not isinstance(table.get("rows"), list):
            raise ValueError("Tables require columns and rows.")
        columns = cast(list[JsonValue], table["columns"])
        if not all(isinstance(col, dict) and isinstance(col.get("name"), str) and isinstance(col.get("type"), str) for col in columns):
            raise ValueError("Table columns require a name and type.")
        if not all(isinstance(row, list) and len(row) == len(columns) for row in cast(list[JsonValue], table["rows"])):
            raise ValueError("Table rows do not match their columns.")
    result["artifacts"] = _artifacts(result["artifacts"])
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_OUTPUT:
        raise ValueError("Analysis output exceeds the byte limit.")
    return result


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("Input limit exceeded.")
        request: object = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("Expected a JSON request.")
        result = run_job(cast(JsonObject, request))
        sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception:
        sys.stderr.write("Analysis failed or returned an invalid result.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
