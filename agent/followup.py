"""结构化追问生成：只针对未确认项、矛盾项和规则要求缺失项提问。"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

from .textutil import trim

QUESTIONS = {
    "需求": "客户本次最想解决的核心问题或业务目标是什么？（请尽量引用客户原话）",
    "核心场景": "这套方案具体用在哪个业务场景、哪些部门或门店？",
    "预算": "客户是否透露预算范围、可接受投入区间或报价反馈？",
    "决策人": "这个项目最终的决策人是谁？姓名、职务是什么？",
    "影响人": "还有哪些人会参与评估、使用或影响决策？",
    "时间计划": "客户计划何时完成决策、上线或验收？",
}

SKIP_ANSWERS = {
    "未确认", "客户未提及", "未提及", "不清楚", "不知道", "无", "没有", "暂无",
    "-", "/", "n/a", "N/A", "skip", "跳过",
}


def is_skip_answer(text: str) -> bool:
    return (text or "").strip() in SKIP_ANSWERS


def build_questions(
    fields: Dict[str, Any],
    conflicts: Sequence[Dict[str, Any]],
    stage: Dict[str, Any],
    rules_cfg: Dict[str, Any],
    answered_fields: Sequence[str] = (),
    skipped_fields: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    rules = {item["id"]: item for item in rules_cfg["rules"]}
    field_severity = rules_cfg.get("field_severity", {})
    priority_rank = {"高": 0, "中": 1, "低": 2}
    questions: List[Dict[str, Any]] = []
    answered = set(answered_fields)
    skipped = set(skipped_fields)

    def add(question: str, field: str, reason: str, rule_id: str, priority: str) -> None:
        if any(item["question"] == question for item in questions):
            return
        rule = rules.get(rule_id, {})
        questions.append({
            "field": field,
            "question": question,
            "reason": reason,
            "rule_id": rule_id,
            "rule_name": rule.get("name", ""),
            "rule_text": rule.get("text", ""),
            "priority": priority,
            "options": ["未确认", "客户未提及"],
        })

    conflict_fields = {conflict["field"] for conflict in conflicts}
    for conflict in conflicts:
        add(
            f"关于「{conflict['field']}」出现了两种说法（{' / '.join(conflict.get('values', []))}），"
            "哪一版是客户最新确认的口径？",
            conflict["field"],
            "客户前后表达矛盾，按 R-05 需同时记录并人工确认",
            "R-05",
            "高",
        )

    for name, question in QUESTIONS.items():
        field = fields.get(name) or {}
        if field.get("status") == "confirmed":
            continue
        if name in skipped:
            continue
        if name in conflict_fields:
            continue
        reason = "该字段当前状态为未确认" if name not in answered else "上一轮补充未形成明确结论"
        add(question, name, reason, "R-03", field_severity.get(name, "中"))

    steps = (fields.get("下一步") or {}).get("items", [])
    if not steps:
        add(
            "本次沟通后双方约定了什么下一步动作？由谁负责、什么时间完成？",
            "下一步",
            "未识别到下一步行动，无法满足 R-06",
            "R-06",
            "中",
        )
    else:
        for step in steps:
            if step.get("owner") == "待确认" or step.get("due") == "待确认":
                add(
                    f"下一步动作「{trim(step['action'], 30)}」的负责人和完成时间分别是什么？",
                    "下一步",
                    "R-06 要求写清动作、负责人与时间",
                    "R-06",
                    "中",
                )
                break

    if stage.get("code") == "unknown":
        add(
            "客户目前是否明确表达了需求、同意演示/试用，或已进入内部立项审批流程？",
            "阶段",
            "缺少可支撑 S0–S5 达成条件的证据，阶段无法判定",
            "R-04",
            "高",
        )
    elif stage.get("conflicts"):
        add(
            "阶段判定涉及的字段存在矛盾，请确认最新进展，以便重新判定阶段。",
            "阶段",
            stage.get("cannot_judge_reason") or "阶段涉及字段存在矛盾",
            "R-05",
            "高",
        )

    questions.sort(key=lambda item: priority_rank.get(item["priority"], 3))
    return questions[:8]
