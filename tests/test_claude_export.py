"""claude.ai からの CSV 取得（claude_export）の計画・検証・配置と、Chrome 周りの純粋関数のテスト。

ブラウザもプロセスも起動しない。プロセスの起動・列挙・終了を実際に行う薄いラッパ
（launch_chrome・list_chrome_pids・terminate_chrome）は対象外で、ここではそれらが使う
コマンドの組み立てと出力の解釈を見る。OS の違いは platform 引数で切り替えて、どの OS の
上でも 3 OS 分を検査する。
"""

import datetime as dt
import json
import re
import urllib.parse
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from seat_analyzer import claude_export, ingest
from seat_analyzer.claude_export import (
    KIND_DIRS,
    MODE_CURRENT,
    MODE_PREVIOUS,
    NO_RESULT_REASON,
    TRIGGER_PREFIX,
    ExportRecord,
    ExportTarget,
    ProfileRun,
    chrome_pids,
    chrome_time_us,
    find_chrome,
    gated_targets,
    launch_command,
    manifest_path,
    match_results,
    new_run_id,
    place_export,
    plan_runs,
    process_listing_command,
    profile_preferences_path,
    read_manifest,
    resolve_mode,
    run_spec,
    staged_file,
    terminate_commands,
    trigger_url,
    update_preferences,
    verify_export,
    write_preferences,
)
from seat_analyzer.config import PACKAGE_CONFIG_PATH, load_config

# 合成の組織 UUID（実在の組織を指さない）
UUID1 = "00000000-0000-4000-8000-000000000001"
UUID2 = "00000000-0000-4000-8000-000000000002"
UUID3 = "00000000-0000-4000-8000-000000000003"
UUID4 = "00000000-0000-4000-8000-000000000004"

ALL_KINDS = ("members", "spend", "code")

# 当月の判定に使う「今日」（当月 2026-10・前月 2026-09）
TODAY = dt.date(2026, 10, 7)

# ヘッダの照合に使うエイリアス表（パッケージ既定の columns）
COLUMNS = load_config(PACKAGE_CONFIG_PATH)["columns"]


def _section(profile: str = "corp", org_id: str = UUID1,
             kinds: list[str] | None = None) -> dict:
    """ロード後の claude_export 区画（書かなかった項目は雛形の既定で埋まった形）。"""
    return {"profile": profile, "org_id": org_id,
            "kinds": list(ALL_KINDS) if kinds is None else kinds}


UNSET = _section(profile="", org_id="")


def _org(section: dict | None = None, workspaces: dict | None = None) -> dict:
    """ロード後の organizations.<組織名>（github_org 等は未設定のまま）。"""
    return {
        "github_org": "",
        "workspaces": workspaces or {},
        "secondary_breakeven_usd": None,
        "claude_export": section or UNSET,
    }


def _workspace(section: dict | None = None, primary: bool = False) -> dict:
    return {
        "primary": primary, "label": "", "fixed_seat": "",
        "credit_limit_default_usd": None, "evaluation_months": None,
        "claude_export": section or UNSET,
    }


def _target(org: str = "example", workspace: str | None = None, profile: str = "corp",
            org_id: str = UUID1, kinds: tuple[str, ...] = ALL_KINDS) -> ExportTarget:
    return ExportTarget(org=org, workspace=workspace, profile=profile, org_id=org_id,
                        kinds=kinds)


# ------------------------------------------------------------------ 対象の列挙


def test_kind_dirs_are_input_subdirectories():
    """配置先の種別ディレクトリは分析が読む入力サブディレクトリと同じ名前。"""
    assert set(KIND_DIRS.values()) == set(ingest.INPUT_SUBDIRS)


def test_gated_targets_reads_an_organization_level_section():
    targets = gated_targets({"example": _org(_section())})
    assert targets == [_target()]
    assert targets[0].dir == "example"


def test_gated_targets_reads_workspace_level_sections():
    organizations = {"example2": _org(workspaces={
        "main": _workspace(_section(org_id=UUID2), primary=True),
        "second": _workspace(_section(org_id=UUID3)),
    })}
    targets = gated_targets(organizations)
    assert targets == [
        _target("example2", "main", org_id=UUID2),
        _target("example2", "second", org_id=UUID3),
    ]
    assert [target.dir for target in targets] == ["example2/main", "example2/second"]


def test_gated_targets_skips_unset_sections_and_keeps_the_written_order():
    """書かなかった組織・workspace は対象外。並びは設定の記述順。"""
    organizations = {
        "org-b": _org(),                                     # 未設定
        "example": _org(_section(profile="group", org_id=UUID1)),
        "example2": _org(workspaces={
            "main": _workspace(_section(org_id=UUID2), primary=True),
            "second": _workspace(),                          # 未設定
        }),
        "org-a": _org(_section(org_id=UUID4)),
    }
    assert [target.dir for target in gated_targets(organizations)] == [
        "example", "example2/main", "org-a",
    ]


def test_gated_targets_normalizes_kinds_order_and_uuid_case():
    """kinds は記述順によらず members / spend / code の順、UUID は小文字に揃える。"""
    section = _section(org_id=UUID1.upper().replace("-4000-", "-4ABC-"),
                       kinds=["code", "members"])
    [target] = gated_targets({"example": _org(section)})
    assert target.kinds == ("members", "code")
    assert target.org_id == "00000000-0000-4abc-8000-000000000001"


