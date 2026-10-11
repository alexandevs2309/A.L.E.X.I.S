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

## git.* — credenciales y validación de comandos

`git.*` (`alexis/tools/git.py`) tiene dos propiedades de seguridad que conviene leer juntas,
porque ninguna es evidente por su nombre.

**No hay shell.** `SandboxRunner.run()` invoca con `create_subprocess_exec(*argv)`: una
lista de argumentos, nunca una cadena. En ese modelo `;`, `&&`, `|` y `$( )` son texto
inerte —no tienen nada que interpretar—. La whitelist de verbos de `git.read` es la
segunda capa, no la primera: impide que a `git.read` le pidan `commit`, que quizá sea
válido pero no es lectura. Los metacarácter se rechazan igualmente y con un motivo
explicado en el mensaje, para que el intento quede en la traza en vez de ignorarse.

**Las credenciales no las maneja este código.** No se leen, no se almacenan y no se
registran. El entorno que el sandbox construye es fijo y sin secretos (`PATH`, `HOME`,
`LANG`); quien autentica es git, con la configuración que ya tenga el repositorio, y lo
que hace ALEXIS es no molestar en ella. Lo que sí se neutraliza es `GIT_TERMINAL_PROMPT`,
para que un push sin credenciales falle en vez de quedarse esperando una entrada que
nadie va a dar.

**`git.push` valida el remoto, y esa comprobación no es opcional.** El sandbox NO aísla la
red —su propio docstring lo dice— así que publicar abre un socket que ninguna otra capa
vería. Sin la validación, un `origin` apuntando a `https://otro-host/x` recibe lo que el
credential helper tuviera preparado: exfiltración de credenciales silenciosa. Por eso
`git.push` resuelve el remoto y lo comprueba con `net.resolve_public()`, **la misma
función que protege a `browser.research`**, y por qué es la misma y no una copia: dos
implementaciones de la misma política se separan con el tiempo.

La preferencia `auto_approve_git_push` **no salta esta comprobación**, ni ninguna otra.
Lo que evita es que la confirmación se repita en cada llamada, y eso lo decide la capa de
aprobanación, no la tool. Una preferencia persistente que autoriza exfiltración de
credenciales es exactamente el tipo de atajo que un control explícito existe para impedir.

**Límite honesto:** la validación dice a qué dirección puede publicar *esta* tool. No
impide que otra tool abra un socket, y no sustituye un namespace de red.

## DNS rebinding: MITIGATED at address level (2026-10-11)

**This section states a control, and what it does not cover.** The claim is deliberately
narrower than "SSRF is solved".

`check_url()` used to validate the *string* of a URL: it did not resolve the name, and it
did not pin the address the connection actually used. A hostname that passed the check
could resolve — at connection time — to a private, loopback or link-local address. That
was documented here as an unmitigated residual risk, and at the time it was accurate.

It is no longer accurate. The control has three parts, and the third is the one that
matters:

1. **The name is resolved.** `resolve_public()` resolves the host and checks **every**
   address returned. A literal IP is classified without any network call.
2. **One bad address condemns the set.** If any resolved address is private, the whole
   set is rejected. Keeping "the public one" would leave the internal one available for
   whenever the attacker chose, because the final choice among addresses is the HTTP
   client's, not this code's.
3. **The connection is pinned.** `http_fetch()` resolves once, validates that result, and
   connects to that address through a custom network backend. The connection does **not**
   re-resolve, so there is no window between checking and connecting. TLS is unaffected:
   httpcore applies it separately with `server_hostname=origin.host`, so SNI and `Host`
   remain the real name.

If DNS resolution **fails**, the destination is rejected. Failing open would reopen the
very risk this closes: an attacker controls when their own DNS fails.

**Contract change worth knowing:** `check_url()` now needs to resolve. A host that does
not exist is therefore blocked — which is the point, but it means a transient DNS
outage now stops the fetch instead of letting it through unverified. Existing tests that
used fictitious domains had to be given a resolver (`tests/test_browser_tool.py`); no test
was weakened to compensate.

**What this does not cover:**

- **Central enforcement is by default, not by sandbox.** `alexis/tools/net.py` is now the
  single way out: `fetch_text()` resolves, validates and pins. `browser.py` delegates to
  it and re-exports, so nothing else had to change.
  `tests/test_network_guard.py` walks `alexis/` looking for raw network primitives and
  **fails** if one appears in a file that is not in its registry, where every entry has to
  state where its URL comes from. Forgetting the guard is therefore a red build, not a
  code review comment nobody reads.
  Four files are registered today, and only one of them handles a URL that a remote party
  can choose: the two `perception/` bridges and the model providers take URLs from
  operator configuration (`--url` flags, `.env`), which is misconfiguration rather than
  attack. `browser.research` was the only one taking a URL from search results — and it
  is now the one behind the door.
- **The registry is a list, not a wall.** An entry can be added without a legitimate
  reason. What the guard buys is that the addition is *visible in the diff*, next to the
  question "where does this URL come from". Nobody was asking that before.
- **The capabilities that would need this and do not exist yet** — `api.http`, `mcp.run`,
  `iot.mqtt` — are declared `missing`. When they are written the guard will require them
  to go through `net.py`; that is a design constraint on writing them, not a fix applied
  afterwards.
- **`allowed_domains` is checked before the name is resolved.** Deliberate: a host
  outside the allowlist is rejected either way, so asking DNS about it would be a wasted
  query and an unnecessary outbound leak.
- **This is not network isolation.** It says which address this tool may connect to. It
  does not constrain other processes, and it does not replace a network namespace.

**Evidence:** `tests/test_browser_dns_rebinding.py` (12 tests). Four contrapruebas confirm
the tests are sensitive to the control and not to its surroundings: removing the pin,
removing the resolution, failing open, and accepting a mixed address set each fail
exactly the test that should catch them.

**Why it stayed open so long:** adding it piecemeal gives the *appearance* of a guarantee
without closing it, which is worse than an honest "not mitigated". Validating the name
without pinning the connection is exactly that — it stops the naive attack and leaves the
one an attacker can time.

**Recorded because it is the failure mode a green suite hides best:** an earlier version
of the pinning test passed *with the pin removed*. It simulated the attacker's bad answer
on the second DNS query, where the code's own fail-closed caught it — green for a reason
unrelated to what it claimed to measure. The test was only honest once the bad answer
moved to the query that actually opens the socket.

**Still worth doing later:** run each tool in a network namespace (`unshare(CLONE_NEWNET)`)
with egress restricted to an allowlist. That needs privileges or a user namespace, and it
is defence in depth: this control decides the destination, the namespace constrains what
can be reached at all.
