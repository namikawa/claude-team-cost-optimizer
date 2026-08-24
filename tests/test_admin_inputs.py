"""管理画面の手入力 CSV（input/<組織名>/admin/）のロードと as-of 選択のテスト。

不明を保つこと（空欄・解釈できない値を 0 や False で埋めない）と、取り違えに直結する
ものを止めること（ファイル名の取得日と snapshot_date の食い違い・同日重複・
organization の複数行・email の欠落と重複）の両方を固定する。

読み取りだけのモジュールなので、同じ入力からは常に同じ結果と同じ並びになることまで
見る（取得日の昇順・email の昇順）。
"""

import copy
import datetime as dt
import inspect
import math
import re
from pathlib import Path

import pytest

from seat_analyzer import admin_inputs
from seat_analyzer.admin_inputs import (
    AdminInputs,
    OrganizationSnapshot,
    UserCreditRecord,
    UserCreditSnapshot,
    as_of,
    load_admin_inputs,
)

ORG_HEADER = (
    "Snapshot Date,Standard Purchased,Premium Purchased,Billing Frequency,Renewal Date,"
    "Standard Unit Price USD,Premium Unit Price USD,Org Credit Enabled,"
    "Org Credit Limit USD,Source"
)
ORG_MINIMAL_HEADER = "Snapshot Date,Source"
ORG_JP_HEADER = (
    "取得日,standard席数,premium席数,支払頻度,更新日,standard単価,premium単価,"
    "組織クレジット,組織クレジット上限,取得元"
)

USERS_HEADER = (
    "Snapshot Date,Email,Account UUID,Credit Enabled,Credit Limit USD,Credit MTD USD,Source"
)
USERS_MINIMAL_HEADER = "Snapshot Date,Email,Source"
USERS_JP_HEADER = "取得日,メールアドレス,account uuid,追加クレジット有効,追加クレジット上限,当月消費,取得元"


def _write(input_dir: Path, name: str, header: str, rows: list[str],
           encoding: str = "utf-8") -> Path:
    """admin/ 直下に CSV を1つ置く（行はそのまま書く）。"""
    directory = input_dir / "admin"
    directory.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{row}\n" for row in rows)
    (directory / name).write_text(header + "\n" + body, encoding=encoding, newline="\n")
    return input_dir


def _org_file(input_dir: Path, date: str, rows: list[str],
              header: str = ORG_HEADER, name: str | None = None) -> Path:
    return _write(input_dir, name or f"organization-{date}.csv", header, rows)


def _users_file(input_dir: Path, date: str, rows: list[str],
                header: str = USERS_HEADER, name: str | None = None) -> Path:
    return _write(input_dir, name or f"users-{date}.csv", header, rows)


def _organization(date: str, source: str = "organization.csv") -> OrganizationSnapshot:
    """as-of 選択の検証用に、日付と由来だけを持つ organization スナップショット。"""
    return OrganizationSnapshot(
        taken_on=dt.date.fromisoformat(date),
        standard_purchased=None,
        premium_purchased=None,
        billing_frequency=None,
        renewal_date=None,
        standard_unit_price_usd=None,
        premium_unit_price_usd=None,
        org_credit_enabled=None,
        org_credit_limit_usd=None,
        collected_via="manual",
        source=source,
    )


def _users(date: str, source: str = "users.csv") -> UserCreditSnapshot:
    return UserCreditSnapshot(
        taken_on=dt.date.fromisoformat(date), records=(), source=source
    )


def _record(email: str, **kwargs) -> UserCreditRecord:
    fields = {
        "account_uuid": None,
        "credit_enabled": None,
        "credit_limit_usd": None,
        "credit_mtd_usd": None,
        "collected_via": "manual",
    }
    return UserCreditRecord(email=email, **{**fields, **kwargs})


# ------------------------------------------------------------------- 入力なし


def test_no_admin_directory_is_an_empty_result(tmp_path, cfg):
    """admin/ が無ければ空の結果を返す（警告も出さない）。"""
    result = load_admin_inputs(tmp_path, cfg)
    assert result == AdminInputs(organization=(), users=(), warnings=())


def test_empty_admin_directory_is_an_empty_result(tmp_path, cfg):
    """admin/ があって CSV が無い場合も空の結果（運用していない組織で警告を出さない）。"""
    (tmp_path / "admin").mkdir()
    result = load_admin_inputs(tmp_path, cfg)
    assert result == AdminInputs(organization=(), users=(), warnings=())


# ------------------------------------------------------------- organization


def test_organization_row_is_read(tmp_path, cfg):
    """全列そろった organization を読む。"""
    _org_file(tmp_path, "2026-08-01", [
        "2026-08-01,7,35,monthly,2027-04-30,25,125,true,$1500,browser",
    ])
    result = load_admin_inputs(tmp_path, cfg)

    assert result.warnings == ()
    (snapshot,) = result.organization
    assert snapshot.taken_on == dt.date(2026, 8, 1)
    assert (snapshot.standard_purchased, snapshot.premium_purchased) == (7, 35)
    assert snapshot.billing_frequency == "monthly"
    assert snapshot.renewal_date == dt.date(2027, 4, 30)
    assert (snapshot.standard_unit_price_usd, snapshot.premium_unit_price_usd) == (25.0, 125.0)
    assert snapshot.org_credit_enabled is True
    assert snapshot.org_credit_limit_usd == 1500.0
    assert snapshot.collected_via == "browser"
    assert snapshot.source == "organization-2026-08-01.csv"


