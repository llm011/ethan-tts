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

/* 把当前会话写进地址栏：/chat/<id>。
   以前所有会话共用 /chat，刷新永远回到第一个对话，也没法分享某一段。 */
function syncUrl(sessionId, push = false) {
  const target = sessionId ? `/chat/${encodeURIComponent(sessionId)}` : "/chat";
  if (location.pathname === target) return;
  if (push) history.pushState({ sessionId }, "", target);
  else history.replaceState({ sessionId }, "", target);
}

function sessionIdFromUrl() {
  const match = location.pathname.match(/^\/chat\/([^/]+)\/?$/);
  return match ? decodeURIComponent(match[1]) : null;
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
