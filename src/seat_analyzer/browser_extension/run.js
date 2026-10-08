// 実行ページ。URL のフラグメントに載った実行内容（spec）に従って claude.ai の管理画面を操作し、
// 結果を staging（Chrome の既定のダウンロード先）の <run_id>/ へ書き出す。
//
//   取得      {run_id, mode: "current"|"previous", orgs: [{uuid, dir, kinds: ["members","spend","code"]}]}
//   組織一覧  {run_id, action: "list-orgs"}
//
// staging に書くもの（保存先の振り分けは background.js が routing に従って行う）:
//   <run_id>/<dir>/<kind_dir>/<元のファイル名>   ダウンロードした CSV
//   <run_id>/progress.json                       各手順の後に上書きする途中経過（status: "running"）
//   <run_id>/manifest.json                       最後に書く結果（コマンドはこれを待つ）
//   <run_id>/orgs.json                           組織一覧のとき
//
// claude.ai の上で押すのはエクスポート系のボタンと、支出レポートのダイアログの期間の選択・
// ダウンロードだけで、設定を変える操作は持たない。通信先は claude.ai だけ。

const ORIGIN = "https://claude.ai";
const KINDS = ["members", "spend", "code"];
const KIND_DIRS = { members: "members", spend: "spend", code: "code-analytics" };
const PAGES = {
  members: `${ORIGIN}/admin-settings/members`,
  spend: `${ORIGIN}/analytics/overview`,
  code: `${ORIGIN}/analytics/claude-code`,
};
const MODE_TEXT = { current: "当月", previous: "前月" };

// ボタンと選択肢の名前（日本語と英語を併記した正規表現）
const LABEL = {
  membersExport: "CSVをエクスポート|Export CSV",
  spendExport: "支出レポートをエクスポート|Export spend report",
  monthToDate: "^(?:月累計|Month to date)(?:\\s|$)",
  lastMonth: "^(?:先月|Last month)(?:\\s|$)",
  download: "^(?:ダウンロード|Download)$",
  codeExport: "エクスポート|Export",
};

// 待ち時間の上限（ミリ秒）
const WAIT = {
  pageLoad: 60000,       // 遷移の完了（status complete）
  human: 10 * 60000,     // ログイン・外部セキュリティ検証を人が済ませるまで
  orgConfirm: 15000,     // 組織の切替の確認
  button: 60000,         // ボタンが現れて有効になるまで
  download: 120000,      // ダウンロードの完了
  codeRetry: 8000,       // Claude Code のエクスポートを押し直すまで
  savedFile: 30000,      // progress.json・manifest.json・orgs.json の保存
};
// Claude Code のエクスポートを押し直す回数の上限
const CODE_RETRIES = 3;
// 操作の前に置く待ち（ミリ秒）。毎回この範囲の一様乱数で選び、取得の操作が一定の間隔で
// 並ばないようにする
const PAUSE = {
  settle: [2000, 4000],  // 遷移の完了から操作までの間
  click: [500, 1500],    // ボタンや選択肢を押す前
};

const logLines = [];
const logEl = document.getElementById("log");
const statusEl = document.getElementById("status");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// PAUSE の範囲 [最小, 最大] から待ち時間（ミリ秒）を選ぶ
const pauseMs = ([min, max]) => min + Math.floor(Math.random() * (max - min + 1));
const errorText = (e) => String(e && e.message ? e.message : e);
const basename = (path) => String(path || "").split(/[\\/]/).pop();

let runId = null;
let runMode = null;
const results = [];

function log(msg) {
  const line = `${new Date().toISOString().slice(11, 19)} ${msg}`;
  logLines.push(line);
  logEl.textContent += line + "\n";
  console.log(line);
}

function setStatus(text, attention = false) {
  statusEl.textContent = text;
  statusEl.classList.toggle("attention", attention);
}

// ---- 実行内容の検査（staging の外を指す名前や想定外の形を受け付けない） ----

function isPlainName(s) {
  return typeof s === "string" && /^[A-Za-z0-9._-]+$/.test(s) && s !== "." && s !== "..";
}

