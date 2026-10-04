# VISION → Reality alignment

`docs/VISION.md` is the north-star. This file answers the only question that matters when
reading it: **of what it describes, what does ALEXIS actually do?**

It exists because `VISION.md` §69 rule 20 requires capabilities that cannot be implemented to
be *registered* as `PLANNED`/`MISSING`/`UNAVAILABLE`, and rule 4 forbids marking anything
complete without real implementation and tests. A 2000-line target document with no
reconciliation would do the opposite of both rules: it would let aspiration read as fact.

**Verification date**: 2026-10-03 · commit `354a388` · **1354 deterministic tests passing**.

## How to read the statuses

| Status | Meaning |
|---|---|
| `IMPLEMENTED` | Real code, wired in the production runtime (`apps/demo`), covered by tests |
| `PARTIAL` | Real code, but a narrower slice than the vision describes |
| `CONTRACT` | Interfaces and data structures exist; no working behaviour behind them |
| `MISSING` | Nothing, but the architecture has a place for it and it is registered |

Nothing is marked complete on the strength of a doc, a class name, or a passing isolated test.

---

## 1. Foundation — what genuinely works today

| Vision § | Capability | Status | Evidence |
|---|---|---|---|
| §2 | Canonical loop | `PARTIAL` | `alexis/cognition/loop.py` — decide→policy→execute→observe→evaluate→replan→verify. Missing: reflection→learn→update as an automatic stage |
| §4.1 | Intelligence separate from capabilities | `IMPLEMENTED` | `alexis/cognition/planner_model.py` proposes; `PlanValidator` disposes; `PolicyEngine`/`AutonomyGate` authorize |
| §4.2 | Data is not authority | `IMPLEMENTED` | Untrusted content is marked as data in every prompt path; `ClaimGuard` (`alexis/cognition/evidence.py`) never promotes a model claim to FACT |
| §4.3 | Autonomy inside explicit authority | `IMPLEMENTED` | `MissionEnvelope`, `alexis/security/policy.py`, `alexis/autonomy/gates.py` |
| §4.4 | No fake capabilities | `IMPLEMENTED` | Capability catalog carries a real `status` per spec; `available()` ≠ `enabled()` |
| §4.5 | Security outside model authority | `IMPLEMENTED` | No path lets a plan widen its envelope, lower its risk, or drop `requires_approval` — tested in CORE-08/08B.1 |
| §4.6 | Completion requires independent verification | `IMPLEMENTED` | `Mission.__setattr__` refuses `COMPLETED` without a confirming `GoalVerification` (`354a388`) |
| §5 | Self Model | `PARTIAL` | `alexis/self/model.py`; learned state persists (`93c6930`). Missing: goals, commitments, skills, health, model_state |
| §7 | World Model | `PARTIAL` | `alexis/world/model.py` — entities, scopes, staleness, conflicts, provenance, persistence. Only FILE/TEST domains; no projects, devices or time |
| §8 | Memory | `PARTIAL` | Episodic + working implemented (`alexis/memory/provider.py`). Semantic/procedural/project/skill memory absent |
| §11 | Success criteria | `IMPLEMENTED` | `GoalVerifier` checks tool-observed predicates against real observations; a model assertion is never enough |
| §13 | Replanning and recovery | `IMPLEMENTED` | Deterministic filter (`loop.py`), plus dynamic replanning via `ModelTask.PLAN` with a validated overlay (`31ab3b8`) |
| §17 | Capability system | `PARTIAL` | 30 specs, **15 with a real adapter**. Each declares inputs, outputs, side effects, risk, sandbox profile, verification |
| §22 | Model Router | `IMPLEMENTED` | `alexis/models/router.py` — REAL/DEGRADED/UNAVAILABLE, chain, per-mission budgets (`c7a259e`), full provenance (`8bfc102`) |
| §32 | Persistence and recovery | `PARTIAL` | PostgreSQL for missions, world, knowledge, evidence, audit. Restart resumes a mission; destructive replay is not implemented |
| §35 | Security architecture | `PARTIAL` | Identity→Capability→Policy→Envelope→Gate→Sandbox→Execute→Observe→Verify→Audit all present. No network allow-list layer |
| §36 | Secret boundary | `PARTIAL` | `secrets/` is gitignored; `scripts/check_secrets.py` blocks staged credentials. No redaction layer inside prompts |
| §54 | Observability and audit | `IMPLEMENTED` | Every model call, policy decision, verification and replan is recorded with correlation |
| §53 | Cloud failure strategy | `IMPLEMENTED` | Provider chain → local → deterministic fallback, preserving provenance |

## 2. Registered but not implemented

These appear in `VISION.md` and have an architectural place, but **no working behaviour**.
Listed so they are not mistaken for absent by accident, nor for present by optimism.

