import { request } from "@/lib/api/client";
import type { AuthResponse, User } from "@/lib/api/types";

export function login(email: string, password: string): Promise<AuthResponse> {
  return request<AuthResponse>("/v1/auth/login", {
    method: "POST",
    body: { email, password, platform: "web" },
  });
}

export function signup(email: string, password: string, fullName?: string): Promise<{ message: string }> {
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
