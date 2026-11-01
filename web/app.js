/* TuDirector SPA — tutte le chiamate usano URL relativi (RP-safe) */
"use strict";

const $ = (sel, el) => (el || document).querySelector(sel);
const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));

const state = {
  view: "projects",
  jobs: [],
  job: null,
  pollTimer: null,
  skills: [],
  files: [],
  stTimer: null,
};

const DEC_COLORS = {
  zoom: "#7ee787", pan: "#7ee787", highlight_pulse: "#a5d6ff",
  effects: "#d2a8ff", text_overlay: "#ffa657", lower_third: "#ffa657",
  polaroid: "#ff7b72", citation_card: "#ff7b72", sticker_anim: "#f2cc60",
  camera_switch: "#4dc3ff", slowmo: "#79c0ff", narration: "#ff9ed2",
  spotlight: "#fff27b",
};
const DEC_LABEL = {
  zoom: "zoom", pan: "pan", highlight_pulse: "pulse", effects: "effetti",
  text_overlay: "testo", lower_third: "banner", polaroid: "polaroid",
  citation_card: "citazione", sticker_anim: "animazione",
  camera_switch: "CAM", slowmo: "rallenty", narration: "voce", spotlight: "spot",
};

/* ---------------------------------------------------------------- api */
async function api(path, opts) {
  const r = await fetch("api/" + path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}

function fmtT(sec) {
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}

/* ---------------------------------------------------------------- views */
function show(view) {
  state.view = view;
  $$(".view").forEach(v => v.classList.remove("active"));
  $("#view-" + view).classList.add("active");
  $$("nav button").forEach(b => b.classList.toggle("active", b.dataset.view === view));
  if (view === "projects") { loadJobs(); }
  if (view === "skills") { loadSkills(); }
  if (view === "settings") { loadSettings(); }
}

/* ---------------------------------------------------------------- health */
async function loadHealth() {
  try {
    const h = await api("health");
    $("#ver").textContent = h.version;
    const badge = $("#modeBadge");
    badge.textContent = h.mock_mode ? "MOCK MODE" : "LIVE";
    badge.className = "badge " + (h.mock_mode ? "mock" : "live");
  } catch (e) { $("#modeBadge").textContent = "offline"; }
}

/* ---------------------------------------------------------------- upload */
function initUpload() {
  const dz = $("#dropzone"), input = $("#fileInput");
  $("#pickFiles").onclick = () => input.click();
  input.onchange = () => addFiles(input.files);
  dz.ondragover = e => { e.preventDefault(); dz.classList.add("drag"); };
  dz.ondragleave = () => dz.classList.remove("drag");
  dz.ondrop = e => { e.preventDefault(); dz.classList.remove("drag"); addFiles(e.dataTransfer.files); };

  $("#startBtn").onclick = startJob;
}

function addFiles(list) {
  for (const f of list) if (f.type.startsWith("video/") || /\.(mp4|mov|mkv|webm|avi|m4v)$/i.test(f.name))
    state.files.push(f);
  renderFileList();
}

function renderFileList() {
  $("#fileList").innerHTML = state.files.map((f, i) =>
    `<li>🎬 ${esc(f.name)} <span class="hint">(${(f.size / 1e6).toFixed(1)} MB${i === 0 ? " — traccia principale" : ` — CAM ${i}`})</span>
     <button class="linklike" data-i="${i}">✕</button></li>`).join("");
  $$("#fileList button").forEach(b => b.onclick = () => {
    state.files.splice(+b.dataset.i, 1); renderFileList();
  });
}

async function startJob() {
  if (!state.files.length) { alert("Aggiungi almeno un video"); return; }
  const fd = new FormData();
  state.files.forEach(f => fd.append("files", f));
  fd.append("skill", $("#skillSelect").value);
  fd.append("title", $("#jobTitle").value);
  const bar = $("#uploadProgress"), fill = $(".bar", bar);
  bar.classList.remove("hidden");
  fill.style.width = "0%";
  try {
    const data = await new Promise((res, rej) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "api/jobs");
      xhr.upload.onprogress = e => { if (e.lengthComputable) fill.style.width = (e.loaded / e.total * 100) + "%"; };
      xhr.onload = () => xhr.status < 400 ? res(JSON.parse(xhr.responseText)) : rej(new Error(xhr.responseText));
      xhr.onerror = () => rej(new Error("errore di rete"));
      xhr.send(fd);
    });
    state.files = []; renderFileList();
    $("#jobTitle").value = "";
    await loadJobs();
    openJob(data.job.id);
  } catch (e) { alert("Upload fallito: " + e.message); }
  finally { bar.classList.add("hidden"); }
}

