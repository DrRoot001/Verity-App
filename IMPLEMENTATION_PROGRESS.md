# Verity — Implementation Progress

Maps PRD requirements to implementation status. Source of truth for scope: `PRD.md`.

**Status legend:** `DONE` (implemented + verified) · `IN PROGRESS` · `BLOCKED` · `NOT STARTED`

Last updated: 2026-08-17

**Provider update:** Groq Chat Completions is integrated through `AIGateway`
with configurable GPT-OSS production-model aliases, structured-output support,
streaming, usage metering, and deterministic local fallback when no key is set.

**Frontend completion pass:** responsive candidate/staff shells, vector brand,
profile/resume/story authoring, workspace tabs, mock interview room, live text
HUD, session history/reports, device/privacy settings, and database-backed admin
operations (users, staff/RBAC, sessions, AI routing/budgets, flags, prompt
versions, and audit history)
routes are implemented. Real Groq smoke tests recorded a 951 ms mock turn and a
2.25 s grounded live answer on the local/free-tier path; these are development
measurements, not production SLO evidence.

---

## Environment audit (performed before Phase 0)

| Dependency | Required by | Found | Action |
|---|---|---|---|
| Node.js ≥20.9 | PRD §23.1 (Next.js) | v20.19.5 | OK |
| pnpm | monorepo tooling | 9.15.9 | OK |
| Python 3.12 | PRD §22.1 (FastAPI) | 3.12.9 | OK |
| Redis 7+ | PRD §22.1 | 8.4.0, running | OK |
| PostgreSQL 16 + pgvector | PRD §22.1, §24 | **absent** | Resolved — see BLOCKER-002 |
| Docker | local infra convenience | **absent** | Not required — using native Postgres/Redis |
| Git repo | Phase 0 | absent | Initialized |
| **Network egress** | all dependency installs | **blocked in sandbox** | **BLOCKER — see below** |

### BLOCKER-001 — no network egress inside the execution sandbox

All outbound network hangs inside the default sandbox (`curl -m 8` to pypi.org did not
return after 120s; `pip install` and `brew install` both hung with zero bytes transferred
and were killed). The same request succeeds immediately when the sandbox is disabled
(`pypi:200`).

**Consequence:** no dependency can be installed — no FastAPI/SQLAlchemy (backend),
no PostgreSQL/pgvector (database), no Next.js (frontend). Every phase from 0 onward
depends on this.

**RESOLVED.** User authorized running installer commands with the sandbox disabled.
Note for future sessions: `run_in_background: true` does **not** honour the sandbox
override — background installs hang silently. All dependency installs must run in the
foreground.

### BLOCKER-002 — Homebrew PostgreSQL builds from source on this host — RESOLVED

`brew install postgresql@16` began compiling a large dependency tree (icu4c, krb5,
openssl, cmake, python@3.14) from source, exceeding practical time limits on macOS 12
/ x86_64.

**Resolution:** local Postgres now comes from the `pgserver` PyPI package, which ships
prebuilt **PostgreSQL 16.2 binaries with pgvector included**. Cluster lives in
`.data/postgres`, managed by the `db-*` Make targets. Production topology (managed
Postgres per PRD §38.2) is unchanged — this affects local development only.

**Decision recorded:** Docker is unavailable on this machine. Rather than block, local
infrastructure runs natively (Homebrew Postgres + Redis). The PRD's containerized
deployment topology (§38.2) is unaffected — it targets managed Kubernetes, not local Docker.
A `docker-compose.yml` remains a P1 convenience item for contributor onboarding, not a
runtime dependency.

---

## Phase 0 — Foundation (PRD §43 Phase 0)

**Exit gate:** a trivial endpoint ships end-to-end through CI to staging with tracing and structured logs.

