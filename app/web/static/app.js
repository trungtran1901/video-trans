const DEFAULT_LANGS = [
  { code: "vi", label: "Tiếng Việt" },
  { code: "en", label: "English" },
  { code: "ja", label: "日本語" },
  { code: "ko", label: "한국어" },
  { code: "zh", label: "中文" },
  { code: "fr", label: "Français" },
  { code: "es", label: "Español" },
  { code: "de", label: "Deutsch" },
];

const MIN_DUR = 0.2; // shortest allowed subtitle, in seconds

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

let selectedLangs = new Set();
let currentJobId = localStorage.getItem("vt_job");
let pollTimer = null;
let keySeq = 1;

/* ------------------------------------------------------------------ */
/* Language chips                                                      */
/* ------------------------------------------------------------------ */

function renderLangChips() {
  const container = $("langList");
  container.innerHTML = "";
  DEFAULT_LANGS.forEach((lang) => {
    const chip = el("div", "chip" + (selectedLangs.has(lang.code) ? " active" : ""), lang.label);
    chip.onclick = () => {
      if (selectedLangs.has(lang.code)) selectedLangs.delete(lang.code);
      else selectedLangs.add(lang.code);
      renderLangChips();
    };
    container.appendChild(chip);
  });
  selectedLangs.forEach((code) => {
    if (!DEFAULT_LANGS.find((l) => l.code === code)) {
      const chip = el("div", "chip active", code);
      chip.onclick = () => { selectedLangs.delete(code); renderLangChips(); };
      container.appendChild(chip);
    }
  });
}

$("btnAddLang").onclick = () => {
  const input = $("customLang");
  const val = input.value.trim().toLowerCase();
  if (val) {
    selectedLangs.add(val);
    input.value = "";
    renderLangChips();
  }
};

function getMode() {
  return document.querySelector('input[name="mode"]:checked').value;
}

/* ------------------------------------------------------------------ */
/* Time helpers                                                        */
/* ------------------------------------------------------------------ */

function formatTime(sec) {
  sec = Math.max(0, sec || 0);
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  return `${String(m).padStart(2, "0")}:${s.toFixed(3).padStart(6, "0")}`;
}

function parseTime(str) {
  str = String(str).trim().replace(",", ".");
  if (!str) return NaN;
  const parts = str.split(":");
  if (parts.length > 3) return NaN;
  let total = 0;
  for (const p of parts) {
    const n = Number(p);
    if (!isFinite(n) || n < 0) return NaN;
    total = total * 60 + n;
  }
  return total;
}

/* ------------------------------------------------------------------ */
/* Job creation & polling                                              */
/* ------------------------------------------------------------------ */

/* ---------- upload with progress ---------- */

let uploadXhr = null;

function formatBytes(n) {
  if (!isFinite(n)) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}

function formatDuration(sec) {
  if (!isFinite(sec) || sec < 0) return "–";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h}g ${String(m).padStart(2, "0")}p`;
  if (m) return `${m}p ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

function uploadWithProgress(url, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    uploadXhr = xhr;
    xhr.open("POST", url);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded, e.total); };
    xhr.onload = () => {
      uploadXhr = null;
      if (xhr.status >= 200 && xhr.status < 300) {
        try { resolve(JSON.parse(xhr.responseText)); }
        catch { reject(new Error("Phản hồi máy chủ không hợp lệ")); }
      } else {
        reject(new Error(xhr.responseText || xhr.statusText || `HTTP ${xhr.status}`));
      }
    };
    xhr.onerror = () => { uploadXhr = null; reject(new Error("Mất kết nối khi tải lên")); };
    xhr.onabort = () => { uploadXhr = null; reject(new Error("Đã hủy tải lên")); };
    xhr.send(formData);
  });
}

/** Returns a callback (loaded,total) that updates bar + speed + ETA (speed averaged over ~3s). */
function makeUploadTracker() {
  const samples = [];
  let lastPaint = 0;
  return (loaded, total) => {
    const now = performance.now();
    samples.push({ t: now, loaded });
    while (samples.length > 2 && now - samples[0].t > 3000) samples.shift();

    const done = loaded >= total;
    if (!done && now - lastPaint < 200) return;
    lastPaint = now;

    const pct = total ? (loaded / total) * 100 : 0;
    const first = samples[0];
    const dt = (now - first.t) / 1000;
    const speed = dt > 0.2 ? (loaded - first.loaded) / dt : 0;
    const eta = speed > 0 ? (total - loaded) / speed : NaN;

    $("progressFill").style.width = pct.toFixed(1) + "%";
    $("upPercent").textContent = pct.toFixed(pct >= 100 ? 0 : 1) + "%";
    $("upSpeed").textContent = speed > 0 ? formatBytes(speed) + "/s" : "–";
    $("upEta").textContent = done ? "Hoàn tất" : "Còn ~" + formatDuration(eta);
    $("upBytes").textContent = `${formatBytes(loaded)} / ${formatBytes(total)}`;
    $("jobMessage").textContent = done
      ? "Đã tải lên xong, máy chủ đang lưu file..."
      : "Đang tải video lên...";
  };
}

$("btnCancelUpload").onclick = () => { if (uploadXhr) uploadXhr.abort(); };

$("btnStart").onclick = async () => {
  const fileInput = $("videoFile");
  if (!fileInput.files.length) { alert("Vui lòng chọn video"); return; }
  if (selectedLangs.size === 0) { alert("Vui lòng chọn ít nhất một ngôn ngữ đích"); return; }

  const formData = new FormData();
  formData.append("target_langs", Array.from(selectedLangs).join(","));
  formData.append("mode", getMode());
  formData.append("context", $("context").value);
  formData.append("source_lang", $("sourceLang").value);
  formData.append("file", fileInput.files[0]); // file cuối cùng

  closeEditor();
  $("btnStart").disabled = true;
  $("jobSection").style.display = "block";
  $("resultSection").style.display = "none";
  $("progressFill").style.width = "0%";
  $("jobMessage").textContent = "Đang tải video lên...";
  $("upPercent").textContent = "0%";
  $("upSpeed").textContent = "–";
  $("upEta").textContent = "–";
  $("upBytes").textContent = `0 / ${formatBytes(fileInput.files[0].size)}`;
  $("uploadStats").style.display = "flex";

  try {
    const data = await uploadWithProgress("/api/jobs", formData, makeUploadTracker());
    currentJobId = data.job_id;
    localStorage.setItem("vt_job", currentJobId);
    $("uploadStats").style.display = "none";
    pollJob();
  } catch (e) {
    $("uploadStats").style.display = "none";
    $("jobMessage").textContent = "Lỗi: " + e.message;
    $("btnStart").disabled = false;
  }
};

async function pollJob() {
  clearTimeout(pollTimer);
  if (!currentJobId) return;

  let job;
  try {
    const res = await fetch(`/api/jobs/${currentJobId}`);
    if (res.status === 404) {
      localStorage.removeItem("vt_job");
      currentJobId = null;
      return;
    }
    job = await res.json();
  } catch (e) {
    pollTimer = setTimeout(pollJob, 3000); // network hiccup: keep trying
    return;
  }

  $("jobSection").style.display = "block";
  $("progressFill").style.width = job.progress + "%";
  $("jobMessage").textContent = job.message || job.status;

  if (job.status === "review") {
    $("btnStart").disabled = false;
    if (!(ed.open && ed.jobId === job.id)) await openEditor(job);
    loadHistory();
    return;
  }
  if (job.status === "completed") {
    renderResults(job);
    $("btnStart").disabled = false;
    loadHistory();
    return;
  }
  if (job.status === "failed") {
    $("jobMessage").textContent = "Lỗi: " + job.error;
    $("btnStart").disabled = false;
    loadHistory();
    return;
  }
  $("btnStart").disabled = true;
  pollTimer = setTimeout(pollJob, 2000);
}

/** Open any job from history: edit if it has segments, otherwise follow its progress. */
async function openJob(id, { editCompleted = true } = {}) {
  const res = await fetch(`/api/jobs/${id}`);
  if (!res.ok) return;
  const job = await res.json();
  currentJobId = id;
  localStorage.setItem("vt_job", id);
  closeEditor();
  $("jobSection").style.display = "block";
  $("progressFill").style.width = job.progress + "%";
  $("jobMessage").textContent = job.message || job.status;

  if (job.status === "completed") {
    renderResults(job);
    if (editCompleted) await openEditor(job);
    return;
  }
  if (job.status === "review" || job.status === "failed") {
    const opened = await openEditor(job);
    if (opened && job.status === "failed") {
      setStatus("Lần xuất trước bị lỗi: " + (job.error || "không rõ") + ". Hãy kiểm tra rồi xuất lại.", true);
    }
    if (!opened) pollJob();
    return;
  }
  pollJob();
}

function renderResults(job) {
  $("resultSection").style.display = "block";
  const container = $("jobResults");
  container.innerHTML = "";
  Object.entries(job.outputs || {}).forEach(([lang, files]) => {
    const row = el("div", "result-item");
    row.appendChild(el("span", null, lang.toUpperCase()));
    const links = el("span");
    Object.keys(files).forEach((kind) => {
      const a = el("a", "download-link", kind === "srt" ? "Tải SRT" : "Tải video");
      a.href = `/api/jobs/${job.id}/download/${lang}/${kind}`;
      links.appendChild(a);
    });
    row.appendChild(links);
    container.appendChild(row);
  });
}

$("btnReedit").onclick = () => currentJobId && openJob(currentJobId);

async function loadHistory() {
  const res = await fetch("/api/jobs");
  const jobs = await res.json();
  const container = $("jobHistory");
  container.innerHTML = "";
  if (!jobs.length) container.appendChild(el("p", "muted", "Chưa có job nào."));
  jobs.forEach((job) => {
    const row = el("div", "history-item");
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.title = "Bấm để mở";
    row.appendChild(el("span", null, `${job.input_filename} — ${job.target_langs.join(", ")}`));

    const right = el("span", "history-right");
    right.appendChild(el("span", `status-badge status-${job.status}`, job.status));

    const busy = job.status === "running" || job.status === "rendering";
    const bDel = el("button", "mini danger", "Xóa");
    bDel.title = busy ? "Đang xử lý, không thể xóa" : "Xóa job này";
    bDel.disabled = busy;
    bDel.onclick = (e) => { e.stopPropagation(); deleteJob(job.id, job.input_filename); };
    right.appendChild(bDel);

    row.appendChild(right);
    const open = () => openJob(job.id);
    row.onclick = open;
    row.onkeydown = (e) => { if (e.key === "Enter") open(); };
    container.appendChild(row);
  });
}

