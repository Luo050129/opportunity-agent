"""可选大模型接入层（OpenAI 兼容 / DeepSeek）。

设计约束：
1. 大模型只做两件事——**规划工具调用顺序**与**润色已确认字段的表述**；
2. 字段值、状态（事实/推断/未确认）、证据、阶段判定一律来自本地规则引擎的工具输出；
3. 任何模型异常都降级为「规则引擎默认编排」，不能影响可用性。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(ROOT_DIR, "llm.local.json")
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_TIMEOUT = 15.0
BREAKER_COOLDOWN_SECONDS = 60.0

# 连续失败后临时熔断：断网/网络受限时不再让每个请求都干等超时
_BREAKER: Dict[str, Any] = {"failures": 0, "until": 0.0, "reason": ""}


def breaker_open() -> bool:
    return time.time() < float(_BREAKER.get("until") or 0)


def breaker_status() -> Dict[str, Any]:
    return {
        "open": breaker_open(),
        "failures": _BREAKER.get("failures", 0),
        "reason": _BREAKER.get("reason", ""),
        "retry_in_seconds": max(0, int(float(_BREAKER.get("until") or 0) - time.time())),
    }


def _trip(reason: str) -> None:
    _BREAKER["failures"] = int(_BREAKER.get("failures", 0)) + 1
    _BREAKER["reason"] = reason[:200]
    if _BREAKER["failures"] >= 2:
        _BREAKER["until"] = time.time() + BREAKER_COOLDOWN_SECONDS


def _reset_breaker() -> None:
    _BREAKER["failures"] = 0
    _BREAKER["until"] = 0.0
    _BREAKER["reason"] = ""


class LLMError(RuntimeError):
    """模型调用失败（消息中不包含密钥等敏感信息）。"""


def _read_local_file() -> Dict[str, Any]:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def load_config(
    enabled_flag: bool = False,
    overrides: Optional[Dict[str, Any]] = None,
    disabled_flag: bool = False,
) -> Dict[str, Any]:
    """优先级：命令行参数 > 环境变量 > llm.local.json > 默认值。

    开关语义：`--enable-llm` 强制开启，`--no-llm` 强制关闭；都不给时由 llm.local.json 的
    enabled 决定（没有该文件即默认关闭）。
    """
    file_config = _read_local_file()
    overrides = overrides or {}
    base_url = (
        overrides.get("base_url")
        or os.environ.get("LLM_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or file_config.get("base_url")
        or DEFAULT_BASE_URL
    )
    api_key = (
        overrides.get("api_key")
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or file_config.get("api_key")
        or ""
    )
    model = (
        overrides.get("model")
        or os.environ.get("LLM_MODEL")
        or file_config.get("model")
        or DEFAULT_MODEL
    )
    if disabled_flag:
        enabled = False
    elif enabled_flag:
        enabled = bool(api_key)
    else:
        enabled = bool(api_key) and bool(file_config.get("enabled"))
    return {
        "enabled": "yes" if enabled else "no",
        "requested": "yes" if enabled_flag else ("no" if disabled_flag else "auto"),
        "base_url": str(base_url).rstrip("/"),
        "api_key": str(api_key),
        "model": str(model),
        "timeout_seconds": float(overrides.get("timeout_seconds") or file_config.get("timeout_seconds") or DEFAULT_TIMEOUT),
        "config_source": "file" if (file_config.get("api_key") and not (os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"))) else "env",
        "file_enabled": "yes" if file_config.get("enabled") else "no",
    }


def describe(config: Dict[str, Any]) -> str:
    if config.get("enabled") != "yes":
        return "未启用（判定全部来自规则引擎）"
    return "已启用：" + str(config.get("model")) + " @ " + str(config.get("base_url")) + "（仅规划与润色，不改判定）"


def mask_key(api_key: str) -> str:
    if not api_key:
        return "(空)"
    return api_key[:6] + "…" + api_key[-4:] if len(api_key) > 12 else "***"


def _request_json(config: Dict[str, Any], path: str, payload: Optional[Dict[str, Any]] = None, method: str = "POST") -> Dict[str, Any]:
    url = config["base_url"] + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + str(config.get("api_key", ""))},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=config.get("timeout_seconds", 30)) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="ignore")[:300]
        except Exception:
            detail = ""
        raise LLMError(("HTTP %s %s %s" % (exc.code, exc.reason, detail)).strip())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise LLMError("网络或响应异常：" + str(exc))


def chat(config: Dict[str, Any], messages: List[Dict[str, Any]], **options: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": config.get("model", DEFAULT_MODEL),
        "messages": messages,
        "temperature": options.pop("temperature", 0),
        "stream": False,
    }
    payload.update(options)
    return _request_json(config, "/chat/completions", payload)


def list_models(config: Dict[str, Any]) -> List[str]:
    body = _request_json(config, "/models", None, method="GET")
    return [item.get("id", "") for item in body.get("data", []) if item.get("id")]


PLANNER_SYSTEM = (
    "你是商机分析 Agent 的编排器，只能做一件事：从给定工具清单中挑选本次要执行的工具并排好顺序。"
    "严禁输出任何字段值、判断结论、金额、客户姓名、阶段结果；严禁编造工具名。"
    "字段抽取、阶段判定、规则校验全部由工具（本地规则引擎）完成，你只负责调度。"
    "只输出 JSON：{\"steps\":[{\"name\":\"工具名\",\"arguments\":{\"reason\":\"理由\"}}],\"notes\":\"一句话说明\"}"
)


def create_plan(
    config: Dict[str, Any],
    tool_specs: List[Dict[str, Any]],
    context: Dict[str, Any],
    intent: str = "首次分析",
) -> Tuple[Optional[List[Dict[str, Any]]], Dict[str, Any]]:
    """让模型给出工具调用计划；失败返回 (None, 诊断信息)，由编排器兜底。"""
    if config.get("enabled") != "yes":
        return None, {"status": "disabled", "reason": "未启用大模型"}
    if breaker_open():
        return None, {
            "status": "breaker_open",
            "reason": "模型连续调用失败，已临时熔断 " + str(breaker_status()["retry_in_seconds"]) + " 秒，改用规则引擎默认编排",
        }

    payload = {
        "intent": intent,
        "tools": [
            {"name": spec["name"], "description": spec["description"], "parameters": spec["parameters"]}
            for spec in tool_specs
        ],
        "context": context,
        "output_schema": {
            "steps": [{"name": "工具名", "arguments": {"reason": "选择理由（一句话）"}}],
            "notes": "对本次编排的说明（一句话）",
        },
    }
    messages = [
        {"role": "system", "content": PLANNER_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    started = time.time()
    tool_calls: List[Dict[str, Any]] = []
    content = ""
    try:
        body = chat(
            config,
            messages,
            tools=[{"type": "function", "function": spec} for spec in tool_specs],
            tool_choice="auto",
        )
        message = (body.get("choices") or [{}])[0].get("message", {}) or {}
        content = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []
    except LLMError as exc:
        _trip(str(exc))
        # 不支持 tools 的模型：退回纯 JSON 规划
        try:
            body = chat(config, messages)
            message = (body.get("choices") or [{}])[0].get("message", {}) or {}
            content = message.get("content") or ""
        except LLMError as inner:
            _trip(str(inner))
            return None, {
                "status": "error",
                "reason": str(exc) + " | 回退失败：" + str(inner),
                "elapsed_ms": int((time.time() - started) * 1000),
            }
    elapsed = int((time.time() - started) * 1000)
    _reset_breaker()

    steps: List[Dict[str, Any]] = []
    for call in tool_calls:
        function = call.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        arguments: Dict[str, Any] = {}
        raw_args = function.get("arguments")
        if isinstance(raw_args, str) and raw_args.strip():
            try:
                arguments = json.loads(raw_args)
            except ValueError:
                arguments = {"_raw": raw_args[:200]}
        elif isinstance(raw_args, dict):
            arguments = raw_args
        steps.append({"name": name, "arguments": arguments})

    mode = "tool_calls"
    notes = content.strip()
    if not steps:
        parsed = parse_json_block(content)
        mode = "json"
        if isinstance(parsed, dict):
            for item in parsed.get("steps", []):
                if isinstance(item, dict):
                    name = item.get("name") or item.get("tool") or ""
                    if name:
                        steps.append({"name": name, "arguments": item.get("arguments") or {"reason": item.get("reason", "")}})
            notes = parsed.get("notes", "") or notes
    if not steps:
        return None, {
            "status": "unparsable",
            "reason": "模型未返回可解析的工具计划",
            "elapsed_ms": elapsed,
            "raw_preview": (content or "")[:200],
        }
    return steps, {"status": "ok", "mode": mode, "notes": notes, "elapsed_ms": elapsed, "model": config.get("model")}


def parse_json_block(content: str) -> Optional[Dict[str, Any]]:
    if not content:
        return None
    text = content.strip()
    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
        text = text.removeprefix("json").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


POLISH_SYSTEM = (
    "你是 CRM 字段摘要润色助手。只允许把给定字段的 value 改写得更通顺，"
    "不得新增事实、不得修改 status、不得新增或改写 evidence、不得改动阶段与合规结论。"
    "输入是 JSON，输出必须是同结构 JSON，只包含 fields 对象。"
)


def polish_summary(result: Dict[str, Any], config: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """可选润色：只改 confirmed 字段的 value 文案。"""
    if config.get("enabled") != "yes":
        return result, {"status": "disabled"}
    if breaker_open():
        return result, {"status": "breaker_open", "reason": "模型临时熔断中，跳过润色"}
    payload = {
        "fields": {
            name: {"value": field.get("value"), "status": field.get("status")}
            for name, field in result["fields"].items()
        }
    }
    messages = [
        {"role": "system", "content": POLISH_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    started = time.time()
    try:
        body = chat(config, messages)
    except LLMError as exc:
        _trip(str(exc))
        return result, {"status": "error", "reason": str(exc)}
    _reset_breaker()
    content = ((body.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "")
    parsed = parse_json_block(content)
    if not parsed:
        return result, {"status": "unparsable", "elapsed_ms": int((time.time() - started) * 1000)}
    polished = 0
    for name, item in (parsed.get("fields") or {}).items():
        current = result["fields"].get(name)
        if not current or current.get("status") != "confirmed":
            continue
        new_value = (item or {}).get("value")
        if isinstance(new_value, str) and new_value.strip() and new_value.strip() != current.get("value"):
            current["value"] = new_value.strip()
            current.setdefault("notes", []).append("字段摘要经可选大模型润色（判定与证据仍来自规则引擎）")
            polished += 1
    return result, {
        "status": "ok",
        "polished_fields": polished,
        "elapsed_ms": int((time.time() - started) * 1000),
        "model": config.get("model"),
    }
