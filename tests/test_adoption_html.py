"""adoption-report.html（Claude Code の活用と GitHub の成果のレポート）のテスト。

表示の材料（`_report_view` が返す値）で値・変化・並びを確かめ、HTML のバイト列では
エスケープ・改行・外部の読み込み・コメントの有無を確かめる。行はすべて合成。
"""

import dataclasses
import re
from pathlib import Path

import pytest

from seat_analyzer.adoption import (
    GithubData,
    GithubId,
    GroupMonth,
    GroupScope,
    PersonMonth,
)
from seat_analyzer.report import (
    ADOPTION_PEOPLE_COLUMNS,
    ADOPTION_TEAMS_COLUMNS,
    write_adoption_report,
)
from seat_analyzer.report.adoption_html import (
    _BOTH,
    _CHARTS,
    _COUNT,
    _RATIO,
    _SCATTER_Y_COLUMN,
    _SCATTERS,
    _SHARE,
    _TILES,
    BLOCKS,
    COLUMNS,
    METRICS,
    Partial,
    _bucket,
    _change,
    _delta,
    _line_chart,
    _month_heads,
    _month_range,
    _report_view,
    _trend3,
    _usd_per_line,
)
from seat_analyzer.report.html import _asset

DOMAIN = "corp.example"
LATEST = "2026-06"
SIX = ("2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06")
THREE = ("2026-04", "2026-05", "2026-06")


def _person(**changes) -> PersonMonth:
    """login の人の ok の月の1行（changes で列を差し替える）。"""
    base = PersonMonth(
        email=f"amy@{DOMAIN}", month=LATEST, seat="premium", department="架空開発部",
        team="Orion-Team", role="", spend_complete=True, total_demand_usd=10.0,
        code_demand_usd=8.0, code_requests=3.0, loc_with_cc=50, github_id=GithubId.LOGIN,
        github_data=GithubData.OK, github_cache_complete=True, merged_pr_count=2,
        lead_time_median_hours=1.5, commit_count=3, lines_added_capped=120, large_commits=0,
        outside_org=False, outside_org_months=0,
    )
    values = {field.name: getattr(base, field.name) for field in dataclasses.fields(base)}
    values.update(changes)
    eligible = (
        values["github_id"] is GithubId.LOGIN and values["github_data"] is GithubData.OK
        and values["github_cache_complete"] is True and values["loc_with_cc"] is not None
    )
    changes.setdefault("outside_org", False if eligible else None)
    changes.setdefault("outside_org_months", 0 if eligible else None)
    return dataclasses.replace(base, **changes)


def _no_github_person(**changes) -> PersonMonth:
    """GitHub 分析の無い組織の人の1行。"""
    return _person(**{
        "github_id": None, "github_data": GithubData.NOT_CONFIGURED,
        "github_cache_complete": None, "merged_pr_count": None,
        "lead_time_median_hours": None, "commit_count": None, "lines_added_capped": None,
        "large_commits": None, "outside_org": None, "outside_org_months": None, **changes,
    })


def _group(**changes) -> GroupMonth:
    """ok の月の組織全体の1行（changes で列を差し替える）。"""
    base = GroupMonth(
        scope=GroupScope.ORGANIZATION, name="", month=LATEST, spend_complete=True,
        github_data=GithubData.OK, github_cache_complete=True, seat_holders=4, code_users=3,
        code_users_3m=2, code_demand_usd=400.0, code_demand_median_usd=90.0,
        loc_median_of_users=120.0, github_people=2, merged_pr_count=6,
        pr_additions_median=40.0, lead_time_median_hours=3.0, lead_time_p90_hours=9.0,
        lines_added_capped=800, code_demand_usd_of_github_people=200.0,
        outside_org_people=None,
    )
    return dataclasses.replace(base, **changes)


def _no_github_group(**changes) -> GroupMonth:
    """GitHub 分析の無い組織の集まりの1行。"""
    return _group(**{
        "github_data": GithubData.NOT_CONFIGURED, "github_cache_complete": None,
        "github_people": None, "merged_pr_count": None, "pr_additions_median": None,
        "lead_time_median_hours": None, "lead_time_p90_hours": None,
        "lines_added_capped": None, "code_demand_usd_of_github_people": None, **changes,
    })


def _org(months, **changes) -> list[GroupMonth]:
    """月ごとの組織全体の行。"""
    return [_group(month=month, **changes) for month in months]


def _row_of(table: dict, label: str) -> dict:
    """数値の表のうち、見出しが label の行。"""
    return next(row for row in table["rows"] if row["label"] == label)


def _metric(key: str):
    """キーの指標。"""
    return next(metric for metric in METRICS if metric.key == key)


def _write(tmp_path: Path, people, teams, org: str = "org-z") -> bytes:
    """レポートを書いて、そのバイト列を返す。"""
    path = tmp_path / "adoption-report.html"
    write_adoption_report(people, teams, path, org=org)
    return path.read_bytes()


