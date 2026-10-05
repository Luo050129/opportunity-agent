# 这个 Agent 的自主规划、工具调用、上下文管理与流程编排

一句话定位：**大模型负责“怎么调”，规则引擎负责“判什么”**。
模型只在工具清单里做选择与排序，字段值、状态（事实／推断／未确认）、原话证据、S0–S5 阶段判定、
R-01~R-06 合规结论全部来自本地确定性工具，因此既具备 Agent 的自主性，又满足题目「不允许只依赖大模型常识自由判断」的要求。

## 1. 自主规划（规划器 + 兜底规划）

两层规划，保证“有模型更聪明、没模型也能跑”：

| 层级 | 触发条件 | 规划方式 |
| --- | --- | --- |
| 动态规划 | 配置了可用模型（`llm.local.json` / `--enable-llm`） | 把工具清单、压缩后的会话上下文、期望输出 schema 交给模型，模型返回 `steps: [{name, arguments:{reason}}]`；优先走 OpenAI 兼容的 `tools/function calling`，模型不支持时自动退回纯 JSON 计划 |
| 静态规划 | 未配置模型、模型报错、计划不可解析 | 规则引擎的默认编排顺序（依赖拓扑序），与动态规划结果等价 |

模型拿到的信息是**上下文摘要**而不是判定权：它看到的是“有没有补充确认、文本规模、当前未确认字段、已知矛盾字段名”，
看不到需要它下结论的业务事实。提示词里明确禁止输出金额、客户姓名、阶段等业务结论。

规划能力目前覆盖：

- **顺序决策**：先分句再抽取、先检测矛盾再判阶段（依赖必须满足，否则计划会被拒绝）；
- **可选步骤决策**：`audit_evidence_coverage`（覆盖审计）、`polish_field_wording`（润色）由规划器挑选，模型漏选时编排器补齐；
- **意图区分**：首轮「首次分析」与追问轮「补充确认后重新分析」使用不同的编排起点。

可继续扩展的规划点：文件解析失败后的重解析分支、多商机拆分规划、置信度不足时的二次抽取、跨轮 replan（把上一轮 trace 摘要喂回规划器）。

## 2. 工具调用（规则引擎即工具集）

工具定义在 `agent/tools.py`，每个工具是「确定性函数 + 依赖声明 + 产物声明」，由编排器统一调度：

| 工具 | 作用 | 依赖 | 产物 |
| --- | --- | --- | --- |
| `split_record` | 记录分句 | — | `sentences` |
| `extract_structured_fields` | 规则+词典抽取需求/场景/预算/决策人/影响人/时间/下一步/风险 | `sentences` | `raw` |
| `merge_followup_answers` | 把追问回答作为新证据并入（含“客户未提及”标注） | `raw` | `raw_merged` |
| `detect_conflicts` | R-05 矛盾检测，双方原话同时保留 | `raw_merged` | `conflicts` |
| `build_crm_fields` | 组装十字段字段与状态、证据 | `raw_merged`、`conflicts` | `fields` |
| `judge_sales_stage` | S0–S5 阶段判定与「还缺什么证据」 | `fields`、`raw_merged`、`conflicts` | `stage` |
| `validate_against_rules` | R-01~R-06 逐条校验、风险等级、整改建议 | `fields`、`conflicts`、`stage` | `validation` |
| `build_followup_questions` | 生成结构化追问问题 | `fields`、`conflicts`、`stage` | `questions` |
| `audit_evidence_coverage` | 找出未被任何规则覆盖的句子 | `raw_merged` | `uncovered` |
| `polish_field_wording` | 模型润色已确认字段表述（唯一会调用模型的工具） | `fields` + 启用模型 | `polish_report` |

工具调用的三条纪律：

1. **白名单**：模型计划里出现清单以外的工具（例如 `delete_all_data`）会被拒绝并记入 `plan.rejected_tools`；
2. **产物驱动**：编排器在每一步前后检查依赖与产物，缺谁补谁，工具拿不到前置产物时返回 `skipped` 而不是抛异常；
3. **失败隔离**：单个工具抛异常只让该步记为 `error`，流水线继续，最终还会触发一次“缺失产物兜底补齐”。

扩展方式：新增一个 `TOOL_SPECS` 条目 + 一个 runner 函数即可，依赖图与规划器会自动识别，无需改编排逻辑。

## 3. 上下文管理（双轨上下文）

