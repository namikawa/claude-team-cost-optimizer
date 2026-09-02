"""設定のロード（パッケージ内の既定設定 + ワークスペース側の差分上書き）。

既定値はパッケージに同梱する default-config.yaml が持ち、利用者のワークスペースに置く
config.yaml は差分だけを書く上書きファイルとして扱う。モデル単価やカラムのエイリアス表は
プログラムの更新で全利用者へ配り、組織固有の設定だけが手元に残る形にするため。
"""

from __future__ import annotations

import math
import os
from pathlib import Path, PurePath

import yaml

from .admin_inputs import ORGANIZATION_OPTIONAL_COLUMNS, USERS_OPTIONAL_COLUMNS
from .github_collect import is_github_org_name
from .ingest import MEMBERS_OPTIONAL_COLUMNS, REQUIRED_COLUMNS, SPEND_OPTIONAL_COLUMNS
from .product_usage import normalize_product_name

# 入力の正準化で参照する任意列（セクション → 正準名）。入力CSV上では省略できるが、
# 正準化の設定そのもの（エイリアス定義）は必須にする
_OPTIONAL_COLUMNS = {
    "spend": SPEND_OPTIONAL_COLUMNS,
    "members": MEMBERS_OPTIONAL_COLUMNS,
    "admin_organization": ORGANIZATION_OPTIONAL_COLUMNS,
    "admin_users": USERS_OPTIONAL_COLUMNS,
}

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
        # 設定の不正は ValueError に統一する（種類を変えると CLI のメッセージと終了コードが変わる）
        raise ValueError(f"{path} の内容が設定のマッピングではありません")  # noqa: TRY004
    return data


def _is_package_config(path: Path) -> bool:
    """path がパッケージ同梱の既定設定そのものか。

    比較は解決後のパスで行う（相対指定・symlink 経由でも同じ判定にする）。解決できない
    環境では、ファイルシステムに触れない字句の比較へ落とす。既定と判定できないと
    _rebase_paths が paths をパッケージのディレクトリ基準へ書き換えてしまうため、
    相対表記で既定を指した場合だけは resolve 抜きでも拾えるようにしておく
    （symlink 経由の指定と resolve の失敗が重なった場合は判定できない。ここは許容する）。
    """
    try:
        return path.resolve() == PACKAGE_CONFIG_PATH.resolve()
    except OSError:
        return _same_lexical_path(path, PACKAGE_CONFIG_PATH)


def _same_lexical_path(a: Path, b: Path) -> bool:
    """ファイルシステムに触れずに2つのパスを比べる。

    絶対化して `.` や `..` を畳み、Windows では大文字小文字と区切りの違いも吸収する
    （symlink は解決しないので、経路が違えば別のパスとして扱う）。
    """
    def key(path: Path) -> str:
        return os.path.normcase(os.path.normpath(path.absolute()))

    return key(a) == key(b)


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
    """設定に書かれたディレクトリを、基準 base から解決した絶対パスにする。

    `~` は自分で展開する。設定ファイルはシェルを介さないため、展開しないと `~` という
    名前のディレクトリを黙って指す（雛形の作成ではそれが実際に作られる）。
    絶対パスと展開後のホーム配下はそのまま使う（pathlib の連結は右側が絶対なら基準を捨てる）。

    基準は絶対化してから連結する。カレントの config.yaml を暗黙に読んだ場合の親は `.` で、
    そのまま連結すると相対パスのまま残り、「設定の置き場所が基準」ではなく実行時の
    カレント基準になってしまう。
    """
    try:
        path = Path(value).expanduser()
    except RuntimeError:
        # ホームディレクトリを特定できない環境。書かれたとおりに `~` のディレクトリを
        # 指すより、意図と違う場所を使わずに止める
        raise ValueError(f"{label} の '~' を展開できません（ホームディレクトリが不明です）") from None
    if _is_ambiguous_path(path):
        raise ValueError(
            f"{label} の値 '{value}' は起点が実行時に決まる曖昧なパスです"
            "（ドライブ文字から始まる絶対パスか、設定ファイルからの相対パスで書いてください）"
        )
    return str(base.absolute() / path)


def _is_ambiguous_path(path: PurePath) -> bool:
    """ドライブかルートだけを持ち、起点が実行時に決まるパスか。

    Windows の `D:data`（D ドライブのカレント基準）と `/foo`（カレントドライブのルート
    基準）が該当する。どちらも連結では基準を捨てるうえ、指す場所がプロセスの状態で
    変わるため、入出力先としては受け付けない。POSIX では `D:data` はただの相対名、
    `/foo` は絶対パスなので該当しない（判定はネイティブの Path で行うこと）。
    """
    return bool(path.drive or path.root) and not path.is_absolute()


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


