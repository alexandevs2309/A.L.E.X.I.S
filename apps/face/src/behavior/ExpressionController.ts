import type { MorphName } from "../avatar/FaceRig";
import type { MorphController } from "../avatar/MorphController";

export type ExpressionTargets = Partial<Record<MorphName, number>>;

/**
 * Control de expresiones (default por estado); se mezcla con parpadeo/gaze (disjuntos).
 * FASE 16: durante la VOZ real (Web Speech) se abre la mandíbula a cadencia vocal
 * (~7 Hz, aproximación por tiempo). NO es lip-sync por fonema (RESERVADO).
 */
export class ExpressionController {
  private targets: ExpressionTargets = {};
  private lambda = 5;
  private speechActive = false;
  private phase = 0;

  constructor(private morph: MorphController) {}

  setTargets(targets: ExpressionTargets): void {
    this.targets = targets;
  }

  /** Activa la abertura de boca mientras el VoiceDriver suena (voz real). */
  setSpeech(active: boolean): void {
    this.speechActive = active;
    if (!active) this.phase = 0;
  }

  update(dt: number): void {
    if (this.speechActive) {
      this.phase += dt * 7.2;
      const jaw = 0.12 + 0.1 * (0.5 + 0.5 * Math.sin(this.phase));
      this.morph.blend({ ...this.targets, jaw_open: jaw, mouth_open: jaw * 0.5 }, this.lambda, dt);
      return;
    }
    this.morph.blend(this.targets, this.lambda, dt);
  }
}