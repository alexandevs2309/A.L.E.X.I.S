export type DebugProvider = () => string;

/** Overlay de diagnóstico. SOLO visible con `?debug=1` (directiva §19). */
export class DebugPanel {
  private timer = 0;

  constructor(private panel: HTMLElement) {
    panel.style.display = "block";
  }

  start(provider: DebugProvider, intervalMs = 220): void {
    const tick = () => {
      this.panel.textContent = provider();
    };
    tick();
    this.timer = window.setInterval(tick, intervalMs);
  }

  stop(): void {
    window.clearInterval(this.timer);
  }

  static enabled(): boolean {
    try {
      return new URLSearchParams(window.location.search).has("debug");
    } catch {
      return false;
    }
  }
}