| # | Requirement | Status | Implementation |
|---|---|---|---|
| 0.1 | Repo structure per PRD §22.2 | DONE | `backend/verity/{apps,modules,ai,realtime,platform}` |
| 0.2 | Module boundaries + import-linter | DONE | `backend/pyproject.toml` `[tool.importlinter]` — 3 contracts incl. NFR-AI-001 vendor-SDK confinement |
| 0.3 | Typed config from env (PRD §28.1) | DONE | `platform/config.py` — pydantic-settings, fails fast on missing production secrets |
| 0.4 | Unified error model (PRD §22.3, FR-ERR-001) | DONE | `platform/errors.py` — closed `ErrorCode` enum, mandatory `recovery_action` |
| 0.5 | Structured logging (PRD §30.1) | DONE | `platform/logging.py` — JSON, request context, content redaction, pseudonymized user refs |
| 0.6 | OTel tracing + Prometheus metrics (PRD §30) | DONE | `platform/telemetry.py` — stage spans, SLO histograms, AI cost counters |
| 0.7 | Postgres engine + session/unit-of-work | DONE | `platform/db/session.py`, `platform/db/base.py` (UUIDv7, mixins per §24.1) |
| 0.8 | Redis clients (cache/session/queue roles) | DONE | `platform/cache.py` |
| 0.9 | HTTP middleware: request id, RED metrics, security headers | DONE | `apps/api/middleware.py` |
| 0.10 | App factory + exception handlers | DONE | `apps/api/app.py` — every exception becomes an `AppError` envelope |
| 0.11 | Health/readiness/metrics endpoints | DONE | `modules/health/router.py` — readiness checks Postgres + Redis for real |
| 0.12 | Alembic migrations wired + applied | DONE | `alembic/env.py`, `platform/db/registry.py`; migration `c16939064b55` applied to live DB |
| 0.13 | Feature flags (PRD §58) | DONE | `platform/flags.py` — global/percentage/plan/user/platform/app-version targeting, fails closed |
| 0.14 | Local secret bootstrap | DONE | `scripts/bootstrap_env.py` — real Ed25519 JWT keypair + 32-byte DEK |
| 0.15 | Local dev orchestration | DONE | `Makefile` — setup, infra, migrate, run, verify |
| 0.16 | Web app shell (Next.js, strict TS) | DONE | `web/` — Next 15, strict TS, Tailwind v4 token system |
| 0.17 | CI workflow | NOT STARTED | Next up |
| 0.18 | Phase 0 verification run | DONE | See below |

### Phase 0 verification results (all green)

| Check | Command | Result |
|---|---|---|
| Lint | `ruff check` | All checks passed |
| Format | `ruff format --check` | 30 files formatted |
| Types | `mypy verity` (strict) | Success — no issues in 23 source files |
| Architecture | `lint-imports` | 3 contracts kept, 0 broken |
| Tests | `pytest` | **43 passed** |
| Database | `alembic upgrade head` + `\d users` | 4 tables live; partial indexes and CHECK constraints verified in Postgres |
| Runtime | `curl /healthz`, `/readyz`, `/metrics`, 404 | 200/200/200; readiness reports real Postgres + Redis latency; 404 returns full error envelope with `recovery_action` + `request_id`; all 5 security headers present |

**Defect found and fixed during verification:** the first `/readyz` probe after boot
returned `down` because pool construction exceeded the 2s check budget — a healthy pod
would have flapped its rollout health gate. Fixed by warming the connection pool during
lifespan startup (`platform/db/session.py::warm_pool`); cold probe now returns `ok`.

---

## Phases 1–16