/* ---------------------------------------------------------------- jobs list */
async function loadJobs() {
  const data = await api("jobs");
  state.jobs = data.jobs;
  const el = $("#jobsList");
  if (!state.jobs.length) { el.innerHTML = "<em>Nessun progetto: carica il primo video.</em>"; return; }
  el.innerHTML = state.jobs.map(j => `
    <div class="job-card" data-id="${j.id}">
      <img src="media/jobs/${j.id}/thumb.jpg" onerror="this.style.visibility='hidden'"
           alt="" loading="lazy">
      <div class="jc-body">
        <div class="jc-title">${esc(j.title)}</div>
        <div class="jc-meta"><span class="status-pill status-${j.status}">${j.status}</span>
          ${esc(j.skill || "")} · ${new Date(j.created_at * 1000).toLocaleString("it-IT")}</div>
      </div>
    </div>`).join("");
  $$(".job-card").forEach(c => c.onclick = () => openJob(c.dataset.id));
}

/* ---------------------------------------------------------------- studio */
async function openJob(id) {
  show("studio");
  $("#studioEmpty").classList.add("hidden");
  $("#studio").classList.remove("hidden");
  state.jobId = id;
  state.logOpen = false;
  $("#logWindow").textContent = "apri il log per vedere la storia di esecuzione…";
  await refreshJob();
  clearInterval(state.stTimer);
  state.stTimer = setInterval(() => {
    if (state.view === "studio") refreshJob(); else clearInterval(state.stTimer);
  }, 2500);
}

async function refreshJob() {
  if (!state.jobId) return;
  try {
    const j = await api("jobs/" + state.jobId);
    state.job = j;
    renderStudio(j);
    if (j.status === "running") $("#stRerun").disabled = true; else $("#stRerun").disabled = false;
  } catch (e) { /* job sparito */ }
}

function renderStudio(j) {
  $("#stTitle").textContent = j.title + `  ·  ${fmtT(j.tracks?.[0]?.duration || 0)}`;
  const sel = $("#stSkill");
  if (sel.options.length !== state.skills.length) fillSkillSelect(sel);
  sel.value = j.skill || "base";

  // stages
  $("#stageList").innerHTML = Object.entries(j.stages).map(([id, st]) => `
    <div class="stage ${st.status}">
      <span class="s-name">${id}</span>
      <div class="s-bar"><div class="fill" style="width:${Math.round((st.progress || 0) * 100)}%"></div></div>
      <span class="s-msg">${esc(st.message || st.status)}</span>
    </div>`).join("");
  if (j.error) $("#stageList").innerHTML += `<p class="hint" style="color:var(--err)">⚠ ${esc(j.error)}</p>`;

  // transcript
  const segs = (j.transcript && j.transcript.segments) || [];
  $("#transcript").innerHTML = segs.length ? segs.map(s =>
    `<div class="seg"><span class="tc">${fmtT(s.start)}–${fmtT(s.end)}</span>${esc(s.text)}</div>`).join("")
    : "<em class='hint'>in attesa trascrizione…</em>";

  // frames
  const caps = j.captions || [];
  $("#frameStrip").innerHTML = caps.length ? caps.map(c => `
    <figure>
      <img src="media/jobs/${j.id}/frames/track-${c.track ?? 0}/f${String(Math.floor(c.t)).padStart(6, "0")}.jpg"
           onerror="this.style.opacity=.15" loading="lazy">
      <figcaption><span class="tc">${fmtT(c.t)}${j.tracks.length > 1 ? ` C${c.track}` : ""}</span> ${esc(c.caption)}</figcaption>
    </figure>`).join("") : "<em class='hint'>in attesa descrizioni…</em>";

  renderTimeline(j);
  renderWindows(j);

  // output: versione render per evitare confusioni vecchio/nuovo (cache-busting)
  const card = $("#outputCard");
  if (j.output) {
    const v = j.render_id || "x";
    const url = `media/jobs/${j.id}/${j.output}?v=${v}`;
    $("#outVideo").src = url;
    $("#outDownload").href = url;
    $("#outDownload").classList.remove("hidden");
    const rends = j.renders || [];
    $("#outputCard h3").textContent = `Output — render ${rends.length}` +
      (rends.length ? ` (ultimo: render_id ${v}, run #${rends[rends.length - 1].run})` : "");
  } else {
    $("#outVideo").removeAttribute("src");
    $("#outDownload").classList.add("hidden");
  }
  if (state.logOpen) loadJobLog(false);
}

