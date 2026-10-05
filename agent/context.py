"""会话上下文管理：原始记录、追问轮次、证据台账、上下文压缩与预算统计。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .textutil import trim

# 送给大模型的上下文预算（字符）。本地规则引擎始终使用完整原文，不受此限制。
DEFAULT_CONTEXT_CHAR_BUDGET = 6000
RAW_HEAD_CHARS = 1500
RAW_TAIL_CHARS = 500
ANSWER_CHARS = 200


@dataclass
class SessionContext:
    text: str
    answers: List[Dict[str, Any]] = field(default_factory=list)
    llm: Dict[str, Any] = field(default_factory=dict)
    state: Dict[str, Any] = field(default_factory=dict)
    trace: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    skipped_fields: List[str] = field(default_factory=list)
    answered_fields: List[str] = field(default_factory=list)
    budget: int = DEFAULT_CONTEXT_CHAR_BUDGET
    _llm_context: Optional[Dict[str, Any]] = None

    # ---------- 轨迹 ----------
    def record(
        self,
        step: int,
        tool: str,
        arguments: Dict[str, Any],
        status: str,
        summary: str,
        elapsed_ms: int,
        produced: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        entry = {
            "step": step,
            "tool": tool,
            "reason": (arguments or {}).get("reason", ""),
            "status": status,
            "summary": summary,
            "produced": produced or [],
            "elapsed_ms": elapsed_ms,
        }
        self.trace.append(entry)
        return entry

    def require(self, *keys: str) -> Tuple[bool, str]:
        missing = [key for key in keys if key not in self.state]
        if missing:
            return False, "缺少前置产物：" + "、".join(missing)
        return True, ""

    # ---------- 证据台账 ----------
    def evidence_ledger(self) -> List[Dict[str, Any]]:
        raw = self.state.get("raw_merged") or self.state.get("raw") or {}
        ledger: List[Dict[str, Any]] = []
        seen = set()
        for key, items in raw.items():
            if key == "sentences" or not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                quote = (item.get("quote") or item.get("sentence") or "").strip()
                if not quote:
                    continue
                marker = (key, quote)
                if marker in seen:
                    continue
                seen.add(marker)
                ledger.append({
                    "kind": key,
                    "quote": trim(quote, 160),
                    "source": item.get("source", "input"),
                    "turn": item.get("turn", 0),
                })
        return ledger

    # ---------- 送给模型的压缩上下文 ----------
    def llm_context(self) -> Dict[str, Any]:
        if self._llm_context is not None:
            return self._llm_context
        context: Dict[str, Any] = {"intent": "首次分析" if not self.answers else "补充确认后重新分析"}
        context["record_digest"] = self._compress_record()
        context["followup_rounds"] = [
            {
                "turn": index + 1,
                "field": answer.get("field", ""),
                "answer": trim(str(answer.get("text", "")), ANSWER_CHARS),
            }
            for index, answer in enumerate(self.answers)
        ]
        fields = self.state.get("fields") or {}
        stage = self.state.get("stage") or {}
        if fields:
            context["current_fields"] = [
                {"field": name, "status": item.get("status"), "value": trim(str(item.get("value") or ""), 80)}
                for name, item in fields.items()
                if name not in {"未确认信息"}
            ]
        if stage:
            context["current_stage"] = {"code": stage.get("code"), "status": stage.get("status")}
        conflicts = self.state.get("conflicts") or []
        if conflicts:
            context["known_conflicts"] = [item.get("field") for item in conflicts]
        context["known_gaps"] = [
            entry.get("field") for entry in ((fields.get("未确认信息") or {}).get("items") or [])
        ][:8]
        self._llm_context = context
        return context

    def _compress_record(self) -> Dict[str, Any]:
        text = self.text or ""
        if len(text) <= RAW_HEAD_CHARS + RAW_TAIL_CHARS:
            return {"chars": len(text), "truncated": False, "text": text}
        head = text[:RAW_HEAD_CHARS]
        tail = text[-RAW_TAIL_CHARS:]
        return {
            "chars": len(text),
            "truncated": True,
            "omitted_chars": len(text) - RAW_HEAD_CHARS - RAW_TAIL_CHARS,
            "text": head + "\n……（中间省略，完整原文仍由本地规则引擎处理）……\n" + tail,
        }

    def context_stats(self) -> Dict[str, Any]:
        context = self.llm_context()
        import json

        payload = json.dumps(context, ensure_ascii=False)
        record = context.get("record_digest", {})
        return {
            "raw_chars": len(self.text or ""),
            "llm_context_chars": len(payload),
            "budget_chars": self.budget,
            "within_budget": len(payload) <= self.budget,
            "record_truncated": bool(record.get("truncated")),
            "omitted_chars": record.get("omitted_chars", 0),
            "followup_rounds": len(self.answers),
            "evidence_items": len(self.evidence_ledger()),
            "conflicts": len(self.state.get("conflicts") or []),
            "note": "本地规则引擎始终使用完整原文，压缩只作用于送给大模型的上下文。",
        }

    def summary_state(self) -> Dict[str, Any]:
        return {
            "artifacts": sorted(self.state.keys()),
            "trace_steps": len(self.trace),
            "elapsed_ms": self.elapsed_ms(),
        }

    def elapsed_ms(self) -> int:
        return int((time.time() - self.started_at) * 1000)

    started_at: float = field(default_factory=time.time)