async function deleteJob(id, name) {
  if (!confirm(`Xóa job "${name}"? Toàn bộ video và file đã xuất của job này sẽ bị xóa vĩnh viễn.`)) return;
  const res = await fetch(`/api/jobs/${id}`, { method: "DELETE" });
  if (!res.ok) {
    alert("Không xóa được: " + (await res.text()));
    return;
  }
  if (currentJobId === id) {
    currentJobId = null;
    localStorage.removeItem("vt_job");
    closeEditor();
    $("jobSection").style.display = "none";
    $("resultSection").style.display = "none";
  }
  loadHistory();
}

/* ------------------------------------------------------------------ */
/* Subtitle editor                                                     */
/* ------------------------------------------------------------------ */

const ed = {
  open: false,
  jobId: null,
  lang: null,
  data: {},        // lang -> [{k,start,end,text}]
  source: [],      // original speech segments (read only)
  undoStack: [],   // one chronological history: {t:"segs",lang,snap} | {t:"cuts",snap}
  redoStack: [],
  cuts: [],        // removed parts of the source timeline: [{k,start,end}]
  cutsDirty: false,
  cutSel: null,
  markIn: null,
  markOut: null,
  skipCuts: true,
  dirty: new Set(),
  selKey: null,
  activeKey: null,
  caretKey: null,
  caretPos: null,
  duration: 0,
  total: 0,
  pps: 80,         // timeline pixels per second
  regions: [],     // [{x,y,w,h,mode}] as fractions of the video frame
  regionsDirty: false,
  subtitleStyle: { font_size: 28, x: 0.5, y: 0.9 },
  subtitleStyleDirty: false,
  watermark: { enabled: false, type: "text", text: "", image: "", opacity: 0.5, x: 0.82, y: 0.86, font_size: 24, scale: 0.16 },
  watermarkDirty: false,
  wmImageVer: 0,
  mode: "",
  dubDirty: false,
  dub: { state: "idle", hasAudio: false, upToDate: false, audioVersion: "", done: 0, total: 0, error: "", listen: true, volume: 1, origVolume: 0.25, duck: true, duckLevel: 0.3, loadedKey: "" },
};

const V = $("editorVideo");
const cur = () => ed.data[ed.lang] || [];
const byKey = (k) => cur().find((s) => s.k === k) || null;
const selSeg = () => byKey(ed.selKey);
const segAt = (t) => cur().find((s) => t >= s.start && t < s.end) || null;
const rowEl = (k) => $("reviewList").querySelector(`[data-k="${k}"]`);
const blockEl = (k) => $("tlTrack").querySelector(`[data-k="${k}"]`);
const isTyping = () => /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
const anyDirty = () => ed.dirty.size || ed.regionsDirty || ed.subtitleStyleDirty || ed.watermarkDirty || ed.dubDirty || ed.cutsDirty;

let noticeTimer = null;
function setStatus(text, isError = false) {
  const s = $("editorStatus");
  s.textContent = text;
  s.classList.toggle("error", isError);
}
function notify(text) {
  setStatus(text, true);
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => setStatus(anyDirty() ? "Có thay đổi chưa lưu" : ""), 3500);
}

/* ---------- history (one chronological stack: subtitle edits AND video cuts) ---------- */

function trimHistory() {
  if (ed.undoStack.length > 200) ed.undoStack.shift();
}
function pushUndo(snap = JSON.stringify(cur())) {
  ed.undoStack.push({ t: "segs", lang: ed.lang, snap });
  trimHistory();
  ed.redoStack = [];
  updateToolbarState();
}
function pushCutUndo(snap = JSON.stringify(ed.cuts)) {
  ed.undoStack.push({ t: "cuts", snap });
  trimHistory();
  ed.redoStack = [];
  updateToolbarState();
}
function markDirty() {
  ed.dirty.add(ed.lang);
  ed._regs = null;
  setStatus("Có thay đổi chưa lưu");
  renderDubPanel();
}
function stepHistory(from, to) {
  const e = from.pop();
  if (!e) return;
  if (e.t === "cuts") {
    to.push({ t: "cuts", snap: JSON.stringify(ed.cuts) });
    ed.cuts = JSON.parse(e.snap);
    ed.cutSel = null;
    cutsChanged();
  } else {
    to.push({ t: "segs", lang: e.lang, snap: JSON.stringify(ed.data[e.lang] || []) });
    ed.data[e.lang] = JSON.parse(e.snap);
    if (e.lang !== ed.lang) {
      ed.lang = e.lang;
      ed.selKey = null;
      ed.activeKey = null;
      renderTabs();
      onDubLangChanged();
    }
    markDirty();
    renderAll();
  }
  updateToolbarState();
}
function undo() { stepHistory(ed.undoStack, ed.redoStack); }
function redo() { stepHistory(ed.redoStack, ed.undoStack); }
/** Run a structural change with undo support, then re-sort and re-render. */
function mutate(fn) {
  pushUndo();
  fn();
  cur().sort((a, b) => a.start - b.start);
  markDirty();
  renderAll();
}

/* ---------- open / close ---------- */

async function openEditor(job) {
  const res = await fetch(`/api/jobs/${job.id}/segments`);
  if (!res.ok) return false;
  const all = await res.json();

  const data = {};
  for (const [lang, segs] of Object.entries(all)) {
    if (lang.startsWith("_")) continue;
    data[lang] = segs.map((s) => ({ k: keySeq++, start: +s.start, end: +s.end, text: s.text || "" }));
  }
  const langs = Object.keys(data);
  if (!langs.length) return false;

  ed.jobId = job.id;
  ed.data = data;
  ed.source = (all._source || []).map((s) => ({ start: +s.start, end: +s.end, text: s.text || "" }));
  ed.undoStack = [];
  ed.redoStack = [];
  ed.cuts = (job.cuts || []).map((c) => ({ k: keySeq++, start: +c.start, end: +c.end }));
  ed.cutsDirty = false;
  ed.cutSel = null;
  ed.markIn = null;
  ed.markOut = null;
  ed.dirty = new Set();
  ed.lang = langs[0];
  ed.duration = job.total_duration || 0;
  ed.selKey = null;
  ed.activeKey = null;
  ed.caretKey = null;
  ed.regions = (job.regions || []).map((r) => ({ ...r }));
  ed.regionsDirty = false;
  ed.subtitleStyle = { font_size: 28, x: 0.5, y: 0.9, ...(job.subtitle_style || {}) };
  ed.subtitleStyleDirty = false;
  ed.watermark = {
    enabled: false, type: "text", text: "", image: "", opacity: 0.5,
    x: 0.82, y: 0.86, font_size: 24, scale: 0.16,
    ...(job.watermark || {}),
  };
  ed.watermarkDirty = false;
  ed.wmImageVer = Date.now();
  ed.mode = job.mode || "";
  resetDub();
  const jd = job.dub || {};
  ed.dub.listen = !!jd.enabled || job.mode === "dubbing";
  ed.dub.origVolume = jd.orig_volume ?? 0.25;
  ed.dub.volume = jd.dub_volume ?? 1;
  ed.dub.duck = jd.duck ?? true;
  ed.dub.duckLevel = jd.duck_level ?? 0.3;
  ed.dub.voices = { ...(jd.voices || {}) };
  ed.dubDirty = false;
  ed.open = true;

  const url = `/api/jobs/${job.id}/video`;
  if (V.getAttribute("src") !== url) {
    $("videoNote").textContent = "";
    V.src = url;
  }

  $("reviewSection").style.display = "block";
  $("app").classList.add("wide");
  document.querySelectorAll("details.setup-acc").forEach((d) => { d.open = false; }); // give the editor room
  setStatus("");
  renderTabs();
  renderAll();
  layoutRegionLayer();
  renderRegions();
  renderSubtitleStylePanel();
  renderWatermarkPanel();
  renderDubPanel();
  renderVoiceSelect();
  refreshDubStatus();
  $("reviewSection").scrollIntoView({ behavior: "smooth", block: "start" });
  return true;
}

function closeEditor() {
  ed.open = false;
  V.pause();
  $("reviewSection").style.display = "none";
  $("app").classList.remove("wide");
  wmOverlay.style.display = "none";
  clearTimeout(dubPollTimer);
  stopVoicePreview();
  dubAudio.pause();
  V.muted = false;
  renderDubPanel();
}

/* ---------- rendering ---------- */

function renderTabs() {
  const tabs = $("reviewTabs");
  tabs.innerHTML = "";
  Object.keys(ed.data).forEach((lang) => {
    const chip = el("div", "chip" + (lang === ed.lang ? " active" : ""), lang.toUpperCase());
    chip.dataset.lang = lang;
    chip.onclick = () => {
      ed.lang = lang;
      ed.selKey = null;
      ed.activeKey = null;
      renderTabs();
      renderAll();
      onDubLangChanged();
    };
    tabs.appendChild(chip);
  });
}

function renderAll() {
  ed._regs = null;
  renderList();
  renderTimeline();
  refreshActive(true);
  updateToolbarState();
}

function sourceTextFor(seg) {
  if (!ed.source.length) return "";
  return ed.source
    .filter((s) => Math.min(s.end, seg.end) - Math.max(s.start, seg.start) > 0.05)
    .map((s) => s.text)
    .join(" ");
}

function renderList() {
  const box = $("reviewList");
  box.innerHTML = "";
  const segs = cur();
  if (!segs.length) {
    box.appendChild(el("p", "muted empty", "Chưa có đoạn phụ đề nào. Đặt đầu phát vào vị trí cần thêm rồi bấm “Thêm đoạn”."));
    return;
  }

  segs.forEach((seg, i) => {
    const row = el("div", "segment-row");
    row.dataset.k = seg.k;

    const head = el("div", "seg-head");
    head.appendChild(el("span", "seg-idx", String(i + 1)));
    head.appendChild(timeInput(seg, "start"));
    head.appendChild(el("span", "seg-dash", "–"));
    head.appendChild(timeInput(seg, "end"));

    const tools = el("span", "seg-tools");
    const ta = el("textarea", "segment-text");
    ta.value = seg.text;
    ta.rows = 2;
    ta.setAttribute("aria-label", `Nội dung đoạn ${i + 1}`);

    const bSplit = el("button", "mini", "Tách");
    bSplit.title = "Tách tại con trỏ (và đầu phát nếu đang nằm trong đoạn)";
    bSplit.onclick = () => splitSegment(seg, ta.selectionStart);
    const bMerge = el("button", "mini", "Gộp");
    bMerge.title = "Gộp với đoạn sau";
    bMerge.onclick = () => mergeNext(seg);
    const bDel = el("button", "mini danger", "Xóa");
    bDel.onclick = () => { ed.selKey = seg.k; deleteSelected(); };
    tools.append(bSplit, bMerge, bDel);
    head.appendChild(tools);
    row.appendChild(head);

    const trackCaret = () => { ed.caretKey = seg.k; ed.caretPos = ta.selectionStart; };
    ta.onfocus = () => { ta._base = JSON.stringify(cur()); selectSeg(seg.k, { seek: true }); trackCaret(); };
    ta.oninput = () => {
      seg.text = ta.value;
      markDirty();
      const lbl = blockEl(seg.k)?.querySelector(".lbl");
      if (lbl) lbl.textContent = seg.text;
      refreshActive(true);
    };
    ta.onchange = () => { if (ta._base) { pushUndo(ta._base); ta._base = null; } };
    ta.onkeyup = trackCaret;
    ta.onclick = trackCaret;
    ta.onkeydown = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        splitSegment(seg, ta.selectionStart);
      }
    };
    row.appendChild(ta);

    const src = sourceTextFor(seg);
    if (src) row.appendChild(el("div", "seg-src", "Gốc: " + src));

    row.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      selectSeg(seg.k, { seek: true });
    });
    box.appendChild(row);
  });
  markClasses();
}

