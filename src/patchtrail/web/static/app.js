"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  conversations: [], active: null, drafts: new Map(), pending: new Map(),
  loadVersion: 0, loadController: null, ready: false, creating: false,
  toastTimer: null, dialogTarget: null, listVersion: 0, cache: new Map(), retries: new Map(),
  jobs: new Map(), reviewed: new Map(), jobActions: new Set(), polling: false, modelReady: false,
};

function remember(id) {
  try { localStorage.setItem("patchtrail.activeConversation", id); } catch { /* Optional. */ }
}

function remembered() {
  try { return localStorage.getItem("patchtrail.activeConversation"); } catch { return null; }
}

async function api(path, { method = "GET", body, signal } = {}) {
  const response = await fetch(path, {
    method, signal,
    headers: { "Content-Type": "application/json", "X-PatchTrail": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data;
  try { data = await response.json(); } catch { throw new Error("服务返回了无法识别的内容。"); }
  if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
  return data;
}

function toast(message, error = false) {
  clearTimeout(state.toastTimer);
  $("toast").textContent = message;
  $("toast").classList.toggle("error", error);
  $("toast").hidden = false;
  state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 6500 : 3000);
}

function setSidebar(open) {
  document.body.classList.toggle("sidebar-open", open);
  $("sidebar-backdrop").hidden = !open;
  $("menu-toggle").setAttribute("aria-expanded", String(open));
  // Keep the off-screen navigation out of the mobile keyboard tab order.
  $("sidebar").inert = matchMedia("(max-width: 700px)").matches && !open;
}

function renderList() {
  const query = $("search").value.trim().toLocaleLowerCase();
  const fragment = document.createDocumentFragment();
  const items = state.conversations.filter((item) => item.title.toLocaleLowerCase().includes(query));
  for (const item of items) {
    const button = document.createElement("button");
    button.className = "conversation-item";
    button.dataset.id = item.id;
    button.classList.toggle("active", state.active?.id === item.id);
    if (state.active?.id === item.id) button.setAttribute("aria-current", "page");
    button.title = item.title;
    const icon = document.createElement("span");
    icon.className = "chat-icon";
    icon.textContent = "◻";
    icon.setAttribute("aria-hidden", "true");
    const title = document.createElement("span");
    title.className = "item-title";
    title.textContent = item.title;
    button.append(icon, title);
    if (state.pending.has(item.id) || conversationBusy(item.id)) {
      const busy = document.createElement("span");
      busy.className = "spinner item-busy";
      busy.setAttribute("aria-label", "处理中");
      button.append(busy);
    }
    button.addEventListener("click", () => selectConversation(item.id));
    fragment.append(button);
  }
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty-list";
    empty.textContent = query ? "没有匹配的会话。" : "从新建会话开始。";
    fragment.append(empty);
  }
  $("conversation-list").replaceChildren(fragment);
  $("conversation-count").textContent = String(state.conversations.length);
}

async function refreshList() {
  const version = ++state.listVersion;
  const data = await api("/api/conversations");
  if (version !== state.listVersion) return;
  state.conversations = data.conversations;
  renderList();
}

function upsertConversation(conversation) {
  // Invalidate a list request started before this mutation completed.
  state.listVersion += 1;
  state.cache.set(conversation.id, conversation);
  const { messages: _messages, ...summary } = conversation;
  state.conversations = [summary, ...state.conversations.filter((item) => item.id !== conversation.id)];
  renderList();
}

function nearBottom() {
  const node = $("chat-scroll");
  return node.scrollHeight - node.scrollTop - node.clientHeight < 100;
}

function scrollBottom() {
  $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight;
  $("new-results").hidden = true;
}

function repositoryDetails(repository) {
  const details = document.createElement("details");
  details.className = "repository-result";
  details.dataset.status = repository.status;
  const summary = document.createElement("summary");
  summary.textContent = repository.status === "invalid" ? "仓库检查未通过" :
    repository.dirty ? "仓库信息 · 有未提交改动" : "仓库信息 · 工作区干净";
  details.append(summary);
  const list = document.createElement("dl");
  const entries = repository.status === "invalid" ? [["原因", repository.error]] : [
    ["路径", repository.path], ["分支", repository.branch], ["提交", repository.commit],
    ["跟踪文件", String(repository.tracked_files)], ["Python 文件", String(repository.python_files)],
    ["测试文件", `${repository.test_files}（按命名发现，未执行）`],
    ["测试线索", repository.test_samples.join("\n") || "未发现已跟踪的 Python 测试文件"],
  ];
  for (const [key, value] of entries) {
    const label = document.createElement("dt"); label.textContent = key;
    const text = document.createElement("dd"); text.textContent = value;
    list.append(label, text);
  }
  details.append(list);
  return details;
}

function messageNode(message) {
  const article = document.createElement("article");
  article.className = `message ${message.role}`;
  article.dataset.messageId = message.id || "pending";
  const header = document.createElement("div"); header.className = "message-header";
  const name = document.createElement("strong");
  name.textContent = message.role === "user" ? "你" : "↗ PatchTrail";
  header.append(name);
  if (message.role === "assistant") {
    const label = document.createElement("span"); label.className = "message-preview-label";
    label.textContent = message.metadata?.kind === "agent_job" ? "修复工作流" : "只读操作 · 未调用模型";
    header.append(label);
  }
  const content = document.createElement("div"); content.className = "message-content";
  // All model/user/repository text stays text, never HTML.
  content.textContent = message.content;
  article.append(header, content);
  if (message.metadata?.kind === "agent_job") {
    const job = (state.jobs.get(message.conversation_id) || []).find((item) => item.id === message.metadata.job_id);
    if (job) article.append(jobCard(job));
  }
  if (message.metadata?.repository) article.append(repositoryDetails(message.metadata.repository));
  if (message.role === "assistant") {
    const actions = document.createElement("div"); actions.className = "message-actions";
    const copy = document.createElement("button"); copy.className = "plain-button copy-button";
    copy.textContent = "复制回复";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(message.content); toast("已复制回复"); }
      catch { toast("复制失败，可以选择文字后手动复制。", true); }
    });
    actions.append(copy); article.append(actions);
  }
  return article;
}

