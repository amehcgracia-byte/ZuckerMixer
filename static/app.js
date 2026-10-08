let appState = null;
let loadingOverlayJob = null;
let loadingOverlayCut = null;
let suppressLoadingOverlay = false;
let checkedSongs = new Set();
let lastLogId = -1;
let songListSignature = "";
let transitionSignature = "";
let resultsSignature = "";
let previewMixes = {};
let previewNodeIdCounter = 1;
const realPreviewJobs = {};
const previewNodeIds = new WeakMap();
let overrideWriteSeq = 1;
let overrideSaveTimer = null;
let overrideWritesInFlight = 0;
let renderInProgress = false;
let activeRenderJobId = null;
const pendingOverrideSongs = new Set();
const pendingOverrideReasons = new Set();
const activeStemLoadPromises = {};
const livePreviewOverrides = {};
let redetectPromptedJobId = null;
const previewVocalExpander = {
  thresholdDb: -45,
  ratio: 2,
  attackSeconds: 0.005,
  releaseSeconds: 0.5,
  kneeDb: 5,
  maxAttenuationDb: -12,
};

const $ = (sel) => document.querySelector(sel);

function makeDialogDismissible(dialog, dismiss = () => dialog.close("cancel")) {
  if (dialog.dataset.dismissible) return;
  dialog.dataset.dismissible = "true";
  const close = dialog.querySelector("[data-dialog-close]") || document.createElement("button");
  if (!close.isConnected) {
    close.type = "button";
    close.className = "dialog-close";
    close.textContent = "X";
    close.setAttribute("aria-label", "Close");
    close.dataset.dialogClose = "true";
    dialog.appendChild(close);
  }
  close.addEventListener("click", () => dismiss());
  dialog.addEventListener("click", (event) => {
    const rect = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) dismiss();
  });
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); dismiss(); });
}

function showConfirm(message, confirmLabel = "Continue", cancelLabel = "Cancel") {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "message-dialog";
    const text = document.createElement("p");
    text.textContent = message;
    const confirm = document.createElement("button");
    confirm.type = "button"; confirm.textContent = confirmLabel;
    const cancel = document.createElement("button");
    cancel.type = "button"; cancel.textContent = cancelLabel;
    const finish = (accepted) => { dialog.close(); dialog.remove(); resolve(accepted); };
    confirm.onclick = () => finish(true);
    cancel.onclick = () => finish(false);
    dialog.append(text, confirm, cancel);
    document.body.appendChild(dialog);
    makeDialogDismissible(dialog, () => finish(false));
    dialog.showModal();
    cancel.focus();
  });
}

function setupDismissControls() {
  $("#closeLoadingOverlay").addEventListener("click", dismissLoadingOverlay);
  $("#loadingOverlay").addEventListener("click", (event) => { if (event.target === event.currentTarget) dismissLoadingOverlay(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !document.querySelector("dialog[open]")) dismissLoadingOverlay(); });
  document.addEventListener("click", (event) => { const toast = $("#toast"); if (toast?.classList.contains("show") && !toast.contains(event.target)) toast.classList.remove("show"); });
}

function dismissLoadingOverlay() {
  window.renderListening?.pause();
  suppressLoadingOverlay = true;
  renderLoadingOverlay();
  document.querySelector("#loadingStageVideo")?.pause();
}

let stateFetchPromise = null;
function fetchStateSnapshot() {
  if (!stateFetchPromise) {
    stateFetchPromise = fetch("/api/state").then(async (response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    }).finally(() => { stateFetchPromise = null; });
  }
  return stateFetchPromise;
}

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  }[ch]));
}

function renderBuildInfo() {
  const build = appState?.build || {};
  const timestamp = build.build_timestamp || "development build";
  const revision = build.source_revision || "unbuilt";
  const version = build.app_version || "development";
  const appHash = build.app_js_sha256 || "unavailable";
  const pid = build.runtime_pid || "unknown";
  const node = $("#buildInfo");
  if (node) node.textContent = `ZuckerMixer ${version} · commit ${revision} · app.js ${appHash} · PID ${pid} · ${timestamp}`;
}

function renderSlotAudit() {
  const node = $("#slotAudit"); const audit = appState?.slot_audit;
  if (!node || !audit) return;
  const integrity = appState?.source_integrity || {};
  const calibration = appState?.detection_calibration || {};
  const strategy = calibration.strategy || {};
  const whisper = appState?.whisper || strategy.whisper || {};
  const candidatePending = Boolean(
    appState?.candidate_pending
    || appState?.segmentation_status === "candidate_pending"
    || calibration.candidate_pending
    || strategy.candidate_only
  );
  const transcriptCount = Number(whisper.transcript_count ?? whisper.completed_windows ?? 0);
  const candidateWindows = Number(whisper.candidate_windows ?? 0);
  const introductions = Number(
    whisper.eligible_introductions
    ?? strategy.whisper_eligible_introductions
    ?? 0
  );
  const diagnostic = candidatePending
    ? `Candidate pending review — not a valid single-song session. Whisper: ${candidateWindows ? `${transcriptCount}/${candidateWindows}` : transcriptCount || "—"} windows; eligible introductions: ${introductions}; visible diagnostic slots: ${audit.visible_slot_count}.`
    : "";
  node.hidden = !(candidatePending || audit.integrity_warning || integrity.warning);
  node.textContent = diagnostic || integrity.warning || audit.integrity_warning || `Session ${audit.session_id || "unknown"}: ${audit.visible_slot_count} slots visible`;
}

function refreshSlotSummary() {
  if (!appState) return;
  const calibration = appState.detection_calibration || {};
  const songs = visibleSongs();
  calibration.count = (appState.songs || []).length;
  calibration.exported_count = songs.filter((song) => song.latest_render).length;
  calibration.pending_count = Math.max(0, Number(calibration.count || songs.length) - calibration.exported_count);
  calibration.ready_count = songs.filter((song) => song.render_valid).length;
  calibration.needs_review_count = songs.filter((song) => song.needs_review || !song.render_valid).length;
  appState.detection_calibration = calibration;
}

const names = {
  kick: "Kick",
  snare: "Snare",
  bass: "Bass",
  guitar: "Guitar",
  keys: "Keys",
  keys_l: "Keys",
  keys_r: "Keys",
  vocal: "Voice",
  drums: "Room",
  room: "Ambience",
  sax: "Sax",
  synth: "Synth",
  horn: "Horn",
  other: "Room/Other",
};

function plainName(stem) {
  if (stem.label) return stem.label;
  const base = names[stem.role] || stem.name;
  const m = stem.name.match(/mic\s*([0-9]+)/i);
  if (m) return `Vocal ${m[1]}`;
  if (stem.role === "drums") return "Overheads";
  return base;
}

function stemFamily(stem) {
  const label = `${stem.role || ""} ${stem.file || ""} ${stem.label || ""}`.toLowerCase();
  if (/kick|snare|hihat|hi[- ]?hat|overhead|drum|percussion|beatbox|rhythm|808|909/.test(label)) return "drums";
  if (/bass/.test(label)) return "bass";
  if (/guitar|gtr/.test(label)) return "guitar";
  if (/keys|piano|organ|rhodes|keyboard/.test(label)) return "keys";
  if (/sax|horn|trumpet|trombone|brass|flute|wind/.test(label)) return "horns";
  if (/vocal|voice|singer|mic|vox/.test(label)) return "vocals";
  if (/synth|moog|pad/.test(label)) return "synth";
  return "room";
}

function amount(value) {
  const n = Number(value || 0);
  if (Math.abs(n) < 0.25) return "normal";
  return n > 0 ? `up ${n.toFixed(1)}` : `down ${Math.abs(n).toFixed(1)}`;
}

function signedDb(value) {
  const n = Number(value || 0);
  return `${n >= 0 ? "+" : ""}${n.toFixed(1)} dB`;
}

function songOverrides(song) {
  appState.overrides.songs ||= {};
  appState.overrides.songs[String(song)] ||= { stems: {}, vocal_bus_db: 0, target_lufs: -14, mastering_intensity: "natural" };
  appState.overrides.songs[String(song)].stems ||= {};
  return appState.overrides.songs[String(song)];
}

function currentSongOverrides(songIndex) {
  return livePreviewOverrides[String(songIndex)] || songOverrides(songIndex);
}

function stemOverrides(song, file) {
  const data = songOverrides(song);
  data.stems[file] ||= {};
  return data.stems[file];
}

function currentStemOverrides(songIndex, file) {
  return livePreviewOverrides[String(songIndex)]?.stems?.[file] || stemOverrides(songIndex, file);
}

const FADER_ROLE_ORDER = ["kick", "snare", "hihat", "drums", "bass", "guitar", "keys", "horn", "sax", "vocal", "synth"];

function faderRoleRank(stem) {
  const label = `${stem.file || ""} ${stem.label || ""}`.toLowerCase();
  if (/\b(hh|hihat|hi[- ]?hat|high[- ]?hat)\b/.test(label)) return 3;
  if (/\b(ov|oh|overhead|overheads|room)\b/.test(label)) return 4;
  if (stem.role === "keys_l" || stem.role === "keys_r") return 7;
  const role = stem.role === "horn" ? "horn" : stem.role;
  const rank = FADER_ROLE_ORDER.indexOf(role);
  return rank >= 0 ? rank + 1 : 99;
}

function stereoPairKey(stem) {
  const raw = String(stem.file || stem.label || "").toLowerCase().replace(/\.[a-z0-9]+$/, "");
  const pair = raw
    .replace(/(?:[ _-]*(?:left|right|channel\s*[12]|ch\s*[12]|[lr]))\s*$/i, "")
    .replace(/(?:[ _-]+[12])\s*$/i, "")
    .replace(/[ _-]+/g, " ").trim();
  return (stem.role === "keys_l" || stem.role === "keys_r") ? `keys:${pair.replace(/keys\s*[lr]$/, "keys")}` : pair;
}

function linkedStemsForSong(songIndex, stem, activeStems) {
  const key = stereoPairKey(stem);
  if (!key) return [stem];
  const matches = activeStems.filter((candidate) => stereoPairKey(candidate) === key);
  return matches.length >= 2 && (/\b(?:l|r|left|right)\b/i.test(stem.file) || ["keys_l", "keys_r"].includes(stem.role)) ? matches : [stem];
}

function orderedFaderGroups(songIndex, activeStems) {
  const ordered = [...activeStems].sort((a, b) => faderRoleRank(a) - faderRoleRank(b) || String(a.file).localeCompare(String(b)));
  const seen = new Set();
  return ordered.flatMap((stem) => {
    if (seen.has(stem.file)) return [];
    const linked = linkedStemsForSong(songIndex, stem, activeStems);
    linked.forEach((item) => seen.add(item.file));
    return [{ stem, linked }];
  });
}

function setLinkedOverride(songIndex, linked, key, value) {
  linked.forEach((item) => {
    stemOverrides(songIndex, item.file)[key] = value;
    livePreviewStemOverrides(songIndex, item.file)[key] = value;
  });
}

function livePreviewSongOverrides(songIndex) {
  const key = String(songIndex);
  livePreviewOverrides[key] ||= JSON.parse(JSON.stringify(songOverrides(songIndex)));
  livePreviewOverrides[key].stems ||= {};
  return livePreviewOverrides[key];
}

function livePreviewStemOverrides(songIndex, file) {
  const song = livePreviewSongOverrides(songIndex);
  song.stems[file] ||= JSON.parse(JSON.stringify(stemOverrides(songIndex, file)));
  return song.stems[file];
}

function syncLivePreviewSongOverride(songIndex) {
  const key = String(songIndex);
  if (!previewMixFor(songIndex) && !document.querySelector(`[data-faders="${songIndex}"]`)?.closest(".fine-tune[open]")) return;
  livePreviewOverrides[key] = JSON.parse(JSON.stringify(songOverrides(songIndex)));
  livePreviewOverrides[key].stems ||= {};
}

function clearLivePreviewSongOverride(songIndex) {
  delete livePreviewOverrides[String(songIndex)];
}

function openPreviewSongIds() {
  return new Set([
    ...Object.keys(previewMixes || {}),
    ...[...document.querySelectorAll(".fine-tune[open] [data-faders]")]
      .map((node) => node.dataset.faders)
      .filter(Boolean),
  ]);
}

function previewStemParams(songIndex, stem) {
  const song = appState.songs.find((item) => Number(item.id) === Number(songIndex));
  return song?.mix_params?.stems?.[stem.file] || null;
}

function defaultPan(stem) {
  const role = stem.role;
  const name = String(stem.name || stem.file || "").toLowerCase();
  if (role === "keys_l" || name.includes(" l")) return -0.35;
  if (role === "keys_r" || name.includes(" r")) return 0.35;
  if (role === "guitar") return -0.35;
  if (role === "keys") return 0.35;
  if (["sax", "horn", "synth"].includes(role)) return 0.25;
  if (role === "vocal" && /mic\s*2/.test(name)) return 0.15;
  return 0;
}

function stemPan(song, stem) {
  const ov = currentStemOverrides(song, stem.file);
  const params = previewStemParams(song, stem);
  if (Number.isFinite(Number(ov.pan))) return Number(ov.pan);
  if (params && Number.isFinite(Number(params.pan))) return Number(params.pan);
  return defaultPan(stem);
}

function roleEqBands(role) {
  if (role === "kick") return [["low", "highpass", 30, 0.707, 0], ["fixed", "peaking", 60, 0.9, 2.5], ["mid", "peaking", 350, 1, -2.5], ["fixed", "peaking", 3500, 0.9, 2], ["air", "highshelf", 10000, 0.707, 0]];
  if (role === "bass") return [["low", "highpass", 35, 0.707, 0], ["mid", "peaking", 300, 1, -2.5], ["fixed", "peaking", 100, 0.8, 1], ["air", "highshelf", 10000, 0.707, 0]];
  if (role === "vocal") return [["low", "highpass", 115, 0.707, 0], ["mid", "peaking", 3000, 0.9, 2], ["air", "highshelf", 11000, 0.707, 1]];
  if (role === "guitar") return [["low", "highpass", 90, 0.707, 0], ["mid", "peaking", 250, 1, -2], ["fixed", "peaking", 2800, 1, 1], ["air", "highshelf", 10000, 0.707, 0]];
  if (["keys", "keys_l", "keys_r"].includes(role)) return [["low", "highpass", 80, 0.707, 0], ["mid", "peaking", 320, 1, -2], ["air", "highshelf", 10000, 0.707, 0]];
  if (["sax", "horn"].includes(role)) return [["low", "highpass", 90, 0.707, 0], ["mid", "peaking", 2500, 0.9, 1.5], ["air", "highshelf", 10000, 0.707, 0]];
  if (role === "synth") return [["low", "highpass", 90, 0.707, 0], ["mid", "peaking", 2500, 0.9, 1.5], ["fixed", "peaking", 320, 1, -1.5], ["air", "highshelf", 10000, 0.707, 0]];
  if (role === "drums") return [["low", "highpass", 150, 0.707, 0], ["mid", "peaking", 300, 1, 0], ["air", "highshelf", 9000, 0.707, 1]];
  if (role === "snare") return [["low", "highpass", 80, 0.707, 0], ["mid", "peaking", 220, 1, -1.5], ["fixed", "peaking", 5000, 0.8, 1.5], ["air", "highshelf", 10000, 0.707, 0]];
  return [["low", "highpass", 80, 0.707, 0], ["mid", "peaking", 300, 1, 0], ["air", "highshelf", 10000, 0.707, 0]];
}

function defaultEq(stem) {
  const bands = roleEqBands(stem.role);
  const low = bands.find(([slot]) => slot === "low");
  const mid = bands.find(([slot]) => slot === "mid");
  const air = bands.find(([slot]) => slot === "air");
  return {
    eq_low_cut_hz: low ? low[2] : 80,
    eq_mid_gain_db: mid ? mid[4] : 0,
    eq_air_gain_db: air ? air[4] : 0,
  };
}

function stemEq(song, stem) {
  const defaults = defaultEq(stem);
  const params = previewStemParams(song, stem);
  const ov = currentStemOverrides(song, stem.file);
  return {
    eq_low_cut_hz: Number.isFinite(Number(ov.eq_low_cut_hz)) ? Number(ov.eq_low_cut_hz) : Number(params?.eq_low_cut_hz ?? defaults.eq_low_cut_hz),
    eq_mid_gain_db: Number.isFinite(Number(ov.eq_mid_gain_db)) ? Number(ov.eq_mid_gain_db) : Number(params?.eq_mid_gain_db ?? defaults.eq_mid_gain_db),
    eq_air_gain_db: Number.isFinite(Number(ov.eq_air_gain_db)) ? Number(ov.eq_air_gain_db) : Number(params?.eq_air_gain_db ?? defaults.eq_air_gain_db),
  };
}

function stemGainDb(song, stem) {
  const ov = currentStemOverrides(song, stem.file);
  const params = previewStemParams(song, stem);
  if (ov.user_confirmed && Number.isFinite(Number(ov.gain_db))) return Number(ov.gain_db);
  if (params && Number.isFinite(Number(params.user_gain_db))) return Number(params.user_gain_db);
  if (params && Number.isFinite(Number(params.gain_db))) return Number(params.gain_db);
  return 0;
}

function stemMakeupGainDb(song, stem) {
  const params = previewStemParams(song, stem);
  return Number(params?.makeup_gain_db ?? 0);
}

function resolvedEqBands(song, stem) {
  const eq = stemEq(song, stem);
  return roleEqBands(stem.role).map(([slot, type, freq, q, gain]) => {
    if (slot === "low") return [slot, type, eq.eq_low_cut_hz, q, gain];
    if (slot === "mid") return [slot, type, freq, q, eq.eq_mid_gain_db];
    if (slot === "air") return [slot, type, freq, q, eq.eq_air_gain_db];
    return [slot, type, freq, q, gain];
  });
}

function baseReverbSendDb(role) {
  return { vocal: -10, sax: -14, horn: -14, guitar: -15, keys: -16, keys_l: -16, keys_r: -16, synth: -17, snare: -20, drums: -24, kick: -42, bass: -42 }[role] ?? null;
}

function baseDelaySendDb(role, leadBonusDb = 0) {
  if (role === "vocal") return -16;
  if (["sax", "horn", "guitar"].includes(role)) return Number(leadBonusDb || 0) > 0 ? -18 : -24;
  if (["keys", "keys_l", "keys_r", "synth"].includes(role) && Number(leadBonusDb || 0) > 0) return -22;
  return null;
}

function numeric(value, fallback = 0) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function stemLeadBonusDb(songIndex, stem) {
  const params = previewStemParams(songIndex, stem);
  return numeric(params?.lead_bonus_db, 0);
}

function effectiveMixDump(songIndex, reason = "preview") {
  const song = appState.songs.find((item) => Number(item.id) === Number(songIndex));
  const activeFiles = new Set(song?.active_stems || []);
  const soloFiles = appState.stems
    .filter((stem) => activeFiles.has(stem.file) && currentStemOverrides(songIndex, stem.file).solo)
    .map((stem) => stem.file);
  const songOv = currentSongOverrides(songIndex);
  const dump = {
    schema: "zucker-effective-mix/v1",
    source: "mix-preview",
    reason,
    song: Number(songIndex),
    visibleIndex: song?.index ?? null,
    activeStemFiles: [...activeFiles].sort(),
    mixedStemFiles: [],
    vocal_bus_db: numeric(songOv.vocal_bus_db, 0),
    target_lufs: numeric(songOv.target_lufs, -14),
    mastering_intensity: String(songOv.mastering_intensity || "natural"),
    master_db: numeric(songOv.master_db, 0),
    bus_processing: {
      vocal_bus: { type: "DynamicsCompressorNode", threshold_db: -12, knee_db: 6, ratio: 2, attack_seconds: 0.15, release_seconds: 0.6 },
      mix_bus: { type: "none in preview" },
      mastering: { type: "none in preview" },
    },
    stems: {},
  };
  const orderedPreviewStems = orderedFaderGroups(songIndex, appState.stems.filter((stem) => activeFiles.has(stem.file)))
    .flatMap(({ linked }) => linked);
  orderedPreviewStems
    .forEach((stem) => {
      const ov = currentStemOverrides(songIndex, stem.file);
      const params = previewStemParams(songIndex, stem) || {};
      const makeupGainDb = numeric(params.makeup_gain_db, 0);
      const userGainDb = stemGainDb(songIndex, stem);
      const preGainDb = makeupGainDb + userGainDb;
      const faderDb = ov.user_confirmed
        ? numeric(ov.fader_db, 0)
        : numeric(params.user_fader_db, 0);
      const muted = Boolean(ov.mute) || faderDb <= -60;
      const solo = Boolean(ov.solo);
      const fxEnabled = ov.fx_enabled !== false;
      const mutedBySolo = Boolean(soloFiles.length && !soloFiles.includes(stem.file));
      const finalGainDb = preGainDb + faderDb;
      const eq = stemEq(songIndex, stem);
      const leadBonusDb = stemLeadBonusDb(songIndex, stem);
      const reverbBaseDb = params.reverb_base_db ?? baseReverbSendDb(stem.role);
      const delayBaseDb = params.delay_base_db ?? baseDelaySendDb(stem.role, leadBonusDb);
      dump.stems[stem.file] = {
        file: stem.file,
        label: stem.label || stem.file,
        role: stem.role,
        makeup_gain_db: makeupGainDb,
        user_gain_db: userGainDb,
        pre_gain_db: preGainDb,
        fader_db: faderDb,
        final_gain_db: finalGainDb,
        final_linear_gain: muted || mutedBySolo ? 0 : dbToGain(finalGainDb),
        mute: muted,
        solo,
        fx_enabled: fxEnabled,
        muted_by_solo: mutedBySolo,
        pan: stemPan(songIndex, stem),
        eq_low_cut_hz: numeric(eq.eq_low_cut_hz),
        eq_mid_gain_db: numeric(eq.eq_mid_gain_db),
        eq_air_gain_db: numeric(eq.eq_air_gain_db),
        lead_bonus_db: leadBonusDb,
        reverb_base_db: reverbBaseDb,
        reverb_send_db: numeric(ov.reverb_send_db, 0),
        reverb_total_db: reverbBaseDb == null ? null : numeric(reverbBaseDb) + numeric(ov.reverb_send_db, 0),
        reverb_linear_gain: reverbBaseDb == null || !fxEnabled ? 0 : dbToGain(numeric(reverbBaseDb) + numeric(ov.reverb_send_db, 0)),
        delay_base_db: delayBaseDb,
        delay_send_db: numeric(ov.delay_send_db, 0),
        delay_total_db: delayBaseDb == null ? null : numeric(delayBaseDb) + numeric(ov.delay_send_db, 0),
        delay_linear_gain: delayBaseDb == null || !fxEnabled ? 0 : dbToGain(numeric(delayBaseDb) + numeric(ov.delay_send_db, 0)),
        track_processing: fxEnabled ? (stem.role === "vocal" ? "per-stem downward expander: AudioWorkletNode" : "no per-stem compressor in preview") : "raw_dry_bypass",
        track_processing_params: stem.role === "vocal" ? {
          threshold_db: previewVocalExpander.thresholdDb,
          ratio: previewVocalExpander.ratio,
          attack_seconds: previewVocalExpander.attackSeconds,
          release_seconds: previewVocalExpander.releaseSeconds,
          knee_db: previewVocalExpander.kneeDb,
          python_max_attenuation_db: previewVocalExpander.maxAttenuationDb,
        } : null,
      };
      if (!muted && !mutedBySolo) dump.mixedStemFiles.push(stem.file);
    });
  dump.mixedStemFiles.sort();
  console.log("[mix-preview effective-mix]", dump);
  console.log("[mix-preview effective-mix json]\n" + JSON.stringify(dump, null, 2));
  return dump;
}

function cloneOverridesPayload() {
  const payload = JSON.parse(JSON.stringify(appState.overrides || { songs: {} }));
  payload.songs ||= {};
  Object.entries(livePreviewOverrides).forEach(([songId, song]) => {
    payload.songs[songId] = JSON.parse(JSON.stringify(song));
  });
  return payload;
}

function syncOverrideSequenceFromState() {
  const seqs = Object.values(appState?.overrides?.songs || {})
    .map((song) => Number(song?._seq || 0))
    .filter(Number.isFinite);
  overrideWriteSeq = Math.max(overrideWriteSeq, ...seqs, 0) + 1;
}

function markOverrideSequence(songIndex) {
  if (songIndex == null) return null;
  const song = songOverrides(songIndex);
  const seq = overrideWriteSeq++;
  const updatedAt = Date.now();
  song._seq = seq;
  song._client_updated_at = updatedAt;
  if (livePreviewOverrides[String(songIndex)]) {
    livePreviewOverrides[String(songIndex)]._seq = seq;
    livePreviewOverrides[String(songIndex)]._client_updated_at = updatedAt;
  }
  pendingOverrideSongs.add(String(songIndex));
  return seq;
}

async function postOverrides(reason = "manual") {
  const payload = cloneOverridesPayload();
  payload.source_folder = appState.source_folder;
  const planSongs = [...pendingOverrideSongs];
  const postedSeqs = Object.fromEntries(Object.entries(payload.songs || {}).map(([songId, song]) => [songId, Number(song?._seq || 0)]));
  console.log("[mix-preview overrides post]", {
    reason,
    pendingSongs: [...pendingOverrideSongs],
    pendingReasons: [...pendingOverrideReasons],
    songs: Object.fromEntries(Object.entries(payload.songs || {}).map(([songId, song]) => [songId, song?._seq ?? null])),
  });
  overrideWritesInFlight += 1;
  try {
    const res = await fetch("/api/overrides", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `override write failed (${res.status})`);
    if (payload.source_folder !== appState.source_folder) return data;
    console.log("[mix-preview overrides ack]", { reason, response: data });
    [...pendingOverrideSongs].forEach((songId) => {
      const currentSeq = Number(appState.overrides?.songs?.[songId]?._seq || 0);
      if (currentSeq <= Number(postedSeqs[songId] || 0)) {
        pendingOverrideSongs.delete(songId);
        if (!openPreviewSongIds().has(songId)) clearLivePreviewSongOverride(songId);
      }
    });
    if (!pendingOverrideSongs.size) pendingOverrideReasons.clear();
    // Changing confirmed Fine Tune parameters invalidates the frozen plan.
    // Rebuild it as an explicit Analyze step, never from inside Render.
    if (!String(reason).includes("before-render")) {
      for (const songId of planSongs) {
        const planRes = await fetch(`/api/analyze-mix/${songId}`, { method: "POST" });
        const planData = await planRes.json().catch(() => ({}));
        if (!planRes.ok) throw new Error(planData.error || `Analyze required for song ${songId}`);
      }
    }
    return data;
  } finally {
    overrideWritesInFlight = Math.max(0, overrideWritesInFlight - 1);
  }
}

async function saveOverrides(options = {}) {
  const songIndexes = options.songIndexes || (options.songIndex == null ? [] : [options.songIndex]);
  songIndexes.forEach(markOverrideSequence);
  pendingOverrideReasons.add(options.reason || "manual");
  if (overrideSaveTimer) {
    clearTimeout(overrideSaveTimer);
    overrideSaveTimer = null;
  }
  return postOverrides(options.reason || "manual");
}

function scheduleOverrideSave(songIndex, reason, delay = 220) {
  const seq = markOverrideSequence(songIndex);
  pendingOverrideReasons.add(reason);
  console.log("[mix-preview overrides scheduled]", {
    song: songIndex,
    reason,
    seq,
    delayMs: delay,
  });
  if (overrideSaveTimer) clearTimeout(overrideSaveTimer);
  overrideSaveTimer = setTimeout(() => {
    overrideSaveTimer = null;
    postOverrides(`debounced:${[...pendingOverrideReasons].join(",")}`)
      .catch((err) => console.error("[mix-preview overrides write failed]", { reason, error: String(err) }));
  }, delay);
}

async function flushOverrideSave(songIndex, reason) {
  markOverrideSequence(songIndex);
  pendingOverrideReasons.add(reason);
  if (overrideSaveTimer) {
    clearTimeout(overrideSaveTimer);
    overrideSaveTimer = null;
  }
  return postOverrides(`flush:${reason}`);
}

async function flushOverrideSaveVisible(songIndex, reason) {
  try {
    return await flushOverrideSave(songIndex, reason);
  } catch (error) {
    console.error("[mix-preview override save failed]", { songIndex, reason, error });
    showToast(`Could not save settings: ${error.message || error}`);
    throw error;
  }
}

