// Chrome は起動せず、本番関数を合成した downloads/storage/URL と組み合わせる。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const extension = process.argv[2];
const run = fs.readFileSync(path.join(extension, "run.js"), "utf8");
const background = fs.readFileSync(path.join(extension, "background.js"), "utf8");
const functionSource = (name) => {
  const matches = [...run.matchAll(new RegExp(`^(?:async )?function ${name}\\([^\\n]*\\) \\{.*?^\\}`, "gms"))];
  assert.equal(matches.length, 1, name);
  return matches[0][0];
};
const source = [functionSource("trackDownload"), functionSource("saveJson")].join("\n");
const flush = () => new Promise(setImmediate);
const passed = [];

function event() {
  const listeners = new Set();
  return {listeners, addListener: (fn) => listeners.add(fn), removeListener: (fn) => listeners.delete(fn),
    emit: (item) => { for (const fn of [...listeners]) fn(item); }};
}

async function checkSave(name) {
  const onCreated = event();
  const onChanged = event();
  const timers = new Map();
  const searches = [];
  const revoked = [];
  let target = {id: 202, state: "in_progress", filename: "/downloads/probe/manifest.json"};
  let options;
  let blob;
  let releaseDownload;
  let releaseSearch;
  let rejectSearch;
  let settled = false;
  let disposed = 0;
  const registered = {};
  const removed = [];
  let timerSequence = 0;
  const context = vm.createContext({Blob, console, runId: "probe", WAIT: {savedFile: 30},
    URL: {createObjectURL: (value) => { blob = value; return "blob:synthetic"; },
      revokeObjectURL: (url) => revoked.push(url)},
    setTimeout: (fn) => { const id = ++timerSequence; timers.set(id, fn); return id; },
    clearTimeout: (id) => timers.delete(id),
    chrome: {storage: {session: {
      set: async (data) => {
        if (name === "register_reject") throw new Error("register rejected");
        if (name === "normal") await flush();
        Object.assign(registered, data);
      },
      remove: async (key) => {
        removed.push(key);
        if (["remove_reject", "download_remove_reject"].includes(name)) throw new Error("remove rejected");
        delete registered[key];
      },
    }},
      downloads: {onCreated, onChanged,
        download: (value) => {
          options = value;
          assert.equal(registered["jsonDownload:blob:synthetic"], "probe/manifest.json");
          if (["download_reject", "download_remove_reject"].includes(name)) return Promise.reject(new Error("download rejected"));
          if (name === "invalid_id") return Promise.resolve(null);
          if (["immediate", "remove_reject", "wrong_path"].includes(name)) target.state = "complete";
          if (name === "unrelated_first") return new Promise((resolve) => { releaseDownload = resolve; });
          return Promise.resolve(202);
        },
        search: ({id}) => {
          searches.push(id);
          if (name === "search_reject") return Promise.reject(new Error("search rejected"));
          if (["late_response", "late_reject"].includes(name) && searches.length === 1) {
            return new Promise((resolve, reject) => { releaseSearch = resolve; rejectSearch = reject; });
          }
          if (name === "wrong_search" && searches.length === 1) return Promise.resolve([{id: 101, state: "complete"}]);
          return Promise.resolve([{...target}]);
        }}}});
  vm.runInContext(source + `
    const originalTracker = trackDownload;
    trackDownload = (...args) => {
      const tracker = originalTracker(...args);
      const dispose = tracker.dispose;
      tracker.dispose = () => { markDispose(); return dispose(); };
      return tracker;
    };
    globalThis.start = () => saveJson("manifest.json", {probe_marker: "JSON"});`, context);
  context.markDispose = () => { disposed++; };
  if (name === "interrupted") target = {...target, state: "interrupted", error: "NETWORK_FAILED"};
  if (name === "wrong_path") target.filename = "/downloads/blob-uuid.json";
  if (name === "windows_path") target.filename = "C:\\downloads\\probe\\manifest.json";
  const promise = context.start();
  // 失敗系も直ちにhandlerを付け、後片付け前のunhandled rejectionを避ける。
  const outcome = promise.then(() => { settled = true; return null; }, (error) => { settled = true; return error; });
  await flush();
  if (name === "register_reject") assert.equal(options, undefined);
  else {
    assert.equal(options.filename, "probe/manifest.json");
    assert.equal(options.conflictAction, "overwrite");
    assert.equal(options.saveAs, false);
  }
  assert.equal(blob.type, "application/json");
  assert.equal(await blob.text(), '{\n  "probe_marker": "JSON"\n}\n');
  if (name === "unrelated_first") {
    onCreated.emit({id: 101, state: "complete"});
    onChanged.emit({id: 101, state: {current: "complete"}});
    assert.equal(searches.length, 0);
    releaseDownload(202);
    await flush();
  }
  const errors = {download_reject: "download rejected", invalid_id: "download did not return an id",
    search_reject: "search rejected", interrupted: "download interrupted: NETWORK_FAILED",
    timeout: "download did not finish within 0.03s", register_reject: "register rejected",
    remove_reject: "remove rejected", download_remove_reject: "download rejected",
    wrong_path: "JSON download saved to an unexpected path"};
  if (!errors[name] && name !== "immediate") {
    assert.equal(settled, false, "対象完了前に成功してはいけない");
    const before = searches.length;
    onCreated.emit({id: 101, state: "complete"});
    onChanged.emit({id: 101, state: {current: "complete"}});
    await flush();
    assert.equal(searches.length, before, "別件通知では検索しない");
    assert.equal(settled, false);
    target.state = "complete";
    onChanged.emit({id: 202, state: {current: "complete"}});
  }
  if (name === "timeout") {
    for (const fn of [...timers.values()]) fn();
  }
  const error = await outcome;
  if (errors[name]) assert.equal(error?.message, errors[name]);
  else assert.equal(error, null);
  if (name === "late_response") releaseSearch([{id: 202, state: "interrupted", error: "TOO_LATE"}]);
  if (name === "late_reject") rejectSearch(new Error("too late"));
  await flush();
  assert(searches.every((id) => id === 202));
  assert.equal(onCreated.listeners.size, 0);
  assert.equal(onChanged.listeners.size, 0);
  assert.equal(timers.size, 0);
  assert.deepEqual(revoked, ["blob:synthetic"]);
  assert.deepEqual(removed, ["jsonDownload:blob:synthetic"]);
  if (!["remove_reject", "download_remove_reject"].includes(name)) assert.deepEqual(registered, {});
  assert.equal(disposed, ["download_reject", "download_remove_reject", "register_reject", "invalid_id"].includes(name) ? 0 : 1);
  passed.push(name);
}

