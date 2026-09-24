export type VisemeName = "rest" | "aa" | "ee" | "oo" | "mm" | "ss" | "kk";

export interface VisemeEvent {
  name: VisemeName;
  weight: number;
}

/**
 * Controlador de visemas. RESERVADO: todavía no existe TTS/audio en ALEXIS.
 * Esta clase solo define la interfaz que conectarán los mouth blendshapes
 * cuando el LipSync real esté disponible. NO modifica morphs aún.
 */
export class VisemeController {
  private enabled = false;
  private current: VisemeEvent = { name: "rest", weight: 0 };
  private lastApplied: VisemeEvent = { name: "rest", weight: 0 };

  enable(): void {
    this.enabled = true;
  }

  /**
   * Aplica un visema sobre los blendshapes de boca.
   * @remarks Reservado: implementación pendiente de TTS. No produce movimiento hoy.
   */
  applyViseme(name: VisemeName, weight: number): void {
    if (!this.enabled) return;
    this.current = { name, weight };
    // TODO(LipSync): mapear name/weight → morphs mouth_* (mouth_open/smile/round/close)
    this.lastApplied = this.current;
  }

  /** Estado real: sin audio, no hay visemas activos. */
  get active(): boolean {
    return false;
  }

  get last(): VisemeEvent {
    return this.lastApplied;
  }
}