会话状态集中在 `agent/context.py` 的 `SessionContext`：

- **原文轨（给规则引擎）**：始终使用完整原文与全部追问问答，保证抽取召回与证据完整；
- **模型轨（给大模型）**：压缩后再送，规则是「原文取头尾、追问答案截断、只给字段状态与缺口清单」。

压缩与预算：

| 项目 | 策略 |
| --- | --- |
| 原文 | 超过 2000 字时保留头 1500 + 尾 500，中间省略并标注省略字数 |
| 追问轮次 | 只保留 `{turn, field, answer(≤200字)}`，丢弃冗余表述 |
| 字段 | 只给 `{field, status, value(≤80字)}`，避免把整段证据塞进模型 |
| 矛盾/缺口 | 只给字段名（`known_conflicts` / `known_gaps`），细节留在本地 |
| 预算 | 默认 6000 字符，响应中回传 `raw_chars`、`llm_context_chars`、`omitted_chars`、`evidence_items`、`conflicts`、`followup_rounds` |

证据台账（`evidence_ledger()`）按「来源 + 原话」去重，每条带 `input` / `followup` 与轮次，
因此追问补充的证据与原始记录证据在同一视图里可回溯，不存在“补充信息覆盖原文”的问题。

多轮会话是无状态的：前端每次把 `base_text` + 全部 `followup_answers` 一起回传，
服务端不保存会话，既不担心状态丢失，也天然支持多窗口并行使用；上下文压缩只影响模型看到的视图，不影响判定。

## 4. 流程编排（带依赖校验的执行循环）

```
输入(文本/文件解析结果 + 追问回答)
   ↓
resolve_plan：模型规划 → 校验(白名单/依赖/必需工具/可选增强/步数上限12) → 不合格则用默认编排
   ↓
执行循环：逐步调用工具（记录 tool / reason / status / summary / 耗时 / 产物）
   ↓
产物校验：缺产物 → 兜底补齐一次
   ↓
组装结果：十字段 + 阶段 + 校验报告 + 追问 + 计划(plan) + 轨迹(trace) + 上下文统计(context)
```

关键编排保证：

- **可审计**：`plan` 说明本次编排来源与理由；`trace` 给出每一步的工具、编排理由、状态、耗时、结果摘要；页面有「执行轨迹」面板可视化。
- **可降级**：模型不可用 → 默认编排；模型给了非法计划 → 拒绝 + 补齐 + 记录；工具异常 → 隔离 + 兜底；文件解析失败 → 明确报错并建议粘贴文本。
- **可复现**：关闭模型时，同一输入的输出完全确定（`meta.llm = disabled`，0 次模型调用）。
- **成本可控**：开启模型时通常只有 1 次规划调用 + 1 次润色调用；其余全是本地毫秒级规则计算（实测工具链总耗时 < 10ms，模型调用 2–5s）。
- **权限边界**：模型既不能写字段，也不能写证据、阶段或合规结论；它唯一的写权限是 `polish_field_wording` 对“已确认为事实”的字段文案做同义改写。

## 5. 一次真实运行的轨迹（DeepSeek + 追问轮）

```
输入：samples/02_关键信息缺失.txt + 两条补充确认（预算 30 万已批 / 张总信息部总监最终签字）
plan.source = llm（deepseek-flash，JSON 计划模式）
 1 split_record                 ok   切分为 6 个句子
 2 extract_structured_fields    ok   规则抽取命中 4 条条目
 3 merge_followup_answers       ok   并入 2 条补充确认、0 条未提及标注
 4 detect_conflicts             ok   矛盾检测命中 0 项
 5 build_crm_fields             ok   组装十字段，其中 5 个字段为事实
 6 judge_sales_stage            ok   阶段判定为 S3 商务评估（confirmed）
 7 validate_against_rules       ok   规则校验完成：5 项问题，风险等级 中
 8 build_followup_questions     ok   生成 3 个待确认问题
 9 audit_evidence_coverage      ok   发现 0 句未被现有规则覆盖
10 polish_field_wording         ok   润色 4 个已确认字段的表述（2.4s）
context: 原文 150 字 → 模型上下文 401 字；证据 7 条；矛盾 0；追问 2 轮
结果：阶段 S3，预算「预算 30 万」，决策人「张总（决策人）」
```

同一条输入把模型关掉（`--no-llm`）时，`plan.source = rule-default`，前 8 步执行结果完全一致，仅少了第 9、10 步与模型耗时。
