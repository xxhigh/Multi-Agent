"""사업보고서 본문 RAG.

1. DART에서 최신 사업보고서 원문(XML)을 내려받는다.
2. 목차(TITLE 태그) 기준으로 서술형 섹션만 잘라낸다.
   - II. 사업의 내용 / IV. 이사의 경영진단 및 분석의견 / XI. 투자자 보호 사항
   - 연결재무제표 주석 중 영업부문, 우발채무 및 약정사항
   (재무제표 숫자는 API 도구로 정확히 조회하므로 제외)
3. 섹션을 chunk로 나눠 OpenAI 임베딩 후 Chroma에 저장한다. 보고서(rcept_no)별로 1회만 인덱싱.
"""

import html
import io
import re
import threading
import zipfile
from functools import lru_cache

import chromadb
from langchain_openai import OpenAIEmbeddings

from . import dart

CHROMA_DIR = dart.DATA_DIR / "chroma"
EMBED_MODEL = "text-embedding-3-small"
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150

TOP_SECTIONS = ("II.", "IV.", "XI.")
NOTE_SECTIONS = ("영업부문 (연결)", "우발채무 및 약정사항 (연결)")

_index_lock = threading.Lock()


@lru_cache(maxsize=1)
def _client() -> chromadb.ClientAPI:
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(CHROMA_DIR))


@lru_cache(maxsize=1)
def _embeddings() -> OpenAIEmbeddings:
    return OpenAIEmbeddings(model=EMBED_MODEL)


# ---------------------------------------------------------------------------
# 문서 파싱
# ---------------------------------------------------------------------------

def _strip(fragment: str) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    return re.sub(r"[\s　\xa0]+", " ", s).strip()


def _table_to_text(m: re.Match) -> str:
    """표의 각 행을 '셀 | 셀 | 셀' 한 줄로 변환."""
    rows = []
    for tr in re.findall(r"<TR[^>]*>(.*?)</TR>", m.group(0), re.S):
        cells = [_strip(c) for c in re.findall(r"<(?:TD|TH|TE|TU)[^>]*>(.*?)</(?:TD|TH|TE|TU)>", tr, re.S)]
        cells = [c for c in cells if c]
        if cells:
            rows.append(" | ".join(cells))
    return "\n" + "\n".join(rows) + "\n"


