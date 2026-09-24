/**
 * Conductor de VOZ REAL (FASE 16): síntesis de voz del navegador (Web Speech API),
 * en ESPAÑOL LATINO (es-419 → es-MX/es-US/es-AR… sobre es-ES). Sin servidor TTS.
 *
 * Honestidad: `speaking` refleja una voz que suena de verdad (start/end del
 * SpeechSynthesisUtterance). El análisis de fonemas → visemas exactos sigue
 * RESERVADO; la boca se mueve por tiempo mientras la voz está activa
 * (ver ExpressionController.setSpeech), documentado como aproximación.
 */

export interface VoiceDriverHandlers {
  onSpeechStart: () => void;
  onSpeechEnd: () => void;
}

/** Códigos de voz latinoamericana que se priorizan sobre es-ES (español ibérico). */
const LATINO_CODES: ReadonlyArray<string> = [
  "es-419",
  "es-mx",
  "es-us",
  "es-ar",
  "es-co",
  "es-cl",
  "es-pe",
  "es-ve",
  "es-ec",
  "es-bo",
  "es-py",
  "es-uy",
  "es-gt",
];

export class VoiceDriver {
  enabled = true;

  private synth: SpeechSynthesis | null = null;
  private voice: SpeechSynthesisVoice | null = null;
  private spoken = false;
  private handlers: VoiceDriverHandlers = { onSpeechStart: () => {}, onSpeechEnd: () => {} };

  constructor() {
    if (typeof window !== "undefined" && "speechSynthesis" in window) {
      this.synth = window.speechSynthesis;
      this.pickLatinoVoice();
      this.synth.addEventListener?.("voiceschanged", () => this.pickLatinoVoice());
    }
  }

  get available(): boolean {
    return this.synth !== null;
  }

  /** Verdad objetiva: hay una voz sonando en este momento. */
  get speaking(): boolean {
    return this.spoken;
  }

  get voiceName(): string {
    return this.voice ? `${this.voice.name} (${this.voice.lang})` : "voz por defecto del navegador";
  }

  on(cb: Partial<VoiceDriverHandlers>): void {
    this.handlers = { ...this.handlers, ...cb };
  }

  private pickLatinoVoice(): void {
    const voices = this.synth?.getVoices?.() ?? [];
    const espanol = voices.filter((v) => (v.lang || "").toLowerCase().startsWith("es"));
    const latinos = espanol.filter((v) => LATINO_CODES.some((c) => (v.lang || "").toLowerCase().startsWith(c)));
    this.voice = latinos[0] ?? espanol[0] ?? null;
    if (espanol.length > 0 && !latinos.length) {
      console.info(
        `VoiceDriver: no hay voz es-419/latina; usaré «${this.voice?.name ?? "…"}» (${this.voice?.lang ?? "?"}).`,
      );
    }
  }

  speak(text: string, lang = "es-419"): void {
    const clean = text.trim();
    if (!this.enabled || !this.synth || !clean) return;
    this.cancel();
    try {
      const u = new SpeechSynthesisUtterance(clean);
      if (this.voice) {
        u.voice = this.voice;
        u.lang = this.voice.lang;
      } else {
        u.lang = lang;
      }
      u.rate = 1.0;
      u.pitch = 1.0;
      u.volume = 1.0;
      u.onstart = () => {
        this.spoken = true;
        this.handlers.onSpeechStart();
      };
      const end = (): void => {
        if (!this.spoken) return;
        this.spoken = false;
        this.handlers.onSpeechEnd();
      };
      u.onend = end;
      u.onerror = () => end();
      this.synth.speak(u);
    } catch {
      /* Voz no disponible → la respuesta queda solo en texto (honesto). */
    }
  }

  cancel(): void {
    this.synth?.cancel();
    if (this.spoken) {
      this.spoken = false;
      this.handlers.onSpeechEnd();
    }
  }

  /** Despierta la síntesis tras el primer gesto del usuario (políticas de autoplay). */
  unlock(): void {
    try {
      this.synth?.resume();
    } catch {
      /* ignore */
    }
  }
}