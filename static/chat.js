"use strict";
/* Voice chat page: record -> ASR -> streaming LLM -> sentence TTS, all over one
   SSE request per turn. The reply is spoken while it is still being written:
   every `sentence` event queues a TTS job, every `audio` event is PCM that is
   scheduled on a Web Audio timeline, so playback never waits for the end. */

const $ = id => document.getElementById(id);
const AUDIO_RATE = 24000;         // TTS output rate, confirmed by the server
const STT_RATE = 16000;           // what we upload for recognition
const VAD_RMS = 0.012;            // below this is treated as silence
const VAD_STOP_MS = 1200;         // pause that ends a turn
const MIN_RECORD_MS = 600;        // shorter presses are accidental
const MAX_RECORD_MS = 60000;

const state = {
  settings: null,
  audioRate: AUDIO_RATE,
  sessions: [],
  sessionId: null,
  messages: [],
  busy: false,
  abort: null,
  speaking: false,
};

const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = iso => {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const today = new Date();
  const same = d.toDateString() === today.toDateString();
  return same ? d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false })
              : d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
};

async function api(path, options = {}) {
  const config = { ...options, headers: { ...(options.headers || {}) } };
  if (config.body && !(config.body instanceof FormData)) config.headers["Content-Type"] = "application/json";
  const res = await fetch(path, config);
  if (!res.ok) {
    let message = `请求失败 (${res.status})`;
    try {
      const data = await res.json();
      message = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data);
    } catch (_) { /* non-json error body */ }
    throw new Error(message);
  }
  return res.json();
}

function toast(message, error = false) {
  const el = document.createElement("div");
  el.className = `toast${error ? " error" : ""}`;
  el.textContent = message;
  $("toast-container").append(el);
  setTimeout(() => el.remove(), error ? 5200 : 2600);
}

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

/* ------------------------------------------------------------------ orb */
let levelRaf = null;
let baseLevel = 0;
// Seven bars on a bell curve: taller in the middle, short at the edges. This
// reads as a sound wave at rest instead of scattered dots.
const BAR_SHAPE = [.3, .52, .78, 1, .78, .52, .3];
function buildBars() {
  $("bars").innerHTML = BAR_SHAPE.map((height, i) =>
    `<i style="--h:${height};animation-delay:${(i * 0.09).toFixed(2)}s"></i>`).join("");
}

window.setOrbLevel = value => {
  baseLevel = value;
  if (levelRaf) return;
  const tick = () => {
    const orb = $("orb");
    const bars = orb.querySelectorAll(".bars i");
    if (baseLevel <= 0.01) {
      // Back to rest: drop the inline styles so the CSS idle pose applies.
      bars.forEach(bar => bar.style.transform = "");
      orb.style.transform = "";
      levelRaf = null;
      return;
    }
    const boost = 1 + baseLevel * 1.6;
    bars.forEach((bar, i) => {
      const shape = BAR_SHAPE[i] ?? .5;
      const phase = i * 0.62 + performance.now() / 145;
      const scale = (.22 + Math.abs(Math.sin(phase)) * .78) * boost * (.55 + shape * .45);
      bar.style.transform = `scaleY(${Math.min(1.5, scale).toFixed(3)})`;
    });
    orb.style.transform = `scale(${1 + baseLevel * 0.06})`;
    levelRaf = requestAnimationFrame(tick);
  };
  if (!levelRaf) tick();
};

function setOrb(mode, title, subtitle) {
  const orb = $("orb");
  orb.className = `orb ${mode}`;
  $("hero").classList.toggle("can-talk", mode === "idle" || mode === "listening");
  if (title) $("hero-title").textContent = title;
  if (subtitle) $("hero-sub").textContent = subtitle;
}
function syncOrbCursor() {
  $("hero").classList.toggle("can-talk", $("orb").classList.contains("idle"));
}

function idleOrb() {
  baseLevel = 0;
  $("orb").style.transform = "";
  $("orb").querySelectorAll(".bars i").forEach(bar => bar.style.transform = "");
  setOrb("idle", state.messages.length ? "" : "按一下，开始说话",
    state.messages.length ? "" : "我会听你说完，然后用语音回答你");
}

