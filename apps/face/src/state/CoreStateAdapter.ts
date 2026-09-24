import type { AlexisVisualState, Attention, CoreVisualSignal } from "./AlexisVisualState";

export interface MissionPayload {
  id: string;
  state: string;
  objective: string;
  results?: { step?: string }[];
}

export interface CoreSnapshot {
  mission: MissionPayload | null;
  present?: { context?: string };
  verification?: { confidence?: number } | null;
}

function lastStep(results?: { step?: string }[]): string | null {
  for (const r of results ?? []) {
    if (r?.step) return r.step;
  }
  return null;
}

/** Convierte el estado REAL del Core (/state) en señal visual del rostro (directiva §15, mapa §8 del audit). */
export function mapCoreState(s: CoreSnapshot): CoreVisualSignal {
  const m = s.mission ?? null;
  const presentCtx = s.present?.context;
  const confidence = s.verification?.confidence ?? 0;

  if (!m) {
    return { state: "idle", activity: "idle", attention: "user", speaking: false, confidence };
  }

  const ms = m.state;
  const step = lastStep(m.results);
  let state: AlexisVisualState;

  switch (ms) {
    case "waiting_approval":
      state = "waiting";
      break;
    case "completed":
      state = "success";
      break;
    case "failed":
    case "blocked":
      state = "error";
      break;
    case "recovering":
      state = "warning";
      break;
    case "stopped":
      state = "idle";
      break;
    case "pending":
    case "planning":
    case "verifying":
      state = "thinking";
      break;
    case "running":
      state =
        step === "research" ? "researching" : step === "execute" ? "focused" : "thinking";
      break;
    default:
      state = "idle";
  }

  const attention: Attention = m ? "thinking" : "user";
  if (presentCtx) {
    void presentCtx; // contexto humano disponible para futura depuración
  }

  return {
    state,
    activity: step ?? ms,
    attention,
    speaking: false,
    confidence,
  };
}

/**
 * Adaptador Core → Rostro. Consume /state y /stream; emite señales visuales.
 * El Core NO cambia: solo emite su estado; este adaptador vive en el frontend.
 */
export class CoreStateAdapter {
  private timer = 0;
  private es: EventSource | null = null;
  private last: CoreVisualSignal = {
    state: "idle",
    activity: "idle",
    attention: "user",
    speaking: false,
    confidence: 0,
  };

  constructor(private apply: (signal: CoreVisualSignal) => void) {}

  start(intervalMs = 1400): void {
    this.poll();
    this.timer = window.setInterval(() => this.poll(), intervalMs);
    try {
      this.es = new EventSource("/stream");
      this.es.onmessage = (e) => {
        const d = JSON.parse(e.data);
        const topic = d.topic || "";
        if (topic.startsWith("mission.")) this.poll();
      };
      this.es.onerror = () => this.es?.close();
    } catch {
      /* SSE opcional */
    }
  }

  stop(): void {
    window.clearInterval(this.timer);
    this.es?.close();
    this.es = null;
  }

  getLastSignal(): CoreVisualSignal {
    return { ...this.last };
  }

  async poll(): Promise<void> {
    try {
      const s = (await (await fetch("/state", { cache: "no-store" })).json()) as CoreSnapshot;
      const signal = mapCoreState(s);
      this.last = signal;
      this.apply(signal);
    } catch {
      /* servidor temporalmente no disponible */
    }
  }
}