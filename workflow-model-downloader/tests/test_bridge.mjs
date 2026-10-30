// Run: node test_bridge.mjs [path/to/bridge.js]
// The browser-free harness replaces only app/api imports and the visual shell.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import vm from "node:vm";

const sourcePath = process.argv[2]
  ? resolve(process.argv[2])
  : fileURLToPath(new URL("../web/bridge.js", import.meta.url));
const source = readFileSync(sourcePath, "utf8").replace(/^import .*;\r?\n/gm, "");
const model = {
  name: "subfolder/example.safetensors", directory: "vae",
  url: "https://huggingface.co/example/repo/resolve/main/example.safetensors",
};

function harness() {
  const state = { extension: null, result: { missingModels: [] }, calls: [], refreshes: 0, notices: [] };
  class Element {}
  const context = vm.createContext({
    URL, AbortController, setTimeout, clearTimeout, console, Element,
    app: {
      registerExtension: (extension) => { state.extension = extension; },
      refreshMissingModels: async () => { state.refreshes++; return state.result; },
    },
    api: {
      fetchApi: async (path, options) => {
        state.calls.push({ path, options });
        return { ok: true, json: async () => ({ jobs: [] }) };
      },
    },
    note: (message) => state.notices.push(message),
  });
  vm.runInContext(source, context, { filename: sourcePath });
  vm.runInContext(`
    setNotice = (message) => note(message);
    showPanel = (message) => note(message);
    render = () => {};
    globalThis.bridge = {
      resolveModels, resolveUnique, modelRecord, readRow, selectedRows,
      interceptDownload, request, refreshCompleted,
      revision: () => workflowRevision,
      setJobs: (values) => { jobs = values; },
      setDownloadHandler: (handler) => { downloadSelected = handler; },
    };
  `, context);
  return { state, context, bridge: context.bridge, extension: state.extension, Element };
}

function fakeRow(h, selected = model) {
  const label = {
    tagName: "BUTTON", hasAttribute: (key) => key === "title",
    getAttribute: () => selected.name,
  };
  const copyLink = { tagName: "BUTTON", hasAttribute: () => true, getAttribute: () => "复制链接" };
  const labels = { children: [label, copyLink], querySelector: () => label };
  const info = {
    tagName: "SPAN", firstElementChild: labels,
    children: [labels, { textContent: `${selected.directory} · 2.3 GB` }],
  };
  const header = { children: [info] };
  const button = new h.Element();
  const rows = { children: [{ contains: (value) => value === button, firstElementChild: header }] };
  button.closest = (selector) => selector.includes("importable-rows") ? rows : button;
  button.matches = () => true;
  return button;
}

test("model lookup isolates categories and preserves relative model names", async () => {
  const h = harness();
  const other = { ...model, directory: "checkpoints", url: "https://huggingface.co/other/repo/resolve/main/example.safetensors" };
  h.state.result = { missingModels: [model, model, other] };
  const found = await h.bridge.resolveModels([model, other], h.bridge.revision());
  assert.equal(found.length, 2);
  assert.equal(found[0].name, model.name);
  assert.equal(found[0].url, model.url);
  assert.equal(found[1].url, other.url);
});

test("conflicting download sources and integrity metadata are refused", () => {
  const { bridge } = harness();
  assert.throws(() => bridge.resolveUnique(model, [model, { ...model, url: `${model.url}?other=1` }]), /多个下载来源/);
  assert.throws(() => bridge.resolveUnique(model, [
    { ...model, hash: "a".repeat(64), hash_type: "sha256" },
    { ...model, hash: "b".repeat(64), hash_type: "sha256" },
  ]), /校验信息不一致/);
  assert.equal(bridge.modelRecord({ ...model, url: "javascript:alert(1)" }), null);
  assert.equal(bridge.modelRecord({ ...model, url: "http://example.com/model" }), null);
});

test("nested workflow metadata is available but never leaks into the next workflow", async () => {
  const h = harness();
  h.extension.beforeLoadGraph();
  h.extension.beforeConfigureGraph({ definitions: { subgraphs: [{ nodes: [{ properties: { models: [model] } }] }] } });
  h.extension.afterLoadGraph();
  const found = await h.bridge.resolveModels([model], h.bridge.revision());
  assert.equal(found[0].url, model.url);
  h.extension.beforeLoadGraph();
  h.extension.afterLoadGraph();
  await assert.rejects(() => h.bridge.resolveModels([model], h.bridge.revision()), /未找到/);
});

test("a workflow switch during scanning prevents downloading its stale selection", async () => {
  const h = harness();
  h.context.app.refreshMissingModels = async () => {
    h.extension.beforeLoadGraph();
    h.extension.afterLoadGraph();
    return { missingModels: [model] };
  };
  await assert.rejects(() => h.bridge.resolveModels([model], h.bridge.revision()), /工作流已切换/);
  assert.equal(h.state.calls.length, 0);
});

test("single-row matching reads the full name instead of the copy-link tooltip", () => {
  const h = harness();
  const selected = h.bridge.readRow(fakeRow(h));
  assert.equal(selected.name, model.name);
  assert.equal(selected.directory, model.directory);
});

test("download-all stays within its card and deduplicates repeated references", () => {
  const h = harness();
  const second = { ...model, name: "other.safetensors" };
  const rowButtons = [fakeRow(h), fakeRow(h), fakeRow(h, second)];
  const card = { querySelector: () => ({}), querySelectorAll: () => rowButtons };
  const all = { matches: () => false, closest: () => ({ parentElement: card }) };
  const rows = h.bridge.selectedRows(all);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].name, model.name);
  assert.equal(rows[1].name, second.name);
});

test("native download is stopped synchronously while ordinary/gated links are untouched", () => {
  const h = harness();
  let stopped = 0;
  let downloads = 0;
  h.bridge.setDownloadHandler(async (rows) => { downloads++; assert.equal(rows[0].name, model.name); });
  const gatedLink = new h.Element();
  gatedLink.closest = () => null;
  const event = (target) => ({ target, preventDefault: () => stopped++, stopImmediatePropagation: () => stopped++ });
  h.bridge.interceptDownload(event(gatedLink));
  assert.equal(stopped, 0);
  assert.equal(downloads, 0);
  h.bridge.interceptDownload(event(fakeRow(h)));
  assert.equal(stopped, 2);
  assert.equal(downloads, 1);
});

test("write requests carry JSON and the same-origin marker; HTTP errors propagate", async () => {
  const h = harness();
  await h.bridge.request("/workflow-model-download/jobs", { method: "POST", body: "{}" });
  const sent = h.state.calls[0];
  assert.equal(sent.options.headers["Content-Type"], "application/json");
  assert.equal(sent.options.headers["X-Workflow-Model-Download"], "1");
  h.context.api.fetchApi = async () => ({ ok: false, status: 400, json: async () => ({ error: "测试错误" }) });
  await assert.rejects(() => h.bridge.request(), /测试错误/);
});

test("a completed target is refreshed once per attempt, including later redownloads", async () => {
  const h = harness();
  h.bridge.setJobs([{ id: "same-target", state: "completed", updated: 10 }]);
  await h.bridge.refreshCompleted();
  await h.bridge.refreshCompleted();
  assert.equal(h.state.refreshes, 1);
  h.bridge.setJobs([{ id: "same-target", state: "completed", updated: 20 }]);
  await h.bridge.refreshCompleted();
  assert.equal(h.state.refreshes, 2, "a later completed attempt must refresh definitions again");
});
