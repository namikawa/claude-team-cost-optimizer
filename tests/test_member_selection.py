"""members の主ファイル選択（対象月末に最も近いスナップショット）のテスト。

月末までのデータは翌月の最初の営業日に取得することが多く、対象月末時点のシート構成は
翌月初のファイルに入っている。そのため選択は月単位ではなくファイル単位で行い、対象月の
末日に最も近いものを採る。spend / code-analytics はこの規則の対象外（対象月のファイル
だけを使う）。
"""

from pathlib import Path

import pytest

from seat_analyzer import ingest
from seat_analyzer.analyze import analyze

from .conftest import SPEND_HEADER, spend_row


def _write_members(input_dir: Path, dates: list[str]) -> Path:
    """指定日付の単日スナップショットを members/ に置く（中身は共通の1名）。"""
    directory = input_dir / "members"
    directory.mkdir(parents=True, exist_ok=True)
    for date in dates:
        (directory / f"members-snap-{date}.csv").write_text(
            "Email,Seat Type\na@x.jp,Standard\n", encoding="utf-8")
    return input_dir


def _members_warning(result: ingest.LoadResult) -> str:
    """members のファイル選択に由来する警告（1件のはず）。"""
    warns = [w for w in result.warnings if w.startswith("members: ")]
    assert len(warns) == 1, warns
    return warns[0]


# --- 選択（対象月 2026-07・末日 07-31） ---

@pytest.mark.parametrize(("dates", "expected"), [
    # 対象月内のみ: 末日に最も近い＝その月の最新
    (["2026-07-16", "2026-07-22", "2026-07-29"], "members-snap-2026-07-29.csv"),
    # 翌月のみ: 末日に最も近い＝その月の最古（月単位で畳むと最新が選ばれてしまう）
    (["2026-08-04", "2026-08-08", "2026-08-15"], "members-snap-2026-08-04.csv"),
    # 前月と翌月: 翌月初のほうが末日に近い
    (["2026-06-30", "2026-08-01"], "members-snap-2026-08-01.csv"),
    # 対象月内にあっても、翌月初のほうが末日に近ければそちらを採る
    (["2026-07-05", "2026-08-01"], "members-snap-2026-08-01.csv"),
    (["2026-06-30"], "members-snap-2026-06-30.csv"),
    # 同距離（末日の1日前と1日後）は末日以前を優先する
    (["2026-07-30", "2026-08-01"], "members-snap-2026-07-30.csv"),
])
def test_selects_file_nearest_to_month_end(cfg, tmp_path, dates, expected):
    input_dir = _write_members(tmp_path / "input", dates)
    assert ingest.load_members(input_dir, "2026-07", cfg).source.name == expected


def test_month_only_file_is_represented_by_month_end(cfg, tmp_path):
    """月のみの命名の代表日はその月の末日（同じ月の単日スナップショットより末日に近い）。"""
    input_dir = _write_members(tmp_path / "input", ["2026-06-05"])
    (input_dir / "members" / "members_2026-06.csv").write_text(
        "Email,Seat Type\na@x.jp,Premium\n", encoding="utf-8")
    result = ingest.load_members(input_dir, "2026-07", cfg)
    assert result.source.name == "members_2026-06.csv"
    assert result.df["seat_type"].tolist() == ["premium"]


# --- 警告の出し分け ---

def test_in_month_selection_keeps_existing_warning(cfg, tmp_path):
    """対象月内から選んだ場合は重複解決の警告だけ（従来どおり）。"""
    input_dir = _write_members(tmp_path / "input", ["2026-07-05", "2026-07-16"])
    result = ingest.load_members(input_dir, "2026-07", cfg)
    assert result.source.name == "members-snap-2026-07-16.csv"
    assert _members_warning(result) == (
        "members: 2026-07 のスナップショットが複数あるため最新の "
        "members-snap-2026-07-16.csv を使用（未使用: members-snap-2026-07-05.csv）"
    )


def test_past_month_selection_keeps_existing_warning(cfg, tmp_path):
    input_dir = _write_members(tmp_path / "input", ["2026-06-30"])
    assert _members_warning(ingest.load_members(input_dir, "2026-07", cfg)) == (
        "members: 2026-07 のファイルが無いため members-snap-2026-06-30.csv を使用"
        "（シート構成が最新でない可能性）"
    )


def test_future_selection_within_export_window_has_no_strong_note(cfg, tmp_path):
    """翌月初の取得は通常運用なので、強い注意は付けない（採らなかった同月分は挙げる）。"""
    input_dir = _write_members(tmp_path / "input", ["2026-08-04", "2026-08-15"])
    assert _members_warning(ingest.load_members(input_dir, "2026-07", cfg)) == (
        "members: 2026-07 月末時点のスナップショットが無いため "
        "members-snap-2026-08-04.csv（月末の 4 日後）を使用"
        "（未使用: members-snap-2026-08-15.csv）"
    )


