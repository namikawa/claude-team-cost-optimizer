"""考察セクションの自動執筆。

`.claude/commands/seat-analysis.md` の手順3〜5（結果の検証・考察の執筆・混入チェック）を
CLI から完結させるためのモジュール。ローカルの Claude Code CLI をヘッドレス（`-p`）で呼び、
考察本文のテキストだけを受け取って report.md / preview.md へ差し込む。

設計上の約束:

- LLM にツールを与えない。資料はすべてプロンプトへ埋め込み、出力は本文テキストのみ。
  ファイルへの書き込みはこのモジュールが行うため、生成側がレポート以外を触ることがない
- `--safe-mode` と空の作業ディレクトリで呼ぶ。プロジェクトの CLAUDE.md や auto-memory には
  他組織の業務情報が含まれうるため、モデルのコンテキストへ入れない（混入経路を塞ぐ）
- 他組織情報の混入チェックは LLM の自己申告に頼らず、このモジュールの決定論的な照合で行う。
  検出したら書き直しを1度求め、それでも残るなら report.md を書き換えない
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import ingest, report

PROMPTS_DIR = Path(__file__).parent / "prompts"

DEFAULTS: dict = {
    "command": "claude",
    "model": "opus",
    "effort": "xhigh",
    "timeout_seconds": 1800,
    "max_attempts": 2,
    "min_output_chars": 200,
    "retries": 2,
    "retry_wait_seconds": 30,
}

# ヘッドレス実行で禁止するツール。資料はプロンプトに埋め込むためツールは一切不要で、
# ファイル・Web の読み取りは他組織情報の混入経路になるため明示的に落とす。
# --safe-mode との二重防御であり、どちらか一方が効かなくなっても片方が残る。
DISALLOWED_TOOLS = (
    "Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Glob", "Grep", "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite",
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_MONTH_DIR_RE = re.compile(r"^\d{4}-\d{2}$")
# 語境界の判定に使う「語の一部とみなす文字」。日本語は含めないため、「架空推進3部の」の
# ように助詞が続く出現も検出できる。`.` と `@` は含めない: 含めると
# `bernard.holloway` の中の姓名や `holloway@example.com` の中のローカル部が
# 「その語ではない」と判定され、検出漏れと（照合元側での）誤検出の両方を招く。
_WORDISH = r"[0-9A-Za-z_-]"
# 混入語として扱うトークンの最小長。短すぎる語（人名の頭文字・2文字の略称）は
# 一般的な文章に偶然含まれて誤検出になるため落とす。
_MIN_TOKEN_LEN = 5
_MIN_GROUP_LEN = 3


class DiscussionError(RuntimeError):
    """考察の生成・検証に失敗した（レポートは書き換えていない）。

    transient=True は再実行で解消しうる失敗（API の一時エラー・タイムアウト等）。
    設定の誤りのような恒久的な失敗と区別してリトライの可否を決める。
    """

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@dataclass
class DiscussionOutcome:
    """1組織1月ぶんの考察生成の結果。"""

    org: str | None
    month: str
    path: Path
    status: str  # written / blocked / kept / dry-run
    attempts: int = 0
    leaks: tuple[str, ...] = ()
    prompt: str = ""
    chars: int = 0


def settings(cfg: dict) -> dict:
    """config.yaml > discussion を既定値で補完した設定。"""
    merged = dict(DEFAULTS)
    merged.update(cfg.get("discussion") or {})
    return merged


# ---------------------------------------------------------------- プロンプト組み立て


def _render(template: str, values: dict[str, str]) -> str:
    """`{{KEY}}` の素朴な置換。資料側の波括弧を壊さないため format は使わない。"""
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def _material_block(title: str, content: str) -> str:
    # コードフェンスを使わない（資料本文が Markdown を含むため）
    return f"===== {title} ここから =====\n{content.strip()}\n===== {title} ここまで =====\n"


def _correction_block(leaks: tuple[str, ...]) -> str:
    terms = "\n".join(f"- {t}" for t in leaks)
    return f"""
