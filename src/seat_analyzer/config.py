"""設定のロード（パッケージ内の既定設定 + ワークスペース側の差分上書き）。

既定値はパッケージに同梱する default-config.yaml が持ち、利用者のワークスペースに置く
config.yaml は差分だけを書く上書きファイルとして扱う。モデル単価やカラムのエイリアス表は
プログラムの更新で全利用者へ配り、組織固有の設定だけが手元に残る形にするため。
"""

from __future__ import annotations

import math
from pathlib import Path

import yaml

from .ingest import MEMBERS_OPTIONAL_COLUMNS, REQUIRED_COLUMNS, SPEND_OPTIONAL_COLUMNS

# パッケージ同梱の既定設定（prompts/・templates/ と同じ流儀でパッケージ内から読む）
PACKAGE_CONFIG_PATH = Path(__file__).parent / "default-config.yaml"
# ワークスペースに置く上書きファイルの名前（--config 省略時にカレントから探す）
WORKSPACE_CONFIG_NAME = "config.yaml"

# config.yaml > discussion セクションの既定値。未指定の項目は discussion_settings() が補完する
DISCUSSION_DEFAULTS: dict = {
    "command": "claude",
    "model": "opus",
    "effort": "xhigh",
    "timeout_seconds": 1800,
    "max_attempts": 2,
    "min_output_chars": 200,
    "retries": 2,
    "retry_wait_seconds": 30,
    "allow_terms": (),
    "public_org_names": (),
}


def discussion_settings(cfg: dict) -> dict:
    """config.yaml > discussion を既定値で補完した設定。"""
    merged = dict(DISCUSSION_DEFAULTS)
    merged.update(cfg.get("discussion") or {})
    return merged


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
            cfg = _merge_override(cfg, override, label=str(override_path))
    elif path is not None:
        raise FileNotFoundError(f"設定ファイルが見つかりません: {override_path}")
    else:
        # 省略時は「無ければ既定のみ」で続行するが、それは存在しないときだけ。
        # ディレクトリやリンク切れを黙って無視すると、上書きしたつもりの設定が
        # 効かないまま分析が完走する
        validate_config_path(override_path)

    for key in ("seats", "decision", "model_prices", "columns"):
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


class _StrictLoader(yaml.SafeLoader):
    """マッピングの重複キーを拒否する SafeLoader。

    既定の読み込みは同じキーが2度現れると後の値で黙って上書きする。同じセクションを
    2回書いた設定では先に書いた側が丸ごと消え、そこに混ざった綴り違いのキーも
    既定との突合に届かないまま「効いているつもり」の状態になる。
    """

    def construct_mapping(self, node, deep: bool = False):
        seen: set = set()
        for key_node, _ in node.value:
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

    # 需要指標の算出基準。綴り違いは auto と同じ扱いで黙って通るため列挙を検証する
    # （小文字化して照合するのは pricing.resolve_cost_basis に合わせるため）
    basis = cfg.get("cost_basis", "auto")
    bases = ("computed", "net_spend", "auto")
    if not (isinstance(basis, str) and basis.lower() in bases):
        errors.append(f"cost_basis は {' / '.join(bases)} のいずれかが必要です")

    # discussion は任意セクション（未指定なら DISCUSSION_DEFAULTS が使われる）
    disc = cfg.get("discussion")
    if disc is not None:
        if not isinstance(disc, dict):
            errors.append("discussion セクションが辞書ではありません")
        else:
            for key in ("command", "model", "effort"):
                if key in disc and not (isinstance(disc[key], str) and disc[key].strip()):
                    errors.append(f"discussion.{key} は空でない文字列が必要です")
            efforts = ("low", "medium", "high", "xhigh", "max")
            if "effort" in disc and disc["effort"] not in efforts:
                errors.append(f"discussion.effort は {'/'.join(efforts)} のいずれかが必要です")
            # 回数は int() で黙って切り捨てられると意図と違う挙動になるため整数を要求する。
            # 秒数は inf/NaN を弾く（time.sleep(inf) は OverflowError で実行を止める）
            for key in ("max_attempts", "min_output_chars", "retries"):
                if key in disc and not _int(disc[key]):
                    errors.append(f"discussion.{key} は整数が必要です")
            if "max_attempts" in disc and _int(disc["max_attempts"]) and disc["max_attempts"] < 1:
                errors.append("discussion.max_attempts は 1 以上が必要です")
            if "min_output_chars" in disc and _int(disc["min_output_chars"]) \
                    and disc["min_output_chars"] < 1:
                errors.append("discussion.min_output_chars は 1 以上が必要です")
            if "retries" in disc and _int(disc["retries"]) and disc["retries"] < 0:
                errors.append("discussion.retries は 0 以上が必要です")
            if "timeout_seconds" in disc and (
                not _finite(disc["timeout_seconds"]) or disc["timeout_seconds"] <= 0
            ):
                errors.append("discussion.timeout_seconds は正の有限な数値が必要です")
            if "retry_wait_seconds" in disc and (
                not _finite(disc["retry_wait_seconds"]) or disc["retry_wait_seconds"] < 0
            ):
                errors.append("discussion.retry_wait_seconds は 0 以上の有限な数値が必要です")
            for key in ("allow_terms", "public_org_names"):
                values = disc.get(key)
                if values is not None and not (
                    isinstance(values, list)
                    and all(isinstance(v, str) and v.strip() for v in values)
                ):
                    errors.append(f"discussion.{key} は空でない文字列のリストが必要です")

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
