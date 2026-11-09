/* ------------------------------------------------------------------ audio */
/* One AudioContext for record and play. iOS only unlocks audio on a user
   gesture, so the context is resumed from the mic/tap handlers. */
const audio = {
  ctx: null,
  analyser: null,
  ensure() {
    if (!this.ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      this.ctx = new AC();
    }
    if (this.ctx.state === "suspended") this.ctx.resume();
    return this.ctx;
  },
};

/* ------------------------------------------------------------------- voice */
const voice = {
  stream: null,
  source: null,
  processor: null,
  chunks: [],
  samples: 0,
  startedAt: 0,
  silentSince: 0,
  active: false,
  canceling: false,
  timer: null,

  async start() {
    if (state.busy || this.active) return;
    if (!navigator.mediaDevices?.getUserMedia) {
      return toast("当前浏览器不支持麦克风，请用 Safari/Chrome 打开", true);
    }
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      return toast(err.name === "NotAllowedError" ? "麦克风权限被拒绝，请在浏览器设置里允许" : `无法打开麦克风：${err.message}`, true);
    }
    const ctx = audio.ensure();
    this.source = ctx.createMediaStreamSource(this.stream);
    this.analyser = ctx.createAnalyser();
    this.analyser.fftSize = 512;
    this.source.connect(this.analyser);
    // A ScriptProcessor keeps the raw samples for ASR and drives the level
    // meter; the analyser alone cannot give us PCM to upload.
    this.processor = ctx.createScriptProcessor(4096, 1, 1);
    this.chunks = [];
    this.samples = 0;
    this.silentSince = 0;
    const buffer = new Float32Array(4096);
    this.processor.onaudioprocess = event => {
      if (!this.active) return;
      const data = event.inputBuffer.getChannelData(0);
      buffer.set(data);
      this.chunks.push(new Float32Array(buffer));
      this.samples += buffer.length;
      const rms = Math.sqrt(buffer.reduce((sum, v) => sum + v * v, 0) / buffer.length);
      this.level(rms);
      const now = performance.now();
      if (rms > VAD_RMS) this.silentSince = 0;
      else if (!this.silentSince) this.silentSince = now;
      else if (now - this.silentSince > VAD_STOP_MS && now - this.startedAt > MIN_RECORD_MS) {
        this.stop(true); // natural pause: end the turn
      }
      if (now - this.startedAt > MAX_RECORD_MS) this.stop(true);
    };
    this.source.connect(this.processor);
    this.processor.connect(ctx.destination);

    this.active = true;
    this.canceling = false;
    this.startedAt = performance.now();
    state.busy = true;
    document.body.classList.add("busy");
    setOrb("listening", "我在听…", "停顿一下就会自动发送");
    $("mic-btn").classList.add("recording");
    $("rec-bar").hidden = false;
    $("rec-text").textContent = "正在听…";
  },

  level(rms) {
    const normalized = Math.min(1, rms / 0.14);
    window.setOrbLevel?.(normalized);
    if (rms <= VAD_RMS) return;
    $("rec-text").textContent = "正在听…";
  },

  async stop(send) {
    if (!this.active) return;
    this.active = false;
    $("mic-btn").classList.remove("recording");
    this.processor?.disconnect();
    this.source?.disconnect();
    this.stream?.getTracks().forEach(track => track.stop());
    this.processor = this.source = this.stream = null;
    window.setOrbLevel?.(0);
    const duration = performance.now() - this.startedAt;
    const pcm = duration >= MIN_RECORD_MS ? this.encode() : null;
    this.chunks = [];
    $("rec-bar").hidden = true;
    if (!send || !pcm) {
      state.busy = false;
      document.body.classList.remove("busy");
      idleOrb();
      syncOrbCursor();
      if (send && !pcm) toast("说话太短了，再说一次");
      return;
    }
    await sendVoice(pcm);
  },

  /* Whatever the browser gives us (usually 48 kHz) is downsampled to 16 kHz
     mono 16-bit WAV, which is what every ASR backend here expects. */
  encode() {
    const rate = audio.ctx.sampleRate;
    const total = this.samples;
    const merged = new Float32Array(total);
    let offset = 0;
    for (const chunk of this.chunks) { merged.set(chunk, offset); offset += chunk.length; }
    const ratio = rate / STT_RATE;
    const outLength = Math.floor(total / ratio);
    const out = new Int16Array(outLength);
    for (let i = 0; i < outLength; i++) {
      // Average the window instead of picking one sample: cheap anti-aliasing.
      const start = Math.floor(i * ratio);
      const end = Math.min(total, Math.floor((i + 1) * ratio));
      let sum = 0;
      for (let j = start; j < end; j++) sum += merged[j];
      const value = sum / Math.max(1, end - start);
      out[i] = Math.max(-1, Math.min(1, value)) * 32767;
    }
    return wavBlob(out, STT_RATE);
  },
};

