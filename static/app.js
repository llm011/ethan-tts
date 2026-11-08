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
  if (view === "settings") { renderSettings(); loadModels(); loadChatSettings(); }
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

async function loadArtifacts() {
  try {
    const params = new URLSearchParams({q:$("artifact-search").value,voice_id:$("artifact-voice").value});
    const data = await api(`/api/artifacts?${params}`); state.artifacts = data.items;
    $("artifact-total").textContent = `共 ${data.total} 个`;
    $("artifact-list").innerHTML = data.items.length ? data.items.map(a => `<div class="artifact-row table-row">
      <div><div class="artifact-title">${esc(a.title)}</div><div class="artifact-text">${esc(a.text)}</div><div class="artifact-id">WAV · ${a.sample_rate/1000}kHz · ${formatBytes(a.size_bytes)}</div></div>
      <div class="table-cell">${esc(a.voice_name)}</div><div class="table-cell"><b>${fmt(a.audio_seconds,1)}s</b><span class="table-sub">生成 ${fmt(a.generation_seconds)}s</span></div>
      <div class="table-cell">${formatTime(a.created_at)}</div><div class="row-actions"><button data-play-artifact="${a.id}">试听</button><a href="/api/artifacts/${a.id}/download">下载</a><button class="danger" data-delete-artifact="${a.id}">删除</button></div>
      <audio class="inline-audio" id="audio-${a.id}" controls preload="none" src="/api/artifacts/${a.id}/audio" hidden></audio></div>`).join("") : empty("还没有音频产物，去合成第一段声音吧");
  } catch(e) { toast(e.message,true); }
}
let artifactSearchTimer;
$("artifact-search").addEventListener("input", () => { clearTimeout(artifactSearchTimer); artifactSearchTimer=setTimeout(loadArtifacts,250); });
$("artifact-voice").addEventListener("change", loadArtifacts);
$("artifact-list").addEventListener("click", async e => {
  const play=e.target.closest("[data-play-artifact]"), del=e.target.closest("[data-delete-artifact]");
  if(play) { const audio=$(`audio-${play.dataset.playArtifact}`); audio.hidden=!audio.hidden; if(!audio.hidden)audio.play().catch(()=>{});else audio.pause(); }
  if(del && confirm("确定删除这个音频文件吗？此操作不可撤销。")) { try{await api(`/api/artifacts/${del.dataset.deleteArtifact}`,{method:"DELETE"});toast("音频已删除");loadArtifacts();loadOverview();}catch(e){toast(e.message,true);} }
});

async function loadTasks() {
  try {
    const data=await api(`/api/tasks?status=${encodeURIComponent($("task-status").value)}`); state.tasks=data.items;
    $("task-total").textContent=`共 ${data.total} 项`;
    $("task-list").innerHTML=data.items.length?data.items.map(t=>{const v=state.voices.find(v=>v.id===t.voice_id);return `<div class="task-row table-row"><div><div class="artifact-title">${esc(t.text.slice(0,65))}</div><div class="artifact-id">${esc(t.id)}${t.duration_seconds?` · ${fmt(t.duration_seconds)}s`:""}</div>${t.error?`<div class="error-detail">${esc(t.error)}</div>`:""}</div><div><span class="status ${t.status}">${statusNames[t.status]||t.status}</span></div><div class="table-cell">${esc(v?.name||t.voice_id)}<span class="table-sub">${esc(t.model_key)}</span></div><div class="table-cell">${formatTime(t.created_at)}</div><div class="row-actions">${["queued","running"].includes(t.status)?`<button data-cancel-task="${t.id}">取消</button>`:""}${["failed","cancelled","interrupted"].includes(t.status)?`<button data-retry-task="${t.id}">重试</button>`:""}${t.artifact_id?`<button data-open-artifact="${t.artifact_id}">产物</button>`:""}</div></div>`;}).join(""):empty("暂无任务记录","☷");
  }catch(e){toast(e.message,true);}
}
$("task-status").addEventListener("change",loadTasks);$("refresh-tasks").addEventListener("click",loadTasks);
$("task-list").addEventListener("click",async e=>{const cancel=e.target.closest("[data-cancel-task]"),retry=e.target.closest("[data-retry-task]"),open=e.target.closest("[data-open-artifact]");
  try{if(cancel){await api(`/api/tasks/${cancel.dataset.cancelTask}/cancel`,{method:"POST"});toast("已请求取消任务");loadTasks();}
  if(retry){await api(`/api/tasks/${retry.dataset.retryTask}/retry`,{method:"POST"});toast("已加入重试队列");loadTasks();}
  if(open)navigate("artifacts");}catch(e){toast(e.message,true);}
});