async function waitForOverrideWrites() {
  while (overrideWritesInFlight > 0) {
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
}

async function prepareOverridesForRender() {
  // A render must not create a new override revision merely because the
  // render button was pressed. Only genuine pending edits may invalidate the
  // frozen DSP plan; those edits are persisted and analyzed explicitly here.
  await waitForOverrideWrites();
  if (!pendingOverrideSongs.size) return;
  await postOverrides("before-render");
}

function currentOverrideSnapshot(songIndex) {
  return JSON.parse(JSON.stringify(livePreviewOverrides[String(songIndex)] || songOverrides(songIndex)));
}

function persistPreviewChange(songIndex, reason) {
  const status = document.querySelector(`[data-real-status="${songIndex}"]`);
  const player = document.querySelector(`[data-preview-player="${songIndex}"]`);
  if (status) status.textContent = "Settings changed — render a new exact preview.";
  if (player) player.removeAttribute("src");
  console.log("[mix-preview overrides write]", {
    song: songIndex,
    reason,
    overrides: currentOverrideSnapshot(songIndex),
  });
  scheduleOverrideSave(songIndex, reason);
}

async function loadState() {
  let res;
  try {
    const next = await fetchStateSnapshot();
    appState = {
      songs: [], transitions: [], stems: [], settings: {}, audio_scan: {}, jobs: [],
      source_folder: "Not available", ...next,
    };
    if (!Array.isArray(appState.songs)) appState.songs = [];
    // Bind the initial job before the first /api/jobs poll.
    if (appState.detection_job && ["queued", "running", "stopping"].includes(appState.detection_job.status)) {
      setLoadingOverlayJob(appState.detection_job);
    }
  } catch (error) {
    appState = appState || {
      songs: [], transitions: [], stems: [], settings: {}, audio_scan: {}, jobs: [],
      source_folder: "Not available", last_error: error?.message || String(error),
    };
    showToast(`Error loading folder: ${appState.last_error || error?.message || String(error)}`);
  }
  renderBuildInfo();
  renderSlotAudit();
  refreshSlotSummary();
  syncOverrideSequenceFromState();
  const detected = visibleSongs().length;
  const review = visibleSongs().filter((song) => song.needs_review).length;
  const loadError = appState.last_error || appState.audio_scan?.error;
  $("#songCount").textContent = loadError
    ? `Error loading folder: ${loadError}`
    : `${detected} songs${appState.source_integrity?.expected_slot_count === (appState.songs || []).length ? " in saved cuts" : " detected"} / ${renderableSongs().length} ready to render${review ? ` · ${review} suggested boundaries to review` : ""}`;
  if (appState.ffmpeg && !appState.ffmpeg.ok) {
    showToast("ffmpeg is missing. Install it with Homebrew: brew install ffmpeg");
  }
  renderCutTools();
  renderSongs();
  renderResults();
  fetch("/api/redetect/second-pass").then((response) => response.json()).then((candidate) => { if (candidate?.status === "pending_confirmation" || candidate?.status === "incomplete") showSecondPassCandidate(candidate); }).catch(() => {});

}

function visibleSongs() {
  return (Array.isArray(appState?.songs) ? appState.songs : []).filter((song) => !song.skipped);
}

function renderableSongs() {
  // Detection warnings are information for the user, not a silent batch
  // filter. Select Cuts remains the place to correct a suspicious window.
  return visibleSongs();
}

function formatTime(seconds) {
  seconds = Math.max(0, Number(seconds || 0));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function parseTime(text) {
  const raw = String(text || "").trim();
  const normalized = /^\d+\.\d{2}$/.test(raw) ? raw.replace(".", ":") : raw;
  const parts = normalized.split(":").map(Number);
  if (parts.some((x) => Number.isNaN(x))) return null;
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  if (parts.length === 1) return parts[0];
  return null;
}

function parseSplitOffsetSeconds(text) {
  const raw = String(text || "").trim();
  if (!raw) return null;
  const normalized = /^\d+\.\d{2}$/.test(raw) ? raw.replace(".", ":") : raw;
  const parts = normalized.split(":");
  if (parts.length === 2) {
    const minutes = Number(parts[0]);
    const seconds = Number(parts[1]);
    if (!Number.isFinite(minutes) || !Number.isFinite(seconds) || seconds < 0 || seconds >= 60) return null;
    return minutes * 60 + seconds;
  }
  if (parts.length === 3) {
    const hours = Number(parts[0]);
    const minutes = Number(parts[1]);
    const seconds = Number(parts[2]);
    if (!Number.isFinite(hours) || !Number.isFinite(minutes) || !Number.isFinite(seconds)) return null;
    if (minutes < 0 || minutes >= 60 || seconds < 0 || seconds >= 60) return null;
    return hours * 3600 + minutes * 60 + seconds;
  }
  const seconds = Number(normalized);
  return Number.isFinite(seconds) ? seconds : null;
}

function parseSplitList(text) {
  return String(text || "")
    .split(/[,\s]+/)
    .map((part) => part.trim())
    .filter(Boolean)
    .map(parseSplitOffsetSeconds)
    .filter((value) => value != null && value > 0);
}

function isEditingText() {
  const el = document.activeElement;
  return Boolean(el && (el.matches("input, select, textarea") || el.isContentEditable));
}

function hasProtectedPlaybackOrPanel() {
  return Boolean(
    window.renderListening?.isPlaying()
    || document.querySelector(".fine-tune[open]")
    || [...document.querySelectorAll("audio")].some((audio) => !audio.paused && !audio.ended)
  );
}

function songsSignature(songs) {
  return JSON.stringify((songs || []).map((song) => ({
    id: song.id,
    index: song.index,
    skipped: song.skipped,
    custom_name: song.custom_name,
    time: song.time,
    duration: song.duration_text,
    suspicious: song.suspicious,
    speech: song.segment?.speech_text || "",
    musicians: song.segment?.musician_labels || [],
    latest: song.latest_render ? `${song.latest_render.version}:${song.latest_render.created}:${song.latest_render.lufs || ""}:${song.latest_render.mix_source || ""}` : "",
  })));
}

function transitionsSignature(transitions) {
  return JSON.stringify((transitions || []).map((item) => [item.id, item.label, item.time, item.duration_text]));
}

function finishedSignature() {
  return JSON.stringify(visibleSongs().filter((song) => song.latest_render).map((song) => [
    song.index,
    song.latest_render.version,
    song.latest_render.created,
    song.latest_render.title,
    song.previous_render ? song.previous_render.version : "",
  ]));
}

function shortElapsed(started) {
  if (!started) return "";
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - Number(started)));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function formatRemaining(seconds) {
  if (!Number.isFinite(Number(seconds)) || Number(seconds) < 1) return "calculating remaining time";
  const total = Math.round(Number(seconds));
  if (total < 60) return `about ${total}s remaining`;
  return `about ${Math.floor(total / 60)}m ${total % 60}s remaining`;
}

function renderCutTools() {
  const sourcePath = appState?.source_folder || appState?.settings?.source_folder || "Not selected";
  const sourceNode = $("#sourceFolderPath");
  if (sourceNode) sourceNode.textContent = sourcePath;
  const scanNode = $("#audioScanSummary");
  const scan = appState?.audio_scan || {};
  const modeNode = $("#audioScanMode");
  if (modeNode && document.activeElement !== modeNode) {
    modeNode.value = appState?.settings?.audio_scan_mode || scan.mode || "auto";
  }
  const knownCountNode = $("#knownSongCount");
  if (knownCountNode && document.activeElement !== knownCountNode) {
    knownCountNode.value = appState?.source_config?.expected_song_count ?? appState?.settings?.known_song_count ?? "";
  }
  const referenceNode = $("#matcheringReference");
  if (referenceNode && document.activeElement !== referenceNode) {
    referenceNode.value = appState?.settings?.matchering_reference || "";
  }
  const matchering = appState?.matchering;
  if (matchering && !matchering.loading && !matchering.available && matchering.reference) {
    const warning = document.createElement("div");
    warning.className = "scan-warning";
    warning.textContent = `Matchering reference is set but unavailable: ${matchering.import_error || "dependency error"}. Renders will stop until this is fixed.`;
    if (scanNode && !scanNode.querySelector(".matchering-warning")) {
      warning.classList.add("matchering-warning");
      scanNode.appendChild(warning);
    }
  }
  if (scanNode) {
    const accepted = Array.isArray(scan.accepted) ? scan.accepted : [];
    const skipped = Array.isArray(scan.skipped) ? scan.skipped : [];
    const includedWarnings = Array.isArray(scan.included_warnings) ? scan.included_warnings : [];
    const acceptedText = accepted.length
      ? `: ${accepted.map((item) => item.file).join(", ")}`
      : "";
    const skippedText = skipped.length
      ? ` · skipped ${skipped.length}: ${skipped.slice(0, 5).map((item) => `${item.file} (${item.reason})`).join(", ")}${skipped.length > 5 ? "…" : ""}`
      : "";
    const includedWarningText = includedWarnings.length
      ? ` · included with warning ${includedWarnings.length}`
      : "";
    const scanStatus = scan.status ? `<strong>${esc(scan.status)}</strong>` : "";
    const whisper = appState?.whisper || {};
    const whisperStatus = whisper.status === "timed_out"
      ? `<div class="scan-warning">Whisper timed out; song proposals remain available.</div>`
      : (whisper.status === "unavailable" || whisper.status === "skipped")
        ? `<div class="scan-warning">Whisper unavailable; song proposals remain available.</div>`
        : "";
    const scanError = scan.error ? `<div class="scan-warning">Error loading folder: ${esc(scan.error)}</div>` : "";
    const calibration = appState?.detection_calibration || {};
    const slotSummary = calibration.count
      ? `<div class="scan-summary">Total slots detected: ${calibration.count} · Slots ready: ${calibration.ready_count ?? 0} · Slots needs_review: ${calibration.needs_review_count ?? 0} · Slots exported: ${calibration.exported_count ?? 0} · Slots pending: ${calibration.pending_count ?? 0}</div>`
      : "";
    if (scan.fragment_warning) {
      const aligned = Array.isArray(scan.aligned_files) ? scan.aligned_files : [];
      const fragments = Array.isArray(scan.fragment_files) ? scan.fragment_files : [];
      scanNode.innerHTML = `
        <strong class="scan-warning">${esc(scan.fragment_warning)}</strong>
        ${scanStatus}${scanError}${slotSummary}<span>Using all decodable files${includedWarningText}.</span>
        <details>
          <summary>Show included/excluded files</summary>
          <div><strong>Included files (${accepted.length}; aligned exports detected: ${aligned.length})</strong><br>${accepted.map((item) => esc(item.file)).join("<br>") || "none"}</div>
          <div><strong>Included with warning (${includedWarnings.length})</strong><br>${includedWarnings.map((item) => `${esc(item.file)} (${esc(item.reason)})`).join("<br>") || "none"}</div>
          <div><strong>Unreadable or excluded files (${skipped.length})</strong><br>${skipped.map((item) => `${esc(item.file)} (${esc(item.reason)})`).join("<br>") || "none"}</div>
        </details>
      `;
    } else {
      scanNode.innerHTML = `${scanStatus}${slotSummary} <span>Audio scan: accepted ${accepted.length}${esc(acceptedText)}${esc(skippedText)}</span>${scanError}${whisperStatus}`;
    }
  }
  const signature = transitionsSignature(appState.transitions);
  if (signature === transitionSignature || isEditingText()) return;
  transitionSignature = signature;
  const list = $("#transitionList");
  if (!list) return;
  list.innerHTML = "";
  appState.transitions.forEach((transition) => {
    const row = document.createElement("div");
    row.className = "transition-row";
    row.innerHTML = `
      <div>
        <strong>${esc(transition.label)}</strong>
        <p>${esc(transition.time)}${transition.duration_text ? ` · ${esc(transition.duration_text)}` : ""}</p>
      </div>
      <audio controls preload="none" src="/preview/${transition.id}"></audio>
    `;
    list.appendChild(row);
  });
}

function renderSongs() {
  const signature = songsSignature(appState.songs);
  if (signature === songListSignature || isEditingText() || hasProtectedPlaybackOrPanel()) return;
  songListSignature = signature;
  const box = $("#songList");
  box.innerHTML = "";
  appState.songs.forEach((song) => {
    const card = document.createElement("article");
    card.dataset.songId = String(song.id);
    card.className = `song-card ${song.skipped ? "skipped" : ""}`;
    const speed = song.bpm ? `${Math.round(song.bpm)} bpm` : "speed unknown";
    const key = song.key || "key unknown";
    const mixInfo = song.latest_render
      ? `v${song.latest_render.version}${song.latest_render.lufs ? ` · ${Number(song.latest_render.lufs).toFixed(1)} loudness` : ""}`
      : "not mixed yet";
    const mixSource = song.latest_render?.mix_source || "Automatic mix";
    const checked = checkedSongs.has(song.id) ? "checked" : "";
    const label = song.skipped ? `Skipped section ${song.id}` : `Slot ${String(song.index).padStart(2, "0")}`;
    const titleText = song.custom_name ? `${label} — ${song.custom_name}` : `${label} — ${song.duration_text}`;
    card.innerHTML = `
      <div class="song-main">
        <input type="checkbox" ${checked} ${song.skipped ? "disabled" : ""} aria-label="Choose ${esc(label)}">
        <div>
          <div class="song-title">
            <strong>${esc(titleText)}</strong>
            ${song.suspicious ? '<span class="warn" title="This one might contain several songs">⚠</span>' : ""}
            <span class="pill">${speed}</span>
            <span class="pill">${key}</span>
            <span class="pill">${mixInfo}</span>
            ${song.latest_render ? `<span class="pill mix-source">${esc(mixSource)}</span>` : ""}
            ${song.needs_review ? '<span class="pill warn">Needs review</span>' : ""}
            ${song.skipped ? `<span class="pill">${song.skip_reason || "skipped"}</span>` : ""}
          </div>
          <div class="song-sub">${esc(song.time)}</div>
          ${song.segment?.speech_text ? `<div class="song-sub whisper-log"><strong>Announcer:</strong> ${esc(song.segment.speech_text)}</div>` : ""}
          ${song.needs_review ? `<div class="song-sub scan-warning"><strong>Needs review:</strong> ${esc(song.segment?.boundary_validation_reason || song.render_validation || "slot requires review")}</div>` : ""}
          ${song.suspicious || song.number_mismatch ? `<details class="decision-evidence"><summary>Why this boundary?</summary><div>Spoken number: ${esc(song.decision_evidence?.spoken_number ?? "none")} · Introduction: ${song.decision_evidence?.introduction_found ? "found" : "not found"} · Duration rule: ${esc(song.decision_evidence?.duration_rule || "not flagged")} · Boundary: ${esc(song.decision_evidence?.boundary_source || "unknown")}</div></details>` : ""}
          <label class="title-edit">
            <span title="Rename">✎</span>
            <input data-name-input="${song.id}" value="${esc(song.custom_name || "")}" placeholder="Name this mix">
          </label>
        </div>
        <div class="row-actions">
          <label class="skip-toggle"><input type="checkbox" data-skip="${song.id}" ${song.skipped ? "checked" : ""}> Skip this one</label>
          <button data-mix-one="${song.id}" class="accent" ${song.skipped ? "disabled" : ""}>Mix this one</button>
          <button data-select-cuts="${song.id}" class="ghost">Select Cuts</button>
          <button data-reset-auto="${song.id}" class="ghost">Reset to automatic mix</button>
        </div>
      </div>
      ${fineTuneHtml(song)}
    `;
    card.querySelector("input").addEventListener("change", (event) => {
      if (event.target.checked) checkedSongs.add(song.id);
      else checkedSongs.delete(song.id);
      updateSelectedButton();
    });
    card.querySelector("[data-mix-one]").addEventListener("click", () => mixSongs([song.id]).catch((error) => showToast(`Render failed: ${error.message || error}`)));
    card.querySelector("[data-select-cuts]").addEventListener("click", () => openCutSelector(song.id));
    card.querySelector("[data-reset-auto]").addEventListener("click", () => resetSongToAutomatic(song.id));
    card.querySelector("[data-skip]").addEventListener("change", (event) => setSkipped(song.id, event.target.checked));
    card.querySelector("[data-name-input]").addEventListener("change", (event) => saveSongName(song.id, event.target.value));
    box.appendChild(card);
    wireFineTune(card, song.id);
  });
  updateSelectedButton();
  window.renderListening?.sync();
}

let cutSelector = null;

function cutTime(seconds) {
  const total = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(Math.floor(total % 60)).padStart(2, "0")}`;
}

function ensureCutSelector() {
  if (cutSelector) return cutSelector;
  const dialog = document.createElement("dialog");
  dialog.id = "cutSelectorDialog";
  dialog.innerHTML = `
    <form method="dialog" class="cut-selector-shell">
      <div class="cut-selector-header"><div><h2>Edit Cuts</h2><p id="cutSelectorTitle" class="muted"></p></div><div class="cut-slot-nav"><button type="button" id="cutPrevious" class="ghost">Previous</button><span id="cutSlotPosition">Slot 1 of 1</span><select id="cutSlotList" aria-label="All slots"></select><button type="button" id="cutNext" class="ghost">Next</button><button type="button" id="cutClose" class="ghost" aria-label="Close">X</button></div></div>
      <div class="cut-wave-wrap"><canvas id="cutWaveform" aria-label="Selected slot waveform"></canvas><div id="cutPlayheadLine" class="cut-playhead-line" aria-hidden="true"></div><div id="cutMarkers" class="cut-markers"></div><div id="cutSelection" class="cut-selection"><button type="button" class="cut-handle left" aria-label="Move start"></button><button type="button" class="cut-center" aria-label="Move selection"></button><button type="button" class="cut-handle right" aria-label="Move end"></button></div></div>
      <div class="cut-zoom-controls" hidden><label>Zoom <input id="cutZoom" type="range" min="1" max="20" step="0.1" value="1"></label><button type="button" id="cutFit" class="ghost">Fit selected slot</button><button type="button" id="cutZoomSelection" class="ghost">Zoom to selection</button><label>Scroll <input id="cutPan" type="range" min="0" max="1000" step="1" value="0"></label></div>
      <div class="cut-editor-tools"><select id="cutEditMode" hidden><option value="select">Select</option><option value="cut">Cut</option><option value="paste">Paste</option><option value="delete">Delete</option></select><button type="button" id="cutBack5" class="ghost">−5s</button><button type="button" id="cutPlay" hidden>Play</button><button type="button" id="cutPause" hidden>Pause</button><button type="button" id="cutStop" hidden>Stop</button><button type="button" id="cutForward5" class="ghost">+5s</button><label>Speed <select id="cutSpeed"><option>0.5</option><option>0.75</option><option selected>1</option><option>1.25</option><option>1.5</option><option>2</option></select></label><audio id="cutAudio" preload="metadata" hidden></audio><span id="cutPlayheadReadout" class="muted">Playhead —</span><span id="cutModeReadout" class="pill">Active tool: Select</span><span class="muted">Space: play / pause · Arrows: seek · Wheel: zoom · Cmd + wheel: scroll</span><label><input type="checkbox" id="cutSongZero"> First song is song 0</label><span id="cutActionLog" class="cut-action-log" role="status">Waiting for an editor action</span></div>
      <div class="cut-readout"><label>Start <input id="cutStart" type="number" step="0.1"></label><label>End <input id="cutEnd" type="number" step="0.1"></label><strong>Duration <span id="cutDuration">—</span></strong><span id="cutValidation" class="cut-validation"></span></div>
      <div id="cutEvidence" class="cut-evidence"></div>
      <div class="cut-selector-actions"><button id="cutApply" type="button" class="accent">Save Changes</button></div>
    </form><div id="cutContextMenu" class="cut-context-menu" hidden><div class="cut-context-heading">Editing mode</div><button data-context-mode="select">Select</button><button data-context-mode="cut">Cut</button><button data-context-mode="paste">Paste</button><button data-context-mode="delete">Delete</button><div class="cut-context-divider"></div><button data-context-action="add">Add cut here</button><button data-context-action="delete">Delete cut here</button><button data-context-action="copy">Copy cut</button><button data-context-action="paste">Paste cut</button><button data-context-action="play">Play from here</button></div>`;
  document.body.appendChild(dialog);
  dialog.querySelector("#cutClose").dataset.dialogClose = "true";
  makeDialogDismissible(dialog, closeCutSelector);
  dialog.addEventListener("close", () => { cutSelector?.audio?.pause(); releaseCutPointerCaptures(cutSelector); clearCutLoading(); });
  dialog.tabIndex = -1;
  cutSelector = { dialog, canvas: dialog.querySelector("#cutWaveform"), selection: dialog.querySelector("#cutSelection"), start: dialog.querySelector("#cutStart"), end: dialog.querySelector("#cutEnd"), zoom: dialog.querySelector("#cutZoom"), pan: dialog.querySelector("#cutPan"), mode: dialog.querySelector("#cutEditMode"), speed: dialog.querySelector("#cutSpeed"), audio: dialog.querySelector("#cutAudio"), playheadLine: null, pendingAudioTime: null, selectedBoundary: null, clipboard: null, capturedPointers: new Map() };
  wireCutSelector(cutSelector);
  return cutSelector;
}

async function openCutSelector(songId) {
  window.renderListening?.pause();
  const ui = ensureCutSelector();
  suppressLoadingOverlay = false;
  setCutLoading("Opening Edit Cuts", "Reading saved cuts", null);
  const sourceFolder = appState.source_folder; const generation = ui.loadingGeneration = (ui.loadingGeneration || 0) + 1;
  const reuseWaveform = ui.sourceFolder === sourceFolder && ui.data?.global_waveform && ui.waveformKey;
  const response = await fetch(`/api/cuts/${songId}${reuseWaveform ? `?waveform_key=${encodeURIComponent(ui.waveformKey)}` : ""}`);
  const data = await response.json();
  if (!data.global_waveform && reuseWaveform) data.global_waveform = ui.data.global_waveform;
  if (generation !== ui.loadingGeneration || sourceFolder !== appState.source_folder) return;
  ui.waveformKey = data.waveform_key;
  if (!response.ok) { clearCutLoading(); return showToast(data.error || "Could not load cut editor."); }
  setCutLoading("Drawing timeline", "Saved cuts loaded", null);
  ui.songId = songId;
  ui.data = data;
  ui.allSlots = (data.markers || []).map((marker) => Number(marker.song_id)).filter(Number.isFinite);
  ui.slotWindowStart = Number(data.waveform.window_start_sec || data.selection.start_sec || 0);
  ui.slotWindowEnd = Number(data.waveform.window_end_sec || data.selection.end_sec || 0);
  ui.sourceStart = Number(data.global_waveform?.window_start_sec || 0);
  ui.sourceEnd = Number(data.global_waveform?.window_end_sec || ui.slotWindowEnd);
  ui.duration = Math.max(0, ui.sourceEnd - ui.sourceStart);
  ui.startValue = Number(data.selection.start_sec);
  ui.endValue = Number(data.selection.end_sec);
  ui.selectedBoundary = null;
  ui.savedClean = false; ui.localUndo = []; ui.localRedo = []; ui.sourceFolder = appState.source_folder;
  ui.dialog.querySelector("#cutSongZero").checked = data.markers[0]?.display_number === 0;
  // A newly opened editor must have a usable insertion point.  Starting on a
  // boundary makes Add cut look dead because the backend correctly rejects it.
  ui.playhead = ui.startValue + (ui.endValue - ui.startValue) / 2;
  ui.viewStart = ui.sourceStart; ui.viewEnd = ui.sourceEnd; ui.originalStart = ui.startValue; ui.originalEnd = ui.endValue;
  ui.dialog.querySelector("#cutSelectorTitle").textContent = `Song ${data.markers.find(marker => Number(marker.song_id) === Number(songId))?.display_number ?? songId}`;
  const slotIndex = Math.max(0, ui.allSlots.indexOf(Number(songId)));
  ui.dialog.querySelector("#cutSlotPosition").textContent = `Slot ${slotIndex + 1} of ${ui.allSlots.length}`;
  const slotList = ui.dialog.querySelector("#cutSlotList"); slotList.innerHTML = ui.allSlots.map((id, index) => `<option value="${id}">Slot ${index + 1} of ${ui.allSlots.length}</option>`).join(""); slotList.value = String(songId);
  ui.dialog.querySelector("#cutPrevious").disabled = slotIndex <= 0; ui.dialog.querySelector("#cutNext").disabled = slotIndex >= ui.allSlots.length - 1;
  ui.dialog.querySelector("#cutEvidence").innerHTML = data.markers.filter((marker) => marker.song_id === songId).map((marker) => `<span class="cut-evidence-item ${marker.status}">${cutTime(marker.start_sec)}–${cutTime(marker.end_sec)} · ${esc(marker.boundary_source || "automatic proposal")} · ${marker.confidence ? `${Math.round(marker.confidence * 100)}%` : "no confidence"}</span>`).join("");
  const audio = ui.dialog.querySelector("#cutAudio"); if (audio) { audio.preload = "metadata"; if (ui.audioSourceFolder !== ui.sourceFolder) { audio.src = `/api/cuts/audio/${songId}`; ui.audioSourceFolder = ui.sourceFolder; } ui.pendingAudioTime = ui.playhead; audio.playbackRate = Number(ui.speed?.value || 1); audio.oncanplay = () => { if (ui.dialog.open) ui.dialog.querySelector("#cutActionLog").textContent = "Playback ready"; }; audio.onerror = () => { if (ui.dialog.open) showToast("Source audio could not be loaded."); }; }
  setCutLoading("Drawing timeline", "Displaying the source waveform", null);
  drawCutEditor();
  if (!ui.dialog.open) ui.dialog.showModal(); ui.dialog.focus({preventScroll:true});
  setCutLoading("Finished", `Slot ${songId} ready for precise editing`, 100);
  setTimeout(() => clearCutLoading(), 700);
}

function setCutLoading(label, detail, progress = 0) {
  if (progress < 100) progress = null;
  const box = $("#loadingStatus");
  if (box) {
    box.hidden = false;
    $("#loadingLabel").textContent = label;
    $("#loadingDetail").textContent = detail || "";
    $("#loadingProgressFill").classList.toggle("progress-indeterminate", progress == null);
    $("#loadingProgressFill").style.width = progress == null ? "35%" : String(Math.max(0, Math.min(100, progress))) + "%";
  }
  loadingOverlayCut = progress >= 100 ? null : { kind: "cut", label, detail, progress, indeterminate: progress == null, phase_index:0, phase_total:1, current_stage: "loading" };
  renderLoadingOverlay();
}
function reportCutAction(ui, action, details = {}) {
  const before = Number(details.before ?? ui.allSlots?.length ?? 0);
  const after = Number(details.after ?? before);
  const time = Number(details.time ?? ui.playhead ?? ui.startValue ?? 0);
  const tool = ui.mode?.value || "select";
  const pointer = details.pointer || "none";
  const reason = details.reason ? ` reason=${details.reason}` : "";
  const message = `tool=${tool} pointer=${pointer} time=${cutTime(time)} slot=${ui.songId} cuts_before=${before} action=${action} cuts_after=${after} total_slots=${after}${reason}`;
  const log = ui.dialog?.querySelector("#cutActionLog");
  if (log) log.textContent = details.error ? (details.reason || "Could not apply edit") : (details.finished ? "Ready" : "");
  console.info("[cut-editor]", message);
  if (["add", "delete", "merge", "move", "undo", "redo", "numbering"].includes(action) && !details.finished) setCutLoading("Saving cut changes", "Updating the timeline", 35);
  else if (details.error) setCutLoading("Error", details.reason || "Could not apply edit", 100);
}
function clearCutLoading() { loadingOverlayCut = null; renderLoadingOverlay(); const box = $("#loadingStatus"); if (box && !document.querySelector("#cutSelectorDialog[open]")) box.hidden = true; }
function cutIsDirty() { const ui = cutSelector; return Boolean(ui && (Math.abs(ui.startValue - ui.originalStart) > 0.05 || Math.abs(ui.endValue - ui.originalEnd) > 0.05)); }
function rememberCutSelection(ui) { ui.localUndo ||= []; ui.localUndo.push([ui.startValue, ui.endValue]); ui.localRedo = []; }
async function saveCutChanges(ui) {
  if (ui.saving) return false;
  ui.saving = true;
  setCutLoading("Saving cuts", "Writing your timeline to disk", null);
  const button = ui.dialog.querySelector("#cutApply"); button.disabled = true; button.textContent = "Saving…";
  try {
    const response = await fetch(`/api/segment-selection/${ui.songId}`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({start_sec:ui.startValue,end_sec:ui.endValue,source_folder:ui.sourceFolder})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not save cuts");
    ui.originalStart = ui.startValue; ui.originalEnd = ui.endValue; ui.localUndo = []; ui.localRedo = [];
    button.textContent = "Saved"; ui.dialog.querySelector("#cutValidation").textContent = "Changes saved";
    applySavedEditorState(result); showToast("Cuts saved."); return true;
  } catch (error) { button.textContent = "Save Changes"; showToast(error.message || error); return false; }
  finally { ui.saving = false; button.disabled = false; clearCutLoading(); }
}
async function resolveUnsavedCuts() {
  if (!cutIsDirty()) return true;
  if (await showConfirm("Save changes?", "Yes", "No")) return saveCutChanges(cutSelector);
  return true;
}
function applySavedEditorState(result) {
  if (!Array.isArray(result.songs)) throw new Error("Saved cuts did not return a song list.");
  appState.songs = result.songs;
  appState.candidate_pending = false; appState.segmentation_status = "ready";
  appState.source_integrity = {...appState.source_integrity, status:"ok", saved_slot_count:result.slot_count, expected_slot_count:result.slot_count, warning:""};
  appState.slot_audit = {...appState.slot_audit, state_slot_count:result.slot_count, backend_slot_count:result.slot_count, visible_slot_count:result.slot_count, integrity_warning:""};
  appState.detection_calibration = {...appState.detection_calibration, candidate_pending:false};
  if (result.overrides) appState.overrides = result.overrides;
  checkedSongs = new Set([...checkedSongs].filter(id => result.songs.some(song => song.id === id && !song.skipped)));
  cutSelector.timelineChanged = true;
  refreshSlotSummary();
  renderSlotAudit();
  const review = visibleSongs().filter(song => song.needs_review).length;
  $("#songCount").textContent = `${result.songs.length} songs in saved cuts${review ? ` · ${review} suggested boundaries to review` : ""}`;
}

async function closeCutSelector() {
  if (cutSelector.closing || cutSelector.saving) return;
  cutSelector.closing = true;
  try { if (await resolveUnsavedCuts()) {
    cutSelector.audio?.pause(); cutSelector.dialog.close(); clearCutLoading();
    if (cutSelector.timelineChanged) {
      setCutLoading("Updating song list", `${appState.songs.length} saved songs`, null);
      await Promise.all(Object.keys(previewMixes).map(id => stopPreviewMix(id)));
      document.querySelectorAll(".fine-tune[open]").forEach(panel => panel.open = false);
      songListSignature = ""; renderCutTools(); renderSongs(); updateSelectedButton();
      cutSelector.timelineChanged = false;
    }
  } }
  finally { cutSelector.closing = false; clearCutLoading(); }
}
async function editAllCuts() {
  const slots = (appState.songs || []).filter((song) => Number.isFinite(Number(song.id))).map((song) => Number(song.id));
  if (!slots.length) return showToast("No slots available for Edit All.");
  const ui = ensureCutSelector();
  ui.editAllQueue = slots.slice(1);
  await openCutSelector(slots[0]);
}


function movePlayheadTo(ui, seconds, { seek = true, redraw = true } = {}) {
  if (!ui) return;
  const next = Math.max(ui.sourceStart ?? 0, Math.min(ui.sourceEnd ?? Number(seconds), Number(seconds)));
  ui.playhead = Number.isFinite(next) ? next : (ui.playhead || ui.sourceStart || 0);
  if (seek && ui.audio) {
    const local = Math.max(0, ui.playhead);
    if (Number.isFinite(ui.audio.duration) && ui.audio.readyState >= 1) ui.audio.currentTime = Math.min(local, Math.max(0, ui.audio.duration));
    else ui.pendingAudioTime = local;
  }
  if (redraw) drawCutEditor();
}

function drawCutEditor() {
  const ui = cutSelector;
  if (!ui?.data) return;
  const canvas = ui.canvas;
  const width = Math.max(700, canvas.clientWidth || 900);
  const height = 190;
  canvas.width = width * devicePixelRatio; canvas.height = height * devicePixelRatio;
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext("2d"); ctx.scale(devicePixelRatio, devicePixelRatio);
  ctx.fillStyle = "#100e0c"; ctx.fillRect(0, 0, width, height);
  const globalWaveform = ui.data.global_waveform || ui.data.waveform;
  const peaks = globalWaveform.peaks || [];
  const visibleStart = ui.viewStart ?? ui.sourceStart ?? 0; const visibleEnd = ui.viewEnd ?? ui.sourceEnd ?? ui.duration; const visibleSpan = Math.max(0.001, visibleEnd - visibleStart);
  ctx.strokeStyle = "#eca35e"; ctx.globalAlpha = .8; ctx.beginPath();
  const pixelPeaks = new Float32Array(width);
  const waveStart = Number(globalWaveform.window_start_sec || 0), waveDuration = Number(globalWaveform.duration_sec || ui.duration);
  const firstPeak = Math.max(0, Math.floor((visibleStart - waveStart) / waveDuration * (peaks.length - 1)));
  const lastPeak = Math.min(peaks.length - 1, Math.ceil((visibleEnd - waveStart) / waveDuration * (peaks.length - 1)));
  for (let index = firstPeak; index <= lastPeak; index++) {
    const seconds = waveStart + index / Math.max(1, peaks.length - 1) * waveDuration;
    const pixel = Math.floor((seconds - visibleStart) / visibleSpan * width);
    if (pixel >= 0 && pixel < width) pixelPeaks[pixel] = Math.max(pixelPeaks[pixel], Number(peaks[index]));
  }
  pixelPeaks.forEach((value, pixel) => { const extent = value * height * .42; ctx.moveTo(pixel, height / 2 + extent); ctx.lineTo(pixel, height / 2 - extent); }); ctx.stroke(); ctx.globalAlpha = 1;
  const x = (seconds) => Math.max(0, Math.min(width, (Number(seconds) - visibleStart) / visibleSpan * width));
  const sx = x(ui.startValue), ex = x(ui.endValue);
  ctx.fillStyle = "rgba(200,111,47,.23)"; ctx.fillRect(sx, 0, Math.max(0, ex - sx), height);
  ctx.strokeStyle = "#ffd08f"; ctx.lineWidth = 2; [sx, ex].forEach((point) => { ctx.beginPath(); ctx.moveTo(point, 0); ctx.lineTo(point, height); ctx.stroke(); });
  ui.selection.hidden = ui.endValue <= visibleStart || ui.startValue >= visibleEnd;
  ui.dialog.querySelector(".cut-handle.left").hidden = ui.startValue < visibleStart || ui.startValue > visibleEnd;
  ui.dialog.querySelector(".cut-handle.right").hidden = ui.endValue < visibleStart || ui.endValue > visibleEnd;
  ui.selection.style.left = `${sx / width * 100}%`; ui.selection.style.width = `${Math.max(0, (ex - sx) / width * 100)}%`;
  if (document.activeElement !== ui.start) ui.start.value = ui.startValue.toFixed(1); if (document.activeElement !== ui.end) ui.end.value = ui.endValue.toFixed(1);
  const duration = ui.endValue - ui.startValue;
  ui.dialog.querySelector("#cutDuration").textContent = `${duration.toFixed(1)} s (${cutTime(duration)})`;
  const valid = Number.isFinite(duration) && duration >= 0.1 && ui.startValue >= ui.sourceStart && ui.endValue <= ui.sourceEnd;
  const validation = ui.dialog.querySelector("#cutValidation"); validation.textContent = valid ? (duration >= 480 && duration <= 780 ? "Valid selection" : "Manual duration — will be preserved") : "Choose a positive range inside the recording"; validation.className = `cut-validation ${valid ? "valid" : "invalid"}`;
  ui.dialog.querySelector("#cutApply").disabled = !valid;
  const markers = ui.dialog.querySelector("#cutMarkers"); markers.innerHTML = (ui.data.markers || []).flatMap((marker) => [[marker.start_sec, marker.song_id === ui.songId ? "selected" : "", marker.song_id], [marker.end_sec, marker.song_id === ui.songId ? "selected" : "", marker.song_id], [marker.comment_start_sec, "comment", marker.song_id]]).filter(([value]) => Number.isFinite(Number(value)) && Number(value) >= visibleStart && Number(value) <= visibleEnd).map(([value, kind, songId]) => `<i class="${kind}" data-boundary="${Number(value)}" data-song="${songId}" style="left:${x(value) / width * 100}%" title="Slot boundary ${cutTime(value)}"></i>`).join("");
  markers.querySelectorAll("i[data-boundary]").forEach((node) => node.addEventListener("click", (event) => {
    event.stopPropagation();
    ui.selectedBoundary = Number(node.dataset.boundary);
    ui.playhead = ui.selectedBoundary;
    reportCutAction(ui, "select-marker", { pointer: "marker", time: ui.playhead, finished: true });
    // drawCutEditor is global, so route the destructive action through the
    // wired button instead of reaching into wireCutSelector's local closure.
    if (ui.mode?.value === "delete") ui.editorOperation?.("delete", { pointer: "marker" }); else drawCutEditor();
  }));
  if (ui.zoom) ui.zoom.value = Math.max(1, Math.min(20, ui.duration / visibleSpan));
  if (ui.playhead == null) ui.playhead = ui.startValue + (ui.endValue - ui.startValue) / 2;
  const playhead = x(ui.playhead); ctx.strokeStyle = "#79d7c4"; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(playhead, 0); ctx.lineTo(playhead, height); ctx.stroke();
  if (ui.playheadLine) { ui.playheadLine.hidden = ui.playhead < visibleStart || ui.playhead > visibleEnd; ui.playheadLine.style.left = String(playhead / width * 100) + "%"; }
  const readout = ui.dialog.querySelector("#cutPlayheadReadout"); if (readout) readout.textContent = `Playhead ${cutTime(ui.playhead)}`;
}

function installCutDrag(element, mode) {
  element.addEventListener("pointerdown", (event) => {
    event.preventDefault(); const ui = cutSelector; rememberCutSelection(ui); const rect = ui.canvas.getBoundingClientRect(); const startX = event.clientX; const originalStart = ui.startValue; const originalEnd = ui.endValue; const viewSpan = ui.viewEnd - ui.viewStart;
    ui.capturedPointers.set(event.pointerId, element);
    element.setPointerCapture(event.pointerId);
    const move = (current) => { const delta = (current.clientX - startX) / rect.width * viewSpan; if (mode === "left") ui.startValue = Math.max(ui.sourceStart, Math.min(originalEnd - 0.1, originalStart + delta)); else if (mode === "right") ui.endValue = Math.min(ui.sourceEnd, Math.max(originalStart + 0.1, originalEnd + delta)); else { const span = originalEnd - originalStart; const next = Math.max(ui.sourceStart, Math.min(ui.sourceEnd - span, originalStart + delta)); ui.startValue = next; ui.endValue = next + span; } drawCutEditor(); };
    const onMove = (moveEvent) => move(moveEvent); const onUp = () => { try { if (element.hasPointerCapture(event.pointerId)) element.releasePointerCapture(event.pointerId); } catch (_) {} ui.capturedPointers.delete(event.pointerId); element.removeEventListener("pointermove", onMove); element.removeEventListener("pointerup", onUp); };
    element.addEventListener("pointermove", onMove); element.addEventListener("pointerup", onUp, { once: true });
  });
}

function releaseCutPointerCaptures(ui) {
  for (const [pointerId, element] of ui.capturedPointers || []) {
    try { if (element.hasPointerCapture(pointerId)) element.releasePointerCapture(pointerId); } catch (_) {}
  }
  ui.capturedPointers?.clear();
  reportCutAction(ui, "pointer capture released", { pointer: "contextmenu", time: ui.playhead, finished: true });
}

function wireCutSelector(ui) {
  installCutDrag(ui.dialog.querySelector(".cut-handle.left"), "left"); installCutDrag(ui.dialog.querySelector(".cut-handle.right"), "right"); installCutDrag(ui.dialog.querySelector(".cut-center"), "center");
  [ui.start, ui.end].forEach((input) => input.addEventListener("input", () => { rememberCutSelection(ui); ui.startValue = Number(ui.start.value); ui.endValue = Number(ui.end.value); drawCutEditor(); }));
  ui.zoom.addEventListener("input", () => { const span = ui.duration / Number(ui.zoom.value); const center = (ui.viewStart + ui.viewEnd) / 2; ui.viewStart = Math.max(ui.sourceStart, Math.min(ui.sourceEnd - span, center - span / 2)); ui.viewEnd = ui.viewStart + span; drawCutEditor(); });
  ui.pan.addEventListener("input", () => { const span = ui.viewEnd - ui.viewStart; const max = Math.max(0, ui.duration - span); ui.viewStart = ui.sourceStart + max * Number(ui.pan.value) / 1000; ui.viewEnd = ui.viewStart + span; drawCutEditor(); });
  ui.dialog.querySelector("#cutFit").addEventListener("click", () => { ui.viewStart = ui.slotWindowStart; ui.viewEnd = ui.slotWindowEnd; drawCutEditor(); });
  ui.dialog.querySelector("#cutZoomSelection").addEventListener("click", () => { const span = Math.min(ui.duration, Math.max(ui.endValue - ui.startValue, (ui.endValue - ui.startValue) * 1.2)); const center = (ui.startValue + ui.endValue) / 2; ui.viewStart = Math.max(ui.sourceStart, Math.min(ui.sourceEnd - span, center - span / 2)); ui.viewEnd = ui.viewStart + span; drawCutEditor(); });
  const setMode = (mode) => {
    ui.mode.value = mode;
    const text = ui.mode.options[ui.mode.selectedIndex].text;
    ui.dialog.querySelector("#cutModeReadout").textContent = `Active tool: ${text}`;
    const selecting = mode === "select";
    ui.dialog.querySelector(".cut-center").style.pointerEvents = selecting ? "auto" : "none";
    ui.dialog.querySelectorAll(".cut-handle").forEach((handle) => { handle.style.pointerEvents = selecting ? "auto" : "none"; });
    reportCutAction(ui, `tool=${mode}`, { pointer: "mode", time: ui.playhead, finished: true });
  };
  ui.mode.addEventListener("change", () => setMode(ui.mode.value));
  setMode("select");
  const refreshEditorAfterOperation = async (result, action, before, at, pointer) => {
    const view = [ui.viewStart, ui.viewEnd];
    setCutLoading("Updating editor", "Cuts saved", null);
    applySavedEditorState(result);
    const atIndex = (result.segments || []).findIndex(segment => segment.start <= at && at < segment.end);
    await openCutSelector(atIndex >= 0 ? atIndex + 1 : Math.min(Number(ui.songId), result.slot_count));
    ui.viewStart = Math.max(ui.sourceStart, view[0]); ui.viewEnd = Math.min(ui.sourceEnd, view[1]);
    movePlayheadTo(ui, at, {seek:false});
    reportCutAction(ui,action,{before,after:result.slot_count,time:at,pointer,finished:true});
    clearCutLoading();
  };
  const editorOperation = async (operation, extra = {}) => {
    if (ui.operationPending || ui.saving) return;
    ui.operationPending = true;
    try {
    await prepareOverridesForRender();
    if (cutIsDirty() && !await saveCutChanges(ui)) return;
    const destructive = ["delete", "merge"].includes(operation);
    const at = extra.at_sec ?? (destructive ? (ui.selectedBoundary ?? ui.playhead) : ui.playhead);
    const before = ui.allSlots?.length || 0;
    const pointer = extra.pointer || "button";
    reportCutAction(ui, operation, { before, time: at, pointer });
    const response = await fetch("/api/editor-cut-operation", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ operation, at_sec: at, source_folder:ui.sourceFolder, ...extra }) });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) { reportCutAction(ui, `${operation} rejected`, { before, time: at, pointer, reason: result.error || "unknown error", error: true }); return showToast(result.error || `Could not ${operation}.`); }
    await Promise.all(Object.keys(previewMixes).map(id => stopPreviewMix(id)));
    document.querySelectorAll(".fine-tune[open]").forEach(panel => panel.open = false);
    Object.keys(livePreviewOverrides).forEach(key => delete livePreviewOverrides[key]);
    ui.selectedBoundary = null;
    await refreshEditorAfterOperation(result, operation, before, at, pointer);
    } catch (error) { showToast(error.message || error); clearCutLoading(); }
    finally { ui.operationPending = false; }
  };
  ui.editorOperation = editorOperation;
  const waveformClick = (event) => { const rect = ui.canvas.getBoundingClientRect(); const at = ui.viewStart + Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * (ui.viewEnd - ui.viewStart); movePlayheadTo(ui, at); const before = ui.allSlots?.length || 0; reportCutAction(ui, "left-click", { before, time: ui.playhead, pointer: "waveform" }); if (ui.mode.value === "cut") editorOperation("add", { pointer: "waveform" }); else if (ui.mode.value === "paste") { if (ui.clipboard) editorOperation("add", { at_sec: ui.playhead, pointer: "waveform-paste" }); else reportCutAction(ui, "paste rejected", { before, time: ui.playhead, pointer: "waveform", reason: "No copied cut or selection", error: true }); } };
  const waveform = ui.dialog.querySelector(".cut-wave-wrap");
  ui.playheadLine = ui.dialog.querySelector("#cutPlayheadLine");
  waveform.addEventListener("pointerdown", (event) => {
    if (ui.mode.value !== "select" || event.target.closest("#cutMarkers, .cut-selection")) return;
    const rect = ui.canvas.getBoundingClientRect();
    const at = ui.viewStart + Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * (ui.viewEnd - ui.viewStart);
    waveform.setPointerCapture?.(event.pointerId);
    movePlayheadTo(ui, at);
    const move = (moveEvent) => {
      const next = ui.viewStart + Math.max(0, Math.min(1, (moveEvent.clientX - rect.left) / rect.width)) * (ui.viewEnd - ui.viewStart);
      movePlayheadTo(ui, next);
    };
    const up = () => {
      try { if (waveform.hasPointerCapture?.(event.pointerId)) waveform.releasePointerCapture(event.pointerId); } catch (_) {}
      waveform.removeEventListener("pointermove", move);
      waveform.removeEventListener("pointerup", up);
    };
    waveform.addEventListener("pointermove", move);
    waveform.addEventListener("pointerup", up, { once: true });
  });
  waveform.addEventListener("click", (event) => { if (event.target.closest("#cutMarkers, .cut-selection")) return; waveformClick(event); });
  ui.speed.addEventListener("change", () => { if (ui.audio) ui.audio.playbackRate = Number(ui.speed.value); });
  ui.dialog.querySelector("#cutPlay").addEventListener("click", () => { if (!ui.audio) return; movePlayheadTo(ui, ui.playhead ?? ui.startValue); (window.renderListening?.pause(), ui.audio.play()).catch((error) => setCutLoading("Playback error", error.message, 100)); });
  ui.dialog.querySelector("#cutPause").addEventListener("click", () => ui.audio?.pause());
  ui.dialog.querySelector("#cutStop").addEventListener("click", () => { if (ui.audio) { ui.audio.pause(); ui.audio.currentTime = 0; } });
  ui.dialog.querySelector("#cutBack5").addEventListener("click", () => { if (ui.audio) ui.audio.currentTime = Math.max(0, ui.audio.currentTime - 5); });
  ui.dialog.querySelector("#cutForward5").addEventListener("click", () => { if (ui.audio) ui.audio.currentTime = Math.min(ui.audio.duration || Infinity, ui.audio.currentTime + 5); });
  ui.audio?.addEventListener("loadstart", () => setCutLoading("Loading audio preview", "Loading audio chunk", 60));
  ui.audio?.addEventListener("loadedmetadata", () => {
    if (ui.pendingAudioTime != null && Number.isFinite(ui.audio.duration)) {
      ui.audio.currentTime = Math.min(ui.pendingAudioTime, ui.audio.duration);
      ui.pendingAudioTime = null;
    }
  });
  ui.audio?.addEventListener("timeupdate", () => {
    if (!Number.isFinite(ui.audio.duration)) return;
    ui.playhead = Math.max(ui.sourceStart || 0, Math.min(ui.sourceEnd || Infinity, ui.audio.currentTime));
    drawCutEditor();
  });
  ui.audio?.addEventListener("waiting", () => setCutLoading("Preparing playback", "Loading audio chunk", 70));
  ui.audio?.addEventListener("canplay", () => setCutLoading("Ready", "Audio preview ready", 100));
  const contextMenu = ui.dialog.querySelector("#cutContextMenu");
  const closeContextMenu = (reason = "context menu closed") => {
    if (!contextMenu.isConnected) return;
    contextMenu.hidden = true;
    releaseCutPointerCaptures(ui);
    contextMenu.remove();
    reportCutAction(ui, reason, { pointer: "contextmenu", time: ui.playhead, finished: true });
    ui.dialog.focus({ preventScroll: true });
  };
  ui.dialog.addEventListener("contextmenu", (event) => {
    const wave = event.target.closest(".cut-wave-wrap");
    if (!wave) return;
    event.preventDefault();
    event.stopPropagation();
    const rect = ui.canvas.getBoundingClientRect();
    movePlayheadTo(ui, ui.viewStart + Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * (ui.viewEnd - ui.viewStart));
    const before = ui.allSlots?.length || 0;
    reportCutAction(ui, "context menu opened", { before, time: ui.playhead, pointer: `contextmenu(${Math.round(event.clientX)},${Math.round(event.clientY)})`, finished: true });
    if (!contextMenu.isConnected) ui.dialog.appendChild(contextMenu);
    contextMenu.hidden = false;
    const dialogRect = ui.dialog.getBoundingClientRect();
    contextMenu.style.left = `${Math.max(4, Math.min(event.clientX - dialogRect.left, ui.dialog.clientWidth - contextMenu.offsetWidth - 8))}px`;
    contextMenu.style.top = `${Math.max(4, Math.min(event.clientY - dialogRect.top, ui.dialog.clientHeight - contextMenu.offsetHeight - 8))}px`;
  });
  contextMenu.querySelectorAll("[data-context-mode]").forEach((button) => button.addEventListener("click", () => { const mode = button.dataset.contextMode; reportCutAction(ui, "menu item selected", { pointer: "contextmenu", time: ui.playhead, finished: true }); closeContextMenu(); setMode(mode); reportCutAction(ui, `selected mode=${mode}`, { pointer: "contextmenu", time: ui.playhead, finished: true }); }));
  contextMenu.querySelectorAll("[data-context-action]").forEach((button) => button.addEventListener("click", () => { const action = button.dataset.contextAction; const before = ui.allSlots?.length || 0; closeContextMenu("context menu closed"); if (action === "copy") { ui.clipboard = { at: ui.selectedBoundary ?? ui.playhead, duration: ui.endValue - ui.startValue }; reportCutAction(ui, "copy", { before, time: ui.playhead, pointer: "contextmenu", finished: true }); } else if (action === "paste") { if (ui.clipboard) editorOperation("add", { at_sec: ui.playhead, pointer: "contextmenu-paste" }); else reportCutAction(ui, "paste rejected", { before, time: ui.playhead, pointer: "contextmenu", reason: "No copied cut or selection", error: true }); } else if (action === "play") ui.dialog.querySelector("#cutPlay").click(); else editorOperation(action === "delete" ? "delete" : "add", { pointer: "contextmenu" }); }));
  ui.dialog.addEventListener("click", (event) => { if (contextMenu.isConnected && !event.target.closest("#cutContextMenu")) closeContextMenu(); });
  waveform.addEventListener("wheel", (event) => {
    event.preventDefault();
    const rect = ui.canvas.getBoundingClientRect(); const span = ui.viewEnd - ui.viewStart;
    let nextSpan = span, nextStart;
    if (event.metaKey || event.ctrlKey) nextStart = ui.viewStart + (event.deltaX || event.deltaY) / rect.width * span;
    else { const fraction = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)); nextSpan = Math.max(1, Math.min(ui.duration, span * Math.exp(event.deltaY * .002))); nextStart = ui.viewStart + span * fraction - nextSpan * fraction; }
    ui.viewStart = Math.max(ui.sourceStart, Math.min(ui.sourceEnd - nextSpan, nextStart)); ui.viewEnd = ui.viewStart + nextSpan; drawCutEditor();
  }, {passive:false});
  ui.dialog.querySelector("#cutSongZero").addEventListener("change", async (event) => {
    if (!await resolveUnsavedCuts()) { event.target.checked = !event.target.checked; return; }
    await editorOperation("numbering", {first_song_number:event.target.checked ? 0 : 1});
  });
  ui.dialog.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea, select, [contenteditable=true]")) return;
    const command = event.metaKey || event.ctrlKey; const key = event.key.toLowerCase();
    if (event.code === "Space") { event.preventDefault(); if (!event.repeat) ui.audio?.paused ? ui.dialog.querySelector("#cutPlay").click() : ui.dialog.querySelector("#cutPause").click(); }
    else if (command && key === "s") { event.preventDefault(); saveCutChanges(ui); }
    else if (command && key === "z") { event.preventDefault(); const from = event.shiftKey ? ui.localRedo : ui.localUndo; const to = event.shiftKey ? ui.localUndo : ui.localRedo; if (from?.length) { to.push([ui.startValue,ui.endValue]); [ui.startValue,ui.endValue] = from.pop(); drawCutEditor(); } else if (!cutIsDirty()) editorOperation(event.shiftKey ? "redo" : "undo", {pointer:"keyboard"}); }
    else if (command && key === "c") { event.preventDefault(); ui.clipboard = {at:ui.selectedBoundary ?? ui.playhead, sourceFolder:ui.sourceFolder}; reportCutAction(ui,"copy",{pointer:"keyboard",finished:true}); }
    else if (command && key === "v") { event.preventDefault(); if (ui.clipboard?.sourceFolder === ui.sourceFolder) editorOperation("add", {at_sec:ui.playhead,pointer:"keyboard"}); }
    else if (event.key === "Delete" || event.key === "Backspace") { event.preventDefault(); editorOperation("delete", {pointer:"keyboard"}); }
    else if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); movePlayheadTo(ui,ui.playhead+(event.key === "ArrowLeft" ? -1 : 1)*(event.shiftKey ? 30 : 5)); }
    else if (!command && ["c","s","p","d"].includes(key)) setMode({c:"cut",s:"select",p:"paste",d:"delete"}[key]);
  });
  const navigate = async (offset) => { if (!await resolveUnsavedCuts()) return; const index = ui.allSlots.indexOf(Number(ui.songId)); const next = ui.allSlots[index + offset]; if (next != null) await openCutSelector(next); };
  ui.dialog.querySelector("#cutPrevious").addEventListener("click", () => navigate(-1));
  ui.dialog.querySelector("#cutNext").addEventListener("click", () => navigate(1));
  ui.dialog.querySelector("#cutSlotList").addEventListener("change", async (event) => { if (!await resolveUnsavedCuts()) { event.target.value = String(ui.songId); return; } await openCutSelector(Number(event.target.value)); });
  ui.dialog.querySelector("#cutApply").addEventListener("click", () => saveCutChanges(ui));
}

function fineTuneHtml(song) {
  return `
    <details class="fine-tune">
      <summary>Fine-tune</summary>
      <div class="fine-preview-bar">
        <div class="preview-transport" data-preview-transport="${song.id}">
          <label class="preview-label"><strong>Preview</strong>
            <button data-preview-render="${song.id}" class="accent" type="button">Preview render · 30s</button>
            <span class="muted">Always renders a fresh exact 30-second excerpt using the current settings.</span>
          </label>
          <div data-quick-controls="${song.id}">
          <div class="preview-label"><strong>Full song · approximate</strong> <span class="warn">Dynamics and mastering are approximated.</span></div>
          <button data-preview-toggle="${song.id}" class="accent">Play Preview</button>
          <input data-preview-seek="${song.id}" type="range" min="0" max="0" step="0.1" value="0" disabled>
          <span data-preview-time="${song.id}">00:00:00 / 00:00:00</span>
          <span class="muted" data-preview-cache="${song.id}">Audio loads when you press Play Preview</span>
          <span class="warn" data-preview-warning="${song.id}" hidden></span>
          </div>
          <div data-real-controls="${song.id}" hidden>
            <div class="muted" data-real-status="${song.id}">Press Preview render to create an exact centered 30-second excerpt.</div>
            <audio data-preview-player="${song.id}" controls preload="none"></audio>
          </div>
        </div>
      </div>
      <div class="fine-body">
        <div class="auto-mix-balance" data-auto-mix-balance="${song.id}"></div>
        <div class="faders" data-faders="${song.id}"></div>
        <div class="simple-sliders">
          <label>Echo <input data-simple="delay_send_db" type="range" min="-18" max="12" step="0.5"><span></span></label>
          <label>Space <input data-simple="reverb_send_db" type="range" min="-18" max="12" step="0.5"><span></span></label>
          <label>Voices level <input data-song="vocal_bus_db" type="range" min="-12" max="6" step="0.5"><span></span></label>
          <label>Overall loudness <input data-song="target_lufs" type="range" min="-16" max="-8" step="0.5"><span></span></label>
          <label>Master
            <select data-mastering="${song.id}">
              <option value="natural">Natural</option>
              <option value="loud">Loud</option>
            </select>
          </label>
          <button data-mix-settings="${song.id}" class="accent">Mix with these settings</button>
        </div>
      </div>
    </details>
  `;
}

function wireFineTune(root, songIndex) {
  const faders = root.querySelector(`[data-faders="${songIndex}"]`);
  faders.innerHTML = '<div class="muted">Open fine-tune to load active tracks.</div>';
  const details = root.querySelector(".fine-tune");
  details.addEventListener("toggle", async () => {
    if (details.open) {
      await closeOtherFineTunePanels(details, songIndex);
      await loadActiveStemFaders(root, songIndex);
    } else {
      await stopPreviewMix(songIndex);
    }
  });
  if (details.open) {
    loadActiveStemFaders(root, songIndex);
  }
  wireFineTuneControls(root, songIndex);
}

async function loadActiveStemFaders(root, songIndex) {
  const faders = root.querySelector(`[data-faders="${songIndex}"]`);
  if (!faders) return;
  if (activeStemLoadPromises[String(songIndex)]) return activeStemLoadPromises[String(songIndex)];
  if (faders.dataset.loaded === "true") return;
  const load = (async () => {
  faders.dataset.loading = "true";
  faders.innerHTML = '<div class="muted">Preparing this song’s balance, gates and effects…</div>';
  try {
    const res = await fetch(`/api/active-stems/${songIndex}`);
    if (!res.ok) throw new Error("active stems unavailable");
    const data = await res.json();
    const song = appState.songs.find((item) => Number(item.id) === Number(songIndex));
    if (song) {
      song.active_stems = data.active_stems || [];
      song.mix_params = data.mix_params || null;
      const balance = data.mix_params?.auto_mix_balance || {};
      const guitar = balance.guitar_reduction_db;
      const original = balance.guitar_original_level_db;
      const reason = balance.guitar_reduction_reason || "no reliable vocal overlap evidence";
      const summary = root.querySelector(`[data-auto-mix-balance="${songIndex}"]`);
      if (summary && Number.isFinite(Number(guitar))) {
        summary.textContent = `Auto-Mix guitars: original ${Number(original).toFixed(1)} dB · balance ${signedDb(guitar)} · ${reason}`;
      }
    }
    renderFaders(root, songIndex);
  } catch (_err) {
    faders.innerHTML = '<div class="muted">Could not load active tracks.</div>';
  } finally {
    faders.dataset.loading = "false";
  }
  })();
  activeStemLoadPromises[String(songIndex)] = load;
  try {
    return await load;
  } finally {
    delete activeStemLoadPromises[String(songIndex)];
  }
}

function renderFaders(root, songIndex) {
  const faders = root.querySelector(`[data-faders="${songIndex}"]`);
  faders.innerHTML = "";
  faders.dataset.loaded = "true";
  const song = appState.songs.find((item) => Number(item.id) === Number(songIndex));
  const activeFiles = new Set(song?.active_stems || []);
  if (!activeFiles.size) {
    faders.innerHTML = '<div class="muted">No active tracks found for this song.</div>';
    return;
  }
  const activeStems = appState.stems.filter((stem) => activeFiles.has(stem.file));
  orderedFaderGroups(songIndex, activeStems).forEach(({ stem, linked }) => {
    const linkedFiles = linked.map((item) => item.file);
    const linkedLabel = linked.length > 1 ? `${linked.map((item) => plainName(item)).join(" / ")} · linked stereo pair` : plainName(stem);
    const ov = stemOverrides(songIndex, stem.file);
    const params = previewStemParams(songIndex, stem);
    if (linked.length > 1) {
      ["gain_db", "fader_db", "mute", "solo", "fx_enabled", "eq_low_cut_hz", "eq_mid_gain_db", "eq_air_gain_db"].forEach((key) => {
        if (ov[key] !== undefined) setLinkedOverride(songIndex, linked, key, ov[key]);
      });
    }
    if (params) {
      if (!ov.gate_user_confirmed && typeof params.gate_enabled === "boolean") {
        ov.gate_enabled = params.gate_enabled;
        livePreviewStemOverrides(songIndex, stem.file).gate_enabled = params.gate_enabled;
      }
      ov.gain_db ??= Number(params.gain_db || 0);
      ov.fader_db ??= Number(params.fader_db || 0);
      ov.pan ??= Number(params.pan || 0);
      ov.eq_low_cut_hz ??= Number(params.eq_low_cut_hz);
      ov.eq_mid_gain_db ??= Number(params.eq_mid_gain_db);
      ov.eq_air_gain_db ??= Number(params.eq_air_gain_db);
    }
    const gainDb = stemGainDb(songIndex, stem);
    const fader = ov.user_confirmed
      ? Number(ov.fader_db || 0)
      : Number(params?.user_fader_db ?? 0);
    const automaticFaderDb = Number(params?.automatic_fader_db ?? params?.computed_gain_db ?? 0);
    if (!Number.isFinite(Number(ov.pan))) ov.pan = defaultPan(stem);
    const pan = Number(ov.pan);
    ov.fx_enabled ??= params?.fx_enabled ?? true;
    if (!ov.effects_user_confirmed) {
      ov.space_enabled = Boolean(params?.space_enabled);
      ov.echo_enabled = Boolean(params?.echo_enabled);
    }
    const fxEnabled = ov.fx_enabled !== false;
    const eq = stemEq(songIndex, stem);
    ov.eq_low_cut_hz ??= eq.eq_low_cut_hz;
    ov.eq_mid_gain_db ??= eq.eq_mid_gain_db;
    ov.eq_air_gain_db ??= eq.eq_air_gain_db;
    Object.assign(livePreviewStemOverrides(songIndex, stem.file), JSON.parse(JSON.stringify(ov)));
    const strip = document.createElement("div");
    strip.className = `strip stem-family-${stemFamily(stem)}`;
    strip.dataset.stemFile = stem.file;
    strip.innerHTML = `
      <strong class="stem-label">${esc(linkedLabel)}</strong>
      <div class="kind">${stem.role === "vocal" ? "voice" : "instrument"}</div>
      <label class="gain-control">Gain <input data-gain type="range" min="-20" max="20" step="0.5" value="${gainDb}"><span>${signedDb(gainDb)}</span></label>
      <input data-fader type="range" min="-60" max="12" step="0.5" value="${fader}">
      <div class="amount">Trim ${amount(fader)} · auto ${signedDb(automaticFaderDb)}</div>
      <button type="button" class="fx-toggle ${fxEnabled ? "active" : ""}" data-fx-toggle>${fxEnabled ? "FX ON" : "FX OFF"}</button>
      <div class="stem-effects" aria-label="Per-stem effects">
        ${[["gate_enabled", "Gate"], ["space_enabled", "Space"], ["echo_enabled", "Echo"]].map(([key, label]) => `<button type="button" class="effect-dot ${ov[key] === true ? "active" : ""}" data-effect="${key}" title="Toggle ${label}"><span></span>${label}</button>`).join("")}
      </div>
      <div class="effect-levels" data-effect-levels></div>
      ${linked.length > 1 ? '<div class="pan-control linked-pan-note">Stereo pair · individual L/R panning preserved</div>' : `<label class="pan-control">Pan <input data-pan type="range" min="-1" max="1" step="0.05" value="${pan}"><span>${panText(pan)}</span></label>`}
      <div class="eq-controls">
        <label>Low cut <input data-eq="eq_low_cut_hz" type="range" min="20" max="220" step="5" value="${eq.eq_low_cut_hz}"><span>${Math.round(eq.eq_low_cut_hz)} Hz</span></label>
        <label>Cut/Boost <input data-eq="eq_mid_gain_db" type="range" min="-8" max="6" step="0.5" value="${eq.eq_mid_gain_db}"><span>${eq.eq_mid_gain_db.toFixed(1)} dB</span></label>
        <label>Air/Warmth <input data-eq="eq_air_gain_db" type="range" min="-6" max="6" step="0.5" value="${eq.eq_air_gain_db}"><span>${eq.eq_air_gain_db.toFixed(1)} dB</span></label>
      </div>
      <div class="mini">
        <button class="${ov.mute ? "active" : ""}" data-mute>Mute</button>
        <button class="${ov.solo ? "active" : ""}" data-solo>Solo</button>
      </div>
    `;
    const updateEffectLabels = () => {
      const sendLabel = (key, enabled) => ov.fx_enabled === false || !enabled ? "off" : `${signedDb(20 * Math.log10(Math.max(1e-12, previewSendGain(songIndex, stem, key))))}`;
      strip.querySelector("[data-effect-levels]").textContent = `Space ${sendLabel("reverb_send_db", ov.space_enabled)} · Echo ${sendLabel("delay_send_db", ov.echo_enabled)}`;
    };
    updateEffectLabels();
    const slider = strip.querySelector("[data-fader]");
    const label = strip.querySelector(".amount");
    const gainSlider = strip.querySelector("[data-gain]");
    const gainLabel = strip.querySelector(".gain-control span");
    gainSlider.addEventListener("input", () => {
      const liveGainDb = Number(gainSlider.value);
      ov.gain_db = liveGainDb;
      setLinkedOverride(songIndex, linked, "gain_db", liveGainDb);
      linked.forEach((item) => { stemOverrides(songIndex, item.file).user_confirmed = true; });
      gainLabel.textContent = signedDb(liveGainDb);
      linked.forEach((item) => applyLivePreGain(songIndex, item, liveGainDb, "gain:direct-input"));
      persistPreviewChange(songIndex, `gain:${stem.file}`);
    });
    gainSlider.addEventListener("change", () => flushOverrideSaveVisible(songIndex, `gain:${stem.file}`).catch(() => {}));
    slider.addEventListener("input", () => {
      const mix = previewMixFor(songIndex);
      const liveNode = faderGainNode(mix, stem.file);
      console.log("[mix-preview fader handler]", {
        phase: "input:start",
        song: songIndex,
        mixKey: previewMixKey(songIndex),
        stem: stem.label || stem.file,
        file: stem.file,
        rawValue: slider.value,
        hasMix: Boolean(mix),
        faderGainFound: Boolean(liveNode),
        faderGainNodeId: previewNodeId(liveNode),
        faderGainInSignalPath: Boolean(liveNode?.__inSignalPath),
        receivesFromNodeId: previewNodeId(liveNode?.__receivesFromNode),
        feedsNodeId: previewNodeId(liveNode?.__feedsNode),
        currentAudioParamValue: liveNode?.gain ? liveNode.gain.value : null,
      });
      const liveFaderDb = Number(slider.value);
      ov.fader_db = liveFaderDb;
      setLinkedOverride(songIndex, linked, "fader_db", liveFaderDb);
      linked.forEach((item) => { stemOverrides(songIndex, item.file).user_confirmed = true; });
      label.textContent = `Trim ${amount(ov.fader_db)} · auto ${signedDb(automaticFaderDb)}`;
      applyLiveFaderGain(songIndex, stem, liveFaderDb, "fader:direct-input");
      linked.forEach((item) => updatePreviewGains(songIndex, item.file, "fader", { faderDb: liveFaderDb }));
      persistPreviewChange(songIndex, `fader:${stem.file}`);
    });
    slider.addEventListener("change", () => {
      const mix = previewMixFor(songIndex);
      const liveNode = faderGainNode(mix, stem.file);
      console.log("[mix-preview fader handler]", {
        phase: "change:commit",
        song: songIndex,
        mixKey: previewMixKey(songIndex),
        stem: stem.label || stem.file,
        file: stem.file,
        rawValue: slider.value,
        faderGainFound: Boolean(liveNode),
        faderGainNodeId: previewNodeId(liveNode),
        currentAudioParamValue: liveNode?.gain ? liveNode.gain.value : null,
      });
      flushOverrideSaveVisible(songIndex, `fader:${stem.file}`).catch(() => {});
    });
    const panSlider = strip.querySelector("[data-pan]");
    const panLabel = strip.querySelector(".pan-control span");
    if (panSlider) {
      panSlider.addEventListener("input", () => {
        ov.pan = Number(panSlider.value);
        stemOverrides(songIndex, stem.file).pan = ov.pan;
        livePreviewStemOverrides(songIndex, stem.file).pan = ov.pan;
        panLabel.textContent = panText(ov.pan);
        updatePreviewPan(songIndex, stem.file);
        persistPreviewChange(songIndex, `pan:${stem.file}`);
      });
      panSlider.addEventListener("change", () => flushOverrideSaveVisible(songIndex, `pan:${stem.file}`).catch(() => {}));
    }
    strip.querySelectorAll("[data-eq]").forEach((eqSlider) => {
      const key = eqSlider.dataset.eq;
      const eqLabel = eqSlider.nextElementSibling;
      eqSlider.addEventListener("input", () => {
        ov[key] = Number(eqSlider.value);
        setLinkedOverride(songIndex, linked, key, ov[key]);
        eqLabel.textContent = key === "eq_low_cut_hz" ? `${Math.round(ov[key])} Hz` : `${Number(ov[key]).toFixed(1)} dB`;
        updatePreviewEq(songIndex, stem.file, key);
        persistPreviewChange(songIndex, `${key}:${stem.file}`);
      });
      eqSlider.addEventListener("change", () => flushOverrideSaveVisible(songIndex, `${key}:${stem.file}`).catch(() => {}));
    });
    strip.querySelector("[data-mute]").addEventListener("click", async (event) => {
      ov.mute = !ov.mute;
      setLinkedOverride(songIndex, linked, "mute", ov.mute);
      event.currentTarget.classList.toggle("active", ov.mute);
      linked.forEach((item) => updatePreviewGains(songIndex, item.file, "mute"));
      await flushOverrideSaveVisible(songIndex, `mute:${stem.file}`);
    });
    strip.querySelector("[data-solo]").addEventListener("click", async (event) => {
      ov.solo = !ov.solo;
      setLinkedOverride(songIndex, linked, "solo", ov.solo);
      event.currentTarget.classList.toggle("active", ov.solo);
      linked.forEach((item) => updatePreviewGains(songIndex, item.file, "solo"));
      await flushOverrideSaveVisible(songIndex, `solo:${stem.file}`);
    });
    strip.querySelector("[data-fx-toggle]").addEventListener("click", async (event) => {
      ov.fx_enabled = !(ov.fx_enabled === true);
      setLinkedOverride(songIndex, linked, "fx_enabled", ov.fx_enabled);
      event.currentTarget.classList.toggle("active", ov.fx_enabled);
      event.currentTarget.textContent = ov.fx_enabled ? "FX ON" : "FX OFF";
      updateEffectLabels();
      linked.forEach((item) => reconnectPreviewStemFx(previewMixFor(songIndex), songIndex, item, ov.fx_enabled));
      persistPreviewChange(songIndex, `fx:${stem.file}`);
      await flushOverrideSaveVisible(songIndex, `fx:${stem.file}`);
      logPreviewGraphIntegrity(songIndex, "fx-toggle");
    });
    strip.querySelectorAll("[data-effect]").forEach((button) => {
      button.addEventListener("click", async () => {
        const key = button.dataset.effect;
        ov[key] = !(ov[key] === true);
        if (key === "gate_enabled") { ov.gate_user_confirmed = true; setLinkedOverride(songIndex, linked, "gate_user_confirmed", true); }
        if (key === "space_enabled" || key === "echo_enabled") {
          ov.effects_user_confirmed = true;
          setLinkedOverride(songIndex, linked, "effects_user_confirmed", true);
        }
        setLinkedOverride(songIndex, linked, key, ov[key]);
        button.classList.toggle("active", ov[key]);
        updateEffectLabels();
        linked.forEach((item) => {
          const live = livePreviewStemOverrides(songIndex, item.file);
          live[key] = ov[key];
        });
        if (key === "space_enabled" || key === "echo_enabled") updatePreviewSends(songIndex, key);
        if (key === "gate_enabled") linked.forEach((item) => reconnectPreviewStemFx(previewMixFor(songIndex), songIndex, item, previewFxEnabled(songIndex, item)));
        persistPreviewChange(songIndex, `${key}:${stem.file}`);
        await flushOverrideSaveVisible(songIndex, `${key}:${stem.file}`);
        logPreviewGraphIntegrity(songIndex, `${key}:${stem.file}`);
      });
    });
    faders.appendChild(strip);
  });
}

function wireFineTuneControls(root, songIndex) {
  root.querySelectorAll("[data-simple]").forEach((input) => {
    const key = input.dataset.simple;
    const vals = Object.values(songOverrides(songIndex).stems || {}).map((x) => Number(x[key] || 0));
    input.value = vals.length ? vals[0] : 0;
    input.nextElementSibling.textContent = amount(input.value);
    input.addEventListener("input", () => {
      input.nextElementSibling.textContent = amount(input.value);
      appState.stems.forEach((stem) => {
        stemOverrides(songIndex, stem.file)[key] = Number(input.value);
        livePreviewStemOverrides(songIndex, stem.file)[key] = Number(input.value);
      });
      updatePreviewSends(songIndex, key);
      persistPreviewChange(songIndex, key);
    });
    input.addEventListener("change", () => flushOverrideSaveVisible(songIndex, key).catch(() => {}));
  });

  root.querySelectorAll("[data-song]").forEach((input) => {
    const key = input.dataset.song;
    const fallback = key === "target_lufs" ? -10 : 0;
    input.value = currentSongOverrides(songIndex)[key] ?? fallback;
    input.nextElementSibling.textContent = key === "target_lufs" ? loudnessText(input.value) : amount(input.value);
    input.addEventListener("input", () => {
      songOverrides(songIndex)[key] = Number(input.value);
      livePreviewSongOverrides(songIndex)[key] = Number(input.value);
      input.nextElementSibling.textContent = key === "target_lufs" ? loudnessText(input.value) : amount(input.value);
      updatePreviewBusGains(songIndex, key);
      persistPreviewChange(songIndex, key);
    });
    input.addEventListener("change", () => flushOverrideSaveVisible(songIndex, key).catch(() => {}));
  });

  const mastering = root.querySelector(`[data-mastering="${songIndex}"]`);
  if (mastering) {
    mastering.value = currentSongOverrides(songIndex).mastering_intensity || "natural";
    mastering.addEventListener("change", async () => {
      songOverrides(songIndex).mastering_intensity = mastering.value;
      songOverrides(songIndex).target_lufs = mastering.value === "loud" ? -9.5 : -14;
      livePreviewSongOverrides(songIndex).mastering_intensity = mastering.value;
      livePreviewSongOverrides(songIndex).target_lufs = mastering.value === "loud" ? -9.5 : -14;
      const loudness = root.querySelector('[data-song="target_lufs"]');
      if (loudness) {
        loudness.value = currentSongOverrides(songIndex).target_lufs;
        loudness.nextElementSibling.textContent = loudnessText(loudness.value);
      }
      updatePreviewBusGains(songIndex, "target_lufs");
      await flushOverrideSaveVisible(songIndex, "mastering_intensity");
    });
  }

  root.querySelector("[data-mix-settings]").addEventListener("click", () => mixSongs([songIndex], false, false).catch((error) => showToast(`Render failed: ${error.message || error}`)));
  const renderPreviewButton = root.querySelector(`[data-preview-render="${songIndex}"]`);
  const quickControls = root.querySelector(`[data-quick-controls="${songIndex}"]`);
  const realControls = root.querySelector(`[data-real-controls="${songIndex}"]`);
  const statusNode = root.querySelector(`[data-real-status="${songIndex}"]`);
  const playerNode = root.querySelector(`[data-preview-player="${songIndex}"]`);
  renderPreviewButton?.addEventListener("click", async () => {
    const mode = 30;
    const mix = previewMixFor(songIndex);
    if (mix?.playing) {
      mix.offset = currentPreviewOffset(mix);
      stopPreviewSources(mix, "switch-to-real-preview");
      updatePreviewTransport(root, songIndex);
    }
    if (quickControls) quickControls.hidden = false;
    if (realControls) realControls.hidden = false;
    if (playerNode) { playerNode.pause(); playerNode.removeAttribute("src"); }
    if (statusNode) statusNode.textContent = `Rendering a fresh exact ${mode}s excerpt...`;
    try {
      await saveOverrides({ songIndex, reason: "real-preview" });
      const res = await fetch(`/api/real-preview/${songIndex}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ duration: Number(mode) }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "real preview failed");
      if (data.cached || data.status === "done") {
        playerNode.src = data.preview_url;
        playerNode.load();
        playerNode.play().catch(() => {});
        if (statusNode) statusNode.textContent = `Exact centered ${Number(data.preview_duration || mode).toFixed(0)}s excerpt ready.`;
      } else {
        realPreviewJobs[String(songIndex)] = data.id;
      }
    } catch (error) {
      if (statusNode) statusNode.textContent = `Real preview failed: ${error.message}`;
    }
  });
  root.querySelector(`[data-preview-toggle="${songIndex}"]`).addEventListener("click", () => toggleFullStemPreview(root, songIndex));
  root.querySelector(`[data-preview-seek="${songIndex}"]`).addEventListener("input", (event) => seekFullStemPreview(root, songIndex, Number(event.target.value || 0)));
}