def _to_text(fragment: str) -> str:
    """DART XML 조각을 텍스트로 변환. 표는 행 단위로 유지."""
    s = re.sub(r"\s+", " ", fragment)  # 원본 줄바꿈 제거 후 구조 태그로 줄을 다시 나눈다
    s = re.sub(r"<TABLE[^>]*>.*?</TABLE>", _table_to_text, s, flags=re.S)
    s = re.sub(r"</P>|<BR\s*/?>|<P[^>]*>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    lines = (re.sub(r"[ \t　\xa0]+", " ", line).strip() for line in s.splitlines())
    return "\n".join(line for line in lines if line)


def parse_sections(xml: str) -> list[tuple[str, str]]:
    """[(섹션 경로, 본문 텍스트)] — RAG 대상 섹션만."""
    titles = list(re.finditer(r'<TITLE[^>]*ATOC="Y"[^>]*>(.*?)</TITLE>', xml, re.S))
    sections, top = [], ""
    for i, m in enumerate(titles):
        title = _to_text(m.group(1)).replace("\n", " ")
        if re.match(r"^[IVX]+\.", title):
            top = title
        if not (top.startswith(TOP_SECTIONS) or title.endswith(NOTE_SECTIONS)):
            continue
        end = titles[i + 1].start() if i + 1 < len(titles) else len(xml)
        text = _to_text(xml[m.end():end])
        if len(text) > 50:
            sections.append((title if title == top else f"{top} > {title}", text))
    return sections


def _chunk(text: str) -> list[str]:
    chunks, buf = [], ""
    for line in text.splitlines():
        while len(line) > CHUNK_SIZE:  # 아주 긴 줄은 강제로 자름
            chunks.append(line[:CHUNK_SIZE])
            line = line[CHUNK_SIZE - CHUNK_OVERLAP:]
        if len(buf) + len(line) + 1 > CHUNK_SIZE and buf:
            chunks.append(buf)
            buf = buf[-CHUNK_OVERLAP:]
        buf = f"{buf}\n{line}" if buf else line
    if buf.strip():
        chunks.append(buf)
    return chunks


def _download_report(rcept_no: str) -> str:
    data = dart._request("document.xml", rcept_no=rcept_no)
    if not zipfile.is_zipfile(io.BytesIO(data)):
        # 파일 없음·요청 한도 초과 등은 zip 대신 <status>/<message> 응답이 온다
        body = data.decode("utf-8", "ignore")
        status = re.search(r"<status>(.*?)</status>", body)
        message = re.search(r"<message>(.*?)</message>", body)
        raise dart.DartError(
            f"사업보고서 원문을 받지 못했습니다 (DART {status.group(1) if status else '?'}: "
            f"{message.group(1) if message else body[:100]})"
        )
    zf = zipfile.ZipFile(io.BytesIO(data))
    name = f"{rcept_no}.xml" if f"{rcept_no}.xml" in zf.namelist() else zf.namelist()[0]
    return zf.read(name).decode("utf-8", "ignore")


# ---------------------------------------------------------------------------
# 인덱싱 / 검색
# ---------------------------------------------------------------------------

def latest_annual_report(corp: dart.Corp) -> tuple[str, int]:
    """최신 사업보고서의 (접수번호, 사업연도)."""
    rows, year, _ = dart.financial_statements(corp.corp_code, "annual")
    return rows[0]["rcept_no"], year


def _collection(rcept_no: str, corp: dart.Corp, year: int):
    """보고서 인덱스를 가져오고, 없으면 만든다."""
    col = _client().get_or_create_collection(f"dart-{rcept_no}", metadata={"hnsw:space": "cosine"})
    with _index_lock:
        if col.count() > 0:
            return col
        docs, metas = [], []
        for path, text in parse_sections(_download_report(rcept_no)):
            for chunk in _chunk(text):
                docs.append(f"[{path}]\n{chunk}")
                metas.append({"section": path, "company": corp.name, "year": year})
        if not docs:
            raise dart.DartError(f"{corp.name} 사업보고서에서 분석할 본문을 찾지 못했습니다.")
        vectors = _embeddings().embed_documents(docs)
        col.add(ids=[f"{rcept_no}-{i}" for i in range(len(docs))], documents=docs,
                embeddings=vectors, metadatas=metas)
    return col


def _bigrams(text: str) -> set[str]:
    """공백 제거 후 2글자 단위 조각. 조사·띄어쓰기 차이에 강한 한국어 키워드 매칭용."""
    s = re.sub(r"[^\w]", "", text.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _keyword_score(query: str, doc: str) -> float:
    q = _bigrams(query)
    return len(q & _bigrams(doc)) / len(q) if q else 0.0


KEYWORD_WEIGHT = 0.5
CANDIDATES = 30


def search(company: str, query: str, k: int = 5) -> str:
    """하이브리드 검색: 벡터 유사도로 후보를 넉넉히 뽑은 뒤 키워드 일치도를 더해 재정렬."""
    corp = dart.resolve_corp(company)
    rcept_no, year = latest_annual_report(corp)
    col = _collection(rcept_no, corp, year)
    res = col.query(query_embeddings=[_embeddings().embed_query(query)],
                    n_results=min(CANDIDATES, col.count()))
    scored = sorted(
        ((1 - dist) + KEYWORD_WEIGHT * _keyword_score(query, doc), doc)
        for doc, dist in zip(res["documents"][0], res["distances"][0])
    )[::-1][:k]
    lines = [f"[{corp.name} {year} 사업보고서 검색: '{query}'] "
             f"원문: https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"]
    for i, (score, doc) in enumerate(scored, 1):
        lines.append(f"\n--- 결과 {i} (점수 {score:.2f}) ---\n{doc}")
    return "\n".join(lines)