def test_organization_minimal_row_leaves_optional_columns_unknown(tmp_path, cfg):
    """必須列だけの表も読める。書かれていない列は不明（None）で、警告は出さない。"""
    _org_file(tmp_path, "2026-08-01", ["2026-08-01,manual"], header=ORG_MINIMAL_HEADER)
    result = load_admin_inputs(tmp_path, cfg)

    assert result.warnings == ()
    (snapshot,) = result.organization
    assert snapshot.standard_purchased is None
    assert snapshot.premium_purchased is None
    assert snapshot.billing_frequency is None
    assert snapshot.renewal_date is None
    assert snapshot.standard_unit_price_usd is None
    assert snapshot.premium_unit_price_usd is None
    assert snapshot.org_credit_enabled is None
    assert snapshot.org_credit_limit_usd is None
    assert snapshot.collected_via == "manual"


def test_organization_japanese_headers_are_mapped(tmp_path, cfg):
    """日本語ヘッダのエイリアスで正準名へ写す。"""
    _org_file(tmp_path, "2026-08-01", [
        "2026-08-01,4,28,annual,2027-03-31,25,125,無効,0,manual",
    ], header=ORG_JP_HEADER)
    (snapshot,) = load_admin_inputs(tmp_path, cfg).organization

    assert (snapshot.standard_purchased, snapshot.premium_purchased) == (4, 28)
    assert snapshot.billing_frequency == "annual"
    assert snapshot.org_credit_enabled is False
    assert snapshot.org_credit_limit_usd == 0.0


def test_blank_cells_are_unknown_without_warnings(tmp_path, cfg):
    """空欄は不明（None）。0 や False では埋めず、警告も出さない。"""
    _org_file(tmp_path, "2026-08-01", ["2026-08-01,,,,,,,,,manual"])
    result = load_admin_inputs(tmp_path, cfg)

    assert result.warnings == ()
    (snapshot,) = result.organization
    assert snapshot.standard_purchased is None
    assert snapshot.org_credit_enabled is None
    assert snapshot.org_credit_limit_usd is None


@pytest.mark.parametrize("row,column", [
    ("2026-08-01,seven,35,monthly,2027-04-30,25,125,true,1500,manual", "standard_purchased"),
    ("2026-08-01,7.5,35,monthly,2027-04-30,25,125,true,1500,manual", "standard_purchased"),
    ("2026-08-01,7,35,monthly,2027-04-30,cheap,125,true,1500,manual", "standard_unit_price_usd"),
    ("2026-08-01,7,35,monthly,2026-13-01,25,125,true,1500,manual", "renewal_date"),
    ("2026-08-01,7,35,monthly,2027-04-30,25,125,maybe,1500,manual", "org_credit_enabled"),
    ("2026-08-01,7,35,monthly,2027-04-30,25,125,true,たくさん,manual", "org_credit_limit_usd"),
])
def test_unreadable_cells_are_unknown_with_a_warning(tmp_path, cfg, row, column):
    """解釈できない値は不明にし、どの列かを警告に残す（写し間違いに気付けるように）。"""
    _org_file(tmp_path, "2026-08-01", [row])
    result = load_admin_inputs(tmp_path, cfg)

    assert getattr(result.organization[0], column) is None
    assert len(result.warnings) == 1
    assert "organization-2026-08-01.csv" in result.warnings[0]
    assert column in result.warnings[0]


@pytest.mark.parametrize("cell,expected", [
    ("7", 7),
    ('"1,024"', 1024),          # 桁区切り（CSV 上は引用符で囲む）
    ("7.0", 7),                 # 小数点つきでも整数を表すなら受ける
    ("1E+2", 100),              # 指数表記も同じ
    ("9007199254740993", 9007199254740993),   # float では別の値へ丸められる桁数
])
def test_counts_are_read_exactly(tmp_path, cfg, cell, expected):
    """個数は値を変えずに読む（float を経由した丸めで別の値にしない）。"""
    _org_file(tmp_path, "2026-08-01", [
        f"2026-08-01,{cell},35,monthly,2027-04-30,25,125,true,1500,manual",
    ])
    result = load_admin_inputs(tmp_path, cfg)

    assert result.warnings == ()
    assert result.organization[0].standard_purchased == expected


@pytest.mark.parametrize("cell", [
    "1_0", "1__0", "_10", "10_", "1e+_2",   # 数値変換が桁区切りとして無視する書き方
    "1E+99999",                             # 指数が字句として受ける範囲を超えている
])
@pytest.mark.parametrize("column", ["standard_purchased", "standard_unit_price_usd"])
def test_unaccepted_number_text_is_unknown_with_a_warning(tmp_path, cfg, cell, column):
    """数値として受ける字句から外れた値は読まない。

    個数（Decimal 経由）と金額（float 経由）の両方で同じ規則にする。
    """
    row = {
        "standard_purchased":
            f"2026-08-01,{cell},35,monthly,2027-04-30,25,125,true,1500,manual",
        "standard_unit_price_usd":
            f"2026-08-01,7,35,monthly,2027-04-30,{cell},125,true,1500,manual",
    }[column]
    _org_file(tmp_path, "2026-08-01", [row])
    result = load_admin_inputs(tmp_path, cfg)

    assert getattr(result.organization[0], column) is None
    assert len(result.warnings) == 1
    assert column in result.warnings[0]


