"""合成サンプルデータ生成（examples/input/ 配下）。

実スペンドレポートの公開仕様に基づくカラム構成で、動作確認・デモ用の
2組織×2ヶ月分データを生成する。実データの形式確認にも参照できる。
組織ごとに input/<組織名>/{spend,members,code-analytics}/ を作る
（code-analytics は任意のため org-b では省略している）。

org-b には 2026-07 の月中差分デモ用に、次のスナップショットも生成する（値は架空）:
  - spend: 月初〜05 / 〜13 / 〜31 の累積エクスポート（月中の利用推移）
  - members: 07-05 / 07-16 の単日スナップショット（月中のメンバー変動。ikeda が
    Standard→Premium、tanabe が新規追加）
  - code-analytics: 07-05 / 07-16 の単日スナップショット（月中の Claude Code 活動。
    shimizu は LoC 横ばいで spend 停止疑いの傍証、他は増加）
  - members-info: 07-05 / 07-16 の日付つきスナップショット（追加クレジット上限 κ）。
    ikeda の κ が $100→$50 に変わる（κ 変更検出）。正数(250)・0(無効)・無制限・空欄の
    4パターンを含み、上限到達・整合性警告・付与候補・E 分布・構成行のデモになる

org-b の 2026-08 は条件つきセクションがすべて出る「全部入り」サンプル（値は架空）。
レポートの見た目を作るとき、実データを使わずに欠けたセクションのない状態を用意できる:

    uv run seat-analyzer analyze --input-dir examples/input --output-dir examples/reports \
        --org org-b --month 2026-08

org-a の members-info.csv は追加クレジット上限の列を持たない固定名ファイルのまま残す
（列なしでも従来どおり動く後方互換の確認用）。

    uv run python examples/generate_sample_data.py
"""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

BASE = Path(__file__).parent / "input"

# モデル単価 (USD per 1M tokens) — config.yaml と一致させる
PRICES = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# 一部ユーザは複数モデルを併用（モデル割合の見える化デモ用）。
# email -> [(model, 利用割合), ...]。未登録ユーザは主モデル100%。
MODEL_MIX_ORG_A = {
    "tanaka@example.co.jp": [
        ("claude-opus-4-8", 0.6), ("claude-sonnet-4-6", 0.3), ("claude-fable-5", 0.1),
    ],
    "nakamura@example.co.jp": [
        ("claude-sonnet-4-6", 0.7), ("claude-haiku-4-5", 0.3),
    ],
}

# ペルソナ: (email, seat, {month: api_cost_usd}, 主モデル)
USERS_ORG_A = [
    # Premium ヘビーユーザ（現状維持）
    ("tanaka@example.co.jp",   "Premium",  {"2026-05": 520.0, "2026-06": 610.0}, "claude-opus-4-8"),
    ("suzuki@example.co.jp",   "Premium",  {"2026-05": 340.0, "2026-06": 415.0}, "claude-opus-4-8"),
    # Premium ライトユーザ（2ヶ月連続低利用 → ダウングレード推奨）
    ("sato@example.co.jp",     "Premium",  {"2026-05": 18.0,  "2026-06": 24.0},  "claude-sonnet-4-6"),
    ("watanabe@example.co.jp", "Premium",  {"2026-05": 35.0,  "2026-06": 12.0},  "claude-sonnet-4-6"),
    # Premium 利用ゼロ（ダウングレード最有力）
    ("ito@example.co.jp",      "Premium",  {},                                    "claude-sonnet-4-6"),
    # Premium 単月だけ低利用（→ 要観察）
    ("yamamoto@example.co.jp", "Premium",  {"2026-05": 480.0, "2026-06": 30.0},  "claude-opus-4-8"),
    # Standard ヘビーユーザ（従量課金が嵩む → アップグレード推奨）
    ("nakamura@example.co.jp", "Standard", {"2026-05": 290.0, "2026-06": 335.0}, "claude-opus-4-8"),
    # Standard 上限到達疑い（allowance mid=50 の 85% 以上）
    ("kobayashi@example.co.jp","Standard", {"2026-05": 46.0,  "2026-06": 48.5},  "claude-sonnet-4-6"),
    # Standard 通常ユーザ（現状維持）
    ("kato@example.co.jp",     "Standard", {"2026-05": 22.0,  "2026-06": 18.0},  "claude-sonnet-4-6"),
    ("yoshida@example.co.jp",  "Standard", {"2026-05": 8.0,   "2026-06": 11.0},  "claude-haiku-4-5"),
    ("yamada@example.co.jp",   "Standard", {"2026-05": 30.0,  "2026-06": 27.0},  "claude-sonnet-4-6"),
]

