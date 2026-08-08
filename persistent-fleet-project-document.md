# Persistent Fleet

**Autonomous demurrage & detention defence, built on durable agents**

| Document | Category | Stack | Status |
| --- | --- | --- | --- |
| PF-001 · Rev A | Ocean freight cost | Temporal + agentic AI | Hackathon submission |

## Contents

1. [Executive summary](#01--executive-summary)
2. [Introduction](#02--introduction)
3. [Glossary of terms](#03--glossary-of-terms)
4. [The short explanation](#04--the-short-explanation)
5. [The full process, explained](#05--the-full-process-explained)
6. [Worked example: one container](#06--worked-example-one-container)
7. [Every place money leaks](#07--every-place-money-leaks)
8. [Leak → agent → Temporal](#08--leak--agent--temporal)
9. [What the agents do](#09--what-the-agents-do)
10. [What Temporal does](#10--what-temporal-does)
11. [The workflow spine](#11--the-workflow-spine)
12. [System architecture](#12--system-architecture)
13. [Event taxonomy](#13--event-taxonomy)
14. [Guardrails](#14--guardrails)
15. [Proving the value](#15--proving-the-value)
16. [Build plan](#16--build-plan)
17. [Demo script](#17--demo-script)
18. [Questions judges will ask](#18--questions-judges-will-ask)
19. [Beyond the hackathon](#19--beyond-the-hackathon)

---

## 01 — Executive summary

> Every shipping container has a lifespan of weeks. Every AI agent has a lifespan of one conversation. That mismatch is why billions of dollars in avoidable penalty fees get paid every year — and it is exactly the mismatch Temporal closes.

When a container arrives at a port, two clocks start. One counts the days the box sits in the terminal; the other counts the days you keep the carrier's equipment after collecting it. Overrun either and the penalties — **demurrage** and **detention** — escalate daily. They are among the largest uncontrolled cost lines in ocean freight, and a substantial portion of what gets billed is either preventable or disputable.

Almost nobody prevents or disputes them, for one reason: the job requires *continuous attention across a 24-to-60 day window*, joining a contract PDF to a live event stream to an invoice that arrives five weeks later. Humans cannot hold ten thousand of those in working memory simultaneously. Neither can a chatbot, which forgets everything the moment the session ends.

Persistent Fleet gives every container its own agent, and gives that agent a lifespan that matches the container's. Built on Temporal, each agent sleeps for days, wakes on real-world events, prices its options, acts within a bounded mandate, escalates to a human when spend crosses a threshold, and — after the box is long gone — assembles and prosecutes the dispute. It survives crashes, deploys and restarts, because the workflow *is* the memory.

| Billed, one container | Defensible | Preventable for | Typically recovered |
| --- | --- | --- | --- |
| $3,860 | ~$2,400 | $340 | $0 |

Those figures come from the worked example in section 06 — one ordinary container having an ordinary bad month. Nothing exotic happens to it. That is the point.

---

## 02 — Introduction

### Why this problem

A good hackathon problem has three properties: the cost is a line item on someone's profit-and-loss statement today, the failure mode is well understood by the people who live it, and the fix is impossible without the technology you are showcasing. Demurrage and detention has all three.

It is not a forecasting problem or an optimisation problem. Nobody is short of algorithms here. It is an **attention problem**. The information needed to avoid the charge exists — it is simply scattered across a contract, an EDI feed, a terminal portal, a shared email inbox and an invoice, separated by up to six weeks. No single person or system ever holds all of it at once, and by the time the bill lands, the evidence that would have won the dispute has decayed.

### Why agentic AI

The work is genuinely judgement-heavy and genuinely unstructured. Reading free-time clauses out of a service contract, deciding whether a carrier advisory applies to *this* container at *this* depot, weighing a $340 expedite against an $1,850 risk, drafting a dispute that cites its evidence — these are not rules you can write down once. They are decisions, made repeatedly, on messy inputs.

### Why Temporal specifically

Here is the honest test, and the one judges should apply: *could you build this with a cron job and a queue?* No — and not marginally. The workflow sleeps for days at a time and must survive deploys. It parks indefinitely waiting for a human to tap approve in Slack. It takes real actions in real carrier systems that must be undone when customs intervenes an hour later. It must produce a permanent, replayable record of what the agent knew and when it knew it, because that record *is* the dispute evidence.

Each of those is a distributed-systems project on its own. Temporal makes them ordinary. That is the argument this document builds, section by section.

> **The framing to lead with**
>
> Today's AI agents are session-shaped: they wake, think, and die. The physical world is not session-shaped — a container spends 45 days in it. Temporal lets us give every container an agent that lives as long as the container's journey does.

---

## 03 — Glossary of terms

Everything below appears later in the document. The three charge types in the first block are the ones people most often confuse, and that confusion is itself a source of loss.

### The three charges

| Term | Definition |
| --- | --- |
| **Demurrage** | Charged by the **carrier** when a *full* container sits inside the terminal past its free time. You are being penalised for occupying yard space. On imports the clock runs from discharge (or from when the box is made available) until you collect it. Mental model: *your cargo in their yard*. |
| **Detention** (a.k.a. per diem) | Charged by the **carrier** when you hold their container *outside* the terminal past its free time. It runs from gate-out until you return the empty to the designated depot. Mental model: *their box in your yard*. |
| **Terminal storage** | Charged by the **terminal operator** — a different company from the carrier — for the same yard time demurrage covers. It usually has its own free-time allowance and its own counting rules. A container can legitimately incur demurrage and storage on the same day. |

### The clock

| Term | Definition |
| --- | --- |
| **Free time** | The number of days granted before charges begin. Set in the service contract for demurrage and detention, and in the terminal tariff for storage. Frequently different for each, and different per port. |
| **Last free day** (LFD) | The final day before charges start accruing. The single most important date in the entire process, and the one most often computed wrongly. |
| **Tier table** | The escalating rate schedule. A typical shape: a modest rate for the first few overdue days, roughly double for the next few, higher again after that. Escalation is why small delays produce disproportionate bills. |
| **Effective LFD** | Our term, not the industry's. The last free day adjusted for days the container was not actually available for collection. The gap between nominal and effective LFD is pure recoverable money. |

### Events and documents

| Term | Definition |
| --- | --- |
| **Discharge** | The container is lifted off the vessel. Nominally starts the demurrage clock. |
| **Availability** (grounded / appointable) | The moment the container can actually be collected — grounded, released, and bookable for an appointment. Often days after discharge. |
| **Gate-out** | The container physically leaves the terminal. Stops the demurrage clock and starts the detention clock in the same instant. |
| **Empty return** | The emptied container is accepted back at the depot. Stops the detention clock. Note *accepted* — arriving is not enough. |
| **Customs hold** (CET exam) | Customs blocks release, often moving the box to a Centralised Examination Station. The importer is legally forbidden to move it. Charges usually continue. |
| **Empty return restriction** | The carrier announces it will not accept empties at a given depot. Detention typically continues accruing anyway. |
| **Bill of lading** (B/L) | The contract of carriage between shipper and carrier. Establishes who is liable for what. |
| **Equipment interchange receipt** (EIR) | The document recording custody transfer of the container at a gate. Primary dispute evidence — it timestamps exactly when the box changed hands. |
| **EDI 315 / 322** | Standard electronic messages carrying container status events (315) and terminal operations events (322). The main machine-readable event feed. |
| **Service contract** | The negotiated agreement between shipper and carrier. Contains the free-time allowances and tier tables — usually as prose inside a PDF, not as structured data. |

### Parties and operations

| Term | Definition |
| --- | --- |
| **Carrier** | The shipping line. Owns the container and the contract with you. Bills demurrage and detention. |
| **Terminal operator** | Runs the port facility. Owns the yard and the appointment system. Bills storage. |
| **Consignee** | The party receiving the cargo. Usually the one who ends up paying. |
| **Drayage** | Short-haul trucking between the terminal and a nearby warehouse or depot. |
| **Chassis** | The wheeled frame a container sits on for road transport. Chronically short in some markets; a chassis shortage will strand a container as effectively as a customs hold. |
| **Merchant haulage** | You arrange your own trucking, and transact with the terminal directly — so terminal storage charges land on you separately. |
| **Carrier haulage** | The carrier arranges the inland move; terminal charges stay inside the carrier's account and reach you consolidated. |
| **Off-dock yard** | A depot outside the terminal. Moving a box there stops the terminal clock at the cost of an extra drayage leg later. |
| **Dispute window** | The period, commonly around 30 days from invoice, in which a charge may be formally contested. Miss it and the money is gone regardless of merit. |

### Technical terms

| Term | Definition |
| --- | --- |
| **Workflow** | In Temporal, a durable function whose entire execution — every step, every result — is recorded and can be replayed. It survives process crashes and redeploys. |
| **Activity** | A single unit of work called by a workflow. Anything non-deterministic — network calls, LLM calls, clock reads — must live in an activity. |
| **Signal** | An asynchronous message delivered into a running workflow from outside. How real-world events reach a sleeping agent. |
| **Query** | A synchronous read of a running workflow's current state, with no side effects. How the dashboard sees what each agent is thinking. |
| **Saga** | A pattern for multi-step operations where each completed step has a compensating action that undoes it if a later step fails. |
| **Entity workflow** | One long-lived workflow instance per real-world object — here, one per container. The architectural centrepiece of this project. |
| **Determinism** | The requirement that workflow code produce identical results when replayed. It is why the agent's reasoning lives in activities and never in the workflow itself. |

---

## 04 — The short explanation

> A shipping line owns the container. A terminal owns the yard. Both give you a limited number of free days to get your cargo out and their equipment back. Past those days, meters start running — and they run at escalating daily rates whether or not the delay was your fault.

Two clocks, running back to back. The first — **demurrage** — counts from the moment the box is discharged from the vessel until you collect it from the terminal. The second — **detention** — starts the instant the first stops, at gate-out, and counts until you return the empty container. A third clock, **terminal storage**, runs alongside the first, billed by a different company under different rules.

The charges exist for a real reason. A container only earns money while it is moving, so an idle box is a depreciating asset occupying a finite yard slot and slowing every other move around it. Escalating penalties are how the industry prices that. But the same mechanism penalises delays you could not possibly control: a customs exam, a terminal with no appointment slots, a carrier that refuses to take its own empty back.

**The failure is not that people don't understand the rules.** It is that the rules live in a contract, the events live in a terminal system, and the charge arrives on an invoice weeks later — three different places, three different formats, no one reconciling them under time pressure. By the time anyone looks, the deadline has passed and the evidence has evaporated.

---

## 05 — The full process, explained

This section walks the entire lifecycle slowly, in the order it happens. If you only read one section to understand the domain, read this one.

### Stage 1 — Before arrival

The carrier issues an **arrival notice** some days before the vessel berths. In a well-run operation the customs entry is filed in advance, the drayage carrier is put on notice, and the warehouse has a receiving slot pencilled in. Crucially, the free-time terms that will govern the next month are already fixed — they were agreed months earlier in the service contract, and they are sitting in a PDF that nobody has opened since.

### Stage 2 — Discharge, and the first clock starts

The vessel berths and the container is lifted off. **This nominally starts the demurrage clock.** If the contract grants five free days, the last free day is five days from now, and after that the escalating tier table applies.

But discharge and availability are not the same event. A container lifted off a vessel typically lands in a stack, and until the terminal grounds it and makes it appointable, no trucker on earth could collect it. That period is often one to three days. The clock does not care.

### Stage 3 — The waiting period

This is where the outcome is decided, and where almost nothing happens. Several things may go wrong, in any combination:

- **Customs places a hold.** The container may be moved to an examination station. It is now legally and physically out of reach. Exams routinely run a week or more.
- **The terminal has no appointment slots.** Congested terminals ration access through appointment systems that fill within minutes of opening. A trucker who cannot get a slot cannot collect the box, however willing.
- **Chassis are short.** No frame, no move.
- **The warehouse cannot receive.** The consignee's own dock is full.

The intervention window is here, and it is cheap. Paying a premium trucker a few hundred dollars on day three costs a fraction of what four days in the top tier costs on day nine. But nobody makes that call, because nobody is watching the countdown.

### Stage 4 — Gate-out, and the clocks swap

The container finally leaves the terminal. The demurrage clock stops and the detention clock starts *in the same instant*. The importer's ops team mentally files the container as "done" — the cargo is on its way. In cost terms, the second half has just begun.

### Stage 5 — Unload and return

The cargo is delivered and stripped from the container, often within a day or two. From the consignee's point of view, finished. The empty box now has to get back to the carrier's designated depot, and this is where a quietly enormous amount of money is lost, because **returning an empty is not always possible.** Carriers routinely announce return restrictions when depots are full — the trucker drives out, is turned away, and parks the box in his own yard. Detention accrues every one of those days.

### Stage 6 — The invoice, weeks later

Three to six weeks after discharge, invoices arrive: demurrage from the carrier, storage from the terminal, detention from the carrier again. The ops team that lived through this container has since handled four hundred others. The dispatcher who could confirm the appointment system was empty on the 13th may have left. Reconstructing the story takes hours.

So finance does the rational thing and pays anything under a threshold without review. That threshold is not a policy — it is an admission that disputing costs more than paying.

> **The quiet one**
>
> Carriers frequently hold cargo release pending payment. So the importer pays under protest to free the goods, fully intending to dispute later. Later never comes, and payment made under duress becomes payment accepted.

---

## 06 — Worked example: one container

Container `MSKU 748192-0`, a 40-foot high cube of garden furniture, Ningbo to Los Angeles, delivering to a warehouse in the Inland Empire. An ordinary container having an ordinary bad month.

**The terms.** The service contract grants 5 free days of demurrage from discharge. The terminal tariff grants 4 free days of storage. Detention free time is 5 days from gate-out. Three different allowances, three different counting rules, living in three different documents.

| Date | Event | Consequence |
| --- | --- | --- |
| Mar 1 | Arrival notice issued | — |
| Mar 3 | Vessel discharges, 06:00 | Demurrage clock starts. Nominal LFD = Mar 8 |
| Mar 3–5 | Container ungrounded, not appointable | Two free days consumed before anyone could act |
| Mar 4 | Customs places CET exam hold | Box moved to examination station; legally immovable |
| Mar 8 | Last free day passes | Demurrage begins on a container nobody may touch |
| Mar 12 | Customs releases | Nine days of hold, all billed |
| Mar 12–15 | Three failed appointment attempts | Terminal has no slots. Dispatcher screenshots the empty grid |
| Mar 15 | Gate-out | Demurrage stops at 7 billed days. Detention starts. Due Mar 20 |
| Mar 17 | Cargo delivered and unloaded | Consignee considers the container finished |
| Mar 19–24 | Carrier empty return restriction | Trucker turned away at depot; empty parked in his yard |
| Mar 26 | Empty accepted | Detention stops at 6 billed days |

Here is what the clocks look like against each other. The circled numbers mark the leaks detailed in section 07.

**Figure 1 — Three clocks, two companies, one container.**

```
DEMURRAGE — CARRIER · FULL BOX IN TERMINAL
[ 5 free days ][ 7 days billed · $1,550 ]        (leaks 1, 2, 3)

TERMINAL STORAGE — TERMINAL OPERATOR · SAME YARD TIME
[ 4 free ][ 8 days billed · $1,350 · different company ]

DETENTION — CARRIER · THEIR BOX IN YOUR HANDS
                          [ 5 free days ][ 6 days billed · $960 ]   (leak 4)

Mar 3        Mar 8          Mar 12      Mar 15      Mar 20     Mar 26
discharge    last free day  exam rel.   gate out    empty due  empty in
```

Gate-out on Mar 15 is the handoff point: demurrage stops and detention starts in the same instant. Total billed: **$3,860**.

---

## 07 — Every place money leaks

The charge is not the leak. The leak is everywhere the organisation fails to prevent, contest, or even notice the charge. There are seven, and they compound.

### Leak 01 — 2 days · The clock started before you could act

March 3 to 5, the box was buried in the stack, ungrounded and unappointable. Two days of a five-day allowance were consumed by the terminal's own handling, not by anything the importer did.

Almost nobody adjusts the last free day for this, because it requires the availability timestamp — which lives in a terminal portal nobody checked on day two. Those two days push you two days deeper into the escalating tier, and the top tier is where the money is.

### Leak 02 — 4 days · Billed for days you were forbidden to move

The customs exam accounts for four of the seven billed demurrage days. The container was physically at an examination station. There was no action the importer could have taken.

A penalty that cannot change behaviour is not an incentive — it is just a fee, and that is precisely the argument that makes these days contestable. Most importers pay them anyway, because nobody assembled the exam timeline while it was fresh.

### Leak 03 — 3 days · Billed for the terminal's own capacity failure

Three days lost because the appointment system had no slots. This is the strongest kind of dispute and the one most often lost, because it depends on evidence that decays: a screenshot of an empty appointment grid on March 13.

If the dispatcher did not capture it, that day is unprovable by March 20. **Evidence perishes faster than the invoice arrives.**

### Leak 04 — 4 days · Detention on a box the carrier refused to take back

Four of the six detention days exist because the carrier itself would not accept the empty. The consignee is being charged for holding equipment they actively tried to return.

On paper this is close to indefensible for the carrier — yet it is paid constantly, because the restriction was announced in an advisory email to a shared inbox, and nobody connected that email to this container's invoice five weeks later.

### Leak 05 — $1,510 · Nobody knew the deadline until it had passed

The contracted free time sat in a PDF. The discharge event sat in an EDI 315. Nothing joined them. There was no moment on March 6 when a system said *you have two days left and no appointment booked*.

The cheapest intervention available — $340 to expedite a trucker — was never even considered, because the decision point passed in silence. Preventing this charge cost $340. The charge cost $1,850.

### Leak 06 — all of it · The dispute window closed before the dispute started

The invoice arrives around April 10, five weeks after discharge. Carrier tariffs commonly allow thirty days to dispute. Meanwhile the team has moved on to four hundred other containers and reconstructing this one takes half a day.

So finance pays anything below a review threshold. Every leak above becomes permanent at this moment, regardless of merit.

### Leak 07 — silent · Paid under duress, then never revisited

Release of the cargo is often withheld pending settlement. The importer pays to free the goods, intending to dispute afterwards. Nothing captures that intent, no process carries it forward, and the protest quietly becomes acceptance.

### The scoreboard

Of $3,860 billed on this one container, roughly **$2,400 is defensible with evidence** — the exam days, the appointment failures, the empty return restriction, and the two days of unavailability that should have shifted the last free day. A further slice was preventable outright for a few hundred dollars of expedite cost on day three.

What actually happens in most organisations: the full $3,860 is paid. Not because anyone decided to, but because **no single person ever held the contract terms, the event stream and the invoice in their head at the same time.**

---

## 08 — Leak → agent → Temporal

This is the core mapping of the project. Read the third column top to bottom: every entry requires state that outlives a process. That is the whole argument for Temporal, and the answer to "why not a cron job and a queue?"

| Leak | What fails today | What the agent does | Temporal primitive |
| --- | --- | --- | --- |
| 01 | Clock starts at discharge, but the box is not collectable for two more days | Polls availability from discharge; when the box is ungrounded, recomputes the *effective* LFD and stamps the gap as cited evidence | Long-running workflow holding contract terms as state; activity retries against a flaky terminal portal |
| 02 | Customs hold timestamps reconstructed weeks later, if at all | Captures hold-placed and hold-released the moment each occurs, and suppresses pointless intervention while the box is immovable | Signal handlers on the live workflow; event history as a tamper-evident audit log |
| 03 | Failed appointment attempts leave no trace | Attempts booking on a schedule and records every failure as timestamped evidence while it is still provable | Durable timer loop; each attempt is an activity, so failures persist permanently in history |
| 04 | Carrier advisory sits unread in a shared inbox | Ingests advisories, matches them to this container's return depot, freezes the defensible window | Signal from an external feed into a workflow that is already sleeping |
| 05 | No system ever says "two days left, no appointment booked" | Wakes at LFD−72h, −48h, −24h, prices the options, escalates with a costed recommendation rather than a raw alert | `workflow.sleep()` measured in days, surviving deploys, crashes and restarts |
| 06 | Dispute window expires while the team is busy | Case is pre-assembled *before* the invoice arrives; the dispute opens with evidence already attached and follows up on a durable cadence | Child workflow spawned at gate-out, running weeks beyond the parent's arc |
| 07 | Payment under protest is never revisited | Records the protest as workflow state and continues prosecuting recovery after payment | Workflow outliving the commercial event that triggered it; `continue-as-new` for long tails |

> **The line for the slide**
>
> Seven leaks, one root cause: no system held the contract, the events and the deadline in the same place for twenty-four consecutive days. Temporal gives the agent a lifespan that matches the container's.

---

## 09 — What the agents do

Five narrow agents rather than one general one. Narrow agents are easier to prompt, easier to evaluate, easier to constrain, and easier to explain to a judge in thirty seconds each.

**Figure 2 — Agent topology.**

```
Watcher  ─────────────►  Strategist  ─────────────►  Negotiator
(normalises events       (prices options,            (books, rebooks,
 and contracts)           predicts outcome)           requests extensions)
   │                          ▲
   │ invoice arrives          │ priors
   ▼                          │
Auditor  ──────►  Case builder  ──────►  Learning store
(recomputes       (cited evidence         (priors, per port
 the invoice)      chronology)             and per carrier)
```

The dashed return path is the learning loop — outcomes become priors that sharpen the next container's assessment.

### Watcher — ingest and normalise

Turns a mess of formats into one clean event model: EDI 315 and 322 messages, vessel schedules, terminal portal scrapes, customs status codes, carrier advisory emails, gate receipts, equipment interchange receipts and invoice PDFs.

Three jobs here are genuinely hard and genuinely agentic. **Resolving identity** — the same container appears as `MSKU7481920`, `MSKU 748192-0` and "the Ningbo box" across four systems. **Extracting terms** — pulling free-time clauses, tier tables, weekend rules and per-port exceptions out of a contract PDF into structured parameters. **Classifying unstructured comms** — reading a carrier advisory and deciding whether it affects this container at this depot. It also flags anomalies: an event contradicting an earlier one, or an expected event that never arrived.

### Strategist — predict and price

Computes the effective last free day from actual availability rather than nominal discharge. Forecasts the probability of gate-out before the LFD given chassis supply, appointment density, trucker capacity and terminal congestion. Then produces a **costed options table** — not a recommendation, a menu with expected values.

| Option | Cost | P(success) | Expected saving |
| --- | --- | --- | --- |
| Do nothing | $0 | — | $0 |
| Expedite drayage | $340 | 0.72 | $1,190 |
| Move to off-dock yard | $220 | 0.85 | $980 |
| Request free-time extension | $0 | 0.35 | $650 |

The crucial output is the **counterfactual**: what the agent predicts will happen if nothing is done. That prediction is stored, then checked against reality later — which is what makes the savings claim auditable rather than assertable.

### Negotiator — act on the world

Books and rebooks drayage. Secures terminal appointments, retrying as slots open. Requests free-time extensions with a reasoned case attached. Negotiates rate and timing with truckers within a bounded mandate. Coordinates chassis. Drafts and sends the messages, then manages the follow-up cadence over weeks.

The bounded mandate is the whole game: hard spend caps, no contract acceptance, mandatory escalation on anomalies, and a defined settlement band it may accept without asking.

### Auditor — validate the invoice

Underrated, and judges respond to it. When the invoice arrives the agent recomputes the charge from first principles: were the tiers applied per contract, were weekends and holidays counted per the contract's rule, is the free time correct for this contract and this port, is the same day billed twice under two labels, is the right legal party being charged. A meaningful share of charges are simply *wrong*, independent of whether the underlying delay was justified.

### Case builder — prosecute the dispute

Assembles the evidence chronology with a source document cited for every factual claim — **no citation, no claim.** Drafts the dispute letter. Then runs the dispute as a long-lived process: submit, follow up on schedule, escalate through carrier tiers, evaluate partial settlement offers against expected recovery, accept within mandate or hand to a human.

### The learning loop

Per-port, per-carrier, per-terminal statistics on what actually works. Which dispute arguments win against which carrier. How far ahead appointments genuinely need booking at each terminal. Which lanes chronically underperform. That feeds back into the Strategist's priors — and upward into procurement: *this carrier's free time is nominally five days but effectively three at this port, so price the next contract accordingly.*

---

## 10 — What Temporal does

Each primitive below earns its place with a concrete job. This is the section to memorise before judging.

| Primitive | What it does here |
| --- | --- |
| **Entity workflows** | One workflow instance per container, alive for its full 24-to-60 day life. Ten thousand containers, ten thousand live agents. The architectural centrepiece. |
| **Durable timers** | `sleep()` measured in days. Checkpoints at LFD−72h, −48h, −24h, then escalating daily reassessments. Survives deploys, crashes and restarts. |
| **Signals** | External events interrupting a sleeping workflow: customs hold placed and released, gate-out scanned, carrier advisory published, empty accepted, invoice received, human approval granted. |
| **Queries** | The control tower reads live agent state straight from the workflow — risk score, days remaining, options under consideration, pending approvals. No separate read model to keep in sync. |
| **Updates** | Synchronous request-response into a running workflow when a human overrides a decision and needs an immediate answer back. |
| **Child workflows** | Gate-out spawns the detention workflow. Invoice arrival spawns the dispute workflow, which outlives its parent by weeks. Clean boundaries mirroring the real-world handoffs. |
| **Activity retries** | Terminal portals and carrier APIs fail constantly. Exponential backoff, per-activity timeouts, non-retryable classes for genuine business failures. The agent never reasons around a flaky HTTP call. |
| **Sagas** | Booked a trucker, then a hold landed — cancel the booking, release the slot, notify the depot. Explicit compensating activities executed in reverse. |
| **Heartbeats** | Slow terminal scrapes and large document extractions report progress, so a stuck worker is detected rather than hanging silently. |
| **Continue-as-new** | Containers that drag on accumulate large histories; restart the workflow with condensed state and keep going. |
| **Event history** | Every decision, input, LLM call and output permanently recorded and replayable. In a dispute process this is not infrastructure — it *is* the product. |
| **Search attributes** | Query the whole fleet: all containers at LAX with an LFD inside 48 hours and no booked appointment. The dashboard's filter layer, for free. |
| **Task queues** | Route LLM-heavy activities to GPU-backed workers and cheap I/O to lightweight ones. Rate-limit a carrier API across the entire fleet at the worker level. |
| **Schedules** | Daily reconciliation sweeps, weekly carrier performance rollups, month-end accrual reporting. |
| **Time-skipping tests** | Compresses 45 days into 30 seconds. Both the test harness and the demo weapon. |
| **Versioning** | Deploy new agent logic while ten thousand workflows are mid-flight without breaking the ones already running. |

---

## 11 — The workflow spine

Three workflows mirror the three real-world arcs. Nothing in workflow code may be non-deterministic — no LLM calls, no clock reads outside `workflow.now()`, no randomness, no I/O. All of that lives in activities.

**Figure 3 — The deterministic spine.**

```
Workflow starts at discharge
  (contract free time loaded as state)
             │
             ▼
Risk checkpoint loop                     ↻ repeats until gate-out
  (durable sleep, wakes near the LFD)      or wakes early on a signal
             │
             ▼
Agent reasons and prices options
  (LLM call inside an activity)
             │
             ▼
Approval gate above threshold            parks indefinitely, safely
  (workflow waits on a signal)
             │
             ▼
Dispute child workflow
  (outlives the parent by weeks)
```

Reasoning happens inside activities so it can be replayed; orchestration happens in the workflow so it can be trusted.

### The five lines that carry the argument

#### 1. Sleeping for days, waking on an event

A durable timer raced against a signal. The agent sleeps through the boring parts but wakes instantly when customs places a hold. This one method is "why Temporal" in eight lines.

```python
# race a multi-day timer against any incoming signal
try:
    await workflow.wait_condition(
        lambda: self.reassess_now or self.gated_out_at is not None,
        timeout=target - workflow.now(),
    )
    return True          # woke early — something happened
except asyncio.TimeoutError:
    return False         # reached the scheduled checkpoint
```

#### 2. The approval gate

No polling loop, no state machine, no expiring job. The workflow parks for as long as the human takes — minutes or days.

```python
if choice.requires_approval or choice.cost_usd > AUTO_APPROVE_LIMIT_USD:
    await workflow.execute_activity(act.notify_human, ...)
    await workflow.wait_condition(lambda: choice.action in self.approvals)
```

#### 3. Compensation, so the agent leaves nothing broken behind

This is what stops an autonomous agent creating phantom bookings and no-show fees in real carrier systems. Note that a missing appointment slot *raises* rather than returning — it is not an error condition, it is evidence, recorded before the unwind.

```python
compensations = []
try:
    booking = await workflow.execute_activity(act.book_drayage, ...)
    compensations.append((act.cancel_drayage, booking))

    slot = await workflow.execute_activity(act.reserve_appointment, ...)
    if slot is None:
        self.events.append(MilestoneEvent(kind="appointment_unavailable", ...))
        raise NoSlotAvailable()          # leak 03, captured as evidence
    compensations.append((act.release_appointment, slot))
except Exception:
    for fn, arg in reversed(compensations):
        await workflow.execute_activity(fn, arg, ...)
```

#### 4. The handoff between clocks

Gate-out ends the demurrage arc and begins the detention arc. `ABANDON` is what lets the second clock outlive the first.

```python
await workflow.start_child_workflow(
    DetentionWorkflow.run, args=[container, terms],
    id=f"detention::{container.container_id}",
    parent_close_policy=ParentClosePolicy.ABANDON,
)
```

#### 5. The dashboard's read path

No projection table, no cache invalidation, no eventual consistency. The workflow *is* the read model.

```python
@workflow.query
def state(self) -> dict:
    return {
        "effective_lfd": str(self.effective_lfd),
        "holds": sorted(self.holds),
        "awaiting_approval": self.pending_approval,
        "counterfactual_usd": self.counterfactual_usd,
    }
```

> **The determinism boundary**
>
> Workflow code replays identically forever, so it cannot contain an LLM call, a random number or a wall-clock read. The agent's reasoning goes inside activities; the workflow only orchestrates. Judges who know Temporal will probe this, and getting it right is the difference between "used Temporal" and "understood Temporal".

---

## 12 — System architecture

**Figure 4 — Architecture.**

```
EVENT SOURCES
[ Carrier & EDI ]  [ Terminal portals ]  [ Customs ]      [ Documents ]
 315, 322,          availability,         holds and        contracts, EIRs,
 advisories         slots                 releases         invoices
       │                  │                   │                 │
       ▼                  ▼                   ▼                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Normalisation and extraction — Watcher agent                         │
│ identity resolution · term extraction · advisory classification      │
│                              → signals                               │
└──────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
╔══════════════════════════════════════════════════════════════════════╗
║ TEMPORAL CLUSTER — ONE WORKFLOW PER CONTAINER                        ║
║  [ DemurrageWorkflow ] → [ DetentionWorkflow ] → [ DisputeWorkflow ] ║
║    discharge→gate-out      gate-out→empty return   invoice→settlement║
║  Activity workers — reasoning queue (LLM) and I/O queue (portals)    ║
║  Event history serves as the permanent, replayable audit trail       ║
╚══════════════════════════════════════════════════════════════════════╝
        │                                           │
        ▼                                           ▼
[ Control tower ]                          [ Outbound actions ]
 queries for live state ·                   bookings · appointments ·
 search attributes for fleet filters        extension requests · disputes
```

Signals flow down into sleeping workflows; queries flow back out to the dashboard. Nothing between the ingestion layer and the UI needs its own database.

Two design decisions are worth defending explicitly.

**No separate state store.** Conventional agent architectures bolt a vector database on and call it memory. Here the workflow state *is* the memory — durable by construction, queryable by construction, and consistent with the audit log by construction, because they are the same thing.

**Split task queues.** LLM activities go to a reasoning queue with low concurrency and generous timeouts; portal scrapes go to a cheap I/O queue with aggressive retries. This also gives you fleet-wide rate limiting against a carrier API for free, at the worker level, without any coordination between workflows.

---

## 13 — Event taxonomy

Every event carries a source document identifier. This is not bookkeeping pedantry — it is what turns a log into admissible dispute evidence, and it is the constraint that keeps the Case Builder honest.

| Event | Delivered as | Effect on the agent |
| --- | --- | --- |
| `discharged` | Workflow start | Starts the demurrage arc, sets nominal LFD |
| `container_available` | Poll or signal | Shifts the effective LFD; the gap becomes leak-01 evidence |
| `hold_placed` | Signal | Suppresses intervention; opens a defensible window |
| `hold_released` | Signal | Closes the window; triggers immediate reassessment |
| `appointment_unavailable` | Activity result | Recorded as leak-03 evidence while it is still provable |
| `appointment_booked` | Activity result | Lowers risk; sets an expected gate-out |
| `gate_out` | Signal | Ends demurrage, spawns the detention child workflow |
| `carrier_advisory` | Signal | Matched against this container's depot; may open leak-04 |
| `empty_returned` | Signal | Ends detention |
| `invoice_received` | Signal | Spawns the dispute workflow; starts the dispute-window timer |
| `carrier_replied` | Signal | Advances or closes the dispute |
| `approval_granted` | Signal | Releases a parked workflow |

Three fields make an event usable: `occurred_at` (when it happened in the world, not when we learned of it), `source_system`, and `source_document_id`. An event without the third is a rumour and cannot appear in a dispute.

---

## 14 — Guardrails

An agent that spends money and sends messages on a carrier's systems needs bounds that are structural, not merely prompted. Every one of these is enforced in workflow code, where it can be audited, rather than in a system prompt, where it can be talked around.

- **Hard spend cap per action and per container.** Anything above the auto-approve limit parks on a signal and waits for a human, indefinitely.
- **No contract acceptance.** The agent may request an extension; it may never agree to amended terms.
- **Bounded settlement authority.** The Case Builder may accept an offer above a defined fraction of the claim, and must escalate anything below it.
- **Mandatory escalation on anomaly.** Contradictory events, missing expected events, or an assessment the agent flags as low-confidence all route to a human.
- **Compensation is required, not optional.** Any action with a real-world side effect must register a compensator before the next step runs.
- **No claim without a citation.** The Case Builder cannot assert a fact that has no `source_document_id`. This is enforced at assembly time, not requested in a prompt.
- **Full replayability.** Every input to every decision is in event history, so any disputed action can be reconstructed exactly as the agent saw it.

> **The honest limitation**
>
> The agent's predictions are only as good as its priors, and at launch it has none. Early deployment should run in shadow mode — assessing and recommending without acting — until the counterfactual record shows its calls are calibrated. Say this out loud to judges; claiming otherwise invites the obvious question.

---

## 15 — Proving the value

Judges are trained to distrust AI savings numbers, so the metric design matters as much as the metric. Be the team that shows its error bars.

### The counterfactual ledger

For every intervention the agent stores what it predicted would happen if it did nothing. Because the simulation continues to run, that prediction gets scored against what actually occurred. The dashboard therefore does not *claim* savings; it *evidences* them, and it shows the cases where the agent was wrong.

### Metrics worth showing

| Metric | Why it matters |
| --- | --- |
| **Charges prevented** | Demurrage and detention avoided against the counterfactual baseline |
| **Charges recovered** | Dispute wins, in dollars and as a share of amounts contested |
| **Dispute rate** | Share of eligible charges actually contested. Today this is near zero, which is the point |
| **Evidence completeness** | Share of billed days with a cited source document. Predicts win rate better than anything else |
| **Intervention precision** | Share of interventions that actually prevented a charge. Guards against an agent that spends freely to look busy |
| **Ops hours avoided** | Manual reconciliation and dispute-assembly time displaced |
| **Prediction calibration** | Predicted versus realised charges. The number that earns the right to act autonomously |

### The demo comparison

Run the same synthetic fleet twice — agents on, agents off — and show both counters side by side. One toggle delivers the entire business case without a single bullet point.

---

## 16 — Build plan

Depth on one container, scale on the map. That combination is what a demo needs.

### Build fully

- The Watcher over a synthetic event stream for roughly 200 containers, with realistic distributions of customs holds, chassis shortages and terminal congestion
- The Strategist with a real costed options table and a stored counterfactual
- The checkpoint timer loop, the approval signal, and one complete intervention path with compensation
- The Auditor's recompute — tiers, weekend counting, double-billing detection
- The Case Builder's cited chronology and drafted dispute
- The control tower: fleet map, per-container reasoning trace, and the Temporal event history viewer beside it

### Mock credibly

- Carrier and terminal APIs — mock services over the synthetic event generator. Nobody expects a live carrier integration
- Outbound email and Slack — log and display rather than send
- The learning store — seed the priors and show the shape of the loop
- Seed a real document set: gate receipts, equipment interchange receipts and invoices as PDFs, so the Case Builder parses something genuine

### Claim as roadmap, do not build

- Container-to-container agent negotiation
- Procurement feedback into contract renewal
- Full carrier tier escalation and legal referral

> **The one thing not to skip**
>
> Temporal's time-skipping test environment. It is both your test harness and the single most persuasive ten seconds of the demo. Wire it early.

---

## 17 — Demo script

Design everything backwards from three moments. Open cold — no architecture slide. Just the live map and a running counter.

### Moment one — time collapse

Zoom into `MSKU 748192-0`. Run its 45-day life in about thirty seconds: the agent sleeping, waking at LFD−72h, detecting the chassis shortage, rebooking the trucker, sleeping again. Say the line out loud: *that was forty-five days, and every decision you just watched is durably recorded.*

### Moment two — the kill

Mid-negotiation, kill the worker on stage. The dashboard freezes. Wait three full seconds — let it be uncomfortable. Restart. The agent resumes mid-thought, same container, same step, memory intact. *An agent framework would have lost this. The container is still in the world, so the agent is still alive.*

This is the most persuasive ten seconds available to you, because every judge has watched an LLM agent die and lose everything.

### Moment three — the swarm

Zoom out. Ten thousand dots, colour-coded by risk. Flip the toggle to **agents off** and watch the map bloom red as containers blow through free time and the cost counter runs. Flip it back and watch interventions fire in waves, the map cool, the counter slow.

### In between

Let one decision cross the spend threshold so the workflow parks and pings Slack. Approve it from your phone, on stage, and watch the workflow resume. Human-in-the-loop you can *see* beats any slide about governance.

### Close

Thirty seconds of architecture — spine, activities, signals, queries, sagas — and then the reframe: *we did not build a logistics app. We built a way to give agents a lifespan that matches the real world. Every container, every reefer, every aircraft component, anything that exists over weeks can now have a mind that persists over weeks.*

---

## 18 — Questions judges will ask

**Why not a cron job and a queue?**

Because the process sleeps for days and must survive deploys, parks indefinitely on human approval, takes real actions that must be undone when customs intervenes an hour later, and must produce a permanent replayable record. Each is a distributed-systems project on its own. Temporal makes all four ordinary.

**Why not LangGraph or a standard agent framework?**

Those manage reasoning within a session. Nothing here is within a session. The unit of work is twenty-four to sixty days long and the agent is asleep for most of it. Reasoning frameworks and durable execution solve different problems — we use an LLM for reasoning and Temporal for lifespan.

**How do you keep the workflow deterministic with an LLM in it?**

We don't put the LLM in the workflow. Every model call is an activity, so its result is recorded in history and replayed rather than re-executed. The workflow only orchestrates.

**What happens when the agent is wrong?**

Spend caps bound the damage, compensation undoes side effects, and the counterfactual ledger surfaces the error rather than hiding it. Early deployment runs in shadow mode until calibration is demonstrated.

**Is this legally sound? Can an AI file a dispute?**

The agent assembles and drafts; a human submits above a threshold. Nothing here changes who is legally responsible. The evidence chronology it produces is stronger than what a human assembles five weeks later, because it was captured contemporaneously.

**Ten thousand concurrent workflows — does that actually scale?**

Workflows sleeping on timers consume no worker capacity. Concurrency is bounded by activity execution, not by how many agents are alive, which is why one agent per container is affordable at all.

**How do you deploy new agent logic without breaking live containers?**

Temporal's versioning. Workflows already in flight continue on the logic they started with; new ones pick up the new path.

**What is the hardest part you have not solved?**

Identity resolution across carrier, terminal and customs systems, and extracting free-time terms reliably from contracts that are written as prose. Both are tractable and both are honest answers.

---

## 19 — Beyond the hackathon

### Nearest adjacencies

The same shape — a durable agent, a hard external deadline, evidence that decays — recurs across logistics.

- **Cargo claims and OS&D.** Damage found at delivery triggers a workflow that gathers photos and inspection reports, determines liability against carrier terms, files within the statutory notice window, and pursues recovery over months.
- **Trade compliance.** Classification, denied-party screening, and the long asynchronous loop with brokers and authorities. Here the audit trail is a regulatory requirement, not a nice-to-have.
- **Cold chain intervention.** A temperature excursion in transit; the agent computes remaining shelf life and chooses between reroute, re-ice, expedite or divert — with compensation if the reroute fails.

### The general claim

Persistent Fleet is a demurrage product, but the pattern is not about demurrage. It is **one durable agent per physical object, for as long as that object exists in the world.** Anything with a lifespan measured in weeks and a cost of inattention measured in dollars fits the same architecture: reefer containers, aircraft components under airworthiness deadlines, pharmaceutical shipments, construction equipment on hire.

That is the claim worth making on the final slide. The demurrage case proves it with real money; the pattern is what generalises.
