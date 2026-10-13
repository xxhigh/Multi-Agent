"""Markdown 보고서를 PDF로 만든다.

Markdown → HTML(markdown 패키지) → PDF(PyMuPDF Story). 한글은 PyMuPDF에 들어 있는 CJK 글꼴로 그리므로
시스템에 글꼴을 따로 설치하지 않아도 된다. 글꼴은 쓰인 글자만 남겨(subset) 파일 크기를 줄인다.
"""

import io
from pathlib import Path

import markdown
import pymupdf

PAGE = pymupdf.paper_rect("a4")
MARGIN = 48  # pt

CSS = """
body { font-family: sans-serif; font-size: 9.5pt; line-height: 1.5; color: #1a1a1a; overflow-wrap: break-word; }
h1 { font-size: 18pt; margin: 0 0 10pt 0; }
h2 { font-size: 13pt; margin: 16pt 0 6pt 0; border-bottom: 1px solid #999; }
h3 { font-size: 10.5pt; margin: 10pt 0 4pt 0; }
p { margin: 0 0 6pt 0; }
ul, ol { margin: 0 0 6pt 0; padding-left: 14pt; }
li { margin-bottom: 3pt; }
table { border-collapse: collapse; margin: 6pt 0 8pt 0; font-size: 8pt; }
th, td { border: 1px solid #888; padding: 3pt 4pt; }
th { background-color: #e8e8e8; }
a { color: #1a1a1a; text-decoration: none; }
"""


def _render(text: str) -> pymupdf.Document:
    """Markdown 텍스트를 A4 쪽으로 조판한다."""
    html = markdown.markdown(text, extensions=["tables", "sane_lists"])
    story = pymupdf.Story(html=html, user_css=CSS)
    buffer = io.BytesIO()
    writer = pymupdf.DocumentWriter(buffer)
    content = PAGE + (MARGIN, MARGIN, -MARGIN, -MARGIN)
    more = True
    while more:  # 내용이 남아 있는 동안 쪽을 추가한다
        device = writer.begin_page(PAGE)
        more, _ = story.place(content)
        story.draw(device)
        writer.end_page()
    writer.close()
    return pymupdf.open("pdf", buffer.getvalue())


def page_count(text: str) -> int:
    """PDF로 만들었을 때의 쪽수."""
    return len(_render(text))


def to_pdf(text: str, path: Path) -> Path:
    """Markdown 텍스트를 A4 PDF로 저장한다."""
    doc = _render(text)
    doc.subset_fonts()  # CJK 글꼴 전체(약 4MB)를 넣지 않고 쓰인 글자만 남긴다
    doc.save(str(path), garbage=4, deflate=True)
    return path
