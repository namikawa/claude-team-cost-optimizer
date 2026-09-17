from pathlib import Path

import pandas as pd
import pytest

from seat_analyzer import ingest
from tests.conftest import spend_row


def test_normalize_header():
    assert ingest.normalize_header("  Total_Net-Spend  USD ") == "total net spend usd"


def test_discover_months_and_load_spend(cfg, make_input):
    input_dir = make_input({
        "2026-05": [spend_row("a@x.jp", 10.0)],
        "2026-06": [spend_row("a@x.jp", 20.0)],
    })
    assert ingest.discover_months(input_dir) == ["2026-05", "2026-06"]

    result = ingest.load_spend(input_dir, "2026-06", cfg)
    df = result.df
    assert {
        "email",
        "account_uuid",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "gross_spend",
        "net_spend",
    } <= set(df.columns)
    assert df["email"].iloc[0] == "a@x.jp"
    assert df["account_uuid"].iloc[0] == "uuid-x"
    assert df["gross_spend"].iloc[0] == pytest.approx(20.0)
    assert df["month"].iloc[0] == "2026-06"


def test_load_spend_maps_current_optional_columns(cfg, tmp_path: Path):
    path = tmp_path / "input" / "spend" / "spend_2026-06.csv"
    path.parent.mkdir(parents=True)
    path.write_text(
        "user_email,account_uuid,product,model,total_requests,total_prompt_tokens,"
        "total_completion_tokens,total_net_spend_usd,total_gross_spend_usd,user_id,"
        "total_web_search_count\n"
        "A@X.JP, 00001234 ,Claude Code,claude-sonnet-4-6,10,1000,100,1.25,2.50,"
        " 00123456 ,3\n",
        encoding="utf-8",
    )

    df = ingest.load_spend(tmp_path / "input", "2026-06", cfg).df

    assert df.loc[0, "email"] == "a@x.jp"
    assert df.loc[0, "account_uuid"] == "00001234"
    assert df.loc[0, "user_id"] == "00123456"
    assert df.loc[0, "gross_spend"] == pytest.approx(2.50)
    assert df.loc[0, "web_search_count"] == 3


def test_load_spend_preserves_id_with_missing_value_in_same_column(cfg, tmp_path: Path):
    path = tmp_path / "input" / "spend" / "spend_2026-06.csv"
    path.parent.mkdir(parents=True)
    path.write_text(
        "Email,Model,Prompt Tokens,Completion Tokens,User ID\n"
        "a@x.jp,claude-sonnet-4-6,1000,100,00123456\n"
        "b@x.jp,claude-sonnet-4-6,2000,200,\n"
        'c@x.jp,claude-sonnet-4-6,3000,300,"   "\n',
        encoding="utf-8",
    )

    df = ingest.load_spend(tmp_path / "input", "2026-06", cfg).df

    assert df.loc[0, "user_id"] == "00123456"
    assert pd.isna(df.loc[1, "user_id"])
    assert pd.isna(df.loc[2, "user_id"])
    assert df["prompt_tokens"].tolist() == [1000, 2000, 3000]


def test_load_spend_adds_na_for_missing_new_optional_columns(cfg, tmp_path: Path):
    path = tmp_path / "input" / "spend" / "spend_2026-06.csv"
    path.parent.mkdir(parents=True)
    path.write_text(
        "Email,Model,Prompt Tokens,Completion Tokens\n"
        "a@x.jp,claude-sonnet-4-6,1000,100\n",
        encoding="utf-8",
    )

    result = ingest.load_spend(tmp_path / "input", "2026-06", cfg)

    assert result.df[list(ingest.SPEND_OPTIONAL_COLUMNS)].isna().all().all()
    assert any("任意カラムなし" in warning for warning in result.warnings)
    assert not any(
        column in warning
        for column in ingest.SPEND_OPTIONAL_COLUMNS
        for warning in result.warnings
    )


def test_load_spend_warns_when_current_optional_columns_are_partially_missing(
    cfg, tmp_path: Path
):
    path = tmp_path / "input" / "spend" / "spend_2026-06.csv"
    path.parent.mkdir(parents=True)
    path.write_text(
        "Email,Account UUID,User ID,Model,Prompt Tokens,Completion Tokens,"
        "Total Gross Spend USD\n"
        "a@x.jp,account-1,user-1,claude-sonnet-4-6,1000,100,2.50\n",
        encoding="utf-8",
    )

    result = ingest.load_spend(tmp_path / "input", "2026-06", cfg)

    assert any("web_search_count" in warning for warning in result.warnings)


