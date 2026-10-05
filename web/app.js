const STAGE_ORDER = ["S0", "S1", "S2", "S3", "S4", "S5"];
const STATUS_LABEL = { confirmed: "事实", inferred: "推断", unconfirmed: "未确认" };
const SEVERITY_CLASS = { "高": "high", "中": "mid", "低": "low" };

const state = {
  samples: [],
  rules: null,
  followupAnswers: [],
  result: null,
  previous: null,
  changed: new Set(),
  round: 0,
};

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const response = await fetch(path, options);
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const message = (payload && payload.message) || `请求失败（HTTP ${response.status}）`;
    throw new Error(message);
  }
  return payload;
}

function escapeHtml(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toast(message, isError) {
  const node = $("toast");
  node.textContent = message;
  node.className = "toast show" + (isError ? " error" : "");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => {
    node.className = "toast" + (isError ? " error" : "");
  }, 3200);
}

function statusChip(status) {
  return `<span class="status ${status}">${STATUS_LABEL[status] || status}</span>`;
}

function evidenceList(evidence) {
  if (!evidence || !evidence.length) return "";
  const items = evidence
    .map((item) => {
      const source = item.source === "followup" ? `补充确认·第 ${item.turn} 轮` : "原始记录";
      const matched = item.matched ? `<span class="tag">${escapeHtml(item.matched)}</span>` : "";
      return `<div class="evidence-quote">“${escapeHtml(item.quote)}”${matched}
        <span class="tag rule">${source}</span></div>`;
    })
    .join("");
  return `<div class="evidence"><div class="evidence-title">原话证据</div>${items}</div>`;
}

function notesList(notes) {
  if (!notes || !notes.length) return "";
  return `<ul class="note-list">${notes.map((note) => `<li>${escapeHtml(note)}</li>`).join("")}</ul>`;
}

function renderBadges(health) {
  const rulesVersion = health ? health.rules_version : "-";
  const llmOn = health && health.llm === "yes";
  const llm = llmOn ? `已启用：${escapeHtml(health.llm_model || "模型")}` : "未启用（纯规则引擎）";
  $("badges").innerHTML = `
    <span class="badge">判定引擎：规则引擎</span>
    <span class="badge">大模型：${llm}</span>
    <span class="badge">规则版本：${escapeHtml(rulesVersion)}</span>
    <span class="badge" id="round-badge">追问轮次：0</span>
  `;
}

function renderSamples() {
  const container = $("sample-list");
  if (!state.samples.length) {
    container.innerHTML = '<span class="hint">未找到样例文件</span>';
    return;
  }
  container.innerHTML = state.samples
    .map((sample, index) => `<span class="chip" data-sample="${index}">${escapeHtml(sample.title)}</span>`)
    .join("");
  container.querySelectorAll("[data-sample]").forEach((node) => {
    node.addEventListener("click", () => {
      const sample = state.samples[Number(node.dataset.sample)];
      $("input-text").value = sample.text;
      state.followupAnswers = [];
      state.round = 0;
      state.previous = null;
      state.result = null;
      toast(`已载入样例：${sample.title}`);
    });
  });
}

function renderRules() {
  if (!state.rules) return;
  const { rules, stages, vague_markers: vague, dicts } = state.rules;
  const ruleRows = rules
    .map((rule) => `<tr><td><strong>${rule.id}</strong><br />${escapeHtml(rule.name)}</td><td>${escapeHtml(rule.text)}</td><td>${escapeHtml(rule.severity)}</td></tr>`)
    .join("");
  const stageRows = stages
    .map((stage) => `<tr><td><strong>${stage.code}</strong><br />${escapeHtml(stage.name)}</td><td>${escapeHtml(stage.condition)}</td></tr>`)
    .join("");
  const dictRows = Object.keys(dicts || {})
    .map((key) => `<tr><td>${escapeHtml(key)}</td><td>${escapeHtml((dicts[key] || []).join("、"))}</td></tr>`)
    .join("");
  $("rules-body").innerHTML = `
    <p class="rule-text">规则来源：${escapeHtml(state.rules.source || "")}（版本 ${escapeHtml(state.rules.version || "")}）</p>
    <table class="rule-table"><thead><tr><th>规则</th><th>原文</th><th>风险等级</th></tr></thead><tbody>${ruleRows}</tbody></table>
    <table class="rule-table"><thead><tr><th>阶段</th><th>达成条件</th></tr></thead><tbody>${stageRows}</tbody></table>
    <table class="rule-table"><thead><tr><th>词典</th><th>词条</th></tr></thead><tbody>${dictRows}</tbody></table>
    <p class="rule-text">判定降级规则：出现「${escapeHtml((vague || []).join("、"))}」等表述时，只作为推断或未确认信息，不写成确定结论。</p>
  `;
}

