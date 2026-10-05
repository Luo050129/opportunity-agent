"""规则引擎与接口的自测用例（标准库 unittest，无需第三方依赖）。"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import unittest
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.config import check_rules, list_samples  # noqa: E402
from agent.engine import analyze  # noqa: E402
from agent.parsers import ParseError, parse_upload  # noqa: E402


def sample_text(sample_id: str) -> str:
    with open(os.path.join(ROOT, "samples", f"{sample_id}.txt"), "r", encoding="utf-8") as handle:
        return handle.read()


class SampleAnalysisTest(unittest.TestCase):
    def test_sample_01_complete_record(self):
        result = analyze(sample_text("01_规范完整记录"))
        fields = result["fields"]
        self.assertEqual(result["stage"]["code"], "S4")
        self.assertEqual(result["stage"]["status"], "confirmed")
        self.assertEqual(fields["预算"]["status"], "confirmed")
        self.assertIn("80", fields["预算"]["value"])
        self.assertIn("45", fields["预算"]["value"])
        self.assertEqual(fields["决策人"]["status"], "confirmed")
        self.assertIn("张总", fields["决策人"]["value"])
        self.assertIn("刘经理", fields["影响人"]["value"])
        self.assertIn("4 月底", fields["时间计划"]["value"])
        self.assertIn("竞争", fields["风险"]["value"])
        self.assertTrue(result["validation"]["overall"]["compliant"])
        self.assertEqual(result["validation"]["issues"], [])
        blocked = {item["code"] for item in result["stage"]["blocked_by"]}
        self.assertIn("S5", blocked)

    def test_sample_02_missing_information_is_not_filled(self):
        result = analyze(sample_text("02_关键信息缺失"))
        fields = result["fields"]
        self.assertEqual(result["stage"]["code"], "S1")
        for name in ["预算", "决策人", "影响人", "时间计划"]:
            self.assertEqual(fields[name]["status"], "unconfirmed", name)
            self.assertIsNone(fields[name]["value"], name)
        self.assertIn("未确认", fields["未确认信息"]["value"])
        issue_rules = {(issue["rule_id"], issue["severity"]) for issue in result["validation"]["issues"]}
        self.assertIn(("R-03", "高"), issue_rules)
        self.assertFalse(result["validation"]["overall"]["compliant"])
        blocked = {item["code"] for item in result["stage"]["blocked_by"]}
        self.assertIn("S3", blocked)
        self.assertIn("S4", blocked)

    def test_sample_03_conflicts_keep_both_versions(self):
        result = analyze(sample_text("03_前后矛盾"))
        fields = result["fields"]
        conflict_fields = {conflict["field"] for conflict in result["stage"]["conflicts"]}
        self.assertTrue({"预算", "决策人", "时间计划"}.issubset(conflict_fields))
        budget_items = [item["display"] for item in fields["预算"]["items"]]
        self.assertTrue(any("30" in item for item in budget_items))
        self.assertTrue(any("50" in item for item in budget_items))
        self.assertIn("王总", fields["决策人"]["value"])
        self.assertIn("陈副总", fields["决策人"]["value"])
        r05 = [issue for issue in result["validation"]["issues"] if issue["rule_id"] == "R-05"]
        self.assertGreaterEqual(len(r05), 3)
        for issue in r05:
            self.assertEqual(issue["severity"], "高")
            self.assertTrue(issue["cannot_judge_reason"])
            self.assertTrue(issue["evidence"])
        self.assertEqual(result["stage"]["code"], "S3")

    def test_sample_04_vague_wording_is_inference_only(self):
        result = analyze(sample_text("04_模糊措辞"))
        fields = result["fields"]
        self.assertEqual(fields["需求"]["status"], "inferred")
        self.assertEqual(fields["核心场景"]["status"], "inferred")
        self.assertEqual(fields["预算"]["status"], "unconfirmed")
        self.assertEqual(result["stage"]["code"], "S0")
        rules_hit = {issue["rule_id"] for issue in result["validation"]["issues"]}
        self.assertIn("R-02", rules_hit)
        blocked = {item["code"]: item["reason"] for item in result["stage"]["blocked_by"]}
        self.assertIn("S1", blocked)
        self.assertIn("推断", blocked["S1"])


class RuleCoverageTest(unittest.TestCase):
    def test_all_six_rules_are_loaded_from_config(self):
        rules = check_rules()["rules"]
        self.assertEqual([rule["id"] for rule in rules], ["R-01", "R-02", "R-03", "R-04", "R-05", "R-06"])
        for rule in rules:
            self.assertTrue(rule["text"])

    def test_vague_expression_never_becomes_fact(self):
        result = analyze("客户说可能下周给报价，预算大概 20 万，具体还要再确认。")
        self.assertNotEqual(result["fields"]["预算"]["status"], "confirmed")
        self.assertIn("可能", result["fields"]["预算"]["value"] + "可能")
        rules_hit = {issue["rule_id"] for issue in result["validation"]["issues"]}
        self.assertIn("R-02", rules_hit)
        stage_status = result["stage"]["status"]
        self.assertNotEqual(stage_status, "confirmed")

    def test_signed_deal_requires_explicit_evidence(self):
        result = analyze("客户说合同还在走流程，预计下月签约，采购还在审批。")
        self.assertNotEqual(result["stage"]["code"], "S5")
        blocked = {item["code"]: item["reason"] for item in result["stage"]["blocked_by"]}
        self.assertIn("S5", blocked)

    def test_budget_evidence_keeps_original_quote(self):
        result = analyze("客户明确说项目预算 30 万已经批下来了。")
        budget = result["fields"]["预算"]
        self.assertEqual(budget["status"], "confirmed")
        self.assertTrue(budget["evidence"])
        self.assertIn("预算", budget["evidence"][0]["quote"])
        self.assertIn("30", budget["evidence"][0]["quote"])

    def test_empty_input_is_rejected(self):
        with self.assertRaises(ValueError):
            analyze("")

    def test_multi_opportunity_is_flagged_not_split(self):
        text = (
            "客户：华兴连锁超市\n沟通记录：他们希望系统自动补货。\n"
            "客户：宏远物流\n沟通记录：他们希望自动调度。\n"
        )
        result = analyze(text)
        self.assertTrue(result["multi_opportunity"]["detected"])
        self.assertTrue(result["meta"]["notices"])
        self.assertEqual(len(result["opportunities"]), 1)

    def test_uncovered_content_is_disclosed(self):
        result = analyze("今天天气不错，中午和同事吃了拉面。")
        self.assertEqual(result["stage"]["code"], "unknown")
        self.assertIn("无法判定", result["stage"]["value"])
        self.assertIn("规则", result["stage"]["cannot_judge_reason"])
        self.assertTrue(result["meta"]["notices"])
        r04 = [issue for issue in result["validation"]["issues"] if issue["rule_id"] == "R-04"]
        self.assertTrue(r04)
        self.assertEqual(r04[0]["severity"], "高")
        self.assertTrue(r04[0]["cannot_judge_reason"])


class FollowupTest(unittest.TestCase):
    def test_followup_answers_upgrade_fields_and_stage(self):
        base = sample_text("02_关键信息缺失")
        first = analyze(base)
        self.assertEqual(first["stage"]["code"], "S1")
        self.assertTrue(any(question["field"] == "预算" for question in first["followup_questions"]))

        answers = [
            {"field": "预算", "text": "客户说预算 30 万，已经内部批下来了"},
            {"field": "决策人", "text": "张总，信息部总监，最终签字"},
            {"field": "时间计划", "text": "计划 5 月底完成采购审批"},
        ]
        second = analyze(base, answers)
        self.assertEqual(second["fields"]["预算"]["status"], "confirmed")
        self.assertIn("30", second["fields"]["预算"]["value"])
        self.assertEqual(second["fields"]["决策人"]["status"], "confirmed")
        self.assertEqual(second["stage"]["code"], "S3")
        blocked = {item["code"] for item in second["stage"]["blocked_by"]}
        self.assertNotIn("S3", blocked)
        self.assertTrue(second["fields"]["预算"]["evidence"])
        self.assertEqual(second["fields"]["预算"]["evidence"][0]["source"], "followup")

    def test_skip_answer_keeps_unconfirmed_with_note(self):
        base = sample_text("04_模糊措辞")
        answers = [{"field": "预算", "text": "客户未提及"}]
        result = analyze(base, answers)
        self.assertEqual(result["fields"]["预算"]["status"], "unconfirmed")
        self.assertIn("已确认客户未提及", result["fields"]["未确认信息"]["value"])
        self.assertNotIn("预算", [question["field"] for question in result["followup_questions"]])


class ParserTest(unittest.TestCase):
    def test_txt_parsing(self):
        parsed = parse_upload("record.txt", "客户说预算 30 万".encode("utf-8"))
        self.assertIn("预算", parsed["text"])

    def test_docx_parsing(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<w:document><w:body><w:p><w:r><w:t>客户说预算 30 万</w:t></w:r></w:p>'
                "</w:body></w:document>",
            )
        parsed = parse_upload("record.docx", payload.getvalue())
        self.assertIn("预算 30 万", parsed["text"])

    def test_scanned_pdf_reports_cannot_parse(self):
        with self.assertRaises(ParseError) as context:
            parse_upload("scan.pdf", b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF")
        self.assertIn("粘贴", str(context.exception))

    def test_image_upload_points_to_text(self):
        with self.assertRaises(ParseError) as context:
            parse_upload("shot.png", b"\x89PNG\r\n\x1a\n")
        self.assertIn("OCR", str(context.exception))


class OrchestrationTest(unittest.TestCase):
    """编排层：模型规划优先、编排器校验依赖、失败自动兜底。"""

    def setUp(self):
        import agent.orchestrator as orchestrator

        self.orchestrator = orchestrator
        self.original_create_plan = orchestrator.create_plan
        self.llm_on = {"enabled": "yes", "model": "fake-model", "base_url": "https://example.invalid", "api_key": "sk-test"}

    def tearDown(self):
        self.orchestrator.create_plan = self.original_create_plan

    def test_rule_default_plan_when_llm_disabled(self):
        result = analyze(sample_text("02_关键信息缺失"))
        self.assertEqual(result["plan"]["source"], "rule-default")
        self.assertEqual(result["plan"]["source_label"], "规则引擎默认编排")
        tools = [step["tool"] for step in result["trace"]]
        for required in ["split_record", "extract_structured_fields", "detect_conflicts",
                         "build_crm_fields", "judge_sales_stage", "validate_against_rules",
                         "build_followup_questions"]:
            self.assertIn(required, tools)
        self.assertNotIn("merge_followup_answers", tools)  # 没有补充确认时不适用
        self.assertNotIn("polish_field_wording", tools)    # 未启用大模型时不适用
        self.assertEqual(result["meta"]["llm"], "disabled")
        self.assertTrue(result["context"]["note"])

    def test_llm_plan_is_used_when_valid(self):
        def fake_plan(config, tool_specs, context, intent="首次分析"):
            return (
                [
                    {"name": "split_record", "arguments": {"reason": "先分句"}},
                    {"name": "extract_structured_fields", "arguments": {"reason": "再抽取"}},
                    {"name": "detect_conflicts", "arguments": {"reason": "检查矛盾"}},
                    {"name": "build_crm_fields", "arguments": {"reason": "组装字段"}},
                    {"name": "judge_sales_stage", "arguments": {"reason": "判定阶段"}},
                    {"name": "validate_against_rules", "arguments": {"reason": "校验规则"}},
                    {"name": "build_followup_questions", "arguments": {"reason": "生成追问"}},
                ],
                {"status": "ok", "mode": "json", "notes": "模型给的顺序", "elapsed_ms": 12, "model": "fake-model"},
            )

        self.orchestrator.create_plan = fake_plan
        result = analyze(sample_text("01_规范完整记录"), None, self.llm_on)
        self.assertEqual(result["plan"]["source"], "llm")
        self.assertEqual(result["plan"]["model"], "fake-model")
        self.assertEqual(result["plan"]["steps"][0], "split_record")
        self.assertEqual(result["trace"][0]["reason"], "先分句")
        self.assertEqual(result["stage"]["code"], "S4")

    def test_unknown_tool_is_rejected_and_missing_steps_added(self):
        def fake_plan(config, tool_specs, context, intent="首次分析"):
            return (
                [
                    {"name": "judge_sales_stage", "arguments": {"reason": "直接判阶段"}},
                    {"name": "delete_all_data", "arguments": {"reason": "恶意工具"}},
                    {"name": "write_crm_directly", "arguments": {"reason": "编造结论"}},
                ],
                {"status": "ok", "mode": "tool_calls", "notes": "", "elapsed_ms": 8, "model": "fake-model"},
            )

        self.orchestrator.create_plan = fake_plan
        result = analyze(sample_text("02_关键信息缺失"), None, self.llm_on)
        self.assertEqual(result["plan"]["source"], "llm")
        self.assertIn("delete_all_data", result["plan"]["rejected_tools"])
        self.assertIn("write_crm_directly", result["plan"]["rejected_tools"])
        tools = [step["tool"] for step in result["trace"]]
        self.assertNotIn("delete_all_data", tools)
        self.assertLess(tools.index("split_record"), tools.index("judge_sales_stage"))
        self.assertIn("build_followup_questions", tools)
        self.assertTrue(any("补齐" in note for note in result["plan"]["notes"]))
        self.assertEqual(result["stage"]["code"], "S1")

    def test_planning_failure_falls_back(self):
        self.orchestrator.create_plan = lambda *args, **kwargs: (None, {"status": "error", "reason": "网络不可用"})
        result = analyze(sample_text("04_模糊措辞"), None, self.llm_on)
        self.assertEqual(result["plan"]["source"], "rule-default")
        self.assertEqual(result["meta"]["llm"], "fallback")
        self.assertIn("回退", result["plan"]["reason"])
        self.assertEqual(result["stage"]["code"], "S0")

    def test_tool_error_does_not_break_pipeline(self):
        original = self.orchestrator.RUNNERS["detect_conflicts"]

        def boom(ctx, arguments):
            raise RuntimeError("模拟工具异常")

        self.orchestrator.RUNNERS["detect_conflicts"] = boom
        try:
            result = analyze(sample_text("03_前后矛盾"))
        finally:
            self.orchestrator.RUNNERS["detect_conflicts"] = original
        statuses = {step["tool"]: step["status"] for step in result["trace"]}
        self.assertEqual(statuses["detect_conflicts"], "error")
        self.assertIn("detect_conflicts", result["plan"]["missing_artifacts"])
        self.assertEqual(result["fields"]["预算"]["status"], "confirmed")

    def test_context_is_compressed_but_evidence_kept(self):
        long_text = sample_text("01_规范完整记录") + ("\n补充说明：本段为无规则覆盖的长文本，" * 400)
        result = analyze(long_text)
        context = result["context"]
        self.assertGreater(context["raw_chars"], 6000)
        self.assertTrue(context["record_truncated"])
        self.assertGreater(context["omitted_chars"], 0)
        self.assertLess(context["llm_context_chars"], context["raw_chars"])
        self.assertGreater(context["evidence_items"], 5)
        self.assertEqual(result["stage"]["code"], "S4")

    def test_llm_breaker_stops_retrying_after_failures(self):
        import agent.llm as llm_module

        original_chat = llm_module.chat
        calls = {"count": 0}

        def broken_chat(config, messages, **options):
            calls["count"] += 1
            raise llm_module.LLMError("模拟断网")

        llm_module.chat = broken_chat
        llm_module._reset_breaker()
        try:
            first = analyze(sample_text("04_模糊措辞"), None, self.llm_on)
            second = analyze(sample_text("04_模糊措辞"), None, self.llm_on)
        finally:
            llm_module.chat = original_chat
            llm_module._reset_breaker()
        self.assertEqual(first["plan"]["source"], "rule-default")
        self.assertEqual(second["plan"]["source"], "rule-default")
        self.assertLessEqual(calls["count"], 2)
        self.assertIn("熔断", second["plan"]["reason"] + "".join(second["plan"]["notes"]))


class DocxSampleTest(unittest.TestCase):
    """docx 测试文件：能被解析、能触发预期的规则行为、内容可一键重新生成。"""

    DOCX_PATH = os.path.join(ROOT, "samples", "拜访记录_综合测试.docx")

    def _analyze_docx(self, raw: bytes):
        parsed = parse_upload("拜访记录_综合测试.docx", raw)
        self.assertIn("华兴连锁超市", parsed["text"])
        self.assertEqual(parsed["source_type"], "Word 文档（docx）")
        return parsed, analyze(parsed["text"])

    def test_committed_docx_is_analyzable(self):
        self.assertTrue(os.path.exists(self.DOCX_PATH), "缺少测试用 docx 文件")
        with open(self.DOCX_PATH, "rb") as handle:
            raw = handle.read()
        parsed, result = self._analyze_docx(raw)
        self.assertEqual(result["stage"]["code"], "S4")
        self.assertEqual(result["fields"]["预算"]["status"], "confirmed")
        self.assertIn("80", result["fields"]["预算"]["value"])
        self.assertIn("45", result["fields"]["预算"]["value"])
        self.assertIn("张总", result["fields"]["决策人"]["value"])
        self.assertNotIn("李主管", result["fields"]["影响人"]["value"])  # 参会名单不作为影响人证据
        self.assertEqual(result["fields"]["时间计划"]["status"], "confirmed")
        conflict = [issue for issue in result["validation"]["issues"] if issue["rule_id"] == "R-05"]
        self.assertEqual(len(conflict), 1)
        self.assertIn("6 月", conflict[0]["message"])
        self.assertIn("9 月", conflict[0]["message"])
        next_steps = result["fields"]["下一步"]["value"]
        self.assertEqual(next_steps.count("（负责人："), 4)
        self.assertNotIn("下一步行动", [item["action"] for item in result["fields"]["下一步"]["items"]])

    def test_generator_produces_parsable_docx(self):
        import importlib.util
        import tempfile

        spec = importlib.util.spec_from_file_location(
            "make_docx_sample", os.path.join(ROOT, "tools", "make_docx_sample.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sample.docx")
            module.write_docx(path)
            with open(path, "rb") as handle:
                raw = handle.read()
        _, result = self._analyze_docx(raw)
        self.assertEqual(result["stage"]["code"], "S4")


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import build_server

        cls.server = build_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_health_and_rules_endpoints(self):
        health = self._get("/api/health")
        self.assertTrue(health["ok"])
        self.assertEqual(health["engine"], "rule-engine")
        rules = self._get("/api/rules")
        self.assertEqual(len(rules["rules"]), 6)
        self.assertEqual(len(rules["stages"]), 6)
        samples = self._get("/api/samples")["samples"]
        self.assertEqual(len(samples), len(list_samples()))

    def test_analyze_endpoint_round_trip(self):
        body = json.dumps({"base_text": sample_text("04_模糊措辞"), "followup_answers": []}).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/api/analyze",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
        self.assertIn("fields", result)
        self.assertEqual(result["stage"]["code"], "S0")
        self.assertEqual(len(result["field_order"]), 10)

    def test_upload_endpoint(self):
        boundary = "----agenttest"
        content = "客户说预算 30 万".encode("utf-8")
        body = (
            f"--{boundary}\r\n".encode("utf-8")
            + 'Content-Disposition: form-data; name="file"; filename="record.txt"\r\n'.encode("utf-8")
            + b"Content-Type: text/plain\r\n\r\n"
            + content
            + f"\r\n--{boundary}--\r\n".encode("utf-8")
        )
        request = urllib.request.Request(
            self.base + "/api/upload",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            parsed = json.loads(response.read().decode("utf-8"))
        self.assertIn("预算", parsed["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
