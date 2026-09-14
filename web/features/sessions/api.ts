import { request } from "@/lib/api/client";

export interface MockSession {
  id: string;
  workspace_id: string;
  mode: string;
  persona: string;
  difficulty: number;
  status: string;
  planned_duration_seconds: number;
  live_feedback: boolean;
}

export interface LiveSession {
  id: string;
  workspace_id: string;
  status: string;
  interview_type: string;
  response_mode: string;
  integrity_mode: string;
  last_event_seq: number;
}

export interface MockTurn {
  utterance: string;
  intent: string;
  expects_answer: boolean;
  sequence: number;
  difficulty: number;
}

export interface SessionReport {
  session_id: string;
  status: string;
  overall_score: number | null;
  summary: string | null;
  dimension_scores: Record<string, number>;
  delivery_metrics?: Record<string, unknown>;
  metrics?: Record<string, unknown>;
  strengths: unknown[];
  weaknesses: unknown[];
  recommendations: unknown[];
  per_question?: Array<Record<string, unknown>>;
  questions?: Array<Record<string, unknown>>;
  coverage?: Array<Record<string, unknown>>;
}

export function listMockSessions(): Promise<MockSession[]> {
  return request("/v1/mock-sessions");
}

export function createMockSession(input: {
  workspace_id: string;
  mode: string;
  persona: string;
  difficulty: number;
  duration_minutes: number;
  live_feedback: boolean;
}): Promise<MockSession> {
  return request("/v1/mock-sessions", { method: "POST", body: input });
}

export function getMockSession(id: string): Promise<MockSession> {
  return request(`/v1/mock-sessions/${id}`);
}

export function nextMockTurn(id: string): Promise<MockTurn> {
  return request(`/v1/mock-sessions/${id}/turn`, { method: "POST" });
}

export interface AnswerResult {
  scores: Record<string, unknown>;
  next_turn: MockTurn;
}

/** Scores the answer and returns the next turn in one round trip. */
export function answerMock(
  id: string,
  answer: string,
  durationSeconds: number,
): Promise<AnswerResult> {
  return request<AnswerResult>(`/v1/mock-sessions/${id}/answer`, {
    method: "POST",
    body: { answer, duration_seconds: durationSeconds },
  });
}

export function endMock(id: string): Promise<SessionReport> {
  return request(`/v1/mock-sessions/${id}/end`, { method: "POST" });
}

export function getMockReport(id: string): Promise<SessionReport> {
  return request(`/v1/mock-sessions/${id}/report`);
}

export function listLiveSessions(): Promise<LiveSession[]> {
  return request("/v1/live-sessions");
}
export function getLiveSession(id: string): Promise<LiveSession> {
  return request(`/v1/live-sessions/${id}`);
}

export function createLiveSession(input: {
  workspace_id: string;
  interview_type: string;
  response_mode: string;
  language?: string;
}): Promise<LiveSession> {
  return request("/v1/live-sessions", { method: "POST", body: input });
}

export function issueLiveTicket(
  id: string,
): Promise<{ ticket: string; expires_in: number; url: string }> {
  return request(`/v1/live-sessions/${id}/ticket`, { method: "POST" });
}

export function getLiveReport(id: string): Promise<SessionReport> {
  return request(`/v1/live-sessions/${id}/report`);
}
