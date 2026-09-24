export function hash1(x: number): number {
  const s = Math.sin(x * 127.1 + 311.7) * 43758.5453;
  return s - Math.floor(s);
}

export function valueNoise1D(x: number): number {
  const i = Math.floor(x);
  const f = x - i;
  return hash1(i) * (1 - f) + hash1(i + 1) * f;
}

export function fbm1D(x: number, octaves = 3): number {
  let sum = 0;
  let amp = 0.5;
  let freq = 1;
  for (let o = 0; o < octaves; o++) {
    sum += amp * valueNoise1D(x * freq);
    amp *= 0.5;
    freq *= 2.1;
  }
  return sum;
}

export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}