function timeInput(seg, field) {
  const inp = el("input", "t-in");
  inp.type = "text";
  inp.value = formatTime(seg[field]);
  inp.dataset.field = field;
  inp.setAttribute("aria-label", field === "start" ? "Thời điểm bắt đầu" : "Thời điểm kết thúc");
  inp.onchange = () => {
    const v = parseTime(inp.value);
    const snap = JSON.stringify(cur());
    const ok = !isNaN(v) &&
      applyTimes(seg, field === "start" ? v : seg.start, field === "end" ? v : seg.end);
    if (!ok) {
      inp.value = formatTime(seg[field]);
      notify("Thời gian không hợp lệ hoặc quá ngắn (tối thiểu 0.2 giây).");
      return;
    }
    pushUndo(snap);
    markDirty();
    positionBlock(seg);
    syncRowTimes(seg);
    refreshActive(true);
  };
  return inp;
}

/** Set start/end, clamped so the segment never overlaps its neighbours. */
function applyTimes(seg, start, end) {
  const segs = cur();
  const i = segs.indexOf(seg);
  const prev = segs[i - 1];
  const next = segs[i + 1];
  const lo = prev ? prev.end : 0;
  const hi = next ? next.start : (ed.duration || Infinity);
  start = Math.max(lo, start);
  end = Math.min(hi, end);
  if (end - start < MIN_DUR) return false;
  seg.start = start;
  seg.end = end;
  return true;
}

function syncRowTimes(seg) {
  const row = rowEl(seg.k);
  if (!row) return;
  row.querySelectorAll(".t-in").forEach((inp) => { inp.value = formatTime(seg[inp.dataset.field]); });
}

function markClasses() {
  document.querySelectorAll("#tlTrack .tl-block, #reviewList .segment-row").forEach((n) => {
    const k = +n.dataset.k;
    n.classList.toggle("selected", k === ed.selKey);
    n.classList.toggle("playing", k === ed.activeKey);
    const sg = byKey(k);
    n.classList.toggle("cut-out", !!sg && isCutAt((sg.start + sg.end) / 2));
  });
}

function scrollRowIntoView(k) {
  const row = rowEl(k);
  const box = $("reviewList");
  if (!row) return;
  if (row.offsetTop < box.scrollTop) box.scrollTop = row.offsetTop - 6;
  else if (row.offsetTop + row.offsetHeight > box.scrollTop + box.clientHeight)
    box.scrollTop = row.offsetTop + row.offsetHeight - box.clientHeight + 6;
}

function selectSeg(k, opt = {}) {
  ed.selKey = k;
  if (ed.cutSel != null) { ed.cutSel = null; markCutClasses(); }
  const seg = selSeg();
  markClasses();
  if (seg && opt.seek) {
    const t = V.currentTime || 0;
    if (t < seg.start || t >= seg.end) seekTo(seg.start);
  }
  if (seg && opt.scrollList) scrollRowIntoView(k);
  updateToolbarState();
}

function updateToolbarState() {
  const seg = selSeg();
  $("btnUndo").disabled = !ed.undoStack.length;
  $("btnRedo").disabled = !ed.redoStack.length;
  $("btnSplit").disabled = !cur().length;
  $("btnMerge").disabled = !seg || cur().indexOf(seg) >= cur().length - 1;
  $("btnDelete").disabled = !seg;
  $("btnCutSeg").disabled = !seg;
}

/* ---------- editing operations ---------- */

function guessSplitPos(text, ratio) {
  const ideal = Math.round(text.length * ratio);
  for (let d = 0; d <= 15; d++) {
    if (/\s/.test(text[ideal + d] || "")) return ideal + d;
    if (/\s/.test(text[ideal - d] || "")) return ideal - d;
  }
  return ideal;
}

function splitSegment(seg, caret) {
  if (seg.end - seg.start < MIN_DUR * 2) { notify("Đoạn quá ngắn để tách."); return; }
  const text = seg.text;
  const len = text.length;
  const t = V.currentTime || 0;
  const inside = t > seg.start + MIN_DUR && t < seg.end - MIN_DUR;
  const hasCaret = caret != null && caret > 0 && caret < len;

  let splitT, pos;
  if (inside) {
    splitT = t;
    pos = hasCaret ? caret : guessSplitPos(text, (t - seg.start) / (seg.end - seg.start));
  } else {
    pos = hasCaret ? caret : guessSplitPos(text, 0.5);
    splitT = seg.start + (seg.end - seg.start) * (len ? pos / len : 0.5);
  }
  splitT = clamp(splitT, seg.start + MIN_DUR, seg.end - MIN_DUR);

  const left = text.slice(0, pos).trim();
  const right = text.slice(pos).trim();
  const tail = { k: keySeq++, start: splitT, end: seg.end, text: right };
  mutate(() => {
    seg.end = splitT;
    seg.text = left;
    cur().push(tail);
    ed.selKey = tail.k;
  });
  scrollRowIntoView(tail.k);
}

function splitTarget() {
  const t = V.currentTime || 0;
  const sel = selSeg();
  if (sel && t >= sel.start - 0.01 && t <= sel.end + 0.01) return sel;
  return segAt(t) || sel;
}

function doSplit() {
  const seg = splitTarget();
  if (!seg) { notify("Hãy chọn một đoạn hoặc đặt đầu phát vào bên trong đoạn cần tách."); return; }
  splitSegment(seg, ed.caretKey === seg.k ? ed.caretPos : null);
}

function mergeNext(seg) {
  const segs = cur();
  const i = segs.indexOf(seg);
  const next = segs[i + 1];
  if (!next) { notify("Đây là đoạn cuối, không có đoạn sau để gộp."); return; }
  mutate(() => {
    seg.text = (seg.text + " " + next.text).trim();
    seg.end = next.end;
    segs.splice(i + 1, 1);
    ed.selKey = seg.k;
  });
}

function deleteSelected() {
  const seg = selSeg();
  if (!seg) { notify("Chưa chọn đoạn nào."); return; }
  const segs = cur();
  const i = segs.indexOf(seg);
  mutate(() => {
    segs.splice(i, 1);
    const n = segs[Math.min(i, segs.length - 1)];
    ed.selKey = n ? n.k : null;
  });
}

function addSegment() {
  const segs = cur();
  let start = V.currentTime || 0;
  const over = segAt(start);
  if (over) start = over.end;
  const next = segs.find((s) => s.start > start);
  const limit = next ? next.start : (ed.total || start + 2);
  const end = Math.min(start + 2, limit);
  if (end - start < MIN_DUR) { notify("Không đủ khoảng trống tại đây. Hãy dời đầu phát sang chỗ trống."); return; }
  const seg = { k: keySeq++, start, end, text: "" };
  mutate(() => { segs.push(seg); ed.selKey = seg.k; });
  seekTo(start);
  const ta = rowEl(seg.k)?.querySelector("textarea");
  if (ta) { scrollRowIntoView(seg.k); ta.focus(); }
}

$("btnUndo").onclick = undo;
$("btnRedo").onclick = redo;
$("btnSplit").onclick = doSplit;
$("btnMerge").onclick = () => { const s = selSeg(); if (s) mergeNext(s); };
$("btnAdd").onclick = addSegment;
$("btnDelete").onclick = deleteSelected;

/* ---------- timeline ---------- */

function positionBlock(seg) {
  const b = blockEl(seg.k);
  if (!b) return;
  b.style.left = seg.start * ed.pps + "px";
  b.style.width = Math.max(4, (seg.end - seg.start) * ed.pps) + "px";
}

function renderTimeline() {
  const segs = cur();
  const maxEnd = Math.max(ed.duration || 0, ...segs.map((s) => s.end), ...ed.source.map((s) => s.end), 1);
  ed.total = ed.duration || maxEnd;
  $("timeline").style.width = Math.ceil(maxEnd * ed.pps) + 60 + "px";

  const ruler = $("tlRuler");
  ruler.innerHTML = "";
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  const step = steps.find((s) => s * ed.pps >= 70) || 600;
  for (let t = 0; t <= maxEnd; t += step) {
    const m = Math.floor(t / 60);
    const s = t % 60;
    const label = `${m}:${step < 1 ? s.toFixed(1).padStart(4, "0") : String(Math.floor(s)).padStart(2, "0")}`;
    const tick = el("div", "tick", label);
    tick.style.left = t * ed.pps + "px";
    ruler.appendChild(tick);
  }

  const src = $("tlSrc");
  src.innerHTML = "";
  ed.source.forEach((s) => {
    const b = el("div", "src-block", s.text);
    b.style.left = s.start * ed.pps + "px";
    b.style.width = Math.max(4, (s.end - s.start) * ed.pps) + "px";
    b.title = s.text;
    src.appendChild(b);
  });

  const track = $("tlTrack");
  track.innerHTML = "";
  segs.forEach((seg) => {
    const b = el("div", "tl-block");
    b.dataset.k = seg.k;
    b.title = seg.text;
    b.appendChild(el("span", "lbl", seg.text));
    b.appendChild(el("i", "h hl"));
    b.appendChild(el("i", "h hr"));
    track.appendChild(b);
    positionBlock(seg);
  });
  renderCuts();
  markClasses();
  updatePlayhead();
}

function setZoom(p) {
  const sc = $("timelineScroll");
  const t = V.currentTime || 0;
  const offset = t * ed.pps - sc.scrollLeft;
  ed.pps = p;
  renderTimeline();
  sc.scrollLeft = t * ed.pps - offset;
}
$("zoom").oninput = (e) => setZoom(+e.target.value);
$("timelineScroll").addEventListener("wheel", (e) => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  const z = clamp(ed.pps * (e.deltaY < 0 ? 1.15 : 1 / 1.15), 20, 300);
  $("zoom").value = z;
  setZoom(z);
}, { passive: false });

