"""レポートの表示順・ラベル・バッジクラスと、Markdown / HTML で共有する固定文言。"""

from __future__ import annotations

from ..analyze import (
    CREDIT_DISABLED,
    CREDIT_UNKNOWN,
    LABEL_EXCLUDED,
    LABEL_HOLD,
    LABEL_IDLE,
    LABEL_PREM_CONSIDER,
    LABEL_PREM_OK,
    LABEL_STD_CAND,
    LABEL_STD_OK,
    STATUS_CHANGE,
    STATUS_EXCLUDED,
    STATUS_KEEP,
    STATUS_UNKNOWN,
    STATUS_WATCH,
    STATUS_WATCH_WAIT,
)

STATUS_ORDER = [STATUS_CHANGE, STATUS_WATCH, STATUS_WATCH_WAIT, STATUS_UNKNOWN,
                STATUS_KEEP, STATUS_EXCLUDED]

# 速報の一次判断ラベルの表示順（対応アクションが明確なものから）
PREVIEW_ORDER = [LABEL_IDLE, LABEL_STD_CAND, LABEL_PREM_CONSIDER, LABEL_HOLD,
                 STATUS_UNKNOWN, LABEL_PREM_OK, LABEL_STD_OK, LABEL_EXCLUDED]

# 判定ステータス → .badge クラス（速報側 _PREVIEW_BADGE_CLASS と同じ設計。
# 未知の値は現状維持相当の b-keep に倒す）。
_STATUS_BADGE_CLASS = {
    STATUS_CHANGE: "b-change",
    STATUS_WATCH: "b-watch",
    STATUS_WATCH_WAIT: "b-watch",
    STATUS_UNKNOWN: "b-unknown",
    STATUS_KEEP: "b-keep",
    STATUS_EXCLUDED: "b-keep",
}

# クレジットモード → 表示ラベル（付与候補の Markdown / HTML で共用）。
# enabled は付与候補に現れないためラベルを持たない。
_CREDIT_MODE_LABEL = {CREDIT_DISABLED: "無効", CREDIT_UNKNOWN: "不明"}

# 部署/チーム軸の共通定義（col, 見出し, （未設定）行を含めるか）。
# チームは（未設定）を除外する（チーム未設定は部署も異なる異質な集合のためまとめても意味がない）。
GROUP_AXES = (
    ("department", "部署別サマリ", True),
    ("team", "チーム別サマリ", False),
)

# Markdown と HTML の両方に出る固定文言。同じ文を2箇所で保守すると片方だけ直す事故が
# 起きるため、ここを唯一の定義とする。Markdown 側は _TEXT[...] を直接埋め、HTML 側は
# テンプレート組み立て時に <!--text:キー--> を _embed_shared_text() が置換する。
# 文中に Jinja/HTML の特殊文字（{ } % < > &）を含めないこと（そのまま出力される）。
_TEXT = {
    # セクション見出し（md は "## " を、HTML は <h2> を前後に付ける）
    "h_snapshot": "月中の利用推移（スナップショット差分）",
    "h_code_diff": "月中の Claude Code 活動（code-analytics 差分）",
    "h_member_changes": "月中のメンバー変動（スナップショット差分）",
    "h_e_dist": "込み枠の実測（E = API換算需要 − 実課金）",
    "h_grant": "追加クレジット付与候補",
    "h_credit_reach": "追加クレジット残額",
    # 注記（末尾の句点は使う側で付ける。md は付けず HTML は付ける）
    "note_stall_caveat": "停止は休暇・案件の谷でも起こるため、上限到達の断定には本人確認が必要です",
    "note_credit_change": "追加クレジット上限を変更した月の課金は部分月のため、"
                          "上限に基づく判定は翌月から行ってください",
    "note_credit_eta": "到達見込みはスナップショットがある場合は直近区間の課金ペース、"
                       "無い場合は月初からの平均ペースによる目安です。平均ペースの場合、"
                       "課金は込み枠を使い切ってから始まるため実際の到達はこれより"
                       "早くなりうる点に注意してください",
    "note_team_total": "チーム別サマリはチーム未設定のユーザを除外しているため、"
                       "縦合計は組織全体と一致しません",
    "note_billed_nonlinear": "実課金は込み量を使い切ってから発生する非線形な値のため、"
                             "月末ペース換算していません",
    # 速報の凡例
    "legend_idle": "遊休候補: 観測期間中の利用がほぼゼロ。解約前にオンボーディング状況のヒアリングを推奨",
    "legend_over": "⚠️超過済: Premium の込み量を観測期間中にすでに超過し実課金が発生（明確なヘビー層）",
    "legend_billed": "⚠️従量あり: Standard 等で従量課金が発生（Premium 検討の重要シグナル）",
    "legend_excluded": "対象外（未割当）: 意図的にシートを割り当てていないメンバー"
                       "（別組織でアサイン済み・管理者等）",
}


def _embed_shared_text(src: str) -> str:
    """HTML テンプレート組み立て時に <!--text:キー--> を _TEXT の文言へ置換する。"""
    for key, value in _TEXT.items():
        src = src.replace(f"<!--text:{key}-->", value)
    return src


# 速報の一次判断ラベル → 既存 .badge クラス。PREVIEW_ORDER に無いラベルは b-keep に倒す。
_PREVIEW_BADGE_CLASS = {
    LABEL_STD_CAND: "b-change", LABEL_PREM_CONSIDER: "b-change",   # アクション候補（緑）
    LABEL_IDLE: "b-watch", LABEL_HOLD: "b-watch",                 # 要観察・保留（橙）
    STATUS_UNKNOWN: "b-unknown",                                  # データ不整合（赤）
    LABEL_PREM_OK: "b-keep", LABEL_STD_OK: "b-keep",             # 現状妥当（グレー）
    LABEL_EXCLUDED: "b-keep",
}
