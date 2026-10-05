"""分析入口（facade）：由编排器执行工具链，再把产物组装成对外结果。

对外结果里新增了三个可审计的部分：
- meta.plan：本次工具计划来自「模型规划」还是「规则引擎默认编排」，以及模型的编排说明；
- meta.trace：逐步的工具调用轨迹（工具名、选择理由、产物、耗时）；
- meta.context：上下文预算统计（原文长度、送给模型的上下文字符数、证据条数等）。
"""
from __future__ import annotations

import datetime as _datetime
import re
from typing import Any, Dict, List, Optional, Sequence

from . import ENGINE_NAME, ENGINE_VERSION
from .config import lexicon as load_lexicon, rules_version
from .fields import FIELD_ORDER, build_unconfirmed
from .orchestrator import orchestrate

MIN_TEXT_LENGTH = 8


def _detect_opportunities(text: str, lex: Dict[str, Any]) -> Dict[str, Any]:
    names: List[str] = []
    for match in re.finditer(r"客户(?:名称)?\s*[:：]\s*([^\s，,。；;、]{2,20})", text):
        names.append(match.group(1).strip())
    suffix_alt = "|".join(sorted(lex["opportunity_subject_suffix"], key=len, reverse=True))
    for match in re.finditer(r"([\u4e00-\u9fa5]{2,6}(?:" + suffix_alt + r"))", text):
        names.append(match.group(1))
    unique: List[str] = []
    for name in names:
        if len(name) < 2 or any(name in kept for kept in unique):
            continue
        unique = [kept for kept in unique if kept not in name]
        unique.append(name)
    subjects = unique[:4]
    return {
        "subjects": subjects,
        "multi": len(subjects) > 1,
        "suggestion": (
            "按客户主体拆分拜访记录后分别分析，可避免不同商机的预算、决策人互相干扰。"
            if len(subjects) > 1 else ""
        ),
        "notices": (
            [
                f"检测到 {len(subjects)} 个疑似客户/项目主体（{'、'.join(subjects)}），"
                "可能包含多个商机；当前按单商机输出，建议拆分为多条商机分别录入后复核。"
            ]
            if len(subjects) > 1 else []
        ),
    }


def analyze(
    base_text: str,
    followup_answers: Optional[Sequence[Dict[str, Any]]] = None,
    llm_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    base_text = base_text or ""
    answers = [answer for answer in (followup_answers or []) if (answer or {}).get("text", "").strip()]
    if not base_text.strip() and not answers:
        raise ValueError("empty_input")

    lex = load_lexicon()
    notices: List[str] = []
    if base_text.strip() and len(base_text.strip()) < MIN_TEXT_LENGTH:
        notices.append(f"输入长度不足 {MIN_TEXT_LENGTH} 个字符，仅完成格式与规则校验，未做实质抽取。")

    ctx, plan_info = orchestrate(base_text, answers, llm_config)
    notices.extend(ctx.notes)

    raw = ctx.state.get("raw_merged") or ctx.state.get("raw") or {"sentences": []}
    fields = ctx.state["fields"]
    stage = ctx.state["stage"]
    validation = ctx.state["validation"]
    questions = ctx.state.get("questions", [])
    uncovered = ctx.state.get("uncovered", [])

    if stage["code"] == "unknown" and uncovered:
        stage["cannot_judge_reason"] += (
            " 另有以下内容未被现有规则覆盖、引擎不做判断，建议人工复核："
            + "；".join(item[:60] for item in uncovered[:2])
        )
        notices.append("部分内容未被现有规则覆盖，已在「无法判断原因」中列出，建议人工复核。")

    stage_evidence: List[Dict[str, Any]] = []
    for item in stage.get("satisfied_conditions", []):
        stage_evidence.extend(item.get("evidence", []))
    fields["阶段"] = {
        "field": "阶段",
        "value": stage["value"],
        "status": stage["status"],
        "evidence": stage_evidence,
        "items": [stage],
        "notes": [stage["cannot_judge_reason"]] if stage.get("cannot_judge_reason") else [],
        "rule_refs": ["R-04"],
    }
    fields["未确认信息"] = build_unconfirmed(fields, ctx.state.get("conflicts", []), stage, ctx.skipped_fields)

    full_text = base_text + "\n" + "\n".join(answer.get("text", "") for answer in answers)
    opportunities = _detect_opportunities(full_text, lex)
    notices.extend(opportunities["notices"])

    llm_enabled = ctx.llm.get("enabled") == "yes"
    llm_status = "disabled"
    if llm_enabled:
        llm_status = "ok" if plan_info.get("source") == "llm" else "fallback"
    polish_status = (ctx.state.get("polish_report") or {}).get("status")
    if polish_status and polish_status not in {"ok", "disabled"}:
        notices.append("大模型润色未生效（" + str(polish_status) + "），已保留规则引擎原始输出。")

    plan_public = {
        "source": plan_info.get("source"),
        "source_label": "模型规划" if plan_info.get("source") == "llm" else "规则引擎默认编排",
        "reason": plan_info.get("reason", ""),
        "notes": plan_info.get("notes", []),
        "rejected_tools": plan_info.get("rejected_tools", []),
        "mode": plan_info.get("mode"),
        "model": plan_info.get("model"),
        "elapsed_ms": plan_info.get("elapsed_ms"),
        "steps": [step["name"] for step in ctx.state.get("plan", [])],
        "missing_artifacts": plan_info.get("missing_artifacts", []),
    }

    return {
        "fields": {name: fields[name] for name in FIELD_ORDER},
        "field_order": FIELD_ORDER,
        "stage": stage,
        "validation": validation,
        "followup_questions": questions,
        "opportunities": [{
            "name": opportunities["subjects"][0] if opportunities["subjects"] else "未识别客户主体",
            "subjects": opportunities["subjects"],
            "multi": opportunities["multi"],
            "suggestion": opportunities["suggestion"],
        }],
        "multi_opportunity": {
            "detected": opportunities["multi"],
            "subjects": opportunities["subjects"],
            "suggestion": opportunities["suggestion"],
        },
        "plan": plan_public,
        "trace": ctx.trace,
        "context": ctx.context_stats(),
        "meta": {
            "engine": ENGINE_NAME,
            "engine_version": ENGINE_VERSION,
            "rules_version": rules_version(),
            "llm": llm_status,
            "llm_model": ctx.llm.get("model") if llm_enabled else None,
            "llm_enabled": "yes" if llm_enabled else "no",
            "turns": len(answers),
            "text_length": len(base_text),
            "generated_at": _datetime.datetime.now().isoformat(timespec="seconds"),
            "notices": notices,
            "degraded": bool(notices) or stage["code"] == "unknown",
            "answered_fields": ctx.answered_fields,
            "skipped_fields": ctx.skipped_fields,
            "raw_sentences": len(raw.get("sentences", [])),
        },
    }