# 小規模な2組織目（横断サマリ・--org オプションのデモ用）
USERS_ORG_B = [
    # Premium ヘビーユーザ（現状維持）
    ("mori@example.co.jp",     "Premium",  {"2026-05": 450.0, "2026-06": 470.0}, "claude-opus-4-8"),
    # Premium 利用ゼロ（ダウングレード最有力）
    ("hayashi@example.co.jp",  "Premium",  {},                                    "claude-sonnet-4-6"),
    # Standard ヘビーユーザ（従量課金が嵩む → アップグレード推奨）
    ("ikeda@example.co.jp",    "Standard", {"2026-05": 210.0, "2026-06": 260.0}, "claude-opus-4-8"),
    # Standard 通常ユーザ（現状維持）
    ("shimizu@example.co.jp",  "Standard", {"2026-05": 16.0,  "2026-06": 21.0},  "claude-sonnet-4-6"),
    ("abe@example.co.jp",      "Standard", {"2026-06": 9.0},                     "claude-haiku-4-5"),
    # シート未割当（別組織でアサイン済み・管理者等 → 判定対象外）
    ("okada@example.co.jp",    "Unassigned", {},                                 "claude-sonnet-4-6"),
]

# members に載っていない利用者（シート不明の検知確認用）
ORPHANS_ORG_A = [("guest@example.co.jp", {"2026-06": 15.0}, "claude-sonnet-4-6")]

# org-b 2026-07 の月中スナップショット（累積エクスポート）。
# 月初〜05 / 〜13 / 〜31 の3時点で、各ユーザの累積 API 換算需要（computed）と
# 累積実課金（net_spend）を明示する。差分分析で以下を再現する架空値:
#   - shimizu: 〜13 以降ほぼ横ばい（停止疑い・Standard 実課金0 → 実効込み量の実測候補）
#   - abe: 累積が小さいまま横ばい（遊休であり停止疑いにはしない＝閾値の区別）
#   - mori / ikeda: 途中の区間で実課金が 0→正 に転じる（込み量の消化ポイント）
# ファイル名は claude.ai の期間付きダウンロード名を模した range 命名にする。
SNAPSHOT_UUID = "0b1c2d3e-4f56-4789-a012-3456789abcde"
# ファイル名の日付サフィックス（月初開始の累積） -> [(email, 累積需要, 累積実課金, model)]
# kudo は追加クレジット無効(κ=0)なのに課金が発生する「整合性警告」デモ用の架空ユーザ。
SNAPSHOTS_ORG_B = {
    "2026-07-01-to-2026-07-05": [
        ("mori@example.co.jp",    80.0,  0.0, "claude-opus-4-8"),
        ("ikeda@example.co.jp",   60.0,  0.0, "claude-opus-4-8"),
        ("shimizu@example.co.jp", 40.0,  0.0, "claude-sonnet-4-6"),
        ("abe@example.co.jp",      5.0,  0.0, "claude-haiku-4-5"),
        ("kudo@example.co.jp",    50.0,  0.0, "claude-opus-4-8"),
    ],
    "2026-07-01-to-2026-07-13": [
        ("mori@example.co.jp",   210.0,  0.0, "claude-opus-4-8"),
        ("ikeda@example.co.jp",  150.0, 20.0, "claude-opus-4-8"),
        ("shimizu@example.co.jp", 45.0,  0.0, "claude-sonnet-4-6"),
        ("abe@example.co.jp",      9.0,  0.0, "claude-haiku-4-5"),
        ("kudo@example.co.jp",   150.0, 30.0, "claude-opus-4-8"),
    ],
    "2026-07-01-to-2026-07-31": [
        ("mori@example.co.jp",   470.0, 220.0, "claude-opus-4-8"),
        ("ikeda@example.co.jp",  260.0,  90.0, "claude-opus-4-8"),
        ("shimizu@example.co.jp", 45.4,   0.0, "claude-sonnet-4-6"),
        ("abe@example.co.jp",      9.0,   0.0, "claude-haiku-4-5"),
        ("kudo@example.co.jp",   260.0, 130.0, "claude-opus-4-8"),
    ],
}