def test_gated_targets_from_a_loaded_config(tmp_path):
    """設定のロードを通した形からも同じ対象を列挙する（kinds の省略は 3 種）。"""
    path = tmp_path / "config.yaml"
    path.write_text(
        "organizations:\n"
        "  example:\n"
        "    claude_export:\n"
        "      profile: corp\n"
        f"      org_id: {UUID1}\n"
        "  example2:\n"
        "    workspaces:\n"
        "      main:\n"
        "        primary: true\n"
        "        claude_export:\n"
        "          profile: corp\n"
        f"          org_id: {UUID2}\n"
        "          kinds: [spend]\n"
        "      second:\n"
        "        claude_export:\n"
        "          profile: group\n"
        f"          org_id: {UUID3}\n",
        encoding="utf-8", newline="\n",
    )
    assert gated_targets(load_config(path)["organizations"]) == [
        _target("example"),
        _target("example2", "main", org_id=UUID2, kinds=("spend",)),
        _target("example2", "second", profile="group", org_id=UUID3),
    ]


def test_no_targets_by_default():
    assert gated_targets(load_config(PACKAGE_CONFIG_PATH)["organizations"]) == []


# ------------------------------------------------------------------ モードと計画


@pytest.mark.parametrize("month,expected", [
    (None, (MODE_CURRENT, "2026-10")),
    ("2026-10", (MODE_CURRENT, "2026-10")),
    ("2026-09", (MODE_PREVIOUS, "2026-09")),
])
def test_resolve_mode(month, expected):
    assert resolve_mode(month, TODAY) == expected


@pytest.mark.parametrize("month", ["2026-08", "2026-11", "2025-10"])
def test_resolve_mode_rejects_other_months_and_names_both(month):
    with pytest.raises(ValueError, match="当月 2026-10・前月 2026-09") as excinfo:
        resolve_mode(month, TODAY)
    assert "取得できるのは当月と前月だけです" in str(excinfo.value)


def test_resolve_mode_crosses_the_year_boundary():
    assert resolve_mode("2025-12", dt.date(2026, 1, 1)) == (MODE_PREVIOUS, "2025-12")


TARGETS = [
    _target("example", profile="corp", org_id=UUID1),
    _target("org-a", profile="group", org_id=UUID4),
    _target("example2", "main", profile="corp", org_id=UUID2),
    _target("example2", "second", profile="corp", org_id=UUID3),
]


def test_plan_runs_groups_by_profile_in_first_appearance_order():
    runs = plan_runs(TARGETS, month="2026-09", today=TODAY)
    assert runs == [
        ProfileRun("corp", MODE_PREVIOUS, "2026-09",
                   (TARGETS[0], TARGETS[2], TARGETS[3])),
        ProfileRun("group", MODE_PREVIOUS, "2026-09", (TARGETS[1],)),
    ]


def test_plan_runs_defaults_to_the_current_month():
    [first, _] = plan_runs(TARGETS, month=None, today=TODAY)
    assert (first.mode, first.month) == (MODE_CURRENT, "2026-10")


def test_plan_runs_narrows_by_org():
    """入れ子レイアウトの組織は、組織名で全 workspace が選ばれる。"""
    runs = plan_runs(TARGETS, month=None, today=TODAY, orgs=["example2", "example2"])
    assert runs == [ProfileRun("corp", MODE_CURRENT, "2026-10", (TARGETS[2], TARGETS[3]))]


def test_plan_runs_narrows_by_profile():
    runs = plan_runs(TARGETS, month=None, today=TODAY, profile="group")
    assert runs == [ProfileRun("group", MODE_CURRENT, "2026-10", (TARGETS[1],))]


def test_plan_runs_rejects_an_org_without_the_section():
    with pytest.raises(ValueError, match="組織 org-x は claude_export が設定されていません"):
        plan_runs(TARGETS, month=None, today=TODAY, orgs=["example", "org-x"])


def test_plan_runs_returns_nothing_when_the_filters_leave_nothing():
    assert plan_runs(TARGETS, month=None, today=TODAY, orgs=["org-a"], profile="corp") == []
    assert plan_runs([], month=None, today=TODAY) == []


def test_plan_runs_rejects_org_names_that_differ_only_in_case(tmp_path):
    """大文字小文字だけが違う組織名は、同じ入力ディレクトリになる環境があるので止める。"""
    path = tmp_path / "config.yaml"
    path.write_text(
        "organizations:\n"
        "  org-a:\n"
        "    claude_export:\n"
        "      profile: corp\n"
        f"      org_id: {UUID1}\n"
        "  Org-A:\n"
        "    claude_export:\n"
        "      profile: corp\n"
        f"      org_id: {UUID2}\n",
        encoding="utf-8", newline="\n",
    )
    targets = gated_targets(load_config(path)["organizations"])
    # 片方だけを選んだ実行でも止める（もう一方の配置先と重なりうるため）
    for orgs in (None, ["org-a"]):
        with pytest.raises(ValueError, match="組織名が衝突しています") as excinfo:
            plan_runs(targets, month=None, today=TODAY, orgs=orgs)
        assert "'org-a'" in str(excinfo.value) and "'Org-A'" in str(excinfo.value)


def test_plan_runs_rejects_workspace_names_that_differ_only_in_case():
    targets = [
        _target("example2", "main", org_id=UUID2),
        _target("example2", "Main", org_id=UUID3),
    ]
    with pytest.raises(ValueError, match="組織 example2 の claude_export を設定した workspace 名"):
        plan_runs(targets, month=None, today=TODAY)


def test_plan_runs_allows_the_same_workspace_name_in_different_orgs():
    targets = [
        _target("example", "main", org_id=UUID1),
        _target("example2", "Main", org_id=UUID2),
    ]
    assert len(plan_runs(targets, month=None, today=TODAY)[0].targets) == 2


def test_plan_runs_rejects_a_month_it_cannot_fetch():
    with pytest.raises(ValueError, match="取得できるのは当月と前月だけです"):
        plan_runs(TARGETS, month="2026-07", today=TODAY)