function updateControls() {
  const busy = state.active && (state.pending.has(state.active.id) || conversationBusy(state.active.id));
  $("send").disabled = !state.ready || !state.active || busy || !$("message-input").value.trim();
  $("message-input").disabled = !state.ready || !state.active || busy;
  $("mode-select").disabled = !state.ready || busy;
  for (const id of ["repository-button", "rename-chat", "delete-chat", "export-chat", "task-settings"]) {
    $(id).disabled = !state.ready || !state.active || busy;
  }
  $("new-chat").disabled = !state.ready || state.creating;
}

function renderConversation({ bottom = false } = {}) {
  const active = state.active;
  const pending = active ? state.pending.get(active.id) : null;
  const messages = [...(active?.messages || [])];
  if (pending) messages.push({ role: "user", content: pending.content });
  $("conversation-title").textContent = active?.title || "新会话";
  $("welcome").hidden = messages.length > 0;
  $("messages").replaceChildren(...messages.map(messageNode));
  $("pending").hidden = !pending;
  $("pending-label").textContent = pending?.mode === "note" ? "正在保存需求…" : pending?.mode === "repair" ? "正在提交修复任务…" : "正在检查仓库…";
  const repo = active?.repository_path || "";
  $("task-hint").textContent = active?.task_source === "file" ?
    `任务文件：${active.task_path} · 最多 8 次模型请求、2 个候选 · 可能产生模型费用` : "退款示例（每次独立仓库）· 最多 8 次模型请求、2 个候选 · 可能产生模型费用";
  $("repository-label").textContent = repo ? repo.split(/[\\/]/).filter(Boolean).at(-1) || repo : "关联仓库";
  $("repository-button").title = repo || "设置本机 Git 仓库路径";
  $("repository-button").classList.toggle("connected", Boolean(repo));
  updateControls();
  renderList();
  if (bottom) scrollBottom();
}

async function selectConversation(id) {
  if (state.active) state.drafts.set(state.active.id, $("message-input").value);
  const version = ++state.loadVersion;
  state.loadController?.abort();
  state.loadController = new AbortController();
  try {
    let conversation = await api(`/api/conversations/${id}`, { signal: state.loadController.signal });
    if (version !== state.loadVersion) return;
    const cached = state.cache.get(id);
    if (cached && cached.updated_at > conversation.updated_at) conversation = cached;
    state.cache.set(id, conversation);
    state.active = conversation;
    remember(id);
    $("message-input").value = state.drafts.get(id) || "";
    renderConversation({ bottom: true });
    await refreshJobs();
    setSidebar(false);
  } catch (error) {
    if (error.name !== "AbortError" && version === state.loadVersion) toast(error.message, true);
  }
}

async function newConversation() {
  if (state.creating) return;
  state.creating = true; updateControls();
  try {
    const conversation = await api("/api/conversations", { method: "POST", body: {} });
    upsertConversation(conversation);
    await selectConversation(conversation.id);
    $("message-input").focus();
  } catch (error) { toast(error.message, true); }
  finally { state.creating = false; updateControls(); }
}

