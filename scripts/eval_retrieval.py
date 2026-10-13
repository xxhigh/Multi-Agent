"""문서 풀 검색 품질 측정: Hit Rate@K, MRR.

정답 문서를 아는 질문 10개로 하이브리드 검색(벡터 + bigram)이 정답 문서를 몇 위에 올리는지 잰다.
실행: python scripts/eval_retrieval.py   (OPENAI_API_KEY 필요, 결과는 outputs/retrieval_eval.json)
"""

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv()

from finance_agent import config  # noqa: E402
from finance_agent.tools import docs, rag  # noqa: E402

K = 5
# (질문, 정답 문서 파일명)
CASES = [
    ("2024년 전 세계 산업용 로봇 설치 대수", "ifr_world_robotics_2025_industrial.md"),
    ("산업용 로봇 신규 설치 지역별 비중 아시아 유럽", "ifr_world_robotics_2025_industrial.md"),
    ("운송·물류 서비스 로봇 판매 대수와 증가율", "ifr_world_robotics_2025_service.md"),
    ("RaaS 로봇 구독 서비스 성장률", "ifr_world_robotics_2025_service.md"),
    ("글로벌 물류 로봇 시장 규모 전망 2035", "robotics_market_layer_map_2026.md"),
    ("AGV에서 AMR로 전환하는 물류 로봇 기술 흐름", "robotics_market_layer_map_2026.md"),
    ("나르고 오더피킹 최고 속도와 적재 중량", "twinny_product_brochure.md"),
    ("트위니 ISO13482 안전 인증과 기업부설연구소", "twinny_product_brochure.md"),
    ("트위니 프리IPO 투자 유치 금액과 투자자", "kidd_2026_preipo.md"),
    ("오더피킹 로봇 공급 업체 수와 미국 진출 계획", "hellot_2023_interview.md"),
]


def main() -> None:
    files = docs.list_files(config.DOCS_DIR)
    name = f"docs-all-{docs._signature(files)}"
    hits_at_k, reciprocal = 0, []
    rows = []
    for query, answer in CASES:
        results = rag.hybrid_search(name, lambda: docs._build(files), query, k=K)
        ranked = list(dict.fromkeys(Path(meta["file"]).name for _, _, meta in results))
        rank = ranked.index(answer) + 1 if answer in ranked else None
        hits_at_k += rank is not None
        reciprocal.append(1 / rank if rank else 0.0)
        rows.append({"query": query, "answer": answer, "rank": rank, "top": ranked[:3]})
        print(f"{'✅' if rank else '❌'} rank={rank}  {query}")
    summary = {"k": K, "cases": len(CASES), "hit_rate_at_k": hits_at_k / len(CASES),
               "mrr": sum(reciprocal) / len(CASES), "details": rows}
    out = config.OUTPUT_DIR / "retrieval_eval.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nHit Rate@{K}: {summary['hit_rate_at_k']:.2f}  MRR: {summary['mrr']:.3f}  → {out}")


if __name__ == "__main__":
    main()
