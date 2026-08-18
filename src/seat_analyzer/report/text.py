"""レポートの表示順・ラベルと、Markdown / HTML で共有する固定文言・条件つき注記。"""

from __future__ import annotations

import pandas as pd

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

# クレジットモード → 表示ラベル（付与候補の Markdown / HTML で共用）。
# enabled は付与候補に現れないためラベルを持たない。
_CREDIT_MODE_LABEL = {CREDIT_DISABLED: "無効", CREDIT_UNKNOWN: "不明"}

# 部署/チーム軸の共通定義（col, 見出し, （未設定）行を含めるか）。
# チームは（未設定）を除外する（チーム未設定は部署も異なる異質な集合のためまとめても意味がない）。
GROUP_AXES = (
    ("department", "部署別サマリ", True),
    ("team", "チーム別サマリ", False),
)

# 複数の出力（Markdown と HTML、または dashboard と preview-dashboard）に同じ文で出る
# 固定文言。同じ文を2箇所で保守すると片方だけ直す事故が起きるため、ここを唯一の定義とする。
# Markdown 側は _TEXT[...] を直接埋め、HTML 側はテンプレート組み立て時に
# <!--text:キー--> を _embed_shared_text() が置換する。
# 文中に Jinja/HTML の特殊文字（{ } % < > &）を含めないこと（そのまま出力される）。
_TEXT = {
    # セクション見出し（md は "## " を、HTML は <h2> を前後に付ける）
    "h_snapshot": "月中の利用推移（スナップショット差分）",
    "h_code_diff": "月中の Claude Code 活動（code-analytics 差分）",
    "h_member_changes": "月中のメンバー変動（スナップショット差分）",
    "h_e_dist": "込み枠の実測（E = API換算需要 − 実課金）",
    "h_grant": "追加クレジット付与候補",
    "h_credit_reach": "追加クレジット残額",
    "h_stats": "組織内の分布（参考値）",
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
    # 推奨一覧の列の読み方。表の脚注と「前提と注意」の両方に同じ文で出る
    "note_cost_columns": "「Std時 / Prem時」= そのシートの場合の想定月額。現シート側は"
                         "シート料+実課金の観測実績、変更先側は込み利用量（推定値）"
                         "モデルの試算",
    "note_confidence": "確度 = 込み利用量の low/mid/high 3シナリオ推定での判定一致度",
    "note_cap_flag": "⚠ = 実課金ゼロなのに需要が込み量推定に迫る Standard ユーザ"
                     "（上限到達の可能性）",
    # 詳細利用状況の脚注（正式ダッシュボードと速報ダッシュボードで共有）
    "note_detail_tokens": "input はキャッシュ読取分を含むため、実入力量より大きく"
                          "見えることがあります。product構成 は利用回数（リクエスト数）基準",
    # 追加クレジット付与候補の空状態。候補がゼロでもカードを出し、何が挙がるのかを添える。
    # 「該当者がいない」と「そもそも判定できていない」は別の状態なので文言を分ける
    # （上限列が空欄の組織では前者に見せると、判定が済んだものと誤読される）
    "note_grant_empty": "追加クレジットが無効または未設定の Standard ユーザのうち、"
                        "需要のモデル試算が Premium 有利になった人をここに挙げます",
    "note_grant_no_data": "members-info.csv の「追加クレジット上限」列が未記入のため、"
                          "付与候補を判定できません",
    # 分布（参考値）の読み方。数値そのものより誤読の防ぎ方が要るので注記を厚くする
    "note_stats_population": "母集団はシート未割当を除く分析対象ユーザで、利用ゼロの"
                             "ユーザも含みます。欠損の扱いが指標ごとに違うため n も"
                             "指標ごとに示します",
    "note_stats_skew": "平均は少数の大口利用に引かれやすいため、平均以下であることは"
                       "低活用を意味しません。個々の位置は中央値・分位点で読んでください",
    "note_stats_censored": "追加クレジット上限に到達したユーザは需要そのものが上限で"
                           "止まっているため、分布の右裾は実態より低く出ます",
    "note_stats_loc": "LoC と spend は網羅範囲が一致せず、LoC の行が無いことは"
                      "コードを書いていないことを意味しません",
    "note_stats_scope": "比較の母集団は当該組織内に閉じています。他組織や一般的な水準との"
                        "比較には使えません",
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


def _disabled_cost_note(users: pd.DataFrame) -> str:
    """クレジット無効ユーザのコスト列の意味注記（無ければ空文字列）。"""
    if "credits_mode" not in users.columns:
        return ""
    judged = users[users["current_seat"].isin(("standard", "premium"))]
    n_disabled = int((judged["credits_mode"] == CREDIT_DISABLED).sum())
    if n_disabled == 0:
        return ""
    if n_disabled == len(judged):
        return ("追加クレジットが無効のため、「Standard時/Premium時」の枠超過分は実際には"
                "請求されません（絞り負担のドル換算＝需要が上限で抑えられる分の目安）")
    return ("クレジット無効のユーザについては、「Standard時/Premium時」の枠超過分は実際には"
            "請求されず、絞り負担のドル換算（需要が上限で抑えられる分の目安）です")


def _cap_legend_supplement(credit_shown: bool) -> str:
    """⚠️上限? 凡例の補足（credit_shown のときのみ。無ければ空文字列）。"""
    if not credit_shown:
        return ""
    return ("追加クレジットが有効なユーザは実課金がセンサーになるため、"
            "実課金ゼロなら枠内と判断でき ⚠️上限? を付けません")
