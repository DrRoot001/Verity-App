"use client";

/**
 * Browser speech recognition (PRD §23 — web transport).
 *
 * The desktop client streams audio frames to a server-side STT provider. The
 * web client cannot capture system audio at all, and shipping raw microphone
 * audio to the backend would add a network hop to the one path where latency
 * is the product. So the browser transcribes locally and pushes finished
 * utterances through `transcript.text` — the same event the test harness uses,
 * converging on the same server-side utterance processor and the same question
 * detector.
 *
 * Limits worth knowing, because they decide what this can be used for:
 * - Chromium only. Safari and Firefox have no usable implementation.
 * - It hears the *device microphone*. In a real interview that means the
 *   interviewer's voice coming out of your speakers, picked up by your mic —
 *   workable with speakers on, not with headphones. Capturing the call's audio
 *   stream directly needs the desktop app (Phase 12).
 * - Chrome's implementation sends audio to Google for recognition. That is a
 *   third party in the path, so the UI says so before the mic opens.
 */

import { useCallback, useEffect, useRef, useState } from "react";

interface RecognitionAlternative {
  transcript: string;
  confidence: number;
}
interface RecognitionResult {
  readonly length: number;
  isFinal: boolean;
  [index: number]: RecognitionAlternative;
}
interface RecognitionEvent extends Event {
  resultIndex: number;
  results: { readonly length: number; [index: number]: RecognitionResult };
}
interface RecognitionErrorEvent extends Event {
  error: string;
}
interface SpeechRecognition extends EventTarget {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  maxAlternatives: number;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((event: RecognitionEvent) => void) | null;
  onerror: ((event: RecognitionErrorEvent) => void) | null;
  onend: (() => void) | null;
}
type RecognitionConstructor = new () => SpeechRecognition;

function constructor(): RecognitionConstructor | null {
  if (typeof window === "undefined") return null;
  const scope = window as unknown as {
    SpeechRecognition?: RecognitionConstructor;
    webkitSpeechRecognition?: RecognitionConstructor;
  };
  return scope.SpeechRecognition ?? scope.webkitSpeechRecognition ?? null;
}

export function speechRecognitionSupported(): boolean {
  return constructor() !== null;
}

export interface Utterance {
  text: string;
  /** Milliseconds since listening began — the timeline the server sequences on. */
  startMs: number;
  endMs: number;
}

export interface ListeningOptions {
  /** Called once per finalized utterance. */
  onUtterance: (utterance: Utterance) => void;
  lang?: string;
}

export interface Listening {
  supported: boolean;
  listening: boolean;
  /** The in-progress phrase, for showing the user they are being heard. */
  interim: string;
  error: string | null;
  start: () => void;
  stop: () => void;
  toggle: () => void;
}

/**
 * Continuous recognition that survives Chrome's habit of ending the stream on
 * its own every ~60s or after a silence. Without the restart the copilot goes
 * quietly deaf mid-interview, which is worse than never having started.
 */
export function useListening({ onUtterance, lang = "en-US" }: ListeningOptions): Listening {
  const recognition = useRef<SpeechRecognition | null>(null);
  const wanted = useRef(false);
  const origin = useRef(0);
  const callback = useRef(onUtterance);
  callback.current = onUtterance;

  const [supported, setSupported] = useState(false);
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setSupported(speechRecognitionSupported()), []);

  const build = useCallback((): SpeechRecognition | null => {
    const Recognition = constructor();
    if (!Recognition) return null;

    const engine = new Recognition();
    engine.continuous = true;
    engine.interimResults = true;
    engine.lang = lang;
    engine.maxAlternatives = 1;

    engine.onresult = (event) => {
      let pending = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        const text = result?.[0]?.transcript?.trim() ?? "";
        if (!text) continue;

        if (result?.isFinal) {
          const endMs = Date.now() - origin.current;
          callback.current({
            text,
            // The utterance's own start is not exposed; approximate it from a
            // conversational speaking rate so ordering stays sane.
            startMs: Math.max(0, endMs - text.split(/\s+/).length * 400),
            endMs,
          });
        } else {
          pending += ` ${text}`;
        }
      }
      setInterim(pending.trim());
    };

    engine.onerror = (event) => {
      // `no-speech` and `aborted` are normal in a quiet room; restarting is the
      // correct response, not an error the user should read.
      if (event.error === "no-speech" || event.error === "aborted") return;
      if (event.error === "not-allowed") {
        wanted.current = false;
        setListening(false);
        setError("Microphone access was blocked. Allow it in your browser to listen.");
        return;
      }
      setError(`Speech recognition stopped: ${event.error}`);
    };

    engine.onend = () => {
      setInterim("");
      if (!wanted.current) {
        setListening(false);
        return;
      }
      // Chrome ends the stream on its own; start a fresh one.
      try {
        engine.start();
      } catch {
        setListening(false);
        wanted.current = false;
      }
    };

    return engine;
  }, [lang]);

  const start = useCallback(() => {
    if (wanted.current) return;
    const engine = recognition.current ?? build();
    if (!engine) {
      setError("This browser can't listen. Chrome supports it; Safari and Firefox don't.");
      return;
    }
    recognition.current = engine;
    wanted.current = true;
    origin.current = Date.now();
    setError(null);
    try {
      engine.start();
      setListening(true);
    } catch {
      // start() throws if the engine is already running; that is the state we want.
      setListening(true);
    }
  }, [build]);

  const stop = useCallback(() => {
    wanted.current = false;
    recognition.current?.stop();
    setListening(false);
    setInterim("");
  }, []);

  useEffect(
    () => () => {
      wanted.current = false;
      recognition.current?.abort();
    },
    [],
  );

  const toggle = useCallback(() => {
    if (wanted.current) stop();
    else start();
  }, [start, stop]);

  return { supported, listening, interim, error, start, stop, toggle };
}