# ---------------------------------------------------------------- 値と変化


def test_values_are_computed_from_the_csv_text():
    """割り算は CSV に書く字句（丸めた後）から計算する（丸める前の値からではない）。"""
    # 需要 2.998 は CSV で "3.00"、÷ 2 人で 1.5 → "$2"（丸める前なら 1.499 → "$1"）
    teams = _org(THREE, seat_holders=2, code_users=2, code_users_3m=1, github_people=1,
                 code_demand_usd=2.998, code_demand_median_usd=1.499)
    table = _report_view([], teams, "org-z")["org_table"]
    assert _row_of(table, "需要 / 人（USD）")["cells"] == ["$2", "$2", "$2"]
    # 中央値 1.499 は CSV で "1.50" → "$2"
    assert _row_of(table, "需要の中央値（USD）")["cells"] == ["$2", "$2", "$2"]


@pytest.mark.parametrize(("latest", "previous", "kind", "expected"), [
    (5, 3, _COUNT, "+2"),
    (3, 5, _COUNT, "−2"),
    (2.5, 2, _COUNT, "+0.5"),
    (0.5, 0.25, _SHARE, "+25 pt"),
    (0.25, 0.5, _SHARE, "−25 pt"),
    (150.0, 100.0, _RATIO, "+50%"),
    (50.0, 100.0, _RATIO, "−50%"),
    (5.0, 0.0, _RATIO, "—"),
])
def test_change_formats(latest, previous, kind, expected):
    """差は人数（小数1桁まで）とポイント、比は百分率。比で古い方が0なら値なし。"""
    assert _change(latest, previous, kind) == expected


def test_delta_needs_both_months_without_partial_values():
    """前月比は最新とその前の月の値がそろい、どちらも途中の値でないときだけ出る。"""
    assert _delta([1.0, 2.0], _RATIO) == "+100%"
    assert _delta([None, 2.0], _RATIO) == "—"
    assert _delta([1.0, None], _RATIO) == "—"
    assert _delta([2.0], _RATIO) == "—"
    assert _delta([1.0, 2.0], _RATIO, [False, True]) == "—"
    assert _delta([1.0, 2.0], _RATIO, [True, False]) == "—"


def test_trend3_needs_six_complete_months():
    """3か月比は直近3か月の平均と前の3か月の平均。6か月そろうときだけ出る。"""
    values = [1.0, 1.0, 1.0, 2.0, 2.0, 2.0]
    assert _trend3(values, _RATIO) == "+100%"
    assert _trend3(values[1:], _RATIO) == "—"
    assert _trend3([None, *values[1:]], _RATIO) == "—"
    assert _trend3([0.0, 0.0, 0.0, 2.0, 2.0, 2.0], _RATIO) == "—"
    assert _trend3(values, _RATIO, [True, False, False, False, False, False]) == "—"


def test_overview_has_trend3_only_with_six_months(tmp_path):
    """月の軸が6か月に満たないと一覧に3か月比を書かない（数値の表の列は残す）。"""
    team = {"scope": GroupScope.TEAM, "name": "Orion-Team"}
    short = _org(THREE) + [_group(month=m, **team) for m in THREE]
    view = _report_view([], short, "org-z")
    assert all(cell["trend3"] is None
               for row in view["sections"][0]["overview"]["rows"] for cell in row["cells"])
    text = _write(tmp_path, [], short).decode("utf-8")
    assert "<th>3 か月比</th>" in text
    assert '<div class="ov-d">3 か月比' not in text

    full = _org(SIX) + [_group(month=m, **team) for m in SIX]
    cells = _report_view([], full, "org-z")["sections"][0]["overview"]["rows"][0]["cells"]
    assert all(cell["trend3"] in ("+0 pt", "+0%") for cell in cells)


# ---------------------------------------------------------------- 月の軸


