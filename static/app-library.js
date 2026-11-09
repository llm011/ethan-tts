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
/* 下载按钮的三种状态：可点、下载中（禁用 + 进度）、失败可重试。
   状态来自服务端轮询，而不是点击时的临时变量——否则 5 秒一次的
   loadModels 重渲染会把「下载中」冲掉，按钮又变回可点。 */
function downloadControl(m, compact=false) {
  const job = state.downloads[m.key];
  const label = compact ? "下载 ↗" : "下载模型 ↗";
  const sizeText = m.size_label ? ` · ${esc(m.size_label)}` : "";
  if (job && job.status === "running") {
    const pct = job.percent == null ? "" : `${job.percent}%`;
    const bar = job.percent == null ? "" :
      `<span class="dl-bar"><i style="width:${Math.max(2, job.percent)}%"></i></span>`;
    return `<div class="dl-progress" data-dl="${esc(m.key)}">${bar}<span>下载中${pct ? ` ${pct}` : ""}…</span></div>`;
  }
  if (job && job.status === "error") {
    return `<button class="${compact ? "text-button" : "button secondary"}" data-download-model="${esc(m.key)}" title="${esc(job.message || "")}">重试下载 ↻</button>`;
  }
  return `<button class="${compact ? "text-button" : "button secondary"}" data-download-model="${esc(m.key)}">${label}${sizeText}</button>`;
}

async function loadDownloads(){
  try{
    const data=await api("/api/models/downloads");
    const next={};
    for(const item of data.items||[]) next[item.model_key]=item;
    const changed=JSON.stringify(next)!==JSON.stringify(state.downloads);
    state.downloads=next;
    if(changed){ if(state.current==="settings") loadModels(); }
  }catch(e){ /* 轮询失败不打扰用户 */ }
}

async function loadModels(){
  try{
    const data=await api("/api/models");
    state.models=data.items;
    state.modelsByRole={tts:data.items.filter(m=>m.role==="tts"),asr:data.items.filter(m=>m.role==="asr")};
    updateSelectedVoice();
    const size=m=>m.size_label?`<span class="model-size">${esc(m.size_label)}</span>`:"";
    const card=m=>`<div class="model-item"><div class="model-item-title"><span>${esc(m.name)}${size(m)}</span><span class="tag ${m.installed?"installed":""}">${m.loaded?"运行中":m.installed?"已下载":"未下载"}</span></div><p>${esc(m.description)}</p>${m.installed?"":downloadControl(m)}</div>`;
    $("model-list").innerHTML=state.modelsByRole.tts.map(card).join("");
    const asrList=$("asr-list");
    if(asrList){
      asrList.innerHTML=state.modelsByRole.asr.map(m=>`<div class="asr-card${m.key===state.chat.asr_model?" active":""}">
        <strong>${esc(m.name)}${m.size_label?`<span class="model-size">${esc(m.size_label)}</span>`:""}</strong><p>${esc(m.description)}</p>
        <span class="tag ${m.installed?"installed":""}">${m.loaded?"运行中":m.installed?"已下载":"未下载"}</span>
        ${m.installed?"":downloadControl(m, true)}</div>`).join("");
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
async function startModelDownload(key,button,showLogs=false){
  button.disabled=true;
  try{
    const r=await api("/api/models/download",{method:"POST",body:JSON.stringify({model_key:key})});
    toast(r.message);
    await loadDownloads();
    await loadModels();
  }catch(e){
    toast(e.message,true);
    button.disabled=false;
    await loadDownloads();
  }
}
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
