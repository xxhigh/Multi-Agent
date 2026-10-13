"""로보틱스 스타트업 투자 평가 agent HTTP API.

실행 (프로젝트 루트에서):
    uvicorn backend.app:app --reload --port 8000

- POST /api/analyze        : 질문을 받아 agent 진행 상황을 SSE로 스트리밍
- GET  /api/reports        : 저장된 리포트 목록
- GET  /api/reports/{name} : 리포트 본문
- GET  /api/reports/{name}/pdf : 리포트 PDF 다운로드
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

from finance_agent import build_graph, config  # noqa: E402
from finance_agent.runner import new_run, save_outputs  # noqa: E402

REPORT_NAME = re.compile(r"^report_[\w-]+\.md$")
TOOL_RESULT_PREVIEW = 300

log = logging.getLogger("uvicorn.error")

app = FastAPI(title="Robotics Startup Investment Agent API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

graph = build_graph()


class AnalyzeRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    thread_id: str | None = None


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _worker_summary(node: str, data: dict) -> str:
    if node == "evaluator" and data.get("eval_result"):
        v = data["eval_result"]
        failed = [k for k in ("groundedness", "neutrality", "bias_control", "coverage") if not getattr(v, k).passed]
        return "품질 평가 통과" if v.passed else f"품질 평가 미달: {', '.join(failed)}"
    if node == "writer":
        return ""
    persp = data.get("perspectives") or {}
    return f"근거 {len(data.get('evidence') or [])}건 수집, 관점 {', '.join(persp)} 정리"


def _events(question: str, thread_id: str):
    """graph.stream 업데이트를 프론트가 그리기 쉬운 SSE 이벤트로 변환."""
    trace_id, state, cfg = new_run(question, thread_id)
    yield _sse("start", {"thread_id": thread_id, "trace_id": trace_id})
    try:
        for namespace, update in graph.stream(state, cfg, stream_mode="updates", subgraphs=True):
            for node, data in update.items():
                if not data:
                    continue
                if namespace:  # 하위 에이전트(ReAct) 서브그래프 내부
                    agent = namespace[0].split(":")[0]
                    msgs = data.get("messages") or []
                    if node == "agent" and msgs and msgs[-1].tool_calls:
                        for call in msgs[-1].tool_calls:
                            yield _sse("tool_call", {"agent": agent, "id": call["id"], "name": call["name"],
                                                     "args": call["args"]})
                    elif node == "tools":
                        for m in msgs:
                            yield _sse("tool_result", {"agent": agent, "id": m.tool_call_id,
                                                       "preview": str(m.content)[:TOOL_RESULT_PREVIEW]})
                elif node == "supervisor":
                    nxt = data.get("next")
                    yield _sse("route", {"next": "FINISH" if nxt in ("FINISH", "FINISH_WARNING") else nxt,
                                         "instruction": data.get("instruction") or ""})
                else:
                    content = data.get("report") or ""
                    yield _sse("worker_done", {"agent": node, "chars": len(content),
                                               "summary": _worker_summary(node, data)})
        final = graph.get_state(cfg).values
        paths = save_outputs(trace_id, final)
        if final.get("report"):
            name = paths["report"].name if "report" in paths else ""
            yield _sse("report", {"content": final["report"], "name": name})
    except Exception as e:  # noqa: BLE001 - 스트림 도중 오류를 클라이언트에 전달
        log.exception("analyze 실패 (trace_id=%s)", trace_id)
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
    if not config.REPORT_DIR.exists():
        return []
    items = []
    for path in sorted(config.REPORT_DIR.glob("report_*.md"), reverse=True):
        created = datetime.fromtimestamp(path.stat().st_mtime)
        items.append({"name": path.name, "title": _title(path.read_text(encoding="utf-8")),
                      "created_at": created.isoformat()})
    return items


@app.get("/api/reports/{name}")
def get_report(name: str) -> dict:
    path = config.REPORT_DIR / name
    if not REPORT_NAME.match(name) or not path.exists():
        raise HTTPException(404, "리포트를 찾을 수 없습니다.")
    return {"name": name, "content": path.read_text(encoding="utf-8")}


@app.get("/api/reports/{name}/pdf")
def get_report_pdf(name: str) -> FileResponse:
    path = (config.REPORT_DIR / name).with_suffix(".pdf")
    if not REPORT_NAME.match(name) or not path.exists():
        raise HTTPException(404, "PDF를 찾을 수 없습니다.")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}
