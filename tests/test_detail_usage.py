"""詳細利用状況（input/output トークン・モデル割合・LoC）のテスト。"""

import re

import pandas as pd

from seat_analyzer.analyze import _short_model, analyze, preview
from seat_analyzer.report import write_details, write_html, write_preview
from seat_analyzer.report.format import _detail_rows, _fmt_tokens
from seat_analyzer.report.markdown import _detail_table_md

from .conftest import spend_row


def test_short_model():
    assert _short_model("claude-opus-4-8") == "Opus 4.8"
    assert _short_model("claude-fable-5") == "Fable 5"
    assert _short_model("claude-sonnet-5") == "Sonnet 5"
    assert _short_model("claude-sonnet-4-6") == "Sonnet 4.6"
    assert _short_model("claude-haiku-4-5-20251001") == "Haiku 4.5"
    assert _short_model("mystery-model") == "mystery-model"  # 判別不能はそのまま


def test_fmt_tokens():
    assert _fmt_tokens(6_720_200_000) == "6.7B"
    assert _fmt_tokens(1_200_000) == "1.2M"
    assert _fmt_tokens(340_000) == "340K"
    assert _fmt_tokens(500) == "500"
    assert _fmt_tokens(0) == "0"


def test_model_breakdown_is_token_basis(cfg, make_input):
    # opus と haiku を同額（コスト同じ）で計上。トークン基準なら安価な haiku の比率が大きい
    input_dir = make_input(
        {"2026-06": [
            spend_row("a@x.jp", 50.0, model="claude-opus-4-8", net=0.0),
            spend_row("a@x.jp", 50.0, model="claude-haiku-4-5", net=0.0),
        ]},
        members=["a@x.jp,Premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    bd = result.users.set_index("email").loc["a@x.jp", "model_breakdown"]
    # コスト基準なら 50/50 だが、トークン基準では haiku が先頭（大きい）
    assert bd.startswith("Haiku 4.5")
    assert "Opus 4.8" in bd


def test_product_breakdown_is_request_basis(cfg, make_input):
    # Claude Code(コスト30) と Chat(コスト10) を各1行（各10リクエスト）。
    # 回数基準なら 50/50、コスト基準なら 75/25 になる → 回数基準を検証
    input_dir = make_input(
        {"2026-06": [
            spend_row("a@x.jp", 30.0, product="Claude Code", net=0.0),
            spend_row("a@x.jp", 10.0, product="Chat", net=0.0),
        ]},
        members=["a@x.jp,Premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    bd = result.users.set_index("email").loc["a@x.jp", "product_breakdown"]
    assert "Claude Code 50%" in bd and "Chat 50%" in bd


def test_detail_rows_sort_and_loc_absence(cfg, make_input):
    input_dir = make_input(
        {"2026-06": [
            spend_row("small@x.jp", 5.0, net=0.0),
            spend_row("big@x.jp", 500.0, net=0.0),
        ]},
        members=["small@x.jp,Premium", "big@x.jp,Premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    rows, has_loc = _detail_rows(result.users)
    assert has_loc is False  # code-analytics なしなら LoC 列なし
    assert rows[0]["email"] == "big@x.jp"  # input+output 降順


def test_detail_rows_order_is_independent_of_input_row_order(cfg, make_input):
    """トークン数もコストも完全に同点のユーザがいても、行順が入力の行順に依存しないこと。

    タイブレークが無いと同点行の並びが入力順のまま残り、レポートの行順が実行環境で
    変わりうる。email 昇順で一意に決まることを固定する。
    """
    input_dir = make_input(
        {"2026-06": [
            spend_row("tie-b@x.jp", 10.0, net=0.0),
            spend_row("tie-a@x.jp", 10.0, net=0.0),   # tie-b とトークン数・コストとも同点
            spend_row("big@x.jp", 500.0, net=0.0),
        ]},
        members=["tie-a@x.jp,Premium", "tie-b@x.jp,Premium", "big@x.jp,Premium"],
    )
    users = analyze(input_dir, "2026-06", cfg, org="org-a").users
    assert users.set_index("email").loc["tie-a@x.jp", "prompt_tokens"] == \
        users.set_index("email").loc["tie-b@x.jp", "prompt_tokens"]   # 同点の前提を確認
    orders = (users, users.iloc[::-1],
              users.sort_values("email"), users.sort_values("email", ascending=False))
    for frame in orders:
        rows, _ = _detail_rows(frame)
        assert [r["email"] for r in rows] == ["big@x.jp", "tie-a@x.jp", "tie-b@x.jp"]


def test_detail_table_md_with_loc():
    users = pd.DataFrame([
        {"email": "a@x.jp", "prompt_tokens": 1_200_000, "completion_tokens": 100_000,
         "api_cost_usd": 234.5, "model_breakdown": "Opus 4.8 100%",
         "product_breakdown": "", "loc_with_cc": 5200},
    ])
    md = _detail_table_md(users)
    assert "## 詳細利用状況" in md
    assert "LoC" in md and "5,200" in md
    assert "1.2M" in md
    assert "API換算需要" in md and "$234.50" in md
    assert "キャッシュ読取分を含む" in md


# --- 速報の詳細利用状況（観測値のまま出す） ---

def _preview_detail_card(cfg, input_dir, tmp_path) -> str:
    """速報ダッシュボードの「詳細利用状況（観測値）」カード1枚分の HTML。"""
    result = preview(input_dir, "2026-06", cfg, days_observed=10, org="org-a")
    out = tmp_path / "pv"
    write_preview(result, out)
    html = (out / "2026-06" / "preview-dashboard.html").read_text(encoding="utf-8")
    card = re.search(r"<h2>詳細利用状況（観測値）</h2>.*?</section>", html, re.S)
    assert card, "速報ダッシュボードに詳細利用状況のカードがありません"
    return card.group(0)


def test_preview_detail_table_keeps_the_observed_values(cfg, make_input, tmp_path):
    """速報の詳細利用状況は観測実績のまま（月末ペース換算 ×3.0 を掛けない）。

    トークン数と product 構成比は日割り換算しても意味を持たない。需要だけを換算すると
    同じ行の中で基準の違う数値が並ぶため、この表は全体を観測値で揃える。
    """
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 30.0, net=0.0)]},
        members=["a@x.jp,Premium"],
    )
    card = _preview_detail_card(cfg, input_dir, tmp_path)
    assert "API換算需要（観測）" in card
    assert "$30.00" in card and "$90.00" not in card     # ×3.0 の換算値は出さない
    assert "Sonnet 4.6 100%" in card and "Claude Code 100%" in card
    assert "10日分" in card                              # 脚注が観測日数を書く


def test_preview_detail_table_has_no_loc_column_without_code_analytics(
        cfg, make_input, tmp_path):
    """code-analytics が無い組織では LoC 列を出さない（0 で埋めた列を作らない）。"""
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 30.0, net=0.0)]},
        members=["a@x.jp,Premium"],
    )
    card = _preview_detail_card(cfg, input_dir, tmp_path)
    assert "LoC" not in card
    assert "時点の code-analytics" not in card     # 列が無ければ時点の注記も出さない


