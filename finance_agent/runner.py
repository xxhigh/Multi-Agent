"""실행 공통 부분 (CLI·API가 함께 쓴다): 초기 State, config, 산출물 저장."""

import logging
from pathlib import Path

from . import config
from .observability import new_trace_id, run_dir, write_summary

log = logging.getLogger(__name__)


def initial_state(question: str, trace_id: str) -> dict:
    return {
        "messages": [("user", question)],
        "question": question,
        "company": "",
        "perspectives": {},
        "evidence": [],
        "report": None,
        "eval_result": None,
        "trace_id": trace_id,
        "next": None,
        "instruction": "",
        "target_perspectives": [],
        "step_count": 0,
        "max_steps": config.MAX_STEPS,
        "attempts": {},
        "revision_count": 0,
        "node_status": {},
        "last_error": None,
        "status": "running",
    }


def run_config(trace_id: str, thread_id: str | None = None) -> dict:
    """LangSmith run에도 trace_id를 metadata로 실어 결정 로그·근거 파일과 연결한다."""
    return {
        "configurable": {"thread_id": thread_id or trace_id},
        "recursion_limit": config.RECURSION_LIMIT,
        "run_name": "finance_supervisor",
        "metadata": {"trace_id": trace_id},
        "tags": ["supervisor-pattern", f"trace:{trace_id}"],
    }


def new_run(question: str, thread_id: str | None = None) -> tuple[str, dict, dict]:
    trace_id = new_trace_id()
    return trace_id, initial_state(question, trace_id), run_config(trace_id, thread_id)


def save_outputs(trace_id: str, state: dict) -> dict[str, Path]:
    """보고서(.md + 제출용 .pdf)와 실행 요약(run-summary.json)을 저장."""
    out = {"summary": write_summary(trace_id, state)}
    report = state.get("report")
    if report and state.get("status") != "out_of_scope":
        config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = config.REPORT_DIR / f"report_{trace_id}.md"
        path.write_text(report, encoding="utf-8")
        (run_dir(trace_id) / "report.md").write_text(report, encoding="utf-8")
        out["report"] = path
        try:
            from .pdf_export import markdown_to_pdf
            out["pdf"] = markdown_to_pdf(report, path.with_suffix(".pdf"))
        except Exception as e:  # noqa: BLE001 - PDF 변환 실패가 보고서 저장을 막지 않게 한다
            log.warning("PDF 변환 실패 (trace_id=%s): %s", trace_id, e)
    return out
