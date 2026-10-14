"""V1 判定の入力の欠け（判定窓の欠月・部分月・副の欠け・シート履歴）のテスト。

デフォルト: Standard $25 / Premium $125、hysteresis=2ヶ月、最低削減額 $20/月。
判定窓は対象月から暦で遡る hysteresis_months か月。spend_row(net=0.0) で需要の小さい
Premium ユーザは毎月 Standard 推奨（削減 $100）になるので、判定窓が揃っていれば
「変更推奨」、揃っていなければ「要観察（データ蓄積待ち）」になる。
"""

from pathlib import Path

import pytest
import yaml

from seat_analyzer import decision_evidence, seat_changes
from seat_analyzer.analyze import (
    STATUS_CHANGE,
    STATUS_KEEP,
    STATUS_WATCH_WAIT,
    analyze,
    analyze_org,
    preview,
    preview_org,
)
from seat_analyzer.analyze.pipeline import consecutive_complete_months, judgment_window
from seat_analyzer.config import load_config
from seat_analyzer.report.html import write_html, write_preview_html
from seat_analyzer.report.markdown import write_markdown, write_preview_markdown
from tests.conftest import SPEND_HEADER, spend_row

ORG = "org-x"


def _light(email: str) -> str:
    """需要の小さい行（Premium なら毎月 Standard 推奨・削減 $100）。"""
    return spend_row(email, 20.0, net=0.0)


def _heavy(email: str) -> str:
    """需要の大きい行（Premium のままが推奨＝現状維持）。"""
    return spend_row(email, 500.0, net=0.0)


def _user(result, email):
    return result.users.set_index("email").loc[email]


def _write_spend(base: Path, name: str, rows: list[str]) -> None:
    """spend/ にファイル名を指定して置く（期間付き・単日日付の命名を作るため）。"""
    directory = base / "spend"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        SPEND_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8", newline="\n")


def _window_notes(result) -> list[str]:
    return [w for w in result.warnings if w.startswith("判定窓")]


def _nested_cfg(tmp_path: Path) -> dict:
    """2 workspace の組織（副は Premium 固定）の上書き設定をロードする。"""
    path = tmp_path / "nested-judgment.yaml"
    path.write_text(yaml.safe_dump({"organizations": {ORG: {"workspaces": {
        "main": {"primary": True},
        "second": {"fixed_seat": "premium"},
    }}}}), encoding="utf-8", newline="\n")
    return load_config(str(path))


# ------------------------------------------------------------------ 規則の関数


def test_judgment_window_counts_back_calendar_months():
    assert judgment_window("2026-06", 2) == ("2026-05", "2026-06")
    assert judgment_window("2026-01", 3) == ("2025-11", "2025-12", "2026-01")
    assert judgment_window("2026-06", 1) == ("2026-06",)


def test_consecutive_complete_months_stops_at_gap_and_partial_month():
    months = ["2026-03", "2026-05", "2026-06"]
    complete = dict.fromkeys(months, True)
    # 2026-04 が欠月なので 2026-03 は数えない
    assert consecutive_complete_months("2026-06", months, complete) == ("2026-05", "2026-06")
    # 部分月でも途切れる
    assert consecutive_complete_months(
        "2026-06", months, {**complete, "2026-05": False}) == ("2026-06",)
    # 対象月そのものが欠月・部分月なら何も数えない
    assert consecutive_complete_months("2026-07", months, complete) == ()
    assert consecutive_complete_months(
        "2026-06", months, {**complete, "2026-06": False}) == ()
    # 完全性が分からない月は全月と確かめられないので数えない
    assert consecutive_complete_months("2026-06", months, {}) == ()


# ------------------------------------------------------------------ 要件 1: 欠月


