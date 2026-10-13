"""마크다운 보고서 → 제출용 PDF (A4, 표지 + 머리말·꼬리말 + 쪽번호).

markdown(표 확장) → HTML → PyMuPDF Story 로 배치한다.
PyMuPDF에 한글 대체 글꼴이 내장돼 있어 별도 폰트 설치 없이 macOS·Linux·Windows에서 동작한다.
"""

import re
from datetime import date
from pathlib import Path

import markdown
import pymupdf

PAGE = pymupdf.paper_rect("a4")
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 52, 64, 58
BODY = pymupdf.Rect(MARGIN_X, MARGIN_TOP, PAGE.width - MARGIN_X, PAGE.height - MARGIN_BOTTOM)
NAVY, ACCENT = "#14284b", "#2f6fdf"

CSS = f"""
body {{ font-family: sans-serif; font-size: 9.2pt; line-height: 1.55; color: #1f2430; }}
h2 {{ font-size: 12.5pt; color: #ffffff; background-color: {NAVY}; padding: 4pt 8pt; margin: 16pt 0 8pt 0; }}
h3 {{ font-size: 10.5pt; color: {NAVY}; border-bottom: 0.8px solid #c9d3e3; padding-bottom: 2pt; margin: 11pt 0 5pt 0; }}
p {{ margin: 0 0 5pt 0; text-align: justify; }}
li {{ margin: 0 0 3pt 0; }}
ul {{ margin: 0 0 6pt 0; }}
strong {{ color: {NAVY}; }}
blockquote {{ margin: 6pt 0 8pt 0; padding: 5pt 9pt; background-color: #f2f5fa; border-left: 3px solid {ACCENT};
              font-size: 8.4pt; color: #3d4657; }}
table {{ border-collapse: collapse; margin: 4pt 0 8pt 0; font-size: 8pt; }}
th {{ color: {NAVY}; font-weight: bold; border: 0.6px solid #b8c4d6; border-bottom: 1.4px solid {NAVY}; padding: 3pt 4pt; }}
td {{ border: 0.6px solid #cfd7e3; padding: 2.5pt 4pt; }}
code {{ font-family: monospace; font-size: 8pt; }}
.ref {{ font-size: 7.6pt; line-height: 1.4; color: #3d4657; }}
.disclaimer {{ font-size: 7.6pt; color: #6b7385; border-top: 0.6px solid #c9d3e3; padding-top: 4pt; margin-top: 10pt; }}
"""

COVER_CSS = f"""
body {{ font-family: sans-serif; color: #1f2430; }}
.label {{ font-size: 9pt; color: {ACCENT}; font-weight: bold; letter-spacing: 1pt; }}
.title {{ font-size: 26pt; font-weight: bold; color: {NAVY}; line-height: 1.25; margin: 8pt 0 6pt 0; }}
.subtitle {{ font-size: 11pt; color: #4a5468; margin-bottom: 24pt; }}
.row {{ font-size: 9.5pt; padding: 6pt 2pt; border-bottom: 0.6px solid #d5dce8; }}
.k {{ color: {ACCENT}; font-weight: bold; }}
.warn {{ margin-top: 18pt; padding: 8pt 10pt; background-color: #fff4e5; border-left: 3px solid #e08a00; font-size: 8.6pt; }}
.note {{ margin-top: 26pt; font-size: 8pt; color: #6b7385; line-height: 1.5; }}
"""


def _split_title(md_text: str) -> tuple[str, str]:
    m = re.search(r"^#\s+(.+)$", md_text, re.M)
    if not m:
        return "투자 가능성 평가 보고서", md_text
    return m.group(1).strip(), md_text[:m.start()] + md_text[m.end():]


def _soft_wrap_urls(html: str) -> str:
    """긴 URL이 줄을 넘지 않도록 '/'·'?'·'=' 뒤에 줄바꿈 가능 지점을 넣는다."""
    return re.sub(r"(https?://[^\s<]+)", lambda m: re.sub(r"([/?=&])", "\\1\u200b", m.group(1)), html)


def _body_html(md_body: str) -> tuple[str, str | None]:
    warning = None
    m = re.match(r"\s*>\s*⚠️\s*품질 경고:\s*(.+)", md_body)
    if m:
        warning = m.group(1).strip()
        md_body = md_body[m.end():]
    html = markdown.markdown(md_body, extensions=["tables", "sane_lists"])
    # REFERENCE 이후는 작은 글씨로, 마지막 면책 문단은 별도 스타일로
    parts = re.split(r"(<h2>REFERENCE</h2>)", html, maxsplit=1)
    if len(parts) == 3:
        tail = parts[2]
        disc = re.search(r"<p>([^<]*투자 권유가 아닙니다[^<]*)</p>\s*$", tail)
        if disc:
            tail = tail[:disc.start()] + f'<p class="disclaimer">{disc.group(1)}</p>'
        html = parts[0] + parts[1] + f'<div class="ref">{tail}</div>'
    return _soft_wrap_urls(html), warning


