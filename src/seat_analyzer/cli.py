"""CLI エントリポイント: seat-analyzer analyze [--month YYYY-MM]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import analyze as analyze_mod
from . import ingest, report
from .config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="seat-analyzer", description="Claude Team シート最適化分析")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyze", help="スペンドレポートを分析してレポートを生成")
    p.add_argument("--month", help="対象月 (YYYY-MM)。省略時は input/spend の最新月")
    p.add_argument("--config", default="config.yaml", help="設定ファイル (default: config.yaml)")
    p.add_argument("--input-dir", default="input", help="入力ディレクトリ (default: input)")
    p.add_argument("--output-dir", default="reports", help="出力ディレクトリ (default: reports)")

    args = parser.parse_args(argv)
    try:
        return _run_analyze(args)
    except (FileNotFoundError, ValueError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


def _run_analyze(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    input_dir = Path(args.input_dir)

    month = args.month
    if month is None:
        months = ingest.discover_months(input_dir)
        if not months:
            raise FileNotFoundError(
                f"{input_dir}/spend/ にスペンドレポートがありません。"
                "README の月次運用手順に従いエクスポートしてください。"
            )
        month = months[-1]
        print(f"対象月未指定のため最新月を使用: {month}")

    result = analyze_mod.analyze(input_dir, month, cfg)
    paths = report.write_all(result, args.output_dir)

    s = result.summary
    print(f"\n=== {month} 分析結果 ===")
    print(f"メンバー: {s['n_members']} 名 (Standard {s['n_standard']} / Premium {s['n_premium']} / 不明 {s['n_unknown']})")
    print(f"現在のシート費用: ${s['seat_cost_now_usd']:,.2f}/月, API換算利用額: ${s['total_api_cost_usd']:,.2f}/月")
    if s.get("org_service_cost_usd"):
        print(f"組織サービス利用（非帰属）: ${s['org_service_cost_usd']:,.2f}/月")
    print(f"変更推奨: {s['n_change_recommended']} 名 (削減見込み ${s['est_monthly_saving_usd']:,.2f}/月)")
    print(f"要観察: {s['n_watching']} 名, 上限到達疑い: {s['n_cap_suspected']} 名")
    print(f"使用データ: {', '.join(s['months_used'])}")

    if result.warnings:
        print("\n--- 警告 ---")
        for w in result.warnings:
            print(f"  ! {w}")

    print("\n--- 出力 ---")
    for kind, path in paths.items():
        print(f"  {kind}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