function timeFromEvent(e) {
  const rect = $("timeline").getBoundingClientRect();
  return clamp((e.clientX - rect.left) / ed.pps, 0, ed.total || Infinity);
}

function seekTo(t) {
  V.currentTime = clamp(t, 0, ed.total || Infinity);
  updatePlayhead();
}

let drag = null;
let scrubbing = false;

$("timeline").addEventListener("pointerdown", (e) => {
  const cutBlk = e.target.closest(".cut-block");
  if (cutBlk) { startCutDrag(e, cutBlk); return; }
  if (e.target.closest("#tlCuts")) { startCutCreate(e); return; }
  const blk = e.target.closest(".tl-block");
  if (!blk) {
    scrubbing = true;
    seekTo(timeFromEvent(e));
    return;
  }
  const seg = byKey(+blk.dataset.k);
  if (!seg) return;
  e.preventDefault();
  const segs = cur();
  const i = segs.indexOf(seg);
  const mode = e.target.classList.contains("hl") ? "l" : e.target.classList.contains("hr") ? "r" : "m";
  if (mode === "m") seekTo(timeFromEvent(e));
  selectSeg(seg.k, { scrollList: true });
  drag = {
    seg, mode,
    x0: e.clientX,
    orig: { start: seg.start, end: seg.end },
    lo: i > 0 ? segs[i - 1].end : 0,
    hi: i < segs.length - 1 ? segs[i + 1].start : (ed.duration || Infinity),
    base: JSON.stringify(segs),
    moved: false,
  };
  blk.classList.add("dragging");
});

window.addEventListener("pointermove", (e) => {
  if (scrubbing) { seekTo(timeFromEvent(e)); return; }
  if (!drag) return;
  if (Math.abs(e.clientX - drag.x0) > 2) drag.moved = true;
  if (!drag.moved) return;

  const { seg, orig, lo, mode } = drag;
  const hi = Math.max(drag.hi, orig.end);
  const dt = (e.clientX - drag.x0) / ed.pps;
  if (mode === "m") {
    const d = clamp(dt, lo - orig.start, hi - orig.end);
    seg.start = orig.start + d;
    seg.end = orig.end + d;
  } else if (mode === "l") {
    seg.start = clamp(orig.start + dt, lo, orig.end - MIN_DUR);
  } else {
    seg.end = clamp(orig.end + dt, orig.start + MIN_DUR, hi);
  }
  positionBlock(seg);
  syncRowTimes(seg);
});

window.addEventListener("pointerup", () => {
  scrubbing = false;
  if (!drag) return;
  blockEl(drag.seg.k)?.classList.remove("dragging");
  if (drag.moved) {
    pushUndo(drag.base);
    markDirty();
    refreshActive(true);
  }
  drag = null;
});

/* ---------- playback sync ---------- */

function updateOverlay(seg) {
  $("subOverlay").textContent = seg ? seg.text : "";
}

function refreshActive(force) {
  if (!ed.open) return;
  const seg = segAt(V.currentTime || 0);
  const k = seg ? seg.k : null;
  if (!force && k === ed.activeKey) return;
  ed.activeKey = k;
  markClasses();
  updateOverlay(seg);
  if (!V.paused && k != null && !isTyping()) scrollRowIntoView(k);
}

function updatePlayhead() {
  if (!ed.open) return;
  const t = V.currentTime || 0;
  const x = t * ed.pps;
  $("playhead").style.left = x + "px";
  $("timeLabel").textContent = `${formatTime(t)} / ${formatTime(ed.total)}`;

  if (!drag && !scrubbing) {
    const sc = $("timelineScroll");
    if (x < sc.scrollLeft + 20 || x > sc.scrollLeft + sc.clientWidth - 40) {
      sc.scrollLeft = Math.max(0, x - sc.clientWidth * 0.3);
    }
  }
  refreshActive(false);
}

function playLoop() {
  skipCuts();
  updatePlayhead();
  if (!V.paused) syncDub(false);
  if (!V.paused && !V.ended) requestAnimationFrame(playLoop);
}

V.addEventListener("loadedmetadata", () => {
  if (isFinite(V.duration) && V.duration > 0) {
    ed.duration = V.duration;
    if (ed.open) renderTimeline();
  }
});
["timeupdate", "seeked", "seeking"].forEach((ev) => V.addEventListener(ev, updatePlayhead));
V.addEventListener("play", () => requestAnimationFrame(playLoop));
V.addEventListener("error", () => {
  $("videoNote").textContent = "Trình duyệt không phát được video này. Bạn vẫn chỉnh sửa được trên timeline và danh sách.";
});

/* ---------- keyboard ---------- */

document.addEventListener("keydown", (e) => {
  if (!ed.open || $("settingsModal").classList.contains("open")) return;
  const key = e.key.toLowerCase();
  const mod = e.ctrlKey || e.metaKey;

  if (mod && key === "s") { e.preventDefault(); saveEditor(); return; }
  if (mod && key === "b") { e.preventDefault(); doSplit(); return; }
  if (isTyping()) return;

  if (mod && key === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
  if (mod && key === "y") { e.preventDefault(); redo(); return; }
  if (mod || e.altKey) return;

  const tag = document.activeElement?.tagName;
  if (e.key === " ") {
    if (tag === "VIDEO" || tag === "BUTTON" || tag === "SUMMARY") return; // native behaviour
    e.preventDefault();
    V.paused ? V.play() : V.pause();
  } else if (key === "s") {
    e.preventDefault();
    doSplit();
  } else if (e.key === "Delete" || e.key === "Backspace") {
    e.preventDefault();
    if (ed.cutSel != null) removeCut(ed.cutSel); else deleteSelected();
  } else if (key === "i") {
    e.preventDefault();
    markPoint("in");
  } else if (key === "o") {
    e.preventDefault();
    markPoint("out");
  } else if (key === "x") {
    e.preventDefault();
    cutRange();
  } else if (e.key === "ArrowLeft") {
    e.preventDefault();
    seekTo((V.currentTime || 0) - (e.shiftKey ? 1 : 0.1));
  } else if (e.key === "ArrowRight") {
    e.preventDefault();
    seekTo((V.currentTime || 0) + (e.shiftKey ? 1 : 0.1));
  }
});

window.addEventListener("beforeunload", (e) => {
  if (ed.open && anyDirty()) { e.preventDefault(); e.returnValue = ""; }
});

/* ---------- save / export ---------- */

async function saveEditor() {
  if (!anyDirty()) { setStatus("Đã lưu"); return true; }
  setStatus("Đang lưu...");
  const selIndex = cur().indexOf(selSeg());

  if (ed.cutsDirty) {
    try {
      const res = await fetch(`/api/jobs/${ed.jobId}/cuts`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cuts: ed.cuts.map(({ start, end }) => ({ start, end })) }),
      });
      if (!res.ok) { setStatus("Không lưu được đoạn cắt: " + (await res.text()), true); return false; }
      const out = await res.json(); // server merges overlapping ranges
      ed.cuts = out.cuts.map((c) => ({ k: keySeq++, start: c.start, end: c.end }));
      ed.cutSel = null;
      ed.cutsDirty = false;
      renderCuts();
    } catch (e) {
      setStatus("Không lưu được: mất kết nối tới máy chủ.", true);
      return false;
    }
  }

  if (ed.regionsDirty) {
    try {
      const res = await fetch(`/api/jobs/${ed.jobId}/regions`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ regions: ed.regions }),
      });
      if (!res.ok) { setStatus("Không lưu được vùng che: " + (await res.text()), true); return false; }
      ed.regions = (await res.json()).regions;
      ed.regionsDirty = false;
      renderRegions();
    } catch (e) {
      setStatus("Không lưu được: mất kết nối tới máy chủ.", true);
      return false;
    }
  }

  if (ed.subtitleStyleDirty) {
    try {
      const res = await fetch(`/api/jobs/${ed.jobId}/subtitle-style`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(ed.subtitleStyle),
      });
      if (!res.ok) { setStatus("Không lưu được cỡ chữ/vị trí: " + (await res.text()), true); return false; }
      ed.subtitleStyle = (await res.json()).subtitle_style;
      ed.subtitleStyleDirty = false;
      renderSubtitleStylePanel();
    } catch (e) {
      setStatus("Không lưu được: mất kết nối tới máy chủ.", true);
      return false;
    }
  }

  if (ed.dubDirty) {
    try {
      const res = await fetch(`/api/jobs/${ed.jobId}/dub-settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled: ed.dub.listen, orig_volume: ed.dub.origVolume, dub_volume: ed.dub.volume,
          duck: ed.dub.duck, duck_level: ed.dub.duckLevel, voices: ed.dub.voices,
        }),
      });
      if (!res.ok) { setStatus("Không lưu được lựa chọn audio: " + (await res.text()), true); return false; }
      ed.dub.voiceStale = false;
      ed.dubDirty = false;
    } catch (e) {
      setStatus("Không lưu được: mất kết nối tới máy chủ.", true);
      return false;
    }
  }

  if (ed.watermarkDirty) {
    const w = ed.watermark;
    try {
      const res = await fetch(`/api/jobs/${ed.jobId}/watermark`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled: w.enabled, type: w.type, text: w.text, opacity: w.opacity,
          x: w.x, y: w.y, font_size: w.font_size, scale: w.scale,
        }),
      });
      if (!res.ok) { setStatus("Không lưu được hình mờ: " + (await res.text()), true); return false; }
      ed.watermark = { ...ed.watermark, ...(await res.json()).watermark };
      ed.watermarkDirty = false;
      renderWatermarkPanel();
    } catch (e) {
      setStatus("Không lưu được: mất kết nối tới máy chủ.", true);
      return false;
    }
  }

  for (const lang of [...ed.dirty]) {
    const payload = ed.data[lang].map(({ start, end, text }) => ({ start, end, text }));
    let res;
    try {
      res = await fetch(`/api/jobs/${ed.jobId}/segments/${lang}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ segments: payload }),
      });
    } catch (e) {
      setStatus("Không lưu được: mất kết nối tới máy chủ.", true);
      return false;
    }
    if (!res.ok) {
      setStatus("Không lưu được: " + (await res.text()), true);
      return false;
    }
    const out = await res.json(); // server drops empty items and re-numbers
    ed.data[lang] = out.segments.map((s) => ({ k: keySeq++, start: s.start, end: s.end, text: s.text }));
    ed.dirty.delete(lang);
  }
  const keep = cur()[selIndex];
  ed.selKey = keep ? keep.k : null;
  renderAll();
  setStatus("Đã lưu");
  refreshDubStatus();
  return true;
}

