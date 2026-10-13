"""마크다운 보고서를 PDF로 변환한다 (제출용 산출물).

markdown(표 확장) → HTML → PyMuPDF Story 로 A4 페이지에 배치한다.
PyMuPDF에 한글 대체 글꼴이 내장돼 있어 별도 폰트 설치 없이 macOS·Linux·Windows에서 동작한다.
"""

from pathlib import Path

import markdown
import pymupdf

PAGE = pymupdf.paper_rect("a4")
MARGIN = 48  # pt
BODY = PAGE + (MARGIN, MARGIN, -MARGIN, -MARGIN - 12)  # 하단은 쪽번호 자리

CSS = """
body { font-family: sans-serif; font-size: 9.5pt; line-height: 1.5; color: #222; }
h1 { font-size: 17pt; margin: 0 0 10pt 0; padding-bottom: 4pt; border-bottom: 1.5px solid #333; }
h2 { font-size: 13pt; margin: 14pt 0 5pt 0; color: #1a3d6d; }
h3 { font-size: 11pt; margin: 10pt 0 4pt 0; }
p, li { margin: 0 0 5pt 0; }
blockquote { margin: 6pt 0; padding: 5pt 8pt; background-color: #fff4e5; border-left: 3px solid #e08a00; }
table { border-collapse: collapse; margin: 5pt 0 8pt 0; font-size: 8pt; }
th, td { border: 0.6px solid #999; padding: 2pt 4pt; }
th { background-color: #eef1f5; }
code { font-family: monospace; font-size: 8.5pt; }
"""


def markdown_to_pdf(md_text: str, pdf_path: Path) -> Path:
    html = markdown.markdown(md_text, extensions=["tables", "sane_lists"])
    story = pymupdf.Story(html=html, user_css=CSS)
    writer = pymupdf.DocumentWriter(str(pdf_path))
    more = True
    while more:
        device = writer.begin_page(PAGE)
        more, _ = story.place(BODY)
        story.draw(device)
        writer.end_page()
    writer.close()

    # 쪽번호 (n / 전체) + 글꼴 서브셋(쓴 글자만 남겨 용량 축소)
    doc = pymupdf.open(str(pdf_path))
    total = doc.page_count
    for i, page in enumerate(doc, start=1):
        label = f"{i} / {total}"
        page.insert_text((PAGE.width / 2 - len(label) * 2.2, PAGE.height - MARGIN / 2),
                         label, fontsize=8, color=(0.45, 0.45, 0.45))
    doc.subset_fonts()
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    pdf_path.write_bytes(data)
    return pdf_path
