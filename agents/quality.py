"""보고서 품질 평가.

보고서가 만들어진 뒤 네 항목을 "부합 여부"로만 판정한다. 내용이 옳은지는 판단하지 않고 형식으로 확인할 수 있는 것만 본다
(인용과 REFERENCE의 대응, 표현, 출처 수, 필수 절). LLM을 쓰지 않으므로 같은 보고서에는 항상 같은 결과가 나온다.

| 항목 | 형식 기준 |
|---|---|
| Groundedness | 본문 인용이 모두 REFERENCE 항목에 대응하고, REFERENCE 항목은 모두 본문에 인용되며,
|              | REFERENCE 항목은 모두 표기 형식에 맞고 그 URL은 도구가 실제로 돌려준 것이며,
|              | 내부 팀원·도구 이름을 출처로 쓰지 않고, 사실 서술 문장의 대부분에 인용이 붙어 있다 |
| 중립성 | 기술의 우열을 판정하거나 특정 기술을 추천하는 표현이 없다 |
| 편향 통제 | 출처가 여럿이고, 한 출처에 인용이 몰리지 않으며, 리스크·한계점에도 근거가 있다 |
| 관점 커버리지 | 기술 성숙도 · 시장성 · 이해관계자 · 도메인 적용 절이 모두 있다 |

단순 질문에 대한 짧은 답변은 Groundedness와 중립성만 판정한다 (나머지는 해당 없음).

판정 전에 tidy 가 REFERENCE 를 본문 인용에 맞춰 정리한다: 인용되지 않은 항목은 빼고, 인용됐는데 빠진 항목은
조사 결과의 출처 목록에서 찾아 넣는다. 기계적으로 맞출 수 있는 것을 LLM에게 다시 쓰게 하지 않기 위해서다.
"""

import re

from . import report as report_format
from .report import REFERENCE, _sections

GROUNDEDNESS, NEUTRALITY, BIAS, COVERAGE = "Groundedness", "중립성", "편향 통제", "관점 커버리지"
ITEMS = (GROUNDEDNESS, NEUTRALITY, BIAS, COVERAGE)

# 관점 커버리지: 보고서에 이 이름의 절(###)이 있어야 한다
PERSPECTIVES = ("기술 성숙도", "시장성", "이해관계자", "도메인 적용")
MIN_PERSPECTIVE_CHARS = 80

MIN_CITED_RATIO = 0.7  # 사실 서술 줄 가운데 인용이 붙어 있어야 하는 비율
MIN_CLAIM_CHARS = 40  # 이보다 짧은 줄은 사실 서술로 세지 않는다
MIN_SOURCES = 5  # 서로 다른 출처 수
MAX_SOURCE_SHARE = 0.4  # 한 출처가 본문 인용에서 차지할 수 있는 비율
MIN_RISK_CITATIONS = 2  # 리스크 장의 인용 수
RISK_CHAPTER, LIMIT_CHAPTER = "7. 사업 리스크", "8. 한계점"
# 인용이 필요 없는 장: 요약, 스코어카드(근거는 표 안에 있음), 한계점
UNCITED_CHAPTERS = ("SUMMARY", "6. 투자 평가", LIMIT_CHAPTER)
ALWAYS_RETRIEVABLE = ("dart.fss.or.kr",)  # 공시 조회 도구는 URL 없이 결과를 주기도 한다
# 출처가 될 수 없는 이름: 조사한 팀원이나 도구가 아니라 그들이 찾은 원자료를 인용해야 한다
INTERNAL_NAMES = ("company_researcher", "tech_researcher", "market_researcher", "evaluator", "writer", "supervisor",
                  "researcher", "web_search", "search_", "calculate_", "score_investment", "get_", "calculator",
                  "내부", "도구", "팀원", "평가자", "심사역", "스코어카드", "internal", "scorecard")
# 본문에서 흔히 쓰는 다른 이름 (REFERENCE 의 이름 → 다른 이름)
COMMON_ALIASES = {"금융감독원 전자공시시스템": ("DART", "OpenDART", "금융감독원", "전자공시시스템", "전자공시")}