def test_preview_detail_table_shows_loc_with_its_observation_date(
        cfg, make_input, write_code_snapshots, tmp_path):
    """code-analytics があれば LoC 列を足し、その観測時点を脚注に書く。

    LoC は対象月で最新のスナップショットの累積値で、spend の観測期間の末日とは
    限らない（月内の別の日で止まっていることがある）。カードの脚注が観測日数だけを
    名乗ると、LoC も同じ期間の値だと読めてしまう。
    """
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 30.0, net=0.0)]},
        members=["a@x.jp,Premium"],
    )
    write_code_snapshots(input_dir, {"2026-06-10": [("a@x.jp", 4200, 7)]})
    card = _preview_detail_card(cfg, input_dir, tmp_path)
    assert '<th class="num">LoC</th>' in card
    assert "4,200" in card
    assert "LoC は 2026-06-10 時点の code-analytics スナップショットの累積値です。" in card


def test_preview_detail_table_omits_the_date_when_the_file_has_no_day(
        cfg, make_input, tmp_path):
    """月のみの命名（cc_YYYY-MM.csv）は時点が決まらないので注記を出さない。

    月名だけのファイルから月末を推測して書くと、実際より新しい時点を断定することになる。
    LoC 列そのものは出せるので、列は残して注記だけを落とす。
    """
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 30.0, net=0.0)]},
        members=["a@x.jp,Premium"],
    )
    code_dir = input_dir / "code-analytics"
    code_dir.mkdir(parents=True, exist_ok=True)
    (code_dir / "cc_2026-06.csv").write_text(
        "Email,Lines with CC\na@x.jp,4200\n", encoding="utf-8", newline="\n")
    card = _preview_detail_card(cfg, input_dir, tmp_path)
    assert '<th class="num">LoC</th>' in card
    assert "時点の code-analytics" not in card


