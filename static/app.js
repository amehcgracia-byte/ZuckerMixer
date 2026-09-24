let appState = null;
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
const pendingOverrideSongs = new Set();
const pendingOverrideReasons = new Set();
const activeStemLoadPromises = {};
const livePreviewOverrides = {};
const previewVocalExpander = {
  thresholdDb: -45,
  ratio: 2,
  attackSeconds: 0.005,
  releaseSeconds: 0.5,
  kneeDb: 5,
  maxAttenuationDb: -12,
};

const $ = (sel) => document.querySelector(sel);

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
  const node = $("#buildInfo");
  if (node) node.textContent = `${timestamp} · ${revision}`;
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
  if (Number.isFinite(Number(ov.gain_db))) return Number(ov.gain_db);
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
      const faderDb = numeric(ov.fader_db, numeric(params.fader_db, 0));
      const muted = Boolean(ov.mute) || faderDb <= -60;
      const solo = Boolean(ov.solo);
      const fxEnabled = ov.fx_enabled === true;
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
        track_processing: fxEnabled ? (stem.role === "vocal" ? "per-stem expander: DynamicsCompressorNode" : "no per-stem compressor in preview") : "raw_dry_bypass",
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
    console.log("[mix-preview overrides ack]", { reason, response: data });
    [...pendingOverrideSongs].forEach((songId) => {
      const currentSeq = Number(appState.overrides?.songs?.[songId]?._seq || 0);
      if (currentSeq <= Number(postedSeqs[songId] || 0)) {
        pendingOverrideSongs.delete(songId);
        if (!openPreviewSongIds().has(songId)) clearLivePreviewSongOverride(songId);
      }
    });
    if (!pendingOverrideSongs.size) pendingOverrideReasons.clear();
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
  const res = await fetch("/api/state");
  appState = await res.json();
  renderBuildInfo();
  syncOverrideSequenceFromState();
  $("#songCount").textContent = `${visibleSongs().length} songs ready`;
  if (appState.ffmpeg && !appState.ffmpeg.ok) {
    showToast("ffmpeg is missing. Install it with Homebrew: brew install ffmpeg");
  }
  renderCutTools();
  renderSongs();
  renderResults();
  // /api/state may have been serialized before a newly queued detection was
  // visible. Reconcile the lightweight live job channel after the large state
  // response so an old 0% snapshot cannot overwrite current progress.
  pollJobs().catch((err) => console.warn("[state jobs refresh failed]", err));
}

