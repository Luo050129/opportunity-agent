"""生成可用于测试/演示的 .docx 拜访记录（仅标准库，不依赖 python-docx）。

用法：
    python tools/make_docx_sample.py
    python tools/make_docx_sample.py --out samples/拜访记录_综合测试.docx

生成的文件可直接在 Agent 页面「上传文件（.txt / .docx / .pdf）」中上传，
也可以直接用 Word 打开编辑后再上传。
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional
from xml.sax.saxutils import escape

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT_DIR, "samples", "拜访记录_综合测试.docx")

TITLE = "商机拜访记录"

PARAGRAPHS: List[str] = [
    "客户名称：华兴连锁超市有限公司",
    "拜访时间：2025年3月14日 14:00—15:30",
    "拜访地点：客户总部三楼会议室",
    "我方参与人：王磊（销售）、赵敏（解决方案）",
    "客户参与人：陈经理（信息部）、李主管（采购部）、刘经理（门店运营负责人）",
    "",
    "一、客户现状与需求",
    "1. 陈经理明确说，现在门店补货完全靠店长经验，华东区 120 家门店经常出现断货和压货并存的情况。",
    "2. 客户希望系统能根据历史销量和库存自动给出补货建议，先覆盖华东区，再考虑全国推广。",
    "3. 李主管补充说，采购希望减少紧急调货，降低物流成本。",
    "",
    "二、方案沟通情况",
    "4. 我方现场演示了补货预测看板，陈经理当场同意安排一次技术交流，时间定在下周三上午，由他们信息部组织，门店运营也会参加。",
    "5. 客户明确说计划 4 月底完成技术验证，5 月底前完成内部立项和采购审批。",
    "6. 第一次沟通时说计划 6 月上线第一家门店试点，本次又说可能要推到 9 月。",
    "",
    "三、商务信息",
    "7. 陈经理说今年 IT 预算已经批下来 80 万，其中这个项目计划投入 45 万，采购要走年度供应商框架，需要先做方案评估再立项审批。",
    "8. 陈经理说最终签字的是信息部总监张总，他只是负责推动和评估。",
    "9. 门店运营负责人刘经理是对接使用者，会提需求建议。",
    "10. 客户提到他们内部也在看另一家供应商的方案，主要担心项目实施周期太长影响门店正常运营。",
    "11. 客户已经完成内部立项评审，目前正在走供应商决策流程。",
    "",
    "四、下一步行动",
    "12. 我这边周五之前把技术交流的详细方案和报价单发给陈经理。",
    "13. 陈经理负责协调门店运营同事参加技术交流，时间定在下周三上午。",
    "14. 我打算下个月再去拜访一次，具体时间还没定。",
    "",
    "五、待确认事项",
    "15. 客户没有提到最终合同条款和付款方式，需要下次沟通时确认。",
]


def _run(text: str, bold: bool = False, size: Optional[int] = None) -> str:
    properties = []
    if bold:
        properties.append("<w:b/>")
    if size:
        properties.append(f'<w:sz w:val="{size}"/>')
    rpr = f"<w:rPr>{''.join(properties)}</w:rPr>" if properties else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _paragraph(text: str, align: Optional[str] = None, bold: bool = False, size: Optional[int] = None) -> str:
    ppr = f'<w:pPr><w:jc w:val="{align}"/></w:pPr>' if align else ""
    return f"<w:p>{ppr}{_run(text, bold=bold, size=size)}</w:p>"


def build_document(title: str = TITLE, paragraphs: Optional[List[str]] = None) -> str:
    body: List[str] = [_paragraph(title, align="center", bold=True, size="32")]
    for item in paragraphs if paragraphs is not None else PARAGRAPHS:
        body.append(_paragraph(item) if item else "<w:p/>")
    body.append(
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + "".join(body) + "</w:body></w:document>"
    )


CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault>
      <w:rPr>
        <w:rFonts w:ascii="Microsoft YaHei" w:eastAsia="Microsoft YaHei" w:hAnsi="Microsoft YaHei"/>
        <w:sz w:val="21"/>
        <w:szCs w:val="21"/>
      </w:rPr>
    </w:rPrDefault>
    <w:pPrDefault>
      <w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr>
    </w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:qFormat/>
  </w:style>
</w:styles>"""

CORE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
  xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>商机拜访记录（Agent 测试用）</dc:title>
  <dc:creator>商机录入与分析助手 Agent</dc:creator>
  <cp:lastModifiedBy>商机录入与分析助手 Agent</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">2025-03-14T09:00:00Z</dcterms:created>
</cp:coreProperties>"""

APP = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
  <Application>Opportunity Agent Sample Generator</Application>
</Properties>"""


def write_docx(path: str, title: str = TITLE, paragraphs: Optional[List[str]] = None) -> str:
    import zipfile

    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("word/_rels/document.xml.rels", DOC_RELS)
        archive.writestr("word/document.xml", build_document(title, paragraphs))
        archive.writestr("word/styles.xml", STYLES)
        archive.writestr("docProps/core.xml", CORE)
        archive.writestr("docProps/app.xml", APP)
    return path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="生成 Agent 测试用的 .docx 拜访记录")
    parser.add_argument("--out", default=DEFAULT_OUT, help="输出路径，默认 samples/拜访记录_综合测试.docx")
    parser.add_argument("--title", default=TITLE, help="文档标题")
    args = parser.parse_args(argv)
    path = write_docx(args.out, args.title)
    print("已生成：" + os.path.abspath(path))
    print("段落数：%d" % (len(PARAGRAPHS) + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