# 기술의 우열 판정·추천 표현
_NON_NEUTRAL = re.compile(
    r"최고의|세계 최고|업계 최고|가장 우수|가장 뛰어난|가장 앞선|가장 진보|독보적|압도적|월등|우월|"
    r"보다 우수|보다 뛰어난|보다 앞선|추천|반드시 도입|유일한 대안|"
    r"best[- ]in[- ]class|\bsuperior\b|unrivall?ed|unmatched", re.I)

_YEAR = r"(?:\d{4}|n\.d\.)"
_PAREN = re.compile(r"\(([^()]*)\)")
_CITE = re.compile(rf"^(.+?),\s*{_YEAR}$")
_REF = re.compile(r"^- (.+?)\((?:\d{4}(?:-\d{2}-\d{2})?|n\.d\.)\)\. ")
_URL = re.compile(r"https?://[^\s<>\"'|)\]]+")
_STOPWORDS = {"for", "and", "of", "the", "on", "in"}


def urls(text: str) -> set[str]:
    """텍스트에 나온 URL (비교할 수 있게 끝의 구두점과 슬래시를 뗀다)."""
    return {u.rstrip(".,;:/*") for u in _URL.findall(text)}


def _key(name: str) -> str:
    return re.sub(r"[\W_]+", "", name.lower())


def _aliases() -> dict[str, set[str]]:
    """문서 풀 보고서의 {발행기관: 본문에서 쓰일 수 있는 다른 이름}."""
    aliases = {_key(name): {_key(a) for a in others} for name, others in COMMON_ALIASES.items()}
    aliases.update({_key(d["source"]): {_key(a) for a in d.get("aliases", [])}
                    for d in report_format.pool_docs() if d.get("source")})
    return aliases


def _same_source(cited: str, reference: str) -> bool:
    """본문의 "(이름, 연도)"가 REFERENCE 항목의 맨 앞 이름을 가리키는가. 줄임말과 등록된 다른 이름도 인정한다."""
    a, b = _key(cited), _key(reference)
    if not a or not b:
        return False
    acronym = "".join(w[0] for w in re.findall(r"[A-Za-z]+", reference) if w.lower() not in _STOPWORDS).lower()
    return (a == b or (len(a) >= 3 and a in b) or (len(b) >= 3 and b in a) or a == acronym
            or any(a == alias or (len(alias) >= 3 and alias in a) for alias in _aliases().get(b, ())))


def _cited_pairs(text: str) -> list[tuple[str, str]]:
    """본문의 "(이름, 연도)" 또는 "(이름, 연도; 이름, 연도)" 인용 → [(이름, 연도)]."""
    pairs = []
    for group in _PAREN.findall(text):
        parts = [_CITE.match(p.strip()) for p in group.split(";")]
        if all(parts):
            pairs += [(m.group(1).strip(), m.group(0).rsplit(",", 1)[1].strip()) for m in parts]
    return pairs


def _citations(text: str) -> list[str]:
    return [name for name, _ in _cited_pairs(text)]


def _entry(line: str) -> tuple[str, str] | None:
    """REFERENCE 표기 형식에 맞는 줄이면 (맨 앞 이름, "- …" 항목)."""
    line = "- " + re.sub(r"^[-*•\d.)\s]*(출처:\s*)?(\[[^\]]+\]\s*)?", "", line.strip()).rstrip()
    if (m := _REF.match(line)) and any(pattern.match(line) for _, pattern in report_format.REFERENCE_FORMATS.values()):
        return m.group(1).strip(), line
    return None