function wavBlob(samples, rate) {
  const header = new ArrayBuffer(44);
  const view = new DataView(header);
  const write = (offset, text) => [...text].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
  write(0, "RIFF"); view.setUint32(4, 36 + samples.byteLength, true); write(8, "WAVE");
  write(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  write(36, "data"); view.setUint32(40, samples.byteLength, true);
  return new Blob([header, samples.buffer], { type: "audio/wav" });
}

/* ----------------------------------------------------------------- playback */
/* Chunks arrive faster than real time, so they are scheduled back to back on
   one timeline. A `pause` event inserts the sentence's own breath. */
const player = {
  ctx: null,
  cursor: 0,
  sources: new Set(),
  // 播放代数：stop() 时 +1。异步拿到的音频回家后先比对代数，
  // 不一致说明用户已经切走/停掉了，直接丢弃，避免「切换对话还在播」。
  generation: 0,
  pending: 0,

  ensure() {
    if (!this.ctx) this.ctx = audio.ensure();
    if (this.ctx.state === "suspended") this.ctx.resume();
    return this.ctx;
  },

  play(pcmBase64, rate, gapSeconds = 0) {
    const ctx = this.ensure();
    const raw = atob(pcmBase64);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    const pcm = new Int16Array(bytes.buffer, 0, Math.floor(bytes.byteLength / 2));
    const floats = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) floats[i] = pcm[i] / 32768;
    const buffer = ctx.createBuffer(1, floats.length, rate || AUDIO_RATE);
    buffer.copyToChannel(floats, 0);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    const startAt = Math.max(this.cursor, ctx.currentTime + 0.04);
    source.start(startAt);
    this.cursor = startAt + buffer.duration + gapSeconds;
    this.sources.add(source);
    this.pending++;
    setOrb("speaking", "正在说…", "再按一下可以打断我");
    source.onended = () => {
      this.sources.delete(source);
      this.pending--;
      if (!this.pending) this.idleSoon();
    };
  },

  /* 整段 PCM 播放（重新朗读用）。token 在发起请求时记录，
     回来时代数变了就说明这次播放已经作废。 */
  playWhole(float32, rate, token) {
    if (token !== this.generation) return false;
    const ctx = this.ensure();
    const buffer = ctx.createBuffer(1, float32.length, rate);
    buffer.copyToChannel(float32, 0);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    const startAt = Math.max(this.cursor, ctx.currentTime + 0.04);
    source.start(startAt);
    this.cursor = startAt + buffer.duration;
    this.sources.add(source);
    this.pending++;
    setOrb("speaking", "正在朗读…", "");
    source.onended = () => {
      this.sources.delete(source);
      this.pending--;
      if (!this.pending) this.idleSoon();
    };
    return true;
  },

  gap(seconds) { this.cursor += Math.max(0, seconds || 0); },

  idleSoon() {
    window.setTimeout(() => {
      if (this.pending || state.busy) return;
      state.speaking = false;
      idleOrb();
      $("stop-btn").hidden = true;
    }, 120);
  },

  stop() {
    this.generation += 1;
    for (const source of this.sources) {
      try { source.stop(); } catch (_) { /* already stopped */ }
    }
    this.sources.clear();
    this.pending = 0;
    this.cursor = 0;
    state.speaking = false;
    $("stop-btn").hidden = true;
    idleOrb();
  },
};