@pytest.mark.parametrize("row,column", [
    ("2026-08-01,-1,35,monthly,2027-04-30,25,125,true,1500,manual", "standard_purchased"),
    ("2026-08-01,7,35,monthly,2027-04-30,-25,125,true,1500,manual", "standard_unit_price_usd"),
    ("2026-08-01,7,35,monthly,2027-04-30,25,125,true,-100,manual", "org_credit_limit_usd"),
])
def test_negative_values_are_unknown_with_a_warning(tmp_path, cfg, row, column):
    """負値は対応する状態が無いので不明にし、警告に残す。"""
    _org_file(tmp_path, "2026-08-01", [row])
    result = load_admin_inputs(tmp_path, cfg)

    assert getattr(result.organization[0], column) is None
    assert len(result.warnings) == 1
    assert column in result.warnings[0]


@pytest.mark.parametrize("row,column", [
    ("2026-08-01,7,35,monthly,2027-04-30,Infinity,125,true,1500,manual",
     "standard_unit_price_usd"),
    ("2026-08-01,7,35,monthly,2027-04-30,1e309,125,true,1500,manual",
     "standard_unit_price_usd"),
    ("2026-08-01,7,35,monthly,2027-04-30,25,125,true,Infinity,manual",
     "org_credit_limit_usd"),
    ("2026-08-01,7,35,monthly,2027-04-30,25,125,true,+inf,manual",
     "org_credit_limit_usd"),
])
def test_non_finite_numbers_are_unknown_with_a_warning(tmp_path, cfg, row, column):
    """数値として非有限になる値は不明にする（上限では「無制限」に化けさせない）。"""
    _org_file(tmp_path, "2026-08-01", [row])
    result = load_admin_inputs(tmp_path, cfg)

    assert getattr(result.organization[0], column) is None
    assert len(result.warnings) == 1
    assert column in result.warnings[0]


@pytest.mark.parametrize("row,column", [
    ("2026-08-01,7,35,monthly,2027-04-30,￥3000,125,true,1500,manual",
     "standard_unit_price_usd"),
    ("2026-08-01,7,35,monthly,2027-04-30,25,125,true,￥1500,manual",
     "org_credit_limit_usd"),
])
def test_yen_amounts_are_unknown_with_a_warning(tmp_path, cfg, row, column):
    """円記号つきの金額は USD として通さない（通貨の取り違えは桁が変わる）。"""
    _org_file(tmp_path, "2026-08-01", [row])
    result = load_admin_inputs(tmp_path, cfg)

    assert getattr(result.organization[0], column) is None
    assert len(result.warnings) == 1
    assert column in result.warnings[0]


@pytest.mark.parametrize("cell,expected", [
    ("無制限", math.inf),
    ("unlimited", math.inf),
    ("0", 0.0),
    ('"$2,500"', 2500.0),   # 桁区切りを含むセル（CSV 上は引用符で囲む）
    ("＄2500", 2500.0),     # 全角ドルも金額として読む
])
def test_org_credit_limit_tokens(tmp_path, cfg, cell, expected):
    """組織クレジット上限は「無制限=inf / 0=無効 / 通貨表記」を members-info と同じ規則で読む。"""
    _org_file(tmp_path, "2026-08-01", [
        f"2026-08-01,7,35,monthly,2027-04-30,25,125,true,{cell},manual",
    ])
    result = load_admin_inputs(tmp_path, cfg)

    assert result.warnings == ()
    assert result.organization[0].org_credit_limit_usd == expected


@pytest.mark.parametrize("rows", [[], [
    "2026-08-01,7,35,monthly,2027-04-30,25,125,true,1500,manual",
    "2026-08-01,8,36,monthly,2027-04-30,25,125,true,1500,manual",
]])
def test_organization_requires_exactly_one_row(tmp_path, cfg, rows):
    """organization は1ファイル1行（0行・2行以上は時点が特定できないため中止）。"""
    _org_file(tmp_path, "2026-08-01", rows)
    with pytest.raises(ValueError, match="1ファイル1行"):
        load_admin_inputs(tmp_path, cfg)


def test_organization_requires_source(tmp_path, cfg):
    """取得元が空の行は受けない（どこから写した値か分からない設定は使えない）。"""
    _org_file(tmp_path, "2026-08-01", ["2026-08-01,"], header=ORG_MINIMAL_HEADER)
    with pytest.raises(ValueError, match="source が空です"):
        load_admin_inputs(tmp_path, cfg)


# -------------------------------------------------------------------- users


