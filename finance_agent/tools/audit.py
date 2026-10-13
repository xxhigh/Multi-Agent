"""DART 감사보고서 처리 (비상장 스타트업용).

비상장 외부감사 대상 기업은 사업보고서가 없고 감사보고서만 공시한다. 여기서는
1) 최신 감사보고서를 찾고 원문(XML)을 내려받아 디스크에 캐시하고,
2) 재무제표 표에서 핵심 계정을 '규칙'으로 뽑고 (LLM이 숫자를 읽지 않게),
3) 감사의견·주석 본문을 RAG 검색용 섹션으로 나눈다.
"""

import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

from . import dart
from .rag import _download_report, _to_text

AUDIT_CACHE = dart.CACHE_DIR / "audit"
LOOKBACK_YEARS = 3

# 표시명 → 재무제표 행 이름 후보 (공백 제거 후 비교, 앞쪽 후보 우선)
ACCOUNTS: dict[str, tuple[str, ...]] = {
    "매출액": ("매출액", "수익(매출액)", "영업수익", "매출"),
    "매출총이익": ("매출총이익", "매출총손실", "매출총이익(손실)"),
    "영업이익": ("영업이익", "영업손실", "영업이익(손실)", "영업손익"),
    "당기순이익": ("당기순이익", "당기순손실", "당기순이익(손실)", "당기순손익"),
    "자산총계": ("자산총계",),
    "부채총계": ("부채총계",),
    "자본총계": ("자본총계",),
    "자본금": ("자본금",),
    "유동자산": ("유동자산",),
    "유동부채": ("유동부채",),
    "현금및현금성자산": ("현금및현금성자산",),
    "영업활동현금흐름": ("영업활동현금흐름", "영업활동으로인한현금흐름", "영업활동으로부터의현금흐름"),
}

SECTIONS = {
    "손익계산서": ["매출액", "매출총이익", "영업이익", "당기순이익"],
    "재무상태표": ["자산총계", "부채총계", "자본총계", "자본금", "유동자산", "유동부채", "현금및현금성자산"],
    "현금흐름표": ["영업활동현금흐름"],
}

UNITS = {"원": 1, "천원": 1_000, "백만원": 1_000_000}
_AMOUNT = re.compile(r"^\(?-?\d{1,3}(?:,\d{3})+\)?$|^\(?-?\d{4,}\)?$")
_LABEL_PREFIX = re.compile(r"^(?:[IVXⅠ-Ⅻ]+\.|\d+\.|\(\d+\)|[가-하]\.)\s*")


@dataclass(frozen=True)
class AuditReport:
    rcept_no: str
    report_nm: str
    rcept_dt: str

    @property
    def url(self) -> str:
        return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={self.rcept_no}"

    @property
    def fiscal(self) -> str:
        m = re.search(r"\((\d{4}\.\d{2})\)", self.report_nm)
        return m.group(1) if m else self.rcept_dt[:4]


def latest_audit_report(corp_code: str) -> AuditReport | None:
    """최근 3년 공시 중 가장 최근 회계연도의 감사보고서. 같은 연도면 연결감사보고서를 우선한다."""
    end = date.today()
    bgn = date(end.year - LOOKBACK_YEARS, 1, 1)
    data = dart._get_json("list.json", corp_code=corp_code, bgn_de=bgn.strftime("%Y%m%d"),
                          end_de=end.strftime("%Y%m%d"), page_count="100")
    reports = [AuditReport(it["rcept_no"], it["report_nm"].strip(), it["rcept_dt"])
               for it in data.get("list", []) if "감사보고서" in it["report_nm"]]
    if not reports:
        return None
    reports.sort(key=lambda r: (r.fiscal, "연결" in r.report_nm, r.rcept_dt), reverse=True)
    return reports[0]


def audit_xml(rcept_no: str) -> str:
    """감사보고서 원문 XML (디스크 캐시)."""
    path = AUDIT_CACHE / f"{rcept_no}.xml"
    if path.exists():
        return path.read_text(encoding="utf-8")
    xml = _download_report(rcept_no)
    AUDIT_CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-8")
    return xml


