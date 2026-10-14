class Pcm16 extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.pos = 0;
    this.out = new Int16Array(512);
    this.n = 0;
    this.prev = 0;
  }

  process(inputs) {
    const ch = inputs[0]?.[0];
    if (!ch) return true;

    for (let i = 0; i < ch.length; i++) {
      const s = ch[i];
      while (this.pos <= 1) {
        // Linear interpolation between previous sample and current sample
        const v = this.prev + (s - this.prev) * this.pos;
        this.out[this.n++] = Math.max(-1, Math.min(1, v)) * 32767;
        if (this.n === 512) {
          this.port.postMessage(this.out.buffer.slice(0));
          this.n = 0;
        }
        this.pos += this.ratio;
      }
      this.pos -= 1;
      this.prev = s;
    }
    return true;
  }
}

registerProcessor("pcm16", Pcm16);
