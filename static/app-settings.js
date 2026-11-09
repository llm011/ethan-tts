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
    await loadChatSettings();await loadDownloads();await loadModels();loadLanAddress();
    navigate(state.current);
    if (state.current === "settings" && hashTab) showSettingsTab(hashTab);
    state.poll=setInterval(()=>{loadOverview();if(state.current==="tasks")loadTasks();if(state.current==="settings"){loadModels();loadDownloads();}},5000);
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
  if ($("setting-use-proxy")) {
    $("setting-use-proxy").checked = !!c.llm_use_proxy;
    $("proxy-row").hidden = !c.llm_use_proxy;
  }
  if ($("setting-proxy-url")) $("setting-proxy-url").value = c.llm_proxy_url || "";
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
  payload.llm_use_proxy = $("setting-use-proxy").checked;
  payload.llm_proxy_url = $("setting-proxy-url").value.trim();
  return payload;
}

$("setting-use-proxy").addEventListener("change", () => {
  $("proxy-row").hidden = !$("setting-use-proxy").checked;
});

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
