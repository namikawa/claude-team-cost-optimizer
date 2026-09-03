"""生成物のファイル名（`{種別}-{YYYYMM}-{組織名}.{拡張子}`）と旧名の解決。

成果物は共有のためにフォルダの外へ出る。種別だけの名前（report.md 等）では、
どの組織のいつの分析かがファイル名から分からなくなるので、月と組織名を名前に含める。
月の部分はハイフン無しの YYYYMM で、ディレクトリ名（`<組織>/YYYY-MM/`）は変えない。

名前を組み立てる場所はここ1箇所にして、レポートの生成（`report/`）と考察の執筆
（`discussion`）が同じ規則を使う。

旧名で生成済みの成果物は自動で改名も削除もしない（意図的に残されたファイルを
ツールが動かさないため）。読み取りだけ「新名があればそちら、無ければ旧名」で解決する。
改名後の初回再生成で、旧名のファイルに書かれた手書きの考察を見失わないようにするため。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Artifact:
    """組織×月ディレクトリに置く生成物1種。"""

    stem: str    # 種別（report / usage-summary 等。ハイフンを含んでよい）
    suffix: str  # 拡張子（先頭のドットを含む）

    @property
    def legacy_name(self) -> str:
        """種別だけの旧名。読み取りのフォールバック先にのみ使う。"""
        return f"{self.stem}{self.suffix}"

    def name(self, month: str, org: str) -> str:
        """`{種別}-{YYYYMM}-{組織名}.{拡張子}`。"""
        return f"{self.stem}-{month.replace('-', '')}-{org}{self.suffix}"

    def path(self, org_output: Path | str, month: str, org: str) -> Path:
        """書き込み先。常に新名を返す。"""
        return Path(org_output) / month / self.name(month, org)

    def legacy_path(self, org_output: Path | str, month: str) -> Path:
        return Path(org_output) / month / self.legacy_name

    def existing_path(self, org_output: Path | str, month: str, org: str) -> Path:
        """読み取り先。新名を優先し、無ければ旧名へフォールバックする。

        どちらも無ければ新名を返す（「無い」ことの扱いは呼び出し側に任せる）。
        """
        path = self.path(org_output, month, org)
        if path.exists():
            return path
        legacy = self.legacy_path(org_output, month)
        return legacy if legacy.exists() else path

    def legacy_sibling(self, path: Path, month: str, org: str) -> Path | None:
        """path が正規の名前のときだけ、同じディレクトリの旧名を返す。

        書き出しの関数（`write_markdown` 等）は任意のパスを受け取る公開 API なので、
        旧名への読み替えを無条件にすると、別名で出力したときに同じディレクトリの
        `report.md` の内容を引き込んでしまう。正規の出力先のときだけ後方互換を効かせる。
        """
        return path.parent / self.legacy_name if path.name == self.name(month, org) else None


# 正式分析の成果物
REPORT = Artifact("report", ".md")
DETAILS = Artifact("details", ".md")
DASHBOARD = Artifact("dashboard", ".html")
RECOMMENDATIONS = Artifact("recommendations", ".csv")
USAGE_SUMMARY = Artifact("usage-summary", ".csv")
# V2 判定の根拠（--decision-version v2 のときだけ書く）
DECISION_EVIDENCE = Artifact("decision-evidence", ".csv")
# 速報モードの成果物
PREVIEW = Artifact("preview", ".md")
PREVIEW_DASHBOARD = Artifact("preview-dashboard", ".html")
