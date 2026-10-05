"""字段抽取层：全部基于词典、正则与位置规则，不调用任何大模型。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from .textutil import (
    Sentence,
    clause_around,
    contains_any,
    dedupe,
    find_all,
    split_sentences,
    strip_sentence_prefix,
    trim,
)

SPEECH_VERBS = ("说", "表示", "提到", "称", "透露", "反馈", "回复", "问", "介绍", "强调", "指出", "交代")
FUTURE_MARKERS = [
    "负责", "需要", "将", "安排", "准备", "提交", "发给", "发过去", "推进", "对接", "跟进",
    "由", "计划", "下周", "下个月", "后续", "接下来", "尽快", "我这边", "我们这边", "打算",
]
STEP_REFERENCE_MARKERS = ["第一次", "上一次", "上次", "之前", "当时", "已经", "已在", "据说", "会后"]
STEP_COMMIT_MARKERS = ["我这边", "我们这边", "我方", "负责", "安排", "需要", "将由", "由客户", "下一步", "后续", "接下来", "尽快", "将在", "会前", "明天", "下周"]
NAME_REJECT_TAIL = set("部门司方室局处中心科组队店户院们级位项")
NAME_REJECT_CHARS = set("责负人是的了和与及个位名岗位会且或跟向于把被让使由")
ROLE_MARKERS = ["由", "负责", "是", "对接", "审核", "审批", "决策", "评估", "把关", "牵头", "参加", "组织", "拍板", "签字", "使用", "建议", "配合"]
NAME_REJECT_WORDS = {
    "信息", "技术", "业务", "采购", "运营", "财务", "商务", "产品", "研发", "项目", "门店",
    "客户", "贵司", "对方", "我们", "他们", "各位", "两个", "三个", "一个", "这个", "那个",
    "整体", "内部", "外部", "公司", "集团", "总部", "大区", "华东", "全国", "销售", "市场",
}


def _person_re(lex: Dict[str, Any]) -> re.Pattern:
    titles = sorted(lex["title_suffixes"], key=len, reverse=True)
    return re.compile(r"([\u4e00-\u9fa5]{1,3})(?:" + "|".join(re.escape(t) for t in titles) + r")")


def _title_re(lex: Dict[str, Any]) -> re.Pattern:
    titles = sorted(lex["title_suffixes"], key=len, reverse=True)
    return re.compile("|".join(re.escape(title) for title in titles))


def _find_persons(text: str, lex: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按“称谓 + 最短可解释姓名”匹配，避免把「门店运营负责人刘经理」整段当成姓名。"""
    persons: List[Dict[str, Any]] = []
    for match in _title_re(lex).finditer(text):
        for length in (1, 2, 3):
            start = match.start() - length
            if start < 0:
                continue
            name = text[start:match.start()]
            if not re.fullmatch(r"[\u4e00-\u9fa5]+", name):
                continue
            if not _is_person_name(name):
                continue
            persons.append({"name": name, "matched": name + match.group(0), "start": start, "end": match.end()})
            break
    return persons


def _is_person_name(name: str) -> bool:
    if not name or name in NAME_REJECT_WORDS:
        return False
    if name[-1] in NAME_REJECT_TAIL:
        return False
    if any(char in NAME_REJECT_CHARS for char in name):
        return False
    if re.search(r"\d", name):
        return False
    return True


def _merge_time_matches(text: str, patterns: Sequence[str]) -> List[Dict[str, Any]]:
    raw: List[tuple] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            if match.group(0).strip():
                raw.append((match.start(), match.end(), match.group(0)))
    raw.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    merged: List[Dict[str, Any]] = []
    for start, end, value in raw:
        if merged and start < merged[-1]["end"]:
            if end > merged[-1]["end"]:
                merged[-1]["end"] = end
                merged[-1]["text"] = text[merged[-1]["start"]:end]
            continue
        merged.append({"start": start, "end": end, "text": value})
    # 相邻片段合并，例如 “4 月” + “月底” → “4 月底”
    combined: List[Dict[str, Any]] = []
    for item in merged:
        if combined and item["start"] == combined[-1]["end"] and item["text"].strip() in {"月底", "月初", "底", "之前", "前"}:
            combined[-1]["end"] = item["end"]
            combined[-1]["text"] = text[combined[-1]["start"]:item["end"]]
            continue
        combined.append(dict(item))
    return combined


