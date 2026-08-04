"""CLI エントリポイント: seat-analyzer {analyze,doctor,init-org}"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import analyze, data_quality, ingest, report
from .config import load_config
from .domain import QualityIssue, Severity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="seat-analyzer", description="Claude Team シート最適化分析")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyze", help="スペンドレポートを分析してレポートを生成")
    p.add_argument("--month", help="対象月 (YYYY-MM)。省略時は対象組織の spend の最新月")
    p.add_argument(
        "--org", action="append",
        help="対象組織（input/ 直下のディレクトリ名）。複数指定可。省略時は全組織を分析",
    )
    p.add_argument("--config", default="config.yaml", help="設定ファイル (default: config.yaml)")
    p.add_argument("--input-dir", default="input", help="入力ディレクトリ (default: input)")
    p.add_argument("--output-dir", default="reports", help="出力ディレクトリ (default: reports)")
    p.add_argument(
        "--preview", action="store_true",
        help="速報モード: 部分月データから一次判断のみ行う（変更推奨・ヒステリシスなし）",
    )
    p.add_argument(
        "--days", type=int,
        help="速報モードの観測日数。省略時はスペンドレポートのファイル名の期間から自動判別",
    )
    p.set_defaults(func=_run_analyze)

    pdoc = sub.add_parser("doctor", help="入力データ（スペンド・メンバー一覧）の品質を検査")
    pdoc.add_argument("--month", help="対象月 (YYYY-MM)。省略時は対象組織の spend の最新月")
    pdoc.add_argument(
        "--org", action="append",
        help="対象組織（input/ 直下のディレクトリ名）。複数指定可。省略時は全組織を検査",
    )
    pdoc.add_argument("--config", default="config.yaml", help="設定ファイル (default: config.yaml)")
    pdoc.add_argument("--input-dir", default="input", help="入力ディレクトリ (default: input)")
    pdoc.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="出力形式。json は構造化issueの配列を stdout へ出す (default: text)",
    )
    pdoc.set_defaults(func=_run_doctor)

    pi = sub.add_parser("init-org", help="新しい組織の入力/出力ディレクトリの雛形を作成")
    pi.add_argument("orgs", nargs="+", metavar="組織名",
                    help="作成する組織名（input/ 直下のディレクトリ名になる）。複数指定可")
    pi.add_argument("--input-dir", default="input", help="入力ディレクトリ (default: input)")
    pi.add_argument("--output-dir", default="reports", help="出力ディレクトリ (default: reports)")
    pi.set_defaults(func=_run_init_org)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


INPUT_SUBDIRS = ("spend", "members", "code-analytics")


def _run_init_org(args: argparse.Namespace) -> int:
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    for org in args.orgs:
        ingest.validate_org_name(org)

    for org in args.orgs:
        existed = (input_dir / org).is_dir()
        for subdir in INPUT_SUBDIRS:
            (input_dir / org / subdir).mkdir(parents=True, exist_ok=True)
        (output_dir / org).mkdir(parents=True, exist_ok=True)
        # members-info.csv はヘッダ行のみの雛形を作る。既存（記入済みの可能性）は上書きしない
        info_path = input_dir / org / "members-info.csv"
        info_created = not info_path.exists()
        if info_created:
            info_path.write_text("email,部署,チーム,職種,追加クレジット上限,備考\n", encoding="utf-8")
        print(f"組織 '{org}' の雛形を{'確認しました（既存）' if existed else '作成しました'}:")
        print(f"  {input_dir / org / 'spend'}/           ← spend_YYYY-MM.csv（必須）")
        print(f"  {input_dir / org / 'members'}/         ← members_YYYY-MM.csv（必須。最低限 email,seat_type の2列）")
        print(f"  {input_dir / org / 'code-analytics'}/  ← cc_YYYY-MM.csv（任意）")
        print(f"  {info_path}  ← 部署・チーム・職種・備考の任意マッピング（{'ヘッダ雛形を作成' if info_created else '既存を保持'}）")
        print(f"  {output_dir / org}/")

    if (input_dir / "spend").is_dir():
        print(
            f"\n! 旧レイアウトのデータが {input_dir}/spend/ にあります。"
            f"分析前に {input_dir}/<組織名>/ 配下へ移動してください"
        )
    print("\nCSV 配置後: uv run seat-analyzer analyze （エクスポート手順は README 参照）")
    return 0


def _resolve_targets(
    input_dir: Path, output_dir: Path, org_args: list[str] | None
) -> list[tuple[str | None, Path, Path]]:
    """分析対象の (組織名, 入力dir, 出力dir) を解決する。

    input/<org>/spend/ 型のマルチ組織レイアウトを基本とし、
    input/spend/ 直下型の旧レイアウトは単一組織（org=None）として扱う。
    """
    orgs = ingest.discover_orgs(input_dir)
    legacy = (input_dir / "spend").is_dir()
    if orgs and legacy:
        raise ValueError(
            f"{input_dir} に組織ディレクトリ（{orgs}）と直下の spend/ が混在しています。"
            f"旧レイアウトのデータを {input_dir}/<組織名>/ 配下へ移動してください"
        )
    if not orgs:
        if org_args:
            raise ValueError(
                f"{input_dir} に組織ディレクトリがありません（--org を使うには "
                f"{input_dir}/<組織名>/spend/ の形でデータを配置してください）"
            )
        if not legacy:
            raise FileNotFoundError(
                f"{input_dir} に入力データがありません。{input_dir}/<組織名>/spend/ に"
                "スペンドレポートを配置してください（README の月次運用手順参照）"
            )
        return [(None, input_dir, output_dir)]

    if org_args:
        unknown = [o for o in org_args if o not in orgs]
        if unknown:
            raise ValueError(f"組織が見つかりません: {unknown}。存在する組織: {orgs}")
        selected = list(dict.fromkeys(org_args))
    else:
        selected = orgs
    # 手動作成された不正名ディレクトリ（summary・パス/Markdown を壊す文字等）を弾く
    for org in selected:
        ingest.validate_org_name(org)
    return [(org, input_dir / org, output_dir / org) for org in selected]


def _resolve_input_targets(
    input_dir: Path, org_args: list[str] | None
) -> list[tuple[str | None, Path]]:
    """検査対象の (組織名, 入力dir)。出力を書かないコマンド用に出力dirを落とす。"""
    return [(org, org_input) for org, org_input, _ in
            _resolve_targets(input_dir, input_dir, org_args)]


def _latest_month(org_input: Path) -> str | None:
    """対象組織のスペンドの最新月。ファイル名を解決できない場合は None（doctor が検査する）。"""
    try:
        months = ingest.discover_months(org_input)
    except ValueError:
        return None
    return months[-1] if months else None


def _notice(message: str, as_json: bool) -> None:
    """JSON 出力時は stdout を JSON だけに保つため、通知は stderr へ出す。"""
    print(message, file=sys.stderr if as_json else sys.stdout)


def _run_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    input_dir = Path(args.input_dir)
    as_json = args.format == "json"
    targets = _resolve_input_targets(input_dir, args.org)

    # 対象月: 未指定なら対象組織全体での最新月。1件も無ければ None のまま検査に渡す
    month = args.month
    if month is None:
        latest = [m for _, org_input in targets if (m := _latest_month(org_input))]
        month = max(latest) if latest else None
        if month is not None:
            _notice(f"対象月未指定のため最新月を使用: {month}", as_json)

    all_issues: list[QualityIssue] = []
    for org, org_input in targets:
        issues = data_quality.inspect_input(org_input, month, cfg, org=org)
        all_issues.extend(issues)
        if not as_json:
            _print_issues(org, month, issues)

    n_error = sum(1 for i in all_issues if i.severity is Severity.ERROR)
    if as_json:
        print(data_quality.issues_to_json(all_issues))
    else:
        n_warning = len(all_issues) - n_error
        print(f"\n検査結果: エラー {n_error} 件 / 警告 {n_warning} 件")
    return 1 if n_error else 0


def _print_issues(org: str | None, month: str | None, issues: list[QualityIssue]) -> None:
    scope = " ".join(x for x in (org, month) if x) or "入力"
    print(f"\n=== {scope} 入力検査 ===")
    if not issues:
        print("  問題は見つかりませんでした")
        return
    for issue in issues:
        print(f"  [{issue.severity.value}] {issue.code.value}: {issue.message}")


def _run_analyze(args: argparse.Namespace) -> int:
    if args.days is not None and not args.preview:
        raise ValueError("--days は --preview 専用のオプションです")

    cfg = load_config(args.config)
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)

    targets = _resolve_targets(input_dir, output_dir, args.org)

    # 対象月: 未指定なら対象組織全体での最新月。その月のデータが無い組織はスキップ
    month = args.month
    if month is None:
        latest = [m[-1] for _, d, _ in targets if (m := ingest.discover_months(d))]
        if not latest:
            raise FileNotFoundError(
                "スペンドレポートがありません。README の月次運用手順に従いエクスポートしてください。"
            )
        month = max(latest)
        print(f"対象月未指定のため最新月を使用: {month}")

    results: list[analyze.AnalysisResult] = []
    skipped: list[str] = []
    n_previewed = 0
    for org, org_input, org_output in targets:
        if month not in ingest.discover_months(org_input):
            if len(targets) == 1:
                raise FileNotFoundError(
                    f"{org_input}/spend/ に {month} のスペンドレポートがありません"
                )
            skipped.append(org or str(org_input))
            continue
        if args.preview:
            days = args.days
            if days is None:
                period = ingest.spend_file_period(org_input, month)
                days = period.days if period else None
                if days is None:
                    raise ValueError(
                        f"--days <観測日数> を指定してください"
                        f"（{org or org_input}: スペンドレポートのファイル名に期間が無いため自動判別できません）"
                    )
                print(f"{org or ''}: ファイル名の期間から観測日数 {days} 日を使用")
            pv = analyze.preview(org_input, month, cfg, days, org=org)
            paths = report.write_preview(pv, org_output)
            _print_preview(pv, paths)
            n_previewed += 1
            continue
        result = analyze.analyze(org_input, month, cfg, org=org)
        paths = report.write_all(result, org_output)
        results.append(result)
        _print_result(result, paths)

    if skipped:
        print(f"\n! {month} のスペンドレポートが無いためスキップした組織: {', '.join(skipped)}")
    if not results and not n_previewed:
        raise FileNotFoundError(f"{month} のデータを持つ組織がありません")

    if len(results) > 1:
        summary_path = report.write_org_summary(results, output_dir)
        _print_totals(results, summary_path)
    return 0


def _print_preview(pv, paths: dict[str, Path]) -> None:
    s = pv.summary
    scope = f"{pv.org} {pv.month}" if pv.org else pv.month
    print(f"\n=== {scope} 速報プレビュー（{pv.days_observed}日間の観測） ===")
    print(f"メンバー: {s['n_members']} 名 (Standard {s['n_standard']} / Premium {s['n_premium']}"
          f" / 未割当 {s.get('n_unassigned', 0)} / 不明 {s['n_unknown']})")
    print(f"観測需要: ${s['total_api_observed_usd']:,.2f} → 月末ペース換算 ${s['total_api_projected_usd']:,.2f}")
    counts = s["label_counts"]
    detail = " / ".join(f"{lb} {n} 名" for lb, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    print(f"一次判断: {detail}")
    if s["n_billed"]:
        print(f"実課金発生: {s['n_billed']} 名")

    if pv.warnings:
        print("\n--- 警告 ---")
        for w in pv.warnings:
            print(f"  ! {w}")
    print(f"\n--- 出力 ---\n  preview:   {paths['markdown']}\n  dashboard: {paths['html']}")


def _print_result(result: analyze.AnalysisResult, paths: dict[str, Path]) -> None:
    s = result.summary
    scope = f"{result.org} {result.month}" if result.org else result.month
    print(f"\n=== {scope} 分析結果 ===")
    print(f"メンバー: {s['n_members']} 名 (Standard {s['n_standard']} / Premium {s['n_premium']}"
          f" / 未割当 {s.get('n_unassigned', 0)} / 不明 {s['n_unknown']})")
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


def _print_totals(results: list[analyze.AnalysisResult], summary_path: Path) -> None:
    n_members = sum(r.summary["n_members"] for r in results)
    seat_cost = sum(r.summary["seat_cost_now_usd"] for r in results)
    n_change = sum(r.summary["n_change_recommended"] for r in results)
    saving = sum(r.summary["est_monthly_saving_usd"] for r in results)
    print(f"\n=== 全体 ({len(results)} 組織) ===")
    print(f"メンバー: {n_members} 名, シート費用: ${seat_cost:,.2f}/月")
    print(f"変更推奨: {n_change} 名 (削減見込み ${saving:,.2f}/月)")
    print(f"横断サマリ: {summary_path}")


if __name__ == "__main__":
    raise SystemExit(main())
