"""리포트 저장: Markdown(.md) + PDF(.pdf).

PDF는 Markdown → HTML(markdown-it-py) → Chromium 인쇄(Playwright)로 만든다.
브라우저 렌더링이라 한글 폰트·표가 화면과 같게 나온다. PDF 생성이 실패해도 .md 저장은 유지한다.
"""

import html
import logging
from datetime import datetime
from pathlib import Path

from markdown_it import MarkdownIt

from .observability import OUTPUT_DIR, log_decision

REPORT_DIR = OUTPUT_DIR / "reports"

log = logging.getLogger("finance_agent")
_md = MarkdownIt("commonmark", {"linkify": False}).enable("table")

CSS = """
@page { size: A4; margin: 16mm 14mm 18mm; }
body { font-family: "Apple SD Gothic Neo", "Noto Sans KR", "Malgun Gothic", sans-serif;
       font-size: 10.5pt; line-height: 1.6; color: #1f2328; }
h1 { font-size: 19pt; margin: 0 0 4mm; padding-bottom: 2mm; border-bottom: 2px solid #1f2328; }
h2 { font-size: 13.5pt; margin: 7mm 0 2mm; padding-bottom: 1mm; border-bottom: 1px solid #d0d7de; }
h3 { font-size: 11.5pt; margin: 5mm 0 2mm; }
p, li { margin: 1mm 0; }
ul { padding-left: 5mm; }
table { border-collapse: collapse; width: 100%; margin: 2mm 0 4mm; font-size: 9pt; page-break-inside: auto; }
tr { page-break-inside: avoid; }
th, td { border: 1px solid #d0d7de; padding: 1.2mm 2mm; }
th { background: #f6f8fa; }
td:not(:first-child) { text-align: right; font-variant-numeric: tabular-nums; }
a { color: #0969da; text-decoration: none; word-break: break-all; }
blockquote { margin: 3mm 0; padding: 2mm 4mm; border-left: 3px solid #d4a72c; background: #fff8c5; }
code { font-size: 9pt; }
"""

FOOTER = ('<div style="width:100%;font-size:8px;color:#888;padding:0 14mm;display:flex;justify-content:space-between;">'
          '<span>{title}</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>')


def _title(report: str) -> str:
    return next((line[2:].strip() for line in report.splitlines() if line.startswith("# ")), "재무 분석 리포트")


def to_html(report: str) -> str:
    title = html.escape(_title(report))
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{title}</title>'
            f"<style>{CSS}</style></head><body>{_md.render(report)}</body></html>")


def to_pdf(report: str, path: Path) -> bool:
    """PDF를 저장하고 성공 여부를 반환한다."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception:  # noqa: BLE001 - 번들 Chromium이 없으면 설치된 Chrome 사용
                browser = p.chromium.launch(channel="chrome")
            page = browser.new_page()
            page.set_content(to_html(report), wait_until="load")
            page.pdf(path=str(path), format="A4", print_background=True, display_header_footer=True,
                     header_template="<span></span>",
                     footer_template=FOOTER.format(title=html.escape(_title(report))),
                     margin={"top": "16mm", "bottom": "18mm", "left": "14mm", "right": "14mm"})
            browser.close()
        return True
    except Exception:  # noqa: BLE001 - PDF 실패가 리포트 저장을 막지 않게 한다
        log.exception("PDF 생성 실패: %s", path.name)
        return False


def save_report(report: str, trace_id: str) -> tuple[Path, Path | None]:
    """(md 경로, pdf 경로 | None). 같은 이름(report_YYYYmmdd_HHMMSS)으로 두 파일을 저장한다."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = REPORT_DIR / f"report_{datetime.now():%Y%m%d_%H%M%S}.md"
    md_path.write_text(report, encoding="utf-8")
    pdf_path = md_path.with_suffix(".pdf")
    pdf_ok = to_pdf(report, pdf_path)
    log_decision(trace_id, "output", "saved", path=md_path.name, pdf=pdf_path.name if pdf_ok else None)
    return md_path, pdf_path if pdf_ok else None