def test_spend_optional_columns_do_not_change_v1_fields(cfg, tmp_path: Path):
    legacy = tmp_path / "legacy_2026-06.csv"
    current = tmp_path / "current_2026-06.csv"
    legacy.write_text(
        "Email,Product,Model,Request Count,Prompt Tokens,Completion Tokens,"
        "Total Net Spend USD\n"
        "a@x.jp,Claude Code,claude-sonnet-4-6,10,1000,100,1.25\n",
        encoding="utf-8",
    )
    current.write_text(
        "Email,Account UUID,User ID,Product,Model,Request Count,Prompt Tokens,"
        "Completion Tokens,Total Gross Spend USD,Total Net Spend USD,"
        "Total Web Search Count\n"
        "a@x.jp,account-1,user-1,Claude Code,claude-sonnet-4-6,10,1000,100,"
        "2.50,1.25,3\n",
        encoding="utf-8",
    )
    v1_columns = [
        "email",
        "product",
        "model",
        "requests",
        "prompt_tokens",
        "completion_tokens",
        "net_spend",
        "month",
    ]

    legacy_df = ingest.load_spend_file(legacy, "2026-06", cfg)
    current_df = ingest.load_spend_file(current, "2026-06", cfg)

    pd.testing.assert_frame_equal(
        legacy_df[v1_columns],
        current_df[v1_columns],
        check_dtype=True,
    )


def test_load_spend_missing_month(cfg, make_input):
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 1.0)]})
    with pytest.raises(FileNotFoundError):
        ingest.load_spend(input_dir, "2026-04", cfg)


def test_missing_required_column_raises(cfg, tmp_path: Path):
    p = tmp_path / "input" / "spend" / "spend_2026-06.csv"
    p.parent.mkdir(parents=True)
    p.write_text("Email,Model\na@x.jp,claude-sonnet-4-6\n", encoding="utf-8")
    with pytest.raises(ValueError, match="必須カラム"):
        ingest.load_spend(tmp_path / "input", "2026-06", cfg)


def test_members_seat_normalization(cfg, make_input):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 1.0)]},
        members=["A@x.jp,Premium seat", "b@x.jp,standard", "c@x.jp,???"],
    )
    result = ingest.load_members(input_dir, "2026-06", cfg)
    seats = result.df.set_index("email")["seat_type"].to_dict()
    assert seats == {"a@x.jp": "premium", "b@x.jp": "standard", "c@x.jp": "unknown"}
    assert any("判別できない" in w for w in result.warnings)


def test_load_members_maps_current_optional_columns(cfg, tmp_path: Path):
    path = tmp_path / "input" / "members" / "members_2026-06.csv"
    path.parent.mkdir(parents=True)
    path.write_text(
        "Email,Account UUID,User ID,Seat Tier,Status\n"
        "A@X.JP, 00001234 , 00123456 ,Premium,Active\n"
        'b@x.jp,"   ","   ",Standard,Awaiting verification\n'
        'c@x.jp,account-3,user-3,Standard,"   "\n',
        encoding="utf-8",
    )

    result = ingest.load_members(tmp_path / "input", "2026-06", cfg)
    df = result.df.set_index("email")

    assert df.loc["a@x.jp", "account_uuid"] == "00001234"
    assert df.loc["a@x.jp", "user_id"] == "00123456"
    assert df.loc["a@x.jp", "member_status"] == "Active"
    assert pd.isna(df.loc["b@x.jp", "account_uuid"])
    assert pd.isna(df.loc["b@x.jp", "user_id"])
    assert df.loc["b@x.jp", "member_status"] == "Awaiting verification"
    assert pd.isna(df.loc["c@x.jp", "member_status"])
    assert df["seat_type"].to_dict() == {
        "a@x.jp": "premium",
        "b@x.jp": "standard",
        "c@x.jp": "standard",
    }


def test_load_members_adds_na_for_missing_optional_columns(cfg, tmp_path: Path):
    path = tmp_path / "input" / "members" / "members_2026-06.csv"
    path.parent.mkdir(parents=True)
    path.write_text(
        "Email,Seat Type\n"
        "a@x.jp,Premium\n",
        encoding="utf-8",
    )

    result = ingest.load_members(tmp_path / "input", "2026-06", cfg)

    assert result.df[list(ingest.MEMBERS_OPTIONAL_COLUMNS)].isna().all().all()
    assert not any(
        column in warning
        for column in ingest.MEMBERS_OPTIONAL_COLUMNS
        for warning in result.warnings
    )
    assert result.df["seat_type"].tolist() == ["premium"]


def test_members_optional_columns_do_not_change_v1_fields(cfg, tmp_path: Path):
    legacy = tmp_path / "members-legacy_2026-06.csv"
    current = tmp_path / "members-current_2026-06.csv"
    legacy.write_text(
        "Email,Seat Type\n"
        "a@x.jp,Premium\n",
        encoding="utf-8",
    )
    current.write_text(
        "Email,Account UUID,User ID,Seat Tier,Status\n"
        "a@x.jp,account-1,user-1,Premium,Active\n",
        encoding="utf-8",
    )

    legacy_df = ingest.load_members_file(legacy, cfg)
    current_df = ingest.load_members_file(current, cfg)

    pd.testing.assert_frame_equal(
        legacy_df[["email", "seat_type"]],
        current_df[["email", "seat_type"]],
        check_dtype=True,
    )


