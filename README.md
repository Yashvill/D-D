# Persistent Fleet

**Autonomous demurrage & detention (D&D) defence for ocean freight, built on durable Temporal agents.**

When a container arrives at a port, two clocks start — **demurrage** (the box sitting in the terminal) and **detention** (keeping the carrier's equipment after collection) — and penalty fees escalate daily. Disputing or preventing these charges requires *continuous attention across a 24–60 day window*, joining a contract PDF to a live event stream to an invoice that arrives five weeks later, inside a 30-day dispute window.

The core bet: **give every container its own durable agent whose lifespan matches the container's.** Built on Temporal, each agent sleeps for days, wakes on real-world events, prices its options, acts within a bounded mandate, escalates to a human when spend crosses a threshold, and — after the box is long gone — assembles and prosecutes the dispute inside the regulatory deadline. It survives crashes, deploys, and restarts, because **the workflow *is* the memory.**

> See the design docs in the repo root for full context:
> `persistent-fleet-full-document-45.html` (product), `persistent-fleet-45-day-runbook-for-temporal.html` (day-by-day build spec), `stakeholders-D&D (1).html` (stakeholder value).

---

## Architecture

The system is organized along two orthogonal axes, joined by a strict determinism boundary.

```
workflows/  ── orchestration only (deterministic, replay-safe)
    │  workflow.execute_activity(act.load_contract_terms, LoadTermsInput(...), retry, timeout)
    ▼
activities/registry.py  ── @activity.defn wrappers (the determinism boundary)
    │  calls plain agent functions; all non-determinism lives here
    ▼
agents/  ── domain logic ── agents/shared/llm_client.complete() ──▶ Gemini / OpenRouter
```

A read path sits alongside it: `temporal_backend/api.py` exposes the workflows over HTTP (queries out, signals in) and `frontend/` renders them as an operator console. Neither holds any state of its own.

### Axis 1 — the agent cast (`agents/`)

Plain, Temporal-agnostic Python. Only the Watcher and Case Builder touch the LLM; the money-critical logic is deterministic by design.

| Agent | Directory | Role | LLM? |
|---|---|---|---|
| **Watcher** | `agents/watcher` | Parse contract into structured terms (`extract_terms`); classify carrier advisories (`classify_advisory`) | Yes |
| **Strategist** | `agents/strategist` | Effective-LFD calculation, risk assessment, option pricing + counterfactual | No |
| **Negotiator** | `agents/negotiator` | Reserve appointments, book drayage / empty return (with saga compensators) | No |
| **Auditor** | `agents/auditor` | Part 541 invoice checklist (`check_part_541`), recompute from contract | No |
| **Case Builder** | `agents/case_builder` | Assemble cited chronology, draft dispute letter (`draft_dispute`) | Yes |

**Shared kernel** (`agents/shared`):

- `models.py` — Pydantic domain types (`ContractTerms`, `Evidence`, `Invoice`, `RiskLevel`, …), the wire format everywhere.
- `llm_client.py` — the **single** LLM boundary. No agent imports `litellm` directly; everything goes through `complete()`.
- `prompts.py`, `charges.py` — prompt templates and tier/rate math.

### Axis 2 — the workflow cast (`temporal_backend/workflows/`)

One parent per container; three children each owning exactly one clock.

```
ContainerWorkflow            (parent, entity workflow — Day 1 → 45+)
  id = container::<container_id>   deterministic ID ⇒ idempotent, no duplicate agents
  owns: identity, contract terms, the event log, ALL evidence
  │
  ├─ DemurrageArc   (child, Day 8→20)   terminal clock; availability polling, effective-LFD, interventions
  ├─ DetentionArc   (child, Day 20→31)  equipment clock; empty-return booking, restriction monitoring
  └─ DisputeArc     (child, ABANDON, Day 40→90+)  one per invoice; audit → file → follow-up → settle
```

The **`ParentClosePolicy.ABANDON`** on `DisputeArc` is load-bearing: it lets each dispute run for months independently, long after the parent finishes. Children report; the parent remembers — evidence accumulates on the parent and is passed down to each arc at spawn time.

### The seam — `temporal_backend/activities/registry.py`

Every agent function is wrapped as an `@activity.defn` taking a single Pydantic input. This is the **determinism boundary**: LLM calls, network I/O, clock reads, and randomness all live on the activity side; workflow code only orchestrates. Workflows import agents inside `workflow.unsafe.imports_passed_through()` to keep the determinism sandbox happy.

### Temporal primitives

- **Signals** (events in): `discharged`, `invoice_received`, `paid_under_protest`, `customs_entry_filed`, `close` on the parent; child arcs own their own.
- **Queries** (state out): `state()`, `evidence_log()` on the parent; `findings()` and `letter_draft()` on `DisputeArc` — live state and audit trail are the same object.
- **Timers**: durable `wait_condition` / sleep loops (availability polls, slot scans, invoice watch).
- **Child workflows**: `execute_child_workflow` / `start_child_workflow`.
- **Saga**: compensators such as `cancel_container_drayage`, `release_terminal_appointment`.
- **Retry policies**: separate `LLM_RETRY` and `IO_RETRY` backoffs.

### Infrastructure glue (`temporal_backend/shared/`, `main.py`)

- `converter.py` — `connect()` client plus the **`pydantic_data_converter`**, which must be identical on client, worker, and tests or `date`/`datetime` payloads will not round-trip. Defines two task queues: `pf-io` and `pf-reasoning`.
- Split task queues let LLM activities run low-concurrency with generous timeouts while portal scrapes run cheap and aggressive — and give fleet-wide rate-limiting for free.
- `main.py` — the worker entrypoint; registers `ALL_WORKFLOWS` + `ALL_ACTIVITIES` on a queue.

---

## The LLM boundary (`agents/shared/llm_client.py`)

The only module that talks to an LLM. Everything goes through `complete()`, which provides:

- **Disk cache** keyed on `sha256(model + schema + prompt)` — repeated calls cost nothing.
- **Three modes** via `LLM_MODE`:
  - `mock` — fixtures only, never touches the network (used by tests).
  - `cache` — refuses to hit the network; serves cached responses only.
  - `live` — makes real calls to whichever provider `LLM_MODEL` names.
- **One bounded repair retry** for malformed JSON, then optional fixture fallback.
- **Pydantic schema validation** of every response.

### Configuration (`.env`)

The `LLM_MODEL` prefix selects the provider *and* which key is read, so switching vendors is configuration rather than code:

```
# Gemini
LLM_MODE=live
LLM_MODEL=gemini/gemini-2.0-flash
GEMINI_API_KEY=...

# or OpenRouter
LLM_MODE=live
LLM_MODEL=openrouter/openai/gpt-oss-20b:free
OPENROUTER_API_KEY=sk-or-...
```

- Keys: <https://aistudio.google.com/apikey> · <https://openrouter.ai/keys>.
- Adding a provider is one line in `PROVIDER_KEYS` (`agents/shared/llm_client.py`) plus a key — no agent changes.
- The cache key includes the model, so **changing provider starts from a cold cache**; the first `live` run re-warms it.
- OpenRouter free-tier slugs rotate and are capped at 50 requests/day (failed requests still count). Warm the cache, then run in `cache` mode.

> **Note:** save `.env` as UTF-8 **without a BOM**. A BOM corrupts the first variable name and the key will read as unset. Avoid editing `.env` with Notepad or PowerShell `>` / `Set-Content` (they add a BOM); use your editor's "UTF-8 (no BOM)" or `Set-Content -Encoding utf8NoBOM`.

---

## Setup

Requires Python 3.12+, the [Temporal CLI](https://docs.temporal.io/cli), and a virtual environment.

```powershell
# from the repo root
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r agents/requirements.txt
```

---

## Running

### Fastest check — the lifecycle test (no server needed)

The whole 45-day lifecycle runs in ~1 second via Temporal's time-skipping test environment, in `mock` mode (no API cost):

```powershell
.\.venv\Scripts\python.exe -m pytest temporal_backend/tests/test_lifecycle.py -v
```

The first run downloads a Temporal test-server binary (one-time).

### Live run with the Web UI

```powershell
# Terminal 1 — dev server (Web UI at http://localhost:8233)
temporal server start-dev

# Terminal 2 — the worker
.\.venv\Scripts\python.exe -m temporal_backend.main

# Terminal 3 — launch and drive a container
.\.venv\Scripts\python.exe scripts/start_workflow.py
```

The worker accepts `--queue` (`pf-io` or `pf-reasoning`) and `--max-activities`.

`scripts/start_workflow.py` launches a `ContainerWorkflow` (deterministic id `container::MSKU7481920`) and drives the canonical lifecycle signal sequence so you can watch it live in the Web UI. Flags:

- `--no-drive` — start only; send signals yourself.
- `--step-seconds N` — delay between signals (default 3).

> **Wall-clock caveat:** a live dev server does not skip time. The workflow advances instantly through its `wait_condition` gates as each signal arrives, but internal polling `sleep`s (availability polls, slot scans, LFD checkpoints) run in real time. For the compressed 45-day run, use the time-skipping test.

### The operator console

Two artefacts, deliberately separate. `index.html` in the repo root is a **pitch page** — the seven leaks, the business case, and a browser-side *simulated* dashboard that is labelled as illustrative. The **console** under `frontend/` is the operator tool: live state, citations, and approvals, read from running workflows through `temporal_backend/api.py`. There is no database behind it — every read is a Temporal query, so the workflow state *is* the read model.

One-time frontend install:

```powershell
cd frontend
npm install       # requires Node 20+ (developed on v22.15.0 / npm 10.9.2)
```

Four processes, in this order:

```powershell
# Terminal 1 — dev server (Temporal Web UI at http://localhost:8233)
temporal server start-dev

# Terminal 2 — the worker.  PF_DEMO_MODE=1 makes the pre-LFD checkpoints fire
# in seconds instead of on the real calendar, which is what lets the approval
# gate be demonstrated live.  LLM_MODE=mock keeps it free and offline.
$env:PF_DEMO_MODE=1; $env:LLM_MODE="mock"
.\.venv\Scripts\python.exe -m temporal_backend.main

# Terminal 3 — the HTTP layer over the workflows
.\.venv\Scripts\python.exe -m uvicorn temporal_backend.api:app --port 8000

# Terminal 4 — the console at http://localhost:5173
cd frontend; npm run dev
```

Then open <http://localhost:5173> and press **Drive the demo** — it fires the same signal sequence as `scripts/start_workflow.py`, from the browser, in about ten seconds.

**What to look at, in order:**

| Screen | Shows |
|---|---|
| **Fleet** (`/`) | One card per container, live from `state()`. Phase, risk, prevented/contested. |
| **Container** (`/container/<id>`) | Phase strip, the LFD gap (Leak 01), the money panel with the stored **counterfactual**, the seven-leak ledger scored against real evidence, both arcs, the fully-cited dispute letter, and the evidence log where every row carries a `source_document_id`. |
| **Approvals** (`/approvals`) | Arcs genuinely parked on `wait_condition(...)`. Approving resumes a blocked workflow — it is not a simulated gate. |

Two human gates exist and both surface here, though they use different signals: the demurrage and detention arcs gate on `approve` (a spend above the cap), while `DisputeArc` gates on `approve_settlement` (a carrier offer below the 70 % mandate). The demo driver walks all the way through the second one, so the recovery actually lands.

**Environment variables**

| Variable | Where | Effect |
|---|---|---|
| `PF_DEMO_MODE=1` | worker | Pre-LFD checkpoints fire seconds after the checkpoint loop starts rather than on calendar dates. Demo only; off by default. |
| `PF_API_READONLY=1` | api | Disables every write route (signals, start, terminate). |
| `PF_API_PORT` | frontend | Backend port the Vite dev proxy targets. Defaults to `8000`. |

> **Ports:** the proxy assumes the API is on `8000`. If something else owns that port, start uvicorn elsewhere and set `PF_API_PORT` to match before `npm run dev`.

### Smoke-test the real LLM calls

```powershell
.\.venv\Scripts\python.exe scripts/smoke_llm.py
```

Runs each of the three genuine LLM agent tasks once against OpenRouter (requires `LLM_MODE=live` and a valid key).

---

## Project layout

```
agents/
  watcher/        extract_terms, classify_advisory, normalize
  strategist/     assess_risk, effective_lfd, price_options
  negotiator/     reserve_appointment, book_drayage
  auditor/        check_part_541, recompute_invoice
  case_builder/   assemble_evidence, draft_dispute
  shared/         llm_client, models, prompts, charges
  fixtures/       mock-mode LLM responses
temporal_backend/
  main.py         worker entrypoint
  api.py          FastAPI layer — every read is a Temporal query, no database
  activities/     registry.py — agents wrapped as activities
  workflows/      container, demurrage, detention, dispute
  shared/         converter.py — client, task queues, Pydantic converter
  tests/          time-skipping lifecycle, converter-gate, approval-visibility
scripts/          start_workflow.py, smoke_llm.py
frontend/         the operator console (Vite + React + TypeScript)
  src/services/   api.ts — typed fetch wrapper preserving HTTP status
  src/hooks/      polling useQuery wrappers
  src/components/ leak ledger, money panel, approval card, evidence table…
  src/pages/      FleetDashboard, ContainerDetail, ApprovalInbox
index.html        the pitch page (its dashboard is labelled illustrative)
```

---

## Testing

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Config lives in `pytest.ini` (`asyncio_mode = auto`, tests under `temporal_backend/tests`). The lifecycle test forces `LLM_MODE=mock`, so tests never make network calls or consume quota.
