// claude.ai の読み込み開始時点でフラグメントを見て、起動の指示があればバックグラウンドへ渡す。
// SPA が / から別のパスへ遷移してフラグメントを落とす前に拾うため document_start で動かす。
(() => {
  const marker = "#seat-analyzer-run=";
  if (!location.hash.startsWith(marker)) return;
  const spec = location.hash.slice(marker.length);
  chrome.runtime.sendMessage({ type: "seat-analyzer-run", spec });
})();