def test_future_selection_far_from_month_end_warns_strongly(cfg, tmp_path):
    input_dir = _write_members(tmp_path / "input", ["2026-08-12"])
    assert _members_warning(ingest.load_members(input_dir, "2026-07", cfg)) == (
        "members: 2026-07 月末時点のスナップショットが無いため "
        "members-snap-2026-08-12.csv（月末の 12 日後）を使用。"
        "対象月当時のシート構成と異なる可能性が高いため、判定は参考値として扱ってください"
    )


@pytest.mark.parametrize(("date", "strong"), [
    ("2026-08-07", False),   # 末日の7日後まで＝通常運用の幅
    ("2026-08-08", True),
])
def test_strong_note_boundary(cfg, tmp_path, date, strong):
    input_dir = _write_members(tmp_path / "input", [date])
    warning = _members_warning(ingest.load_members(input_dir, "2026-07", cfg))
    assert ("参考値として扱ってください" in warning) is strong


@pytest.mark.parametrize(("name", "expected"), [
    ("members-snap-2026-08-01.csv", True),
    ("members-snap-2026-08-07.csv", True),
    ("members-snap-2026-08-08.csv", False),   # 通常運用の幅を超える
    ("members-snap-2026-07-31.csv", False),   # 末日ちょうど（対象月内）
    ("members-snap-2026-07-16.csv", False),   # 対象月内
    ("members-snap-2026-06-30.csv", False),   # 過去月
    ("members_2026-08.csv", False),           # 代表日は 08-31 で幅の外
    ("members.csv", False),                   # 期間を解釈できない
])
def test_is_near_month_end(name, expected):
    """警告の強さを揃えるための述語（doctor と load_members が共有する）。"""
    assert ingest.is_near_month_end(Path(name), "2026-07") is expected


# --- 月中のメンバー変動との独立性 ---

def test_member_changes_use_in_month_snapshots(cfg, make_snapshots, write_member_snapshots):
    """主ファイルが翌月初になっても、メンバー変動は対象月内のスナップショットで計算する。"""
    input_dir = make_snapshots(
        "2026-07", {"2026-07-31": [spend_row("a@x.jp", 80.0, net=0.0)]},
    )
    write_member_snapshots(input_dir, {
        "2026-07-05": ["a@x.jp,standard"],
        "2026-07-16": ["a@x.jp,premium"],
        "2026-08-03": ["a@x.jp,premium", "b@x.jp,standard"],
    })
    result = analyze(input_dir, "2026-07", cfg, org="org-a")
    assert Path(result.sources["members"]).name == "members-snap-2026-08-03.csv"

    mc = result.member_changes
    assert mc is not None
    assert [s["label"] for s in mc["snaps"]] == ["07-05", "07-16"]
    assert mc["seat_changes"] == [
        {"email": "a@x.jp", "from": "standard", "to": "premium",
         "interval_label": "07-05→07-16"}
    ]
    # 翌月のスナップショットは差分の起点にしない（b@x.jp は加入として挙がらない）
    assert mc["joined"] == [] and mc["left"] == []
    # 主ファイルの構成は判定対象に反映される（b@x.jp は利用ゼロで含まれる）
    assert set(result.users["email"]) == {"a@x.jp", "b@x.jp"}


# --- 他の入力種別への波及がないこと ---

def test_spend_and_code_analytics_do_not_use_other_months(cfg, tmp_path):
    """末日に近いファイルを探すのは members だけ。他は対象月のファイルだけを使う。"""
    input_dir = tmp_path / "input"
    (input_dir / "spend").mkdir(parents=True)
    (input_dir / "spend" / "spend_2026-08.csv").write_text(
        SPEND_HEADER + "\n" + spend_row("a@x.jp", 1.0) + "\n", encoding="utf-8")
    (input_dir / "code-analytics").mkdir(parents=True)
    (input_dir / "code-analytics" / "cc_2026-08.csv").write_text(
        "Email,Lines with CC\na@x.jp,10\n", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        ingest.load_spend(input_dir, "2026-07", cfg)
    assert ingest.load_code_analytics(input_dir, "2026-07", cfg) is None


def test_spend_snapshot_selection_unchanged_with_next_month_files(cfg, tmp_path):
    """同一月の複数スペンドは従来どおり期間の広い方を採る（翌月のファイルがあっても同じ）。"""
    input_dir = tmp_path / "input"
    (input_dir / "spend").mkdir(parents=True)
    for name in ("spend-report-2026-07-01-to-2026-07-05.csv",
                 "spend-report-2026-07-01-to-2026-07-31.csv",
                 "spend-report-2026-08-01-to-2026-08-31.csv"):
        (input_dir / "spend" / name).write_text(
            SPEND_HEADER + "\n" + spend_row("a@x.jp", 1.0) + "\n", encoding="utf-8")
    result = ingest.load_spend(input_dir, "2026-07", cfg)
    assert result.source.name == "spend-report-2026-07-01-to-2026-07-31.csv"
