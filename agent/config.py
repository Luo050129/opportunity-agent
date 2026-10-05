"""规则与词典加载：所有业务规则都来自 rules/ 目录，代码中不写死业务规则。"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Dict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_DIR = os.path.join(BASE_DIR, "rules")
SAMPLES_DIR = os.path.join(BASE_DIR, "samples")
WEB_DIR = os.path.join(BASE_DIR, "web")


def _load(name: str) -> Dict[str, Any]:
    with open(os.path.join(RULES_DIR, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def check_rules() -> Dict[str, Any]:
    return _load("check_rules.json")


@lru_cache(maxsize=1)
def stage_definitions() -> Dict[str, Any]:
    return _load("stage_definitions.json")


@lru_cache(maxsize=1)
def lexicon() -> Dict[str, Any]:
    return _load("lexicon.json")


def rules_version() -> str:
    return check_rules().get("version", "1.0")


def rule_index() -> Dict[str, Dict[str, Any]]:
    return {item["id"]: item for item in check_rules()["rules"]}


def rule_of(rule_id: str) -> Dict[str, Any]:
    return rule_index().get(rule_id, {"id": rule_id, "text": "", "name": "", "severity": "低"})


def evidence_required_fields() -> list:
    return list(check_rules().get("evidence_required_fields", []))


def public_rules() -> Dict[str, Any]:
    """供页面“规则依据”面板只读展示。"""
    rules = check_rules()
    stages = stage_definitions()
    lex = lexicon()
    return {
        "version": rules.get("version"),
        "source": rules.get("source"),
        "rules": rules.get("rules", []),
        "stages": stages.get("stages", []),
        "vague_markers": lex.get("vague_markers", []),
        "budget_contexts": lex.get("budget_contexts", []),
        "time_contexts": lex.get("time_contexts", []),
        "dicts": {
            "决策相关词": lex.get("decision_keywords", []),
            "影响/参与词": lex.get("influence_keywords", []),
            "风险词": lex.get("risk_keywords", []),
            "需求失效词": lex.get("invalid_markers", []),
        },
    }


def list_samples() -> list:
    samples = []
    if not os.path.isdir(SAMPLES_DIR):
        return samples
    for name in sorted(os.listdir(SAMPLES_DIR)):
        if not name.lower().endswith(".txt"):
            continue
        path = os.path.join(SAMPLES_DIR, name)
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        title = os.path.splitext(name)[0]
        if "_" in title:
            _, rest = title.split("_", 1)
            title = rest
        samples.append({"id": os.path.splitext(name)[0], "title": title, "text": text})
    return samples
