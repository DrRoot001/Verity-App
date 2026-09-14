import Link from "next/link";
import React from "react";

export function Logo({
  compact = false,
  href = "/dashboard",
}: {
  compact?: boolean;
  href?: string;
}) {
  return (
    <Link href={href} className="inline-flex items-center gap-2.5" aria-label="Verity home">
      <Mark />
      {compact ? null : (
        <span className="text-[15px] font-semibold tracking-[-0.025em]">Verity</span>
      )}
    </Link>
  );
}

export function Mark({ className = "size-8" }: { className?: string }) {
  return (
    <svg viewBox="0 0 36 36" className={className} role="img" aria-label="Verity" fill="none">
      <rect width="36" height="36" rx="10" fill="var(--color-accent)" />
      <path
        d="M9.2 10.2 16.4 27h3.2l7.2-16.8h-4.1L18 22.1l-4.7-11.9H9.2Z"
        fill="var(--color-accent-contrast)"
      />
      <path
        d="m23.3 8.3 2.1 2.1 3.8-4"
        stroke="#9FE2C1"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
