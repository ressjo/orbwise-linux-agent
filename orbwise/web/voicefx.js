/* "Stimm-Effekt": Klangkette für die Sprachausgabe (Web Audio).
 * Etwas tiefere Stimme (über die Abspielrate – der Server spricht entsprechend schneller, damit das Tempo gleich bleibt),
 * sonore Bässe, ein Hauch Chorus und metallischer Kammfilter für den "KI"-Charakter, kurzer Raumhall, Kompressor. */
(function () {
  "use strict";

  function impulse(ctx, seconds, decay) {
    const len = Math.floor(ctx.sampleRate * seconds);
    const buf = ctx.createBuffer(2, len, ctx.sampleRate);
    for (let ch = 0; ch < 2; ch++) {
      const d = buf.getChannelData(ch);
      for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, decay);
    }
    return buf;
  }

  class VoiceFX {
    constructor(ctx, destination) {
      this.ctx = ctx;
      this.enabled = false;
      this.intensity = 0.6;
      this.input = ctx.createGain();

      this.highpass = ctx.createBiquadFilter();
      this.highpass.type = "highpass";
      this.highpass.frequency.value = 70;
      this.low = ctx.createBiquadFilter();
      this.low.type = "lowshelf";
      this.low.frequency.value = 180;
      this.presence = ctx.createBiquadFilter();
      this.presence.type = "peaking";
      this.presence.frequency.value = 2800;
      this.presence.Q.value = 0.9;
      this.air = ctx.createBiquadFilter();
      this.air.type = "highshelf";
      this.air.frequency.value = 7000;

      this.input.connect(this.highpass).connect(this.low).connect(this.presence).connect(this.air);

      this.comp = ctx.createDynamicsCompressor();
      this.comp.threshold.value = -20;
      this.comp.ratio.value = 3;
      this.comp.attack.value = 0.005;
      this.comp.release.value = 0.2;
      this.comp.connect(destination);

      this.dry = ctx.createGain();
      this.air.connect(this.dry).connect(this.comp);

      // Chorus: leicht modulierte Verzögerung
      this.chorusDelay = ctx.createDelay(0.05);
      this.chorusDelay.delayTime.value = 0.014;
      this.lfo = ctx.createOscillator();
      this.lfo.frequency.value = 0.3;
      this.lfoDepth = ctx.createGain();
      this.lfoDepth.gain.value = 0.0025;
      this.lfo.connect(this.lfoDepth).connect(this.chorusDelay.delayTime);
      this.lfo.start();
      this.chorus = ctx.createGain();
      this.air.connect(this.chorusDelay).connect(this.chorus).connect(this.comp);

      // Metallischer Kammfilter (kurze Verzögerung mit Rückkopplung)
      this.combDelay = ctx.createDelay(0.02);
      this.combDelay.delayTime.value = 0.0045;
      this.combFeedback = ctx.createGain();
      this.combFeedback.gain.value = 0.35;
      this.combDelay.connect(this.combFeedback).connect(this.combDelay);
      this.comb = ctx.createGain();
      this.air.connect(this.combDelay).connect(this.comb).connect(this.comp);

      // Raumhall
      this.convolver = ctx.createConvolver();
      this.convolver.buffer = impulse(ctx, 1.1, 3.2);
      this.reverb = ctx.createGain();
      this.air.connect(this.convolver).connect(this.reverb).connect(this.comp);

      this.apply();
    }

    set(enabled, intensity) {
      this.enabled = !!enabled;
      if (typeof intensity === "number") this.intensity = Math.max(0, Math.min(1, intensity));
      this.apply();
    }

    apply() {
      const k = this.enabled ? this.intensity : 0;
      const t = this.ctx.currentTime;
      const ramp = (param, value) => param.setTargetAtTime(value, t, 0.05);
      ramp(this.low.gain, 5 * k);
      ramp(this.presence.gain, 2 * k);
      ramp(this.air.gain, 3 * k);
      ramp(this.dry.gain, 1 - 0.15 * k);
      ramp(this.chorus.gain, 0.28 * k);
      ramp(this.comb.gain, 0.14 * k);
      ramp(this.reverb.gain, 0.22 * k);
    }

    /** Abspielrate für die "tiefere" Stimme (1 = unverändert). */
    get rate() {
      return this.enabled ? +(1 - 0.08 * this.intensity).toFixed(3) : 1;
    }

    /** Tonhöhe für die Browser-Sprachausgabe (Fallback ohne Piper). */
    get synthPitch() {
      return this.enabled ? 1 - 0.3 * this.intensity : 1;
    }
  }

  window.VoiceFX = VoiceFX;
})();
