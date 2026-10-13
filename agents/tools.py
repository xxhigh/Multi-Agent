"""스타트업 투자 평가 agent가 사용하는 도구 모음.

- 기업 정보·뉴스·투자 유치: Tavily 웹 검색
- 산업·기술 문서: data/docs 문서 풀 (FAISS, bge-m3)
- 국내 상장사의 기업개황·재무제표·공시: OpenDART (비상장·해외 기업은 조회되지 않는다)
- 시가총액: Yahoo Finance(yfinance)
재무 비율과 스코어카드 합산은 LLM이 아니라 Python에서 처리해 정확도를 확보한다.
"""

import ast
import operator
import os
from functools import lru_cache, wraps
from typing import Literal

import yfinance as yf
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from tavily import TavilyClient

from . import criteria, dart
from .dart import DartError

# 이 표시가 있는 도구 결과(재무제표, 스코어카드)는 요약 과정에서 숫자가 바뀌지 않도록 보고서까지 그대로 전달한다
VERBATIM_MARK = "(원본 표: 숫자·단위를 수정하지 말고 그대로 인용)"


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _krw(value) -> str:
    """원 단위 금액을 조/억 단위로 표기."""
    if value is None:
        return "N/A"
    sign = "-" if value < 0 else ""
    v = abs(value)
    if v >= 1e12:
        return f"{sign}{v / 1e12:,.2f}조"
    if v >= 1e8:
        return f"{sign}{v / 1e8:,.0f}억"
    return f"{sign}{v:,.0f}원"


