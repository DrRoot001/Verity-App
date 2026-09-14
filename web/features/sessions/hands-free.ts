"use client";

/**
 * Hands-free turn-taking for the spoken mock interview.
 *
 * A real interview has no buttons. If the candidate has to click a microphone
 * before every answer and a submit button after it, they are operating an app,
 * not being interviewed — and the thing being practised (answering out loud,
 * under time pressure, without a safety net) never actually happens.
 *
 * So the loop runs itself: the interviewer speaks, the microphone opens the
 * moment it stops, and the answer is submitted when the candidate has been
 * quiet long enough to have finished.
 *
 * The end-of-turn silence is the one number that matters. Too short and it cuts
 * people off mid-thought — the pauses inside a real STAR answer routinely run
 * past a second. Too long and every turn feels laggy. 2.5s is chosen to sit
 * above natural mid-answer pauses while staying under the point where silence
 * reads as "it stopped working".
 */

import { useCallback, useEffect, useRef, useState } from "react";

export const END_OF_TURN_SILENCE_MS = 2500;

export interface HandsFree {
  enabled: boolean;
  setEnabled: (on: boolean) => void;
  /** Restart the silence countdown — called on every recognised phrase. */
  noteSpeech: () => void;
  /** Stop any pending countdown, e.g. once the answer has been submitted. */
  clear: () => void;
  /** Seconds remaining before auto-submit, or null when not counting down. */
  countdown: number | null;
}

export function useHandsFree(onSilence: () => void): HandsFree {
  const [enabled, setEnabled] = useState(true);
  const [countdown, setCountdown] = useState<number | null>(null);

  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const tick = useRef<ReturnType<typeof setInterval> | null>(null);
  // Kept in a ref so restarting the countdown never needs a new callback
  // identity, which would re-run the effects that own the microphone.
  const fire = useRef(onSilence);
  fire.current = onSilence;

  const clear = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    if (tick.current) clearInterval(tick.current);
    timer.current = null;
    tick.current = null;
    setCountdown(null);
  }, []);

  const noteSpeech = useCallback(() => {
    clear();

    const deadline = Date.now() + END_OF_TURN_SILENCE_MS;
    setCountdown(Math.ceil(END_OF_TURN_SILENCE_MS / 1000));

    tick.current = setInterval(() => {
      setCountdown(Math.max(0, Math.ceil((deadline - Date.now()) / 1000)));
    }, 250);

    timer.current = setTimeout(() => {
      clear();
      fire.current();
    }, END_OF_TURN_SILENCE_MS);
  }, [clear]);

  useEffect(() => clear, [clear]);

  return {
    enabled,
    setEnabled: (on: boolean) => {
      setEnabled(on);
      if (!on) clear();
    },
    noteSpeech,
    clear,
    countdown,
  };
}