# org-b 2026-07 の members 単日スナップショット（月中のメンバー変動デモ）。
# 07-05 → 07-16 で ikeda が Standard→Premium（シート変更）、tanabe が新規追加。
# 主データ（当月判定）には最新の 07-16 が使われる（date スナップショットは最新採用）。
# 日付 -> [(email, seat), ...]
MEMBER_SNAPSHOTS_ORG_B = {
    "2026-07-05": [
        ("mori@example.co.jp",    "Premium"),
        ("hayashi@example.co.jp", "Premium"),
        ("ikeda@example.co.jp",   "Standard"),
        ("shimizu@example.co.jp", "Standard"),
        ("abe@example.co.jp",     "Standard"),
        ("okada@example.co.jp",   "Unassigned"),
        ("kudo@example.co.jp",    "Standard"),
    ],
    "2026-07-16": [
        ("mori@example.co.jp",    "Premium"),
        ("hayashi@example.co.jp", "Premium"),
        ("ikeda@example.co.jp",   "Premium"),     # Standard → Premium（シート変更）
        ("shimizu@example.co.jp", "Standard"),
        ("abe@example.co.jp",     "Standard"),
        ("okada@example.co.jp",   "Unassigned"),
        ("kudo@example.co.jp",    "Standard"),
        ("tanabe@example.co.jp",  "Standard"),    # 月中の新規追加（新規メンバー）
    ],
}

# org-b 2026-07 の members-info 単日スナップショット（追加クレジット上限 κ のデモ）。
# 日付つきで置くと「対象月の月末以前で最新」を採用する。07-05 → 07-16 で ikeda の κ が
# $100→$50 に変わる（κ 変更検出のデモ）。値は正数(250)・0(無効)・無制限・空欄の4パターンを含む。
# 部署・チーム・職種・備考は空欄（クレジット機能に焦点を当てたデモ）。すべて架空値。
# 日付 -> [(email, 追加クレジット上限), ...]
MEMBERS_INFO_SNAPSHOTS_ORG_B = {
    "2026-07-05": [
        ("mori@example.co.jp",    "250"),
        ("hayashi@example.co.jp", "0"),
        ("ikeda@example.co.jp",   "100"),
        ("shimizu@example.co.jp", "無制限"),
        ("abe@example.co.jp",     ""),
        ("okada@example.co.jp",   ""),
        ("kudo@example.co.jp",    "0"),
    ],
    "2026-07-16": [
        ("mori@example.co.jp",    "250"),
        ("hayashi@example.co.jp", "0"),
        ("ikeda@example.co.jp",   "50"),     # κ 変更: $100 → $50
        ("shimizu@example.co.jp", "無制限"),
        ("abe@example.co.jp",     ""),
        ("okada@example.co.jp",   ""),
        ("kudo@example.co.jp",    "0"),
        ("tanabe@example.co.jp",  ""),
    ],
}

# org-b 2026-07 の code-analytics 単日スナップショット（月中の Claude Code 活動デモ）。
# 累積 LoC / PR。shimizu は横ばい（spend 停止疑いと突合して「停止の傍証」になる）。
# 日付 -> [(email, 累積 LoC, 累積 PR), ...]
CODE_SNAPSHOTS_ORG_B = {
    "2026-07-05": [
        ("mori@example.co.jp",    3200, 14),
        ("ikeda@example.co.jp",   2100,  9),
        ("shimizu@example.co.jp",  260,  2),
        ("abe@example.co.jp",       40,  1),
    ],
    "2026-07-16": [
        ("mori@example.co.jp",    6800, 27),
        ("ikeda@example.co.jp",   4300, 18),
        ("shimizu@example.co.jp",  260,  2),   # 横ばい → shimizu 停止疑いの傍証
        ("abe@example.co.jp",       90,  1),
    ],
}