def test_month_range_is_calendar_months():
    """月の軸は暦の月を間を空けずに並べる（年をまたぐ）。"""
    assert _month_range("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert _month_range(LATEST, LATEST) == [LATEST]


def test_years_are_added_to_the_first_month_and_january():
    """年は最初の月と各年の1月にだけ添える。"""
    heads = _month_heads(["2025-11", "2025-12", "2026-01", "2026-02"],
                         Partial(frozenset(), frozenset()), _BOTH)
    assert [head["year"] for head in heads] == ["2025", None, "2026", None]
    assert [head["label"] for head in heads] == ["11月", "12月", "1月", "2月"]


def test_missing_month_is_a_month_without_values(tmp_path):
    """行の無い月は値なしの列として並び、冒頭に出て、その月が絡む前月比は値なし。"""
    teams = _org(["2026-04", "2026-06"])
    people = [_person(month="2026-04"), _person(month=LATEST),
              _person(email=f"bob@{DOMAIN}", month=LATEST)]
    view = _report_view(people, teams, "org-z")
    assert view["missing"] == "2026-05"
    rows = view["org_table"]["rows"]
    assert all(row["cells"][1] == "—" for row in rows)
    assert all(row["delta"] == "—" for row in rows)
    # 人ごとの一覧: 分析できなかった月は全員「×」、シートの無い月は空白（別の見た目）
    people_rows = [row for row in view["people"]["rows"] if "name" in row]
    amy, bob = people_rows
    assert [cell["text"] for cell in amy["blocks"][0]] == ["", "×", ""]
    assert amy["blocks"][0][1]["cls"] == "c na"
    assert bob["blocks"][0][0] == {"cls": None, "title": None, "text": ""}
    assert bob["blocks"][0][1]["text"] == "×"
    text = _write(tmp_path, people, teams).decode("utf-8")
    assert "<p>2026-05 は、入力が欠けていて分析できなかった月です（値はありません）。</p>" in text
    assert "<span>× 分析できなかった月</span>" in text
    assert "<td></td>" in text


def test_no_missing_month_has_no_missing_notes(tmp_path):
    """行の無い月が無ければ、冒頭の月の注記も凡例の「×」も出ない。"""
    text = _write(tmp_path, [_person()], _org(THREE)).decode("utf-8")
    assert "分析できなかった月" not in text


# ---------------------------------------------------------------- 途中の値


def test_partial_spend_month_marks_and_blanks_spend_changes(tmp_path):
    """支出が途中の月に＊が付き、支出の側の指標の変化だけが値なし（人数と GitHub は出る）。"""
    teams = _org(THREE[:2]) + [_group(month=LATEST, spend_complete=False, seat_holders=5,
                                      github_people=3)]
    view = _report_view([_person()], teams, "org-z")
    table = view["org_table"]
    assert [head["mark"] for head in table["heads"]] == ["", "", "＊"]
    assert _row_of(table, "シートを持つ人")["delta"] == "+1"
    assert _row_of(table, "GitHub の対象")["delta"] == "+1"
    assert _row_of(table, "使った人の割合")["delta"] == "—"
    assert _row_of(table, "需要 / 人（USD）")["delta"] == "—"
    assert _row_of(table, "PR / 人")["delta"] != "—"
    assert _row_of(table, "費用 / マージ行（USD）")["delta"] == "—"
    assert view["scatter"]["heading"].startswith(f"使った量と GitHub の成果（{LATEST}＊・")
    tiles = {tile["label"]: tile for tile in view["tiles"]}
    assert tiles["Claude Code の需要"]["value"].endswith("＊")
    assert tiles["Claude Code の需要"]["delta"] == "—"
    assert tiles["GitHub のマージ行"]["delta"] == "+0%"
    text = _write(tmp_path, [_person()], teams).decode("utf-8")
    assert ("<p>＊ は、支出レポートが月の途中までの月です。これらの月の値は途中までの値で、"
            "その値が絡む前月比・3 か月比は出していません。</p>") in text


def test_partial_github_month_marks_and_blanks_github_changes():
    """GitHub の収集が未完了の月に†が付き、GitHub の指標の変化だけが値なし。"""
    teams = _org(THREE[:2]) + [_group(month=LATEST, github_cache_complete=False)]
    view = _report_view([_person()], teams, "org-z")
    table = view["org_table"]
    assert [head["mark"] for head in table["heads"]] == ["", "", "†"]
    assert _row_of(table, "使った人の割合")["delta"] == "+0 pt"
    assert _row_of(table, "需要 / 人（USD）")["delta"] == "+0%"
    assert _row_of(table, "GitHub の対象")["delta"] == "+0"
    assert _row_of(table, "PR / 人")["delta"] == "—"
    assert _row_of(table, "費用 / マージ行（USD）")["delta"] == "—"
    assert view["scatter"]["heading"].startswith(f"使った量と GitHub の成果（{LATEST}†・")


def test_no_partial_month_has_no_mark_note(tmp_path):
    """途中の値の月が無ければ、冒頭の印の説明も印も出ない。"""
    text = _write(tmp_path, [_person()], _org(THREE)).decode("utf-8")
    assert "支出レポートが月の途中までの月" not in text
    assert "GitHub の収集が完了していない月" not in text
    assert "＊" not in text and "†" not in text


def test_share3_is_partial_when_two_months_before_is_partial():
    """2か月前が＊の月なら、3か月続けて使った人の割合の変化だけが値なしになる。"""
    teams = [_group(month=m, spend_complete=m != "2026-04") for m in SIX]
    table = _report_view([], teams, "org-z")["org_table"]
    share3 = _row_of(table, "3 か月続けて使った人の割合")
    assert (share3["delta"], share3["trend3"]) == ("—", "—")
    assert _row_of(table, "使った人の割合")["delta"] == "+0 pt"
    assert _row_of(table, "需要 / 人（USD）")["delta"] == "+0%"
    # 印は＊の月の見出しにだけ付く
    assert [head["mark"] for head in table["heads"]] == ["", "", "", "＊", "", ""]


# ---------------------------------------------------------------- 負の値


def test_negative_values_are_drawn_at_zero_but_shown_as_is(tmp_path):
    """需要が負でも書け、図と色の段では0の位置、表と title 属性には負の値が出る。"""
    teams = _org(THREE[:2]) + [_group(month=LATEST, code_demand_usd=-8.0)]
    people = [_person(code_demand_usd=-2.5)]
    view = _report_view(people, teams, "org-z")
    demand = next(item["chart"] for item in view["charts"]
                  if item["title"] == "Claude Code の需要")
    baseline = demand["ticks"][0]["y"]
    assert demand["dots"][-1]["cy"] == baseline
    assert demand["dots"][-1]["tip"] == f"{LATEST}: $-8"
    assert _row_of(view["org_table"], "需要 / 人（USD）")["cells"][-1] == "$-2"
    cell = view["people"]["rows"][1]["blocks"][0][-1]
    assert cell["cls"] == "c z"
    assert cell["title"] == f"amy {LATEST}: $-2.50"
    scatter = view["scatter"]["charts"][1]["chart"]
    zero_x = next(line["x1"] for line in scatter["grid"]
                  if line["anchor"] == "middle" and line["cls"] == "axisline")
    assert scatter["points"][0]["cx"] == zero_x
    assert scatter["points"][0]["tip"].startswith("amy: Claude Code の需要（USD） $-2 / ")
    _write(tmp_path, people, teams)


# ---------------------------------------------------------------- GitHub 分析の無い組織


def test_without_github_there_are_no_github_metrics(tmp_path):
    """GitHub 分析の無い組織は GitHub の要約・図・表の行・一覧の列・枠・散布図を出さない。"""
    team = {"scope": GroupScope.TEAM, "name": "Orion-Team"}
    teams = [_no_github_group(month=m) for m in THREE]
    teams += [_no_github_group(month=m, **team) for m in THREE]
    view = _report_view([_no_github_person()], teams, "org-z")
    assert [tile["label"] for tile in view["tiles"]] == [
        "Claude Code を使った人", "Claude Code の需要"]
    assert [item["title"] for item in view["charts"]] == [
        "Claude Code を使った人", "Claude Code の需要", "Claude Code が加えた行（中央値）"]
    assert [row["label"] for row in view["org_table"]["rows"]] == [
        metric.label for metric in METRICS if not metric.github]
    assert view["sections"][0]["overview"]["labels"] == [
        "使った人の割合", "需要 / 人（USD）", "加えた行（中央値）"]
    assert [block["title"] for block in view["people"]["blocks"]] == [
        "Claude Code の需要（USD）", "Claude Code が加えた行"]
    assert view["scatter"] is None
    text = _write(tmp_path, [_no_github_person()], teams).decode("utf-8")
    assert "<h1>Claude Code の活用と成果 — org-z</h1>" in text
    assert ("<p>この組織は GitHub 分析の対象ではないので、GitHub の成果の指標"
            "（マージ行・PR・リードタイム・マージ行あたりの費用）は載せていません。</p>") in text
    assert 'id="scatter"' not in text and "#scatter" not in text
    assert "GitHub ID なし・未記入" not in text
    assert "PR とコミットは GitHub" not in text


def test_with_github_the_heading_names_github(tmp_path):
    """GitHub 分析のある組織は題に GitHub の成果を書き、散布図の節へのリンクを置く。"""
    text = _write(tmp_path, [_person()], _org(THREE)).decode("utf-8")
    assert "<h1>Claude Code の活用と GitHub の成果 — org-z</h1>" in text
    assert '<a href="#scatter">使った量と成果（参考）</a>' in text
    assert "この組織は GitHub 分析の対象ではない" not in text


# ---------------------------------------------------------------- 集まり


def test_no_team_or_department_rows_have_no_sections(tmp_path):
    """チーム・部署の行が無い組織では、その節とリンクが無い。"""
    view = _report_view([_person()], _org(THREE), "org-z")
    assert view["sections"] == []
    text = _write(tmp_path, [_person()], _org(THREE)).decode("utf-8")
    for scope in ("team", "department"):
        assert f'id="{scope}"' not in text and f'href="#{scope}"' not in text


def test_groups_are_ordered_by_latest_size_then_name(tmp_path):
    """人数の多い順・同数は名前の順・最新の月に居ない集まりは後ろ。先頭は組織全体。"""
    team = GroupScope.TEAM
    teams = _org(THREE) + [
        _group(scope=team, name="Beta-Team", seat_holders=2, code_users=2, code_users_3m=1),
        _group(scope=team, name="Alpha-Team", seat_holders=2, code_users=2, code_users_3m=1),
        _group(scope=team, name="Gamma-Team", seat_holders=3),
        _group(scope=team, name="Aged-Team", month="2026-05", seat_holders=4),
        _group(scope=GroupScope.DEPARTMENT, name="架空開発部", seat_holders=4),
    ]
    view = _report_view([_person()], teams, "org-z")
    assert [section["id"] for section in view["sections"]] == ["team", "department"]
    rows = view["sections"][0]["overview"]["rows"]
    assert [row["name"] for row in rows] == [
        "組織全体", "Gamma-Team", "Alpha-Team", "Beta-Team", "Aged-Team"]
    assert [row["holders"] for row in rows] == ["4 人", "3 人", "2 人", "2 人", "—"]
    assert [row["is_org"] for row in rows] == [True, False, False, False, False]
    text = _write(tmp_path, [_person()], teams).decode("utf-8")
    for row in rows:
        assert f'<a href="#{row["anchor"]}">' in text
        assert f'id="{row["anchor"]}"' in text
    assert rows[0]["anchor"] == "g-organization"
    nav = re.search(r"<nav>(.*?)</nav>", text).group(1)
    assert nav.index("#team") < nav.index("#department")


# ---------------------------------------------------------------- 人ごとの一覧


def test_people_are_ordered_by_department_then_email(tmp_path):
    """部署（最後の月の値。未記入は末尾）→ メールの順で、量の多い順にしない。"""
    people = [
        _person(email=f"zed@{DOMAIN}", department="架空開発部", code_demand_usd=9000.0),
        _person(email=f"amy@{DOMAIN}", department="架空開発部", code_demand_usd=1.0),
        _person(email=f"bob@{DOMAIN}", department=""),
        _person(email=f"cy@{DOMAIN}", department="架空開発部", month="2026-05"),
        _person(email=f"cy@{DOMAIN}", department="架空営業部"),
    ]
    view = _report_view(people, _org(THREE), "org-z")
    order = [row.get("department", row.get("name")) for row in view["people"]["rows"]]
    assert order == ["架空営業部", "cy", "架空開発部", "amy", "zed", "部署なし", "bob"]
    data = _write(tmp_path, people, _org(THREE))
    assert DOMAIN.encode() not in data


def test_bucket_edges():
    """色の段は境目ちょうどの値で1段濃くなる（直前の値は1段薄い）。"""
    demand, loc, lines = (block.edges for block in BLOCKS)
    assert [_bucket(v, demand) for v in (0.0, 0.004, 0.005, 49.99, 50, 199.99, 200,
                                         999.99, 1000, 2999.99, 3000)] == [
        "z", "z", "b1", "b1", "b2", "b2", "b3", "b3", "b4", "b4", "b5"]
    assert [_bucket(v, loc) for v in (0, 1, 99, 100, 9999, 10000, 99999, 100000)] == [
        "z", "b1", "b1", "b2", "b3", "b4", "b4", "b5"]
    assert [_bucket(v, lines) for v in (0, 1, 4999, 5000, 19999, 20000)] == [
        "z", "b1", "b3", "b4", "b4", "b5"]


def test_heat_cells_distinguish_zero_blank_and_github_states():
    """0・値なし・GitHub ID なしと未記入・シートの無い月は、色の段と別の見た目。"""
    people = [
        _person(email=f"amy@{DOMAIN}", code_demand_usd=0.0, loc_with_cc=None),
        _person(email=f"bob@{DOMAIN}", github_id=GithubId.NONE, merged_pr_count=None,
                lead_time_median_hours=None, commit_count=None, lines_added_capped=None,
                large_commits=None),
        _person(email=f"cy@{DOMAIN}", github_id=GithubId.UNSET, merged_pr_count=None,
                lead_time_median_hours=None, commit_count=None, lines_added_capped=None,
                large_commits=None, month="2026-05"),
        _person(email=f"dee@{DOMAIN}", github_data=GithubData.NO_COMMITS, commit_count=None,
                lines_added_capped=None, large_commits=None),
    ]
    view = _report_view(people, _org(THREE), "org-z")
    rows = {row["name"]: row for row in view["people"]["rows"] if "name" in row}
    demand, loc, lines = rows["amy"]["blocks"]
    assert demand[-1]["cls"] == "c z" and demand[-1]["title"] == f"amy {LATEST}: $0.00"
    assert loc[-1]["cls"] == "c nd" and loc[-1]["title"] == f"amy {LATEST}: データなし"
    assert lines[-1]["cls"] == "c b2"
    assert lines[-1]["title"] == f"amy {LATEST}: マージ行 120・コミット 3 件・PR 2 件"
    bob = rows["bob"]["blocks"][2][-1]
    assert (bob["cls"], bob["text"]) == ("c na", "–")
    assert bob["title"] == f"bob {LATEST}: この Organization で開発しない人（GitHub ID なし）"
    assert rows["bob"]["github"] == "GitHub なし"
    cy = rows["cy"]["blocks"][2]
    assert cy[1]["title"] == "cy 2026-05: GitHub ID が未記入"
    assert cy[-1] == {"cls": None, "title": None, "text": ""}  # 最新の月はシートなし
    assert rows["cy"]["github"] == "GitHub 未記入"
    dee = rows["dee"]["blocks"][2][-1]
    assert (dee["cls"], dee["title"]) == ("c nd", f"dee {LATEST}: 行数の材料なし")


# ---------------------------------------------------------------- 散布図


def test_scatter_places_people_with_both_values():
    """点は最新の月で、横軸とマージ行の両方に値のある人だけ（0 は置き、値なしは置かない）。"""
    people = [
        _person(email=f"amy@{DOMAIN}"),
        _person(email=f"bob@{DOMAIN}", loc_with_cc=0, code_demand_usd=0.0,
                lines_added_capped=0),
        _person(email=f"cy@{DOMAIN}", github_id=GithubId.NONE, merged_pr_count=None,
                lead_time_median_hours=None, commit_count=None, lines_added_capped=None,
                large_commits=None),
        _person(email=f"dee@{DOMAIN}", loc_with_cc=None),
        _person(email=f"eve@{DOMAIN}", month="2026-05"),
    ]
    charts = _report_view(people, _org(THREE), "org-z")["scatter"]["charts"]
    names = [[point["tip"].split(":")[0] for point in chart["chart"]["points"]]
             for chart in charts]
    assert names == [["amy", "bob"], ["amy", "bob", "dee"]]
    assert charts[0]["chart"]["points"][1]["tip"] == (
        "bob: Claude Code が加えた行 0 / GitHub のマージ行（上限つき） 0")


# ---------------------------------------------------------------- 図


def test_count_chart_uses_whole_ticks():
    """人数の図は目盛りを整数にし、最後の点に値のラベルを付ける。"""
    chart = _line_chart(list(THREE), [3.0, None, 4.0], lambda v: f"{v:.0f} 人", whole=True)
    assert [tick["label"] for tick in chart["ticks"]] == ["0", "2", "4"]
    assert chart["segments"] == []  # 値の無い月で線が切れ、点1つの区切りは線にしない
    assert len(chart["dots"]) == 2
    assert chart["value_label"]["text"] == "4 人"


# ---------------------------------------------------------------- 文字列と出力


def test_input_text_is_not_interpreted_as_html(tmp_path):
    """名前の `<`・`&`・式の先頭文字はエスケープされ、式のエスケープの引用符は付かない。"""
    name = "<b>&=SUM(A1)"
    teams = _org(THREE) + [_group(scope=GroupScope.TEAM, name=name)]
    people = [_person(department="-架空\r\n開発部\r推進")]
    data = _write(tmp_path, people, teams)
    text = data.decode("utf-8")
    assert "&lt;b&gt;&amp;=SUM(A1)" in text
    assert "<b>&" not in text
    assert "'=SUM" not in text and "'-架空" not in text
    assert b"\r" not in data
    assert "-架空\n開発部\n推進" in text


def test_url_like_names_are_written_as_text(tmp_path):
    """集まりの名前に URL の字句があっても書け、文字として出る。"""
    name = "https://example.invalid/team?a=1"
    teams = _org(THREE) + [_group(scope=GroupScope.TEAM, name=name)]
    text = _write(tmp_path, [_person()], teams).decode("utf-8")
    assert "https://example.invalid/team?a=1" in text
    assert 'href="https:' not in text


def test_output_has_no_comments_scripts_or_external_references(tmp_path):
    """出力に HTML コメントと script が無く、リンクはページ内だけ。"""
    teams = _org(THREE) + [_group(scope=GroupScope.TEAM, name="Orion-Team")]
    text = _write(tmp_path, [_person()], teams).decode("utf-8")
    assert "<!--" not in text
    assert "<script" not in text.lower()
    assert " src=" not in text
    assert all(href.startswith("#") for href in re.findall(r'href="([^"]*)"', text))


def test_template_and_css_load_nothing_external():
    """テンプレートと CSS に外部の URL・link・@import・url( が無い。"""
    for name in ("adoption-report.html.j2", "adoption-report.css"):
        source = _asset(name)
        for needle in ("http:", "https:", "<link", "@import", "url(", "<script", "src=",
                       "<!--"):
            assert needle not in source, (name, needle)


def test_light_and_dark_colors_go_through_variables():
    """配色は prefers-color-scheme で切り替わり、色の指定はすべて変数を通る。"""
    css = _asset("adoption-report.css")
    assert "@media (prefers-color-scheme:dark){:root{" in css
    rules = re.sub(r":root\{[^}]*\}", "", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL))
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rules)
    assert "var(--" in rules


