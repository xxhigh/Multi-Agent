import type { Agent } from "@/lib/api";
import { AGENTS } from "@/lib/agents";

const ORDER: Agent[] = ["supervisor", "researcher", "market_researcher", "investment_analyst", "writer", "evaluator"];

export function Pipeline({ active, completed }: { active: Agent | null; completed: Agent[] }) {
  return (
    <ol className="flex flex-wrap items-center gap-1.5 text-xs">
      {ORDER.map((agent, i) => {
        const meta = AGENTS[agent];
        const isActive = active === agent;
        const isDone = completed.includes(agent);
        return (
          <li key={agent} className="flex items-center gap-1.5">
            {i > 0 && <span className="text-muted">→</span>}
            <span
              className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 transition ${
                isActive
                  ? `border-transparent bg-surface ring-2 ${meta.ring} ${meta.text} font-medium`
                  : isDone
                    ? `border-line ${meta.text}`
                    : "border-line text-muted"
              }`}
            >
              <span
                className={`size-1.5 rounded-full ${isActive || isDone ? meta.dot : "bg-line"} ${
                  isActive ? "animate-pulse" : ""
                }`}
              />
              {meta.label}
              <span className="hidden text-muted sm:inline">· {meta.role}</span>
              {isDone && !isActive && <span aria-label="완료">✓</span>}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
