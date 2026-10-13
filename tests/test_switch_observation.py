"""主と副で揃った累積時点から作る区間の観測（Step 49）。"""

from dataclasses import replace
from datetime import date

import pytest

from seat_analyzer.analyze import analyze, analyze_org
from seat_analyzer.report import write_all, write_details
from seat_analyzer.report.spaces import multi_space_md, switch_observation_md

from .conftest import SPEND_HEADER, spend_row
from .test_persons import ORG, _cfg

MONTH = "2026-08"
EMAIL = "user-a@example.com"
HEADING = "主と副の切り替え"


def _org(make_input, tmp_path, snapshots, members=None, labels=None, no_net=False):
    """入れ子の合成入力で、月初からの累積ファイルを指定順に置いて分析する。"""
    workspaces = {}
    for name, points in snapshots.items():
        input_dir = make_input(
            {}, members=(members or {}).get(name, [f"{EMAIL},premium"]),
            members_month=MONTH, org=ORG, workspace=name,
        )
        directory = input_dir / ORG / name / "spend"
        directory.mkdir(parents=True, exist_ok=True)
        for index, (day, rows) in enumerate(points):
            path = directory / f"spend-report-x{index}-{MONTH}-01-to-{MONTH}-{day:02d}.csv"
            text = SPEND_HEADER + "\n" + "\n".join(rows) + "\n"
            if no_net:
                text = "\n".join(line.rsplit(",", 1)[0] for line in text.splitlines()) + "\n"
            path.write_text(text, encoding="utf-8", newline="\n")
        workspaces[name] = {"primary": True} if name == "main" else {"fixed_seat": "premium"}
        workspaces[name]["label"] = (labels or {}).get(name, name)
    cfg = _cfg(tmp_path, workspaces)
    # 月次の需要の基準が違っても観測表は API 換算のままであることを全テストで見る
    cfg["cost_basis"] = "net_spend"
    return analyze_org(input_dir / ORG, MONTH, cfg, ORG)


def _points(*days):
    """日付ごとに需要と実課金が増える1人ぶんの累積。"""
    return [(day, [spend_row(EMAIL, day * 10.0, net=day * 1.0)]) for day in days]


def _outputs(org, path):
    """全5成果物を既存の書き込み経路で作り、バイト列で返す。"""
    return {key: file.read_bytes() for key, file in write_all(org, path).items()}


def test_intervals_rendered_with_days_deltas_and_report_link(make_input, tmp_path):
    org = _org(make_input, tmp_path, {"main": _points(3, 9), "second": _points(3, 9)},
               labels={"main": "主スペース", "second": "副スペース"})
    paths = write_all(org, tmp_path / "out")
    md = paths["details"].read_text(encoding="utf-8")
    assert f"## {HEADING}（副スペース）" in md
    assert "スナップショット: 〜08-03 / 〜08-09（主と副で終了日が一致したもの）" in md
    assert "使っていない時点" not in md
    assert f"| {EMAIL} | 月初→〜08-03 | 3 | +$30.00 | +$30.00 | +$3.00 |" in md
    assert f"| {EMAIL} | 〜08-03→〜08-09 | 6 | +$60.00 | +$60.00 | +$6.00 |" in md
    # snapshot_diff の最低日数の閾値は、この観測表には適用しない
    assert len(org.persons.switch_observations[0].rows) == 2
    report = paths["markdown"].read_text(encoding="utf-8")
    expected = (f"- 区間ごとの主と副の需要は details-202608-{ORG}.md の「{HEADING}」にあります")
    assert expected in report
    assert report.index("- 人別の利用（全員1人1行）") < report.index(expected)
    assert md.index("## 月中の利用推移（スナップショット差分）（副スペース）") < md.index(
        f"## {HEADING}（副スペース）")
    legend = switch_observation_md(org)[0].split("\n\n")[-1].splitlines()
    assert len(legend) == 3
    assert all(line.startswith("- ") for line in legend)
    assert "上限に当たった時刻や切り替えた時刻は分からない" in legend[1]
    assert "区間ごとにシートを持っていたかは示さない" in legend[2]


