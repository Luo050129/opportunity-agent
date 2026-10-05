"""分句、片段定位、词典匹配等纯文本工具。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Sequence

SENTENCE_SEPARATORS = "。！？!?；;\r\n"
CLAUSE_SEPARATORS = "。！？!?；;，,\r\n"


@dataclass(frozen=True)
class Sentence:
    text: str
    start: int
    end: int


def split_sentences(text: str) -> List[Sentence]:
    sentences: List[Sentence] = []
    buffer_start = 0
    for index, char in enumerate(text):
        if char in SENTENCE_SEPARATORS:
            _push(sentences, text, buffer_start, index)
            buffer_start = index + 1
    _push(sentences, text, buffer_start, len(text))
    return sentences


def _push(sentences: List[Sentence], text: str, start: int, end: int) -> None:
    raw = text[start:end]
    stripped = raw.strip()
    if not stripped:
        return
    offset = start + (len(raw) - len(raw.lstrip()))
    numbering = re.match(r"\d{1,2}\s*[.、)）]\s*", stripped)
    if numbering:
        stripped = stripped[numbering.end():].strip()
        if not stripped:
            return
        found = text.find(stripped, start, end)
        offset = found if found >= 0 else offset
    sentences.append(Sentence(stripped, offset, offset + len(stripped)))


def contains_any(text: str, words: Sequence[str]) -> bool:
    return any(word and word in text for word in words)


def find_all(text: str, words: Sequence[str]) -> List[str]:
    found: List[str] = []
    for word in words:
        if word and word in text and word not in found:
            found.append(word)
    return found


def dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def trim(text: str, limit: int = 160) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


def clause_around(text: str, start: int, end: int, backward: int = 12, forward: int = 10) -> str:
    left = max(0, start - backward)
    right = min(len(text), end + forward)
    for index in range(start - 1, max(0, left - 12) - 1, -1):
        if text[index] in CLAUSE_SEPARATORS:
            left = index + 1
            break
    for index in range(end, min(len(text), right + 8)):
        if text[index] in CLAUSE_SEPARATORS:
            right = index
            break
    while left > 0 and text[left - 1] not in CLAUSE_SEPARATORS:
        left -= 1
    return text[left:right].strip()


def strip_sentence_prefix(text: str) -> str:
    return re.sub(r"^(下一步|后续|接下来|待办|会后)\s*[:：]?\s*", "", text.strip())