def test_users_rows_are_read(tmp_path, cfg):
    """全列そろった users を読む（account_uuid は先頭ゼロを保つ）。"""
    _users_file(tmp_path, "2026-08-01", [
        "2026-08-01,User1@example.com,00123,true,$250,$18.75,browser",
        "2026-08-01,user2@example.com,,false,0,0,browser",
    ])
    result = load_admin_inputs(tmp_path, cfg)

    assert result.warnings == ()
    (snapshot,) = result.users
    assert snapshot.taken_on == dt.date(2026, 8, 1)
    assert snapshot.source == "users-2026-08-01.csv"
    first, second = snapshot.records
    assert first.email == "user1@example.com"
    assert first.account_uuid == "00123"
    assert first.credit_enabled is True
    assert first.credit_limit_usd == 250.0
    assert first.credit_mtd_usd == 18.75
    assert first.collected_via == "browser"
    assert second.email == "user2@example.com"
    assert second.account_uuid is None
    assert second.credit_enabled is False
    assert (second.credit_limit_usd, second.credit_mtd_usd) == (0.0, 0.0)


def test_users_minimal_rows_leave_credit_unknown(tmp_path, cfg):
    """必須列だけの表も読める（クレジット設定は不明のまま）。"""
    _users_file(tmp_path, "2026-08-01", [
        "2026-08-01,user1@example.com,manual",
    ], header=USERS_MINIMAL_HEADER)
    result = load_admin_inputs(tmp_path, cfg)

    assert result.warnings == ()
    (record,) = result.users[0].records
    assert record == _record("user1@example.com")


def test_users_japanese_headers_are_mapped(tmp_path, cfg):
    """日本語ヘッダのエイリアスで正準名へ写す。"""
    _users_file(tmp_path, "2026-08-01", [
        "2026-08-01,user1@example.com,,有効,無制限,12.5,manual",
    ], header=USERS_JP_HEADER)
    (record,) = load_admin_inputs(tmp_path, cfg).users[0].records

    assert record.credit_enabled is True
    assert record.credit_limit_usd == math.inf
    assert record.credit_mtd_usd == 12.5


@pytest.mark.parametrize("row,column", [
    ("2026-08-01,user1@example.com,,perhaps,250,0,manual", "credit_enabled"),
    ("2026-08-01,user1@example.com,,true,いくらか,0,manual", "credit_limit_usd"),
    ("2026-08-01,user1@example.com,,true,250,unknown,manual", "credit_mtd_usd"),
    ("2026-08-01,user1@example.com,,true,-1,0,manual", "credit_limit_usd"),
    ("2026-08-01,user1@example.com,,true,250,-5,manual", "credit_mtd_usd"),
])
def test_unreadable_user_cells_are_unknown_with_a_warning(tmp_path, cfg, row, column):
    """users 側の解釈できない値・負値も不明にし、email 付きで警告に残す。"""
    _users_file(tmp_path, "2026-08-01", [row])
    result = load_admin_inputs(tmp_path, cfg)

    (record,) = result.users[0].records
    assert getattr(record, column) is None
    assert len(result.warnings) == 1
    assert "user1@example.com" in result.warnings[0]
    assert column in result.warnings[0]


def test_users_without_data_rows_warns(tmp_path, cfg):
    """データ行が無い users は空で返し、置き忘れに気付けるよう警告する。"""
    _users_file(tmp_path, "2026-08-01", [])
    result = load_admin_inputs(tmp_path, cfg)

    (snapshot,) = result.users
    assert snapshot.records == ()
    assert result.warnings == ((
        "admin: users-2026-08-01.csv: データ行がありません"
        "（テンプレートのまま置かれている可能性があります）"
    ),)


def test_users_reject_empty_email(tmp_path, cfg):
    """email の無い行は受けない（誰の設定か決まらない）。"""
    _users_file(tmp_path, "2026-08-01", [
        "2026-08-01,user1@example.com,,true,250,0,manual",
        "2026-08-01, ,,true,250,0,manual",
    ])
    with pytest.raises(ValueError, match="2 行目の email が空です"):
        load_admin_inputs(tmp_path, cfg)


def test_users_reject_duplicate_email(tmp_path, cfg):
    """同じ email の行が複数あれば中止する（大小文字の違いも同じ email）。"""
    _users_file(tmp_path, "2026-08-01", [
        "2026-08-01,user1@example.com,,true,250,0,manual",
        "2026-08-01,User1@Example.com,,false,0,0,manual",
    ])
    with pytest.raises(ValueError, match="の行が複数あります"):
        load_admin_inputs(tmp_path, cfg)


def test_users_require_source_per_row(tmp_path, cfg):
    """取得元は行ごとに必須（部分的に埋めた表を黙って通さない）。"""
    _users_file(tmp_path, "2026-08-01", [
        "2026-08-01,user1@example.com,manual",
        "2026-08-01,user2@example.com,",
    ], header=USERS_MINIMAL_HEADER)
    with pytest.raises(ValueError, match="2 行目の source が空です"):
        load_admin_inputs(tmp_path, cfg)


# ----------------------------------------------------------------- ヘッダの曖昧さ


