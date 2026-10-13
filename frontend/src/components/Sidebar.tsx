import type { ReportMeta } from "@/lib/api";

function formatDate(iso: string) {
  const d = new Date(iso);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function Sidebar({
  reports,
  selected,
  onSelect,
  onNew,
  error,
}: {
  reports: ReportMeta[];
  selected: string | null;
  onSelect: (name: string) => void;
  onNew: () => void;
  error: string | null;
}) {
  return (
    <nav className="flex h-full flex-col gap-4 p-4">
      <button onClick={onNew} className="btn-primary w-full">
        + 새 분석
      </button>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <h2 className="mb-2 px-2 text-xs font-medium tracking-wide text-muted uppercase">지난 리포트</h2>
        {error && <p className="px-2 text-xs text-red-600 dark:text-red-400">{error}</p>}
        {!error && reports.length === 0 && <p className="px-2 text-xs text-muted">아직 리포트가 없어요.</p>}
        <ul className="space-y-0.5">
          {reports.map((r) => (
            <li key={r.name}>
              <button
                onClick={() => onSelect(r.name)}
                className={`w-full rounded-lg px-2 py-2 text-left transition hover:bg-subtle ${
                  selected === r.name ? "bg-subtle" : ""
                }`}
              >
                <span className="line-clamp-2 text-sm">{r.title}</span>
                <span className="text-xs text-muted tabular-nums">{formatDate(r.created_at)}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </nav>
  );
}
