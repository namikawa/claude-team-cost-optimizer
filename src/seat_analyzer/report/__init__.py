"""レポート生成: report / details / dashboard / recommendations / usage-summary

ファイル名は `{種別}-{YYYYMM}-{組織名}.{拡張子}`（組み立ては naming.py が持つ）。

V2 判定の根拠（decision-evidence）は上の5種とは別で、`--decision-version v2` の
ときだけ書く。write_all の成果物には含めない。
"""

from __future__ import annotations

from pathlib import Path

from ..analyze import (
    AnalysisResult,
    PreviewResult,
)
from .csv_out import write_csv
from .details import write_details
from .document import WriteResult, discussion_body, document_body, write_discussion
from .evidence_csv import EVIDENCE_COLUMNS, write_decision_evidence
from .html import write_html, write_preview_html
from .markdown import write_markdown, write_org_summary, write_preview_markdown
from .naming import (
    DASHBOARD,
    DECISION_EVIDENCE,
    DETAILS,
    PREVIEW,
    PREVIEW_DASHBOARD,
    RECOMMENDATIONS,
    REPORT,
    USAGE_SUMMARY,
    Artifact,
)
from .usage_csv import write_usage_csv

# 公開 API。write_preview_markdown は write_preview の内部実装なので含めない。
# 並びは役割順（オーケストレーション → 出力形式ごとの writer → 考察 → 成果物名）。
# 辞書順に並べ替えるとこの対応が読めなくなるため RUF022 は抑制する。
__all__ = [  # noqa: RUF022
    "write_all",
    "write_preview",
    "write_markdown",
    "write_details",
    "write_html",
    "write_preview_html",
    "write_csv",
    "write_usage_csv",
    "write_decision_evidence",
    "EVIDENCE_COLUMNS",
    "write_org_summary",
    "write_discussion",
    "WriteResult",
    "document_body",
    "discussion_body",
    "Artifact",
    "REPORT",
    "DETAILS",
    "DASHBOARD",
    "RECOMMENDATIONS",
    "USAGE_SUMMARY",
    "DECISION_EVIDENCE",
    "PREVIEW",
    "PREVIEW_DASHBOARD",
]


def write_all(result: AnalysisResult, output_dir: str | Path) -> dict[str, Path]:
    month, org = result.month, result.org
    (Path(output_dir) / month).mkdir(parents=True, exist_ok=True)
    paths = {
        "csv": RECOMMENDATIONS.path(output_dir, month, org),
        "usage": USAGE_SUMMARY.path(output_dir, month, org),
        "markdown": REPORT.path(output_dir, month, org),
        "details": DETAILS.path(output_dir, month, org),
        "html": DASHBOARD.path(output_dir, month, org),
    }
    write_csv(result, paths["csv"])
    write_usage_csv(result, paths["usage"])
    write_markdown(result, paths["markdown"])
    write_details(result, paths["details"])
    write_html(result, paths["html"])
    return paths


def write_preview(result: PreviewResult, output_dir: str | Path) -> dict[str, Path]:
    """速報モードの出力（reports/<組織>/<月>/ の preview と preview-dashboard）。

    正式レポート（report / details / dashboard / recommendations）には触れない。
    戻り値は正式側 write_all と同様の paths dict（keys: "markdown", "html"）。
    """
    month, org = result.month, result.org
    (Path(output_dir) / month).mkdir(parents=True, exist_ok=True)
    path = PREVIEW.path(output_dir, month, org)
    write_preview_markdown(result, path)

    html_path = PREVIEW_DASHBOARD.path(output_dir, month, org)
    write_preview_html(result, html_path)
    return {"markdown": path, "html": html_path}
