"use strict";
const $ = id => document.getElementById(id);
const state = { voices: [], models: [], modelsByRole: {tts: [], asr: []}, settings: {}, chat: {}, artifacts: [], tasks: [], logs: [], pronunciations: [], current: "studio", lastArtifact: null, poll: null, settingsTab: "general" };
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = (n, digits=2) => Number(n || 0).toFixed(digits);
const formatBytes = n => n < 1024 ? `${n} B` : n < 1048576 ? `${fmt(n/1024,1)} KB` : n < 1073741824 ? `${fmt(n/1048576,1)} MB` : `${fmt(n/1073741824,2)} GB`;
const formatTime = iso => { if (!iso) return "—"; const d = new Date(iso); return d.toLocaleString("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}); };
const statusNames = {queued:"等待中",running:"运行中",succeeded:"已完成",failed:"失败",cancelled:"已取消",interrupted:"已中断"};
const kindNames = {custom:"预设音色",design:"文字设计",clone:"声音克隆"};
const examples = [
  "折腾了一周，最后留下的还是本地 Qwen3-TTS。官方权重，Apache-2.0，可以商用。",
  "大家好，欢迎来到我的频道。今天想跟你聊一个我折腾了整整一周才搞明白的事。",
  "夜深了，城市的灯一盏一盏亮起来。而故事，才刚刚开始。",
  "您猜怎么着？折腾了一礼拜，最后还真就让我给弄成了。",
  "我跟你说嘛，这个事情硬是巴适得很，一哈儿就搞定咯。",
];
let exampleIndex = 0;

async function api(path, options={}) {
  const config = {...options, headers: {...(options.headers || {})}};
  if (config.body && !(config.body instanceof FormData)) config.headers["Content-Type"] = "application/json";
  const response = await fetch(path, config);
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try { const data = await response.json(); message = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data); } catch (_) {}
    throw new Error(message);
  }
  return response.headers.get("content-type")?.includes("json") ? response.json() : response;
}
function toast(message, error=false) {
  const el = document.createElement("div"); el.className = `toast${error ? " error" : ""}`; el.textContent = message;
  $("toast-container").append(el); setTimeout(() => el.remove(), error ? 6500 : 3200);
}
function empty(message, icon="♪") { return `<div class="empty"><span class="empty-icon">${icon}</span><p>${esc(message)}</p></div>`; }
function setBusy(id, busy, busyText="处理中…") {
  const el = $(id); if (busy) { el.dataset.text = el.textContent; el.textContent = busyText; } else if (el.dataset.text) el.textContent = el.dataset.text;
  el.disabled = busy;
}

function navigate(view) {
  state.current = view;
  document.querySelectorAll(".view").forEach(el => el.classList.toggle("active", el.id === `view-${view}`));
  document.querySelectorAll("[data-view]").forEach(el => el.classList.toggle("active", el.dataset.view === view));
  $("breadcrumb").textContent = {studio:"语音合成",voices:"音色库",artifacts:"音频产物",tasks:"任务队列",logs:"运行日志",settings:"系统设置"}[view];
  history.replaceState(null, "", `#${view}${view === "settings" && state.settingsTab !== "general" ? "/" + state.settingsTab : ""}`);
  if (view === "voices") loadVoices();
  if (view === "artifacts") loadArtifacts();
  if (view === "tasks") loadTasks();
  if (view === "logs") loadLogs();
  if (view === "settings") { renderSettings(); loadModels(); loadChatSettings(); loadDownloads(); }
}
document.querySelectorAll("[data-view]").forEach(el => el.addEventListener("click", () => navigate(el.dataset.view)));
document.querySelectorAll("[data-nav]").forEach(el => el.addEventListener("click", () => navigate(el.dataset.nav)));

async function loadOverview() {
  try {
    const data = await api("/api/overview");
    $("memory").textContent = `${fmt(data.runtime.metal_active_gb)} GB`;
    $("task-badge").hidden = !(data.tasks.queued || data.tasks.running);
    $("task-badge").textContent = (data.tasks.queued || 0) + (data.tasks.running || 0);
    $("storage-note").textContent = `${data.artifacts} 个音频 · ${formatBytes(data.storage_bytes)} · 共 ${fmt(data.audio_seconds/60,1)} 分钟`;
    state.models = data.models;
    updateSelectedVoice();
  } catch (e) { console.error(e); }
}