# --- org-b 2026-08: 条件つきセクションを網羅する「全部入り」サンプル（すべて架空値） ---
#
# 出るようにしている条件つきセクション:
#   追加クレジット構成 / 前月からの変化（利用開始・停止・主な増減・実課金の新規発生） /
#   月中の利用推移（停止疑い・込み量の消化） / 月中の Claude Code 活動 /
#   月中のメンバー変動（シート変更・追加・削除・上限変更） / 込み枠の実測 /
#   追加クレジット付与候補 / 備考 / 部署別・チーム別サマリ / LoC 列 /
#   組織内の分布（リクエスト数の行を含む） / 組織サービス利用 / ⚠️上限到達疑い /
#   シート不明（月中に members から消えたユーザ）
#
# 月初開始の累積スナップショット3時点。(email, 累積需要, 累積実課金, model, product構成)。
# ikeda は 2026-07 に利用があり 2026-08 は行を持たない（＝利用停止の検出）。
# email に @ を含まない行は組織サービス利用（シート判定の対象外・サマリに別枠計上）。
SNAPSHOTS_ORG_B_08 = {
    "2026-08-01-to-2026-08-05": [
        ("mori@example.co.jp",     120.0,  0.0, "claude-opus-4-8",
         (("Claude Code", 0.7), ("Cowork", 0.2), ("Chat", 0.1))),
        ("kudo@example.co.jp",      90.0,  0.0, "claude-opus-4-8",
         (("Claude Code", 0.6), ("Chat", 0.4))),
        ("tanabe@example.co.jp",    60.0,  0.0, "claude-sonnet-4-6",
         (("Claude Code", 0.8), ("Design", 0.2))),
        ("shimizu@example.co.jp",   40.0,  0.0, "claude-sonnet-4-6", (("Claude Code", 1.0),)),
        ("abe@example.co.jp",       12.0,  0.0, "claude-haiku-4-5", (("Chat", 1.0),)),
        ("endo@example.co.jp",       8.0,  0.0, "claude-haiku-4-5", (("Chat", 1.0),)),
        ("(org service usage)",     20.0, 20.0, "claude-sonnet-4-6", (("Code Review", 1.0),)),
    ],
    "2026-08-01-to-2026-08-15": [
        ("mori@example.co.jp",     300.0, 40.0, "claude-opus-4-8",
         (("Claude Code", 0.7), ("Cowork", 0.2), ("Chat", 0.1))),
        ("kudo@example.co.jp",     200.0,  0.0, "claude-opus-4-8",
         (("Claude Code", 0.6), ("Chat", 0.4))),
        ("tanabe@example.co.jp",   120.0, 10.0, "claude-sonnet-4-6",
         (("Claude Code", 0.8), ("Design", 0.2))),
        ("shimizu@example.co.jp",   47.6,  0.0, "claude-sonnet-4-6", (("Claude Code", 1.0),)),
        ("abe@example.co.jp",       30.0,  0.0, "claude-haiku-4-5", (("Chat", 1.0),)),
        ("endo@example.co.jp",       8.0,  0.0, "claude-haiku-4-5", (("Chat", 1.0),)),
        ("(org service usage)",     45.0, 45.0, "claude-sonnet-4-6", (("Code Review", 1.0),)),
    ],
    "2026-08-01-to-2026-08-31": [
        ("mori@example.co.jp",     520.0, 260.0, "claude-opus-4-8",
         (("Claude Code", 0.7), ("Cowork", 0.2), ("Chat", 0.1))),
        ("kudo@example.co.jp",     320.0,   0.0, "claude-opus-4-8",
         (("Claude Code", 0.6), ("Chat", 0.4))),
        ("tanabe@example.co.jp",   180.0,  30.0, "claude-sonnet-4-6",
         (("Claude Code", 0.8), ("Design", 0.2))),
        # 〜08-15 からの増分が小さく累積は十分ある = 停止疑い（Standard・実課金ゼロ）
        ("shimizu@example.co.jp",   48.0,   0.0, "claude-sonnet-4-6", (("Claude Code", 1.0),)),
        ("abe@example.co.jp",       46.0,   0.0, "claude-haiku-4-5", (("Chat", 1.0),)),
        # 累積が小さいまま横ばい（遊休であり停止疑いにはしない＝閾値の区別）
        ("endo@example.co.jp",       8.0,   0.0, "claude-haiku-4-5", (("Chat", 1.0),)),
        ("(org service usage)",     60.0,  60.0, "claude-sonnet-4-6", (("Code Review", 1.0),)),
    ],
}

