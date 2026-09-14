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

export function updateWorkspace(
  id: string,
  input: Record<string, unknown>,
): Promise<ContextBundle> {
  return request(`/v1/workspaces/${id}`, { method: "PATCH", body: input });
}
export function attachJobDescription(id: string, rawText: string): Promise<ContextBundle> {
  return request(`/v1/workspaces/${id}/job-description`, {
    method: "POST",
    body: { raw_text: rawText },
  });
}
export function archiveWorkspace(id: string): Promise<void> {
  return request(`/v1/workspaces/${id}/archive`, { method: "POST" });
}
