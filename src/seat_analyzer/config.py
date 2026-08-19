"""設定のロード（パッケージ内の既定設定 + ワークスペース側の差分上書き）。

既定値はパッケージに同梱する default-config.yaml が持ち、利用者のワークスペースに置く
config.yaml は差分だけを書く上書きファイルとして扱う。モデル単価やカラムのエイリアス表は
プログラムの更新で全利用者へ配り、組織固有の設定だけが手元に残る形にするため。
"""

from __future__ import annotations

import math
import unicodedata
from pathlib import Path

import yaml

from .ingest import MEMBERS_OPTIONAL_COLUMNS, REQUIRED_COLUMNS, SPEND_OPTIONAL_COLUMNS

# パッケージ同梱の既定設定（prompts/・templates/ と同じ流儀でパッケージ内から読む）
PACKAGE_CONFIG_PATH = Path(__file__).parent / "default-config.yaml"
# ワークスペースに置く上書きファイルの名前（--config 省略時にカレントから探す）
WORKSPACE_CONFIG_NAME = "config.yaml"


def load_config(path: str | Path | None = None) -> dict:
    """既定設定に上書きファイルを重ねた設定。

    path を省略するとカレントの config.yaml を上書きとして使う（無ければ既定のみ）。
    path を明示した場合はそのファイルが必須で、無ければ FileNotFoundError にする。
    """
    cfg = _read_mapping(PACKAGE_CONFIG_PATH, allow_empty=False)
    override_path = Path(path) if path is not None else Path(WORKSPACE_CONFIG_NAME)
    if override_path.is_file():
        override = _read_mapping(override_path, allow_empty=True)
        if override:
            override = _rebase_paths(override, override_path)
            cfg = _merge_override(cfg, override, label=str(override_path))
    elif path is not None:
        raise FileNotFoundError(f"設定ファイルが見つかりません: {override_path}")
    else:
        # 省略時は「無ければ既定のみ」で続行するが、それは存在しないときだけ。
        # ディレクトリやリンク切れを黙って無視すると、上書きしたつもりの設定が
        # 効かないまま分析が完走する
        validate_config_path(override_path)

    for key in ("paths", "seats", "decision", "model_prices", "columns", "discussion",
                "product_policy"):
        if key not in cfg:
            raise ValueError(f"設定に '{key}' セクションがありません")
    _validate(cfg)
    return cfg


def validate_config_path(path: Path) -> None:
    """設定ファイルとして読める形かを確かめる（存在しない場合は何もしない）。

    存在するのに通常のファイルでない（ディレクトリ・リンク切れ等）ときにエラーにする。
    """
    if path.is_file() or not (path.is_symlink() or path.exists()):
        return
    raise ValueError(
        f"{path} は通常のファイルではありません"
        "（ディレクトリやリンク切れを設定ファイルとして読めません。"
        "取り除くか --config で別のファイルを指定してください）"
    )


# YAML のマージキー（`<<: *anchor`）が持つタグ。キーそのものに値は無く、基底の
# 実装が展開して取り除く
_MERGE_TAG = "tag:yaml.org,2002:merge"