def _clean_scene_prefix(prefix: str, lex: Dict[str, Any]) -> str:
    cleaned = prefix
    for stop in sorted(lex["scene_stop_prefix"], key=len, reverse=True):
        index = cleaned.find(stop)
        if index >= 0:
            cleaned = cleaned[index + len(stop):]
    cleaned = re.sub(r"^(的|了|和|与|是|在|要|想|会|能|可|把|给|做|上|用|买|目前|现在|已经|即将)+", "", cleaned)
    cleaned = cleaned.strip()
    if any(char in lex.get("scene_function_words", []) for char in cleaned):
        return ""
    return cleaned


def parse_amount(amount_text: str, units: Dict[str, int]) -> Optional[Dict[str, Any]]:
    number_match = re.search(r"\d+(?:\.\d+)?", amount_text)
    if not number_match:
        return None
    number = float(number_match.group(0))
    unit_key = None
    for unit in sorted(units.keys(), key=len, reverse=True):
        if unit in amount_text:
            unit_key = unit
            break
    if unit_key is None:
        return None
    currency = "USD" if "美" in unit_key else "CNY"
    return {
        "number": number,
        "unit": unit_key,
        "currency": currency,
        "value": number * units[unit_key],
    }


def _budget_label_scope(window: str, lex: Dict[str, Any]) -> tuple:
    scope_rules = lex["budget_scope_rules"]
    for label in ("总预算", "项目投入", "报价"):
        if contains_any(window, scope_rules.get(label, [])):
            if label == "总预算":
                return label, "总预算"
            if label == "项目投入":
                return label, "项目"
            return label, "默认"
    return "预算", "默认"


