"""재무 분석 agent HTTP API.

실행 (프로젝트 루트에서):
    uvicorn backend.app:app --reload --port 8000

- POST /api/analyze        : 질문을 받아 agent 진행 상황을 SSE로 스트리밍
- GET  /api/reports        : 저장된 리포트 목록
- GET  /api/reports/{name} : 리포트 본문
- GET  /api/reports/{name}/pdf : 리포트 PDF 다운로드 (없으면 생성)
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from finance_agent import build_graph  # noqa: E402
from finance_agent.export import REPORT_DIR, save_report, to_pdf  # noqa: E402
from finance_agent.observability import run_config  # noqa: E402

REPORT_NAME = re.compile(r"^report_\d{8}_\d{6}\.md$")
TOOL_RESULT_PREVIEW = 300

log = logging.getLogger("uvicorn.error")

app = FastAPI(title="Finance Agent API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# InMemorySaver를 공유하므로 같은 thread_id로 후속 질문을 이어갈 수 있다.
graph = build_graph()


class AnalyzeRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    thread_id: str | None = None


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _events(question: str, thread_id: str):
    """graph.stream 업데이트를 프론트가 그리기 쉬운 SSE 이벤트로 변환."""
    trace_id = uuid.uuid4().hex
    config = run_config(thread_id, trace_id, question)
    yield _sse("start", {"thread_id": thread_id, "trace_id": trace_id})
    try:
        stream = graph.stream({"messages": [("user", question)], "trace_id": trace_id}, config,
                              stream_mode="updates", subgraphs=True)
        for namespace, update in stream:
            for node, data in update.items():
                if not data:
                    continue
                if namespace:  # researcher/analyst 서브그래프 내부
                    agent = namespace[0].split(":")[0]
                    msg = data["messages"][-1]
                    if node == "agent" and msg.tool_calls:
                        for call in msg.tool_calls:
                            yield _sse("tool_call", {"agent": agent, "id": call["id"], "name": call["name"],
                                                     "args": call["args"]})
                    elif node == "tools":
                        for m in data["messages"]:
                            yield _sse("tool_result", {"agent": agent, "id": m.tool_call_id,
                                                       "preview": str(m.content)[:TOOL_RESULT_PREVIEW]})
                elif node == "supervisor":
                    d, s = data["decision"], data["sufficiency"]
                    yield _sse("route", {"next": d["next"], "instruction": d["instruction"], "reason": d["reason"],
                                         "override": d["override"], "missing": s["missing"],
                                         "step": data["step_count"]})
                elif node in ("researcher", "analyst"):
                    if data.get("node_status", {}).get(node) == "failed":
                        yield _sse("worker_done", {"agent": node, "chars": 0, "summary": data.get("last_error", ""),
                                                   "failed": True})
                    else:
                        f = data["findings"][node]
                        yield _sse("worker_done", {"agent": node, "chars": len(f["content"]), "summary": f["content"],
                                                   "attempt": f["attempt"]})
                elif node == "writer":
                    yield _sse("worker_done", {"agent": "writer", "chars": len(data["report"]), "summary": ""})
                elif node == "evaluator":
                    ev = data["eval_result"]
                    judge = ev.get("judge") or {}
                    yield _sse("evaluation", {"passed": ev["passed"], "issues": ev["rule_issues"],
                                              "rule_checks": ev["rule_checks"],
                                              "scores": {k: v["score"] for k, v in judge.items()
                                                         if isinstance(v, dict) and "score" in v}})
        report = graph.get_state(config).values.get("report")
        if report:
            md_path, pdf_path = save_report(report, trace_id)
            yield _sse("report", {"content": report, "name": md_path.name,
                                  "pdf": pdf_path.name if pdf_path else None})
    except Exception as e:  # noqa: BLE001 - 스트림 도중 오류를 클라이언트에 전달
        log.exception("analyze 실패 (thread_id=%s, trace_id=%s)", thread_id, trace_id)
        yield _sse("error", {"message": f"{type(e).__name__}: {e}"})
    yield _sse("done", {})


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest) -> StreamingResponse:
    thread_id = req.thread_id or str(uuid.uuid4())
    return StreamingResponse(
        _events(req.question.strip(), thread_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _title(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "제목 없음"


@app.get("/api/reports")
def list_reports() -> list[dict]:
    if not REPORT_DIR.exists():
        return []
    items = []
    for path in sorted(REPORT_DIR.glob("report_*.md"), reverse=True):
        created = datetime.strptime(path.stem, "report_%Y%m%d_%H%M%S")
        items.append({"name": path.name, "title": _title(path.read_text(encoding="utf-8")),
                      "created_at": created.isoformat()})
    return items


@app.get("/api/reports/{name}")
def get_report(name: str) -> dict:
    path = REPORT_DIR / name
    if not REPORT_NAME.match(name) or not path.exists():
        raise HTTPException(404, "리포트를 찾을 수 없습니다.")
    return {"name": name, "content": path.read_text(encoding="utf-8")}


@app.get("/api/reports/{name}/pdf")
def get_report_pdf(name: str) -> FileResponse:
    path = REPORT_DIR / name
    if not REPORT_NAME.match(name) or not path.exists():
        raise HTTPException(404, "리포트를 찾을 수 없습니다.")
    pdf = path.with_suffix(".pdf")
    if not pdf.exists() and not to_pdf(path.read_text(encoding="utf-8"), pdf):  # 예전 리포트는 요청 시 생성
        raise HTTPException(500, "PDF를 생성하지 못했습니다.")
    return FileResponse(pdf, media_type="application/pdf", filename=pdf.name)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}
