"""투자 보고서 형식 검사.

보고서는 SUMMARY 로 시작해 REFERENCE 로 끝나고, REFERENCE 는 자료 종류별 표기 형식을 따른다.
구성은 최대 10장이다: 장(##)은 SUMMARY 와 REFERENCE 를 포함해 10개로 고정이고, PDF 분량도 10쪽을 넘지 않는다.
형식은 프롬프트로 지시하지만 LLM이 어길 수 있으므로 writer가 낸 결과를 여기서 다듬고 검사한다.
- normalize_references: 코드로 고칠 수 있는 것(자료 종류별 분류, 문서 풀 보고서의 표기)을 고친다.
- violations: 남은 위반 사항을 찾아 writer가 고쳐 쓰도록 되돌려 준다.
"""

import json
import re

from . import pdf
from .dart import DATA_DIR

SUMMARY, REFERENCE = "SUMMARY", "REFERENCE"
# 투자 평가 보고서의 장 제목 (순서 고정)
CHAPTERS = (SUMMARY, "1. 사업 아이디어", "2. 팀 구성", "3. 기술 분석", "4. 시장 규모 및 경쟁 구도",
            "5. 사업화 및 재무", "6. 투자 평가", "7. 사업 리스크", "8. 한계점", REFERENCE)
MAX_CHAPTERS = 10  # 보고서 구성 상한. 장을 추가하려면 다른 장을 합쳐야 한다
MAX_PAGES = 10  # PDF 분량 상한 (A4)
assert len(CHAPTERS) <= MAX_CHAPTERS
RISK_SECTIONS = ("시장", "기술", "규제", "경쟁")
SUMMARY_MAX_CHARS = 700  # 반 페이지 분량

_DATE = r"(\d{4}-\d{2}-\d{2}|\d{4}|n\.d\.)"
# 자료 종류 → (표기 형식, 정규식)
REFERENCE_FORMATS = {
    "기관 보고서": ("발행기관(YYYY). *보고서명*. URL",
               re.compile(r"^- .+\((\d{4}|n\.d\.)\)\. \*.+\*\. https?://\S+$")),
    "학술 논문": ("저자(YYYY). 논문제목. *학술지명*, 권(호), 페이지.",
              re.compile(r"^- .+\(\d{4}\)\. .+\. \*.+\*, .+\.$")),
    "웹페이지": ("기관명 또는 작성자(YYYY-MM-DD). *제목*. 사이트명, URL",
             re.compile(rf"^- .+\({_DATE}\)\. \*.+\*\. .+, https?://\S+$")),
}


def pool_docs() -> list[dict]:
    """문서 풀 보고서의 서지 정보 (data/docs/metadata.json)."""
    path = DATA_DIR / "docs" / "metadata.json"
    return list(json.loads(path.read_text(encoding="utf-8")).values()) if path.exists() else []


def pool_references() -> dict[str, str]:
    """문서 풀 보고서의 {URL: REFERENCE 항목}."""
    return {d["url"]: f"- {d['source']}({d['date'][:4]}). *{d['title']}*. {d['url']}"
            for d in pool_docs() if d.get("url") and d.get("source") and d.get("title") and d.get("date")}


def normalize_references(report: str) -> str:
    """REFERENCE 의 항목을 자료 종류별 소제목 아래로 다시 묶고, 문서 풀 보고서는 서지 정보대로 표기한다.

    항목의 내용은 바꾸지 않는다. 형식이 맞지 않는 항목은 그대로 남겨 violations 가 잡게 한다.
    """
    match = re.search(rf"^## +{REFERENCE}[ \t]*$", report, flags=re.M)
    if not match:
        return report
    pool = pool_references()
    groups: dict[str, list[str]] = {kind: [] for kind in REFERENCE_FORMATS}
    kind, under_heading = "웹페이지", False
    for line in (ln.strip() for ln in report[match.end():].splitlines()):
        if line.startswith("### "):
            under_heading = line[4:].strip() in REFERENCE_FORMATS
            kind = line[4:].strip() if under_heading else "웹페이지"
            continue
        if not line or line.startswith("#"):
            continue
        line = "- " + re.sub(r"^([-*•]|\d+[.)])\s+", "", line)
        line = re.sub(r"\.\*(?= )", "*.", line)  # 마침표로 끝나는 제목: "*제목.* 사이트" → "*제목*. 사이트"
        line = re.sub(rf"\(\s*{_DATE}\s*\)", r"(\1)", line, count=1)  # "( n.d.)" → "(n.d.)"
        if under_heading and kind == "웹페이지" and not REFERENCE_FORMATS["웹페이지"][1].match(line):
            # 웹페이지인데 사이트명이 빠진 항목: 맨 앞의 기관명을 사이트명으로 쓴다
            line = re.sub(rf"^- (.+?)\({_DATE}\)\. (\*.+\*)\. (https?://\S+)$", r"- \1(\2). \3. \1, \4", line)
        url = next((u for u in pool if u in line), None)
        if url:
            line, target = pool[url], "기관 보고서"
        else:
            # 표기 형식으로 종류를 알 수 있으면 그 종류로, 아니면 원래 있던 소제목 아래에 둔다
            target = next((k for k in ("웹페이지", "학술 논문", "기관 보고서") if REFERENCE_FORMATS[k][1].match(line)), kind)
        if line not in groups[target]:
            groups[target].append(line)
    body = "\n\n".join(f"### {k}\n" + "\n".join(lines) for k, lines in groups.items() if lines)
    return report[:match.end()] + "\n\n" + body + "\n"