async function loadVoices() {
  try {
    const params = new URLSearchParams({q: $("voice-search").value, kind: $("voice-kind").value, favorite: $("voice-favorite").checked});
    const data = await api(`/api/voices?${params}`);
    // Studio and dropdowns always show the unfiltered list; library honors filters.
    if (!$("voice-search").value && !$("voice-kind").value && !$("voice-favorite").checked) {
      state.voices = data.items; renderVoiceOptions();
    }
    $("voice-grid").innerHTML = data.items.length ? data.items.map(v => `<article class="voice-card">
      <button class="favorite ${v.is_favorite ? "active" : ""}" data-favorite="${esc(v.id)}" title="收藏">${v.is_favorite ? "★" : "☆"}</button>
      <div class="voice-card-top"><div class="avatar">${esc(v.name.slice(0,1))}</div><div><div class="voice-card-name">${esc(v.name)}${v.dialect ? '<span class="voice-tag">方言</span>' : ""}</div><div class="voice-card-sub">${esc(v.speaker || (v.kind === "design" ? "VoiceDesign" : "Voice Clone"))}</div></div></div>
      <div class="voice-card-desc">${esc(v.description || v.instruct || "暂无描述")}</div>
      ${voiceReadiness(v)}
      <div class="voice-card-foot"><span class="tag">${kindNames[v.kind] || v.kind}${v.kind === "clone" && v.reference_path ? " · 录音已保存" : ""}</span><div class="card-actions"><button class="text-button" data-use-voice="${esc(v.id)}">使用</button><button class="text-button" data-edit-voice="${esc(v.id)}">编辑</button></div></div>
    </article>`).join("") : empty("没有找到匹配的音色", "◉");
  } catch (e) { toast(e.message, true); }
}
function renderVoiceOptions() {
  const selected = $("synth-voice").value || state.settings.default_voice_id;
  const options = state.voices.map(v => `<option value="${esc(v.id)}">${esc(v.name)} · ${kindNames[v.kind]}${v.dialect ? " · 方言" : ""}</option>`).join("");
  $("synth-voice").innerHTML = options; $("artifact-voice").innerHTML = '<option value="">全部音色</option>' + options; $("setting-voice").innerHTML = options;
  $("synth-voice").value = state.voices.some(v => v.id === selected) ? selected : state.voices[0]?.id;
  $("setting-voice").value = state.settings.default_voice_id;
  const chatVoice=$("setting-chat-voice");
  if(chatVoice&&state.chat.voice_id)chatVoice.value=state.chat.voice_id;
  updateSelectedVoice();
}
function requiredModel(voice) {
  const key = {custom:"custom-0.6b-4bit",design:"design-1.7b-4bit",clone:"base-0.6b-4bit"}[voice?.kind];
  return state.modelsByRole.tts.find(m => m.key === key) || state.models.find(m => m.key === key);
}
function voiceReadiness(v) {
  if (v.kind === "custom") return "";
  const model = requiredModel(v);
  const saved = v.kind === "clone" ? (v.reference_path ? "录音已保存" : "待上传有效录音") : "描述已保存";
  const ready = model?.installed && (v.kind !== "clone" || v.reference_path);
  const next = !model?.installed ? "还需下载 " + (v.kind === "clone" ? "Base 克隆模型" : "VoiceDesign 模型") : v.kind === "clone" && !v.reference_path ? "请重新上传有效录音" : "模型已就绪，无需训练";
  return `<div class="voice-readiness ${ready ? "ready" : "pending"}">${saved} · ${next}</div>`;
}
function updateSelectedVoice() {
  const v = state.voices.find(v => v.id === $("synth-voice").value);
  const model = requiredModel(v);
  $("selected-voice-info").innerHTML = v ? `<strong>${esc(v.description || v.name)}</strong>${voiceReadiness(v)}` : "";
  $("synth-instruct").placeholder = v?.kind === "design" ? (v.instruct || "声音设计指令") : "例如：轻快一点，带些笑意";
  const missing = v && model && !model.installed;
  $("required-model-btn").hidden = !missing;
  if (missing) $("required-model-btn").dataset.modelKey = model.key;
  $("model-hint").textContent = !v ? "请选择发音人" : missing ?
    `${v.kind === "clone" ? "录音已保存（无需训练）" : "声音描述已保存"}；还需下载对应模型` :
    v.kind === "clone" && !v.reference_path ? "请编辑音色重新上传有效录音；Base 模型已就绪" :
    v.kind === "clone" ? "Base 模型与有效录音已就绪；合成时现场提取音色，无需训练" :
    v.kind === "design" ? "VoiceDesign 模型已就绪；按描述现场生成声音" : "CustomVoice 模型已就绪，首次合成自动加载";
}
$("synth-voice").addEventListener("change", updateSelectedVoice);
$("voice-search").addEventListener("input", () => { clearTimeout(state.searchTimer); state.searchTimer = setTimeout(loadVoices, 250); });
$("voice-kind").addEventListener("change", loadVoices); $("voice-favorite").addEventListener("change", loadVoices);
$("voice-grid").addEventListener("click", async e => {
  const use = e.target.closest("[data-use-voice]"), edit = e.target.closest("[data-edit-voice]"), favorite = e.target.closest("[data-favorite]");
  if (use) { navigate("studio"); $("synth-voice").value = use.dataset.useVoice; updateSelectedVoice(); $("synth-text").focus(); }
  if (edit) openVoiceModal(state.voices.find(v => v.id === edit.dataset.editVoice));
  if (favorite) { const v = state.voices.find(v => v.id === favorite.dataset.favorite); await api(`/api/voices/${v.id}`, {method:"PATCH",body:JSON.stringify({is_favorite:!v.is_favorite})}); loadVoices(); }
});