/* ----------------------------------------------------------------- thread */
function renderThread() {
  $("thread").innerHTML = state.messages.map(message => messageHtml(message)).join("");
  $("hero").classList.toggle("small", state.messages.length > 0);
  scrollThread();
}

function messageHtml(message) {
  const role = message.role;
  const avatar = role === "user" ? "我" : "e";
  const asr = role === "user" && message.input_mode === "voice"
    ? `<span class="tag-asr">语音识别${message.asr_elapsed ? ` · ${Number(message.asr_elapsed).toFixed(2)}s` : ""}</span>` : "";
  const meta = role === "assistant"
    ? `<div class="msg-meta"><span>${esc(message.llm_model || "")}${message.elapsed ? ` · ${Number(message.elapsed).toFixed(1)}s` : ""}</span>
        <button data-speak="${esc(message.id)}">重新朗读</button><button data-copy="${esc(message.id)}">复制</button></div>`
    : `<div class="msg-meta">${asr}<span>${fmtTime(message.created_at)}</span></div>`;
  return `<div class="msg ${role}">
    <div class="avatar">${avatar}</div>
    <div class="bubble">${esc(message.content)}${message.error ? `<div class="msg-error">${esc(message.error)}</div>` : ""}</div>
  </div>${meta}`;
}

function appendLive(role, text = "") {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.dataset.live = "1";
  el.innerHTML = `<div class="avatar">${role === "user" ? "我" : "e"}</div>
    <div class="bubble">${esc(text)}<span class="caret"></span></div>`;
  $("thread").append(el);
  $("hero").classList.add("small");
  scrollThread();
  return el;
}
function liveBubble(el, text) {
  el.querySelector(".bubble").innerHTML = `${esc(text)}<span class="caret"></span>`;
  scrollThread();
}
function scrollThread() {
  const thread = $("thread");
  thread.scrollTop = thread.scrollHeight;
}

async function loadSessions() {
  try {
    const data = await api("/api/chat/sessions");
    state.sessions = data.items || [];
    renderSessions();
  } catch (err) { console.debug(err); }
}

function renderSessions() {
  $("session-list").innerHTML = state.sessions.length
    ? state.sessions.map(session => `<div class="rail-row">
        <button class="rail-item${session.id === state.sessionId ? " active" : ""}" data-session="${esc(session.id)}">
          <span class="rail-title">${esc(session.title)}</span>
          <span class="rail-preview">${esc((session.preview || "空白对话").slice(0, 30))}${session.message_count ? ` · ${session.message_count}` : ""}</span>
        </button>
        <button class="rail-del" data-del-session="${esc(session.id)}" title="删除对话" aria-label="删除对话">×</button>
      </div>`).join("")
    : `<div class="rail-empty">还没有对话，按一下麦克风开始吧</div>`;
}

async function openSession(sessionId) {
  if (state.busy) return toast("等这一轮说完再切换对话", true);
  player.stop();
  try {
    const data = await api(`/api/chat/sessions/${sessionId}`);
    state.sessionId = data.id;
    state.messages = data.messages || [];
    $("session-title").textContent = data.title;
    renderThread();
    renderSessions();
    closeRail();
    setOrb("idle");
    idleOrb();
  } catch (err) { toast(err.message, true); }
}

function newSession() {
  if (state.busy) return toast("等这一轮说完再新建对话", true);
  player.stop();
  state.sessionId = null;
  state.messages = [];
  $("session-title").textContent = "新的对话";
  renderThread();
  renderSessions();
  closeRail();
  idleOrb();
}

/* ------------------------------------------------------------------- SSE */
async function streamTurn(form) {
  state.abort = new AbortController();
  const response = await fetch("/api/chat/turn", {
    method: "POST", body: form, signal: state.abort.signal,
  });
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";
    for (const frame of frames) handleFrame(frame);
  }
  if (buffer.trim()) handleFrame(buffer);
}

function handleFrame(frame) {
  let event = "message";
  const dataLines = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return;
  let data;
  try { data = JSON.parse(dataLines.join("\n")); } catch (_) { return; }
  handlers[event]?.(data);
}