def test_same_rows_in_any_order_give_the_same_bytes(tmp_path):
    """同じ行の集合を別の順で渡しても同じバイト列で、改行は LF だけ。"""
    people = [_person(email=f"{who}@{DOMAIN}", month=m) for who in ("amy", "bob") for m in SIX]
    teams = _org(SIX) + [_group(scope=GroupScope.TEAM, name="Orion-Team", month=m)
                         for m in SIX]
    first = _write(tmp_path, people, teams)
    second = _write(tmp_path, list(reversed(people)), list(reversed(teams)))
    assert first == second
    assert b"\r" not in first


def test_wrong_row_types_and_missing_organization_rows():
    """行の型が違えば TypeError、組織全体の行が無ければ・同じ鍵の行が重なれば ValueError。"""
    with pytest.raises(TypeError):
        _report_view([_group()], _org(THREE), "org-z")
    with pytest.raises(TypeError):
        _report_view([_person()], [_person()], "org-z")
    with pytest.raises(ValueError, match="組織全体の行"):
        _report_view([_person()], [_group(scope=GroupScope.TEAM, name="Orion-Team")], "org-z")
    with pytest.raises(ValueError, match="同じ集まりの同じ月"):
        _report_view([], [_group(), _group()], "org-z")
    with pytest.raises(ValueError, match="同じ人の同じ月"):
        _report_view([_person(), _person()], _org(THREE), "org-z")


