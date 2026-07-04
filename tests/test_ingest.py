from pathlib import Path

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
    assert set(["email", "model", "prompt_tokens", "completion_tokens", "net_spend"]) <= set(df.columns)
    assert df["email"].iloc[0] == "a@x.jp"
    assert df["month"].iloc[0] == "2026-06"


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


def test_members_fallback_to_earlier_month(cfg, make_input):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 1.0)]},
        members=["a@x.jp,premium"],
        members_month="2026-05",
    )
    result = ingest.load_members(input_dir, "2026-06", cfg)
    assert any("フォールバック" in w or "使用" in w for w in result.warnings)
    assert result.df["seat_type"].iloc[0] == "premium"
