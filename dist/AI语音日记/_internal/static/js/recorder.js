// 麦克风录音 → 16kHz 单声道 16bit WAV(语音识别接口要求的格式)
(function () {
  const TARGET_RATE = 16000;

  function downsample(chunks, total, inRate, outRate) {
    const merged = new Float32Array(total);
    let offset = 0;
    for (const c of chunks) { merged.set(c, offset); offset += c.length; }
    if (inRate === outRate) return merged;
    const ratio = inRate / outRate;
    const outLen = Math.floor(total / ratio);
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i++) {
      // 区间平均 = 简单低通, 避免直接抽点产生混叠
      const start = Math.floor(i * ratio);
      const end = Math.min(total, Math.floor((i + 1) * ratio));
      let sum = 0;
      for (let j = start; j < end; j++) sum += merged[j];
      out[i] = end > start ? sum / (end - start) : merged[start];
    }
    return out;
  }

  function encodeWav(samples, rate) {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const str = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
    str(0, 'RIFF'); view.setUint32(4, 36 + samples.length * 2, true);
    str(8, 'WAVE'); str(12, 'fmt ');
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true);
    view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    str(36, 'data'); view.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Blob([view], { type: 'audio/wav' });
  }

  class WavRecorder {
    async start(onLevel) {
      if (!navigator.mediaDevices || !window.AudioWorkletNode) {
        throw new Error('当前浏览器不支持录音, 请使用最新版 Chrome 或 Edge');
      }
      try {
        this.stream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
      } catch (e) {
        if (e.name === 'NotAllowedError') throw new Error('麦克风权限被拒绝, 请点击地址栏左侧的图标允许使用麦克风');
        if (e.name === 'NotFoundError') throw new Error('没有检测到麦克风');
        throw new Error('无法打开麦克风: ' + e.message);
      }
      this.ctx = new AudioContext();
      await this.ctx.audioWorklet.addModule('/static/js/pcm-worklet.js');
      this.source = this.ctx.createMediaStreamSource(this.stream);
      this.node = new AudioWorkletNode(this.ctx, 'pcm-capture');
      this.mute = this.ctx.createGain();
      this.mute.gain.value = 0;  // 接到输出保证节点被调度, 但不回放声音
      this.chunks = [];
      this.length = 0;
      let lastLevelAt = 0;
      this.node.port.onmessage = (e) => {
        const d = e.data;
        this.chunks.push(d);
        this.length += d.length;
        const now = performance.now();
        if (onLevel && now - lastLevelAt > 80) {
          lastLevelAt = now;
          let sum = 0;
          for (let i = 0; i < d.length; i++) sum += d[i] * d[i];
          onLevel(Math.min(1, Math.sqrt(sum / d.length) * 4));
        }
      };
      this.source.connect(this.node);
      this.node.connect(this.mute);
      this.mute.connect(this.ctx.destination);
    }

    get seconds() {
      return this.ctx ? this.length / this.ctx.sampleRate : 0;
    }

    async stop() {
      const rate = this.ctx.sampleRate;
      this.source.disconnect();
      this.node.disconnect();
      this.node.port.onmessage = null;
      this.stream.getTracks().forEach((t) => t.stop());
      await this.ctx.close();
      const samples = downsample(this.chunks, this.length, rate, TARGET_RATE);
      this.chunks = [];
      return { blob: encodeWav(samples, TARGET_RATE), seconds: samples.length / TARGET_RATE };
    }

    cancel() {
      try { this.stream && this.stream.getTracks().forEach((t) => t.stop()); } catch (_) {}
      try { this.ctx && this.ctx.close(); } catch (_) {}
      this.chunks = [];
    }
  }

  window.WavRecorder = WavRecorder;
})();
