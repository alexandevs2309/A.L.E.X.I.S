import * as THREE from "three";

const PARTICLE_VERTEX = /* glsl */ `
  uniform float uTime;
  uniform float uActivity;
  uniform float uMode;
  attribute float aSeed;
  attribute vec3 aBase;
  varying float vLife;
  void main() {
    vec3 dir = normalize(aBase);
    float phase = aSeed * 6.2831;
    float amp = 0.10 + 0.35 * uActivity;
    vec3 p = aBase;
    float swirl = amp * sin(uTime * 0.35 + phase) * smoothstep(0.2, 1.0, uActivity);
    if (uMode > 0.5) {
      // patrón "error": actividad contenida, deriva hacia dentro
      float inw = amp * 0.4 * sin(uTime * 0.9 + phase * 3.0);
      p += dir * inw * (uActivity + 0.4);
    } else {
      p += dir * swirl;
    }
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    float dist = max(-mv.z, 0.5);
    // tamaños clamp de 1-14px para la distancia de cámara (~4.4u):
    // evita fill-rate desbocado que congela la GPU.
    gl_PointSize = max(1.0, min(14.0, (0.6 + 6.0 * uActivity) * (20.0 / dist)));
    vLife = 0.4 + 0.6 * uActivity;
    gl_Position = projectionMatrix * mv;
  }
`;

const PARTICLE_FRAGMENT = /* glsl */ `
  uniform vec3 uColor;
  uniform float uActivity;
  varying float vLife;
  void main() {
    float d = length(gl_PointCoord - 0.5);
    float a = smoothstep(0.5, 0.1, d) * vLife;
    vec3 col = mix(uColor, vec3(0.95, 1.0, 1.0), vLife * 0.4);
    gl_FragColor = vec4(col, a);
  }
`;

/** Partículas que rodean el rostro (THREE.Points, GPU-friendly). */
export class ParticleSystem {
  readonly points: THREE.Points;

  private uniforms: {
    uTime: { value: number };
    uActivity: { value: number };
    uMode: { value: number };
    uColor: { value: THREE.Color };
  };

  constructor(count = 1800, headRadius = 1.35) {
    const positions = new Float32Array(count * 3);
    const base = new Float32Array(count * 3);
    const seeds = new Float32Array(count);

    for (let i = 0; i < count; i++) {
      const u = Math.random() * 2 - 1;
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(u);
      const shell = 0.3 + 0.7 * Math.random() * Math.random();
      const rx = headRadius * (1.0 + 0.28 * shell);
      const ry = headRadius * 1.14 * (1.0 + 0.28 * shell);
      const rz = headRadius * 0.95 * (1.0 + 0.28 * shell);
      const x = rx * Math.sin(phi) * Math.cos(theta);
      const y = ry * Math.cos(phi) - 0.05;
      const z = rz * Math.sin(phi) * Math.sin(theta) + 0.3;

      positions[i * 3] = x;
      positions[i * 3 + 1] = y;
      positions[i * 3 + 2] = z;
      base[i * 3] = x;
      base[i * 3 + 1] = y;
      base[i * 3 + 2] = z;
      seeds[i] = Math.random();
    }

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute("aBase", new THREE.BufferAttribute(base, 3));
    geometry.setAttribute("aSeed", new THREE.BufferAttribute(seeds, 1));

    this.uniforms = {
      uTime: { value: 0 },
      uActivity: { value: 0.15 },
      uMode: { value: 0 },
      uColor: { value: new THREE.Color(0x54c9ef) },
    };

    const material = new THREE.ShaderMaterial({
      uniforms: this.uniforms,
      vertexShader: PARTICLE_VERTEX,
      fragmentShader: PARTICLE_FRAGMENT,
      transparent: true,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });

    this.points = new THREE.Points(geometry, material);
    this.points.frustumCulled = false;
  }

  update(t: number, activity: number, mode: "normal" | "error"): void {
    this.uniforms.uTime.value = t;
    this.uniforms.uActivity.value = activity;
    this.uniforms.uMode.value = mode === "error" ? 1 : 0;
  }
}