async function checkBackground() {
  let listener;
  const stored = {};
  let rejectRead = false;
  const context = vm.createContext({chrome: {runtime: {id: "probe-extension"},
    storage: {session: {get: async (key) => {
      if (rejectRead) throw new Error("read rejected");
      return {[key]: stored[key]};
    }}},
    downloads: {onDeterminingFilename: {addListener: (fn) => { listener = fn; }}}}});
  vm.runInContext(background.slice(background.indexOf("chrome.downloads.onDeterminingFilename.addListener")), context);
  const suggestFor = async (item) => {
    let calls;
    let count = 0;
    listener(item, (...args) => { calls = args; count++; });
    await flush();
    assert.equal(count, 1, "suggestは各ダウンロードで1回だけ呼ぶ");
    return calls;
  };
  stored.routing = {prefix: "csv-run/spend", name: "obsolete.json"};
  stored["jsonDownload:blob:own"] = "probe/manifest.json";
  const [own] = await suggestFor({byExtensionId: "probe-extension", url: "blob:own"});
  assert.equal(own.filename, "probe/manifest.json");
  assert.equal(own.conflictAction, "overwrite");
  assert.equal((await suggestFor({byExtensionId: "probe-extension", url: "blob:other"})).length, 0);
  assert.equal((await suggestFor({byExtensionId: "probe-extension", url: "blob:own-suffix"})).length, 0);
  delete stored.routing;
  assert.equal((await suggestFor({url: "blob:own", filename: "page.csv"})).length, 0, "URL一致だけのページには適用しない");
  assert.equal((await suggestFor({filename: "page.csv"})).length, 0, "JSON中のページCSVは保存先を変えない");
  stored.routing = {prefix: "csv-run/spend"};
  const [suggestion] = await suggestFor({filename: "page.csv"});
  assert.equal(suggestion.filename, "csv-run/spend/page.csv");
  assert.equal(suggestion.conflictAction, "overwrite");
  rejectRead = true;
  assert.equal((await suggestFor({byExtensionId: "probe-extension", url: "blob:own"})).length, 0);
  passed.push("background_routing");
}