function visibleSongs() {
  return appState.songs.filter((song) => !song.skipped);
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
    document.querySelector(".fine-tune[open]")
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
    knownCountNode.value = appState?.settings?.known_song_count ?? "";
  }
  const referenceNode = $("#matcheringReference");
  if (referenceNode && document.activeElement !== referenceNode) {
    referenceNode.value = appState?.settings?.matchering_reference || "";
  }
  const matchering = appState?.matchering;
  if (matchering && !matchering.available && matchering.reference) {
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
    const acceptedText = accepted.length
      ? `: ${accepted.map((item) => item.file).join(", ")}`
      : "";
    const skippedText = skipped.length
      ? ` · skipped ${skipped.length}: ${skipped.slice(0, 5).map((item) => `${item.file} (${item.reason})`).join(", ")}${skipped.length > 5 ? "…" : ""}`
      : "";
    if (scan.fragment_warning) {
      const aligned = Array.isArray(scan.aligned_files) ? scan.aligned_files : [];
      const fragments = Array.isArray(scan.fragment_files) ? scan.fragment_files : [];
      scanNode.innerHTML = `
        <strong class="scan-warning">${esc(scan.fragment_warning)}</strong>
        <span>Using ${scan.using_aligned_only ? "aligned exports only" : "all detected files"}.</span>
        <details>
          <summary>Show included/excluded files</summary>
          <div><strong>Included files (${accepted.length}; aligned exports detected: ${aligned.length})</strong><br>${accepted.map((item) => esc(item.file)).join("<br>") || "none"}</div>
          <div><strong>Excluded files (${skipped.length}; Logic fragments detected: ${fragments.length})</strong><br>${skipped.map((item) => `${esc(item.file)} (${esc(item.reason)})`).join("<br>") || "none"}</div>
        </details>
      `;
    } else {
      scanNode.textContent = `Audio scan: accepted ${accepted.length}${acceptedText}${skippedText}`;
    }
  }
  const signature = transitionsSignature(appState.transitions);
  if (signature === transitionSignature || isEditingText()) return;
  transitionSignature = signature;
  const list = $("#transitionList");
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
    card.className = `song-card ${song.skipped ? "skipped" : ""}`;
    const speed = song.bpm ? `${Math.round(song.bpm)} bpm` : "speed unknown";
    const key = song.key || "key unknown";
    const mixInfo = song.latest_render
      ? `v${song.latest_render.version}${song.latest_render.lufs ? ` · ${Number(song.latest_render.lufs).toFixed(1)} loudness` : ""}`
      : "not mixed yet";
    const mixSource = song.latest_render?.mix_source || "Automatic mix";
    const checked = checkedSongs.has(song.id) ? "checked" : "";
    const label = song.skipped ? `Skipped section ${song.id}` : `Song ${String(song.index).padStart(2, "0")}`;
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
            ${song.skipped ? `<span class="pill">${song.skip_reason || "skipped"}</span>` : ""}
          </div>
          <div class="song-sub">${esc(song.time)}</div>
          ${song.segment?.speech_text ? `<div class="song-sub whisper-log"><strong>Announcer:</strong> ${esc(song.segment.speech_text)}</div>` : ""}
          ${song.suspicious || song.number_mismatch ? `<details class="decision-evidence"><summary>Why this boundary?</summary><div>Spoken number: ${esc(song.decision_evidence?.spoken_number ?? "none")} · Introduction: ${song.decision_evidence?.introduction_found ? "found" : "not found"} · Duration rule: ${esc(song.decision_evidence?.duration_rule || "not flagged")} · Boundary: ${esc(song.decision_evidence?.boundary_source || "unknown")}</div></details>` : ""}
          <label class="title-edit">
            <span title="Rename">✎</span>
            <input data-name-input="${song.id}" value="${esc(song.custom_name || "")}" placeholder="Name this mix">
          </label>
        </div>
        <div class="row-actions">
          <label class="skip-toggle"><input type="checkbox" data-skip="${song.id}" ${song.skipped ? "checked" : ""}> Skip this one</label>
          <button data-mix-one="${song.id}" class="accent" ${song.skipped ? "disabled" : ""}>Mix this one</button>
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
    card.querySelector("[data-mix-one]").addEventListener("click", () => mixSongs([song.id]));
    card.querySelector("[data-reset-auto]").addEventListener("click", () => resetSongToAutomatic(song.id));
    card.querySelector("[data-skip]").addEventListener("change", (event) => setSkipped(song.id, event.target.checked));
    card.querySelector("[data-name-input]").addEventListener("change", (event) => saveSongName(song.id, event.target.value));
    box.appendChild(card);
    wireFineTune(card, song.id);
  });
  updateSelectedButton();
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
          <button data-preview-toggle="${song.id}" class="accent" disabled>Loading preview...</button>
          <input data-preview-seek="${song.id}" type="range" min="0" max="0" step="0.1" value="0" disabled>
          <span data-preview-time="${song.id}">00:00:00 / 00:00:00</span>
          <span class="muted" data-preview-cache="${song.id}">Preview stems not loaded</span>
          <span class="warn" data-preview-warning="${song.id}" hidden></span>
          </div>
          <div data-real-controls="${song.id}" hidden>
            <div class="muted" data-real-status="${song.id}">Press Preview render to create an exact centered 30-second excerpt.</div>
            <audio data-preview-player="${song.id}" controls preload="none"></audio>
          </div>
        </div>
      </div>
      <div class="fine-body">
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
      loadFullStemPreview(root, songIndex);
    } else {
      await stopPreviewMix(songIndex);
    }
  });
  if (details.open) {
    loadActiveStemFaders(root, songIndex).then(() => loadFullStemPreview(root, songIndex));
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
  faders.innerHTML = '<div class="muted">Loading active tracks...</div>';
  try {
    const res = await fetch(`/api/active-stems/${songIndex}`);
    if (!res.ok) throw new Error("active stems unavailable");
    const data = await res.json();
    const song = appState.songs.find((item) => Number(item.id) === Number(songIndex));
    if (song) {
      song.active_stems = data.active_stems || [];
      song.mix_params = data.mix_params || null;
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
      ov.gain_db ??= Number(params.gain_db || 0);
      ov.fader_db ??= Number(params.fader_db || 0);
      ov.pan ??= Number(params.pan || 0);
      ov.eq_low_cut_hz ??= Number(params.eq_low_cut_hz);
      ov.eq_mid_gain_db ??= Number(params.eq_mid_gain_db);
      ov.eq_air_gain_db ??= Number(params.eq_air_gain_db);
    }
    const gainDb = stemGainDb(songIndex, stem);
    const fader = Number(ov.fader_db || 0);
    const automaticFaderDb = Number(params?.automatic_fader_db ?? params?.computed_gain_db ?? 0);
    if (!Number.isFinite(Number(ov.pan))) ov.pan = defaultPan(stem);
    const pan = Number(ov.pan);
    const fxEnabled = ov.fx_enabled === true;
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
    const slider = strip.querySelector("[data-fader]");
    const label = strip.querySelector(".amount");
    const gainSlider = strip.querySelector("[data-gain]");
    const gainLabel = strip.querySelector(".gain-control span");
    gainSlider.addEventListener("input", () => {
      const liveGainDb = Number(gainSlider.value);
      ov.gain_db = liveGainDb;
      setLinkedOverride(songIndex, linked, "gain_db", liveGainDb);
      gainLabel.textContent = signedDb(liveGainDb);
      linked.forEach((item) => applyLivePreGain(songIndex, item, liveGainDb, "gain:direct-input"));
      persistPreviewChange(songIndex, `gain:${stem.file}`);
    });
    gainSlider.addEventListener("change", () => flushOverrideSave(songIndex, `gain:${stem.file}`));
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
      flushOverrideSave(songIndex, `fader:${stem.file}`);
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
      panSlider.addEventListener("change", () => flushOverrideSave(songIndex, `pan:${stem.file}`));
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
      eqSlider.addEventListener("change", () => flushOverrideSave(songIndex, `${key}:${stem.file}`));
    });
    strip.querySelector("[data-mute]").addEventListener("click", async (event) => {
      ov.mute = !ov.mute;
      setLinkedOverride(songIndex, linked, "mute", ov.mute);
      event.currentTarget.classList.toggle("active", ov.mute);
      linked.forEach((item) => updatePreviewGains(songIndex, item.file, "mute"));
      await flushOverrideSave(songIndex, `mute:${stem.file}`);
    });
    strip.querySelector("[data-solo]").addEventListener("click", async (event) => {
      ov.solo = !ov.solo;
      setLinkedOverride(songIndex, linked, "solo", ov.solo);
      event.currentTarget.classList.toggle("active", ov.solo);
      linked.forEach((item) => updatePreviewGains(songIndex, item.file, "solo"));
      await flushOverrideSave(songIndex, `solo:${stem.file}`);
    });
    strip.querySelector("[data-fx-toggle]").addEventListener("click", async (event) => {
      ov.fx_enabled = !(ov.fx_enabled === true);
      setLinkedOverride(songIndex, linked, "fx_enabled", ov.fx_enabled);
      event.currentTarget.classList.toggle("active", ov.fx_enabled);
      event.currentTarget.textContent = ov.fx_enabled ? "FX ON" : "FX OFF";
      linked.forEach((item) => reconnectPreviewStemFx(previewMixFor(songIndex), songIndex, item, ov.fx_enabled));
      persistPreviewChange(songIndex, `fx:${stem.file}`);
      await flushOverrideSave(songIndex, `fx:${stem.file}`);
      logPreviewGraphIntegrity(songIndex, "fx-toggle");
    });
    strip.querySelectorAll("[data-effect]").forEach((button) => {
      button.addEventListener("click", async () => {
        const key = button.dataset.effect;
        ov[key] = !(ov[key] === true);
        setLinkedOverride(songIndex, linked, key, ov[key]);
        button.classList.toggle("active", ov[key]);
        linked.forEach((item) => {
          const live = livePreviewStemOverrides(songIndex, item.file);
          live[key] = ov[key];
        });
        if (key === "space_enabled" || key === "echo_enabled") updatePreviewSends(songIndex, key);
        if (key === "gate_enabled") linked.forEach((item) => reconnectPreviewStemFx(previewMixFor(songIndex), songIndex, item, previewFxEnabled(songIndex, item)));
        persistPreviewChange(songIndex, `${key}:${stem.file}`);
        await flushOverrideSave(songIndex, `${key}:${stem.file}`);
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
    input.addEventListener("change", () => flushOverrideSave(songIndex, key));
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
    input.addEventListener("change", () => flushOverrideSave(songIndex, key));
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
      await flushOverrideSave(songIndex, "mastering_intensity");
    });
  }

  root.querySelector("[data-mix-settings]").addEventListener("click", () => mixSongs([songIndex], false, false));
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
  mix.gains = {};
  mix.panners = {};
  mix.reverbSends = {};
  mix.delaySends = {};
  mix.eqNodes = {};
  mix.buffers = [];
}

