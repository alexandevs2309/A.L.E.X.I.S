PAGE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ALEXIS</title>
<style>
  :root { --ink:#e2e8f0; --dim:#93a4bb; --faint:#5b6b80; --line:#263449; --bg:#0b1018; --accent:#7dd3ee; --ok:#7ad8a8; --wait:#e8c078; --err:#e3918b; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font-family:"Segoe UI", system-ui, -apple-system, sans-serif; min-height:100vh; }
  .wrap { max-width:680px; margin:0 auto; padding:26px 18px 60px; }
  header { display:flex; align-items:center; justify-content:space-between; gap:12px; padding-bottom:14px; border-bottom:1px solid var(--line); margin-bottom:22px; }
  .mark { font-size:19px; letter-spacing:9px; color:var(--ink); font-weight:600; }
  .mark b { color:var(--accent); font-weight:400; }
  .id { display:flex; align-items:center; gap:12px; }
  .avatar { width:36px; height:36px; display:block; border-radius:50%; background:#0a1522; }
  .mini svg { width:30px; height:30px; display:block; border-radius:50%; background:#0a1522; }
  .lid { transform-box:fill-box; transform-origin:center; animation:blink 4.8s infinite; }
  @keyframes blink { 0%,90%,100% { transform:scaleY(1); } 93% { transform:scaleY(.06); } 96% { transform:scaleY(.9); } }
  #companion { position:fixed; left:0; top:0; width:120px; height:120px; z-index:40; pointer-events:none; opacity:.96; }
  .cmp-core { position:absolute; inset:0; animation:bob 3.4s ease-in-out infinite; }
  .cmp-core svg { width:120px; height:120px; display:block; filter:drop-shadow(0 0 18px rgba(125,211,238,.45)); }
  @keyframes bob { 0%,100% { transform:translateY(0); } 50% { transform:translateY(-7px); } }
  .cmp-ring { position:absolute; inset:-10px; border:1px dashed rgba(125,211,238,.4); border-radius:50%; animation:rot 16s linear infinite; }
  .cmp-ring::after { content:""; position:absolute; top:-1px; left:50%; width:6px; height:6px; border-radius:50%; background:#8fd8ff; box-shadow:0 0 8px #8fd8ff; }
  @keyframes rot { to { transform:rotate(360deg); } }
  .cmp-halo { position:absolute; inset:-26px; border-radius:50%; background:radial-gradient(circle, rgba(125,211,238,.14) 0%, rgba(125,211,238,0) 70%); animation:halo 3.6s ease-in-out infinite; }
  @keyframes halo { 0%,100% { opacity:.55; transform:scale(1); } 50% { opacity:.95; transform:scale(1.06); } }
  #companion[data-ctx="waiting_approval"] .cmp-halo { background:radial-gradient(circle, rgba(232,192,120,.2) 0%, rgba(232,192,120,0) 70%); }
  #companion[data-ctx="completed"] .cmp-core svg { filter:drop-shadow(0 0 24px rgba(122,216,168,.55)); }
  #companion[data-ctx="executing"] .cmp-core svg { filter:drop-shadow(0 0 22px rgba(90,224,255,.6)); }
  #companion[data-ctx="thinking"] .cmp-core svg { filter:drop-shadow(0 0 20px rgba(125,211,238,.55)); }
  #companion[data-ctx="error"] .cmp-halo { background:radial-gradient(circle, rgba(227,145,139,.18) 0%, rgba(227,145,139,0) 70%); }
  .hdr-right { display:flex; align-items:center; gap:14px; }
  #status { font-size:11px; letter-spacing:2px; text-transform:uppercase; color:var(--faint); }
  .sys-btn { background:none; border:1px solid var(--line); color:var(--dim); font-size:11px; letter-spacing:1px; padding:6px 10px; border-radius:6px; cursor:pointer; }
  .sys-btn:hover { border-color:var(--faint); color:var(--ink); }
  .vm { display:inline-flex; align-items:center; gap:7px; }
  .vm .dot { width:8px; height:8px; border-radius:50%; background:var(--faint); transition:background .2s; }
  .vm button { background:none; border:1px solid var(--line); color:var(--dim); font-size:11px; letter-spacing:1px; padding:6px 10px; border-radius:6px; cursor:pointer; }
  .vm button:hover { border-color:var(--faint); color:var(--ink); }
  .vm.on .dot { background:var(--ok); box-shadow:0 0 8px var(--ok); }
  .vm.on button { border-color:rgba(122,216,168,.55); color:var(--ok); }
  #thread { display:flex; flex-direction:column; gap:10px; margin-bottom:20px; }
  .turn { max-width:88%; padding:10px 14px; border-radius:10px; font-size:14.5px; line-height:1.55; }
  .turn.human { align-self:flex-end; background:#16233b; border:1px solid var(--line); }
  .turn.ai { align-self:flex-start; display:flex; gap:10px; align-items:flex-start; background:transparent; padding:2px 4px; color:var(--dim); }
  .turn.ai .mini { flex:none; }
  .turn.ai .mini img { width:30px; height:30px; border-radius:50%; object-fit:cover; border:1px solid var(--line); }
  .turn.ai .msg { padding-top:4px; }
  .turn.ai .head { color:var(--ink); }
  .turn.ai .sub { font-size:13px; margin-top:4px; }
  #composer { margin:6px 0 18px; }
  .ask { color:var(--dim); font-size:15px; margin:18px 0 10px; }
  form { display:flex; gap:10px; }
  input[type=text] { flex:1; background:#0e1624; border:1px solid var(--line); border-radius:9px; padding:12px 14px; color:var(--ink); font-size:15px; }
  input[type=text]:focus { outline:none; border-color:var(--accent); }
  button[type=submit] { background:var(--accent); color:#04121a; border:0; border-radius:9px; padding:0 20px; font-weight:700; letter-spacing:1px; cursor:pointer; }
  button[type=submit]:disabled { opacity:.5; cursor:wait; }
  #activity { margin:6px 0 18px; }
  .box { border:1px solid var(--line); border-radius:12px; padding:16px 18px; }
  .box h3 { margin:0 0 8px; font-size:15px; color:var(--ink); font-weight:600; }
  .box ul { margin:6px 0 0; padding-left:18px; color:var(--dim); font-size:13.5px; }
  .box li { margin:3px 0; }
  .box.thinking { border-color:transparent; padding:6px 2px; }
  #decision { border:1px solid rgba(232,192,120,.45); }
  #decision h3 { color:var(--wait); }
  .kv { color:var(--dim); font-size:13.5px; margin:3px 0; }
  .acts { display:flex; gap:10px; margin-top:14px; }
  .acts .approve { background:var(--wait); color:#221a06; border:0; border-radius:8px; padding:9px 18px; font-weight:700; cursor:pointer; }
  .acts .deny { background:transparent; border:1px solid var(--line); color:var(--dim); border-radius:8px; padding:9px 18px; cursor:pointer; }
  .ok { color:var(--ok); }
  #result .lbl { color:var(--faint); font-size:11px; letter-spacing:2px; text-transform:uppercase; margin-top:10px; }
  #result ul { margin:4px 0 0; padding-left:18px; color:var(--dim); font-size:13.5px; }
  .conf { margin-top:10px; font-size:13px; color:var(--dim); }
  #error-box b { color:var(--err); }
  #sys { margin-top:30px; border-top:1px solid var(--line); padding-top:14px; }
  #sys summary { cursor:pointer; color:var(--faint); font-size:11px; letter-spacing:2px; text-transform:uppercase; }
  #sys summary:hover { color:var(--dim); }
  .sys-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:12px; }
  .sys-col h4 { font-size:10px; letter-spacing:2px; color:var(--faint); text-transform:uppercase; margin:10px 0 4px; }
  pre { background:#0a101a; border:1px solid var(--line); border-radius:8px; padding:10px; font-size:11px; color:#8fa2b8; overflow:auto; max-height:300px; margin:0; }
  .sys-events { font-family:monospace; font-size:11px; color:#7fdf8f; list-style:none; padding:0; margin:0; max-height:300px; overflow:auto; background:#0a101a; border:1px solid var(--line); border-radius:8px; }
  .sys-events li { padding:3px 8px; border-bottom:1px dashed #16202f; color:var(--dim); }
  .sys-events .t { color:var(--faint); margin-right:8px; }
  .note { color:var(--faint); font-size:12.5px; margin:16px 0 8px; }
  @media (max-width:560px) { .sys-grid { grid-template-columns:1fr; } }
</style>
</head>
<body>
<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true">
  <defs>
    <linearGradient id="gbodyb" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#bceaff"></stop>
      <stop offset=".55" stop-color="#55b6f0"></stop>
      <stop offset="1" stop-color="#1e5f96"></stop>
    </linearGradient>
    <radialGradient id="girisg" cx=".5" cy=".38" r=".75">
      <stop offset="0" stop-color="#f2fbff"></stop>
      <stop offset=".42" stop-color="#8fd8ff"></stop>
      <stop offset="1" stop-color="#1fb3f5"></stop>
    </radialGradient>
    <symbol id="gideon" viewBox="0 0 120 120">
      <path d="M12 48 Q2 62 12 76 Q15 61 12 48Z" fill="url(#gbodyb)" opacity=".7"></path>
      <path d="M108 48 Q118 62 108 76 Q105 61 108 48Z" fill="url(#gbodyb)" opacity=".7"></path>
      <path d="M60 14 C92 14 104 40 104 66 C104 96 84 108 60 108 C36 108 16 96 16 66 C16 40 28 14 60 14 Z" fill="url(#gbodyb)"></path>
      <path d="M60 14 C92 14 104 40 104 66 C104 96 84 108 60 108 C36 108 16 96 16 66 C16 40 28 14 60 14 Z" fill="none" stroke="#c9f2ff" stroke-opacity=".55" stroke-width="1.5"></path>
      <path d="M39 44 C47 32 73 32 81 44 C89 58 89 78 81 92 C73 100 47 100 39 92 C31 78 31 58 39 44Z" fill="rgba(7,22,38,.93)"></path>
      <path d="M39 44 C47 32 73 32 81 44 C89 58 89 78 81 92 C73 100 47 100 39 92 C31 78 31 58 39 44Z" fill="none" stroke="#8fd8ff" stroke-opacity=".4" stroke-width="1.2"></path>
      <g class="lid">
        <ellipse cx="60" cy="64" rx="10" ry="14" fill="url(#girisg)"></ellipse>
        <ellipse cx="57" cy="60" rx="3.4" ry="4.2" fill="#ffffff" opacity=".9"></ellipse>
      </g>
      <path d="M46 44 Q60 38 74 44" fill="none" stroke="#c9f2ff" stroke-opacity=".75" stroke-width="2" stroke-linecap="round"></path>
    </symbol>
  </defs>
</svg>
<div id="companion" aria-hidden="true">
  <div class="cmp-ring"></div>
  <div class="cmp-halo"></div>
  <div class="cmp-core"><svg viewBox="0 0 120 120"><use href="#gideon"></use></svg></div>
</div>
<div class="wrap">
  <header>
    <div class="id">
      <svg class="avatar" viewBox="0 0 120 120" aria-label="ALEXIS"><use href="#gideon"></use></svg>
      <div class="mark">A<b>.</b>LEXIS</div>
    </div>
    <div class="hdr-right">
      <span id="status">Inactivo</span>
      <span id="vmode" class="vm" data-on="0" title="Modo voz: off = solo texto. Da una palmada o activa el toggle para hablar"><span class="dot"></span><button id="voice-toggle" type="button">VOZ</button></span>
      <button class="sys-btn" id="sys-toggle" type="button">Sistema</button>
    </div>
  </header>

  <div id="thread"></div>

  <div id="composer">
    <div id="askline" class="ask">¿Qué quieres que haga?</div>
    <form id="form">
      <input id="objective" type="text" placeholder="Describe tu solicitud…" autocomplete="off">
      <button id="launch" type="submit">ENVIAR</button>
    </form>
  </div>

  <div id="activity"></div>

  <details id="sys">
    <summary>Modo sistema · diagnóstico</summary>
    <div class="sys-grid">
      <div class="sys-col">
        <h4>Estado</h4>
        <pre id="rawjson">{}</pre>
      </div>
      <div class="sys-col">
        <h4>Eventos en vivo</h4>
        <ul class="sys-events" id="sysfeed"></ul>
      </div>
    </div>
  </details>
</div>

<script>
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));

const STEP = {
  understand: "Estoy entendiendo el objetivo.",
  research: "Estoy recopilando contexto y evidencia.",
  execute: "Estoy ejecutando la acción.",
  verify: "Estoy verificando el resultado.",
};
const TURNS = {
  "mission.planning": "Estoy analizando tu solicitud y armando un plan.",
  "mission.approval_required": "Necesito tu decisión para continuar.",
  "mission.completed": "La misión se completó.",
  "mission.failed": "No pude completar la misión.",
  "mission.cancelled": "La misión fue cancelada.",
  "mission.stopped": "La misión fue cancelada.",
};

let ctx = "idle";
let missionId = null;

function say(text, human = false) {
  const div = document.createElement("div");
  div.className = human ? "turn human" : "turn ai";
  if (human) {
    div.textContent = text;
  } else {
    div.innerHTML = `<span class="mini"><svg viewBox="0 0 120 120"><use href="#gideon"></use></svg></span><div class="msg"></div>`;
    div.querySelector(".msg").textContent = text;
  }
  $("thread").appendChild(div);
  $("thread").scrollTop = $("thread").scrollHeight;
}

function eventTurn(topic, payload) {
  if (topic === "mission.step_started") {
    const label = STEP[payload] || "Estoy trabajando en ello.";
    say(label);
    return true;
  }
  const text = TURNS[topic];
  if (text) { say(text); return true; }
  return false;
}

function composerVisible(show) {
  $("composer").style.display = show ? "" : "none";
  if (show) {
    $("askline").textContent = ctx === "idle" ? "¿Qué quieres que haga?" : "¿Qué quieres que haga a continuación?";
  }
}

function decisionBox(d) {
  const el = $("activity");
  if (!d) { el.innerHTML = ""; return; }
  el.innerHTML =
    `<div class="box" id="decision">
       <h3>Necesito tu decisión</h3>
       <div class="kv">Acción: <b>${esc(d.action)}</b></div>
       <div class="kv">Nivel: ${esc(d.risk)}</div>
       <div class="kv">Objetivo: ${esc(d.objective || "")}</div>
       <div class="kv">${esc(d.reason)}</div>
       <div class="acts">
         <button class="approve" id="approve">APROBAR</button>
         <button class="deny" id="deny">CANCELAR</button>
       </div>
     </div>`;
  $("approve").addEventListener("click", async () => {
    if (!missionId) return;
    const b = $("approve"); b.disabled = true;
    await fetch(`/missions/${missionId}/approve`, { method: "POST" });
    say("Autorizado. Continúo.");
    setTimeout(refresh, 400);
  });
  $("deny").addEventListener("click", async () => {
    if (!missionId) return;
    await fetch(`/missions/${missionId}/deny`, { method: "POST" });
    say("Entendido, lo dejo aquí.");
    setTimeout(refresh, 400);
  });
}

function activityBox(p, m) {
  const el = $("activity");
  if (!m) { el.innerHTML = ""; return; }
  let html = "";
  if (p.context === "thinking") {
    html = `<div class="box thinking"><h3>${esc(p.headline)}</h3>${bullets(p.points)}</div>`;
  } else if (p.context === "researching") {
    html = `<div class="box thinking"><h3>${esc(p.headline)}</h3>${bullets(p.points)}</div>`;
  } else if (p.context === "executing") {
    html = `<div class="box thinking"><h3>${esc(p.headline)}</h3>${bullets(p.points)}</div>`;
  } else if (p.context === "waiting_approval") {
    decisionBox(p.decision);
    return;
  } else if (p.context === "completed") {
    const r = p.result || {};
    html = `<div class="box" id="result">
      <h3 class="ok">${esc(p.headline)}</h3>
      <div class="kv">Objetivo: ${esc(r.summary || "")}</div>
      <div class="lbl">Ejecutado</div><ul>${(r.steps || []).map(s => `<li>${esc(s)}</li>`).join("")}</ul>
      <div class="lbl">Evidencia</div><ul>${(r.evidence || []).map(e => `<li>${esc(e)}</li>`).join("") || "<li>sin evidencia registrada</li>"}</ul>
      <div class="conf">Confianza: <b>${esc(r.confidence ?? "—")}%</b></div>
      <div class="kv">${esc(r.next || "")}</div>
    </div>`;
  } else if (p.context === "error") {
    const e = p.error || {};
    html = `<div class="box" id="error-box"><h3>${esc(p.headline)}</h3><div class="kv">${esc(e.summary || "")}</div><div class="kv"><b>¿Qué necesito?</b> ${esc(e.needs || "")}</div></div>`;
  } else if (p.context === "cancelled") {
    html = `<div class="box thinking"><h3>${esc(p.headline)}</h3></div>`;
  }
  el.innerHTML = html;
}

function bullets(items) {
  if (!items || !items.length) return "";
  return `<ul>${items.map(i => `<li>${esc(i)}</li>`).join("")}</ul>`;
}

function setStatus(s) {
  $("status").textContent = s;
}

async function refresh() {
  let s;
  try { s = await (await fetch("/state")).json(); } catch { return; }
  const p = s.present;
  if (!p) return;
  ctx = p.context;
  missionId = s.mission ? s.mission.id : missionId;
  const cp = document.getElementById("companion");
  if (cp) cp.dataset.ctx = ctx;
  setStatus(p.status);
  composerVisible(["idle", "completed", "cancelled", "error"].includes(ctx));
  activityBox(p, s.mission);
  setVoiceModeUI(!!s.voice_mode);
  $("rawjson").textContent = JSON.stringify(s, null, 2);
}
setInterval(refresh, 1500);
refresh();

const es = new EventSource("/stream");
es.onmessage = (e) => {
  let d;
  try { d = JSON.parse(e.data); } catch { return; }
  const gotTurn = eventTurn(d.topic, d.payload);
  const li = document.createElement("li");
  const t = new Date().toLocaleTimeString();
  li.innerHTML = `<span class="t">${esc(t)}</span>${esc(d.topic)} ${esc(typeof d.payload === "object" ? JSON.stringify(d.payload) : d.payload)}`;
  $("sysfeed").prepend(li);
  while ($("sysfeed").children.length > 200) $("sysfeed").lastChild.remove();
  if (gotTurn || d.topic === "mission.step_completed" || d.topic === "mission.approval_required") {
    setTimeout(refresh, 350);
  }
};

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const objective = $("objective").value.trim();
  if (!objective) return;
  const btn = $("launch");
  btn.disabled = true;
  say(`«${objective}»`, true);
  try {
    const res = await fetch("/missions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ objective }),
    });
    const data = await res.json();
    missionId = data.id;
    $("objective").value = "";
    setTimeout(refresh, 400);
  } finally { btn.disabled = false; }
});

$("sys-toggle").addEventListener("click", () => {
  $("sys").open = !$("sys").open;
});

function setVoiceModeUI(on) {
  const el = $("vmode");
  el.dataset.on = on ? "1" : "0";
  el.classList.toggle("on", !!on);
  el.title = on
    ? "Modo voz: on = escucho y respondo hablando (lo activa una palmada o el toggle)"
    : "Modo voz: off = respondo solo en texto (da una palmada para activarlo)";
}
async function loadVoiceMode() {
  try {
    const r = await (await fetch("/voice-mode")).json();
    setVoiceModeUI(!!r.enabled);
  } catch {}
}
$("voice-toggle").addEventListener("click", async () => {
  const target = $("vmode").dataset.on !== "1";
  try {
    const r = await fetch("/voice-mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: target }),
    });
    const d = await r.json();
    setVoiceModeUI(!!d.enabled);
    say(d.enabled ? "Modo voz activado. Escucho y respondo por voz." : "Modo voz desactivado. Respondo solo en texto. Da una palmada para volver a hablar.");
  } catch {}
});
loadVoiceMode();

const companion = document.getElementById("companion");
const SPEED = { idle:.9, thinking:.9, researching:1.25, executing:1.7, waiting_approval:.55, completed:1.1, cancelled:.7, error:.7 };
let cpos = { x: Math.max((innerWidth-120)/2, 16), y: 96 };
let ctarget = null;
const companionTarget = () => {
  const W = window.innerWidth, H = window.innerHeight;
  const yMax = Math.max(Math.min(H*0.6, H-260)-96, 1);
  return { x: 16 + Math.random()*Math.max(W-152, 1), y: 96 + Math.random()*yMax };
};
setInterval(() => { if (!ctarget) ctarget = companionTarget(); }, 500);
function companionStep() {
  const sp = SPEED[ctx] ?? .9;
  if (ctx === "waiting_approval") {
    const d = document.getElementById("decision");
    if (d) { const r = d.getBoundingClientRect(); ctarget = { x: r.left-28, y: r.top+20 }; }
  }
  if (!ctarget) ctarget = companionTarget();
  const dist = Math.hypot(ctarget.x-cpos.x, ctarget.y-cpos.y);
  cpos.x += (ctarget.x-cpos.x)*(0.03*sp);
  cpos.y += (ctarget.y-cpos.y)*(0.03*sp);
  if (dist < 22) ctarget = companionTarget();
  const wob = Math.sin(Date.now()/640)*3;
  companion.style.transform = `translate3d(${cpos.x}px, ${cpos.y}px, 0) rotate(${wob}deg)`;
  companion.dataset.ctx = ctx;
  requestAnimationFrame(companionStep);
}
requestAnimationFrame(companionStep);
</script>
</body>
</html>
"""