const handlers = {
  status(data) {
    state.speaking = true;
    const copy = {
      asr: ["正在识别你的话…", "马上就好", "正在识别…"],
      thinking: ["我在想…", "马上就说", "正在思考…"],
      tts: ["正在准备声音…", "模型首次使用要加载，之后会快很多", "正在合成语音…"],
    }[data.stage] || ["稍等一下…", "", "处理中…"];
    setOrb("thinking", copy[0], copy[1]);
    $("typing").hidden = false;
    $("typing-label").textContent = copy[2];
    startWaitTimer(copy[2]);
  },
  asr(data) {
    if (!data.text) return;
    const live = appendLive("user", data.text);
    live.querySelector(".bubble").dataset.asr = "1";
  },
  user(message) {
    // Replace the live transcript with the saved message so ids match.
    document.querySelectorAll("[data-live]").forEach(el => el.remove());
    state.liveEl = null;
    state.liveAssistant = "";
    state.messages.push(message);
    if (!state.sessionId) { state.sessionId = message.session_id; }
    renderThread();
  },
  sentence(data) {
    $("typing-label").textContent = `第 ${data.index + 1} 句`;
    if (!state.liveAssistant) state.liveAssistant = "";
    state.liveAssistant += data.text;
    if (!state.liveEl) state.liveEl = appendLive("assistant", "");
    liveBubble(state.liveEl, state.liveAssistant);
    $("stop-btn").hidden = false;
  },
  audio(data) {
    stopWaitTimer();
    $("typing").hidden = true;
    player.play(data.pcm, data.rate || state.settings?.audio_rate || AUDIO_RATE);
  },
  pause(data) { player.gap(data.seconds); },
  first_audio(data) {
    $("stage-sub").textContent = `首句 ${(data.ms / 1000).toFixed(2)}s`;
  },
  warn(data) { toast(data.message, true); },
  error(data) {
    $("typing").hidden = true;
    finishLive();
    toast(data.message, true);
    if (data.action === "open_settings") {
      showSetupNotice("还没有配置对话模型", data.message, "/#settings/model", "去模型设置 →");
    } else if (data.action === "download_model") {
      showSetupNotice(
        "缺少模型", data.message,
        data.model_key ? `/?download=${encodeURIComponent(data.model_key)}` : "/", "去下载 →");
    }
  },
  done(data) {
    $("typing").hidden = true;
    if (data.empty) { finishLive(); return; }
    const live = state.liveEl;
    finishLive();
    if (data.message && typeof data.message === "object") {
      state.messages.push(data.message);
      live?.remove();
      state.sessionId = data.message.session_id || state.sessionId;
      renderThread();
    }
    // The rail refresh is independent of the turn finishing; do not make the
    // caller wait on it or the next turn could start mid-render.
    loadSessions().then(() => {
      const current = state.sessions.find(s => s.id === state.sessionId);
      if (current) $("session-title").textContent = current.title;
    });
  },
};

/* 等待计时：超过 3 秒就把秒数显示出来，让用户知道没卡死。 */
let waitTimer = null, waitStart = 0;
function startWaitTimer(label) {
  stopWaitTimer();
  waitStart = performance.now();
  waitTimer = setInterval(() => {
    const secs = (performance.now() - waitStart) / 1000;
    if (secs < 3) return;
    $("typing-label").textContent = `${label.replace(/…$/, "")} · ${secs.toFixed(0)}s`;
  }, 500);
}
function stopWaitTimer() {
  if (waitTimer) clearInterval(waitTimer);
  waitTimer = null;
}

function finishLive() {
  if (state.liveEl) {
    const caret = state.liveEl.querySelector(".caret");
    caret?.remove();
  }
  state.liveEl = null;
  state.liveAssistant = "";
}