function renderStage(stage) {
  const currentIndex = STAGE_ORDER.indexOf(stage.code);
  const steps = STAGE_ORDER.map((code, index) => {
    const definition = (state.rules && state.rules.stages || []).find((item) => item.code === code) || { name: "" };
    let cls = "stage-step";
    if (currentIndex >= 0 && index === currentIndex) cls += " current";
    else if (currentIndex >= 0 && index < currentIndex) cls += " passed";
    return `<div class="${cls}">${code} ${escapeHtml(definition.name)}</div>`;
  }).join("");

  const satisfied = (stage.satisfied_conditions || [])
    .map((item) => `<li><strong>${item.code} 达成条件：</strong>${escapeHtml(item.condition)}<br />
      <span class="rule-text">${escapeHtml(item.reason)}</span></li>`)
    .join("");
  const blocked = (stage.blocked_by || [])
    .map((item) => `<li><strong>${item.code} ${escapeHtml(item.name)}：</strong>${escapeHtml(item.reason)}</li>`)
    .join("");
  const conflicts = (stage.conflicts || [])
    .map((item) => `<li>${escapeHtml(item.message)}</li>`)
    .join("");

  const reason = stage.cannot_judge_reason
    ? `<div class="notice danger">${stage.code === "unknown" ? "无法判断" : "需人工确认"}：${escapeHtml(stage.cannot_judge_reason)}</div>`
    : "";

  return `
    <div class="card">
      <h3>阶段判定 ${statusChip(stage.status)}</h3>
      <div class="stage-line">${steps}</div>
      <p><strong>判定结果：</strong>${escapeHtml(stage.value)}${stage.condition ? `（达成条件：${escapeHtml(stage.condition)}）` : ""}</p>
      ${reason}
      ${satisfied ? `<div class="evidence-title">已满足条件</div><ul class="note-list">${satisfied}</ul>` : ""}
      ${blocked ? `<div class="evidence-title">未满足 / 被拦下的更高阶段</div><ul class="note-list">${blocked}</ul>` : ""}
      ${conflicts ? `<div class="evidence-title">阶段涉及的矛盾</div><ul class="note-list">${conflicts}</ul>` : ""}
    </div>`;
}

function renderFields(fields, order) {
  return order.map((name) => {
    const field = fields[name];
    if (!field) return "";
    const changed = state.changed.has(name) ? " changed" : "";
    const valueNode = field.value
      ? `<p class="field-value">${escapeHtml(field.value)}</p>`
      : `<p class="field-value empty">未确认（按规则不做补全）</p>`;
    const ruleTags = (field.rule_refs || []).map((id) => `<span class="tag rule">${escapeHtml(id)}</span>`).join("");
    const items = name === "风险" && field.items && field.items.length
      ? `<ul class="note-list">${field.items.map((item) => `<li>[${escapeHtml(item.severity)}] ${escapeHtml(item.message)}</li>`).join("")}</ul>`
      : "";
    return `
      <div class="field-card${changed}">
        <div class="field-head">
          <span class="field-name">${escapeHtml(name)}${ruleTags}</span>
          ${statusChip(field.status)}
        </div>
        ${valueNode}
        ${items}
        ${evidenceList(field.evidence)}
        ${notesList(field.notes)}
      </div>`;
  }).join("");
}