| Phase | PRD §43 | Status |
|---|---|---|
| 1 — Identity & design system | weeks 3–6 | **DONE** — 1a auth + 1b web app, design system, 11 UX states, browser-verified |
| 2 — Candidate Intelligence core | weeks 6–9 | **DONE** — graph, retrieval, indexing, review/approval API |
| 3 — Ingestion | weeks 9–12 | **DONE** — pipeline, versioning, diff/merge, HTTP surface; AC-GRAPH-001 & 002 pass |
| 4 — Workspace + ContextBundle | weeks 12–14 | **DONE** — context spine + HTTP surface; AC-WS-001 passes; E1 journey passes over HTTP |
| 5 — Story Bank & Preparation | weeks 14–17 | **DONE** — story generation, ranked plan, readiness, UI; browser-verified |
| 6 — Mock Interview + AI stack | weeks 17–21 | **DONE** — gateway, prompt registry, interviewer engine, reports, learning loop |
| 7 — Realtime infrastructure | weeks 21–25 | **DONE** — engine, event sourcing + replay, VAD, STT failover, WebSocket transport with ticket auth; AC-RT-010 passes |
| 8 — Question detection & context | weeks 25–28 | **DONE** — signal fusion, classification, memory, token-budgeted assembly |
| 9 — Copilot answer engine | weeks 28–31 | **DONE** — dual-lane generation, grounding validator, response modes; AC-COP-002 passes |
| 10 — Reports & the loop | weeks 31–33 | **DONE** — live coverage report, session history, live weakness → prep mutation; AC-WS-002 passes |
| 11 — Billing & entitlements | weeks 33–35 | **DEFERRED** — descoped by the product owner |
| 12 — Desktop application | weeks 30–36 | **IN PROGRESS** — standalone Tauri HUD, local preferences, direct Groq pipeline, multi-key failover, resume/job context, menu-bar lifecycle, persisted capture protection, and macOS fullscreen visibility are implemented; signed distribution and capture-client E2E remain blocked |
| 13 — Admin & operations | weeks 35–38 | **DONE** — staff RBAC, audit trail, metrics, session diagnostics, operations UI |
| 14 — Reliability hardening | weeks 38–41 | **PARTIAL** — AC-PRIV-001 passes (verified deletion pipeline + export). Load/chaos/restore drills need infrastructure this environment does not have |
| 15 — Launch gates | weeks 41–43 | NOT STARTED |
| 16 — P1 features | post-GA | NOT STARTED |

---

## Desktop HUD capture protection (2026-08-17)

Implemented for the Live Interview HUD with Tauri
`WebviewWindow::set_content_protected`. Protection defaults on when preferences
are missing or malformed. The pre-sign-in/setup checkbox and active-HUD button
apply changes to the current native window immediately and persist them in
`desktop-preferences.json`. The HUD also joins all macOS Spaces so it remains
visible over a full-screen call window.

macOS implementation inspected in the resolved dependency versions used by
this build: Tauri `2.11.5` / `tauri-runtime-wry 2.11.4` delegates to Tao
`0.35.3`, which calls `NSWindow.setSharingType(NSWindowSharingNone)` when
enabled and `NSWindowSharingReadOnly` when disabled.

Runtime host: **macOS 12.7.6 (21H1320)**. Verification was executed against the
debug application on 2026-08-17:

| Scenario | Exact result |
|---|---|
| Normal desktop usage | **PASS (visibility)** — CoreGraphics reported the 420 x 620 HUD on-screen with alpha 1 at floating layer 5 while protection was enabled. The HUD remains an ordinary visible app/process. |
| Full-screen Zoom/Meet-style window | **PASS (visibility only)** — the HUD stayed on-screen at the same bounds before and during a real full-screen Google Chrome window after joining all Spaces. Chrome was returned from full-screen after the test. |
| Screenshot | **BLOCKED / INCONCLUSIVE for exclusion** — the automation shell lacks macOS Screen Recording permission. `screencapture` omitted all application windows, including Finder/Notes controls, so the resulting image cannot prove HUD-specific exclusion. A native Cmd+Shift+3 event was also sent while the HUD was confirmed on-screen, but the configured screenshot destination received no new file. |
| macOS screen recording | **BLOCKED** — `screencapture -v -V3` returned `capture error The operation could not be completed` and produced no recording. |
| Individual-window capture/share path | **BLOCKED** — `screencapture -l <HUD window id>` returned `could not create image from window`. Without Screen Recording permission, this does not distinguish protection from TCC denial. |
| Full-screen screen share | **NOT VERIFIED** — no live Zoom/Meet share session was available to inspect from a second participant. Fullscreen HUD visibility passed; capture exclusion did not. |
| Individual-window sharing in Zoom/Meet | **NOT VERIFIED** — no live client/share recipient was available. AppKit sharing protection is configured, but a picker/result was not observed. |
| Setting persistence | **PASS (automated)** — Rust tests cover default-on, malformed-file fail-closed, and an explicit disabled-value round trip. |
| Immediate toggle | **IMPLEMENTED; runtime UI automation blocked** — the command applies the AppKit call before saving and the UI re-reads native state on persistence errors. This session could not synthesize the click because macOS denied assistive access. |