def tidy(report: str, findings_text: str, retrieved: set[str]) -> str:
    """REFERENCE 를 본문 인용에 맞춘다: 인용되지 않은 항목은 빼고, 빠진 항목은 조사 결과의 출처 목록에서 찾아 넣는다.

    넣는 항목은 조사 결과에 표기 형식대로 적혀 있고 URL이 도구가 돌려준 것일 때만이다. 본문은 건드리지 않는다.
    """
    match = re.search(rf"^## +{REFERENCE}[ \t]*$", report, flags=re.M)
    if not match:
        return report
    body, section = report[:match.start()], report[match.end():]
    cited = _cited_pairs(body)
    known = {u.lower() for u in retrieved}
    kept = [e for ln in section.splitlines() if ln.strip().startswith("- ") and (e := _entry(ln) or (None, ln.strip()))]
    kept = [(name, line) for name, line in kept
            if name is None or any(_same_source(c, name) for c, _ in cited)]  # 형식이 틀린 항목은 판정에 맡긴다
    catalog = [e for ln in [*report_format.pool_references().values(), *findings_text.splitlines()] if (e := _entry(ln))]
    catalog = [(name, line) for name, line in catalog
               if all(u.lower() in known or any(d in u for d in ALWAYS_RETRIEVABLE) for u in urls(line))]
    for name, year in dict.fromkeys(cited):
        if any(n and _same_source(name, n) for n, _ in kept):
            continue
        candidates = [e for e in catalog if _same_source(name, e[0])]
        same_year = [e for e in candidates if f"({year}" in e[1]]
        kept += (same_year or candidates[:1])
    lines = list(dict.fromkeys(line for _, line in kept))
    return report_format.normalize_references(body.rstrip() + f"\n\n## {REFERENCE}\n" + "\n".join(lines) + "\n")


def _claims(text: str) -> list[str]:
    """사실 서술로 볼 줄: 제목·표·짧은 줄·"확인되지 않음"은 뺀다."""
    lines = (ln.strip() for ln in text.splitlines())
    return [ln for ln in lines if len(ln) >= MIN_CLAIM_CHARS and not ln.startswith(("#", "|"))
            and "확인되지 않" not in ln]


def _subsections(text: str) -> dict[str, str]:
    parts = re.split(r"^### +(.+?)\s*$", text, flags=re.M)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts), 2)}