def test_metric_definitions_match_the_design_table():
    """14指標の変化の種類・一覧・途中の値になる条件は設計書 §27.8 の表のとおり。"""
    table = {metric.key: (metric.kind, metric.overview, metric.partial) for metric in METRICS}
    assert table == {
        "holders": (_COUNT, False, ""),
        "share": (_SHARE, True, "s"),
        "share3": (_SHARE, False, "s"),
        "demand_pp": (_RATIO, True, "s"),
        "demand_median": (_RATIO, False, "s"),
        "loc_median": (_RATIO, True, "s"),
        "gh_people": (_COUNT, False, ""),
        "outside_org": (_COUNT, False, "s"),
        "lines_pp": (_RATIO, True, "g"),
        "pr_pp": (_RATIO, True, "g"),
        "pr_size": (_RATIO, False, "g"),
        "lead_median": (_RATIO, True, "g"),
        "lead_p90": (_RATIO, False, "g"),
        "cost_per_line": (_RATIO, True, "sg"),
    }
    assert _metric("share3").span == 3
    # GitHub の指標は表の「GitHub の対象」以下
    start = [metric.key for metric in METRICS].index("gh_people")
    assert [metric.github for metric in METRICS] == [i >= start for i in range(len(METRICS))]


@pytest.mark.parametrize(("value", "expected"), [
    (0.0, "$0.00"),
    (0.0015, "$0.0015"),
    (0.0099, "$0.0099"),
    (0.01, "$0.01"),
    (0.16, "$0.16"),
    (-0.003, "$-0.0030"),
])
def test_usd_per_line_keeps_sub_cent_values(value, expected):
    """マージ行あたりの費用は、1 セント未満（0 を除く）を CSV と同じ小数4桁で出す。"""
    assert _usd_per_line(value) == expected


