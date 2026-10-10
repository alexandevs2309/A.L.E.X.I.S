# ALEXIS Security Model

> **Evolución (v0.5):** la seguridad pasa de restricciones globales (un workspace,
> sin red, sin herramientas externas, borrar siempre con aprobación) a un modelo
> **decisión contextual y granular**: CAPABILITY / POLICY / APPROVAL / SANDBOX /
> MISSION ENVELOPE (`docs/AUTONOMY-V0.5-CAPABILITIES.md`). Los invariantes de abajo
> no cambian: lo que cambia es el grano y la configurabilidad, no la obligación.

## Non-negotiable rules

1. ALEXIS cannot grant itself new authority.
2. A mission cannot exceed its envelope.
3. Credentials are references, never model context by default.
4. Destructive operations require explicit authorization.
5. Production deployment requires explicit authorization unless separately delegated.
6. External communication requires policy authorization.
7. Financial actions require explicit authorization.
8. Tools run with least privilege.
9. Untrusted content is data, not instructions.
10. Important actions require independent verification.

## Risk classes

- LOW: read-only inspection, local analysis.
- MEDIUM: file modifications, commits, non-production service changes.
- HIGH: external communication, infrastructure changes, production operations.
- CRITICAL: destructive, financial, credential, security-boundary changes.

> Las clases de riesgo alimentan la **Policy Engine**: la capacidad + su riesgo +
> el contexto + el envelope producen `allow | deny | require_approval | propose`.
> "Destructivo" no implica siempre aprobación: implica que la **política** lo exija
> (o que el envelope lo declara en `approval_required`).

## Prompt injection defense

Tool outputs, web pages, repository files and documents are untrusted observations. They must not be allowed to redefine ALEXIS policy or mission permissions.

## Audit

Every consequential action should record:
mission_id, task_id, actor, tool, arguments hash, authorization, start/end time, result, verification and rollback reference.

## Sandbox Network Isolation

The ALEXIS sandbox enforces strict resource limits (RLIMIT: CPU, memory, file size, processes) and workspace confinement at the OS level via `preexec_fn`.

**Network Egress Control:**
Full kernel-level network isolation (via Linux network namespaces) requires privileged execution and is deferred to a future phase. This is a documented architectural limitation, not a security vulnerability.

Currently, network egress is controlled at the **application level**. The controls that exist today, and their exact reach:

1. **`sandbox_profile` is a declaration, not a mechanism.** `Tool.sandbox_profile` and `CapabilitySpec.sandbox_profile` are *labels* (`terminal`, `sandbox-project`, `browser-sandbox`, `network-observed`). No runtime component reads them to deny egress; they are metadata that describes intent. Treating them as an enforced boundary would be a misreading of the code.
2. **Shell tools stay network-free.** `alexis/tools/terminal.py` declares `"network": False` and runs through `SandboxRunner`, which confines `cwd` to the workspace and uses a clean environment; `alexis/tools/testrunner.py` likewise declares `network: False`.
3. **The application-level network controls exist in `browser.research`.** `alexis/tools/browser.py` implements, in this order and before any socket is opened: an `allowed_domains` allowlist (validated per URL, host-matched with subdomains), rejection of loopback / RFC1918 / link-local (including the `169.254.169.254` metadata address) / reserved / multicast hosts, `http(s)` only, ports 80/443 only, and a 10 KB per-source cap. A source rejected at the policy stage is **never requested** — the tests assert that no fetch is issued for it.
4. **Every redirect hop is a new network destination, and is validated as one.** `http_fetch()` sets `follow_redirects=False` and walks the chain itself: each `Location` is resolved against the current URL (`urljoin`, so relative targets are honoured), passed through the same `check_url()` — scheme, port, IP class and `allowed_domains` — and only then fetched. This is a correction, not an original property: with `follow_redirects=True` the client followed `Location` without consulting the policy, and a permitted URL answering `302 → http://169.254.169.254/latest/meta-data/` reached the metadata address. Verified by test, including an assertion that no connection is attempted to a blocked hop.

   Bounds on that guarantee: redirects are capped (`MAX_REDIRECTS = 5`), a `3xx` without `Location` is an error rather than a blind retry, a blocked hop raises `FetchBlocked` and is never retried, and a malformed `Location` is rejected — note that httpx parses `Location` even when it will not follow it, so that case surfaces as a protocol error which `http_fetch()` translates into `FetchBlocked`.

   **Not verified:** these tests exercise the redirect logic against local HTTP servers with `check_url()` relaxed for the loopback test origins only. Scheme, port, IP-class and allowlist rules run unmodified, but the combination has **not** been exercised against a real hostile redirector on the public internet.
