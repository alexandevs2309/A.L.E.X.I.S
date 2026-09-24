import * as THREE from "three";
import { clamp } from "../util/damp";
import { MORPH_NAMES, type MorphName } from "./FaceRig";

export class MorphController {
  private geo: THREE.BufferGeometry;
  private index = new Map<string, number>();

  private get influences(): number[] | undefined {
    return (this.geo as unknown as { morphTargetInfluences?: number[] }).morphTargetInfluences;
  }

  constructor(geo: THREE.BufferGeometry) {
    this.geo = geo;
    MORPH_NAMES.forEach((name, idx) => this.index.set(name, idx));
  }

  set(name: MorphName, value: number): void {
    const idx = this.index.get(name);
    if (idx === undefined) return;
    const values = this.influences;
    if (!values) return;
    values[idx] = clamp(value, 0, 1);
  }

  get(name: MorphName): number {
    const idx = this.index.get(name);
    const values = this.influences;
    if (idx === undefined || !values) return 0;
    return values[idx];
  }

  /** Establece un set de morphs con blending exponencial suave. */
  blend(targets: Partial<Record<MorphName, number>>, lambda: number, dt: number): void {
    for (const name of MORPH_NAMES) {
      const goal = targets[name] ?? 0;
      const cur = this.get(name);
      const next = cur + (goal - cur) * (1 - Math.exp(-lambda * dt));
      this.set(name, next);
    }
  }

  weights(): Record<string, number> {
    const out: Record<string, number> = {};
    MORPH_NAMES.forEach((name) => {
      const v = this.get(name);
      if (v > 0.001) out[name] = Math.round(v * 100) / 100;
    });
    return out;
  }
}