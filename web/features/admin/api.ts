import { request } from "@/lib/api/client";

export interface StaffPrincipal {
  user_id: string;
  email: string;
  roles: string[];
  permissions: string[];
}

export interface AdminMetrics {
  users_total: number;
  users_active_7d: number;
  workspaces_total: number;
  mock_sessions_total: number;
  live_sessions_total: number;
  reports_total: number;
  stories_approved: number;
  activated_users: number;
}

export interface AdminUser {
  id: string;
  email: string;
  full_name: string | null;
  status: string;
  email_verified: boolean;
  created_at: string;
  workspaces: number;
  mock_sessions: number;
  live_sessions: number;
  staff_roles: string[];
}

export interface StaffGrant {
  id: string;
  user_id: string;
  email: string;
  role: string;
  created_at: string;
}

export interface AdminSession {
  id: string;
  kind: "mock" | "live";
  user_id: string;
  user_email: string;
  workspace_id: string;
  status: string;
  mode: string;
  created_at: string;
  duration_seconds: number | null;
}

export interface AISettings {
  provider: "stub" | "groq" | "anthropic";
  fast_model: string;
  realtime_model: string;
  reasoning_model: string;
  daily_budget_usd: number;
  budget_soft_threshold: number;
  session_max_generations: number;
  session_max_generations_per_minute: number;
  session_max_duration_minutes: number;
  groq_key_configured: boolean;
  anthropic_key_configured: boolean;
}

export interface FeatureFlag {
  id: string;
  key: string;
  description: string;
  enabled: boolean;
  rollout_percentage: number;
  updated_at: string;
}

export interface PromptVersion {
  id: string | null;
  prompt_id: string;
  version: number;
  task_class: string;
  status: string;
  system_template: string;
  user_template: string;
  variables: string[];
  output_schema: Record<string, unknown> | null;
  notes: string;
  eval_score: number | null;
}

export interface SessionDiagnostics {
  session_id: string;
  status: string;
  generations: number;
  metered_seconds: number;
  last_event_seq: number;
  latencies: Array<Record<string, unknown>>;
  grounding: { checked: number; downgraded: number };
}

export interface AuditEntry {
  id: string;
  actor_id: string | null;
  subject_user_id: string | null;
  action: string;
  resource_type: string;
  resource_id: string | null;
  reason: string | null;
  case_id: string | null;
  created_at: string;
}

export function whoami(): Promise<StaffPrincipal> {
  return request<StaffPrincipal>("/v1/admin/me");
}

export function metrics(): Promise<AdminMetrics> {
  return request<AdminMetrics>("/v1/admin/metrics");
}

export function searchUsers(q: string): Promise<AdminUser[]> {
  return request<AdminUser[]>(`/v1/admin/users?q=${encodeURIComponent(q)}`);
}

export function suspendUser(
  userId: string,
  reason: string,
  caseId: string,
): Promise<AdminUser> {
  return request<AdminUser>(`/v1/admin/users/${userId}/suspend`, {
    method: "POST",
    body: { reason, case_id: caseId },
  });
}

export function restoreUser(
  userId: string,
  reason: string,
  caseId: string,
): Promise<AdminUser> {
  return request<AdminUser>(`/v1/admin/users/${userId}/restore`, {
    method: "POST",
    body: { reason, case_id: caseId },
  });
}

export function scheduleUserDeletion(
  userId: string,
  confirmEmail: string,
  reason: string,
  caseId: string,
): Promise<{ user_id: string; status: string; execute_after: string }> {
  return request(`/v1/admin/users/${userId}/delete`, {
    method: "POST",
    body: { confirm_email: confirmEmail, reason, case_id: caseId },
  });
}

export function cancelUserDeletion(
  userId: string,
  reason: string,
  caseId: string,
): Promise<void> {
  return request(`/v1/admin/users/${userId}/deletion/cancel`, {
    method: "POST",
    body: { reason, case_id: caseId },
  });
}

export function listStaff(): Promise<StaffGrant[]> {
  return request<StaffGrant[]>("/v1/admin/staff");
}

export function grantStaff(userId: string, role: string): Promise<StaffGrant> {
  return request<StaffGrant>("/v1/admin/staff", {
    method: "POST",
    body: { user_id: userId, role },
  });
}

export function revokeStaff(grantId: string): Promise<void> {
  return request<void>(`/v1/admin/staff/${grantId}`, { method: "DELETE" });
}

export function listAdminSessions(kind = "all"): Promise<AdminSession[]> {
  return request<AdminSession[]>(`/v1/admin/sessions?kind=${encodeURIComponent(kind)}`);
}

export function getAISettings(): Promise<AISettings> {
  return request<AISettings>("/v1/admin/settings/ai");
}

export function saveAISettings(value: AISettings): Promise<AISettings> {
  // The *_key_configured flags are read-only status, not settings to write back.
  const body = { ...value } as Partial<AISettings>;
  delete body.groq_key_configured;
  delete body.anthropic_key_configured;
  return request<AISettings>("/v1/admin/settings/ai", { method: "PUT", body });
}

export function listFlags(): Promise<FeatureFlag[]> {
  return request<FeatureFlag[]>("/v1/admin/flags");
}

export function saveFlag(flag: FeatureFlag): Promise<FeatureFlag> {
  return request<FeatureFlag>(`/v1/admin/flags/${flag.key}`, {
    method: "PUT",
    body: {
      description: flag.description,
      enabled: flag.enabled,
      rollout_percentage: flag.rollout_percentage,
    },
  });
}

export function listPrompts(): Promise<PromptVersion[]> {
  return request<PromptVersion[]>("/v1/admin/prompts");
}

export function createPrompt(value: {
  prompt_id: string;
  task_class: string;
  system_template: string;
  user_template: string;
  variables: string[];
  output_schema: Record<string, unknown> | null;
  notes: string;
}): Promise<PromptVersion> {
  return request<PromptVersion>("/v1/admin/prompts", { method: "POST", body: value });
}

export function activatePrompt(promptId: string, version: number): Promise<PromptVersion> {
  return request<PromptVersion>(
    `/v1/admin/prompts/${encodeURIComponent(promptId)}/${version}/activate`,
    { method: "POST" },
  );
}

export function sessionDiagnostics(sessionId: string): Promise<SessionDiagnostics> {
  return request<SessionDiagnostics>(`/v1/admin/sessions/${sessionId}/diagnostics`);
}

export function auditLog(): Promise<AuditEntry[]> {
  return request<AuditEntry[]>("/v1/admin/audit");
}
