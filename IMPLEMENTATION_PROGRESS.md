# Verity — Implementation Progress

Maps PRD requirements to implementation status. Source of truth for scope: `PRD.md`.

**Status legend:** `DONE` (implemented + verified) · `IN PROGRESS` · `BLOCKED` · `NOT STARTED`

Last updated: 2026-08-16

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
| 0.16 | Web app shell (Next.js, strict TS) | NOT STARTED | Next up |
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
| 1 — Identity & design system | weeks 3–6 | 1a auth **DONE** (endpoints + tests); 1b design system NOT STARTED |
| 2 — Candidate Intelligence core | weeks 6–9 | **DONE** (see below) |
| 3 — Ingestion | weeks 9–12 | **DONE** — resume pipeline, versioning, diff/merge; AC-GRAPH-001 & 002 pass |
| 4 — Workspace + ContextBundle | weeks 12–14 | NOT STARTED |
| 5 — Story Bank & Preparation | weeks 14–17 | NOT STARTED |
| 6 — Mock Interview + AI stack | weeks 17–21 | NOT STARTED |
| 7 — Realtime infrastructure | weeks 21–25 | NOT STARTED |
| 8 — Question detection & context | weeks 25–28 | NOT STARTED |
| 9 — Copilot answer engine | weeks 28–31 | NOT STARTED |
| 10 — Reports & the loop | weeks 31–33 | NOT STARTED |
| 11 — Billing & entitlements | weeks 33–35 | NOT STARTED |
| 12 — Desktop application | weeks 30–36 | NOT STARTED |
| 13 — Admin & operations | weeks 35–38 | NOT STARTED |
| 14 — Reliability hardening | weeks 38–41 | NOT STARTED |
| 15 — Launch gates | weeks 41–43 | NOT STARTED |
| 16 — P1 features | post-GA | NOT STARTED |

---

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
| No context reconstruction outside `ContextBundle` | §11.2 FR-WS-003 | architecture test (pending Phase 4) | NOT STARTED |
| Every user-owned query is `user_id`-scoped in SQL | §28.2, AC-SEC-001 | cross-user retrieval + ingestion tests; query-shape test pending | PARTIAL |
| No plan-name literals outside `billing/` | §19 FR-BILL-001 | CI grep check (pending Phase 11) | NOT STARTED |
| `candidate_fact` requires non-empty `evidence_ids` | §12.6 | evidence resolution rejects unapproved/cross-user ids; schema validator pending Phase 9 | PARTIAL |
| No AI call bypasses the metering wrapper | §33 FR-COST-002 | provider gateway is the only reachable path (pending Phase 6) | NOT STARTED |

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
