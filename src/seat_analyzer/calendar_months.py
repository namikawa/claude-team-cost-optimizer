"""分析と入力検査が共有する暦上の月の計算。"""

from __future__ import annotations


def previous_month(month: str) -> str:
    """YYYY-MM の暦上の直前月。"""
    year, mon = (int(part) for part in month.split("-"))
    return f"{year - 1}-12" if mon == 1 else f"{year}-{mon - 1:02d}"


def judgment_window(month: str, n_months: int) -> tuple[str, ...]:
    """対象月から暦で遡る判定窓を昇順で返す（少なくとも対象月を含む）。"""
    found = [month]
    for _ in range(max(n_months - 1, 0)):
        found.append(previous_month(found[-1]))
    return tuple(reversed(found))
