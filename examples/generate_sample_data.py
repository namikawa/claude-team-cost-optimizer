"""合成サンプルデータ生成（examples/input/ 配下）。

実スペンドレポートの公開仕様に基づくカラム構成で、動作確認・デモ用の
2ヶ月分データを生成する。実データの形式確認にも参照できる。

    uv run python examples/generate_sample_data.py
"""

from __future__ import annotations

import csv
from pathlib import Path

BASE = Path(__file__).parent / "input"

# モデル単価 (USD per 1M tokens) — config.yaml と一致させる
PRICES = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# ペルソナ: (email, seat, {month: api_cost_usd}, 主モデル)
USERS = [
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

# members に載っていない利用者（シート不明の検知確認用）
ORPHAN = ("guest@example.co.jp", {"2026-06": 15.0}, "claude-sonnet-4-6")

CC_STATS = {  # (PRs with CC, All PRs, Lines with CC, All Lines) — 2026-06
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


def tokens_for_cost(cost: float, model: str) -> tuple[int, int]:
    """入力:出力 = 10:1 の前提で cost に一致するトークン数を逆算する。"""
    inp, outp = PRICES[model]
    completion = cost / ((10 * inp + outp) / 1e6)
    return int(completion * 10), int(completion)


def write_spend(month: str) -> None:
    path = BASE / "spend" / f"spend_{month}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    entries = [(u[0], u[2], u[3]) for u in USERS] + [ORPHAN]
    for email, costs, model in entries:
        if month not in costs:
            continue
        total = costs[month]
        # Claude Code 8割 / Chat 2割 の2行に分割
        for product, share in (("Claude Code", 0.8), ("Chat", 0.2)):
            cost = round(total * share, 4)
            p_tok, c_tok = tokens_for_cost(cost, model)
            rows.append({
                "Email": email,
                "Account UUID": f"uuid-{abs(hash(email)) % 10**8:08d}",
                "Product": product,
                "Model": model,
                "Model Family": model.rsplit("-", 2)[0],
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


def write_members(month: str) -> None:
    path = BASE / "members" / f"members_{month}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Email", "Name", "Role", "Seat Type"])
        for email, seat, _, _ in USERS:
            name = email.split("@")[0].title()
            writer.writerow([email, name, "Member", seat])
    print(f"wrote {path}")


def write_code_analytics(month: str) -> None:
    path = BASE / "code-analytics" / f"cc_{month}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Email", "PRs with CC", "All PRs", "Lines with CC", "All Lines"])
        for email, stats in CC_STATS.items():
            writer.writerow([email, *stats])
    print(f"wrote {path}")


if __name__ == "__main__":
    for month in ("2026-05", "2026-06"):
        write_spend(month)
    write_members("2026-06")
    write_code_analytics("2026-06")
