/**
 * Dictado por VOZ REAL (FASE 17): Web Speech API — SpeechRecognition (Chrome/Edge)
 * en ESPAÑOL LATINO (es-MX). Convierte tu voz en un objetivo de misión para el
 * runtime (POST /missions), exactamente igual que escribirlo en el chat.
 *
 * Honestidad: `running` refleja que el micrófono está abierto de verdad; la
 * primera vez el navegador pide permiso para usar el micrófono. No inventa
 * texto: lo que no reconoce simplemente no lo envía.
 *
 * No requiere backend: el audio nunca sale de tu navegador.
 */

export interface RecognitionCallbacks {
  onStart: () => void;
  onInterim: (text: string) => void;
  onFinal: (text: string) => void;
  onEnd: () => void;
  onError: (message: string) => void;
}

/** Códigos de idioma español prioritarios (latín primero, luego España). */
const SPANISH_CODES = ["es-MX", "es-419", "es-US", "es-AR", "es-CO", "es-PE", "es-CL", "es-ES"];

type AnyRecognition = Record<string, unknown> & {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: unknown;
  onerror: unknown;
  onend: unknown;
};

export class SpeechRecognizer {
  enabled = true;

  private rec: AnyRecognition | null = null;
  running = false;
  private lastFinal = "";
  private cbs: RecognitionCallbacks = {
    onStart: () => {},
    onInterim: () => {},
    onFinal: () => {},
    onEnd: () => {},
    onError: () => {},
  };

  constructor() {
    const Ctor =
      (window as unknown as { SpeechRecognition?: new () => AnyRecognition }).SpeechRecognition ||
      (window as unknown as { webkitSpeechRecognition?: new () => AnyRecognition }).webkitSpeechRecognition;
    if (Ctor) {
      try {
        this.rec = new Ctor();
      } catch {
        this.rec = null;
      }
    }
  }

  get available(): boolean {
    return this.rec !== null;
  }

  on(cb: Partial<RecognitionCallbacks>): void {
    this.cbs = { ...this.cbs, ...cb };
  }

  /** Empieza a escuchar una frase; devuelve true si quedó abierto. */
  start(lang = "es-MX"): boolean {
    if (!this.available || !this.enabled) return false;
    if (this.running) return true;
    const r = this.rec!;
    r.lang = lang;
    r.continuous = false;
    r.interimResults = true;
    r.maxAlternatives = 1;
    this.running = true;
    this.lastFinal = "";

    r.onresult = (ev: { resultIndex: number; results: ArrayLike<{ isFinal: boolean; length: number; [i: number]: { transcript: string } }> }) => {
      let interim = "";
      let finalText = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const res = ev.results[i];
        if (res.isFinal) finalText += res[0].transcript;
        else interim += res[0].transcript;
      }
      if (interim.trim()) this.cbs.onInterim(interim.trim());
      if (finalText.trim()) {
        this.lastFinal = finalText.trim();
        this.cbs.onFinal(this.lastFinal);
      }
    };
    r.onerror = (ev: { error?: string }) => {
      this.running = false;
      this.cbs.onError(ev?.error ? String(ev.error) : "error de reconocimiento");
    };
    r.onend = () => {
      this.running = false;
      this.cbs.onEnd();
    };
    this.cbs.onStart();
    try {
      r.start();
      return true;
    } catch {
      this.running = false;
      return false;
    }
  }

  stop(): void {
    try {
      this.rec?.stop();
    } catch {
      /* ignore */
    }
  }

  abort(): void {
    try {
      this.rec?.abort();
    } catch {
      /* ignore */
    }
    this.running = false;
  }
}