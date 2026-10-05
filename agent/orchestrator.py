"""流程编排：让模型规划「调用哪些工具、按什么顺序」，由编排器校验依赖并兜底执行。

编排原则：
1. **规划可替换**：有模型时由模型出计划，无模型/模型出错时用规则引擎的默认顺序，两者结果一致；
2. **依赖由编排器保证**：模型漏掉的必需工具会被自动补齐，写错工具名会被拒绝并记录；
3. **判定不可被模型改写**：工具是确定性的，最终字段值、状态、证据、阶段全部来自工具产物；
4. **可审计**：每一步工具调用、理由、产物、耗时都写入 trace，随结果一起返回。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .context import SessionContext
from .llm import create_plan
from .tools import RUNNERS, SPEC_BY_NAME, TOOL_SPECS

CANONICAL_ORDER: List[str] = [spec["name"] for spec in TOOL_SPECS]
REQUIRED_TOOLS: List[str] = [spec["name"] for spec in TOOL_SPECS if not spec["optional"]]
MAX_STEPS = 12
PRODUCERS: Dict[str, str] = {}
for _spec in TOOL_SPECS:
    for _artifact in _spec["produces"]:
        PRODUCERS.setdefault(_artifact, _spec["name"])


def _applicable(tool: str, ctx: SessionContext) -> Tuple[bool, str]:
    if tool == "merge_followup_answers" and not ctx.answers:
        return False, "本轮没有补充确认内容"
    if tool == "polish_field_wording" and ctx.llm.get("enabled") != "yes":
        return False, "未启用大模型润色"
    return True, ""


def _default_plan(ctx: SessionContext) -> List[Dict[str, Any]]:
    plan = []
    for name in CANONICAL_ORDER:
        ok, _ = _applicable(name, ctx)
        if ok:
            plan.append({"name": name, "arguments": {"reason": "规则引擎默认编排顺序，保证判定不依赖模型"}})
    return plan


def _normalize(llm_steps: Sequence[Dict[str, Any]], ctx: SessionContext) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """校验模型计划：拒绝未知工具、按依赖补齐前置工具、补上必需工具。"""
    notes: List[str] = []
    rejected: List[str] = []
    ordered: List[str] = []
    arguments: Dict[str, Dict[str, Any]] = {}
    in_progress: set = set()

    def add(tool: str, reason: str) -> None:
        if tool not in SPEC_BY_NAME or tool in ordered or tool in in_progress:
            return
        ok, why = _applicable(tool, ctx)
        if not ok:
            notes.append(f"跳过 {tool}：{why}")
            return
        in_progress.add(tool)
        # 先补齐该工具依赖的前置工具，保证计划在拓扑上可执行
        for artifact in SPEC_BY_NAME[tool]["requires"]:
            producer = PRODUCERS.get(artifact)
            if producer:
                add(producer, "编排器补齐前置工具（依赖：" + artifact + "）")
        ordered.append(tool)
        in_progress.discard(tool)
        if reason:
            arguments[tool] = {"reason": reason}

    for step in llm_steps:
        name = (step or {}).get("name", "")
        if not name or name not in SPEC_BY_NAME:
            if name:
                rejected.append(name)
            continue
        add(name, ((step.get("arguments") or {}).get("reason") or "").strip())

    for tool in REQUIRED_TOOLS:
        if tool not in ordered:
            ok, why = _applicable(tool, ctx)
            if ok:
                notes.append(f"模型未安排必需工具 {tool}，编排器已补上")
                add(tool, "编排器补齐必需工具")
            else:
                notes.append(f"必需工具 {tool} 本轮不适用：{why}")

    for spec in TOOL_SPECS:
        if not spec["optional"] or spec["name"] in ordered:
            continue
        ok, why = _applicable(spec["name"], ctx)
        if ok:
            notes.append(f"编排器补齐可选增强工具 {spec['name']}")
            add(spec["name"], "编排器补齐可选增强工具")

    plan = [{"name": name, "arguments": arguments.get(name, {"reason": ""})} for name in ordered][:MAX_STEPS]
    return plan, notes, rejected


def resolve_plan(ctx: SessionContext) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """决定本次执行的工具计划：模型规划优先，失败则规则默认编排。"""
    info: Dict[str, Any] = {"source": "rule-default", "notes": [], "rejected_tools": []}
    if ctx.llm.get("enabled") != "yes":
        info["reason"] = "未启用大模型，使用规则引擎默认编排"
        return _default_plan(ctx), info

    steps, diagnostics = create_plan(
        ctx.llm,
        TOOL_SPECS,
        ctx.llm_context(),
        intent="补充确认后重新分析" if ctx.answers else "首次分析",
    )
    info["diagnostics"] = diagnostics
    if not steps:
        detail = diagnostics.get("reason") or diagnostics.get("status")
        info["reason"] = "模型规划不可用，回退到规则引擎默认编排：" + str(detail)
        return _default_plan(ctx), info

    plan, notes, rejected = _normalize(steps, ctx)
    if not plan:
        info["reason"] = "模型计划经校验后为空，回退到规则引擎默认编排"
        return _default_plan(ctx), info

    info["source"] = "llm"
    info["notes"] = notes
    info["rejected_tools"] = rejected
    info["model"] = ctx.llm.get("model")
    info["mode"] = diagnostics.get("mode")
    info["elapsed_ms"] = diagnostics.get("elapsed_ms")
    info["notes_text"] = diagnostics.get("notes", "")
    return plan, info


def _missing_artifacts(ctx: SessionContext) -> List[str]:
    missing: List[str] = []
    for tool in REQUIRED_TOOLS:
        spec = SPEC_BY_NAME[tool]
        if not any(artifact in ctx.state for artifact in spec["produces"]):
            missing.append(tool)
    return missing


def orchestrate(text: str, answers: Optional[Sequence[Dict[str, Any]]] = None, llm_config: Optional[Dict[str, Any]] = None) -> Tuple[SessionContext, Dict[str, Any]]:
    ctx = SessionContext(text=text or "", answers=list(answers or []), llm=llm_config or {"enabled": "no"})
    plan, plan_info = resolve_plan(ctx)
    ctx.state["plan"] = plan

    for index, step in enumerate(plan, start=1):
        tool = step["name"]
        arguments = step.get("arguments") or {}
        runner = RUNNERS.get(tool)
        started = time.time()
        if runner is None:
            ctx.record(index, tool, arguments, "error", "工具未注册", 0)
            continue
        try:
            status, summary, produced = runner(ctx, arguments)
        except Exception as exc:  # 单步失败不影响整体，记录后继续
            status, summary, produced = "error", f"执行异常：{exc}", []
        ctx.record(index, tool, arguments, status, summary, int((time.time() - started) * 1000), produced)

    missing = _missing_artifacts(ctx)
    if missing:
        plan_info.setdefault("notes", []).append("检测到缺失产物，执行兜底补齐：" + "、".join(missing))
        for tool in missing:
            started = time.time()
            try:
                status, summary, produced = RUNNERS[tool](ctx, {"reason": "兜底补齐"})
            except Exception as exc:
                status, summary, produced = "error", f"兜底执行异常：{exc}", []
            ctx.record(len(ctx.trace) + 1, tool, {"reason": "兜底补齐"}, status, summary, int((time.time() - started) * 1000), produced)

    plan_info["steps"] = len(ctx.trace)
    plan_info["missing_artifacts"] = missing
    return ctx, plan_info