@pytest.mark.parametrize("header,canonical", [
    ("Snapshot Date,snapshot_date,Email,Source", "snapshot_date"),
    ("取得日,Snapshot Date,Email,Source", "snapshot_date"),
    ("Snapshot Date,Email,User Email,Source", "email"),
])
def test_two_headers_for_one_canonical_column_is_an_error(tmp_path, cfg, header, canonical):
    """同じ正準列へ写るヘッダが2つある表は中止する（片方が黙って捨てられないように）。"""
    _write(tmp_path, "users-2026-08-01.csv", header,
           ["2026-08-01,2026-08-01,user1@example.com,manual"])
    with pytest.raises(ValueError, match=f"同じ列 {canonical} に対応するヘッダが複数あります"):
        load_admin_inputs(tmp_path, cfg)


def test_two_headers_for_one_canonical_column_is_an_error_in_organization(tmp_path, cfg):
    """organization 側でも同じ検査が効く（エイリアスと正準名の共存も含む）。"""
    _write(tmp_path, "organization-2026-08-01.csv",
           "Snapshot Date,Standard Purchased,standard seats,Source",
           ["2026-08-01,7,7,manual"])
    with pytest.raises(ValueError, match="同じ列 standard_purchased"):
        load_admin_inputs(tmp_path, cfg)


def test_identical_duplicate_headers_are_an_error(tmp_path, cfg):
    """完全に同名のヘッダが2つある表も中止する。

    読み込みは2つ目を Email.1 へ改名するため、読み込み後の列名を見ても曖昧さが消えて
    しまう（先頭の列だけが黙って採用される）。検査は生のヘッダ行で行う。
    """
    _write(tmp_path, "users-2026-08-01.csv", "Snapshot Date,Email,Email,Source",
           ["2026-08-01,user1@example.com,user2@example.com,manual"])
    with pytest.raises(ValueError, match="同じ列 email に対応するヘッダが複数あります"):
        load_admin_inputs(tmp_path, cfg)


def test_headers_that_look_like_missing_values_are_still_compared(tmp_path, cfg):
    """`NA` のような欠損語彙のヘッダも列名として突き合わせる。

    先頭行はデータ行として読むため、欠損の語彙を素通しにすると列名が消え、曖昧さの
    検査から外れる。
    """
    broken = copy.deepcopy(cfg)
    broken["columns"]["admin_users"]["email"] = ["NA", "email"]
    _write(tmp_path, "users-2026-08-01.csv", "Snapshot Date,NA,Email,Source",
           ["2026-08-01,user1@example.com,user2@example.com,manual"])
    with pytest.raises(ValueError, match="同じ列 email に対応するヘッダが複数あります"):
        load_admin_inputs(tmp_path, broken)


def test_alias_shared_by_two_canonical_columns_is_an_error(tmp_path, cfg):
    """1つのヘッダが2つの正準列の候補になっている設定は、入力を読む前に中止する。

    写る先が定義の並び順で決まり、もう一方の正準列が黙って NA になるため。
    """
    broken = copy.deepcopy(cfg)
    broken["columns"]["admin_users"]["account_uuid"] = ["value"]
    broken["columns"]["admin_users"]["credit_enabled"] = ["value"]
    _users_file(tmp_path, "2026-08-01", ["2026-08-01,user1@example.com,manual"],
                header=USERS_MINIMAL_HEADER)
    with pytest.raises(ValueError, match="columns.admin_users: ヘッダ 'value' が"):
        load_admin_inputs(tmp_path, broken)


# ------------------------------------------------- ファイル名と取得日の突き合わせ


def test_same_date_within_one_kind_is_an_error(tmp_path, cfg):
    """同一種別・同一取得日のファイルが2つあれば中止する。"""
    _users_file(tmp_path, "2026-08-01", ["2026-08-01,user1@example.com,manual"],
                header=USERS_MINIMAL_HEADER)
    _users_file(tmp_path, "2026-08-01", ["2026-08-01,user2@example.com,manual"],
                header=USERS_MINIMAL_HEADER, name="users-copy-2026-08-01.csv")
    with pytest.raises(ValueError, match="取得日 2026-08-01 の CSV が複数あります"):
        load_admin_inputs(tmp_path, cfg)


def test_same_date_across_kinds_is_fine(tmp_path, cfg):
    """organization と users が同じ取得日なのは通常（別の表なので重複ではない）。"""
    _org_file(tmp_path, "2026-08-01", ["2026-08-01,manual"], header=ORG_MINIMAL_HEADER)
    _users_file(tmp_path, "2026-08-01", ["2026-08-01,user1@example.com,manual"],
                header=USERS_MINIMAL_HEADER)
    result = load_admin_inputs(tmp_path, cfg)

    assert result.warnings == ()
    assert len(result.organization) == 1
    assert len(result.users) == 1


@pytest.mark.parametrize("cell,message", [
    ("2026-07-01", "一致しません"),
    ("", "が空です"),
    ("2026/08/01", "解釈できません"),
])
def test_organization_snapshot_date_must_match_the_file_name(tmp_path, cfg, cell, message):
    """ファイル名の取得日と食い違う snapshot_date は取り違えとして中止する。"""
    _org_file(tmp_path, "2026-08-01", [f"{cell},manual"], header=ORG_MINIMAL_HEADER)
    with pytest.raises(ValueError, match=message):
        load_admin_inputs(tmp_path, cfg)