function renderTimeline(j) {
  const dur = j.tracks?.[0]?.duration || 0;
  const el = $("#timeline");
  if (!dur) { el.innerHTML = ""; return; }
  const decs = (j.windows || []).flatMap(w => (w.decisions || []).map(d => ({...d, w: w.window_id})));
  el.innerHTML = decs.map(d => {
    const left = d.t / dur * 100, width = Math.max(0.8, d.dur / dur * 100);
    const c = DEC_COLORS[d.type] || "#8b98a9";
    return `<div class="dec-chip" data-t="${d.t}" title="${esc(d.type)} @ ${fmtT(d.t)}"
      style="left:${left}%;width:${width}%;background:${c}">${DEC_LABEL[d.type] || d.type}</div>`;
  }).join("") +
  Array.from({length: Math.floor(dur / 30)}, (_, i) =>
    `<div class="tick" style="left:${(i * 30) / dur * 100}%;width:1px;background:var(--line)"></div>`).join("");
}

function renderWindows(j) {
  const wins = j.windows || [];
  const el = $("#windows");
  if (!wins.length) { el.innerHTML = "<em class='hint'>il regista non ha ancora deciso…</em>"; return; }
  el.innerHTML = wins.map(w => `
    <div class="window" data-wid="${w.window_id}">
      <div class="w-head">
        <input type="checkbox" class="merge-check" data-wid="${w.window_id}"
               title="seleziona per unire">
        <span class="w-id">${w.window_id}${w.merged_from ? " ⧉" : ""} · ${fmtT(w.t_start)}–${fmtT(w.t_end)}</span>
        <span class="w-mood">mood: ${esc(w.mood || "—")} ${w.engine === "mock" ? "· [mock]" : ""}${w.edited ? " · ✎" : ""}</span>
        <span style="flex:1"></span>
        <button class="prev-btn" data-wid="${w.window_id}"
                data-t0="${w.t_start}" data-dur="${Math.max(1, w.t_end - w.t_start)}"
                title="preview 400px di questo spezzone">⚡ preview</button>
        <button class="redo-btn" data-wid="${w.window_id}">↻ redo</button>
      </div>
      <div class="w-summary">${esc(w.summary || "")}</div>
      <div class="decisions">
        ${(w.decisions || []).map(d => `
          <div class="decision">
            <span class="d-type" style="color:${DEC_COLORS[d.type] || "var(--accent2)"}">${DEC_LABEL[d.type] || d.type}
              @ ${fmtT(d.t)} (${d.dur}s)</span>
            <span class="d-params">${esc(decParams(d))}</span>
            ${d.asset ? `<img class="d-thumb" src="media/jobs/${j.id}/${d.asset}">` :
              d.reason ? `<span class="d-reason" title="${esc(d.reason)}">💡</span>` : ""}
          </div>`).join("") || "<em class='hint'>nessuna mossa di regia</em>"}
      </div>
      <div class="row" style="margin-top:6px">
        <input id="nl-${w.window_id}" class="nl-input" style="flex:1"
               placeholder="modifica con linguaggio naturale, es: 'allunga lo zoom e togli la polaroid'">
        <button id="nlbtn-${w.window_id}" class="nl-btn" data-wid="${w.window_id}">✎ Applica</button>
      </div>
    </div>`).join("");
  $$(".redo-btn").forEach(b => b.onclick = () => redoWindow(b.dataset.wid));
  $$(".prev-btn").forEach(b => b.onclick = () => {
    $$(".window").forEach(el => el.classList.remove("previewing"));
    b.closest(".window").classList.add("previewing");
    startPreview(+b.dataset.t0, +b.dataset.dur, b.dataset.wid);
  });
  $$(".merge-check").forEach(c => c.onchange = updateMergeBar);
  $$(".nl-btn").forEach(b => b.onclick = () => nlEditWindow(b.dataset.wid));
  $$(".nl-input").forEach(i => i.onkeydown = e => {
    if (e.key === "Enter") nlEditWindow(i.id.replace("nl-", ""));
  });
}

