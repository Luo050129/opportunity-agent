"""工具层：把规则引擎的每一步能力包装成可被规划器调用的确定性工具。

约束：所有工具都是本地、确定性的；大模型只能选择「调用哪些工具、按什么顺序」，
不能提供字段值、状态或结论——因此工具输出即最终判定。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Tuple

from .config import check_rules, lexicon as load_lexicon, stage_definitions
from .conflicts import detect_conflicts as detect_conflicts_impl
from .context import SessionContext
from .extract import extract_raw, merge_raw, parse_amount
from .fields import build_fields
from .followup import build_questions, is_skip_answer
from .llm import polish_summary
from .stage import judge_stage
from .textutil import find_all, split_sentences
from .validate import build_validation

AUGMENT_PREFIX = {
    "需求": "客户需要：",
    "核心场景": "业务场景：",
    "预算": "预算：",
    "决策人": "决策人：",
    "影响人": "影响人：",
    "时间计划": "时间计划：",
    "下一步": "下一步：",
}

FIELD_RAW_KEYS = {
    "需求": ("needs",),
    "核心场景": ("scenes",),
    "预算": ("budget_items", "budget_notes"),
    "决策人": ("decision_items",),
    "影响人": ("influence_items",),
    "时间计划": ("time_items",),
    "下一步": ("steps",),
}


def has_field_items(raw: Dict[str, Any], field: str) -> bool:
    return any(raw.get(key) for key in FIELD_RAW_KEYS.get(field, ()))


def forced_raw(field: str, text: str, lex: Dict[str, Any], turn: int) -> Dict[str, Any]:
    """针对某个字段的补充确认文本，直接构造该字段的条目（兜底，避免回答被漏掉）。"""
    vague = bool(find_all(text, lex["vague_markers"]))
    base = {"sentence": text, "source": "followup", "turn": turn, "vague": vague}
    display = "补充确认：" + text
    if field == "预算":
        for pattern in lex["amount_regex"]:
            match = re.search(pattern, text)
            if not match:
                continue
            parsed = parse_amount(match.group(0), lex["amount_units"])
            if parsed:
                return {"budget_items": [{
                    **base,
                    "display": "预算" + text.strip(),
                    "amount_text": text.strip(),
                    "label": "预算",
                    "scope": "默认",
                    "value": parsed["value"],
                    "currency": parsed["currency"],
                }]}
        return {"budget_notes": [{**base, "reason": display}]}
    if field == "决策人":
        return {"decision_items": [{
            **base,
            "name": text.strip(),
            "display": display,
            "kind": "person" if len(text.strip()) <= 8 else "role",
            "keyword": "补充确认",
        }]}
    if field == "影响人":
        return {"influence_items": [{
            **base,
            "name": text.strip(),
            "display": display,
            "kind": "person" if len(text.strip()) <= 12 else "role",
        }]}
    if field == "时间计划":
        return {"time_items": [{**base, "text": text.strip(), "display": display, "milestone": None}]}
    if field == "下一步":
        from .extract import _detect_owner, _merge_time_matches

        due = _merge_time_matches(text, lex["time_regex"])
        return {"steps": [{
            **base,
            "action": text.strip(),
            "owner": _detect_owner(text, lex),
            "due": due[0]["text"].strip() if due else "待确认",
        }]}
    if field == "需求":
        return {"needs": [{**base, "text": text.strip(), "keyword": "补充确认"}]}
    if field == "核心场景":
        return {"scenes": [{**base, "text": text.strip(), "keyword": text.strip()}]}
    return {}


# ---------------------------------------------------------------- 工具实现

def _split_record(ctx: SessionContext, arguments: Dict[str, Any]) -> Tuple[str, str, List[str]]:
    sentences = [sentence.text for sentence in split_sentences(ctx.text)]
    ctx.state["sentences"] = sentences
    return "ok", f"切分为 {len(sentences)} 个句子", ["sentences"]


def _extract_structured_fields(ctx: SessionContext, arguments: Dict[str, Any]) -> Tuple[str, str, List[str]]:
    ok, reason = ctx.require("sentences")
    if not ok:
        return "skipped", reason, []
    lex = load_lexicon()
    raw = extract_raw(ctx.text, lex, "input", 0)
    if not ctx.answers:
        ctx.state["raw_merged"] = raw
    ctx.state["raw"] = raw
    hits = sum(len(raw.get(key, [])) for key in
               ["needs", "scenes", "budget_items", "decision_items", "influence_items", "time_items", "steps", "risks"])
    return "ok", f"规则抽取命中 {hits} 条条目", ["raw"]


def _merge_followup_answers(ctx: SessionContext, arguments: Dict[str, Any]) -> Tuple[str, str, List[str]]:
    if not ctx.answers:
        return "skipped", "本轮没有补充确认内容", []
    ok, reason = ctx.require("raw")
    if not ok:
        return "skipped", reason, []
    lex = load_lexicon()
    parts: List[Dict[str, Any]] = [ctx.state["raw"]]
    for index, answer in enumerate(ctx.answers, start=1):
        field = (answer.get("field") or "").strip()
        text = (answer.get("text") or "").strip()
        if is_skip_answer(text):
            ctx.skipped_fields.append(field)
            ctx.notes.append(f"第 {index} 轮追问：{field or '该项'} 标注为客户未提及，保留未确认状态、不做补全。")
            continue
        ctx.answered_fields.append(field)
        parts.append(extract_raw(AUGMENT_PREFIX.get(field, "") + text, lex, "followup", index))
        if field in FIELD_RAW_KEYS and not has_field_items(merge_raw(parts), field):
            forced = forced_raw(field, text, lex, index)
            if forced:
                parts.append(forced)
    ctx.state["raw_merged"] = merge_raw(parts)
    return "ok", f"并入 {len(ctx.answered_fields)} 条补充确认、{len(ctx.skipped_fields)} 条未提及标注", ["raw_merged"]


def _detect_conflicts(ctx: SessionContext, arguments: Dict[str, Any]) -> Tuple[str, str, List[str]]:
    raw = ctx.state.get("raw_merged") or ctx.state.get("raw")
    if raw is None:
        return "skipped", "缺少抽取产物", []
    conflicts = detect_conflicts_impl(raw, load_lexicon())
    ctx.state["conflicts"] = conflicts
    fields_hit = "、".join(item["field"] for item in conflicts) or "无"
    return "ok", f"矛盾检测命中 {len(conflicts)} 项（{fields_hit}）", ["conflicts"]


def _build_crm_fields(ctx: SessionContext, arguments: Dict[str, Any]) -> Tuple[str, str, List[str]]:
    raw = ctx.state.get("raw_merged") or ctx.state.get("raw")
    if raw is None:
        return "skipped", "缺少抽取产物", []
    fields = build_fields(raw, load_lexicon(), ctx.state.get("conflicts", []))
    ctx.state["fields"] = fields
    confirmed = sum(1 for item in fields.values() if item.get("status") == "confirmed")
    return "ok", f"组装十字段，其中 {confirmed} 个字段为事实", ["fields"]


def _judge_sales_stage(ctx: SessionContext, arguments: Dict[str, Any]) -> Tuple[str, str, List[str]]:
    raw = ctx.state.get("raw_merged") or ctx.state.get("raw")
    if raw is None or "fields" not in ctx.state:
        return "skipped", "缺少字段或抽取产物", []
    stage = judge_stage(ctx.state["fields"], raw, ctx.state.get("conflicts", []), load_lexicon(), stage_definitions()["stages"])
    ctx.state["stage"] = stage
    return "ok", f"阶段判定为 {stage['value']}（{stage['status']}）", ["stage"]


def _validate_against_rules(ctx: SessionContext, arguments: Dict[str, Any]) -> Tuple[str, str, List[str]]:
    if "fields" not in ctx.state or "stage" not in ctx.state:
        return "skipped", "缺少字段或阶段产物", []
    raw = ctx.state.get("raw_merged") or ctx.state.get("raw") or {}
    validation = build_validation(
        ctx.state["fields"],
        raw,
        ctx.state.get("conflicts", []),
        ctx.state["stage"],
        load_lexicon(),
        check_rules(),
    )
    ctx.state["validation"] = validation
    overall = validation["overall"]
    return "ok", f"规则校验完成：{overall['issue_count']} 项问题，风险等级 {overall['risk_level']}", ["validation"]


def _build_followup_questions(ctx: SessionContext, arguments: Dict[str, Any]) -> Tuple[str, str, List[str]]:
    if "fields" not in ctx.state or "stage" not in ctx.state:
        return "skipped", "缺少字段或阶段产物", []
    questions = build_questions(
        ctx.state["fields"],
        ctx.state.get("conflicts", []),
        ctx.state["stage"],
        check_rules(),
        ctx.answered_fields,
        ctx.skipped_fields,
    )
    ctx.state["questions"] = questions
    return "ok", f"生成 {len(questions)} 个待确认问题", ["questions"]


def _audit_evidence_coverage(ctx: SessionContext, arguments: Dict[str, Any]) -> Tuple[str, str, List[str]]:
    raw = ctx.state.get("raw_merged") or ctx.state.get("raw")
    if raw is None:
        return "skipped", "缺少抽取产物", []
    quotes: List[str] = []
    for key, items in raw.items():
        if key == "sentences" or not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                quote = (item.get("quote") or item.get("sentence") or "").strip()
                if quote:
                    quotes.append(quote)
    uncovered: List[str] = []
    for sentence in raw.get("sentences", []):
        text = sentence.strip()
        if len(text) < 6 or re.match(r"^[^：:]{1,12}[：:]", text):
            continue
        if any(quote in text or text in quote for quote in quotes):
            continue
        uncovered.append(text)
    ctx.state["uncovered"] = uncovered
    return "ok", f"发现 {len(uncovered)} 句未被现有规则覆盖", ["uncovered"]


def _polish_field_wording(ctx: SessionContext, arguments: Dict[str, Any]) -> Tuple[str, str, List[str]]:
    if ctx.llm.get("enabled") != "yes":
        return "skipped", "未启用大模型润色", []
    if "fields" not in ctx.state:
        return "skipped", "缺少字段产物", []
    result, report = polish_summary({"fields": ctx.state["fields"]}, ctx.llm)
    ctx.state["fields"] = result["fields"]
    ctx.state["polish_report"] = report
    status = "ok" if report.get("status") == "ok" else "failed"
    if status == "ok":
        return status, f"润色 {report.get('polished_fields', 0)} 个已确认字段的表述", ["polish_report"]
    return status, "润色未生效：" + str(report.get("status")), ["polish_report"]


RUNNERS: Dict[str, Callable[[SessionContext, Dict[str, Any]], Tuple[str, str, List[str]]]] = {
    "split_record": _split_record,
    "extract_structured_fields": _extract_structured_fields,
    "merge_followup_answers": _merge_followup_answers,
    "detect_conflicts": _detect_conflicts,
    "build_crm_fields": _build_crm_fields,
    "judge_sales_stage": _judge_sales_stage,
    "validate_against_rules": _validate_against_rules,
    "build_followup_questions": _build_followup_questions,
    "audit_evidence_coverage": _audit_evidence_coverage,
    "polish_field_wording": _polish_field_wording,
}

_REASON_PARAM = {
    "type": "object",
    "properties": {"reason": {"type": "string", "description": "选择该工具的理由（一句话，不要给业务结论）"}},
}

TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "split_record",
        "description": "把拜访记录切分成句子，供后续规则抽取使用。",
        "parameters": _REASON_PARAM,
        "requires": (),
        "produces": ("sentences",),
        "optional": False,
    },
    {
        "name": "extract_structured_fields",
        "description": "用规则与词典从原文抽取需求、场景、预算、决策人、影响人、时间计划、下一步、风险条目。",
        "parameters": _REASON_PARAM,
        "requires": ("sentences",),
        "produces": ("raw", "raw_merged"),
        "optional": False,
    },
    {
        "name": "merge_followup_answers",
        "description": "把用户在追问表单里的补充确认作为新证据并入原文（仅当本轮带有补充确认时可用）。",
        "parameters": _REASON_PARAM,
        "requires": ("raw",),
        "produces": ("raw_merged",),
        "optional": True,
    },
    {
        "name": "detect_conflicts",
        "description": "按 R-05 检测预算金额、时间计划、决策人、需求失效四类前后矛盾，并同时保留两种说法。",
        "parameters": _REASON_PARAM,
        "requires": ("raw_merged",),
        "produces": ("conflicts",),
        "optional": False,
    },
    {
        "name": "build_crm_fields",
        "description": "把抽取条目组装成 CRM 十字段字段，逐字段给出状态（事实/推断/未确认）与原话证据。",
        "parameters": _REASON_PARAM,
        "requires": ("raw_merged", "conflicts"),
        "produces": ("fields",),
        "optional": False,
    },
    {
        "name": "judge_sales_stage",
        "description": "按 S0–S5 达成条件自高向低判定销售阶段，输出已满足条件与未满足原因。",
        "parameters": _REASON_PARAM,
        "requires": ("fields", "raw_merged", "conflicts"),
        "produces": ("stage",),
        "optional": False,
    },
    {
        "name": "validate_against_rules",
        "description": "按 R-01 ~ R-06 逐条校验并生成问题清单、风险等级、整改建议与无法判断原因。",
        "parameters": _REASON_PARAM,
        "requires": ("fields", "conflicts", "stage"),
        "produces": ("validation",),
        "optional": False,
    },
    {
        "name": "build_followup_questions",
        "description": "基于未确认字段、矛盾项与缺失证据生成结构化追问问题。",
        "parameters": _REASON_PARAM,
        "requires": ("fields", "conflicts", "stage"),
        "produces": ("questions",),
        "optional": False,
    },
    {
        "name": "audit_evidence_coverage",
        "description": "审计原文中未被任何规则覆盖的句子，便于声明无法判断范围（可选增强）。",
        "parameters": _REASON_PARAM,
        "requires": ("raw_merged",),
        "produces": ("uncovered",),
        "optional": True,
    },
    {
        "name": "polish_field_wording",
        "description": "可选的文案润色：只改写已确认为事实的字段表述，不改状态、证据与阶段（需启用大模型）。",
        "parameters": _REASON_PARAM,
        "requires": ("fields",),
        "produces": ("polish_report",),
        "optional": True,
    },
]

SPEC_BY_NAME = {spec["name"]: spec for spec in TOOL_SPECS}
