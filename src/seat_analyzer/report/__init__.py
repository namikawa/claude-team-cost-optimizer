"""レポート生成: report.md / details.md / dashboard.html / recommendations.csv / usage-summary.csv"""

from __future__ import annotations

from pathlib import Path

from ..analyze import (
    AnalysisResult,
    PreviewResult,
)
from .csv_out import write_csv
from .details import write_details
from .document import discussion_body, document_body, write_discussion
from .html import write_html, write_preview_html
from .markdown import write_markdown, write_org_summary, write_preview_markdown
from .usage_csv import write_usage_csv

# 公開 API。write_preview_markdown は write_preview の内部実装なので含めない。
__all__ = [
    "write_all",
    "write_preview",
    "write_markdown",
    "write_details",
    "write_html",
    "write_preview_html",
    "write_csv",
    "write_usage_csv",
    "write_org_summary",
    "write_discussion",
    "document_body",
    "discussion_body",
]


def write_all(result: AnalysisResult, output_dir: str | Path) -> dict[str, Path]:
    out = Path(output_dir) / result.month
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "csv": out / "recommendations.csv",
        "usage": out / "usage-summary.csv",
        "markdown": out / "report.md",
        "details": out / "details.md",
        "html": out / "dashboard.html",
    }
    write_csv(result, paths["csv"])
    write_usage_csv(result, paths["usage"])
    write_markdown(result, paths["markdown"])
    write_details(result, paths["details"])
    write_html(result, paths["html"])
    return paths


def write_preview(result: PreviewResult, output_dir: str | Path) -> dict[str, Path]:
    """速報モードの出力（reports/<組織>/<月>/preview.md と preview-dashboard.html）。

    正式レポート（report.md / details.md / dashboard.html / recommendations.csv）には触れない。
    戻り値は正式側 write_all と同様の paths dict（keys: "markdown", "html"）。
    """
    out = Path(output_dir) / result.month
    out.mkdir(parents=True, exist_ok=True)
    path = out / "preview.md"
    write_preview_markdown(result, path)

    html_path = out / "preview-dashboard.html"
    write_preview_html(result, html_path)
    return {"markdown": path, "html": html_path}