$("btnSaveEdits").onclick = saveEditor;

$("btnCloseEditor").onclick = async () => {
  if (anyDirty() && !(await saveEditor())) return;
  closeEditor();
};

$("btnExport").onclick = async () => {
  const btn = $("btnExport");
  btn.disabled = true;
  try {
    if (!(await saveEditor())) return;
    const res = await fetch(`/api/jobs/${ed.jobId}/export`, { method: "POST" });
    if (!res.ok) { setStatus("Không xuất được: " + (await res.text()), true); return; }
    currentJobId = ed.jobId;
    localStorage.setItem("vt_job", currentJobId);
    closeEditor();
    $("resultSection").style.display = "none";
    $("jobSection").style.display = "block";
    $("progressFill").style.width = "90%";
    $("jobMessage").textContent = "Đang render video...";
    pollJob();
  } finally {
    btn.disabled = false;
  }
};

/* ------------------------------------------------------------------ */
/* LLM settings modal                                                  */
/* ------------------------------------------------------------------ */

/* ---------- logo / old subtitle regions ---------- */

const REGION_LABELS = { blur: "Làm mờ", box: "Che đặc (đen)", delogo: "Xóa logo (nội suy)" };
const regionLayer = $("regionLayer");
let drawMode = false;
let draft = null;

/** Match the overlay layer to the visible video frame (the <video> element can be letterboxed). */
function layoutRegionLayer() {
  const vw = V.videoWidth, vh = V.videoHeight;
  if (!vw || !vh) { regionLayer.style.display = "none"; return; }
  const cw = V.clientWidth, ch = V.clientHeight;
  const scale = Math.min(cw / vw, ch / vh);
  const w = vw * scale, h = vh * scale;
  regionLayer.style.display = "block";
  regionLayer.style.width = w + "px";
  regionLayer.style.height = h + "px";
  regionLayer.style.left = V.offsetLeft + (cw - w) / 2 + "px";
  regionLayer.style.top = V.offsetTop + (ch - h) / 2 + "px";
  positionSubOverlay();
  positionWmOverlay();
}
new ResizeObserver(layoutRegionLayer).observe(V);
V.addEventListener("loadedmetadata", layoutRegionLayer);

/* ---------- subtitle style (font size + drag position) ---------- */

const subOverlay = $("subOverlay");

function renderSubtitleStylePanel() {
  $("subFontSize").value = ed.subtitleStyle.font_size;
  $("subFontSizeVal").textContent = ed.subtitleStyle.font_size;
  positionSubOverlay();
}

/** Keep the on-screen overlay's font size and position visually matching the exported burn-in. */
function positionSubOverlay() {
  if (!ed.open) return;
  const frameW = parseFloat(regionLayer.style.width) || 0;
  const realW = V.videoWidth || frameW || 1;
  const scale = frameW ? frameW / realW : 1;
  subOverlay.style.fontSize = Math.max(8, ed.subtitleStyle.font_size * scale) + "px";
  subOverlay.style.left = ed.subtitleStyle.x * 100 + "%";
  subOverlay.style.top = ed.subtitleStyle.y * 100 + "%";
}

$("subFontSize").oninput = () => {
  const v = +$("subFontSize").value;
  ed.subtitleStyle.font_size = v;
  $("subFontSizeVal").textContent = v;
  ed.subtitleStyleDirty = true;
  setStatus("Có thay đổi chưa lưu");
  positionSubOverlay();
};

$("btnMoveSub").onclick = () => {
  const active = subOverlay.classList.toggle("movable");
  $("btnMoveSub").textContent = active ? "Xong" : "Di chuyển phụ đề";
  $("btnMoveSub").classList.toggle("accent", active);
};

let subDragging = false;

subOverlay.addEventListener("pointerdown", (e) => {
  if (!subOverlay.classList.contains("movable")) return;
  e.preventDefault();
  subOverlay.setPointerCapture(e.pointerId);
  subOverlay.classList.add("dragging");
  subDragging = true;
});

subOverlay.addEventListener("pointermove", (e) => {
  if (!subDragging) return;
  const rect = regionLayer.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  ed.subtitleStyle.x = clamp((e.clientX - rect.left) / rect.width, 0, 1);
  ed.subtitleStyle.y = clamp((e.clientY - rect.top) / rect.height, 0, 1);
  ed.subtitleStyleDirty = true;
  positionSubOverlay();
});

subOverlay.addEventListener("pointerup", () => {
  if (!subDragging) return;
  subDragging = false;
  subOverlay.classList.remove("dragging");
  setStatus("Có thay đổi chưa lưu");
});

/* ---------- watermark (text / image, opacity, drag position) ---------- */

const wmOverlay = $("wmOverlay");

function updateWmBadge() {
  $("wmBadge").textContent = ed.watermark.enabled ? "Bật" : "";
}

function wmChanged() {
  updateWmBadge();
  ed.watermarkDirty = true;
  setStatus("Có thay đổi chưa lưu");
  positionWmOverlay();
}

/** Rebuild the overlay content (text or <img>) – only when type/text/image changes. */
function renderWmContent() {
  const w = ed.watermark;
  wmOverlay.innerHTML = "";
  if (w.type === "image") {
    if (w.image) {
      const img = document.createElement("img");
      img.alt = "";
      img.draggable = false;
      img.src = `/api/jobs/${ed.jobId}/watermark-image?v=${ed.wmImageVer}`;
      wmOverlay.appendChild(img);
    } else {
      wmOverlay.textContent = "Chưa chọn ảnh";
    }
  } else {
    wmOverlay.textContent = w.text || "Hình mờ";
  }
  positionWmOverlay();
}

/** Size / position / opacity only (cheap; runs while dragging). */
function positionWmOverlay() {
  if (!ed.open) return;
  const w = ed.watermark;
  wmOverlay.style.display = w.enabled ? "block" : "none";
  wmOverlay.style.left = w.x * 100 + "%";
  wmOverlay.style.top = w.y * 100 + "%";
  wmOverlay.style.opacity = w.opacity;

  if (w.type === "image" && w.image) {
    wmOverlay.style.width = w.scale * 100 + "%";
    wmOverlay.style.fontSize = "";
  } else {
    const frameW = parseFloat(regionLayer.style.width) || 0;
    const realW = V.videoWidth || frameW || 1;
    const scale = frameW ? frameW / realW : 1;
    wmOverlay.style.width = "";
    wmOverlay.style.fontSize = Math.max(6, w.font_size * scale) + "px";
  }
}

function renderWatermarkPanel() {
  const w = ed.watermark;
  updateWmBadge();
  $("wmEnabled").checked = !!w.enabled;
  $("wmBody").classList.toggle("disabled", !w.enabled);
  document.querySelectorAll('input[name="wmType"]').forEach((r) => { r.checked = r.value === w.type; });
  $("wmTextRow").style.display = w.type === "text" ? "" : "none";
  $("wmImageRow").style.display = w.type === "image" ? "" : "none";
  $("wmText").value = w.text || "";
  $("wmFontSize").value = w.font_size;
  $("wmFontSizeVal").textContent = w.font_size;
  $("wmScale").value = Math.round(w.scale * 100);
  $("wmScaleVal").textContent = Math.round(w.scale * 100) + "%";
  $("wmOpacity").value = Math.round(w.opacity * 100);
  $("wmOpacityVal").textContent = Math.round(w.opacity * 100) + "%";
  renderWmContent();
}

$("wmEnabled").onchange = () => {
  ed.watermark.enabled = $("wmEnabled").checked;
  $("wmBody").classList.toggle("disabled", !ed.watermark.enabled);
  wmChanged();
};

document.querySelectorAll('input[name="wmType"]').forEach((r) => {
  r.onchange = () => {
    if (!r.checked) return;
    ed.watermark.type = r.value;
    $("wmTextRow").style.display = r.value === "text" ? "" : "none";
    $("wmImageRow").style.display = r.value === "image" ? "" : "none";
    renderWmContent();
    wmChanged();
  };
});

$("wmText").oninput = () => {
  ed.watermark.text = $("wmText").value;
  renderWmContent();
  wmChanged();
};

$("wmFontSize").oninput = () => {
  ed.watermark.font_size = +$("wmFontSize").value;
  $("wmFontSizeVal").textContent = ed.watermark.font_size;
  wmChanged();
};

$("wmScale").oninput = () => {
  const v = +$("wmScale").value;
  ed.watermark.scale = v / 100;
  $("wmScaleVal").textContent = v + "%";
  wmChanged();
};

$("wmOpacity").oninput = () => {
  const v = +$("wmOpacity").value;
  ed.watermark.opacity = v / 100;
  $("wmOpacityVal").textContent = v + "%";
  wmChanged();
};

$("wmImage").onchange = async () => {
  const file = $("wmImage").files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  setStatus("Đang tải ảnh hình mờ...");
  try {
    const res = await fetch(`/api/jobs/${ed.jobId}/watermark-image`, { method: "POST", body: fd });
    if (!res.ok) { setStatus("Không tải được ảnh: " + (await res.text()), true); return; }
    const { watermark } = await res.json();
    ed.watermark.image = watermark.image;
    ed.watermark.type = "image";
    ed.wmImageVer = Date.now();
    renderWatermarkPanel();
    wmChanged();
  } catch (e) {
    setStatus("Không tải được ảnh: mất kết nối tới máy chủ.", true);
  } finally {
    $("wmImage").value = "";
  }
};

$("btnMoveWm").onclick = () => {
  const active = wmOverlay.classList.toggle("movable");
  $("btnMoveWm").textContent = active ? "Xong" : "Di chuyển hình mờ";
  $("btnMoveWm").classList.toggle("accent", active);
};

let wmDrag = null;

wmOverlay.addEventListener("pointerdown", (e) => {
  if (!wmOverlay.classList.contains("movable")) return;
  e.preventDefault();
  wmOverlay.setPointerCapture(e.pointerId);
  wmOverlay.classList.add("dragging");
  const r = wmOverlay.getBoundingClientRect();
  wmDrag = { dx: e.clientX - r.left, dy: e.clientY - r.top, w: r.width, h: r.height };
});

wmOverlay.addEventListener("pointermove", (e) => {
  if (!wmDrag) return;
  const lr = regionLayer.getBoundingClientRect();
  if (!lr.width || !lr.height) return;
  // keep the whole watermark inside the frame (backend uses x,y as its top-left corner)
  const maxX = Math.max(0, 1 - wmDrag.w / lr.width);
  const maxY = Math.max(0, 1 - wmDrag.h / lr.height);
  ed.watermark.x = clamp((e.clientX - wmDrag.dx - lr.left) / lr.width, 0, maxX);
  ed.watermark.y = clamp((e.clientY - wmDrag.dy - lr.top) / lr.height, 0, maxY);
  ed.watermarkDirty = true;
  positionWmOverlay();
});

