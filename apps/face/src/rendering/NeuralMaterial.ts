import * as THREE from "three";

export interface NeuralUniforms {
  [uniform: string]: THREE.IUniform;
  uTime: THREE.IUniform<number>;
  uIntensity: THREE.IUniform<number>;
  uBaseColor: THREE.IUniform<THREE.Color>;
}

/** Material neuronal del rostro (directiva §11): base oscura + emission cian/azul. */
export function createFaceMaterial(): THREE.MeshPhysicalMaterial {
  const mat = new THREE.MeshPhysicalMaterial({
    color: 0x1b2c40,
    metalness: 0.25,
    roughness: 0.42,
    emissive: new THREE.Color(0x0d3d5c),
    emissiveIntensity: 0.55,
    transparent: true,
    opacity: 0.94,
  });
  (mat as unknown as { morphTargets: boolean }).morphTargets = true;
  return mat;
}

/** Puntos neuronales sobre la superficie del rostro (GLSL, additive, GPU-friendly). */
export function createNetworkPointsMaterial(): THREE.ShaderMaterial {
  const uniforms: NeuralUniforms = {
    uTime: { value: 0 },
    uIntensity: { value: 1 },
    uBaseColor: { value: new THREE.Color(0x7fd6ff) },
  };
  return new THREE.ShaderMaterial({
    transparent: true,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    uniforms,
    vertexShader: /* glsl */ `
      uniform float uTime;
      uniform float uIntensity;
      attribute float aPhase;
      varying float vFade;
      void main() {
        vec3 p = position;
        float pulse = 0.06 * uIntensity * sin(uTime * 2.2 + aPhase * 6.2831);
        p += normal * pulse;
        vec4 mv = modelViewMatrix * vec4(p, 1.0);
        // factor de tamaño corregido para la distancia de cámara (~4.4u):
        // tamaños de 2-6px, nunca gigantes (evita freeze por overdraw).
        gl_PointSize = clamp((0.15 + 0.10 * uIntensity) * (260.0 / -mv.z), 1.0, 6.0);
        vFade = 0.55 + 0.45 * sin(uTime * 3.0 + aPhase * 20.0);
        gl_Position = projectionMatrix * mv;
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uIntensity;
      uniform vec3 uBaseColor;
      varying float vFade;
      void main() {
        vec2 c = gl_PointCoord - 0.5;
        float d = length(c);
        float a = smoothstep(0.5, 0.12, d) * (0.35 + 0.65 * uIntensity) * vFade;
        vec3 col = uBaseColor * (0.7 + 0.45 * vFade);
        gl_FragColor = vec4(col, a);
      }
    `,
  });
}

/** Líneas de malla digital (wireframe suave) additive. */
export function createNetworkLinesMaterial(): THREE.ShaderMaterial {
  const uniforms: NeuralUniforms = {
    uTime: { value: 0 },
    uIntensity: { value: 1 },
    uBaseColor: { value: new THREE.Color(0x36c6f5) },
  };
  return new THREE.ShaderMaterial({
    transparent: true,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    uniforms,
    vertexShader: /* glsl */ `
      uniform float uTime;
      varying float vNoise;
      attribute float aPhase;
      void main() {
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        vNoise = 0.5 + 0.5 * sin(uTime * 1.6 + aPhase * 18.0);
        gl_Position = projectionMatrix * mv;
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uIntensity;
      uniform vec3 uBaseColor;
      varying float vNoise;
      void main() {
        float a = (0.10 + 0.22 * uIntensity) * (0.45 + 0.55 * vNoise);
        gl_FragColor = vec4(uBaseColor, a);
      }
    `,
  });
}

export class NeuralMaterialController {
  readonly face: THREE.MeshPhysicalMaterial;
  readonly points: THREE.ShaderMaterial;
  readonly lines: THREE.ShaderMaterial;

  constructor(face: THREE.MeshPhysicalMaterial, points: THREE.ShaderMaterial, lines: THREE.ShaderMaterial) {
    this.face = face;
    this.points = points;
    this.lines = lines;
  }

  addPointPhases(geometry: THREE.BufferGeometry): void {
    const n = geometry.attributes.position.count;
    const phases = new Float32Array(n);
    for (let i = 0; i < n; i++) phases[i] = Math.random();
    geometry.setAttribute("aPhase", new THREE.BufferAttribute(phases, 1));
  }

  addLinePhases(geom: THREE.BufferGeometry | THREE.EdgesGeometry): void {
    const p = geom.getAttribute("position");
    if (!p) return;
    const n = p.count;
    const phases = new Float32Array(n);
    for (let i = 0; i < n; i++) phases[i] = Math.random();
    geom.setAttribute("aPhase", new THREE.BufferAttribute(phases, 1));
  }

  update(t: number, activity: number, glowBoost: number): void {
    const un = (m: THREE.ShaderMaterial) => m.uniforms as unknown as NeuralUniforms;
    const a = activity * (1 + glowBoost);
    this.face.emissiveIntensity = 0.55 + 0.8 * activity + 1.6 * glowBoost;
    this.face.opacity = 0.94 - 0.06 * activity;
    un(this.points).uTime.value = t;
    un(this.points).uIntensity.value = a;
    un(this.lines).uTime.value = t;
    un(this.lines).uIntensity.value = a;
  }
}