"""관측성 계층.

결정 로그(라우팅 사유, 재작업 판단, 품질 평가 결과)는 State가 아니라 여기서 외부로 적재한다.
- outputs/logs/decisions.jsonl : {ts, trace_id, node, event, ...} 한 줄씩
- LangSmith : run_config()가 trace_id를 metadata·tag로 붙여 같은 키로 조회할 수 있게 한다
"""

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs"
LOG_PATH = OUTPUT_DIR / "logs" / "decisions.jsonl"

log = logging.getLogger("finance_agent")
_lock = threading.Lock()


def log_decision(trace_id: str, node: str, event: str, **fields) -> None:
    record = {"ts": datetime.now().isoformat(timespec="seconds"), "trace_id": trace_id,
              "node": node, "event": event, **fields}
    log.info("%s", record)
    with _lock:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def run_config(thread_id: str, trace_id: str, question: str) -> dict:
    """graph.stream/invoke 에 넘길 config. LangSmith 트레이스에 trace_id를 심는다."""
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 80,
        "run_name": "finance-supervisor",
        "tags": ["supervisor-pattern", f"trace:{trace_id}"],
        "metadata": {"trace_id": trace_id, "thread_id": thread_id, "question": question,
                     "model": os.getenv("OPENAI_MODEL", "gpt-5-mini")},
    }


def langsmith_enabled() -> bool:
    return os.getenv("LANGSMITH_TRACING", "").lower() == "true" and bool(os.getenv("LANGSMITH_API_KEY"))
