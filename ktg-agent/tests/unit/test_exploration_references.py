from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest

from ktg_agent.core.messages import JsonObject
from ktg_agent.exploration.references import MAX_PAYLOAD_BYTES, ReferenceKind, ReferenceStore
from ktg_agent.ktg.client import KTGError


def test_round_trip_preserves_json_and_does_not_share_mutable_values() -> None:
    store = ReferenceStore()
    payload: JsonObject = {"entity": "dv.machinery", "id": 7, "nested": {"values": [1, None, True]}}
    ref = store.put("record", owner="alice", scope="session-1", payload=payload)
    assert ref.startswith("rec_") and len(ref) >= 40
    cast(dict[str, object], payload["nested"])["values"] = ["changed"]
    result = store.get(ref, owner="alice", scope="session-1")
    assert result["nested"] == {"values": [1, None, True]}
    result["entity"] = "changed"
    assert store.get(ref, owner="alice", scope="session-1")["entity"] == "dv.machinery"
    store.close()


def test_references_recover_after_reopen_and_across_runs_in_same_scope(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "refs.db"
    first = ReferenceStore(path)
    payload: JsonObject = {"source_run": "run-1", "rows": [{"name": "装配机"}]}
    ref = first.put("result", owner="visitor-1", scope="session-1", payload=payload)
    assert ref.startswith("res_")
    first.close()
    second = ReferenceStore(path)
    assert second.get(ref, owner="visitor-1", scope="session-1") == payload
    second.close()


@pytest.mark.parametrize("owner,scope", [("bob", "session-1"), ("alice", "session-2")])
def test_scope_denial_and_unknown_reference_have_same_public_error(owner: str, scope: str) -> None:
    store = ReferenceStore()
    ref = store.put("result", owner="alice", scope="session-1", payload={"secret": "hidden"})
    with pytest.raises(KTGError) as denied:
        store.get(ref, owner=owner, scope=scope)
    for unknown in ("res_" + "a" * 43, "../refs.db", "", "rec_7"):
        with pytest.raises(KTGError) as missing:
            store.get(unknown, owner=owner, scope=scope)
        assert (missing.value.code, str(missing.value)) == (denied.value.code, str(denied.value))
    assert denied.value.code == "REFERENCE_UNAVAILABLE"
    assert "hidden" not in str(denied.value)
    store.close()


def test_ttl_boundary_does_not_slide_and_wrong_owner_cannot_probe_expiry() -> None:
    now = [1000.0]
    store = ReferenceStore(ttl_seconds=10, clock=lambda: now[0])
    ref = store.put("result", owner="alice", scope="s", payload={"n": 1})
    now[0] = 1009
    assert store.get(ref, owner="alice", scope="s") == {"n": 1}
    now[0] = 1010
    with pytest.raises(KTGError) as expired:
        store.get(ref, owner="alice", scope="s")
    assert expired.value.code == "REFERENCE_EXPIRED"
    with pytest.raises(KTGError) as denied:
        store.get(ref, owner="bob", scope="s")
    assert denied.value.code == "REFERENCE_UNAVAILABLE"
    assert store.prune() == 1
    assert store.prune() == 0
    store.close()


def test_per_reference_ttl_and_prune_keep_unexpired_rows() -> None:
    now = [0.0]
    store = ReferenceStore(ttl_seconds=20, clock=lambda: now[0])
    store.put("record", owner="a", scope="s", payload={}, ttl_seconds=5)
    live = store.put("result", owner="a", scope="s", payload={})
    now[0] = 5
    assert store.prune() == 1
    assert store.get(live, owner="a", scope="s") == {}
    store.close()


def test_concurrent_operations_and_unique_tokens(tmp_path: Path) -> None:
    store = ReferenceStore(tmp_path / "refs.db")

    def round_trip(index: int) -> str:
        ref = store.put("result", owner="a", scope="s", payload={"index": index})
        assert store.get(ref, owner="a", scope="s") == {"index": index}
        return ref

    with ThreadPoolExecutor(max_workers=8) as pool:
        refs = list(pool.map(round_trip, range(100)))
    assert len(set(refs)) == 100
    store.close()
    store.close()


@pytest.mark.parametrize("ttl", [0, -1, float("nan"), float("inf"), True])
def test_invalid_default_ttl_is_rejected(ttl: float) -> None:
    with pytest.raises(ValueError):
        ReferenceStore(ttl_seconds=ttl)


@pytest.mark.parametrize("owner,scope", [("", "s"), ("a", ""), ("a", " ")])
def test_empty_identity_components_cannot_sign_references(owner: str, scope: str) -> None:
    store = ReferenceStore()
    with pytest.raises(KTGError) as invalid:
        store.put("record", owner=owner, scope=scope, payload={})
    assert invalid.value.code == "VALIDATION_ERROR"
    store.close()


@pytest.mark.parametrize("bad", [{"n": float("nan")}, {"n": float("inf")}, {"n": (1, 2)}, {1: "value"}, {"n": object()}])
def test_payload_must_be_strict_json(bad: object) -> None:
    store = ReferenceStore()
    with pytest.raises(KTGError) as invalid:
        store.put("result", owner="a", scope="s", payload=cast(JsonObject, bad))
    assert invalid.value.code == "VALIDATION_ERROR"
    store.close()


def test_oversized_circular_and_deep_payloads_are_rejected() -> None:
    store = ReferenceStore()
    circle: JsonObject = {}
    circle["self"] = circle
    deep: JsonObject = {}
    for _ in range(80):
        deep = {"child": deep}
    payloads: list[JsonObject] = [{"text": "x" * MAX_PAYLOAD_BYTES}, circle, deep]
    for payload in payloads:
        with pytest.raises(KTGError) as invalid:
            store.put("result", owner="a", scope="s", payload=payload)
        assert invalid.value.code == "VALIDATION_ERROR"
    store.close()


def test_invalid_kind_and_per_reference_ttl_are_rejected() -> None:
    store = ReferenceStore()
    with pytest.raises(KTGError):
        store.put(cast(ReferenceKind, "unknown"), owner="a", scope="s", payload={})
    for ttl in (0, -1, float("nan"), float("inf")):
        with pytest.raises(KTGError):
            store.put("result", owner="a", scope="s", payload={}, ttl_seconds=ttl)
    store.close()


def test_focus_is_owner_and_scope_bound_and_clear_is_local() -> None:
    store = ReferenceStore()
    alice = store.put("record", owner="alice", scope="s1", payload={"id": 1})
    bob = store.put("record", owner="bob", scope="s1", payload={"id": 2})
    other_session = store.put("record", owner="alice", scope="s2", payload={"id": 3})
    store.set_focus(owner="alice", scope="s1", record_ref=alice)
    store.set_focus(owner="bob", scope="s1", record_ref=bob)
    store.set_focus(owner="alice", scope="s2", record_ref=other_session)
    assert store.get_focus(owner="alice", scope="s1") == alice
    assert store.get_focus(owner="bob", scope="s1") == bob
    assert store.get_focus(owner="alice", scope="s2") == other_session
    assert store.get_focus(owner="unknown", scope="s1") is None
    store.clear_focus(owner="alice", scope="s1")
    store.clear_focus(owner="alice", scope="s1")
    assert store.get_focus(owner="alice", scope="s1") is None
    assert store.get_focus(owner="bob", scope="s1") == bob
    assert store.get_focus(owner="alice", scope="s2") == other_session
    store.close()


def test_focus_set_rejects_unknown_wrong_owner_wrong_scope_kind_and_expiry_atomically() -> None:
    now = [1000.0]
    store = ReferenceStore(ttl_seconds=100, clock=lambda: now[0])
    valid = store.put("record", owner="alice", scope="s", payload={"id": 1})
    bob = store.put("record", owner="bob", scope="s", payload={"id": 2})
    other_scope = store.put("record", owner="alice", scope="other", payload={"id": 3})
    result = store.put("result", owner="alice", scope="s", payload={"rows": []})
    expired = store.put("record", owner="alice", scope="s", payload={}, ttl_seconds=1)
    now[0] += 1
    store.set_focus(owner="alice", scope="s", record_ref=valid)
    messages = set()
    for reference in (bob, other_scope, result, "rec_" + "a" * 43, "../refs.db"):
        with pytest.raises(KTGError) as denied:
            store.set_focus(owner="alice", scope="s", record_ref=reference)
        assert denied.value.code == "REFERENCE_UNAVAILABLE"
        messages.add(str(denied.value))
        assert store.get_focus(owner="alice", scope="s") == valid
    assert len(messages) == 1
    with pytest.raises(KTGError) as expiry:
        store.set_focus(owner="alice", scope="s", record_ref=expired)
    assert expiry.value.code == "REFERENCE_EXPIRED"
    assert store.get_focus(owner="alice", scope="s") == valid
    store.close()


def test_focus_recovers_after_legacy_database_upgrade_and_reopen(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old-reference-store.db"
    # Existing databases contain the old references table and no focus table.
    with sqlite3.connect(path) as legacy:
        legacy.execute("""CREATE TABLE exploration_references (
            reference TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL,
            scope TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
            expires_at REAL NOT NULL)""")
    now = [1000.0]
    first = ReferenceStore(path, ttl_seconds=10, clock=lambda: now[0])
    record = first.put("record", owner="visitor:scope1", scope="session:s", payload={"id": 1})
    first.set_focus(owner="visitor:scope1", scope="session:s", record_ref=record)
    first.close()
    reopened = ReferenceStore(path, clock=lambda: now[0])
    assert reopened.get_focus(owner="visitor:scope1", scope="session:s") == record
    assert reopened.get_focus(owner="visitor:scope2", scope="session:s") is None
    now[0] = 1009.0
    assert reopened.get_focus(owner="visitor:scope1", scope="session:s") == record
    now[0] = 1010.0
    assert reopened.get_focus(owner="visitor:scope1", scope="session:s") is None
    with sqlite3.connect(path) as inspect:
        assert inspect.execute("SELECT count(*) FROM exploration_focus").fetchone()[0] == 0
    reopened.close()


def test_focus_ttl_does_not_slide_and_prune_removes_only_expired_focus(tmp_path: Path) -> None:
    now = [0.0]
    path = tmp_path / "refs.db"
    store = ReferenceStore(path, ttl_seconds=10, clock=lambda: now[0])
    old = store.put("record", owner="a", scope="s", payload={}, ttl_seconds=5)
    live = store.put("record", owner="b", scope="s", payload={})
    store.set_focus(owner="a", scope="s", record_ref=old)
    store.set_focus(owner="b", scope="s", record_ref=live)
    now[0] = 4.0
    assert store.get_focus(owner="a", scope="s") == old
    store.set_focus(owner="a", scope="s", record_ref=old)
    now[0] = 5.0
    assert store.prune() == 1
    assert store.get_focus(owner="a", scope="s") is None
    assert store.get_focus(owner="b", scope="s") == live
    store.close()
    reopened = ReferenceStore(path, clock=lambda: now[0])
    assert reopened.get_focus(owner="a", scope="s") is None
    assert reopened.get_focus(owner="b", scope="s") == live
    reopened.close()


def test_focus_replacement_does_not_delete_old_record_and_concurrent_updates_stay_unique(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "refs.db"
    store = ReferenceStore(path)
    references = [store.put("record", owner="a", scope="s", payload={"id": i}) for i in range(20)]
    store.set_focus(owner="a", scope="s", record_ref=references[0])
    store.set_focus(owner="a", scope="s", record_ref=references[1])
    assert store.get_focus(owner="a", scope="s") == references[1]
    assert store.get(references[0], owner="a", scope="s") == {"id": 0}

    def update(reference: str) -> None:
        store.set_focus(owner="a", scope="s", record_ref=reference)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(update, references))
    assert store.get_focus(owner="a", scope="s") in references
    with sqlite3.connect(path) as inspect:
        assert inspect.execute("SELECT count(*) FROM exploration_focus").fetchone()[0] == 1
    store.clear_focus(owner="a", scope="s")
    assert store.get(references[1], owner="a", scope="s") == {"id": 1}
    store.close()


def test_expired_foreign_reference_is_indistinguishable_and_closed_focus_fails() -> None:
    now = [0.0]
    store = ReferenceStore(ttl_seconds=1, clock=lambda: now[0])
    foreign = store.put("record", owner="bob", scope="s", payload={"id": 1})
    now[0] = 1.0
    with pytest.raises(KTGError) as failure:
        store.set_focus(owner="alice", scope="s", record_ref=foreign)
    assert failure.value.code == "REFERENCE_UNAVAILABLE"
    assert store.get_focus(owner="alice", scope="s") is None
    store.close()
    with pytest.raises(KTGError) as closed:
        store.get_focus(owner="alice", scope="s")
    assert closed.value.code == "REFERENCE_STORE_CLOSED"
    with pytest.raises(KTGError):
        store.set_focus(owner="bob", scope="s", record_ref=foreign)
    with pytest.raises(KTGError):
        store.clear_focus(owner="alice", scope="s")
