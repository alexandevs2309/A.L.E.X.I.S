export type AlexisVisualState =
  | "idle"
  | "listening"
  | "thinking"
  | "researching"
  | "focused"
  | "speaking"
  | "waiting"
  | "success"
  | "warning"
  | "error";

export type Attention = "user" | "thinking" | "away";

/** Señal visual derivada del Core (construida por CoreStateAdapter). */
export interface CoreVisualSignal {
  state: AlexisVisualState;
  activity: string;
  attention: Attention;
  speaking: boolean;
  confidence: number;
}

export const ALL_FACE_STATES: AlexisVisualState[] = [
  "idle",
  "listening",
  "thinking",
  "researching",
  "focused",
  "speaking",
  "waiting",
  "success",
  "warning",
  "error",
];

type Listener = (state: AlexisVisualState, signal: CoreVisualSignal) => void;

class AlexisVisualStateBus {
  private current: AlexisVisualState = "idle";
  private signal: CoreVisualSignal = {
    state: "idle",
    activity: "idle",
    attention: "user",
    speaking: false,
    confidence: 0,
  };
  private listeners = new Set<Listener>();

  getState(): AlexisVisualState {
    return this.current;
  }

  getSignal(): CoreVisualSignal {
    return { ...this.signal };
  }

  /** API pública (directiva §18): setAlexisState("thinking", options). */
  setState(state: AlexisVisualState, patch: Partial<CoreVisualSignal> = {}): void {
    if (!ALL_FACE_STATES.includes(state)) {
      this.current = "idle";
    } else {
      this.current = state;
    }
    this.signal = { ...this.signal, ...patch, state: this.current };
    for (const l of this.listeners) l(this.current, this.getSignal());
  }

  on(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}

export const visualStateBus = new AlexisVisualStateBus();

/** Directiva §18: API simple de estados visuales. */
export function setAlexisState(state: AlexisVisualState, patch: Partial<CoreVisualSignal> = {}): void {
  visualStateBus.setState(state, patch);
}