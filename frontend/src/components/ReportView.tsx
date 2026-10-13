"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function ReportView({ content, name }: { content: string; name: string }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    await navigator.clipboard.writeText(content);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const download = () => {
    const url = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: name });
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <article className="rounded-xl border border-line bg-surface">
      <div className="flex items-center justify-between gap-2 border-b border-line px-4 py-2.5">
        <span className="truncate font-mono text-xs text-muted">{name}</span>
        <div className="flex shrink-0 gap-1.5">
          <button onClick={copy} className="btn-ghost">
            {copied ? "복사됨" : "복사"}
          </button>
          <button onClick={download} className="btn-ghost">
            .md 다운로드
          </button>
        </div>
      </div>
      <div className="prose prose-sm max-w-none px-5 py-5 sm:prose-base sm:px-7 dark:prose-invert prose-headings:tracking-tight prose-table:text-sm">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            table: ({ children }) => (
              <div className="overflow-x-auto">
                <table>{children}</table>
              </div>
            ),
          }}
        >
          {content}
        </ReactMarkdown>
      </div>
    </article>
  );
}