function loudnessText(value) {
  const n = Number(value);
  if (n >= -9) return "louder";
  if (n <= -14) return "gentler";
  return "normal";
}

function panText(value) {
  const n = Number(value || 0);
  if (Math.abs(n) < 0.025) return "C";
  return n < 0 ? `L${Math.round(Math.abs(n) * 100)}` : `R${Math.round(n * 100)}`;
}

function dbToGain(db) {
  return Math.pow(10, Number(db || 0) / 20);
}

function previewNodeId(node) {
  if (!node) return null;
  if (!previewNodeIds.has(node)) previewNodeIds.set(node, `n${previewNodeIdCounter++}`);
  return previewNodeIds.get(node);
}

function faderGainNode(mix, stemFile) {
  return mix?.faderGains?.[stemFile] || mix?.gains?.[stemFile] || null;
}

function previewMixKey(songIndex) {
  return String(songIndex);
}

function previewMixFor(songIndex) {
  return previewMixes[previewMixKey(songIndex)] || null;
}

function setPreviewMix(songIndex, mix) {
  previewMixes[previewMixKey(songIndex)] = mix;
}

function deletePreviewMix(songIndex) {
  delete previewMixes[previewMixKey(songIndex)];
}

async function closeOtherFineTunePanels(currentDetails, currentSongIndex) {
  const stops = [];
  document.querySelectorAll(".fine-tune[open]").forEach((details) => {
    if (details === currentDetails) return;
    const faders = details.querySelector("[data-faders]");
    const otherSongIndex = faders?.dataset.faders;
    details.open = false;
    if (otherSongIndex != null && String(otherSongIndex) !== String(currentSongIndex)) {
      stops.push(stopPreviewMix(otherSongIndex));
    }
  });
  await Promise.all(stops);
}

