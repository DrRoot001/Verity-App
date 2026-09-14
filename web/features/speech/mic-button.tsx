"use client";

/** Shared microphone control, so both interview rooms behave identically. */

export function MicButton({
  listening,
  onClick,
  disabled = false,
  label,
}: {
  listening: boolean;
  onClick: () => void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-pressed={listening}
      aria-label={listening ? "Stop listening" : "Start listening"}
      className={[
        "inline-flex items-center gap-2.5 rounded-full border px-4 py-2 text-sm font-medium",
        "transition-[background-color,border-color,box-shadow] duration-[var(--duration-micro)]",
        "disabled:cursor-not-allowed disabled:opacity-50",
        listening
          ? "border-[var(--color-critical-border)] bg-[var(--color-critical-quiet)] text-[var(--color-critical)]"
          : "border-[var(--color-border-strong)] bg-[var(--color-surface)] text-[var(--color-text-primary)] hover:bg-[var(--color-raised)]",
      ].join(" ")}
    >
      <span className="relative flex size-2.5">
        {listening ? (
          <span className="absolute inline-flex size-full animate-ping rounded-full bg-[var(--color-critical)] opacity-70" />
        ) : null}
        <span
          className={`relative inline-flex size-2.5 rounded-full ${
            listening ? "bg-[var(--color-critical)]" : "bg-[var(--color-text-muted)]"
          }`}
        />
      </span>
      {label ?? (listening ? "Listening" : "Start mic")}
    </button>
  );
}

/** Live words as they are recognised — proof to the user that it is hearing them. */
export function InterimTranscript({ text }: { text: string }) {
  if (!text) return null;
  return (
    <p className="text-sm italic text-[var(--color-text-muted)]" aria-live="polite">
      {text}…
    </p>
  );
}