function decParams(d) {
  const p = {...d.params};
  delete p._png; delete p.svg;
  if (d.params && d.params.svg) p.svg = "(svg " + d.params.svg.length + " B)";
  return Object.entries(p).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" ").slice(0, 160);
}

async function redoWindow(wid) {
  const note = prompt("Nota per il regista (opzionale — come vuole cambiare la regia di questa finestra?)", "");
  if (note === null) return;
  try {
    await api(`jobs/${state.jobId}/window/${wid}/redo`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({note})
    });
    await refreshJob();
  } catch (e) { alert("redo fallito: " + e.message); }
}

/* ---------------------------------------------------------------- preview */
async function startPreview(t0Arg, durArg, label) {
  const t0 = t0Arg !== undefined ? t0Arg : parseFloat($("#prevStart").value || 0);
  const dur = durArg !== undefined ? durArg : parseFloat($("#prevDur").value || 20);
  const st = $("#prevStatus");
  st.textContent = label ? `preview ${label}…` : "generazione preview…";
  state.previewWindow = label || null;
  try {
    await api(`jobs/${state.jobId}/preview`, {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({t0, dur})});
    // attende il completamento via polling del job
    const iv = setInterval(async () => {
      const j = await api("jobs/" + state.jobId);
      const ps = j.stages.preview || {};
      if (ps.status === "done") {
        clearInterval(iv);
        $("#prevVideo").src = `media/jobs/${state.jobId}/${ps.message}?v=${Date.now()}`;
        $("#prevLabel").textContent = state.previewWindow
          ? `Preview dello spezzone ${state.previewWindow}` : "Preview";
        $("#prevBox").classList.remove("hidden");
        st.textContent = "✓ preview pronta";
      } else if (ps.status === "error") {
        clearInterval(iv);
        st.textContent = "✗ " + (ps.message || "errore").slice(0, 120);
      } else {
        st.textContent = `… ${Math.round((ps.progress || 0) * 100)}%`;
      }
    }, 1500);
  } catch (e) { st.textContent = "✗ " + e.message; }
}

/* ---------------------------------------------------------------- merge */
function updateMergeBar() {
  const sel = $$(".merge-check:checked").map(c => c.dataset.wid);
  $("#mergeBar").classList.toggle("hidden", sel.length < 1);
  $("#mergeCount").textContent = sel.length;
}

async function mergeSelected() {
  const ids = $$(".merge-check:checked").map(c => c.dataset.wid);
  if (ids.length < 1) return;
  const query = $("#mergeQuery").value.trim();
  if (!confirm(`Unire ${ids.length} finestre in un unico effetto di regia?`)) return;
  try {
    await api(`jobs/${state.jobId}/merge`, {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({windows: ids, query: query || null})});
    await refreshJob();
  } catch (e) { alert("merge fallito: " + e.message); }
}

async function nlEditWindow(wid) {
  const input = $(`#nl-${wid}`);
  const query = (input.value || "").trim();
  if (!query) { alert("Scrivi la modifica richiesta (es. 'allunga lo zoom a 6 secondi')"); return; }
  const btn = $(`#nlbtn-${wid}`);
  btn.disabled = true; btn.textContent = "…";
  try {
    await api(`jobs/${state.jobId}/window/${wid}/edit`, {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({query})});
    await refreshJob();
  } catch (e) { alert("edit fallito: " + e.message); }
  finally { btn.disabled = false; btn.textContent = "✎ Applica"; }
}