class _StrictLoader(yaml.SafeLoader):
    """マッピングの重複キーを拒否する SafeLoader。

    既定の読み込みは同じキーが2度現れると後の値で黙って上書きする。同じセクションを
    2回書いた設定では先に書いた側が丸ごと消え、そこに混ざった綴り違いのキーも
    既定との突合に届かないまま「効いているつもり」の状態になる。

    アンカーの展開は基底の実装に任せる（重複の判定は明示的に書かれたキーどうしだけで
    行う。マージで来たキーを明示キーで上書きするのは YAML の仕様）。マージキー自身は
    1つのマッピングに1つだけ許す。2つ並べると展開の優先が正規のリスト形式
    （`<<: [*a, *b]` は先に書いた基底が勝つ）と逆になるため、書いた順序の読み方が
    形によって変わってしまう。
    """

    def construct_mapping(self, node, deep: bool = False):
        seen: set = set()
        merge_keys = 0
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                # 値を持たないキー（構成子が無いため construct_object では作れない）。
                # 複数の基底を使うときは1つのマージキーにリストで並べる
                merge_keys += 1
                if merge_keys > 1:
                    raise yaml.constructor.ConstructorError(
                        None, None, "キー '<<' が重複しています", key_node.start_mark)
                continue
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicated = key in seen
            except TypeError:
                continue  # ハッシュできないキーは基底の実装がエラーにする
            if duplicated:
                raise yaml.constructor.ConstructorError(
                    None, None, f"キー '{key}' が重複しています", key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _read_mapping(path: Path, *, allow_empty: bool) -> dict:
    """YAML をマッピングとして読む。allow_empty なら空ファイル（全行コメント）は {}。"""
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.load(f, Loader=_StrictLoader)
    except FileNotFoundError:
        if path == PACKAGE_CONFIG_PATH:
            raise FileNotFoundError(
                f"既定の設定ファイルが見つかりません: {path}"
                "（インストールが壊れている可能性があります）"
            ) from None
        raise
    except yaml.YAMLError as e:
        raise ValueError(f"{path} の YAML が不正です: {e}") from None
    if data is None and allow_empty:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} の内容が設定のマッピングではありません")
    return data


def _is_package_config(path: Path) -> bool:
    """path がパッケージ同梱の既定設定そのものか。

    比較は解決後のパスで行う（相対指定・symlink 経由でも同じ判定にする）。解決できない
    場合は書かれたとおりに比べる（判定を誤っても上書きとして扱われるだけで、既定の
    値そのものは変わらない）。
    """
    try:
        return path.resolve() == PACKAGE_CONFIG_PATH.resolve()
    except OSError:
        return path == PACKAGE_CONFIG_PATH


def _rebase_paths(override: dict, config_path: Path) -> dict:
    """上書きファイルの paths を、その設定ファイルの置き場所を基準に解決した上書きを返す。

    ワークスペースの config.yaml に書いた入出力先は、どのディレクトリから実行しても
    同じ場所を指す（--config で別の場所の設定を読んだときは、その設定の隣を見る）。
    基準を与えるのは上書きファイルに書かれた値だけで、パッケージ内の既定
    （input / reports）と CLI のフラグはカレントディレクトリ基準のままにする。
    重ねたあとの設定からは値の出所が分からなくなるため、マージの前に解決する。

    --config でパッケージ内の既定そのものを指した場合も既定として扱う
    （指した場所がパッケージの中なので、解決するとそこを入力先にしてしまう）。
    同じファイルかどうかは書き方に依らせない（相対指定でも既定は既定）。
    """
    paths = override.get("paths")
    if not isinstance(paths, dict) or _is_package_config(config_path):
        return override    # 種別の誤りは _merge_override が既定と突き合わせて報告する
    base = config_path.parent
    resolved = dict(paths)
    for key, value in paths.items():
        # 文字列でない値と空文字は解決せずに通し、_merge_override と _validate に報告させる
        if isinstance(value, str) and value.strip():
            resolved[key] = _resolve_path_value(value, base, label=f"{config_path} の paths.{key}")
    return {**override, "paths": resolved}


def _resolve_path_value(value: str, base: Path, *, label: str) -> str:
    """設定に書かれたディレクトリを、基準 base から解決したパスにする。

    `~` は自分で展開する。設定ファイルはシェルを介さないため、展開しないと `~` という
    名前のディレクトリを黙って指す（雛形の作成ではそれが実際に作られる）。
    絶対パスと展開後のホーム配下はそのまま使う（pathlib の連結は右側が絶対なら基準を捨てる）。
    """
    try:
        path = Path(value).expanduser()
    except RuntimeError:
        # ホームディレクトリを特定できない環境。書かれたとおりに `~` のディレクトリを
        # 指すより、意図と違う場所を使わずに止める
        raise ValueError(f"{label} の '~' を展開できません（ホームディレクトリが不明です）") from None
    return str(base / path)


def _kind(value) -> str:
    """マージで区別する値の種別（表示用の名前）。

    辞書とそれ以外だけを見分けると、数値の位置にリストを書いた上書きが素通りして、
    後段の計算まで進んでから型エラーになる。
    """
    if isinstance(value, dict):
        return "辞書"
    if isinstance(value, list):
        return "リスト"
    return "値"


