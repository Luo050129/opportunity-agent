"""规则校验层：逐条检查 R-01 ~ R-06，输出问题清单与整体合规判断。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .textutil import find_all, trim

CHECKED_FIELDS = ["需求", "核心场景", "预算", "决策人", "影响人", "时间计划", "下一步"]


def _issue(
    rules: Dict[str, Dict[str, Any]],
    rule_id: str,
    severity: str,
    message: str,
    suggestion: str,
    evidence: Optional[List[Dict[str, Any]]] = None,
    cannot_judge_reason: Optional[str] = None,
) -> Dict[str, Any]:
    rule = rules.get(rule_id, {})
    return {
        "rule_id": rule_id,
        "rule_name": rule.get("name", ""),
        "rule_text": rule.get("text", ""),
        "issue_type": rule.get("issue_type", ""),
        "severity": severity,
        "message": message,
        "suggestion": suggestion,
        "evidence": evidence or [],
        "cannot_judge_reason": cannot_judge_reason,
    }


def build_validation(
    fields: Dict[str, Any],
    raw: Dict[str, Any],
    conflicts: Sequence[Dict[str, Any]],
    stage: Dict[str, Any],
    lex: Dict[str, Any],
    rules_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    rules = {item["id"]: item for item in rules_cfg["rules"]}
    field_severity = rules_cfg.get("field_severity", {})
    issues: List[Dict[str, Any]] = []

    # R-01 事实边界
    for name in CHECKED_FIELDS:
        field = fields.get(name) or {}
        if field.get("status") != "confirmed":
            continue
        for evidence in field.get("evidence", []):
            markers = find_all(evidence.get("quote", ""), lex["vague_markers"])
            if markers:
                issues.append(_issue(
                    rules, "R-01", "高",
                    f"字段「{name}」被标为事实，但原话包含模糊表述「{markers[0]}」",
                    "将该字段降级为推断或未确认，待客户明确确认后再写为事实",
                    [evidence],
                    f"原话「{trim(evidence.get('quote',''), 60)}」不足以支撑确定结论",
                ))
                break

    # R-02 推断处理
    vague_sentences: List[str] = []
    for sentence in raw.get("sentences", []):
        if find_all(sentence, lex["vague_markers"]):
            vague_sentences.append(sentence)
    if vague_sentences:
        markers: List[str] = []
        for sentence in vague_sentences:
            for marker in find_all(sentence, lex["vague_markers"]):
                if marker not in markers:
                    markers.append(marker)
        downgraded = [
            name for name in CHECKED_FIELDS
            if (fields.get(name) or {}).get("status") == "inferred"
        ]
        issues.append(_issue(
            rules, "R-02", "中",
            f"检测到 {len(vague_sentences)} 处模糊表述（{'、'.join(markers[:5])}），"
            f"已按规则降级为推断/未确认，未写成确定结论"
            + (f"；涉及字段：{'、'.join(downgraded)}" if downgraded else ""),
            "把这些字段作为待确认项向客户确认，确认前不得写入 CRM 事实字段",
            [{"quote": trim(sentence, 120), "source": "input", "turn": 0} for sentence in vague_sentences[:3]],
        ))

    # R-03 缺失信息
    for name in ["需求", "核心场景", "预算", "决策人", "影响人", "时间计划"]:
        field = fields.get(name) or {}
        if field.get("status") == "confirmed":
            continue
        severity = field_severity.get(name, "中")
        reason = "仅有推断性表达" if field.get("status") == "inferred" else "原始记录中未提供"
        issues.append(_issue(
            rules, "R-03", severity,
            f"「{name}」未确认（{reason}），按规则标注未确认、不做补全",
            f"向客户确认{name}相关信息后再更新该字段",
            field.get("evidence", []),
            f"{name}缺少可支撑的原话依据" if field.get("status") != "confirmed" else None,
        ))

    # R-04 证据要求
    for name in rules_cfg.get("evidence_required_fields", []):
        if name == "阶段":
            if stage.get("code") == "unknown":
                issues.append(_issue(
                    rules, "R-04", "高",
                    "销售阶段无法判定：缺少可支撑 S0–S5 达成条件的原话依据",
                    "补充客户明确表达的需求、方案验证、预算或审批进展后重新判定",
                    [],
                    stage.get("cannot_judge_reason"),
                ))
            elif not stage.get("satisfied_conditions"):
                issues.append(_issue(
                    rules, "R-04", "高",
                    f"阶段判定为 {stage.get('value')}，但没有保留原话依据",
                    "补充支撑该阶段的原话或原始记录依据",
                    [],
                    "阶段缺少证据",
                ))
            continue
        field = fields.get(name) or {}
        if field.get("status") == "confirmed" and not field.get("evidence"):
            issues.append(_issue(
                rules, "R-04", "高",
                f"「{name}」被标为事实，但缺少原话或原始记录依据",
                "补充原话引用，或将字段降级为未确认",
                [],
                f"{name}缺少证据",
            ))

    # R-05 矛盾处理
    for conflict in conflicts:
        issues.append(_issue(
            rules, "R-05", "高",
            conflict["message"],
            "同时保留两种表述并与客户确认最新口径，确认前标为风险或待确认",
            conflict.get("evidence", []),
            "客户前后表达矛盾，需人工确认哪一版为准",
        ))

    # R-06 下一步行动
    steps = (fields.get("下一步") or {}).get("items", [])
    if not steps:
        issues.append(_issue(
            rules, "R-06", "中",
            "未识别到下一步行动",
            "与客户约定下一步动作、负责人和时间",
            [],
            "原始记录中没有可识别的下一步行动",
        ))
    for step in steps:
        if step.get("owner") == "待确认":
            issues.append(_issue(
                rules, "R-06", "中",
                f"下一步动作「{trim(step['action'], 30)}」未写清建议负责人",
                "明确该动作由我方还是客户方负责，未约定则标「待确认」",
                [],
                "负责人未确认",
            ))
        if step.get("due") == "待确认":
            issues.append(_issue(
                rules, "R-06", "低",
                f"下一步动作「{trim(step['action'], 30)}」未约定时间",
                "按规则标注为「待确认」，并在下次沟通中补上时间",
                [],
                None,
            ))

    highest = "无"
    for level in ["高", "中", "低"]:
        if any(issue["severity"] == level for issue in issues):
            highest = level
            break
    critical_ok = all(
        (fields.get(name) or {}).get("status") == "confirmed" and (fields.get(name) or {}).get("evidence")
        for name in ["预算", "决策人", "时间计划"]
    )
    compliant = highest != "高" and critical_ok
    summary = (
        f"共 {len(issues)} 项问题；风险等级：{highest or '无'}；"
        f"关键字段（预算/决策人/时间计划）{'均有原话依据' if critical_ok else '存在未确认或缺少依据的项'}。"
    )
    return {
        "overall": {
            "compliant": compliant,
            "risk_level": highest,
            "issue_count": len(issues),
            "summary": summary,
            "critical_fields_ok": critical_ok,
        },
        "issues": issues,
    }
