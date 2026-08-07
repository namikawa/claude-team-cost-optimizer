"""他組織情報の混入チェック（照合エンジン）。

他組織の入力 CSV と生成済みレポートから禁止語（組織名・メールアドレス・人名トークン・
部署/チーム名）を集め、対象のテキストに現れるものを検出する。考察の執筆（`discussion`）と
公開テキストの検査（`public_text`）が共通で使う土台で、このモジュール自身はどちらにも
依存しない。

- 照合は「取りこぼし（他組織情報を通す）」より「誤検出（正当な記述を止める）」に倒す。
  取りこぼしは他組織の情報が担当者に共有される一方、誤検出は止まるだけで人が確認すれば
  済む。誤検出を人が判断できるよう、検出結果には一致箇所の文脈を含める
- 禁止語の収集元が読めない場合は中止する（fail-closed）。不完全な禁止語集合で照合を
  続けると、検出漏れが「検出なし」と区別できなくなるため
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import ingest

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
    """照合・検査・考察の生成に失敗した。

    考察の生成で送出された場合、レポートは書き換えていない。
    transient=True は再実行で解消しうる失敗（通信の一時障害・429・5xx・タイムアウト）で、
    認証や設定の誤りのような恒久的な失敗と区別してリトライの可否を決める。考察の生成
    以外の経路では常に False。
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

    公開済みと分かっている組織名を通したい場合は、source で緩めるのではなく呼び出し側で
    terms から外す（`config.yaml > discussion.public_org_names`）。source は変動する
    内容なので、影響が最大の組織名の除外根拠にはしない。
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