function isDir(s) {
  if (typeof s !== "string") return false;
  const parts = s.split("/");
  return parts.length <= 2 && parts.every((p) => p !== "" && p !== "." && p !== ".." && !/[\\:\x00]/.test(p));
}

function specProblem(spec) {
  if (!spec || typeof spec !== "object") return "not an object";
  if (!isPlainName(spec.run_id)) return "run_id";
  if (spec.action === "list-orgs") return null;
  if (spec.action !== undefined) return "action";
  if (!["current", "previous"].includes(spec.mode)) return "mode";
  if (!Array.isArray(spec.orgs) || spec.orgs.length === 0) return "orgs";
  for (const org of spec.orgs) {
    if (!org || typeof org.uuid !== "string" || !/^[0-9a-f-]{36}$/.test(org.uuid)) return "orgs[].uuid";
    if (!isDir(org.dir)) return "orgs[].dir";
    if (!Array.isArray(org.kinds) || org.kinds.length === 0 || !org.kinds.every((k) => KINDS.includes(k))) {
      return "orgs[].kinds";
    }
  }
  return null;
}

// 同じ run_id を 2 度実行しない（タブの再読み込みや、前回のタブが復元されたときに
// 取得をやり直さないため）
async function claimRun(id) {
  const { started = [] } = await chrome.storage.local.get("started");
  if (started.includes(id)) return false;
  started.push(id);
  await chrome.storage.local.set({ started: started.slice(-500) });
  return true;
}

// 同じブラウザで進行中の別の実行（chrome.storage.session の activeRun）。開始から
// 1 時間を超えた記録は、結果を保存できずに残ったものとみなして無視する
const ACTIVE_RUN_TTL_MS = 60 * 60 * 1000;

async function otherActiveRun(id) {
  const { activeRun } = await chrome.storage.session.get("activeRun");
  if (!activeRun || activeRun.run_id === id) return null;
  if (!(Date.now() - activeRun.started_at < ACTIVE_RUN_TTL_MS)) return null;
  return activeRun.run_id;
}

async function releaseActiveRun(id) {
  const { activeRun } = await chrome.storage.session.get("activeRun");
  if (activeRun && activeRun.run_id === id) await chrome.storage.session.remove("activeRun");
}

// ---- タブ内で実行する関数（直列化されて渡るので外側の変数を参照しない） ----

function pageState() {
  const t = document.title;
  if (document.querySelector("#challenge-running, #cf-chl-widget, iframe[src*='challenges.cloudflare.com']") || /moment|セキュリティ検証/.test(t)) {
    return { state: "challenge", title: t };
  }
  if (location.pathname.startsWith("/login") || location.pathname.startsWith("/magic-link")) {
    return { state: "login", title: t };
  }
  return { state: "app", title: t, path: location.pathname };
}

