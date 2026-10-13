"""문서 풀 RAG.

data/docs 의 PDF(Physical AI · 로보틱스 산업·기술 보고서)를 FAISS 인덱스로 만들어 검색한다.
- 인덱스: data/vectorstores/BAAI--bge-m3 (LangChain FAISS 형식, 정규화된 bge-m3 임베딩)
- 임베딩 모델(BAAI/bge-m3)은 로컬에서 돌린다. 처음 쓸 때 모델을 내려받는다.
- 문서별 서지 정보(제목·발행기관·날짜·URL)는 data/docs/metadata.json 에 적는다. 보고서의 REFERENCE 에 쓰인다.

문서를 바꾼 뒤 인덱스 다시 만들기 (프로젝트 루트에서):
    python -m agents.docs
"""

import json
import re
import threading
import unicodedata

from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings

from . import dart

DOCS_DIR = dart.DATA_DIR / "docs"
METADATA_FILE = DOCS_DIR / "metadata.json"
STORE_DIR = dart.DATA_DIR / "vectorstores" / "BAAI--bge-m3"
EMBED_MODEL = "BAAI/bge-m3"
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150
MIN_PAGE_CHARS = 50  # 표지·간지처럼 글자가 거의 없는 쪽은 건너뛴다

KEYWORD_WEIGHT = 0.5
CANDIDATES = 30

# 도구는 여러 스레드에서 동시에 호출된다. 임베딩 모델을 동시에 돌리면 프로세스가 죽으므로(macOS MPS)
# 모델 로드와 벡터 검색을 한 번에 하나씩만 실행한다.
_lock = threading.Lock()
_store: FAISS | None = None


def _embeddings() -> HuggingFaceEmbeddings:
    return HuggingFaceEmbeddings(model_name=EMBED_MODEL, encode_kwargs={"normalize_embeddings": True})


def _load() -> FAISS:
    global _store
    if _store is None:
        if not (STORE_DIR / "index.faiss").exists():
            raise FileNotFoundError(f"문서 풀 인덱스가 없습니다: {STORE_DIR} ('python -m agents.docs' 로 만드세요)")
        # index.pkl 은 직접 만든 인덱스 파일이므로 역직렬화를 허용한다
        _store = FAISS.load_local(str(STORE_DIR), _embeddings(), allow_dangerous_deserialization=True)
    return _store


# ---------------------------------------------------------------------------
# 검색
# ---------------------------------------------------------------------------

def _bigrams(text: str) -> set[str]:
    """공백 제거 후 2글자 단위 조각. 조사·띄어쓰기 차이에 강한 한국어 키워드 매칭용."""
    s = re.sub(r"[^\w]", "", text.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _keyword_score(query: str, doc: str) -> float:
    q = _bigrams(query)
    return len(q & _bigrams(doc)) / len(q) if q else 0.0


def search(query: str, k: int = 5) -> str:
    """하이브리드 검색: 벡터 유사도로 후보를 넉넉히 뽑은 뒤 키워드 일치도를 더해 재정렬."""
    with _lock:
        store = _load()
        candidates = store.similarity_search_with_score(query, k=min(CANDIDATES, store.index.ntotal))
    scored = sorted(
        # 정규화된 벡터의 L2 거리 제곱 d 는 코사인 유사도 1 - d/2 에 해당한다
        ((1 - dist / 2) + KEYWORD_WEIGHT * _keyword_score(query, doc.page_content), i)
        for i, (doc, dist) in enumerate(candidates)
    )[::-1][:k]
    lines = [f"[문서 풀 검색: '{query}']"]
    for rank, (score, i) in enumerate(scored, 1):
        doc = candidates[i][0]
        m = doc.metadata
        lines.append(f"\n--- 결과 {rank} (점수 {score:.2f}) ---\n"
                     f"출처: {m.get('title', '제목 없음')} | {m.get('source', '')} | {m.get('date', '')} | "
                     f"p.{m.get('page', '?')} | {m.get('url', '')}\n{doc.page_content}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 인덱싱
# ---------------------------------------------------------------------------

def _chunks() -> list[Document]:
    """data/docs 의 PDF를 쪽 단위로 읽어 chunk로 나눈다. 쪽 번호와 서지 정보를 metadata에 담는다."""
    import pymupdf
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    # macOS는 한글 파일명을 자모 분리형(NFD)으로 돌려주므로 맞춰서 비교한다
    nfc = lambda s: unicodedata.normalize("NFC", s)  # noqa: E731
    manifest = {nfc(k): v for k, v in json.loads(METADATA_FILE.read_text(encoding="utf-8")).items()} \
        if METADATA_FILE.exists() else {}
    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)

    chunks = []
    for path in sorted(DOCS_DIR.glob("*.pdf")):
        pdf = pymupdf.open(path)
        info = manifest.get(nfc(path.name), {})
        if not info:
            print(f"  ! {path.name}: metadata.json 에 서지 정보가 없어 PDF 속성으로 대체합니다")
        base = {
            "doc_id": nfc(path.stem),
            "title": info.get("title") or pdf.metadata.get("title") or nfc(path.stem),
            "source": info.get("source") or pdf.metadata.get("author") or "",
            "date": info.get("date", ""),
            "url": info.get("url", ""),
            "language": info.get("language", ""),
            "total_pages": len(pdf),
        }
        before = len(chunks)
        for number, page in enumerate(pdf, 1):
            text = page.get_text().strip()
            if len(text) < MIN_PAGE_CHARS:
                continue
            for piece in splitter.split_text(text):
                chunks.append(Document(page_content=piece, metadata={**base, "page": number, "chunk_id": len(chunks)}))
        print(f"  {path.name}: {len(pdf)}쪽 → {len(chunks) - before}개 chunk")
    return chunks


def build() -> int:
    """data/docs 의 PDF로 인덱스를 새로 만들어 기존 인덱스를 덮어쓴다. 만든 chunk 수를 돌려준다."""
    global _store
    chunks = _chunks()
    if not chunks:
        raise FileNotFoundError(f"인덱싱할 PDF가 없습니다: {DOCS_DIR}")
    with _lock:
        store = FAISS.from_documents(chunks, _embeddings())
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        store.save_local(str(STORE_DIR))
        _store = store
    return len(chunks)


if __name__ == "__main__":
    print(f"문서 풀 인덱싱: {DOCS_DIR}")
    print(f"완료: chunk {build()}개 → {STORE_DIR}")
