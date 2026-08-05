"""考察セクションの自動執筆。

`.claude/commands/seat-analysis.md` の手順3〜5（結果の検証・考察の執筆・混入チェック）を
CLI から完結させるためのモジュール。ローカルの Claude Code CLI をヘッドレス（`-p`）で呼び、
考察本文のテキストだけを受け取って report.md / preview.md へ差し込む。

この経路には人のレビューが入らない（cron 実行もできる）ため、対話セッションで人が担って
いた「他組織の情報が混ざっていないか」の確認を機械で代替する必要がある。以下はそのための
設計上の約束:

- LLM にツールを与えない。資料はすべてプロンプトへ埋め込み、出力は本文テキストのみ。
  ファイルへの書き込みはこのモジュールが行うため、生成側がレポート以外を触ることがない
- `--safe-mode` と空の作業ディレクトリで呼ぶ。プロジェクトの CLAUDE.md や auto-memory には
  他組織の業務情報が含まれうるため、モデルのコンテキストへ入れない（混入経路を塞ぐ）
- 他組織情報の混入チェックは LLM の自己申告に頼らず、このモジュールの決定論的な照合で行う。
  検出したら書き直しを1度求め、それでも残るなら report.md を書き換えない
- 照合は「取りこぼし（他組織情報を通す）」より「誤検出（正当な記述を止める）」に倒す。
  取りこぼしは他組織の情報が担当者に共有される一方、誤検出は書き込まずに止まるだけで
  人が確認すれば済む。誤検出時に人が判断できるよう一致箇所の文脈を返し、確認済みの語は
  allow で明示的に許可できるようにしている
"""

from __future__ import annotations

import math
import os
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
    "allow_terms": (),
}