def extract_budget(sentences: Sequence[Sentence], lex: Dict[str, Any], source: str, turn: int) -> Dict[str, List]:
    items: List[Dict[str, Any]] = []
    notes: List[Dict[str, Any]] = []
    patterns = [re.compile(p) for p in lex["amount_regex"]]
    for sentence in sentences:
        text = sentence.text
        has_context = contains_any(text, lex["budget_contexts"])
        found = False
        for pattern in patterns:
            for match in pattern.finditer(text):
                window = text[max(0, match.start() - 14): min(len(text), match.end() + 10)]
                if not has_context and not contains_any(window, lex["budget_contexts"]):
                    continue
                parsed = parse_amount(match.group(0), lex["amount_units"])
                if not parsed:
                    continue
                label, scope = _budget_label_scope(window, lex)
                local_window = text[max(0, match.start() - 14): match.end()]
                found = True
                items.append({
                    "display": f"{label}{match.group(0).strip()}",
                    "amount_text": match.group(0).strip(),
                    "label": label,
                    "scope": scope,
                    "value": parsed["value"],
                    "currency": parsed["currency"],
                    "vague": bool(find_all(local_window, lex["vague_markers"])),
                    "sentence": text,
                    "quote": clause_around(text, match.start(), match.end(), backward=14, forward=8),
                    "source": source,
                    "turn": turn,
                })
        if has_context and not found:
            negatives = find_all(text, lex["negative_markers"])
            reason = (
                f"提到预算但为未确认表述（{'、'.join(negatives)}）"
                if negatives
                else "提到预算但未给出金额"
            )
            notes.append({
                "reason": reason,
                "sentence": text,
                "vague": True,
                "source": source,
                "turn": turn,
            })
    deduped = []
    seen = set()
    for item in items:
        key = (item["display"], item["sentence"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return {"budget_items": deduped, "budget_notes": notes}


def extract_needs(sentences: Sequence[Sentence], lex: Dict[str, Any], source: str, turn: int) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for sentence in sentences:
        for keyword in lex["need_keywords"]:
            index = sentence.text.find(keyword)
            if index < 0:
                continue
            tail = sentence.text[index + len(keyword):]
            clause = re.split(r"[，,。；;！!？?]", tail, maxsplit=1)[0].strip()
            clause = re.sub(r"^(的|了|要|能|可以|有|个|一条|一套|一种|一些|尽量|最好|再|重新|考虑|看看)+", "", clause).strip()
            clause = clause.strip("“”\"'‘’（）()")
            if len(clause) < 2:
                continue
            if re.match(r"^(再|重新|考虑|看看|评估|确定|研究)", clause):
                continue
            items.append({
                "text": trim(clause, 40),
                "keyword": keyword,
                "vague": bool(find_all(sentence.text, lex["vague_markers"])),
                "sentence": sentence.text,
                "source": source,
                "turn": turn,
            })
    deduped = []
    seen = set()
    for item in items:
        if item["text"] in seen:
            continue
        seen.add(item["text"])
        deduped.append(item)
    return deduped


def extract_scenes(sentences: Sequence[Sentence], lex: Dict[str, Any], source: str, turn: int) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    suffix_alt = "|".join(sorted(lex["scene_system_suffix"], key=len, reverse=True))
    generic_re = re.compile(r"([\u4e00-\u9fa5A-Za-z0-9]{2,10})(?:" + suffix_alt + r")")
    for sentence in sentences:
        vague = bool(find_all(sentence.text, lex["vague_markers"]))
        for keyword in lex["scene_keywords"]:
            if keyword in sentence.text:
                items.append({
                    "text": keyword,
                    "keyword": keyword,
                    "vague": vague,
                    "sentence": sentence.text,
                    "source": source,
                    "turn": turn,
                })
        for match in generic_re.finditer(sentence.text):
            raw_prefix = match.group(1)
            prefix = _clean_scene_prefix(raw_prefix, lex)
            if len(prefix) < 2 or len(prefix) > 6:
                continue
            suffix = match.group(0)[len(raw_prefix):]
            items.append({
                "text": prefix + suffix,
                "keyword": match.group(0),
                "vague": vague,
                "sentence": sentence.text,
                "source": source,
                "turn": turn,
            })
    deduped = []
    seen = set()
    for item in items:
        key = item["keyword"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:6]


def _is_speaker(text: str, person: Dict[str, Any]) -> bool:
    window = text[person["end"]: person["end"] + 4]
    if not window or contains_any(window, ["，", "。", "；", "、", "：", ","]):
        return False
    return contains_any(window, SPEECH_VERBS)


def _is_negated(text: str, start: int, end: int, lex: Dict[str, Any]) -> bool:
    before = text[max(0, start - 6): start]
    after = text[end: end + 6]
    return contains_any(before, lex["negation_markers"]) or contains_any(after, lex["negation_markers"])


def _self_reference_decision(text: str, matched: str) -> bool:
    pattern = (
        re.escape(matched)
        + r"[^，。；]{0,12}(?:签字|拍板|决策|说了算|审批)[^，。；]{0,6}(?:是|由)?他"
    )
    return re.search(pattern, text) is not None


def _decision_keyword_for_person(text: str, person: Dict[str, Any], keywords: Sequence[str]) -> Optional[str]:
    for keyword in keywords:
        for match in re.finditer(re.escape(keyword), text):
            if 0 <= person["start"] - match.end() <= 14:
                return keyword
            if 0 <= match.start() - person["end"] <= 2:
                return keyword
    return None


def extract_people(sentences: Sequence[Sentence], lex: Dict[str, Any], source: str, turn: int) -> Dict[str, List]:
    decisions: List[Dict[str, Any]] = []
    influencers: List[Dict[str, Any]] = []
    notes: List[Dict[str, Any]] = []
    person_re = _person_re(lex)
    for sentence in sentences:
        text = sentence.text
        if contains_any(text, ["参与人", "参会人", "与会人"]):
            continue  # 参会名单只用于识别角色，不作为影响人证据
        vague = bool(find_all(text, lex["vague_markers"]))
        decision_keywords = find_all(text, lex["decision_keywords"])
        influence_keywords = find_all(text, lex["influence_keywords"])
        persons: List[Dict[str, Any]] = []
        for found in _find_persons(text, lex):
            person = dict(found)
            person["negated"] = _is_negated(text, person["start"], person["end"], lex)
            person["speaker"] = _is_speaker(text, {"end": person["end"]})
            person["self_reference"] = _self_reference_decision(text, person["matched"])
            persons.append(person)
        for person in persons:
            if person["negated"]:
                notes.append({
                    "reason": f"原文出现对「{person['matched']}」的否定表述，已排除为决策人",
                    "sentence": text,
                    "source": source,
                    "turn": turn,
                })
                continue
            keyword = None
            if person["self_reference"]:
                keyword = "自述签字/决策人"
            elif not person["speaker"]:
                keyword = _decision_keyword_for_person(text, person, decision_keywords)
            if keyword:
                score = lex["decision_keywords"].index(keyword) if keyword in lex["decision_keywords"] else -1
                decisions.append({
                    "name": person["matched"],
                    "display": f"{person['matched']}（{keyword}）",
                    "kind": "person",
                    "keyword": keyword,
                    "score": score,
                    "vague": vague,
                    "sentence": text,
                    "source": source,
                    "turn": turn,
                })
            elif influence_keywords:
                attributed = _attributed_keywords(text, person, lex["influence_keywords"])
                ranks = [lex["influence_keywords"].index(k) for k in attributed if k in lex["influence_keywords"]]
                influencers.append({
                    "name": person["matched"],
                    "display": f"{person['matched']}（{'、'.join(attributed[:2])}）",
                    "kind": "person",
                    "score": min(ranks) if ranks else 99,
                    "vague": vague,
                    "sentence": text,
                    "source": source,
                    "turn": turn,
                })
        for role in lex["role_words"]:
            index = text.find(role)
            if index < 0:
                continue
            window = (index - 3, index + len(role) + 6)
            if any(window[0] <= person["start"] <= window[1] for person in persons):
                continue
            context = text[max(0, index - 8): index + len(role) + 8]
            if not contains_any(context, ROLE_MARKERS):
                continue
            role_entry = {
                "name": role,
                "display": f"{role}（{'、'.join((decision_keywords or influence_keywords)[:2]) or '角色提及'}）",
                "kind": "role",
                "score": 99,
                "vague": True,
                "sentence": text,
                "source": source,
                "turn": turn,
            }
            if decision_keywords:
                decisions.append(role_entry)
            elif influence_keywords:
                influencers.append(role_entry)
    return {
        "decision_items": _best_by_name(decisions),
        "influence_items": _best_by_name(influencers),
        "people_notes": notes,
    }


def _best_by_name(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一人多次出现时保留“证据最强”的那条（关键词优先级靠前即更强）。"""
    best: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for item in items:
        name = item["name"]
        if name not in best:
            best[name] = item
            order.append(name)
            continue
        if item.get("score", 99) < best[name].get("score", 99):
            best[name] = item
    result = []
    for name in order:
        entry = best[name]
        entry.pop("score", None)
        result.append(entry)
    return result


def _attributed_keywords(text: str, person: Dict[str, Any], keywords: Sequence[str]) -> List[str]:
    """取离该人最近的参与/影响类关键词，避免用同句其他位置的词描述他。"""
    hits: List[tuple] = []
    for keyword in keywords:
        for match in re.finditer(re.escape(keyword), text):
            if person["start"] - 16 <= match.start() and match.end() <= person["end"] + 28:
                hits.append((abs(match.start() - person["end"]), keyword))
    if not hits:
        return [keyword for keyword in keywords if keyword in text][:2]
    hits.sort()
    ordered: List[str] = []
    for _, keyword in hits:
        if keyword not in ordered:
            ordered.append(keyword)
    return ordered


def _dedupe_by(items: Sequence[Dict[str, Any]], *keys: str) -> List[Dict[str, Any]]:
    result = []
    seen = set()
    for item in items:
        key = tuple(item.get(k) for k in keys)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def extract_time(sentences: Sequence[Sentence], lex: Dict[str, Any], source: str, turn: int) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for sentence in sentences:
        text = sentence.text
        if contains_any(text, lex["past_markers"]) and not contains_any(text, lex["plan_markers"]):
            continue
        matches = _merge_time_matches(text, lex["time_regex"])
        if not matches:
            continue
        if not (contains_any(text, lex["time_contexts"]) or contains_any(text, lex["plan_markers"])):
            continue
        vague = bool(find_all(text, lex["vague_markers"]))
        milestones = find_all(text, lex["milestone_keywords"])
        for match in matches:
            local_window = text[max(0, match["start"] - 14): match["end"]]
            milestone = None
            best_distance = None
            for candidate in milestones:
                index = text.find(candidate, match["end"])
                distance = index - match["end"] if index >= 0 else None
                if distance is not None and 0 <= distance <= 16 and (best_distance is None or distance < best_distance):
                    milestone = candidate
                    best_distance = distance
            if milestone is None and milestones:
                milestone = milestones[0]
            items.append({
                "text": match["text"].strip(),
                "display": f"{match['text'].strip()}（{milestone}）" if milestone else match["text"].strip(),
                "milestone": milestone,
                "vague": vague and bool(find_all(local_window, lex["vague_markers"])),
                "sentence": text,
                "quote": clause_around(text, match["start"], match["end"], backward=12, forward=6),
                "source": source,
                "turn": turn,
            })
    return _dedupe_by(items, "display", "sentence")


def _detect_owner(text: str, lex: Dict[str, Any]) -> str:
    if contains_any(text, lex["owner_self"]):
        return "我方"
    if contains_any(text, lex["owner_customer"]):
        return "客户方"
    if _person_re(lex).search(text):
        return "客户方"
    return "待确认"


def extract_steps(sentences: Sequence[Sentence], lex: Dict[str, Any], source: str, turn: int) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for sentence in sentences:
        text = sentence.text
        if len(text) < 10:  # 章节标题、抬头行不构成下一步
            continue
        has_next = contains_any(text, lex["next_step_keywords"])
        actions = find_all(text, lex["action_keywords"])
        if not actions and not contains_any(text, FUTURE_MARKERS):
            continue
        if not has_next and not actions:
            continue
        if actions and not has_next and not contains_any(text, FUTURE_MARKERS):
            continue
        if contains_any(text, STEP_REFERENCE_MARKERS) and not contains_any(text, STEP_COMMIT_MARKERS):
            continue
        if not has_next and contains_any(text, lex.get("process_keywords", [])):
            continue
        if not has_next and contains_any(text, lex.get("step_exclude_context", [])):
            continue
        if has_next:
            action = strip_sentence_prefix(text)
        else:
            action = ""
            for keyword in actions:
                for match in re.finditer(re.escape(keyword), text):
                    candidate = clause_around(text, match.start(), match.end(), backward=10, forward=16)
                    if contains_any(candidate, FUTURE_MARKERS):
                        action = strip_sentence_prefix(candidate)
                        break
                if action:
                    break
            if not action and actions:
                index = text.find(actions[0])
                action = clause_around(text, index, index + len(actions[0]), backward=10, forward=16)
        due_matches = _merge_time_matches(text, lex["time_regex"] + [r"(?:周|星期)[一二三四五六日天]", r"\d{1,2}\s*[日号]"])
        due = due_matches[0]["text"].strip() if due_matches else "待确认"
        items.append({
            "action": trim(action, 60),
            "owner": _detect_owner(action or text, lex),
            "due": due,
            "vague": bool(find_all(text, lex["vague_markers"])),
            "sentence": text,
            "source": source,
            "turn": turn,
        })
    return _dedupe_by(items, "action", "sentence")


def extract_risks(
    sentences: Sequence[Sentence],
    lex: Dict[str, Any],
    source: str,
    turn: int,
    conflicts: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for sentence in sentences:
        text = sentence.text
        for keyword in find_all(text, lex["risk_keywords"]):
            items.append({
                "type": "客户顾虑/风险信号",
                "message": f"命中风险信号「{keyword}」：{trim(text, 60)}",
                "quote": trim(text, 160),
                "keyword": keyword,
                "severity": "中",
                "confirmed": True,
                "source": source,
                "turn": turn,
            })
        for keyword in find_all(text, lex["competitor_keywords"]):
            items.append({
                "type": "竞争风险",
                "message": f"存在竞争/替代方案信号「{keyword}」：{trim(text, 60)}",
                "quote": trim(text, 160),
                "keyword": keyword,
                "severity": "高",
                "confirmed": True,
                "source": source,
                "turn": turn,
            })
        for keyword in find_all(text, lex["urgency_keywords"]):
            if _keyword_in_negation(text, keyword):
                continue
            items.append({
                "type": "时间压力",
                "message": f"客户表达时间紧迫「{keyword}」：{trim(text, 60)}",
                "quote": trim(text, 160),
                "keyword": keyword,
                "severity": "中",
                "confirmed": True,
                "source": source,
                "turn": turn,
            })
        for keyword in find_all(text, lex["invalid_markers"]):
            items.append({
                "type": "需求失效",
                "message": f"出现需求失效表达「{keyword}」：{trim(text, 60)}",
                "quote": trim(text, 160),
                "keyword": keyword,
                "severity": "高",
                "confirmed": True,
                "source": source,
                "turn": turn,
            })
    for conflict in conflicts or []:
        items.append({
            "type": "矛盾待确认",
            "message": conflict["message"],
            "quote": " / ".join(ev["quote"] for ev in conflict.get("evidence", [])),
            "keyword": conflict["field"],
            "severity": "高",
            "confirmed": True,
            "source": source,
            "turn": turn,
        })
    return _dedupe_by(items, "type", "message")


def evidence_of(item: Dict[str, Any], matched: Optional[str] = None) -> Dict[str, Any]:
    return {
        "quote": trim(item.get("quote") or item.get("sentence") or "", 200),
        "matched": matched or item.get("display") or item.get("text") or item.get("action") or "",
        "source": item.get("source", "input"),
        "turn": item.get("turn", 0),
    }


def _keyword_in_negation(text: str, keyword: str) -> bool:
    for match in re.finditer(re.escape(keyword), text):
        window = text[max(0, match.start() - 5): match.start() + 1]
        if any(marker in window for marker in ("不", "没", "未", "别", "无需", "没有")):
            return True
    return False


def extract_raw(text: str, lex: Dict[str, Any], source: str = "input", turn: int = 0) -> Dict[str, Any]:
    sentences = split_sentences(text)
    raw: Dict[str, Any] = {
        "sentences": [sentence.text for sentence in sentences],
        "needs": extract_needs(sentences, lex, source, turn),
        "scenes": extract_scenes(sentences, lex, source, turn),
        "time_items": extract_time(sentences, lex, source, turn),
        "steps": extract_steps(sentences, lex, source, turn),
        "risks": extract_risks(sentences, lex, source, turn),
        "invalid_markers": [
            {"keyword": keyword, "sentence": sentence.text, "source": source, "turn": turn}
            for sentence in sentences
            for keyword in find_all(sentence.text, lex["invalid_markers"])
        ],
    }
    raw.update(extract_budget(sentences, lex, source, turn))
    raw.update(extract_people(sentences, lex, source, turn))
    return raw


def merge_raw(parts: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for part in parts:
        for key, value in part.items():
            if key == "sentences":
                merged.setdefault(key, []).extend(value)
            elif isinstance(value, list):
                merged.setdefault(key, []).extend(value)
            else:
                merged[key] = value
    return merged
