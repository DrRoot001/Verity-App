import { request } from "@/lib/api/client";
import type { AuthResponse, User } from "@/lib/api/types";

export function login(email: string, password: string): Promise<AuthResponse> {
  return request<AuthResponse>("/v1/auth/login", {
    method: "POST",
    body: { email, password, platform: "web" },
  });
}

export function signup(
  email: string,
  password: string,
  fullName?: string,
): Promise<{ message: string }> {
  return request("/v1/auth/signup", {
    method: "POST",
    body: {
      email,
      password,
      full_name: fullName ?? null,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    },
  });
}

export function me(): Promise<User> {
  return request<User>("/v1/users/me");
}

export function updateMe(
  input: Partial<Pick<User, "full_name" | "locale" | "timezone" | "interview_locale">>,
): Promise<User> {
  return request("/v1/users/me", { method: "PATCH", body: input });
}
export interface Device {
  id: string;
  name: string;
  platform: string;
  app_version: string | null;
  trusted: boolean;
  last_seen_at: string;
  is_current: boolean;
}
export function listDevices(): Promise<Device[]> {
  return request("/v1/auth/devices");
}
export function revokeDevice(id: string): Promise<void> {
  return request(`/v1/auth/devices/${id}`, { method: "DELETE" });
}
export function revokeAllDevices(): Promise<{ message: string }> {
  return request("/v1/auth/devices/revoke-all", { method: "POST" });
}

export function verifyEmail(token: string): Promise<User> {
  return request<User>("/v1/auth/verify-email", { method: "POST", body: { token } });
}

export function requestPasswordReset(email: string): Promise<{ message: string }> {
  return request("/v1/auth/password/reset-request", { method: "POST", body: { email } });
}

export function confirmPasswordReset(
  token: string,
  password: string,
): Promise<{ message: string }> {
  return request("/v1/auth/password/reset", { method: "POST", body: { token, password } });
}