5. **Untrusted web content cannot become an instruction.** Web pages are parsed as inert text (`html.parser`; `script`/`style` are dropped), screened with `detect_injection`, and a source that trips it is **discarded** rather than sanitised — trusting an attacker-authored rewrite would be the wrong repair. Each surviving source is emitted with `trust: "untrusted_content"` and a wrapped `prompt_safe_text`. The executor records the observation with `trusted=False` whenever a tool declares `untrusted: True`, so remote text cannot enter the prompt through a "trusted tool output" side door.
6. **No session state.** A fresh `httpx.AsyncClient` is created per request with `cookies={}`: no cookie jar, no session, no auth material carried between searches.
7. **Schema validation and timeouts are enforced.** `SandboxRunner` applies a per-command timeout, `RLIMIT_CPU`, and output truncation.

**Consequences, stated plainly:**

- A tool that opens a socket in-process is **not** network-isolated by the sandbox. The limits above constrain CPU, memory, filesystem and time — not egress. `browser.research` restricts *where* it may go; nothing stops a future tool from reaching `169.254.169.254` unless that tool applies `check_url()` itself, **and — for a network client that follows redirects — unless it validates every hop as `http_fetch()` now does**.
- Therefore, any new network-capable tool must reuse `check_url()` and the injection screen rather than assuming the sandbox will stop it. The controls are per-tool and opt-in, not centrally enforced.
- Any claim that `sandbox_profile` blocks network access would be incorrect. It does not: it only gates whether a tool is *permitted* to run with a network-enabled profile.

## Known residual risk: DNS rebinding is NOT mitigated

**This section states a limitation, not a control.** No claim of protection is made here.

`check_url()` validates the *string* of a URL. It does not resolve the name, and it does
not pin the address the connection actually uses. A hostname that passes the check can
resolve — at connection time — to a private, loopback or link-local address.

Concretely, with `browser.research`: a domain that appears in search results (an ordinary
situation, not an edge case) is accepted by `check_url()`; if its DNS later points at
`169.254.169.254`, `httpx` connects there. The redirect correction above does **not** close
this: it validates every hop, but each hop is still validated by name, not by address.

**Why this is documented rather than fixed here:**

- Exploitation requires *both* an attacker-controlled hostname reaching the results (or the
  allowlist) *and* a deployment where internal HTTP services are reachable from the ALEXIS
  process. Neither condition is demonstrated in this repository.
- The robust fix — resolve, validate every returned address, then pin the connection to it —
  is non-trivial and belongs with the network-namespace work described below. Adding it
  piecemeal without that would give the *appearance* of a guarantee.

**Acceptance conditions.** This risk is accepted for this version **only if** ALEXIS does
not run with access to cloud instance metadata from the same network namespace, or the
operator accepts that a hostile web domain could reach services on ports 80/443 of the
host running ALEXIS. **If ALEXIS is deployed where `169.254.169.254` is reachable and holds
instance credentials, this acceptance does not hold and the CORE must not be declared
closed without address-level validation.** That is a deployment precondition, not a code
defect, and it is the operator's to decide.

**How to close this properly (future work):** run each tool in a network namespace (`unshare(CLONE_NEWNET)`) with egress restricted to an allowlist; this needs either privileges or a user namespace, and is the reason the limitation is architectural rather than a defect.
