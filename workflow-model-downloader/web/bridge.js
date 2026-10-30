import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// Compatible with the MissingModelCard/Row selectors in frontend 1.57.4.
// No vendor patches, desktop-host impersonation, or automatic workflow runs.
const ENDPOINT = "/workflow-model-download/jobs";
const ONE = '[data-testid="missing-model-download"]';
const ALL = '[data-testid="missing-model-download-all"]';
const ROWS = '[data-testid="missing-model-importable-rows"]';
const ACTIVE = new Set(["queued", "downloading", "verifying"]);
const LABELS = {
  queued: "排队中", downloading: "下载中", verifying: "校验中",
  completed: "已完成", error: "下载失败", cancelled: "已取消",
};

let workflowRevision = 0;
let workflowLoading = false;
let embeddedModels = [];
let submitting = false;
let polling = null;
let pollTimer = null;
let stopped = false;
let jobs = [];
let ui;
let expanded = false;
let refreshPending = false;
const manifests = new Map();
const refreshed = new Set();
const actions = new Set();
const cards = new Map();

const keyOf = (model) => JSON.stringify([model.directory, model.name]);
const text = (value) => typeof value === "string" ? value : "";

function modelRecord(value) {
  if (!value || typeof value !== "object") return null;
  const model = {
    name: text(value.name), directory: text(value.directory), url: text(value.url),
  };
  if (!model.name || !model.directory || !model.url) return null;
  try {
    if (new URL(model.url).protocol !== "https:") return null;
  } catch { return null; }
  const hash = text(value.hash);
  const hashType = text(value.hash_type || value.hashType);
  if (hash) model.hash = hash;
  if (hashType) model.hash_type = hashType;
  return model;
}

// Capture original metadata because graph.serialize() can omit top-level models.
// These records are only a lookup fallback for a row already shown as missing.
function rememberWorkflow(data) {
  workflowRevision += 1;
  embeddedModels = [];
  const pending = [data];
  const seen = new Set();
  while (pending.length) {
    const graph = pending.pop();
    if (!graph || typeof graph !== "object" || seen.has(graph)) continue;
    seen.add(graph);
    const sources = [graph.models];
    for (const node of Array.isArray(graph.nodes) ? graph.nodes : []) {
      sources.push(node?.properties?.models);
    }
    for (const source of sources) {
      if (!Array.isArray(source)) continue;
      for (const value of source) {
        const model = modelRecord(value);
        if (model) embeddedModels.push(model);
      }
    }
    if (Array.isArray(graph.definitions?.subgraphs)) {
      pending.push(...graph.definitions.subgraphs);
    }
  }
}

function readRow(button) {
  const rows = button.closest(ROWS);
  if (!rows) throw new Error("无法识别这个缺失模型条目，请刷新页面后重试。");
  const row = Array.from(rows.children).find((child) => child.contains(button));
  const header = row?.firstElementChild;
  const info = header && Array.from(header.children).find((child) =>
    child.tagName === "SPAN" && child.firstElementChild?.querySelector("[title]"));
  const label = info?.firstElementChild && Array.from(info.firstElementChild.children)
    .find((child) => (child.tagName === "BUTTON" || child.tagName === "SPAN") && child.hasAttribute("title"));
  const name = label?.getAttribute("title");
  const directory = info?.children[1]?.textContent?.split(" · ")[0].trim();
  if (!name || !directory || !/^[\w-]+$/.test(directory)) {
    throw new Error("无法读取模型名称和分类，请刷新页面后重试。");
  }
  return { name, directory };
}

function selectedRows(button) {
  if (button.matches(ONE)) return [readRow(button)];
  const actionsBox = button.closest('[data-testid="missing-model-actions"]');
  const card = actionsBox?.parentElement;
  if (!card?.querySelector(ROWS)) {
    throw new Error("无法识别缺失模型列表，请刷新页面后重试。");
  }
  const selected = Array.from(card.querySelectorAll(ONE), readRow);
  if (!selected.length) throw new Error("当前没有可下载的缺失模型。");
  return [...new Map(selected.map((model) => [keyOf(model), model])).values()];
}

function resolveUnique(row, available) {
  const matches = available.filter((model) => keyOf(model) === keyOf(row));
  const urls = new Set(matches.map((model) => model.url));
  if (!matches.length) return null;
  if (urls.size !== 1) {
    throw new Error(`“${row.name}”有多个下载来源，无法确定对应文件。请核对工作流中的模型信息。`);
  }
  const hashes = new Set(matches.filter((model) => model.hash)
    .map((model) => JSON.stringify([model.hash_type?.toLowerCase(), model.hash?.toLowerCase()])));
  if (hashes.size > 1) {
    throw new Error(`“${row.name}”的校验信息不一致，请核对工作流中的模型信息。`);
  }
  return { ...matches[0], ...matches.find((model) => model.hash) };
}