# ヘッドレス実行で禁止するツール。資料はプロンプトに埋め込むためツールは一切不要。
#
# 主たる保証は `--tools ""`（組み込みツールを空集合にする許可リスト方式）で、以下は
# それが効かなくなった場合に残る追加防御にすぎない。**denylist は網羅していない**:
# 実測（`--tools default` で init イベントのツール一覧を確認）では、この列挙を通した
# 後も 20件以上のツールが残る。ツール名は環境とバージョンに依存するため網羅は諦め、
# 混入・外部送信に直接つながる種類だけを挙げている。
# - ファイル・Web の読み取り: 他組織情報をモデルへ持ち込む経路
# - 外向きの送信・共有: 対象組織のデータを外へ出す経路
# - ツールの発見・追い足し: 上記2種を後から呼べるようにする経路
# 存在しない名前を渡しても CLI は警告のみで正常終了する（実測）ため、環境差で名前が
# 消えても壊れない。
DISALLOWED_TOOLS = (
    # ファイル・Web の読み取り
    "Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Glob", "Grep", "WebFetch", "WebSearch",
    # 外向きの送信・共有
    "SendMessage", "PushNotification", "RemoteTrigger", "ShareOnboardingGuide",
    # ツールの発見・追い足し、他エージェントの起動
    "ToolSearch", "Skill", "Task", "Agent", "Workflow", "TodoWrite",
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_MONTH_DIR_RE = re.compile(r"^\d{4}-\d{2}$")

# 語境界の判定に使う「語の一部とみなす文字」。日本語を含めないため、「架空推進3部の」の
# ように助詞が続く出現も検出できる。`.` と `@` は含めない: 含めると
# `bernard.holloway` の中の姓名や `holloway@example.com` の中のローカル部が
# 「その語ではない」と判定され、検出漏れと（照合元側での）誤検出の両方を招く。
_WORDISH = r"[0-9A-Za-z_-]"

# 短い日本語の語に使う語境界。漢字・カタカナ（と々〆）を「語の一部」とみなし、ひらがな・
# 記号・空白は境界とみなす。日本語の固有名詞は文中でほぼ必ず助詞（ひらがな）か記号に続く
# ため、これで「開発部の削減余地」は検出しつつ「製品開発部門」への部分一致は避けられる。
# 長い語にこの規則を使うと「架空推進3部第2チーム」のような固有名の埋め込みを取りこぼす
# ので、短い語（一般語である可能性が高い側）だけに限定する。
_JP_WORDISH = "々〆㐀-䶿一-鿿豈-﫿゠-ヿｦ-ﾟ"
_WORDISH_JP = rf"[0-9A-Za-z_\-{_JP_WORDISH}]"
_HAS_JP_RE = re.compile(f"[{_JP_WORDISH}぀-ゟ]")
_JP_STRICT_MAX_LEN = 3

# 禁止語として扱うトークンの最小長。短すぎる語（人名の頭文字・1文字の略称）は一般的な
# 文章に偶然含まれて誤検出になるため落とす。人名トークンは姓が3文字のローマ字になる例
# （sato / kato / mori 等）が普通にあるため 3 まで許す。
_MIN_PERSON_TOKEN_LEN = 3
_MIN_TERM_LEN = 2

# 一致箇所の前後をどれだけ文脈として返すか
_CONTEXT_CHARS = 24


class DiscussionError(RuntimeError):
    """考察の生成・検証に失敗した（レポートは書き換えていない）。

    transient=True は再実行で解消しうる失敗（通信の一時障害・429・5xx・タイムアウト）。
    認証や設定の誤りのような恒久的な失敗と区別してリトライの可否を決める。
    """

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class Term:
    """禁止語1件。kind によって照合規則が変わる。

    kind == "org" の組織名は、対象組織の資料に現れても混入として扱う（常時禁止）。
    組織名が他組織のレポートに現れるのはそれ自体が問題であり、資料側に混入があった場合に
    それを許可根拠にしてはいけないため。それ以外（メール・人名・部署名）は、ドメインの
    共有や同姓による誤検出を避けるため、対象組織の資料に現れる語を除外する。
    """

    text: str
    kind: str  # org / address / domain / person / group

    @property
    def always_forbidden(self) -> bool:
        return self.kind == "org"

    @property
    def allowable(self) -> bool:
        """--allow-term で許可してよい種類か。

        組織名とメールアドレスは、一般語と衝突して誤検出になる余地が実質なく、
        通ってしまった場合の影響が大きい。人の判断で通す対象から外す。
        """
        return self.kind not in ("org", "address")


@dataclass(frozen=True)
class LeakHit:
    """混入の検出1件。"""

    term: str
    kind: str
    context: str  # 一致箇所の前後（誤検出かどうかを人が判断するため）
    allowable: bool = True  # --allow-term で許可できる種類か


@dataclass
class DiscussionOutcome:
    """1組織1月ぶんの考察生成の結果。"""

    org: str | None
    month: str
    path: Path
    status: str  # written / blocked / kept / dry-run
    attempts: int = 0
    leaks: tuple[LeakHit, ...] = ()
    prompt: str = ""
    chars: int = 0


def settings(cfg: dict) -> dict:
    """config.yaml > discussion を既定値で補完した設定。"""
    merged = dict(DEFAULTS)
    merged.update(cfg.get("discussion") or {})
    return merged


# ---------------------------------------------------------------- プロンプト組み立て


_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


def _render(template: str, values: dict[str, str]) -> str:
    """`{{KEY}}` を一度だけ置換する。

    逐次 replace すると、先に挿入した値の中の `{{KEY}}` が後続キーとして再置換され、
    資料や組織名の内容がプロンプト構造を壊しうる（組織名には波括弧が使える）。
    """
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def _material_block(title: str, content: str) -> str:
    # コードフェンスを使わない（資料本文が Markdown を含むため）
    return f"===== {title} ここから =====\n{content.strip()}\n===== {title} ここまで =====\n"


def _correction_block(leaks: tuple[LeakHit, ...]) -> str:
    terms = "\n".join(f"- {h.term}（該当箇所: …{h.context}…）" for h in leaks)
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
    preview: bool, leaks: tuple[LeakHit, ...] = (),
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


def document_path(org_output: Path, month: str, preview: bool) -> Path:
    """考察を書き込む対象ファイル。"""
    return org_output / month / ("preview.md" if preview else "report.md")


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
    terms: tuple[Term, ...] = (), include_previous: bool = False, notify=None,
) -> tuple[list[tuple[str, str]], str]:
    """(プロンプトへ渡す資料, 混入チェックの照合元テキスト) を返す。

    照合元には機械生成された当月の資料だけを入れる。前月の考察は人・LLM の散文で、
    そこに混入があった場合に今月の混入を見逃す照合元になってしまうため含めない。

    前月の考察は既定では資料に含めない（include_previous=False）。これは検証できない
    人手の文書であり、含めるとモデルへ渡す資料が「機械生成された対象組織のデータのみ」
    という前提が崩れる。過去のレポートに混入があった場合、それを引き写す経路になる
    （混入チェックには 2文字の姓や金額のような死角があり、そこは塞げない）。
    include_previous=True のときも渡す前に混入チェックを通し、落ちたら除外する。
    """
    notify = notify or (lambda _message: None)
    doc_path = document_path(org_output, month, preview)
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

    source_text = "\n".join(source)
    prev_dir = _prev_month_dir(org_output, month) if include_previous else None
    if prev_dir is not None:
        prev_report = prev_dir / "report.md"
        if prev_report.exists():
            prev = report.discussion_body(prev_report.read_text(encoding="utf-8"))
            if prev:
                # allow は「今回の生成物を人が確認した」ことに基づく許可なので、
                # 過去の考察の検査には適用しない
                hits = find_leaks(prev, terms, source=source_text)
                if hits:
                    notify(
                        f"前月（{prev_dir.name}）の考察に他組織の語が含まれるため資料から除外します: "
                        f"{', '.join(h.term for h in hits)}"
                    )
                else:
                    materials.append((
                        f"資料{len(materials) + 1}: 前回の正式レポートの考察（{prev_dir.name}）",
                        prev,
                    ))
    return materials, source_text