function renderedFaderFiles(songIndex) {
  const faders = document.querySelector(`[data-faders="${songIndex}"]`);
  return new Set([...faders?.querySelectorAll(".strip[data-stem-file]") || []].map((node) => node.dataset.stemFile).filter(Boolean));
}

function filesOnlyIn(left, right) {
  return [...left].filter((file) => !right.has(file)).sort();
}

function previewStemByFile(file) {
  return appState?.stems?.find((stem) => stem.file === file) || null;
}

function setPreviewGraphWarning(songIndex, message) {
  const warning = document.querySelector(`[data-preview-warning="${songIndex}"]`);
  if (!warning) return;
  warning.textContent = message || "";
  warning.hidden = !message;
}

function disconnectPreviewSource(source, reason = "disconnect") {
  if (!source) return;
  try { source.stop(); } catch (_err) {}
  try { source.disconnect(); } catch (_err) {}
  source.__disconnected = true;
  source.__disconnectReason = reason;
}

function disconnectPreviewNode(node) {
  if (!node) return;
  try { node.disconnect(); } catch (_err) {}
  node.__disconnected = true;
}

function disconnectPreviewGraph(mix, reason = "teardown") {
  if (!mix) return;
  (mix.sources || []).forEach((source) => disconnectPreviewSource(source, reason));
  Object.values(mix.preGains || {}).forEach(disconnectPreviewNode);
  Object.values(mix.faderGains || {}).forEach(disconnectPreviewNode);
  Object.values(mix.vocalExpanders || {}).forEach(disconnectPreviewNode);
  Object.values(mix.instrumentGates || {}).forEach(disconnectPreviewNode);
  Object.values(mix.gains || {}).forEach(disconnectPreviewNode);
  Object.values(mix.panners || {}).forEach(disconnectPreviewNode);
  Object.values(mix.reverbSends || {}).forEach(disconnectPreviewNode);
  Object.values(mix.delaySends || {}).forEach(disconnectPreviewNode);
  Object.values(mix.eqNodes || {}).forEach((nodes) => {
    Object.values(nodes || {}).forEach((node) => {
      if (node && typeof node.disconnect === "function") disconnectPreviewNode(node);
    });
  });
  [
    mix.instrumentGain,
    mix.vocalLeveler,
    mix.vocalGain,
    mix.masterGain,
    mix.previewLimiter,
    mix.reverbInput,
    mix.delayInput,
    mix.reverbNode,
    mix.delayNode,
    mix.delayFeedback,
    mix.delayWet,
  ].forEach(disconnectPreviewNode);
  mix.sources = [];
  mix.preGains = {};
  mix.faderGains = {};
  mix.vocalExpanders = {};
  mix.instrumentGates = {};
  mix.gains = {};
  mix.panners = {};
  mix.reverbSends = {};
  mix.delaySends = {};
  mix.eqNodes = {};
  mix.buffers = [];
}

function previewFxEnabled(songIndex, stem) {
  return currentStemOverrides(songIndex, stem.file).fx_enabled !== false;
}

function previewEffectEnabled(songIndex, stem, key) {
  return currentStemOverrides(songIndex, stem.file)[key] === true;
}

function schedulePreviewSectionGate(mix, songIndex, stem, offset, when) {
  const node = mix?.instrumentGates?.[stem.file];
  if (!node) return;
  const points = previewStemParams(songIndex, stem)?.gate_points || [];
  const param = node.gain;
  param.cancelScheduledValues(when);
  if (!previewFxEnabled(songIndex, stem) || !previewEffectEnabled(songIndex, stem, "gate_enabled") || !points.length) {
    param.setValueAtTime(1, when);
    return;
  }
  let value = Number(points[0][1]);
  for (let i = 1; i < points.length; i++) {
    const [t, gain] = points[i];
    const [before, previous] = points[i - 1];
    if (offset <= t) {
      value = previous + (gain - previous) * Math.max(0, offset - before) / Math.max(1e-9, t - before);
      break;
    }
    value = gain;
  }
  param.setValueAtTime(value, when);
  points.forEach(([t, gain]) => {
    if (t > offset) param.linearRampToValueAtTime(gain, when + t - offset);
  });
}