@pytest.mark.parametrize("cell", ["20260801", "2026-W31-5"])
def test_snapshot_date_must_be_written_as_yyyy_mm_dd(tmp_path, cfg, cell):
    """snapshot_date は YYYY-MM-DD だけを受ける（コンパクト形式・週日付は受けない）。

    ファイル名の取得日と同じ日を指す書き方でも、形式の揺れは写し間違いと区別できない。
    """
    _org_file(tmp_path, "2026-08-01", [f"{cell},manual"], header=ORG_MINIMAL_HEADER)
    with pytest.raises(ValueError, match="解釈できません"):
        load_admin_inputs(tmp_path, cfg)


@pytest.mark.parametrize("cell", ["20270430", "2027-W18-5"])
def test_other_dates_must_be_written_as_yyyy_mm_dd(tmp_path, cfg, cell):
    """snapshot_date 以外の日付も形式を限る（外れたら不明 + 警告）。"""
    _org_file(tmp_path, "2026-08-01", [
        f"2026-08-01,7,35,monthly,{cell},25,125,true,1500,manual",
    ])
    result = load_admin_inputs(tmp_path, cfg)

    assert result.organization[0].renewal_date is None
    assert len(result.warnings) == 1
    assert "renewal_date" in result.warnings[0]


def test_users_snapshot_date_mismatch_in_any_row_is_an_error(tmp_path, cfg):
    """users は1行でも取得日が食い違えば中止する。"""
    _users_file(tmp_path, "2026-08-01", [
        "2026-08-01,user1@example.com,manual",
        "2026-07-01,user2@example.com,manual",
    ], header=USERS_MINIMAL_HEADER)
    with pytest.raises(ValueError, match="2 行目の snapshot_date"):
        load_admin_inputs(tmp_path, cfg)


@pytest.mark.parametrize("name", [
    "users-2026-08-01-to-2026-08-31.csv",   # 期間（取得日が1点に決まらない）
    "users-2026-07-01-to-2026-08-31.csv",   # 月をまたぐ期間
    "users-2026-08.csv",                    # 月のみ（時点不明）
    "users-latest.csv",                     # 日付なし
])
def test_files_without_a_single_date_are_excluded_with_a_warning(tmp_path, cfg, name):
    """取得日を1点に決められないファイル名は除外し、除外した旨を警告に残す。"""
    _write(tmp_path, name, USERS_MINIMAL_HEADER, ["2026-08-01,user1@example.com,manual"])
    result = load_admin_inputs(tmp_path, cfg)

    assert result.users == ()
    assert result.warnings == ((
        f"admin: {name} はファイル名から取得日（YYYY-MM-DD の単日）を読み取れないため"
        "読み込みません"
    ),)


@pytest.mark.parametrize("name", [
    "users-2026-08-01-TO-2026-08-31.csv",    # 期間として解釈されない綴り（大文字）
    "users-2026-08-01-and-2026-08-31.csv",   # 期間を別の語でつないだ名前
    "users-2026-08-01-2026-08-31.csv",       # 日付を並べただけの名前
    "users-2026-08-01-and-1999-12-31.csv",   # 2つ目が対象外の年
    "users-2026-08-01-and-2026-13-01.csv",   # 2つ目が暦としてありえない月
    "users-2026-08-01-and-2026-8-31.csv",    # 2つ目の月が1桁
    "users-2026-8-01-to-2026-08-31.csv",     # 1つ目の月が1桁（末尾だけが日付に見える）
    "users-2026-08-1-to-2026-08-31.csv",     # 1つ目の日が1桁
    "users-12026-08-01.csv",                 # 年の桁が多い
    "users-2026-08-011.csv",                 # 日の桁が多い
    "users-2026-08-01-final.csv",            # 取得日が末尾でない
    "users-v2-2026-08-01.csv",               # 種別と取得日の間の語に数字がある
    "users-v２-2026-08-01.csv",              # 語の数字が全角
    "users-２０２６-０８-０１-to-2026-08-31.csv",   # 全角で書いた日付を語が飲み込む形
])
def test_non_canonical_file_names_are_excluded_with_a_warning(tmp_path, cfg, name):
    """取得日つきの正準な形でない名前は除外する。

    正準な形は「種別 + 数字を含まない任意の語 + 末尾の取得日」。取得日が末尾でない名前・
    桁を多く（少なく）書いた名前・日付を2つ並べた名前は、どの数字が取得日なのかが名前
    から決まらないので受けない。間の語に数字を許すと、期間の始まりや2つ目の日付を語と
    して飲み込み、末尾だけが取得日に見える名前が通る（数字は全角も含めて拒む）。
    """
    _write(tmp_path, name, USERS_MINIMAL_HEADER, ["2026-08-01,user1@example.com,manual"])
    result = load_admin_inputs(tmp_path, cfg)

    assert result.users == ()
    assert result.warnings == ((
        f"admin: {name} はファイル名から取得日（YYYY-MM-DD の単日）を読み取れないため"
        "読み込みません"
    ),)