def _merge_override(base: dict, override: dict, *, label: str, path: tuple[str, ...] = ()) -> dict:
    """既定設定 base に上書き override を重ねた辞書を返す（base は変更しない）。

    辞書はキー単位で再帰マージし、リストと値は丸ごと置換する（単価表のような一覧は
    部分的に混ぜると意図しない並びになるため）。

    この設定はどの階層でもキーが閉じた集合なので、既定に無いキーはエラーにする。
    綴り違い（columns.spend.emial 等）を黙って無視すると、上書きしたつもりの値が
    効かないまま既定で分析が完走してしまう。
    """
    merged = dict(base)
    for key, value in override.items():
        where = ".".join((*path, str(key)))
        if key not in base:
            raise ValueError(
                f"{label} の '{where}' は既定に存在しないキーです（綴りを確認してください）")
        current = base[key]
        if value is None:
            raise ValueError(
                f"{label} の '{where}' の値が空です"
                "（既定のままにする項目は行ごと消してください）"
            )
        want, got = _kind(current), _kind(value)
        if want != got:
            raise ValueError(
                f"{label} の '{where}' は{want}で指定してください（{got}が書かれています）")
        merged[key] = (
            _merge_override(current, value, label=label, path=(*path, str(key)))
            if want == "辞書" else value
        )
    return merged