function reconnectPreviewStemFx(mix, songIndex, stem, enabled) {
  const preGain = mix?.preGains?.[stem.file];
  const fader = faderGainNode(mix, stem.file);
  const eqNodes = Object.values(mix?.eqNodes?.[stem.file] || {}).filter((node) => node && typeof node.connect === "function");
  const expander = mix?.vocalExpanders?.[stem.file];
  const instrumentGate = mix?.instrumentGates?.[stem.file];
  if (!preGain || !fader) return;
  try { preGain.disconnect(); } catch (_err) {}
  eqNodes.forEach((node) => { try { node.disconnect(); } catch (_err) {} });
  if (expander) { try { expander.disconnect(); } catch (_err) {} }
  if (instrumentGate) { try { instrumentGate.disconnect(); } catch (_err) {} }
  const gateEnabled = enabled && (stem.role === "vocal" || previewEffectEnabled(songIndex, stem, "gate_enabled"));
  const chain = enabled ? [...eqNodes, ...(expander && gateEnabled ? [expander] : []), ...(instrumentGate && gateEnabled ? [instrumentGate] : []), fader] : [fader];
  let from = preGain;
  chain.forEach((to) => { from.connect(to); to.__receivesFromNode = from; from = to; });
  fader.__fedByEqChain = Boolean(enabled && eqNodes.length);
  fader.__inSignalPath = true;
  if (expander) expander.__inSignalPath = gateEnabled;
  const reverb = mix.reverbSends?.[stem.file];
  const delay = mix.delaySends?.[stem.file];
  if (reverb) reverb.gain.value = enabled && previewEffectEnabled(songIndex, stem, "space_enabled") ? previewSendGain(songIndex, stem, "reverb_send_db") : 0;
  if (delay) delay.gain.value = enabled && previewEffectEnabled(songIndex, stem, "echo_enabled") ? previewSendGain(songIndex, stem, "delay_send_db") : 0;
  preGain.__fxEnabled = enabled;
  if (instrumentGate) schedulePreviewSectionGate(mix, songIndex, stem, currentPreviewOffset(mix), mix.ctx.currentTime);
}

function logPreviewGraphIntegrity(songIndex, reason) {
  const mix = previewMixFor(songIndex);
  if (!mix) {
    console.log("[mix-preview graph-integrity]", { song: songIndex, reason, hasMix: false });
    return;
  }
  if (mix.loading && reason !== "init:complete-graph-built") return;
  const song = appState.songs.find((item) => Number(item.id) === Number(songIndex));
  const activeFiles = new Set(song?.active_stems || []);
  const stems = appState.stems.filter((stem) => activeFiles.has(stem.file));
  // A single logical fader can represent a linked stereo pair.  The graph
  // must therefore be checked against all active files, not only the first
  // DOM strip in each linked group.
  const domFaderFiles = renderedFaderFiles(songIndex);
  const faderNodeFiles = new Set(Object.keys(mix.faderGains || {}));
  const renderedFiles = new Set(domFaderFiles);
  const graphFiles = new Set([
    ...Object.keys(mix.preGains || {}),
    ...Object.keys(mix.faderGains || {}),
    ...(mix.buffers || []).map(({ stem }) => stem?.file).filter(Boolean),
  ]);
  const connectedSources = (mix.sources || []).filter((source) => !source.__disconnected);
  const sourceFiles = new Set(connectedSources.map((source) => source.__stemFile).filter(Boolean));
  const graphOnly = filesOnlyIn(graphFiles, renderedFiles);
  const renderedOnly = filesOnlyIn(renderedFiles, graphFiles);
  const sourceCountMismatch = filesOnlyIn(activeFiles, faderNodeFiles).length || filesOnlyIn(faderNodeFiles, activeFiles).length;
  const domFaderCountMismatch = filesOnlyIn(activeFiles, domFaderFiles).length || filesOnlyIn(domFaderFiles, activeFiles).length;
  const missingSources = mix.playing ? filesOnlyIn(renderedFiles, sourceFiles) : [];
  const filenameWarnings = stems
    .filter((stem) => String(stem.file) !== String(stem.file).trim())
    .map((stem) => ({
      file: stem.file,
      exactFileJson: JSON.stringify(stem.file),
      trimmedFileJson: JSON.stringify(String(stem.file).trim()),
      length: String(stem.file).length,
      trimmedLength: String(stem.file).trim().length,
    }));
  const orphanSources = connectedSources
    .filter((source) => source.__stemFile && !renderedFiles.has(source.__stemFile))
    .map((source) => {
      const stem = previewStemByFile(source.__stemFile);
      return {
        song: songIndex,
        stem: stem?.label || source.__stemFile,
        file: source.__stemFile,
        nodeId: previewNodeId(source),
      };
    });
  const graphChainErrors = stems
    .filter((stem) => renderedFiles.has(stem.file))
    .map((stem) => {
      const preGain = mix.preGains?.[stem.file];
      const fader = faderGainNode(mix, stem.file);
      const panner = mix.panners?.[stem.file];
      const errors = [];
      if (!preGain) errors.push("missing preGain node");
      if (!fader) errors.push("missing fader gain node");
      if (!panner) errors.push("missing panner node");
      if (fader && fader.__receivesFromNode == null) errors.push("fader has no tracked input node");
      if (stem.role === "vocal" && !mix.vocalExpanders?.[stem.file]) errors.push("missing vocal expander node");
      if (stem.role === "vocal" && previewFxEnabled(songIndex, stem) && fader && fader.__receivesFromNode !== mix.vocalExpanders?.[stem.file]) errors.push("vocal fader is not fed by expander");
      if (fader && fader.__feedsNode !== panner) errors.push("fader does not feed this stem's panner");
      if (panner && !panner.__inSignalPath) errors.push("panner not marked in signal path");
      return errors.length ? {
        file: stem.file,
        exactFileJson: JSON.stringify(stem.file),
        label: stem.label || stem.file,
        role: stem.role,
        errors,
        preGainId: previewNodeId(preGain),
        vocalExpanderId: previewNodeId(mix.vocalExpanders?.[stem.file]),
        vocalExpanderProcessing: mix.vocalExpanders?.[stem.file]?.__processing || null,
        faderGainId: previewNodeId(fader),
        pannerId: previewNodeId(panner),
        faderReceivesFromNodeId: previewNodeId(fader?.__receivesFromNode),
        faderFeedsNodeId: previewNodeId(fader?.__feedsNode),
      } : null;
    })
    .filter(Boolean);
  const sourceConnectionErrors = mix.playing ? stems
    .filter((stem) => renderedFiles.has(stem.file))
    .map((stem) => {
      const preGain = mix.preGains?.[stem.file];
      const sources = connectedSources.filter((source) => source.__stemFile === stem.file);
      const errors = [];
      if (sources.length !== 1) errors.push(`expected exactly one live source, found ${sources.length}`);
      sources.forEach((source) => {
        if (source.__connectedPreGain !== preGain) errors.push(`source ${previewNodeId(source)} is not connected to this stem's preGain`);
      });
      return errors.length ? {
        file: stem.file,
        exactFileJson: JSON.stringify(stem.file),
        label: stem.label || stem.file,
        role: stem.role,
        errors,
        expectedPreGainId: previewNodeId(preGain),
        sourceNodeIds: sources.map(previewNodeId),
        sourceConnectedPreGainIds: sources.map((source) => previewNodeId(source.__connectedPreGain)),
      } : null;
    })
    .filter(Boolean) : [];
  const mismatched = Boolean(
    graphOnly.length
    || renderedOnly.length
    || sourceCountMismatch
    || domFaderCountMismatch
    || missingSources.length
    || orphanSources.length
    || graphChainErrors.length
    || sourceConnectionErrors.length
  );
  const payload = {
    song: songIndex,
    reason,
    loaded: Boolean(mix.loaded),
    loading: Boolean(mix.loading),
    playing: Boolean(mix.playing),
    activeStemCount: activeFiles.size,
    sourceStemCount: activeFiles.size,
    renderedFaderCount: faderNodeFiles.size,
    domFaderFileCount: domFaderFiles.size,
    sourceCountEqualsRenderedFaderCount: !sourceCountMismatch,
    sourceCountEqualsDomFaderCount: !domFaderCountMismatch,
    graphStemCount: graphFiles.size,
    connectedSourceNodeCount: connectedSources.length,
    activeStemFiles: [...activeFiles].sort(),
    renderedFaderFiles: [...domFaderFiles].sort(),
    faderNodeFiles: [...faderNodeFiles].sort(),
    graphStemFiles: [...graphFiles].sort(),
    connectedSourceFiles: [...sourceFiles].sort(),
    graphStemsWithoutRenderedFader: graphOnly,
    renderedFadersWithoutGraphStem: renderedOnly,
    renderedFadersWithoutConnectedSource: missingSources,
    orphanSourcesWithoutRenderedFader: orphanSources,
    filenameWarnings,
    graphChainErrors,
    sourceConnectionErrors,
    busNodes: {
      instrumentGain: previewNodeId(mix.instrumentGain),
      vocalLeveler: previewNodeId(mix.vocalLeveler),
      vocalGain: previewNodeId(mix.vocalGain),
      masterGain: previewNodeId(mix.masterGain),
      reverbInput: previewNodeId(mix.reverbInput),
      delayInput: previewNodeId(mix.delayInput),
    },
    stems: stems.map((stem) => {
      const fader = faderGainNode(mix, stem.file);
      const preGain = mix.preGains?.[stem.file];
      const panner = mix.panners?.[stem.file];
      const eqNodes = mix.eqNodes?.[stem.file] || {};
      return {
        file: stem.file,
        exactFileJson: JSON.stringify(stem.file),
        fileLength: String(stem.file).length,
        label: stem.label || stem.file,
        role: stem.role,
        sourceNodeIds: (mix.sources || []).filter((source) => source.__stemFile === stem.file).map(previewNodeId),
        sourceConnectedPreGainIds: (mix.sources || []).filter((source) => source.__stemFile === stem.file).map((source) => previewNodeId(source.__connectedPreGain)),
        preGainId: previewNodeId(preGain),
        vocalExpanderId: previewNodeId(mix.vocalExpanders?.[stem.file]),
        faderGainId: previewNodeId(fader),
        legacyGainAliasSameNode: Boolean(fader && mix.gains?.[stem.file] === fader),
        eqLowId: previewNodeId(eqNodes.low),
        eqMidId: previewNodeId(eqNodes.mid),
        eqAirId: previewNodeId(eqNodes.air),
        pannerId: previewNodeId(panner),
        faderReceivesFromNodeId: previewNodeId(fader?.__receivesFromNode),
        faderFeedsNodeId: previewNodeId(fader?.__feedsNode),
        reverbSendId: previewNodeId(mix.reverbSends?.[stem.file]),
        delaySendId: previewNodeId(mix.delaySends?.[stem.file]),
        preGainInSignalPath: Boolean(preGain?.__inSignalPath),
        faderGainInSignalPath: Boolean(fader?.__inSignalPath),
        eqOutputFeedsThisFaderGain: Boolean(fader?.__fedByEqChain),
        pannerInSignalPath: Boolean(panner?.__inSignalPath),
        currentFaderGainValue: fader?.gain ? fader.gain.value : null,
      };
    }),
  };
  if (mismatched) {
    console.error("[mix-preview graph-integrity]", payload);
    setPreviewGraphWarning(songIndex, "Preview graph error: a stem source is not tied to its fader. Check console.");
    connectedSources
      .filter((source) => source.__stemFile && !renderedFiles.has(source.__stemFile))
      .forEach((source) => disconnectPreviewSource(source, "orphan-source-integrity-failure"));
  } else {
    setPreviewGraphWarning(songIndex, "");
    console.log("[mix-preview graph-integrity]", payload);
  }
}

function previewTargetGain(songIndex, stem, options = {}) {
  const ov = currentStemOverrides(songIndex, stem.file);
  const liveFaderDb = Number(options.faderDb);
  const faderDb = Number.isFinite(liveFaderDb) ? liveFaderDb : Number(ov.fader_db || 0);
  const soloFiles = appState.stems
    .filter((item) => currentStemOverrides(songIndex, item.file).solo)
    .map((item) => item.file);
  if (ov.mute) return 0;
  if (faderDb <= -60) return 0;
  if (soloFiles.length && !soloFiles.includes(stem.file)) return 0;
  return dbToGain(faderDb);
}

function applyLiveFaderGain(songIndex, stem, faderDb, action) {
  const mix = previewMixFor(songIndex);
  const gain = faderGainNode(mix, stem.file);
  const liveFaderDb = Number(faderDb);
  const target = previewTargetGain(songIndex, stem, { faderDb: liveFaderDb });
  if (!mix || !gain) {
    console.log("[mix-preview gain direct]", {
      song: songIndex,
      mixKey: previewMixKey(songIndex),
      stem: stem.label || stem.file,
      file: stem.file,
      action,
      liveFaderDb: Number.isFinite(liveFaderDb) ? liveFaderDb : null,
      storedFaderDb: Number(currentStemOverrides(songIndex, stem.file).fader_db || 0),
      gain: target,
      hasMix: Boolean(mix),
      hasGainNode: Boolean(gain),
      graph: !mix ? "no preview mix loaded" : "missing gain node",
    });
    return;
  }
  const now = mix.ctx.currentTime;
  gain.gain.cancelScheduledValues(now);
  gain.gain.setValueAtTime(target, now);
  console.log("[mix-preview gain direct]", {
    song: songIndex,
    mixKey: previewMixKey(songIndex),
    stem: stem.label || stem.file,
    file: stem.file,
    action,
    liveFaderDb: Number.isFinite(liveFaderDb) ? liveFaderDb : null,
    storedFaderDb: Number(currentStemOverrides(songIndex, stem.file).fader_db || 0),
    gain: target,
    audioParamValue: gain.gain.value,
    hasMix: true,
    hasGainNode: true,
    faderGainNodeId: previewNodeId(gain),
    gainNodeInSignalPath: Boolean(gain.__inSignalPath),
    receivesFromNodeId: previewNodeId(gain.__receivesFromNode),
    feedsNodeId: previewNodeId(gain.__feedsNode),
    faderGainSource: "live",
  });
}

function updatePreviewGains(songIndex, changedFile = null, action = "gain", options = {}) {
  const mix = previewMixFor(songIndex);
  const changedStem = appState.stems.find((stem) => stem.file === changedFile);
  if (!mix) {
    if (changedStem) {
      console.log("[mix-preview gain]", {
        song: songIndex,
        mixKey: previewMixKey(songIndex),
        stem: changedStem.label || changedStem.file,
        file: changedStem.file,
        gain: previewTargetGain(songIndex, changedStem, options),
        liveFaderDb: Number.isFinite(Number(options.faderDb)) ? Number(options.faderDb) : null,
        storedFaderDb: Number(currentStemOverrides(songIndex, changedStem.file).fader_db || 0),
        faderGainSource: Number.isFinite(Number(options.faderDb)) ? "live" : "stored",
        action,
        mute: Boolean(currentStemOverrides(songIndex, changedStem.file).mute),
        solo: Boolean(currentStemOverrides(songIndex, changedStem.file).solo),
        hasMix: false,
        hasGainNode: false,
        hasSourceNode: false,
        graph: "no preview mix loaded",
      });
    }
    return;
  }
  const now = mix.ctx.currentTime;
  appState.stems.forEach((stem) => {
    const gain = faderGainNode(mix, stem.file);
    const liveOptions = changedFile === stem.file ? options : {};
    const target = previewTargetGain(songIndex, stem, liveOptions);
    const sourceExists = Boolean((mix.sources || []).some((source) => source.__stemFile === stem.file));
    const shouldLog = !changedFile || changedFile === stem.file || action === "solo";
    const liveFaderDb = Number(liveOptions.faderDb);
    const storedFaderDb = Number(currentStemOverrides(songIndex, stem.file).fader_db || 0);
    if (!gain) {
      if (shouldLog) {
        console.log("[mix-preview gain]", {
          song: songIndex,
          mixKey: previewMixKey(songIndex),
          stem: stem.label || stem.file,
          file: stem.file,
          gain: target,
          liveFaderDb: Number.isFinite(liveFaderDb) ? liveFaderDb : null,
          storedFaderDb,
          faderGainSource: Number.isFinite(liveFaderDb) ? "live" : "stored",
          action,
          mute: Boolean(currentStemOverrides(songIndex, stem.file).mute),
          solo: Boolean(currentStemOverrides(songIndex, stem.file).solo),
          hasMix: true,
          hasGainNode: false,
          hasSourceNode: sourceExists,
          graph: "missing gain node",
        });
      }
      return;
    }
    gain.gain.cancelScheduledValues(now);
    gain.gain.setValueAtTime(target, now);
    if (shouldLog) {
      console.log("[mix-preview gain]", {
        song: songIndex,
        mixKey: previewMixKey(songIndex),
        stem: stem.label || stem.file,
        file: stem.file,
        gain: target,
        audioParamValue: gain.gain.value,
        liveFaderDb: Number.isFinite(liveFaderDb) ? liveFaderDb : null,
        storedFaderDb,
        faderGainSource: Number.isFinite(liveFaderDb) ? "live" : "stored",
        action,
        mute: Boolean(currentStemOverrides(songIndex, stem.file).mute),
        solo: Boolean(currentStemOverrides(songIndex, stem.file).solo),
        hasMix: true,
        hasGainNode: true,
        hasSourceNode: sourceExists,
        faderGainNodeId: previewNodeId(gain),
        gainNodeInSignalPath: Boolean(gain.__inSignalPath),
        receivesFromNodeId: previewNodeId(gain.__receivesFromNode),
        feedsNodeId: previewNodeId(gain.__feedsNode),
        eqOutputFeedsThisGainNode: Boolean(gain.__fedByEqChain),
        graph: "source -> pre-gain -> role EQ -> fader gain -> panner -> dry submix + shared send buses -> master -> destination",
      });
    }
  });
  logPreviewGraphIntegrity(songIndex, `${action}:${changedFile || "all"}`);
}

function applyLivePreGain(songIndex, stem, gainDbValue, action) {
  const mix = previewMixFor(songIndex);
  if (!stem || !mix) return;
  const preGain = mix.preGains?.[stem.file];
  const gainDb = Number(gainDbValue);
  const makeupGainDb = stemMakeupGainDb(songIndex, stem);
  const gain = dbToGain(makeupGainDb + gainDb);
  if (preGain && Number.isFinite(gainDb)) preGain.gain.setValueAtTime(gain, mix.ctx.currentTime);
  console.log("[mix-preview pre-gain]", {
    song: songIndex,
    stem: stem.label || stem.file,
    file: stem.file,
    action,
    gainDb,
    makeupGainDb,
    totalPreEqGainDb: makeupGainDb + gainDb,
    gain,
    audioParamValue: preGain?.gain ? preGain.gain.value : null,
    hasPreGainNode: Boolean(preGain),
    preGainNodeInSignalPath: Boolean(preGain?.__inSignalPath),
    faderGainNodeId: previewNodeId(faderGainNode(mix, stem.file)),
    feedsEqChain: Boolean(faderGainNode(mix, stem.file)?.__fedByEqChain),
    graph: "source -> gain -> EQ -> fader",
  });
  logPreviewGraphIntegrity(songIndex, `pre-gain:${stem.file}`);
}

function updatePreviewStemGain(songIndex, changedFile) {
  const stem = appState.stems.find((item) => item.file === changedFile);
  console.error("[mix-preview pre-gain blocked]", {
    song: songIndex,
    stem: stem?.label || changedFile,
    file: changedFile,
    reason: "pre-gain node writes must come from direct Gain slider input",
  });
}

function updatePreviewPan(songIndex, changedFile) {
  const mix = previewMixFor(songIndex);
  const stem = appState.stems.find((item) => item.file === changedFile);
  if (!stem || !mix) return;
  const pan = stemPan(songIndex, stem);
  const panner = mix.panners?.[stem.file];
  if (panner?.pan) panner.pan.setValueAtTime(pan, mix.ctx.currentTime);
  console.log("[mix-preview pan]", {
    song: songIndex,
    stem: stem.label || stem.file,
    file: stem.file,
    pan,
    audioParamValue: panner?.pan ? panner.pan.value : null,
    hasPannerNode: Boolean(panner),
    pannerInSignalPath: Boolean(panner?.__inSignalPath),
    graph: "fader gain -> panner -> dry/send buses",
  });
  logPreviewGraphIntegrity(songIndex, `pan:${changedFile}`);
}

function updatePreviewEq(songIndex, changedFile, key) {
  const mix = previewMixFor(songIndex);
  const stem = appState.stems.find((item) => item.file === changedFile);
  if (!stem || !mix) return;
  const nodes = mix.eqNodes?.[stem.file] || {};
  const eq = stemEq(songIndex, stem);
  const now = mix.ctx.currentTime;
  if (key === "eq_low_cut_hz" && nodes.low) nodes.low.frequency.setValueAtTime(eq.eq_low_cut_hz, now);
  if (key === "eq_mid_gain_db" && nodes.mid) nodes.mid.gain.setValueAtTime(eq.eq_mid_gain_db, now);
  if (key === "eq_air_gain_db" && nodes.air) nodes.air.gain.setValueAtTime(eq.eq_air_gain_db, now);
  console.log("[mix-preview eq]", {
    song: songIndex,
    stem: stem.label || stem.file,
    file: stem.file,
    control: key,
    lowCutHz: eq.eq_low_cut_hz,
    midGainDb: eq.eq_mid_gain_db,
    airGainDb: eq.eq_air_gain_db,
    hasLowCutNode: Boolean(nodes.low),
    hasMidNode: Boolean(nodes.mid),
    hasAirNode: Boolean(nodes.air),
    eqInSignalPath: Boolean(nodes.__inSignalPath),
    faderGainNodeId: previewNodeId(faderGainNode(mix, stem.file)),
    outputFeedsFaderGain: Boolean(faderGainNode(mix, stem.file)?.__fedByEqChain),
    graph: "source -> gain -> EQ nodes -> fader gain",
  });
  logPreviewGraphIntegrity(songIndex, `${key}:${changedFile}`);
}

function previewSendGain(songIndex, stem, key) {
  const ov = currentStemOverrides(songIndex, stem.file);
  const params = previewStemParams(songIndex, stem);
  const leadBonusDb = stemLeadBonusDb(songIndex, stem);
  const base = params
    ? (key === "reverb_send_db" ? params.reverb_base_db : params.delay_base_db)
    : (key === "reverb_send_db" ? baseReverbSendDb(stem.role) : baseDelaySendDb(stem.role, leadBonusDb));
  if (base == null) return 0;
  const sceneOffset = Number(params?.[key === "reverb_send_db" ? "reverb_scene_offset_db" : "delay_scene_offset_db"] || 0);
  return dbToGain(base + sceneOffset + Number(ov[key] || 0));
}

function updatePreviewSends(songIndex, key) {
  if (key === "space_enabled") key = "reverb_send_db";
  if (key === "echo_enabled") key = "delay_send_db";
  const mix = previewMixFor(songIndex);
  if (!mix) return;
  const now = mix.ctx.currentTime;
  appState.stems.forEach((stem) => {
    const gainNode = key === "reverb_send_db" ? mix.reverbSends?.[stem.file] : mix.delaySends?.[stem.file];
    if (!gainNode) return;
    const effectKey = key === "reverb_send_db" ? "space_enabled" : "echo_enabled";
    const gain = previewFxEnabled(songIndex, stem) && previewEffectEnabled(songIndex, stem, effectKey)
      ? previewSendGain(songIndex, stem, key) : 0;
    gainNode.gain.cancelScheduledValues(now);
    gainNode.gain.setTargetAtTime(gain, now, 0.02);
    console.log("[mix-preview send]", {
      song: songIndex,
      stem: stem.label || stem.file,
      file: stem.file,
      control: key,
      gain,
      hasSendGainNode: true,
      bus: key === "reverb_send_db" ? "shared reverb" : "shared delay",
      graph: "post-fader panner -> send gain -> shared send bus -> master",
    });
  });
  logPreviewGraphIntegrity(songIndex, `send:${key}`);
}

function updatePreviewBusGains(songIndex, key = "all") {
  const mix = previewMixFor(songIndex);
  if (!mix) return;
  const now = mix.ctx.currentTime;
  const song = currentSongOverrides(songIndex);
  const canonical = appState.songs.find((item) => Number(item.id) === Number(songIndex))?.mix_params;
  const vocalGain = dbToGain(Number(song.vocal_bus_db || 0));
  const targetLufs = Number(song.target_lufs ?? canonical?.target_lufs ?? -14);
  const estimatedRmsDb = estimatePreviewRmsDb(mix);
  const normalizationDb = Number.isFinite(estimatedRmsDb) ? targetLufs - estimatedRmsDb : 0;
  mix.previewNormalizationGainDb = normalizationDb;
  const masterGain = dbToGain(normalizationDb);
  if (mix.vocalGain && (key === "all" || key === "vocal_bus_db")) {
    mix.vocalGain.gain.cancelScheduledValues(now);
    mix.vocalGain.gain.setTargetAtTime(vocalGain, now, 0.02);
  }
  if (mix.masterGain && (key === "all" || key === "target_lufs")) {
    mix.masterGain.gain.cancelScheduledValues(now);
    mix.masterGain.gain.setTargetAtTime(masterGain, now, 0.02);
  }
  console.log("[mix-preview bus]", {
    song: songIndex,
    control: key,
    vocalGain,
    masterGain,
    estimatedRmsDb,
    normalizationDb,
    hasVocalGainNode: Boolean(mix.vocalGain),
    hasVocalLevelerNode: Boolean(mix.vocalLeveler),
    hasMasterGainNode: Boolean(mix.masterGain),
    graph: "instrument/vocal/send buses -> normalization gain -> soft output limiter -> destination",
  });
  logPreviewGraphIntegrity(songIndex, `bus:${key}`);
}

// Estimate the summed stereo signal. Averaging powers across stems underestimates
// the mix by 10*log10(track count), so simply loading more tracks boosts hiss.
// EQ, dynamics and effects still make this an RMS estimate rather than LUFS.
function estimatePreviewRmsDb(mix) {
  if (!mix?.loaded || !mix.buffers?.length) return NaN;
  const length = Math.max(...mix.buffers.map(({buffer}) => buffer.length));
  const stride = Math.max(1, Math.ceil(length / 120000));
  const count = Math.ceil(length / stride);
  const left = new Float64Array(count);
  const right = new Float64Array(count);
  mix.buffers.forEach(({ stem, buffer }) => {
    const pre = Number(mix.preGains?.[stem.file]?.gain?.value ?? 1);
    const fader = Number(mix.faderGains?.[stem.file]?.gain?.value ?? 1);
    const vocal = stem.role === "vocal" ? Number(mix.vocalGain?.gain?.value ?? 1) : 1;
    const gain = pre * fader * vocal;
    const pan = Math.max(-1, Math.min(1, Number(mix.panners?.[stem.file]?.pan?.value ?? 0)));
    const a = buffer.getChannelData(0);
    const b = buffer.numberOfChannels > 1 ? buffer.getChannelData(1) : null;
    const angle = b ? (pan <= 0 ? pan + 1 : pan) * Math.PI / 2 : (pan + 1) * Math.PI / 4;
    const c = Math.cos(angle), s = Math.sin(angle);
    for (let i = 0, n = 0; i < a.length; i += stride, n++) {
      const x = a[i] * gain;
      if (!b) {
        left[n] += x * c; right[n] += x * s;
      } else {
        const y = b[i] * gain;
        left[n] += pan <= 0 ? x + y * c : x * c;
        right[n] += pan <= 0 ? y * s : y + x * s;
      }
    }
  });
  let sumSquares = 0;
  for (let i = 0; i < count; i++) sumSquares += left[i] ** 2 + right[i] ** 2;
  return sumSquares > 0 ? 10 * Math.log10(sumSquares / (count * 2)) : NaN;
}

function previewTimeText(seconds) {
  return formatTime(seconds).replace(/\.\d+$/, "");
}

function previewCacheText(bytes) {
  const mb = bytes / (1024 * 1024);
  return `${mb.toFixed(mb >= 10 ? 0 : 1)} MB cached`;
}

function makeImpulse(ctx, seconds = 1.4) {
  const length = Math.max(1, Math.floor(ctx.sampleRate * seconds));
  const impulse = ctx.createBuffer(2, length, ctx.sampleRate);
  for (let ch = 0; ch < 2; ch += 1) {
    const data = impulse.getChannelData(ch);
    for (let i = 0; i < length; i += 1) {
      const decay = Math.pow(1 - i / length, 2.2);
      data[i] = (Math.random() * 2 - 1) * decay * 0.45;
    }
  }
  return impulse;
}