def test_members_fallback_to_earlier_month(cfg, make_input):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 1.0)]},
        members=["a@x.jp,premium"],
        members_month="2026-05",
    )
    result = ingest.load_members(input_dir, "2026-06", cfg)
    assert any("フォールバック" in w or "使用" in w for w in result.warnings)
    assert result.df["seat_type"].iloc[0] == "premium"


# --- 複数 workspace のレイアウト（1組織が複数の Team スペースを運用する形） ---


def test_discover_workspaces_lists_children_with_spend(make_input):
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]},
                           org="org-x", workspace="second")
    make_input({"2026-06": [spend_row("a@x.jp", 12.0)]}, org="org-x", workspace="main")
    assert ingest.discover_workspaces(input_dir / "org-x") == ["main", "second"]


def test_discover_workspaces_ignores_children_without_spend(make_input):
    """workspace の判定は構造（spend/ を持つか）。名前では判定しない。"""
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]},
                           members=["a@x.jp,Premium"], org="org-x", workspace="main")
    org_input = input_dir / "org-x"
    for name in ("github-cache", "members", "admin", "code-analytics"):
        (org_input / name).mkdir(parents=True, exist_ok=True)
    (org_input / "members-info.csv").write_text("email\n", encoding="utf-8")
    assert ingest.discover_workspaces(org_input) == ["main"]


def test_discover_workspaces_is_empty_for_traditional_layout(make_input):
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]},
                           members=["a@x.jp,Premium"], org="org-x")
    assert ingest.discover_workspaces(input_dir / "org-x") == []


def test_workspace_layout_single_and_nested(make_input):
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]}, org="org-x")
    make_input({"2026-06": [spend_row("a@x.jp", 10.0)]}, org="org-y", workspace="main")
    assert ingest.workspace_layout(input_dir / "org-x") == "single"
    assert ingest.workspace_layout(input_dir / "org-y") == "nested"
    # 入力がまだ何も無い組織は従来レイアウト扱い（spend/ が無いことは doctor が検査する）
    (input_dir / "org-z").mkdir()
    assert ingest.workspace_layout(input_dir / "org-z") == "single"


def test_workspace_layout_rejects_mixed_layout(make_input, tmp_path):
    """直下と子ディレクトリの両方に spend/ がある形は、どちらとしても読めないので止める。"""
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]}, org="org-x")
    make_input({"2026-06": [spend_row("a@x.jp", 10.0)]}, org="org-x", workspace="second")

    with pytest.raises(ValueError) as excinfo:
        ingest.workspace_layout(input_dir / "org-x", "org-x")
    message = str(excinfo.value)
    assert "org-x" in message and "second" in message
    assert str(tmp_path) not in message      # 絶対パスを含めない

    # 例外を投げない検査用の経路は、混在も種別の1つとして返す
    assert ingest.detect_workspace_layout(input_dir / "org-x") == ("mixed", ["second"])


def test_discover_orgs_includes_nested_layout_orgs(make_input):
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]}, org="org-a")
    make_input({"2026-06": [spend_row("b@x.jp", 10.0)]}, org="org-b", workspace="main")
    (input_dir / "not-an-org").mkdir()
    assert ingest.discover_orgs(input_dir) == ["org-a", "org-b"]


def test_workspace_settings_reads_config(cfg):
    assert ingest.workspace_settings(cfg, "org-x") == {}
    configured = {
        "organizations": {"org-x": {"workspaces": {"main": {"primary": True}}}},
    }
    assert ingest.workspace_settings(configured, "org-x") == {"main": {"primary": True}}
    assert ingest.workspace_settings(configured, "org-y") == {}


def test_compare_workspaces_reports_both_directions():
    """config と入力ディレクトリの食い違いは、向きを分けて昇順で返す。"""
    assert ingest.compare_workspaces(["main", "second"], ["main", "second"]) == ([], [])
    assert ingest.compare_workspaces(["main"], ["main", "second"]) == (["second"], [])
    assert ingest.compare_workspaces(["main", "second"], ["main"]) == ([], ["second"])
    assert ingest.compare_workspaces(["b", "a"], ["c"]) == (["c"], ["a", "b"])
    # 従来レイアウト（発見ゼロ）に設定だけがある場合は、すべて「ディレクトリが無い」側
    assert ingest.compare_workspaces([], ["main", "second"]) == (["main", "second"], [])


def test_empty_spend_has_the_canonical_columns():
    """利用が無かった月の明細は、行が無いだけで列の構成は実データと同じ。"""
    empty = ingest.empty_spend()
    assert empty.empty
    for column in (*ingest.REQUIRED_COLUMNS["spend"], "product", "requests",
                   "net_spend", "account_uuid", "user_id", "month"):
        assert column in empty.columns
