"""CLI エントリポイント: seat-analyzer {analyze,discuss,check-text,doctor,init-org}"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import analyze, data_quality, discussion, ingest, report
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
    p.add_argument(
        "--with-discussion", action="store_true",
        help="レポート生成後に考察の執筆まで行う（discuss と同じ処理。ヘッドレス Claude CLI を使用）",
    )
    p.add_argument(
        "--force-discussion", action="store_true",
        help="--with-discussion で記入済みの考察も上書きする",
    )
    p.add_argument(
        "--allow-term", action="append", metavar="語",
        help="--with-discussion の混入チェックで許可する語（discuss --allow-term と同じ）",
    )
    p.add_argument(
        "--with-previous-discussion", action="store_true",
        help="--with-discussion で前月の考察も資料に渡す（discuss と同じ。既定では渡さない）",
    )
    p.set_defaults(func=_run_analyze)

    pdis = sub.add_parser(
        "discuss", help="生成済みレポートの「## 考察」をヘッドレス Claude CLI に執筆させる")
    pdis.add_argument("--month", help="対象月 (YYYY-MM)。省略時は対象組織の spend の最新月")
    pdis.add_argument(
        "--org", action="append",
        help="対象組織（input/ 直下のディレクトリ名）。複数指定可。省略時は全組織",
    )
    pdis.add_argument("--config", default="config.yaml", help="設定ファイル (default: config.yaml)")
    pdis.add_argument("--input-dir", default="input", help="入力ディレクトリ (default: input)")
    pdis.add_argument("--output-dir", default="reports", help="出力ディレクトリ (default: reports)")
    pdis.add_argument(
        "--preview", action="store_true", help="report.md ではなく preview.md の考察を対象にする")
    pdis.add_argument(
        "--force", action="store_true",
        help="記入済みの考察を上書きする（既定では手書きの考察を守るため書き換えない）",
    )
    pdis.add_argument(
        "--dry-run", action="store_true",
        help="組み立てたプロンプトを標準出力へ出して終了する（Claude は呼ばない）",
    )
    pdis.add_argument(
        "--allow-term", action="append", metavar="語",
        help="混入チェックで検出された語のうち、内容を確認して無害と判断したものを許可する"
             "（複数指定可）。チェック全体を無効化する手段は用意しない",
    )
    pdis.add_argument(
        "--with-previous-discussion", action="store_true",
        help="前月レポートの考察も資料としてモデルに渡す。既定では渡さない"
             "（人手の文書で内容を検証できず、過去の混入を引き写す経路になるため）",
    )
    pdis.set_defaults(func=_run_discuss)

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

    pchk = sub.add_parser(
        "check-text",
        help="公開予定のテキスト（PR 本文・コメント・コミットメッセージ等）に業務情報が"
             "含まれないか検査する",
    )
    pchk.add_argument(
        "files", nargs="*", metavar="ファイル",
        help="検査するファイル。'-' または省略で標準入力を読む",
    )
    pchk.add_argument("--text", help="ファイルではなく文字列を直接検査する")
    pchk.add_argument(
        "--diff", action="store_true",
        help="入力を unified diff として扱い、追加される内容だけを検査する"
             "（削除行に現れる語で落ちないようにする）",
    )
    pchk.add_argument(
        "--allow-term", action="append", metavar="語",
        help="内容を確認して無害と判断した語を許可する（複数指定可）",
    )
    pchk.add_argument("--config", default="config.yaml", help="設定ファイル (default: config.yaml)")
    pchk.add_argument("--input-dir", default="input", help="入力ディレクトリ (default: input)")
    pchk.add_argument("--output-dir", default="reports", help="出力ディレクトリ (default: reports)")
    pchk.add_argument(
        "--repo-root", metavar="パス",
        help="「すでに公開されている内容」を読むリポジトリのルート。"
             "省略時は --config の置かれたディレクトリ",
    )
    pchk.set_defaults(func=_run_check_text)

    pi = sub.add_parser("init-org", help="新しい組織の入力/出力ディレクトリの雛形を作成")
    pi.add_argument("orgs", nargs="+", metavar="組織名",
                    help="作成する組織名（input/ 直下のディレクトリ名になる）。複数指定可")
    pi.add_argument("--input-dir", default="input", help="入力ディレクトリ (default: input)")
    pi.add_argument("--output-dir", default="reports", help="出力ディレクトリ (default: reports)")
    pi.set_defaults(func=_run_init_org)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, discussion.DiscussionError) as e:
        # 入力の読み取りに由来する失敗（欠損・権限・不正な値）と、混入チェックを
        # 保証できない状況は traceback を出さずエラー終了する
        print(f"エラー: {e}", file=sys.stderr)
        return 1


INPUT_SUBDIRS = ingest.INPUT_SUBDIRS


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
    input_dir: Path, output_dir: Path, org_args: list[str] | None,
    orgs: list[str] | None = None, legacy: bool | None = None,
) -> list[tuple[str | None, Path, Path]]:
    """分析対象の (組織名, 入力dir, 出力dir) を解決する。

    input/<org>/spend/ 型のマルチ組織レイアウトを基本とし、
    input/spend/ 直下型の旧レイアウトは単一組織（org=None）として扱う。

    orgs / legacy を渡すと組織の発見条件を差し替えられる（doctor は spend/ が
    欠けた組織も検査対象にするため、より広い条件で発見する）。
    """
    if orgs is None:
        orgs = ingest.discover_orgs(input_dir)
    if legacy is None:
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


def _discover_inspect_orgs(input_dir: Path) -> list[str]:
    """検査対象の組織候補（昇順）。

    分析用の discover_orgs は spend/ を持つディレクトリだけを組織とするが、doctor は
    spend/ が欠けていること自体を検査するため、入力ディレクトリらしさ（既知の入力
    サブディレクトリか members-info.csv を持つ）で判定する。
    """
    if not input_dir.is_dir():
        return []
    orgs = []
    for path in sorted(input_dir.iterdir()):
        if not path.is_dir():
            continue
        # 先頭ドット等の不正名も候補に含め、analyze と同じ validate_org_name で拒否する
        if any((path / sub).is_dir() for sub in INPUT_SUBDIRS) or (
            path / "members-info.csv"
        ).exists():
            orgs.append(path.name)
    return orgs


def _resolve_input_targets(
    input_dir: Path, org_args: list[str] | None
) -> list[tuple[str | None, Path]]:
    """検査対象の (組織名, 入力dir)。出力を書かないコマンド用に出力dirを落とす。"""
    if not input_dir.is_dir():
        # 組織名の検証より先に入力ディレクトリ自体の可否を判定する
        # （--org 指定時も構造化 issue として JSON へ出せるようにするため）
        # message の決定性のためパスは埋め込まない（実行環境依存値を持ち込まないため）
        raise FileNotFoundError(
            "--input-dir に指定されたディレクトリがありません"
            "（README の月次運用手順に従いデータを配置してください）"
        )
    orgs = _discover_inspect_orgs(input_dir)
    # 組織があるなら混在判定は analyze と同じく直下 spend/ のみで行う（残骸の
    # members/ 等で検査を止めない）。組織が無いときだけ、spend/ を欠いた旧レイアウトも
    # 単一組織として拾って欠損を検査する
    legacy = (
        (input_dir / "spend").is_dir() if orgs
        else any((input_dir / sub).is_dir() for sub in INPUT_SUBDIRS)
    )
    return [(org, org_input) for org, org_input, _ in
            _resolve_targets(input_dir, input_dir, org_args, orgs=orgs, legacy=legacy)]


def _latest_month(org_input: Path) -> str | None:
    """対象組織のスペンドの最新月。ファイル名を解決できない場合は None（doctor が検査する）。"""
    try:
        months = ingest.discover_months(org_input)
    except (OSError, ValueError):
        return None
    return months[-1] if months else None


def _notice(message: str, as_json: bool) -> None:
    """JSON 出力時は stdout を JSON だけに保つため、通知は stderr へ出す。"""
    print(message, file=sys.stderr if as_json else sys.stdout)


def _run_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    input_dir = Path(args.input_dir)
    as_json = args.format == "json"
    all_issues: list[QualityIssue] = []
    month = args.month
    try:
        targets = _resolve_input_targets(input_dir, args.org)
    except OSError as exc:
        # 入力ディレクトリ自体が読めない・存在しない。使い方の誤り（組織名の誤り・
        # レイアウト混在）は ValueError のまま main で扱う
        targets = []
        all_issues.extend(data_quality.input_unavailable_issues(input_dir, exc))

    # 対象月: 未指定なら対象組織全体での最新月。1件も無ければ None のまま検査に渡す
    if month is None and targets:
        latest = [m for _, org_input in targets if (m := _latest_month(org_input))]
        month = max(latest) if latest else None
        if month is not None:
            _notice(f"対象月未指定のため最新月を使用: {month}", as_json)

    for org, org_input in targets:
        issues = data_quality.inspect_input(org_input, month, cfg, org=org)
        all_issues.extend(issues)
        if not as_json:
            _print_issues(org, month, issues)
    if not targets and not as_json:
        _print_issues(None, month, all_issues)

    n_error = sum(1 for i in all_issues if i.severity is Severity.ERROR)
    if as_json:
        # 組織ごとの検査結果を連結したままでは --org の指定順で並びが変わるため、
        # 正準順序で直列化する（同一のissue多重集合なら常に同一の文字列になる）
        print(data_quality.issues_to_canonical_json(all_issues))
    else:
        n_warning = len(all_issues) - n_error
        print(f"\n検査結果: エラー {n_error} 件 / 警告 {n_warning} 件")
    return 1 if n_error else 0


def _print_issues(org: str | None, month: str | None, issues: list[QualityIssue]) -> None:
    scope = " ".join(x for x in (org, month) if x)
    print(f"\n=== {scope + ' ' if scope else ''}入力検査 ===")
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
    # 使い方の誤りは分析を走らせる前に落とす（3組織の分析を完走してから失敗させない）
    if args.with_discussion:
        _check_allow_scope(tuple(args.allow_term or ()), len(targets))

    # 対象月: 未指定なら対象組織全体での最新月。その月のデータが無い組織はスキップ
    month = _resolve_month(targets, args.month)

    results: list[analyze.AnalysisResult] = []
    skipped: list[str] = []
    written: list[tuple[str | None, Path]] = []
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
            written.append((org, org_output))
            n_previewed += 1
            continue
        result = analyze.analyze(org_input, month, cfg, org=org)
        paths = report.write_all(result, org_output)
        results.append(result)
        written.append((org, org_output))
        _print_result(result, paths)

    if skipped:
        print(f"\n! {month} のスペンドレポートが無いためスキップした組織: {', '.join(skipped)}")
    if not results and not n_previewed:
        raise FileNotFoundError(f"{month} のデータを持つ組織がありません")

    if len(results) > 1:
        summary_path = report.write_org_summary(results, output_dir)
        _print_totals(results, summary_path)

    if args.with_discussion:
        return _run_discussions(
            written, month=month, input_dir=input_dir, output_dir=output_dir, cfg=cfg,
            preview=args.preview, force=args.force_discussion, dry_run=False,
            allow=tuple(args.allow_term or ()),
            include_previous=args.with_previous_discussion,
        )
    return 0


# ASCII 数字のみ・全体一致で検証する（`\d` は全角数字にも一致し、`$` は末尾改行を許す）
_MONTH_RE = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")


def _resolve_month(targets: list[tuple[str | None, Path, Path]], month: str | None) -> str:
    """対象月。未指定なら対象組織全体での spend の最新月。

    対象月は出力パスの一部（reports/<組織名>/<月>/）になるため形式を厳密に検証する。
    検証しないと `--month ../<他組織>/<月>` で別組織のレポートを読み書きできてしまう。
    """
    if month is not None:
        if not _MONTH_RE.fullmatch(month):
            raise ValueError(f"対象月の形式が不正です: {month!r}（YYYY-MM 形式で指定してください）")
        return month
    latest = [m[-1] for _, d, _ in targets if (m := ingest.discover_months(d))]
    if not latest:
        raise FileNotFoundError(
            "スペンドレポートがありません。README の月次運用手順に従いエクスポートしてください。"
        )
    month = max(latest)
    print(f"対象月未指定のため最新月を使用: {month}")
    return month


def _run_check_text(args: argparse.Namespace) -> int:
    """公開予定のテキストを検査する。業務情報を検出したら終了コード 1。"""
    cfg = load_config(args.config)
    # baseline（すでに公開されている内容）はリポジトリのルートから読む。
    # config.yaml はリポジトリ直下に置く運用なので、省略時はその親をルートとみなす
    root = Path(args.repo_root) if args.repo_root else Path(args.config).resolve().parent

    sources: list[tuple[str, str]] = []
    if args.text is not None:
        sources.append(("--text", args.text))
    names = list(args.files) or ([] if args.text is not None else ["-"])
    # 検査対象のファイル自身を baseline から除く（自分自身を根拠に素通りさせない）
    exclude = tuple(Path(n) for n in names if n != "-")
    for name in names:
        if name == "-":
            sources.append(("(標準入力)", sys.stdin.read()))
        else:
            sources.append((name, Path(name).read_text(encoding="utf-8")))

    n_hits = 0
    allowable_seen = False
    for label, text in sources:
        if args.diff:
            text = discussion.diff_added_text(text)
        result = discussion.check_public_text(
            text, input_dir=Path(args.input_dir), output_dir=Path(args.output_dir),
            cfg=cfg, root=root, allow=tuple(args.allow_term or ()), exclude=exclude,
        )
        if not result.hits:
            print(f"  {label}: 業務情報は検出されませんでした（{result.n_terms} 語と照合）")
            continue
        n_hits += len(result.hits)
        print(f"\n! {label}: 業務情報が含まれています"
              f"（{len(result.hits)} 件 / {result.n_terms} 語と照合）", file=sys.stderr)
        for hit in result.hits:
            note = "" if hit.allowable else "・--allow-term では許可できません"
            allowable_seen = allowable_seen or hit.allowable
            print(f"    {hit.term}（{hit.kind}{note}） … {hit.context} …", file=sys.stderr)

    if n_hits:
        print(
            "\n公開する前に該当箇所を、出典が特定できない一般論へ書き換えてください"
            "（例: 組織名を『ある組織』、部署名を『短い英字略称の部署』とする）。",
            file=sys.stderr,
        )
        if allowable_seen:
            print("誤検出と判断した語は --allow-term <語> で許可できます。", file=sys.stderr)
        return 1
    return 0


def _check_allow_scope(allow: tuple[str, ...], n_targets: int) -> None:
    """--allow-term は「その組織の生成物を人が確認した」ことに基づくため単一組織限定。

    恒久的に許可したい語は config.yaml > discussion.allow_terms を使う。
    """
    if allow and n_targets > 1:
        raise ValueError(
            "--allow-term は単一組織に対してのみ指定できます（--org で対象を絞るか、"
            "恒久的に許可する語は config.yaml > discussion.allow_terms に書いてください）"
        )


def _run_discuss(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    targets = _resolve_targets(input_dir, output_dir, args.org)
    month = _resolve_month(targets, args.month)
    return _run_discussions(
        [(org, org_output) for org, _, org_output in targets],
        month=month, input_dir=input_dir, output_dir=output_dir, cfg=cfg,
        preview=args.preview, force=args.force, dry_run=args.dry_run,
        allow=tuple(args.allow_term or ()),
        include_previous=args.with_previous_discussion,
    )


def _run_discussions(
    items: list[tuple[str | None, Path]], *, month: str, input_dir: Path, output_dir: Path,
    cfg: dict, preview: bool, force: bool, dry_run: bool, allow: tuple[str, ...] = (),
    include_previous: bool = False,
) -> int:
    """組織ごとに考察を生成する。1組織の失敗で他組織を止めない。"""
    _check_allow_scope(allow, len(items))

    # 対象月のレポートが無い組織はスキップする（analyze と同じ扱い）。組織ごとに
    # spend の月がずれるため、月未指定の実行で他組織が毎回ハードエラーになるのを避ける。
    # 単一組織のときは generate のエラーで理由を示す
    skipped: list[str] = []
    if len(items) > 1:
        targets = []
        for org, org_output in items:
            if discussion.document_path(org_output, month, preview).exists():
                targets.append((org, org_output))
            else:
                skipped.append(org or str(org_output))
        items = targets

    if not dry_run:
        print(f"\n=== 考察の執筆（{len(items)} 組織） ===")
    failed: list[str] = []
    blocked: list[str] = []
    for org, org_output in items:
        scope = f"{org} {month}" if org else month
        try:
            outcome = discussion.generate(
                org=org, month=month, input_dir=input_dir, output_dir=output_dir,
                org_output=org_output, cfg=cfg, preview=preview, force=force, dry_run=dry_run,
                allow=allow, include_previous=include_previous,
                notify=lambda m, scope=scope: print(f"  {scope}: {m}", file=sys.stderr),
            )
        except (discussion.DiscussionError, OSError, ValueError) as exc:
            print(f"  ! {scope}: 考察を生成できませんでした: {exc}", file=sys.stderr)
            failed.append(scope)
            continue
        if outcome.status == "blocked":
            blocked.append(scope)
        _print_discussion(outcome, scope)

    if skipped:
        # --dry-run は stdout をプロンプトだけに保つ契約なので通知は stderr へ回す
        print(f"\n! {month} のレポートが無いためスキップした組織: {', '.join(skipped)}"
              f"（先に analyze を実行してください）",
              file=sys.stderr if dry_run else sys.stdout)
    if blocked:
        print(
            f"\n! 他組織情報の混入が解消しなかったため書き込みを中止した組織: {', '.join(blocked)}",
            file=sys.stderr,
        )
    if failed:
        print(f"! 考察の生成に失敗した組織: {', '.join(failed)}", file=sys.stderr)
    return 1 if (blocked or failed) else 0


def _print_discussion(outcome: discussion.DiscussionOutcome, scope: str) -> None:
    if outcome.status == "dry-run":
        print(f"===== プロンプト: {scope} =====", file=sys.stderr)
        print(outcome.prompt)
        return
    if outcome.status == "kept":
        print(f"  {scope}: 記入済みの考察があるため変更しません（上書きは --force）")
    elif outcome.status == "written":
        print(f"  {scope}: 考察を書き込みました "
              f"（{outcome.chars} 文字 / 試行 {outcome.attempts} 回）→ {outcome.path}")
    elif outcome.status == "blocked":
        print(f"  {scope}: 混入チェックで他組織の語を検出したため書き込みを中止しました "
              f"（試行 {outcome.attempts} 回）", file=sys.stderr)
        for hit in outcome.leaks:
            # 一致箇所の文脈を出す。誤検出（他組織の短い部署名が無関係な複合語に
            # 一致した等）かどうかを人が判断できるようにするため
            note = "" if hit.allowable else "・--allow-term では許可できません"
            print(f"    検出語: {hit.term}（{hit.kind}{note}） … {hit.context} …",
                  file=sys.stderr)
        if any(h.allowable for h in outcome.leaks):
            print("    誤検出と判断した語は --allow-term <語> で許可できます", file=sys.stderr)


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
