import * as THREE from "three";

export const MORPH_NAMES = [
  "neutral",
  "blink_left",
  "blink_right",
  "blink_both",
  "brow_up",
  "brow_down",
  "brow_inner",
  "eye_squint",
  "mouth_open",
  "mouth_close",
  "mouth_smile",
  "mouth_frown",
  "mouth_round",
  "jaw_open",
  "jaw_left",
  "jaw_right",
  "cheek_raise",
  "cheek_compress",
] as const;

export type MorphName = (typeof MORPH_NAMES)[number];

function g2(x: number, cx: number, y: number, cy: number, sx: number, sy: number): number {
  return Math.exp(-((x - cx) * (x - cx)) / (2 * sx) - ((y - cy) * (y - cy)) / (2 * sy));
}

const EYE_X = 0.34;
const EYE_Y = 0.06;
const LID_Y = 0.14;

function lidMask(x: number, y: number, side: number): number {
  const sum =
    g2(x, EYE_X, y, LID_Y, 0.02, 0.022) +
    g2(x, -EYE_X, y, LID_Y, 0.02, 0.022);
  const s = Math.max(sum, 0);
  if (side === 0) return Math.min(s, 1);
  return g2(x, side * EYE_X, y, LID_Y, 0.02, 0.022);
}

function browMask(x: number, y: number): number {
  return Math.max(
    g2(x, 0.16, y, 0.27, 0.028, 0.014),
    g2(x, -0.16, y, 0.27, 0.028, 0.014)
  );
}

function cheekMask(x: number, y: number): number {
  return Math.max(
    g2(x, 0.3, y, -0.08, 0.03, 0.028),
    g2(x, -0.3, y, -0.08, 0.03, 0.028)
  );
}

function mouthMask(x: number, y: number): number {
  return g2(x, 0, y, 0.0, 0.021, 0.02);
}

function chinMask(x: number, y: number): number {
  const down = 1 - Math.min(1, Math.max(0, (y + 0.26) / 0.24));
  return down * (0.45 + 0.55 * Math.exp(-(x * x) / 0.35));
}