async function waitForButton(pattern, timeoutMs) {
  const re = new RegExp(pattern);
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const b = [...document.querySelectorAll("button, [role=button]")].find((e) => re.test(e.innerText.trim()));
    if (b) return true;
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

// ボタンが現れて有効になるまで待ってから押す（データの読み込み中は無効のことがある）
async function clickButton(pattern, scope, timeoutMs) {
  const re = new RegExp(pattern);
  const deadline = Date.now() + timeoutMs;
  let b = null;
  let seen = [];
  while (Date.now() < deadline) {
    const root = scope === "dialog" ? document.querySelector("[role=dialog]") : document;
    if (root) {
      const buttons = [...root.querySelectorAll("button, [role=button]")];
      seen = buttons.map((e) => e.innerText.trim()).filter(Boolean).slice(0, 20);
      b = buttons.find((e) => re.test(e.innerText.trim()));
      if (b && !b.disabled && b.getAttribute("aria-disabled") !== "true") break;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  if (!b) {
    const noDialog = scope === "dialog" && !document.querySelector("[role=dialog]");
    return { ok: false, reason: noDialog ? "dialog not shown" : `button not found (${pattern})`, seen };
  }
  const disabled = !!b.disabled || b.getAttribute("aria-disabled") === "true";
  if (disabled) return { ok: false, reason: `button stayed disabled (${pattern})` };
  b.click();
  return { ok: true, text: b.innerText.trim() };
}

// 支出レポートのダイアログで期間を選び、表示された日付範囲を返す。alwaysClick でなければ、
// 既に選ばれている選択肢は押さない
async function spendDialog(radioPattern, alwaysClick) {
  const pause = (ms) => new Promise((r) => setTimeout(r, ms));
  const deadline = Date.now() + 10000;
  let dlg = null;
  while (Date.now() < deadline && !(dlg = document.querySelector("[role=dialog]"))) {
    await pause(300);
  }
  if (!dlg) return { ok: false, reason: "spend report dialog not shown" };
  const re = new RegExp(radioPattern);
  const radios = [...dlg.querySelectorAll("[role=radio]")];
  const radio = radios.find((e) => re.test(e.innerText.trim()));
  if (!radio) return { ok: false, reason: "period option not found", options: radios.map((e) => e.innerText.trim()) };
  const isChecked = (e) => e.getAttribute("aria-checked") === "true" || e.getAttribute("data-state") === "checked";
  const wasChecked = isChecked(radio);
  if (alwaysClick || !wasChecked) {
    radio.click();
    await pause(800);
  }
  const m = dlg.innerText.match(/\d{4}-\d{2}-\d{2} to \d{4}-\d{2}-\d{2}/);
  return { ok: true, wasChecked, checked: isChecked(radio), range: m ? m[0] : null };
}

// Claude Code の表示月。clickPrev なら月送り（「<」）を 1 回押し、表示が変わったことを確かめる
async function codeMonth(clickPrev) {
  const pause = (ms) => new Promise((r) => setTimeout(r, ms));
  const labelRe = /^\d{4}年\d{1,2}月$|^[A-Z][a-z]+ \d{4}$/;
  const find = () => [...document.querySelectorAll("button")].find((b) => labelRe.test(b.innerText.trim()));
  let deadline = Date.now() + 60000;
  let label = null;
  while (Date.now() < deadline && !(label = find())) {
    await pause(500);
  }
  if (!label) return { ok: false, reason: "month label not found" };
  const before = label.innerText.trim();
  if (!clickPrev) return { ok: true, label: before };
  // 月送りのボタンは表示月のボタンと同じグループの先頭
  const group = label.parentElement && label.parentElement.parentElement;
  const prev = group ? group.querySelector("button") : null;
  if (!prev || prev === label) return { ok: false, reason: "previous-month button not found", before };
  prev.click();
  deadline = Date.now() + 10000;
  let after = before;
  while (Date.now() < deadline) {
    await pause(500);
    const now = find();
    after = now ? now.innerText.trim() : null;
    if (after && after !== before) return { ok: true, before, label: after };
  }
  return { ok: false, reason: "month label did not change", before, label: after };
}

// Claude Code のページは表の描画に数秒かかる。行の数が 3 秒変わらなくなるまで待つ
async function waitForCodeData() {
  const deadline = Date.now() + 60000;
  let last = -1;
  let stableSince = 0;
  while (Date.now() < deadline) {
    const rows = document.querySelectorAll("table tbody tr").length;
    if (rows > 0 && rows === last) {
      if (Date.now() - stableSince >= 3000) return { ok: true, rows };
    } else {
      last = rows;
      stableSince = Date.now();
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  return { ok: last > 0, rows: last, note: "row count still changing at 60s" };
}

// 参加している組織の一覧（読み取りだけ）
async function fetchOrganizations() {
  try {
    const res = await fetch("/api/organizations", { credentials: "include" });
    if (!res.ok) return { ok: false, reason: `HTTP ${res.status}` };
    const data = await res.json();
    if (!Array.isArray(data)) return { ok: false, reason: "unexpected response" };
    const pick = (o, key) => (o && o[key] !== undefined ? o[key] : null);
    return {
      ok: true,
      orgs: data.map((o) => ({
        uuid: pick(o, "uuid"),
        name: pick(o, "name"),
        rate_limit_tier: pick(o, "rate_limit_tier"),
        plan: pick(o, "plan_display_name"),
      })),
    };
  } catch (e) {
    return { ok: false, reason: String(e && e.message ? e.message : e) };
  }
}

// ---- 実行ページ側のヘルパ ----

async function exec(tabId, func, args = []) {
  const res = await chrome.scripting.executeScript({ target: { tabId }, func, args });
  return res && res[0] ? res[0].result : undefined;
}

// 遷移の途中などで実行できなければ undefined（待ち合わせの見回りに使う）
async function tryExec(tabId, func, args = []) {
  try {
    return await exec(tabId, func, args);
  } catch (e) {
    return undefined;
  }
}

// 組織の切替。claude.ai は Cookie の lastActiveOrg で表示する組織を決める
async function setOrgCookie(uuid) {
  const existing = await chrome.cookies.getAll({ domain: "claude.ai", name: "lastActiveOrg" });
  for (const c of existing) {
    await chrome.cookies.remove({ url: "https://claude.ai" + c.path, name: c.name });
  }
  await chrome.cookies.set({
    url: "https://claude.ai/",
    name: "lastActiveOrg",
    value: uuid,
    domain: ".claude.ai",
    path: "/",
    secure: true,
    sameSite: "lax",
    expirationDate: Math.floor(Date.now() / 1000) + 365 * 86400,
  });
}

// タブごとに、ページが呼んだ組織の API の UUID を時系列で記録する（前のページの残りの
// 通信が混ざるため、順序で判定する）
const seenOrgs = new Map();
chrome.webRequest.onBeforeRequest.addListener(
  (d) => {
    const m = d.url.match(/\/api\/organizations\/([0-9a-f-]{36})/);
    if (m) {
      if (!seenOrgs.has(d.tabId)) seenOrgs.set(d.tabId, []);
      seenOrgs.get(d.tabId).push(m[1]);
    }
  },
  { urls: ["https://claude.ai/api/organizations/*"] },
);

// 直近 3 件（3 件に満たなければ観測した全件）の API 呼び出しが対象の組織に揃うまで待つ
async function confirmActiveOrg(tabId, uuid) {
  const deadline = Date.now() + WAIT.orgConfirm;
  while (Date.now() < deadline) {
    const seq = seenOrgs.get(tabId) || [];
    const tail = seq.slice(-3);
    if (tail.length >= 1 && tail.every((u) => u === uuid)) return { ok: true, seen: [...new Set(seq)] };
    await sleep(1000);
  }
  return { ok: false, seen: [...new Set(seenOrgs.get(tabId) || [])] };
}

async function navigate(tabId, url) {
  seenOrgs.delete(tabId);
  const loaded = new Promise((resolve) => {
    const timer = setTimeout(() => { chrome.tabs.onUpdated.removeListener(listener); resolve(); }, WAIT.pageLoad);
    const listener = (id, info) => {
      if (id === tabId && info.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
  });
  await chrome.tabs.update(tabId, { url });
  await loaded;
  const wait = pauseMs(PAUSE.settle);
  log(`  opened ${new URL(url).pathname}; waiting ${(wait / 1000).toFixed(1)}s`);
  await sleep(wait);
}

// ボタンや選択肢を押す前の短い待ち。タブ内でボタンを探して押す処理を呼ぶ手前に置く
// （探してから押すまでの間は空けない）
async function pauseBeforeClick() {
  await sleep(pauseMs(PAUSE.click));
}

// ログインと外部セキュリティ検証は人に任せ、管理画面の状態になるまで待つ（自動化・回避はしない）
async function ensureApp(tabId, url) {
  const deadline = Date.now() + WAIT.human;
  const before = statusEl.textContent;
  let notified = false;
  while (Date.now() < deadline) {
    const st = await tryExec(tabId, pageState);
    if (st && st.state === "app") {
      if (notified) {
        log("human action done");
        setStatus(before);
      }
      return st;
    }
    if (!notified && st) {
      log(`human action needed: ${st.state} (${st.title})`);
      setStatus("要操作: ブラウザで操作してください（ログイン・セキュリティ検証）", true);
      notified = true;
    }
    await sleep(3000);
    if (st && st.state === "login") {
      // ログインが済んだら目的のページへ戻す
      const now = await tryExec(tabId, pageState);
      if (now && now.state === "app" && !now.path.startsWith(new URL(url).pathname)) await navigate(tabId, url);
    }
  }
  throw new Error("sign-in or security verification not completed within 10 minutes");
}

async function openPage(tabId, url) {
  await navigate(tabId, url);
  await ensureApp(tabId, url);
}

// ダウンロード 1 件の完了を待つ。page から始まるダウンロードは、待ち始めてから最初に
// 作られた 1 件を採る。拡張機能が自分で始めたものは adopt で id を渡す
function trackDownload(timeoutMs) {
  let id = null;
  let done = false;
  let resolveFn;
  let rejectFn;
  const promise = new Promise((resolve, reject) => { resolveFn = resolve; rejectFn = reject; });
  const cleanup = () => {
    clearTimeout(timer);
    chrome.downloads.onCreated.removeListener(onCreated);
    chrome.downloads.onChanged.removeListener(onChanged);
  };
  const finish = (fn, value) => {
    if (done) return;
    done = true;
    cleanup();
    fn(value);
  };
  const check = async () => {
    if (id === null || done) return;
    const [item] = await chrome.downloads.search({ id });
    if (!item) return;
    if (item.state === "complete") finish(resolveFn, item);
    else if (item.state === "interrupted") finish(rejectFn, new Error(`download interrupted: ${item.error || ""}`));
  };
  const onCreated = (item) => {
    if (id === null) {
      id = item.id;
      check();
    }
  };
  const onChanged = (delta) => {
    if (delta.id === id && delta.state) check();
  };
  const timer = setTimeout(() => {
    const what = id === null ? "download did not start" : `download did not finish within ${timeoutMs / 1000}s`;
    finish(rejectFn, new Error(what));
  }, timeoutMs);
  chrome.downloads.onCreated.addListener(onCreated);
  chrome.downloads.onChanged.addListener(onChanged);
  promise.catch(() => {});
  return {
    promise,
    created: () => id !== null,
    adopt: (knownId) => { id = knownId; check(); },
    dispose: () => finish(rejectFn, new Error("stopped")),
  };
}

async function waitUntil(predicate, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline && !predicate()) await sleep(250);
}

// ページの操作の結果を確かめる（失敗なら理由を投げる）
function expectOk(outcome, what) {
  if (outcome && outcome.ok !== false) return outcome;
  log(`  ${what}: ${JSON.stringify(outcome)}`);
  throw new Error(outcome && outcome.reason ? outcome.reason : `${what} failed`);
}

// routing を置いてからページのダウンロードを起こし、保存されたファイル名を返す。
// retries を渡すと、retryAfterMs の間にダウンロードが始まらなければ押し直す
async function exportWith(prefix, trigger, { retries = 0, retryAfterMs = 0 } = {}) {
  await chrome.storage.session.set({ routing: { prefix } });
  const tracker = trackDownload(WAIT.download);
  let reclicked = false;
  try {
    await trigger();
    for (let i = 1; i <= retries && !tracker.created(); i++) {
      await waitUntil(tracker.created, retryAfterMs);
      if (tracker.created()) break;
      log(`  no download after ${retryAfterMs / 1000}s; clicking again (${i}/${retries})`);
      reclicked = true;
      await trigger();
    }
    const item = await tracker.promise;
    return { filename: basename(item.filename) };
  } finally {
    tracker.dispose();
    // 押し直したときは遅れて始まったダウンロードも同じ場所へ入るよう、少し待ってから外す
    if (reclicked) await sleep(3000);
    await chrome.storage.session.remove("routing");
  }
}

// 拡張機能が作った JSON を <run_id>/<name> に保存し、保存が終わるまで待つ
async function saveJson(name, data) {
  const blob = new Blob([JSON.stringify(data, null, 2) + "\n"], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  // blob の既定のファイル名は UUID になるので、routing の name で固定する
  await chrome.storage.session.set({ routing: { prefix: runId, name } });
  const tracker = trackDownload(WAIT.savedFile);
  try {
    const id = await chrome.downloads.download({ url, conflictAction: "overwrite", saveAs: false });
    tracker.adopt(id);
    await tracker.promise;
  } finally {
    tracker.dispose();
    await chrome.storage.session.remove("routing");
    URL.revokeObjectURL(url);
  }
}

async function saveProgress() {
  try {
    await saveJson("progress.json", {
      run_id: runId,
      mode: runMode,
      status: "running",
      updated_at: new Date().toISOString(),
      results,
      log: logLines,
    });
  } catch (e) {
    log(`progress.json not saved: ${errorText(e)}`);
  }
}

// ---- 種別ごとのエクスポート ----

function prefixFor(org, kind) {
  return `${runId}/${org.dir}/${KIND_DIRS[kind]}`;
}

// 開いたページが対象の組織を表示していることを確かめる（揃わなければ投げる）。組織の
// 切替の直後だけでなく、ページを開くたびに確かめる（ログインし直すと表示する組織が
// 変わることがあるため）
async function ensureOrg(tabId, org, where) {
  const active = await confirmActiveOrg(tabId, org.uuid);
  log(`[${org.dir}] ${where} organizations seen: ${active.seen.map((u) => u.slice(0, 8)).join(",") || "none"}`);
  if (!active.ok) throw new Error("organization switch not confirmed");
}

async function switchOrg(tabId, org) {
  await setOrgCookie(org.uuid);
  await openPage(tabId, PAGES.members);
  await ensureOrg(tabId, org, "members");
}

// メンバー一覧（組織の切替でメンバー一覧のページにいる）
async function exportMembers(tabId, org) {
  return exportWith(prefixFor(org, "members"), async () => {
    await pauseBeforeClick();
    expectOk(await exec(tabId, clickButton, [LABEL.membersExport, null, WAIT.button]), "export button");
  });
}

// 支出レポート。当月は「月累計」、前月は「先月」を選び、期間が月の 1 日から始まることを
// 確かめてからダウンロードする
async function exportSpend(tabId, org) {
  await openPage(tabId, PAGES.spend);
  await ensureOrg(tabId, org, "spend");
  const present = await exec(tabId, waitForButton, [LABEL.spendExport, WAIT.button]);
  if (!present) throw new Error("spend report unavailable");
  const previous = runMode === "previous";
  let range = null;
  const saved = await exportWith(prefixFor(org, "spend"), async () => {
    await pauseBeforeClick();
    expectOk(await exec(tabId, clickButton, [LABEL.spendExport, null, WAIT.button]), "export button");
    await pauseBeforeClick();
    const dialog = expectOk(
      await exec(tabId, spendDialog, [previous ? LABEL.lastMonth : LABEL.monthToDate, previous]),
      "period option",
    );
    log(`[${org.dir}] spend dialog ${JSON.stringify(dialog)}`);
    range = dialog.range;
    if (!range) throw new Error("date range not shown in the dialog");
    if (!/^\d{4}-\d{2}-01 to /.test(range)) throw new Error(`date range does not start on the 1st: ${range}`);
    await pauseBeforeClick();
    expectOk(await exec(tabId, clickButton, [LABEL.download, "dialog", WAIT.button]), "download button");
  });
  return { ...saved, range };
}

// Claude Code analytics。当月は表示中の月、前月は月送りを 1 回押した月
async function exportCode(tabId, org) {
  await openPage(tabId, PAGES.code);
  await ensureOrg(tabId, org, "code");
  const previous = runMode === "previous";
  // 前月は月送りのボタンを押す
  if (previous) await pauseBeforeClick();
  const month = expectOk(await exec(tabId, codeMonth, [previous]), "month control");
  const ready = await exec(tabId, waitForCodeData);
  log(`[${org.dir}] code month ${JSON.stringify(month)} data ${JSON.stringify(ready)}`);
  return exportWith(prefixFor(org, "code"), async () => {
    await pauseBeforeClick();
    expectOk(await exec(tabId, clickButton, [LABEL.codeExport, null, WAIT.button]), "export button");
  }, { retries: CODE_RETRIES, retryAfterMs: WAIT.codeRetry });
}

const EXPORTERS = { members: exportMembers, spend: exportSpend, code: exportCode };

// ---- 本体 ----

async function runExport(spec, tabId) {
  const total = spec.orgs.reduce((n, org) => n + org.kinds.length, 0);
  const head = `${runId}（${MODE_TEXT[runMode]}）`;
  for (const org of spec.orgs) {
    const kinds = KINDS.filter((kind) => org.kinds.includes(kind));
    setStatus(`${head}: ${org.dir} の組織に切り替えています`);
    try {
      await switchOrg(tabId, org);
      log(`[${org.dir}] organization switched`);
    } catch (e) {
      // 切替を確かめられない組織は丸ごと飛ばす（別の組織のデータを保存しないため）
      const reason = errorText(e);
      log(`[${org.dir}] skipped: ${reason}`);
      for (const kind of kinds) results.push({ dir: org.dir, kind, ok: false, reason });
      await saveProgress();
      continue;
    }
    // 種別ごとに独立して行う（1 つの失敗で残りを飛ばさない）
    for (const kind of kinds) {
      setStatus(`${head}: ${org.dir} ${kind}（${results.length + 1}/${total}）`);
      try {
        const saved = await EXPORTERS[kind](tabId, org);
        results.push({ dir: org.dir, kind, ok: true, ...saved });
        log(`[${org.dir}] ${kind}: ok ${saved.filename}`);
      } catch (e) {
        const reason = errorText(e);
        results.push({ dir: org.dir, kind, ok: false, reason });
        log(`[${org.dir}] ${kind}: failed ${reason}`);
      }
      await saveProgress();
    }
  }
  setStatus(`${head}: 結果を書き出しています`);
  await saveJson("manifest.json", {
    run_id: runId,
    mode: runMode,
    finished_at: new Date().toISOString(),
    results,
    log: logLines,
  });
  const failed = results.filter((r) => !r.ok).length;
  setStatus(`${head}: 完了（成功 ${results.length - failed} 件・失敗 ${failed} 件）`);
}

async function runListOrgs(tabId) {
  setStatus(`${runId}: 組織の一覧を取得しています`);
  let data;
  try {
    await openPage(tabId, `${ORIGIN}/`);
    const res = await exec(tabId, fetchOrganizations);
    if (res && res.ok) {
      data = res.orgs;
      log(`organizations: ${data.length}`);
    } else {
      data = { error: res && res.reason ? res.reason : "organizations not fetched" };
    }
  } catch (e) {
    data = { error: errorText(e) };
  }
  if (!Array.isArray(data)) log(`list-orgs failed: ${data.error}`);
  await saveJson("orgs.json", data);
  setStatus(`${runId}: 完了`);
}

async function main() {
  // バックグラウンドの診断ログを先に表示する（起動の経路の切り分け用）
  const { diag = [] } = await chrome.storage.local.get("diag");
  for (const d of diag) log(`[diag] ${d}`);
  await chrome.storage.local.remove("diag");
  if (window.top !== window) {
    setStatus("埋め込まれた状態では実行しません");
    return;
  }
  if (!location.hash) {
    setStatus("実行内容がありません（診断ログの表示のみ）");
    return;
  }
  let spec;
  try {
    spec = JSON.parse(decodeURIComponent(location.hash.slice(1)));
  } catch (e) {
    setStatus("実行内容を読めません", true);
    log(`spec not readable: ${errorText(e)}`);
    return;
  }
  const problem = specProblem(spec);
  if (problem) {
    setStatus(`実行内容が不正です（${problem}）`, true);
    return;
  }
  const other = await otherActiveRun(spec.run_id);
  if (other) {
    setStatus(`別の実行（${other}）が進行中です`, true);
    return;
  }
  if (!(await claimRun(spec.run_id))) {
    setStatus(`${spec.run_id} は開始済みです（再読み込みでは実行しません）`);
    return;
  }
  runId = spec.run_id;
  runMode = spec.mode || null;
  await chrome.storage.session.set({ activeRun: { run_id: runId, started_at: Date.now() } });
  log(spec.action === "list-orgs" ? `run ${runId} list-orgs` : `run ${runId} mode=${runMode} orgs=${spec.orgs.length}`);
  const tab = await chrome.tabs.create({ url: "about:blank", active: true });
  if (spec.action === "list-orgs") await runListOrgs(tab.id);
  else await runExport(spec, tab.id);
  // manifest.json・orgs.json を保存し終えてから外す
  await releaseActiveRun(runId);
}

main().catch(async (e) => {
  log("FATAL " + (e && e.stack ? e.stack : e));
  setStatus("失敗（ログを確認してください）", true);
  if (runId) await releaseActiveRun(runId).catch(() => {});
});