function previewFxEnabled(songIndex, stem) {
  return currentStemOverrides(songIndex, stem.file).fx_enabled === true;
}

function previewEffectEnabled(songIndex, stem, key) {
  return currentStemOverrides(songIndex, stem.file)[key] === true;
}

function reconnectPreviewStemFx(mix, songIndex, stem, enabled) {
  const preGain = mix?.preGains?.[stem.file];
  const fader = faderGainNode(mix, stem.file);
  const eqNodes = Object.values(mix?.eqNodes?.[stem.file] || {}).filter((node) => node && typeof node.connect === "function");
  const expander = mix?.vocalExpanders?.[stem.file];
  if (!preGain || !fader) return;
  try { preGain.disconnect(); } catch (_err) {}
  eqNodes.forEach((node) => { try { node.disconnect(); } catch (_err) {} });
  if (expander) { try { expander.disconnect(); } catch (_err) {} }
  const gateEnabled = enabled && previewEffectEnabled(songIndex, stem, "gate_enabled");
  const chain = enabled ? [...eqNodes, ...(expander && gateEnabled ? [expander] : []), fader] : [fader];
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
      if (stem.role === "vocal" && previewFxEnabled(songIndex, stem) && previewEffectEnabled(songIndex, stem, "gate_enabled") && fader && fader.__receivesFromNode !== mix.vocalExpanders?.[stem.file]) errors.push("vocal fader is not fed by expander");
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
  return dbToGain(base + Number(ov[key] || 0));
}

