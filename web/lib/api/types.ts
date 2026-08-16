/**
 * API contract types, mirroring the backend Pydantic models (PRD §25).
 *
 * Hand-written rather than generated so the shapes the UI depends on are
 * reviewed deliberately; the OpenAPI document at /openapi.json is the source
 * of truth these are checked against.
 */

export type ErrorCode =
  | "validation_failed"
  | "unauthenticated"
  | "invalid_credentials"
  | "email_not_verified"
  | "token_expired"
  | "token_invalid"
  | "token_reuse_detected"
  | "account_suspended"
  | "forbidden"
  | "not_found"
  | "conflict"
  | "invalid_state_transition"
  | "entitlement_required"
  | "entitlement_exhausted"
  | "rate_limited"
  | "file_too_large"
  | "file_type_rejected"
  | "extraction_failed"
  | "ai_provider_unavailable"
  | "ai_timeout"
  | "dependency_unavailable"
  | "internal_error";

export type RecoveryType =
  | "retry"
  | "reauthenticate"
  | "verify_email"
  | "upgrade"
  | "grant_permission"
  | "edit_input"
  | "contact_support"
  | "wait"
  | "reconnect"
  | "none";

export interface RecoveryAction {
  type: RecoveryType;
  target?: string | null;
  label?: string | null;
}

export interface ErrorEnvelope {
  error: {
    code: ErrorCode;
    message: string;
    detail?: Record<string, unknown>;
    recovery_action: RecoveryAction;
    request_id?: string | null;
    retryable: boolean;
  };
}

// ── Identity ─────────────────────────────────────────────────────────

export interface User {
  id: string;
  email: string;
  full_name: string | null;
  locale: string;
  timezone: string;
  interview_locale: string;
  email_verified: boolean;
  mfa_enabled: boolean;
  onboarding_state: Record<string, unknown>;
  created_at: string;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: "Bearer";
  expires_in: number;
}

export interface AuthResponse {
  user: User;
  tokens: TokenPair;
}

// ── Candidate graph ──────────────────────────────────────────────────

export type EntityType =
  | "experience"
  | "project"
  | "skill"
  | "achievement"
  | "education"
  | "certification";

export type NodeStatus =
  | "draft"
  | "pending_review"
  | "approved"
  | "rejected"
  | "superseded";

export interface Provenance {
  status: NodeStatus;
  source: string;
  source_ref: string | null;
  extracted_by: string | null;
  confidence: number | null;
  confirmed_at: string | null;
  has_user_corrections: boolean;
}

export interface GraphNode {
  id: string;
  entity_type: EntityType;
  label: string;
  detail: Record<string, unknown>;
  provenance: Provenance;
}

export interface ReviewQueue {
  pending: GraphNode[];
  approved_count: number;
  pending_count: number;
  rejected_count: number;
}

export interface Story {
  id: string;
  title: string;
  categories: string[];
  situation: string | null;
  task: string | null;
  actions: string[];
  result: string | null;
  metrics: unknown[];
  skills_demonstrated: string[];
  status: "suggested" | "approved" | "rejected" | "needs_detail";
  speak_time_seconds: number | null;
  source_evidence_ids: string[];
}

// ── Workspace ────────────────────────────────────────────────────────

export type InterviewStage =
  | "preparing"
  | "recruiter_screen"
  | "hiring_manager"
  | "technical"
  | "onsite"
  | "final"
  | "offer"
  | "rejected";

export interface WorkspaceSummary {
  id: string;
  company_name: string;
  role_title: string;
  seniority: string | null;
  stage: InterviewStage;
  round_index: number;
  interview_at: string | null;
  integrity_mode: "assisted" | "proctored";
  readiness_score: number | null;
  live_copilot_allowed: boolean;
  archived: boolean;
}

export type MatchStatus = "strong" | "partial" | "gap";

export interface EvidenceRef {
  id: string;
  type: string;
  label: string;
}

export interface RequirementMatch {
  requirement: string;
  kind: "must_have" | "nice_to_have";
  status: MatchStatus;
  evidence: EvidenceRef[];
  rationale: string;
  matched_terms: string[];
}

export interface MatchResult {
  overall_score: number;
  requirements: RequirementMatch[];
  technology_coverage: Record<string, boolean>;
  strengths: RequirementMatch[];
  gaps: RequirementMatch[];
  formula: Record<string, number>;
  computed_without_jd: boolean;
}

export interface CandidateSummary {
  headline: string | null;
  experience_level: string | null;
  years_experience: number | null;
  experiences: Array<{
    id: string;
    title: string;
    company: string;
    start: string | null;
    end: string | null;
    is_current: boolean;
    achievements: Array<{ id: string; statement: string; has_metric: boolean }>;
  }>;
  skills: string[];
  education: Array<{ institution: string; degree: string | null; end_year: number | null }>;
  story_index: Array<{
    id: string;
    title: string;
    categories: string[];
    speak_time_seconds: number | null;
  }>;
  approved_counts: Record<string, number>;
  pending_counts: Record<string, number>;
}

export interface OpportunitySummary {
  company_name: string;
  role_title: string;
  seniority: string | null;
  stage: string;
  interview_at: string | null;
  integrity_mode: string;
  jd_present: boolean;
  must_have: string[];
  nice_to_have: string[];
  technologies: string[];
  likely_themes: Array<{ value: string; confidence: number; basis: string }>;
  competencies: Array<{ value: string; confidence: number; basis: string }>;
}

export interface ContextBundle {
  workspace_id: string;
  version: number;
  candidate: CandidateSummary;
  opportunity: OpportunitySummary;
  match: MatchResult;
  inferred_opportunity: boolean;
  warnings: string[];
}

export interface IngestionResult {
  version: {
    id: string;
    resume_id: string;
    version: number;
    status: string;
    source_type: string;
    extraction_quality: number | null;
    extraction_warnings: unknown[];
    page_count: number | null;
    parsed_at: string | null;
  };
  diff: Array<{
    entity_type: string;
    change: "added" | "changed" | "unchanged" | "removed";
    label: string;
    existing_id: string | null;
    user_corrected: boolean;
    fields_changed: string[];
  }>;
  created_node_count: number;
  experiences_found: number;
  skills_found: number;
  warnings: string[];
}