def _validate(cfg: dict) -> None:
    """料金改定などで config.yaml を編集した際のミスを実行前に検出する。"""
    errors: list[str] = []

    def _num(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def _int(v) -> bool:
        return isinstance(v, int) and not isinstance(v, bool)

    def _finite(v) -> bool:
        # 巨大な int は float 変換で OverflowError になる。設定ミスとして扱う。
        # 金額・比率には NaN・Infinity も使えない（比較が常に偽になり、判定が黙って
        # 変わる）ので、数値の検査はすべてこちらを通す
        try:
            return _num(v) and math.isfinite(v)
        except OverflowError:
            return False

    def _text(v) -> bool:
        return isinstance(v, str) and bool(v.strip())

    # 入出力ディレクトリ。空文字はカレントディレクトリとして解決され、意図しない場所を
    # 入力元・出力先にする（--input-dir の省略時にだけ効くので気づきにくい）
    paths = cfg["paths"]
    if not isinstance(paths, dict):
        errors.append("paths セクションが辞書ではありません")
    else:
        for key in ("input", "output"):
            if not _text(paths.get(key)):
                errors.append(f"paths.{key} は空でない文字列が必要です")

    for seat in ("standard", "premium"):
        s = cfg["seats"].get(seat)
        if not isinstance(s, dict):
            errors.append(f"seats.{seat} がありません")
            continue
        if not _finite(s.get("price_usd")) or s["price_usd"] < 0:
            errors.append(f"seats.{seat}.price_usd は 0 以上の有限な数値が必要です")
        allowance = s.get("allowance_usd")
        if not isinstance(allowance, dict):
            errors.append(f"seats.{seat}.allowance_usd がありません")
        else:
            for scenario in ("low", "mid", "high"):
                v = allowance.get(scenario)
                if not _finite(v) or v < 0:
                    errors.append(
                        f"seats.{seat}.allowance_usd.{scenario} は 0 以上の有限な数値が必要です")
            if all(_finite(allowance.get(k)) for k in ("low", "mid", "high")) and not (
                allowance["low"] <= allowance["mid"] <= allowance["high"]
            ):
                errors.append(f"seats.{seat}.allowance_usd は low <= mid <= high が必要です")
    std, prem = cfg["seats"].get("standard"), cfg["seats"].get("premium")
    if (
        isinstance(std, dict) and isinstance(prem, dict)
        and _finite(std.get("price_usd")) and _finite(prem.get("price_usd"))
        and prem["price_usd"] <= std["price_usd"]
    ):
        errors.append("seats.premium.price_usd は standard より大きい必要があります")

    d = cfg["decision"]
    # 真偽値は int の一種なので _int で除く（yes と書くと 1 として通ってしまう）
    if not _int(d.get("hysteresis_months")) or d["hysteresis_months"] < 1:
        errors.append("decision.hysteresis_months は 1 以上の整数が必要です")
    if not _finite(d.get("buffer_ratio")) or not 0 <= d["buffer_ratio"] <= 1:
        errors.append("decision.buffer_ratio は 0〜1 の数値が必要です")
    if not _finite(d.get("censoring_margin")) or d["censoring_margin"] <= 0:
        errors.append("decision.censoring_margin は正の数値が必要です")

    # 追加クレジットの表示閾値。既定設定が必ず持ち、上書きはキーを消せないため常に検査する
    # （NaN は上限到達の判定を黙って変え、非有限値は設定値の金額表示も壊す）
    uc = cfg["usage_credits"]
    if not isinstance(uc, dict):
        errors.append("usage_credits セクションが辞書ではありません")
    else:
        for key in ("cap_tolerance_usd", "grant_suggested_cap_usd"):
            if not _finite(uc.get(key)) or uc[key] < 0:
                errors.append(f"usage_credits.{key} は 0 以上の有限な数値が必要です")

    # 需要指標の算出基準。綴り違いは auto と同じ扱いで黙って通るため列挙を検証する
    # （小文字化して照合するのは pricing.resolve_cost_basis に合わせるため）
    basis = cfg.get("cost_basis", "auto")
    bases = ("computed", "net_spend", "auto")
    if not (isinstance(basis, str) and basis.lower() in bases):
        errors.append(f"cost_basis は {' / '.join(bases)} のいずれかが必要です")

    # product の分類。既定設定が必ず持ち、上書きはキーを消せないため常に検査する
    # （欠けている場合は既定設定の破損として同じ経路で報告する）
    policy = cfg["product_policy"]
    if not isinstance(policy, dict):
        errors.append("product_policy セクションが辞書ではありません")
    else:
        def _names(key: str) -> list[tuple[str, str]]:
            """(照合用に正規化した product 名, 設定に書かれた名前) を記述順で返す。

            前後空白・大小文字・Unicode の正規化形式の違いは同じ product 名として扱う
            （設定ミスを拾うのが目的なので取りこぼしより誤検出に倒す）。NFC 正規化して
            casefold する比較そのものは組織名の衝突判定 ingest.check_org_name_collisions
            と同じ。組織名は前後空白を含むこと自体を不正にしているのに対し、product 名は
            前後空白を落としてから比較する点だけが異なる。
            """
            names = policy.get(key)
            if not isinstance(names, list):
                return []
            return [
                (unicodedata.normalize("NFC", v.strip()).casefold(), v.strip())
                for v in names if _text(v)
            ]

        for key in ("primary", "supplementary", "prohibited"):
            names = policy.get(key)
            if not (isinstance(names, list) and all(_text(v) for v in names)):
                errors.append(f"product_policy.{key} は空でない文字列のリストが必要です")
        # 空リストは supplementary・prohibited では正当（該当なし）だが、primary が空だと
        # 「開発利用の主軸」を定義できず、活用の評価そのものが成立しない
        if isinstance(policy.get("primary"), list) and not policy["primary"]:
            errors.append("product_policy.primary には1つ以上の product 名が必要です")
        threshold = policy.get("supplementary_high_usd")
        if not _finite(threshold) or threshold < 0:
            errors.append(
                "product_policy.supplementary_high_usd は 0 以上の有限な数値が必要です")
        # 同一リスト内の重複は分類こそ決まるが書き間違いなので弾く
        for key in ("primary", "supplementary", "prohibited"):
            seen: set[str] = set()
            repeated: list[str] = []   # 報告は設定の記述順（集合の反復順に依らない）
            for normalized, written in _names(key):
                if normalized in seen:
                    repeated.append(written)
                seen.add(normalized)
            if repeated:
                errors.append(
                    f"product_policy.{key} に同じ product 名が複数あります: "
                    + ", ".join(repeated)
                )
        # 分類として排他なのは primary と supplementary だけ。prohibited は「この組織で
        # 使わせない」という直交する指定なので、primary・supplementary と重ねて書ける
        supplementary = {normalized for normalized, _ in _names("supplementary")}
        overlap = [w for normalized, w in _names("primary") if normalized in supplementary]
        if overlap:
            errors.append(
                "product_policy の primary と supplementary に同じ product 名があります"
                "（どちらの分類として数えるかが決まりません）: " + ", ".join(overlap)
            )

    # discussion の各項目は既定設定が必ず持ち、上書きはキーを消せないため、値の有無を
    # 条件にせず常に検査する（欠けている場合は既定設定の破損として同じ経路で報告する）
    disc = cfg["discussion"]
    if not isinstance(disc, dict):
        errors.append("discussion セクションが辞書ではありません")
    else:
        for key in ("command", "model", "effort"):
            if not (isinstance(disc.get(key), str) and disc[key].strip()):
                errors.append(f"discussion.{key} は空でない文字列が必要です")
        efforts = ("low", "medium", "high", "xhigh", "max")
        if disc.get("effort") not in efforts:
            errors.append(f"discussion.effort は {'/'.join(efforts)} のいずれかが必要です")
        # 回数は int() で黙って切り捨てられると意図と違う挙動になるため整数を要求する。
        # 秒数は inf/NaN を弾く（time.sleep(inf) は OverflowError で実行を止める）
        for key in ("max_attempts", "min_output_chars", "retries"):
            if not _int(disc.get(key)):
                errors.append(f"discussion.{key} は整数が必要です")
        if _int(disc.get("max_attempts")) and disc["max_attempts"] < 1:
            errors.append("discussion.max_attempts は 1 以上が必要です")
        if _int(disc.get("min_output_chars")) and disc["min_output_chars"] < 1:
            errors.append("discussion.min_output_chars は 1 以上が必要です")
        if _int(disc.get("retries")) and disc["retries"] < 0:
            errors.append("discussion.retries は 0 以上が必要です")
        if not _finite(disc.get("timeout_seconds")) or disc["timeout_seconds"] <= 0:
            errors.append("discussion.timeout_seconds は正の有限な数値が必要です")
        if not _finite(disc.get("retry_wait_seconds")) or disc["retry_wait_seconds"] < 0:
            errors.append("discussion.retry_wait_seconds は 0 以上の有限な数値が必要です")
        terms = disc.get("allow_terms")
        if not (
            isinstance(terms, list)
            and all(isinstance(v, str) and v.strip() for v in terms)
        ):
            errors.append("discussion.allow_terms は空でない文字列のリストが必要です")

    patterns = cfg["model_prices"].get("patterns")
    if not isinstance(patterns, list) or not patterns:
        errors.append("model_prices.patterns が空です")
    else:
        # match はモデル名との部分一致に使う文字列。数値等が入ると照合の時点で落ちる
        for i, pat in enumerate(patterns):
            if not isinstance(pat, dict) or not _text(pat.get("match")) \
                    or not _finite(pat.get("input")) or not _finite(pat.get("output")):
                errors.append(
                    f"model_prices.patterns[{i}] には match（空でない文字列）と"
                    "input/output（有限な数値）が必要です"
                )
    default = cfg["model_prices"].get("default")
    if not isinstance(default, dict) or not _finite(default.get("input")) \
            or not _finite(default.get("output")):
        errors.append("model_prices.default には input/output の有限な数値が必要です")

    # 入力処理が参照するカラムエイリアスが columns セクションに定義されているか。
    # 任意列は入力CSV上では省略可能だが、正準化の設定自体は必須とする。
    columns = cfg["columns"]
    if not isinstance(columns, dict):
        errors.append("columns セクションが辞書ではありません")
    else:
        for section, required in REQUIRED_COLUMNS.items():
            sec = columns.get(section)
            if not isinstance(sec, dict):
                errors.append(f"columns.{section} がありません")
                continue
            configured_columns = list(required)
            if section == "spend":
                configured_columns.extend(SPEND_OPTIONAL_COLUMNS)
            elif section == "members":
                configured_columns.extend(MEMBERS_OPTIONAL_COLUMNS)
            for canonical in configured_columns:
                aliases = sec.get(canonical)
                if not isinstance(aliases, list) or not aliases:
                    errors.append(f"columns.{section}.{canonical} のエイリアス定義がありません")

    if errors:
        raise ValueError("config.yaml の設定に問題があります:\n  - " + "\n  - ".join(errors))