function updateCount() { $("char-count").textContent = $("synth-text").value.length; }
$("synth-text").addEventListener("input", updateCount);
$("clear-text").addEventListener("click", () => { $("synth-text").value = ""; updateCount(); $("synth-text").focus(); });
$("example-btn").addEventListener("click", () => { $("synth-text").value = examples[exampleIndex++ % examples.length]; updateCount(); });
$("temperature").addEventListener("input", () => $("temperature-value").textContent = $("temperature").value);
$("speed").addEventListener("input", () => {
  const v = Number($("speed").value);
  // Show 1× instead of 1.00×, but keep one decimal for non-integers.
  $("speed-value").textContent = `${Number.isInteger(v) ? v : v.toFixed(2).replace(/0$/, "")}×`;
});

function synthesisRequest() {
  const text = $("synth-text").value.trim();
  if (!text) throw new Error("请输入需要合成的文本");
  if (!$("synth-voice").value) throw new Error("请选择发音人");
  return {
    text, voice_id: $("synth-voice").value, instruct: $("synth-instruct").value.trim() || null,
    temperature: Number($("temperature").value), repetition_penalty: Number($("repetition").value),
    top_k: Number($("top-k").value), top_p: Number($("top-p").value),
    speed: Number($("speed").value),
  };
}
function validateAdvanced(req) {
  if (req.repetition_penalty < .8 || req.repetition_penalty > 2 || req.top_p <= 0 || req.top_p > 1 || req.top_k < 1 || req.top_k > 200) throw new Error("请检查高级参数的有效范围");
  if (req.speed < .5 || req.speed > 2) throw new Error("语速需在 0.5×–2.0× 之间");
}
async function synthesize() {
  let req;
  try { req = synthesisRequest(); validateAdvanced(req); } catch (e) { return toast(e.message, true); }
  setBusy("synth-btn", true, "正在合成…"); $("result-status").textContent = "正在生成，请稍候…";
  const started = performance.now();
  try {
    const response = await api("/api/tts", {method:"POST",body:JSON.stringify(req)});
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    state.lastArtifact = response.headers.get("X-Artifact-ID");
    $("result-empty").hidden = true; $("result-content").hidden = false; $("result-metrics").hidden = false;
    $("result-audio").src = url; $("result-audio").style.display = "block";
    $("result-title").textContent = req.text.slice(0, 70);
    $("result-download").href = `/api/artifacts/${state.lastArtifact}/download`;
    const gen = Number(response.headers.get("X-Gen-Seconds")), audio = Number(response.headers.get("X-Audio-Seconds"));
    $("metric-generation").textContent = `${fmt(gen)} s`; $("metric-audio").textContent = `${fmt(audio)} s`;
    $("metric-speed").textContent = `${fmt(audio/Math.max(gen,.01))}×`; $("metric-total").textContent = `${fmt((performance.now()-started)/1000)} s`;
    $("result-status").textContent = "合成完成 · 已保存至音频产物";
    if (state.settings.autoplay) $("result-audio").play().catch(() => {});
    loadOverview();
  } catch (e) { $("result-status").textContent = "合成失败"; toast(e.message, true); }
  finally { setBusy("synth-btn", false); }
}
$("synth-btn").addEventListener("click", synthesize);

