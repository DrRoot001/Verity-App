"use client";

/**
 * Live session socket (PRD §26).
 *
 * The browser listens to the microphone and pushes finalized utterances as
 * `transcript.text` on the `system` channel — the interviewer's channel. The
 * server decides whether an utterance is a question worth answering; the client
 * never re-decides. A transport that guesses generates guidance for small talk,
 * which is the exact failure the detector exists to prevent (FR-RT-002).
 */

export interface ServerFrame {
  seq: number;
  type: string;
  payload: Record<string, unknown>;
}

export function openLiveSocket(url: string, ticket: string): WebSocket {
  const base = (process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000").replace(
    /^http/,
    "ws",
  );
  return new WebSocket(`${base}${url}?ticket=${encodeURIComponent(ticket)}`, "verity.rt.v1");
}

export function sendEvent(
  socket: WebSocket,
  sessionId: string,
  type: string,
  payload: Record<string, unknown>,
): void {
  if (socket.readyState !== WebSocket.OPEN) return;
  socket.send(
    JSON.stringify({
      v: 1,
      event_id: `evt_${crypto.randomUUID().replaceAll("-", "")}`,
      session_id: sessionId,
      seq: 0,
      ts: new Date().toISOString(),
      type,
      payload,
    }),
  );
}

/** One heard utterance from the interviewer. */
export function sendHeard(
  socket: WebSocket,
  sessionId: string,
  text: string,
  startMs: number,
  endMs: number,
): void {
  sendEvent(socket, sessionId, "transcript.text", {
    channel: "system",
    content: text,
    is_final: true,
    start_ms: startMs,
    end_ms: endMs,
  });
}