def test_team_summary_excludes_unset(cfg, make_input, tmp_path):
    from seat_analyzer.report.format import _group_summary_rows

    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 30.0, net=0.0), spend_row("b@x.jp", 20.0, net=0.0)]},
        members=["a@x.jp,Premium", "b@x.jp,Premium"],
    )
    (input_dir / "members-info.csv").write_text(
        "email,部署,チーム,職種,備考\n"
        "a@x.jp,開発,基盤,,\n"
        "b@x.jp,営業,,,\n",  # b はチーム未設定
        encoding="utf-8",
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    # チーム軸: include_unset=False で（未設定）が消える／部署軸: 残る
    team_groups = [r["group"] for r in _group_summary_rows(result.users, result.summary, "team", include_unset=False)]
    dept_groups = [r["group"] for r in _group_summary_rows(result.users, result.summary, "department")]
    assert "（未設定）" not in team_groups and "基盤" in team_groups
    assert "（未設定）" not in dept_groups   # この例では部署は両者設定済み

    write_details(result, tmp_path / "details.md")
    write_html(result, tmp_path / "dashboard.html")
    md = (tmp_path / "details.md").read_text(encoding="utf-8")
    # チーム別サマリ見出し以降に（未設定）が出ない
    team_section = md.split("## チーム別サマリ")[1].split("##")[0]
    assert "（未設定）" not in team_section


def test_group_summary_includes_prorated_loc():
    from seat_analyzer.report.format import _group_summary_rows
    from seat_analyzer.report.markdown import _group_summary_md
    users = pd.DataFrame([
        {"email": "a@x.jp", "current_seat": "premium", "status": "現状維持",
         "api_cost_usd": 100.0, "billed_extra_usd": 0.0, "monthly_saving_usd": None,
         "team": "基盤", "loc_with_cc": 1000},
        {"email": "b@x.jp", "current_seat": "premium", "status": "現状維持",
         "api_cost_usd": 50.0, "billed_extra_usd": 0.0, "monthly_saving_usd": None,
         "team": "基盤; SRE", "loc_with_cc": 400},  # 兼務 → LoC も 1/2 ずつ按分
    ])
    summary = {"seat_price_standard_usd": 25.0, "seat_price_premium_usd": 125.0}
    by = {r["group"]: r for r in _group_summary_rows(users, summary, "team")}
    assert round(by["基盤"]["loc"]) == 1200   # 1000 + 400*0.5
    assert round(by["SRE"]["loc"]) == 200     # 400*0.5
    md = _group_summary_md(users, summary, "team", "チーム別サマリ")
    assert "LoC" in md and "1,200" in md