/** Construye la cabeza procedural (placeholder técnicamente correcto) con morph targets reales. */
export function buildHeadGeometry(): THREE.BufferGeometry {
  const geo = new THREE.SphereGeometry(1, 60, 60);
  const src = geo.attributes.position as THREE.BufferAttribute;
  const count = src.count;
  const base = new Float32Array(count * 3);

  for (let i = 0; i < count; i++) {
    const x = src.getX(i);
    const y = src.getY(i);
    const z = src.getZ(i);

    let ny = y * 1.22;
    if (ny > 0) ny *= 1 + 0.05 * Math.max(0, 0.55 - Math.abs(z));
    const chin = Math.max(0, -ny);
    ny -= 0.1 * (chin * chin / 0.35) * Math.max(0, -z);
    const brow = Math.max(0, (ny - 0.22) / 0.42) * Math.max(0, z / 0.55);
    let nz = z + 0.085 * Math.min(1, brow);

    const nose = 0.16 * Math.exp(-(x * x) / 0.045 - ((ny - 0.02) * (ny - 0.02)) / 0.16);
    nz += nose;

    const eyeIndent =
      g2(x, EYE_X, ny, EYE_Y, 0.028, 0.05) + g2(x, -EYE_X, ny, EYE_Y, 0.028, 0.05);
    nz -= 0.03 * eyeIndent;

    let nx = x;
    nx *= 1 + 0.05 * Math.exp(-ny * ny * 2.2) * Math.min(1, Math.max(0, nz / 0.4));
    if (nz < 0) nz *= 1.03;

    base[i * 3] = nx;
    base[i * 3 + 1] = ny;
    base[i * 3 + 2] = nz;
  }

  geo.setAttribute("position", new THREE.BufferAttribute(base, 3));
  geo.computeVertexNormals();

  const morphTargets: Record<string, Float32Array> = {};

  for (const name of MORPH_NAMES) {
    morphTargets[name] = new Float32Array(count * 3);
  }
  const lid = (side: number, w = 1) => (i: number): { dy: number; dx?: number; dz?: number } => {
    const x = base[i * 3];
    const y = base[i * 3 + 1];
    const m = lidMask(x, y, side) * w;
    return { dy: -0.5 * m, dz: 0.02 * m };
  };

  const apply = (name: MorphName, fn: (i: number) => { dy: number; dx?: number; dz?: number }, weight = 1) => {
    const t = morphTargets[name];
    for (let i = 0; i < count; i++) {
      const d = fn(i);
      t[i * 3] += (d.dx ?? 0) * weight;
      t[i * 3 + 1] += d.dy * weight;
      t[i * 3 + 2] += (d.dz ?? 0) * weight;
    }
  };

  apply("blink_both", lid(0));
  apply("blink_left", lid(-1));
  apply("blink_right", lid(1));

  apply("eye_squint", (i) => {
    const x = base[i * 3];
    const y = base[i * 3 + 1];
    const inner = Math.max(
      g2(x, EYE_X, y, 0.06, 0.018, 0.028),
      g2(x, -EYE_X, y, 0.06, 0.018, 0.028)
    );
    const lower = Math.max(
      g2(x, EYE_X, y, 0.0, 0.02, 0.02),
      g2(x, -EYE_X, y, 0.0, 0.02, 0.02)
    );
    const m = Math.min(1, inner * 0.7 + lower * 0.6);
    return { dy: -0.2 * m + 0.06 * cheekMask(x, y) * 0.4 };
  });

  apply("brow_up", (i) => ({ dy: 0.1 * browMask(base[i * 3], base[i * 3 + 1]) }));
  apply("brow_down", (i) => ({ dy: -0.08 * browMask(base[i * 3], base[i * 3 + 1]) }));
  apply("brow_inner", (i) => {
    const x = base[i * 3];
    const y = base[i * 3 + 1];
    const m = Math.max(g2(x, 0.1, y, 0.25, 0.025, 0.014), g2(x, -0.1, y, 0.25, 0.025, 0.014));
    return { dy: -0.05 * m, dx: -Math.sign(x) * 0.03 * m };
  });

  apply("mouth_open", (i) => {
    const m = mouthMask(base[i * 3], base[i * 3 + 1]);
    return { dy: -0.34 * m, dx: 0.02 * Math.sign(base[i * 3]) * m };
  });
  // mouth_close: reservado (neutral); morph existe sin desplazamiento
  apply("mouth_smile", (i) => {
    const x = base[i * 3];
    const y = base[i * 3 + 1];
    const m = Math.max(
      g2(x, 0.16, y, -0.42, 0.02, 0.022),
      g2(x, -0.16, y, -0.42, 0.02, 0.022)
    );
    return { dy: 0.09 * m, dx: Math.sign(x) * 0.05 * m };
  });
  apply("mouth_frown", (i) => {
    const x = base[i * 3];
    const y = base[i * 3 + 1];
    const m = Math.max(
      g2(x, 0.16, y, -0.42, 0.02, 0.022),
      g2(x, -0.16, y, -0.42, 0.02, 0.022)
    );
    return { dy: -0.07 * m };
  });
  apply("mouth_round", (i) => {
    const m = mouthMask(base[i * 3], base[i * 3 + 1]);
    return { dz: 0.1 * m, dy: 0.02 * m };
  });

  apply("jaw_open", (i) => {
    const m = chinMask(base[i * 3], base[i * 3 + 1]);
    return { dy: -0.22 * m };
  });
  apply("jaw_left", (i) => {
    const m = chinMask(base[i * 3], base[i * 3 + 1]);
    return { dx: 0.13 * m, dy: 0.02 * m };
  });
  apply("jaw_right", (i) => {
    const m = chinMask(base[i * 3], base[i * 3 + 1]);
    return { dx: -0.13 * m, dy: 0.02 * m };
  });

  apply("cheek_raise", (i) => {
    const m = cheekMask(base[i * 3], base[i * 3 + 1]);
    return { dy: 0.08 * m };
  });
  apply("cheek_compress", (i) => {
    const x = base[i * 3];
    const m = cheekMask(x, base[i * 3 + 1]);
    return { dy: 0, dx: -Math.sign(x) * 0.05 * m };
  });

  const attributes: THREE.BufferAttribute[] = MORPH_NAMES.map(
    (name) => new THREE.BufferAttribute(morphTargets[name], 3)
  );
  geo.morphAttributes.position = attributes;
  const dict: Record<string, number> = {};
  MORPH_NAMES.forEach((name, idx) => {
    dict[name] = idx;
  });
  const morphState = geo as unknown as {
    morphTargetDictionary?: Record<string, number>;
    morphTargetInfluences?: number[];
  };
  morphState.morphTargetDictionary = dict;
  morphState.morphTargetInfluences = new Array(MORPH_NAMES.length).fill(0);

  return geo;
}

function createEyeTexture(): THREE.Texture {
  const c = document.createElement("canvas");
  c.width = 128;
  c.height = 128;
  const ctx = c.getContext("2d")!;
  const g = ctx.createRadialGradient(64, 62, 4, 64, 64, 60);
  g.addColorStop(0, "#eafaff");
  g.addColorStop(0.35, "#8fd8ff");
  g.addColorStop(0.72, "#19b7f5");
  g.addColorStop(1, "#06324f");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 128, 128);
  ctx.beginPath();
  ctx.arc(64, 64, 7, 0, Math.PI * 2);
  ctx.fillStyle = "#02111f";
  ctx.fill();
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