async function sendMessage(event) {
  event.preventDefault();
  const content = $("message-input").value.trim();
  const conversationId = state.active?.id;
  if (!content || !state.ready || !conversationId || state.pending.has(conversationId)) return;
  const mode = $("mode-select").value;
  const previous = state.retries.get(conversationId);
  const requestId = previous?.content === content && previous?.mode === mode &&
    previous?.repository === state.active.repository_path ? previous.requestId : crypto.randomUUID();
  state.pending.set(conversationId, { content, mode });
  state.drafts.delete(conversationId);
  $("message-input").value = "";
  renderConversation({ bottom: true });
  try {
    const result = await api(`/api/conversations/${conversationId}/messages`, {
      method: "POST", body: { content, mode, request_id: requestId },
    });
    state.pending.delete(conversationId);
    state.retries.delete(conversationId);
    upsertConversation(result);
    // A late reply must never replace another conversation's messages or draft.
    if (state.active?.id === conversationId) {
      const shouldScroll = nearBottom();
      state.active = result;
      renderConversation({ bottom: shouldScroll });
      if (!shouldScroll) $("new-results").hidden = false;
    }
  } catch (error) {
    state.pending.delete(conversationId);
    state.retries.set(conversationId, {
      content, mode, requestId, repository: state.cache.get(conversationId)?.repository_path || "",
    });
    state.drafts.set(conversationId, content);
    if (state.active?.id === conversationId) {
      $("message-input").value = content;
      renderConversation();
    }
    renderList();
    toast(`${error.message} 输入已保留；若网络中断，可刷新确认是否已经保存。`, true);
  }
}

function showDialog(id) {
  if (!state.active || state.pending.has(state.active.id) || conversationBusy(state.active.id)) return;
  state.dialogTarget = state.active.id;
  $(id).showModal();
}

function bindForm(id, callback) {
  $(id).addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $(id).querySelector('[type="submit"]');
    if (button.disabled) return;
    button.disabled = true;
    try { await callback(state.dialogTarget); }
    catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  });
}

async function patchConversation(id, body) {
  const result = await api(`/api/conversations/${id}`, { method: "PATCH", body });
  upsertConversation(result);
  if (state.active?.id === id) { state.active = result; renderConversation(); }
}

$("composer").addEventListener("submit", sendMessage);
$("message-input").addEventListener("input", updateControls);
$("message-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    if (!$("send").disabled) $("composer").requestSubmit();
  }
});
$("new-chat").addEventListener("click", newConversation);
$("search").addEventListener("input", renderList);
$("menu-toggle").addEventListener("click", () => setSidebar(!document.body.classList.contains("sidebar-open")));
$("sidebar-backdrop").addEventListener("click", () => { setSidebar(false); $("menu-toggle").focus(); });
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && document.body.classList.contains("sidebar-open")) {
    setSidebar(false); $("menu-toggle").focus();
  }
});
matchMedia("(max-width: 700px)").addEventListener("change", () => setSidebar(false));
$("new-results").addEventListener("click", scrollBottom);
$("chat-scroll").addEventListener("scroll", () => { if (nearBottom()) $("new-results").hidden = true; });

for (const button of document.querySelectorAll(".suggestion")) {
  button.addEventListener("click", () => {
    if (!state.active || state.pending.has(state.active.id)) return;
    $("message-input").value = button.dataset.prompt.replaceAll("\\n", "\n");
    $("mode-select").value = button.dataset.mode;
    updateControls(); $("message-input").focus();
  });
}
for (const button of document.querySelectorAll("[data-close-dialog]")) {
  button.addEventListener("click", () => button.closest("dialog").close());
}
$("repository-button").addEventListener("click", () => {
  $("repository-path").value = state.active?.repository_path || "";
  showDialog("repository-dialog");
});
$("rename-chat").addEventListener("click", () => {
  $("rename-input").value = state.active?.title || "";
  showDialog("rename-dialog");
});
$("delete-chat").addEventListener("click", () => showDialog("delete-dialog"));
bindForm("repository-form", async (id) => {
  await patchConversation(id, { repository_path: $("repository-path").value.trim() });
  $("repository-dialog").close(); toast("仓库路径已保存。发送任务即可检查。");
});
$("task-settings").addEventListener("click", () => {
  $("task-source").value = state.active?.task_source || "demo";
  $("task-path").value = state.active?.task_path || "";
  showDialog("task-dialog");
});
bindForm("task-form", async (id) => {
  const result = await api(`/api/conversations/${id}/task`, { method: "POST", body: {
    task_source: $("task-source").value, task_path: $("task-path").value.trim(),
  }});
  upsertConversation(result);
  if (state.active?.id === id) { state.active = result; renderConversation(); }
  $("task-dialog").close(); toast("任务设置已保存");
});
bindForm("rename-form", async (id) => {
  await patchConversation(id, { title: $("rename-input").value.trim() });
  $("rename-dialog").close();
});
bindForm("delete-form", async (id) => {
  await api(`/api/conversations/${id}`, { method: "DELETE" });
  $("delete-dialog").close();
  state.drafts.delete(id);
  state.cache.delete(id); state.retries.delete(id);
  if (state.active?.id === id) {
    state.active = null; $("message-input").value = ""; renderConversation();
  }
  await refreshList();
  if (!state.active) {
    if (state.conversations.length) await selectConversation(state.conversations[0].id);
    else await newConversation();
  }
});
$("export-chat").addEventListener("click", () => {
  if (!state.active) return;
  const blob = new Blob([JSON.stringify(state.active, null, 2)], { type: "application/json" });
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href; anchor.download = `patchtrail-${state.active.id}.json`;
  anchor.click(); setTimeout(() => URL.revokeObjectURL(href), 1000);
});

