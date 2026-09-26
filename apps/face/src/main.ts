import { FaceScene } from "./rendering/SceneManager";
import { AlexisFace } from "./avatar/AlexisFace";
import { CoreStateAdapter } from "./state/CoreStateAdapter";
import type { CoreVisualSignal } from "./state/AlexisVisualState";
import { DebugPanel } from "./rendering/Debug";
import { SpeechRecognizer } from "./speech/SpeechRecognizer";
import { VoiceDriver } from "./speech/VoiceDriver";

const root = document.getElementById("face-root")!;
const input = document.getElementById("ask-input") as HTMLInputElement;
const form = document.getElementById("ask-form") as HTMLFormElement;
const chatLog = document.getElementById("chat-log")!;
const hud = document.getElementById("hud")!;

interface WindowWithWebGL {
  WebGLRenderingContext?: unknown;
}
const w = window as WindowWithWebGL;
if (!w.WebGLRenderingContext) {
  root.style.display = "none";
  const msg = document.getElementById("webgl-message")!;
  msg.style.display = "block";
  msg.textContent = "ALEXIS necesita WebGL para mostrar su rostro. Actualiza tu navegador o activa la aceleración gráfica.";
  throw new Error("WebGL no disponible");
}

let scene: FaceScene;
let face: AlexisFace;
try {
  scene = new FaceScene(root);
  face = new AlexisFace(scene);
} catch (err) {
  root.style.display = "none";
  const msg = document.getElementById("webgl-message")!;
  msg.style.display = "block";
  msg.textContent =
    "El rostro no pudo iniciar (" + String((err as Error)?.message || err) + "). Recarga la página o abre en otro navegador.";
  throw err;
}

let missionId: string | null = null;
let lastSignal: CoreVisualSignal = {
  state: "idle",
  activity: "idle",
  attention: "user",
  speaking: false,
  confidence: 0,
};

/* VOZ real (FASE 16): síntesis del navegador en español latino. */
const voice = new VoiceDriver();
voice.on({
  onSpeechStart: () => {
    face.setSpeaking(true);
    const s: CoreVisualSignal = { ...lastSignal, state: "speaking", attention: "user", speaking: true };
    face.applySignal(s);
    hud.innerHTML = `A<b style="color:#19b7f5">.</b>LEXIS · voz`;
  },
  onSpeechEnd: () => {
    face.setSpeaking(false);
    void adapter.poll();
  },
});
const voiceBtn = document.createElement("button");
voiceBtn.id = "voice-btn";
voiceBtn.title = voice.available ? `Voz: ${voice.voiceName}` : "Voz no disponible en este navegador";
voiceBtn.textContent = "🔊";
voiceBtn.disabled = !voice.available;
voiceBtn.addEventListener("click", () => {
  voice.enabled = !voice.enabled;
  voiceBtn.textContent = voice.enabled ? "🔊" : "🔇";
  if (!voice.enabled) voice.cancel();
});
document.body.appendChild(voiceBtn);
window.addEventListener(
  "pointerdown",
  () => {
    if (voice.available) voice.unlock();
  },
  { once: true },
);

/* DICTADO por voz (FASE 17): el micrófono del navegador convierte tu voz en
   un objetivo de misión, igual que escribirlo en el chat. */
const recognizer = new SpeechRecognizer();
const micBtn = document.createElement("button");
micBtn.id = "mic-btn";
micBtn.title = recognizer.available ? "Dictame una tarea por voz" : "Reconocimiento de voz no disponible (usa Chrome/Edge y permiso de micrófono)";
micBtn.textContent = "🎤";
micBtn.disabled = !recognizer.available;
micBtn.addEventListener("click", () => {
  if (recognizer.running) {
    recognizer.abort();
  } else {
    recognizer.enabled = true;
    recognizer.start();
  }
});
document.body.appendChild(micBtn);

interface ChatReply {
  text?: string;
  kind?: string;
  mission_id?: string | null;
  error?: string;
}

function missionBadge(id: string): void {
  const div = document.createElement("div");
  div.className = "msg mission";
  div.textContent = `MISIÓN · ${id.slice(0, 8)}`;
  chatLog.appendChild(div);
  while (chatLog.children.length > 5) chatLog.firstElementChild?.remove();
}

/* El avatar usa la MISMA ruta conversacional que /classic: /chat decide si esto es
   conversación o misión. El frontend no decide nada por su cuenta. */
function sendObjective(objective: string): void {
  const clean = objective.trim();
  if (!clean) return;
  bubble(`«${clean}»`);
  void fetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: clean }),
  })
    .then((res) => res.json())
    .then((data: ChatReply) => {
      if (data.error) {
        bubble(`No pude procesar eso: ${data.error}`);
        return;
      }
      if (data.text) bubble(data.text);
      if (data.mission_id) {
        missionId = data.mission_id;
        adapter.setMission(missionId);
        missionBadge(missionId);
        terminalLastShown = "";
      }
      void adapter.poll();
    })
    .catch(() => bubble("No pude conectar con el núcleo."));
}

