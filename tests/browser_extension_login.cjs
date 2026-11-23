// Chrome は起動せず、本番の実行ページを合成の DOM と Chrome API で動かす。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const run = fs.readFileSync(path.join(process.argv[2], "run.js"), "utf8");
const runId = "corp-login-20261123-120000";
const tabId = 202;
const passed = [];

function element(hidden = false) {
  const target = new EventTarget();
  return {
    textContent: "",
    classList: { toggle() {} },
    hidden,
    disabled: false,
    addEventListener: target.addEventListener.bind(target),
    click() {
      if (!this.disabled) target.dispatchEvent(new Event("click"));
    },
  };
}

function storage(data = {}) {
  return {
    get: async (key) => ({ [key]: structuredClone(data[key]) }),
    set: async (values) => { Object.assign(data, structuredClone(values)); },
    remove: async (key) => { delete data[key]; },
  };
}

async function runLoginPage({ embedded = false, local = {} } = {}) {
  const elements = { log: element(), status: element(), confirm: element(true) };
  const removals = [];
  const creates = [];
  const updates = [];
  const window = {};
  window.top = embedded ? {} : window;
  const context = vm.createContext({
    document: { getElementById: (id) => elements[id] },
    location: { hash: "#" + encodeURIComponent(JSON.stringify({ run_id: runId, action: "login" })) },
    window,
    console: { log() {} },
    chrome: {
      storage: { local: storage(local), session: storage() },
      tabs: {
        create: async (options) => {
          creates.push(structuredClone(options));
          return { id: tabId };
        },
        update: async (id, options) => { updates.push([id, structuredClone(options)]); },
      },
      cookies: {
        getAll: async () => [
          { name: "sessionKey", path: "/", value: "synthetic" },
          { name: "sessionKey-copy", path: "/copy", value: "synthetic" },
          { name: "lastActiveOrg", path: "/", value: "synthetic" },
        ],
        remove: async (options) => {
          removals.push(structuredClone(options));
          return true;
        },
      },
      webRequest: { onBeforeRequest: { addListener() {} } },
    },
  });
  vm.runInContext(run, context, { filename: "run.js" });
  await new Promise(setImmediate);
  return { elements, removals, creates, updates };
}

function assertNoSideEffects({ removals, creates, updates }, scene) {
  assert.equal(removals.length, 0, `${scene}は Cookie を消さない`);
  assert.equal(creates.length, 0, `${scene}は作業タブを作らない`);
  assert.equal(updates.length, 0, `${scene}はタブを移動しない`);
}

function assertNoFatal({ status, log }) {
  assert.doesNotMatch(log.textContent, /FATAL/);
  assert.notEqual(status.textContent, "失敗（ログを確認してください）");
}

async function checkConfirmation() {
  const page = await runLoginPage();
  const { elements, removals, creates, updates } = page;
  assertNoFatal(elements);
  assertNoSideEffects(page, "クリック前");
  assert.equal(elements.confirm.hidden, false);
  assert.match(elements.status.textContent, /ログインし直す/);

  elements.confirm.click();
  await new Promise(setImmediate);
  assertNoFatal(elements);
  assert.deepEqual(removals, [
    { url: "https://claude.ai/", name: "sessionKey" },
    { url: "https://claude.ai/copy", name: "sessionKey-copy" },
  ]);
  assert.deepEqual(creates, [{ url: "about:blank", active: true }]);
  assert.deepEqual(updates, [[tabId, { url: "https://claude.ai/login" }]]);
  assert.equal(elements.confirm.hidden, true);
  assert.equal(elements.confirm.disabled, true);
  assert.match(elements.log.textContent, /login confirmed/);
  passed.push("confirmation");
}

async function checkBlocked(name, options, scene, status) {
  const page = await runLoginPage(options);
  assertNoFatal(page.elements);
  assertNoSideEffects(page, scene);
  assert.equal(page.elements.confirm.hidden, true, `${scene}はボタンを出さない`);
  assert.match(page.elements.status.textContent, status);
  passed.push(name);
}

async function checkReload() {
  const local = { started: [] };
  const first = await runLoginPage({ local });
  assertNoFatal(first.elements);
  assert.equal(first.elements.confirm.hidden, false, "初回表示はボタンを出す");

  await checkBlocked("reload", { local }, "再読み込み後", /開始済み/);
  assertNoSideEffects(first, "クリックしていない初回表示");
}

(async () => {
  await checkConfirmation();
  await checkBlocked("embedded", { embedded: true }, "iframe 内", /埋め込まれた状態では実行しません/);
  await checkReload();
  console.log(JSON.stringify({ passed }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
