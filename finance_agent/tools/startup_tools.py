"""Physical AI·로보틱스 스타트업 투자 평가 도구.

- 기업개황·공시·감사보고서: OpenDART (비상장 외부감사 대상 기업 포함)
- 산업·기업 자료: 문서 풀(data/docs) RAG
숫자(재무 계정·비율)는 LLM이 아니라 Python이 원문 표에서 뽑고 계산한다.
"""

from functools import wraps

from langchain_core.tools import tool

from .. import config
from . import audit, dart, docs, rag
from .dart import DartError
from .common import _krw, calculator

FINANCIAL_TABLE_MARK = "(원본 표: 숫자·단위를 수정하지 말고 그대로 인용)"
TERM_WORDS = ("상환전환우선주", "전환상환우선주", "전환사채", "신주인수권", "파생상품부채", "정부보조금")


def _safe(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DartError as e:
            return f"오류: {e}"
    return wrapper


def _pct(v) -> str:
    return "N/A" if v is None else f"{v * 100:.1f}%"


def _div(a, b):
    return None if a is None or b in (None, 0) else a / b


# ---------------------------------------------------------------------------
# researcher: 회사·팀·기술
# ---------------------------------------------------------------------------

@tool
@_safe
def search_company(query: str) -> str:
    """회사명 일부로 DART 등록 기업(비상장 포함)을 검색한다. 정식 회사명을 모를 때 먼저 사용한다."""
    found = dart.search_corps(query)
    if not found:
        return f"'{query}'에 해당하는 DART 등록 기업이 없습니다."
    return "\n".join(f"{c.name} ({'종목코드 ' + c.stock_code if c.stock_code else '비상장'})" for c in found)


@tool
@_safe
def get_company_overview(company: str) -> str:
    """DART 기업개황: 정식 회사명, 상장 여부, 대표자, 설립일, 업종코드, 주소, 홈페이지.

    company: DART 정식 회사명 (예: '트위니')
    """
    corp = dart.resolve_corp(company)
    info = dart.company_info(corp.corp_code)
    est = info.get("est_dt", "")
    listed = dart.MARKETS.get(info.get("corp_cls"), "N/A")
    return "\n".join([
        f"회사명: {info.get('corp_name')} ({info.get('corp_name_eng', '')})",
        f"상장 구분: {listed}{' (비상장)' if not corp.stock_code else ', 종목코드 ' + corp.stock_code}",
        f"대표자: {info.get('ceo_nm', 'N/A')}",
        f"설립일: {est[:4]}-{est[4:6]}-{est[6:]}" if len(est) == 8 else "설립일: N/A",
        f"업종코드(KSIC): {info.get('induty_code', 'N/A')}",
        f"주소: {info.get('adres', 'N/A')}",
        f"홈페이지: {info.get('hm_url') or 'N/A'}",
        f"DART 기업 고유번호: {corp.corp_code}",
    ])


@tool
@_safe
def get_recent_disclosures(company: str, days: int = 1095) -> str:
    """DART 공시 목록(날짜, 보고서명, 링크). 유상증자 결정(투자 유치), 감사보고서, 지배구조 변화 확인용.

    company: DART 정식 회사명
    days: 조회 기간(일). 비상장 스타트업은 공시가 드물어 기본 3년.
    """
    corp = dart.resolve_corp(company)
    items = dart.recent_disclosures(corp.corp_code, days)
    if not items:
        return f"{corp.name}의 최근 {days}일 공시가 없습니다."
    lines = [f"[{corp.name} 최근 {days}일 DART 공시]"]
    for it in items:
        d = it["rcept_dt"]
        lines.append(f"{d[:4]}-{d[4:6]}-{d[6:]} | {it['report_nm'].strip()} | {it['flr_nm']} | "
                     f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it['rcept_no']}")
    return "\n".join(lines)


@tool
def search_company_docs(company: str, query: str, k: int = 5) -> str:
    """문서 풀의 기업 자료(제품 소개서·IR·기사)에서 질문과 관련된 부분을 검색한다 (RAG).

    창업자·팀, 제품·기술 사양, 고객·도입 사례, 투자 유치 이력, 상장 계획 같은 정성 정보에 사용한다.
    company: DART 정식 회사명 (data/docs/companies/<회사명>/ 폴더)
    query: 찾을 내용 (예: '제품 라인업과 기술 사양', '투자 유치 이력', '고객사 도입 사례')
    """
    folder = docs.company_folder(company)
    if folder is None:
        return f"오류: '{company}' 기업 자료 폴더가 없습니다 (data/docs/companies/{company}/)."
    return docs.search(folder, f"기업 자료 · {folder.name}", query, k)


# ---------------------------------------------------------------------------
# market_researcher: 산업·시장
# ---------------------------------------------------------------------------

@tool
def search_industry_docs(query: str, k: int = 5) -> str:
    """문서 풀의 산업 자료(로보틱스·Physical AI 시장 리포트)에서 질문과 관련된 부분을 검색한다 (RAG).

    query: 찾을 내용 (예: '물류 로봇 시장 규모와 성장률', '서비스 로봇 판매 대수', 'RaaS 비즈니스 모델', '경쟁 기업')
    """
    return docs.search(config.DOCS_DIR / "industry", "산업 자료", query, k)


# ---------------------------------------------------------------------------
# investment_analyst: 재무·투자조건·리스크
# ---------------------------------------------------------------------------

def _financial_table(corp: dart.Corp) -> tuple[str, dict]:
    loaded = audit.load(corp.corp_code)
    if loaded is None:
        raise DartError(f"{corp.name}의 최근 {audit.LOOKBACK_YEARS}년 감사보고서가 DART에 없습니다 "
                        "(외부감사 대상이 아니거나 공시 이력 없음).")
    report, sections = loaded
    fin, _ = audit.extract_financials(audit.statements_text(sections))
    if not fin:
        raise DartError(f"{corp.name} 감사보고서({report.report_nm})에서 재무제표 표를 읽지 못했습니다.")
    year = report.fiscal[:4]
    prev_year = str(int(year) - 1) if year.isdigit() else "전기"
    out = [f"[{corp.name} {report.report_nm}] {FINANCIAL_TABLE_MARK}",
           f"원문: {report.url}", "",
           f"| 계정 | {year} | {prev_year} |", "|---|---:|---:|"]
    for section, keys in audit.SECTIONS.items():
        out.append(f"| **{section}** | | |")
        for key in keys:
            if key in fin:
                cur, prev = fin[key]
                out.append(f"| {key} | {_krw(cur)} | {_krw(prev)} |")
    return "\n".join(out), {"fin": fin, "report": report, "sections": sections}


@tool
@_safe
def get_audit_financials(company: str) -> str:
    """최신 DART 감사보고서의 재무제표 핵심 계정(2개년)과 투자 판단 지표를 Python으로 계산해 돌려준다.

    지표: 매출 성장률, 매출총이익률, 영업이익률, 순이익률, 부채비율, 자본잠식 여부, 유동비율,
    현금 소진 기간(현금 ÷ 영업활동 순유출), 감사의견의 계속기업 불확실성 문단 여부, 주석의 투자조건 키워드.
    company: DART 정식 회사명
    """
    corp = dart.resolve_corp(company)
    table, ctx = _financial_table(corp)
    fin = ctx["fin"]
    g = lambda k, i=0: fin.get(k, (None, None))[i]  # noqa: E731
    rev, rev_prev = g("매출액"), g("매출액", 1)
    equity, capital = g("자본총계"), g("자본금")
    cash, ocf = g("현금및현금성자산"), g("영업활동현금흐름")
    if equity is not None and equity < 0:
        impairment = "완전자본잠식 (자본총계 < 0)"
    elif equity is not None and capital and equity < capital:
        impairment = f"부분자본잠식 (자본총계 < 자본금, 잠식률 {_pct(1 - equity / capital)})"
    else:
        impairment = "해당 없음" if equity is not None else "N/A"
    runway = (f"{cash / -ocf:.1f}년 (현금 {_krw(cash)} ÷ 연간 영업 순유출 {_krw(-ocf)})"
              if cash is not None and ocf is not None and ocf < 0 else "N/A (영업현금흐름 흑자 또는 자료 없음)")
    gc = audit.going_concern_flag(ctx["sections"])
    terms = audit.keyword_counts(ctx["sections"], TERM_WORDS)
    lines = [
        table, "",
        "■ 투자 판단 지표 (Python 계산)",
        f"  매출 성장률: {_pct(_div(rev - rev_prev, abs(rev_prev)) if rev is not None and rev_prev else None)}",
        f"  매출총이익률: {_pct(_div(g('매출총이익'), rev))}",
        f"  영업이익률: {_pct(_div(g('영업이익'), rev))}",
        f"  순이익률: {_pct(_div(g('당기순이익'), rev))}",
        f"  부채비율(부채/자본): {_pct(_div(g('부채총계'), equity)) if equity and equity > 0 else 'N/A (자본총계 0 이하)'}",
        f"  자본잠식: {impairment}",
        f"  유동비율(유동자산/유동부채): {_pct(_div(g('유동자산'), g('유동부채')))}",
        f"  현금 소진 기간(runway): {runway}",
        f"  감사의견 계속기업 불확실성 문단: {gc or '없음'}",
        f"  주석 투자조건 키워드 등장 횟수: {', '.join(f'{k} {v}회' for k, v in terms.items()) or '없음'}",
        "  ※ 상환전환우선주(RCPS)가 부채로 분류되면 자본잠식이 회계 분류 효과일 수 있음 — search_audit_report로 주석 확인",
    ]
    return "\n".join(lines)


@tool
@_safe
def search_audit_report(company: str, query: str, k: int = 5) -> str:
    """최신 DART 감사보고서의 감사의견·주석 본문에서 질문과 관련된 부분을 검색한다 (RAG).

    투자조건(상환전환우선주·전환사채 조건), 차입금, 계속기업, 특수관계자, 정부보조금, 연구개발비,
    매출 구성처럼 재무 숫자의 '이유'를 찾을 때 사용한다.
    company: DART 정식 회사명
    query: 찾을 내용 (예: '상환전환우선주 발행 조건', '차입금 만기', '매출 구성', '연구개발비')
    """
    corp = dart.resolve_corp(company)
    loaded = audit.load(corp.corp_code)
    if loaded is None:
        return f"오류: {corp.name}의 감사보고서가 DART에 없습니다."
    report, sections = loaded
    hits = rag.hybrid_search(
        f"audit-{report.rcept_no}",
        lambda: rag.chunk_sections(audit.rag_sections(sections), {"company": corp.name, "rcept_no": report.rcept_no}),
        query, k)
    lines = [f"[{corp.name} {report.report_nm} 검색: '{query}'] 원문: {report.url}"]
    for i, (score, doc, _) in enumerate(hits, 1):
        lines.append(f"\n--- 결과 {i} (점수 {score:.2f}) ---\n{doc}")
    return "\n".join(lines)


RESEARCHER_TOOLS = [search_company, get_company_overview, get_recent_disclosures, search_company_docs]
MARKET_TOOLS = [search_industry_docs, search_company_docs]
INVESTMENT_TOOLS = [get_audit_financials, search_audit_report, get_recent_disclosures, calculator]