function renderValidation(validation, stage) {
  const overall = validation.overall || {};
  const issues = validation.issues || [];
  const issueNodes = issues.map((issue) => {
    const cls = SEVERITY_CLASS[issue.severity] || "low";
    const evidence = evidenceList(issue.evidence);
    return `
      <div class="issue ${cls}">
        <div class="issue-head">
          <span class="sev ${cls}">风险 ${escapeHtml(issue.severity)}</span>
          <span>${escapeHtml(issue.rule_id)} ${escapeHtml(issue.rule_name)}</span>
          <span>${escapeHtml(issue.issue_type)}</span>
        </div>
        <p class="issue-message">${escapeHtml(issue.message)}</p>
        <p class="rule-text">规则原文：${escapeHtml(issue.rule_text)}</p>
        <p class="issue-body"><strong>整改建议：</strong>${escapeHtml(issue.suggestion)}</p>
        ${issue.cannot_judge_reason ? `<p class="issue-body"><strong>无法判断原因：</strong>${escapeHtml(issue.cannot_judge_reason)}</p>` : ""}
        ${evidence}
      </div>`;
  }).join("");
  return `
    <div class="card">
      <h3>规则校验报告</h3>
      <div class="summary-bar">
        <span class="verdict ${overall.compliant ? "ok" : "bad"}">${overall.compliant ? "合规" : "需要整改"}</span>
        <span class="summary-meta">风险等级：${escapeHtml(overall.risk_level || "无")}</span>
        <span class="summary-meta">问题数：${escapeHtml(overall.issue_count || 0)}</span>
        <span class="summary-meta">阶段：${escapeHtml(stage.value)}</span>
      </div>
      <p class="issue-body">${escapeHtml(overall.summary || "")}</p>
      ${issueNodes || '<p class="hint">未发现需要整改的问题。</p>'}
    </div>`;
}

function renderOrchestration(result) {
  const plan = result.plan || {};
  const trace = result.trace || [];
  const stats = result.context || {};
  const steps = trace.map((step) => `
    <li class="tool-step ${step.status}">
      <div class="tool-head">
        <strong>${step.step}. ${escapeHtml(step.tool)}</strong>
        <span class="tag rule">${escapeHtml(step.status)}</span>
        <span class="rule-text">${escapeHtml(step.elapsed_ms)} ms</span>
      </div>
      <div>${escapeHtml(step.summary)}</div>
      ${step.reason ? `<div class="rule-text">编排理由：${escapeHtml(step.reason)}</div>` : ""}
    </li>`).join("");
  const notes = (plan.notes || []).map((note) => `<li>${escapeHtml(note)}</li>`).join("");
  const rejected = (plan.rejected_tools || []).length
    ? `<div class="notice danger">已拒绝模型计划中的非法工具：${escapeHtml(plan.rejected_tools.join("、"))}</div>`
    : "";
  const reason = plan.reason ? `<p class="rule-text">${escapeHtml(plan.reason)}</p>` : "";
  return `
    <div class="card">
      <h3>执行轨迹：工具调用与流程编排
        <span class="status ${plan.source === "llm" ? "inferred" : "unconfirmed"}">${escapeHtml(plan.source_label || "")}</span>
      </h3>
      ${reason}
      <div class="summary-bar">
        <span class="summary-meta">编排来源：${escapeHtml(plan.source_label || "-")}${plan.model ? "（" + escapeHtml(plan.model) + "）" : ""}</span>
        <span class="summary-meta">规划耗时：${escapeHtml(plan.elapsed_ms || 0)} ms</span>
        <span class="summary-meta">工具步数：${trace.length}</span>
        <span class="summary-meta">原文 ${escapeHtml(stats.raw_chars || 0)} 字 → 模型上下文 ${escapeHtml(stats.llm_context_chars || 0)} 字</span>
        <span class="summary-meta">证据条数：${escapeHtml(stats.evidence_items || 0)}｜矛盾：${escapeHtml(stats.conflicts || 0)}｜追问轮次：${escapeHtml(stats.followup_rounds || 0)}</span>
      </div>
      ${rejected}
      <ol class="trace-list">${steps}</ol>
      ${notes ? `<div class="evidence-title">编排器调整记录</div><ul class="note-list">${notes}</ul>` : ""}
      <p class="hint">${escapeHtml(stats.note || "")}</p>
    </div>`;
}