# 既定に列挙できないキー（利用者ごとの組織名）を書けるセクションと、その各エントリの
# 雛形。ここに載るのは1階層だけで、エントリの中身は雛形との突き合わせで従来どおり厳格に
# 検査する（書けるキー・値の種別・空値の扱いが既定のセクションと同じになる）。
_DYNAMIC_ENTRIES: dict[tuple[str, ...], dict] = {
    ("organizations",): {"github_org": ""},
}


def _merge_override(base: dict, override: dict, *, label: str, path: tuple[str, ...] = ()) -> dict:
    """既定設定 base に上書き override を重ねた辞書を返す（base は変更しない）。

    辞書はキー単位で再帰マージし、リストと値は丸ごと置換する（単価表のような一覧は
    部分的に混ぜると意図しない並びになるため）。

    この設定はどの階層でもキーが閉じた集合なので、既定に無いキーはエラーにする。
    綴り違い（columns.spend.emial 等）を黙って無視すると、上書きしたつもりの値が
    効かないまま既定で分析が完走してしまう。例外は `_DYNAMIC_ENTRIES` に挙げた
    セクションの直下だけで、そこは利用者が名前を決めるキー（組織名）を受ける。
    """
    merged = dict(base)
    for key, value in override.items():
        where = ".".join((*path, str(key)))
        if key not in base:
            template = _DYNAMIC_ENTRIES.get(path)
            if template is None:
                raise ValueError(
                    f"{label} の '{where}' は既定に存在しないキーです（綴りを確認してください）")
            current = template
        else:
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


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite(value: object) -> bool:
    """有限の実数か。float 化で桁あふれする巨大な int も False。

    金額・比率には NaN・Infinity も使えない（比較が常に偽になり、判定が黙って
    変わる）ので、数値の検査はすべてこちらを通す。
    """
    try:
        return _is_number(value) and math.isfinite(value)
    except OverflowError:
        return False


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_paths(cfg: dict, errors: list[str]) -> None:
    # 入出力ディレクトリ。空文字はカレントディレクトリとして解決され、意図しない場所を
    # 入力元・出力先にする（--input-dir の省略時にだけ効くので気づきにくい）
    paths = cfg["paths"]
    if not isinstance(paths, dict):
        errors.append("paths セクションが辞書ではありません")
        return
    for key in ("input", "output"):
        if not _is_text(paths.get(key)):
            errors.append(f"paths.{key} は空でない文字列が必要です")


def _validate_seats(cfg: dict, errors: list[str]) -> None:
    seats = cfg["seats"]
    for seat in ("standard", "premium"):
        settings = seats.get(seat)
        if not isinstance(settings, dict):
            errors.append(f"seats.{seat} がありません")
            continue
        if not _is_finite(settings.get("price_usd")) or settings["price_usd"] < 0:
            errors.append(f"seats.{seat}.price_usd は 0 以上の有限な数値が必要です")
        allowance = settings.get("allowance_usd")
        if not isinstance(allowance, dict):
            errors.append(f"seats.{seat}.allowance_usd がありません")
            continue
        for scenario in ("low", "mid", "high"):
            value = allowance.get(scenario)
            if not _is_finite(value) or value < 0:
                errors.append(
                    f"seats.{seat}.allowance_usd.{scenario} は 0 以上の有限な数値が必要です"
                )
        if all(
            _is_finite(allowance.get(key)) for key in ("low", "mid", "high")
        ) and not allowance["low"] <= allowance["mid"] <= allowance["high"]:
            errors.append(
                f"seats.{seat}.allowance_usd は low <= mid <= high が必要です"
            )

    standard, premium = seats.get("standard"), seats.get("premium")
    if (
        isinstance(standard, dict)
        and isinstance(premium, dict)
        and _is_finite(standard.get("price_usd"))
        and _is_finite(premium.get("price_usd"))
        and premium["price_usd"] <= standard["price_usd"]
    ):
        errors.append("seats.premium.price_usd は standard より大きい必要があります")


def _validate_decision(cfg: dict, errors: list[str]) -> None:
    decision = cfg["decision"]
    # 真偽値は int の一種なので _is_integer で除く（yes と書くと 1 として通ってしまう）
    if (
        not _is_integer(decision.get("hysteresis_months"))
        or decision["hysteresis_months"] < 1
    ):
        errors.append("decision.hysteresis_months は 1 以上の整数が必要です")
    if (
        not _is_finite(decision.get("buffer_ratio"))
        or not 0 <= decision["buffer_ratio"] <= 1
    ):
        errors.append("decision.buffer_ratio は 0〜1 の数値が必要です")
    if (
        not _is_finite(decision.get("censoring_margin"))
        or decision["censoring_margin"] <= 0
    ):
        errors.append("decision.censoring_margin は正の数値が必要です")