// Streaming playback is driven by a small state machine so the user can pause
// and resume without losing the audio that has already arrived.
const streamPlayer = {
  context: null, reader: null, controller: null, cursor: 0,
  paused: false, active: false, done: false,
  reset() {
    this.paused = false; this.active = false; this.done = false;
    this.cursor = 0; this.reader = null; this.controller = null;
    if (this.context) { this.context.close().catch(() => {}); this.context = null; }
  },
  sync() {
    $("pause-btn").hidden = !this.active;
    $("stop-stream-btn").hidden = !this.active;
    $("pause-btn").textContent = this.paused ? "▶ 继续" : "❚❚ 暂停";
    $("stream-btn").disabled = this.active;
  },
  async pause() {
    if (!this.context || !this.active) return;
    this.paused = true;
    await this.context.suspend();
    $("result-status").textContent = "已暂停 · 音频继续在后台生成";
    this.sync();
  },
  async resume() {
    if (!this.context || !this.active) return;
    this.paused = false;
    // AudioContext.currentTime freezes while suspended, so scheduled buffers
    // keep their relative timing and simply continue from where they stopped.
    await this.context.resume();
    $("result-status").textContent = this.done ? "播放中 · 未保存产物" : "播放中 · 正在继续生成";
    this.sync();
  },
  stop() {
    const wasActive = this.active;
    this.controller?.abort();
    this.reader?.cancel().catch(() => {});
    this.reset();
    this.sync();
    if (wasActive) $("result-status").textContent = "已停止 · 未保存产物";
  },
};

$("stream-btn").addEventListener("click", async () => {
  let req;
  try { req = synthesisRequest(); validateAdvanced(req); } catch (e) { return toast(e.message, true); }
  streamPlayer.stop();
  streamPlayer.reset();
  setBusy("stream-btn", true, "连接中…");
  $("result-status").textContent = "流式生成中，首段音频返回后开始播放…";
  const controller = new AbortController();
  streamPlayer.controller = controller;
  try {
    const response = await api("/api/tts/stream", {
      method: "POST", body: JSON.stringify(req), signal: controller.signal,
    });
    // WAV is not a supported MSE container in Chrome. Decode the known PCM16
    // stream with Web Audio instead, scheduling chunks as they arrive.
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) throw new Error("此浏览器不支持流式播放，请使用合成音频");
    const context = new AudioContext({sampleRate: 24000});
    await context.resume();
    Object.assign(streamPlayer, {
      context, reader: response.body.getReader(),
      cursor: context.currentTime + .08, active: true, done: false, paused: false,
    });
    $("result-empty").hidden = true; $("result-content").hidden = false; $("result-metrics").hidden = true;
    $("result-audio").style.display = "none"; $("result-title").textContent = req.text.slice(0, 70);
    $("result-download").hidden = true;
    setBusy("stream-btn", false);
    streamPlayer.sync();
    $("result-status").textContent = "正在生成并播放 · 可随时暂停";

    let header = new Uint8Array(0), received = 0;
    while (true) {
      const {done, value} = await streamPlayer.reader.read();
      if (done) break;
      const joined = new Uint8Array(header.length + value.length);
      joined.set(header); joined.set(value, header.length);
      const start = received === 0 ? Math.min(44, joined.length) : 0;
      const length = joined.length - start - ((joined.length - start) % 2);
      if (length > 0) {
        const pcm = new Int16Array(joined.buffer, joined.byteOffset + start, length / 2);
        const floats = new Float32Array(pcm.length);
        for (let i = 0; i < pcm.length; i++) floats[i] = pcm[i] / 32768;
        const audioBuffer = context.createBuffer(1, floats.length, 24000);
        audioBuffer.copyToChannel(floats, 0);
        const source = context.createBufferSource();
        source.buffer = audioBuffer; source.connect(context.destination);
        const at = Math.max(streamPlayer.cursor, context.currentTime + .02);
        source.start(at);
        streamPlayer.cursor = at + audioBuffer.duration;
      }
      header = joined.slice(start + length); received += value.length;
    }
    streamPlayer.done = true;
    $("result-status").textContent = "流式生成完成，正在播放尾段 · 未保存产物";
    const remaining = Math.max(0, streamPlayer.cursor - context.currentTime);
    setTimeout(() => {
      if (streamPlayer.done) { streamPlayer.reset(); streamPlayer.sync(); }
    }, (remaining + .8) * 1000);
  } catch (e) {
    if (e.name === "AbortError") return; // user pressed stop
    $("result-status").textContent = "流式试听失败，可尝试合成音频";
    toast(e.message, true);
    streamPlayer.reset(); streamPlayer.sync();
  } finally {
    setBusy("stream-btn", false);
    $("result-download").hidden = false;
    streamPlayer.sync();
  }
});

$("pause-btn").addEventListener("click", () => {
  streamPlayer.paused ? streamPlayer.resume() : streamPlayer.pause();
});
$("stop-stream-btn").addEventListener("click", () => streamPlayer.stop());
