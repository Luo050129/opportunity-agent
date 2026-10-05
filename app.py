"""商机录入与分析助手 Agent 本地网页服务（仅使用标准库）。

启动方式:
    python app.py
    python app.py --port 8123 --open
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from agent import ENGINE_NAME, ENGINE_VERSION
from agent.config import WEB_DIR, list_samples, public_rules, rules_version
from agent.engine import analyze
from agent.llm import describe as describe_llm, load_config
from agent.parsers import ParseError, parse_upload

MAX_BODY_BYTES = 5 * 1024 * 1024
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

LLM_SETTINGS: Dict[str, Any] = {"enabled": "no"}


class AgentHandler(BaseHTTPRequestHandler):
    server_version = "OpportunityAgent/" + ENGINE_VERSION

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ValueError("payload_too_large")
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> Dict[str, Any]:
        body = self._read_body()
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if path == "/api/health":
                return self._send_json({
                    "ok": True,
                    "engine": ENGINE_NAME,
                    "version": ENGINE_VERSION,
                    "rules_version": rules_version(),
                    "llm": LLM_SETTINGS.get("enabled", "no"),
                    "llm_model": LLM_SETTINGS.get("model") if LLM_SETTINGS.get("enabled") == "yes" else None,
                    "llm_base_url": LLM_SETTINGS.get("base_url") if LLM_SETTINGS.get("enabled") == "yes" else None,
                })
            if path == "/api/rules":
                return self._send_json(public_rules())
            if path == "/api/samples":
                return self._send_json({"samples": list_samples()})
            if path.startswith("/api/"):
                return self._send_json({"error": "not_found", "message": "未知接口 " + path}, 404)
            return self._serve_static(path.lstrip("/"))
        except Exception as exc:
            traceback.print_exc()
            return self._send_json({"error": "server_error", "message": str(exc)}, 500)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/analyze":
                return self._handle_analyze()
            if path == "/api/upload":
                return self._handle_upload()
            return self._send_json({"error": "not_found", "message": "未知接口 " + path}, 404)
        except ValueError as exc:
            if str(exc) == "payload_too_large":
                return self._send_json({"error": "too_large", "message": "请求体超过 5MB 限制，请精简输入。"}, 413)
            return self._send_json({"error": "bad_request", "message": str(exc)}, 400)
        except ParseError as exc:
            return self._send_json({"error": "parse_error", "message": str(exc)}, 422)
        except Exception as exc:
            traceback.print_exc()
            return self._send_json({"error": "server_error", "message": "服务内部错误：" + str(exc)}, 500)

    def _handle_analyze(self) -> None:
        payload = self._read_json()
        base_text = str(payload.get("base_text") or "")
        answers = payload.get("followup_answers") or []
        if not isinstance(answers, list):
            raise ValueError("followup_answers 必须是数组")
        if not base_text.strip() and not answers:
            return self._send_json({
                "error": "empty_input",
                "message": "请先粘贴或上传拜访记录再点击分析。",
            }, 400)
        result = analyze(base_text, answers, LLM_SETTINGS)
        return self._send_json(result)

    def _handle_upload(self) -> None:
        content_type = self.headers.get("Content-Type") or ""
        if "multipart/form-data" not in content_type:
            raise ValueError("上传接口需要 multipart/form-data")
        boundary = _boundary_of(content_type)
        if not boundary:
            raise ValueError("缺少 multipart boundary")
        filename, data = _extract_file_part(self._read_body(), boundary.encode("utf-8"))
        if not data:
            return self._send_json({"error": "empty_file", "message": "没有收到文件内容，请重新选择文件。"}, 400)
        parsed = parse_upload(filename, data)
        return self._send_json(parsed)

    def _serve_static(self, relative: str) -> None:
        safe = os.path.normpath(relative).replace("\\", "/").lstrip("/")
        if safe.startswith("..") or os.path.isabs(safe):
            return self._send_json({"error": "forbidden", "message": "非法路径"}, 403)
        target = os.path.join(WEB_DIR, safe)
        if not os.path.isfile(target):
            return self._send_json({"error": "not_found", "message": "文件不存在"}, 404)
        extension = os.path.splitext(target)[1].lower()
        with open(target, "rb") as handle:
            body = handle.read()
        return self._send_bytes(body, CONTENT_TYPES.get(extension, "application/octet-stream"))


def _boundary_of(content_type: str) -> Optional[str]:
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    return match.group(1) if match else None


def _extract_file_part(body: bytes, boundary: bytes) -> Tuple[str, bytes]:
    parts = body.split(b"--" + boundary)
    for part in parts:
        if b"\r\n\r\n" not in part:
            continue
        headers, _, content = part.partition(b"\r\n\r\n")
        if b"filename=" not in headers:
            continue
        match = re.search(rb'filename="([^"]*)"', headers)
        filename = match.group(1).decode("utf-8", errors="ignore") if match else "upload"
        return filename, content.rstrip(b"\r\n-")
    return "", b""


def build_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), AgentHandler)
    server.daemon_threads = True
    return server


def bind_server(host: str, port: int, attempts: int = 10) -> Tuple[ThreadingHTTPServer, int]:
    """从指定端口开始找一个可用端口，避免现场演示被占用端口挡住。"""
    last_error: Optional[OSError] = None
    for offset in range(attempts):
        candidate = port + offset
        if _port_in_use(host, candidate):
            last_error = OSError("port %s is already in use" % candidate)
            continue
        try:
            return build_server(host, candidate), candidate
        except OSError as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise OSError("no available port")


def _port_in_use(host: str, port: int) -> bool:
    """Windows 上 SO_REUSEADDR 允许重复绑定同一端口，必须主动探测。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.3)
    try:
        probe.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="商机录入与分析助手 Agent")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1（仅本机可访问）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--enable-llm", action="store_true", help="启用可选大模型：规划工具调用 + 字段润色")
    parser.add_argument("--no-llm", action="store_true", help="强制关闭大模型，只用规则引擎（默认读取 llm.local.json）")
    parser.add_argument("--llm-model", default=None, help="覆盖模型名，默认读取 llm.local.json / LLM_MODEL")
    parser.add_argument("--llm-base-url", default=None, help="覆盖服务地址，默认读取 llm.local.json / LLM_BASE_URL")
    args = parser.parse_args(argv)

    config = load_config(args.enable_llm, {"model": args.llm_model, "base_url": args.llm_base_url}, disabled_flag=args.no_llm)
    LLM_SETTINGS.clear()
    LLM_SETTINGS.update(config)
    llm_note = describe_llm(config)

    try:
        server, actual_port = bind_server(args.host, args.port)
    except OSError as exc:
        print("端口 %s 启动失败：%s" % (args.port, exc))
        print("可换端口重试，例如：python app.py --port 8801")
        return 1
    url = "http://%s:%s/" % (args.host, actual_port)
    if actual_port != args.port:
        print("提示：端口 %s 已被占用，已自动改用端口 %s。" % (args.port, actual_port), flush=True)
    print("=" * 68, flush=True)
    print("商机录入与分析助手 Agent 已启动", flush=True)
    print("访问地址： " + url, flush=True)
    print("规则版本： %s    大模型增强：%s" % (rules_version(), llm_note), flush=True)
    print("按 Ctrl+C 停止服务", flush=True)
    print("=" * 68, flush=True)
    if args.open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止服务。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