def _validate_decision_v2(cfg: dict, errors: list[str]) -> None:
    # V2判定の設定。enabled が偽のあいだも値を検査する（有効化した時点で壊れている
    # 設定に気づくのでは遅い。編集ミスは編集した実行で検出する）
    decision = cfg["decision_v2"]
    if not isinstance(decision, dict):
        errors.append("decision_v2 セクションが辞書ではありません")
        return
    if not isinstance(decision.get("enabled"), bool):
        errors.append("decision_v2.enabled は真偽値が必要です")
    for direction in ("upgrade", "downgrade"):
        section = decision.get(direction)
        months = (
            section.get("min_complete_months") if isinstance(section, dict) else None
        )
        if not _is_integer(months) or months < 1:
            errors.append(
                f"decision_v2.{direction}.min_complete_months は 1 以上の整数が必要です"
            )
    # Code 需要の閾値は昇格・降格で向きが逆（以上で昇格・未満で降格）だが、値の
    # 契約は同じなので同じ粒度で検査する（片方だけ緩いと、その向きの判定が壊れた
    # 設定のまま動く）
    for direction, key in (
        ("upgrade", "min_code_demand_usd"),
        ("downgrade", "max_code_demand_usd"),
    ):
        section = decision.get(direction)
        threshold = section.get(key) if isinstance(section, dict) else None
        if not _is_finite(threshold) or threshold < 0:
            errors.append(
                f"decision_v2.{direction}.{key} は 0 以上の有限な数値が必要です"
            )
    days = decision.get("recent_seat_change_days")
    if not _is_integer(days) or days < 1:
        errors.append("decision_v2.recent_seat_change_days は 1 以上の整数が必要です")
    saving = decision.get("min_assignment_saving_usd")
    if not _is_finite(saving) or saving < 0:
        errors.append(
            "decision_v2.min_assignment_saving_usd は 0 以上の有限な数値が必要です"
        )


def _validate_usage_credits(cfg: dict, errors: list[str]) -> None:
    # 追加クレジットの表示閾値。既定設定が必ず持ち、上書きはキーを消せないため常に検査する
    # （NaN は上限到達の判定を黙って変え、非有限値は設定値の金額表示も壊す）
    usage_credits = cfg["usage_credits"]
    if not isinstance(usage_credits, dict):
        errors.append("usage_credits セクションが辞書ではありません")
        return
    for key in ("cap_tolerance_usd", "grant_suggested_cap_usd"):
        if not _is_finite(usage_credits.get(key)) or usage_credits[key] < 0:
            errors.append(f"usage_credits.{key} は 0 以上の有限な数値が必要です")


def _validate_organizations(cfg: dict, errors: list[str]) -> None:
    """GitHub 分析を有効にする組織の対応表を検査する。

    キーの綴り違いは「一致する組織ディレクトリが無い」形になるため、ここでは止めずに
    doctor が GITHUB_CONFIG_UNMATCHED として報告する（入力の配置はロード時に分からない）。
    値の側は GitHub の Organization 名として読める字句かをここで確かめる。読めない値を
    通すと、そのままリクエストのパスへ入って「参照できません」とだけ報告することになり、
    設定の書き間違いだと分からなくなる。
    """
    organizations = cfg["organizations"]
    if not isinstance(organizations, dict):
        errors.append("organizations セクションが辞書ではありません")
        return
    for name, entry in organizations.items():
        if not _is_text(name):
            errors.append(f"organizations のキーには組織名が必要です: {name!r}")
            continue
        if not isinstance(entry, dict):
            errors.append(f"organizations.{name} は辞書が必要です")
            continue
        if not is_github_org_name(entry.get("github_org")):
            errors.append(
                f"organizations.{name}.github_org は GitHub の Organization 名が"
                "必要です（英数字とハイフンの1〜39文字。先頭と末尾は英数字で、"
                "ハイフンは連続しません）"
            )


def _validate_cost_basis(cfg: dict, errors: list[str]) -> None:
    # 需要指標の算出基準。綴り違いは auto と同じ扱いで黙って通るため列挙を検証する
    # （小文字化して照合するのは pricing.resolve_cost_basis に合わせるため）
    basis = cfg.get("cost_basis", "auto")
    allowed = ("computed", "net_spend", "auto")
    if not (isinstance(basis, str) and basis.lower() in allowed):
        errors.append(f"cost_basis は {' / '.join(allowed)} のいずれかが必要です")