### Standalone desktop runtime (2026-08-17)

The desktop interview assistant is now independent from the Verity web product.
The login/signup screen and all calls to `/v1/auth`, `/v1/workspaces`, live
session creation, ticket issuance, and the backend WebSocket were removed.
Desktop startup now opens directly to local settings and audio-source selection.

The native pipeline performs local RMS-based speech segmentation, flushes after
a 360 ms pause, posts 16 kHz mono WAV directly to Groq
`whisper-large-v3-turbo`, detects interview questions locally, then streams an
answer from `openai/gpt-oss-20b` with low reasoning effort. The HUD displays
measured STT, first-response, and total latency. The API key and role/company
context are desktop-local preferences; the key can alternatively be injected
with `VERITY_GROQ_API_KEY` or `GROQ_API_KEY`.

Version 0.2 adds multiple locally persisted keys with ordered failover, an API
connection test, and PDF/TXT/Markdown import for resume and job description.
The prompt uses bounded document context and instructs the model not to invent
resume facts. The macOS app now runs with Accessory activation policy and hides
its Dock icon; Show, Hide, and Quit remain available from the menu-bar icon.
This is not process hiding: Verity remains visible to macOS and Activity Monitor.

`cargo test` passes **13 tests**, `node --check ui/main.js` passes, and
the standalone app launched with five macOS audio-input indicators present.
A real Groq latency result is **NOT VERIFIED** because this machine currently
has no Groq key configured. A response within one second is an optimization
target, not a guaranteed free-tier SLO; network latency, Groq queueing, speech
duration, and the 360 ms end-of-speech decision are external/runtime factors.

These macOS 12.7.6 results must not be generalized to newer macOS releases.
Tauri/AppKit accepted the protection call without error, but this environment
could not establish whether full-display capture, ScreenCaptureKit-based tools,
or a particular conferencing client honors it. The application is **not**
"undetectable" and is not process-hidden; this feature concerns HUD contents
only. A release gate remains: grant Screen Recording permission to the test
harness, join a live Zoom/Meet call with a second observer, and repeat every
blocked/not-verified row on each supported macOS major version.

Current verification commands completed: `cargo fmt --check`, `cargo test`
(**13 passed**), and `node --check ui/main.js`. Package verification is recorded
below once the universal release build completes.

---

## Phase 2 — Candidate Intelligence core (DONE)

**Exit gate:** approved entities → retrieval returns correct entities at p95 < 300 ms. Met.

| # | Requirement | PRD ref | Status | Implementation |
|---|---|---|---|---|
| 2.1 | Graph schema (10 tables) | §24.3 | DONE | `candidate_graph/models.py`; migration `c90d355cf068` |
| 2.2 | Provenance layers on every node | §12.2 | DONE | `Provenanced` mixin; `effective()` resolves user_corrected → ai_extracted → column |
| 2.3 | Approved-only evidence | FR-GRAPH-001 | DONE | Enforced in retrieval + indexer; 3 tests |
| 2.4 | Unified embedding index | §12.3 | DONE | `graph_embeddings`: HNSW cosine + generated tsvector, verified in-database |
| 2.5 | Typed hybrid retrieval (RRF) | FR-SRCH-002 | DONE | `retrieval.py` — FTS + pgvector fused, k=60 |
| 2.6 | One-hop graph expansion | §12.3 | DONE | experience → projects → achievements(+metrics) |
| 2.7 | Tenant isolation in SQL | AC-SEC-001 | DONE | Ownership predicate in every query; 2 cross-user tests |
| 2.8 | Degraded retrieval on embedding failure | §20.3 | DONE | Falls back to lexical-only, flags `degraded` |
| 2.9 | Transactional indexing | §11.4 | DONE | `indexer.py`; story approval → retrievable, test-proven |
| 2.10 | Content-hash skip | §33 | DONE | Unchanged text skips the embedding call |
| 2.11 | Vector purge for deletion | §29.4 | DONE | `purge_user()`; test asserts zero rows remain |
| 2.12 | Provider abstraction | §20.2, NFR-AI-001 | DONE | `ai/providers/base.py` protocols + embeddings; contract-enforced |

