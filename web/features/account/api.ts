import { request } from "@/lib/api/client";

export interface DeletionState {
  status: string;
  execute_after: string;
  grace_period_days: number;
  cancellable: boolean;
}

export function exportAccount(): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>("/v1/account/export");
}

export function requestDeletion(confirmEmail: string): Promise<DeletionState> {
  return request<DeletionState>("/v1/account/deletion", {
    method: "POST",
    body: { confirm_email: confirmEmail },
  });
}

export function cancelDeletion(): Promise<void> {
  return request<void>("/v1/account/deletion", { method: "DELETE" });
}
