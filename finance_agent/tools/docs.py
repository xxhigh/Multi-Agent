"""문서 풀(data/docs) RAG — 산업 리포트·기업 자료 PDF와 수집 메모(.md) 검색.

- industry/            : 로보틱스·Physical AI 산업·시장 자료
- companies/<회사명>/  : 기업별 IR·제품 자료·기사
PDF는 페이지 단위로 텍스트를 뽑고(PyMuPDF), .md는 YAML 머리말(title/source/url/published)을 출처로 쓴다.
파일 목록·수정시각이 바뀌면 컬렉션 이름(해시)이 바뀌어 자동으로 다시 인덱싱한다.
"""

import hashlib
import re
import unicodedata
from pathlib import Path

from .. import config
from . import rag

_FRONT = re.compile(r"^---\n(.*?)\n---\n", re.S)


def _front_matter(text: str) -> tuple[dict, str]:
    m = _FRONT.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, text[m.end():]


def _pdf_pages(path: Path) -> list[tuple[int, str]]:
    import pymupdf  # PDF 처리 시에만 필요

    with pymupdf.open(str(path)) as doc:
        return [(i + 1, page.get_text()) for i, page in enumerate(doc)]


def list_files(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.rglob("*") if p.suffix.lower() in (".pdf", ".md") and p.name != "README.md")


def _signature(files: list[Path]) -> str:
    h = hashlib.sha1()
    for p in files:
        h.update(f"{p.relative_to(config.DOCS_DIR)}:{p.stat().st_mtime_ns}".encode())
    return h.hexdigest()[:12]


def _build(files: list[Path]) -> list[tuple[str, dict]]:
    items: list[tuple[str, dict]] = []
    for path in files:
        rel = str(path.relative_to(config.DOCS_DIR))
        if path.suffix.lower() == ".pdf":
            for page, text in _pdf_pages(path):
                if len(text.strip()) < 30:
                    continue
                meta = {"file": rel, "title": path.stem, "source": "PDF", "url": "", "published": "", "page": page}
                items += rag.chunk_sections([(f"{path.stem} p.{page}", text)], meta)
        else:
            front, body = _front_matter(path.read_text(encoding="utf-8"))
            meta = {"file": rel, "title": front.get("title", path.stem), "source": front.get("source", ""),
                    "url": front.get("url", ""), "published": front.get("published", ""), "page": 0}
            items += rag.chunk_sections([(meta["title"], body)], meta)
    return items


def company_folder(company: str) -> Path | None:
    """DART 회사명과 폴더명을 공백·'(주)' 차이 없이 맞춘다."""
    base = config.DOCS_DIR / "companies"
    if not base.exists():
        return None
    def norm(s: str) -> str:  # macOS 파일명(NFD)과 DART 회사명(NFC) 차이도 흡수
        return re.sub(r"\s|\(주\)|주식회사", "", unicodedata.normalize("NFC", s)).lower()

    for d in base.iterdir():
        if d.is_dir() and norm(d.name) == norm(company):
            return d
    return None


def search(folder: Path, label: str, query: str, k: int = 5) -> str:
    files = list_files(folder)
    if not files:
        return f"오류: 문서 풀 '{label}'에 자료가 없습니다 ({folder.relative_to(config.ROOT_DIR)})."
    hits = rag.hybrid_search(f"docs-{_signature(files)}", lambda: _build(files), query, k)
    lines = [f"[문서 풀 검색: {label} / '{query}']"]
    sources = {}
    for i, (score, doc, meta) in enumerate(hits, 1):
        where = f" p.{meta['page']}" if meta.get("page") else ""
        lines.append(f"\n--- 결과 {i} (점수 {score:.2f}) · {meta['title']}{where} ---\n{doc}")
        sources[meta["file"]] = meta
    lines.append("\n[출처]")
    for meta in sources.values():
        pub = f", {meta['published']}" if meta.get("published") else ""
        url = f" — {meta['url']}" if meta.get("url") else ""
        lines.append(f"- {meta['source'] or '문서'} · {meta['title']}{pub}{url}")
    return "\n".join(lines)