wmOverlay.addEventListener("pointerup", () => {
  if (!wmDrag) return;
  wmDrag = null;
  wmOverlay.classList.remove("dragging");
  setStatus("Có thay đổi chưa lưu");
});

/* ---------- dubbing preview (dubbed voice replaces the original audio in the editor) ---------- */

const dubAudio = $("dubAudio");
let dubPollTimer = null;

function resetDub() {
  clearTimeout(dubPollTimer);
  dubAudio.pause();
  dubAudio.removeAttribute("src");
  dubAudio.load();
  ed.dub = { state: "idle", hasAudio: false, upToDate: false, audioVersion: "", done: 0, total: 0, error: "", listen: true, volume: 1, origVolume: 0.25, duck: true, duckLevel: 0.3, loadedKey: "", voices: {}, voiceStale: false };
  V.muted = false;
}

const dubActive = () => ed.open && ed.dub.listen && ed.dub.hasAudio;

/* Voice windows where the original audio is ducked (same rules as dubbing.duck_regions on the server). */
function voiceRegions() {
  if (ed._regs) return ed._regs;
  const regs = [];
  for (const sg of cur()) {
    if (!sg.text.trim()) continue;
    const a = Math.max(0, sg.start - 0.15), b = sg.end + 0.25;
    if (regs.length && a - regs[regs.length - 1][1] <= 0.6) regs[regs.length - 1][1] = Math.max(regs[regs.length - 1][1], b);
    else regs.push([a, b]);
  }
  ed._regs = regs;
  return regs;
}

function inVoice(t) {
  for (const r of voiceRegions()) {
    if (t >= r[0] && t <= r[1]) return true;
    if (r[0] > t) break;
  }
  return false;
}

let duckGain = 1;

/** Mix in the preview: original audio (its own volume, ducked under the voice) + dubbed voice. */
function applyMix(jump) {
  if (!dubActive()) return;
  const d = ed.dub;
  const target = d.duck && inVoice(V.currentTime || 0) ? d.duckLevel : 1;
  duckGain = jump ? target : duckGain + (target - duckGain) * 0.25;   // eased per frame, no clicks
  V.muted = false;
  V.volume = clamp(d.origVolume * duckGain, 0, 1);
  dubAudio.volume = clamp(d.volume, 0, 1);
}

function applyDubMute() {
  if (!dubActive()) {
    dubAudio.pause();
    V.muted = false;
    V.volume = 1;               // original audio only, untouched
    return;
  }
  applyMix(true);
}

/** Keep the dub audio locked to the video clock (drift > 0.2s gets corrected). */
function syncDub(force) {
  if (!dubActive()) return;
  applyMix(!!force);
  const t = V.currentTime || 0;
  const dur = dubAudio.duration;
  if (isFinite(dur) && t >= dur - 0.05) { dubAudio.pause(); return; }
  if (force || Math.abs(dubAudio.currentTime - t) > 0.2) {
    try { dubAudio.currentTime = t; } catch (e) { /* metadata not ready yet */ }
  }
  dubAudio.playbackRate = V.playbackRate || 1;
  if (!V.paused && dubAudio.paused) dubAudio.play().catch(() => {});
  else if (V.paused && !dubAudio.paused) dubAudio.pause();
}

function loadDubAudio() {
  const d = ed.dub;
  if (!d.hasAudio) {
    if (d.loadedKey) {
      dubAudio.pause();
      dubAudio.removeAttribute("src");
      dubAudio.load();
      d.loadedKey = "";
    }
    applyDubMute();
    return;
  }
  const key = `${ed.jobId}/${ed.lang}/${d.audioVersion}`;
  if (key !== d.loadedKey) {
    d.loadedKey = key;
    dubAudio.src = `/api/jobs/${ed.jobId}/dub-preview/${ed.lang}/audio?v=${d.audioVersion}`;
    dubAudio.load();
  }
  applyDubMute();
  syncDub(true);
}

dubAudio.addEventListener("loadedmetadata", () => syncDub(true));

async function refreshDubStatus() {
  if (!(ed.open && ed.lang)) return;
  clearTimeout(dubPollTimer);
  const jobId = ed.jobId, lang = ed.lang;
  let s;
  try {
    const res = await fetch(`/api/jobs/${jobId}/dub-preview/${lang}/status`);
    if (!res.ok) return;
    s = await res.json();
  } catch (e) {
    dubPollTimer = setTimeout(refreshDubStatus, 3000);
    return;
  }
  if (!ed.open || ed.jobId !== jobId || ed.lang !== lang) return;
  const hadAudio = ed.dub.hasAudio, wasRunning = ed.dub.state === "running";
  Object.assign(ed.dub, {
    state: s.state, hasAudio: s.has_audio, upToDate: s.up_to_date,
    audioVersion: s.audio_version, done: s.done, total: s.total, error: s.error || "", engine: s.engine || "",
  });
  // first dubbed track just finished: switch to it automatically (the person can switch back)
  if (wasRunning && !hadAudio && s.state === "idle" && s.has_audio && !ed.dub.listen) {
    ed.dub.listen = true;
    ed.dubDirty = true;
    setStatus("Có thay đổi chưa lưu");
  }
  loadDubAudio();
  renderDubPanel();
  if (s.state === "running") dubPollTimer = setTimeout(refreshDubStatus, 1200);
}

function onDubLangChanged() {
  Object.assign(ed.dub, { state: "idle", hasAudio: false, upToDate: false, done: 0, total: 0, error: "" });
  loadDubAudio();
  renderDubPanel();
  renderVoiceSelect();
  refreshDubStatus();
}

function renderDubPanel() {
  const panel = $("dubPanel");
  const on = ed.open;
  panel.style.display = on ? "" : "none";
  if (!on) return;

  const d = ed.dub;
  const running = d.state === "running";
  const stale = !d.upToDate || ed.dirty.has(ed.lang) || d.voiceStale;

  let text, cls;
  if (running) { text = `Đang tạo giọng ${d.done}/${d.total}`; cls = "warn"; }
  else if (d.state === "failed") { text = "Lỗi"; cls = "err"; }
  else if (!d.hasAudio) { text = d.listen ? "Chưa tạo (sẽ tạo khi xuất)" : "Chưa tạo"; cls = "idle"; }
  else if (stale) { text = "Cần cập nhật"; cls = "warn"; }
  else { text = "Đã cập nhật"; cls = "ok"; }
  const badge = $("dubBadge");
  badge.textContent = text;
  badge.className = "dub-badge " + cls;

  const btn = $("btnDubBuild");
  btn.disabled = running || (d.hasAudio && !stale);
  btn.textContent = running ? "Đang tạo..." : !d.hasAudio ? "Tạo giọng lồng tiếng" : "Cập nhật lồng tiếng";

  $("dubProgressWrap").style.display = running ? "" : "none";
  $("dubProgressFill").style.width = (d.total ? (d.done / d.total) * 100 : 0) + "%";
  $("dubEngine").textContent = d.engine ? "Giọng đọc: " + d.engine : "";

  document.querySelectorAll('input[name="dubSource"]').forEach((r) => { r.checked = (r.value === "dub") === d.listen; });
  $("dubMix").classList.toggle("disabled", !d.listen);
  $("dubOrigVolume").value = Math.round(d.origVolume * 100);
  $("dubOrigVolumeVal").textContent = Math.round(d.origVolume * 100) + "%";
  $("dubVolume").value = Math.round(d.volume * 100);
  $("dubVolumeVal").textContent = Math.round(d.volume * 100) + "%";
  $("dubDuck").checked = d.duck;
  $("dubDuckLevel").disabled = !d.duck;
  $("dubDuckLevel").value = Math.round(d.duckLevel * 100);
  $("dubDuckLevelVal").textContent = Math.round(d.duckLevel * 100) + "%";

  $("dubHint").textContent = d.state === "failed"
    ? "Không tạo được giọng: " + d.error
    : "Tạo giọng lồng tiếng rồi chọn “Audio gốc” (chỉ âm thanh gốc) hoặc “Audio lồng tiếng” (giọng đọc trộn với âm gốc). Kéo “Âm gốc” xuống để giọng đọc nghe rõ, hoặc bật tự động hạ âm gốc: âm gốc tự nhỏ đi đúng lúc có giọng đọc rồi to lại. Các mức này được lưu và áp dụng y như vậy khi xuất video, với cả phụ đề cứng và mềm. Đặt “Âm gốc” = 0% nếu muốn thay hoàn toàn. Sau khi sửa phụ đề, bấm “Cập nhật” — chỉ đoạn đã đổi được đọc lại."
  mixChanged();
}

/** Apply the current mix to the preview player (does not mark anything as changed). */
function mixChanged() {
  applyDubMute();
}

/** A mix / source setting was changed by the person: apply to preview and mark as unsaved. */
function dubSettingChanged() {
  ed.dubDirty = true;
  setStatus("Có thay đổi chưa lưu");
  mixChanged();
}

$("btnDubBuild").onclick = async () => {
  if (!ed.open || !ed.lang) return;
  // the server builds the voice from the SAVED segments, so save pending edits first
  if (anyDirty() && !(await saveEditor())) return;
  try {
    const res = await fetch(`/api/jobs/${ed.jobId}/dub-preview/${ed.lang}`, { method: "POST" });
    if (!res.ok) {
      Object.assign(ed.dub, { state: "failed", error: await res.text() });
      renderDubPanel();
      return;
    }
  } catch (e) {
    Object.assign(ed.dub, { state: "failed", error: "mất kết nối tới máy chủ." });
    renderDubPanel();
    return;
  }
  Object.assign(ed.dub, { state: "running", done: 0, error: "" });
  renderDubPanel();
  refreshDubStatus();
};

document.querySelectorAll('input[name="dubSource"]').forEach((r) => {
  r.onchange = () => {
    if (!r.checked) return;
    ed.dub.listen = r.value === "dub";
    renderDubPanel();
    loadDubAudio();
    dubSettingChanged();
  };
});

$("dubOrigVolume").oninput = () => {
  ed.dub.origVolume = +$("dubOrigVolume").value / 100;
  $("dubOrigVolumeVal").textContent = $("dubOrigVolume").value + "%";
  dubSettingChanged();
};

/* ---------- voice picker + preview ---------- */

const voiceCache = {};   // lang -> {voices:[{id,label,engine}], online}
let voiceAudio = null;
let voiceUrl = null;

