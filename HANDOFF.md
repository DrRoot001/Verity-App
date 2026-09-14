# Verity — Engineering Handoff

Everything you need to run, understand, and continue this codebase.
`PRD.md` is the specification; `IMPLEMENTATION_PROGRESS.md` maps its
requirements to status. This document is the operational layer between them.

---

## 1. Running it

Two processes and two datastores. Postgres runs from the `pgserver` Python
package (prebuilt binary with pgvector bundled) — there is no Homebrew or Docker
dependency.

```bash
make infra
```

Then the two processes, in separate terminals:

```bash
make api
```

```bash
make web
```

`make help` lists every target; `make verify` runs the full gate.

| Service | URL |
|---|---|
| Web | http://127.0.0.1:3000 |
| API | http://127.0.0.1:8000 |
| OpenAPI | http://127.0.0.1:8000/docs |
| Health | http://127.0.0.1:8000/healthz, `/readyz` |

### First-time setup

```bash
make setup && make infra && make migrate && make seed
make bootstrap-admin email=you@example.com password='a-long-unique-password'
```

---

## 2. Accounts and administration

`python -m verity.apps.cli seed` now creates operational settings and feature
flags only. It never creates users, resumes, workspaces, sessions, or known
passwords. Bootstrap the first administrator explicitly:

```bash
make bootstrap-admin email=you@example.com password='a-long-unique-password'
```

The local workspace currently has one bootstrap administrator:

| Email | Role | Local password |
|---|---|---|
| `admin@finalround.dev` | admin | `FinalRound-Admin-2026!` |

Change that password with the same bootstrap command before sharing the
environment. `python -m verity.apps.cli purge-demo` removes only the fixed
legacy demo identities and `@verityqa.dev` integration-test accounts; it does
not remove normal users.

Staff accounts receive a dedicated operations navigation. A non-staff account
gets **404, not 403**, from every admin route. Administrators can grant and
revoke least-privilege staff roles in `/admin/staff`; every change is audited,
self-revocation is blocked, and the final active admin cannot be removed.

The admin surface now includes live metrics, user suspend/restore/deletion
workflow, session inventory and diagnostics, staff/RBAC, database-backed AI
routing and budgets, feature flags, versioned prompts, and immutable audit
history. Provider secret values remain deployment-managed and never enter the
browser.

---

## 3. Configuration

`.env` at the repo root is complete and working for local development —
JWT signing keys and the data-encryption key are already generated in it.
`.env.example` documents every variable. `web/.env.local` holds only
`NEXT_PUBLIC_API_URL`; nothing secret belongs there.

**What is not set, and what that means:**

| Variable | Effect while empty |
|---|---|
| `VERITY_GROQ_API_KEY` | With `VERITY_LLM_PRIMARY_PROVIDER=groq`, the gateway routes to `DeterministicProvider` while this is empty — a real, seeded implementation producing schema-valid output. Orchestration, grounding validation, metering and fallback are all exercised; the prose is not model-generated. `gateway.py` refuses this provider in production. |
| `VERITY_ANTHROPIC_API_KEY` | Used only when `VERITY_LLM_PRIMARY_PROVIDER=anthropic`; it remains available as an alternative provider. |
| `VERITY_DEEPGRAM_API_KEY` / `VERITY_ASSEMBLYAI_API_KEY` | No live speech-to-text. Sessions run in text mode; the STT failover chain and Manual Mode are reachable but not driven by audio. |
| `VERITY_STRIPE_SECRET_KEY` | Billing is deferred (Phase 11), `VERITY_PAYMENT_PROVIDER=stub`. |
| OAuth client IDs | Google/Apple sign-in unavailable; password auth is complete. |

Setting `VERITY_GROQ_API_KEY` is the single change that turns the default local
configuration from deterministic to live. Groq uses `llama-3.1-8b-instant` for
classification, `openai/gpt-oss-20b` for realtime generation, and
`openai/gpt-oss-120b` for batch reasoning; all model IDs are configurable. To use Anthropic instead, set
`VERITY_LLM_PRIMARY_PROVIDER=anthropic` and `VERITY_ANTHROPIC_API_KEY`.

---

## 4. The architecture that must not be broken

One rule governs this codebase, and four import-linter contracts enforce it:

```
Candidate Intelligence → Interview Workspace → ContextBundle → Consumers
```

Every feature — Copilot, mock interviews, preparation, reports — is a
**consumer** of one context path. No feature reconstructs context for itself.
The single entry point is `ContextResolver.resolve()` in
`backend/verity/modules/workspace/context.py`. If you find yourself querying the
candidate graph from a feature module, you are building the thing this
architecture exists to prevent.

```bash
cd backend && .venv/bin/lint-imports
```

Four contracts, all currently kept:

1. **Vendor AI SDKs confined to `ai.providers`** (NFR-AI-001) — no `anthropic`
   or `openai` import may appear anywhere else.
2. **Layered architecture** — `apps → modules → ai|realtime → platform`.
3. **Feature modules sit above Candidate Intelligence** — and among themselves:
   `privacy → admin → preparation → sessions → workspace → documents →
   candidate_graph`. Preparation sits above sessions because the Learning Loop
   flows one way; if sessions ever read a plan, the loop becomes a cycle.
