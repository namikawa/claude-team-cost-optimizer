
/* ダッシュボードの対話部分（タブ・テーマ・列ソート・検索・折りたたみ・高さ変更）。

   共有先の閲覧環境をこちらで選べないため、JS が無効でも内容が読めることを前提に置く。
   タブの出し分けと対話用の UI は、この直下で付ける html.js の下でしか効かない。
   行の折りたたみもここで行い、サーバ側は常に全行を書き出す（サーバ側で行を削ると
   JS が動かない環境で内容そのものが欠ける）。操作できる部品のうち、データから
   作る必要があるもの（判定フィルタの選択肢）だけをサーバ側が描き、それ以外の
   ツールバー・グラブバー・展開ボタンはここで作る。

   CSV 由来の値はここへ渡さない。データはサーバ側で描画済みで、ここは DOM の
   テキストと属性を読んで並べ替え・出し入れをするだけ（数値は計算し直さない）。 */
(function () {
  "use strict";

  var root = document.documentElement;
  var THEME_KEY = "seatdash-theme";
  var theme = "auto";

  /* 初期表示の行数・本数と、スクロール領域の最小高さ（デザイン仕様の数値）。 */
  var TABLE_ROWS = 15;
  var BAR_ROWS = 20;
  var MIN_BOX_H = 180;

  /* 組み立てた一覧。タブを切り替えたあとにグラブバーの要否を測り直すために持つ */
  var lists = [];

  function each(collection, fn) {
    for (var i = 0; i < collection.length; i += 1) {
      fn(collection[i], i);
    }
  }

  function toArray(collection) {
    var out = [];
    each(collection, function (item) { out.push(item); });
    return out;
  }

  /* ---------- テーマ ---------- */

  /* テーマは属性の付け外しで表す。Auto は属性を外して OS 設定（メディアクエリ）に委ねる */
  function applyTheme(mode) {
    if (mode === "light" || mode === "dark") {
      root.setAttribute("data-theme", mode);
    } else {
      root.removeAttribute("data-theme");
    }
  }

  function readTheme() {
    try {
      return window.localStorage.getItem(THEME_KEY);
    } catch (e) {
      return null;                     /* プライベートモード等で読めないだけ。既定に倒す */
    }
  }

  function saveTheme(mode) {
    try {
      window.localStorage.setItem(THEME_KEY, mode);
    } catch (e) {
      /* 保存できなくても表示は成立する（次に開いたとき既定へ戻るだけ） */
    }
  }

  /* 属性の反映は描画前に済ませる（本文の描画後に切り替えると色が一瞬入れ替わる）。
     ボタンの選択表示は DOM ができてから当てる */
  root.className = root.className ? root.className + " js" : "js";
  var saved = readTheme();
  theme = (saved === "light" || saved === "dark") ? saved : "auto";
  applyTheme(theme);

  function markTheme() {
    each(document.querySelectorAll("[data-theme-set]"), function (button) {
      var on = button.getAttribute("data-theme-set") === theme;
      if (on) {
        button.classList.add("is-active");
      } else {
        button.classList.remove("is-active");
      }
      button.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function setTheme(mode) {
    theme = mode;
    applyTheme(mode);
    markTheme();
    saveTheme(mode);
  }

  /* ---------- タブ ---------- */

  /* タブは単純な表示切替。スクロール位置には触れない（切替後もその位置を保つ） */
  function showTab(name) {
    each(document.querySelectorAll(".tabpanel"), function (panel) {
      var on = panel.getAttribute("data-tab") === name;
      if (on) {
        panel.classList.add("is-active");
      } else {
        panel.classList.remove("is-active");
      }
    });
    each(document.querySelectorAll(".tab"), function (tab) {
      var on = tab.getAttribute("data-tab") === name;
      if (on) {
        tab.classList.add("is-active");
      } else {
        tab.classList.remove("is-active");
      }
      tab.setAttribute("aria-selected", on ? "true" : "false");
    });
    /* 隠れている間は高さが測れない（display: none の中は 0 になる）ので、
       パネルを切り替えた「あと」に測り直す。前に測ると隠れる直前・隠れたままの
       寸法を掴み、初期表示以外のタブでグラブバーが出ないままになる */
    refreshGrabs();
  }

  /* ---------- 表示文字列の数値化（ソートの比較キー） ---------- */

  var UNITS = { B: 1e9, M: 1e6, K: 1e3 };
  var NUMBER_RE = /-?\d+(?:\.\d+)?[BMK]?/;

  /* 画面に出ている文字列をそのまま解釈する（$1.2K → 1200）。書式を決めているのは
     サーバ側で、ここは並べ替えのための比較キーを作るだけなので値を計算し直さない。
     セルには金額のほかに ⚠ のような印が付くため、装飾を落としてから最初の数値を拾う。
     解釈できない値（—・空）は最小に倒す。 */
  function toNumber(text) {
    var found = NUMBER_RE.exec(text.replace(/[\s,$+%]/g, ""));
    if (!found) {
      return -Infinity;
    }
    var token = found[0];
    var unit = UNITS[token.charAt(token.length - 1)];
    return unit ? parseFloat(token.slice(0, -1)) * unit : parseFloat(token);
  }

  function textOf(node) {
    return node ? (node.textContent || "") : "";
  }

  function sortKey(row, index, numeric) {
    var text = textOf(row.cells ? row.cells[index] : null);
    return numeric ? toNumber(text) : text.replace(/\s+/g, " ").trim().toLowerCase();
  }

  function compare(a, b) {
    if (a === b) {
      return 0;
    }
    return a < b ? -1 : 1;
  }

  /* ---------- 一覧（表の行 / 棒）の絞り込み・折りたたみ ---------- */

  /* 検索の突合先は、表示名（ローカル部）と title のメールアドレスの両方。
     どちらもサーバが描画済みの DOM から読む。 */
  function searchText(row) {
    var cell = row.querySelector(".user, .name");
    if (!cell) {
      return "";
    }
    return (textOf(cell) + " " + (cell.getAttribute("title") || "")).toLowerCase();
  }

  function statusText(row) {
    return textOf(row.querySelector(".judge .badge")).trim();
  }

  function show(element, on) {
    if (on) {
      element.classList.remove("is-out");
    } else {
      element.classList.add("is-out");
    }
  }

  /* 絞り込みの結果と折りたたみを一度に当てる。折りたたみは「絞り込みに残った行の
     先頭から数えて上限まで」なので、検索・フィルタ・並べ替えのどれが動いても
     ここを通す。 */
  function apply(list) {
    var matched = 0;
    each(list.rows, function (row) {
      var ok = true;
      if (list.query && row._search.indexOf(list.query) < 0) {
        ok = false;
      }
      if (ok && list.status && row._status !== list.status) {
        ok = false;
      }
      if (ok) {
        matched += 1;
      }
      show(row, ok && (list.expanded || matched <= list.limit));
    });
    var rest = list.expanded ? 0 : Math.max(0, matched - list.limit);
    if (list.moreBox) {
      list.moreCount.textContent = String(rest);
      /* 隠れている行が無ければボタンごと消す（絞り込みで上限を下回る場合も含む） */
      if (rest > 0) {
        list.moreBox.classList.add("is-on");
      } else {
        list.moreBox.classList.remove("is-on");
      }
    }
    if (list.count) {
      var filtering = Boolean(list.query || list.status);
      list.count.textContent = filtering
        ? matched + " / " + list.rows.length + " " + list.unit + "（絞り込み中）"
        : list.rows.length + " " + list.unit;
    }
    /* 行の出し入れで内容の高さが変わる＝グラブバーで動かせるかどうかも変わる。
       検索・フィルタ・展開はすべてここを通るので、判定の更新もここに1つ置く */
    updateGrab(list);
  }

  /* ---------- 列ソート ---------- */

  function markSort(list) {
    each(list.marks, function (mark, index) {
      var on = index === list.sortCol;
      mark.indicator.textContent = on ? (list.sortDir > 0 ? "↑" : "↓") : "";
      mark.th.setAttribute(
        "aria-sort", on ? (list.sortDir > 0 ? "ascending" : "descending") : "none");
    });
  }

  function sortRows(list, index, dir) {
    var numeric = list.numeric[index];
    var keyed = [];
    each(list.rows, function (row, at) {
      keyed.push({ row: row, at: at, key: sortKey(row, index, numeric) });
    });
    /* 同値の行はもとの並びを保つ（安定ソート）。向きを掛けるのはキーの比較だけで、
       同値のときの比較には掛けない */
    keyed.sort(function (a, b) {
      var order = compare(a.key, b.key);
      return order === 0 ? a.at - b.at : dir * order;
    });
    var rows = [];
    each(keyed, function (item) {
      list.body.appendChild(item.row);
      rows.push(item.row);
    });
    list.rows = rows;
  }

  function addSort(list) {
    var head = list.table.tHead;
    if (!head || head.rows.length === 0) {
      return;
    }
    each(head.rows[head.rows.length - 1].cells, function (th, index) {
      /* 数値列の印は表側がすでに持っている（金額・件数の右寄せに使う .num）。
         初回の向きは数値列が降順・文字列列が昇順 */
      list.numeric.push(th.classList.contains("num"));
      var indicator = document.createElement("span");
      indicator.className = "sort-ind";
      th.appendChild(indicator);
      th.classList.add("sortable");
      th.setAttribute("aria-sort", "none");
      list.marks.push({ th: th, indicator: indicator });
      th.addEventListener("click", function () {
        list.sortDir = (list.sortCol === index) ? -list.sortDir
          : (list.numeric[index] ? -1 : 1);
        list.sortCol = index;
        sortRows(list, index, list.sortDir);
        markSort(list);
        apply(list);          /* 並びが変わると折りたたみで残る行の顔ぶれも変わる */
      });
    });
  }

  /* ---------- スクロール領域の高さ変更 ---------- */

  /* 掴んで動かせる範囲は [MIN_BOX_H, 内容の高さ] なので、内容が最小高さ以下の表は
     どこへ動かしても表示が変わらない（max-height は内容を引き伸ばさない）。
     そういう表ではバーを出さない ── ポインタ操作を扱えない環境でバーを作らないのと
     同じ「操作できない部品を出さない」規則。

     測れるのは表示中のタブだけ（display: none の中では 0 になる）なので、測り直す
     契機を2つ持つ: 行数が変わったとき（apply）と、タブが表に出たとき（showTab）。 */
  function updateGrab(list) {
    if (!list.grab) {
      return;
    }
    if (list.box.scrollHeight > MIN_BOX_H) {
      list.grab.classList.add("is-on");
    } else {
      list.grab.classList.remove("is-on");
    }
  }

  function refreshGrabs() {
    each(lists, updateGrab);
  }

  /* 測るだけとはいえリサイズは連続で飛んでくるので、この間隔に1回へまとめる。

     requestAnimationFrame ではなくタイマーで待つ。フレームは描画が止まっている間
     （背景タブなど）配信されないことがあり、まとめ待ちの印を立てたまま次のフレームが
     来ないと、以後の測り直しが全部落ちる。タイマーなら必ず動く。 */
  var REFRESH_MS = 120;
  var refreshTimer = 0;

  function scheduleRefresh() {
    if (refreshTimer) {
      return;                            /* 待っている間に来た分は、この1回にまとまる */
    }
    refreshTimer = window.setTimeout(function () {
      refreshTimer = 0;
      refreshGrabs();
    }, REFRESH_MS);
  }

  /* 幅が変わるとセルの折り返しが変わり、内容の高さが最小高さの境界を跨ぐことがある
     （ウィンドウのリサイズ・ズーム・端末の回転はどれもこの経路を通る）。測り直さないと、
     狭めたときに必要になったバーが出ず、広げたときに動かせないバーが残る。

     窓の寸法変化はどの環境でも拾えるので、まずそれを土台に置く。そのうえで、箱そのものの
     寸法を見られる環境では ResizeObserver も併せて使う（窓の大きさは変わらないまま
     折り返しだけが変わる場合 ── 拡大縮小・書体の読み込み・別の要素の増減 ── を拾うため）。
     ResizeObserver の側だけに寄せると、非対応の環境で監視が丸ごと落ちる。 */
  function watchSize() {
    window.addEventListener("resize", scheduleRefresh);
    if (window.ResizeObserver) {
      var observer = new ResizeObserver(scheduleRefresh);
      each(lists, function (list) {
        if (list.box) {
          observer.observe(list.box);
        }
      });
    }
  }

  /* ブラウザ標準の resize コーナーは使わず（ダークテーマで見えないため）、
     スクロール領域の直下にグラブバーを置いて縦だけを変える。ポインタ操作を
     扱えない環境ではバーそのものを作らない（押せない部品を出さないため）。 */
  function addGrab(list) {
    if (!window.PointerEvent) {
      return null;
    }
    var box = list.box;
    var grab = document.createElement("div");
    grab.className = "grab";
    grab.setAttribute("aria-hidden", "true");
    grab.appendChild(document.createElement("span"));
    grab.appendChild(document.createElement("span"));
    box.parentNode.insertBefore(grab, box.nextSibling);
    list.grab = grab;

    var dragging = false;
    var startY = 0;
    var startH = 0;
    var contentH = 0;

    grab.addEventListener("pointerdown", function (ev) {
      dragging = true;
      startY = ev.clientY;
      startH = box.clientHeight;
      contentH = box.scrollHeight;      /* 中身より高くしても余白が増えるだけなので上限に使う */
      document.body.classList.add("resizing");
      if (grab.setPointerCapture) {
        grab.setPointerCapture(ev.pointerId);
      }
      ev.preventDefault();
    });

    grab.addEventListener("pointermove", function (ev) {
      if (!dragging) {
        return;
      }
      /* 見るのは縦だけ（横幅は列構成で決まっており、ここでは変えない） */
      var wanted = startH + (ev.clientY - startY);
      var upper = Math.max(MIN_BOX_H, contentH);
      box.style.maxHeight = Math.max(MIN_BOX_H, Math.min(wanted, upper)) + "px";
    });

    function end(ev) {
      if (!dragging) {
        return;
      }
      dragging = false;
      document.body.classList.remove("resizing");
      if (grab.releasePointerCapture && grab.hasPointerCapture
          && grab.hasPointerCapture(ev.pointerId)) {
        grab.releasePointerCapture(ev.pointerId);
      }
    }

    grab.addEventListener("pointerup", end);
    grab.addEventListener("pointercancel", end);
    return grab;
  }

  /* ---------- ツールバー（件数・検索・判定フィルタ） ---------- */

  function closestCard(element) {
    var node = element;
    while (node && node.classList) {
      if (node.classList.contains("card")) {
        return node;
      }
      node = node.parentNode;
    }
    return null;
  }

  /* ツールバーはカードの見出し行に置く。判定フィルタを持つカードはサーバ側が
     すでに空でないツールバーを描いているので、あればそれを使う */
  function toolbarOf(card) {
    if (!card) {
      return null;
    }
    var head = card.querySelector(".card-hd");
    if (!head) {
      return null;
    }
    var bar = head.querySelector(".toolbar");
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "toolbar";
      head.appendChild(bar);
    }
    return bar;
  }

  function addSearch(list, bar) {
    var input = document.createElement("input");
    input.type = "search";
    input.className = "search";
    input.placeholder = "ユーザを検索";
    input.setAttribute("aria-label", "ユーザ名・メールアドレスで絞り込む");
    bar.insertBefore(input, bar.firstChild);
    input.addEventListener("input", function () {
      list.query = input.value.trim().toLowerCase();
      apply(list);
    });
  }

  function addCount(list, bar) {
    var count = document.createElement("span");
    count.className = "list-count";
    bar.appendChild(count);
    list.count = count;
  }

  function addFilter(list, bar) {
    var select = bar.querySelector("select[data-filter]");
    if (!select) {
      return;
    }
    select.addEventListener("change", function () {
      list.status = select.value;
      apply(list);
    });
  }

  /* ---------- 展開ボタン ---------- */

  function addMore(list, anchor) {
    var box = document.createElement("div");
    box.className = "listmore";
    var button = document.createElement("button");
    button.type = "button";
    button.className = "more";
    var count = document.createElement("span");
    count.className = "more-n";
    button.appendChild(document.createTextNode("残り "));
    button.appendChild(count);
    button.appendChild(document.createTextNode(" " + list.unit + "を表示"));
    box.appendChild(button);
    anchor.parentNode.insertBefore(box, anchor.nextSibling);
    /* 展開したら畳み直さない（読み手が広げた状態を勝手に戻さない） */
    button.addEventListener("click", function () {
      list.expanded = true;
      apply(list);
    });
    list.moreBox = box;
    list.moreCount = count;
  }

  /* ---------- 一覧の組み立て ---------- */

  function newList(rows, limit) {
    var list = {
      rows: rows, limit: limit, expanded: false, query: "", status: "",
      numeric: [], marks: [], sortCol: -1, sortDir: 1,
      count: null, moreBox: null, moreCount: null, grab: null, unit: "件",
    };
    lists.push(list);
    return list;
  }

  function setUp(list, anchor) {
    /* 検索・フィルタの突合先は行ごとに一度だけ読む（並べ替えても行の中身は変わらない） */
    var person = false;
    each(list.rows, function (row) {
      row._search = searchText(row);
      row._status = statusText(row);
      person = person || row._search !== "";
    });
    /* 検索の対象は人が並ぶ一覧だけ（部署・チーム単位の集計表は対象にしない）。
       件数の単位もそれに合わせる */
    list.unit = person ? "名" : "件";
    if (person) {
      var bar = toolbarOf(closestCard(anchor));
      if (bar) {
        addSearch(list, bar);
        addFilter(list, bar);
        addCount(list, bar);
      }
    }
    if (list.rows.length > list.limit) {
      addMore(list, anchor);
    }
    apply(list);
  }

  function setUpTables() {
    each(document.querySelectorAll(".tablebox"), function (box) {
      var table = box.querySelector("table");
      var body = table ? table.tBodies[0] : null;
      if (!body) {
        return;
      }
      var list = newList(toArray(body.rows), TABLE_ROWS);
      list.box = box;
      list.table = table;
      list.body = body;
      addSort(list);
      setUp(list, addGrab(list) || box);
    });
  }

  function setUpBars() {
    each(document.querySelectorAll(".bars"), function (bars) {
      var rows = [];
      each(bars.children, function (child) {
        if (child.classList && child.classList.contains("bar")) {
          rows.push(child);
        }
      });
      if (rows.length === 0) {
        return;
      }
      var holder = bars.parentNode;
      var anchor = (holder.classList && holder.classList.contains("card-bd"))
        ? holder : bars;
      setUp(newList(rows, BAR_ROWS), anchor);
    });
  }

  function init() {
    markTheme();
    each(document.querySelectorAll("[data-theme-set]"), function (button) {
      button.addEventListener("click", function (ev) {
        setTheme(ev.currentTarget.getAttribute("data-theme-set"));
      });
    });
    each(document.querySelectorAll(".tab"), function (tab) {
      tab.addEventListener("click", function (ev) {
        showTab(ev.currentTarget.getAttribute("data-tab"));
      });
    });
    setUpTables();
    setUpBars();
    watchSize();          /* 監視の登録は一覧を組み立てたあと（箱が揃ってから） */
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