def _product_names(policy: dict, key: str) -> list[tuple[str, str]]:
    """正規化した product 名と設定上の表記を、記述順で返す。

    前後空白・大小文字・Unicode の正規化形式の違いは同じ product 名として扱う
    （設定ミスを拾うのが目的なので取りこぼしより誤検出に倒す）。比較そのものは
    組織名の衝突判定 ingest.check_org_name_collisions と同じだが、組織名は前後空白を
    含むこと自体を不正にしているのに対し、product 名は前後空白を落としてから
    比較する点だけが異なる。
    """
    names = policy.get(key)
    if not isinstance(names, list):
        return []
    return [
        (normalized, value.strip())
        for value in names
        if _is_text(value)
        and (normalized := normalize_product_name(value)) is not None
    ]


def _duplicate_product_names(policy: dict, key: str) -> list[str]:
    """同じ正規化名を2回目以降に記述したときの、元の表記。

    報告は設定の記述順（集合の反復順に依らない）。
    """
    seen: set[str] = set()
    repeated: list[str] = []
    for normalized, written in _product_names(policy, key):
        if normalized in seen:
            repeated.append(written)
        seen.add(normalized)
    return repeated


def _validate_product_policy(cfg: dict, errors: list[str]) -> None:
    # product の分類。既定設定が必ず持ち、上書きはキーを消せないため常に検査する
    # （欠けている場合は既定設定の破損として同じ経路で報告する）
    policy = cfg["product_policy"]
    if not isinstance(policy, dict):
        errors.append("product_policy セクションが辞書ではありません")
        return

    for key in ("primary", "supplementary", "prohibited"):
        names = policy.get(key)
        if not (isinstance(names, list) and all(_is_text(value) for value in names)):
            errors.append(f"product_policy.{key} は空でない文字列のリストが必要です")
    # 空リストは supplementary・prohibited では正当（該当なし）だが、primary が空だと
    # 「開発利用の主軸」を定義できず、活用の評価そのものが成立しない
    if isinstance(policy.get("primary"), list) and not policy["primary"]:
        errors.append("product_policy.primary には1つ以上の product 名が必要です")
    threshold = policy.get("supplementary_high_usd")
    if not _is_finite(threshold) or threshold < 0:
        errors.append(
            "product_policy.supplementary_high_usd は 0 以上の有限な数値が必要です"
        )

    # 同一リスト内の重複は分類こそ決まるが書き間違いなので弾く
    for key in ("primary", "supplementary", "prohibited"):
        repeated = _duplicate_product_names(policy, key)
        if repeated:
            errors.append(
                f"product_policy.{key} に同じ product 名が複数あります: "
                + ", ".join(repeated)
            )

    # 分類として排他なのは primary と supplementary だけ。prohibited は「この組織で
    # 使わせない」という直交する指定なので、primary・supplementary と重ねて書ける
    supplementary = {
        normalized for normalized, _ in _product_names(policy, "supplementary")
    }
    overlap = [
        written
        for normalized, written in _product_names(policy, "primary")
        if normalized in supplementary
    ]
    if overlap:
        errors.append(
            "product_policy の primary と supplementary に同じ product 名があります"
            "（どちらの分類として数えるかが決まりません）: " + ", ".join(overlap)
        )


def _validate_discussion_ranges(discussion: dict, errors: list[str]) -> None:
    """discussion の型検査後に、整数・秒数の範囲を検査する。

    回数は int() で黙って切り捨てられると意図と違う挙動になるため整数を要求する。
    秒数は inf/NaN を弾く（time.sleep(inf) は OverflowError で実行を止める）。
    """
    if (
        _is_integer(discussion.get("max_attempts"))
        and discussion["max_attempts"] < 1
    ):
        errors.append("discussion.max_attempts は 1 以上が必要です")
    if (
        _is_integer(discussion.get("min_output_chars"))
        and discussion["min_output_chars"] < 1
    ):
        errors.append("discussion.min_output_chars は 1 以上が必要です")
    if _is_integer(discussion.get("retries")) and discussion["retries"] < 0:
        errors.append("discussion.retries は 0 以上が必要です")
    if (
        not _is_finite(discussion.get("timeout_seconds"))
        or discussion["timeout_seconds"] <= 0
    ):
        errors.append("discussion.timeout_seconds は正の有限な数値が必要です")
    if (
        not _is_finite(discussion.get("retry_wait_seconds"))
        or discussion["retry_wait_seconds"] < 0
    ):
        errors.append("discussion.retry_wait_seconds は 0 以上の有限な数値が必要です")