def _pct(value) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def _div(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b


def _num(s) -> float | None:
    if s in (None, "", "-"):
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


# (표시명, 재무제표 구분, account_id 후보, account_nm 후보)
_ACCOUNTS = {
    "매출액": (("IS", "CIS"), ["ifrs-full_Revenue"], ["매출액", "수익(매출액)", "영업수익", "매출"]),
    "매출총이익": (("IS", "CIS"), ["ifrs-full_GrossProfit"], ["매출총이익"]),
    "영업이익": (("IS", "CIS"), ["dart_OperatingIncomeLoss"], ["영업이익", "영업이익(손실)"]),
    "당기순이익": (("IS", "CIS"), ["ifrs-full_ProfitLoss"], ["당기순이익", "당기순이익(손실)"]),
    "지배주주순이익": (("IS", "CIS"), ["ifrs-full_ProfitLossAttributableToOwnersOfParent"], []),
    "기본주당이익": (("IS", "CIS"), ["ifrs-full_BasicEarningsLossPerShare"], ["기본주당이익"]),
    "자산총계": (("BS",), ["ifrs-full_Assets"], ["자산총계"]),
    "부채총계": (("BS",), ["ifrs-full_Liabilities"], ["부채총계"]),
    "자본총계": (("BS",), ["ifrs-full_Equity"], ["자본총계"]),
    "지배주주지분": (("BS",), ["ifrs-full_EquityAttributableToOwnersOfParent"], []),
    "유동자산": (("BS",), ["ifrs-full_CurrentAssets"], ["유동자산"]),
    "유동부채": (("BS",), ["ifrs-full_CurrentLiabilities"], ["유동부채"]),
    "현금및현금성자산": (("BS",), ["ifrs-full_CashAndCashEquivalents"], ["현금및현금성자산"]),
    "영업활동현금흐름": (("CF",), ["ifrs-full_CashFlowsFromUsedInOperatingActivities"], ["영업활동현금흐름"]),
    "유형자산취득": (("CF",), ["ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],
                ["유형자산의 취득", "유형자산의취득"]),
    "배당금지급": (("CF",), ["ifrs-full_DividendsPaidClassifiedAsFinancingActivities"], ["배당금의 지급", "배당금지급"]),
}

_SECTIONS = {
    "손익계산서": ["매출액", "매출총이익", "영업이익", "당기순이익", "지배주주순이익", "기본주당이익"],
    "재무상태표": ["자산총계", "부채총계", "자본총계", "지배주주지분", "유동자산", "유동부채", "현금및현금성자산"],
    "현금흐름표": ["영업활동현금흐름", "유형자산취득", "배당금지급"],
}


def _find(rows: list[dict], key: str) -> dict | None:
    sj_divs, ids, names = _ACCOUNTS[key]
    for sj in sj_divs:  # 손익계산서(IS)를 포괄손익계산서(CIS)보다 우선
        for r in rows:
            if r["sj_div"] == sj and r["account_id"] in ids:
                return r
        for r in rows:
            if r["sj_div"] == sj and r["account_nm"].replace(" ", "") in [n.replace(" ", "") for n in names]:
                return r
    return None


def _values(rows: list[dict], key: str, quarterly: bool) -> list[float | None]:
    """[당기, 전기, 전전기] 값. 분기 보고서의 손익·현금흐름은 누적 금액 기준."""
    r = _find(rows, key)
    if r is None:
        return [None, None, None]
    if quarterly:
        cur = _num(r.get("thstrm_add_amount")) if r.get("thstrm_add_amount") else _num(r.get("thstrm_amount"))
        prev = _num(r.get("frmtrm_add_amount")) if r.get("frmtrm_add_amount") else _num(r.get("frmtrm_amount"))
        return [cur, prev, None]
    return [_num(r.get("thstrm_amount")), _num(r.get("frmtrm_amount")), _num(r.get("bfefrmtrm_amount"))]


def _resolve(company: str) -> dart.Corp:
    return dart.resolve_corp(company)


@lru_cache(maxsize=64)
def _yf_ticker(stock_code: str, corp_cls: str) -> yf.Ticker:
    suffix = ".KQ" if corp_cls == "K" else ".KS"
    return yf.Ticker(stock_code + suffix)


def _market_cap(corp: dart.Corp) -> float | None:
    info = dart.company_info(corp.corp_code)
    try:
        return _yf_ticker(corp.stock_code, info.get("corp_cls", "Y")).info.get("marketCap")
    except Exception:  # noqa: BLE001
        return None


def _safe(fn):
    """DART 오류를 LLM이 읽을 수 있는 문자열로 반환."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DartError as e:
            return f"오류: {e}"
    return wrapper


# ---------------------------------------------------------------------------
# 국내 상장사 도구: DART 공시 데이터
# ---------------------------------------------------------------------------

@tool
@_safe
def search_company(query: str) -> str:
    """회사명 일부 또는 종목코드로 국내 상장사를 검색한다. 정식 회사명을 모를 때 먼저 사용한다."""
    found = dart.search_corps(query)
    if not found:
        return f"'{query}'에 해당하는 상장사가 없습니다."
    return "\n".join(f"{c.name} (종목코드 {c.stock_code})" for c in found)


@tool
@_safe
def get_company_overview(company: str) -> str:
    """기업 개황(시장, 대표자, 업종코드, 설립일, 결산월)과 현재 시가총액을 조회한다.

    company: 정식 회사명(예: '삼성전자') 또는 6자리 종목코드(예: '005930')
    """
    corp = _resolve(company)
    info = dart.company_info(corp.corp_code)
    est = info.get("est_dt", "")
    lines = [
        f"회사명: {info.get('corp_name')} ({info.get('corp_name_eng', '')})",
        f"종목코드: {corp.stock_code}, 시장: {dart.MARKETS.get(info.get('corp_cls'), 'N/A')}",
        f"대표자: {info.get('ceo_nm', 'N/A')}",
        f"업종코드(KSIC): {info.get('induty_code', 'N/A')}",
        f"설립일: {est[:4]}-{est[4:6]}-{est[6:]}" if len(est) == 8 else "설립일: N/A",
        f"결산월: {info.get('acc_mt', 'N/A')}월",
        f"홈페이지: {info.get('hm_url') or 'N/A'}",
        f"시가총액(Yahoo Finance): {_krw(_market_cap(corp))}",
    ]
    return "\n".join(lines)


@tool
@_safe
def get_financial_statements(company: str, report: str = "annual", year: int | None = None) -> str:
    """DART 공시 재무제표 핵심 계정을 조회한다 (연결 우선, 없으면 별도).

    company: 정식 회사명 또는 6자리 종목코드
    report: 'annual'(사업보고서), 'q1'(1분기), 'half'(반기), 'q3'(3분기),
            'latest'(가장 최근 정기보고서 — 올해 실적 흐름을 볼 때 사용)
    year: 사업연도. 생략하면 가장 최근 보고서
    연간은 최근 3개 사업연도, 분기는 당기 누적과 전년 동기 누적(재무상태표는 전기말)을 반환한다.
    """
    corp = _resolve(company)
    if report == "latest":
        report, year = dart.latest_report(corp.corp_code)
    rows, y, fs = dart.financial_statements(corp.corp_code, report, year)
    quarterly = report != "annual"
    if quarterly:
        headers = [f"{y} {report} 누적", f"{y - 1} 동기 누적(재무상태표는 전기말)"]
    else:
        headers = [f"{y}", f"{y - 1}", f"{y - 2}"]
    # LLM이 숫자를 옮겨 적다 단위를 틀리지 않도록 보고서에 그대로 붙일 수 있는 Markdown 표로 만든다
    out = [f"[{corp.name} {'연결' if fs == 'CFS' else '별도'}재무제표, {report}] {VERBATIM_MARK}",
           "",
           "| 계정 | " + " | ".join(headers) + " |",
           "|---|" + "---:|" * len(headers)]
    for section, keys in _SECTIONS.items():
        out.append(f"| **{section}** |" + " |" * len(headers))
        for key in keys:
            vals = _values(rows, key, quarterly)[: len(headers)]
            if all(v is None for v in vals):
                continue
            fmt = (lambda v: "N/A" if v is None else f"{v:,.0f}원") if key == "기본주당이익" else _krw
            out.append(f"| {key} | " + " | ".join(fmt(v) for v in vals) + " |")
    # 보고서의 REFERENCE 에 그대로 옮길 수 있는 출처 표기 (접수번호 앞 8자리가 접수일)
    rcept = rows[0].get("rcept_no", "")
    if len(rcept) >= 8:
        title = {"annual": "사업보고서", "half": "반기보고서", "q1": "분기보고서(1분기)", "q3": "분기보고서(3분기)"}[report]
        out += ["", f"출처: [웹페이지] 금융감독원 전자공시시스템({rcept[:4]}-{rcept[4:6]}-{rcept[6:8]}). "
                    f"*{corp.name} {title} ({y})*. DART, https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}"]
    return "\n".join(out)


@tool
@_safe
def get_recent_disclosures(company: str, days: int = 90) -> str:
    """최근 DART 공시 목록(날짜, 보고서명, 제출인, 링크)을 조회한다. 최근 이슈 파악에 사용한다.

    company: 정식 회사명 또는 6자리 종목코드
    days: 조회 기간(일)
    """
    corp = _resolve(company)
    items = dart.recent_disclosures(corp.corp_code, days)
    if not items:
        return f"{corp.name}의 최근 {days}일 공시가 없습니다."
    lines = [f"[{corp.name} 최근 {days}일 공시]"]
    for it in items:
        d = it["rcept_dt"]
        lines.append(
            f"{d[:4]}-{d[4:6]}-{d[6:]} | {it['report_nm'].strip()} | {it['flr_nm']} | "
            f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it['rcept_no']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 문서 풀 도구: 산업·기술 문서 RAG
# ---------------------------------------------------------------------------

@tool
def search_documents(query: str, k: int = 5) -> str:
    """문서 풀(data/docs)에서 질문과 관련된 부분을 검색한다 (RAG).

    검색 범위: 미리 수집해 둔 Physical AI · 로보틱스 산업·기술 보고서(증권사 산업 분석, 싱크탱크 정책 보고서,
    컨설팅사 전망). 개별 기업의 최신 소식이 아니라 기술 동향(로봇 파운데이션 모델, 학습 데이터, 하드웨어·부품),
    시장 규모·성장 전망, 생태계와 가치사슬, 정책, 주요 업체 구도처럼 산업 차원의 근거가 필요할 때 사용한다.
    결과마다 문서 제목·발행기관·날짜·쪽수가 붙는다. 한국어·영어 문서가 섞여 있어 두 언어 모두 검색된다.
    query: 찾을 내용 (예: '휴머노이드 로봇 시장 규모 전망', '로봇 파운데이션 모델 학습 데이터', 'Physical AI supply chain')
    k: 가져올 문단 수
    처음 호출할 때는 임베딩 모델을 올리느라 시간이 걸린다.
    """
    try:
        from . import docs  # 임베딩 모델 의존성이 무거워서 실제로 쓸 때 불러온다
        return docs.search(query, max(1, min(k, 10)))
    except Exception as e:  # noqa: BLE001 - 인덱스 없음·모델 로드 실패 등을 LLM이 읽을 수 있게 반환
        return f"오류: 문서 풀 검색 실패 ({type(e).__name__}: {e})"


# ---------------------------------------------------------------------------
# Evaluator 도구: 재무 비율 · 스코어카드
# ---------------------------------------------------------------------------

@tool
@_safe
def calculate_financial_ratios(company: str) -> str:
    """최근 사업보고서(연결)로 성장성·수익성·안정성·현금흐름·밸류에이션 지표를 계산한다.

    매출/영업이익/순이익 성장률, 매출총이익률, 영업이익률, 순이익률, ROE, ROA,
    부채비율(부채/자본), 유동비율, FCF·FCF 마진, 배당성향, PER·PBR(시가총액 기준)을 반환한다.
    company: 정식 회사명 또는 6자리 종목코드
    """
    corp = _resolve(company)
    rows, y, fs = dart.financial_statements(corp.corp_code, "annual")
    v = {k: _values(rows, k, quarterly=False) for k in _ACCOUNTS}

    def g(key, i=0):
        return v[key][i]

    def growth(key):
        cur, prev = g(key, 0), g(key, 1)
        return _div(cur - prev, abs(prev)) if cur is not None and prev else None

    def avg(key):
        cur, prev = g(key, 0), g(key, 1)
        if cur is None:
            return None
        return (cur + prev) / 2 if prev is not None else cur

    owner_ni = g("지배주주순이익") if g("지배주주순이익") is not None else g("당기순이익")
    owner_eq_key = "지배주주지분" if g("지배주주지분") is not None else "자본총계"
    capex = abs(g("유형자산취득")) if g("유형자산취득") is not None else None
    fcf = g("영업활동현금흐름") - capex if g("영업활동현금흐름") is not None and capex is not None else None
    dividends = abs(g("배당금지급")) if g("배당금지급") is not None else None
    mcap = _market_cap(corp)
    ttm_ni, ttm_label, latest_eq = _ttm_owner_ni(corp, y, owner_ni)
    per_ni = ttm_ni if ttm_ni is not None else owner_ni
    pbr_eq = latest_eq if latest_eq is not None else g(owner_eq_key)
    per = _div(mcap, per_ni) if per_ni and per_ni > 0 else None
    pbr = _div(mcap, pbr_eq)

    lines = [
        f"[{corp.name} 재무 비율, {y} 사업보고서 {'연결' if fs == 'CFS' else '별도'} 기준]",
        "■ 성장성 (전년 대비)",
        f"  매출액: {_pct(growth('매출액'))}, 영업이익: {_pct(growth('영업이익'))}, 당기순이익: {_pct(growth('당기순이익'))}",
        "■ 수익성",
        f"  매출총이익률: {_pct(_div(g('매출총이익'), g('매출액')))}",
        f"  영업이익률: {_pct(_div(g('영업이익'), g('매출액')))}",
        f"  순이익률: {_pct(_div(g('당기순이익'), g('매출액')))}",
        f"  ROE(지배주주 순이익/평균 지배주주지분): {_pct(_div(owner_ni, avg(owner_eq_key)))}",
        f"  ROA(당기순이익/평균 자산): {_pct(_div(g('당기순이익'), avg('자산총계')))}",
        "■ 안정성",
        f"  부채비율(부채총계/자본총계): {_pct(_div(g('부채총계'), g('자본총계')))}",
        f"  유동비율(유동자산/유동부채): {_pct(_div(g('유동자산'), g('유동부채')))}",
        "■ 현금흐름",
        f"  영업활동현금흐름: {_krw(g('영업활동현금흐름'))}, 유형자산 투자(CAPEX): {_krw(capex)}",
        f"  FCF(영업CF - CAPEX): {_krw(fcf)}, FCF 마진: {_pct(_div(fcf, g('매출액')))}",
        f"  배당금 지급: {_krw(dividends)}, 배당성향(배당금/지배주주 순이익): {_pct(_div(dividends, owner_ni))}",
        f"■ 밸류에이션 (현재 시가총액 기준, 순이익: {ttm_label or f'{y} 연간'})",
        f"  시가총액: {_krw(mcap)}",
        f"  지배주주 순이익({ttm_label or f'{y} 연간'}): {_krw(per_ni)}",
        f"  PER: {f'{per:.2f}배' if per else 'N/A(적자 또는 데이터 없음)'}",
        f"  PBR(최근 지배주주지분 기준): {f'{pbr:.2f}배' if pbr else 'N/A'}",
    ]
    return "\n".join(lines)


def _ttm_owner_ni(corp: dart.Corp, annual_year: int, annual_ni) -> tuple:
    """연간 보고서 이후 분기 보고서가 있으면 최근 12개월(TTM) 지배주주 순이익과 최신 지배주주지분을 계산.

    TTM = 직전 연간 + 당해 누적 - 전년 동기 누적
    """
    for report in ("q3", "half", "q1"):
        try:
            rows, y, _ = dart.financial_statements(corp.corp_code, report, annual_year + 1)
        except DartError:
            continue
        key = "지배주주순이익" if _find(rows, "지배주주순이익") else "당기순이익"
        cur, prev = _values(rows, key, quarterly=True)[:2]
        eq_key = "지배주주지분" if _find(rows, "지배주주지분") else "자본총계"
        latest_eq = _values(rows, eq_key, quarterly=True)[0]
        if None in (cur, prev, annual_ni):
            return None, None, latest_eq
        return annual_ni + cur - prev, f"TTM({y} {report}까지 최근 12개월)", latest_eq
    return None, None, None


# ---------------------------------------------------------------------------
# 웹 검색 도구
# ---------------------------------------------------------------------------

WEB_TOPICS = ("general", "news", "finance")
WEB_TIME_RANGES = ("day", "week", "month", "year")
WEB_CONTENT_PREVIEW = 500


@lru_cache(maxsize=1)
def _tavily() -> TavilyClient:
    return TavilyClient(api_key=os.environ["TAVILY_API_KEY"])


@tool
def web_search(query: str, topic: str = "general", time_range: str | None = None, max_results: int = 5) -> str:
    """Tavily로 웹을 검색해 제목·날짜·URL·본문 요약을 반환한다.

    스타트업의 기업 정보(설립, 창업자·팀, 제품, 고객, 투자 유치 이력, 기업가치)와 기술 자료, 시장·경쟁사 동향,
    최근 뉴스를 찾을 때 사용한다. 영어 자료가 많은 해외 기업은 영어 검색어도 함께 쓴다.
    query: 검색어. 회사명과 주제를 함께 넣는다 (예: 'Figure AI Series C funding valuation', '레인보우로보틱스 휴머노이드 양산')
    topic: 'general'(일반 웹, 기본값), 'news'(뉴스 기사), 'finance'(금융·시장 정보)
    time_range: 최근 기간으로 제한. 'day', 'week', 'month', 'year' 중 하나. 생략하면 제한 없음
    max_results: 가져올 결과 수 (1~10)
    """
    if not os.getenv("TAVILY_API_KEY", "").strip():
        return "오류: .env에 TAVILY_API_KEY가 설정되어 있지 않아 웹 검색을 할 수 없습니다."
    if topic not in WEB_TOPICS:
        return f"오류: topic은 {list(WEB_TOPICS)} 중 하나여야 합니다."
    if time_range is not None and time_range not in WEB_TIME_RANGES:
        return f"오류: time_range는 {list(WEB_TIME_RANGES)} 중 하나여야 합니다."
    try:
        res = _tavily().search(query, topic=topic, time_range=time_range, max_results=max(1, min(max_results, 10)))
    except Exception as e:  # noqa: BLE001 - 한도 초과·네트워크 오류 등을 LLM이 읽을 수 있게 반환
        return f"오류: 웹 검색 실패 ({type(e).__name__}: {e})"
    results = res.get("results", [])
    if not results:
        return f"'{query}'에 대한 웹 검색 결과가 없습니다."
    lines = [f"[웹 검색: '{query}', topic={topic}, 기간={time_range or '전체'}]"]
    for i, r in enumerate(results, 1):
        published = f" ({r['published_date']})" if r.get("published_date") else ""
        lines.append(f"\n--- 결과 {i}{published} ---\n{r.get('title', '')}\n{r.get('url', '')}\n"
                     f"{(r.get('content') or '')[:WEB_CONTENT_PREVIEW]}")
    return "\n".join(lines)


_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg,
    ast.UAdd: operator.pos, ast.Mod: operator.mod,
}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("허용되지 않는 표현식")


@tool
def calculator(expression: str) -> str:
    """사칙연산/거듭제곱 수식을 계산한다. 예: '(120.5 - 98.2) / 98.2 * 100'"""
    try:
        return str(_eval(ast.parse(expression, mode="eval").body))
    except Exception as e:  # noqa: BLE001
        return f"계산 오류: {e}"


class CriterionScore(BaseModel):
    criterion: Literal[tuple(c.key for c in criteria.CRITERIA)] = Field(description="평가 항목 key")
    score: int | None = Field(description="1~5점 (5가 가장 우수). 근거가 없어 평가할 수 없으면 null")
    rationale: str = Field(description="점수의 근거를 출처와 함께 1~2문장으로")


@tool
def score_investment(company: str, scores: list[CriterionScore]) -> str:
    """평가 항목별 점수를 받아 가중 합산한 종합 점수(100점)와 투자 의견을 스코어카드 표로 반환한다.

    company: 평가 대상 기업명
    scores: 모든 평가 항목에 대해 하나씩. 근거가 없는 항목은 score를 null로 둔다 (그 항목을 빼고 환산)
    종합 점수와 투자 의견은 직접 계산하지 말고 반드시 이 도구의 결과를 사용한다.
    """
    given = {s.criterion: s for s in scores}
    absent = [c.key for c in criteria.CRITERIA if c.key not in given]
    if absent:
        return f"오류: 다음 평가 항목이 빠졌습니다: {absent}. 모든 항목을 넣어 다시 호출하세요 (근거가 없으면 score=null)."
    invalid = [s.criterion for s in scores if s.score is not None and not 1 <= s.score <= criteria.MAX_SCORE]
    if invalid:
        return f"오류: score는 1~{criteria.MAX_SCORE} 또는 null이어야 합니다: {invalid}"

    total, weight, verdict = criteria.total({k: s.score for k, s in given.items()})
    out = [f"[{company} 투자 평가 스코어카드] {VERBATIM_MARK}",
           "",
           "| 평가 항목 | 가중치 | 점수 | 근거 요약 |",
           "|---|---:|---:|---|"]
    for c in criteria.CRITERIA:
        s = given[c.key]
        score = "평가 보류" if s.score is None else f"{s.score} / {criteria.MAX_SCORE}"
        out.append(f"| {c.name} | {c.weight}% | {score} | {' '.join(s.rationale.replace('|', '/').split())} |")
    total_text = "N/A" if total is None else f"{total:.1f} / 100"
    out.append(f"| **종합** | {weight}% 평가 | **{total_text}** | **{verdict}** |")
    return "\n".join(out)


COMPANY_TOOLS = [web_search, search_company, get_company_overview, get_financial_statements, get_recent_disclosures]
TECH_TOOLS = [search_documents, web_search]
MARKET_TOOLS = [search_documents, web_search]
EVALUATOR_TOOLS = [score_investment, calculate_financial_ratios, calculator]