@pytest.mark.parametrize("main_days,secondary_days", [
    ((3, 9, 31), (31,)), ((3, 31), (9, 31)), ((3, 9), (10, 31)),
])
def test_fewer_than_two_common_dates_has_no_section_or_link(
    make_input, tmp_path, main_days, secondary_days,
):
    org = _org(make_input, tmp_path,
               {"main": _points(*main_days), "second": _points(*secondary_days)})
    assert org.persons.switch_observations == ()
    assert switch_observation_md(org) == []
    assert HEADING not in multi_space_md(org)
    out = tmp_path / "details.md"
    write_details(org, out)
    assert HEADING not in out.read_text(encoding="utf-8")


def test_unmatched_dates_ignored_and_explained(make_input, tmp_path):
    org = _org(make_input, tmp_path,
               {"main": _points(3, 6, 31), "second": _points(3, 9, 31)})
    observation = org.persons.switch_observations[0]
    assert observation.ends == (date(2026, 8, 3), date(2026, 8, 31))
    assert [r.days for r in observation.rows] == [3, 28]
    assert observation.rows[1].primary_demand_usd == 280.0
    md = switch_observation_md(org)[0]
    assert "スナップショット: 〜08-03 / 〜08-31" in md
    assert ("使っていない時点: main 〜08-06（secondに使える同じ日付が無い） / "
            "second 〜08-09（mainに使える同じ日付が無い）") in md
    assert "→〜08-06" not in md and "→〜08-09" not in md


@pytest.mark.parametrize("duplicate_workspace", ["main", "second"])
@pytest.mark.parametrize("enough_common", [False, True])
def test_duplicate_dates_excluded(make_input, tmp_path, duplicate_workspace, enough_common):
    days = (3, 9, 31) if enough_common else (9, 31)
    snapshots = {"main": _points(*days), "second": _points(*days)}
    snapshots[duplicate_workspace].append((9, [spend_row(EMAIL, 987.0, net=42.0)]))
    org = _org(make_input, tmp_path, snapshots)
    if not enough_common:
        assert not org.persons.switch_observations
        assert switch_observation_md(org) == []
        assert HEADING not in multi_space_md(org)
        return
    observation = org.persons.switch_observations[0]
    assert observation.ends == (date(2026, 8, 3), date(2026, 8, 31))
    assert observation.rows[1].primary_demand_usd == 280.0
    assert observation.rows[1].secondary_demand_usd == 280.0
    assert observation.rows[1].primary_billed_usd == 28.0
    md = switch_observation_md(org)[0]
    assert f"{duplicate_workspace} 〜08-09（同じ日付のファイルが複数ある）" in md
    assert "→〜08-09" not in md


def test_eligible_people_include_zero_usage_and_any_primary_seat(make_input, tmp_path):
    members = {
        "main": ["user-z@example.com,unassigned", f"{EMAIL},standard",
                 "user-b@example.com,premium", "user-c@example.com,premium"],
        "second": ["user-z@example.com,premium", f"{EMAIL},premium",
                   "user-b@example.com,unassigned", "user-d@example.com,premium"],
    }
    org = _org(make_input, tmp_path, {"main": _points(3, 31), "second": _points(3, 31)}, members)
    rows = org.persons.switch_observations[0].rows
    assert [row.email for row in rows] == [EMAIL, EMAIL, "user-z@example.com", "user-z@example.com"]
    assert [(r.start, r.end) for r in rows[:2]] == [
        (None, date(2026, 8, 3)), (date(2026, 8, 3), date(2026, 8, 31))]
    assert all((r.primary_demand_usd, r.secondary_demand_usd, r.primary_billed_usd) == (0, 0, 0)
               for r in rows[2:])
    md = switch_observation_md(org)[0]
    assert "| user-z@example.com | 月初→〜08-03 | 3 | +$0.00 | +$0.00 | +$0.00 |" in md
    assert all(email not in md for email in (
        "user-b@example.com", "user-c@example.com", "user-d@example.com"))


def test_no_eligible_people_has_no_outputs(make_input, tmp_path):
    org = _org(make_input, tmp_path, {"main": _points(3, 31), "second": _points(3, 31)},
               {"second": [f"{EMAIL},unassigned"]})
    assert org.persons.switch_observations == ()
    outputs = _outputs(org, tmp_path / "out")
    assert all(HEADING.encode() not in text for text in outputs.values())