def _cover_html(title: str, warning: str | None) -> str:
    company = re.sub(r"\s*투자 가능성 평가 보고서\s*$", "", title) or title
    rows = [
        ("평가 대상", company),
        ("평가 일자", date.today().strftime("%Y년 %m월 %d일")),
        ("보고서 유형", "Physical AI·로보틱스 스타트업 투자 가능성 평가"),
        ("작성 방식", "LangGraph Supervisor 멀티 에이전트 (근거 충분성 판정 → 작성 → 품질 평가 루프)"),
        ("데이터 출처", "OpenDART 기업개황·공시·감사보고서, 산업 리포트, 기업 공개 자료"),
        ("평가 체계", "창업자 5 · 시장성 30 · 제품·기술력 25 · 경쟁 우위 20 · 실적 10 · 투자조건 10"),
    ]
    table = "".join(f'<div class="row"><span class="k">{k}</span>&#160;&#160;&#160;{v}</div>' for k, v in rows)
    warn = f'<div class="warn"><b>품질 경고</b> — {warning}</div>' if warning else ""
    return (f'<div class="label">INVESTMENT REVIEW REPORT</div>'
            f'<div class="title">{company}<br/>투자 가능성 평가 보고서</div>'
            f'<div class="subtitle">SUMMARY · COMPANY &amp; TEAM · TECHNOLOGY · MARKET · INVESTMENT ASSESSMENT &amp; RISKS</div>'
            f"{table}{warn}"
            '<div class="note">본 보고서는 공개 자료만으로 작성된 평가 참고 자료이며 투자 권유가 아닙니다. '
            "모든 수치에는 본문 [E#] 인용과 REFERENCE의 원문 링크가 연결되어 있습니다.</div>")


def _place(writer, story, first_rect=None):
    more = True
    first = True
    while more:
        device = writer.begin_page(PAGE)
        more, _ = story.place(first_rect if first and first_rect is not None else BODY)
        story.draw(device)
        writer.end_page()
        first = False


def _decorate(doc, title: str) -> None:
    """표지 띠, 본문 머리말(보고서 제목)·꼬리말(쪽번호) — 표지는 쪽번호 없음."""
    navy = pymupdf.pdfcolor["navy"] if "navy" in pymupdf.pdfcolor else (0.08, 0.16, 0.29)
    cover = doc[0]
    cover.draw_rect(pymupdf.Rect(0, 0, PAGE.width, 14), color=None, fill=(0.08, 0.16, 0.29))
    cover.draw_rect(pymupdf.Rect(0, PAGE.height - 8, PAGE.width, PAGE.height), color=None, fill=(0.18, 0.44, 0.87))
    total = doc.page_count - 1
    for i in range(1, doc.page_count):
        page = doc[i]
        y = MARGIN_TOP - 20
        page.insert_htmlbox(pymupdf.Rect(MARGIN_X, y - 12, PAGE.width - MARGIN_X - 90, y + 2),
                            f'<span style="font-family:sans-serif;font-size:7.5pt;color:#6b7385">{title}</span>')
        page.insert_htmlbox(pymupdf.Rect(PAGE.width - MARGIN_X - 90, y - 12, PAGE.width - MARGIN_X, y + 2),
                            '<div style="font-family:sans-serif;font-size:7.5pt;color:#6b7385;text-align:right">INVESTMENT REVIEW</div>')
        page.draw_line(pymupdf.Point(MARGIN_X, y + 4), pymupdf.Point(PAGE.width - MARGIN_X, y + 4),
                       color=navy, width=0.6)
        fy = PAGE.height - MARGIN_BOTTOM + 22
        page.draw_line(pymupdf.Point(MARGIN_X, fy - 8), pymupdf.Point(PAGE.width - MARGIN_X, fy - 8),
                       color=(0.8, 0.84, 0.9), width=0.4)
        page.insert_htmlbox(pymupdf.Rect(MARGIN_X, fy - 4, PAGE.width - MARGIN_X, fy + 10),
                            f'<div style="font-family:sans-serif;font-size:7.5pt;color:#6b7385;text-align:right">{i} / {total}</div>')
        page.insert_htmlbox(pymupdf.Rect(MARGIN_X, fy - 4, PAGE.width - MARGIN_X - 60, fy + 10),
                            '<span style="font-family:sans-serif;font-size:7pt;color:#9aa1b0">공개 자료 기반 평가 참고 자료 · 투자 권유 아님</span>')


def markdown_to_pdf(md_text: str, pdf_path: Path) -> Path:
    title, body = _split_title(md_text)
    body_html, warning = _body_html(body)
    writer = pymupdf.DocumentWriter(str(pdf_path))
    _place(writer, pymupdf.Story(html=_cover_html(title, warning), user_css=COVER_CSS),
           first_rect=pymupdf.Rect(MARGIN_X + 6, 150, PAGE.width - MARGIN_X - 6, PAGE.height - 80))
    _place(writer, pymupdf.Story(html=body_html, user_css=CSS))
    writer.close()

    doc = pymupdf.open(str(pdf_path))
    _decorate(doc, title)
    doc.subset_fonts()
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    pdf_path.write_bytes(data)
    return pdf_path
