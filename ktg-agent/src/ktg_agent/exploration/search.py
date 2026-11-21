"""Offline candidate retrieval with inspectable lexical and domain evidence.

This module neither fetches records nor authorizes access. Callers supply only
already-authorized fields. Character similarity and a curated domain lexicon
are not pretrained semantic retrieval. Optional encoders must be provisioned
locally by the caller; this module never downloads models or makes requests.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from math import isfinite, sqrt
from typing import Protocol
import re
import unicodedata

from ktg_agent.core.messages import JsonObject
from ktg_agent.exploration.catalog import EntitySpec


class SemanticEncoder(Protocol):
    """Injected, offline encoding backend. Input order must be preserved."""

    def encode(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


# Generic concepts and related concepts, not acceptance-question templates.
_CONCEPTS: tuple[tuple[str, ...], ...] = (
    ("漏油", "渗油", "油液泄漏", "油液渗漏", "油液渗出", "油渗出来", "油管渗漏", "液压管路渗漏", "液压泄漏"),
    ("异响", "噪声异常", "不正常的声音", "异常噪音", "响声异常"),
    ("发烫", "过热", "温度过高", "异常发热", "高温报警"),
    ("卡顿", "卡住", "卡滞", "堵转", "无法转动"),
    ("坏掉", "损坏", "故障", "坏了", "运行异常"),
    ("装配", "组装"),
    ("维修", "检修", "修理"),
    ("工单", "生产订单"),
)
_RELATED = {"漏油": ("密封件损坏", "密封圈破损", "密封老化", "密封失效")}
_ENTITY_TERMS: Mapping[str, tuple[str, ...]] = {
    "dv.machinery": ("设备", "机器", "机台"),
    "dv.repair": ("维修", "检修", "修理", "报修"),
    "dv.repair_line": ("维修内容", "故障现象", "维修项目"),
    "pro.workorder": ("工单", "生产订单", "生产单"),
    "pro.task": ("生产任务", "工序任务"),
    "pro.feedback": ("报工", "报产"),
    "md.item": ("物料", "产品", "料号"),
    "md.client": ("客户",), "md.workshop": ("车间",),
    "tm.tool": ("工装", "夹具"),
    "wm.stock": ("库存", "存货", "存量"),
}
_PREFIX_PATTERNS = (
    re.compile(r"(?P<prefix>[A-Za-z][A-Za-z0-9_-]{0,63})\s*(?:开头|打头|起头)"),
    re.compile(r"(?:开头(?:是|为)|前缀(?:是|为|:|：)?)\s*(?P<prefix>[A-Za-z][A-Za-z0-9_-]{0,63})"),
)
_TIME_PHRASES = {
    "最近一次": "latest", "最近那次": "latest", "最后一次": "latest",
    "最后一张": "latest", "最新": "latest", "最近": "latest",
    "上次": "previous", "上回": "previous", "之前": "previous",
    "上周": "relative_calendar", "本周": "relative_calendar",
    "上个月": "relative_calendar", "这个月": "relative_calendar",
    "前一阵子": "relative_unspecified", "前阵子": "relative_unspecified",
    "前些天": "relative_unspecified",
}
_STOP_PHRASES = (
    "帮我找一下", "帮我查一下", "找一下", "查一下", "帮我", "找找", "查查",
    "查询", "查看", "看看", "一下", "那个", "那台", "这台", "那次", "哪台",
    "机器", "设备", "机台", "总是", "发生", "开头", "编号", "编码",
    "之前", "最近", "以前", "是否", "情况", "一台", "的", "且", "和", "有",
)
_CHINESE_DIGITS = dict(zip("零一二三四五六七八九", range(10)))


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _number(text: str) -> int | None:
    if text.isdecimal():
        return int(text)
    if text in _CHINESE_DIGITS:
        return _CHINESE_DIGITS[text]
    if "十" in text and text.count("十") == 1:
        tens, units = text.split("十")
        if tens and tens not in _CHINESE_DIGITS or units and units not in _CHINESE_DIGITS:
            return None
        return _CHINESE_DIGITS.get(tens, 1) * 10 + _CHINESE_DIGITS.get(units, 0)
    return None


def _terms(text: str) -> list[str]:
    # Aspect particles vary independently of the manufacturing vocabulary.
    text = re.sub(r"(?<=[漏渗坏停修响卡])[过了着]", "", text)
    for phrase in sorted(_STOP_PHRASES, key=len, reverse=True):
        text = text.replace(phrase, " ")
    terms: list[str] = []
    for phrase in sorted({word for group in _CONCEPTS for word in group}, key=len, reverse=True):
        if phrase in text:
            terms.append(phrase)
            text = text.replace(phrase, " ")
    terms.extend(re.findall(r"[a-z0-9][a-z0-9_-]*|[\u4e00-\u9fff]{2,}", text))
    return list(dict.fromkeys(terms))


def parse_clues(query: str) -> JsonObject:
    """Extract explicit clues, without choosing objects or inventing date bounds.

    relative_days is an interval length, not an absolute cutoff. The caller must
    resolve time against its clock/timezone and select the business date field.
    """
    text = unicodedata.normalize("NFKC", query[:1024])
    result: JsonObject = {}
    for pattern in _PREFIX_PATTERNS:
        match = pattern.search(text)
        if match:
            leading = text[:match.start()]
            negative = re.search(r"(?:不是|不要|排除|除了|不以|非)\s*$", leading)
            result["excluded_code_prefix" if negative else "code_prefix"] = match.group("prefix")
            result["prefix_text"] = match.group(0)
            remaining = text[match.end():]
            text = leading[:negative.start()] if negative else leading
            text += " " + remaining
            break
    relative = re.search(r"(?:最近|近|过去)\s*([0-9零一二三四五六七八九十]{1,4})\s*天", text)
    if relative and (days := _number(relative.group(1))) is not None:
        result.update(time_hint="relative_period", time_text=relative.group(0), relative_days=days)
        text = text.replace(relative.group(0), " ")
    else:
        for phrase, hint in _TIME_PHRASES.items():
            if phrase in text:
                result.update(time_hint=hint, time_text=phrase)
                text = text.replace(phrase, " ")
                break
    # Time-only requests should be listed/sorted by the engine, not similarity.
    if "time_hint" in result:
        text = re.sub(r"(?:一次|那次|维修|检修|修理)", " ", text)
    result["terms"] = [term for term in _terms(_normalize(text))]
    return result


def infer_entities(query: str, available: Sequence[EntitySpec]) -> list[str]:
    """Infer resource kinds from names only within the caller's visible catalog."""
    text = _normalize(query)
    ranked: list[tuple[int, str]] = []
    for entity in available:
        aliases = (entity.label, entity.key, *_ENTITY_TERMS.get(entity.key, ()))
        score = sum(len(alias) for alias in aliases if _normalize(alias) in text)
        if score:
            ranked.append((-score, entity.key))
    return [key for _, key in sorted(ranked)]