async function boot() {
  updateControls(); setSidebar(false);
  try {
    const health = await api("/api/healthz");
    state.modelReady = health.model.configured;
    $("model-label").textContent = health.model.model || "模型未配置";
    if (!state.modelReady) $("capability-text").textContent = "本机模型密钥尚未配置；仍可记录需求和只读检查仓库。";
    await refreshList();
    state.ready = true;
    $("connection-status").textContent = "本地服务已连接 · 需求与记录自动保存";
    const id = remembered();
    const selected = state.conversations.find((item) => item.id === id) || state.conversations[0];
    if (selected) await selectConversation(selected.id);
    else await newConversation();
  } catch (error) {
    $("connection-status").textContent = "无法连接本地服务，请确认 app.py 正在运行后刷新。";
    toast(error.message, true);
  }
  updateControls();
}

boot();

const activeStates = new Set(["QUEUED", "RUNNING", "STOP_REQUESTED"]);
const jobLabels = { QUEUED: "准备中", RUNNING: "正在修复", STOP_REQUESTED: "等待当前步骤结束后暂停",
  SUSPENDED: "已暂停", INTERRUPTED: "执行进程已中断", RECOVERY_REQUIRED: "需要恢复",
  WAITING_APPROVAL: "验收通过 · 待审批", APPROVED: "已批准 · 待应用", REJECTED: "已拒绝",
  APPLIED: "补丁已应用", FAILED: "未通过验收", EXPIRED: "审批已过期", INVALIDATED: "审批已失效",
  APPLYING: "正在应用", };
const nodeLabels = { prepare: "准备仓库与基准测试", model: "模型分析与工具规划", tools: "执行代码工具",
  feedback: "运行反馈测试", final_verification: "最终独立验收" };

function conversationBusy(id) {
  return (state.jobs.get(id) || []).some((job) => activeStates.has(job.state));
}

