import { clamp } from "../util/damp";
import type { MorphController } from "../avatar/MorphController";

type BlinkKind = "full" | "slow" | "partial" | "double";

interface BlinkSpec {
  kind: BlinkKind;
  duration: number;
  intensity: number; // 0..1
  double: boolean;
  side: "both" | "left" | "right";
}

const KINDS: { kind: BlinkKind; duration: number; intensity: number; double: boolean }[] = [
  { kind: "full", duration: 0.15, intensity: 1, double: false },
  { kind: "full", duration: 0.13, intensity: 0.95, double: false },
  { kind: "slow", duration: 0.32, intensity: 1, double: false },
  { kind: "partial", duration: 0.12, intensity: 0.38, double: false },
  { kind: "partial", duration: 0.1, intensity: 0.5, double: false },
  { kind: "double", duration: 0.34, intensity: 1, double: true },
];

/** Parpadeo natural con variación (directiva §7): no setInterval fijo; random(2.5s, 7s). */
export class BlinkController {
  private nextAt = 0;
  private active: { start: number; kind: BlinkSpec; len: number } | null = null;
  private speed = 1;

  constructor(seed = 0) {
    this.nextAt = performance.now() / 1000 + this.roll();
  }

  setSpeed(v: number): void {
    this.speed = clamp(v, 0.3, 1.6);
  }

  private roll(): number {
    return 2.5 + Math.random() * 4.5;
  }

  /** Trigger manual (p. ej. estado SUCCESS). */
  blinkNow(): void {
    this.nextAt = 0;
  }

  update(dt: number, now: number, morph: MorphController): void {
    if (this.active) {
      const t = (now - this.active.start) / this.active.len;
      if (t >= 1) {
        this.active = null;
        morph.set("blink_both", 0);
        morph.set("blink_left", 0);
        morph.set("blink_right", 0);
        morph.set("eye_squint", 0);
        this.nextAt = now + this.roll();
        return;
      }
      this.apply(t, this.active.kind, morph);
      return;
    }

    if (now >= this.nextAt) {
      const k = KINDS[Math.floor(Math.random() * KINDS.length)];
      const sideRoll = Math.random();
      const side = sideRoll < 0.6 ? "both" : sideRoll < 0.8 ? "left" : "right";
      const len = k.duration / Math.max(this.speed, 0.4);
      this.active = {
        start: now,
        kind: { ...k, side },
        len,
      };
    }
  }

  private apply(t: number, k: BlinkSpec, morph: MorphController): void {
    const cycle = k.double ? this.doubleShape(t) : Math.sin(Math.min(t, 1) * Math.PI) * k.intensity;

    if (k.side === "left" || k.side === "right") {
      morph.set("blink_left", k.side === "left" ? cycle : 0);
      morph.set("blink_right", k.side === "right" ? cycle : 0);
      morph.set("blink_both", 0);
    } else {
      morph.set("blink_both", cycle);
      const squint = k.intensity < 0.6 ? 0 : 0.18 * cycle;
      morph.set("eye_squint", squint);
    }
  }

  private doubleShape(t: number): number {
    // dos parpadeos rápidos en una sola ventana
    if (t < 0.45) return Math.sin((t / 0.45) * Math.PI);
    if (t >= 0.45 && t < 0.75) return 0;
    return Math.sin(((t - 0.75) / 0.25) * Math.PI);
  }
}