/* ------------------------------------------------------------------- turns */
async function sendVoice(pcm) {
  state.busy = true;
  document.body.classList.add("busy");
  state.liveEl = null;
  state.liveAssistant = "";
  player.stop();
  player.ensure();
  const form = new FormData();
  if (state.sessionId) form.append("session_id", state.sessionId);
  form.append("audio", pcm, "turn.wav");
  try {
    await streamTurn(form);
  } catch (err) {
    if (err.name !== "AbortError") toast(err.message, true);
    finishLive();
  } finally {
    state.busy = false;
    document.body.classList.remove("busy");
    $("typing").hidden = true;
    idleOrb();
    syncOrbCursor();
    player.idleSoon();
  }
}

async function sendText() {
  const text = $("text-input").value.trim();
  if (!text) return;
  if (state.busy) return toast("等这一轮说完再说", true);
  $("text-input").value = "";
  state.busy = true;
  document.body.classList.add("busy");
  state.liveEl = null;
  state.liveAssistant = "";
  player.stop();
  player.ensure();
  const form = new FormData();
  if (state.sessionId) form.append("session_id", state.sessionId);
  form.append("transcript", text);
  try {
    await streamTurn(form);
  } catch (err) {
    if (err.name !== "AbortError") toast(err.message, true);
    finishLive();
  } finally {
    state.busy = false;
    document.body.classList.remove("busy");
    $("typing").hidden = true;
    idleOrb();
    syncOrbCursor();
    player.idleSoon();
  }
}

function interrupt() {
  state.abort?.abort();
  player.stop();
  state.busy = false;
  document.body.classList.remove("busy");
  $("typing").hidden = true;
  finishLive();
  idleOrb();
  syncOrbCursor();
}

/* ------------------------------------------------------------------ wiring */
function bindMic(button) {
  // Press and hold on desktop, tap to start / tap to send on phones (a long
  // press conflicts with the browser's own gestures on iOS Safari).
  const touch = matchMedia("(pointer: coarse)").matches;
  if (touch) {
    button.addEventListener("click", () => (voice.active ? voice.stop(true) : voice.start()));
    return;
  }
  button.addEventListener("mousedown", event => { event.preventDefault(); voice.start(); });
  button.addEventListener("mouseup", () => voice.stop(true));
  button.addEventListener("mouseleave", () => voice.active && voice.stop(true));
  button.addEventListener("touchstart", event => { event.preventDefault(); voice.start(); }, { passive: false });
  button.addEventListener("touchend", event => { event.preventDefault(); voice.stop(true); }, { passive: false });
  button.addEventListener("keydown", event => {
    if (event.key === " " || event.key === "Enter") { event.preventDefault(); voice.active ? voice.stop(true) : voice.start(); }
  });
}

function showSetupNotice(title, text, href, label) {
  $("setup-title").textContent = title;
  $("setup-text").textContent = text;
  $("setup-action").href = href;
  $("setup-action").textContent = label;
  $("setup-notice").hidden = false;
}

function checkSetup() {
  const settings = state.settings;
  if (!settings) return;
  const configured = settings.configured;
  if (!configured) {
    showSetupNotice("还没有配置对话模型", "语音对话需要一个模型来回答。先去设置页填好模型地址和 API Key，回来就能直接说话。",
      "/#settings/model", "去模型设置 →");
  } else if (state.configWarning) {
    // 配置看起来是占位值：直接说清楚，别等用户去翻日志里的 nodename 报错。
    showSetupNotice("模型地址可能填错了", state.configWarning, "/#settings/model", "去修改 →");
  } else {
    $("setup-notice").hidden = true;
    const asr = state.models.asr.find(model => model.key === settings.asr_model);
    if (asr && !asr.installed) {
      showSetupNotice("语音识别模型还没有下载", `需要 ${asr.name} 才能把你说的话变成文字。`,
        `/?download=${encodeURIComponent(asr.key)}`, "去下载 →");
    }
  }
  updateModelChip();
}

function updateModelChip() {
  const chip = $("model-chip");
  const configured = state.settings?.configured;
  chip.textContent = configured ? (state.settings.llm_model || "已配置") : "未配置模型";
  chip.classList.toggle("warn", !configured);
}

function openRail() { $("rail").classList.add("open"); $("rail-mask").hidden = false; }
function closeRail() { $("rail").classList.remove("open"); $("rail-mask").hidden = true; }