def test_sub_cent_cost_reads_the_same_in_table_chart_and_tile():
    """1 セント未満の費用は、表のセル・図の title と最後の点のラベル・要約で同じ字句。"""
    # 需要 1.2 ÷ マージ行 800 = 0.0015（CSV も "0.0015"）
    teams = _org(THREE, code_demand_usd_of_github_people=1.2, lines_added_capped=800)
    view = _report_view([_person()], teams, "org-z")
    expected = "$0.0015"
    assert _row_of(view["org_table"], "費用 / マージ行（USD）")["cells"][-1] == expected
    chart = next(item["chart"] for item in view["charts"]
                 if item["title"] == "マージ行あたりの費用")
    assert chart["dots"][-1]["tip"] == f"{LATEST}: {expected}"
    assert chart["value_label"]["text"] == expected
    tile = next(tile for tile in view["tiles"] if tile["label"] == "マージ行あたりの費用")
    assert tile["value"] == expected


def test_every_column_the_report_reads_is_in_the_column_table():
    """指標・図・要約・人ごとの一覧の枠・散布図が読む列は、すべて列の表にあり、表の列名。"""
    read = {name for metric in METRICS for name in metric.columns}
    read |= {chart.column for chart in _CHARTS}
    read |= {tile.column for tile in _TILES}
    read |= {block.column for block in BLOCKS}
    read |= {_SCATTER_Y_COLUMN} | {column for _, _, column, _, _ in _SCATTERS}
    assert read <= set(COLUMNS)
    assert set(COLUMNS) <= set(ADOPTION_TEAMS_COLUMNS) | set(ADOPTION_PEOPLE_COLUMNS)