# org-b 2026-08 の members 単日スナップショット。08-05 → 08-16 で tanabe が
# Standard→Premium（シート変更）、sasaki が新規追加、endo が削除（＝当月の spend に
# 利用があるのに members に居ない「シート不明」になる）。
MEMBER_SNAPSHOTS_ORG_B_08 = {
    "2026-08-05": [
        ("mori@example.co.jp",    "Premium"),
        ("hayashi@example.co.jp", "Premium"),
        ("ikeda@example.co.jp",   "Premium"),
        ("shimizu@example.co.jp", "Standard"),
        ("abe@example.co.jp",     "Standard"),
        ("okada@example.co.jp",   "Unassigned"),
        ("kudo@example.co.jp",    "Standard"),
        ("tanabe@example.co.jp",  "Standard"),
        ("endo@example.co.jp",    "Standard"),
    ],
    "2026-08-16": [
        ("mori@example.co.jp",    "Premium"),
        ("hayashi@example.co.jp", "Premium"),
        ("ikeda@example.co.jp",   "Premium"),
        ("shimizu@example.co.jp", "Standard"),
        ("abe@example.co.jp",     "Standard"),
        ("okada@example.co.jp",   "Unassigned"),
        ("kudo@example.co.jp",    "Standard"),
        ("tanabe@example.co.jp",  "Premium"),    # Standard → Premium（シート変更）
        ("sasaki@example.co.jp",  "Standard"),   # 月中の新規追加
    ],
}

# org-b 2026-08 の members-info 単日スナップショット（部署・チーム・職種・上限・備考）。
# 08-05 → 08-16 で ikeda の κ が $50→$250 に変わる（κ 変更検出）。sasaki は載せない
# （管理画面への追加に members-info の追記が追従していない状態＝未登録の警告）。
# (email, 追加クレジット上限, 部署, チーム, 職種, 備考)
_MEMBERS_INFO_ORG_B_08 = [
    ("mori@example.co.jp",    "250", "プラットフォーム開発部", "基盤チーム",     "テックリード", ""),
    ("hayashi@example.co.jp", "0",   "プロダクト開発部",       "Webチーム",      "エンジニア",
     "2026-07 ヒアリング済み: 8月も利用予定なし"),
    ("ikeda@example.co.jp",   "50",  "プラットフォーム開発部", "基盤チーム",     "エンジニア", ""),
    ("shimizu@example.co.jp", "無制限", "プロダクト開発部",    "モバイルチーム", "エンジニア", ""),
    ("abe@example.co.jp",     "",    "コーポレート",           "情シスチーム",   "エンジニア", ""),
    ("okada@example.co.jp",   "",    "コーポレート",           "",               "マネージャー",
     "別組織でシート割当済みのため未割当"),
    ("kudo@example.co.jp",    "0",   "プロダクト開発部",       "Webチーム",      "エンジニア",
     "追加クレジットが無効のため実課金は発生しない"),
    ("tanabe@example.co.jp",  "100", "プラットフォーム開発部", "SREチーム",      "エンジニア", ""),
    ("endo@example.co.jp",    "",    "コーポレート",           "情シスチーム",   "エンジニア", ""),
]