def _sections(report: str) -> list[tuple[str, str]]:
    """[(장 제목, 본문)] — '## ' 제목 기준."""
    parts = re.split(r"^## +(.+?)\s*$", report, flags=re.M)
    return [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts), 2)]


def violations(report: str, full: bool) -> list[str]:
    """형식 위반 목록. full 이면 투자 평가 보고서의 목차 전체를, 아니면 REFERENCE 만 검사한다."""
    found = []
    sections = _sections(report)
    titles = [t for t, _ in sections]
    body = dict(sections)

    if len(titles) > MAX_CHAPTERS:
        found.append(f"장(##)이 {len(titles)}개입니다. {MAX_CHAPTERS}개를 넘을 수 없습니다.")
    pages = pdf.page_count(report)
    if pages > MAX_PAGES:
        found.append(f"보고서가 PDF로 {pages}쪽입니다. {MAX_PAGES}쪽 이내가 되도록 핵심만 남기고 줄이세요 "
                     "(표와 REFERENCE 는 유지).")

    if full:
        if titles != list(CHAPTERS):
            found.append(f"장(##) 제목과 순서가 다릅니다. 정확히 다음과 같아야 하며 다른 장을 추가할 수 없습니다: "
                         f"{' / '.join(CHAPTERS)}. 현재: {' / '.join(titles) or '없음'}")
        summary = body.get(SUMMARY, "").strip()
        if len(summary) > SUMMARY_MAX_CHARS:
            found.append(f"SUMMARY 가 {len(summary)}자입니다. {SUMMARY_MAX_CHARS}자 이내(반 페이지)로 줄이세요.")
        if re.search(r"^\|", summary, flags=re.M):
            found.append("SUMMARY 에는 표를 넣지 않습니다.")
        risks = re.findall(r"^### +(.+?)\s*$", body.get("7. 사업 리스크", ""), flags=re.M)
        absent = [r for r in RISK_SECTIONS if not any(h.startswith(r) for h in risks)]
        if absent:
            found.append(f"'7. 사업 리스크'에 다음 소제목(###)이 없습니다: {', '.join(absent)}")

    if not titles or titles[-1] != REFERENCE:
        found.append("보고서의 마지막 장은 '## REFERENCE' 여야 하고 그 뒤에 다른 내용이 올 수 없습니다.")
        return found

    kind, count = None, 0
    for line in (ln.strip() for ln in body[REFERENCE].splitlines()):
        if not line:
            continue
        if line.startswith("### "):
            kind = line[4:].strip()
            if kind not in REFERENCE_FORMATS:
                found.append(f"REFERENCE 의 소제목은 {', '.join(REFERENCE_FORMATS)} 중 하나여야 합니다: '{kind}'")
                kind = None
        elif kind is None:
            found.append(f"REFERENCE 의 항목은 자료 종류 소제목(### {' / '.join(REFERENCE_FORMATS)}) 아래에 있어야 합니다: {line[:60]}")
        else:
            count += 1
            form, pattern = REFERENCE_FORMATS[kind]
            if not pattern.match(line):
                found.append(f"REFERENCE {kind} 표기 형식이 다릅니다. 형식: '- {form}' (날짜를 모르면 n.d.). 현재: {line[:120]}")
    if count == 0:
        found.append("REFERENCE 에 자료가 없습니다. 본문에 인용한 자료를 형식에 맞게 적으세요.")
    return found
