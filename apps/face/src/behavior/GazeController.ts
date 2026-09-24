import * as THREE from "three";
import { damp } from "../util/damp";
import type { FaceRigResult } from "../avatar/FaceRig";

export type GazeTarget = "camera" | "user" | "left" | "right" | "up" | "down" | "neutral" | "thinking" | "away";

const PRESETS: Record<GazeTarget, THREE.Vector3> = {
  camera: new THREE.Vector3(0, 0, 1),
  user: new THREE.Vector3(0, 0.02, 1),
  left: new THREE.Vector3(-0.55, 0, 0.85).normalize(),
  right: new THREE.Vector3(0.55, 0, 0.85).normalize(),
  up: new THREE.Vector3(0, 0.35, 0.94).normalize(),
  down: new THREE.Vector3(0, -0.3, 0.95).normalize(),
  neutral: new THREE.Vector3(0, 0, 1),
  thinking: new THREE.Vector3(0.14, 0.3, 0.95).normalize(),
  away: new THREE.Vector3(0.03, 0.03, 0.998).normalize(),
};

const MAX_PITCH = 0.42;
const MAX_YAW = 0.5;

/** Director de la mirada (directiva §6): target → damp → rotación de ojos. */
export class GazeController {
  private target = new THREE.Vector3(0, 0, 1);
  private current = new THREE.Vector3(0, 0, 1);
  private lambda = 6;
  private lastTargetName: GazeTarget = "camera";

  setTarget(name: GazeTarget): void {
    this.lastTargetName = name;
    this.target.copy(PRESETS[name] ?? PRESETS.camera);
  }

  setLook(x: number, y: number, z: number): void {
    this.target.set(x, y, z).normalize();
  }

  get targetName(): GazeTarget {
    return this.lastTargetName;
  }

  targetVector(): THREE.Vector3 {
    return this.target.clone();
  }

  update(dt: number, rig: FaceRigResult): void {
    this.current.x = damp(this.current.x, this.target.x, this.lambda, dt);
    this.current.y = damp(this.current.y, this.target.y, this.lambda, dt);
    this.current.z = damp(this.current.z, this.target.z, this.lambda, dt);

    const yaw = Math.atan2(this.current.x, this.current.z);
    const pitch = Math.asin(Math.max(-1, Math.min(1, this.current.y)));
    const yawC = Math.max(-MAX_YAW, Math.min(MAX_YAW, yaw));
    const pitchC = Math.max(-MAX_PITCH, Math.min(MAX_PITCH, pitch));

    rig.eyeL.rotation.y = yawC;
    rig.eyeL.rotation.x = pitchC;
    rig.eyeR.rotation.y = yawC;
    rig.eyeR.rotation.x = pitchC;
  }
}