function renderQuestions(questions) {
  if (!questions || !questions.length) {
    return `
      <div class="card">
        <h3>待确认信息追问</h3>
        <p class="hint">当前没有需要追问的未确认项。如果是补充了新信息，可直接修改左侧原文后重新分析。</p>
      </div>`;
  }
  const nodes = questions.map((question) => `
    <div class="question" data-question="${escapeHtml(question.field)}">
      <div class="q-head">
        <span>字段：${escapeHtml(question.field)} ｜ 依据 ${escapeHtml(question.rule_id)} ${escapeHtml(question.rule_name)}</span>
        <span>优先级：${escapeHtml(question.priority)}</span>
      </div>
      <p>${escapeHtml(question.question)}</p>
      <p class="rule-text">${escapeHtml(question.reason)}｜规则原文：${escapeHtml(question.rule_text)}</p>
      <textarea data-answer="${escapeHtml(question.field)}" placeholder="输入客户明确表达的原话，例如：客户说预算 30 万"></textarea>
      <div class="q-actions">
        ${(question.options || []).map((option) => `<button class="ghost" data-quick="${escapeHtml(option)}" data-field="${escapeHtml(question.field)}">${escapeHtml(option)}</button>`).join("")}
      </div>
    </div>`).join("");
  const history = state.followupAnswers.length
    ? `<div class="evidence-title">已提交的补充确认</div><ul class="history note-list">${state.followupAnswers
        .map((answer) => `<li>第 ${answer.round} 轮 · ${escapeHtml(answer.field)}：${escapeHtml(answer.text)}</li>`)
        .join("")}</ul>`
    : "";
  return `
    <div class="card">
      <h3>待确认信息追问（结构化，回答后自动重跑全部规则）</h3>
      ${nodes}
      <div class="actions">
        <button class="primary" id="submit-followup">提交补充确认并重新分析</button>
        <button class="ghost" id="reset-followup">清空补充记录</button>
      </div>
      ${history}
    </div>`;
}

function render() {
  const result = state.result;
  if (!result) return;
  const panel = $("result-panel");
  const notices = (result.meta.notices || [])
    .map((note) => `<div class="notice">${escapeHtml(note)}</div>`)
    .join("");
  const changed = state.changed.size
    ? `<div class="notice">本轮补充确认后发生变化的字段：${escapeHtml(Array.from(state.changed).join("、"))}</div>`
    : "";
  const meta = result.meta;
  panel.innerHTML = `
    <h2>2. 分析结果</h2>
    <div class="summary-bar">
      <span class="verdict ${result.validation.overall.compliant ? "ok" : "bad"}">${result.validation.overall.compliant ? "合规" : "需要整改"}</span>
      <span class="summary-meta">风险等级：${escapeHtml(result.validation.overall.risk_level)}</span>
      <span class="summary-meta">阶段：${escapeHtml(result.stage.value)}</span>
      <span class="summary-meta">字段来源：规则引擎 ${escapeHtml(meta.engine_version)}</span>
      <span class="summary-meta">大模型：${meta.llm === "disabled" ? "未调用" : escapeHtml(meta.llm)}</span>
    </div>
    ${notices}${changed}
    ${renderStage(result.stage)}
    <div class="card">
      <h3>CRM 十字段</h3>
      <div class="field-grid">${renderFields(result.fields, result.field_order)}</div>
    </div>
    ${renderValidation(result.validation, result.stage)}
    ${renderQuestions(result.followup_questions)}
    ${renderOrchestration(result)}
    <div class="card">
      <h3>导出</h3>
      <div class="actions">
        <button class="primary" id="export-json">下载 JSON</button>
        <button class="ghost" id="copy-json">复制 JSON</button>
        <button class="ghost" id="copy-crm">复制 CRM 字段摘要</button>
      </div>
      <p class="hint">导出内容包含十字段结果、阶段判定过程、规则校验报告与追问记录，便于直接粘贴进 CRM 或归档。</p>
    </div>`;
  bindResultActions();
}

function bindResultActions() {
  const panel = $("result-panel");
  panel.querySelectorAll("[data-quick]").forEach((node) => {
    node.addEventListener("click", () => {
      const field = node.dataset.field;
      const box = panel.querySelector(`textarea[data-answer="${field}"]`);
      if (box) box.value = node.dataset.quick;
    });
  });
  const submit = $("submit-followup");
  if (submit) submit.addEventListener("click", submitFollowup);
  const reset = $("reset-followup");
  if (reset) reset.addEventListener("click", () => {
    state.followupAnswers = [];
    state.round = 0;
    toast("已清空补充确认记录");
    analyze();
  });
  const exportBtn = $("export-json");
  if (exportBtn) exportBtn.addEventListener("click", downloadJson);
  const copyJson = $("copy-json");
  if (copyJson) copyJson.addEventListener("click", () => copyText(JSON.stringify(state.result, null, 2), "已复制完整 JSON"));
  const copyCrm = $("copy-crm");
  if (copyCrm) copyCrm.addEventListener("click", () => copyText(crmSummary(), "已复制 CRM 字段摘要"));
}