async function loadLogs(){try{const p=new URLSearchParams({level:$("log-level").value,category:$("log-category").value,q:$("log-search").value});const data=await api(`/api/logs?${p}`);state.logs=data.items;$("log-list").innerHTML=data.items.length?data.items.map(l=>`<div class="log-row"><span class="log-time">${formatTime(l.created_at)}</span><span class="log-level ${l.level}">${l.level}</span><span class="log-category">${esc(l.category)}</span><div class="log-message">${esc(l.message)}${l.task_id?`<small>task: ${esc(l.task_id)}</small>`:""}${Object.keys(l.metadata).length?`<small>${esc(JSON.stringify(l.metadata))}</small>`:""}</div></div>`).join(""):empty("暂无日志","≡");}catch(e){toast(e.message,true);}}
["log-level","log-category"].forEach(id=>$(id).addEventListener("change",loadLogs));$("refresh-logs").addEventListener("click",loadLogs);
$("log-search").addEventListener("keydown",e=>{if(e.key==="Enter")loadLogs();});
$("clear-logs").addEventListener("click",async()=>{if(confirm("确定清空全部运行日志吗？")){await api("/api/logs",{method:"DELETE"});loadLogs();toast("日志已清空");}});

function renderSettings(){const s=state.settings;if(!s.max_chars)return;$("setting-voice").value=s.default_voice_id;$("setting-chars").value=s.max_chars;$("setting-sentence").value=s.sentence_chars;$("setting-interval").value=s.streaming_interval;$("setting-autoplay").checked=s.autoplay;$("char-max").textContent=s.max_chars;$("synth-text").maxLength=s.max_chars;}
$("save-settings").addEventListener("click",async()=>{setBusy("save-settings",true);try{state.settings=await api("/api/settings",{method:"PATCH",body:JSON.stringify({default_voice_id:$("setting-voice").value,max_chars:Number($("setting-chars").value),sentence_chars:Number($("setting-sentence").value),streaming_interval:Number($("setting-interval").value),autoplay:$("setting-autoplay").checked})});renderSettings();$("settings-status").textContent="设置已保存";toast("设置已保存");}catch(e){toast(e.message,true);}finally{setBusy("save-settings",false);}});
async function loadModels(){
  try{
    const data=await api("/api/models");
    state.models=data.items;
    state.modelsByRole={tts:data.items.filter(m=>m.role==="tts"),asr:data.items.filter(m=>m.role==="asr")};
    updateSelectedVoice();
    const size=m=>m.size_label?`<span class="model-size">${esc(m.size_label)}</span>`:"";
    const card=m=>`<div class="model-item"><div class="model-item-title"><span>${esc(m.name)}${size(m)}</span><span class="tag ${m.installed?"installed":""}">${m.loaded?"运行中":m.installed?"已下载":"未下载"}</span></div><p>${esc(m.description)}</p>${m.installed?"":`<button class="button secondary" data-download-model="${m.key}">下载模型 ↗${m.size_label?` · ${esc(m.size_label)}`:""}</button>`}</div>`;
    $("model-list").innerHTML=state.modelsByRole.tts.map(card).join("");
    const asrList=$("asr-list");
    if(asrList){
      asrList.innerHTML=state.modelsByRole.asr.map(m=>`<div class="asr-card${m.key===state.chat.asr_model?" active":""}">
        <strong>${esc(m.name)}${m.size_label?`<span class="model-size">${esc(m.size_label)}</span>`:""}</strong><p>${esc(m.description)}</p>
        <span class="tag ${m.installed?"installed":""}">${m.loaded?"运行中":m.installed?"已下载":"未下载"}</span>
        ${m.installed?"":`<button class="text-button" data-download-model="${m.key}">下载 ↗${m.size_label?` · ${esc(m.size_label)}`:""}</button>`}</div>`).join("");
      const select=$("setting-asr-model");
      if(select) select.innerHTML=state.modelsByRole.asr.map(m=>`<option value="${esc(m.key)}">${esc(m.name)}${m.installed?"":"（未下载）"}</option>`).join("");
      const total=$("models-total");
      if(total){
        const used=state.models.filter(m=>m.installed).reduce((sum,m)=>sum+(m.size_bytes||0),0);
        total.textContent=used?`已下载 ${state.models.filter(m=>m.installed).length} 个 · 占用 ${formatBytes(used)}`:"";
      }
      if(select&&state.chat.asr_model) select.value=state.chat.asr_model;
    }
  }catch(e){toast(e.message,true);}
}
async function startModelDownload(key,button,showLogs=false){button.disabled=true;const old=button.textContent;button.textContent="下载中…";try{const r=await api("/api/models/download",{method:"POST",body:JSON.stringify({model_key:key})});toast(r.message);if(showLogs)navigate("logs");}catch(e){toast(e.message,true);button.disabled=false;button.textContent=old;}}
["model-list","asr-list"].forEach(id=>{const el=$(id);if(el)el.addEventListener("click",e=>{const b=e.target.closest("[data-download-model]");if(b)startModelDownload(b.dataset.downloadModel,b,true);});});
$("required-model-btn").addEventListener("click",e=>startModelDownload(e.currentTarget.dataset.modelKey,e.currentTarget));
$("unload-models").addEventListener("click",async()=>{if(!confirm("释放模型内存？下次合成会重新加载模型。"))return;try{await api("/api/models/unload",{method:"POST"});toast("模型内存已释放");loadModels();loadOverview();}catch(e){toast(e.message,true);}});