async function fetchVoices(lang) {
  if (voiceCache[lang]) return voiceCache[lang];
  const res = await fetch(`/api/voices/${encodeURIComponent(lang)}`);
  if (!res.ok) throw new Error("HTTP " + res.status);
  const data = await res.json();
  if (data.online) voiceCache[lang] = data;   // don't cache the offline fallback: retry next time
  return data;
}

function voiceMessage(text, isError = false) {
  const m = $("voiceMsg");
  m.textContent = text;
  m.classList.toggle("error", isError);
}

/** Fill the combobox with the voices of the language being edited. */
async function renderVoiceSelect() {
  if (!ed.open || !ed.lang) return;
  const lang = ed.lang, sel = $("dubVoice");
  stopVoicePreview();
  sel.disabled = true;
  sel.innerHTML = "";
  sel.appendChild(new Option("Đang tải danh sách giọng...", ""));

  let data;
  try { data = await fetchVoices(lang); }
  catch (e) { data = { voices: [{ id: "", label: "Tự động (mặc định)" }], online: false }; }
  if (!ed.open || ed.lang !== lang) return;   // the person switched language meanwhile

  sel.innerHTML = "";
  const chosen = ed.dub.voices[lang] || "";
  let found = false;
  data.voices.forEach((v) => {
    sel.appendChild(new Option(v.label, v.id));
    if (v.id === chosen) found = true;
  });
  if (chosen && !found) sel.appendChild(new Option(chosen + " (đã lưu)", chosen));
  sel.value = chosen;
  sel.disabled = false;
  voiceMessage(data.online ? "" : "Không tải được danh sách đầy đủ (cần internet). Chỉ hiện giọng mặc định.", !data.online);
}

function stopVoicePreview() {
  if (voiceAudio) { voiceAudio.onended = null; voiceAudio.pause(); voiceAudio = null; }
  if (voiceUrl) { URL.revokeObjectURL(voiceUrl); voiceUrl = null; }
  const b = $("btnVoicePreview");
  if (b) b.textContent = "▶ Nghe thử";
}

$("dubVoice").onchange = () => {
  const v = $("dubVoice").value;
  if (v) ed.dub.voices[ed.lang] = v; else delete ed.dub.voices[ed.lang];
  ed.dub.voiceStale = true;   // the dubbed track must be rebuilt with the new voice
  stopVoicePreview();
  voiceMessage("");
  renderDubPanel();
  dubSettingChanged();
};

$("btnVoicePreview").onclick = async () => {
  const btn = $("btnVoicePreview");
  if (voiceAudio) { stopVoicePreview(); return; }   // second click = stop
  if (!ed.open || !ed.lang) return;
  V.pause();
  const lang = ed.lang, voice = $("dubVoice").value;
  btn.disabled = true;
  btn.textContent = "Đang tạo mẫu...";
  voiceMessage("");
  try {
    const res = await fetch(`/api/jobs/${ed.jobId}/voice-preview/${encodeURIComponent(lang)}?voice=${encodeURIComponent(voice)}`);
    if (!res.ok) {
      const raw = await res.text();
      let msg = raw;
      try { msg = JSON.parse(raw).detail || raw; } catch (e) { /* plain text */ }
      throw new Error(msg);
    }
    const blob = await res.blob();
    if (!ed.open || ed.lang !== lang) return;
    voiceUrl = URL.createObjectURL(blob);
    voiceAudio = new Audio(voiceUrl);
    voiceAudio.onended = stopVoicePreview;
    await voiceAudio.play();
    btn.textContent = "■ Dừng";
  } catch (e) {
    stopVoicePreview();
    voiceMessage("Không nghe thử được: " + e.message, true);
  } finally {
    btn.disabled = false;
  }
};

V.addEventListener("play", stopVoicePreview);

$("dubVolume").oninput = () => {
  ed.dub.volume = +$("dubVolume").value / 100;
  $("dubVolumeVal").textContent = $("dubVolume").value + "%";
  dubSettingChanged();
};

$("dubDuck").onchange = () => {
  ed.dub.duck = $("dubDuck").checked;
  $("dubDuckLevel").disabled = !ed.dub.duck;
  dubSettingChanged();
};

$("dubDuckLevel").oninput = () => {
  ed.dub.duckLevel = +$("dubDuckLevel").value / 100;
  $("dubDuckLevelVal").textContent = $("dubDuckLevel").value + "%";
  dubSettingChanged();
};

V.addEventListener("play", () => syncDub(true));
V.addEventListener("playing", () => syncDub(true));
V.addEventListener("pause", () => dubAudio.pause());
V.addEventListener("waiting", () => dubAudio.pause());
V.addEventListener("ended", () => dubAudio.pause());
V.addEventListener("seeked", () => syncDub(true));
V.addEventListener("ratechange", () => { dubAudio.playbackRate = V.playbackRate || 1; });

function placeRegionEl(node, r) {
  node.style.left = r.x * 100 + "%";
  node.style.top = r.y * 100 + "%";
  node.style.width = r.w * 100 + "%";
  node.style.height = r.h * 100 + "%";
}

function regionsChanged() {
  ed.regionsDirty = true;
  setStatus("Có thay đổi chưa lưu");
  renderRegions();
}

function renderRegions() {
  $("regionBadge").textContent = ed.regions.length || "";
  regionLayer.querySelectorAll(".region:not(.draft)").forEach((n) => n.remove());
  const list = $("regionList");
  list.innerHTML = "";
  ed.regions.forEach((r, i) => {
    const box = el("div", `region mode-${r.mode}`);
    placeRegionEl(box, r);
    regionLayer.appendChild(box);

    const row = el("div", "region-row");
    row.appendChild(el("span", "name", `Vùng ${i + 1}`));
    const sel = el("select");
    Object.entries(REGION_LABELS).forEach(([value, label]) => {
      const opt = el("option", null, label);
      opt.value = value;
      sel.appendChild(opt);
    });
    sel.value = r.mode;
    sel.setAttribute("aria-label", `Kiểu xử lý vùng ${i + 1}`);
    sel.onchange = () => { r.mode = sel.value; regionsChanged(); };
    const del = el("button", "mini danger", "Xóa");
    del.onclick = () => { ed.regions.splice(i, 1); regionsChanged(); };
    row.append(sel, del);
    list.appendChild(row);
  });
}

$("btnDrawRegion").onclick = () => {
  drawMode = !drawMode;
  regionLayer.classList.toggle("drawing", drawMode);
  $("btnDrawRegion").textContent = drawMode ? "Xong" : "Vẽ vùng";
  $("btnDrawRegion").classList.toggle("accent", drawMode);
  if (drawMode) V.pause();
};

function regionPoint(e) {
  const rect = regionLayer.getBoundingClientRect();
  return {
    x: clamp((e.clientX - rect.left) / rect.width, 0, 1),
    y: clamp((e.clientY - rect.top) / rect.height, 0, 1),
  };
}

function draftRect() {
  return {
    x: Math.min(draft.x0, draft.x1), y: Math.min(draft.y0, draft.y1),
    w: Math.abs(draft.x1 - draft.x0), h: Math.abs(draft.y1 - draft.y0),
  };
}

regionLayer.addEventListener("pointerdown", (e) => {
  if (!drawMode) return;
  e.preventDefault();
  regionLayer.setPointerCapture(e.pointerId);
  const p = regionPoint(e);
  draft = { x0: p.x, y0: p.y, x1: p.x, y1: p.y, node: el("div", "region draft") };
  regionLayer.appendChild(draft.node);
  placeRegionEl(draft.node, draftRect());
});

regionLayer.addEventListener("pointermove", (e) => {
  if (!draft) return;
  const p = regionPoint(e);
  draft.x1 = p.x;
  draft.y1 = p.y;
  placeRegionEl(draft.node, draftRect());
});

regionLayer.addEventListener("pointerup", () => {
  if (!draft) return;
  const r = draftRect();
  draft.node.remove();
  draft = null;
  if (r.w >= 0.01 && r.h >= 0.01) {
    ed.regions.push({ ...r, mode: $("regionMode").value });
    regionsChanged();
  }
});

/* ---------- video cuts (parts of the source timeline removed on export) ---------- */

const MIN_CUT = 0.1;
const cutBlockEl = (k) => $("tlCuts").querySelector(`[data-k="${k}"]`);
const cutTotal = () => ed.cuts.reduce((a, c) => a + (c.end - c.start), 0);

function isCutAt(t) { return ed.cuts.some((c) => t >= c.start && t < c.end); }

function cutsChanged() {
  ed.cutsDirty = true;
  setStatus("Có thay đổi chưa lưu");
  renderCuts();
}

function positionCutBlock(c) {
  const b = cutBlockEl(c.k);
  if (!b) return;
  b.style.left = c.start * ed.pps + "px";
  b.style.width = Math.max(4, (c.end - c.start) * ed.pps) + "px";
}

function markCutClasses() {
  $("tlCuts").querySelectorAll(".cut-block").forEach((n) => {
    n.classList.toggle("selected", +n.dataset.k === ed.cutSel);
  });
}

function selectCut(k) {
  ed.cutSel = k;
  markCutClasses();
}

/** Redraw the cut lane, the in/out marks, the cut list and the "after cutting" duration. */
function renderCuts() {
  const lane = $("tlCuts");
  lane.innerHTML = "";

  if (ed.markIn != null && ed.markOut != null) {
    const r = el("div", "mark-range");
    r.style.left = ed.markIn * ed.pps + "px";
    r.style.width = Math.max(0, (ed.markOut - ed.markIn) * ed.pps) + "px";
    lane.appendChild(r);
  }
  ed.cuts.forEach((c) => {
    const b = el("div", "cut-block" + (c.k === ed.cutSel ? " selected" : ""));
    b.dataset.k = c.k;
    b.title = `Đoạn bị cắt ${formatTime(c.start)} – ${formatTime(c.end)}`;
    b.appendChild(el("span", "lbl", "✂ " + (c.end - c.start).toFixed(1) + "s"));
    b.appendChild(el("i", "h hl"));
    b.appendChild(el("i", "h hr"));
    lane.appendChild(b);
    positionCutBlock(c);
  });
  [["in", ed.markIn, "I"], ["out", ed.markOut, "O"]].forEach(([, t, label]) => {
    if (t == null) return;
    const pin = el("div", "mark-pin");
    pin.dataset.l = label;
    pin.style.left = t * ed.pps + "px";
    lane.appendChild(pin);
  });

  const list = $("cutList");
  list.innerHTML = "";
  if (!ed.cuts.length) {
    list.appendChild(el("p", "muted", "Chưa cắt đoạn nào. Kéo trên làn “Cắt” của timeline, hoặc dùng I / O / X."));
  }
  ed.cuts.forEach((c, i) => {
    const row = el("div", "region-row cut-row");
    row.appendChild(el("span", "name", `Đoạn ${i + 1}: ${formatTime(c.start)} – ${formatTime(c.end)} (${(c.end - c.start).toFixed(1)}s)`));
    const go = el("button", "mini", "Đi tới");
    go.type = "button";
    go.onclick = () => { seekTo(Math.max(0, c.start - 1)); selectCut(c.k); };
    const del = el("button", "mini danger", "Bỏ cắt");
    del.type = "button";
    del.onclick = () => removeCut(c.k);
    row.append(go, del);
    list.appendChild(row);
  });

  const removed = cutTotal();
  $("cutInfo").textContent = ed.cuts.length
    ? `Sau khi cắt: ${formatTime(Math.max(0, ed.total - removed))} (bỏ ${formatTime(removed)})`
    : "";
  $("cutBadge").textContent = ed.cuts.length || "";
  $("btnClearCuts").disabled = !ed.cuts.length;
  markClasses();
}