let dictText: HTMLDivElement | null = null;
recognizer.on({
  onStart: () => {
    micBtn.classList.add("listening");
    micBtn.textContent = "⏹";
    setListening();
    hud.innerHTML = `A<b style="color:#19b7f5">.</b>LEXIS · escuchando`;
    const div = document.createElement("div");
    div.className = "msg";
    div.textContent = "Escuchando… habla ahora.";
    chatLog.appendChild(div);
    dictText = div;
  },
  onInterim: (text) => {
    if (dictText && dictText.isConnected) dictText.textContent = text;
  },
  onFinal: (text) => {
    if (dictText && dictText.isConnected) {
      dictText.textContent = `«${text}»`;
      dictText = null;
    }
    sendObjective(text);
  },
  onError: (message) => {
    if (dictText && dictText.isConnected) {
      dictText.textContent =
        message === "not-allowed" || message === "service-not-allowed"
          ? "Permiso de micrófono denegado. Autorízalo en el navegador y vuelve a pulsar el micrófono."
          : `No te escuché: ${message}. Vuelve a pulsar el micrófono.`;
    }
    micBtn.classList.remove("listening");
    micBtn.textContent = "🎤";
    void adapter.poll();
  },
  onEnd: () => {
    micBtn.classList.remove("listening");
    micBtn.textContent = "🎤";
    void adapter.poll();
  },
});

const adapter = new CoreStateAdapter(
  (signal) => {
    lastSignal = signal;
    if (voice.speaking) {
      face.setSpeaking(true);
      face.applySignal({ ...signal, state: "speaking", speaking: true, attention: "user" });
    } else {
      face.applySignal(signal);
    }
    hud.innerHTML = `A<b style="color:#19b7f5">.</b>LEXIS · ${signal.state}`;
    renderWaitingReply(signal.state);
    void reportTerminal(signal.state);
  },
  (id) => {
    /* Misión que empieza en vivo (palmada u otro cliente): se adopta y se muestra
       en el hilo para que conversation y panel sigan siendo la misma cosa. */
    missionId = id;
    missionBadge(id);
    terminalLastShown = "";
  },
  (response) => {
    /* P0 §5.6.1: el avatar presenta la respuesta compuesta por el Core. No inventa su
       propio veredicto, y un `goal_verified: false` nunca se presenta como logro. */
    if (!response.text) return;
    bubble(response.text);
    if (response.pending && response.pending.length) {
      bubble("Pendiente: " + response.pending.join("; "));
    }
    if (response.cognition_outcome === "degraded" || response.cognition_outcome === "unavailable") {
      bubble("Aviso: el razonamiento no vino de un modelo real.");
    }
  },
);
adapter.start();

function bubble(text: string): void {
  const div = document.createElement("div");
  div.className = "msg";
  div.textContent = text;
  chatLog.appendChild(div);
  while (chatLog.children.length > 5) chatLog.firstElementChild?.remove();
}

/* LISTENING: el rostro atiende mientras el usuario escribe (interacción local; el Core no cambia). */
function setListening(): void {
  const s: CoreVisualSignal = { ...lastSignal, state: "listening", activity: "listening", attention: "user" };
  lastSignal = s;
  face.applySignal(s);
  hud.innerHTML = `A<b style="color:#19b7f5">.</b>LEXIS · listening`;
}
input.addEventListener("focusin", setListening);
input.addEventListener("input", setListening);
input.addEventListener("blur", () => {
  void adapter.poll();
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const objective = input.value.trim();
  if (!objective) return;
  input.value = "";
  sendObjective(objective);
});

/* Decisión de aprobación cuando el rostro espera autorización (WAITING). */
let decisionBox: HTMLDivElement | null = null;
function renderWaitingReply(state: string): void {
  if (state === "waiting" && !decisionBox) {
    decisionBox = document.createElement("div");
    decisionBox.style.cssText =
      "position:fixed;left:50%;transform:translateX(-50%);bottom:24vh;z-index:20;display:flex;gap:10px;align-items:center;background:rgba(10,21,34,.82);border:1px solid rgba(232,192,120,.45);border-radius:12px;padding:10px 14px;";
    const label = document.createElement("span");
    label.textContent = "Necesito tu decisión.";
    label.style.cssText = "color:#e0c184;font:600 13px system-ui";
    void fetch("/state")
      .then((r) => r.json())
      .then((body) => {
        const pa = body?.mission?.pending_approval;
        if (pa?.action && label.isConnected) {
          label.textContent = `Requiere autorización: «${pa.action}» (${pa.risk ?? "?"}) — ${pa.reason ?? ""}`;
        }
      })
      .catch(() => {});
    const approve = document.createElement("button");
    approve.textContent = "Aprobar y continuar";
    approve.style.cssText = "background:#e0c184;color:#221a06;border:0;border-radius:8px;padding:6px 16px;font-weight:700;cursor:pointer;font:600 13px system-ui";
    const deny = document.createElement("button");
    deny.textContent = "Cancelar";
    deny.style.cssText = "background:transparent;border:1px solid rgba(230,160,120,.5);color:#e0b0a0;border-radius:8px;padding:6px 14px;cursor:pointer;font:600 13px system-ui";
    approve.onclick = async () => {
      if (missionId) await fetch(`/missions/${missionId}/approve`, { method: "POST" }).catch(() => {});
      bubble("Autorizado. Ejecutando…");
      cleanupDecision();
      void adapter.poll();
    };
    deny.onclick = async () => {
      if (missionId) await fetch(`/missions/${missionId}/deny`, { method: "POST" }).catch(() => {});
      bubble("Entendido, lo dejo aquí.");
      cleanupDecision();
      void adapter.poll();
    };
    decisionBox.append(label, approve, deny);
    document.body.appendChild(decisionBox);
    voice.speak("Necesito tu decisión antes de continuar.");
  } else if (state !== "waiting") {
    cleanupDecision();
  }
}
function cleanupDecision(): void {
  decisionBox?.remove();
  decisionBox = null;
}

