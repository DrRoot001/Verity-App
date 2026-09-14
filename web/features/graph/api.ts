import { request, uploadFile } from "@/lib/api/client";
import type { GraphNode, IngestionResult, ReviewQueue, Story } from "@/lib/api/types";

export function reviewQueue(): Promise<ReviewQueue> {
  return request<ReviewQueue>("/v1/profile/review");
}

export function bulkApprove(
  entityType: string,
  ids: string[],
): Promise<{ approved: number; indexed: number }> {
  return request("/v1/profile/bulk-approve", {
    method: "POST",
    body: { entity_type: entityType, ids },
  });
}

export function rejectNode(entityType: string, id: string): Promise<GraphNode> {
  return request<GraphNode>(`/v1/profile/${entityType}/${id}/reject`, { method: "POST" });
}

export function pasteResume(rawText: string): Promise<IngestionResult> {
  return request<IngestionResult>("/v1/resumes/paste", {
    method: "POST",
    body: { raw_text: rawText },
  });
}

export function listStories(): Promise<Story[]> {
  return request<Story[]>("/v1/stories");
}

export function getProfile(): Promise<{
  headline: string | null;
  summary: string | null;
  experience_level: string | null;
  target_role: string | null;
  approved_counts: Record<string, number>;
  pending_counts: Record<string, number>;
}> {
  return request("/v1/profile");
}
export function listNodes(entityType: string): Promise<GraphNode[]> {
  return request(`/v1/profile/${entityType}`);
}
export function updateNode(
  entityType: string,
  id: string,
  changes: Record<string, unknown>,
): Promise<GraphNode> {
  return request(`/v1/profile/${entityType}/${id}`, { method: "PATCH", body: changes });
}
export function approveNode(entityType: string, id: string): Promise<GraphNode> {
  return request(`/v1/profile/${entityType}/${id}/approve`, { method: "POST" });
}
export function createStory(input: {
  title: string;
  categories: string[];
  situation: string;
  task: string;
  actions: string[];
  result: string;
  skills_demonstrated: string[];
}): Promise<Story> {
  return request("/v1/stories", { method: "POST", body: input });
}
export function approveStory(id: string): Promise<Story> {
  return request(`/v1/stories/${id}/approve`, { method: "POST" });
}
export function generateStories(): Promise<{ created: number; titles: string[] }> {
  return request("/v1/preparation/stories/generate", { method: "POST" });
}
export function uploadResume(file: File): Promise<IngestionResult> {
  return uploadFile("/v1/resumes/upload", file);
}
export function listResumeVersions(): Promise<IngestionResult["version"][]> {
  return request("/v1/resumes/versions");
}
