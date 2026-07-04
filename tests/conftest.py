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
    """input ディレクトリを組み立てるヘルパ。"""

    def _make(spend_by_month: dict[str, list[str]], members: list[str] | None = None,
              members_month: str = "2026-06") -> Path:
        input_dir = tmp_path / "input"
        for month, rows in spend_by_month.items():
            p = input_dir / "spend" / f"spend_{month}.csv"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(SPEND_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
        if members is not None:
            p = input_dir / "members" / f"members_{members_month}.csv"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("Email,Seat Type\n" + "\n".join(members) + "\n", encoding="utf-8")
        return input_dir

    return _make