def test_unrounded_cumulative_differences_and_existing_latest_delta(make_input, tmp_path):
    # 丸めた累積の差は1.01だが、未丸め累積の差を丸めると1.00になる
    points = [(3, [spend_row(EMAIL, 1.004, net=0.004)]),
              (31, [spend_row(EMAIL, 2.006, net=1.006)])]
    org = _org(make_input, tmp_path, {"main": points, "second": points})
    row = org.persons.switch_observations[0].rows[1]
    snap = org.workspaces["main"].snapshot
    assert row.primary_demand_usd == row.secondary_demand_usd == row.primary_billed_usd == 1.0
    assert row.primary_demand_usd == snap["rows"][0]["latest_delta"]
    assert round(snap["rows"][0]["cum"][1] - snap["rows"][0]["cum"][0], 2) == 1.01
    assert snap["cumulative_snapshots"][0]["end"] == date(2026, 8, 3)
    assert snap["cumulative_snapshots"][0]["days"] == 3
    assert snap["cumulative_snapshots"][0]["cum"][EMAIL] == pytest.approx(1.004, abs=0.00005)


def test_sum_rows_missing_email_and_negative_delta(make_input, tmp_path):
    org = _org(make_input, tmp_path, {
        "main": [(3, [spend_row(EMAIL, 10, net=2), spend_row(EMAIL, 20, net=3)]),
                 (9, [spend_row("other@example.com", 1, net=0)]),
                 (31, [spend_row(EMAIL, 50, net=8)])],
        "second": [(3, [spend_row(EMAIL, 4, net=0), spend_row(EMAIL, 6, net=0)]),
                   (9, [spend_row(EMAIL, 12, net=0)]),
                   (31, [spend_row(EMAIL, 15, net=0)])],
    })
    rows = org.persons.switch_observations[0].rows
    assert [(r.primary_demand_usd, r.secondary_demand_usd, r.primary_billed_usd) for r in rows] == [
        (30, 10, 5), (-30, 2, -5), (50, 3, 8)]
    assert f"| {EMAIL} | 〜08-03→〜08-09 | 6 | -$30.00 | +$2.00 | -$5.00 |" in (
        switch_observation_md(org)[0])


def test_missing_net_spend_is_zero(make_input, tmp_path):
    org = _org(make_input, tmp_path, {"main": _points(3, 31), "second": _points(3, 31)},
               no_net=True)
    assert all(row.primary_billed_usd == 0 for row in org.persons.switch_observations[0].rows)


def test_multiple_secondaries_follow_workspace_name_order(make_input, tmp_path):
    org = _org(make_input, tmp_path, {
        "main": _points(3, 9, 31), "second-z": _points(3, 31), "second-a": _points(9, 31),
    })
    assert [o.workspace for o in org.persons.switch_observations] == ["second-a", "second-z"]
    out = tmp_path / "details.md"
    write_details(org, out)
    md = out.read_text(encoding="utf-8")
    assert md.index(f"## {HEADING}（second-a）") < md.index(f"## {HEADING}（second-z）")
    assert multi_space_md(org).count("- 区間ごとの主と副の需要は") == 1


def test_unchanged_outputs_outside_details_and_report(make_input, tmp_path):
    org = _org(make_input, tmp_path, {"main": _points(3, 31), "second": _points(3, 31)})
    baseline = replace(org, persons=replace(org.persons, switch_observations=()))
    before, after = _outputs(baseline, tmp_path / "before"), _outputs(org, tmp_path / "after")
    assert {key for key in before if before[key] != after[key]} == {"markdown", "details"}
    for key in ("csv", "usage", "html"):
        assert HEADING.encode() not in after[key]


def test_single_workspace_unchanged(make_input, tmp_path, cfg):
    input_dir = make_input({MONTH: [spend_row(EMAIL, 20, net=0)]},
                           members=[f"{EMAIL},premium"], members_month=MONTH, org=ORG)
    result = analyze(input_dir / ORG, MONTH, cfg, ORG)
    org = analyze_org(input_dir / ORG, MONTH, cfg, ORG)
    assert org.persons.switch_observations == ()
    before, after = _outputs(result, tmp_path / "before"), _outputs(org, tmp_path / "after")
    assert before == after
    assert all(HEADING.encode() not in text for text in after.values())
