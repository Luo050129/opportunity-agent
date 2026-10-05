"""文件解析：docx / pdf / txt 等，全部使用标准库；解析失败时给出明确原因。"""
from __future__ import annotations

import html
import io
import os
import re
import zlib
from typing import Dict, List


class ParseError(Exception):
    """解析失败，message 会直接展示给使用者。"""


TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".log", ".json", ".text"}


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_upload(filename: str, data: bytes) -> Dict[str, object]:
    ext = os.path.splitext(filename or "")[1].lower()
    warnings: List[str] = []
    if not data:
        raise ParseError("文件内容为空，请重新上传或直接粘贴文本。")
    if ext in TEXT_EXTENSIONS or ext == "":
        text = _decode(data)
        source_type = "纯文本"
    elif ext == ".docx":
        text = _docx_text(data)
        source_type = "Word 文档（docx）"
    elif ext == ".pdf":
        text, pdf_warnings = _pdf_text(data)
        warnings.extend(pdf_warnings)
        source_type = "PDF"
    elif ext in {".doc", ".rtf", ".wps"}:
        raise ParseError(f"暂不支持 {ext} 格式，请另存为 .docx / .txt，或直接把文本粘贴到输入框。")
    elif ext in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".heic"}:
        raise ParseError("当前版本不提供图片 OCR，请把截图中的文字粘贴为文本，或上传 txt/docx/pdf。")
    else:
        text = _decode(data)
        source_type = f"未知格式（按纯文本解析 {ext}）"
        warnings.append(f"未识别的扩展名 {ext}，已按纯文本尝试解析。")
    if not text.strip():
        raise ParseError("文件中没有解析出可用文本，请粘贴文本或更换文件。")
    return {"text": text, "source_type": source_type, "warnings": warnings, "filename": filename}


def _docx_text(data: bytes) -> str:
    try:
        import zipfile
    except ImportError:  # pragma: no cover
        raise ParseError("当前环境缺少 zipfile 模块，无法解析 docx。")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            target = "word/document.xml"
            if target not in names:
                raise ParseError("不是标准的 .docx 文件（缺少 word/document.xml），请另存为 .docx 或 .txt。")
            xml = archive.read(target).decode("utf-8", errors="ignore")
    except zipfile.BadZipFile:
        raise ParseError("文件不是有效的 .docx（可能已损坏或实际是 .doc），请另存为 .docx 或直接粘贴文本。")
    xml = xml.replace("</w:p>", "\n").replace("<w:tab/>", "\t").replace("<w:br/>", "\n")
    xml = re.sub(r"</w:tc>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(text)


def _pdf_text(data: bytes):
    if not data.startswith(b"%PDF"):
        raise ParseError("文件不是有效的 PDF（缺少 PDF 头），请确认文件类型。")
    if b"/Encrypt" in data:
        raise ParseError("该 PDF 已加密，无法解析，请提供未加密文件或直接粘贴文本。")
    warnings: List[str] = []
    chunks: List[str] = []
    for match in re.finditer(rb"stream\r?\n", data):
        start = match.end()
        end = data.find(b"endstream", start)
        if end < 0:
            continue
        raw = data[start:end]
        try:
            decoded = zlib.decompress(raw)
        except zlib.error:
            continue
        chunks.append(_pdf_stream_text(decoded))
    text = "\n".join(chunk for chunk in chunks if chunk.strip()).strip()
    if len(text) < 10:
        raise ParseError(
            "无法从 PDF 中解析出文本（可能是扫描件、图片型 PDF 或使用了自定义字体编码），"
            "请直接粘贴文本内容，或改用 .docx / .txt。"
        )
    printable = sum(1 for char in text if _is_readable(char))
    if printable / max(len(text), 1) < 0.6:
        raise ParseError(
            "PDF 文本层无法正常解码（疑似扫描件或字体编码不可用），请粘贴文本后重试。"
        )
    warnings.append("PDF 文本为轻量解析结果，如与原文件有出入请以原文为准。")
    return text, warnings


def _is_readable(char: str) -> bool:
    if char in "\r\n\t":
        return True
    if "\u4e00" <= char <= "\u9fff":
        return True
    if 32 <= ord(char) < 127:
        return True
    return char in "，。；：、（）【】《》“”‘’！？—…·％￥$"


def _pdf_stream_text(stream: bytes) -> str:
    content = stream.decode("latin-1", errors="ignore")
    pieces: List[str] = []
    for match in re.finditer(r"\((?:[^()\\]|\\.)*\)", content):
        pieces.append(_unescape_pdf_string(match.group(0)[1:-1]))
    for match in re.finditer(r"<([0-9A-Fa-f\s]{4,})>", content):
        hex_value = re.sub(r"\s+", "", match.group(1))
        try:
            raw = bytes.fromhex(hex_value if len(hex_value) % 2 == 0 else hex_value[:-1])
            pieces.append(raw.decode("utf-16-be", errors="ignore"))
        except ValueError:
            continue
    return " ".join(piece for piece in pieces if piece.strip())


def _unescape_pdf_string(value: str) -> str:
    result = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            mapping = {"n": "\n", "r": "\r", "t": "\t", "b": "", "f": "", "(": "(", ")": ")", "\\": "\\"}
            if nxt in mapping:
                result.append(mapping[nxt])
                index += 2
                continue
            octal = re.match(r"[0-7]{1,3}", value[index + 1:])
            if octal:
                result.append(chr(int(octal.group(0), 8)))
                index += 1 + len(octal.group(0))
                continue
            index += 2
            continue
        result.append(char)
        index += 1
    return "".join(result)