function jobCard(job) {
  const card = document.createElement("section"); card.className = "job-card";
  card.dataset.jobId = job.id; card.dataset.state = job.state;
  const title = document.createElement("strong"); title.textContent = jobLabels[job.state] || job.state;
  card.append(title);
  const stage = document.createElement("p");
  const requested = Math.max(0, ...job.timeline.filter(e => e.kind === "model_requested").map(e => e.call || 0));
  const modelCalls = Math.max(job.progress?.model_calls || 0, job.usage?.model_calls || 0, requested);
  const candidates = Math.max(job.progress?.candidate_count || 0, job.usage?.candidate_count || 0);
  stage.textContent = `${nodeLabels[job.progress?.node] || "等待后台进度"} · 模型请求 ${modelCalls}/8 · 候选 ${candidates}/2`;
  card.append(stage);
  const target = document.createElement("p"); target.className = "job-target";
  target.textContent = `目标仓库：${job.repository_path}`; card.append(target);
  if (job.error_code && ["FAILED", "RECOVERY_REQUIRED", "INTERRUPTED"].includes(job.state)) {
    const error = document.createElement("p"); error.className = "job-error";
    error.textContent = `${job.error_code}${job.error ? `：${job.error}` : ""}`; card.append(error);
  }
  if (job.verification) {
    const checks = document.createElement("div"); checks.className = "job-checks";
    const labels = { candidate_regression: "回归测试", candidate_feedback: "修复测试", candidate_holdout: "保留集验收" };
    for (const [key, label] of Object.entries(labels)) {
      const check = job.verification.stages[key];
      if (!check) continue;
      const row = document.createElement("span"); row.textContent = `${check.valid && check.passed ? "✓" : "✕"} ${label}`;
      checks.append(row);
    }
    card.append(checks);
  }
  const timeline = document.createElement("div"); timeline.className = "job-timeline";
  for (const event of job.timeline.slice(-4)) {
    const line = document.createElement("div");
    const toolLabels = { read_file: "读取源码", list_files: "查看源码目录", find_symbol: "定位函数", search_code: "搜索代码", edit_files: "生成候选补丁", submit_candidate: "提交候选验证" };
    const eventLabels = { run_finished: "本次执行结束", run_resumed: "继续上次执行", model_received: "模型响应已保存", interrupted_verifier_cleaned: "已清理中断的测试环境", unknown_model_retry_authorized: "已明确重试上一请求" };
    line.textContent = event.node ? `${nodeLabels[event.node] || "执行步骤"} · ${event.kind === "node_started" ? "开始" : event.kind === "node_replayed" ? "从记录恢复" : "完成"}` :
      event.kind === "model_requested" ? `模型请求 #${event.call}` : event.name ? (toolLabels[event.name] || "执行代码工具") : (eventLabels[event.kind] || "状态已更新");
    timeline.append(line);
  }
  card.append(timeline);
  const actions = document.createElement("div"); actions.className = "job-actions";
  function button(label, callback, disabled = false) {
    const b = document.createElement("button"); b.type = "button"; b.className = "plain-button"; b.textContent = label;
    b.disabled = disabled || state.jobActions.has(job.id); b.addEventListener("click", callback); actions.append(b);
  }
  if (activeStates.has(job.state)) button("暂停执行", () => jobAction(job, "pause"), job.state === "STOP_REQUESTED");
  if (["SUSPENDED", "INTERRUPTED", "RECOVERY_REQUIRED"].includes(job.state) && !job.approval) {
    if (job.error_code === "model_outcome_unknown") {
      const note = document.createElement("p"); note.textContent = "上一请求的结果和费用未知。重试将新增一次请求，原请求仍占用预算。"; card.append(note);
      button("明确重试模型请求", () => jobAction(job, "retry_unknown"));
    } else button("继续执行", () => jobAction(job, "resume"));
  }
  if (job.approval) {
    button("查看补丁", () => showDiff(job));
    if (job.state === "WAITING_APPROVAL") {
      button("批准补丁", () => jobAction(job, "approve"), state.reviewed.get(job.id) !== job.approval.binding_sha256);
      button("拒绝补丁", () => jobAction(job, "reject"));
    }
    if (job.state === "APPROVED") button("应用已批准补丁", () => jobAction(job, "apply"));
    if (["APPLYING", "RECOVERY_REQUIRED"].includes(job.state)) button("恢复补丁应用", () => jobAction(job, "recover_apply"));
  }
  card.append(actions);
  return card;
}

async function showDiff(job) {
  try {
    const result = await api(`/api/jobs/${job.id}/diff`);
    $("diff-target").textContent = `目标：${result.repository_path}`;
    $("diff-identity").textContent = `基准 ${result.base_commit}\n补丁 SHA-256 ${result.patch_sha256}`;
    $("diff-content").textContent = result.diff;
    state.reviewed.set(job.id, result.binding_sha256);
    $("diff-dialog").showModal(); renderConversation();
  } catch (error) { toast(error.message, true); }
}

async function jobAction(job, action) {
  if (state.jobActions.has(job.id)) return;
  state.jobActions.add(job.id); renderConversation();
  try {
    await api(`/api/jobs/${job.id}/actions`, { method: "POST", body: { action, binding: job.approval?.binding_sha256 || "" } });
    await refreshJobs();
  } catch (error) { toast(error.message, true); }
  finally { state.jobActions.delete(job.id); if (state.active?.id === job.conversation_id) renderConversation(); }
}

async function refreshJobs() {
  const id = state.active?.id;
  if (!id || !state.ready || state.polling) return;
  state.polling = true;
  try {
    const data = await api(`/api/conversations/${id}/jobs`);
    const previous = JSON.stringify(state.jobs.get(id));
    state.jobs.set(id, data.jobs);
    if (state.active?.id === id && JSON.stringify(data.jobs) !== previous) {
      const conversation = await api(`/api/conversations/${id}`);
      if (state.active?.id !== id) return;
      const bottom = nearBottom(); state.active = conversation; state.cache.set(id, conversation);
      renderConversation({ bottom });
    }
  } catch { /* A transient connection loss keeps the existing task cards visible. */ }
  finally { state.polling = false; }
}
setInterval(refreshJobs, 1500);