async function checkConcurrent() {
  const stored = {};
  const onCreated = event();
  const onChanged = event();
  const items = new Map();
  const revoked = [];
  let nextId = 200;
  let nextUrl = 0;
  let determine;
  const context = vm.createContext({Blob, console, setTimeout, clearTimeout, runId: "probe", WAIT: {savedFile: 1000},
    URL: {createObjectURL: () => `blob:${++nextUrl}`, revokeObjectURL: (url) => revoked.push(url)},
    chrome: {runtime: {id: "probe-extension"}, storage: {session: {
      set: async (data) => Object.assign(stored, data), get: async (key) => ({[key]: stored[key]}),
      remove: async (key) => { delete stored[key]; },
    }}, downloads: {onCreated, onChanged,
      onDeterminingFilename: {addListener: (fn) => { determine = fn; }},
      download: async (options) => {
        assert.equal(stored[`jsonDownload:${options.url}`], options.filename);
        // 実際のbackground listenerとsaveJsonの登録を接続して保存先を決める。
        const suggestion = await new Promise((resolve) => {
          assert.equal(determine({byExtensionId: "probe-extension", url: options.url}, resolve), true);
        });
        assert.equal(suggestion.filename, options.filename);
        assert.equal(suggestion.conflictAction, "overwrite");
        const id = ++nextId;
        items.set(id, {id, filename: "/downloads/" + suggestion.filename, state: "in_progress"});
        return id;
      }, search: async ({id}) => [{...items.get(id)}],
    }}});
  vm.runInContext(background.slice(background.indexOf("chrome.downloads.onDeterminingFilename.addListener")) +
    "\n" + source + '\nglobalThis.start = (name) => saveJson(name, {});', context);
  const first = context.start("first.json");
  const second = context.start("second.json");
  await flush();
  assert.deepEqual(Object.keys(stored).sort(), ["jsonDownload:blob:1", "jsonDownload:blob:2"]);
  items.get(201).state = "complete";
  onChanged.emit({id: 201, state: {current: "complete"}});
  await first;
  assert.deepEqual(Object.keys(stored), ["jsonDownload:blob:2"]);
  items.get(202).state = "complete";
  onChanged.emit({id: 202, state: {current: "complete"}});
  await second;
  assert.deepEqual(stored, {});
  assert.deepEqual(revoked.sort(), ["blob:1", "blob:2"]);
  assert.equal(onCreated.listeners.size + onChanged.listeners.size, 0);
  passed.push("concurrent_registration");
}

(async () => {
  for (const name of ["normal", "immediate", "unrelated_first", "wrong_search", "late_response", "late_reject",
    "download_reject", "invalid_id", "search_reject", "interrupted", "timeout", "windows_path",
    "register_reject", "remove_reject", "download_remove_reject", "wrong_path"]) await checkSave(name);
  await checkBackground();
  await checkConcurrent();
  console.log(JSON.stringify({passed}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