# ---------------------------------------------------------------- 混入チェック


def _scandir(path: Path) -> list[os.DirEntry]:
    """ディレクトリの列挙。読めない場合は DiscussionError にする。

    pathlib の is_dir()/exists()/glob() は権限エラーを False や空リストとして飲み込む。
    それでは「他組織のディレクトリが読めなかった」ことが検出漏れと区別できないため、
    禁止語の収集に使う列挙はすべてここを通し、失敗を明示的な中止に変える。
    """
    try:
        with os.scandir(path) as it:
            return list(it)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise DiscussionError(
            f"{path} を列挙できないため混入チェックを保証できません: {exc}"
        ) from exc


def _is_org_input_dir(path: Path) -> bool:
    """入力側の組織ディレクトリか。既知の入力サブディレクトリを持つかで構造的に判定する。

    名前で除外する（spend 等を落とす）方式にすると、`input/members/spend/` のように
    入力サブディレクトリと同名の組織が実在した場合にその組織の禁止語が丸ごと抜ける。
    組織名の検証（ingest.validate_org_name）はこれらの名前を許すため、構造で判定する。
    旧レイアウトの `input/spend/` は CSV しか持たないのでここで除外される。
    """
    for entry in _scandir(path):
        if entry.is_dir() and entry.name in ingest.INPUT_SUBDIRS:
            return True
        if entry.is_file() and entry.name == "members-info.csv":
            return True
    return False


def _is_org_output_dir(path: Path) -> bool:
    """出力側の組織ディレクトリか。月ディレクトリを子に持つかで構造的に判定する。

    旧レイアウトの `reports/YYYY-MM/` と横断サマリの `reports/summary/` は月ディレクトリを
    持たないため除外される。組織名が月の形式でも `reports/<月>/<月>/` になるので拾える。
    """
    return any(
        e.is_dir() and _MONTH_DIR_RE.match(e.name) for e in _scandir(path)
    )


def _org_dir_names(base: Path, is_org) -> set[str]:
    return {
        e.name for e in _scandir(base)
        if e.is_dir() and not e.name.startswith(".") and is_org(Path(e.path))
    }


