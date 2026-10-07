// バックグラウンド（service worker）。起動の受け口と、ダウンロードの保存先の振り分けを受け持つ。

// 診断ログ（storage.local の diag に直近 50 件を残す。実行ページが起動時に表示する）
async function diag(msg) {
  try {
    const { diag = [] } = await chrome.storage.local.get("diag");
    diag.push(`${new Date().toISOString().slice(11, 19)} ${msg}`);
    await chrome.storage.local.set({ diag: diag.slice(-50) });
  } catch (e) {
    // 診断ログの失敗で起動を止めない
  }
}
diag("service worker started");

// 1. 起動の受け口。CLI は https://claude.ai/#seat-analyzer-run=<spec> を開くだけで、
//    そのタブを実行ページ（run.html#<spec>）へ遷移させる。chrome-extension:// の URL は
//    コマンドラインから直接開けないため、claude.ai の URL を経由する。
const handled = new Set();
function startRun(tabId, spec, via) {
  // 同じタブを 2 つの経路から受け取っても 1 回だけ起動する
  if (handled.has(tabId)) return;
  handled.add(tabId);
  diag(`startRun via ${via} tab=${tabId} spec=${spec.length}chars`);
  chrome.tabs.update(tabId, { url: chrome.runtime.getURL("run.html") + "#" + spec })
    .catch((e) => diag(`tabs.update failed: ${e && e.message}`));
}

// 経路 A: trigger.js（document_start のコンテンツスクリプト）からの通知
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg && msg.type === "seat-analyzer-run" && typeof msg.spec === "string") {
    diag(`message from tab=${sender.tab ? sender.tab.id : "none"}`);
    if (sender.tab) startRun(sender.tab.id, msg.spec, "message");
  }
});

// 経路 B: タブの URL の更新（A が届かない場合の保険）。SPA がパスを変えてもフラグメントは
// 残るので、パスは問わない
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  const url = info.url || (tab && tab.url) || "";
  const m = url.match(/^https:\/\/claude\.ai\/[^#]*#seat-analyzer-run=(.+)$/);
  if (m) startRun(tabId, m[1], "onUpdated");
  else if (url.includes("seat-analyzer-run")) diag(`onUpdated unmatched url=${url.slice(0, 80)}`);
});

// 2. ダウンロードの保存先の振り分け。実行ページが chrome.storage.session の routing に
//    {prefix, name} を置いている間だけ、既定のダウンロード先（staging）の下の
//    prefix/（name か、サイトが付けた元のファイル名）に保存する。同名は上書きする。
chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  chrome.storage.session.get("routing").then(({ routing }) => {
    if (routing && routing.prefix) {
      suggest({ filename: `${routing.prefix}/${routing.name || item.filename}`, conflictAction: "overwrite" });
    } else {
      suggest();
    }
  }).catch(() => suggest());
  return true;
});