def test_new_run_id():
    """識別子の時刻は渡されたローカル時刻のまま（UTC へ直さない）。"""
    run = ProfileRun("corp", MODE_PREVIOUS, "2026-09", (TARGETS[0],))
    now = dt.datetime(2026, 10, 7, 11, 0, 9, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    assert new_run_id(run, now) == "corp-previous-20261007-110009"


# ------------------------------------------------------------------ spec とトリガー URL


RUN = ProfileRun("corp", MODE_PREVIOUS, "2026-09", (
    _target("example", org_id=UUID1),
    _target("example2", "main", org_id=UUID2, kinds=("members", "spend")),
))


def test_run_spec():
    assert run_spec(RUN, "corp-previous-20261007-110009") == {
        "run_id": "corp-previous-20261007-110009",
        "mode": "previous",
        "orgs": [
            {"uuid": UUID1, "dir": "example", "kinds": ["members", "spend", "code"]},
            {"uuid": UUID2, "dir": "example2/main", "kinds": ["members", "spend"]},
        ],
    }


def test_trigger_url_round_trips():
    spec = run_spec(RUN, "corp-previous-20261007-110009")
    url = trigger_url(spec)
    assert url.startswith(TRIGGER_PREFIX)
    payload = url[len(TRIGGER_PREFIX):]
    # フラグメントの中で区切りとして読まれる文字を残さない
    assert re.fullmatch(r"[A-Za-z0-9%._~-]+", payload)
    assert json.loads(urllib.parse.unquote(payload)) == spec


def test_trigger_url_keeps_non_ascii_names_as_utf8():
    """組織名の日本語は \\u エスケープではなく UTF-8 のまま URL エンコードする。"""
    run = ProfileRun("corp", MODE_CURRENT, "2026-10", (_target("組織", "副"),))
    url = trigger_url(run_spec(run, "corp-current-20261007-110009"))
    assert urllib.parse.quote("組織/副", safe="") in url
    assert "%5Cu" not in url
    assert json.loads(urllib.parse.unquote(url[len(TRIGGER_PREFIX):]))["orgs"][0]["dir"] \
        == "組織/副"


# ------------------------------------------------------------------ manifest


def _write_manifest(tmp_path: Path, payload) -> Path:
    path = tmp_path / "manifest.json"
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


MANIFEST = {
    "run_id": "corp-previous-20261007-110009",
    "mode": "previous",
    "finished_at": "2026-10-07T02:00:49.000Z",
    "results": [
        {"dir": "example2/main", "kind": "members", "ok": True,
         "filename": f"members-{UUID2}-2026-10-07.csv"},
        {"dir": "example2/main", "kind": "spend", "ok": True,
         "filename": f"spend-report-{UUID2}-2026-09-01-to-2026-09-30.csv",
         "range": "2026-09-01 to 2026-09-30"},
        {"dir": "example2/main", "kind": "code", "ok": False,
         "reason": "export button not present after 60s"},
    ],
    "log": ["..."],
}


def test_read_manifest(tmp_path):
    run_id, mode, records = read_manifest(_write_manifest(tmp_path, MANIFEST))
    assert (run_id, mode) == ("corp-previous-20261007-110009", "previous")
    assert records == [
        ExportRecord("example2/main", "members", True,
                     f"members-{UUID2}-2026-10-07.csv", None),
        ExportRecord("example2/main", "spend", True,
                     f"spend-report-{UUID2}-2026-09-01-to-2026-09-30.csv", None),
        ExportRecord("example2/main", "code", False, None,
                     "export button not present after 60s"),
    ]


@pytest.mark.parametrize("payload,fragment", [
    ('{"run_id": "x", ', "JSON として読めません"),
    ([], "オブジェクトではありません"),
    ({"run_id": "x", "results": []}, "run_id と mode"),
    ({"run_id": "x", "mode": "current"}, "results の一覧がありません"),
    ({"run_id": "x", "mode": "current", "results": {}}, "results の一覧がありません"),
    ({"run_id": "x", "mode": "current", "results": ["members"]},
     r"results\[0\] がオブジェクトではありません"),
    ({"run_id": "x", "mode": "current", "results": [{"kind": "spend", "ok": True}]},
     r"results\[0\]\.dir が文字列ではありません"),
])
def test_read_manifest_rejects_a_broken_manifest(tmp_path, payload, fragment):
    with pytest.raises(ValueError, match=fragment):
        read_manifest(_write_manifest(tmp_path, payload))


@pytest.mark.parametrize("item,reason", [
    ({}, "filename missing"),
    ({"filename": ""}, "filename missing"),
    ({"filename": "../spend.csv"}, "invalid filename"),
    ({"filename": "a\\b.csv"}, "invalid filename"),
    ({"filename": ".."}, "invalid filename"),
    # Windows のドライブ相対・絶対パス
    ({"filename": "D:spend-2026-09-01-to-2026-09-30.csv"}, "invalid filename"),
    ({"filename": "C:\\x.csv"}, "invalid filename"),
    # 別のデータストリーム
    ({"filename": "spend.csv:extra"}, "invalid filename"),
])
def test_read_manifest_fails_an_ok_result_without_a_usable_filename(tmp_path, item, reason):
    """ok でも使えるファイル名が無ければ失敗に倒す（staging の外を指す名前も読まない）。"""
    payload = {"run_id": "x", "mode": "current",
               "results": [{"dir": "example", "kind": "spend", "ok": True, **item}]}
    [record] = read_manifest(_write_manifest(tmp_path, payload))[2]
    assert record == ExportRecord("example", "spend", False, None, reason)


def test_match_results_follows_the_plan():
    """計画の順に引き当て、結果の無い組み合わせは失敗、計画に無い結果は捨てる。"""
    run = ProfileRun("corp", MODE_CURRENT, "2026-10", (
        _target("example"),
        _target("example2", "main", org_id=UUID2, kinds=("spend",)),
    ))
    records = [
        ExportRecord("example2/main", "spend", False, None, "first try"),
        ExportRecord("example", "members", True, "members-a.csv", None),
        ExportRecord("org-x", "spend", True, "spend-x.csv", None),         # 計画に無い
        ExportRecord("example", "spend", True, "spend-a.csv", None),
        ExportRecord("example2/main", "spend", True, "spend-b.csv", None),  # 後勝ち
    ]
    paired = match_results(run, records)
    assert [(target.dir, kind, record.ok, record.filename, record.reason)
            for target, kind, record in paired] == [
        ("example", "members", True, "members-a.csv", None),
        ("example", "spend", True, "spend-a.csv", None),
        ("example", "code", False, None, NO_RESULT_REASON),
        ("example2/main", "spend", True, "spend-b.csv", None),
    ]
    assert paired[3][0] is run.targets[1]


def test_staging_paths_follow_the_plan(tmp_path):
    staging = tmp_path / "exports"
    assert manifest_path(staging, "run-1") == staging / "run-1" / "manifest.json"
    assert staged_file(staging, "run-1", _target("example"), "code", "a.csv") \
        == staging / "run-1" / "example" / "code-analytics" / "a.csv"
    assert staged_file(staging, "run-1", _target("example2", "main"), "members", "b.csv") \
        == staging / "run-1" / "example2" / "main" / "members" / "b.csv"


# ------------------------------------------------------------------ 検証


SPEND_HEADER = "user_email,model,product,total_prompt_tokens,total_completion_tokens"
MEMBERS_HEADER = "Email,Seat Tier,Status"
CODE_HEADER = "User,Lines this month,PRs with CC"


def _csv(directory: Path, name: str, header: str, body: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(header + "\n" + body, encoding="utf-8", newline="\n")
    return path


def _verify(path: Path, kind: str, month: str):
    return verify_export(path, kind, month, columns_aliases=COLUMNS)


@pytest.mark.parametrize("kind,name,header,month", [
    ("spend", f"spend-report-{UUID1}-2026-09-01-to-2026-09-30.csv", SPEND_HEADER, "2026-09"),
    # 当月の部分月
    ("spend", f"spend-report-{UUID1}-2026-10-01-to-2026-10-06.csv", SPEND_HEADER, "2026-10"),
    # Claude Code analytics の終了日は部分月でも月末日
    ("code", f"claude-code-{UUID1}-2026-10-01-to-2026-10-31.csv", CODE_HEADER, "2026-10"),
    ("code", f"claude-code-{UUID1}-2026-09-01-to-2026-09-30.csv", CODE_HEADER, "2026-09"),
    # 前月モードのメンバー一覧は当日（翌月）の日付になる
    ("members", f"members-{UUID1}-2026-10-07.csv", MEMBERS_HEADER, "2026-09"),
    ("members", f"members-{UUID1}-2026-10-07.csv", MEMBERS_HEADER, "2026-10"),
])
def test_verify_export_accepts_matching_files(tmp_path, kind, name, header, month):
    assert _verify(_csv(tmp_path, name, header, "user1@example.com,x\n"), kind, month) \
        == claude_export.Verdict(True, None)


def test_verify_export_accepts_quoted_and_bom_headers(tmp_path):
    path = tmp_path / f"members-{UUID1}-2026-10-07.csv"
    path.write_bytes('\ufeff"User Email", "Seat Type"\nuser1@example.com,Premium\n'
                     .encode("utf-8"))
    assert _verify(path, "members", "2026-10").ok


def test_verify_export_rejects_a_missing_file(tmp_path):
    verdict = _verify(tmp_path / "spend_2026-09-01_to_2026-09-30.csv", "spend", "2026-09")
    assert not verdict.ok and "ありません" in verdict.reason


def test_verify_export_rejects_an_empty_file(tmp_path):
    path = tmp_path / "spend-2026-09-01-to-2026-09-30.csv"
    path.write_bytes(b"")
    verdict = _verify(path, "spend", "2026-09")
    assert not verdict.ok and "空" in verdict.reason


@pytest.mark.parametrize("kind,header,missing,section", [
    ("spend", "model,requests", "email", "spend"),
    # 欠けた列が複数あれば、必須列の並びで最初の 1 列を理由にする
    ("spend", "user_email,model", "prompt_tokens", "spend"),
    ("members", "Email,Status", "seat_type", "members"),
    ("code", "User", "loc_with_cc", "code_analytics"),
])
def test_verify_export_rejects_a_header_without_a_required_column(
    tmp_path, kind, header, missing, section
):
    """分析の読み込みが必須にする列（Claude Code は LoC の列も）が欠けた CSV を配置しない。"""
    names = {"spend": "spend-2026-09-01-to-2026-09-30.csv",
             "members": "members-2026-10-07.csv",
             "code": "code-2026-09-01-to-2026-09-30.csv"}
    verdict = _verify(_csv(tmp_path, names[kind], header), kind, "2026-09")
    assert not verdict.ok
    assert f"のヘッダに {missing} に当たる列がありません" in verdict.reason
    assert f"columns.{section}.{missing} のエイリアスと一致しません" in verdict.reason


def test_verify_export_rejects_a_code_analytics_csv_as_spend(tmp_path):
    """Claude Code の CSV を支出レポートとして配置しない（期間付きの名前は同じ形）。"""
    path = _csv(tmp_path, f"claude-code-{UUID1}-2026-09-01-to-2026-09-30.csv", CODE_HEADER,
                "user1@example.com,120,1\n")
    assert _verify(path, "code", "2026-09").ok
    verdict = _verify(path, "spend", "2026-09")
    assert not verdict.ok
    assert "支出レポートのヘッダに model に当たる列がありません" in verdict.reason


def test_verify_export_rejects_a_spend_csv_as_code_analytics(tmp_path):
    """支出レポートを Claude Code analytics として配置しない（LoC の列が無い）。"""
    path = _csv(tmp_path, f"spend-report-{UUID1}-2026-09-01-to-2026-09-30.csv", SPEND_HEADER,
                "user1@example.com,claude-sonnet-4-6,Chat,10,20\n")
    assert _verify(path, "spend", "2026-09").ok
    verdict = _verify(path, "code", "2026-09")
    assert not verdict.ok
    assert "Claude Code analyticsのヘッダに loc_with_cc に当たる列がありません" \
        in verdict.reason


def test_verify_export_rejects_a_header_longer_than_the_limit(tmp_path):
    """上限を超えたヘッダは途中で切れたものとして扱い、照合しない。"""
    header = SPEND_HEADER + "," + "x" * (64 * 1024)
    path = _csv(tmp_path, "spend-2026-09-01-to-2026-09-30.csv", header,
                "user1@example.com,claude-sonnet-4-6,Chat,10,20\n")
    verdict = _verify(path, "spend", "2026-09")
    assert verdict == claude_export.Verdict(False, "支出レポートのヘッダが長すぎます（64 KiB 以内）")


def test_verify_export_accepts_a_header_just_under_the_limit(tmp_path):
    header = SPEND_HEADER + "," + "x" * (64 * 1024 - len(SPEND_HEADER) - 2)
    path = _csv(tmp_path, "spend-2026-09-01-to-2026-09-30.csv", header)
    assert len(header) + 1 == 64 * 1024   # 改行まで含めてちょうど上限
    assert _verify(path, "spend", "2026-09").ok


def test_verify_export_rejects_a_header_that_is_not_utf8(tmp_path):
    path = tmp_path / "spend-2026-09-01-to-2026-09-30.csv"
    path.write_bytes("メール,model\n".encode("cp932"))
    verdict = _verify(path, "spend", "2026-09")
    assert not verdict.ok and "UTF-8" in verdict.reason


@pytest.mark.parametrize("kind,name,header,month,fragment", [
    # 支出レポートの開始が 1 日でない
    ("spend", "spend-2026-09-02-to-2026-09-30.csv", SPEND_HEADER, "2026-09", "1日から"),
    # 終了が翌月（月をまたぐ期間）
    ("spend", "spend-2026-09-01-to-2026-10-01.csv", SPEND_HEADER, "2026-09", "月をまたぐ"),
    # 別の月
    ("spend", "spend-2026-08-01-to-2026-08-31.csv", SPEND_HEADER, "2026-09", "1日から"),
    # 期間の無いファイル名
    ("spend", "spend_2026-09.csv", SPEND_HEADER, "2026-09", "期間"),
    ("spend", "spend.csv", SPEND_HEADER, "2026-09", "期間を読み取れません"),
    # Claude Code analytics の開始が 1 日でない
    ("code", "code-2026-09-15-to-2026-09-30.csv", CODE_HEADER, "2026-09", "1日から"),
    ("code", "code-2026-09-30.csv", CODE_HEADER, "2026-09", "期間"),
    # メンバー一覧の日付が対象月より前
    ("members", "members-2026-08-31.csv", MEMBERS_HEADER, "2026-09", "より前"),
    ("members", "members-2026-09-01-to-2026-09-30.csv", MEMBERS_HEADER, "2026-09",
     "スナップショットの日付"),
])
def test_verify_export_rejects_periods_that_do_not_match(
    tmp_path, kind, name, header, month, fragment
):
    verdict = _verify(_csv(tmp_path, name, header, "user1@example.com\n"), kind, month)
    assert not verdict.ok
    assert fragment in verdict.reason


# ------------------------------------------------------------------ 配置


def _staged(tmp_path: Path, name: str = "spend-2026-09-01-to-2026-09-30.csv",
            text: str = "new\n") -> Path:
    path = tmp_path / "staging" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def test_place_export_copies_into_a_single_space_org(tmp_path):
    input_dir = tmp_path / "input"
    (input_dir / "example").mkdir(parents=True)
    src = _staged(tmp_path, text="a,b\r\n1,2\r\n")

    dest = place_export(src, input_dir, _target("example"), "spend")

    assert dest == input_dir / "example" / "spend" / src.name
    assert dest.read_bytes() == src.read_bytes()   # 改行も含めてそのまま
    assert src.exists()                             # staging の元は残す


def test_place_export_copies_into_a_workspace(tmp_path):
    input_dir = tmp_path / "input"
    (input_dir / "example2" / "main").mkdir(parents=True)
    src = _staged(tmp_path, f"claude-code-{UUID2}-2026-09-01-to-2026-09-30.csv")

    dest = place_export(src, input_dir, _target("example2", "main"), "code")

    assert dest == input_dir / "example2" / "main" / "code-analytics" / src.name


def test_place_export_overwrites_the_same_name_and_leaves_no_temporary_file(tmp_path):
    input_dir = tmp_path / "input"
    kind_dir = input_dir / "example" / "members"
    kind_dir.mkdir(parents=True)
    (kind_dir / "members-2026-10-07.csv").write_bytes(b"old\n")
    src = _staged(tmp_path, "members-2026-10-07.csv", "new\n")

    place_export(src, input_dir, _target("example"), "members")

    assert sorted(path.name for path in kind_dir.iterdir()) == ["members-2026-10-07.csv"]
    assert (kind_dir / "members-2026-10-07.csv").read_bytes() == b"new\n"


def test_place_export_does_not_create_an_org_directory(tmp_path):
    """設定の綴り違いで新しい組織ディレクトリを作らない。"""
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    with pytest.raises(ValueError, match="init-org example"):
        place_export(_staged(tmp_path), input_dir, _target("example"), "spend")
    assert list(input_dir.iterdir()) == []


def test_place_export_does_not_create_a_workspace_directory(tmp_path):
    input_dir = tmp_path / "input"
    (input_dir / "example2").mkdir(parents=True)
    with pytest.raises(ValueError, match="--workspaces second"):
        place_export(_staged(tmp_path), input_dir, _target("example2", "second"), "spend")
    assert list((input_dir / "example2").iterdir()) == []


def test_place_export_rejects_names_that_leave_the_input_directory(tmp_path):
    input_dir = tmp_path / "input"
    (tmp_path / "outside").mkdir()
    input_dir.mkdir()
    with pytest.raises(ValueError):
        place_export(_staged(tmp_path), input_dir, _target("../outside"), "spend")
    assert list((tmp_path / "outside").iterdir()) == []


def test_place_export_cleans_up_after_a_failed_copy(tmp_path):
    input_dir = tmp_path / "input"
    (input_dir / "example").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        place_export(tmp_path / "staging" / "missing.csv", input_dir, _target("example"),
                     "spend")
    assert list((input_dir / "example" / "spend").iterdir()) == []


# ------------------------------------------------------------------ Preferences


NOW_US = 13_400_000_000_000_000
PATTERN = "https://claude.ai:443,*"


def test_chrome_time_counts_microseconds_from_1601():
    assert chrome_time_us(0) == 11_644_473_600_000_000
    assert chrome_time_us(1.5) == 11_644_473_601_500_000


def test_profile_preferences_path(tmp_path):
    assert profile_preferences_path(tmp_path, "corp") \
        == tmp_path / "corp" / "Default" / "Preferences"


def test_update_preferences_writes_the_settings_into_an_empty_profile(tmp_path):
    prefs: dict = {}
    assert update_preferences(prefs, staging_dir=tmp_path / "exports", now_chrome_us=NOW_US)
    assert prefs == {
        "profile": {"content_settings": {"exceptions": {"automatic_downloads": {
            PATTERN: {"expiration": "0", "last_modified": str(NOW_US), "model": 0,
                      "setting": 1},
        }}}},
        "download": {
            "prompt_for_download": False,
            "default_directory": str(tmp_path / "exports"),
            "directory_upgrade": True,
        },
    }


def test_update_preferences_is_idempotent(tmp_path):
    """2 回目は時刻が違っても書き換えない（last_modified の差だけで変更にしない）。"""
    prefs: dict = {}
    update_preferences(prefs, staging_dir=tmp_path, now_chrome_us=NOW_US)
    snapshot = json.dumps(prefs, sort_keys=True)
    assert not update_preferences(prefs, staging_dir=tmp_path, now_chrome_us=NOW_US + 1)
    assert json.dumps(prefs, sort_keys=True) == snapshot


def test_update_preferences_keeps_other_keys(tmp_path):
    other_site = {"expiration": "0", "last_modified": "1", "model": 0, "setting": 2}
    prefs = {
        "browser": {"has_seen_welcome_page": True},
        "profile": {
            "name": "Person 1",
            "content_settings": {"exceptions": {
                "automatic_downloads": {"https://example.com:443,*": other_site},
                "cookies": {"x": {}},
            }},
        },
        "download": {"extensions_to_open": ""},
    }
    assert update_preferences(prefs, staging_dir=tmp_path, now_chrome_us=NOW_US)
    assert prefs["browser"] == {"has_seen_welcome_page": True}
    assert prefs["profile"]["name"] == "Person 1"
    exceptions = prefs["profile"]["content_settings"]["exceptions"]
    assert exceptions["cookies"] == {"x": {}}
    assert exceptions["automatic_downloads"]["https://example.com:443,*"] == other_site
    assert exceptions["automatic_downloads"][PATTERN]["setting"] == 1
    assert prefs["download"]["extensions_to_open"] == ""


def test_update_preferences_follows_a_new_staging_directory(tmp_path):
    prefs: dict = {}
    update_preferences(prefs, staging_dir=tmp_path / "a", now_chrome_us=NOW_US)
    assert update_preferences(prefs, staging_dir=tmp_path / "b", now_chrome_us=NOW_US)
    assert prefs["download"]["default_directory"] == str(tmp_path / "b")


@pytest.mark.parametrize("existing", [
    {"expiration": "0", "last_modified": "1", "model": 0, "setting": 2},    # ブロック
    {"expiration": "0", "last_modified": "1", "model": False, "setting": 1},
    "allow",
])
def test_update_preferences_replaces_a_different_permission(tmp_path, existing):
    prefs = {"profile": {"content_settings": {"exceptions": {
        "automatic_downloads": {PATTERN: existing}}}}}
    assert update_preferences(prefs, staging_dir=tmp_path, now_chrome_us=NOW_US)
    assert prefs["profile"]["content_settings"]["exceptions"]["automatic_downloads"][
        PATTERN] == {"expiration": "0", "last_modified": str(NOW_US), "model": 0, "setting": 1}


def test_update_preferences_rejects_an_unexpected_shape(tmp_path):
    with pytest.raises(ValueError, match="download"):
        update_preferences({"download": "x"}, staging_dir=tmp_path, now_chrome_us=NOW_US)


def test_write_preferences_backs_up_the_original(tmp_path):
    path = tmp_path / "corp" / "Default" / "Preferences"
    path.parent.mkdir(parents=True)
    original = b'{"browser":{"has_seen_welcome_page":true},"intl":{"accept_languages":"ja"}}'
    path.write_bytes(original)
    staging = tmp_path / "ダウンロード"

    assert write_preferences(path, staging_dir=staging, now=0.0)

    assert (path.parent / "Preferences.bak").read_bytes() == original
    text = path.read_bytes().decode("utf-8")
    # 日本語はエスケープせずに書き（ensure_ascii=False）、改行は LF のまま
    assert "ダウンロード" in text and "\r" not in text
    written = json.loads(text)
    assert written["browser"] == {"has_seen_welcome_page": True}
    assert written["download"]["default_directory"] == str(staging)
    assert sorted(entry.name for entry in path.parent.iterdir()) \
        == ["Preferences", "Preferences.bak"]

    # 変更が無ければ書かない（バックアップも元の内容のまま）
    before = path.read_bytes()
    assert not write_preferences(path, staging_dir=staging, now=1.0)
    assert path.read_bytes() == before
    assert (path.parent / "Preferences.bak").read_bytes() == original


def test_write_preferences_does_not_create_a_missing_file(tmp_path):
    """一度も起動していないプロファイルの Preferences は作らない（Chrome に作らせる）。"""
    path = tmp_path / "corp" / "Default" / "Preferences"
    with pytest.raises(ValueError, match="プロファイルを Chrome で一度起動してから"):
        write_preferences(path, staging_dir=tmp_path / "exports", now=0.0)
    assert not (tmp_path / "corp").exists()


def test_write_preferences_rejects_a_broken_file(tmp_path):
    path = tmp_path / "Preferences"
    path.write_bytes(b"{broken")
    with pytest.raises(ValueError, match="JSON"):
        write_preferences(path, staging_dir=tmp_path, now=0.0)
    assert path.read_bytes() == b"{broken"


# ------------------------------------------------------------------ Chrome の場所


def _files(*existing: Path):
    """存在するファイルを固定し、問い合わせた順を記録する is_file。"""
    asked: list[Path] = []

    def is_file(path: Path) -> bool:
        asked.append(path)
        return path in existing

    return is_file, asked


def _no_which(name: str) -> None:
    raise AssertionError(f"which を呼ばない: {name}")


MAC_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def test_find_chrome_prefers_the_configured_path(tmp_path):
    chrome = tmp_path / "chrome"
    is_file, asked = _files(chrome, MAC_CHROME)
    assert find_chrome(str(chrome), platform="darwin", environ={}, is_file=is_file,
                       which=_no_which) == chrome
    assert asked == [chrome]


def test_find_chrome_does_not_fall_back_when_the_configured_path_is_missing(tmp_path):
    """設定した場所に無ければ、既定の場所にあっても使わない（設定の誤りを隠さない）。"""
    is_file, _ = _files(MAC_CHROME)
    assert find_chrome(str(tmp_path / "missing"), platform="darwin", environ={},
                       is_file=is_file, which=_no_which) is None


def test_find_chrome_expands_the_home_directory():
    expected = Path("~/bin/chrome").expanduser()
    is_file, _ = _files(expected)
    assert find_chrome("~/bin/chrome", platform="linux", environ={}, is_file=is_file,
                       which=_no_which) == expected


def test_find_chrome_on_macos():
    is_file, _ = _files(MAC_CHROME)
    assert find_chrome("", platform="darwin", environ={}, is_file=is_file,
                       which=_no_which) == MAC_CHROME
    is_file, _ = _files()
    assert find_chrome("", platform="darwin", environ={}, is_file=is_file,
                       which=_no_which) is None


def test_find_chrome_on_windows_tries_the_install_locations_in_order():
    environ = {
        "ProgramFiles": r"C:\Program Files",
        "ProgramFiles(x86)": r"C:\Program Files (x86)",
        "LOCALAPPDATA": r"C:\Users\user\AppData\Local",
    }
    parts = ("Google", "Chrome", "Application", "chrome.exe")
    candidates = [Path(environ[key], *parts)
                  for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    is_file, asked = _files(candidates[2])
    assert find_chrome("", platform="win32", environ=environ, is_file=is_file,
                       which=_no_which) == candidates[2]
    assert asked == candidates

    # 定義されていない環境変数は飛ばす
    is_file, asked = _files()
    assert find_chrome("", platform="win32", environ={"LOCALAPPDATA": environ["LOCALAPPDATA"]},
                       is_file=is_file, which=_no_which) is None
    assert asked == [candidates[2]]


def test_find_chrome_on_linux_searches_the_path_in_order():
    asked: list[str] = []

    def which(name: str) -> str | None:
        asked.append(name)
        return "/usr/bin/chromium" if name == "chromium" else None

    assert find_chrome("", platform="linux", environ={}, is_file=_files()[0],
                       which=which) == Path("/usr/bin/chromium")
    assert asked == ["google-chrome", "google-chrome-stable", "chromium"]

    asked.clear()
    assert find_chrome("", platform="linux", environ={}, is_file=_files()[0],
                       which=lambda name: asked.append(name)) is None
    assert asked == ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]


# ------------------------------------------------------------------ 起動・列挙・終了のコマンド


def test_launch_command(tmp_path):
    chrome = tmp_path / "chrome"
    profile = tmp_path / "profiles" / "corp"
    url = TRIGGER_PREFIX + "%7B%7D"
    assert launch_command(chrome, profile, url) == [
        str(chrome), f"--user-data-dir={profile}", "--no-first-run",
        "--no-default-browser-check", url,
    ]


def test_process_listing_command():
    assert process_listing_command("darwin") == ["ps", "-axo", "pid=,command="]
    assert process_listing_command("linux") == ["ps", "-axo", "pid=,command="]
    assert process_listing_command("win32") == [
        "powershell", "-NoProfile", "-Command",
        (
            "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine"
            " | ConvertTo-Csv -NoTypeInformation"
        ),
    ]


def test_terminate_commands():
    assert terminate_commands([10, 11], platform="darwin", force=False) is None
    assert terminate_commands([10, 11], platform="linux", force=True) is None
    assert terminate_commands([10, 11], platform="win32", force=False) == [
        ["taskkill", "/PID", "10"], ["taskkill", "/PID", "11"],
    ]
    assert terminate_commands([10], platform="win32", force=True) == [
        ["taskkill", "/PID", "10", "/F"],
    ]


UNIX_PROFILE = PurePosixPath("/home/user/.seat-analyzer/profiles/corp")
CHROME_BIN = "/opt/google/chrome/chrome"

PS_LISTING = f"""\
    1 /sbin/init
  101 {CHROME_BIN} --user-data-dir={UNIX_PROFILE} --no-first-run --no-default-browser-check {TRIGGER_PREFIX}%7B%7D
  102 {CHROME_BIN} --type=renderer --user-data-dir={UNIX_PROFILE} --lang=ja
  103 {CHROME_BIN} --user-data-dir={UNIX_PROFILE}2 --no-first-run
  104 {CHROME_BIN}
  105 {CHROME_BIN} --user-data-dir=/home/user/.config/google-chrome
  106 /usr/bin/vim {UNIX_PROFILE}/notes.txt
  107 {CHROME_BIN} --no-first-run --user-data-dir={UNIX_PROFILE}
not-a-pid line
"""


def test_chrome_pids_from_ps():
    """そのプロファイルの親プロセスだけを拾う（子・別プロファイル・前方一致を除く）。"""
    assert chrome_pids(PS_LISTING, UNIX_PROFILE, platform="linux") == [101, 107]
    assert chrome_pids(PS_LISTING, str(UNIX_PROFILE), platform="darwin") == [101, 107]


def test_chrome_pids_with_a_space_in_the_profile_path():
    """プロファイルの置き場の途中に空白があっても拾う（ps は引数の区切りを残さない）。"""
    profile = PurePosixPath("/home/user/My Profiles/corp")
    listing = (f"  201 {CHROME_BIN} --user-data-dir={profile} --no-first-run\n"
               f"  202 {CHROME_BIN} --user-data-dir=/home/user/My --no-first-run\n")
    assert chrome_pids(listing, profile, platform="darwin") == [201]


WIN_PROFILE = PureWindowsPath(r"C:\Users\user\.seat-analyzer\profiles\corp")
WIN_CHROME = r'""C:\Program Files\Google\Chrome\Application\chrome.exe""'

WIN_LISTING = "\r\n".join([
    '"ProcessId","CommandLine"',
    '"4",""',
    f'"2001","{WIN_CHROME} --user-data-dir={WIN_PROFILE} --no-first-run {TRIGGER_PREFIX}x"',
    f'"2002","{WIN_CHROME} --type=renderer --user-data-dir=""{WIN_PROFILE}"" --lang=ja"',
    f'"2003","{WIN_CHROME} ""--user-data-dir={WIN_PROFILE}2"""',
    rf'"2004","{WIN_CHROME} --user-data-dir=""c:\users\user\.seat-analyzer\profiles\corp"" --x"',
    f'"2005","{WIN_CHROME} ""--user-data-dir={WIN_PROFILE}"""',
    f'"2006","{WIN_CHROME}"',
    "",
])


def test_chrome_pids_from_powershell_csv():
    """引用符つき・大文字小文字の違いも同じプロファイルとして拾い、子と別プロファイルは除く。"""
    assert chrome_pids(WIN_LISTING, WIN_PROFILE, platform="win32") == [2001, 2004, 2005]


def test_chrome_pids_ignore_a_profile_whose_path_extends_the_target_with_a_space():
    """`corp backup` のように対象のパスに空白と別名が続くプロファイルは拾わない。"""
    listing = "\n".join([
        f"  301 {CHROME_BIN} --user-data-dir={UNIX_PROFILE} backup --no-first-run",
        f"  302 {CHROME_BIN} --user-data-dir={UNIX_PROFILE} backup",
        # 値の後に次のフラグではなく別の語が来る形は、引数の終わりとみなさない
        f"  303 {CHROME_BIN} --user-data-dir={UNIX_PROFILE} {TRIGGER_PREFIX}x",
        f"  304 {CHROME_BIN} --user-data-dir={UNIX_PROFILE}\t--no-first-run",
    ])
    assert chrome_pids(listing, UNIX_PROFILE, platform="linux") == [304]

    windows = "\r\n".join([
        '"ProcessId","CommandLine"',
        f'"2101","{WIN_CHROME} --user-data-dir=""{WIN_PROFILE} backup"" --no-first-run"',
        f'"2102","{WIN_CHROME} ""--user-data-dir={WIN_PROFILE} backup"" --no-first-run"',
        f'"2103","{WIN_CHROME} --user-data-dir={WIN_PROFILE} backup --no-first-run"',
        f'"2104","{WIN_CHROME} --user-data-dir=""{WIN_PROFILE}"" --no-first-run"',
    ])
    assert chrome_pids(windows, WIN_PROFILE, platform="win32") == [2104]


def test_chrome_pids_without_a_powershell_header():
    """ヘッダの無い出力（PowerShell が動かなかった等）からは何も拾わない。"""
    listing = f'"2001","{WIN_CHROME} --user-data-dir={WIN_PROFILE}"\r\n'
    assert chrome_pids(listing, WIN_PROFILE, platform="win32") == []