function updatePreviewSends(songIndex, key) {
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

// The Python master measures integrated loudness after summing and then gains
// the result to TARGET_LUFS. The browser cannot use pyloudnorm, so estimate
// the same summed RMS from the decoded buffers after the exact per-stem gains;
// for music this tracks LUFS closely enough to keep preview/export within the
// product's 1 LU parity tolerance. The output limiter handles inter-sample
// peaks introduced by the browser graph.
function estimatePreviewRmsDb(mix) {
  if (!mix?.loaded || !mix.buffers?.length) return NaN;
  let sumSquares = 0;
  let sampleCount = 0;
  mix.buffers.forEach(({ stem, buffer }) => {
    const pre = Number(mix.preGains?.[stem.file]?.gain?.value ?? 1);
    const fader = Number(mix.faderGains?.[stem.file]?.gain?.value ?? 1);
    const gain = pre * fader;
    const stride = Math.max(1, Math.floor(buffer.length / 120000));
    for (let channel = 0; channel < buffer.numberOfChannels; channel += 1) {
      const data = buffer.getChannelData(channel);
      for (let i = 0; i < data.length; i += stride) {
        const value = data[i] * gain;
        sumSquares += value * value;
        sampleCount += 1;
      }
    }
  });
  if (!sampleCount || !sumSquares) return -120;
  return 10 * Math.log10(sumSquares / sampleCount);
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
  if (stem.role !== "vocal" || !ctx.createDynamicsCompressor) return null;
  const node = ctx.createDynamicsCompressor();
  node.threshold.value = previewVocalExpander.thresholdDb;
  node.knee.value = previewVocalExpander.kneeDb;
  node.ratio.value = previewVocalExpander.ratio;
  node.attack.value = previewVocalExpander.attackSeconds;
  node.release.value = previewVocalExpander.releaseSeconds;
  node.__stemFile = stem.file;
  node.__inSignalPath = true;
  node.__processing = {
    type: "per-stem expander: DynamicsCompressorNode",
    threshold_db: previewVocalExpander.thresholdDb,
    ratio: previewVocalExpander.ratio,
    attack_seconds: previewVocalExpander.attackSeconds,
    release_seconds: previewVocalExpander.releaseSeconds,
    knee_db: previewVocalExpander.kneeDb,
    python_max_attenuation_db: previewVocalExpander.maxAttenuationDb,
  };
  return node;
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
    if (status && existingMix.loading) status.textContent = "Loading tracks...";
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
    decoded = await Promise.all(previewStems.map(async (stem) => {
      const res = await fetch(`/stem-full/${songIndex}/${stem.index}`);
      if (!res.ok || res.status === 204) return null;
      const bytes = Number(res.headers.get("X-Preview-Cache-Bytes") || 0);
      const arrayBuffer = await res.arrayBuffer();
      const buffer = await ctx.decodeAudioData(arrayBuffer);
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
    playButton.textContent = mix.buffers.length ? "Mix Preview" : "Preview unavailable";
  }
  return mix;
}

function startPreviewSources(mix, songIndex, offset) {
  stopPreviewSources(mix);
  const when = mix.ctx.currentTime + 0.03;
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
  if (play) play.textContent = mix.playing ? "Pause Preview" : "Mix Preview";
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

async function refreshState(options = {}) {
  const renderLarge = options.renderLarge !== false;
  const res = await fetch("/api/state");
  const next = await res.json();
  const protectedOverrideSongs = new Set([...pendingOverrideSongs, ...openPreviewSongIds(), ...Object.keys(livePreviewOverrides)]);
  if (appState?.overrides && (protectedOverrideSongs.size || overrideSaveTimer || overrideWritesInFlight)) {
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
  appState = next;
  renderBuildInfo();
  syncOverrideSequenceFromState();
  checkedSongs = new Set([...checkedSongs].filter((id) => appState.songs.some((song) => song.id === id && !song.skipped)));
  $("#songCount").textContent = `${visibleSongs().length} songs ready`;
  if (renderLarge) {
    renderCutTools();
    renderSongs();
  }
  renderResults();
  // The state payload is intentionally large and can lag the worker. Always
  // finish with the authoritative, lightweight job snapshot.
  pollJobs().catch((err) => console.warn("[state jobs refresh failed]", err));
}

function updateSelectedButton() {
  $("#mixSelected").textContent = `Mix selected (${checkedSongs.size})`;
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

async function mixSongs(songs, useBatchMaster = true, isBatchAction = songs.length > 1) {
  if (!songs.length) {
    showToast("Choose at least one song.");
    return;
  }
  const useSavedMixes = isBatchAction ? await chooseMixSource() : true;
  if (useSavedMixes == null) return;
  if (useBatchMaster) {
    const batchMaster = $("#batchMaster")?.value || "natural";
    songs.forEach((songId) => {
      songOverrides(songId).mastering_intensity = batchMaster;
      songOverrides(songId).target_lufs = batchMaster === "loud" ? -9.5 : -14;
    });
  }
  const singleSong = songs.length === 1 ? songs[0] : null;
  if (singleSong != null) {
    try {
      await ensureMixParamsForSong(singleSong);
    } catch (_err) {
      showToast("Could not load mix parameters for this song.");
      return;
    }
  }
  const previewEffectiveMix = singleSong != null ? effectiveMixDump(singleSong, "before-render-click") : null;
  const renderTargetDir = await chooseRenderFolder();
  if (renderTargetDir === false) return;
  console.info("RENDER DESTINATION request_target", renderTargetDir);
  await saveOverrides({ songIndexes: songs, reason: "before-render" });
  const url = singleSong != null ? `/api/render/${singleSong}` : "/api/render";
  const body = singleSong != null
    ? { render_target_dir: renderTargetDir || undefined, render_destination_trace: { save_dialog_return: renderTargetDir }, use_saved_mixes: useSavedMixes, preview_effective_mix: previewEffectiveMix }
    : { songs, render_target_dir: renderTargetDir || undefined, render_destination_trace: { save_dialog_return: renderTargetDir }, use_saved_mixes: useSavedMixes };
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    showToast(data.error || "Choose at least one song.");
    return;
  }
  await pollJobs();
}

function mixEverything() {
  mixSongs(visibleSongs().map((song) => song.id), true, true);
}

function chooseMixSource() {
  const dialog = $("#mixChoiceDialog");
  if (!dialog) return Promise.resolve(false);
  return new Promise((resolve) => {
    const finish = (value) => {
      dialog.close();
      resolve(value);
    };
    $("#useAutomaticMixes").onclick = () => finish(false);
    $("#useSavedMixes").onclick = () => finish(true);
    dialog.addEventListener("cancel", () => resolve(null), { once: true });
    dialog.showModal();
    $("#useAutomaticMixes").focus();
  });
}

async function resetSongToAutomatic(songId) {
  if (!confirm(`Clear saved overrides for Song ${songId} and use the current automatic mix?`)) return;
  const response = await fetch(`/api/reset-automatic/${songId}`, { method: "POST" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) return showToast(data.error || "Could not reset this song.");
  await refreshState({ renderLarge: true });
  showToast(`Song ${songId} reset to automatic mix.`);
}

function renderJobs(items) {
  const box = $("#jobs");
  box.innerHTML = "";
  // Prefer the job that is actually executing. A queued redetect can be newer
  // than a running one (for example after a double click); showing its 0%
  // status made the live analysis bar appear frozen even while the worker
  // was reporting real progress.
  const active = [...items].reverse().find((job) => ["running", "stopping"].includes(job.status))
    || [...items].reverse().find((job) => job.status === "queued");
  const redetectButton = $("#redetectSongs");
  if (redetectButton) {
    redetectButton.disabled = items.some((job) => job.kind === "redetect" && ["queued", "running", "stopping"].includes(job.status));
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
    loading.classList.toggle("stalled", Date.now() - Number(detectionJob.progress_updated_at || detectionJob.heartbeat || Date.now() / 1000) * 1000 > 30000);
  } else if (loading) {
    loading.hidden = true;
  }
  $(".progress-box").classList.toggle("working", Boolean(active));
  if (active) {
    const current = active.current_item || (active.current ? `Song ${String(active.current).padStart(2, "0")}` : "Waiting");
    const stage = active.current_stage ? ` · ${active.current_stage}` : "";
    $("#currentWork").textContent = active.status === "stopping" ? "Stopping after this song" : `${current}${stage}`;
    const total = active.total_count || active.songs.length;
    const position = active.current ? (active.done_count || 0) + 1 : (active.done_count || 0);
    const elapsed = shortElapsed(active.started);
    const detail = active.stage_detail || "working";
    $("#queuePosition").textContent = `${detail} · ${position} of ${total} · ${active.song_progress || 0}% · ${elapsed || "0:00"} elapsed · ${formatRemaining(active.eta_seconds)}`;
    $("#progressFill").style.width = `${active.song_progress || 0}%`;
    const messages = {
      scanning: ["Checking the room mics...", "Reading the session clock..."],
      "analyzing stems": ["Listening to the drummer...", "Counting the groove..."],
      "detecting songs": ["Finding where the MC talks...", "Looking for the next song..."],
      mixing: ["Balancing the band...", "Giving every stem its place..."],
      mastering: ["Making it loud enough for the bar...", "Polishing the final bounce..."],
      encoding: ["Packing the mix for listening...", "Putting the finishing label on it..."],
    };
    const fun = $("#progressFun");
    const choices = messages[String(active.current_stage || "").toLowerCase()] || ["Keeping the session moving..."];
    if (fun) fun.textContent = choices[Math.floor(Date.now() / 5000) % choices.length];
    const stall = $("#progressStall");
    const updated = Number(active.progress_updated_at || active.heartbeat || Date.now() / 1000) * 1000;
    const stale = Date.now() - updated > 30000;
    if (stall) {
      stall.hidden = !stale;
      stall.textContent = stale ? "This phase is still active — reading or processing a large file can keep the percentage steady for a while." : "";
    }
  } else {
    $("#currentWork").textContent = "Nothing mixing right now";
    $("#queuePosition").textContent = "Ready";
    $("#progressFill").style.width = "0%";
    $("#progressFun").textContent = "";
    $("#progressStall").hidden = true;
  }
  items.slice(-5).reverse().forEach((job) => {
    const el = document.createElement("div");
    el.className = `job ${job.status === "error" ? "error" : ""}`;
    const errorText = job.error ? `<pre class="copyable-error">${esc(job.error)}</pre><button class="copy-error" data-copy-error="${esc(job.id)}">Copy error</button>` : "";
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
    if (copyButton) copyButton.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(String(job.error || ""));
        copyButton.textContent = "Copied";
      } catch (_err) {
        showToast("Could not copy the error text.");
      }
    });
    if (job.status === "error") showToast(`Song ${String(job.current || "").padStart(2, "0")} failed — see details`);
  });
}

function friendlyStatus(job) {
  if (job.status === "queued") return "Waiting";
  if (job.status === "running") return "Mixing";
  if (job.status === "stopping") return "Stopping";
  if (job.status === "cancelled") return "Cancelled";
  if (job.status === "error") return "Needs attention";
  return "Finished";
}

async function pollJobs() {
  const res = await fetch("/api/jobs");
  const jobs = await res.json();
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
  // The live job poll starts before the initial state request. Do not let a
  // fast /api/jobs response dereference the not-yet-hydrated state object.
  if (!appState) return jobs;
  appState.jobs = jobs;
  renderJobs(jobs);
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

function showToast(text) {
  const toast = $("#toast");
  toast.textContent = text;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 6000);
}

async function pollingLoop() {
  if (document.visibilityState === "visible") {
    await pollLogs();
    const jobs = await pollJobs();
    const active = (jobs || []).some((job) => ["queued", "running", "stopping"].includes(job.status));
    await refreshState({ renderLarge: !active });
    setTimeout(pollingLoop, active ? 3000 : 5000);
  } else {
    setTimeout(pollingLoop, 6000);
  }
}

if (typeof document !== "undefined") {
  $("#mixSelected").addEventListener("click", () => mixSongs([...checkedSongs], true, true));
  $("#mixAll").addEventListener("click", mixEverything);
  $("#cancelJob").addEventListener("click", () => {
    if (!window.confirm("Are you sure you want to cancel?")) return;
    fetch("/api/cancel", { method: "POST" }).then(pollJobs);
  });
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
    const response = await fetch("/api/redetect", { method: "POST" });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      showToast(data.error || "Could not start song detection.");
      return;
    }
    showToast("Looking for songs again.");
    await pollJobs();
    // Do not wait for the general polling loop to repaint the song list. The
    // completion path invalidates the server's old detection state, so this
    // fetch is the authoritative post-Re-detect count/boundary refresh.
    const waitForFreshDetection = async () => {
      const jobs = await pollJobs();
      const job = [...jobs].reverse().find((item) => item.kind === "redetect");
      if (job?.status === "done") {
        await refreshState({ renderLarge: true });
        showToast(`${appState.songs.length} songs detected from a fresh pass.`);
      } else if (job?.status === "error" || job?.status === "cancelled") {
        showToast(`Re-detect ${job.status}.`);
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
    const detection = await fetch("/api/redetect", { method: "POST" });
    if (!detection.ok) {
      showToast("Source folder changed, but automatic detection could not start.");
      return;
    }
    await pollJobs();
  });

  // Start the lightweight job channel before the expensive initial state
  // request. This keeps the analysis indicator live even while /api/state is
  // busy scanning and transcribing the source folder.
  pollJobs().catch((err) => console.warn("[initial jobs poll failed]", err));
  setInterval(() => pollJobs().catch((err) => console.warn("[jobs poll failed]", err)), 1000);
  loadState();
  setTimeout(pollingLoop, 2000);
}

if (typeof module !== "undefined") {
  module.exports = { parseSplitOffsetSeconds, parseSplitList };
}