function openVoiceModal(voice=null){$("voice-modal").hidden=false;$("voice-form").reset();$("voice-form-error").hidden=true;$("edit-voice-id").value=voice?.id||"";$("voice-modal-title").textContent=voice?(voice.is_preset?"内置音色":"编辑音色"):"新建音色";$("new-voice-type").hidden=!!voice;$("voice-name").value=voice?.name||"";$("voice-description").value=voice?.description||"";$("voice-instruct").value=voice?.instruct||"";$("voice-reference-text").value=voice?.reference_text||"";$("clone-fields").hidden=voice?voice.kind!=="clone":true;$("design-fields").hidden=voice?voice.kind!=="design":false;$("voice-reference").required=!voice&&false;$("voice-name").disabled=!!voice?.is_preset;$("voice-description").disabled=!!voice?.is_preset;$("voice-instruct").disabled=!!voice?.is_preset;$("voice-reference-text").disabled=!!voice?.is_preset;$("save-voice").textContent=voice?.is_preset?"关闭":"保存音色";$("delete-voice").hidden=!voice||!!voice.is_preset;}
function closeVoiceModal(){$("voice-modal").hidden=true;}
$("new-voice-btn").addEventListener("click",()=>openVoiceModal());$("close-voice-modal").addEventListener("click",closeVoiceModal);$("cancel-voice").addEventListener("click",closeVoiceModal);$("voice-modal").addEventListener("click",e=>{if(e.target===$("voice-modal"))closeVoiceModal();});
$("delete-voice").addEventListener("click",async()=>{const id=$("edit-voice-id").value;if(!id||!confirm("确定删除这个自定义音色及其参考录音吗？已生成的音频会保留。"))return;try{await api(`/api/voices/${id}`,{method:"DELETE"});closeVoiceModal();toast("音色已删除");await loadVoices();}catch(e){$("voice-form-error").textContent=e.message;$("voice-form-error").hidden=false;}});
document.querySelectorAll('input[name="voice-type"]').forEach(el=>el.addEventListener("change",()=>{const clone=document.querySelector('input[name="voice-type"]:checked').value==="clone";$("clone-fields").hidden=!clone;$("design-fields").hidden=clone;}));
$("voice-form").addEventListener("submit",async e=>{e.preventDefault();const id=$("edit-voice-id").value,existing=state.voices.find(v=>v.id===id);if(existing?.is_preset)return closeVoiceModal();setBusy("save-voice",true);$("voice-form-error").hidden=true;
  try{let voice;if(id){voice=await api(`/api/voices/${id}`,{method:"PATCH",body:JSON.stringify({name:$("voice-name").value,description:$("voice-description").value,instruct:$("voice-instruct").value||null,reference_text:$("voice-reference-text").value||null})});}else{const kind=document.querySelector('input[name="voice-type"]:checked').value;voice=await api("/api/voices",{method:"POST",body:JSON.stringify({name:$("voice-name").value,kind,description:$("voice-description").value,instruct:$("voice-instruct").value||null,reference_text:$("voice-reference-text").value||null})});}
    $("edit-voice-id").value=voice.id;
    const file=$("voice-reference").files[0];if(file){const form=new FormData();form.append("file",file);form.append("reference_text",$("voice-reference-text").value);await api(`/api/voices/${voice.id}/reference`,{method:"POST",body:form});}else if(voice.kind==="clone"&&!voice.reference_path)throw new Error("音色已创建，请上传参考录音后再使用");
    closeVoiceModal();await loadVoices();await loadOverview();
    if(voice.kind!=="custom"){navigate("studio");$("synth-voice").value=voice.id;updateSelectedVoice();}
    const model=requiredModel(voice);toast(voice.kind!=="custom"&&model&&!model.installed?"音色信息已保存；还需下载对应模型才能合成":"音色已保存，可以开始合成");
  }catch(e){$("voice-form-error").textContent=e.message;$("voice-form-error").hidden=false;}finally{setBusy("save-voice",false);}
});