4. **Peer modules do not import each other's internals**.

These contracts have repeatedly caught real design questions rather than style
violations. When one fails, the usual correct answer is that the dependency is
pointing the wrong way — not that the contract needs an exception.

---

## 5. Verification

```bash
cd backend
.venv/bin/python -m ruff check verity tests
.venv/bin/python -m mypy verity          # strict, 89 files
.venv/bin/lint-imports                    # 4 contracts
.venv/bin/python -m pytest                # 278 tests
```

```bash
cd web
npx eslint app features lib components
npx tsc --noEmit
```

Integration tests need Postgres and Redis running. They are not mocked — they
drive the real database, the real WebSocket, and the real grounding validator.

**Two lessons the test suite encodes.** Browser verification catches what type
checking cannot: an entire design-token system was silently dead because
Tailwind v4 needs `bg-[var(--token)]`, not `bg-[--token]`, and every type check
passed. And integration tests catch what unit tests cannot: a token-reuse
revocation was rolled back by the surrounding unit of work, so the security
control did nothing while its unit test passed.

---

## 6. Where things are

```
backend/verity/
  platform/        errors, security, config, db, cache, storage, email, logging
  ai/              gateway (the only path to a provider), prompts, answers, providers/
  realtime/        protocol, VAD, question detector
  modules/
    identity/      auth, sessions, tokens
    candidate_graph/  the canonical layer — models, retrieval, indexer, stories
    documents/     resume ingestion, parsing, versioning
    workspace/     context.py ← the single context path
    sessions/      mock interviews, live engine, copilot, live_report
    preparation/   plans, readiness, the Learning Loop consumer
    admin/         staff RBAC, audit
    privacy/       export and the deletion pipeline
  apps/
    api/app.py     router mounting
    cli.py         seed
web/
  app/(app)/       dashboard, review, workspaces, admin, account
  features/        one api.ts per domain — components never call the client directly
  lib/api/         typed client with transparent token refresh
  components/ui/   design system
```

### Decisions worth knowing before you change something

- **`AIGateway` is the only path to a provider.** It meters, budgets, routes by
  task class, and records cost. A direct provider call bypasses all of it and
  breaks the contract in §4.
- **Grounding is validated in pure Python**, not by asking a model to check
  itself (`ai/answers.py`). A `candidate_fact` must cite real evidence *and* its
  entities and numerals must appear in the evidence corpus, or it is downgraded.
- **The question detector gates generation.** `should_generate` travels on the
  event payload; the transport must not re-decide. It generated answers for
  small talk when it did.
- **Live reports measure coverage, not score.** Nobody graded the candidate, so
  the report says which role requirements were probed and which had no evidence
  behind them. Those unbacked requirements are exactly what feeds the next prep
  plan.
- **Deletion verifies per store.** `PrivacyService._verify` re-reads every
  user-owned table (from SQLAlchemy metadata, so new tables are covered
  automatically), plus object storage listings, and fails the job if anything
  answers. Postgres is deleted **last** — delete the user row first and a failed
  job loses its only handle on what remains.

---

## 7. What is not built

Stated plainly, because a handoff that overstates completion is worse than no
handoff.

| Area | Status |
|---|---|
| **Billing (Phase 11)** | Deferred by the product owner. Entitlement keys exist in the schema; no gating, checkout, or webhooks. |
| **Desktop app (Phase 12)** | Not started. It is a separate Tauri application — native audio capture, keychain, updater, notarized builds. The realtime protocol it consumes is frozen and reachable today over the WebSocket. |
| **Live HUD UI** | The socket works end-to-end and is covered by tests, but no web screen connects to it yet. This is the largest single gap between "the system works" and "a user can use it". |
| **Mock interview UI** | Same shape: the API and engine are complete and tested; the screen is not built. |
| **Reliability drills (Phase 14)** | AC-PRIV-001 passes. Load tests at 500 concurrent sessions, chaos injection, failover, and backup-restore verification need infrastructure this environment does not have. |
| **Real STT** | Provider chain and Manual Mode failover are implemented; no key is configured, so nothing has been driven by real audio. |
| **Job scheduler** | The deletion pipeline is complete and runnable via `POST /v1/account/deletion/run` (staff-only). Nothing schedules it yet — point a cron container at that endpoint, or run `PrivacyService.due_jobs()` from a worker. |

### If you continue, in this order

1. **Live HUD screen.** Everything behind it exists. This is the highest ratio
   of user-visible value to remaining work in the codebase.
2. **Mock interview screen.** Same argument, smaller.
3. **Scheduler** for the deletion queue — the pipeline is the hard part and it
   is done.
4. **Billing**, when it is wanted. The PRD sequenced it late on purpose:
   retrofitting gating is about a week; building it before the metered features
   existed would have been speculative.

---

## 8. Recorded deviations from the PRD

Four, all in `IMPLEMENTATION_PROGRESS.md` with reasoning: `citext` removed in
favour of `VARCHAR` + normalization + a CHECK constraint; `pgserver` instead of
a system Postgres; `pg_trgm` deferred; `pgcrypto` unnecessary. The PRD was not
rewritten to match — deviations are recorded against it.