@pytest.mark.parametrize("name", [
    "users-2026-08-01.csv",
    "organization-2026-08-01.csv",
    "users-copy-2026-08-01.csv",            # 接尾辞つき
    "users_2026_08_01.csv",                 # アンダースコア区切り
    "Users-2026-08-01.csv",                 # 大文字始まり
    "users-copy_2026-08-01.csv",            # 区切りの混在
    "users-manual-export-2026-08-01.csv",   # 語が複数（いずれも数字なし）
])
def test_canonical_file_names_are_read(tmp_path, cfg, name):
    """種別 + 数字を含まない任意の語 + 末尾の取得日、という形の名前は読み込む。"""
    _write(tmp_path, name, USERS_MINIMAL_HEADER, ["2026-08-01,user1@example.com,manual"])
    result = load_admin_inputs(tmp_path, cfg)

    assert result.warnings == ()
    snapshots = result.organization or result.users
    assert snapshots[0].taken_on == dt.date(2026, 8, 1)


def test_file_name_decision_uses_a_single_rule():
    """ファイル名の判定は構造規則1つに畳んである。

    以前は「日付の形をしたトークンを数える」規則と併用していた。数え上げを戻すと、
    どちらの規則がどこまでを保証するのかが再び分かれ、片方の穴が塞がったまま
    もう片方から入る状態に戻る。判定が参照する正規表現の数で固定する。
    """
    source = inspect.getsource(admin_inputs._is_canonical_name)

    assert sorted(set(re.findall(r"_[A-Z0-9_]+_RE", source))) == ["_NAMED_DATE_RE"]
    assert not hasattr(admin_inputs, "_DATE_SHAPED_RE")


def test_unknown_file_name_is_excluded_with_a_warning(tmp_path, cfg):
    """organization / users のどちらでもない CSV は除外し、警告に残す。"""
    _write(tmp_path, "credits-2026-08-01.csv", USERS_MINIMAL_HEADER,
           ["2026-08-01,user1@example.com,manual"])
    result = load_admin_inputs(tmp_path, cfg)

    assert (result.organization, result.users) == ((), ())
    assert result.warnings == ((
        "admin: credits-2026-08-01.csv は organization / users のどちらの CSV か"
        "判別できないため読み込みません"
    ),)


def test_cp932_csv_is_read(tmp_path, cfg):
    """cp932 で保存された CSV も読める（Excel から書き出した表を受けるため）。"""
    _write(tmp_path, "users-2026-08-01.csv", USERS_JP_HEADER,
           ["2026-08-01,user1@example.com,,有効,無制限,10,手入力"], encoding="cp932")
    (record,) = load_admin_inputs(tmp_path, cfg).users[0].records

    assert record.credit_enabled is True
    assert record.credit_limit_usd == math.inf
    assert record.collected_via == "手入力"


# ---------------------------------------------------------------------- 決定性


def test_snapshots_are_sorted_by_date_and_records_by_email(tmp_path, cfg):
    """スナップショットは取得日の昇順、records は email の昇順（入力順に依らない）。"""
    _users_file(tmp_path, "2026-08-15", [
        "2026-08-15,user3@example.com,manual",
        "2026-08-15,user1@example.com,manual",
    ], header=USERS_MINIMAL_HEADER)
    _users_file(tmp_path, "2026-08-01", [
        "2026-08-01,user2@example.com,manual",
    ], header=USERS_MINIMAL_HEADER)
    result = load_admin_inputs(tmp_path, cfg)

    assert [s.taken_on for s in result.users] == [dt.date(2026, 8, 1), dt.date(2026, 8, 15)]
    assert [r.email for r in result.users[1].records] == [
        "user1@example.com", "user3@example.com"]
    # 同じ入力からは常に同じ結果（並び・警告まで含めて等値になる）
    assert load_admin_inputs(tmp_path, cfg) == result


def test_all_snapshots_are_kept_for_within_month_trends(tmp_path, cfg):
    """同じ月の複数時点はすべて保持する（月内の当月消費の推移を見るため）。"""
    _users_file(tmp_path, "2026-08-08", [
        "2026-08-08,user1@example.com,,true,250,40,manual",
    ])
    _users_file(tmp_path, "2026-08-15", [
        "2026-08-15,user1@example.com,,true,250,90,manual",
    ])
    result = load_admin_inputs(tmp_path, cfg)

    assert [s.records[0].credit_mtd_usd for s in result.users] == [40.0, 90.0]


# ------------------------------------------------------------------- as-of 選択


def test_as_of_takes_the_latest_on_or_before_month_end():
    """対象月の月末以前で最新を採る（月末より後の設定変更を持ち込まない）。"""
    inputs = AdminInputs(
        organization=(_organization("2026-06-20", "a.csv"),
                      _organization("2026-07-15", "b.csv"),
                      _organization("2026-08-05", "c.csv")),
        users=(),
        warnings=(),
    )
    result = as_of(inputs, "2026-07")

    assert result.organization.source == "b.csv"
    assert result.users is None
    assert result.warnings == ()