def split_sections(xml: str) -> dict[str, str]:
    """{제목: 텍스트} — 감사의견, 재무제표, 주석 등 목차 단위."""
    titles = list(re.finditer(r"<TITLE[^>]*>(.*?)</TITLE>", xml, re.S))
    out = {}
    for i, m in enumerate(titles):
        title = re.sub(r"\s+", "", _to_text(m.group(1)))
        end = titles[i + 1].start() if i + 1 < len(titles) else len(xml)
        out[title] = _to_text(xml[m.end():end])
    return out


def _amount(cell: str) -> float | None:
    c = cell.replace(" ", "")
    if not _AMOUNT.match(c):
        return None
    neg = c.startswith("(") or c.startswith("-")
    v = float(re.sub(r"[^\d]", "", c))
    return -v if neg else v


def _norm_label(cell: str) -> str:
    return re.sub(r"\s+", "", _LABEL_PREFIX.sub("", cell.strip()))


def extract_financials(statements_text: str) -> tuple[dict[str, tuple[float | None, float | None]], int]:
    """재무제표 텍스트(표는 '셀 | 셀' 행)에서 계정별 (당기, 전기) 값을 원 단위로 뽑는다."""
    unit_m = re.search(r"단위\s*:\s*(백만원|천원|원)", statements_text)
    unit = UNITS[unit_m.group(1)] if unit_m else 1
    rows = []
    for line in statements_text.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 2:
            continue
        amounts = [a for a in (_amount(c) for c in cells[1:]) if a is not None]
        if amounts:
            rows.append((_norm_label(cells[0]), amounts))
    found: dict[str, tuple[float | None, float | None]] = {}
    for key, names in ACCOUNTS.items():
        targets = [n.replace(" ", "") for n in names]
        for name in targets:
            hit = next((a for label, a in rows if label == name), None)
            if hit:
                cur = hit[0] * unit
                prev = hit[1] * unit if len(hit) > 1 else None
                # '영업손실'처럼 이름이 손실이고 괄호 없이 양수로 적힌 경우 부호를 맞춘다
                if "손실" in name and "이익" not in name:
                    cur = -abs(cur)
                    prev = -abs(prev) if prev is not None else None
                found[key] = (cur, prev)
                break
    return found, unit


def going_concern_flag(sections: dict[str, str]) -> str | None:
    """감사의견 본문에서 '계속기업 관련 중요한 불확실성' 강조 문단이 있으면 해당 문장을 돌려준다."""
    opinion = next((t for k, t in sections.items() if "감사인의감사보고서" in k), "")
    # 감사인 책임 설명(모든 보고서에 들어가는 정형 문구)과 구분하려고 강조 문단 제목만 찾는다
    m = re.search(r"계속기업\s*관련\s*중요한\s*불확실성[^\n]{0,300}", opinion)
    return m.group(0).strip() if m else None


def keyword_counts(sections: dict[str, str], words: tuple[str, ...]) -> dict[str, int]:
    """주석에서 투자조건 관련 키워드(예: 상환전환우선주) 등장 횟수."""
    notes = "\n".join(t for k, t in sections.items() if "주석" in k)
    return {w: notes.count(w) for w in words if notes.count(w)}


@lru_cache(maxsize=32)
def load(corp_code: str) -> tuple[AuditReport, dict[str, str]] | None:
    report = latest_audit_report(corp_code)
    if report is None:
        return None
    return report, split_sections(audit_xml(report.rcept_no))


def statements_text(sections: dict[str, str]) -> str:
    return next((t for k, t in sections.items() if "재무제표" in k and "주석" not in k), "")


def rag_sections(sections: dict[str, str]) -> list[tuple[str, str]]:
    """RAG 대상: 감사의견(계속기업 등) + 주석. 재무제표 숫자 표는 규칙 추출로 따로 다룬다."""
    keep = ("감사인의감사보고서", "주석")
    return [(k, t) for k, t in sections.items() if any(s in k for s in keep) and len(t) > 50]
