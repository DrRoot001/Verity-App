/**
 * Button (Verity Design Language v2).
 *
 * Primary is solid accent; secondary is a hairline-bordered surface. The
 * difference between a cheap-looking button and an expensive one is almost
 * entirely in the transition, the border treatment and the pressed state, so
 * those are specified rather than left to defaults.
 */

import Link from "next/link";
import type { ButtonHTMLAttributes, ReactNode } from "react";

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md" | "lg";

const BASE = [
  "inline-flex items-center justify-center gap-2 rounded-[var(--radius-control)]",
  "border font-medium whitespace-nowrap select-none",
  "transition-[background-color,border-color,color,box-shadow,transform]",
  "duration-[var(--duration-micro)] ease-[var(--ease-standard)]",
  "active:translate-y-px",
  "disabled:cursor-not-allowed disabled:opacity-50 disabled:active:translate-y-0",
].join(" ");

const VARIANTS: Record<Variant, string> = {
  primary: [
    "bg-[var(--color-accent)] text-[var(--color-accent-contrast)] border-transparent",
    "shadow-[var(--shadow-1)] hover:bg-[var(--color-accent-hover)]",
  ].join(" "),
  secondary: [
    "bg-[var(--color-surface)] text-[var(--color-text-primary)]",
    "border-[var(--color-border-strong)] shadow-[var(--shadow-1)]",
    "hover:bg-[var(--color-raised)] hover:border-[var(--color-text-muted)]",
  ].join(" "),
  ghost: [
    "bg-transparent text-[var(--color-text-secondary)] border-transparent",
    "hover:bg-[var(--color-raised)] hover:text-[var(--color-text-primary)]",
  ].join(" "),
  danger: [
    "bg-[var(--color-critical)] text-white border-transparent",
    "shadow-[var(--shadow-1)] hover:opacity-90",
  ].join(" "),
};

// Minimum 24px target (WCAG 2.2), 44px on touch (PRD FR-A11Y-011).
const SIZES: Record<Size, string> = {
  sm: "h-8 px-3 text-sm min-w-8",
  md: "h-9 px-4 text-base min-w-9",
  lg: "h-11 px-5 text-md min-w-11",
};

interface Props extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "className"> {
  variant?: Variant;
  size?: Size;
  href?: string;
  loading?: boolean;
  fullWidth?: boolean;
  children: ReactNode;
}

export function Button({
  variant = "primary",
  size = "md",
  href,
  loading = false,
  fullWidth = false,
  disabled,
  children,
  ...rest
}: Props) {
  const classes = [BASE, VARIANTS[variant], SIZES[size], fullWidth ? "w-full" : ""].join(" ");

  if (href) {
    return (
      <Link href={href} className={classes}>
        {children}
      </Link>
    );
  }

  return (
    <button className={classes} disabled={disabled || loading} aria-busy={loading} {...rest}>
      {loading ? <Spinner /> : null}
      {children}
    </button>
  );
}

/** Sized to the text beside it, so the button does not resize when busy. */
function Spinner() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 16 16"
      className="size-3.5 animate-spin opacity-70"
      fill="none"
    >
      <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="2" opacity="0.25" />
      <path
        d="M14.5 8a6.5 6.5 0 0 0-6.5-6.5"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}
