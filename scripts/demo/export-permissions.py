"""Print the reviewed showcase permission allowlist as a JSON array.

The 70 leaves were frozen from the authenticated cloud demo's 2026-11-21
permission snapshot. New tool manifests must never expand account grants.
This script does not build services, initialize stores or modify any roles.
"""
from __future__ import annotations

import json
from pathlib import Path
import re


_LEAF = re.compile(r"mes:[a-z][a-z0-9_]*:[a-z][a-z0-9_]*:(?:list|query|add|edit|remove)\Z")


def load_showcase_permissions(path: Path | None = None) -> tuple[str, ...]:
    """Load only the committed profile; validate before sync consumes stdout."""
    source = path if path is not None else Path(__file__).with_name("showcase-permissions.json")
    value: object = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, list) or len(value) != 70:
        raise ValueError("The showcase profile must contain exactly 70 permission leaves.")
    permissions: set[str] = set()
    for permission in value:
        if not isinstance(permission, str) or _LEAF.fullmatch(permission) is None:
            raise ValueError("The showcase profile contains an invalid permission leaf.")
        permissions.add(permission)
    if len(permissions) != 70:
        raise ValueError("The showcase profile contains duplicate permission leaves.")
    return tuple(sorted(permissions))


def main() -> None:
    print(json.dumps(load_showcase_permissions(), ensure_ascii=False))


if __name__ == "__main__":
    main()
