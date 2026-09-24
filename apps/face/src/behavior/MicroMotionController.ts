import { valueNoise1D, fbm1D } from "../util/noise";

export interface MicroMotionOutput {
  yaw: number;
  pitch: number;
  roll: number;
  /** respiración visual (desplazamiento vertical sutil) */
  breath: number;
  /** centelleo del glow */
  glowFlicker: number;
}

/**
 * Motor de micro-movimiento (directiva §9): ruido procedural de bajísima amplitud.
 * El rostro nunca se congela; nada se exagera.
 */
export class MicroMotionController {
  private out: MicroMotionOutput = { yaw: 0, pitch: 0, roll: 0, breath: 0, glowFlicker: 0 };

  update(t: number, damping: number): void {
    const slow = Math.sin(t * 0.9) * 0.004;
    const slow2 = Math.cos(t * 0.63) * 0.003;
    const noiseYaw = (valueNoise1D(t * 0.35) - 0.5) * 0.008;
    const noisePitch = (valueNoise1D(t * 0.29 + 7.3) - 0.5) * 0.008;
    const noiseRoll = (valueNoise1D(t * 0.41 + 3.1) - 0.5) * 0.005;
    const breath = Math.sin(t * 1.6) * 0.004;
    const flicker = 0.018 * valueNoise1D(t * 3.2) + 0.01 * fbm1D(t * 1.7, 2);

    this.out.yaw = (slow + noiseYaw) * damping;
    this.out.pitch = (slow2 + noisePitch) * damping;
    this.out.roll = noiseRoll * damping;
    this.out.breath = breath * damping;
    this.out.glowFlicker = flicker * damping;
  }

  get output(): MicroMotionOutput {
    return this.out;
  }
}