import { damp } from "../util/damp";
import type { FaceRigResult } from "../avatar/FaceRig";
import type { MicroMotionOutput } from "./MicroMotionController";

export interface HeadRotation {
  yaw: number;
  pitch: number;
  roll: number;
}

/** Micro-movimientos de cabeza (directiva §8): yaw/pitch/roll lentos, pequeños, por estado. */
export class HeadMotionController {
  private target: HeadRotation = { yaw: 0, pitch: 0, roll: 0 };
  private current: HeadRotation = { yaw: 0, pitch: 0, roll: 0 };
  private lambda = 3.2;

  setTarget(yaw: number, pitch: number, roll: number): void {
    this.target = { yaw, pitch, roll };
  }

  get orientation(): HeadRotation {
    return this.current;
  }

  update(dt: number, micro: MicroMotionOutput, rig: FaceRigResult): void {
    this.current.yaw = damp(this.current.yaw, this.target.yaw, this.lambda, dt);
    this.current.pitch = damp(this.current.pitch, this.target.pitch, this.lambda, dt);
    this.current.roll = damp(this.current.roll, this.target.roll, this.lambda, dt);

    rig.headRig.rotation.order = "YXZ";
    rig.headRig.rotation.y = this.current.yaw + micro.yaw;
    rig.headRig.rotation.x = this.current.pitch + micro.pitch;
    rig.headRig.rotation.z = this.current.roll + micro.roll;
  }
}