**Defects found and fixed during verification:**
- `plainto_tsquery` resolved to `(varchar, varchar)` which does not exist — required an explicit `regconfig` cast. Lexical search was completely broken and only a live-database test surfaced it.
- Graph expansion reused one `stmt` variable across two model types; mypy caught the type confusion before it could return wrong-typed rows.
- Integration fixtures reused the cached application engine across per-test event loops, producing "Event loop is closed" teardown errors. Fixtures now own a `NullPool` engine per test.

**Note on the local embedding provider:** `HashedNgramEmbedding` is a real deterministic *lexical* embedding (hashed character n-grams, sublinear weighting, L2-normalized) — not a semantic model. It exists so retrieval, indexing and deletion are testable offline with no API key, and it is never routed in production. `OpenAICompatibleEmbedding` is the real path and is selected whenever a key is configured.

---

## Architectural invariants under continuous enforcement

These are the constraints that must never regress. Each has a mechanical check.

| Invariant | PRD ref | Enforcement | Status |
|---|---|---|---|
| Vendor AI SDKs confined to `ai/providers` | NFR-AI-001 | import-linter contract | DONE |
| Layered architecture (apps → modules → ai/realtime → platform) | §22.2 | import-linter contract | DONE |
| Modules do not import each other's internals | §22.2 | import-linter independence contract | DONE |
| No context reconstruction outside `ContextBundle` | §11.2 FR-WS-003 | `ContextResolver` is the sole entry point; import-linter layers contract | DONE |
| Every user-owned query is `user_id`-scoped in SQL | §28.2, AC-SEC-001 | cross-user retrieval + ingestion tests; query-shape test pending | PARTIAL |
| No plan-name literals outside `billing/` | §19 FR-BILL-001 | CI grep check (pending Phase 11) | NOT STARTED |
| `candidate_fact` requires non-empty `evidence_ids` | §12.6 | schema-level claim typing + post-generation validator downgrades unsupported facts | DONE |
| No AI call bypasses the metering wrapper | §33 FR-COST-002 | `AIGateway` is the only path to a provider; budget check + token/cost recording on every call | DONE |

---

## Deviations from PRD

Recorded rather than silently applied. None of these contradict the PRD; they are
environment adaptations or refinements below the PRD's level of detail.

| # | Deviation | PRD ref | Justification |
|---|---|---|---|
| D-01 | `citext` extension not used; `users.email` is `VARCHAR(320)` normalized by `normalize_email()` with a `CHECK (email = lower(email))` | §24.2 | The local Postgres distribution ships only `vector` + `plpgsql`. Depending on an extension that cannot be exercised locally is worse than removing the dependency. Explicit boundary normalization plus a database-enforced invariant gives identical semantics and is portable across every Postgres distribution. |
| D-02 | Local Postgres via `pgserver` (PyPI) instead of Homebrew | §22.1 | See BLOCKER-002. Same PostgreSQL 16 + pgvector; local development only. |
| D-03 | `pg_trgm` deferred | §10.9 FR-SRCH-002 | Hybrid search needs `tsvector` (built in) + pgvector (present). `pg_trgm` only adds trigram fuzzy matching, which is not required until the search surface lands in a later phase. |
| D-04 | `pgcrypto` not required | §24.1 | `gen_random_uuid()` is built into Postgres 13+, and primary keys are UUIDv7 generated application-side for index locality. |