function setupPreviewBuses(mix, songIndex) {
  const ctx = mix.ctx;
  mix.masterGain = ctx.createGain();
  mix.previewLimiter = ctx.createDynamicsCompressor();
  mix.instrumentGain = ctx.createGain();
  mix.vocalLeveler = ctx.createDynamicsCompressor();
  mix.vocalGain = ctx.createGain();
  mix.reverbInput = ctx.createGain();
  mix.delayInput = ctx.createGain();
  mix.reverbNode = ctx.createConvolver();
  mix.delayNode = ctx.createDelay(1.2);
  mix.delayFeedback = ctx.createGain();
  mix.delayWet = ctx.createGain();
  mix.reverbNode.buffer = makeImpulse(ctx);
  mix.reverbInput.gain.value = 1;
  mix.delayInput.gain.value = 1;
  mix.delayNode.delayTime.value = 0.22;
  mix.delayFeedback.gain.value = 0.28;
  mix.delayWet.gain.value = 0.65;
  mix.vocalLeveler.threshold.value = -12;
  mix.vocalLeveler.knee.value = 6;
  mix.vocalLeveler.ratio.value = 2;
  mix.vocalLeveler.attack.value = 0.15;
  mix.vocalLeveler.release.value = 0.6;
  mix.instrumentGain.connect(mix.masterGain);
  mix.vocalLeveler.connect(mix.vocalGain);
  mix.vocalGain.connect(mix.masterGain);
  mix.reverbInput.connect(mix.reverbNode).connect(mix.masterGain);
  mix.delayInput.connect(mix.delayNode);
  mix.delayNode.connect(mix.delayWet).connect(mix.masterGain);
  mix.delayNode.connect(mix.delayFeedback).connect(mix.delayNode);
  mix.masterGain.connect(ctx.destination);
  mix.previewLimiter.threshold.value = -1;
  mix.previewLimiter.knee.value = 0;
  mix.previewLimiter.ratio.value = 20;
  mix.previewLimiter.attack.value = 0.003;
  mix.previewLimiter.release.value = 0.1;
  // Replace the direct destination connection with the parity limiter.
  mix.masterGain.disconnect(ctx.destination);
  mix.masterGain.connect(mix.previewLimiter).connect(ctx.destination);
  updatePreviewBusGains(songIndex, "all");
}

function createPreviewEqChain(ctx, songIndex, stem) {
  const bands = resolvedEqBands(songIndex, stem);
  return bands.map(([slot, type, freq, q, gain]) => {
    const node = ctx.createBiquadFilter();
    node.type = type;
    node.frequency.value = freq;
    node.Q.value = q;
    node.gain.value = gain;
    node.__eqSlot = slot;
    return node;
  });
}

function createPreviewVocalExpander(ctx, stem) {
  if (stem.role !== "vocal") return null;
  const node = new AudioWorkletNode(ctx, "zucker-vocal-expander", {
    processorOptions: previewVocalExpander,
  });
  node.__stemFile = stem.file;
  node.__inSignalPath = true;
  node.__processing = {
    type: "per-stem downward expander: AudioWorkletNode",
    threshold_db: previewVocalExpander.thresholdDb,
    ratio: previewVocalExpander.ratio,
    attack_seconds: previewVocalExpander.attackSeconds,
    release_seconds: previewVocalExpander.releaseSeconds,
    knee_db: previewVocalExpander.kneeDb,
    python_max_attenuation_db: previewVocalExpander.maxAttenuationDb,
  };
  return node;
}

function restorePreviewSourceLevel(buffer, encodingGainDb) {
  const gainDb = Number(encodingGainDb || 0);
  if (!Number.isFinite(gainDb) || gainDb === 0) return buffer;
  const gain = dbToGain(-gainDb);
  for (let channel = 0; channel < buffer.numberOfChannels; channel += 1) {
    const samples = buffer.getChannelData(channel);
    for (let i = 0; i < samples.length; i += 1) samples[i] *= gain;
  }
  return buffer;
}

async function loadFullStemPreview(root, songIndex) {
  const card = document.querySelector(`[data-faders="${songIndex}"]`)?.closest(".song-card");
  root = root || card || document;
  // The preview request must not race the active-stem request.  On a first
  // open, waiting here prevents an empty appState snapshot from being treated
  // as a real "no premix" result.
  await loadActiveStemFaders(card || root, songIndex);
  const status = root.querySelector(`[data-preview-cache="${songIndex}"]`);
  const playButton = root.querySelector(`[data-preview-toggle="${songIndex}"]`);
  if (playButton) {
    playButton.disabled = true;
    playButton.textContent = "Loading preview...";
  }
  const existingMix = previewMixFor(songIndex);
  if (existingMix?.loaded || existingMix?.loading) {
    if (existingMix.loaded) updatePreviewTransport(root, songIndex);
    else if (status) status.textContent = "Loading tracks...";
    return existingMix;
  }
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) {
    if (status) status.textContent = "Live preview is not available in this system web view.";
    return null;
  }
  if (status) status.textContent = "Loading tracks...";
  const ctx = new AudioCtx();
  const mix = {
    ctx,
    songIndex,
    mixKey: previewMixKey(songIndex),
    sources: [],
    preGains: {},
    faderGains: {},
    vocalExpanders: {},
    instrumentGates: {},
    gains: {},
    panners: {},
    reverbSends: {},
    delaySends: {},
    eqNodes: {},
    buffers: [],
    loaded: false,
    loading: true,
    playing: false,
    offset: 0,
    startedAt: 0,
    duration: 0,
    cacheBytes: 0,
    timer: null,
    closed: false,
    closing: false,
    closePromise: null,
  };
  setPreviewMix(songIndex, mix);
  setupPreviewBuses(mix, songIndex);
  const song = appState.songs.find((item) => Number(item.id) === Number(songIndex));
  const activeFiles = new Set(song?.active_stems || []);
  const previewStems = appState.stems.filter((stem) => activeFiles.has(stem.file));
  let decoded = [];
  try {
    if (previewStems.some((stem) => stem.role === "vocal")) {
      await ctx.audioWorklet.addModule("/static/vocal-expander.js");
    }
    decoded = await Promise.all(previewStems.map(async (stem) => {
      const res = await fetch(`/stem-full/${songIndex}/${stem.index}`);
      if (!res.ok || res.status === 204) return null;
      const bytes = Number(res.headers.get("X-Preview-Cache-Bytes") || 0);
      const arrayBuffer = await res.arrayBuffer();
      const buffer = await ctx.decodeAudioData(arrayBuffer);
      restorePreviewSourceLevel(buffer, res.headers.get("X-Preview-Source-Gain-Db"));
      return { stem, buffer, bytes: bytes || arrayBuffer.byteLength };
    }));
  } catch (_err) {
    mix.loading = false;
    if (status) status.textContent = "Could not load full-song preview stems.";
    await stopPreviewMix(songIndex);
    return null;
  }
  if (mix.closed || previewMixFor(songIndex) !== mix) {
    disconnectPreviewGraph(mix, "superseded-preview-load");
    await ctx.close().catch(() => {});
    return null;
  }
  decoded.filter(Boolean).forEach(({ stem, buffer, bytes }) => {
    if (mix.faderGains[stem.file]) {
      console.error("[mix-preview graph-integrity]", {
        song: songIndex,
        stem: stem.label || stem.file,
        file: stem.file,
        error: "attempted to replace existing per-stem fader gain node",
        existingFaderGainId: previewNodeId(mix.faderGains[stem.file]),
      });
      return;
    }
    const preGain = ctx.createGain();
    const eqNodes = createPreviewEqChain(ctx, songIndex, stem);
    const vocalExpander = createPreviewVocalExpander(ctx, stem);
    const gain = ctx.createGain();
    const panner = ctx.createStereoPanner ? ctx.createStereoPanner() : ctx.createGain();
    const reverbSend = ctx.createGain();
    const delaySend = ctx.createGain();
    preGain.__stemFile = stem.file;
    if (vocalExpander) vocalExpander.__stemFile = stem.file;
    gain.__stemFile = stem.file;
    panner.__stemFile = stem.file;
    reverbSend.__stemFile = stem.file;
    delaySend.__stemFile = stem.file;
    preGain.gain.value = dbToGain(stemMakeupGainDb(songIndex, stem) + stemGainDb(songIndex, stem));
    gain.gain.value = previewTargetGain(songIndex, stem);
    if (panner.pan) panner.pan.value = stemPan(songIndex, stem);
    reverbSend.gain.value = previewSendGain(songIndex, stem, "reverb_send_db");
    delaySend.gain.value = previewSendGain(songIndex, stem, "delay_send_db");
    const firstEq = eqNodes[0];
    const beforeFader = vocalExpander || gain;
    if (firstEq) {
      preGain.connect(firstEq);
      eqNodes.forEach((node, index) => {
        node.connect(eqNodes[index + 1] || beforeFader);
      });
      gain.__fedByEqChain = true;
      if (vocalExpander) vocalExpander.__receivesFromNode = eqNodes[eqNodes.length - 1];
    } else {
      preGain.connect(beforeFader);
      gain.__fedByEqChain = false;
      if (vocalExpander) vocalExpander.__receivesFromNode = preGain;
    }
    if (vocalExpander) {
      vocalExpander.connect(gain);
      vocalExpander.__feedsNode = gain;
      gain.__receivesFromNode = vocalExpander;
      gain.__fedByEqChain = Boolean(firstEq);
    } else {
      gain.__receivesFromNode = firstEq ? eqNodes[eqNodes.length - 1] : preGain;
    }
    gain.connect(panner);
    gain.__feedsNode = panner;
    panner.connect(stem.role === "vocal" ? mix.vocalLeveler : mix.instrumentGain);
    panner.connect(reverbSend).connect(mix.reverbInput);
    panner.connect(delaySend).connect(mix.delayInput);
    preGain.__inSignalPath = true;
    if (vocalExpander) vocalExpander.__inSignalPath = true;
    gain.__inSignalPath = true;
    panner.__inSignalPath = true;
    mix.eqNodes[stem.file] = eqNodes.reduce((acc, node) => {
      if (node.__eqSlot && node.__eqSlot !== "fixed") acc[node.__eqSlot] = node;
      return acc;
    }, { __inSignalPath: true });
    mix.preGains[stem.file] = preGain;
    if (vocalExpander) mix.vocalExpanders[stem.file] = vocalExpander;
    if (stem.role !== "vocal") mix.instrumentGates[stem.file] = ctx.createGain();
    mix.faderGains[stem.file] = gain;
    mix.gains[stem.file] = gain;
    mix.panners[stem.file] = panner;
    mix.reverbSends[stem.file] = reverbSend;
    mix.delaySends[stem.file] = delaySend;
    mix.buffers.push({ stem, buffer });
    reconnectPreviewStemFx(mix, songIndex, stem, previewFxEnabled(songIndex, stem));
    mix.duration = Math.max(mix.duration, buffer.duration);
    mix.cacheBytes += bytes;
  });
  mix.loaded = true;
  mix.loading = false;
  updatePreviewBusGains(songIndex, "all");
  logPreviewGraphIntegrity(songIndex, "init:complete-graph-built");
  const seek = root.querySelector(`[data-preview-seek="${songIndex}"]`);
  if (seek) {
    seek.max = String(mix.duration || 0);
    seek.disabled = !mix.buffers.length;
  }
  updatePreviewTransport(root, songIndex);
  if (status) status.textContent = mix.buffers.length ? previewCacheText(mix.cacheBytes) : "No active preview audio found for this song.";
  if (playButton) {
    playButton.disabled = !mix.buffers.length;
    playButton.textContent = mix.buffers.length ? "Play Preview" : "Preview unavailable";
  }
  return mix;
}

function startPreviewSources(mix, songIndex, offset) {
  stopPreviewSources(mix);
  const when = mix.ctx.currentTime + 0.03;
  mix.buffers.forEach(({ stem }) => schedulePreviewSectionGate(mix, songIndex, stem, offset, when));
  mix.sources = mix.buffers.map(({ stem, buffer }) => {
    const source = mix.ctx.createBufferSource();
    source.buffer = buffer;
    const preGain = mix.preGains[stem.file];
    if (!preGain) {
      console.error("[mix-preview graph-integrity]", {
        song: songIndex,
        stem: stem.label || stem.file,
        file: stem.file,
        exactFileJson: JSON.stringify(stem.file),
        error: "cannot start source: missing preGain for stem file",
      });
      return null;
    }
    source.connect(preGain);
    source.__songIndex = songIndex;
    source.__stemFile = stem.file;
    source.__connectedPreGain = preGain;
    source.__disconnected = false;
    console.log("[mix-preview graph]", {
      song: songIndex,
      stem: stem.label || stem.file,
      file: stem.file,
      sourceNodeId: previewNodeId(source),
      hasGainNode: Boolean(faderGainNode(mix, stem.file)),
      hasPreGainNode: Boolean(mix.preGains[stem.file]),
      hasVocalExpanderNode: Boolean(mix.vocalExpanders?.[stem.file]),
      hasPannerNode: Boolean(mix.panners?.[stem.file]),
      hasReverbSend: Boolean(mix.reverbSends?.[stem.file]),
      hasDelaySend: Boolean(mix.delaySends?.[stem.file]),
      eqBands: resolvedEqBands(songIndex, stem),
      pan: stemPan(songIndex, stem),
      faderGainNodeId: previewNodeId(faderGainNode(mix, stem.file)),
      vocalExpanderNodeId: previewNodeId(mix.vocalExpanders?.[stem.file]),
      vocalExpanderProcessing: mix.vocalExpanders?.[stem.file]?.__processing || null,
      legacyGainAliasSameNode: Boolean(mix.gains?.[stem.file] === faderGainNode(mix, stem.file)),
      gainNodeInSignalPath: Boolean(faderGainNode(mix, stem.file)?.__inSignalPath),
      preGainNodeInSignalPath: Boolean(mix.preGains[stem.file]?.__inSignalPath),
      eqOutputFeedsThisGainNode: Boolean(faderGainNode(mix, stem.file)?.__fedByEqChain),
      pannerInSignalPath: Boolean(mix.panners?.[stem.file]?.__inSignalPath),
      graph: stem.role === "vocal"
        ? "source -> pre-gain -> role EQ -> vocal expander -> fader gain -> panner -> vocal bus -> master -> destination"
        : "source -> pre-gain -> role EQ -> fader gain -> panner -> dry submix + shared reverb/delay sends -> master -> destination",
      offset,
    });
    source.start(when, Math.min(offset, Math.max(0, buffer.duration - 0.01)));
    return source;
  }).filter(Boolean);
  mix.startedAt = when;
  mix.offset = offset;
  mix.playing = true;
  logPreviewGraphIntegrity(songIndex, "playback:start");
}

function stopPreviewSources(mix, reason = "source-stop") {
  mix.sources.forEach((source) => {
    disconnectPreviewSource(source, reason);
  });
  mix.sources = [];
  mix.playing = false;
}

function currentPreviewOffset(mix) {
  if (!mix?.playing) return mix?.offset || 0;
  return Math.min(mix.duration || 0, Math.max(0, mix.offset + (mix.ctx.currentTime - mix.startedAt)));
}

async function toggleFullStemPreview(root, songIndex) {
  window.renderListening?.pause();
  const mix = await loadFullStemPreview(root, songIndex);
  if (!mix || !mix.buffers.length) return;
  if (mix.ctx.state === "suspended") await mix.ctx.resume();
  if (mix.playing) {
    mix.offset = currentPreviewOffset(mix);
    stopPreviewSources(mix);
  } else {
    if (mix.offset >= mix.duration - 0.05) mix.offset = 0;
    startPreviewSources(mix, songIndex, mix.offset);
  }
  updatePreviewTransport(root, songIndex);
}

function seekFullStemPreview(root, songIndex, offset) {
  const mix = previewMixFor(songIndex);
  if (!mix || !mix.loaded) return;
  mix.offset = Math.min(Math.max(0, offset), mix.duration || 0);
  if (mix.playing) startPreviewSources(mix, songIndex, mix.offset);
  updatePreviewTransport(root, songIndex);
}

function updatePreviewTransport(root, songIndex) {
  const mix = previewMixFor(songIndex);
  const play = root.querySelector(`[data-preview-toggle="${songIndex}"]`);
  const seek = root.querySelector(`[data-preview-seek="${songIndex}"]`);
  const time = root.querySelector(`[data-preview-time="${songIndex}"]`);
  if (!mix) return;
  if (play) play.disabled = !mix.loaded || !mix.buffers.length;
  const offset = currentPreviewOffset(mix);
  if (play) play.textContent = mix.playing ? "Pause Preview" : "Play Preview";
  if (seek && document.activeElement !== seek) seek.value = String(offset);
  if (time) time.textContent = `${previewTimeText(offset)} / ${previewTimeText(mix.duration || 0)}`;
  if (mix.timer) clearTimeout(mix.timer);
  if (mix.playing) {
    if (offset >= (mix.duration || 0) - 0.05) {
      stopPreviewSources(mix);
      mix.offset = 0;
      updatePreviewTransport(root, songIndex);
    } else {
      mix.timer = setTimeout(() => updatePreviewTransport(root, songIndex), 250);
    }
  }
}

async function stopPreviewMix(songIndex) {
  const mix = previewMixFor(songIndex);
  if (!mix) return;
  if (mix.closing) {
    await mix.closePromise?.catch(() => {});
    return;
  }
  mix.closing = true;
  mix.closed = true;
  mix.closePromise = (async () => {
    if (mix.timer) clearTimeout(mix.timer);
    console.log("[mix-preview teardown]", {
      song: songIndex,
      mixKey: previewMixKey(songIndex),
      sourceNodeCount: (mix.sources || []).filter((source) => !source.__disconnected).length,
      graphStemCount: new Set([...Object.keys(mix.preGains || {}), ...Object.keys(mix.faderGains || {})]).size,
      renderedFaderCount: renderedFaderFiles(songIndex).size,
      reason: "stopPreviewMix:start",
    });
    stopPreviewSources(mix, "stop-preview-mix");
    disconnectPreviewGraph(mix, "stop-preview-mix");
    await mix.ctx.close().catch(() => {});
    deletePreviewMix(songIndex);
    if (!pendingOverrideSongs.has(String(songIndex))) clearLivePreviewSongOverride(songIndex);
    console.log("[mix-preview teardown]", {
      song: songIndex,
      mixKey: previewMixKey(songIndex),
      sourceNodeCount: 0,
      graphStemCount: 0,
      renderedFaderCount: renderedFaderFiles(songIndex).size,
      reason: "stopPreviewMix:complete",
    });
  })();
  await mix.closePromise;
}

function renderResults() {
  const signature = finishedSignature();
  if (signature === resultsSignature) return;
  resultsSignature = signature;
  const box = $("#resultsList");
  const done = visibleSongs()
    .filter((song) => song.latest_render)
    .sort((a, b) => String(b.latest_render.created).localeCompare(String(a.latest_render.created)));
  if (!done.length) {
    box.innerHTML = '<div class="result-card"><div class="result-main"><p>No finished mixes yet.</p></div></div>';
    return;
  }
  box.innerHTML = "";
  done.forEach((song) => {
    const latest = song.latest_render;
    const previous = song.previous_render;
    const card = document.createElement("article");
    card.className = "result-card";
    card.innerHTML = `
      <div class="result-main">
        <div>
          <h3>Song ${String(song.index).padStart(2, "0")} — ${esc(latest.title || "Finished mix")}</h3>
          <p>${song.duration_text}${latest.lufs ? ` · ${Number(latest.lufs).toFixed(1)} loudness` : ""} · v${latest.version}</p>
          <div class="version-links">
            <a href="/download/${song.index}/0">Download</a>
            ${previous ? `<a href="/download/${song.index}/1">Previous version</a>` : ""}
          </div>
        </div>
        <audio controls src="/audio/${song.index}/0"></audio>
        <button data-open="${song.index}">Show in Finder</button>
      </div>
    `;
    card.querySelector("[data-open]").addEventListener("click", () => fetch(`/api/open/${song.index}`, { method: "POST" }));
    card.querySelector("audio").addEventListener("error", () => showToast(`Song ${String(song.index).padStart(2, "0")} file not found.`));
    box.appendChild(card);
  });
}

