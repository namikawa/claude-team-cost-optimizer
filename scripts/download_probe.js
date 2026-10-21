/* 合成検証専用。Chrome の API は実機モードでは差し替えない。 */

if (typeof module !== "undefined" && module.exports) {
  runMocks().catch((error) => { console.error(error); process.exitCode = 1; });
} else {
  setupBrowser();
}

async function runMocks() {
  const fs = require("node:fs");
  const path = require("node:path");
  const vm = require("node:vm");
  const assert = require("node:assert/strict");
  const [flag, root] = process.argv.slice(2);
  if (flag !== "--mock" || !root) throw new Error("usage: node scripts/download_probe.js --mock <root>");
  const config = JSON.parse(fs.readFileSync(path.join(root, "probe.json"), "utf8"));
  if (config.format !== "seat-download-probe-v1" || path.resolve(root) !== config.root) {
    throw new Error("not a generated probe root");
  }
  const source = fs.readFileSync(path.join(root, "extension/production.js"), "utf8");
  const cases = [];
  for (const duringWait of [false, true]) {
    const events = [];
    const event = () => {
      const listeners = new Set();
      return { addListener: (fn) => listeners.add(fn), removeListener: (fn) => listeners.delete(fn),
        emit: (value) => { for (const fn of listeners) fn(value); } };
    };
    const onCreated = event();
    const onChanged = event();
    let releaseDownload;
    let releaseSearch;
    let settled = false;
    let returnedId = null;
    let complete = !duringWait;
    const old = {id: 101, state: "complete", filename: "unrelated.csv"};
    const context = vm.createContext({setTimeout, clearTimeout, console, Blob, runId: "probe",
      URL: {createObjectURL: () => "blob:synthetic", revokeObjectURL: () => {}},
      observe: (item) => { returnedId = item.id; },
      chrome: {storage: {session: {set: async () => {}, remove: async () => {}}},
        downloads: {onCreated, onChanged,
        download: () => new Promise((resolve) => { releaseDownload = resolve; }),
        search: ({id}) => {
        events.push({event: "search", id});
        assert.equal(id, 202);
        if (duringWait && !releaseSearch) return new Promise((resolve) => { releaseSearch = resolve; });
        return Promise.resolve([{id: 202, state: complete ? "complete" : "in_progress",
          filename: "/downloads/probe/manifest.json"}]);
      }}}});
    vm.runInContext(source + `
      const original = trackDownload;
      trackDownload = (...args) => {
        const tracker = original(...args);
        tracker.promise.then(observe);
        return tracker;
      };
      globalThis.start = () => saveJson("manifest.json", {probe_marker: "JSON"});`, context);
    const promise = context.start().then(() => { settled = true; });
    await new Promise(setImmediate);
    if (duringWait) releaseDownload(202);
    await new Promise(setImmediate);
    events.push({event: "onCreated", id: 101});
    onCreated.emit(old);
    onChanged.emit({id: 101, state: {current: "complete"}});
    await new Promise(setImmediate);
    assert.equal(settled, false);
    if (duringWait) {
      releaseSearch([{id: 202, state: "in_progress"}]);
      await new Promise(setImmediate);
      assert.equal(settled, false);
      complete = true;
      onChanged.emit({id: 202, state: {current: "complete"}});
    } else {
      events.push({event: "download_return", id: 202});
      releaseDownload(202);
    }
    await promise;
    assert.equal(returnedId, 202);
    cases.push({case: duringWait ? "unrelated_during_wait" : "unrelated_before_return",
      intended_id: 202, returned_id: returnedId, unrelated_ignored: true, events});
  }
  const result = {evidence: "node_mock", node: process.version, sources: config.sources, cases,
    limitation: "任意のイベント順を与えたモック。Chromeでの発生や頻度は示さない。"};
  fs.writeFileSync(path.join(root, "results/node_mock.json"), JSON.stringify(result, null, 2) + "\n", "utf8");
  console.log("Nodeモック2件: 別件ID 101を採用せず、対象ID 202だけで完了。実機とは別の証拠です。");
}