def test_as_of_falls_back_to_the_oldest_with_a_strong_warning():
    """月末以前が1つも無ければ最古を採り、当時の設定と異なりうる旨を警告する。"""
    inputs = AdminInputs(
        organization=(),
        users=(_users("2026-09-01", "later.csv"), _users("2026-09-20", "latest.csv")),
        warnings=(),
    )
    result = as_of(inputs, "2026-07")

    assert result.users.source == "later.csv"
    assert result.warnings == ((
        "admin: 2026-07 月末以前のスナップショットが無いため最古の later.csv を使用。"
        "対象月当時の設定と異なる可能性があります"
    ),)


def test_as_of_returns_none_without_snapshots():
    """1つも無い種別は None（警告も出さない）。"""
    result = as_of(AdminInputs(organization=(), users=(), warnings=()), "2026-07")
    assert (result.organization, result.users, result.warnings) == (None, None, ())


def test_as_of_selects_each_kind_independently(tmp_path, cfg):
    """organization と users は別々に選ぶ（取得日が揃っていなくてよい）。"""
    _org_file(tmp_path, "2026-07-01", ["2026-07-01,manual"], header=ORG_MINIMAL_HEADER)
    _users_file(tmp_path, "2026-08-15", ["2026-08-15,user1@example.com,manual"],
                header=USERS_MINIMAL_HEADER)
    result = as_of(load_admin_inputs(tmp_path, cfg), "2026-07")

    assert result.organization.source == "organization-2026-07-01.csv"
    assert result.users.source == "users-2026-08-15.csv"
    assert len(result.warnings) == 1
    assert "users-2026-08-15.csv" in result.warnings[0]


def test_as_of_accepts_the_month_end_itself():
    """月末そのものの取得日は「月末以前」に含める。"""
    inputs = AdminInputs(
        organization=(_organization("2026-02-28", "leap.csv"),), users=(), warnings=())
    assert as_of(inputs, "2026-02").organization.source == "leap.csv"


@pytest.mark.parametrize("month", [
    "2026-13", "2026-7", "202607", "2026-07-01", "", None,
    "２０２６-07",   # 全角数字（\\d の既定は全角にも一致する）
    "2026-07\n",    # 末尾の改行（$ は改行の直前にも一致する）
    " 2026-07",
])
def test_as_of_rejects_a_malformed_month(month):
    """対象月は ASCII の YYYY-MM に限る（全角数字・前後の余分な文字を通さない）。"""
    inputs = AdminInputs(organization=(), users=(), warnings=())
    with pytest.raises(ValueError, match="YYYY-MM"):
        as_of(inputs, month)


# ------------------------------------------------------------------ 構築時の検証


@pytest.mark.parametrize("field,value", [
    ("standard_purchased", -1),
    ("premium_purchased", -1),
    ("standard_unit_price_usd", -25.0),
    ("org_credit_limit_usd", -1.0),
])
def test_organization_rejects_negative_values(field, value):
    """負の席数・金額に対応する状態は無いので構築時に拒否する。"""
    with pytest.raises(ValueError, match="負の値は指定できません"):
        _organization("2026-08-01").__class__(
            **{**vars(_organization("2026-08-01")), field: value})


@pytest.mark.parametrize("field", ["credit_limit_usd", "credit_mtd_usd"])
def test_user_record_rejects_negative_amounts(field):
    with pytest.raises(ValueError, match="負の値は指定できません"):
        _record("user1@example.com", **{field: -1.0})


def test_user_record_rejects_nan_amount():
    """NaN は比較が常に偽になり判定を黙って変えるため受けない。"""
    with pytest.raises(ValueError, match="有限でない数値"):
        _record("user1@example.com", credit_mtd_usd=math.nan)


def test_user_credit_limit_accepts_unlimited():
    """上限なし（inf）は追加クレジット上限の正当な値。"""
    assert _record("user1@example.com", credit_limit_usd=math.inf).credit_limit_usd == math.inf


def test_user_record_normalizes_email():
    """email は前後空白を除いて小文字へ揃えて保持する（突き合わせの鍵なので）。"""
    assert _record(" User1@Example.COM ").email == "user1@example.com"


@pytest.mark.parametrize("emails", [
    ("user2@example.com", "user1@example.com"),          # 非昇順
    ("user1@example.com", "user1@example.com"),          # 完全重複
    ("user1@example.com", "User1@Example.com"),          # 大小文字だけ違う重複
])
def test_records_must_be_sorted_and_unique(emails):
    """records の並びと一意性は構築時に確かめる（出力の決定性を型の側で保つ）。

    大小文字だけが違う email も同一人物として重複扱いにする（レコード側で正規化して
    いるため、この検査が表記の揺れで素通りしない）。
    """
    with pytest.raises(ValueError, match="email の昇順・重複なし"):
        UserCreditSnapshot(
            taken_on=dt.date(2026, 8, 1),
            records=tuple(_record(email) for email in emails),
            source="users.csv",
        )


def test_snapshots_must_be_sorted_by_date():
    """スナップショットの並びも構築時に確かめる（as-of が末尾を最新として引くため）。"""
    with pytest.raises(ValueError, match="取得日の昇順・重複なし"):
        AdminInputs(
            organization=(_organization("2026-08-05"), _organization("2026-07-01")),
            users=(),
            warnings=(),
        )