function crmSummary() {
  const result = state.result;
  const lines = [`商机字段（规则引擎 v${result.meta.engine_version}，规则版本 ${result.meta.rules_version}）`];
  result.field_order.forEach((name) => {
    const field = result.fields[name];
    lines.push(`【${name}】${field.value || "未确认"}（${STATUS_LABEL[field.status]}）`);
  });
  lines.push(`【阶段判定】${result.stage.value}`);
  lines.push(`【合规结论】${result.validation.overall.compliant ? "合规" : "需要整改"}；风险等级：${result.validation.overall.risk_level}`);
  (result.validation.issues || []).forEach((issue, index) => {
    lines.push(`${index + 1}. [${issue.rule_id} ${issue.severity}] ${issue.message}｜建议：${issue.suggestion}`);
  });
  return lines.join("\n");
}

async function copyText(text, message) {
  try {
    await navigator.clipboard.writeText(text);
    toast(message);
  } catch (error) {
    window.prompt("浏览器未授权剪贴板，请手动复制：", text);
  }
}

function downloadJson() {
  const blob = new Blob([JSON.stringify(state.result, null, 2)], { type: "application/json;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `商机分析结果_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.json`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(link.href);
  toast("已导出 JSON");
}

function collectAnswers() {
  const answers = [];
  document.querySelectorAll("textarea[data-answer]").forEach((node) => {
    const text = node.value.trim();
    if (text) answers.push({ field: node.dataset.answer, text });
  });
  return answers;
}

async function analyze() {
  const baseText = $("input-text").value;
  if (!baseText.trim() && !state.followupAnswers.length) {
    toast("请先粘贴或上传拜访记录", true);
    return;
  }
  const button = $("analyze-btn");
  button.disabled = true;
  button.textContent = "分析中…";
  try {
    const result = await api("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_text: baseText, followup_answers: state.followupAnswers }),
    });
    state.previous = state.result;
    state.changed = diffFields(state.previous, result);
    state.result = result;
    render();
    $("round-badge") && ($("round-badge").textContent = `追问轮次：${state.round}`);
    if (state.changed.size) toast(`分析完成，本轮变化字段：${Array.from(state.changed).join("、")}`);
    else toast("分析完成");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "开始分析";
  }
}

function diffFields(previous, current) {
  const changed = new Set();
  if (!previous) return changed;
  current.field_order.forEach((name) => {
    const before = previous.fields[name];
    const after = current.fields[name];
    if (!before || !after) return;
    if (before.value !== after.value || before.status !== after.status) changed.add(name);
  });
  if (previous.stage.code !== current.stage.code) changed.add("阶段");
  return changed;
}

function submitFollowup() {
  const answers = collectAnswers();
  if (!answers.length) {
    toast("请至少填写一条补充确认内容，或点击“客户未提及”标注", true);
    return;
  }
  state.round += 1;
  answers.forEach((answer) => state.followupAnswers.push({ ...answer, round: state.round }));
  analyze();
}

async function handleUpload(file) {
  if (!file) return;
  const form = new FormData();
  form.append("file", file, file.name);
  try {
    const parsed = await api("/api/upload", { method: "POST", body: form });
    $("input-text").value = parsed.text;
    const note = `已读取 ${parsed.filename}（${parsed.source_type}）`;
    $("upload-hint").textContent = (parsed.warnings || []).length
      ? `${note}｜提示：${parsed.warnings.join("；")}`
      : note;
    toast(note);
  } catch (error) {
    $("upload-hint").textContent = `解析失败：${error.message}`;
    toast(error.message, true);
  }
}

function boot() {
  $("analyze-btn").addEventListener("click", analyze);
  $("clear-btn").addEventListener("click", () => {
    $("input-text").value = "";
    $("upload-hint").textContent = "支持粘贴文本与上传文件；不提供图片 OCR，截图请先转成文字。";
    state.followupAnswers = [];
    state.round = 0;
    state.result = null;
    state.previous = null;
    state.changed = new Set();
    $("result-panel").innerHTML = "";
    toast("已清空");
  });
  $("file-input").addEventListener("change", (event) => handleUpload(event.target.files[0]));
  api("/api/health").then(renderBadges).catch(() => toast("无法连接本地服务", true));
  api("/api/rules").then((rules) => { state.rules = rules; renderRules(); }).catch(() => {});
  api("/api/samples").then((payload) => { state.samples = payload.samples || []; renderSamples(); }).catch(() => {});
}

document.addEventListener("DOMContentLoaded", boot);