@pytest.mark.parametrize(("months", "hysteresis", "expected"), [
    (("2026-04", "2026-05", "2026-06"), 2, ["2026-05", "2026-06"]),
    (("2026-04", "2026-06"), 2, ["2026-06"]),
    (("2026-06",), 2, ["2026-06"]),
    (("2025-10", "2025-11", "2025-12", "2026-01"), 3,
     ["2025-11", "2025-12", "2026-01"]),
])
def test_summary_judgment_months(cfg, make_input, months, hysteresis, expected):
    cfg["decision"]["hysteresis_months"] = hysteresis
    input_dir = make_input(
        {m: [_light("user@example.com")] for m in months},
        members=["user@example.com,premium"], members_month=months[-1],
    )
    result = analyze(input_dir, months[-1], cfg, org=ORG)
    assert result.summary["judgment_months"] == expected
    assert result.summary["months_used"] == list(months)


@pytest.mark.parametrize(("writer", "suffix", "expected"), [
    (write_markdown, "md", "| 判定に使用した月 | 2026-05, 2026-06（ヒステリシス 2 ヶ月） |"),
    (write_html, "html", "判定に使用した月: 2026-05, 2026-06（2ヶ月ヒステリシス）。"),
])
def test_single_judgment_months_display(cfg, make_input, tmp_path, writer, suffix, expected):
    input_dir = make_input(
        {m: [_light("user@example.com")] for m in ("2026-04", "2026-05", "2026-06")},
        members=["user@example.com,premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, org=ORG)
    path = tmp_path / f"report.{suffix}"
    writer(result, path)
    assert expected in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(("writer", "suffix"), [(write_markdown, "md"), (write_html, "html")])
@pytest.mark.parametrize("second_months", [("2026-06",), ("2026-04", "2026-06")])
def test_multi_judgment_months_display(make_input, tmp_path, writer, suffix, second_months):
    cfg = _nested_cfg(tmp_path)
    settings = cfg["organizations"][ORG]["workspaces"]
    settings["main"]["label"] = "主スペース"
    settings["second"]["label"] = "副スペース"
    input_dir = make_input(
        {m: [_light("user@example.com")] for m in ("2026-04", "2026-05", "2026-06")},
        members=["user@example.com,premium"], org=ORG, workspace="main",
    )
    make_input(
        {m: [_light("user@example.com")] for m in second_months},
        members=["user@example.com,premium"], org=ORG, workspace="second",
    )
    result = analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    path = tmp_path / f"report.{suffix}"
    writer(result, path)
    line = next(line for line in path.read_text(encoding="utf-8").splitlines()
                if "判定に使用した月" in line)
    assert "主スペース: 2026-05, 2026-06 / 副スペース: 2026-06" in line
    assert "2026-04" not in line


@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize(("writer", "suffix", "command", "ending"), [
    (write_preview_markdown, "md", "`analyze`", ""),
    (write_preview_html, "html", "analyze", "。"),
])
def test_preview_decision_note_uses_setting(cfg, make_input, tmp_path,
                                          multi, writer, suffix, command, ending):
    if multi:
        cfg = _nested_cfg(tmp_path)
    cfg["decision"]["hysteresis_months"] = 3
    input_dir = make_input(
        {"2026-06": [_light("user@example.com")]},
        members=["user@example.com,premium"], org=ORG if multi else None,
        workspace="main" if multi else None,
    )
    if multi:
        make_input({"2026-06": [_light("user@example.com")]},
                   members=["user@example.com,premium"], org=ORG, workspace="second")
        result = preview_org(input_dir / ORG, "2026-06", cfg, 15, ORG)
        assert all(r.summary["hysteresis_months"] == 3 for r in result.workspaces.values())
    else:
        result = preview(input_dir, "2026-06", cfg, 15, ORG)
        assert result.summary["hysteresis_months"] == 3
    path = tmp_path / f"preview.{suffix}"
    writer(result, path)
    text = path.read_text(encoding="utf-8")
    expected = (
        "変更推奨・ヒステリシス判定は行いません。確定判断は全月データ3ヶ月分"
        f"での正式分析（{command}）で行ってください{ending}"
    )
    assert (f"- {expected}\n" if suffix == "md" else f"<li>{expected}</li>") in text
    assert "全月データ2ヶ月分" not in text


def test_missing_month_in_window_holds_change(cfg, make_input):
    input_dir = make_input(
        {"2026-04": [_light("light@x.jp"), _heavy("heavy@x.jp")],
         "2026-06": [_light("light@x.jp"), _heavy("heavy@x.jp")]},
        members=["light@x.jp,premium", "heavy@x.jp,premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    light = _user(result, "light@x.jp")
    # 状態だけが保留になり、推奨・確度・削減見込みは変わらない
    assert light["status"] == STATUS_WATCH_WAIT
    assert light["recommended_seat"] == "standard"
    assert light["confidence"] == "高"
    assert light["monthly_saving_usd"] == 100.0
    # 推奨が現シートと同じ人は保留しない
    assert _user(result, "heavy@x.jp")["status"] == STATUS_KEEP
    assert _window_notes(result) == [(
        "判定窓（2026-05〜2026-06）のうち 2026-05 のスペンドレポートが無いため、変更推奨を"
        "出していません（推奨が現シートと違うユーザは「要観察（データ蓄積待ち）」）"
    )]


def test_months_before_oldest_spend_are_not_a_gap(cfg, make_input):
    # 最古の月より前が無いのはデータの蓄積が足りないだけ（従来の「月数が足りない」と同じ）
    input_dir = make_input({"2026-06": [_light("light@x.jp")]},
                           members=["light@x.jp,premium"])
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    assert _user(result, "light@x.jp")["status"] == STATUS_WATCH_WAIT
    assert _window_notes(result) == []


def test_gap_outside_window_keeps_change(cfg, make_input):
    input_dir = make_input(
        {"2026-03": [_light("light@x.jp")], "2026-05": [_light("light@x.jp")],
         "2026-06": [_light("light@x.jp")]},
        members=["light@x.jp,premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    assert _user(result, "light@x.jp")["status"] == STATUS_CHANGE
    assert _window_notes(result) == []


# ------------------------------------------------------------------ 要件 2 (a): 主の部分月


def test_partial_target_month_holds_change(cfg, make_input):
    input_dir = make_input({"2026-05": [_light("light@x.jp")]},
                           members=["light@x.jp,premium"])
    _write_spend(input_dir, "spend-report-uuid-2026-06-01-to-2026-06-20.csv",
                 [_light("light@x.jp")])
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    assert _user(result, "light@x.jp")["status"] == STATUS_WATCH_WAIT
    assert result.complete == {"2026-05": True, "2026-06": False}
    assert (
        "spend-report-uuid-2026-06-01-to-2026-06-20.csv: 2026-06 は部分月データ"
        "（06-01〜06-20 の 20日分）のため、この月が判定窓に入るあいだは変更推奨を出しません。"
        "月中の一次判断には --preview を利用してください"
    ) in result.warnings


def test_partial_past_month_in_window_holds_change(cfg, make_input):
    input_dir = make_input({"2026-06": [_light("light@x.jp")]},
                           members=["light@x.jp,premium"])
    _write_spend(input_dir, "spend-report-uuid-2026-05-01-to-2026-05-15.csv",
                 [_light("light@x.jp")])
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    assert _user(result, "light@x.jp")["status"] == STATUS_WATCH_WAIT


def test_partial_month_outside_window_keeps_change(cfg, make_input):
    input_dir = make_input(
        {"2026-05": [_light("light@x.jp")], "2026-06": [_light("light@x.jp")]},
        members=["light@x.jp,premium"],
    )
    _write_spend(input_dir, "spend-report-uuid-2026-04-01-to-2026-04-10.csv",
                 [_light("light@x.jp")])
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    assert _user(result, "light@x.jp")["status"] == STATUS_CHANGE
    assert (
        "spend-report-uuid-2026-04-01-to-2026-04-10.csv: 2026-04 は部分月データ"
        "（04-01〜04-10 の 10日分）です。判定窓の外の月なので変更推奨の判定には使っていません"
    ) in result.warnings


def test_single_date_spend_is_not_treated_as_partial(cfg, make_input):
    # 期間の無い命名（単日日付・月のみ）は従来どおり全月として扱う
    input_dir = make_input({"2026-05": [_light("light@x.jp")]},
                           members=["light@x.jp,premium"])
    _write_spend(input_dir, "spend-report-uuid-2026-06-30.csv", [_light("light@x.jp")])
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    assert result.complete == {"2026-05": True, "2026-06": True}
    assert _user(result, "light@x.jp")["status"] == STATUS_CHANGE


def test_complete_is_set_without_decision_context(cfg, make_input):
    input_dir = make_input({"2026-05": [_light("light@x.jp")]},
                           members=["light@x.jp,premium"])
    _write_spend(input_dir, "spend-report-uuid-2026-06-01-to-2026-06-20.csv",
                 [_light("light@x.jp")])
    plain = analyze(input_dir, "2026-06", cfg, org="org-a")
    with_context = analyze(input_dir, "2026-06", cfg, org="org-a", decision_context=True)
    assert plain.decision_context is None
    assert plain.complete == with_context.complete == with_context.decision_context.complete


# ------------------------------------------------------------------ 要件 2 (b): 副の欠け


def _nested_input(make_input, *, main_months: tuple[str, ...],
                  second_months: tuple[str, ...]) -> Path:
    """主に2人（a は副にもアカウントを持つ・b は主だけ）、副に a がいる組織。"""
    input_dir = make_input(
        {month: [_light("a@x.jp"), _light("b@x.jp")] for month in main_months},
        members=["a@x.jp,premium", "b@x.jp,premium"], org=ORG, workspace="main",
    )
    make_input(
        {month: [spend_row("a@x.jp", 1.0, net=0.0)] for month in second_months},
        members=["a@x.jp,premium"], org=ORG, workspace="second",
    )
    return input_dir


def test_secondary_missing_month_holds_every_primary_user(make_input, tmp_path):
    input_dir = _nested_input(make_input, main_months=("2026-04", "2026-05", "2026-06"),
                              second_months=("2026-04", "2026-06"))
    org = analyze_org(input_dir / ORG, "2026-06", _nested_cfg(tmp_path), ORG)
    main = org.workspaces["main"]
    # 副にアカウントの無い b も保留する（組織単位）
    assert _user(main, "a@x.jp")["status"] == STATUS_WATCH_WAIT
    assert _user(main, "b@x.jp")["status"] == STATUS_WATCH_WAIT
    assert _window_notes(main) == [(
        "判定窓（2026-05〜2026-06）で副 workspace のスペンドレポートがそろっていないため、"
        "変更推奨を出していません（second は 2026-05 が無い）"
    )]
    # workspace 自身の完全性は副と合わせない
    assert main.complete == {"2026-04": True, "2026-05": True, "2026-06": True}


def test_secondary_partial_month_holds_every_primary_user(make_input, tmp_path):
    input_dir = _nested_input(make_input, main_months=("2026-05", "2026-06"),
                              second_months=("2026-05",))
    _write_spend(input_dir / ORG / "second",
                 "spend-report-uuid-2026-06-01-to-2026-06-20.csv",
                 [spend_row("a@x.jp", 1.0, net=0.0)])
    org = analyze_org(input_dir / ORG, "2026-06", _nested_cfg(tmp_path), ORG)
    main = org.workspaces["main"]
    assert _user(main, "b@x.jp")["status"] == STATUS_WATCH_WAIT
    assert _window_notes(main) == [(
        "判定窓（2026-05〜2026-06）で副 workspace のスペンドレポートがそろっていないため、"
        "変更推奨を出していません（second は 2026-06 が部分月）"
    )]
    assert main.complete == {"2026-05": True, "2026-06": True}
    assert org.workspaces["second"].complete == {"2026-05": True, "2026-06": False}


def test_months_before_secondary_started_are_not_a_gap(make_input, tmp_path):
    input_dir = _nested_input(make_input, main_months=("2026-05", "2026-06"),
                              second_months=("2026-06",))
    org = analyze_org(input_dir / ORG, "2026-06", _nested_cfg(tmp_path), ORG)
    main = org.workspaces["main"]
    assert _user(main, "a@x.jp")["status"] == STATUS_CHANGE
    assert _user(main, "b@x.jp")["status"] == STATUS_CHANGE
    assert _window_notes(main) == []


def test_allowed_missing_secondary_month_is_not_a_gap(make_input, tmp_path):
    # --allow-missing-workspace で需要 0 とした対象月は全月の観測（欠けに当たらない）
    input_dir = _nested_input(make_input, main_months=("2026-05", "2026-06"),
                              second_months=("2026-05",))
    org = analyze_org(input_dir / ORG, "2026-06", _nested_cfg(tmp_path), ORG,
                      allow_missing=("second",))
    main = org.workspaces["main"]
    assert org.workspaces["second"].complete == {"2026-05": True, "2026-06": True}
    assert _user(main, "b@x.jp")["status"] == STATUS_CHANGE
    assert _window_notes(main) == []


# ------------------------------------------------------------------ 要件 7: 完全性の値の範囲


def test_secondary_partial_month_does_not_leak_into_v2_or_payout(make_input, tmp_path):
    cfg = _nested_cfg(tmp_path)
    input_dir = _nested_input(make_input, main_months=("2026-05", "2026-06"),
                              second_months=("2026-05", "2026-06"))

    def run():
        org = analyze_org(input_dir / ORG, "2026-06", cfg, ORG, decision_context=True)
        changes = seat_changes.detect_from_input(input_dir / ORG / "main", cfg)
        rows = {row.email: row for row in decision_evidence.evaluate_org(org, changes, cfg)}
        payout = {j.email: j for j in org.persons.payout}
        return org, rows, payout

    full_org, full_rows, full_payout = run()
    second_spend = input_dir / ORG / "second" / "spend"
    (second_spend / "spend_2026-06.csv").rename(
        second_spend / "spend-report-uuid-2026-06-01-to-2026-06-20.csv")
    partial_org, partial_rows, partial_payout = run()

    # 主の V1 判定だけが副の部分月で保留になる
    assert _user(full_org.workspaces["main"], "b@x.jp")["status"] == STATUS_CHANGE
    assert _user(partial_org.workspaces["main"], "b@x.jp")["status"] == STATUS_WATCH_WAIT
    # 主の V2 の材料は主自身の完全性のまま
    assert partial_org.workspaces["main"].decision_context.complete == {
        "2026-05": True, "2026-06": True}
    # 副を持たない人の V2 の行と払い出し判定は変わらない
    assert partial_rows["b@x.jp"] == full_rows["b@x.jp"]
    assert partial_payout["b@x.jp"] == full_payout["b@x.jp"]


# ------------------------------------------------------------------ 要件 3: シート履歴


def test_join_on_first_day_of_target_month_is_held(cfg, make_input, write_member_snapshots):
    # 9/1 加入。8 月の spend に行が無い（需要 0）ので、従来は加入1か月目で変更推奨が出た
    input_dir = make_input({
        "2026-08": [_light("old@x.jp")],
        "2026-09": [_light("old@x.jp"), _light("new@x.jp"), _heavy("busy@x.jp")],
    })
    write_member_snapshots(input_dir, {
        "2026-08-29": ["old@x.jp,premium"],
        "2026-09-01": ["old@x.jp,premium", "new@x.jp,premium", "busy@x.jp,premium"],
        "2026-09-30": ["old@x.jp,premium", "new@x.jp,premium", "busy@x.jp,premium"],
    })
    result = analyze(input_dir, "2026-09", cfg, org="org-a")
    assert _user(result, "new@x.jp")["status"] == STATUS_WATCH_WAIT
    assert _user(result, "new@x.jp")["recommended_seat"] == "standard"
    assert _user(result, "old@x.jp")["status"] == STATUS_CHANGE
    # 加入したが推奨が現シートと同じ人は保留の対象ではない（警告にも載せない）
    assert _user(result, "busy@x.jp")["status"] == STATUS_KEEP
    assert _window_notes(result) == [(
        "判定窓のあいだに加入・シート種別の変更があったユーザ 1 名は変更推奨を出していません"
        "（「要観察（データ蓄積待ち）」）: ['new@x.jp']"
    )]


def test_seat_change_in_window_is_held(cfg, make_input, write_member_snapshots):
    input_dir = make_input({
        "2026-08": [_light("switch@x.jp"), _light("steady@x.jp")],
        "2026-09": [_light("switch@x.jp"), _light("steady@x.jp")],
    })
    write_member_snapshots(input_dir, {
        "2026-07-31": ["switch@x.jp,standard", "steady@x.jp,premium"],
        "2026-08-15": ["switch@x.jp,premium", "steady@x.jp,premium"],
        "2026-09-30": ["switch@x.jp,premium", "steady@x.jp,premium"],
    })
    result = analyze(input_dir, "2026-09", cfg, org="org-a")
    assert _user(result, "switch@x.jp")["status"] == STATUS_WATCH_WAIT
    # 判定窓の始まり以前から同じ種別で居る人は従来どおり
    assert _user(result, "steady@x.jp")["status"] == STATUS_CHANGE


def test_without_members_before_window_start_join_cannot_be_seen(
        cfg, make_input, write_member_snapshots):
    # 保証の範囲の外: 判定窓の始まり以前の members が無く、範囲内のファイルすべてに
    # 同じ種別で載っている人は、窓の途中で加入していても今の挙動（変更推奨）のまま
    input_dir = make_input({
        "2026-08": [_light("late@x.jp")],
        "2026-09": [_light("late@x.jp")],
    })
    write_member_snapshots(input_dir, {
        "2026-08-15": ["late@x.jp,premium"],
        "2026-09-30": ["late@x.jp,premium"],
    })
    result = analyze(input_dir, "2026-09", cfg, org="org-a")
    assert _user(result, "late@x.jp")["status"] == STATUS_CHANGE
    assert _window_notes(result) == []


def test_hysteresis_of_one_month_looks_from_latest_file_before_month_start(
        cfg, make_input, write_member_snapshots):
    cfg["decision"]["hysteresis_months"] = 1
    input_dir = make_input({"2026-09": [_light("a@x.jp"), _light("b@x.jp")]})
    write_member_snapshots(input_dir, {
        # 範囲の始まり（9/1 以前で最新）より前のファイルは見ない
        "2026-08-10": ["b@x.jp,premium"],
        "2026-08-29": ["a@x.jp,premium"],
        "2026-09-15": ["a@x.jp,premium", "b@x.jp,premium"],
        "2026-09-30": ["a@x.jp,premium", "b@x.jp,premium"],
    })
    result = analyze(input_dir, "2026-09", cfg, org="org-a")
    assert _user(result, "a@x.jp")["status"] == STATUS_CHANGE
    assert _user(result, "b@x.jp")["status"] == STATUS_WATCH_WAIT


def test_all_files_on_the_start_date_are_checked(cfg, make_input, write_member_snapshots):
    # 範囲の始まりと同じ代表日のファイル（月のみの命名は月末日）はすべて見る
    input_dir = make_input({
        "2026-08": [_light("x@x.jp")],
        "2026-09": [_light("x@x.jp")],
    })
    write_member_snapshots(input_dir, {
        "2026-07-31": ["other@x.jp,premium"],
        "2026-09-30": ["x@x.jp,premium", "other@x.jp,premium"],
    })
    (input_dir / "members" / "members_2026-07.csv").write_text(
        "Email,Seat Type\nx@x.jp,premium\nother@x.jp,premium\n",
        encoding="utf-8", newline="\n")
    result = analyze(input_dir, "2026-09", cfg, org="org-a")
    assert _user(result, "x@x.jp")["status"] == STATUS_WATCH_WAIT
    assert _user(result, "other@x.jp")["status"] == STATUS_CHANGE


def test_files_after_the_adopted_members_are_not_checked(
        cfg, make_input, write_member_snapshots):
    input_dir = make_input({
        "2026-08": [_light("p@x.jp")],
        "2026-09": [_light("p@x.jp")],
    })
    write_member_snapshots(input_dir, {
        "2026-07-31": ["p@x.jp,premium"],
        "2026-09-30": ["p@x.jp,premium"],
        # 対象月の採用ファイル（9/30）より後のファイルは範囲の外
        "2026-10-03": ["q@x.jp,premium"],
    })
    result = analyze(input_dir, "2026-09", cfg, org="org-a")
    assert result.sources["members"].endswith("members-snap-2026-09-30.csv")
    assert _user(result, "p@x.jp")["status"] == STATUS_CHANGE