MEMBERS_INFO_SNAPSHOTS_ORG_B_08 = {
    "2026-08-05": _MEMBERS_INFO_ORG_B_08,
    # ikeda の上限だけ $50 → $250 に変える（それ以外は 08-05 と同じ）
    "2026-08-16": [
        (email, "250" if email == "ikeda@example.co.jp" else cap, *rest)
        for email, cap, *rest in _MEMBERS_INFO_ORG_B_08
    ],
}

# org-b 2026-08 の code-analytics 単日スナップショット（累積 LoC / PR）。
# shimizu は横ばい（spend の停止疑いと突合して「停止の傍証」になる）。
# ikeda は行が無い（spend も止まっているユーザ）。
CODE_SNAPSHOTS_ORG_B_08 = {
    "2026-08-05": [
        ("mori@example.co.jp",    2400,  9),
        ("kudo@example.co.jp",     900,  4),
        ("tanabe@example.co.jp",   400,  2),
        ("shimizu@example.co.jp",  310,  3),
        ("abe@example.co.jp",      100,  1),
    ],
    "2026-08-16": [
        ("mori@example.co.jp",    5900, 22),
        ("kudo@example.co.jp",    2600, 11),
        ("tanabe@example.co.jp",  1500,  7),
        ("shimizu@example.co.jp",  310,  3),   # 横ばい → shimizu 停止疑いの傍証
        ("abe@example.co.jp",      180,  2),
    ],
}

# 部署・チーム・職種・備考のマッピング（任意ファイル members-info.csv のデモ）。
# 組織階層は 部署 > チーム。日本語ヘッダ（email,部署,チーム,職種,備考）で日本語
# エイリアスの動作確認も兼ねる。org-a のみ生成。
# (email, 部署, チーム, 職種, 備考)
MEMBERS_INFO_ORG_A = [
    ("tanaka@example.co.jp",    "プラットフォーム開発部", "基盤チーム",     "テックリード", ""),
    ("suzuki@example.co.jp",    "プラットフォーム開発部", "基盤チーム",     "エンジニア",   ""),
    ("sato@example.co.jp",      "プロダクト開発部",       "Webチーム",      "エンジニア",   "2026-06 ヒアリング済み: 7月からPJ利用予定"),
    ("watanabe@example.co.jp",  "プロダクト開発部",       "Webチーム",      "エンジニア",   ""),
    ("ito@example.co.jp",       "コーポレート",           "情シスチーム",   "エンジニア",   "2026-06 休職中・9月復帰予定"),
    ("yamamoto@example.co.jp",  "プラットフォーム開発部", "基盤チーム; SREチーム", "エンジニア", "2チーム兼務（兼務按分のデモ）"),
    ("nakamura@example.co.jp",  "プロダクト開発部",       "モバイルチーム", "テックリード", ""),
    ("kobayashi@example.co.jp", "プロダクト開発部",       "モバイルチーム", "エンジニア",   ""),
    ("kato@example.co.jp",      "コーポレート",           "情シスチーム",   "エンジニア",   ""),
    ("yoshida@example.co.jp",   "コーポレート",           "デザインチーム", "デザイナー",   ""),
    ("yamada@example.co.jp",    "プラットフォーム開発部", "SREチーム",      "エンジニア",   ""),
]

CC_STATS_ORG_A = {  # (PRs with CC, All PRs, Lines with CC, All Lines) — 2026-06
    "tanaka@example.co.jp": (24, 30, 5200, 6800),
    "suzuki@example.co.jp": (18, 26, 3900, 6100),
    "sato@example.co.jp": (1, 12, 80, 2400),
    "watanabe@example.co.jp": (2, 9, 150, 1900),
    "yamamoto@example.co.jp": (3, 11, 400, 2100),
    "nakamura@example.co.jp": (21, 24, 4700, 5600),
    "kobayashi@example.co.jp": (9, 14, 1800, 2900),
    "kato@example.co.jp": (5, 10, 700, 1700),
    "yoshida@example.co.jp": (2, 8, 200, 1500),
    "yamada@example.co.jp": (6, 12, 900, 2000),
}