def test_outside_org_mark_tooltip_and_unchanged_cells(tmp_path):
    """該当するマージ行セルだけに印と本文色のクラスを足し、行数と連続月数を添える。"""
    people = [_person(month=THREE[0]), _person(month=THREE[1], loc_with_cc=None),
              _person(loc_with_cc=1234, merged_pr_count=0, lead_time_median_hours=None,
                      commit_count=0, lines_added_capped=0, outside_org=True, outside_org_months=3)]
    teams = _org(THREE, outside_org_people=1)
    view = _report_view(people, teams, "org-z")
    cells = view["people"]["rows"][1]["blocks"][2]
    assert cells[0]["cls"] == "c b2" and cells[0]["text"] == ""
    assert cells[1]["cls"] == "c b2" and cells[1]["text"] == ""
    tip = (f"amy {LATEST}: マージ行 0・コミット 0 件・PR 0 件"
           "（Claude Code が加えた行 1,234 があるのに、この Organization に"
           "コミットも PR も無い月・3 か月連続）")
    assert cells[2] == {"cls": "c z fl", "title": tip, "text": "!"}
    html = _write(tmp_path, people, teams)
    assert f'<td class="c z fl" title="{tip}">!</td>'.encode() in html
    assert b'td.c.fl{color:var(--ink);font-weight:700;font-size:12.5px}' in html
    assert _write(tmp_path, reversed(people), reversed(teams)) == html


