import { request } from "@/lib/api/client";

export interface PrepTask {
  id: string;
  section: string;
  title: string;
  detail: string | null;
  priority: "critical" | "high" | "medium" | "optional";
  priority_score: number;
  score_breakdown: {
    factors?: Record<string, number>;
    weights?: Record<string, number>;
    contributions?: Record<string, number>;
    total?: number;
  };
  action: { type: string; params: Record<string, unknown> };
  estimated_minutes: number;
  scheduled_for: string | null;
  status: "open" | "in_progress" | "done" | "dismissed";
  source: string;
}

export interface ReadinessDriver {
  factor: string;
  value: number;
  weight: number;
  detail: string;
}

export interface PrepPlan {
  plan_id: string;
  workspace_id: string;
  round_index: number;
  version: number;
  readiness: { score: number; drivers: ReadinessDriver[] };
  tasks: PrepTask[];
}

export function getPlan(workspaceId: string): Promise<PrepPlan | null> {
  return request<PrepPlan | null>(`/v1/preparation/plans?workspace_id=${workspaceId}`);
}

export function generatePlan(workspaceId: string): Promise<PrepPlan> {
  return request<PrepPlan>(`/v1/preparation/plans/generate?workspace_id=${workspaceId}`, {
    method: "POST",
  });
}

export function setTaskStatus(taskId: string, status: PrepTask["status"]): Promise<PrepTask> {
  return request<PrepTask>(`/v1/preparation/tasks/${taskId}`, {
    method: "PATCH",
    body: { status },
  });
}

export function generateStories(): Promise<{ created: number; titles: string[] }> {
  return request("/v1/preparation/stories/generate", { method: "POST" });
}
