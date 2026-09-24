import * as THREE from "three";

export interface FrameStats {
  fps: number;
  frameMs: number;
}

export class FaceScene {
  readonly renderer: THREE.WebGLRenderer;
  readonly scene: THREE.Scene;
  readonly camera: THREE.PerspectiveCamera;

  private raf = 0;
  private clock = new THREE.Clock();
  private fps = 60;
  private disposed = false;

  constructor(container: HTMLElement) {
    this.renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: true,
      powerPreference: "default",
      failIfMajorPerformanceCaveat: false,
    });
    this.renderer.setPixelRatio(Math.min(globalThis.devicePixelRatio || 1, 1.5));
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.0;
    container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x05090f);

    this.camera = new THREE.PerspectiveCamera(42, 1, 0.1, 30);
    this.camera.position.set(0, 0.0, 4.4);
    this.camera.lookAt(0, -0.1, 0);

    const ambient = new THREE.AmbientLight(0x4a6a86, 1.1);
    const key = new THREE.DirectionalLight(0xbcdcff, 1.35);
    key.position.set(1.2, 1.6, 2.4);
    const rim = new THREE.DirectionalLight(0x1fb3f5, 0.7);
    rim.position.set(-1.4, -0.8, -1.2);
    this.scene.add(ambient, key, rim);

    this.resize();
    globalThis.addEventListener("resize", this.resize);
  }

  add(object: THREE.Object3D): void {
    this.scene.add(object);
  }

  addUpdate(fn: THREE.Object3D): void {}

  start(update: (dt: number, t: number) => void): void {
    const loop = () => {
      if (this.disposed) return;
      const dt = Math.min(this.clock.getDelta(), 0.05);
      const t = this.clock.elapsedTime;
      update(dt, t);
      this.renderer.render(this.scene, this.camera);
      this.fps = this.fps * 0.92 + (1 / Math.max(dt, 1e-4)) * 0.08;
      this.raf = requestAnimationFrame(loop);
    };
    this.raf = requestAnimationFrame(loop);
  }

  getStats(): FrameStats {
    return { fps: Math.round(this.fps), frameMs: Math.round(1000 / Math.max(this.fps, 1)) };
  }

  private resize = (): void => {
    const w = globalThis.innerWidth;
    const h = globalThis.innerHeight;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  };

  dispose(): void {
    this.disposed = true;
    cancelAnimationFrame(this.raf);
    globalThis.removeEventListener("resize", this.resize);
    this.renderer.dispose();
  }
}