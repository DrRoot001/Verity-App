import { request } from "@/lib/api/client";
import type { ContextBundle, WorkspaceSummary } from "@/lib/api/types";

export function listWorkspaces(): Promise<WorkspaceSummary[]> {
  return request<WorkspaceSummary[]>("/v1/workspaces");
}

export function createWorkspace(input: {
  company_name: string;
  role_title: string;
  jd_text?: string;
}): Promise<WorkspaceSummary> {
  return request<WorkspaceSummary>("/v1/workspaces", { method: "POST", body: input });
}

export function getContext(workspaceId: string): Promise<ContextBundle> {
  return request<ContextBundle>(`/v1/workspaces/${workspaceId}/context`);
}