/* ---------------------------------------------------------------- log */
async function loadJobLog(scroll) {
  if (!state.jobId) return;
  try {
    const lvl = $("#logFilter").value;
    const data = await api(`jobs/${state.jobId}/log?level=${lvl}&limit=2000`);
    const el = $("#logWindow");
    const entries = data.log || [];
    // contatore + raggruppamento per run
    const runs = [...new Set(entries.map(e => e.run).filter(Boolean))];
    const nErr = entries.filter(e => e.level === "error").length;
    const nWarn = entries.filter(e => e.level === "warn").length;
    $("#logStats").textContent = `${entries.length} voci · ${runs.length ? runs.length + " run" : ""} `
      + `· ${nWarn} ⚠ ${nErr} ✗`;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    el.innerHTML = entries.map(e => {
      const t = new Date(e.t * 1000).toLocaleTimeString("it-IT");
      const dat = e.data ? `<span class="ldata">${esc(JSON.stringify(e.data).slice(0, 160))}</span>` : "";
      return `<div class="logline2 ${e.level}">
        <span class="lt">${t}</span>
        <span class="lv ${e.level}">${e.level === "info" ? "info" : e.level === "warn" ? "⚠" : "✗"}</span>
        <span class="lstage">${esc(e.stage || "")}</span>
        <span class="lmsg">${esc(e.msg)}</span>${dat}</div>`;
    }).join("");
    if (scroll || atBottom) el.scrollTop = el.scrollHeight;
  } catch (e) { $("#logWindow").textContent = "log non disponibile: " + e.message; }
}

function initLog() {
  $("#logRefresh").onclick = () => loadJobLog(true);
  $("#logFilter").onchange = () => loadJobLog(true);
  $("#logWindow").addEventListener("click", () => {
    if (!state.logOpen) { state.logOpen = true; loadJobLog(true); }
  });
}

function initStudio() {
  $("#prevBtn").onclick = () => startPreview();
  $("#mergeBtn").onclick = () => mergeSelected();
  $("#stRerun").onclick = async () => {
    if (!state.jobId) return;
    await api(`jobs/${state.jobId}/run`, {method: "POST", body: "{}"});
    refreshJob();
  };
  $("#stCancel").onclick = async () => {
    if (!state.jobId) return;
    await api(`jobs/${state.jobId}/cancel`, {method: "POST"});
    refreshJob();
  };
  $("#stRender").onclick = async () => {
    if (!state.jobId) return;
    await api(`jobs/${state.jobId}/render`, {method: "POST"});
    refreshJob();
  };
  $("#stSkill").onchange = async () => { /* applicata al prossimo re-run */ };
}