# 組織名 → (メンバー, 非メンバー利用者, code-analytics。None なら生成しない)
ORGS = {
    "org-a": (USERS_ORG_A, ORPHANS_ORG_A, CC_STATS_ORG_A),
    "org-b": (USERS_ORG_B, [], None),
}


def tokens_for_cost(cost: float, model: str) -> tuple[int, int]:
    """入力:出力 = 10:1 の前提で cost に一致するトークン数を逆算する。"""
    inp, outp = PRICES[model]
    completion = cost / ((10 * inp + outp) / 1e6)
    return int(completion * 10), int(completion)


def write_spend(org: str, month: str, users: list, orphans: list) -> None:
    path = BASE / org / "spend" / f"spend_{month}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    entries = [(u[0], u[2], u[3]) for u in users] + orphans
    for email, costs, model in entries:
        if month not in costs:
            continue
        total = costs[month]
        models = MODEL_MIX_ORG_A.get(email, [(model, 1.0)])
        # モデル×プロダクト（Claude Code 8割 / Chat 2割）の組み合わせで明細行を生成
        for mdl, mshare in models:
            for product, pshare in (("Claude Code", 0.8), ("Chat", 0.2)):
                cost = round(total * mshare * pshare, 4)
                if cost <= 0:
                    continue
                p_tok, c_tok = tokens_for_cost(cost, mdl)
                rows.append({
                    "Email": email,
                    # hash() はラン間で不定のため、再生成しても差分が出ない決定的ハッシュを使う
                    "Account UUID": f"uuid-{hashlib.md5(email.encode()).hexdigest()[:8]}",
                    "Product": product,
                    "Model": mdl,
                    "Model Family": mdl.rsplit("-", 2)[0],
                    "Request Count": max(1, int(cost * 4)),
                    "Prompt Tokens": p_tok,
                    "Completion Tokens": c_tok,
                    "Total Gross Spend USD": f"{cost:.4f}",
                    "Total Net Spend USD": f"{cost:.4f}",
                })
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {path} ({len(rows)} rows)")


def write_spend_snapshot(org: str, date_suffix: str, entries: list) -> None:
    """月初開始の累積スナップショット1件を range 命名の CSV で書く（差分分析デモ用）。

    entries は (email, 累積需要, 累積実課金, model) か、末尾に product 構成
    ((product, 割合), ...) を足した5要素。省略時は Claude Code 100%。累積実課金は
    ユーザ単位の合計なので先頭の product 行にまとめて載せる。
    """
    name = f"spend-report-{SNAPSHOT_UUID}-{date_suffix}.csv"
    path = BASE / org / "spend" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for entry in entries:
        email, cum_cost, cum_net, model = entry[:4]
        products = entry[4] if len(entry) > 4 else (("Claude Code", 1.0),)
        for i, (product, share) in enumerate(products):
            cost = round(cum_cost * share, 4)
            net = cum_net if i == 0 else 0.0
            p_tok, c_tok = tokens_for_cost(cost, model)
            rows.append({
                "Email": email,
                "Account UUID": f"uuid-{hashlib.md5(email.encode()).hexdigest()[:8]}",
                "Product": product,
                "Model": model,
                "Model Family": model.rsplit("-", 2)[0],
                "Request Count": max(1, int(cost * 4)),
                "Prompt Tokens": p_tok,
                "Completion Tokens": c_tok,
                "Total Gross Spend USD": f"{net:.4f}",
                "Total Net Spend USD": f"{net:.4f}",
            })
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {path} ({len(rows)} rows)")


def write_members(org: str, month: str, users: list) -> None:
    path = BASE / org / "members" / f"members_{month}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Email", "Name", "Role", "Seat Type"])
        for email, seat, _, _ in users:
            name = email.split("@")[0].title()
            writer.writerow([email, name, "Member", seat])
    print(f"wrote {path}")


def write_members_snapshot(org: str, date: str, entries: list) -> None:
    """members の単日スナップショット1件を日付命名の CSV で書く（メンバー変動デモ用）。"""
    name = f"members-{SNAPSHOT_UUID}-{date}.csv"
    path = BASE / org / "members" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Email", "Name", "Role", "Seat Type"])
        for email, seat in entries:
            writer.writerow([email, email.split("@")[0].title(), "Member", seat])
    print(f"wrote {path}")