def semantic_status(encoder: SemanticEncoder | None = None) -> JsonObject:
    return {"mode": "lexical_domain_expansion_character_similarity",
            "pretrained_semantic_configured": encoder is not None,
            "encoder": type(encoder).__name__ if encoder is not None else None,
            "automatic_model_download": False}


def _evidence(field: str, raw: str, term: str, matched: str, method: str,
              score: float, *, expanded: str | None = None) -> JsonObject:
    return {"field": field, "raw_text": raw, "query_term": term,
            "matched_text": matched, "expanded_term": expanded,
            "method": method, "strength": "strong" if method in {"lexical", "prefix", "prefix_exclusion"} else "weak",
            "score": score, "proves_cause": False}


def _lexical_match(term: str, fields: Mapping[str, str]) -> tuple[float, JsonObject | None]:
    for field, raw in fields.items():
        if term in _normalize(raw):
            return 0.95, _evidence(field, raw, term, term, "lexical", 0.95)
    for group in _CONCEPTS:
        if term not in group:
            continue
        for alias in group:
            for field, raw in fields.items():
                if alias in _normalize(raw):
                    return 0.7, _evidence(field, raw, term, alias, "domain_synonym", 0.7, expanded=alias)
        for alias in _RELATED.get(group[0], ()):
            for field, raw in fields.items():
                if alias in _normalize(raw):
                    return 0.25, _evidence(field, raw, term, alias, "domain_related", 0.25, expanded=alias)
    if not 3 <= len(term) <= 64:
        return 0.0, None
    best: tuple[float, str, str, str] = (0.0, "", "", "")
    characters = set(term)
    minimum_overlap = max(1, (len(characters) + 1) // 2)
    for field, raw in fields.items():
        normalized = _normalize(raw)[:4000]
        if len(characters.intersection(normalized)) < minimum_overlap:
            continue
        # Bounded local windows tolerate a typo inside a longer equipment name.
        for length in (len(term) - 1, len(term), len(term) + 1):
            for start in range(max(0, len(normalized) - length + 1)):
                part = normalized[start:start + length]
                if len(characters.intersection(part)) < minimum_overlap:
                    continue
                ratio = SequenceMatcher(None, term, part, autojunk=False).ratio()
                if ratio > best[0]:
                    best = ratio, field, raw, part
    if best[0] >= 0.66:
        ratio, field, raw, matched = best
        score = 0.45 * ratio
        return score, _evidence(field, raw, term, matched, "character_similarity", score)
    return 0.0, None


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left or not all(isfinite(x) for x in (*left, *right)):
        raise ValueError("Invalid local semantic encoder vectors")
    norm = sqrt(sum(x * x for x in left) * sum(x * x for x in right))
    return sum(x * y for x, y in zip(left, right)) / norm if norm else 0.0


def _identity_fields(fields: Mapping[str, str]) -> list[tuple[str, str]]:
    return [(field, raw) for field, raw in fields.items()
            if "." not in field and _normalize(field).endswith(
                ("code", "name", "spec", "specification", "model", "编号", "名称", "型号", "规格"))]


def rank_record(query: str, fields: Mapping[str, str], *,
                encoder: SemanticEncoder | None = None) -> tuple[float, list[JsonObject]]:
    """Rank one candidate from authorized root fields and related evidence.

    Root identity fields use unqualified logical keys (code/name/specification/
    model), or their wire names. Related fields use dotted keys; their IDs must
    not satisfy a root-object prefix. Scores are ranking signals, not calibrated
    probabilities. Weak candidates never prove identity, state or causality.
    """
    clues = parse_clues(query)
    evidence: list[JsonObject] = []
    prefix = clues.get("code_prefix")
    if isinstance(prefix, str):
        for field, raw in _identity_fields(fields):
            if _normalize(raw).startswith(_normalize(prefix)):
                evidence.append(_evidence(field, raw, prefix, raw[:len(prefix)], "prefix", 0.98))
                break
        if not evidence:
            return 0.0, []
    excluded = clues.get("excluded_code_prefix")
    if isinstance(excluded, str):
        identity = _identity_fields(fields)
        if not identity or any(_normalize(raw).startswith(_normalize(excluded)) for _, raw in identity):
            return 0.0, []
        field, raw = identity[0]
        evidence.append(_evidence(field, raw, excluded, raw, "prefix_exclusion", 0.98))
    terms = clues.get("terms")
    assert isinstance(terms, list)
    if not terms:
        return (0.98 if evidence else 0.0), evidence
    scores: list[float] = []
    for term in terms:
        assert isinstance(term, str)
        score, match = _lexical_match(term, fields)
        scores.append(score)
        if match is not None:
            evidence.append(match)
    score = sum(scores) / len(scores)
    if encoder is not None and fields:
        items = list(fields.items())
        vectors = encoder.encode([query, *(raw for _, raw in items)])
        if len(vectors) != len(items) + 1:
            raise ValueError("Local semantic encoder returned wrong vector count")
        similarities = [_cosine(vectors[0], vector) for vector in vectors[1:]]
        index = max(range(len(items)), key=similarities.__getitem__)
        similarity = similarities[index]
        if similarity >= 0.75 and similarity * 0.69 > score:
            field, raw = items[index]
            score = similarity * 0.69
            evidence.append(_evidence(field, raw, query, raw, "semantic_encoder", score))
    if prefix is not None:
        score = 0.25 * 0.98 + 0.75 * score
    return round(score, 6), evidence
