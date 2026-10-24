/* Orbwise – der Orb: drei feine Ringe um einen Punkt, in der Akzentfarbe des Themas.
 *
 * Zustände zeigen sich ruhig über Farbe und Bewegung:
 *   idle      atmet langsam              listening  Ringe folgen der Mikrofon-Lautstärke (grün)
 *   thinking  zwei Bögen kreisen          speaking   Ringe wellen mit der Stimme
 *   executing gestrichelter Außenring     confirm    pulsiert (warn)     error  rot     offline  grau
 * Außen ein dünner Bogen für die Kontext-Auslastung, kleine Punkte für laufende Werkzeuge (Satelliten).
 * Farben kommen aus den CSS-Variablen – hell und dunkel passen damit von selbst.
 */
(() => {
  const TAU = Math.PI * 2;
  const lerp = (a, b, t) => a + (b - a) * t;

  // Zustand → CSS-Variable der Farbe, Tempo, wie stark die Ringe auf Ton reagieren
  const STATES = {
    offline:   { color: "--faint",   speed: 0,    react: 0 },
    idle:      { color: "--accent",  speed: 0.25, react: 0 },
    listening: { color: "--success", speed: 0.5,  react: 1 },
    thinking:  { color: "--reason",  speed: 1.2,  react: 0 },
    speaking:  { color: "--accent",  speed: 0.6,  react: 1 },
    executing: { color: "--warn",    speed: 1,    react: 0 },
    confirm:   { color: "--warn",    speed: 0.4,  react: 0 },
    error:     { color: "--danger",  speed: 0.3,  react: 0 },
  };

  function parseColor(text) {
    const s = (text || "").trim();
    let m = s.match(/^#([0-9a-f]{6})$/i);
    if (m) return [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16));
    m = s.match(/^#([0-9a-f]{3})$/i);
    if (m) return [...m[1]].map((c) => parseInt(c + c, 16));
    m = s.match(/rgba?\(([^)]+)\)/);
    if (m) return m[1].split(",").slice(0, 3).map((v) => parseFloat(v));
    return [43, 111, 115];
  }

  class Orb {
    constructor(canvas, overlay = null) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      if (overlay) overlay.getContext("2d").clearRect(0, 0, overlay.width, overlay.height);  // nicht mehr gebraucht
      this.state = "offline";
      this.colors = {};
      this.color = [120, 120, 120];
      this.level = 0;
      this.targetLevel = 0;
      this.spectrum = null;
      this.tokenEnergy = 0;
      this.ctxFrac = null;
      this.ctxShow = 0;
      this.ctxFlash = 0;
      this.memIds = new Set();
      this.memLevel = 0;
      this.satellites = new Map();   // id → {born, failed, until}
      this.bootT = null;
      this.t = 0;
      this.rot = 0;
      this.reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      this._readColors();
      // Thema gewechselt (hell/dunkel) → Farben neu lesen
      new MutationObserver(() => this._readColors()).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
      this._resize = this._resize.bind(this);
      window.addEventListener("resize", this._resize);
      if (window.ResizeObserver) new ResizeObserver(() => this._resize()).observe(this.canvas);
      this._resize();
      this.last = performance.now();
      requestAnimationFrame(this._frame.bind(this));
    }

    // ---------------------------------------------------------------- Schnittstelle (wie bisher)
    setState(s) { if (STATES[s]) this.state = s; }
    setLevel(v) { this.targetLevel = Math.max(0, Math.min(1, v)); }
    setSpectrum(arr) { this.spectrum = arr; }
    setZoom() { /* früher: in den Orb zoomen – der Gedankengang steht jetzt im Chat */ }
    token() { this.tokenEnergy = Math.min(14, this.tokenEnergy + 1); }
    boot() { if (!this.reduced) this.bootT = 0; }

    setContext(frac, summarized) {
      frac = Math.max(0, Math.min(1, frac || 0));
      if (this.ctxFrac !== null && (frac < this.ctxFrac - 0.08 || summarized)) this.ctxFlash = 1;
      if (this.ctxFrac === null) this.ctxShow = frac;
      this.ctxFrac = frac;
    }

    memoryGlow(id, on) {
      if (on) this.memIds.add(id);
      else this.memIds.delete(id);
    }

    addSatellite(id) {
      if (!this.satellites.has(id)) this.satellites.set(id, { born: this.t, failed: false, until: null });
    }

    removeSatellite(id, failed = false) {
      const s = this.satellites.get(id);
      if (!s) return;
      s.failed = failed;
      s.until = this.t + (failed ? 1.2 : 0.4);  // kurz nachleuchten, Fehler etwas länger
    }

    clearSatellites(keepPrefix = "") {
      for (const id of [...this.satellites.keys()]) if (!keepPrefix || !id.startsWith(keepPrefix)) this.satellites.delete(id);
    }

    // ---------------------------------------------------------------- intern
    _readColors() {
      const css = getComputedStyle(document.documentElement);
      for (const name of ["--accent", "--success", "--reason", "--warn", "--danger", "--faint", "--border-strong"]) {
        this.colors[name] = parseColor(css.getPropertyValue(name));
      }
    }

    _rgba(rgb, a) { return `rgba(${rgb[0] | 0},${rgb[1] | 0},${rgb[2] | 0},${a})`; }

    _resize() {
      const r = this.canvas.getBoundingClientRect();
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      this.w = r.width;
      this.h = r.height;
      this.canvas.width = Math.max(1, Math.round(r.width * dpr));
      this.canvas.height = Math.max(1, Math.round(r.height * dpr));
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    _frame(now) {
      requestAnimationFrame(this._frame.bind(this));
      const dt = Math.min(0.05, (now - this.last) / 1000);
      if (document.hidden || this.w < 2) { this.last = now; return; }
      // ruhige Zustände brauchen keine 60 Bilder pro Sekunde
      const calm = (this.state === "idle" || this.state === "offline") && !this.satellites.size && this.bootT === null;
      if (calm && dt < 1 / 30) return;
      this.last = now;
      const st = STATES[this.state];
      const target = this.colors[st.color] || this.colors["--accent"];
      const k = 1 - Math.pow(0.02, dt);
      for (let i = 0; i < 3; i++) this.color[i] = lerp(this.color[i], target[i], k);
      this.level = lerp(this.level, this.targetLevel * st.react, 1 - Math.pow(0.001, dt));
      this.tokenEnergy = Math.max(0, this.tokenEnergy - dt * 6);
      const motion = this.reduced ? 0.2 : 1;
      this.t += dt * motion;
      this.rot += dt * motion * (st.speed + this.tokenEnergy * 0.08);
      this.memLevel = lerp(this.memLevel, this.memIds.size ? 1 : 0, k);
      if (this.ctxFrac !== null) this.ctxShow = lerp(this.ctxShow, this.ctxFrac, k);
      this.ctxFlash = Math.max(0, this.ctxFlash - dt * 0.8);
      if (this.bootT !== null && (this.bootT += dt) > 1.6) this.bootT = null;
      for (const [id, s] of this.satellites) if (s.until !== null && this.t > s.until) this.satellites.delete(id);
      this._draw();
    }

    _draw() {
      const ctx = this.ctx;
      const w = this.w, h = this.h;
      ctx.clearRect(0, 0, w, h);
      const cx = w / 2, cy = h / 2;
      const R = Math.min(w, h) * 0.38;
      const c = this.color;
      const st = this.state;
      const breathe = st === "idle" ? Math.sin(this.t * 1.4) * 0.012 : 0;
      const pulse = st === "confirm" ? (Math.sin(this.t * 4) + 1) * 0.5 : 0;
      // Aufbau beim Start: Ring für Ring
      const grow = (i) => this.bootT === null ? 1 : Math.max(0, Math.min(1, (this.bootT - i * 0.25) / 0.6));

      // Ton: Spektrum verformt die Ringe leicht, Lautstärke weitet sie
      const lvl = this.level;
      const wobble = (ring, a) => {
        if (!this.spectrum || lvl < 0.02) return 0;
        const n = this.spectrum.length;
        const v = this.spectrum[Math.floor(((a / TAU + ring * 0.13) % 1 + 1) % 1 * n)] || 0;
        return (v / 255) * lvl * R * 0.06;
      };
      const ring = (radius, width, alpha, index) => {
        const g = grow(index);
        if (g <= 0) return;
        ctx.beginPath();
        const steps = 120;
        for (let i = 0; i <= steps; i++) {
          const a = (i / steps) * TAU;
          const r = radius * g * (1 + breathe) + wobble(index, a);
          const x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r;
          i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        }
        ctx.strokeStyle = this._rgba(c, alpha * g);
        ctx.lineWidth = width;
        ctx.stroke();
      };

      const spread = 1 + lvl * 0.12;
      ring(R * 1.0 * spread, 1, 0.3 + pulse * 0.3, 2);
      ring(R * 0.68 * spread, 1.3, 0.55 + pulse * 0.2, 1);
      ring(R * 0.42 * (1 + lvl * 0.2), 1.6, 0.85, 0);

      // Denken: zwei Bögen kreisen zwischen den Ringen
      if (st === "thinking") {
        ctx.lineWidth = 2;
        ctx.lineCap = "round";
        for (const [radius, dir, len] of [[R * 0.84, 1, 0.9], [R * 0.55, -1.4, 0.7]]) {
          const a = this.rot * dir;
          ctx.beginPath();
          ctx.arc(cx, cy, radius, a, a + len);
          ctx.strokeStyle = this._rgba(c, 0.85);
          ctx.stroke();
        }
      }
      // Ausführen: gestrichelter Außenring dreht sich
      if (st === "executing") {
        ctx.save();
        ctx.setLineDash([4, 7]);
        ctx.lineDashOffset = -this.rot * 20;
        ctx.beginPath();
        ctx.arc(cx, cy, R * 1.12, 0, TAU);
        ctx.strokeStyle = this._rgba(c, 0.6);
        ctx.lineWidth = 1.2;
        ctx.stroke();
        ctx.restore();
      }

      // Kern: Punkt; Gedächtnis-Aktion → warmer Hof
      if (this.memLevel > 0.02) {
        ctx.beginPath();
        ctx.arc(cx, cy, R * 0.3, 0, TAU);
        ctx.fillStyle = this._rgba(this.colors["--warn"], 0.14 * this.memLevel);
        ctx.fill();
      }
      ctx.beginPath();
      ctx.arc(cx, cy, R * 0.17 * grow(0) * (1 + lvl * 0.25 + this.tokenEnergy * 0.006), 0, TAU);
      ctx.fillStyle = this._rgba(c, 1);
      ctx.fill();

      // Kontext-Auslastung: dünner Bogen außen, ab Mittag im Uhrzeigersinn
      if (this.ctxFrac !== null && R > 40) {
        const rr = R * 1.24, start = -Math.PI / 2;
        ctx.lineCap = "round";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(cx, cy, rr, 0, TAU);
        ctx.strokeStyle = this._rgba(this.colors["--border-strong"], 0.6);
        ctx.stroke();
        const full = this.ctxShow;
        const col = full > 0.9 ? this.colors["--danger"] : full > 0.75 ? this.colors["--warn"] : c;
        ctx.beginPath();
        ctx.arc(cx, cy, rr, start, start + TAU * Math.max(0.005, full));
        ctx.strokeStyle = this._rgba(col, 0.55 + this.ctxFlash * 0.45);
        ctx.stroke();
      }

      // Laufende Werkzeuge: kleine Punkte auf dem Außenring
      let i = 0;
      const n = this.satellites.size;
      for (const s of this.satellites.values()) {
        const a = this.rot * 0.6 + (i++ / Math.max(1, n)) * TAU;
        const x = cx + Math.cos(a) * R * spread, y = cy + Math.sin(a) * R * spread;
        const fade = s.until === null ? Math.min(1, (this.t - s.born) * 4) : Math.max(0, (s.until - this.t) / 0.4);
        ctx.beginPath();
        ctx.arc(x, y, Math.max(2.5, R * 0.05), 0, TAU);
        ctx.fillStyle = this._rgba(s.failed ? this.colors["--danger"] : c, Math.min(1, fade));
        ctx.fill();
      }
    }
  }

  window.Orb = Orb;
})();
