"""矛盾检测（R-05）：只做“同时记录 + 标注待确认”，绝不替客户选边。"""
from __future__ import annotations

from itertools import combinations
from typing import Any, Dict, List, Sequence

from .textutil import contains_any


def _evidence(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {"quote": item.get("sentence", ""), "source": item.get("source", "input"), "turn": item.get("turn", 0)}
        for item in items
    ]


def _budget_conflicts(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    conflicts: List[Dict[str, Any]] = []
    by_currency: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        by_currency.setdefault(item["currency"], []).append(item)
    for group in by_currency.values():
        distinct: Dict[float, Dict[str, Any]] = {}
        for item in group:
            distinct.setdefault(round(float(item["value"]), 2), item)
        if len(distinct) < 2:
            continue
        keys = sorted(distinct)
        for left, right in combinations(keys, 2):
            scopes = {distinct[left]["scope"], distinct[right]["scope"]}
            if scopes == {"总预算", "项目"}:
                continue  # 总预算与项目投入口径不同，不算矛盾
            conflicts.append({
                "field": "预算",
                "type": "金额矛盾",
                "message": (
                    f"预算出现两个不同金额（{distinct[left]['display']} 与 {distinct[right]['display']}），"
                    "按 R-05 同时记录并标为待确认，需人工确认哪一版为准。"
                ),
                "values": [distinct[left]["display"], distinct[right]["display"]],
                "evidence": _evidence([distinct[left], distinct[right]]),
            })
            break
        if conflicts:
            break
    return conflicts


def _time_conflicts(items: Sequence[Dict[str, Any]], lex: Dict[str, Any]) -> List[Dict[str, Any]]:
    if len(items) < 2:
        return []
    # 优先：同一句里出现“推到/推迟”等改期词，说明客户对同一计划给了两个时间
    by_sentence: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        by_sentence.setdefault(item.get("sentence", ""), []).append(item)
    for sentence, group in by_sentence.items():
        texts = {item["text"] for item in group}
        if len(texts) >= 2 and contains_any(sentence, lex["reschedule_markers"]):
            first = group[0]
            second = next(item for item in group if item["text"] != first["text"])
            return [{
                "field": "时间计划",
                "type": "时间矛盾",
                "message": (
                    f"客户对同一计划先后给出两个时间（{first['display']} 与 {second['display']}），"
                    "按 R-05 同时记录并标为待确认。"
                ),
                "values": [first["display"], second["display"]],
                "evidence": _evidence([first, second]),
            }]
    # 其次：同一里程碑出现两个不同时间
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(item.get("milestone") or "未标注里程碑", []).append(item)
    for milestone, group in grouped.items():
        texts = {item["text"] for item in group}
        if len(texts) >= 2:
            first = group[0]
            second = next(item for item in group if item["text"] != first["text"])
            return [{
                "field": "时间计划",
                "type": "时间矛盾",
                "message": (
                    f"同一里程碑「{milestone}」出现两个时间（{first['display']} 与 {second['display']}），"
                    "按 R-05 同时记录并标为待确认。"
                ),
                "values": [first["display"], second["display"]],
                "evidence": _evidence([first, second]),
            }]
    return []


def _decision_conflicts(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    persons: List[Dict[str, Any]] = []
    seen = set()
    for item in items:
        if item.get("kind") != "person":
            continue
        if item["name"] in seen:
            continue
        seen.add(item["name"])
        persons.append(item)
    if len(persons) < 2:
        return []
    return [{
        "field": "决策人",
        "type": "决策人矛盾",
        "message": (
            f"决策人出现多个表述（{'、'.join(p['name'] for p in persons)}），"
            "按 R-05 同时记录并标为待确认，需人工确认最终决策人。"
        ),
        "values": [p["display"] for p in persons],
        "evidence": _evidence(persons),
    }]


def _requirement_conflicts(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    invalid = raw.get("invalid_markers") or []
    needs = raw.get("needs") or []
    if not invalid or not needs:
        return []
    return [{
        "field": "需求",
        "type": "需求失效",
        "message": (
            f"原文既描述了客户需求，又出现需求失效表述「{invalid[0]['keyword']}」，"
            "按 R-05 同时记录并标为风险。"
        ),
        "values": [needs[0]["text"], invalid[0]["keyword"]],
        "evidence": _evidence([needs[0], invalid[0]]),
    }]


def detect_conflicts(raw: Dict[str, Any], lex: Dict[str, Any]) -> List[Dict[str, Any]]:
    conflicts: List[Dict[str, Any]] = []
    conflicts.extend(_budget_conflicts(raw.get("budget_items", [])))
    conflicts.extend(_time_conflicts(raw.get("time_items", []), lex))
    conflicts.extend(_decision_conflicts(raw.get("decision_items", [])))
    conflicts.extend(_requirement_conflicts(raw))
    return conflicts
