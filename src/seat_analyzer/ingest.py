"""入力 CSV（スペンドレポート / メンバー一覧 / Claude Code 分析）のロード。

ヘッダは正規化（小文字化・空白/アンダースコア統一）してから config.yaml の
エイリアス表と照合するため、実ファイルのカラム名差異はエイリアス追記で吸収できる。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

MONTH_RE = re.compile(r"(20\d{2}-(?:0[1-9]|1[0-2]))")


@dataclass
class LoadResult:
    """1ファイル分のロード結果。warnings はレポートに転記する。"""

    df: pd.DataFrame
    source: Path
    warnings: list[str] = field(default_factory=list)


def normalize_header(name: str) -> str:
    s = str(name).strip().lower()
    s = s.replace("_", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def map_columns(
    df: pd.DataFrame,
    aliases: dict[str, list[str]],
    required: list[str],
    source: Path,
) -> tuple[pd.DataFrame, list[str]]:
    """正規化ヘッダをエイリアス表で正準名にリネームする。"""
    warnings: list[str] = []
    normalized = {col: normalize_header(col) for col in df.columns}
    rename: dict[str, str] = {}
    for canonical, alias_list in aliases.items():
        candidates = {normalize_header(a) for a in alias_list} | {normalize_header(canonical)}
        for col, norm in normalized.items():
            if norm in candidates and canonical not in rename.values():
                rename[col] = canonical
                break
    out = df.rename(columns=rename)

    missing = [c for c in required if c not in out.columns]
    if missing:
        raise ValueError(
            f"{source}: 必須カラムが見つかりません: {missing}\n"
            f"  実ファイルのヘッダ: {list(df.columns)}\n"
            f"  config.yaml > columns にエイリアスを追記してください"
        )
    optional_missing = [c for c in aliases if c not in out.columns and c not in required]
    if optional_missing:
        warnings.append(f"{source.name}: 任意カラムなし: {optional_missing}")
    return out, warnings


def month_of_file(path: Path) -> str | None:
    m = MONTH_RE.search(path.name)
    return m.group(1) if m else None


def _files_by_month(directory: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    if not directory.exists():
        return result
    for p in sorted(directory.glob("*.csv")):
        month = month_of_file(p)
        if month:
            result[month] = p
    return result


def discover_months(input_dir: Path) -> list[str]:
    """スペンドレポートが存在する月の一覧（昇順）。"""
    return sorted(_files_by_month(Path(input_dir) / "spend"))


def _read_csv(path: Path) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path}: 文字コードを判別できません（utf-8 / cp932 を試行）")


def _to_numeric(df: pd.DataFrame, cols: list[str]) -> None:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(
                df[c].astype(str).str.replace(",", "").str.replace("$", ""),
                errors="coerce",
            )


def load_spend(input_dir: Path, month: str, cfg: dict) -> LoadResult:
    files = _files_by_month(Path(input_dir) / "spend")
    if month not in files:
        raise FileNotFoundError(
            f"{input_dir}/spend/ に {month} のスペンドレポートがありません"
            f"（例: spend_{month}.csv）。存在する月: {sorted(files) or 'なし'}"
        )
    path = files[month]
    df = _read_csv(path)
    df, warnings = map_columns(
        df,
        cfg["columns"]["spend"],
        required=["email", "model", "prompt_tokens", "completion_tokens"],
        source=path,
    )
    _to_numeric(df, [
        "requests", "prompt_tokens", "completion_tokens", "net_spend", "gross_spend",
        "uncached_input_tokens", "cache_read_tokens",
        "cache_write_5m_tokens", "cache_write_1h_tokens",
    ])
    df["email"] = df["email"].astype(str).str.strip().str.lower()
    df["month"] = month
    return LoadResult(df=df, source=path, warnings=warnings)


def _normalize_seat(value: str) -> str:
    s = str(value).strip().lower()
    if "premium" in s:
        return "premium"
    if "standard" in s or s in ("member", "basic"):
        return "standard"
    return "unknown"


def load_members(input_dir: Path, month: str, cfg: dict) -> LoadResult:
    """対象月のメンバー一覧。無ければ直近の過去月にフォールバック（警告付き）。"""
    files = _files_by_month(Path(input_dir) / "members")
    warnings: list[str] = []
    if not files:
        raise FileNotFoundError(
            f"{input_dir}/members/ にメンバー一覧がありません"
            f"（例: members_{month}.csv。最低限 email,seat_type の2列で可）"
        )
    if month in files:
        path = files[month]
    else:
        earlier = [m for m in sorted(files) if m <= month]
        path = files[earlier[-1]] if earlier else files[sorted(files)[0]]
        warnings.append(
            f"members: {month} のファイルが無いため {path.name} を使用（シート構成が最新でない可能性）"
        )
    df = _read_csv(path)
    df, w = map_columns(
        df,
        cfg["columns"]["members"],
        required=["email", "seat_type"],
        source=path,
    )
    warnings.extend(w)
    df["email"] = df["email"].astype(str).str.strip().str.lower()
    df["seat_type"] = df["seat_type"].map(_normalize_seat)
    unknown = df[df["seat_type"] == "unknown"]
    if not unknown.empty:
        warnings.append(
            f"members: シート種別を判別できないユーザ {len(unknown)} 名"
            f"（値に premium/standard を含まない）: {unknown['email'].head(5).tolist()}"
        )
    df = df.drop_duplicates(subset="email", keep="last")
    return LoadResult(df=df, source=path, warnings=warnings)


def load_code_analytics(input_dir: Path, month: str, cfg: dict) -> LoadResult | None:
    """Claude Code 貢献データ（任意）。無ければ None。"""
    files = _files_by_month(Path(input_dir) / "code-analytics")
    if month not in files:
        return None
    path = files[month]
    df = _read_csv(path)
    df, warnings = map_columns(
        df,
        cfg["columns"]["code_analytics"],
        required=["email"],
        source=path,
    )
    _to_numeric(df, ["prs_with_cc", "prs_total", "loc_with_cc", "loc_total"])
    df["email"] = df["email"].astype(str).str.strip().str.lower()
    df = df.drop_duplicates(subset="email", keep="last")
    return LoadResult(df=df, source=path, warnings=warnings)
