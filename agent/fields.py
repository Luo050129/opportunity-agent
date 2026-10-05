"""把抽取条目组装成 CRM 十字段字段，并生成未确认信息清单。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .extract import evidence_of
from .textutil import dedupe, trim

FIELD_ORDER = [
    "需求", "核心场景", "预算", "决策人", "影响人",
    "时间计划", "阶段", "风险", "下一步", "未确认信息",
]


def _status(items: Sequence[Dict[str, Any]]) -> str:
    if not items:
        return "unconfirmed"
    if any(not item.get("vague") for item in items):
        return "confirmed"
    return "inferred"


def _field(
    name: str,
    value: Optional[str],
    status: str,
    evidence: Optional[List[Dict[str, Any]]] = None,
    items: Optional[List[Any]] = None,
    notes: Optional[List[str]] = None,
    rule_refs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "field": name,
        "value": value,
        "status": status,
        "evidence": evidence or [],
        "items": items or [],
        "notes": notes or [],
        "rule_refs": rule_refs or [],
    }


def _text_field(name: str, items: Sequence[Dict[str, Any]], limit: int = 3, rule_refs: Optional[List[str]] = None) -> Dict[str, Any]:
    selected, notes = _select_items(items, "text")
    selected = selected[:limit]
    value = "；".join(dedupe(item["text"] for item in selected)) or None
    return _field(
        name,
        value,
        _status(selected),
        [evidence_of(item, item["text"]) for item in selected],
        [{"text": item["text"], "sentence": trim(item["sentence"], 160)} for item in selected],
        notes,
        rule_refs=rule_refs,
    )


def _select_items(items: Sequence[Dict[str, Any]], label_key: str):
    """优先使用可确认为事实的条目；纯推断条目降级到 notes，避免把推断写成事实。"""
    confirmed = [item for item in items if not item.get("vague")]
    inferred = [item for item in items if item.get("vague")]
    if confirmed:
        notes = [
            f"推断性表述（未确认为事实）：{item.get(label_key, '')}｜原话：{trim(item.get('sentence', ''), 60)}"
            for item in inferred[:3]
        ]
        return confirmed, notes
    return inferred, []


def build_fields(raw: Dict[str, Any], lex: Dict[str, Any], conflicts: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    fields: Dict[str, Dict[str, Any]] = {}
    fields["需求"] = _text_field("需求", raw.get("needs", []), rule_refs=["R-01", "R-02"])
    fields["核心场景"] = _text_field("核心场景", raw.get("scenes", []), rule_refs=["R-01", "R-02"])

    budget_items = raw.get("budget_items", [])
    budget_notes = raw.get("budget_notes", [])
    selected_budget, budget_inferred_notes = _select_items(budget_items, "display")
    budget_value = "；".join(dedupe(item["display"] for item in selected_budget)) or None
    budget_note_lines = budget_inferred_notes + [note["reason"] for note in budget_notes]
    fields["预算"] = _field(
        "预算",
        budget_value,
        _status(selected_budget),
        [evidence_of(item) for item in selected_budget],
        [
            {
                "display": item["display"],
                "amount": item["value"],
                "currency": item["currency"],
                "scope": item["scope"],
                "sentence": trim(item["sentence"], 160),
            }
            for item in budget_items
        ],
        budget_note_lines,
        ["R-01", "R-03", "R-04", "R-05"],
    )
    fields["预算"]["notes"] += [
        f"命中矛盾：{conflict['message']}" for conflict in conflicts if conflict["field"] == "预算"
    ]

    decision_items = raw.get("decision_items", [])
    selected_decisions, decision_notes = _select_items(decision_items, "display")
    fields["决策人"] = _field(
        "决策人",
        "；".join(dedupe(item["display"] for item in selected_decisions)) or None,
        _status(selected_decisions),
        [evidence_of(item) for item in selected_decisions],
        [{"display": item["display"], "kind": item["kind"], "sentence": trim(item["sentence"], 160)} for item in decision_items],
        decision_notes + [note["reason"] for note in raw.get("people_notes", []) if "否定" in note["reason"]],
        ["R-01", "R-03", "R-04", "R-05"],
    )
    fields["决策人"]["notes"] += [
        f"命中矛盾：{conflict['message']}" for conflict in conflicts if conflict["field"] == "决策人"
    ]

    influence_items = raw.get("influence_items", [])
    selected_influence, influence_notes = _select_items(influence_items, "display")
    fields["影响人"] = _field(
        "影响人",
        "；".join(dedupe(item["display"] for item in selected_influence)) or None,
        _status(selected_influence),
        [evidence_of(item) for item in selected_influence],
        [{"display": item["display"], "kind": item["kind"]} for item in influence_items],
        influence_notes,
        rule_refs=["R-01", "R-03"],
    )

    time_items = raw.get("time_items", [])
    selected_time, time_notes = _select_items(time_items, "display")
    fields["时间计划"] = _field(
        "时间计划",
        "；".join(dedupe(item["display"] for item in selected_time)) or None,
        _status(selected_time),
        [evidence_of(item) for item in selected_time],
        [{"display": item["display"], "milestone": item.get("milestone"), "sentence": trim(item["sentence"], 160)} for item in time_items],
        time_notes,
        rule_refs=["R-01", "R-03", "R-04", "R-05"],
    )
    fields["时间计划"]["notes"] += [
        f"命中矛盾：{conflict['message']}" for conflict in conflicts if conflict["field"] == "时间计划"
    ]

    risks = raw.get("risks", [])
    if risks:
        fields["风险"] = _field(
            "风险",
            "；".join(dedupe(item["message"] for item in risks))[:400],
            "confirmed" if any(item.get("confirmed") for item in risks) else "inferred",
            [{"quote": item["quote"], "matched": item.get("keyword", ""), "source": item.get("source", "input"), "turn": item.get("turn", 0)} for item in risks],
            [{"type": item["type"], "message": item["message"], "severity": item["severity"]} for item in risks],
            rule_refs=["R-05"],
        )
    else:
        fields["风险"] = _field(
            "风险",
            "未识别到风险信号",
            "inferred",
            notes=["按当前规则库未命中风险信号；如存在未记录的客户顾虑，需人工补充。"],
            rule_refs=["R-05"],
        )

    steps = raw.get("steps", [])
    selected_steps, step_notes = _select_items(steps, "action")
    step_items = [
        {
            "action": item["action"],
            "owner": item["owner"],
            "due": item["due"],
            "sentence": trim(item["sentence"], 160),
        }
        for item in selected_steps
    ]
    step_value = "；".join(
        f"{index + 1}) {item['action']}（负责人：{item['owner']}；时间：{item['due']}）"
        for index, item in enumerate(step_items)
    ) or None
    fields["下一步"] = _field(
        "下一步",
        step_value,
        _status(selected_steps),
        [evidence_of(item, item["action"]) for item in selected_steps],
        step_items,
        step_notes,
        rule_refs=["R-06"],
    )
    return fields


def build_unconfirmed(
    fields: Dict[str, Dict[str, Any]],
    conflicts: Sequence[Dict[str, Any]],
    stage: Dict[str, Any],
    skipped_fields: Sequence[str] = (),
) -> Dict[str, Any]:
    entries: List[Dict[str, str]] = []
    skipped = set(skipped_fields)
    for name in ["需求", "核心场景", "预算", "决策人", "影响人", "时间计划"]:
        field = fields.get(name)
        if not field or field["status"] == "confirmed":
            continue
        if name in skipped:
            reason = "已确认客户未提及，按 R-03 保留未确认标注，不做补全"
        elif field["value"] and field["status"] == "inferred":
            reason = "仅有推断性表达，未获得客户明确确认"
        else:
            reason = "原始记录中未提供，按 R-03 标注未确认、不做补全"
        if field["notes"]:
            reason = f"{reason}（{'；'.join(field['notes'][:1])}）"
        entries.append({"field": name, "reason": reason})
    for conflict in conflicts:
        entries.append({"field": conflict["field"], "reason": f"存在矛盾待确认：{conflict['message']}"})
    for item in fields.get("下一步", {}).get("items", []):
        missing = []
        if item["owner"] == "待确认":
            missing.append("负责人")
        if item["due"] == "待确认":
            missing.append("时间")
        if missing:
            entries.append({
                "field": "下一步",
                "reason": f"动作「{trim(item['action'], 30)}」缺少{'、'.join(missing)}，按 R-06 标注待确认",
            })
    if stage.get("code") == "unknown":
        entries.append({"field": "阶段", "reason": stage.get("cannot_judge_reason", "证据不足，无法判定阶段")})
    elif stage.get("conflicts"):
        entries.append({"field": "阶段", "reason": stage.get("cannot_judge_reason") or "阶段涉及字段存在矛盾，需人工确认"})
    value = "；".join(f"{entry['field']}：{entry['reason']}" for entry in entries[:8]) or None
    status = "confirmed"
    return _field(
        "未确认信息",
        value if entries else "无未确认信息（关键字段均有原话依据）",
        status,
        [],
        entries,
        rule_refs=["R-03", "R-05", "R-06"],
    )
