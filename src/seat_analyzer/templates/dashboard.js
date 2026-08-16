
/* ダッシュボードの対話部分（タブ切替とテーマ切替のみ）。

   共有先の閲覧環境をこちらで選べないため、JS が無効でも内容が読めることを前提に置く。
   タブの出し分けは、この直下で付ける html.js の下でしか効かないようにしてあり、
   JS が動かなければ全 section が縦に積まれた状態で表示される。

   CSV 由来の値はここへ渡さない（データはサーバ側で描画済みで、ここは DOM 操作だけを行う）。 */
(function () {
  "use strict";

  var root = document.documentElement;
  var THEME_KEY = "seatdash-theme";
  var theme = "auto";

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
    var buttons = document.querySelectorAll("[data-theme-set]");
    for (var i = 0; i < buttons.length; i += 1) {
      var on = buttons[i].getAttribute("data-theme-set") === theme;
      if (on) {
        buttons[i].classList.add("is-active");
      } else {
        buttons[i].classList.remove("is-active");
      }
      buttons[i].setAttribute("aria-pressed", on ? "true" : "false");
    }
  }

  function setTheme(mode) {
    theme = mode;
    applyTheme(mode);
    markTheme();
    saveTheme(mode);
  }

  /* タブは単純な表示切替。スクロール位置には触れない（切替後もその位置を保つ） */
  function showTab(name) {
    var panels = document.querySelectorAll(".tabpanel");
    for (var i = 0; i < panels.length; i += 1) {
      var onPanel = panels[i].getAttribute("data-tab") === name;
      if (onPanel) {
        panels[i].classList.add("is-active");
      } else {
        panels[i].classList.remove("is-active");
      }
    }
    var tabs = document.querySelectorAll(".tab");
    for (var j = 0; j < tabs.length; j += 1) {
      var onTab = tabs[j].getAttribute("data-tab") === name;
      if (onTab) {
        tabs[j].classList.add("is-active");
      } else {
        tabs[j].classList.remove("is-active");
      }
      tabs[j].setAttribute("aria-selected", onTab ? "true" : "false");
    }
  }

  function init() {
    markTheme();
    var buttons = document.querySelectorAll("[data-theme-set]");
    for (var i = 0; i < buttons.length; i += 1) {
      buttons[i].addEventListener("click", function (ev) {
        setTheme(ev.currentTarget.getAttribute("data-theme-set"));
      });
    }
    var tabs = document.querySelectorAll(".tab");
    for (var j = 0; j < tabs.length; j += 1) {
      tabs[j].addEventListener("click", function (ev) {
        showTab(ev.currentTarget.getAttribute("data-tab"));
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
