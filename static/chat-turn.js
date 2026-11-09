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
    if (!state.sessionId) {
      state.sessionId = message.session_id;
      syncUrl(state.sessionId, true);
    }
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

/* 前进/后退：地址变了就切到对应会话，不重新加载页面。 */
window.addEventListener("popstate", () => {
  const target = sessionIdFromUrl();
  if (target === state.sessionId) return;
  if (state.busy) {
    // 正在对话时 openSession/newSession 会被挡住，地址却已经变了，
    // 会出现「地址是一个会话、内容是另一个」。这里把地址退回去，
    // 而不是让两者不一致。
    toast("等这一轮说完再切换对话", true);
    syncUrl(state.sessionId);
    return;
  }
  if (target) openSession(target);
  else newSession();
});

async function bootstrap() {
  buildBars();
  const data = await api("/api/chat/bootstrap");
  state.settings = data.settings;
  state.configWarning = data.config_warning || "";
  state.models = data.models;
  state.voices = data.voices || [];
  state.sessions = data.sessions || [];
  state.audioRate = data.audio_rate || AUDIO_RATE;
  renderSessions();
  state.messages = [];
  renderThread();
  renderVoiceChip();
  idleOrb();
  syncOrbCursor();
  checkSetup();
  if (state.settings?.configured) {
    // 后台预热，失败也无所谓；成功了第一次回复就不用等模型加载。
    fetch("/api/chat/warmup", { method: "POST" }).catch(() => {});
  }
  const fromUrl = sessionIdFromUrl();
  if (fromUrl) {
    await openSession(fromUrl);
    // 地址里的会话已被删除时，退回到最近一条，而不是停在空页。
    if (state.sessionId !== fromUrl && state.sessions.length) {
      await openSession(state.sessions[0].id);
    }
  } else if (state.sessions.length) {
    await openSession(state.sessions[0].id);
  }
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
  $("voice-chip").addEventListener("click", openVoiceSheet);
  $("voice-sheet-close").addEventListener("click", closeVoiceSheet);
  $("voice-sheet-mask").addEventListener("click", closeVoiceSheet);
  $("voice-list").addEventListener("click", e => {
    const option = e.target.closest("[data-voice-id]");
    if (option) chooseVoice(option.dataset.voiceId);
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !$("voice-sheet").hidden) closeVoiceSheet();
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
