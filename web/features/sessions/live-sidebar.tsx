"use client";

/** Live copilot sidebar: what was asked, and what the mic actually heard. */

export function LiveSidebar({
  history,
  heard,
}: {
  history: Array<{ q: string; a?: string }>;
  heard: string[];
}) {
  return (
    <aside className="space-y-4">
      <div className="rounded-xl border border-[var(--color-border-subtle)] bg-[var(--color-surface)] p-4">
        <h2 className="text-sm font-medium">Question history</h2>
        <ol className="mt-3 space-y-4">
          {history.length ? (
            history.map((item, index) => (
              <li key={index} className="border-l-2 border-[var(--color-accent-border)] pl-3">
                <p className="text-sm">{item.q}</p>
                {item.a ? (
                  <p className="mt-1 line-clamp-2 text-xs text-[var(--color-text-muted)]">
                    {item.a}
                  </p>
                ) : null}
              </li>
            ))
          ) : (
            <li className="text-sm text-[var(--color-text-muted)]">No questions yet.</li>
          )}
        </ol>
      </div>

      {heard.length ? (
        <div className="rounded-xl border border-[var(--color-border-subtle)] bg-[var(--color-surface)] p-4">
          <h2 className="text-sm font-medium">What it heard</h2>
          <p className="mt-1 text-xs text-[var(--color-text-muted)]">
            Not every line is a question — the detector decides.
          </p>
          <ul className="mt-3 space-y-1.5">
            {heard.slice(-6).map((line, index) => (
              <li key={index} className="text-xs text-[var(--color-text-secondary)]">
                {line}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </aside>
  );
}
