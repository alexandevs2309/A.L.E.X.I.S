import type * as THREE from "three";

/** Gestión de los materiales del rostro (opisidad/emisión por estado). */
export class MaterialController {
  constructor(
    private face: THREE.MeshPhysicalMaterial,
    private opacity: number,
    private emissiveBase: number
  ) {}

  setOpacity(v: number): void {
    this.face.opacity = Math.min(1, Math.max(0.55, v));
  }

  setEmission(boost: number): void {
    this.face.emissiveIntensity = this.emissiveBase + boost;
  }

  setFaceMat(material?: THREE.MeshPhysicalMaterial): void {
    if (material) this.face = material;
  }
}