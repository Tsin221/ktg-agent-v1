"""Held-out wording for offline clue retrieval, not real model acceptance."""

from collections.abc import Sequence

from ktg_agent.exploration.catalog import get_entity
from ktg_agent.exploration.search import infer_entities, parse_clues, rank_record, semantic_status


def test_prefix_is_a_constraint_even_when_fault_text_is_a_perfect_match() -> None:
    query = "找找 R 开头、之前漏过油的机器"
    clues = parse_clues(query)
    assert clues["code_prefix"] == "R"
    match, evidence = rank_record(query, {"code": "R-006", "repair.malfunction": "液压管路渗漏"})
    wrong, _ = rank_record(query, {"code": "AR-006", "repair.malfunction": "漏油"})
    assert match > 0 and wrong == 0
    assert any(row["method"] == "prefix" and row["strength"] == "strong" for row in evidence)
    assert any(row["raw_text"] == "液压管路渗漏" for row in evidence)


def test_domain_expansion_returns_original_evidence_and_related_damage_stays_weak() -> None:
    direct, direct_evidence = rank_record("漏油", {"fault": "停机检查：漏油"})
    expanded, expanded_evidence = rank_record("漏油", {"fault": "液压管路渗漏"})
    related, related_evidence = rank_record("漏油", {"fault": "密封件损坏"})
    unrelated, _ = rank_record("漏油", {"fault": "按钮颜色变更"})
    assert direct > expanded > related > unrelated == 0
    assert any(row["strength"] == "strong" for row in direct_evidence)
    assert all(row["strength"] == "weak" for row in expanded_evidence)
    assert all(row["method"] == "domain_related" for row in related_evidence)
    assert all(row["strength"] == "weak" and row["proves_cause"] is False for row in related_evidence)
    assert expanded_evidence[0]["raw_text"] == "液压管路渗漏"
    assert expanded_evidence[0]["query_term"] == "漏油"


def test_colloquial_variations_use_shared_aspect_and_domain_rules() -> None:
    for query in ("之前有油渗出来那台", "帮我找一下漏过油的设备", "查查油液泄漏"):
        score, evidence = rank_record(query, {"repair.line.malfunction": "油管渗漏，已处理"})
        assert score > 0, query
        assert any(row["raw_text"] == "油管渗漏，已处理" for row in evidence)
    for query in ("总是发烫的机台", "发生过卡顿的设备"):
        fault = "电机温度过高" if "发烫" in query else "主轴卡滞"
        assert rank_record(query, {"fault": fault})[0] > 0


def test_typos_are_candidates_and_are_not_called_semantic_models() -> None:
    score, evidence = rank_record("装佩机", {"name": "一号装配机"})
    assert 0 < score < 0.8
    assert evidence[0]["method"] == "character_similarity"
    assert evidence[0]["strength"] == "weak"
    assert semantic_status()["pretrained_semantic_configured"] is False


def test_clue_time_is_preserved_without_inventing_dates() -> None:
    latest = parse_clues("最近一次维修")
    assert latest["time_hint"] == "latest"
    assert latest["terms"] == []
    assert "from_time" not in latest
    relative = parse_clues("近七天 R 开头的设备")
    assert relative["relative_days"] == 7 and relative["code_prefix"] == "R"
    assert relative["time_text"] == "近七天"
    assert parse_clues("编号是 DEV-001").get("code_prefix") is None


def test_negative_prefix_is_not_silently_reversed_into_a_positive_constraint() -> None:
    clues = parse_clues("不是 R 开头的机器")
    assert "code_prefix" not in clues
    assert clues["excluded_code_prefix"] == "R"
    assert rank_record("不是 R 开头的机器", {"code": "R-006"})[0] == 0
    assert rank_record("不是 R 开头的机器", {"code": "X-006"})[0] > 0


def test_entities_are_only_inferred_from_authorized_available_resources() -> None:
    available = [get_entity("dv.machinery"), get_entity("dv.repair"), get_entity("pro.workorder")]
    assert "dv.machinery" in infer_entities("那个R开头的机器", available)
    assert "dv.repair" in infer_entities("检修的单子", available)
    assert "pro.workorder" in infer_entities("生产订单做到哪里", available)
    assert infer_entities("那个R开头的机器", [get_entity("pro.workorder")]) == []


def test_optional_encoder_is_explicit_and_never_promotes_similarity_to_fact() -> None:
    class LocalEncoder:
        def encode(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
            return [[1.0, 0.0] for _ in texts]

    encoder = LocalEncoder()
    score, evidence = rank_record("陌生表达", {"fault": "不同文本"}, encoder=encoder)
    assert score > 0
    assert evidence[0]["method"] == "semantic_encoder"
    assert evidence[0]["strength"] == "weak"
    assert semantic_status(encoder)["pretrained_semantic_configured"] is True
    assert rank_record("R开头", {"code": "X-001"}, encoder=encoder)[0] == 0
