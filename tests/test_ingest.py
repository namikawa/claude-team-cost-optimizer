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
