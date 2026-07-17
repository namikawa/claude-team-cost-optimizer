"""合成サンプルデータ生成（examples/input/ 配下）。

実スペンドレポートの公開仕様に基づくカラム構成で、動作確認・デモ用の
2組織×2ヶ月分データを生成する。実データの形式確認にも参照できる。
組織ごとに input/<組織名>/{spend,members,code-analytics}/ を作る
（code-analytics は任意のため org-b では省略している）。

org-b には 2026-07 の月中差分デモ用に、次の3種のスナップショットも生成する（値は架空）:
  - spend: 月初〜05 / 〜13 / 〜31 の累積エクスポート（月中の利用推移）
  - members: 07-05 / 07-16 の単日スナップショット（月中のメンバー変動。ikeda が
    Standard→Premium、tanabe が新規追加）
  - code-analytics: 07-05 / 07-16 の単日スナップショット（月中の Claude Code 活動。
    shimizu は LoC 横ばいで spend 停止疑いの傍証、他は増加）

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
SNAPSHOTS_ORG_B = {
    "2026-07-01-to-2026-07-05": [
        ("mori@example.co.jp",    80.0,  0.0, "claude-opus-4-8"),
        ("ikeda@example.co.jp",   60.0,  0.0, "claude-opus-4-8"),
        ("shimizu@example.co.jp", 40.0,  0.0, "claude-sonnet-4-6"),
        ("abe@example.co.jp",      5.0,  0.0, "claude-haiku-4-5"),
    ],
    "2026-07-01-to-2026-07-13": [
        ("mori@example.co.jp",   210.0,  0.0, "claude-opus-4-8"),
        ("ikeda@example.co.jp",  150.0, 20.0, "claude-opus-4-8"),
        ("shimizu@example.co.jp", 45.0,  0.0, "claude-sonnet-4-6"),
        ("abe@example.co.jp",      9.0,  0.0, "claude-haiku-4-5"),
    ],
    "2026-07-01-to-2026-07-31": [
        ("mori@example.co.jp",   470.0, 220.0, "claude-opus-4-8"),
        ("ikeda@example.co.jp",  260.0,  90.0, "claude-opus-4-8"),
        ("shimizu@example.co.jp", 45.4,   0.0, "claude-sonnet-4-6"),
        ("abe@example.co.jp",      9.0,   0.0, "claude-haiku-4-5"),
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
    ],
    "2026-07-16": [
        ("mori@example.co.jp",    "Premium"),
        ("hayashi@example.co.jp", "Premium"),
        ("ikeda@example.co.jp",   "Premium"),     # Standard → Premium（シート変更）
        ("shimizu@example.co.jp", "Standard"),
        ("abe@example.co.jp",     "Standard"),
        ("okada@example.co.jp",   "Unassigned"),
        ("tanabe@example.co.jp",  "Standard"),    # 月中の新規追加（新規メンバー）
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
    """月初開始の累積スナップショット1件を range 命名の CSV で書く（差分分析デモ用）。"""
    name = f"spend-report-{SNAPSHOT_UUID}-{date_suffix}.csv"
    path = BASE / org / "spend" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for email, cum_cost, cum_net, model in entries:
        p_tok, c_tok = tokens_for_cost(cum_cost, model)
        rows.append({
            "Email": email,
            "Account UUID": f"uuid-{hashlib.md5(email.encode()).hexdigest()[:8]}",
            "Product": "Claude Code",
            "Model": model,
            "Model Family": model.rsplit("-", 2)[0],
            "Request Count": max(1, int(cum_cost * 4)),
            "Prompt Tokens": p_tok,
            "Completion Tokens": c_tok,
            "Total Gross Spend USD": f"{cum_net:.4f}",
            "Total Net Spend USD": f"{cum_net:.4f}",
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
    """任意ファイル members-info.csv（月情報なし・org ディレクトリ直下・固定ファイル名）。"""
    path = BASE / org / "members-info.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["email", "部署", "チーム", "職種", "備考"])
        writer.writerows(info)
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