/* Reporte visible del resultado: muestra qué hizo la misión (pasos, verificación,
   contenido leído) en vez de quedarse mudo. Es honesto: si falló, muestra el error. */
let terminalLastShown = "";

async function reportTerminal(state: string): Promise<void> {
  if (state !== "success" && state !== "error") return;
  const body: any = await fetch("/state").then((res) => res.json()).catch(() => null);
  const m = body?.mission;
  if (!m) return;
  /* sólo reportamos la misión que NOSOTROS_ABRIMOS con /chat: si /state trae otra,
     pertenece a otra sesión o a otro cliente y no debe aparecer como respuesta. */
  if (!missionId || m.id !== missionId) return;
  const key = `${state}:${m.id}`;
  if (terminalLastShown === key) return;
  terminalLastShown = key;

  const lines: string[] = [];
  if (state === "success") lines.push("Misión completada con verificación.");
  else lines.push("La misión no pudo completarse.");
  if (Array.isArray(m.results) && m.results.length) {
    lines.push("Pasos: " + m.results.map((r: any) => `${r.step}${r.success ? " ✓" : " ✗"}`).join(" · "));
  }
  const failedTasks = (body.tasks ?? []).filter((t: any) => t.status === "failed" && t.error);
  if (failedTasks.length) lines.push("Error: " + failedTasks[0].error);
  const ver = body.verification;
  if (ver?.notes) {
    lines.push(`Verificación: ${ver.notes}${ver.confidence != null ? ` (confianza ${ver.confidence})` : ""}`);
  }
  const readObs: any = (body.memory ?? []).find(
    (x: any) =>
      x.mission_id === m.id &&
      (x.source === "tool.fs.read" || x.source === "tool.fs.write") &&
      x.content?.ok,
  );
  if (readObs?.content?.content) {
    const preview = String(readObs.content.content).slice(0, 500);
    lines.push(`${readObs.source === "tool.fs.write" ? "Contenido escrito:" : "Contenido leído:"}\n${preview}`);
  }
  for (const line of lines) bubble(line);

  const spoken = lines
    .join(". ")
    .replace(/ ✓/g, ", listo")
    .replace(/ ✗/g, ", fallido")
    .replace(/«/g, "")
    .replace(/»/g, "")
    .replace(/\s*\n+\s*/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (spoken) voice.speak(spoken);
}

/* DEBUG overlay: solo con ?debug=1 (fuera de la experiencia normal). */
if (DebugPanel.enabled()) {
  document.body.classList.add("debug");
  const panel = document.getElementById("debug-panel")!;
  const debug = new DebugPanel(panel);
  debug.start(() => {
    const stats = scene.getStats();
    const weights = face.morph.weights();
    const wStr = Object.entries(weights)
      .map(([k, v]) => `${k} ${v.toFixed(2)}`)
      .join("  ");
    return [
      `FPS: ${stats.fps} (${stats.frameMs}ms)`,
      `estado: ${lastSignal.state} · actividad: ${lastSignal.activity}`,
      `attention: ${lastSignal.attention} · speaking: ${lastSignal.speaking ? "true" : "false"}`,
      `confidence: ${lastSignal.confidence.toFixed(2)}`,
      `gaze: ${face.getGazeTarget()}`,
      `morphs: ${wStr || "(sin pesos)"}`,
      `renderer: ${scene.renderer.info.render.triangles} tris · ${scene.renderer.info.render.calls} calls`,
    ].join("\n");
  });
}

scene.start((dt, t) => face.update(dt, t));
void adapter.poll();

/* Saludo por voz (una vez; si el navegador bloquea antes del primer gesto, ya se
   desbloquea con pointerdown → voice.unlock()). */
if (voice.available) {
  setTimeout(() => voice.speak("Hola. Soy ALEXIS. Puedo ayudarte en español. Pulsa el micrófono y dictame una tarea."), 700);
}