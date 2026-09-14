"use client";

/**
 * The interviewer's voice (PRD §11 — mock interview).
 *
 * A mock interview that only ever appears as text trains the wrong thing: the
 * pressure of being asked out loud and having to answer immediately is most of
 * what makes a real interview hard. So the interviewer speaks.
 *
 * `speechSynthesis` is used rather than a TTS provider because it is instant,
 * free, offline, and available in every browser that can also listen. Swapping
 * in a provider voice later is a change behind this one function.
 */

import { useCallback, useEffect, useState } from "react";

export function speechSynthesisSupported(): boolean {
  return typeof window !== "undefined" && "speechSynthesis" in window;
}

/** Prefer a natural-sounding local English voice over the robotic default. */
function pickVoice(): SpeechSynthesisVoice | null {
  const voices = window.speechSynthesis.getVoices().filter((v) => v.lang.startsWith("en"));
  if (voices.length === 0) return null;

  const preferred = ["Samantha", "Google US English", "Microsoft Aria", "Daniel", "Karen"];
  for (const name of preferred) {
    const match = voices.find((v) => v.name.includes(name));
    if (match) return match;
  }
  return voices.find((v) => v.localService) ?? voices[0] ?? null;
}

export interface Speaker {
  supported: boolean;
  speaking: boolean;
  muted: boolean;
  setMuted: (muted: boolean) => void;
  speak: (text: string, onDone?: () => void) => void;
  cancel: () => void;
}

export function useSpeaker(): Speaker {
  const [supported, setSupported] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [muted, setMuted] = useState(false);

  useEffect(() => {
    setSupported(speechSynthesisSupported());
    if (!speechSynthesisSupported()) return;
    // Voices load asynchronously in Chrome; touching the list primes it.
    window.speechSynthesis.getVoices();
    const prime = () => window.speechSynthesis.getVoices();
    window.speechSynthesis.addEventListener("voiceschanged", prime);
    return () => window.speechSynthesis.removeEventListener("voiceschanged", prime);
  }, []);

  const cancel = useCallback(() => {
    if (!speechSynthesisSupported()) return;
    window.speechSynthesis.cancel();
    setSpeaking(false);
  }, []);

  const speak = useCallback(
    (text: string, onDone?: () => void) => {
      if (!speechSynthesisSupported() || muted || !text.trim()) {
        // With voice off there is nothing to wait for, so the caller's
        // follow-on step must still run or the interview stalls.
        onDone?.();
        return;
      }

      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(text);
      const voice = pickVoice();
      if (voice) utterance.voice = voice;
      // Slightly under default: interviewers do not read at podcast speed, and
      // a rushed question is harder to answer for the wrong reason.
      utterance.rate = 0.95;
      utterance.pitch = 1;
      utterance.onstart = () => setSpeaking(true);
      utterance.onend = () => {
        setSpeaking(false);
        onDone?.();
      };
      utterance.onerror = () => {
        setSpeaking(false);
        onDone?.();
      };
      window.speechSynthesis.speak(utterance);
    },
    [muted],
  );

  useEffect(() => () => window.speechSynthesis?.cancel(), []);

  return {
    supported,
    speaking,
    muted,
    setMuted: (next: boolean) => {
      setMuted(next);
      if (next) cancel();
    },
    speak,
    cancel,
  };
}
