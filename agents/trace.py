"""실행 트레이스 (JSONL).

결정 이력과 사유, 작업 결과 본문처럼 계속 쌓이는 기록은 State가 아니라 여기에 남긴다.
State와는 run_id + step 으로 이어진다: outputs/traces/<run_id>.jsonl
"""

import json
from datetime import datetime
from pathlib import Path

TRACE_DIR = Path(__file__).resolve().parent.parent / "outputs" / "traces"


def path(run_id: str) -> Path:
    return TRACE_DIR / f"{run_id}.jsonl"


def log(run_id: str, event: str, **fields) -> None:
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    record = {"ts": datetime.now().isoformat(timespec="seconds"), "run_id": run_id, "event": event, **fields}
    with path(run_id).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
