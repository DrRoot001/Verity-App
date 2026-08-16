/** Button primitive (PRD §23.5). Renders as a link when `href` is given. */

import Link from "next/link";
import type { ButtonHTMLAttributes, ReactNode } from "react";

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md" | "lg";

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-[var(--color-accent)] text-[var(--color-accent-contrast)] hover:bg-[var(--color-accent-hover)] border-transparent",
  secondary:
    "bg-[var(--color-surface)] text-[var(--color-text-primary)] border-[var(--color-border-strong)] hover:bg-[var(--color-raised)]",
  ghost:
    "bg-transparent text-[var(--color-text-secondary)] border-transparent hover:bg-[var(--color-raised)]",
  danger: "bg-[var(--color-critical)] text-white border-transparent hover:opacity-90",
};

// Minimum 24px target (WCAG 2.2), 44px on touch (PRD FR-A11Y-011).
const SIZES: Record<Size, string> = {
  sm: "h-8 px-3 text-sm min-w-[2rem]",
  md: "h-9 px-4 text-base min-w-[2.25rem]",
  lg: "h-11 px-6 text-md min-w-[2.75rem]",
};

interface Props extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "className"> {
  variant?: Variant;
  size?: Size;
  href?: string;
  loading?: boolean;
  children: ReactNode;
}

export function Button({
  variant = "primary",
  size = "md",
  href,
  loading = false,
  disabled,
  children,
  ...rest
}: Props) {
  const classes = [
    "inline-flex items-center justify-center gap-2 rounded-[var(--radius-control)] border font-medium",
    "transition-colors duration-[var(--duration-micro)] ease-[var(--ease-standard)]",
    "disabled:cursor-not-allowed disabled:opacity-55",
    VARIANTS[variant],
    SIZES[size],
  ].join(" ");

  if (href) {
    return (
      <Link href={href} className={classes}>
        {children}
      </Link>
    );
  }

  return (
    <button className={classes} disabled={disabled || loading} aria-busy={loading} {...rest}>
      {loading ? (
        <span aria-hidden="true" className="opacity-70">
          ···
        </span>
      ) : null}
      {children}
    </button>
  );
}
