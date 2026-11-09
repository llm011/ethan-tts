
/* ---------------------------------------------------------- voice picker */
/* 朗读音色以前只能去设置页改，聊到一半想换个声音很别扭。
   这里直接放一个切换器：选中即写回设置，从下一轮起生效。 */
const LANG_NAMES = { Chinese: "中文", English: "英语", Japanese: "日语", Korean: "韩语" };

function voiceLabel(voice) {
  const parts = [LANG_NAMES[voice.language] || voice.language || ""];
  const gender = (voice.description || "").match(/^(男声|女声)/)?.[1];
  if (gender) parts.push(gender);
  if (voice.dialect) parts.push("方言");
  return parts.filter(Boolean).join(" · ");
}

function currentVoice() {
  return (state.voices || []).find(v => v.id === state.settings?.voice_id);
}

function renderVoiceChip() {
  const voice = currentVoice();
  $("voice-name").textContent = voice ? voice.name : "音色";
  $("voice-chip").classList.toggle("warn", !voice);
}

function renderVoiceList() {
  const chosen = state.settings?.voice_id;
  $("voice-list").innerHTML = (state.voices || []).map(voice => `
    <button class="voice-option${voice.id === chosen ? " active" : ""}" role="option"
            aria-selected="${voice.id === chosen}" data-voice-id="${esc(voice.id)}">
      <span class="vo-main">
        <span class="vo-name">${esc(voice.name)}</span>
        <span class="vo-traits">${esc(voiceLabel(voice))}</span>
      </span>
      <span class="vo-desc">${esc(voice.description || "")}</span>
      ${voice.usage_hint ? `<span class="vo-usage">适合 ${esc(voice.usage_hint)}</span>` : ""}
      ${voice.id === chosen ? '<span class="vo-check">✓</span>' : ""}
    </button>`).join("") || '<div class="vo-empty">还没有可用的音色</div>';
}

function openVoiceSheet() {
  if (!(state.voices || []).length) return toast("还没有可用音色", true);
  renderVoiceList();
  $("voice-sheet").hidden = false;
  $("voice-sheet-mask").hidden = false;
  $("voice-sheet").querySelector(".voice-option.active")?.scrollIntoView({ block: "nearest" });
}

function closeVoiceSheet() {
  $("voice-sheet").hidden = true;
  $("voice-sheet-mask").hidden = true;
}

async function chooseVoice(voiceId) {
  if (voiceId === state.settings?.voice_id) return closeVoiceSheet();
  try {
    state.settings = await api("/api/chat/settings", {
      method: "PATCH", body: JSON.stringify({ voice_id: voiceId }),
    });
    renderVoiceChip();
    renderVoiceList();
    closeVoiceSheet();
    // 音色在每轮开始时读取一次，朗读中途切换要到下一轮才生效。
    const name = currentVoice()?.name || voiceId;
    toast(state.busy ? `已切换为「${name}」，这轮说完后生效` : `已切换为「${name}」`);
  } catch (err) { toast(err.message, true); }
}

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
  syncHeroMode();
  scrollThread();
}

/* 没有消息时圆形图标居中占满舞台；有消息后缩成顶部标记，
   把剩余高度全部让给消息区（两者都 flex:1 时会把高度对半分）。 */
function syncHeroMode() {
  const empty = state.messages.length === 0 && !$("thread").children.length;
  $("hero").classList.toggle("idle-mode", empty);
  $("hero").classList.toggle("small", !empty);
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
  syncHeroMode();
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

/* 把当前会话写进地址栏：/chat/<id>。
   以前所有会话共用 /chat，刷新永远回到第一个对话，也没法分享某一段。 */
function syncUrl(sessionId, push = false) {
  // 会话 id 形如 chat:595584e74239；冒号在 URL 路径里合法，
  // 只转义会截断路径的 ? 和 #，地址更可读。
  const target = sessionId ? `/chat/${sessionId.replace(/[?#]/g, encodeURIComponent)}` : "/chat";
  if (location.pathname === target) return;
  if (push) history.pushState({ sessionId }, "", target);
  else history.replaceState({ sessionId }, "", target);
}

function sessionIdFromUrl() {
  const match = location.pathname.match(/^\/chat\/([^/]+)\/?$/);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);   // 兼容早期 %3A 的链接
  } catch (_) {
    return match[1];
  }
}

async function openSession(sessionId, options = {}) {
  if (state.busy) return toast("等这一轮说完再切换对话", true);
  player.stop();
  try {
    const data = await api(`/api/chat/sessions/${sessionId}`);
    state.sessionId = data.id;
    syncUrl(data.id, options.push === true);
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
  syncUrl(null, true);
  $("session-title").textContent = "新的对话";
  renderThread();
  renderSessions();
  closeRail();
  idleOrb();
}
