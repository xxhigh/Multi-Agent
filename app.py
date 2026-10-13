"""재무 분석 멀티 agent 실행 스크립트 (CLI + HTTP API).

CLI (프로젝트 루트에서):
    python app.py                         # 대화형 모드
    python app.py "셀트리온 재무 분석해줘"   # 단일 질문

HTTP API:
    uvicorn app:app --reload --port 8000

- POST /api/analyze        : 질문을 받아 agent 진행 상황을 SSE로 스트리밍
- GET  /api/reports        : 저장된 리포트 목록
- GET  /api/reports/{name} : 리포트 본문

리포트는 outputs/ 에 저장된다.
"""

import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from agents import build_graph  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"
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


def _save_report(report: str) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"report_{datetime.now():%Y%m%d_%H%M%S}.md"
    path.write_text(report, encoding="utf-8")
    return path


def _events(question: str, thread_id: str):
    """graph.stream 업데이트를 프론트가 그리기 쉬운 SSE 이벤트로 변환."""
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 50}
    yield _sse("start", {"thread_id": thread_id})
    try:
        stream = graph.stream({"messages": [("user", question)]}, config, stream_mode="updates", subgraphs=True)
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
                    nxt = data.get("next")
                    instruction = ""
                    if data.get("messages"):
                        instruction = data["messages"][-1].content.split("] ", 1)[-1]
                    yield _sse("route", {"next": nxt, "instruction": instruction})
                elif data.get("messages"):
                    content = data["messages"][-1].content
                    yield _sse("worker_done", {"agent": node, "chars": len(content),
                                               "summary": content if node != "writer" else ""})
                    if node == "writer":
                        yield _sse("report", {"content": content, "name": _save_report(content).name})
    except Exception as e:  # noqa: BLE001 - 스트림 도중 오류를 클라이언트에 전달
        log.exception("analyze 실패 (thread_id=%s)", thread_id)
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
    if not OUTPUT_DIR.exists():
        return []
    items = []
    for path in sorted(OUTPUT_DIR.glob("report_*.md"), reverse=True):
        created = datetime.strptime(path.stem, "report_%Y%m%d_%H%M%S")
        items.append({"name": path.name, "title": _title(path.read_text(encoding="utf-8")),
                      "created_at": created.isoformat()})
    return items


@app.get("/api/reports/{name}")
def get_report(name: str) -> dict:
    path = OUTPUT_DIR / name
    if not REPORT_NAME.match(name) or not path.exists():
        raise HTTPException(404, "리포트를 찾을 수 없습니다.")
    return {"name": name, "content": path.read_text(encoding="utf-8")}


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(question: str, thread_id: str) -> None:
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 50}
    report = None
    stream = graph.stream({"messages": [("user", question)]}, config, stream_mode="updates", subgraphs=True)
    for namespace, update in stream:
        for node, data in update.items():
            if namespace:  # researcher/analyst 내부의 도구 호출 표시
                if node == "agent" and data and data["messages"][-1].tool_calls:
                    for call in data["messages"][-1].tool_calls:
                        args = ", ".join(f"{k}={v}" for k, v in call["args"].items())
                        print(f"      🔧 {call['name']}({args})")
            elif node == "supervisor":
                nxt = data.get("next")
                print(f"  🧭 supervisor → {nxt}")
            elif data and data.get("messages"):
                msg = data["messages"][-1]
                print(f"  ✅ {node} 완료 ({len(msg.content)}자)")
                if node == "writer":
                    report = msg.content

    if report:
        print("\n" + "=" * 70 + "\n" + report + "\n" + "=" * 70)
        print(f"\n📄 리포트 저장: {_save_report(report)}")


def main() -> None:
    thread_id = str(uuid.uuid4())

    if len(sys.argv) > 1:
        run(" ".join(sys.argv[1:]), thread_id)
        return

    print("💰 재무 분석 agent (종료: exit)")
    while True:
        try:
            q = input("\n질문> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in {"exit", "quit", "q"}:
            break
        if q:
            run(q, thread_id)


if __name__ == "__main__":
    main()