def _validate_discussion(cfg: dict, errors: list[str]) -> None:
    # discussion の各項目は既定設定が必ず持ち、上書きはキーを消せないため、値の有無を
    # 条件にせず常に検査する（欠けている場合は既定設定の破損として同じ経路で報告する）
    discussion = cfg["discussion"]
    if not isinstance(discussion, dict):
        errors.append("discussion セクションが辞書ではありません")
        return

    for key in ("command", "model", "effort"):
        if not _is_text(discussion.get(key)):
            errors.append(f"discussion.{key} は空でない文字列が必要です")
    efforts = ("low", "medium", "high", "xhigh", "max")
    if discussion.get("effort") not in efforts:
        errors.append(f"discussion.effort は {'/'.join(efforts)} のいずれかが必要です")
    for key in ("max_attempts", "min_output_chars", "retries"):
        if not _is_integer(discussion.get(key)):
            errors.append(f"discussion.{key} は整数が必要です")
    _validate_discussion_ranges(discussion, errors)
    terms = discussion.get("allow_terms")
    if not (
        isinstance(terms, list)
        and all(isinstance(value, str) and value.strip() for value in terms)
    ):
        errors.append("discussion.allow_terms は空でない文字列のリストが必要です")


def _validate_model_prices(cfg: dict, errors: list[str]) -> None:
    model_prices = cfg["model_prices"]
    patterns = model_prices.get("patterns")
    if not isinstance(patterns, list) or not patterns:
        errors.append("model_prices.patterns が空です")
    else:
        # match はモデル名との部分一致に使う文字列。数値等が入ると照合の時点で落ちる
        for index, pattern in enumerate(patterns):
            if (
                not isinstance(pattern, dict)
                or not _is_text(pattern.get("match"))
                or not _is_finite(pattern.get("input"))
                or not _is_finite(pattern.get("output"))
            ):
                errors.append(
                    f"model_prices.patterns[{index}] には match（空でない文字列）と"
                    "input/output（有限な数値）が必要です"
                )
                continue
            # cache_read（キャッシュ読取の倍率）は任意。0 以下・非有限の倍率は需要を
            # 黙って過小に（あるいは無限に）見せるので、書かれた場合だけ検査する
            if "cache_read" in pattern and not (
                _is_finite(pattern["cache_read"]) and pattern["cache_read"] > 0
            ):
                errors.append(
                    f"model_prices.patterns[{index}].cache_read は正の有限な数値が必要です"
                    "（省略すると cache_multipliers.read を使います）"
                )
    default = model_prices.get("default")
    if (
        not isinstance(default, dict)
        or not _is_finite(default.get("input"))
        or not _is_finite(default.get("output"))
    ):
        errors.append("model_prices.default には input/output の有限な数値が必要です")


def _validate_columns(cfg: dict, errors: list[str]) -> None:
    # 入力処理が参照するカラムエイリアスが columns セクションに定義されているか。
    # 任意列は入力CSV上では省略可能だが、正準化の設定自体は必須とする。
    columns = cfg["columns"]
    if not isinstance(columns, dict):
        errors.append("columns セクションが辞書ではありません")
        return
    for section, required in REQUIRED_COLUMNS.items():
        aliases_by_name = columns.get(section)
        if not isinstance(aliases_by_name, dict):
            errors.append(f"columns.{section} がありません")
            continue
        configured = [*required, *_OPTIONAL_COLUMNS.get(section, ())]
        for canonical in configured:
            aliases = aliases_by_name.get(canonical)
            if not isinstance(aliases, list) or not aliases:
                errors.append(
                    f"columns.{section}.{canonical} のエイリアス定義がありません"
                )


def _validate(cfg: dict) -> None:
    """料金改定などで config.yaml を編集した際のミスを実行前に検出する。"""
    errors: list[str] = []

    _validate_paths(cfg, errors)
    _validate_seats(cfg, errors)
    _validate_decision(cfg, errors)
    _validate_decision_v2(cfg, errors)
    _validate_usage_credits(cfg, errors)
    _validate_organizations(cfg, errors)
    _validate_cost_basis(cfg, errors)
    _validate_product_policy(cfg, errors)
    _validate_discussion(cfg, errors)
    _validate_model_prices(cfg, errors)
    _validate_columns(cfg, errors)

    if errors:
        raise ValueError("config.yaml の設定に問題があります:\n  - " + "\n  - ".join(errors))