async function resolveModels(rows, revision) {
  if (workflowLoading) throw new Error("工作流正在加载，请加载完成后再点击下载。");
  if (typeof app.refreshMissingModels !== "function") {
    throw new Error("当前前端缺少模型识别接口，请检查 ComfyUI 前端版本。");
  }
  const result = await app.refreshMissingModels({ silent: true, reloadDefs: false });
  if (workflowLoading || revision !== workflowRevision) throw new Error("工作流已切换，请在当前工作流中重新点击下载。");
  const candidates = [
    ...(result?.missingModels || []),
    ...(result?.confirmedCandidates || []).filter((model) => model.isMissing === true),
  ].map(modelRecord).filter(Boolean);
  return rows.map((row) => {
    const model = resolveUnique(row, candidates) || resolveUnique(row, embeddedModels);
    if (!model) {
      throw new Error(`未找到“${row.name}”的完整下载信息。请刷新缺失模型列表后重试。`);
    }
    return model;
  });
}

async function request(path = ENDPOINT, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await api.fetchApi(path, {
      ...options, signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(options.method === "POST" ? { "X-Workflow-Model-Download": "1" } : {}),
        ...options.headers,
      },
    });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(text(body?.error) || text(body?.message) || `请求失败（${response.status}）`);
    }
    return body;
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("连接超时。后台任务可能仍在继续，请刷新下载状态。");
    throw error;
  } finally { clearTimeout(timeout); }
}

function make(tag, className, content) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined) element.textContent = content;
  return element;
}

function makeButton(label, handler) {
  const button = make("button", "wmd-button", label);
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}

function ensureUI() {
  if (ui) return ui;
  const style = make("style");
  style.textContent = `
    #workflow-model-download-panel {
      --wmd-bg: var(--comfy-menu-bg, #20232a); --wmd-fg: var(--fg-color, #f4f4f5);
      --wmd-border: var(--border-color, #707781); --wmd-accent: #5aa9f8;
      position:fixed; left:72px; bottom:16px; z-index:2100;
      width:min(420px,calc(100vw - 88px)); color:var(--wmd-fg);
      font:14px/1.5 system-ui,sans-serif;
    }
    #workflow-model-download-panel [hidden] { display:none !important; }
    #workflow-model-download-panel * { box-sizing:border-box; }
    #workflow-model-download-panel .wmd-shell { background:var(--wmd-bg); border:1px solid var(--wmd-border); border-radius:12px; box-shadow:0 6px 28px #0005; overflow:hidden; }
    #workflow-model-download-panel .wmd-header { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:10px 12px; }
    #workflow-model-download-panel .wmd-title { margin:0; font-size:16px; font-weight:650; }
    #workflow-model-download-panel .wmd-body { max-height:min(60vh,560px); overflow:auto; overscroll-behavior:contain; padding:0 12px 12px; }
    #workflow-model-download-panel .wmd-note { margin:0 0 10px; overflow-wrap:anywhere; }
    #workflow-model-download-panel .wmd-error { border-left:3px solid #ef7777; padding-left:8px; }
    #workflow-model-download-panel .wmd-list { display:flex; flex-direction:column; gap:10px; }
    #workflow-model-download-panel .wmd-card { border:1px solid var(--wmd-border); border-radius:8px; padding:10px; }
    #workflow-model-download-panel .wmd-name { font-weight:600; overflow-wrap:anywhere; }
    #workflow-model-download-panel .wmd-meta { overflow-wrap:anywhere; margin:3px 0; }
    #workflow-model-download-panel .wmd-progress { width:100%; height:8px; accent-color:var(--wmd-accent); margin:6px 0; }
    #workflow-model-download-panel .wmd-actions { display:flex; align-items:center; justify-content:space-between; gap:8px; }
    #workflow-model-download-panel .wmd-button { min-height:36px; padding:6px 12px; border:1px solid var(--wmd-border); border-radius:7px; color:inherit; background:var(--wmd-bg); font:inherit; cursor:pointer; }
    #workflow-model-download-panel .wmd-button:hover { filter:brightness(1.16); }
    #workflow-model-download-panel .wmd-button:active { filter:brightness(.9); }
    #workflow-model-download-panel .wmd-button:disabled { opacity:.6; cursor:wait; }
    #workflow-model-download-panel .wmd-button:focus-visible { outline:2px solid var(--wmd-accent); outline-offset:2px; }
    #workflow-model-download-panel .wmd-launcher { box-shadow:0 4px 16px #0004; }
    #workflow-model-download-panel .wmd-footer { display:flex; justify-content:space-between; align-items:center; gap:8px; margin-top:12px; }
    @media(max-width:520px) { #workflow-model-download-panel { left:12px; bottom:12px; width:calc(100vw - 24px); } }
  `;
  document.head.append(style);
  const root = make("section");
  root.id = "workflow-model-download-panel";
  root.setAttribute("aria-label", "模型下载");
  root.hidden = true;
  const launcher = makeButton("模型下载", () => showPanel());
  launcher.classList.add("wmd-launcher");
  launcher.setAttribute("aria-controls", "workflow-model-download-content");
  const shell = make("div", "wmd-shell");
  shell.id = "workflow-model-download-content";
  const header = make("div", "wmd-header");
  const title = make("h2", "wmd-title", "模型下载");
  const collapse = makeButton("收起", () => {
    expanded = false;
    render();
    launcher.focus();
  });
  header.append(title, collapse);
  const body = make("div", "wmd-body");
  const notice = make("p", "wmd-note", "下载的模型会自动保存到统一模型库。");
  notice.setAttribute("role", "status");
  notice.setAttribute("aria-live", "polite");
  notice.setAttribute("aria-atomic", "true");
  const list = make("div", "wmd-list");
  const footer = make("div", "wmd-footer");
  const summary = make("span");
  const refresh = makeButton("刷新状态", () => void poll(true));
  footer.append(summary, refresh);
  body.append(notice, list, footer);
  shell.append(header, body);
  root.append(launcher, shell);
  document.body.append(root);
  ui = { root, launcher, shell, notice, list, summary, refresh };
  render();
  return ui;
}

