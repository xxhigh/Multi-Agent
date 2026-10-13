"""관측성 계층: trace_id, 결정 로그, 근거(Evidence) 원문 저장.

- 결정 로그는 State가 아니라 여기서 파일(JSONL)로 남긴다 → State 무한 증식 방지.
- 모든 기록에 trace_id가 붙고, 같은 trace_id를 LangSmith run metadata에도 넣어 서로 잇는다.
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .tools import PROVIDER_BY_TYPE

log = logging.getLogger("finance_agent")

_DART_URL = re.compile(r"https://dart\.fss\.or\.kr/\S+")


def new_trace_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def run_dir(trace_id: str) -> Path:
    path = config.RUN_DIR / trace_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_decision(trace_id: str, node: str, decision: str, reason: str, **extra) -> dict:
    """라우팅·평가 결정과 그 사유를 decisions.jsonl에 한 줄로 적재."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trace_id": trace_id,
        "node": node,
        "decision": decision,
        "reason": reason,
        **extra,
    }
    with (run_dir(trace_id) / "decisions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    log.info("[%s] %s → %s (%s)", trace_id, node, decision, reason)
    return record


def read_decisions(trace_id: str) -> list[dict]:
    path = config.RUN_DIR / trace_id / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ── 근거 원문 저장 ──────────────────────────────────────────────


def save_evidence(trace_id: str, evidence_id: str, ev_type: str, tool: str, content: str,
                  collected_by: str) -> dict:
    """도구 결과 원문을 파일로 저장하고 State에 넣을 참조 메타데이터를 돌려준다."""
    folder = run_dir(trace_id) / "evidence"
    folder.mkdir(exist_ok=True)
    path = folder / f"{evidence_id}.md"
    path.write_text(content, encoding="utf-8")
    first_line = content.strip().splitlines()[0] if content.strip() else tool
    title = first_line.strip("[] ").split("]")[0][:120]
    url_match = _DART_URL.search(content)
    return {
        "id": evidence_id,
        "type": ev_type,
        "provider": PROVIDER_BY_TYPE.get(ev_type, ev_type),
        "tool": tool,
        "title": title,
        "url": url_match.group(0) if url_match else None,
        "path": str(path),
        "collected_by": collected_by,
    }


def load_evidence(path: str, limit: int | None = None) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return text if limit is None else text[:limit]


def write_summary(trace_id: str, state: dict) -> Path:
    """실행 종료 후 재현·제출용 요약(run-summary.json)을 남긴다."""
    decisions = read_decisions(trace_id)
    routes = [d["decision"] for d in decisions if d["node"] == "supervisor"]
    summary = {
        "trace_id": trace_id,
        "company": state.get("company"),
        "status": state.get("status"),
        "step_count": state.get("step_count"),
        "revision_count": state.get("revision_count"),
        "attempts": state.get("attempts"),
        "route_sequence": routes,
        "rework_dispatches": sum(1 for d in decisions if d.get("is_rework")),
        "evidence_count": len(state.get("evidence") or []),
        "eval_passed": (state.get("eval_result").passed if state.get("eval_result") else None),
        "node_status": state.get("node_status"),
        "last_error": state.get("last_error"),
    }
    path = run_dir(trace_id) / "run-summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path