function createEye(): THREE.Group {
  const group = new THREE.Group();
  const sclera = new THREE.Mesh(
    new THREE.SphereGeometry(0.058, 24, 16),
    new THREE.MeshStandardMaterial({ color: 0x0a2033, roughness: 0.3, emissive: 0x0d3d5c, emissiveIntensity: 0.4 })
  );
  const iris = new THREE.Mesh(
    new THREE.CircleGeometry(0.041, 32),
    new THREE.MeshBasicMaterial({ map: createEyeTexture(), transparent: true })
  );
  iris.position.z = 0.057;
  const glow = new THREE.Sprite(
    new THREE.SpriteMaterial({
      map: (() => {
        const c = document.createElement("canvas");
        c.width = 64;
        c.height = 64;
        const ctx2 = c.getContext("2d")!;
        const rg = ctx2.createRadialGradient(32, 32, 2, 32, 32, 30);
        rg.addColorStop(0, "rgba(90,224,255,0.85)");
        rg.addColorStop(1, "rgba(90,224,255,0)");
        ctx2.fillStyle = rg;
        ctx2.fillRect(0, 0, 64, 64);
        return new THREE.CanvasTexture(c);
      })(),
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    })
  );
  glow.scale.setScalar(0.2);
  group.add(sclera, iris, glow);
  return group;
}

export interface FaceRigResult {
  faceRoot: THREE.Group;
  headRig: THREE.Group;
  headMesh: THREE.Mesh;
  neck: THREE.Group;
  jaw: THREE.Object3D;
  eyeL: THREE.Group;
  eyeR: THREE.Group;
  geometry: THREE.BufferGeometry;
  pointsMesh: THREE.Points;
  linesMesh: THREE.LineSegments;
}

/** Rig facial (directiva §5): Head / Neck / Jaw / Eye.L / Eye.R / FaceRoot. */
export function buildFaceRig(
  faceMaterial: THREE.Material,
  pointOverlay: THREE.Material,
  lineOverlay: THREE.Material
): FaceRigResult {
  const geometry = buildHeadGeometry();

  const head = new THREE.Mesh(geometry, faceMaterial);
  const faceWithMorphs = head.material as THREE.MeshPhysicalMaterial & { morphTargets?: boolean };
  faceWithMorphs.morphTargets = true;
  head.material = faceWithMorphs;

  const addPhase = (geom: THREE.BufferGeometry | THREE.EdgesGeometry): void => {
    const p = geom.getAttribute("position");
    if (!p) return;
    const n = p.count;
    const phases = new Float32Array(n);
    for (let i = 0; i < n; i++) phases[i] = Math.random();
    geom.setAttribute("aPhase", new THREE.BufferAttribute(phases, 1));
  };

  const points = new THREE.Points(geometry.clone(), pointOverlay);
  points.frustumCulled = false;
  addPhase(points.geometry);
  const edges = new THREE.EdgesGeometry(geometry, 45);
  const lines = new THREE.LineSegments(edges, lineOverlay);
  lines.frustumCulled = false;
  addPhase(lines.geometry);

  const headRig = new THREE.Group();
  headRig.add(head, points, lines);

  const neck = new THREE.Group();
  const neckMesh = new THREE.Mesh(
    new THREE.CylinderGeometry(0.3, 0.42, 0.5, 24),
    new THREE.MeshStandardMaterial({ color: 0x0a2033, roughness: 0.6, emissive: 0x0d3d5c, emissiveIntensity: 0.3 })
  );
  neckMesh.position.y = -0.9;
  neck.add(neckMesh);

  const jaw = new THREE.Object3D();
  jaw.name = "Jaw";
  jaw.position.set(0, -0.52, 0.42);

  const eyeL = createEye();
  eyeL.position.set(-EYE_X, EYE_Y, 0.24);
  const eyeR = createEye();
  eyeR.position.set(EYE_X, EYE_Y, 0.24);
  eyeL.name = "Eye.L";
  eyeR.name = "Eye.R";

  headRig.add(jaw, eyeL, eyeR);

  const faceRoot = new THREE.Group();
  faceRoot.add(headRig, neck);
  faceRoot.name = "FaceRoot";

  return { faceRoot, headRig, headMesh: head, neck, jaw, eyeL, eyeR, geometry, pointsMesh: points, linesMesh: lines };
}