/** Add a removed range; overlapping/touching cuts are merged. Returns true on success. */
function addCut(start, end) {
  start = clamp(start, 0, ed.total);
  end = clamp(end, 0, ed.total);
  if (end - start < MIN_CUT) { notify("Đoạn cắt quá ngắn (tối thiểu 0,1 giây)."); return false; }

  const snap = JSON.stringify(ed.cuts);
  let s = start, e = end;
  const rest = [];
  ed.cuts.forEach((c) => {
    if (c.end >= s && c.start <= e) { s = Math.min(s, c.start); e = Math.max(e, c.end); }
    else rest.push(c);
  });
  const next = [...rest, { k: keySeq++, start: s, end: e }].sort((a, b) => a.start - b.start);
  const removed = next.reduce((a, c) => a + (c.end - c.start), 0);
  if (ed.total - removed < 0.3) { notify("Không thể cắt toàn bộ video."); return false; }

  pushCutUndo(snap);
  ed.cuts = next;
  ed.cutSel = null;
  cutsChanged();
  return true;
}

function removeCut(k) {
  const i = ed.cuts.findIndex((c) => c.k === k);
  if (i < 0) return;
  pushCutUndo();
  ed.cuts.splice(i, 1);
  if (ed.cutSel === k) ed.cutSel = null;
  cutsChanged();
}

function markPoint(kind) {
  const t = V.currentTime || 0;
  if (kind === "in") {
    ed.markIn = t;
    if (ed.markOut != null && ed.markOut <= t) ed.markOut = null;
  } else {
    ed.markOut = t;
    if (ed.markIn != null && ed.markIn >= t) ed.markIn = null;
  }
  renderCuts();
  notify(kind === "in" ? `Điểm vào: ${formatTime(t)}` : `Điểm ra: ${formatTime(t)}`);
}

/** Cut from the "in" mark to the "out" mark (or to the playhead when no out mark is set). */
function cutRange() {
  if (ed.markIn == null) { notify("Hãy đặt điểm vào (phím I) tại chỗ bắt đầu đoạn cần cắt."); return; }
  const end = ed.markOut != null ? ed.markOut : (V.currentTime || 0);
  if (end - ed.markIn < MIN_CUT) { notify("Dời đầu phát (hoặc điểm ra) ra xa điểm vào hơn."); return; }
  if (addCut(ed.markIn, end)) {
    ed.markIn = null;
    ed.markOut = null;
    renderCuts();
  }
}

/** While playing, jump over removed ranges so the preview matches the exported video. */
function skipCuts() {
  if (!ed.skipCuts || V.paused || !ed.cuts.length) return;
  const t = V.currentTime || 0;
  const c = ed.cuts.find((x) => t >= x.start - 0.03 && t < x.end);
  if (c) V.currentTime = Math.min(c.end, V.duration || c.end);
}

/* drag a cut block (move / resize) */
let cdrag = null;

function startCutDrag(e, blk) {
  const cut = ed.cuts.find((c) => c.k === +blk.dataset.k);
  if (!cut) return;
  e.preventDefault();
  selectCut(cut.k);
  const i = ed.cuts.indexOf(cut);
  cdrag = {
    cut,
    mode: e.target.classList.contains("hl") ? "l" : e.target.classList.contains("hr") ? "r" : "m",
    x0: e.clientX,
    orig: { start: cut.start, end: cut.end },
    lo: i > 0 ? ed.cuts[i - 1].end : 0,
    hi: i < ed.cuts.length - 1 ? ed.cuts[i + 1].start : ed.total,
    base: JSON.stringify(ed.cuts),
    moved: false,
  };
}

/* drag on an empty part of the cut lane to create a new cut */
let ccreate = null;

function startCutCreate(e) {
  e.preventDefault();
  const t0 = timeFromEvent(e);
  const node = el("div", "cut-block draft");
  $("tlCuts").appendChild(node);
  ccreate = { t0, t1: t0, x0: e.clientX, node, moved: false };
  paintCutDraft();
}

function paintCutDraft() {
  const a = Math.min(ccreate.t0, ccreate.t1), b = Math.max(ccreate.t0, ccreate.t1);
  ccreate.node.style.left = a * ed.pps + "px";
  ccreate.node.style.width = Math.max(2, (b - a) * ed.pps) + "px";
}

window.addEventListener("pointermove", (e) => {
  if (ccreate) {
    if (Math.abs(e.clientX - ccreate.x0) > 3) ccreate.moved = true;
    ccreate.t1 = timeFromEvent(e);
    paintCutDraft();
    return;
  }
  if (!cdrag) return;
  if (Math.abs(e.clientX - cdrag.x0) > 2) cdrag.moved = true;
  if (!cdrag.moved) return;
  const { cut, orig, lo, hi, mode } = cdrag;
  const dt = (e.clientX - cdrag.x0) / ed.pps;
  if (mode === "m") {
    const d = clamp(dt, lo - orig.start, hi - orig.end);
    cut.start = orig.start + d;
    cut.end = orig.end + d;
  } else if (mode === "l") {
    cut.start = clamp(orig.start + dt, lo, orig.end - MIN_CUT);
  } else {
    cut.end = clamp(orig.end + dt, orig.start + MIN_CUT, hi);
  }
  positionCutBlock(cut);
});

window.addEventListener("pointerup", () => {
  if (ccreate) {
    const c = ccreate;
    ccreate = null;
    c.node.remove();
    const a = Math.min(c.t0, c.t1), b = Math.max(c.t0, c.t1);
    if (c.moved && b - a >= MIN_CUT) addCut(a, b);
    else seekTo(c.t0); // a plain click on the lane just moves the playhead
    return;
  }
  if (!cdrag) return;
  const d = cdrag;
  cdrag = null;
  if (d.moved) {
    pushCutUndo(d.base);
    cutsChanged();
  }
});

$("btnMarkIn").onclick = () => markPoint("in");
$("btnMarkOut").onclick = () => markPoint("out");
$("btnCutRange").onclick = cutRange;
$("btnCutSeg").onclick = () => {
  const s = selSeg();
  if (!s) { notify("Hãy chọn một đoạn phụ đề trước."); return; }
  addCut(s.start, s.end);
};
$("btnClearCuts").onclick = () => {
  if (!ed.cuts.length) return;
  if (!confirm("Bỏ tất cả đoạn cắt?")) return;
  pushCutUndo();
  ed.cuts = [];
  ed.cutSel = null;
  cutsChanged();
};
$("cutSkip").onchange = () => { ed.skipCuts = $("cutSkip").checked; };

/* ---------- collapsible menus ---------- */

(function initAccordions() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("vt_acc2") || "{}"); } catch (e) { saved = {}; }
  const panels = () => Array.from(document.querySelectorAll("#editorPanels details.acc"));
  const syncAllLabel = () => {
    $("btnAccAll").textContent = panels().every((d) => d.open) ? "Thu gọn tất cả" : "Mở tất cả";
  };
  document.querySelectorAll("details[data-persist]").forEach((d) => {
    if (d.id in saved) d.open = !!saved[d.id];
    d.addEventListener("toggle", () => {
      saved[d.id] = d.open;
      try { localStorage.setItem("vt_acc2", JSON.stringify(saved)); } catch (e) { /* storage unavailable */ }
      syncAllLabel();
    });
  });
  $("btnAccAll").onclick = () => {
    const open = !panels().every((d) => d.open);
    panels().forEach((d) => { d.open = open; });
    syncAllLabel();
  };
  syncAllLabel();
})();

const modal = $("settingsModal");
$("btnSettings").onclick = async () => {
  modal.classList.add("open");
  const res = await fetch("/api/llm/config");
  const cfg = await res.json();
  $("cfgBaseUrl").value = cfg.base_url || "";
  $("cfgModel").value = cfg.model || "";
  $("cfgTemperature").value = cfg.temperature ?? 0.3;
  $("cfgJsonMode").checked = !!cfg.supports_json_mode;
};

$("btnCloseSettings").onclick = () => modal.classList.remove("open");

$("btnFetchModels").onclick = async () => {
  await saveSettings(false);
  const res = await fetch("/api/llm/models");
  const list = $("modelList");
  list.innerHTML = "";
  if (res.ok) {
    const data = await res.json();
    data.models.forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m;
      list.appendChild(opt);
    });
  } else {
    $("testResult").textContent = "Không lấy được danh sách model";
  }
};

$("btnTestConn").onclick = async () => {
  await saveSettings(false);
  const resultEl = $("testResult");
  resultEl.textContent = "Đang kiểm tra...";
  const res = await fetch("/api/llm/test", { method: "POST" });
  if (res.ok) {
    resultEl.textContent = "✅ Kết nối thành công";
    resultEl.style.color = "#6fd699";
  } else {
    const err = await res.json();
    resultEl.textContent = "❌ " + err.detail;
    resultEl.style.color = "#e0555f";
  }
};

$("btnSaveSettings").onclick = async () => {
  await saveSettings(true);
  modal.classList.remove("open");
};

async function saveSettings(closeAfter) {
  const payload = {
    base_url: $("cfgBaseUrl").value,
    api_key: $("cfgApiKey").value,
    model: $("cfgModel").value,
    temperature: parseFloat($("cfgTemperature").value),
    supports_json_mode: $("cfgJsonMode").checked,
  };
  if (!payload.api_key) delete payload.api_key;
  await fetch("/api/llm/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/* ------------------------------------------------------------------ */
/* Init                                                                */
/* ------------------------------------------------------------------ */

renderLangChips();
loadHistory();
if (currentJobId) openJob(currentJobId, { editCompleted: false });