def evaluate(report: str, full: bool, retrieved: set[str]) -> dict:
    """{"passed": bool, "items": {항목: True | False | None(해당 없음)}, "issues": [미달 사유]}

    full: 투자 평가 보고서인가 (아니면 단순 질문에 대한 짧은 답변)
    retrieved: 조사 과정에서 도구가 실제로 돌려준 URL
    """
    sections = _sections(report)
    chapters = dict(sections)
    head = re.split(r"^## ", report, maxsplit=1, flags=re.M)[0]
    body = head + "\n".join(text for title, text in sections if title != REFERENCE)
    prose = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("|"))  # 원본 그대로 옮긴 표는 뺀다
    ref_lines = [ln.strip() for ln in chapters.get(REFERENCE, "").splitlines() if ln.strip().startswith("- ")]
    refs = [(m.group(1).strip(), ln) for ln in ref_lines if (m := _REF.match(ln))]
    cited = _citations(body)
    issues: dict[str, list[str]] = {item: [] for item in ITEMS}

    # ── Groundedness: 주장이 검색된 출처로 추적되는가 ─────────────────────────
    g = issues[GROUNDEDNESS]
    g += [v for v in report_format.violations(report, full=False)]  # REFERENCE 표기 형식 (URL 포함 여부까지)
    internal = sorted({n for n in [*cited, *(name for name, _ in refs)]
                       if any(word in n.lower() for word in INTERNAL_NAMES)})
    if internal:
        g.append(f"내부 팀원·도구 이름은 출처가 아닙니다: {', '.join(internal[:8])}. "
                 "그 결과에 적힌 원자료(기관명, 연도)를 인용하고, 원자료가 없는 서술은 빼세요.")
    unmatched = sorted({c for c in cited if c not in internal and not any(_same_source(c, name) for name, _ in refs)})
    if unmatched:
        g.append(f"본문 인용이 REFERENCE 에 없습니다: {', '.join(unmatched[:8])}. REFERENCE 항목의 맨 앞 이름과 같게 쓰세요. "
                 "대화의 출처 목록에 URL과 함께 있는 자료만 REFERENCE 에 넣을 수 있고, 없는 자료라면 그 서술을 빼세요.")
    unused = [name for name, _ in refs if not any(_same_source(c, name) for c in cited)]
    if unused:
        g.append(f"본문에 인용되지 않은 REFERENCE 항목이 있습니다: {', '.join(unused[:8])}.")
    known = {u.lower() for u in retrieved}
    unknown = sorted(u for ln in ref_lines for u in urls(ln)
                     if u.lower() not in known and not any(d in u for d in ALWAYS_RETRIEVABLE))
    if unknown:
        g.append(f"조사 결과에 없는 URL이 REFERENCE 에 있습니다: {', '.join(unknown[:5])}. "
                 "대화에 나온 URL을 그대로 쓰고, 없는 자료는 본문 인용과 함께 빼세요.")
    claims = _claims("\n".join(text for title, text in sections if title not in (*UNCITED_CHAPTERS, REFERENCE))
                     if full else body)
    uncited = [c for c in claims if not _citations(c)]
    if claims and 1 - len(uncited) / len(claims) < MIN_CITED_RATIO:
        g.append(f"사실 서술 {len(claims)}줄 중 {len(uncited)}줄에 출처 인용 \"(기관명, 연도)\"가 없습니다 "
                 f"({MIN_CITED_RATIO:.0%} 이상 필요). 예: {uncited[0][:60]}…")

    # ── 중립성: 기술의 우열 판정·추천 표현이 없는가 ───────────────────────────
    found = sorted({m.group(0) for m in _NON_NEUTRAL.finditer(prose)})
    if found:
        issues[NEUTRALITY].append(f"기술의 우열을 판정하거나 추천하는 표현이 있습니다: {', '.join(found)}. "
                                  "평가어를 빼고 확인된 사실만 서술하세요.")

    applicable = ITEMS if full else (GROUNDEDNESS, NEUTRALITY)
    if full:
        # ── 편향 통제: 단일 출처·유리한 근거 편중이 없는가 ────────────────────
        b = issues[BIAS]
        sources = {_key(name) for name, _ in refs}
        if len(sources) < MIN_SOURCES:
            b.append(f"서로 다른 출처가 {len(sources)}개입니다 ({MIN_SOURCES}개 이상 필요). 대화에 있는 다른 출처의 근거도 인용하세요.")
        counts = {name: sum(_same_source(c, name) for c in cited) for name, _ in refs}
        if cited and counts and len(cited) >= MIN_SOURCES:
            top = max(counts, key=counts.get)
            if counts[top] / len(cited) > MAX_SOURCE_SHARE:
                b.append(f"본문 인용 {len(cited)}건 중 {counts[top]}건이 '{top}' 한 출처입니다 "
                         f"({MAX_SOURCE_SHARE:.0%} 이하여야 함). 다른 출처의 근거로 교차 확인해 인용을 분산하세요.")
        if len(_citations(chapters.get(RISK_CHAPTER, ""))) < MIN_RISK_CITATIONS:
            b.append(f"'{RISK_CHAPTER}'에 출처가 달린 근거가 {MIN_RISK_CITATIONS}건 미만입니다. 부정적 근거도 출처와 함께 적으세요.")
        if not _claims(chapters.get(LIMIT_CHAPTER, "")) and "확인되지 않" not in chapters.get(LIMIT_CHAPTER, ""):
            b.append(f"'{LIMIT_CHAPTER}'이 비어 있습니다.")

        # ── 관점 커버리지: 네 관점을 포괄하는가 ───────────────────────────────
        subsections = {}
        for text in chapters.values():
            subsections.update(_subsections(text))
        absent = [p for p in PERSPECTIVES
                  if len(next((t for name, t in subsections.items() if name.startswith(p)), "").strip()) < MIN_PERSPECTIVE_CHARS]
        if absent:
            issues[COVERAGE].append(f"다음 관점의 절(###)이 없거나 내용이 {MIN_PERSPECTIVE_CHARS}자 미만입니다: {', '.join(absent)}.")

    items = {item: (not issues[item] if item in applicable else None) for item in ITEMS}
    return {"passed": all(v is not False for v in items.values()), "items": items,
            "issues": [f"[{item}] {text}" for item in applicable for text in issues[item]]}
