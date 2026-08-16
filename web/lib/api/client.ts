/**
 * Typed API client (PRD §25.1).
 *
 * Every failure becomes an `ApiError` carrying the backend's `recovery_action`,
 * so a UI surface can always offer a way forward instead of a dead end
 * (FR-ERR-001). Components never call this directly — they go through the
 * hooks in `features/*`, which is what keeps business logic out of components
 * (FR-FE-001).
 */

import type { ErrorCode, ErrorEnvelope, RecoveryAction } from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  readonly code: ErrorCode;
  readonly status: number;
  readonly detail: Record<string, unknown>;
  readonly recoveryAction: RecoveryAction;
  readonly requestId: string | null;
  readonly retryable: boolean;

  constructor(status: number, envelope: ErrorEnvelope) {
    super(envelope.error.message);
    this.name = "ApiError";
    this.status = status;
    this.code = envelope.error.code;
    this.detail = envelope.error.detail ?? {};
    this.recoveryAction = envelope.error.recovery_action;
    this.requestId = envelope.error.request_id ?? null;
    this.retryable = envelope.error.retryable;
  }

  /** Field-level messages for form rendering, keyed by field path. */
  get fieldErrors(): Record<string, string> {
    const fields = this.detail.fields;
    return typeof fields === "object" && fields !== null
      ? (fields as Record<string, string>)
      : {};
  }
}

/** Raised when the network never delivered a response at all. */
export class NetworkError extends Error {
  constructor(cause: unknown) {
    super("We couldn't reach Verity. Check your connection and try again.");
    this.name = "NetworkError";
    this.cause = cause;
  }
}

type TokenReader = () => string | null;
type TokenRefresher = () => Promise<boolean>;

let readAccessToken: TokenReader = () => null;
let refreshTokens: TokenRefresher = async () => false;

export function setTokenReader(reader: TokenReader): void {
  readAccessToken = reader;
}

/**
 * Register the refresh strategy. Access tokens last 15 minutes by design
 * (PRD §10.2); without this the user would be signed out mid-task on a
 * timer, which is a worse failure than the short TTL prevents.
 */
export function setTokenRefresher(refresher: TokenRefresher): void {
  refreshTokens = refresher;
}

/** Concurrent 401s share one refresh rather than each starting their own. */
let inFlightRefresh: Promise<boolean> | null = null;

async function refreshOnce(): Promise<boolean> {
  inFlightRefresh ??= refreshTokens().finally(() => {
    inFlightRefresh = null;
  });
  return inFlightRefresh;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  /** Set for operations that create billable or side-effectful resources. */
  idempotencyKey?: string;
}

export async function request<T>(
  path: string,
  options: RequestOptions = {},
  { allowRefresh = true }: { allowRefresh?: boolean } = {},
): Promise<T> {
  const { method = "GET", body, signal, idempotencyKey } = options;

  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;

  const token = readAccessToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body === undefined ? null : JSON.stringify(body),
      signal: signal ?? null,
      credentials: "omit",
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new NetworkError(cause);
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const payload: unknown = text ? JSON.parse(text) : null;

  if (!response.ok) {
    if (isErrorEnvelope(payload)) {
      const error = new ApiError(response.status, payload);
      // An expired access token is recoverable without user action; a revoked
      // session or a reuse detection is not, and must fall through to sign-in.
      if (allowRefresh && error.code === "token_expired" && (await refreshOnce())) {
        return request<T>(path, options, { allowRefresh: false });
      }
      throw error;
    }
    // The backend always returns an envelope; a bare failure means something
    // upstream (proxy, gateway) answered instead.
    throw new ApiError(response.status, {
      error: {
        code: "internal_error",
        message: "Something went wrong upstream of the API.",
        detail: {},
        recovery_action: { type: "retry", label: "Try again" },
        request_id: response.headers.get("X-Request-ID"),
        retryable: true,
      },
    });
  }

  return payload as T;
}

function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  return (
    typeof value === "object" &&
    value !== null &&
    "error" in value &&
    typeof (value as { error: unknown }).error === "object"
  );
}

/** Upload a file. Kept separate because it must not set a JSON content type. */
export async function uploadFile<T>(
  path: string,
  file: File,
  fields: Record<string, string> = {},
): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  for (const [key, value] of Object.entries(fields)) form.append(key, value);

  const headers: Record<string, string> = {};
  const token = readAccessToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { method: "POST", headers, body: form });
  } catch (cause) {
    throw new NetworkError(cause);
  }

  const text = await response.text();
  const payload: unknown = text ? JSON.parse(text) : null;
  if (!response.ok && isErrorEnvelope(payload)) throw new ApiError(response.status, payload);
  return payload as T;
}