function applyTheme(theme){document.documentElement.dataset.theme=theme;localStorage.setItem("ethan-tts-theme",theme);}
$("theme-toggle").addEventListener("click",()=>applyTheme(document.documentElement.dataset.theme==="dark"?"light":"dark"));
applyTheme(localStorage.getItem("ethan-tts-theme")||"light");

// ---------------------------------------------------------------- pronunciation
async function loadPronunciations() {
  try {
    const data = await api("/api/pronunciations");
    state.pronunciations = data.items;
    $("pron-list").innerHTML = data.items.length ? data.items.map(p => `<div class="pron-row">
      <div class="pron-pair"><b>${esc(p.term)}</b><span class="arrow">→</span><b>${esc(p.say_as)}</b>${p.note ? `<small>${esc(p.note)}</small>` : ""}${p.match_case ? '<small>区分大小写</small>' : ""}</div>
      <span class="pron-hits">${p.hits ? `命中 ${p.hits} 次` : "尚未命中"}</span>
      <label class="switch" title="启用"><input type="checkbox" data-pron-toggle="${p.id}" ${p.is_enabled ? "checked" : ""}><span></span></label>
      <button class="text-button" data-pron-edit="${p.id}">编辑</button>
      <button class="text-button danger-text" data-pron-delete="${p.id}">删除</button>
    </div>`).join("") : empty("还没有读音规则。试试添加 Qwen → 千问", "◉");
  } catch (e) { toast(e.message, true); }
}

$("pron-form").addEventListener("submit", async e => {
  e.preventDefault();
  const term = $("pron-term").value.trim(), say_as = $("pron-say").value.trim();
  if (!term || !say_as) return toast("原文和读法都要填", true);
  setBusy("pron-add", true, "添加中…");
  try {
    const editing = $("pron-form").dataset.editId;
    if (editing) {
      await api(`/api/pronunciations/${editing}`, {method:"PATCH", body:JSON.stringify({term, say_as, note:$("pron-note").value.trim(), match_case:$("pron-case").checked})});
    } else {
      await api("/api/pronunciations", {method:"POST", body:JSON.stringify({term, say_as, note:$("pron-note").value.trim(), match_case:$("pron-case").checked})});
    }
    resetPronForm(); await loadPronunciations(); toast(editing ? "已更新读音规则" : "已添加读音规则");
  } catch (err) { toast(err.message, true); }
  finally { setBusy("pron-add", false); }
});