@pytest.mark.parametrize("empty", [False, True])
def test_outside_org_legend_and_explanation_always_appear_with_github(tmp_path, empty):
    """該当者や人の行がなくても、GitHub 分析のある組織では凡例と読み方を出す。"""
    people = [] if empty else [_person()]
    text = _write(tmp_path, people, _org(THREE)).decode()
    assert ('<span><span class="outside-org-mark">!</span> Claude Code が加えた行があるのに、'
            'この Organization にコミットも PR も無い月</span>') in text
    assert '.legend .outside-org-mark{font-weight:600;color:var(--ink)}' in text
    assert ("成果が見えない人＝その月に Claude Code が加えた行が一定以上あるのに、この Organization に"
            "コミットもマージされた PR も無い人（人ごとの一覧の「!」）。ローカルだけの作業・マージ前・"
            "別の Organization の業務を区別できないので、本人に確かめるきっかけとして読みます。") in text
    assert text.index("費用 / マージ行＝") < text.index("成果が見えない人＝")
    assert '<td class="c z fl"' not in text


def test_outside_org_metric_position_delta_and_no_overview_or_summary():
    """人数の行は対象の次に置き差で比べ、区切り・一覧・要約・図には足さない。"""
    teams = [
        _group(month=month, outside_org_people=0 if i < 3 else 2) for i, month in enumerate(SIX)
    ]
    view = _report_view([], teams, "org-z")
    rows = view["org_table"]["rows"]
    index = next(i for i, row in enumerate(rows) if row["label"] == "GitHub の対象")
    row = rows[index + 1]
    assert row["label"] == "うち成果が見えない人"
    assert row["cells"] == ["0 人"] * 3 + ["2 人"] * 3
    assert row["delta"] == "+0" and row["trend3"] == "+2"
    metric = _metric("outside_org")
    assert not metric.sep and not metric.overview
    assert all(tile.column != "outside_org_people" for tile in _TILES)
    assert all(chart.column != "outside_org_people" for chart in _CHARTS)
    changed = _report_view(
        [], [*teams[:-1], dataclasses.replace(teams[-1], outside_org_people=1)], "org-z",
    )
    assert _row_of(changed["org_table"], "うち成果が見えない人")["delta"] == "−1"


@pytest.mark.parametrize("partial_month", ["2026-03", "2026-05", "2026-06"])
def test_outside_org_metric_partial_spend_suppresses_comparisons(partial_month):
    """支出が途中の月は3か月比を止め、直近2か月にあれば前月比も止める。"""
    teams = [_group(month=m, outside_org_people=1, spend_complete=m != partial_month) for m in SIX]
    row = _row_of(_report_view([], teams, "org-z")["org_table"], "うち成果が見えない人")
    assert row["trend3"] == "—"
    assert row["delta"] == ("—" if partial_month in SIX[-2:] else "+0")


def test_outside_org_metric_blank_and_no_github_display(tmp_path):
    """空欄の人数はダッシュにし、GitHub 分析がなければ印・凡例・説明・行を出さない。"""
    teams = [_group(month=m, outside_org_people=None, github_cache_complete=False) for m in SIX]
    row = _row_of(_report_view([], teams, "org-z")["org_table"], "うち成果が見えない人")
    assert row["cells"] == ["—"] * 6 and row["delta"] == "—" and row["trend3"] == "—"
    text = _write(tmp_path, [_no_github_person()], [_no_github_group()]).decode()
    for absent in ('class="c z fl"', 'class="outside-org-mark"', "成果が見えない人＝", "うち成果が見えない人"):
        assert absent not in text