function setupBrowser() {
  const config = PROBE_CONFIG;
  const output = document.getElementById("log");
  const button = document.getElementById("start");
  const events = [];
  const cases = [];
  const created = new Map();
  const trackers = [];
  let currentCase = "setup";
  let sequence = 0;
  let runId = null;
  // 原文 saveJson が参照する変数と exportWith の診断出力。
  window.log = (message) => record("production_log", {message});
  const record = (event, data = {}) => {
    const row = {sequence: ++sequence, at: new Date().toISOString(),
      ms: performance.now(), case: currentCase, event, ...data};
    events.push(row);
    output.textContent += JSON.stringify(row) + "\n";
  };
  const itemFields = (item) => ({id: item.id, url: item.url, finalUrl: item.finalUrl,
    referrer: item.referrer, filename: item.filename, state: item.state, error: item.error,
    byExtensionId: item.byExtensionId, mime: item.mime, startTime: item.startTime, endTime: item.endTime});
  chrome.downloads.onCreated.addListener((item) => {
    created.set(item.id, {case: currentCase, ...itemFields(item)});
    record("onCreated", itemFields(item));
  });
  chrome.downloads.onChanged.addListener((delta) => record("onChanged", {delta}));
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "session" && changes.routing) record("routing", changes.routing);
  });
  const originalTrack = trackDownload;
  trackDownload = (timeoutMs, knownId = null) => {
    const tracker = originalTrack(timeoutMs, knownId);
    const observed = {case: currentCase, tracker: trackers.length + 1, known_id: knownId};
    trackers.push(observed);
    record("tracker_start", {tracker: observed.tracker, timeoutMs, known_id: knownId});
    tracker.promise.then((item) => {
      observed.returned = itemFields(item);
      record("tracker_complete", {tracker: observed.tracker, ...itemFields(item)});
    }, (error) => {
      observed.error = String(error.message);
      record("tracker_error", {tracker: observed.tracker, error: observed.error});
    });
    return tracker;
  };
  const pageDownload = async (tabId, url) => {
    record("request_download", {tabId, url});
    await chrome.scripting.executeScript({target: {tabId}, func: (href) => {
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.download = "";
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
    }, args: [url]});
  };
  const status = async () => (await fetch(config.origin + "/status")).json();
  const drain = async () => {
    const deadline = Date.now() + 25000;
    // 応答終了後に onCreated が届く余地も含め、安定した状態を続けて観測する。
    let stable = 0;
    while (Date.now() < deadline) {
      const server = await status();
      const items = await chrome.downloads.search({});
      const active = items.filter((item) => created.has(item.id) && item.state === "in_progress");
      if (server.pending === 0 && active.length === 0) stable++;
      else stable = 0;
      if (stable >= 4) return;
      await sleep(250);
    }
    throw new Error("予定応答/ダウンロードが終了せず、次のケースを停止しました");
  };
  const downloadUrl = (name, mode, origin = config.origin) =>
    `${origin}/download?case=${name}&mode=${mode}`;
  const withCase = async (name, operation) => {
    currentCase = name;
    const row = {case: name, started: new Date().toISOString()};
    cases.push(row);
    record("case_start");
    try {
      row.result = await operation();
    } catch (error) {
      row.error = String(error.message);
      record("case_error", {error: row.error});
    }
    await drain();
    row.downloads = (await chrome.downloads.search({})).filter((item) => created.get(item.id)?.case === name)
      .map(itemFields);
    row.trackers = trackers.filter((tracker) => tracker.case === name);
    row.requests = (await status()).requests.filter((request) => request.case.startsWith(name));
    record("case_end", {error: row.error || null});
    return row;
  };
  button.addEventListener("click", async () => {
    button.disabled = true;
    const stamp = "probe_" + Date.now();
    let tabs = [];
    let fatal = null;
    try {
      if (Object.keys(await chrome.storage.session.get("routing")).length) {
        throw new Error("routingが残っています。専用Chromeを終了して再準備してください");
      }
      tabs = await Promise.all([chrome.tabs.create({url: config.origin + "/page", active: false}),
        chrome.tabs.create({url: config.origin + "/page", active: false})]);
      for (const tab of tabs) {
        const deadline = Date.now() + 10000;
        while ((await chrome.tabs.get(tab.id)).status !== "complete") {
          if (Date.now() > deadline) throw new Error("合成ページの読み込みが終了しません");
          await sleep(100);
        }
      }
      const [target, same] = tabs;
      const normal = stamp + "_normal";
      const normalRow = await withCase(normal, async () => {
        const result = await exportWith(normal, () => pageDownload(target.id, downloadUrl(normal, "single")));
        runId = normal;
        window.runId = runId;
        await saveJson("normal.json", {probe_marker: "NORMAL_JSON"});
        return result;
      });
      const csv = normalRow.trackers[0]?.returned;
      const json = normalRow.trackers[1];
      if (normalRow.error || !csv?.url.includes(`case=${normal}`) || !json?.known_id ||
          json.known_id !== json.returned?.id || normalRow.downloads.some((item) => item.state !== "complete")) {
        throw new Error("正常単独取得のAPI観測が不成立です。競合ケースを停止します");
      }
      const overwrite = stamp + "_overwrite";
      const overwriteRow = await withCase(overwrite, async () => {
        runId = overwrite;
        window.runId = runId;
        await saveJson("overwrite.json", {probe_marker: "OVERWRITE_FIRST"});
        await saveJson("overwrite.json", {probe_marker: "OVERWRITE_SECOND"});
        return {expected_marker: "OVERWRITE_SECOND"};
      });
      if (overwriteRow.error) throw new Error("JSON上書きのAPI観測が不成立です。競合ケースを停止します");
      for (const [suffix, origin] of [["same", config.origin], ["foreign", config.otherOrigin]]) {
        const name = stamp + "_" + suffix;
        if (suffix === "foreign") {
          await chrome.tabs.update(same.id, {url: config.otherOrigin + "/page"});
          const deadline = Date.now() + 10000;
          while ((await chrome.tabs.get(same.id)).status !== "complete") {
            if (Date.now() > deadline) throw new Error("別originページの読み込みが終了しません");
            await sleep(100);
          }
        }
        await withCase(name, () => exportWith(name, async () => {
          // same はURLとreferrerも同じ。内容はサーバの要求順で異なる。
          await pageDownload(same.id, downloadUrl(name, suffix, origin));
          await sleep(100);
          await pageDownload(target.id, downloadUrl(name, suffix));
        }));
      }
      const late = stamp + "_late";
      await withCase(late, async () => {
        const first = await exportWith(late + "/first", () => pageDownload(target.id, downloadUrl(late, "late")),
          {retries: 1, retryAfterMs: 1000});
        record("next_export_start");
        const next = await exportWith(late + "/next", () => pageDownload(target.id, downloadUrl(late + "_next", "next")));
        return {first, next};
      });
      for (let attempt = 1; attempt <= 3; attempt++) {
        const name = stamp + "_json_" + attempt;
        await withCase(name, async () => {
          runId = name;
          window.runId = runId;
          await Promise.all([pageDownload(same.id, downloadUrl(name, "parallel")),
            saveJson("parallel.json", {probe_marker: "PARALLEL_JSON", attempt})]);
          return {attempt};
        });
      }
    } catch (error) {
      fatal = String(error.message);
      record("fatal", {error: fatal});
    } finally {
      const result = {evidence: "chrome_live", browser: navigator.userAgent,
        sources: config.sources, stamp, events, cases, fatal,
        note: "予定順の成立と実ファイルを照合して判定する。正常系の内容確認はinspectが必要。"};
      try {
        const response = await fetch(config.origin + "/record", {method: "POST",
          headers: {"Content-Type": "text/plain"}, body: JSON.stringify(result)});
        if (!response.ok) throw new Error("HTTP " + response.status);
        record("saved", {file: "results/chrome_live.json"});
        if (fatal) output.textContent += "\n記録完了。API観測の受入失敗。実ファイルはinspectで確認してください。\n";
        else output.textContent += "\n記録完了。実ファイル確認前のため受入未判定。inspectを実行してください。\n";
        const showAcceptance = async () => {
          try {
            const acceptance = await (await fetch(config.origin + "/acceptance")).json();
            if (acceptance.status === "pending") setTimeout(showAcceptance, 1000);
            else output.textContent += acceptance.status === "passed"
              ? "\n実ファイル確認: JSON受入成功。CSVの既知問題は残ります。\n"
              : "\n実ファイル確認: JSON受入失敗。results/acceptance.jsonを確認してください。\n";
          } catch (error) {
            output.textContent += "\n実ファイル受入結果の読取失敗: " + error.message + "\n";
          }
        };
        showAcceptance();
      } catch (error) {
        output.textContent += "\n記録失敗: " + error.message + "\n";
      }
      // 検証ページと合成ページは結果の確認用に残す。
    }
  }, {once: true});
}
