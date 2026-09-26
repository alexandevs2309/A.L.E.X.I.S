import type { AlexisVisualState, Attention, CoreVisualSignal } from "./AlexisVisualState";

/** P0 §5.6.1: respuesta compuesta por `ResponseComposer`. Sin chain-of-thought. */
export interface ComposedResponse {
  mission_id?: string;
  text: string;
  verdict: string;
  goal_verified: boolean;
  blocked: boolean;
  needs_user: boolean;
  pending: string[];
  cognition_outcome: string;
}

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
 *
 * REGLA DE PERTENENCIA: el avatar refleja la misión que identifiés con `/chat` en
 * este turno. Una misión que sólo aparece en `/state` —porque la inició otro cliente,
 * porque el servidor la restauró al arrancar, o porque es de otra sesión— NO se
 * muestra: el rostro vuelve a idle. Mismo principio que el hilo conversacional de
 * `/classic`: la conversación es la fuente de verdad, `/state` sólo da progreso.
 */
export class CoreStateAdapter {
  private timer = 0;
  private es: EventSource | null = null;
  private ownedMissionId: string | null = null;
  private last: CoreVisualSignal = {
    state: "idle",
    activity: "idle",
    attention: "user",
    speaking: false,
    confidence: 0,
  };

  constructor(
    private apply: (signal: CoreVisualSignal) => void,
    private onMissionStarted?: (id: string) => void,
      private onComposedResponse?: (payload: ComposedResponse) => void,
  ) {}

  /** Identidad de la misión actual: la que devolvió `/chat` en este turno. */
  setMission(id: string | null): void {
    this.ownedMissionId = id ?? null;
  }

  getMission(): string | null {
    return this.ownedMissionId;
  }

  owns(missionId: string | null | undefined): boolean {
    return !!missionId && !!this.ownedMissionId && missionId === this.ownedMissionId;
  }

  start(intervalMs = 1400): void {
    this.poll();
    this.timer = window.setInterval(() => this.poll(), intervalMs);
    try {
      this.es = new EventSource("/stream");
      this.es.onmessage = (e) => {
        const d = JSON.parse(e.data);
        const topic = d.topic || "";
        // Una misión que empieza AHORA (este turno o una palmada) sí puede adoptarse:
        // el stream sólo emite eventos vivos, nunca historial.
        if (topic === "mission.planning" && !this.ownedMissionId && typeof d.payload === "string") {
          this.setMission(d.payload);
          this.onMissionStarted?.(d.payload);
          // P0 §5.6.1: la respuesta compuesta es la fuente semántica común. El avatar
          // sólo la presenta; no compone su propio veredicto.
          if (topic === "mission.response" && this.owns(d.payload?.mission_id)) {
            this.onComposedResponse?.(d.payload as ComposedResponse);
          }
        }
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
      const signal = this.owns(s.mission?.id) ? mapCoreState(s) : mapCoreState({ ...s, mission: null });
      this.last = signal;
      this.apply(signal);
    } catch {
      /* servidor temporalmente no disponible */
    }
  }
}