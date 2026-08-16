import { request } from "@/lib/api/client";
import type { GraphNode, IngestionResult, ReviewQueue, Story } from "@/lib/api/types";

export function reviewQueue(): Promise<ReviewQueue> {
  return request<ReviewQueue>("/v1/profile/review");
}

export function bulkApprove(entityType: string, ids: string[]): Promise<{ approved: number; indexed: number }> {
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
