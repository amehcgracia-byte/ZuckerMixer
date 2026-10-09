/* Same downward-only microphone dynamics as process_track_streaming. */
class ZuckerVocalExpander extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const p = options.processorOptions || {};
    this.threshold = p.thresholdDb ?? -45;
    this.ratio = p.ratio ?? 2;
    this.knee = p.kneeDb ?? 5;
    this.minimum = p.maxAttenuationDb ?? -12;
    this.release = Math.exp(-1 / ((p.releaseSeconds ?? 0.5) * sampleRate));
    this.attack = Math.exp(-1 / ((p.attackSeconds ?? 0.005) * sampleRate));
    this.envelopes = [];
    this.gains = [];
  }

  process(inputs, outputs) {
    const input = inputs[0] || [];
    const output = outputs[0] || [];
    for (let ch = 0; ch < output.length; ch++) {
      const source = input[ch];
      const target = output[ch];
      let envelope = this.envelopes[ch] ?? 0;
      let gain = this.gains[ch] ?? 1;
      for (let i = 0; i < target.length; i++) {
        const x = source?.[i] || 0;
        envelope = this.release * envelope + (1 - this.release) * Math.abs(x);
        const below = Math.max(0, this.threshold - 20 * Math.log10(Math.max(envelope, 1e-12)));
        const knee = Math.min(1, below / this.knee);
        const gainDb = Math.max(this.minimum, -below * (1 - 1 / this.ratio) * knee);
        const wanted = 10 ** (gainDb / 20);
        gain = this.attack * gain + (1 - this.attack) * wanted;
        target[i] = x * gain;
      }
      this.envelopes[ch] = envelope;
      this.gains[ch] = gain;
    }
    return true;
  }
}
registerProcessor('zucker-vocal-expander', ZuckerVocalExpander);