function resetPronForm() {
  delete $("pron-form").dataset.editId;
  $("pron-term").value = ""; $("pron-say").value = ""; $("pron-note").value = "";
  $("pron-case").checked = false; $("pron-add").textContent = "＋ 添加";
  $("pron-term").disabled = false;
}

$("pron-list").addEventListener("click", async e => {
  const del = e.target.closest("[data-pron-delete]");
  const edit = e.target.closest("[data-pron-edit]");
  if (del) {
    const rule = state.pronunciations.find(p => p.id === del.dataset.pronDelete);
    if (!confirm(`删除「${rule?.term} → ${rule?.say_as}」？`)) return;
    try { await api(`/api/pronunciations/${del.dataset.pronDelete}`, {method:"DELETE"}); await loadPronunciations(); toast("已删除"); }
    catch (err) { toast(err.message, true); }
  }
  if (edit) {
    const rule = state.pronunciations.find(p => p.id === edit.dataset.pronEdit);
    if (!rule) return;
    $("pron-form").dataset.editId = rule.id;
    $("pron-term").value = rule.term; $("pron-say").value = rule.say_as;
    $("pron-note").value = rule.note; $("pron-case").checked = !!rule.match_case;
    $("pron-add").textContent = "保存修改";
    $("pron-term").focus();
  }
});

$("pron-list").addEventListener("change", async e => {
  const toggle = e.target.closest("[data-pron-toggle]");
  if (!toggle) return;
  try {
    await api(`/api/pronunciations/${toggle.dataset.pronToggle}`, {method:"PATCH", body:JSON.stringify({is_enabled: toggle.checked})});
    toast(toggle.checked ? "已启用" : "已停用");
  } catch (err) { toast(err.message, true); loadPronunciations(); }
});

$("pron-preview-btn").addEventListener("click", async () => {
  const text = $("pron-test").value.trim() || "Qwen3-TTS 是本地模型，Qwen 这个词应该读作千问。";
  setBusy("pron-preview-btn", true, "预览中…");
  try {
    const r = await api("/api/pronunciations/preview", {method:"POST", body:JSON.stringify({text})});
    const hits = r.matches.map(m => `<span class="hit">${esc(m.term)} → ${esc(m.say_as)}</span>`).join("");
    $("pron-result").hidden = false;
    $("pron-result").innerHTML = `<span class="label">模型实际会读</span><span class="spoken">${esc(r.spoken)}</span>${hits || '<span class="label">没有命中任何规则</span>'}`;
  } catch (e) { toast(e.message, true); }
  finally { setBusy("pron-preview-btn", false); }
});

async function init(){
  // Switch the view first so the active tab and content match the URL
  // immediately; data loading must not gate the initial paint.
  applyTheme(localStorage.getItem("ethan-tts-theme")||"light");
  const [hashView, hashTab] = location.hash.slice(1).split("/");
  navigate(hashView||"studio");
  if (hashView === "settings" && hashTab) showSettingsTab(hashTab);
  try{
    state.settings=await api("/api/settings");
    applyTheme(localStorage.getItem("ethan-tts-theme")||state.settings.theme);
    await loadVoices();renderSettings();updateCount();await loadOverview();await loadPronunciations();
    await loadChatSettings();await loadModels();loadLanAddress();
    navigate(state.current);
    if (state.current === "settings" && hashTab) showSettingsTab(hashTab);
    state.poll=setInterval(()=>{loadOverview();if(state.current==="tasks")loadTasks();if(state.current==="settings")loadModels();},5000);
  }catch(e){toast(`初始化失败：${e.message}`,true);}
}
init();

