"use client";

/**
 * Client-side session handling (PRD §10.2).
 *
 * The access token lives in memory with a sessionStorage mirror so a reload
 * does not sign the user out. The refresh token is deliberately *not* stored
 * here — the desktop app keeps it in the OS keychain, and the web app trades
 * it immediately rather than parking a long-lived credential in JS-readable
 * storage.
 */

import { setTokenReader } from "@/lib/api/client";
import type { AuthResponse, User } from "@/lib/api/types";

const ACCESS_KEY = "verity.access";
const REFRESH_KEY = "verity.refresh";
const USER_KEY = "verity.user";

let accessToken: string | null = null;

export function bootstrapSession(): void {
  if (typeof window === "undefined") return;
  accessToken = window.sessionStorage.getItem(ACCESS_KEY);
  setTokenReader(() => accessToken);
}

export function storeSession(auth: AuthResponse): void {
  accessToken = auth.tokens.access_token;
  window.sessionStorage.setItem(ACCESS_KEY, auth.tokens.access_token);
  window.sessionStorage.setItem(REFRESH_KEY, auth.tokens.refresh_token);
  window.sessionStorage.setItem(USER_KEY, JSON.stringify(auth.user));
  setTokenReader(() => accessToken);
}

export function clearSession(): void {
  accessToken = null;
  window.sessionStorage.removeItem(ACCESS_KEY);
  window.sessionStorage.removeItem(REFRESH_KEY);
  window.sessionStorage.removeItem(USER_KEY);
}

export function currentUser(): User | null {
  if (typeof window === "undefined") return null;
  const raw = window.sessionStorage.getItem(USER_KEY);
  return raw ? (JSON.parse(raw) as User) : null;
}

export function isSignedIn(): boolean {
  return typeof window !== "undefined" && window.sessionStorage.getItem(ACCESS_KEY) !== null;
}
