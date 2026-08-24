// マイク入力の生 PCM(Float32) を 2048 サンプルごとにメインスレッドへ送る
class PCMTap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunks = [];
    this.total = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) {
      this.chunks.push(new Float32Array(ch));
      this.total += ch.length;
      if (this.total >= 2048) {
        const all = new Float32Array(this.total);
        let o = 0;
        for (const c of this.chunks) { all.set(c, o); o += c.length; }
        this.port.postMessage(all, [all.buffer]);
        this.chunks = [];
        this.total = 0;
      }
    }
    return true;
  }
}
registerProcessor("pcm-tap", PCMTap);