| Vision § | Capability | Status | Where it would live |
|---|---|---|---|
| §6 | User Model | `MISSING` | New model alongside `alexis/self/` |
| §9, §10 | Goal management & decomposition | `MISSING` | Above `MissionEngine`; nothing manages goals beyond one mission |
| §14 | Causal reasoning | `MISSING` | `diagnose_failure` classifies failure kinds; it does not build causal chains |
| §15 | Experiment engine | `CONTRACT` | `alexis/experiments/engine.py` — interface only |
| §16 | Autonomous research | `MISSING` | `browser.research` is registered `missing` in the catalog |
| §18, §19 | Capability discovery & tool creation | `MISSING` | No inspection→evaluation→approval→registration pipeline |
| §20, §21 | Skill system & learning lifecycle | `MISSING` | `alexis/learning/system.py` appends experiences. Reflection→lesson→skill→version does not exist |
| §25 | Resource budgets | `PARTIAL` | Cost and time budgets exist per mission; no token/call/disk budgets |
| §26 | Commitment manager | `MISSING` | — |
| §27 | Scheduler | `CONTRACT` | `alexis/autonomy/scheduler.py`, 51 lines: an interface, not a running scheduler |
| §28 | Event & trigger engine | `PARTIAL` | `EventBus` is real and is how missions report; nothing consumes external events to *originate* work |
| §29, §30 | Self-health & model quality monitoring | `MISSING` | No degradation detection, no provider history |
| §31 | Independent criticism (ModelCritic) | `MISSING` | `PlanValidator` is deterministic validation, not a critic |
| §33 | Uncertainty engine | `PARTIAL` | Confidence and uncertainty are tracked in `KnowledgeState`; not a first-class subsystem |
| §34 | Operational explainability | `PARTIAL` | Structured decision metadata exists; not surfaced as a first-class audit view |
| §45, §46 | Sub-agents & multi-agent | `MISSING` | `AgentRegistry` is a registry; no delegation with scoped authority |
| §48 | Long-term autonomy | `MISSING` | Single missions only |
| §49, §50 | Human collaboration modes & autonomy levels | `PARTIAL` | Autonomy levels exist; PAUSE/STOP/CANCEL/DENY are partial |
| §57 | Self-diagnostics | `MISSING` | — |
| §58 | Self-improvement | `MISSING` | Must stay bounded (rule 19). Nothing exists yet |
| §59 | Knowledge graph | `CONTRACT` | WorldModel relations are the seed; no graph semantics |
| §60 | Temporal reasoning | `PARTIAL` | Timestamps and staleness exist; no before/after/during reasoning |
| §61 | Predictive monitoring | `MISSING` | — |
| §62 | Opportunity detection | `MISSING` | — |
| §63 | Personal OS layer | `PARTIAL` | Orchestrates filesystem + tests + desktop. Not yet the user's whole environment |

## 3. Capabilities with no real adapter

Registered in the catalog with `status=missing`, so the system reports them honestly instead
of pretending: `git.read`, `git.commit`, `git.push`, `terminal.run`, `browser.research`,
`api.http`, `mcp.run`, `codex.run`, `opencode.run`, `obsidian.run`, `omniroute.run`,
`hermes.run`, `vision.screen`, `speech.stt`, `iot.mqtt`.

Real adapters exist for: `fs.read`, `fs.stat`, `fs.write`, `fs.remove`, `execute.test`,
`desktop.tools`, `execution.sandbox`, `research.filesystem`, `verification.filesystem`,
`tts.speak`, `cognition.understand`, `cognition.analyze`, `autonomy.gates`, `autonomy.queue`,
`perception.clap`.

> `verification.filesystem` is registered and available but **has no tool implementation**
> (`build_filesystem_tools` returns only the four `fs.*` tools). It must not be treated as
> independently observable evidence of existence.

## 4. Presence, voice, vision, physical

| Vision § | Capability | Status |
|---|---|---|
| §39 | Vision | `MISSING` — `vision.screen` registered, no adapter |
| §40 | Audio & voice | `PARTIAL` — `tts.speak` real; STT/VAD/barge-in absent |
| §41 | Presence engine | `PARTIAL` — `alexis/self/presence.py` derives states from real Core state |
| §42 | Advanced avatar | `CONTRACT` — `apps/face/` frontend exists; not driven by verified state everywhere |
| §43 | Desktop perception | `PARTIAL` — `desktop.tools` is a delegated host integration |
| §44 | Physical world & IoT | `MISSING` — `iot.mqtt` registered; no adapter. Correct: physical control needs stricter risk classes |

## 5. Where the vision and the code disagree on purpose

Three places where the vision is stricter than the implementation, recorded rather than
quietly resolved in either direction:

1. **§12 Dynamic Planning** — the vision expects plans carrying `preconditions`,
   `expected effects`, `alternatives`, `rollback`. `PlanStep` has `objective` and
   `success_criteria` fields but the planner does not populate them. Not a filter, not
   verified: incomplete.
2. **§11** — the vision implies any meaningful goal can be verified. Today only tool-observed
   predicates are. A goal in free language with no checkable predicate stays
   `insufficient_evidence`, which is the honest outcome, not a satisfied one.
3. **§2 loop** — the canonical loop ends `REFLECT → LEARN → UPDATE`. The loop stops at
   `VERIFY → COMMIT`. Reflection exists (`alexis/learning/reflection.py`) but is not an
   automatic stage that feeds the next decision.

## 6. What this means for the next increment

The vision's own §67 priority order puts **core correctness → safety → persistence** first,
then goal management, then recovery, then learning. Read against the real state:

- The P0 cognitive core is at **75.0%** with the scoring method stated in
  `P0-COGNITIVE-CORE-GAP-ANALYSIS.md`. The remaining P0 gaps are capability selection under a
  real model, a composed response, and reflection.
- **Reflection (§17 of the gap analysis, `MISSING`)** is the smallest gap that the vision
  needs and the code does not have, and it has an existing seam: the loop already stops at
  COMMIT with a `WorldModel`, a `KnowledgeState` and an `EvidenceStore`.
- Goal management (§9, §10) is the largest structural absence: ALEXIS manages one mission at a
  time and has no notion of an objective outliving it.

**Nothing in this file authorises work.** It records distance. What to do next is a decision
for the user, not something an agent should infer from a roadmap.

---

*Aligned with `VISION.md`. If the two ever disagree, `VISION.md` describes the target and
this file describes the system; the truth about the system is in the code and its tests.*