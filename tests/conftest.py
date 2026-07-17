from pathlib import Path

import pytest

from seat_analyzer.config import load_config

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture
def cfg() -> dict:
    return load_config(REPO_ROOT / "config.yaml")


SPEND_HEADER = (
    "Email,Account UUID,Product,Model,Model Family,Request Count,"
    "Prompt Tokens,Completion Tokens,Total Gross Spend USD,Total Net Spend USD"
)


def spend_row(email: str, cost: float, model: str = "claude-sonnet-4-6",
              product: str = "Claude Code", net: float | None = None) -> str:
    """tokens×単価 = cost になる行を生成（入力:出力=10:1）。net で spend 列を上書き可。"""
    prices = {"opus": (5.0, 25.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}
    inp, outp = next(v for k, v in prices.items() if k in model)
    completion = cost / ((10 * inp + outp) / 1e6)
    p_tok, c_tok = int(completion * 10), int(completion)
    net_val = cost if net is None else net
    return (
        f"{email},uuid-x,{product},{model},{model.rsplit('-', 2)[0]},"
        f"10,{p_tok},{c_tok},{net_val:.4f},{net_val:.4f}"
    )


@pytest.fixture
def make_input(tmp_path: Path):
    """input ディレクトリを組み立てるヘルパ。

    org=None で旧レイアウト（input/spend 直下）、org 指定で input/<org>/spend 配下に
    生成する。複数回呼べば同じ input/ にマルチ組織構成を組み立てられる。
    戻り値は常に input/ のルート（旧レイアウトでは組織ディレクトリを兼ねる）。
    """

    def _make(spend_by_month: dict[str, list[str]], members: list[str] | None = None,
              members_month: str = "2026-06", org: str | None = None) -> Path:
        input_dir = tmp_path / "input"
        base = input_dir / org if org else input_dir
        for month, rows in spend_by_month.items():
            p = base / "spend" / f"spend_{month}.csv"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(SPEND_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
        if members is not None:
            p = base / "members" / f"members_{members_month}.csv"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("Email,Seat Type\n" + "\n".join(members) + "\n", encoding="utf-8")
        return input_dir

    return _make


@pytest.fixture
def make_snapshots(tmp_path: Path):
    """同一月の複数スナップショット（月初開始 range 命名）を組み立てるヘルパ。

    snapshots は {終了日 "YYYY-MM-DD": [行, ...]} で、月初〜終了日の累積エクスポートを
    claude.ai のダウンロード名（spend-report-...-月初-to-終了日.csv）で置く。
    extra_files は {ファイル名: [行, ...]} で、月初開始でない range 等の追加ファイルを置く。
    """

    def _make(month: str, snapshots: dict[str, list[str]],
              members: list[str] | None = None, members_month: str | None = None,
              org: str | None = None, extra_files: dict[str, list[str]] | None = None) -> Path:
        input_dir = tmp_path / "input"
        base = input_dir / org if org else input_dir
        spend_dir = base / "spend"
        spend_dir.mkdir(parents=True, exist_ok=True)
        for end, rows in snapshots.items():
            name = f"spend-report-uuid-{month}-01-to-{end}.csv"
            (spend_dir / name).write_text(
                SPEND_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
        for name, rows in (extra_files or {}).items():
            (spend_dir / name).write_text(
                SPEND_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
        if members is not None:
            p = base / "members" / f"members_{members_month or month}.csv"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("Email,Seat Type\n" + "\n".join(members) + "\n", encoding="utf-8")
        return input_dir

    return _make


@pytest.fixture
def write_member_snapshots():
    """members の単日スナップショット（日付命名）を既存 input_dir に追加するヘルパ。

    snapshots は {日付 "YYYY-MM-DD": ["email,seat", ...]} で、日付付きのファイル名
    （members-snap-YYYY-MM-DD.csv）で置く。kind=date として月中のメンバー変動差分に使う。
    """

    def _make(input_dir: Path, snapshots: dict[str, list[str]], org: str | None = None) -> None:
        base = input_dir / org if org else input_dir
        d = base / "members"
        d.mkdir(parents=True, exist_ok=True)
        for date, rows in snapshots.items():
            (d / f"members-snap-{date}.csv").write_text(
                "Email,Seat Type\n" + "\n".join(rows) + "\n", encoding="utf-8")

    return _make


@pytest.fixture
def write_code_snapshots():
    """code-analytics の単日スナップショット（日付命名）を既存 input_dir に追加するヘルパ。

    snapshots は {日付 "YYYY-MM-DD": [(email, loc[, prs]), ...]} で、日付付きの
    ファイル名（cc-snap-YYYY-MM-DD.csv）で置く。with_prs=False で PR 列を省く。
    """

    def _make(input_dir: Path, snapshots: dict[str, list[tuple]],
              org: str | None = None, with_prs: bool = True) -> None:
        base = input_dir / org if org else input_dir
        d = base / "code-analytics"
        d.mkdir(parents=True, exist_ok=True)
        header = "Email,Lines with CC" + (",PRs with CC" if with_prs else "")
        for date, rows in snapshots.items():
            lines = []
            for row in rows:
                lines.append(",".join(str(x) for x in row))
            (d / f"cc-snap-{date}.csv").write_text(
                header + "\n" + "\n".join(lines) + "\n", encoding="utf-8")

    return _make