async function bootstrap() {
  buildBars();
  const data = await api("/api/chat/bootstrap");
  state.settings = data.settings;
  state.configWarning = data.config_warning || "";
  state.models = data.models;
  state.sessions = data.sessions || [];
  state.audioRate = data.audio_rate || AUDIO_RATE;
  renderSessions();
  state.messages = [];
  renderThread();
  idleOrb();
  syncOrbCursor();
  checkSetup();
  if (state.settings?.configured) {
    // 后台预热，失败也无所谓；成功了第一次回复就不用等模型加载。
    fetch("/api/chat/warmup", { method: "POST" }).catch(() => {});
  }
  if (state.sessions.length) await openSession(state.sessions[0].id);
}

function bindUi() {
  bindMic($("mic-btn"));
  // The orb mirrors the mic: on desktop it offers the same press-to-talk
  // affordance, and it is the only obvious target on a large screen.
  $("orb").addEventListener("click", () => {
    if (state.busy) return;
    voice.active ? voice.stop(true) : voice.start();
  });
  $("text-input").addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.isComposing) {
      event.preventDefault();
      sendText();
    }
  });
  $("new-chat").addEventListener("click", newSession);
  $("menu-btn").addEventListener("click", openRail);
  $("rail-close").addEventListener("click", closeRail);
  $("rail-mask").addEventListener("click", closeRail);
  $("stop-btn").addEventListener("click", interrupt);
  $("rec-cancel").addEventListener("click", () => voice.stop(false));
  $("rec-send").addEventListener("click", () => voice.stop(true));
  $("theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("ethan-tts-theme", next);
  });

  $("session-list").addEventListener("click", async event => {
    const del = event.target.closest("[data-del-session]");
    if (del) {
      event.stopPropagation();
      if (!confirm("删除这段对话？")) return;
      try {
        await api(`/api/chat/sessions/${del.dataset.delSession}`, { method: "DELETE" });
        if (del.dataset.delSession === state.sessionId) newSession();
        await loadSessions();
        toast("对话已删除");
      } catch (err) { toast(err.message, true); }
      return;
    }
    const item = event.target.closest("[data-session]");
    if (item) openSession(item.dataset.session);
  });

  $("thread").addEventListener("click", async event => {
    const speak = event.target.closest("[data-speak]");
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      const message = state.messages.find(m => m.id === copy.dataset.copy);
      if (message) {
        try { await navigator.clipboard.writeText(message.content); toast("已复制"); }
        catch (_) { toast("复制失败", true); }
      }
    }
    if (speak) {
      const message = state.messages.find(m => m.id === speak.dataset.speak);
      if (!message) return;
      player.stop();
      speak.textContent = "朗读中…";
      try {
        const res = await fetch("/api/chat/speak", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: message.content.slice(0, 2000),
                                 voice_id: state.settings.voice_id }),
        });
        if (!res.ok) throw new Error((await res.json()).detail || "朗读失败");
        const blob = await res.blob();
        const buffer = await blob.arrayBuffer();
        const pcm = new Int16Array(buffer, 44, Math.floor((buffer.byteLength - 44) / 2));
        const floats = new Float32Array(pcm.length);
        for (let i = 0; i < pcm.length; i++) floats[i] = pcm[i] / 32768;
        const ctx = player.ensure();
        const audioBuffer = ctx.createBuffer(1, floats.length, state.audioRate);
        audioBuffer.copyToChannel(floats, 0);
        const source = ctx.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(ctx.destination);
        source.start();
        player.sources.add(source);
        player.pending++;
        setOrb("speaking", "正在朗读…", "");
        source.onended = () => { player.sources.delete(source); player.pending--; if (!player.pending) player.idleSoon(); };
      } catch (err) { toast(err.message, true); }
      finally { speak.textContent = "重新朗读"; }
    }
  });
}

function applyTheme() {
  const theme = localStorage.getItem("ethan-tts-theme")
    || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.dataset.theme = theme;
}

applyTheme();
bindUi();
bootstrap().catch(err => toast(`初始化失败：${err.message}`, true));