async function saveSongName(songId, name) {
  const res = await fetch(`/api/song-name/${songId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) {
    showToast("Could not save that name.");
    return;
  }
  await refreshState();
}

async function setSkipped(songId, skipped) {
  const current = new Set(appState.settings.skipped_segments || []);
  if (skipped) current.add(songId);
  else current.delete(songId);
  await saveSettings({ skipped_segments: [...current] });
}

async function saveSettings(partial) {
  const payload = { ...partial };
  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    showToast("Could not save that change.");
    return;
  }
  await refreshState();
}

function preservePreviewMixParams(next, previous) {
  if (!previous || next.source_folder !== previous.source_folder) return;
  const oldSongs = new Map((previous.songs || []).map(song => [String(song.id), song]));
  (next.songs || []).forEach(song => {
    const old = oldSongs.get(String(song.id));
    if (!old?.mix_params || song.mix_params) return;
    if (["start", "end", "render_end", "revision"].every(key => song[key] === old[key])) {
      song.mix_params = old.mix_params;
    }
  });
}

async function refreshState(options = {}) {
  const renderLarge = options.renderLarge !== false;
  const rawNext = await fetchStateSnapshot();
  const next = { songs: [], transitions: [], stems: [], settings: {}, audio_scan: {}, jobs: [], ...rawNext };
  if (!Array.isArray(next.songs)) next.songs = [];
  if (next.detection_job && ["queued", "running", "stopping"].includes(next.detection_job.status)) setLoadingOverlayJob(next.detection_job);
  const protectedOverrideSongs = new Set([...pendingOverrideSongs, ...openPreviewSongIds(), ...Object.keys(livePreviewOverrides)]);
  if (next.source_folder === appState?.source_folder && appState?.overrides && (protectedOverrideSongs.size || overrideSaveTimer || overrideWritesInFlight)) {
    next.overrides ||= {};
    next.overrides.songs ||= {};
    protectedOverrideSongs.forEach((songId) => {
      if (livePreviewOverrides?.[songId]) {
        next.overrides.songs[songId] = JSON.parse(JSON.stringify(livePreviewOverrides[songId]));
      } else if (appState.overrides?.songs?.[songId]) {
        next.overrides.songs[songId] = appState.overrides.songs[songId];
      }
    });
  }
  preservePreviewMixParams(next, appState);
  if (appState?.source_folder && appState.source_folder !== next.source_folder) {
    if (overrideSaveTimer) { clearTimeout(overrideSaveTimer); overrideSaveTimer = null; }
    pendingOverrideReasons.clear();
    await Promise.all(Object.keys(previewMixes).map(id => stopPreviewMix(id)));
    document.querySelectorAll(".fine-tune[open]").forEach(panel => panel.open = false);
    document.querySelectorAll("audio").forEach(audio => audio.pause());
    window.renderListening?.reset();
    checkedSongs.clear(); Object.keys(livePreviewOverrides).forEach(key => delete livePreviewOverrides[key]); pendingOverrideSongs.clear(); songListSignature = ""; transitionSignature = "";
    if (cutSelector?.dialog.open) { cutSelector.audio?.pause(); cutSelector.dialog.close(); }
    if (cutSelector) { cutSelector.clipboard = null; cutSelector.data = null; }
  }
  appState = next;
  window.renderListening?.sync();
  refreshSlotSummary();
    renderBuildInfo();
    renderSlotAudit();
  syncOverrideSequenceFromState();
  checkedSongs = new Set([...checkedSongs].filter((id) => appState.songs.some((song) => song.id === id && !song.skipped)));
  const detected = visibleSongs().length;
  const review = visibleSongs().filter((song) => song.needs_review).length;
  $("#songCount").textContent = `${detected} songs${appState.source_integrity?.expected_slot_count === (appState.songs || []).length ? " in saved cuts" : " detected"} / ${renderableSongs().length} ready to render${review ? ` · ${review} suggested boundaries to review` : ""}`;
  if (renderLarge) {
    renderCutTools();
    renderSongs();
  }
  renderResults();
  // The state payload is intentionally large and can lag the worker. Always
  // finish with the authoritative, lightweight job snapshot.

}

function updateSelectedButton() {
  $("#mixSelected").textContent = "Mix selected";
  $("#mixSelected").disabled = checkedSongs.size === 0;
}

function sanitizeFilename(text) {
  const cleaned = String(text || "")
    .replace(/[\\/:*?"<>|]/g, "_")
    .replace(/\s+/g, " ")
    .trim();
  return cleaned || "Mix";
}

function defaultRenderFilename(songId) {
  const song = appState.songs.find((item) => Number(item.id) === Number(songId));
  const index = String(song?.index ?? songId).padStart(2, "0");
  const title = song?.custom_name ? `${index} - ${song.custom_name}` : `${index} - Mix`;
  return `${sanitizeFilename(title)}.mp3`;
}

async function chooseRenderFolder() {
  const defaultDir = appState?.settings?.last_render_dir || appState?.output_dir || "";
  const api = window.pywebview?.api;
  if (!api?.choose_save_folder) return null;
  const result = await api.choose_save_folder(defaultDir);
  if (!result || result.cancelled || !result.folder) return false;
  console.info("RENDER DESTINATION save_dialog_return", result.folder);
  appState.settings ||= {};
  appState.settings.last_render_dir = result.folder;
  fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ last_render_dir: appState.settings.last_render_dir }),
  }).catch((err) => console.error("[render save-dialog settings failed]", String(err)));
  return result.folder;
}

function setRenderControlsBusy(busy, detail = "") {
  renderInProgress = Boolean(busy);
  const selectors = [
    "#mixSelected",
    "#mixAll",
    "[data-mix-one]",
    "[data-mix-settings]",
  ];
  document.querySelectorAll(selectors.join(",")).forEach((button) => {
    if (busy) {
      if (!button.dataset.renderWasDisabled) button.dataset.renderWasDisabled = button.disabled ? "1" : "0";
      button.disabled = true;
      if (button.matches("#mixSelected, #mixAll, [data-mix-settings]")) button.dataset.renderOriginalText ||= button.textContent;
    } else {
      const wasDisabled = button.dataset.renderWasDisabled === "1";
      button.disabled = wasDisabled || (button.id === "mixSelected" && checkedSongs.size === 0);
      if (button.dataset.renderOriginalText) button.textContent = button.dataset.renderOriginalText;
      delete button.dataset.renderWasDisabled;
      delete button.dataset.renderOriginalText;
    }
  });
  const status = document.querySelector("#currentWork");
  if (status && busy) status.textContent = detail || "Preparing render…";
}

async function waitForRenderJob(jobId) {
  const deadline = Date.now() + 2 * 60 * 60 * 1000;
  while (Date.now() < deadline) {
    const jobs = await pollJobs();
    const job = jobs.find((item) => String(item.id) === String(jobId));
    if (job && ["done", "pending_review", "partial_failed", "error", "cancelled"].includes(job.status)) {
      if (!["done", "pending_review"].includes(job.status)) throw new Error(jobErrorText(job) || `Render ${job.status}`);
      return job;
    }
    await new Promise((resolve) => setTimeout(resolve, 800));
  }
  throw new Error(`Render job ${jobId} timed out after 2 hours.`);
}

function jobErrorText(job) {
  if (!job) return "";
  const parts = [];
  if (job.error) parts.push(String(job.error));
  if (Array.isArray(job.batch_errors)) {
    job.batch_errors.forEach((item) => {
      const detail = item && (item.error || item.message);
      if (detail && !parts.some((part) => part.includes(String(detail)))) {
        parts.push(`song ${item.segment_id ?? item.song ?? "?"}: ${detail}`);
      }
    });
  }
  if (job.stderr_tail) parts.push(String(job.stderr_tail));
  return parts.join("\n\n");
}

function renderResultSummary(job) {
  if (!job) return "Ready";
  if (job.status === "done") return ["render", "mix", "preview"].includes(job.kind || "render") ? "All done. Your renders are ready." : "All done.";
  if (job.status === "pending_confirmation") return "Songs ready for confirmation.";
  if (job.status === "pending_review") return "Some songs need review.";
  if (job.status === "cancelled") return "Render cancelled.";
  if (/no space left|not enough disk space/i.test(jobErrorText(job))) return "Not enough disk space. Free some space and try again.";
  if (job.status === "partial_failed") return "Some renders failed. See the report for details.";
  return "Render failed. See the report for details.";
}

function jobReportText(job) {
  return `ZuckerMixer report · ${job.id}\nStatus: ${job.status}\n${renderResultSummary(job)}\n` +
    (job.batch_summary ? JSON.stringify(job.batch_summary, null, 2) + "\n" : "") +
    jobErrorText(job);
}

async function copyJobReport(job, button) {
  const report = jobReportText(job);
  try {
    await navigator.clipboard.writeText(report);
  } catch (_error) {
    const text = document.createElement("textarea");
    text.value = report;
    text.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0";
    document.body.appendChild(text);
    text.select();
    const copied = document.execCommand("copy");
    text.remove();
    if (!copied) {
      const dialog = document.createElement("dialog");
      const field = document.createElement("textarea");
      field.value = report;
      field.readOnly = true;
      field.style.cssText = "width:70vw;height:50vh";
      const close = document.createElement("button");
      close.textContent = "Close";
      close.onclick = () => { dialog.close(); dialog.remove(); };
      dialog.append(field, close);
      document.body.appendChild(dialog);
      makeDialogDismissible(dialog, () => { dialog.close(); dialog.remove(); });
      dialog.showModal();
      field.select();
      return;
    }
  }
  button.textContent = "Copied";
}

async function ensureRenderPlans(songIds) {
  const missing = [];
  for (let index = 0; index < songIds.length; index += 1) {
    const songId = songIds[index];
    setRenderControlsBusy(true, `Preparing DSP plan ${index + 1}/${songIds.length}…`);
    const started = Date.now();
    const watchdog = setInterval(() => {
      const elapsed = Math.floor((Date.now() - started) / 1000);
      if (elapsed >= 10) {
        const status = document.querySelector("#currentWork");
        if (status) status.textContent = `Preparing DSP plan ${index + 1}/${songIds.length}… stalled ${elapsed}s for song ${songId}`;
      }
    }, 1000);
    let response;
    let data;
    try {
      response = await fetch(`/api/mix-plan-status/${songId}`);
      data = await response.json().catch(() => ({}));
    } finally {
      clearInterval(watchdog);
    }
    if (!response.ok) throw new Error(data.error || `Could not inspect DSP plan for song ${songId}.`);
    if (!data.valid) missing.push({ songId, reason: data.reason || "Analyze required." });
  }
  if (!missing.length) return;
  for (let index = 0; index < missing.length; index += 1) {
    const { songId } = missing[index];
    setRenderControlsBusy(true, `Preparing DSP plan ${index + 1}/${missing.length}…`);
    const response = await fetch(`/api/analyze-mix/${songId}`, { method: "POST" });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      throw new Error(data.error || `Analyze required for song ${songId}.`);
    }
  }
}

async function ensureMixParamsForSong(songId) {
  const song = appState.songs.find((item) => Number(item.id) === Number(songId));
  if (song?.active_stems?.length && song?.mix_params?.stems) return;
  const res = await fetch(`/api/active-stems/${songId}`);
  if (!res.ok) throw new Error("active stems unavailable");
  const data = await res.json();
  if (song) {
    song.active_stems = data.active_stems || [];
    song.mix_params = data.mix_params || null;
  }
}

async function mixSongs(songs, useBatchMaster = true, isBatchAction = songs.length > 1, preflightLabel = "") {
  if (!songs.length) {
    showToast("Choose at least one song.");
    return;
  }
  if (renderInProgress) {
    showToast(activeRenderJobId ? `Render already in progress (${activeRenderJobId}).` : "Render already in progress.");
    return;
  }
  const requestedSongs = songs.map((songId) => Number(songId));
  if (new Set(requestedSongs).size !== requestedSongs.length) {
    showToast("The batch contains duplicate songs.");
    return;
  }
  setRenderControlsBusy(true, preflightLabel || "Preparing render…");
  let jobId = null;
  try {
    const useSavedMixes = isBatchAction ? await chooseMixSource() : true;
    if (useSavedMixes == null) return;
    if (useBatchMaster) {
      const batchMaster = $("#batchMaster")?.value || "natural";
      songs.forEach((songId) => {
        const nextTarget = batchMaster === "loud" ? -9.5 : -14;
        const current = songOverrides(songId);
        const changed = current.mastering_intensity !== batchMaster || Number(current.target_lufs) !== nextTarget;
        current.mastering_intensity = batchMaster;
        current.target_lufs = nextTarget;
        livePreviewSongOverrides(songId).mastering_intensity = batchMaster;
        livePreviewSongOverrides(songId).target_lufs = nextTarget;
        if (changed) {
          markOverrideSequence(songId);
          pendingOverrideReasons.add("batch-master");
        }
      });
    }
    const singleSong = songs.length === 1 ? songs[0] : null;
    // Render preserves saved cuts and prepares any missing per-song analysis
    // in its worker before mixing. It never redetects the session.
    const previewEffectiveMix = singleSong != null ? effectiveMixDump(singleSong, "before-render-click") : null;
    const renderTargetDir = await chooseRenderFolder();
    if (renderTargetDir === false) return;
    console.info("RENDER DESTINATION request_target", renderTargetDir);
    await prepareOverridesForRender();
    // Preparation belongs to the worker, with visible progress; preserve all
    // requested songs and the current saved mix controls.
    const overridesResponse = await fetch("/api/overrides");
    const overridesSnapshot = overridesResponse.ok
      ? await overridesResponse.json()
      : cloneOverridesPayload();
    const url = singleSong != null ? `/api/render/${singleSong}` : "/api/render";
    const body = singleSong != null
      ? { render_target_dir: renderTargetDir || undefined, render_destination_trace: { save_dialog_return: renderTargetDir }, use_saved_mixes: useSavedMixes, preview_effective_mix: previewEffectiveMix, overrides_snapshot: overridesSnapshot }
      : { songs: requestedSongs, requested_song_ids: requestedSongs, expected_song_count: requestedSongs.length, render_target_dir: renderTargetDir || undefined, render_destination_trace: { save_dialog_return: renderTargetDir }, use_saved_mixes: useSavedMixes, overrides_snapshot: overridesSnapshot };
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Could not create render job.");
    if (!data.id) throw new Error("Render request returned no job id.");
    jobId = data.id;
    if (isBatchAction) {
      const accepted = (data.validated_song_ids || data.songs || []).map((value) => Number(value));
      if (accepted.length !== requestedSongs.length || accepted.some((value, index) => value !== requestedSongs[index])) {
        throw new Error("The backend did not preserve the complete requested song list.");
      }
    }
    activeRenderJobId = jobId;
    setRenderControlsBusy(true, `Rendering ${jobId}…`);
    await waitForRenderJob(jobId);
  } catch (error) {
    console.error("[render] failed", error);
    const message = /no space left|not enough disk space/i.test(String(error.message || error)) ? "Not enough disk space. Free some space and try again." : "Render failed. See the report for details.";
    showToast(`Render failed: ${message}`);
    const status = document.querySelector("#currentWork");
    if (status) status.textContent = `Render failed: ${message}`;
    const detail = document.querySelector("#queuePosition");
    if (detail) detail.textContent = message;
  } finally {
    activeRenderJobId = null;
    setRenderControlsBusy(false);
    await pollJobs().catch(() => {});
  }
}

function mixEverything() {
  const songs = renderableSongs().map((song) => Number(song.id));
  if (!songs.length) {
    showToast("No valid songs are available for Mix everything.");
    return;
  }
  const preflight = `Preparing ${songs.length} songs`;
  const currentWork = document.querySelector("#currentWork");
  if (currentWork) currentWork.textContent = preflight;
  console.info("[batch] Mix everything requested", { count: songs.length, song_ids: songs });
  mixSongs(songs, true, songs.length > 1, preflight);
}

async function startOptionalWhisperAnalysis() {
  const response = await fetch("/api/redetect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ allow_whisper: true }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || !data.id) {
    showToast(data.error || "Could not start optional Whisper analysis.");
    return;
  }
  setLoadingOverlayJob(data);
  showToast("Optional Whisper analysis started.");
  await pollJobs();
}
async function runSecondWhisperPass() {
  setCutLoading("Second Whisper pass", "Finding missing commentator presentations in suspicious intervals", 10);
  const response = await fetch("/api/redetect/second-pass", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  const result = await response.json().catch(() => ({}));
  if (!response.ok || result.ok === false) { setCutLoading("Error", result.error || result.reason || "Second Whisper pass failed.", 100); return showToast(result.error || result.reason || "Second Whisper pass failed."); }
  setCutLoading("Finished", `Found ${result.new_proposals?.length || 0} new proposals; ${result.missing_count || 0} possible slots still need review`, 100);
  showToast(`Second Whisper pass: ${result.new_proposals?.length || 0} new proposals. Needs review: ${result.missing_count || 0}.`);
  showSecondPassCandidate(result);
  await refreshState({ renderLarge: false });
}

function showSecondPassCandidate(candidate) {
  const node = $("#audioScanSummary");
  if (!node || !candidate) return;
  const proposals = Array.isArray(candidate.new_proposals) ? candidate.new_proposals : [];
  const detail = proposals.map((item) => `${cutTime(item.start)} · ${esc(item.text || "additional presentation")} · needs_review`).join("<br>") || "No new proposal was safe to accept automatically.";
  const existing = node.querySelector(".second-pass-result"); existing?.remove();
  const box = document.createElement("div"); box.className = "second-pass-result scan-warning"; box.innerHTML = `<strong>Needs review: ${candidate.missing_count || 0} possible missing slots</strong><br>Second pass found ${proposals.length} candidate(s); manual cuts remain authoritative.<br>${detail}`; node.appendChild(box);
}

function chooseMixSource() {
  const dialog = $("#mixChoiceDialog");
  if (!dialog) return Promise.resolve(null);
  makeDialogDismissible(dialog);
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => { if (settled) return; settled = true; resolve(value); };
    $("#useAutomaticMixes").onclick = () => { finish(false); dialog.close(); };
    $("#useSavedMixes").onclick = () => { finish(true); dialog.close(); };
    dialog.addEventListener("close", () => finish(null), { once: true });
    dialog.showModal();
  });
}

async function resetSongToAutomatic(songId) {
  if (!await showConfirm(`Clear saved overrides for Song ${songId} and use the current automatic mix?`)) return;
  const response = await fetch(`/api/reset-automatic/${songId}`, { method: "POST" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) return showToast(data.error || "Could not reset this song.");
  await refreshState({ renderLarge: true });
  showToast(`Song ${songId} reset to automatic mix.`);
}



  const LOADING_MESSAGE_CATALOG = {
    preparing: [
      "Opening the original session...", "Keeping every stem aligned...", "Waking the session clock...",
      "Checking that the band arrived together...", "Unpacking the long take without losing the plot...",
      "Finding the beginning before finding the songs...", "The tape is long; the coffee is longer...",
      "Making sure nobody starts on the wrong bar...", "Reading the room before touching the faders...",
      "Preparing the session map...", "Counting microphones, cables and suspicious silences...",
      "The original recording is taking a deep breath...", "Setting the compass for a very long jam...",
      "No song left behind...", "Loading the session with theatrical patience...",
      "Getting the whole band on the same timeline..."
    ],
    scanning: [
      "Checking the room mics...", "Reading the session clock...", "Looking for audio hiding in the folder...",
      "Inspecting every stem, even the shy ones...", "Making sure the WAVs are really WAVs...",
      "Counting channels before they count themselves...", "Checking which microphones actually contain music...",
      "Scanning the long take from end to end...", "Listening for the first real downbeat...",
      "Separating signal from cable philosophy...", "Looking for silence that means something...",
      "Making friends with the external drive...", "Checking offsets so the band stays together...",
      "The stems are lining up like a very patient choir...",
      "Searching for the places where the presenter appears...",
      "No folder left unexamined..."
    ],
    "analyzing stems": [
      "Listening to the drummer...", "Comparing the parallel stems...", "Measuring who is actually playing...",
      "Finding the musical signal inside the room tone...", "Checking the kick against the bass...",
      "Looking for the snare's opinion...", "Comparing the two microphones fairly...",
      "Finding which guitar is trying to run the meeting...", "Checking whether the piano brought too much furniture...",
      "Listening for brass, flute and other plot twists...", "Measuring the quiet parts without calling them empty...",
      "Separating musical dynamics from microphone noise...", "Finding the useful peaks...",
      "Building a level map for the whole band...", "Checking the harmonic crowding...",
      "The stems are introducing themselves one by one..."
    ],
    "reading cached envelopes": [
      "Reusing the audio map...", "Reading cached activity envelopes...", "Opening yesterday's map without trusting ghosts...",
      "Checking the saved timeline...", "Finding the quietest safe boundaries...",
      "Reconstructing the session from its acoustic footprints...", "The cache remembers more than the presenter...",
      "Reading the envelope trail...", "Comparing the new source signature...",
      "Making sure this cache belongs to this folder...", "Looking for old cuts that still make sense...",
      "Turning yesterday's measurements into today's decisions...",
      "The waveform has left breadcrumbs...", "Checking the cache before doing the long work again...",
      "Recovering the session's musical geography...", "No stale snapshot gets to run the show..."
    ],
    "transcribing speech": [
      "Listening to the commentator...", "Waiting for the transcript...", "Finding the introductions between the songs...",
      "Separating presenter from trumpet...", "Reading the stage announcements...", "Looking for names, titles and useful clues...",
      "Whisper is listening carefully...", "Checking whether this voice starts the next slot...",
      "Turning speech into song boundaries...", "The microphone is telling us what comes next...",
      "Finding the human hand in the long recording...", "Checking the presenter against the instruments...",
      "Not every word is a cut, but every cut needs a reason...",
      "Listening for the announcement that unlocks the next song...",
      "Speech first, panic never...", "The presenter is acting as the session's chapter titles..."
    ],
    "detecting songs": [
      "Finding where the MC talks...", "Looking for the next song...", "Following the presenter-led slots...",
      "Marking beginnings without cutting through music...", "Counting musical chapters...",
      "Finding the gap that is actually a beginning...", "Checking whether this is one song or two...",
      "Building the song list from the whole session...", "Protecting the middle of every performance...",
      "Looking for a clean hand-off...", "The set is becoming a tracklist...",
      "Finding the next real entrance...", "Separating silence, introduction and music...",
      "Checking the expected song count without forcing it...",
      "One boundary at a time...", "The jam is finally giving up its structure..."
    ],
    "merging song boundaries": [
      "Merging boundaries without losing songs...", "Joining evidence from voice and instruments...",
      "Resolving two timestamps that both think they are right...", "Keeping the safest cut...",
      "Comparing adjacent candidates...", "Making the timeline agree with itself...",
      "Checking that no song is swallowed by a merge...", "The boundaries are negotiating...",
      "Protecting introductions at the front of each slot...", "Removing duplicate cuts, not real songs...",
      "Looking for the cleanest shared boundary across all stems...",
      "The timeline is folding itself neatly...", "No musical phrase gets chopped for administrative reasons...",
      "Merging clues, preserving performances...", "Almost a tracklist...", "Making the final decision at the quiet point..."
    ],
    "validating cuts": [
      "Checking the proposed boundaries...", "Preparing the cuts for review...", "Making sure no instrument is mid-phrase...",
      "Checking every stem at the edge...", "Confirming the presenter belongs to the next song...",
      "Testing the boundary against the full session...", "Looking for active instruments at the cut...",
      "Keeping unsafe cuts visible instead of hiding them...", "Checking duration and musical continuity...",
      "The cut gate is asking difficult questions...", "No silent drummer gets blamed...",
      "Verifying that every slot shares the same timeline...", "Checking the first seconds of every song...",
      "A good cut sounds inevitable...", "Reviewing the edges before rendering...", "The scissors are staying polite..."
    ],
    mixing: [
      "Balancing the band...", "Giving every stem its place...", "Letting the kick lead without swallowing the bass...",
      "Finding space for the two microphones...", "Taming the guitar before it calls the police...",
      "Keeping the piano below the vocal spotlight...", "Matching the flute to the trumpet...",
      "Putting the overheads in the room...", "Checking that quiet tracks are musical, not noisy...",
      "Building the mix from the rhythm section upward...", "Making room for the voice...",
      "Keeping the bass present without making mud...", "Finding the song's natural center...",
      "The faders are having a serious conversation...", "Balancing tone, level and space...",
      "This is the part where the band becomes a record..."
    ],
    mastering: [
      "Polishing the final bounce...", "Keeping the kick as the peak without clipping...",
      "Checking loudness and headroom...", "Making the master travel well...",
      "Preserving the dynamics while controlling the edges...", "Checking the true peak ceiling...",
      "Keeping the room around the instruments...", "Making sure the master breathes...",
      "Comparing loudness without flattening the song...", "Finishing the mix with a light touch...",
      "Checking the low end one last time...", "Making the vocal survive small speakers...",
      "The final polish is almost dry...", "Mastering, not repainting the whole house...",
      "Checking that the reverb did not move into the foreground...", "One last pass over the stereo image..."
    ],
    encoding: [
      "Packing the mix for listening...", "Putting the finishing label on it...", "Writing the MP3 without losing the master...",
      "Checking that the export decodes cleanly...", "Turning the finished song into a portable record...",
      "Keeping the metadata tidy...", "Making the file easy to find later...",
      "Checking the final duration...", "Writing the audio at full quality...",
      "The song is leaving the studio...", "Verifying the last bytes...",
      "Making a copy that the player will actually understand...",
      "Almost ready for the headphones...", "Exporting the finished chapter...",
      "The mix is getting its passport...", "Final file, final check..."
    ],
    loading: [
      "Warming up the tape machine...", "Finding the exact waveform...", "Opening the selected slot...",
      "Drawing the session at a useful scale...", "Preparing the playhead...",
      "Making the waveform large enough to see...", "Loading the audio without moving the cut...",
      "Finding the exact point under the cursor...", "The waveform is stretching its legs...",
      "Checking the selected song...", "Preparing playback and navigation...",
      "Keeping the editor responsive...", "One slot, all stems, one timeline...",
      "Loading the part you actually asked to hear...", "The waveform is nearly on stage...",
      "Getting ready for a precise cut..."
    ],
    cancelling: [
      "Stopping the worker safely...", "Closing the session without leaving ghosts...",
      "Asking every process to come back from the coffee break...", "Releasing the audio files...",
      "Putting the faders down gently...", "Stopping the render queue...",
      "Closing the loading screen after the worker confirms...", "Cleaning up temporary files...",
      "Cancelling without touching your saved cuts...", "The band is packing up...",
      "Waiting for the last process to leave the stage...", "Returning to the start screen...",
      "No render will be left running backstage...", "Finishing the cancellation safely...",
      "The session is stopping, not crashing...", "Almost back to the main screen..."
    ],
    fallback: [
      "Keeping the session moving...", "The band is still tuning...", "Checking one more thing...",
      "Working through the long take...", "The audio map is thinking...", "Patience, but with diagnostics...",
      "Making progress behind the curtain...", "The next useful detail is on its way...",
      "No panic: the worker is still alive...", "Counting carefully...", "Listening before deciding...",
      "The session is larger than it looks...", "One careful step at a time...", "Still working on the good version...",
      "The machine is thinking in bars...", "Almost a useful answer..."
    ]
  };

  function loadingMessagesForState(state) {
    const value = state || {};
    const stage = String(value.current_stage || value.stage || value.detail || "").toLowerCase();
    const song = Number(value.current || value.current_song || value.song_index || 0);
    const total = Number(value.total_count || value.total || value.song_count || 0);
    let key = Object.keys(LOADING_MESSAGE_CATALOG).find((name) => name !== "fallback" && stage.includes(name));
    if (!key && String(value.status || "").toLowerCase() === "stopping") key = "cancelling";
    if (!key && value.kind === "cut") key = "loading";
    const base = LOADING_MESSAGE_CATALOG[key || "fallback"];
    const prefix = song > 0 && total > 0 ? `Song ${song}/${total}: ` : "";
    const contextual = base.map((message) => prefix + message);
    if (stage.includes("transcrib") && value.allow_whisper === false) {
      return [
        "Whisper is optional here; using the acoustic timeline...",
        "No presenter transcript required for this pass...",
        "Checking song boundaries without waiting for Whisper...",
        "The stems are providing the structure...",
        "Using the session's musical evidence first..."
      ];
    }
    return contextual;
  }

function renderLoadingOverlay() {
  const overlay = $("#loadingOverlay");
  if (!overlay) return;
  // Edit Cuts owns the foreground while its dialog is open. A background
  // Whisper/render job must not cover or block waveform editing.
  const cutEditorOpen = Boolean(document.querySelector("#cutSelectorDialog[open]"));
  const state = cutEditorOpen ? loadingOverlayCut : (loadingOverlayCut || loadingOverlayJob);
  const busy = Boolean(state) && !suppressLoadingOverlay;
  overlay.hidden = !busy;
  document.body.classList.toggle("loading-mode", busy);
  const video = $("#loadingStageVideo");
  if (!busy) {
    if (window.renderListening?.isPlaying()) window.renderListening.pause();
    video?.pause();
    return;
  }
  if (video?.paused) video.play().catch(() => {});
  const stage = String(state.current_stage || state.stage || "").toLowerCase();
  const isRenderTask = ["render", "mix"].includes(String(state.kind || "").toLowerCase());
  const title = state.kind === "cut" ? (state.label || "Loading editor") :
    (state.status === "stopping" ? "Canceling…" :
      stage.includes("preparing") || stage === "waiting" ? "Preparing the session..." :
      stage.includes("redetect") || stage.includes("detecting") ? "Finding songs..." : "Working...");
  const detail = state.detail || state.stage_detail || "Please wait...";
  const progress = Number(state.kind === "redetect" ? state.progress : (state.progress ?? state.song_progress ?? 0));
  const renderProgress = Number(state.process_progress ?? state.song_progress ?? 0);
  const choices = loadingMessagesForState(state);
  const fun = state.kind === "cut" ? "" : choices[Math.floor(Date.now() / 5000) % choices.length];
  const safeProgress = Math.max(0, Math.min(100, Number.isFinite(progress) ? progress : 0));
  const safeRenderProgress = Math.max(0, Math.min(100, Number.isFinite(renderProgress) ? renderProgress : 0));
  const titleNode = $("#loadingOverlayTitle");
  const detailNode = $("#loadingOverlayDetail");
  const timingNode = $("#loadingOverlayTiming");
  const funNode = $("#loadingOverlayFun");
  const fillNode = $("#loadingOverlayProgressFill");
  const percentNode = $("#loadingOverlayPercent");
  const renderRow = $("#loadingRenderProgress");
  const renderLabel = $("#loadingRenderLabel");
  const renderFill = $("#loadingRenderProgressFill");
  const renderPercent = $("#loadingRenderPercent");
  const phaseList = $("#loadingPhaseList");
  const activityAge = $("#loadingActivityAge");
  const health = $("#loadingOverlayHealth");
  const songStates = $("#loadingSongStates");
  const cancelNode = $("#cancelLoadingWork");
  const versionNode = $("#loadingOverlayVersion");
  const cancellable = state.kind !== "cut" && ["queued", "running", "stopping"].includes(String(state.status || ""));
  if (cancelNode) {
    cancelNode.hidden = !cancellable;
    cancelNode.disabled = String(state.status || "") === "stopping";
    cancelNode.textContent = String(state.status || "") === "stopping" ? "Canceling…" : "Cancel loading";
  }
  if (titleNode) titleNode.textContent = title;
  if (detailNode) detailNode.textContent = detail;
  if (versionNode) {
    const build = appState?.build || {};
    versionNode.textContent = build.app_version ? `ZuckerMixer ${build.app_version}` : "";
  }
  if (timingNode) {
    const elapsed = Number(state.elapsed_seconds || 0);
    const eta = Number(state.eta_seconds || 0);
    const elapsedText = elapsed >= 60 ? `Elapsed: ${Math.floor(elapsed / 60)}m ${String(Math.floor(elapsed % 60)).padStart(2, "0")}s` : `Elapsed: ${Math.floor(elapsed)}s`;
    const etaText = eta > 0 ? `ETA: ~${eta >= 60 ? `${Math.floor(eta / 60)}m ${Math.round(eta % 60)}s` : `${Math.round(eta)}s`}` : `Stage: ${state.phase_index != null ? Number(state.phase_index) + 1 : "—"}/${state.phase_total || 7}`;
    timingNode.innerHTML = `<span>${esc(elapsedText)}</span><span>${esc(etaText)}</span>`;
  }
  if (funNode) funNode.textContent = fun;
  if (fillNode) fillNode.classList.toggle("progress-indeterminate", Boolean(state.indeterminate));
  if (fillNode) fillNode.style.width = String(safeProgress) + "%";
  if (percentNode) percentNode.textContent = state.indeterminate ? "Working…" : String(Math.round(safeProgress)) + "%";
  const showProcessRow = state.kind !== "cut";
  if (renderRow) renderRow.hidden = !showProcessRow;
  if (renderLabel && showProcessRow) {
    if (state.kind === "redetect" || state.current_stage === "transcribing speech") {
      const completed = Number(state.whisper_completed_windows || 0);
      const total = Number(state.whisper_total_windows || state.candidate_windows || 0);
      const calibrationCurrent = Number(state.calibration_current || 0);
      const calibrationTotal = Number(state.calibration_total || 0);
      const stageLabel = state.current_stage === "transcribing speech" ? "Whisper" : (state.current_stage || "Process");
      const modelLoading = state.current_stage === "transcribing speech"
        && !state.whisper_model_ready
        && /loading|starting|runtime/i.test(String(state.stage_detail || ""));
      renderLabel.textContent = modelLoading
        ? `${stageLabel} · loading model`
        : calibrationTotal
          ? `${stageLabel} · ${calibrationCurrent}/${calibrationTotal}`
          : total
            ? `${stageLabel} · ${completed}/${total}`
            : stageLabel;
    } else {
      const current = state.current_item || (state.current ? `Song ${String(state.current).padStart(2, "0")}` : "Current process");
      const total = state.total_count || (Array.isArray(state.songs) ? state.songs.length : "");
      renderLabel.textContent = total ? `${current} · ${state.current || 0}/${total}` : current;
    }
  }
  if (renderFill) renderFill.style.width = String(safeRenderProgress) + "%";
  if (renderPercent) renderPercent.textContent = String(Math.round(safeRenderProgress)) + "%";
  const phases = isRenderTask ? [["prepare", "Check saved cuts"], ["analyze", "Prepare this song’s mix"], ["mix", "Mix audio blocks"], ["master", "Master"], ["encode", "Encode MP3"], ["save", "Save result"]] : state.kind === "cut" ? [["edit", state.label || "Edit Cuts"]] : [
    ["prepare", "Prepare original session"],
    ["scan", "Scan WAV stems"],
    ["cache", "Read audio envelopes"],
    ["analyze", "Analyze instruments"],
    ["whisper", "Transcribe commentator"],
    ["merge", "Merge song boundaries"],
    ["review", "Prepare cuts"],
  ];
  const phaseIndex = state.kind === "cut" ? 0 : isRenderTask ? (stage.includes("analyz") ? 1 : stage.includes("mix") ? 2 : stage.includes("master") ? 3 : stage.includes("encod") ? 4 : stage.includes("finish") ? 5 : 0) : Number.isFinite(Number(state.phase_index)) ? Number(state.phase_index) : (
    stage.includes("prepar") || stage === "waiting" ? 0 :
    stage.includes("scanning") ? 1 :
    stage.includes("cached") || stage.includes("envelope") ? 2 :
    stage.includes("analyz") || stage.includes("activity") ? 3 :
    stage.includes("transcrib") || stage.includes("whisper") ? 4 :
    stage.includes("validat") ? 6 : 5
  );
  if (phaseList) {
    phaseList.innerHTML = phases.map((phase, index) => {
      const cls = index < phaseIndex ? "done" : index === phaseIndex ? "active" : "";
      const mark = index < phaseIndex ? "✓" : index === phaseIndex ? "●" : "○";
      return `<div class="loading-phase ${cls}"><span class="loading-phase-mark">${mark}</span><span>${phase[1]}</span></div>`;
    }).join("");
  }
  if (songStates) {
    const summaryRows = Array.isArray(state.batch_summary?.songs) ? state.batch_summary.songs : [];
    const requested = Array.isArray(state.songs) ? state.songs : [];
    const summaryByNumber = new Map(summaryRows.map((row) => {
      const number = Number(row.song ?? row.index ?? row.id);
      return [Number.isFinite(number) ? number : String(row.song ?? row.index ?? row.id ?? ""), row];
    }));
    const queueRows = requested.length
      ? requested.map((number, index) => {
        const numericNumber = Number(number);
        const displayNumber = appState.songs.find(song => Number(song.id) === numericNumber)?.display_number ?? numericNumber;
        const summary = summaryByNumber.get(Number(displayNumber));
        if (summary) return { ...summary, song: displayNumber };
        const isCurrent = numericNumber === Number(state.current_segment_id ?? state.current);
        const isDone = !isCurrent && index < Number(state.done_count || 0);
        return {
          song: displayNumber,
          status: isCurrent ? "working" : isDone ? "done" : "queued",
          detail: isCurrent ? "working" : isDone ? "done" : "queued",
        };
      })
      : summaryRows;
    if (queueRows.length) {
      songStates.innerHTML = queueRows.map((row, rowIndex) => {
        const number = row.song ?? row.index ?? row.id ?? "?";
        const reportedStatus = String(row.status || row.state || "queued").toLowerCase();
        const explicitFinished = ["done", "completed", "rendered", "written", "mp3_written", "finished"].includes(reportedStatus);
        const inferredFinished = !explicitFinished
          && !reportedStatus.includes("fail")
          && !reportedStatus.includes("error")
          && Number(rowIndex) < Number(state.done_count || 0)
          && Number(number) !== Number(state.current);
        const finished = explicitFinished || inferredFinished;
        const status = finished ? "done" : reportedStatus;
        const cls = status.includes("fail") || status.includes("error")
          ? "error"
          : finished
            ? "done"
            : Number(number) === Number(state.current)
              ? "active"
              : "";
        const mark = finished ? "✓" : status.includes("fail") || status.includes("error") ? "!" : status === "working" || Number(number) === Number(state.current) ? "●" : "○";
        const detail = finished ? "done" : (row.detail || row.status || row.state || "queued");
        return `<div class="loading-song-state ${cls}" data-song-status="${esc(status)}" data-song-index="${esc(number)}"><span class="loading-song-label"><span class="loading-song-check" aria-hidden="true">${mark}</span> Song ${String(number).padStart(2, "0")}</span><span>${esc(detail)}</span></div>`;
      }).join("");
    } else {
      const facts = [
        state.stem_count ? `${state.stem_count} stems` : "",
        state.candidate_windows ? `${state.candidate_windows} speech windows` : "",
        state.transcript_count ? `${state.transcript_count} transcripts` : "",
        state.candidate_count ? `${state.candidate_count} song candidates` : "",
      ].filter(Boolean);
      songStates.innerHTML = facts.length ? `<div class="loading-song-state"><span>Detection status</span><span>${esc(facts.join(" · "))}</span></div>` : "";
    }
  }
  window.renderListening?.sync();
  const lastUpdate = Number(state.progress_updated_at || state.heartbeat || state.updated_at || 0) * 1000;
  const age = lastUpdate ? Math.max(0, Math.floor((Date.now() - lastUpdate) / 1000)) : 0;
  if (activityAge) activityAge.textContent = age ? `updated ${age}s ago` : "updating now";
  if (health) {
    const stale = age >= 12;
    health.classList.toggle("stale", stale);
    health.textContent = stale
      ? `No new worker event for ${age}s. Still in “${state.current_stage || "unknown"}”; the process may be reading a large source or blocked.`
      : (state.last_event || detail || "Worker is active.");
  }
}

function setLoadingOverlayJob(job) {
  if (job && String(job.id) !== String(loadingOverlayJob?.id)) suppressLoadingOverlay = false;
  loadingOverlayJob = job || null;
  renderLoadingOverlay();
}

function renderJobs(items) {
  const box = $("#jobs");
  box.innerHTML = "";
  // Prefer the job that is actually executing. A queued redetect can be newer
  // than a running one (for example after a double click); showing its 0%
  // status made the live analysis bar appear frozen even while the worker
  // was reporting real progress.
  const currentSource = loadingOverlayJob?.source_folder || appState?.source_folder || appState?.settings?.source_folder;
  const sourceItems = items.filter((job) => !job.source_folder || !currentSource || job.source_folder === currentSource);
  const requestedJob = loadingOverlayJob && sourceItems.find((job) => String(job.id) === String(loadingOverlayJob.id));
  const active = (requestedJob && ["queued", "running", "stopping"].includes(requestedJob.status) ? requestedJob : null) || [...sourceItems].reverse().find((job) => ["running", "stopping"].includes(job.status))
    || [...sourceItems].reverse().find((job) => job.status === "queued");
  // Keep the job received from /api/state through the first polling race.
  // Clear it only when the same job is observed terminal, never merely
  // because /api/jobs returned an incomplete snapshot for a moment.
  const trackedOverlay = loadingOverlayJob && items.find((job) => String(job.id) === String(loadingOverlayJob.id));
  const overlayLive = loadingOverlayJob && ["queued", "running", "stopping"].includes(String(loadingOverlayJob.status || ""));
  if (active && !suppressLoadingOverlay) {
    setLoadingOverlayJob(active);
  } else if (!suppressLoadingOverlay && trackedOverlay && !["queued", "running", "stopping"].includes(String(trackedOverlay.status || ""))) {
    setLoadingOverlayJob(null);
  } else if (!suppressLoadingOverlay && !overlayLive) {
    setLoadingOverlayJob(null);
  }
  const activeRender = active && ["render", "mix"].includes(active.kind) ? active : null;
  if (!renderInProgress && activeRender) {
    activeRenderJobId = activeRender.id;
    setRenderControlsBusy(true, `Rendering ${activeRender.id}…`);
  }
  const redetectButton = $("#redetectSongs");
  if (redetectButton) {
    redetectButton.disabled = sourceItems.some((job) => job.kind === "redetect" && ["queued", "running", "stopping"].includes(job.status));
  }
  const loading = $("#loadingStatus");
  const loadingFill = $("#loadingProgressFill");
  const loadingLabel = $("#loadingLabel");
  const loadingDetail = $("#loadingDetail");
  const detectionJob = active && active.kind === "redetect" ? active : null;
  if (loading && detectionJob) {
    loading.hidden = false;
    const scanning = String(detectionJob.current_stage || "").toLowerCase() === "scanning folder";
    loadingLabel.textContent = scanning ? "Scanning source folder..." : "Loading songs...";
    loadingDetail.textContent = `${detectionJob.stage_detail || "working"} · ${detectionJob.progress || 0}% · ${formatRemaining(detectionJob.eta_seconds)}`;
    loadingFill.style.width = `${detectionJob.progress || 0}%`;
    loading.classList.toggle("stalled", Date.now() - Number(detectionJob.progress_updated_at || detectionJob.heartbeat || Date.now() / 1000) * 1000 > 10000);
  } else if (loading) {
    loading.hidden = true;
  }
  $(".progress-box").classList.toggle("working", Boolean(active));
  if (active) {
    $(".progress-box").classList.remove("success", "warning");
    const current = active.current_item || (active.current ? `Song ${String(active.current).padStart(2, "0")}` : "Waiting");
    const stage = active.current_stage ? ` · ${active.current_stage}` : "";
    $("#currentWork").textContent = active.status === "stopping" ? "Canceling…" : `${current}${stage}`;
    const total = active.total_count || active.songs.length;
    const position = active.current ? (active.done_count || 0) + 1 : (active.done_count || 0);
    const elapsed = shortElapsed(active.started);
    const detail = active.stage_detail || "working";
    const workerPid = active.child_pid || active.pid || active.launch_pid || "pending";
    const lastEventAt = Number(active.last_event_at || active.progress_updated_at || active.heartbeat || 0) * 1000;
    const silenceSeconds = lastEventAt ? Math.max(0, Math.floor((Date.now() - lastEventAt) / 1000)) : 0;
    const stallText = silenceSeconds >= 10 ? ` · stalled ${silenceSeconds}s · last: ${active.last_event || detail}` : "";
    const memoryText = active.memory_mb != null ? ` · ${active.memory_mb} MB` : "";
    const reviewDetail = active.needs_review_count
      ? ` · Safe songs: ${active.safe_count || 0}/${total} · Needs review: ${active.needs_review_songs?.join(", ") || active.needs_review_count + "/" + total}`
      : "";
    const reasonDetail = active.needs_review_details?.[0]
      ? ` · Reason: ${active.needs_review_details[0].reason}${active.needs_review_details[0].stems?.length ? ` (${active.needs_review_details[0].stems.join(", ")})` : ""}`
      : "";
    const blockedDetail = active.current_stage === "pending_review" && active.needs_review_songs?.length
      ? ` · Current blocked song: ${active.needs_review_songs[0]}`
      : "";
    $("#queuePosition").textContent = `${detail}${reviewDetail}${blockedDetail}${reasonDetail} · PID ${workerPid} · ${position} of ${total} · ${active.song_progress || 0}% · ${elapsed || "0:00"} elapsed · ${formatRemaining(active.eta_seconds)}${memoryText}${stallText}`;
    $("#progressFill").style.width = `${active.song_progress || 0}%`;
    const fun = $("#progressFun");
    const choices = loadingMessagesForState(active);
    if (fun) fun.textContent = choices[Math.floor(Date.now() / 5000) % choices.length];
    const stall = $("#progressStall");
    const updated = Number(active.progress_updated_at || active.heartbeat || Date.now() / 1000) * 1000;
    const stale = Date.now() - updated > 10000;
    if (stall) {
      stall.hidden = !stale;
      stall.textContent = stale ? `No worker event for ${Math.floor((Date.now() - updated) / 1000)}s — stage: ${active.current_stage || "unknown"}; last event: ${active.last_event || active.stage_detail || "unknown"}. Cancel if it does not resume.` : "";
    }
  } else {
    const terminal = [...sourceItems].reverse().find((job) => ["done", "pending_confirmation", "pending_review", "partial_failed", "error", "cancelled"].includes(job.status));
    $(".progress-box").classList.toggle("success", terminal?.status === "done");
    $(".progress-box").classList.toggle("warning", Boolean(terminal && ["error", "partial_failed", "pending_review"].includes(terminal.status)));
    $("#currentWork").textContent = renderResultSummary(terminal);
    $("#queuePosition").textContent = "";
    if (terminal) {
      const copy = document.createElement("button");
      copy.textContent = "Copy report";
      copy.onclick = () => copyJobReport(terminal, copy);
      $("#queuePosition").appendChild(copy);
    }
    $("#progressFill").style.width = "0%";
    $("#progressFun").textContent = "";
    $("#progressStall").hidden = true;
  }
  items.slice(-5).reverse().forEach((job) => {
    const el = document.createElement("div");
    el.className = `job ${["error", "partial_failed"].includes(job.status) ? "error" : ""}`;
    const fullError = jobErrorText(job);
    const errorText = `<button class="copy-error" data-copy-error="${esc(job.id)}">Copy report</button>`;
    el.innerHTML = `
      <div>
        <strong>${friendlyStatus(job)}</strong>
        <p>${job.current ? `Song ${String(job.current).padStart(2, "0")}` : `${job.songs.length} songs`}${job.current_stage ? ` · ${job.current_stage}` : ""}</p>
        ${errorText}
      </div>
      <span>${job.progress || 0}%</span>
    `;
    box.appendChild(el);
    const copyButton = el.querySelector("[data-copy-error]");
    if (copyButton) copyButton.addEventListener("click", () => copyJobReport(job, copyButton));
  });
}

function friendlyStatus(job) {
  if (job.status === "queued") return "Waiting";
  if (job.status === "running") return "Mixing";
  if (job.status === "stopping") return "Stopping";
  if (job.status === "cancelled") return "Cancelled";
  if (job.status === "error") return "Needs attention";
  if (job.status === "partial_failed") return "Partial failure";
  if (job.status === "pending_review") return "Pending review";
  return "Finished";
}

function hideLoadingOverlayImmediately() {
  loadingOverlayJob = null;
  loadingOverlayCut = null;
  const video = $("#loadingStageVideo");
  if (video) {
    video.pause();
    video.currentTime = 0;
  }
  const fallback = $("#loadingStageFallback");
  if (fallback) fallback.hidden = true;
  const overlay = $("#loadingOverlay");
  if (overlay) overlay.hidden = true;
  document.body.classList.remove("loading-mode");
  const loading = $("#loadingStatus");
  if (loading) loading.hidden = true;
  renderLoadingOverlay();
}

let cancellationUiPending = false;
async function cancelActiveWork() {
  if (!await showConfirm("Are you sure you want to cancel all active work?")) return;
  const buttons = [$("#cancelLoadingWork"), $("#cancelJob")].filter(Boolean);
  buttons.forEach((button) => {
    button.disabled = true;
    button.textContent = "Canceling…";
  });
  cancellationUiPending = true;
  window.renderListening?.pause();
  if (loadingOverlayJob) loadingOverlayJob = { ...loadingOverlayJob, status: "stopping", current_stage: "cancelling", stage_detail: "Stopping active processes…" };
  renderLoadingOverlay();
  showToast("Canceling… stopping the active processes.");
  setRenderControlsBusy(true);

  const finishCancellationUi = async (message) => {
    cancellationUiPending = false;
    const cutDialog = $("#cutSelectorDialog");
    if (cutDialog?.open) cutDialog.close("cancel");
    cutSelector = null;
    hideLoadingOverlayImmediately();
    setRenderControlsBusy(false);
    buttons.forEach((button) => {
      button.disabled = false;
      if (button.id === "cancelLoadingWork") button.textContent = "Cancel loading";
      if (button.id === "cancelJob") button.textContent = "Cancel";
    });
    if (message) showToast(message);
  };

  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 1500);
    let response;
    try {
      response = await fetch("/api/cancel", { method: "POST", signal: controller.signal });
    } finally {
      clearTimeout(timeout);
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || "Could not cancel the active work.");
    }
  } catch (error) {
    cancellationUiPending = false;
    buttons.forEach(button => { button.disabled = false; button.textContent = "Retry cancel"; });
    showToast(error.name === "AbortError" ? "Could not confirm cancellation yet. Retry cancel." : (error.message || String(error)));
    return;
  }

  // Do not make the browser wait for ffmpeg/Whisper. Poll in the background
  // and restore the normal page only after the backend reports a terminal job.
  void (async () => {
    let terminal = false;
    while (cancellationUiPending) {
      try {
        const jobs = await pollJobs();
        if (!jobs.some((job) => ["queued", "running", "stopping"].includes(String(job.status || "")))) {
          terminal = true;
          break;
        }
      } catch (_error) {
        // The cancellation request was acknowledged. A temporary poll error
        // must not reopen the loading screen or freeze the controls.
      }
      await new Promise((resolve) => setTimeout(resolve, 350));
    }
    if (terminal) {
      await finishCancellationUi("All active work was cancelled. Ready.");
      await refreshState({ renderLarge: true }).catch(() => {});
    } else {
      showToast("Canceling… waiting for process cleanup. Keep this window open or close the application to stop it.");
    }
  })();
}
let jobsPollPromise = null;
function pollJobs() {
  if (!jobsPollPromise) jobsPollPromise = pollJobsOnce().finally(() => { jobsPollPromise = null; });
  return jobsPollPromise;
}

async function pollJobsOnce() {
  const res = await fetch("/api/jobs");
  const jobs = (await res.json()).map(job => cancellationUiPending && ["queued", "running", "stopping"].includes(job.status) ? {...job, status: "stopping", current_stage: "cancelling", stage_detail: "Stopping active processes…"} : job);
  console.log("[jobs poll]", jobs.map((job) => ({
    id: job.id,
    status: job.status,
    current: job.current,
    progress: job.progress,
    songProgress: job.song_progress,
    stage: job.current_stage,
    detail: job.stage_detail,
    doneCount: job.done_count,
    totalCount: job.total_count,
  })));
  // The live job poll starts before the initial state request. Render the
  // loading overlay even before /api/state has finished hydrating.
  if (!appState) {
    renderJobs(jobs);
    return jobs;
  }
  // A native save-dialog callback can interrupt the original promise chain.
  // Always release the controls when the tracked job is terminal, otherwise
  // the UI can remain stuck on "Rendering…" even though the worker stopped.
  if (activeRenderJobId) {
    const tracked = jobs.find((job) => String(job.id) === String(activeRenderJobId));
    // Keep the historical terminal set visible for compatibility: if (tracked && ["done", "partial_failed", "error", "cancelled"].includes(tracked.status))
    // pending_review is an additional successful-but-incomplete terminal state.
    if (tracked && ["done", "partial_failed", "error", "cancelled"].includes(tracked.status) || tracked && tracked.status === "pending_review") {
      const terminalMessage = renderResultSummary(tracked);
      activeRenderJobId = null;
      setRenderControlsBusy(false);
      if (!["done"].includes(tracked.status)) showToast(terminalMessage);
    }
  }
  appState.jobs = jobs;
  renderJobs(jobs);
  window.renderListening?.poll(appState.source_folder).catch(error => console.warn("[render previews]", error));
  Object.entries(realPreviewJobs).forEach(([songId, jobId]) => {
    const job = jobs.find((item) => item.id === jobId);
    if (!job) return;
    const status = document.querySelector(`[data-real-status="${songId}"]`);
    const player = document.querySelector(`[data-preview-player="${songId}"]`);
    if (job.status === "done" && job.preview_url) {
      if (player && player.src !== new URL(job.preview_url, window.location.href).href) {
        player.src = job.preview_url;
        player.load();
      }
      if (status) status.textContent = `Ready — exact ${Number(job.preview_duration || 60).toFixed(0)}s excerpt rendered in ${Number(job.preview_elapsed_seconds || 0).toFixed(1)}s.`;
      delete realPreviewJobs[songId];
    } else if (job.status === "error") {
      if (status) status.textContent = `Real preview failed: ${job.error || "see job details"}`;
      delete realPreviewJobs[songId];
    } else if (status) {
      status.textContent = `${job.current_stage || "Rendering"}: ${job.stage_detail || "working"}`;
    }
  });
  return jobs;
}

async function pollLogs() {
  const res = await fetch(`/api/logs?since=${lastLogId}`);
  const lines = await res.json();
  if (lines.length) {
    lastLogId = lines[lines.length - 1].id;
    const panel = $("#logPanel");
    panel.textContent += lines.map((x) => x.line).join("\n") + "\n";
    panel.scrollTop = panel.scrollHeight;
  }
}

async function reviewRedetectCandidate(job) {
  if (!job || redetectPromptedJobId === job.id) return;
  redetectPromptedJobId = job.id;
  const response = await fetch(`/api/redetect/candidate?job_id=${encodeURIComponent(job.id)}`);
  const candidate = await response.json().catch(() => ({}));
  if (!candidate.available) return;
  const warning = candidate.warning ? `\nWARNING: ${candidate.warning}` : "";
  const approve = await showConfirm(`Re-detect finished on the complete original session.\n\nCurrent slots: ${candidate.old_count}\nCandidate slots: ${candidate.new_count}\nOriginal stems: ${candidate.source_stem_count || "?"}\nSource duration: ${cutTime(candidate.source_duration_sec || 0)}${warning}\n\nReplace the current list only if this comparison is correct?\nCancel keeps all current and manual cuts.`);
  if (!approve) { showToast("Comparison closed; current cuts and the detection proposal are preserved."); return; }
  const endpoint = "/api/redetect/commit";
  const result = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_id: job.id }) }).then((r) => r.json().catch(() => ({})));
  if (!result.ok) return showToast(result.error || "Could not replace the current slot list.");
  await refreshState({ renderLarge: true });
  showToast(`Re-detect confirmed: ${result.count} slots loaded.`);
}

let toastTimer = null;
function showToast(text) {
  const toast = $("#toast");
  clearTimeout(toastTimer);
  toast.replaceChildren();
  const message = document.createElement("span"); message.textContent = text;
  const close = document.createElement("button"); close.type = "button"; close.textContent = "X"; close.setAttribute("aria-label", "Close message");
  close.onclick = () => toast.classList.remove("show");
  toast.append(message, close);
  toast.classList.add("show");
  toastTimer = setTimeout(() => toast.classList.remove("show"), 6000);
}

async function pollingLoop() {
  if (document.visibilityState === "visible") {
    try {
    await pollLogs();
    const jobs = appState?.jobs || [];
    const pendingRedetect = [...(jobs || [])].reverse().find((job) => job.kind === "redetect" && job.status === "pending_confirmation" && job.source_folder === appState?.source_folder);
    if (pendingRedetect) await reviewRedetectCandidate(pendingRedetect);
    const active = (jobs || []).some((job) => ["queued", "running", "stopping"].includes(job.status));
    await refreshState({ renderLarge: !active });
    setTimeout(pollingLoop, active ? 5000 : 15000);
    } catch (error) { console.warn("[state poll failed]", error); setTimeout(pollingLoop, 5000); }
  } else {
    setTimeout(pollingLoop, 6000);
  }
}

function setupLoadingMedia() {
  const video = $("#loadingStageVideo");
  const fallback = $("#loadingStageFallback");
  if (!video || !fallback) return;
  const showFallback = () => { video.hidden = true; if (!fallback.src) fallback.src = fallback.dataset.src; fallback.hidden = false; };
  const showVideo = () => { video.hidden = false; fallback.hidden = true; };
  video.addEventListener("loadeddata", showVideo);
  video.addEventListener("canplay", showVideo);
  video.addEventListener("error", showFallback);

  if (video.readyState >= 2) showVideo();
}

if (typeof document !== "undefined") {
  setupLoadingMedia();
  setupDismissControls();
  $("#selectAllSongs")?.addEventListener("click", () => {
    const available = visibleSongs(); const allSelected = available.length > 0 && available.every(song => checkedSongs.has(song.id));
    checkedSongs = allSelected ? new Set() : new Set(available.map(song => song.id));
    document.querySelectorAll(".song-card[data-song-id]").forEach(card => {
      const checkbox = card.querySelector(".song-main > input[type=checkbox]");
      if (checkbox) checkbox.checked = checkedSongs.has(Number(card.dataset.songId));
    });
    $("#selectAllSongs").textContent = allSelected ? "Select all" : "Deselect all";
    updateSelectedButton();
  });
  $("#mixSelected").addEventListener("click", () => mixSongs([...checkedSongs], true, true).catch((error) => showToast(`Render failed: ${error.message || error}`)));
  $("#mixAll").addEventListener("click", () => mixEverything());
  $("#editAllCuts").addEventListener("click", () => editAllCuts().catch((error) => { setCutLoading("Error", error.message || String(error)); showToast(error.message || String(error)); }));
  $("#secondWhisperPass").addEventListener("click", () => startOptionalWhisperAnalysis().catch((error) => { setCutLoading("Error", error.message || String(error), 100); showToast(error.message || String(error)); }));
  $("#cancelJob")?.addEventListener("click", () => { cancelActiveWork(); });
  $("#cancelLoadingWork")?.addEventListener("click", () => { cancelActiveWork(); });
  $("#audioScanMode").addEventListener("change", () => {
    saveSettings({ audio_scan_mode: $("#audioScanMode").value });
  });
  $("#knownSongCount").addEventListener("change", () => {
    saveSettings({ known_song_count: $("#knownSongCount").value.trim() });
  });
  $("#matcheringReference").addEventListener("change", () => {
    saveSettings({ matchering_reference: $("#matcheringReference").value.trim() });
  });
  $("#chooseMatcheringReference").addEventListener("click", async () => {
    const api = window.pywebview?.api;
    if (!api?.choose_reference_file) {
      showToast("Reference-file picking is available in the desktop app.");
      return;
    }
    const result = await api.choose_reference_file($("#matcheringReference").value.trim());
    if (!result || result.cancelled || !result.path) return;
    $("#matcheringReference").value = result.path;
    await saveSettings({ matchering_reference: result.path });
    showToast("Matchering reference saved. New renders will use it.");
  });
  $("#redetectSongs").addEventListener("click", async () => {
    if (!await showConfirm("Re-detect the complete original session? Current slots remain until you confirm the comparison.")) return;
    setCutLoading("Re-detecting songs", "Preparing full original session", 2);
    const response = await fetch("/api/redetect", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ allow_whisper: false }) });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      showToast(data.error || "Could not start song detection.");
      return;
    }
    const queuedJob = await response.json();
    // Replace the temporary 2% editor indicator with the real worker state.
    clearCutLoading();
    setLoadingOverlayJob(queuedJob);
    showToast("Re-detecting the complete original session.");
    await pollJobs();
    // Do not wait for the general polling loop to repaint the song list. The
    // completion path invalidates the server's old detection state, so this
    // fetch is the authoritative post-Re-detect count/boundary refresh.
    const waitForFreshDetection = async () => {
      const jobs = await pollJobs();
      const job = jobs.find((item) => item.id === queuedJob.id);
      if (job?.status === "pending_confirmation") {
        setLoadingOverlayJob(null);
        clearCutLoading();
        await reviewRedetectCandidate(job);
      } else if (job?.status === "done") {
        setLoadingOverlayJob(null);
        clearCutLoading();
        await refreshState({ renderLarge: true });
        showToast(`${appState.songs.length} songs detected from a fresh pass.`);
      } else if (job?.status === "pending_review") {
        setLoadingOverlayJob(null);
        clearCutLoading();
        await refreshState({ renderLarge: true });
        const found = Number(job.candidate_count || 0);
        const preserved = Number(job.previous_count || appState?.songs?.length || 0);
        showToast(`Re-detect incomplete: ${found} slot(s) found; current session (${preserved}) preserved.`);
      } else if (job?.status === "error" || job?.status === "cancelled") {
        setLoadingOverlayJob(null);
        clearCutLoading();
        const detail = job.error || job.warning || job.stage_detail || job.last_event || "see job details";
        showToast(`Re-detect ${job.status}: ${detail}`);
      } else {
        setTimeout(() => waitForFreshDetection().catch((err) => console.warn("[redetect refresh failed]", err)), 1000);
      }
    };
    setTimeout(() => waitForFreshDetection().catch((err) => console.warn("[redetect refresh failed]", err)), 500);
  });
  $("#changeSourceFolder").addEventListener("click", async () => {
    const api = window.pywebview?.api;
    if (!api?.choose_source_folder) {
      showToast("Source-folder picking is available in the desktop app.");
      return;
    }
    const current = appState?.source_folder || appState?.settings?.source_folder || "";
    const result = await api.choose_source_folder(current);
    if (!result || result.cancelled || !result.folder) return;
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_folder: result.folder }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      showToast(data.error || "Could not change the source folder.");
      return;
    }
    showToast("Source folder changed. Scanning audio files...");
    const detection = await fetch("/api/redetect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allow_whisper: false }),
    });
    if (!detection.ok) {
      showToast("Source folder changed, but automatic detection could not start.");
      return;
    }
    const detectionJob = await detection.json();
    setLoadingOverlayJob(detectionJob);
    await pollJobs();
    const waitForSourceDetection = async () => {
      const jobs = await pollJobs();
      const job = jobs.find((item) => item.id === detectionJob.id);
      if (job?.status === "pending_confirmation") {
        await reviewRedetectCandidate(job);
      } else if (job?.status === "done") {
        await loadState();
        showToast(`${appState.audio_scan?.accepted?.length || 0} WAV/audio files loaded.`);
      } else if (job?.status === "pending_review") {
        await loadState();
        showToast(`Folder detection incomplete: ${job.candidate_count || 0} slot(s) found; current session preserved.`);
      } else if (job?.status === "error" || job?.status === "cancelled") {
        await loadState();
        showToast(`Error loading folder: ${job.error || job.stage_detail || job.status}`);
      } else {
        setTimeout(() => waitForSourceDetection().catch((err) => showToast(`Error loading folder: ${err.message || err}`)), 1000);
      }
    };
    setTimeout(() => waitForSourceDetection().catch((err) => showToast(`Error loading folder: ${err.message || err}`)), 250);
  });

  // Start the lightweight job channel before the expensive initial state
  // request. This keeps the analysis indicator live even while /api/state is
  // busy scanning and transcribing the source folder.
  pollJobs().catch((err) => console.warn("[initial jobs poll failed]", err));
  const jobLoop = async () => {
    try { await pollJobs(); } catch (error) { console.warn("[jobs poll failed]", error); }
    setTimeout(jobLoop, document.visibilityState === "visible" ? 1500 : 6000);
  };
  setTimeout(jobLoop, 1500);
  loadState();
  setTimeout(pollingLoop, 2000);
}

if (typeof module !== "undefined") {
  module.exports = { parseSplitOffsetSeconds, parseSplitList, renderResultSummary, jobReportText, makeDialogDismissible, showConfirm, chooseMixSource, fetchStateSnapshot, setupDismissControls };
}
