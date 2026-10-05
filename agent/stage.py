"""销售阶段判定（S0–S5）：自高向低扫描，必须有原话证据才能认定。"""
from __future__ import annotations

import re

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .textutil import contains_any, find_all, trim


def _hit_sentences(sentences: Sequence[str], keywords: Sequence[str]) -> List[Tuple[str, List[str]]]:
    hits: List[Tuple[str, List[str]]] = []
    for sentence in sentences:
        matched = [keyword for keyword in keywords if keyword and keyword in sentence]
        if matched:
            hits.append((sentence, matched))
    return hits


def _check_stage(stage: Dict[str, Any], fields: Dict[str, Any], sentences: Sequence[str], lex: Dict[str, Any]) -> Tuple[bool, List[Dict[str, Any]], Optional[str]]:
    check = stage.get("check", {})
    check_type = check.get("type")
    guards = stage.get("guards", [])

    if check_type == "field_status":
        matched_fields = [
            name for name in check.get("fields", [])
            if fields.get(name, {}).get("status") in check.get("status", ["confirmed"])
        ]
        if matched_fields:
            evidence = []
            for name in matched_fields:
                for item in fields[name]["evidence"]:
                    evidence.append({"quote": item["quote"], "matched": name, "source": item["source"], "turn": item["turn"]})
            return True, evidence, None
        detail = "、".join(
            f"{name}（当前状态：{'推断' if fields.get(name, {}).get('status') == 'inferred' else '未确认'}）"
            for name in check.get("fields", [])
        )
        return False, [], f"对应字段未达到“明确”要求：{detail}"

    keywords = check.get("keywords", [])
    hits = _hit_sentences(sentences, keywords)
    if not hits:
        sample = "、".join(keywords[:3])
        return False, [], f"未出现达成条件所需证据（关注词：{sample} 等）"

    require_agreement = check_type == "keywords_with_agreement"
    entry_markers = stage.get("entry_markers", [])
    valid: List[Tuple[str, List[str]]] = []
    blocked: List[Tuple[str, str, List[str]]] = []
    for sentence, matched in hits:
        positive = [keyword for keyword in matched if not _keyword_negated(sentence, keyword, lex)]
        if not positive:
            blocked.append((sentence, "关键词处于否定/未确认表述中（如“没提到”“还没定”），不构成达成证据", matched))
            continue
        guard_hits = find_all(sentence, guards)
        if guard_hits:
            blocked.append((sentence, f"同一句存在「{guard_hits[0]}」的计划/未完成口径", matched))
            continue
        if require_agreement and not contains_any(sentence, check.get("agreement", [])):
            blocked.append((sentence, "同一句未见客户明确同意或安排的表述", matched))
            continue
        if entry_markers and not contains_any(sentence, entry_markers):
            blocked.append((sentence, "只出现相关词，但未见“已进入/正在推进”的过程性表述", matched))
            continue
        valid.append((sentence, positive))

    if not valid:
        sentence, reason, matched = blocked[0]
        return False, [], f"提到「{'、'.join(matched[:2])}」，但{reason}：{trim(sentence, 60)}"

    extra = stage.get("extra_check")
    if extra and extra.get("type") == "requirement_valid":
        invalid_hits = [
            (sentence, find_all(sentence, extra.get("invalid_keywords", [])))
            for sentence in sentences
        ]
        invalid_hits = [(sentence, kws) for sentence, kws in invalid_hits if kws]
        if invalid_hits:
            sentence, kws = invalid_hits[0]
            return False, [], f"出现需求失效表达「{kws[0]}」，需求有效性不成立：{trim(sentence, 60)}"

    evidence = [
        {"quote": sentence, "matched": "、".join(matched[:3]), "source": "input", "turn": 0}
        for sentence, matched in valid
    ]
    return True, evidence, None


def _keyword_negated(sentence: str, keyword: str, lex: Dict[str, Any]) -> bool:
    for match in re.finditer(re.escape(keyword), sentence):
        window = sentence[max(0, match.start() - 8): match.end() + 8]
        if find_all(window, lex["negative_markers"]):
            return True
    return False


def judge_stage(
    fields: Dict[str, Any],
    raw: Dict[str, Any],
    conflicts: Sequence[Dict[str, Any]],
    lex: Dict[str, Any],
    stages: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    sentences = raw.get("sentences", [])
    chosen: Optional[Dict[str, Any]] = None
    chosen_evidence: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []

    for stage in stages:
        ok, evidence, reason = _check_stage(stage, fields, sentences, lex)
        if chosen is None:
            if ok:
                chosen = stage
                chosen_evidence = evidence
            else:
                blocked.append({
                    "code": stage["code"],
                    "name": stage["name"],
                    "condition": stage["condition"],
                    "reason": reason or "证据不足",
                    "evidence": [],
                })

    relevant_conflicts = [
        conflict for conflict in conflicts
        if conflict["field"] in {"预算", "决策人", "时间计划", "需求"}
    ]

    if chosen is None:
        return {
            "code": "unknown",
            "name": "无法判定",
            "value": "无法判定（建议人工复核）",
            "status": "unconfirmed",
            "condition": "",
            "satisfied_conditions": [],
            "blocked_by": blocked,
            "conflicts": relevant_conflicts,
            "cannot_judge_reason": "输入中未识别到可支撑 S0–S5 任一达成条件的证据，无法判定阶段，建议人工复核并补充关键信息。",
        }

    vague_evidence = any(find_all(item["quote"], lex["vague_markers"]) for item in chosen_evidence)
    status = "confirmed" if chosen_evidence and not vague_evidence else ("inferred" if chosen_evidence else "unconfirmed")
    cannot_judge_reason = None
    if relevant_conflicts:
        cannot_judge_reason = "阶段涉及的字段存在矛盾证据，已同时保留，需人工确认哪一版为准。"
    elif status != "confirmed":
        cannot_judge_reason = "阶段依据的原话含模糊表述，建议与客户确认后升级。"

    return {
        "code": chosen["code"],
        "name": chosen["name"],
        "value": f"{chosen['code']} {chosen['name']}",
        "status": status,
        "condition": chosen["condition"],
        "satisfied_conditions": [{
            "code": chosen["code"],
            "name": chosen["name"],
            "condition": chosen["condition"],
            "reason": "达成条件已满足，且保留原话证据",
            "evidence": chosen_evidence,
        }],
        "blocked_by": blocked,
        "conflicts": relevant_conflicts,
        "cannot_judge_reason": cannot_judge_reason,
    }