def write_code_snapshot(org: str, date: str, entries: list) -> None:
    """code-analytics の単日スナップショット1件を日付命名の CSV で書く（活動の差分デモ用）。"""
    name = f"code-analytics-{SNAPSHOT_UUID}-{date}.csv"
    path = BASE / org / "code-analytics" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Email", "Lines with CC", "PRs with CC"])
        for email, loc, prs in entries:
            writer.writerow([email, loc, prs])
    print(f"wrote {path}")


def write_members_info(org: str, info: list) -> None:
    """任意ファイル members-info.csv（月情報なし・org ディレクトリ直下・固定ファイル名）。

    後方互換確認用に追加クレジット上限の列を持たない旧形式のまま残す（org-a）。
    """
    path = BASE / org / "members-info.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["email", "部署", "チーム", "職種", "備考"])
        writer.writerows(info)
    print(f"wrote {path}")


def write_members_info_snapshot(org: str, date: str, entries: list) -> None:
    """members-info の日付つきスナップショット1件（追加クレジット上限のデモ用）。

    entries は [(email, 追加クレジット上限), ...] か、末尾に
    (部署, チーム, 職種, 備考) を足した6要素。省略した項目は空欄で書く。
    """
    name = f"members-info-{SNAPSHOT_UUID}-{date}.csv"
    path = BASE / org / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["email", "部署", "チーム", "職種", "追加クレジット上限", "備考"])
        for entry in entries:
            email, credit_limit = entry[:2]
            dept, team, role, note = (list(entry[2:]) + [""] * 4)[:4]
            writer.writerow([email, dept, team, role, credit_limit, note])
    print(f"wrote {path}")


def write_code_analytics(org: str, month: str, cc_stats: dict) -> None:
    path = BASE / org / "code-analytics" / f"cc_{month}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Email", "PRs with CC", "All PRs", "Lines with CC", "All Lines"])
        for email, stats in cc_stats.items():
            writer.writerow([email, *stats])
    print(f"wrote {path}")


if __name__ == "__main__":
    for org, (users, orphans, cc_stats) in ORGS.items():
        for month in ("2026-05", "2026-06"):
            write_spend(org, month, users, orphans)
        write_members(org, "2026-06", users)
        if cc_stats is not None:
            write_code_analytics(org, "2026-06", cc_stats)
    # 任意入力デモ: 部署・職種・備考は org-a のみ（org-b は生成しない）
    write_members_info("org-a", MEMBERS_INFO_ORG_A)

    # org-b の 2026-07: 月中スナップショット（差分分析デモ）。
    # members は月中のメンバー変動デモのため単日スナップショット2件で置く
    # （月次ファイルは作らない。当月判定には最新の 07-16 が使われる）。
    for date_suffix, entries in SNAPSHOTS_ORG_B.items():
        write_spend_snapshot("org-b", date_suffix, entries)
    for date, entries in MEMBER_SNAPSHOTS_ORG_B.items():
        write_members_snapshot("org-b", date, entries)
    for date, entries in CODE_SNAPSHOTS_ORG_B.items():
        write_code_snapshot("org-b", date, entries)
    # org-b の追加クレジット上限（日付つき members-info スナップショット）
    for date, entries in MEMBERS_INFO_SNAPSHOTS_ORG_B.items():
        write_members_info_snapshot("org-b", date, entries)

    # org-b の 2026-08: 条件つきセクションがすべて出る「全部入り」サンプル
    for date_suffix, entries in SNAPSHOTS_ORG_B_08.items():
        write_spend_snapshot("org-b", date_suffix, entries)
    for date, entries in MEMBER_SNAPSHOTS_ORG_B_08.items():
        write_members_snapshot("org-b", date, entries)
    for date, entries in CODE_SNAPSHOTS_ORG_B_08.items():
        write_code_snapshot("org-b", date, entries)
    for date, entries in MEMBERS_INFO_SNAPSHOTS_ORG_B_08.items():
        write_members_info_snapshot("org-b", date, entries)
