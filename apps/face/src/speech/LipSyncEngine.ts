import type { VisemeController, VisemeEvent } from "./VisemeController";

/**
 * Motor de lip-sync. FASE 16: TTS REAL de ALEXIS = síntesis de voz del navegador
 * (VoiceDriver, español latino). `speaking` refleja que hay Voz sonando.
 * El análisis de fonemas → visemas exactos sigue RESERVADO: la boca se mueve
 * por tiempo (ExpressionController.setSpeech), no por fonema.
 */
export class LipSyncEngine {
  private visemes: VisemeController | null = null;
  private enabled = false;
  private audioLength = 0;
  private speakingFlag = false;

  bind(visemes: VisemeController): void {
    this.visemes = visemes;
  }

  enable(): void {
    this.enabled = true;
    this.visemes?.enable();
  }

  /** Refleja la voz real que observa el VoiceDriver (start/end no mienten). */
  setSpeaking(on: boolean): void {
    this.speakingFlag = on;
  }

  /** Reservado: consumiría el PCM del TTS para extraer visemas por fonema. */
  consumeAudio(_audio: Float32Array): void {
    if (!this.enabled) return;
    this.audioLength = _audio.length;
    // TODO(LipSync): análisis de fonemas → secuencia de VisemeEvent
  }

  /** Verdad objetiva: ¿está sonando la voz propia de ALEXIS? */
  get speaking(): boolean {
    return this.speakingFlag;
  }

  get lastViseme(): VisemeEvent {
    return this.visemes?.last ?? { name: "rest", weight: 0 };
  }
}