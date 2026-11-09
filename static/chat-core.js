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
  voices: [],
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