def _files_under(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    """root 以下の該当ファイルを再帰的に集める。列挙できないディレクトリがあれば中止する。

    Path.rglob は走査中の OSError を抑制するため使わない。
    """
    found: list[Path] = []
    stack = [root]
    while stack:
        for entry in _scandir(stack.pop()):
            if entry.is_dir():
                stack.append(Path(entry.path))
            elif entry.name.lower().endswith(suffixes):
                found.append(Path(entry.path))
    return sorted(found)


def _csv_paths(root: Path) -> list[Path]:
    return _files_under(root, (".csv",))


def _emails_in_text(text: str) -> set[str]:
    return set(_EMAIL_RE.findall(text))


def _terms_from_email(email: str) -> set[Term]:
    """メールアドレスから混入検出に使う語を取り出す。"""
    local, _, domain = email.partition("@")
    terms = {Term(email, "address"), Term(local, "person")}
    # ローカル部の区切り（姓・名）。`.` だけでなく `_` `+` `-` `%` でも区切る運用がある
    terms |= {
        Term(seg, "person") for seg in re.split(r"[._%+-]", local)
        if len(seg) >= _MIN_PERSON_TOKEN_LEN
    }
    if domain:
        terms.add(Term(domain, "domain"))
        first = domain.split(".")[0]
        if len(first) >= _MIN_PERSON_TOKEN_LEN:
            terms.add(Term(first, "domain"))
    return terms


def _group_terms(org_input: Path, cfg: dict) -> set[Term]:
    """members-info.csv の部署・チーム名。

    職種（role）は「エンジニア」等の一般語が多く、一般論の記述を誤検出するため含めない。
    """
    terms: set[Term] = set()
    infos = [p for p in _csv_paths(org_input)
             if p.parent == org_input and p.name.startswith("members-info")]
    for path in infos:
        try:
            df = ingest.load_members_info_file(path, cfg)
        except (OSError, ValueError) as exc:
            # 読めなければ部署名の照合を保証できない。「取りこぼしより誤検出」の方針の
            # 帰結として、不完全な禁止語集合で書き込みを続行せずここで止める
            raise DiscussionError(
                f"他組織の {path} を読めないため混入チェックを保証できません: {exc}"
            ) from exc
        for col in ("department", "team"):
            if col not in df.columns:
                continue
            for cell in df[col].dropna().astype(str):
                for name in ingest.parse_affiliations(cell):
                    if len(name.strip()) >= _MIN_TERM_LEN:
                        terms.add(Term(name.strip(), "group"))
    return terms


def forbidden_terms(
    *, input_dir: Path, output_dir: Path, target_org: str | None, cfg: dict,
) -> tuple[Term, ...]:
    """他組織に由来する語（組織名・メール・人名トークン・部署/チーム名）。

    収集元が1件でも読めない場合は DiscussionError にする（不完全な集合で通さない）。
    """
    others = (
        _org_dir_names(input_dir, _is_org_input_dir)
        | _org_dir_names(output_dir, _is_org_output_dir)
    ) - {target_org}
    terms: set[Term] = {Term(o, "org") for o in others}
    for org in sorted(others):
        org_input = input_dir / org
        # 入力が消えている（reports 側にだけ残っている）組織でも、生成済みレポートから
        # メール・人名を拾えるようにする。拾えないと組織名1件だけの禁止語になる
        sources = _csv_paths(org_input) + _files_under(output_dir / org, (".csv", ".md"))
        for path in sources:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise DiscussionError(
                    f"他組織の {path} を読めないため混入チェックを保証できません: {exc}"
                ) from exc
            for email in _emails_in_text(text):
                terms |= _terms_from_email(email)
        terms |= _group_terms(org_input, cfg)
    # 長さの下限は「メールや部署名から派生した語」の誤検出対策。組織名は派生語ではなく
    # 明示的な識別子なので対象外にする（1文字の組織名も許される）
    return tuple(sorted(
        (t for t in terms if t.kind == "org" or len(t.text) >= _MIN_TERM_LEN),
        key=lambda t: (t.text, t.kind),
    ))


# 同じ文字列が複数の kind で登録されたときにどれを採るかの優先順（小さいほど厳しい）。
# 組織名とドメインが `acme` / `acme.com` のように重なる場合、緩い側（domain）を採ると
# 「--allow-term で許可できます」と誤って案内してしまうため、常に厳しい側へ寄せる。
_KIND_RANK = {"org": 0, "address": 1, "domain": 2, "person": 3, "group": 4}


def _merge_terms(terms: tuple[Term, ...]) -> list[Term]:
    """同一文字列（大文字小文字を無視）を、より厳しい kind に寄せて1件にまとめる。"""
    best: dict[str, Term] = {}
    for term in terms:
        low = term.text.lower()
        current = best.get(low)
        if current is None or _KIND_RANK.get(term.kind, 99) < _KIND_RANK.get(current.kind, 99):
            best[low] = term
    return [best[k] for k in sorted(best)]


def _boundary(term: str, kind: str) -> str:
    """語に応じた語境界パターン。

    短い日本語の語は漢字・カタカナも語の一部とみなし、一般語への部分一致を避ける。
    ただし組織名は例外で常に積極照合する。数が少なく誤検出の余地が小さい一方、通した
    場合の影響が最も大きいため（短い規則だと「東京」が「東京支社」に一致しなくなる）。
    """
    if kind != "org" and len(term) <= _JP_STRICT_MAX_LEN and _HAS_JP_RE.search(term):
        return _WORDISH_JP
    return _WORDISH


def _search_term(haystack_lower: str, term_lower: str, kind: str) -> re.Match | None:
    wordish = _boundary(term_lower, kind)
    pattern = rf"(?<!{wordish}){re.escape(term_lower)}(?!{wordish})"
    return re.search(pattern, haystack_lower)


def _context_of(text: str, match: re.Match) -> str:
    start = max(0, match.start() - _CONTEXT_CHARS)
    end = min(len(text), match.end() + _CONTEXT_CHARS)
    return text[start:end].replace("\n", " ").strip()


def find_leaks(
    text: str, terms: tuple[Term, ...], *, source: str, allow: tuple[str, ...] = (),
) -> tuple[LeakHit, ...]:
    """text に現れる他組織由来の語。

    組織名は常時禁止。それ以外は対象組織の資料 source に現れる語を除外する
    （ドメイン共有・同姓による誤検出を避けるため）。allow に挙げた語は、人が内容を
    確認して無害と判断したものとして除外する。ただし組織名とメールアドレスは
    allow の対象外（Term.allowable）— 一般語と衝突する余地が実質なく、影響が大きいため。
    """
    lowered, source_lower = text.lower(), source.lower()
    allowed = {a.strip().lower() for a in allow if a.strip()}
    hits: dict[str, LeakHit] = {}
    for term in _merge_terms(terms):
        low = term.text.lower()
        if term.allowable and low in allowed:
            continue
        match = _search_term(lowered, low, term.kind)
        if match is None:
            continue
        if not term.always_forbidden and _search_term(source_lower, low, term.kind):
            continue
        hits.setdefault(term.text, LeakHit(
            term.text, term.kind, _context_of(text, match), term.allowable))
    return tuple(hits[k] for k in sorted(hits))


# ---------------------------------------------------------------- ヘッドレス実行


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```$", re.DOTALL)
# API エラーは出力の先頭に限らない（ストリーミング途中で失敗すると本文の後に付く）。
# 行頭一致を全行に対して探す
_API_ERROR_RE = re.compile(
    r"^[ \t]*API Error:?[ \t]*(?P<status>\d{3})?", re.IGNORECASE | re.MULTILINE)
_DISCUSSION_HEADING_RE = re.compile(r"^##[ \t]*考察[ \t]*$\n?", re.MULTILINE)
# 考察として最低限備えるべき形。プロンプトで `###` の小見出しを指示しているため、
# 1つも無い出力は指示に従っていない（前置きだけ・拒否文・途中で切れた出力等）
_HEADING_RE = re.compile(r"^###[ \t]*\S", re.MULTILINE)
# ツール呼び出しのマークアップ。ツールは渡していないが、モデルがテキストとして
# 捏造することがある。そのまま書き込むとレポートにマークアップが残る
_TOOL_MARKUP_RE = re.compile(
    r"</?\s*(?:function_calls|function_results|invoke|antml:[a-z_]+)\b", re.IGNORECASE)


def _strip_fence(text: str) -> str:
    """全体がコードフェンスで囲まれていたら剥がす（指示に反した出力の救済）。"""
    m = _FENCE_RE.match(text.strip())
    return m.group(1).strip() if m else text.strip()


def _strip_discussion_heading(text: str) -> str:
    """「## 考察」の見出し行を落とす。

    プロンプトでは出力しないよう指示しているが、付いてきた場合に差し込むとレポートに
    同名の H2 が2つ並ぶ。前置き（「以下が考察です。」等）の後に見出しが来ることも
    あるため、先頭限定ではなく行全体が見出しの箇所をすべて対象にする。
    """
    return _DISCUSSION_HEADING_RE.sub("", text.strip()).strip()


def _api_error_is_transient(status: str | None) -> bool:
    """API エラーが再実行で解消しうるものか（429 と 5xx のみ）。

    認証・引数・コンテキスト長などの 4xx を再試行しても同じ結果になり、待ち時間だけ増える。
    ステータスが読めない場合は一時扱いにする（通信断のメッセージ等）。
    """
    if status is None:
        return True
    code = int(status)
    return code == 429 or 500 <= code < 600


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
        # 組み込みツールを空集合にする（許可リスト方式が主たる保証）
        "--tools", "",
        "--disallowedTools", *DISALLOWED_TOOLS,
        # 設定ファイル由来の MCP 設定を無視する。空の作業ディレクトリに .mcp.json を
        # 置かれた場合の保険。claude.ai 管理の MCP サーバには効かず、それを落として
        # いるのは --safe-mode（実測: --safe-mode を外すと claude mcp list に3件残る）。
        # つまり MCP は組み込みツールのような二重防御になっていない
        "--strict-mcp-config",
        # 対象組織のレポート全文が ~/.claude のトランスクリプトに残らないようにする
        "--no-session-persistence",
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
        # claude -p は API エラーでも 0 を返すため、非ゼロは使い方・設定の誤りとみなす
        detail = ((proc.stderr or "").strip() or out).splitlines()
        raise DiscussionError(
            f"{command} が異常終了しました (exit {proc.returncode}): "
            f"{detail[0][:300] if detail else '出力なし'}"
        )
    # API エラー・空出力・形の崩れた出力をそのまま考察として書き込まないための門。
    # いずれも再実行で解消しうるため transient として扱う（4xx の API エラーを除く）
    if not out:
        raise DiscussionError(f"{command} の出力が空でした", transient=True)
    if m := _API_ERROR_RE.search(out):
        line = out[m.start():].splitlines()[0].strip()
        raise DiscussionError(
            f"{command} が API エラーを返しました: {line[:300]}",
            transient=_api_error_is_transient(m.group("status")),
        )
    body = _strip_discussion_heading(_strip_fence(out))
    if len(body) < int(s["min_output_chars"]):
        raise DiscussionError(
            f"生成された考察が短すぎます（{len(body)} 文字 < "
            f"{s['min_output_chars']} 文字）: {body[:200]}", transient=True,
        )
    # 長さだけでは前置き・拒否文・捏造されたツール呼び出しを弾けないため、
    # 「考察の形をしているか」を肯定的に検査する
    if mk := _TOOL_MARKUP_RE.search(body):
        raise DiscussionError(
            f"出力にツール呼び出しのマークアップが含まれます: {mk.group(0)}", transient=True)
    if not _HEADING_RE.search(body):
        raise DiscussionError(
            f"出力に `###` の小見出しがありません（考察の形になっていません）: {body[:200]}",
            transient=True,
        )
    return body


def _call_with_retry(runner, prompt: str, s: dict, notify) -> str:
    """一時的な失敗（429・5xx・タイムアウト等）に限ってリトライする。"""
    retries = max(0, int(s["retries"]))
    wait = float(s["retry_wait_seconds"])
    wait = wait if math.isfinite(wait) and wait > 0 else 0.0
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
    allow: tuple[str, ...] = (), include_previous: bool = False, runner=None, notify=None,
) -> DiscussionOutcome:
    """1組織1月ぶんの考察を生成して書き込む。

    既に記入済みの考察は force が無ければ上書きしない（手書きの考察を守るため）。
    混入が検出された場合は書き直しを求め、max_attempts 回で解消しなければ書き込まない。
    """
    # 既定の runner は呼び出し時に解決する（デフォルト引数で束縛すると差し替えが効かない）
    runner = runner or run_claude
    notify = notify or (lambda _message: None)
    s = settings(cfg)
    # config の allow_terms は「恒久的に無害と確認済みの語」。--allow-term は単一組織
    # 実行に限られるため、全組織実行でも効く許可の置き場としてこちらを使う
    allow = tuple(allow) + tuple(s.get("allow_terms") or ())
    doc_path = document_path(org_output, month, preview)
    scope = f"{org} {month}" if org else month

    terms = forbidden_terms(
        input_dir=input_dir, output_dir=output_dir, target_org=org, cfg=cfg)
    materials, source = collect_materials(
        org_output=org_output, month=month, preview=preview, terms=terms,
        include_previous=include_previous, notify=notify)
    prompt = build_prompt(org=org, scope=scope, materials=materials, preview=preview)

    # --dry-run はプロンプトの確認用なので、上書き可否に関係なく必ずプロンプトを返す
    if dry_run:
        return DiscussionOutcome(org, month, doc_path, "dry-run", prompt=prompt)
    if not force and (existing := _existing_discussion(doc_path)):
        return DiscussionOutcome(org, month, doc_path, "kept", chars=len(existing))

    max_attempts = max(1, int(s["max_attempts"]))
    leaks: tuple[LeakHit, ...] = ()
    for attempt in range(1, max_attempts + 1):
        body = _call_with_retry(
            runner,
            build_prompt(org=org, scope=scope, materials=materials,
                         preview=preview, leaks=leaks) if leaks else prompt,
            s, notify,
        )
        leaks = find_leaks(body, terms, source=source, allow=allow)
        if leaks:
            # 検出語は必ず運用者に残す。書き直しで解消した場合に何も出さないと、
            # 誤検出なら「正当な記述が静かに削られた」ことに気づけず、真の混入なら
            # 「モデルが他組織名を出力しようとした」という兆候の記録が消える。
            # 最終試行分は blocked として呼び出し側が表示するため、ここでは出さない
            if attempt < max_attempts:
                notify(f"試行 {attempt}: 混入を検出したため書き直しを求めます")
                for hit in leaks:
                    notify(f"  検出語: {hit.term}（{hit.kind}） … {hit.context} …")
            continue
        # 生成には最大で timeout_seconds かかる。その間に人が考察を書いた場合や
        # 並行する analyze が本文を更新した場合に、それを巻き戻さないよう書き込み側で
        # 判定と置換を畳み、置換直前にも内容が変わっていないか確認する
        if not report.write_discussion(doc_path, body, only_if_unwritten=not force):
            notify("生成中にレポートが変更されたため書き込みません（上書きは --force）")
            existing = _existing_discussion(doc_path) or ""
            return DiscussionOutcome(org, month, doc_path, "kept", chars=len(existing))
        return DiscussionOutcome(
            org, month, doc_path, "written", attempts=attempt, chars=len(body))
    return DiscussionOutcome(
        org, month, doc_path, "blocked", attempts=max_attempts, leaks=leaks)


def _existing_discussion(doc_path: Path) -> str | None:
    return report.discussion_body(doc_path.read_text(encoding="utf-8"))