# 前回の出力の差し戻し（最優先で従うこと）

前回の出力に、対象組織の資料に現れない固有名詞が含まれていました。これは他組織の情報の
混入であり、このままでは共有できません。以下の語を出力から完全に取り除き、必要なら出典を
特定できない一般論に言い換えたうえで、考察の全文を書き直してください。

検出された語:
{terms}
"""


def build_prompt(
    *, org: str | None, scope: str, materials: list[tuple[str, str]],
    preview: bool, leaks: tuple[str, ...] = (),
) -> str:
    shell = (PROMPTS_DIR / "discussion.md").read_text(encoding="utf-8")
    aspects_name = "aspects-preview.md" if preview else "aspects-full.md"
    aspects = (PROMPTS_DIR / aspects_name).read_text(encoding="utf-8")
    return _render(shell, {
        "ORG": org or "（単一組織）",
        "SCOPE": scope,
        "ASPECTS": aspects.strip(),
        "CORRECTION": _correction_block(leaks) if leaks else "",
        "MATERIALS": "\n".join(_material_block(t, c) for t, c in materials),
    })


def _prev_month_dir(org_output: Path, month: str) -> Path | None:
    """対象月より前で最も新しい出力月のディレクトリ。"""
    if not org_output.is_dir():
        return None
    months = sorted(
        p.name for p in org_output.iterdir()
        if p.is_dir() and _MONTH_DIR_RE.match(p.name) and p.name < month
    )
    return org_output / months[-1] if months else None


def collect_materials(
    *, org_output: Path, month: str, preview: bool,
) -> tuple[list[tuple[str, str]], str]:
    """(プロンプトへ渡す資料, 混入チェックの照合元テキスト) を返す。

    照合元には機械生成された当月の資料だけを入れる。前月の考察は人・LLM の散文で、
    そこに混入があった場合に今月の混入を見逃す照合元になってしまうため含めない。
    """
    doc_name = "preview.md" if preview else "report.md"
    doc_path = org_output / month / doc_name
    if not doc_path.exists():
        raise DiscussionError(
            f"{doc_path} がありません。先に "
            f"`seat-analyzer analyze{' --preview' if preview else ''}` を実行してください"
        )
    body = report.document_body(doc_path.read_text(encoding="utf-8"))
    materials = [(f"資料1: 分析レポート本文（{month}）", body)]
    source = [body]

    if not preview:
        csv_path = org_output / month / "recommendations.csv"
        if csv_path.exists():
            csv_text = csv_path.read_text(encoding="utf-8")
            materials.append((f"資料2: ユーザ別推奨一覧 recommendations.csv（{month}）", csv_text))
            source.append(csv_text)

    prev_dir = _prev_month_dir(org_output, month)
    if prev_dir is not None:
        prev_report = prev_dir / "report.md"
        if prev_report.exists():
            prev = report.discussion_body(prev_report.read_text(encoding="utf-8"))
            if prev:
                materials.append((
                    f"資料{len(materials) + 1}: 前回の正式レポートの考察（{prev_dir.name}）", prev,
                ))
    return materials, "\n".join(source)


# ---------------------------------------------------------------- 混入チェック


def _org_dir_names(base: Path) -> set[str]:
    """入力・出力ディレクトリ直下の組織ディレクトリ名。

    出力側には横断サマリ（summary）と旧レイアウトの月ディレクトリが混ざるため落とす。
    """
    if not base.is_dir():
        return set()
    return {
        p.name for p in base.iterdir()
        if p.is_dir() and p.name != "summary" and not _MONTH_DIR_RE.match(p.name)
        and not p.name.startswith(".")
    }


def _emails_in_text(text: str) -> set[str]:
    return set(_EMAIL_RE.findall(text))


def _tokens_from_email(email: str) -> set[str]:
    """メールアドレスから混入検出に使う語を取り出す。"""
    local, _, domain = email.partition("@")
    tokens = {email, local}
    # ローカル部のドット区切り（姓・名）。短い断片（頭文字等）は誤検出源なので落とす
    tokens |= {seg for seg in local.split(".") if len(seg) >= _MIN_TOKEN_LEN}
    if domain:
        tokens.add(domain)
        first = domain.split(".")[0]
        if len(first) >= _MIN_TOKEN_LEN:
            tokens.add(first)
    return tokens


def _group_names(org_input: Path, cfg: dict) -> set[str]:
    """members-info.csv の部署・チーム名。

    職種（role）は「エンジニア」等の一般語が多く、一般論の記述を誤検出するため含めない。
    """
    names: set[str] = set()
    for path in sorted(org_input.glob("members-info*.csv")):
        try:
            df = ingest.load_members_info_file(path, cfg)
        except (OSError, ValueError):
            continue
        for col in ("department", "team"):
            if col not in df.columns:
                continue
            for cell in df[col].dropna().astype(str):
                for name in ingest.parse_affiliations(cell):
                    if len(name.strip()) >= _MIN_GROUP_LEN:
                        names.add(name.strip())
    return names


def forbidden_terms(
    *, input_dir: Path, output_dir: Path, target_org: str | None, cfg: dict,
) -> tuple[str, ...]:
    """他組織に由来する語（組織名・メール・人名トークン・部署/チーム名）。"""
    others = (_org_dir_names(input_dir) | _org_dir_names(output_dir)) - {target_org}
    terms: set[str] = set(others)
    for org in sorted(others):
        org_input = input_dir / org
        if not org_input.is_dir():
            continue
        for path in sorted(org_input.rglob("*.csv")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for email in _emails_in_text(text):
                terms |= _tokens_from_email(email)
        terms |= _group_names(org_input, cfg)
    return tuple(sorted(t for t in terms if len(t) >= _MIN_GROUP_LEN))


def _contains_term(haystack_lower: str, term_lower: str) -> bool:
    pattern = rf"(?<!{_WORDISH}){re.escape(term_lower)}(?!{_WORDISH})"
    return re.search(pattern, haystack_lower) is not None


def find_leaks(text: str, terms: tuple[str, ...], *, source: str) -> tuple[str, ...]:
    """text に現れ、対象組織の資料 source には現れない語（= 他組織情報の混入）。"""
    lowered, source_lower = text.lower(), source.lower()
    found = {
        term for term in terms
        if _contains_term(lowered, term.lower())
        and not _contains_term(source_lower, term.lower())
    }
    return tuple(sorted(found))


# ---------------------------------------------------------------- ヘッドレス実行


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```$", re.DOTALL)
_API_ERROR_RE = re.compile(r"^\s*API Error", re.IGNORECASE)


def _strip_fence(text: str) -> str:
    """全体がコードフェンスで囲まれていたら剥がす（指示に反した出力の救済）。"""
    m = _FENCE_RE.match(text.strip())
    return m.group(1).strip() if m else text.strip()


def run_claude(prompt: str, s: dict) -> str:
    """ヘッドレス Claude CLI を呼び、考察本文のテキストを返す。"""
    command = str(s["command"])
    if shutil.which(command) is None and not Path(command).is_file():
        raise DiscussionError(
            f"'{command}' が見つかりません。Claude Code CLI を PATH に置くか、"
            "config.yaml > discussion.command にフルパスを指定してください"
        )
    cmd = [
        command, "-p",
        # プロジェクトの CLAUDE.md・auto-memory・hooks・MCP を読み込ませない
        "--safe-mode",
        "--output-format", "text",
        "--model", str(s["model"]),
        "--effort", str(s["effort"]),
        "--disallowedTools", *DISALLOWED_TOOLS,
    ]
    # 空の作業ディレクトリで実行し、リポジトリのファイルを起点にさせない
    with tempfile.TemporaryDirectory(prefix="seat-analyzer-discuss-") as workdir:
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True,
                timeout=float(s["timeout_seconds"]), cwd=workdir, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DiscussionError(
                f"{command} が {s['timeout_seconds']} 秒以内に応答しませんでした", transient=True
            ) from exc
        except OSError as exc:
            raise DiscussionError(f"{command} を実行できませんでした: {exc}") from exc

    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        detail = ((proc.stderr or "").strip() or out).splitlines()
        raise DiscussionError(
            f"{command} が異常終了しました (exit {proc.returncode}): "
            f"{detail[0][:300] if detail else '出力なし'}", transient=True,
        )
    # API エラー・空出力・異常に短い出力をそのまま考察として書き込まないための門。
    # いずれも再実行で解消しうるため transient として扱う
    if not out:
        raise DiscussionError(f"{command} の出力が空でした", transient=True)
    if _API_ERROR_RE.match(out):
        raise DiscussionError(
            f"{command} が API エラーを返しました: {out.splitlines()[0][:300]}", transient=True)
    body = _strip_fence(out)
    if len(body) < int(s["min_output_chars"]):
        raise DiscussionError(
            f"生成された考察が短すぎます（{len(body)} 文字 < "
            f"{s['min_output_chars']} 文字）: {body[:200]}", transient=True,
        )
    return body


def _call_with_retry(runner, prompt: str, s: dict, notify) -> str:
    """一時的な失敗（API エラー・タイムアウト等）に限ってリトライする。"""
    retries = max(0, int(s["retries"]))
    wait = max(0.0, float(s["retry_wait_seconds"]))
    for i in range(retries + 1):
        try:
            return runner(prompt, s)
        except DiscussionError as exc:
            if not exc.transient or i == retries:
                raise
            notify(f"一時的な失敗のため再試行します（{i + 1}/{retries}）: {exc}")
            if wait:
                time.sleep(wait)
    raise AssertionError("到達しない")  # pragma: no cover


# ---------------------------------------------------------------- オーケストレーション


def generate(
    *, org: str | None, month: str, input_dir: Path, output_dir: Path, org_output: Path,
    cfg: dict, preview: bool = False, force: bool = False, dry_run: bool = False,
    runner=None, notify=None,
) -> DiscussionOutcome:
    """1組織1月ぶんの考察を生成して書き込む。

    既に記入済みの考察は force が無ければ上書きしない（手書きの考察を守るため）。
    混入が検出された場合は書き直しを求め、max_attempts 回で解消しなければ書き込まない。
    """
    # 既定の runner は呼び出し時に解決する（デフォルト引数で束縛すると差し替えが効かない）
    runner = runner or run_claude
    notify = notify or (lambda _message: None)
    s = settings(cfg)
    doc_path = org_output / month / ("preview.md" if preview else "report.md")
    scope = f"{org} {month}" if org else month

    materials, source = collect_materials(
        org_output=org_output, month=month, preview=preview)

    if not force:
        existing = report.discussion_body(doc_path.read_text(encoding="utf-8"))
        if existing:
            return DiscussionOutcome(org, month, doc_path, "kept", chars=len(existing))

    prompt = build_prompt(org=org, scope=scope, materials=materials, preview=preview)
    if dry_run:
        return DiscussionOutcome(org, month, doc_path, "dry-run", prompt=prompt)

    terms = forbidden_terms(
        input_dir=input_dir, output_dir=output_dir, target_org=org, cfg=cfg)

    max_attempts = max(1, int(s["max_attempts"]))
    leaks: tuple[str, ...] = ()
    for attempt in range(1, max_attempts + 1):
        body = _call_with_retry(
            runner,
            build_prompt(org=org, scope=scope, materials=materials,
                         preview=preview, leaks=leaks) if leaks else prompt,
            s, notify,
        )
        leaks = find_leaks(body, terms, source=source)
        if not leaks:
            report.write_discussion(doc_path, body)
            return DiscussionOutcome(
                org, month, doc_path, "written", attempts=attempt, chars=len(body))
    return DiscussionOutcome(
        org, month, doc_path, "blocked", attempts=max_attempts, leaks=leaks)