/* ---------------------------------------------------------------- skills */
function fillSkillSelect(sel, def) {
  sel.innerHTML = state.skills.filter(s => !s.invalid).map(s =>
    `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
  if (def) sel.value = def;
}

async function loadSkills() {
  const data = await api("skills");
  state.skills = data.skills;
  fillSkillSelect($("#skillSelect"));
  if (state.job) fillSkillSelect($("#stSkill"), state.job.skill);
  $("#skillsList").innerHTML = state.skills.map(s => `
    <div class="sk-row ${s.invalid ? "invalid" : ""}" data-id="${esc(s.id)}">
      <div><div class="sk-name">${esc(s.name)} <span class="hint">(${esc(s.id)})</span></div>
        <div class="sk-desc">${esc(s.description || "")}</div></div>
      <div class="hint">${(s.tools || []).length} tool</div>
    </div>`).join("");
  $$(".sk-row").forEach(r => r.onclick = () => editSkill(r.dataset.id));
}

async function editSkill(id) {
  const data = await api("skills/" + id);
  $("#skId").value = id;
  $("#skYaml").value = data.yaml;
  $("#skMsg").textContent = "";
}

function initSkills() {
  $("#newSkill").onclick = () => {
    $("#skId").value = "";
    $("#skYaml").value = `name: "Nuova skill"\nid: nuova\nscenario: base\ndescription: ""\nsystem_extra: |\n  …\ntools:\n  - zoom\n  - polaroid\nlimits:\n  max_simultaneous_overlays: 2\n  max_zoom_strength: 0.4\n  max_decisions: 8\nstyle:\n  pacing: dinamico\n  tone: neutro\npreset_effects: {}\n`;
  };
  $("#skSave").onclick = async () => {
    const id = $("#skId").value.trim() || "skill-" + Date.now();
    try {
      await api("skills", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({id, yaml: $("#skYaml").value})});
      $("#skMsg").textContent = "✓ salvata";
      $("#skId").value = id;
      loadSkills();
    } catch (e) { $("#skMsg").textContent = "✗ " + e.message; }
  };
  $("#skDelete").onclick = async () => {
    const id = $("#skId").value.trim();
    if (!id || !confirm("Eliminare la skill " + id + "?")) return;
    try { await api("skills/" + id, {method: "DELETE"}); loadSkills(); }
    catch (e) { $("#skMsg").textContent = "✗ " + e.message; }
  };
}

/* ---------------------------------------------------------------- settings */
async function loadSettings() {
  const s = await api("settings");
  const fields = [
    ["vision_model", "Modello vision (OpenRouter)"],
    ["director_model", "Modello regista (OpenRouter)"],
    ["fal_whisper_model", "Modello STT (fal.ai)"],
    ["fal_image_model", "Modello immagini (fal.ai)"],
    ["fal_tts_model", "Modello voce narrante (fal.ai)"],
    ["stt_language", "Lingua del parlato (it, en, es, fr…)"],
    ["window_len", "Durata finestra regista (s)"],
    ["window_mode", "Modalità finestre: fixed | scene"],
    ["window_min", "Finestra minima in modalità scene (s)"],
    ["window_max", "Finestra massima in modalità scene (s)"],
    ["frame_fps", "Frame al secondo analizzati"],
    ["vision_batch", "Frame per richiesta vision"],
    ["vision_concurrency", "Richieste vision in parallelo"],
    ["assets_policy", "Policy asset: auto | search | generate"],
    ["out_fps", "FPS uscita (0 = auto: fedeli alla sorgente)"],
    ["audio_offset_ms", "Offset audio ms (+ ritarda voce, − anticipa)"],
  ];
  $("#settingsForm").innerHTML = fields.map(([k, label]) => `
    <label>${label}<input data-key="${k}" value="${esc(String(s.editable[k] ?? ""))}"></label>`).join("") +
    `<div class="hint" style="grid-column:1/-1">OpenRouter: ${s.openrouter_configured ? "✓ configurato" : "✗ MANCANTE"} ·
      fal.ai: ${s.fal_configured ? "✓ configurato" : "✗ MANCANTE"}</div>`;
  $("#apiLog").innerHTML = (s.api_log || []).reverse().map(e =>
    `<div class="logline">${new Date(e.t * 1000).toLocaleTimeString("it-IT")} · ${esc(e.model)} ·
      ${e.latency}s · p:${e.prompt_tokens || "–"} c:${e.completion_tokens || "–"} · ${esc(e.kind)}</div>`
  ).join("") || "<em>nessuna chiamata registrata</em>";
}

function initSettings() {
  $("#saveSettings").onclick = async () => {
    const payload = {};
    $$("#settingsForm input").forEach(i => {
      const k = i.dataset.key, v = i.value;
      payload[k] = isNaN(+v) || v === "" ? v : +v;
    });
    try { await api("settings", {method: "PUT", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)}); loadHealth(); alert("Impostazioni salvate"); }
    catch (e) { alert("Errore: " + e.message); }
  };
}

/* ---------------------------------------------------------------- boot */
async function boot() {
  initUpload(); initStudio(); initSkills(); initSettings(); initLog();
  $$("nav button").forEach(b => b.onclick = () => show(b.dataset.view));
  try {
    const data = await api("skills");
    state.skills = data.skills;
    fillSkillSelect($("#skillSelect"));
  } catch (e) {}
  await loadHealth();
  show("projects");
}
boot();
