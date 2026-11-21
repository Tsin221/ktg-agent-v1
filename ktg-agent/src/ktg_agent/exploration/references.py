"""Owner-scoped opaque references; authorization of payload contents stays with callers."""

from __future__ import annotations

import json
import math
import re
import secrets
import sqlite3
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from time import time
from typing import Literal, cast

from ktg_agent.core.messages import JsonObject
from ktg_agent.ktg.client import KTGError

MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_PAYLOAD_DEPTH = 48
_REF_PATTERN = re.compile(r"(?:rec|res)_[A-Za-z0-9_-]{43}\Z")
type ReferenceKind = Literal["record", "result"]


def _valid_duration(value: float) -> bool:
    return not isinstance(value, bool) and math.isfinite(value) and value > 0


def _invalid(message: str) -> KTGError:
    return KTGError("VALIDATION_ERROR", message, status=400)


def _check_json(value: object, depth: int = 0) -> None:
    if depth > MAX_PAYLOAD_DEPTH:
        raise _invalid("Reference payload exceeds the nesting limit.")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _check_json(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _check_json(item, depth + 1)
        return
    raise _invalid("Reference payload must contain only finite JSON values.")


def _payload_text(payload: JsonObject) -> str:
    if not isinstance(payload, dict):
        raise _invalid("Reference payload must be a JSON object.")
    _check_json(payload)
    try:
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise _invalid("Reference payload exceeds the byte limit.")
    except (TypeError, ValueError, RecursionError) as exc:
        raise _invalid("Reference payload cannot be encoded as JSON.") from exc
    return text


class ReferenceStore:
    """Persist JSON snapshots without making a reference a permanent authorization."""

    def __init__(
        self, path: Path | str = ":memory:", ttl_seconds: float = 1800,
        clock: Callable[[], float] = time,
    ) -> None:
        if not _valid_duration(ttl_seconds):
            raise ValueError("ttl_seconds must be positive and finite")
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = RLock()
        self._closed = False
        self._db = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
        self._db.execute("PRAGMA busy_timeout=5000")
        if str(path) != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS exploration_references (
                    reference TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('record', 'result')),
                    owner TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
            """)
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS exploration_references_expiry
                ON exploration_references(expires_at)
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS exploration_focus (
                    owner TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    record_ref TEXT NOT NULL,
                    PRIMARY KEY (owner, scope)
                )
            """)

    def _now(self) -> float:
        now = self._clock()
        if not math.isfinite(now):
            raise ValueError("Reference clock must return a finite time")
        return now

    def _ensure_open(self) -> None:
        if self._closed:
            raise KTGError("REFERENCE_STORE_CLOSED", "Reference store is closed.", status=503)

    def put(
        self, kind: ReferenceKind, *, owner: str, scope: str,
        payload: JsonObject, ttl_seconds: float | None = None,
    ) -> str:
        if kind not in ("record", "result"):
            raise _invalid("Reference kind must be record or result.")
        if not isinstance(owner, str) or not owner.strip() or not isinstance(scope, str) or not scope.strip():
            raise _invalid("Reference owner and scope must not be empty.")
        duration = self._ttl_seconds if ttl_seconds is None else ttl_seconds
        if not _valid_duration(duration):
            raise _invalid("Reference TTL must be positive and finite.")
        text = _payload_text(payload)
        with self._lock:
            self._ensure_open()
            now = self._now()
            expires_at = now + duration
            if not math.isfinite(expires_at):
                raise _invalid("Reference expiry must be finite.")
            prefix = "rec_" if kind == "record" else "res_"
            reference = prefix + secrets.token_urlsafe(32)
            with self._db:
                self._db.execute(
                    "INSERT INTO exploration_references VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (reference, kind, owner, scope, text, now, expires_at),
                )
        return reference

    def get(self, ref: str, *, owner: str, scope: str) -> JsonObject:
        unavailable = KTGError("REFERENCE_UNAVAILABLE", "Reference is unavailable.", status=404)
        if not isinstance(ref, str) or _REF_PATTERN.fullmatch(ref) is None:
            raise unavailable
        if not isinstance(owner, str) or not owner.strip() or not isinstance(scope, str) or not scope.strip():
            raise unavailable
        with self._lock:
            self._ensure_open()
            row = cast(tuple[str, float] | None, self._db.execute(
                "SELECT payload, expires_at FROM exploration_references "
                "WHERE reference=? AND owner=? AND scope=?", (ref, owner, scope),
            ).fetchone())
            if row is None:
                raise unavailable
            if row[1] <= self._now():
                raise KTGError("REFERENCE_EXPIRED", "Reference has expired.", status=410)
            # Loading on every read gives callers isolated nested objects, never cache aliases.
            return cast(JsonObject, json.loads(row[0]))

    def prune(self) -> int:
        with self._lock:
            self._ensure_open()
            with self._db:
                now = self._now()
                cursor = self._db.execute(
                    "DELETE FROM exploration_references WHERE expires_at<=?", (now,),
                )
                self._db.execute("""
                    DELETE FROM exploration_focus
                    WHERE NOT EXISTS (
                        SELECT 1 FROM exploration_references r
                        WHERE r.reference=exploration_focus.record_ref
                          AND r.owner=exploration_focus.owner AND r.scope=exploration_focus.scope
                          AND r.kind='record' AND r.expires_at>?
                    )
                """, (now,))
                return cursor.rowcount

    def set_focus(self, *, owner: str, scope: str, record_ref: str) -> None:
        """Store a validated pointer only; never extend the record's TTL.

        INSERT ... SELECT validates the target in the same SQLite write as the
        focus replacement, so another connection cannot prune between checks.
        Business scope and current permissions remain the read layer's job.
        """
        unavailable = KTGError("REFERENCE_UNAVAILABLE", "Reference is unavailable.", status=404)
        if (not isinstance(record_ref, str) or _REF_PATTERN.fullmatch(record_ref) is None
                or not record_ref.startswith("rec_") or not isinstance(owner, str)
                or not owner.strip() or not isinstance(scope, str) or not scope.strip()):
            raise unavailable
        with self._lock:
            self._ensure_open()
            now = self._now()
            with self._db:
                cursor = self._db.execute("""
                    INSERT INTO exploration_focus(owner, scope, record_ref)
                    SELECT owner, scope, reference FROM exploration_references
                    WHERE reference=? AND owner=? AND scope=? AND kind='record' AND expires_at>?
                    ON CONFLICT(owner, scope) DO UPDATE SET record_ref=excluded.record_ref
                """, (record_ref, owner, scope, now))
                if cursor.rowcount != 1:
                    expiry = cast(tuple[float] | None, self._db.execute(
                        "SELECT expires_at FROM exploration_references "
                        "WHERE reference=? AND owner=? AND scope=? AND kind='record'",
                        (record_ref, owner, scope),
                    ).fetchone())
                    if expiry is not None and expiry[0] <= now:
                        raise KTGError("REFERENCE_EXPIRED", "Reference has expired.", status=410)
                    raise unavailable

    def get_focus(self, *, owner: str, scope: str) -> str | None:
        """Return a live pointer without loading body data or refreshing expiry."""
        with self._lock:
            self._ensure_open()
            row = cast(tuple[str, str | None, str | None, str | None, float | None] | None,
                       self._db.execute("""
                SELECT f.record_ref, r.kind, r.owner, r.scope, r.expires_at
                FROM exploration_focus f LEFT JOIN exploration_references r ON r.reference=f.record_ref
                WHERE f.owner=? AND f.scope=?
            """, (owner, scope)).fetchone())
            if row is None:
                return None
            if (row[1] != "record" or row[2] != owner or row[3] != scope
                    or row[4] is None or row[4] <= self._now()):
                with self._db:
                    self._db.execute(
                        "DELETE FROM exploration_focus WHERE owner=? AND scope=? AND record_ref=?",
                        (owner, scope, row[0]),
                    )
                return None
            return row[0]

    def clear_focus(self, *, owner: str, scope: str) -> None:
        with self._lock:
            self._ensure_open()
            with self._db:
                self._db.execute("DELETE FROM exploration_focus WHERE owner=? AND scope=?", (owner, scope))

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True