function showPanel(message, error = false) {
  ensureUI();
  expanded = true;
  ui.root.hidden = false;
  if (message) setNotice(message, error);
  render();
}

function setNotice(message, error = false) {
  ensureUI();
  if (ui.notice.textContent !== message) ui.notice.textContent = message;
  ui.notice.classList.toggle("wmd-error", error);
}

function size(value) {
  let number = Number(value);
  if (!Number.isFinite(number) || number < 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  while (number >= 1024 && unit < units.length - 1) { number /= 1024; unit++; }
  return `${number.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function createCard(job) {
  const element = make("article", "wmd-card");
  const name = make("div", "wmd-name");
  const category = make("div", "wmd-meta");
  const progress = make("progress", "wmd-progress");
  progress.max = 100;
  const status = make("div", "wmd-meta");
  const error = make("div", "wmd-meta wmd-error");
  const actionsBox = make("div", "wmd-actions");
  const bytes = make("span");
  const action = makeButton("取消", () => {
    const current = jobs.find((item) => String(item.id) === String(job.id));
    if (current) void actOnJob(current);
  });
  actionsBox.append(bytes, action);
  element.append(name, category, status, progress, error, actionsBox);
  return { element, name, category, progress, status, error, bytes, action };
}

function render() {
  if (!ui) return;
  const active = jobs.filter((job) => ACTIVE.has(job.state)).length;
  ui.launcher.textContent = active ? `模型下载 · ${active} 项进行中` : "模型下载";
  ui.launcher.setAttribute("aria-expanded", String(expanded));
  ui.launcher.hidden = expanded;
  ui.shell.hidden = !expanded;
  ui.refresh.disabled = Boolean(polling);
  ui.summary.textContent = jobs.length ? `${active} 项进行中 · ${jobs.filter((job) => job.state === "completed").length} 项完成` : "暂无下载任务";
  const currentIds = new Set(jobs.map((job) => String(job.id)));
  for (const [id, card] of cards) {
    if (!currentIds.has(id)) { card.element.remove(); cards.delete(id); }
  }
  for (const job of jobs) {
    const id = String(job.id);
    let card = cards.get(id);
    if (!card) { card = createCard(job); cards.set(id, card); ui.list.append(card.element); }
    card.name.textContent = job.name || "模型文件";
    card.category.textContent = job.directory || "";
    card.status.textContent = LABELS[job.state] || "状态更新中";
    const total = Number(job.total);
    const downloaded = Math.max(0, Number(job.downloaded) || 0);
    if (job.state === "completed") card.progress.value = 100;
    else if (Number.isFinite(total) && total > 0) card.progress.value = Math.min(100, downloaded / total * 100);
    else card.progress.removeAttribute("value");
    card.progress.setAttribute("aria-label", `${job.name}下载进度`);
    card.progress.hidden = !ACTIVE.has(job.state) && job.state !== "completed";
    card.bytes.textContent = total > 0 ? `${size(downloaded)} / ${size(total)}` : size(downloaded);
    card.error.textContent = text(job.error);
    card.error.hidden = !job.error;
    card.action.hidden = !ACTIVE.has(job.state) && job.state !== "error" && job.state !== "cancelled";
    card.action.textContent = actions.has(id) ? "处理中…" : ACTIVE.has(job.state) ? "取消" : "重试";
    card.action.disabled = actions.has(id);
    card.action.setAttribute("aria-label", `${ACTIVE.has(job.state) ? "取消" : "重试"} ${job.name}`);
  }
}

async function refreshCompleted() {
  if (refreshPending || workflowLoading) return;
  // Job IDs identify targets and are reused on retry/redownload. The backend
  // completion timestamp distinguishes attempts without refreshing every poll.
  const completionKey = (job) => JSON.stringify([String(job.id), job.updated ?? null]);
  const completed = jobs.filter((job) => job.state === "completed" && !refreshed.has(completionKey(job)));
  if (!completed.length) return;
  refreshPending = true;
  try {
    await app.refreshMissingModels({ silent: true });
    completed.forEach((job) => refreshed.add(completionKey(job)));
    setNotice("模型已保存，缺失模型列表已刷新，可以继续使用工作流。");
  } catch (error) {
    setNotice(`模型已保存，但列表刷新失败：${error.message}。可点击刷新状态重试。`, true);
  } finally { refreshPending = false; }
}

function schedulePoll(delay = 1500) {
  clearTimeout(pollTimer);
  if (!stopped && (expanded || jobs.some((job) => ACTIVE.has(job.state)))) {
    pollTimer = setTimeout(() => void poll(), delay);
  }
}

async function poll(showError = false) {
  if (polling) return polling;
  polling = (async () => {
    try {
      const result = await request();
      if (!Array.isArray(result?.jobs)) throw new Error("下载状态返回异常，请重启 ComfyUI 后重试。");
      jobs = result.jobs.filter((job) => job && job.id !== undefined);
      for (const job of jobs) {
        const model = modelRecord(job.model) || modelRecord(job);
        if (model) manifests.set(keyOf(model), model);
      }
      if (jobs.length) {
        ensureUI();
        ui.root.hidden = false;
      }
      render();
      void refreshCompleted();
    } catch (error) {
      if (showError) showPanel(`无法更新下载状态：${error.message}`, true);
      else if (ui && !ui.root.hidden) setNotice(`连接暂时中断，正在重试。${error.message}`, true);
    }
  })();
  render();
  try { await polling; }
  finally { polling = null; render(); schedulePoll(); }
}

async function enqueue(models) {
  for (const model of models) manifests.set(keyOf(model), model);
  const result = await request(ENDPOINT, { method: "POST", body: JSON.stringify({ models }) });
  if (Array.isArray(result?.jobs)) jobs = result.jobs;
  setNotice(`${models.length} 个模型已提交，后台会依次下载到统一模型库。`);
  await poll(true);
}

async function actOnJob(job) {
  const id = String(job.id);
  if (actions.has(id)) return;
  actions.add(id);
  render();
  try {
    if (ACTIVE.has(job.state)) {
      await request(`${ENDPOINT}/${encodeURIComponent(id)}/cancel`, { method: "POST", body: "{}" });
      setNotice(`已请求取消“${job.name}”。`);
      await poll(true);
    } else {
      const model = modelRecord(job.model) || modelRecord(job) || manifests.get(keyOf(job)) ||
        (await resolveModels([{ name: job.name, directory: job.directory }], workflowRevision))[0];
      await enqueue([model]);
    }
  } catch (error) { showPanel(error.message, true); }
  finally { actions.delete(id); render(); }
}

async function downloadSelected(rows, revision) {
  try {
    const models = await resolveModels(rows, revision);
    await enqueue(models);
  } catch (error) { showPanel(error.message, true); }
  finally { submitting = false; schedulePoll(); }
}

function interceptDownload(event) {
  const target = event.target instanceof Element ? event.target : event.target?.parentElement;
  const button = target?.closest(`${ONE},${ALL}`);
  if (!button || button.disabled) return;
  // Stop the native handler synchronously, before any scan/network await.
  // Gated-repository links, copy URL, and every other button remain untouched.
  event.preventDefault();
  event.stopImmediatePropagation();
  if (submitting) { showPanel("正在识别模型并提交下载，请稍候。"); return; }
  try {
    const rows = selectedRows(button);
    submitting = true;
    showPanel(`正在识别 ${rows.length} 个模型的下载来源…`);
    void downloadSelected(rows, workflowRevision);
  } catch (error) { showPanel(error.message, true); }
}

app.registerExtension({
  name: "local.workflow-model-download",
  beforeLoadGraph() {
    workflowRevision += 1;
    workflowLoading = true;
    embeddedModels = [];
  },
  beforeConfigureGraph(graphData) { rememberWorkflow(graphData); },
  afterLoadGraph() { workflowLoading = false; },
  onGraphLoadError() { workflowLoading = false; },
  setup() {
    document.addEventListener("click", interceptDownload, true);
    window.addEventListener("pagehide", () => { stopped = true; clearTimeout(pollTimer); });
    window.addEventListener("pageshow", () => { stopped = false; void poll(); });
    void poll();
  },
});