// ------------------------------------------------------------------ chat settings
// The settings page owns the model configuration: without it the voice chat
// page has nothing to talk to. Everything here is a PATCH to /api/chat/settings,
// so a save is atomic and the page can re-read the masked key state.

function showSettingsTab(name) {
  state.settingsTab = name;
  document.querySelectorAll(".tab").forEach(el => el.classList.toggle("active", el.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach(el => el.classList.toggle("active", el.dataset.panel === name));
  if (state.current === "settings") {
    history.replaceState(null, "", `#settings${name === "general" ? "" : "/" + name}`);
  }
  if (name === "speech" || name === "resources") loadModels();
  if (name === "general") loadChatSettings();
}

document.querySelectorAll(".tab").forEach(el =>
  el.addEventListener("click", () => showSettingsTab(el.dataset.tab)));

async function loadChatSettings() {
  try {
    const [data, bootstrap] = await Promise.all([
      api("/api/chat/settings"),
      state.defaultPrompt === undefined || !state.presets ? api("/api/chat/bootstrap") : Promise.resolve(null),
    ]);
    state.chat = data;
    if (bootstrap) {
      state.presets = bootstrap.presets;
      state.defaultPrompt = bootstrap.default_prompt;
      const asr = bootstrap.models.asr || [];
      const select = $("setting-asr-model");
      if (select && !select.options.length) {
        select.innerHTML = asr.map(m => `<option value="${esc(m.key)}">${esc(m.name)}${m.installed ? "" : "（未下载）"}</option>`).join("");
      }
    }
    renderChatSettings();
  } catch (e) { console.debug(e); }
}

function renderChatSettings() {
  const c = state.chat;
  if (!c || typeof c.llm_base_url !== "string") return;
  if ($("setting-base-url")) $("setting-base-url").value = c.llm_base_url || "";
  if ($("setting-model")) $("setting-model").value = c.llm_model || "";
  if ($("setting-temperature")) $("setting-temperature").value = c.llm_temperature ?? .7;
  if ($("setting-max-tokens")) $("setting-max-tokens").value = c.llm_max_tokens ?? 320;
  if ($("setting-system-prompt")) $("setting-system-prompt").value = c.system_prompt || state.defaultPrompt || "";
  if ($("setting-first-chars")) $("setting-first-chars").value = c.tts_first_chars ?? 8;
  if ($("setting-tts-max")) $("setting-tts-max").value = c.tts_max_chars ?? 60;
  if ($("setting-speech-rate")) $("setting-speech-rate").value = c.speech_rate ?? 1;
  if ($("setting-history")) $("setting-history").value = c.history_turns ?? 8;
  if ($("setting-asr-model") && c.asr_model) $("setting-asr-model").value = c.asr_model;
  if ($("api-key-hint")) {
    $("api-key-hint").textContent = c.has_api_key
      ? "已保存一个 Key；留空表示不修改，点「清除」可删除"
      : "只保存在本地数据库，不会出现在页面上";
  }
  const chatVoice = $("setting-chat-voice");
  if (chatVoice) {
    const options = state.voices.map(v => `<option value="${esc(v.id)}">${esc(v.name)} · ${kindNames[v.kind]}</option>`).join("");
    chatVoice.innerHTML = options;
    if (c.voice_id) chatVoice.value = c.voice_id;
  }
  const presets = $("model-presets");
  if (presets) {
    presets.innerHTML = (state.presets || []).map(p =>
      `<button class="preset-chip" data-preset="${esc(p.base_url)}|${esc(p.model)}">${esc(p.label)}</button>`).join("");
  }
}

async function saveChatPatch(payload, statusEl, message = "已保存") {
  try {
    state.chat = await api("/api/chat/settings", { method: "PATCH", body: JSON.stringify(payload) });
    renderChatSettings();
    if (statusEl) { statusEl.textContent = message; setTimeout(() => statusEl.textContent = "", 2400); }
    toast(message);
    return true;
  } catch (e) { toast(e.message, true); return false; }
}

function modelPayload() {
  const payload = {
    llm_base_url: $("setting-base-url").value.trim(),
    llm_model: $("setting-model").value.trim(),
    llm_temperature: Number($("setting-temperature").value),
    llm_max_tokens: Number($("setting-max-tokens").value),
  };
  const key = $("setting-api-key").value.trim();
  if (key) payload.llm_api_key = key;
  return payload;
}

$("save-model").addEventListener("click", async () => {
  const ok = await saveChatPatch(modelPayload(), $("model-status"), "模型设置已保存");
  if (ok) $("setting-api-key").value = "";
});

$("test-model").addEventListener("click", async () => {
  setBusy("test-model", true, "测试中…");
  $("model-status").textContent = "";
  try {
    const result = await api("/api/chat/settings/test", {
      method: "POST",
      body: JSON.stringify({
        base_url: $("setting-base-url").value.trim(),
        api_key: $("setting-api-key").value.trim(),
        model: $("setting-model").value.trim(),
      }),
    });
    $("model-status").textContent = `连接成功 · ${result.seconds}s · 返回「${result.reply}」`;
    toast("模型连接正常");
  } catch (e) {
    $("model-status").textContent = e.message;
    toast(e.message, true);
  } finally { setBusy("test-model", false); }
});

$("fetch-models").addEventListener("click", async () => {
  const base = $("setting-base-url").value.trim();
  if (!base) return toast("先填接口地址", true);
  setBusy("fetch-models", true, "读取中…");
  try {
    const params = new URLSearchParams({ base_url: base, api_key: $("setting-api-key").value.trim() });
    const data = await api(`/api/chat/settings/models?${params}`);
    $("model-options").innerHTML = data.items.map(id => `<option value="${esc(id)}"></option>`).join("");
    toast(data.items.length ? `找到 ${data.items.length} 个模型，点输入框可选` : "该服务没有返回模型列表");
  } catch (e) { toast(e.message, true); }
  finally { setBusy("fetch-models", false); }
});

$("model-presets").addEventListener("click", e => {
  const chip = e.target.closest("[data-preset]");
  if (!chip) return;
  const [base, model] = chip.dataset.preset.split("|");
  $("setting-base-url").value = base;
  $("setting-model").value = model;
});

$("toggle-key").addEventListener("click", () => {
  const input = $("setting-api-key");
  input.type = input.type === "password" ? "text" : "password";
  $("toggle-key").textContent = input.type === "password" ? "显示" : "隐藏";
});

$("clear-key").addEventListener("click", async () => {
  if (!confirm("清除已保存的 API Key？")) return;
  $("setting-api-key").value = "";
  await saveChatPatch({ llm_api_key: "__clear__" }, $("model-status"), "API Key 已清除");
});

$("save-prompt").addEventListener("click", () =>
  saveChatPatch({ system_prompt: $("setting-system-prompt").value }, $("prompt-status"), "提示词已保存"));

$("reset-prompt").addEventListener("click", async () => {
  if (!confirm("恢复默认的语音对话提示词？")) return;
  if (!state.defaultPrompt) {
    const data = await api("/api/chat/bootstrap");
    state.defaultPrompt = data.default_prompt;
  }
  $("setting-system-prompt").value = state.defaultPrompt;
  toast("已填入默认提示词，记得保存");
});

$("save-chat-settings").addEventListener("click", () =>
  saveChatPatch({
    voice_id: $("setting-chat-voice").value,
    tts_first_chars: Number($("setting-first-chars").value),
    tts_max_chars: Number($("setting-tts-max").value),
    speech_rate: Number($("setting-speech-rate").value),
    history_turns: Number($("setting-history").value),
  }, $("chat-settings-status"), "对话设置已保存"));

$("save-asr").addEventListener("click", async () => {
  const ok = await saveChatPatch({ asr_model: $("setting-asr-model").value }, $("asr-status"), "语音识别模型已保存");
  if (ok) loadModels();
});

async function loadLanAddress() {
  try {
    const data = await api("/api/network");
    $("lan-address").textContent = data.urls.length ? data.urls.join("  ·  ") + "/chat" : "未检测到局域网地址";
  } catch (_) { $("lan-address").textContent = "—"; }
}
