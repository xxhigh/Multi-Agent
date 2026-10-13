"""OpenDART API 클라이언트.

- 기업 고유번호(corp_code) 목록은 최초 1회 내려받아 .cache/ 에 저장한다.
- 재무제표는 연결(CFS)을 우선 조회하고, 없으면 별도(OFS)로 대체한다.
"""

import io
import json
import re
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

BASE_URL = "https://opendart.fss.or.kr/api"
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
CORP_CACHE = CACHE_DIR / "corp_codes.json"
CORP_CACHE_TTL = 7 * 24 * 3600

# 자주 쓰는 약칭 → DART 정식 회사명
ALIASES = {
    "현대차": "현대자동차", "네이버": "NAVER", "하이닉스": "SK하이닉스", "삼전": "삼성전자",
    "엘지전자": "LG전자", "엘지화학": "LG화학", "엘지에너지솔루션": "LG에너지솔루션", "엔솔": "LG에너지솔루션",
    "삼바": "삼성바이오로직스", "포스코": "POSCO홀딩스", "현대모비스": "현대모비스", "케이티": "KT",
}

REPORT_CODES = {"annual": "11011", "half": "11012", "q1": "11013", "q3": "11014"}
MARKETS = {"Y": "코스피", "K": "코스닥", "N": "코넥스", "E": "기타"}


class DartError(Exception):
    pass


@dataclass(frozen=True)
class Corp:
    corp_code: str
    name: str
    stock_code: str


def _api_key() -> str:
    key = os.getenv("OPENDART_API_KEY", "").strip()
    if not key:
        raise DartError(".env에 OPENDART_API_KEY가 설정되어 있지 않습니다.")
    return key


def _request(path: str, **params) -> bytes:
    params["crtfc_key"] = _api_key()
    url = f"{BASE_URL}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


def _get_json(path: str, **params) -> dict:
    data = json.loads(_request(path, **params))
    status = data.get("status")
    if status == "013":  # 조회된 데이터 없음
        return {"list": []}
    if status != "000":
        raise DartError(f"DART 오류 {status}: {data.get('message')}")
    return data


# ---------------------------------------------------------------------------
# 기업 검색
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _listed_corps() -> list[Corp]:
    """상장사(종목코드가 있는 기업) 목록."""
    if CORP_CACHE.exists() and time.time() - CORP_CACHE.stat().st_mtime < CORP_CACHE_TTL:
        rows = json.loads(CORP_CACHE.read_text(encoding="utf-8"))
    else:
        zf = zipfile.ZipFile(io.BytesIO(_request("corpCode.xml")))
        root = ET.fromstring(zf.read(zf.namelist()[0]))
        rows = [
            {"corp_code": e.findtext("corp_code"), "name": e.findtext("corp_name"),
             "stock_code": (e.findtext("stock_code") or "").strip()}
            for e in root.iter("list")
        ]
        rows = [r for r in rows if r["stock_code"]]
        CACHE_DIR.mkdir(exist_ok=True)
        CORP_CACHE.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return [Corp(**r) for r in rows]


def _normalize(name: str) -> str:
    return name.replace(" ", "").replace("(주)", "").replace("주식회사", "").lower()


def search_corps(query: str, limit: int = 10) -> list[Corp]:
    """회사명 또는 6자리 종목코드로 상장사를 검색. 정확히 일치하는 항목이 맨 앞."""
    q = query.strip()
    code = re.search(r"\b(\d{6})\b", q)  # '현대자동차(005380)' 같은 입력 처리
    if code:
        q = code.group(1)
    corps = _listed_corps()
    if q.isdigit():
        return [c for c in corps if c.stock_code == q.zfill(6)]
    nq = _normalize(q)
    exact = [c for c in corps if _normalize(c.name) == nq]
    partial = [c for c in corps if nq in _normalize(c.name) and c not in exact]
    partial.sort(key=lambda c: len(c.name))
    return (exact + partial)[:limit]


def resolve_corp(query: str) -> Corp:
    """정확히 한 기업으로 확정. 애매하면 후보를 담아 예외를 던진다."""
    query = ALIASES.get(query.strip().replace(" ", ""), query)
    found = search_corps(query)
    if not found:
        raise DartError(f"'{query}'에 해당하는 상장사를 찾을 수 없습니다. 정식 회사명이나 종목코드를 사용하세요.")
    if re.search(r"\b\d{6}\b", query) or _normalize(found[0].name) == _normalize(query):
        return found[0]
    cands = ", ".join(f"{c.name}({c.stock_code})" for c in found[:8])
    raise DartError(f"'{query}'와 정확히 일치하는 회사가 없습니다. 후보: {cands}. "
                    "의도한 회사가 후보에 없으면 정식 회사명이나 종목코드로 다시 시도하세요.")


# ---------------------------------------------------------------------------
# 데이터 조회
# ---------------------------------------------------------------------------

@lru_cache(maxsize=128)
def company_info(corp_code: str) -> dict:
    return _get_json("company.json", corp_code=corp_code)


@lru_cache(maxsize=256)
def _statements(corp_code: str, year: int, report: str, fs_div: str) -> tuple:
    data = _get_json("fnlttSinglAcntAll.json", corp_code=corp_code, bsns_year=str(year),
                     reprt_code=REPORT_CODES[report], fs_div=fs_div)
    return tuple(data.get("list", []))


def financial_statements(corp_code: str, report: str = "annual", year: int | None = None,
                         consolidated: bool = True) -> tuple[list[dict], int, str]:
    """(계정 목록, 사업연도, 'CFS'|'OFS') 반환. year가 없으면 가장 최근 보고서를 찾는다."""
    if report not in REPORT_CODES:
        raise DartError(f"report는 {list(REPORT_CODES)} 중 하나여야 합니다.")
    years = [year] if year else [date.today().year - i for i in range(0, 3)]
    fs_order = ("CFS", "OFS") if consolidated else ("OFS",)
    for y in years:
        for fs in fs_order:
            rows = _statements(corp_code, y, report, fs)
            if rows:
                return list(rows), y, fs
    raise DartError(f"{years}년 {report} 재무제표를 찾을 수 없습니다.")


def latest_report(corp_code: str) -> tuple[str, int]:
    """가장 최근에 제출된 정기보고서의 (report, 사업연도)."""
    y = date.today().year
    candidates = [(y, "q3"), (y, "half"), (y, "q1"), (y - 1, "annual"), (y - 1, "q3")]  # 최신순
    for year, report in candidates:
        if _statements(corp_code, year, report, "CFS") or _statements(corp_code, year, report, "OFS"):
            return report, year
    return "annual", y - 1


def recent_disclosures(corp_code: str, days: int = 90, limit: int = 20) -> list[dict]:
    end = date.today()
    bgn = date.fromordinal(end.toordinal() - days)
    data = _get_json("list.json", corp_code=corp_code, bgn_de=bgn.strftime("%Y%m%d"),
                     end_de=end.strftime("%Y%m%d"), page_count=str(min(limit, 100)))
    return data.get("list", [])
