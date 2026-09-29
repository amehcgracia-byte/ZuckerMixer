#!/usr/bin/env python3
"""
Automated multitrack jam-session detection, mixing, mastering, and MP3 export.

Default behavior is intentionally review-first:
    python3 jam_mix_pipeline.py

That inspects stems, detects likely songs, prints the segment list, and stops.
Run the full render only after review:
    python3 jam_mix_pipeline.py --mix
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import hashlib
import io
import queue
import threading
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from scipy import ndimage, signal
try:
    import matchering as matchering_api
except Exception as _matchering_import_error:
    matchering_api = None
    MATCHERING_IMPORT_ERROR = f"{type(_matchering_import_error).__name__}: {_matchering_import_error}"
else:
    MATCHERING_IMPORT_ERROR = ""


# ------------------------- configurable defaults -------------------------

SOURCE_DIR = Path.home() / "Music" / "JamStems"
OUTPUT_ROOT = Path.home() / "Music" / "JamMixes"
SESSION_DATE = date.today().isoformat()

SILENCE_GAP_SECONDS = 8.0
DEFAULT_SILENCE_GAP_SECONDS = SILENCE_GAP_SECONDS
MIN_SONG_SECONDS = 90.0
DETECTION_FRAME_SECONDS = 1.0
DETECTION_SMOOTH_SECONDS = 9.0
SILENCE_THRESHOLD_DB = -25.0
DEFAULT_SILENCE_THRESHOLD_DB = SILENCE_THRESHOLD_DB
MIN_ACTIVE_STEM_SECONDS = 10.0
SEGMENT_PRE_PAD_SECONDS = 3.0
SEGMENT_POST_PAD_SECONDS = 4.0
RENDER_FADE_IN_SECONDS = 0.5
RENDER_FADE_OUT_SECONDS = 8.0
EDGE_EXTENSION_LOOK_SECONDS = 30.0
EDGE_EXTENSION_MIN_ACTIVITY_SECONDS = 3.0
POST_SONG_MIC_LOOK_SECONDS = 20.0
POST_SONG_MIC_MIN_ACTIVITY_SECONDS = 2.0
MC_BREAK_MIN_SECONDS = 4.0
MC_BREAK_SMOOTH_SECONDS = 3.0
MC_NEXT_FADE_SECONDS = 8.0
MC_VOICE_ACTIVE_WITHIN_DB = 20.0
MC_INSTRUMENT_QUIET_BELOW_DB = 30.0
MC_VOICE_DOMINANCE_DB = 10.0
MC_ACTIVE_LEVEL_FLOOR_DBFS = -60.0
MC_ALLOWED_LOUD_INSTRUMENTS = 2
MC_SCAN_MERGE_GAP_SECONDS = 45.0
MC_FOLLOW_ACTIVITY_SECONDS = 60.0
MC_STRUCTURAL_MIN_SONG_SECONDS = 6 * 60.0
MC_STRUCTURAL_MAX_SONG_SECONDS = 16 * 60.0
MC_STRUCTURAL_CLOSE_SONG_SECONDS = 4 * 60.0
MC_DISCARDED_HIGH_SCORE_COUNT = 20
MC_CLUSTER_REWARD_SECONDS = 25.0
MC_SHORT_ISOLATED_SECONDS = 12.0
MC_MIN_FINAL_SONG_SECONDS = 4 * 60.0
MC_SANITY_ANCHOR_TOLERANCE_SECONDS = 90.0
MC_EAR_CONFIRMED_SPLITS = []
MC_SANITY_ANCHORS = []
SUSPICIOUS_SHORT_SONG_SECONDS = 5 * 60.0
SUSPICIOUS_LONG_SONG_SECONDS = 20 * 60.0
# Permissive defaults. Each source can define its own review policy.
HARD_MIN_SONG_SECONDS = 90.0
HARD_MAX_SONG_SECONDS = 4 * 3600.0
EXPECTED_SONG_COUNT = None
EXPECTED_SLOT_COUNT = None
RHYTHM_ANALYSIS_MAX_SECONDS = 300.0
RHYTHM_ANALYSIS_SR = 11025
RHYTHM_ONSET_TOLERANCE_BEATS = 0.16
RHYTHM_INCONSISTENT_ATTENUATION_DB = -3.0
RHYTHM_SPARSE_ATTENUATION_DB = -2.0
VOCAL_PRIORITY_MARGIN_DB = 2.0
VOICE_WIND_EXPLICIT_TERMS = ("flute", "trumpet", "trombone", "sax", "horn", "brass", "woodwind")
SYNTH_BELOW_MELODIC_MARGIN_DB = 3.0
NOISE_ANALYSIS_MAX_SECONDS = 300.0
NOISE_BROADBAND_FLATNESS_THRESHOLD = 0.38
NOISE_SUSTAINED_FRACTION_THRESHOLD = 0.70
NOISE_EMPTY_RMS_THRESHOLD_DBFS = -43.0
NOISE_FLOOR_MIN_DBFS = -72.0
NOISE_REDUCTION_STRENGTH = 0.85
NOISE_REDUCTION_FLOOR = 0.22
HUM_PEAK_RATIO_THRESHOLD = 7.0
HUM_MIN_RMS_DBFS = -68.0
HUM_FUNDAMENTALS_HZ = (50.0, 60.0)

TARGET_TRACK_RMS_DBFS = -18.0
STEM_INACTIVE_FLOOR_DBFS = -55.0
STEM_RELATIVE_INACTIVE_DB = 25.0
STEM_DYNAMIC_ACTIVE_SPREAD_DB = 15.0
# Some instruments, especially bass, can sit well below the loudest drum or
# vocal stem while still carrying a sustained musical part.  Keep this
# role-aware instead of making one session's loudest stem the gate for every
# other stem.
STEM_ACTIVITY_ROLE_OVERRIDES = {
    "bass": {"floor_dbfs": -62.0, "relative_db": 32.0, "spread_db": 6.0},
    "guitar": {"floor_dbfs": -60.0, "relative_db": 29.0, "spread_db": 8.0},
    "keys": {"floor_dbfs": -60.0, "relative_db": 29.0, "spread_db": 8.0},
    "keys_l": {"floor_dbfs": -60.0, "relative_db": 29.0, "spread_db": 8.0},
    "keys_r": {"floor_dbfs": -60.0, "relative_db": 29.0, "spread_db": 8.0},
}
MAX_TRACK_MAKEUP_GAIN_DB = 30.0
MAX_DRUM_MAKEUP_GAIN_DB = 36.0
# Automatic mixing establishes relative balance only. It may attenuate a
# stem, but it must never boost a source without an explicit user value.
AUTO_MIX_MAX_BOOST_DB = 0.0
# Auto-Mix may make a small, explicit correction only for roles where a quiet
# capture would otherwise disappear. All other roles can only be attenuated
# automatically. These limits are per song, never session-global.
AUTO_MIX_ROLE_BOOST_LIMITS_DB = {"vocal": 3.0, "bass": 3.0}
# Deliberate first-pass guitar trim: guitars were repeatedly masking vocals
# in the user's real sessions. This is applied before per-song caps and is
# included in the analysis signature so old plans cannot survive unnoticed.
AUTO_MIX_ROLE_TRIMS_DB = {"guitar": -3.0}
AUTO_MIX_PROFILE_VERSION = 2
AUTO_MIX_MAX_ATTENUATION_DB = -12.0
# Vocal-role stems include the session's mic channels.  The channel may carry
# speech, singing, flute, or another acoustic source, so this is intentionally
# a capture-role lift rather than a vocal-content detector.
# Vocal stems are already normalized against the same track target as the
# rhythm section.  A further automatic lift stacked on top of that target,
# and was especially harmful when two vocal stems were summed at the bus.
# Keep the setting explicit for reports/compatibility, but do not add hidden
# vocal gain upstream of the bus.
AUTOMATIC_VOCAL_MIC_LIFT_DB = 0.0
VOCAL_BUS_TRIM_DB = 0.0
INSTRUMENT_SECTION_GATE_DROP_DB = 20.0
INSTRUMENT_SECTION_GATE_MIN_SECONDS = 5.0
INSTRUMENT_SECTION_GATE_FADE_SECONDS = 2.0
MIC_GATE_THRESHOLD_DBFS = -45.0
MIC_EXPANDER_THRESHOLD_DBFS = -45.0
MIC_EXPANDER_RATIO = 2.0
MIC_EXPANDER_ATTACK_MS = 5.0
MIC_EXPANDER_RELEASE_MS = 500.0
MIC_EXPANDER_HYSTERESIS_DB = 5.0
MIC_EXPANDER_MAX_ATTENUATION_DB = -12.0
TARGET_LUFS = -14.0
MASTERING_INTENSITY = "natural"
MASTERING_TARGETS = {"natural": -14.0, "loud": -9.5}
TRUE_PEAK_CEILING_DBFS = -1.0
PREMASTER_HEADROOM_DB = -6.0
MATCHERING_REFERENCE: Path | None = None
EXPORT_SAMPLE_RATE = 44100
MP3_BITRATE = "320k"
FFMPEG_PATH: str | None = None
FFMPEG_CANDIDATES = (
    "/usr/local/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
    "/usr/bin/ffmpeg",
)
TRANSCODE_CACHE_ROOT: Path | None = None
ACCEPTED_AUDIO_EXTENSIONS = {".wav", ".aif", ".aiff", ".flac", ".mp3", ".m4a", ".aac", ".ogg"}
DIRECT_SOUNDFILE_EXTENSIONS = {".wav", ".aif", ".aiff", ".flac", ".ogg"}
AUDIO_SCAN_REPORT: dict[str, object] = {"accepted": [], "skipped": [], "source": ""}
AUDIO_SCAN_MODE = "auto"
LOGIC_FRAGMENT_RE = re.compile(r"^.+#\d{2,}$", re.IGNORECASE)

# Five-minute blocks keep the same streaming DSP state while avoiding the
# per-block overhead that makes long full-session verification renders
# unnecessarily slow.  Peak/RMS safety is still measured and applied per
# block, so this does not change the clipping contract.
RENDER_CHUNK_SECONDS = 300.0
DETECTION_CACHE = Path(".jam_detection_envelopes.npz")
DETECTION_CACHE_ROOT: Path | None = None
LEGACY_DETECTION_CACHE: Path | None = None
DETECTION_CACHE_ALGORITHM_VERSION = "20260926-global-boundary-timeline-v5"
SPEECH_DETECTION_ALGORITHM_VERSION = "20260827-whisper-v1"
SPEECH_TRANSCRIPTION_CACHE_VERSION = "20260926-commentator-slots-v3"
WHISPER_MODEL_SIZE = os.environ.get("ZUCKER_WHISPER_MODEL", "tiny")
WHISPER_TIMEOUT_SECONDS = float(os.environ.get("ZUCKER_WHISPER_IDLE_TIMEOUT_SECONDS", os.environ.get("ZUCKER_WHISPER_TIMEOUT_SECONDS", "300")))
# Model construction may include a first-run model download or CPU
# initialization. Do not count the process-import time against this watchdog.
WHISPER_MODEL_START_TIMEOUT_SECONDS = float(os.environ.get("ZUCKER_WHISPER_MODEL_START_TIMEOUT_SECONDS", "180"))
WHISPER_ENV = Path(__file__).resolve().parent / ".whisperenv"
LAST_SPEECH_TRANSCRIPTIONS: list[dict[str, object]] = []
LAST_WHISPER_STATUS: dict[str, object] = {"status": "not_started"}
WHISPER_ALLOWED = True
KNOWN_SONG_COUNT: int | None = None
DETECTION_RESCAN_MODE = False
DETECTION_PROFILE_VERSION = "20260927-source-scoped-profile-v1"
DETECTION_PROFILE_SIGNATURE = "default"
SESSION_DETECTION_PROFILE: dict[str, object] = {}


def whisper_runtime_paths() -> tuple[Path, Path]:
    """Resolve Whisper from the frozen app first, then from a source checkout."""
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    if getattr(sys, "frozen", False):
        # The frozen executable dispatches --whisper-worker itself. The script
        # path is still returned so the detector can verify the bundled data.
        return Path(sys.executable), bundle_root / "whisper_transcribe.py"
    roots = [Path(__file__).resolve().parent, Path.cwd(), Path.home() / "ZuckerMixer"]
    for root in roots:
        python = root / ".whisperenv" / "bin" / "python"
        worker = root / "whisper_transcribe.py"
        if python.exists() and worker.exists():
            return python, worker
    return WHISPER_ENV / "bin" / "python", Path(__file__).resolve().parent / "whisper_transcribe.py"
STRICT_TIMELINE_EXPORT = True
NO_BEXT_OFFSETS = False
MIX_OVERRIDES: dict[str, object] = {}
MIX_OVERRIDE_VERIFY_TRACE: dict[str, object] = {}
FADER_HARD_SILENCE_DB = -60.0
PROGRESS_HOOK = None


def detection_cache_path(source_dir: Path | None = None) -> Path:
    """Return the cache path reserved for one source folder."""
    source = (source_dir or SOURCE_DIR).expanduser().resolve()
    root = DETECTION_CACHE_ROOT or DETECTION_CACHE.parent
    digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:16]
    return root / f"jam_detection_envelopes_{digest}.npz"


def configure_detection_cache(source_dir: Path | None = None, cache_root: Path | None = None) -> Path:
    """Point the detector at the source-specific cache and return its path."""
    global DETECTION_CACHE, DETECTION_CACHE_ROOT
    if cache_root is not None:
        DETECTION_CACHE_ROOT = Path(cache_root).expanduser().resolve()
    DETECTION_CACHE = detection_cache_path(source_dir)
    return DETECTION_CACHE


def _profile_number(profile: dict[str, object], keys: tuple[str, ...], default: float | None) -> float | None:
    for key in keys:
        value = profile.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


def _profile_time(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if ":" not in text:
            return float(text)
        parts = [float(part) for part in text.split(":")]
        if len(parts) == 2:
            return parts[0] * 60.0 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600.0 + parts[1] * 60.0 + parts[2]
    except (TypeError, ValueError):
        return None
    return None


def _profile_markers(values: object, prefix: str) -> list[tuple[str, float]]:
    if not isinstance(values, list):
        return []
    markers: list[tuple[str, float]] = []
    for index, item in enumerate(values, 1):
        label = f"{prefix} {index}"
        raw_time = item
        if isinstance(item, dict):
            label = str(item.get("label") or label)
            raw_time = item.get("seconds", item.get("time", item.get("timestamp")))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            label = str(item[0] or label)
            raw_time = item[1]
        seconds = _profile_time(raw_time)
        if seconds is not None and seconds >= 0:
            markers.append((label, seconds))
    return markers


def configure_detection_profile(profile: dict[str, object] | None = None) -> dict[str, object]:
    """Apply one source's detection policy without leaking another source's calibration."""
    global SESSION_DETECTION_PROFILE, DETECTION_PROFILE_SIGNATURE
    global WHISPER_MODEL_SIZE, WHISPER_TIMEOUT_SECONDS, KNOWN_SONG_COUNT
    global EXPECTED_SONG_COUNT, EXPECTED_SLOT_COUNT, MIN_SONG_SECONDS
    global HARD_MIN_SONG_SECONDS, HARD_MAX_SONG_SECONDS
    global MC_STRUCTURAL_MIN_SONG_SECONDS, MC_STRUCTURAL_MAX_SONG_SECONDS
    global MC_STRUCTURAL_CLOSE_SONG_SECONDS, MC_MIN_FINAL_SONG_SECONDS
    global SUSPICIOUS_SHORT_SONG_SECONDS, SUSPICIOUS_LONG_SONG_SECONDS
    global MC_EAR_CONFIRMED_SPLITS, MC_SANITY_ANCHORS
    raw = dict(profile or {})
    SESSION_DETECTION_PROFILE = raw
    expected_raw = raw.get("expected_song_count", raw.get("known_song_count"))
    try:
        expected = int(expected_raw) if expected_raw not in (None, "") else None
    except (TypeError, ValueError):
        expected = None
    if expected is not None and expected < 1:
        expected = None
    KNOWN_SONG_COUNT = expected
    EXPECTED_SONG_COUNT = expected
    EXPECTED_SLOT_COUNT = expected

    minimum = _profile_number(raw, ("min_song_seconds", "hard_min_song_seconds"), 90.0)
    maximum = _profile_number(raw, ("max_song_seconds", "hard_max_song_seconds"), 4 * 3600.0)
    structural_min = _profile_number(raw, ("structural_min_song_seconds",), minimum)
    structural_max = _profile_number(raw, ("structural_max_song_seconds",), maximum)
    close_min = _profile_number(raw, ("structural_close_song_seconds",), minimum)
    final_min = _profile_number(raw, ("min_final_song_seconds",), minimum)
    suspicious_short = _profile_number(raw, ("suspicious_short_song_seconds",), 5 * 60.0)
    suspicious_long = _profile_number(raw, ("suspicious_long_song_seconds",), 20 * 60.0)
    MIN_SONG_SECONDS = max(1.0, float(minimum or 90.0))
    HARD_MIN_SONG_SECONDS = max(1.0, float(minimum or 90.0))
    HARD_MAX_SONG_SECONDS = max(HARD_MIN_SONG_SECONDS, float(maximum or 4 * 3600.0))
    MC_STRUCTURAL_MIN_SONG_SECONDS = max(1.0, float(structural_min or HARD_MIN_SONG_SECONDS))
    MC_STRUCTURAL_MAX_SONG_SECONDS = max(MC_STRUCTURAL_MIN_SONG_SECONDS, float(structural_max or HARD_MAX_SONG_SECONDS))
    MC_STRUCTURAL_CLOSE_SONG_SECONDS = max(1.0, float(close_min or MC_STRUCTURAL_MIN_SONG_SECONDS))
    MC_MIN_FINAL_SONG_SECONDS = max(1.0, float(final_min or MC_STRUCTURAL_MIN_SONG_SECONDS))
    SUSPICIOUS_SHORT_SONG_SECONDS = max(0.0, float(suspicious_short or 0.0))
    SUSPICIOUS_LONG_SONG_SECONDS = max(SUSPICIOUS_SHORT_SONG_SECONDS, float(suspicious_long or HARD_MAX_SONG_SECONDS))

    model = str(raw.get("whisper_model") or os.environ.get("ZUCKER_WHISPER_MODEL", "tiny")).strip()
    WHISPER_MODEL_SIZE = model or "tiny"
    timeout = _profile_number(raw, ("whisper_idle_timeout_seconds", "whisper_timeout_seconds"), float(os.environ.get("ZUCKER_WHISPER_IDLE_TIMEOUT_SECONDS", os.environ.get("ZUCKER_WHISPER_TIMEOUT_SECONDS", "300"))))
    # This is an inactivity watchdog, not a total Whisper runtime cap. Old
    # profiles that stored the former 90-second total timeout are migrated to
    # a safe watchdog automatically.
    WHISPER_TIMEOUT_SECONDS = max(180.0, float(timeout or 300.0))
    MC_EAR_CONFIRMED_SPLITS = _profile_markers(raw.get("ear_confirmed_splits", []), "ear")
    MC_SANITY_ANCHORS = _profile_markers(raw.get("sanity_anchors", []), "anchor")
    DETECTION_PROFILE_SIGNATURE = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    return dict(raw)


def _analysis_cache_encode(value: object, arrays: dict[str, np.ndarray]) -> object:
    if isinstance(value, np.ndarray):
        key = f"array_{len(arrays):04d}"
        arrays[key] = np.asarray(value)
        return {"__ndarray__": key}
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(key): _analysis_cache_encode(item, arrays) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_analysis_cache_encode(item, arrays) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _analysis_cache_restore(value: object, arrays: dict[str, np.ndarray]) -> object:
    if isinstance(value, dict) and set(value) == {"__ndarray__"}:
        return np.asarray(arrays[str(value["__ndarray__"])])
    if isinstance(value, dict):
        return {str(key): _analysis_cache_restore(item, arrays) for key, item in value.items()}
    if isinstance(value, list):
        return [_analysis_cache_restore(item, arrays) for item in value]
    return value


def save_analysis_cache(path: Path, payload: dict[str, object]) -> None:
    """Persist Auto-Mix analysis without executable pickle payloads."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays: dict[str, np.ndarray] = {}
    metadata = _analysis_cache_encode(payload, arrays)
    tmp_path = path.with_name(f".{path.name}.tmp.npz")
    with tmp_path.open("wb") as handle:
        np.savez_compressed(handle, __meta__=np.array(json.dumps(metadata, sort_keys=True, default=str), dtype=str), **arrays)
        handle.flush()
        os.fsync(handle.fileno())
    tmp_path.replace(path)


def load_analysis_cache(path: Path) -> dict[str, object]:
    """Load a non-executable Auto-Mix analysis cache."""
    path = Path(path)
    if path.suffix.lower() == ".pkl":
        raise RuntimeError("Legacy pickle analysis cache rejected; run Analyze again to create a safe cache.")
    with np.load(path, allow_pickle=False) as data:
        metadata = json.loads(str(data["__meta__"]))
        arrays = {name: np.asarray(data[name]) for name in data.files if name != "__meta__"}
    restored = _analysis_cache_restore(metadata, arrays)
    if not isinstance(restored, dict):
        raise RuntimeError("Analysis cache metadata is not an object.")
    return restored


def report_progress(payload: dict[str, object]) -> None:
    if PROGRESS_HOOK is None:
        return
    try:
        PROGRESS_HOOK(payload)
    except Exception:
        pass


@dataclass(frozen=True)
class Stem:
    path: Path
    name: str
    role: str
    samplerate: int
    channels: int
    frames: int
    duration: float
    timeline_frames: int
    offset_seconds: float
    offset_source: str

    @property
    def timeline_duration(self) -> float:
        return self.timeline_frames / self.samplerate


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    core_start: float | None = None
    core_end: float | None = None
    nominal_end: float | None = None
    mc_start: float | None = None
    mc_end: float | None = None
    next_mc_start: float | None = None
    next_mc_end: float | None = None
    post_mic_start: float | None = None
    post_mic_end: float | None = None
    boundary_source: str = "drum-silence"
    trimmed_start_seconds: float = 0.0
    trimmed_end_seconds: float = 0.0
    boundary_validation: str = ""
    boundary_validation_reason: str = ""
    speech_text: str = ""
    speech_reason: str = ""
    speech_intro_text: str = ""
    speech_intro_start: float | None = None
    speech_confidence: float = 0.0
    spoken_song_number: int | None = None
    spoken_last: bool = False
    musician_labels: tuple[tuple[str, str], ...] = ()
    assigned_song_number: int | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class McCandidate:
    start: float
    end: float
    mic_active_seconds: float
    median_mic_relative: float
    median_drum_relative: float
    median_instrument_relative: float
    follow_activity_seconds: float
    follow_start_delay: float | None
    score: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def db_to_amp(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def amp_to_db(x: float) -> float:
    return float(20.0 * math.log10(max(float(x), 1e-12)))


def fmt_time(seconds: float) -> str:
    seconds = max(0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"


def sanitize_filename(text: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:150]


def resolve_ffmpeg() -> str | None:
    global FFMPEG_PATH
    if FFMPEG_PATH and Path(FFMPEG_PATH).exists():
        return FFMPEG_PATH
    path = shutil.which("ffmpeg")
    if path:
        FFMPEG_PATH = path
        return FFMPEG_PATH
    for candidate in FFMPEG_CANDIDATES:
        if Path(candidate).exists():
            FFMPEG_PATH = candidate
            return FFMPEG_PATH
    return None


ROLE_SYNONYMS: dict[str, tuple[str, ...]] = {
    # Keep this table intentionally human-editable: technicians' labels vary
    # from session to session and matching is case-insensitive/substring based.
    "kick": ("kick", "kickdrum", "bd", "bdjam", "bombo", "bass drum", "low drum"),
    "snare": ("snare", "snr", "hh", "hihat", "hi-hat", "hi hat", "high hat", "cymbal"),
    "drums": ("drum", "drums", "kit", "overhead", "overheads", "ov", "ovjam", "beatbox"),
    "bass": ("bass", "bassgtr", "bass gtr", "low end", "electric bass"),
    "keys": ("keys", "keybd", "keyboard", "piano", "organ", "epiano", "rhodes", "synth keys"),
    "guitar": ("guit", "gtr", "guitar", "acoustic guitar", "electric guitar"),
    "vocal": ("vocal", "vocals", "vox", "voice", "mic", "microphone", "singer", "talkback", "announcer", "box"),
    "sax": ("sax", "saxophone", "alto sax", "tenor sax", "baritone sax"),
    "horn": ("horn", "horns", "brass", "trumpet", "trump", "trombone", "bone", "woodwind"),
    "synth": ("synth", "synthesizer", " moog", "pad", "lead synth"),
    "room": ("room", "ambience", "ambient", "amb", "far mic", "crowd", "audience"),
}

# Rhythm can be recorded on a non-kit track. Session labels are more reliable
# than the role classifier for beatbox/drum-machine/percussion tracks.
RHYTHM_SOURCE_TERMS = (
    "beatbox", "beat", "drum machine", "drummachine", "rhythm",
    "percussion", "groove box", "groovebox", "rhythm box", "drum pad",
    "808", "909",
)


def is_rhythm_source_stem(stem: Stem) -> bool:
    label = str(stem.path.name or "").lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", label)
    compact = normalized.replace(" ", "")
    return any(term in normalized or term.replace(" ", "") in compact for term in RHYTHM_SOURCE_TERMS)


def is_generic_stem_name(stem: Stem) -> bool:
    """Treat unlabeled recorder channels as rhythm candidates, conservatively."""
    label = Path(stem.path.name).stem.lower()
    label = re.sub(r"[^a-z0-9]+", " ", label).strip()
    if re.fullmatch(r"(?:file|track|audio|untitled|stem|channel|ch)(?:\s+\d+)?", label):
        return True
    return bool(re.fullmatch(r"(?:\d+|(?:file|track|audio|untitled|stem|channel|ch)\s*\d+)", label))


def drum_equivalent_stems(stems: list[Stem]) -> list[Stem]:
    """Return kit drums plus named and generic alternate rhythm sources."""
    return [
        stem for stem in stems
        if stem.role in {"kick", "snare", "drums"}
        or is_rhythm_source_stem(stem)
        or is_generic_stem_name(stem)
    ]


def _has_role_synonym(label: str, synonyms: tuple[str, ...]) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
    compact = normalized.replace(" ", "")
    return any(
        synonym.strip() in normalized or synonym.strip().replace(" ", "") in compact
        for synonym in synonyms
    )


def classify_role(name: str) -> str:
    low = str(name or "").lower()
    # Beatbox is a percussion/room label, not a vocal label; resolve it before
    # the intentionally broad "box" vocal synonym.
    if _has_role_synonym(low, ROLE_SYNONYMS["drums"]):
        return "drums"
    if _has_role_synonym(low, ROLE_SYNONYMS["kick"]):
        return "kick"
    if _has_role_synonym(low, ROLE_SYNONYMS["snare"]):
        return "snare"
    if _has_role_synonym(low, ROLE_SYNONYMS["bass"]):
        return "bass"
    if "keys l" in low or "keysl" in low or "keyboard l" in low:
        return "keys_l"
    if "keys r" in low or "keysr" in low or "keyboard r" in low:
        return "keys_r"
    if _has_role_synonym(low, ROLE_SYNONYMS["keys"]):
        return "keys"
    if _has_role_synonym(low, ROLE_SYNONYMS["guitar"]):
        return "guitar"
    if _has_role_synonym(low, ROLE_SYNONYMS["sax"]):
        return "sax"
    if _has_role_synonym(low, ROLE_SYNONYMS["horn"]):
        return "horn"
    if _has_role_synonym(low, ROLE_SYNONYMS["synth"]):
        return "synth"
    if _has_role_synonym(low, ROLE_SYNONYMS["vocal"]):
        return "vocal"
    if _has_role_synonym(low, ROLE_SYNONYMS["room"]):
        return "room"
    return "other"


def _transcode_cache_path(path: Path) -> Path:
    cache_root = TRANSCODE_CACHE_ROOT or (Path(tempfile.gettempdir()) / "zucker-mixer-audio-cache")
    identity = f"{path.resolve()}:{path.stat().st_size}:{path.stat().st_mtime_ns}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:24]
    return cache_root / digest / path.name


def _audio_for_processing(path: Path) -> Path:
    suffix = path.suffix.lower()
    if suffix in DIRECT_SOUNDFILE_EXTENSIONS:
        try:
            sf.info(str(path))
            return path
        except Exception:
            pass
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    cached = _transcode_cache_path(path)
    cached.parent.mkdir(parents=True, exist_ok=True)
    if not cached.exists() or cached.stat().st_size == 0:
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_name(f".{cached.name}.tmp")
        subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-acodec", "pcm_s16le", str(tmp)],
            check=True,
        )
        tmp.replace(cached)
    sf.info(str(cached))
    return cached


def scan_audio_files(source_dir: Path) -> tuple[list[Path], dict[str, object]]:
    accepted: list[Path] = []
    skipped: list[dict[str, str]] = []
    if not source_dir.exists() or not source_dir.is_dir():
        report = {
            "source": str(source_dir),
            "accepted": [],
            "skipped": [{"file": str(source_dir), "reason": "source folder not found"}],
            "status": "Error loading folder",
            "error": "source folder not found",
            "mode": AUDIO_SCAN_MODE,
            "fragment_warning": "",
            "fragment_files": [],
            "aligned_files": [],
            "using_aligned_only": False,
        }
        return [], report
    # Selected jams may contain subfolders.  Keep every file in the scan and
    # match extensions case-insensitively; the source folder is the user's
    # authority, not the layout used by the previous jam.
    candidates = sorted(
        (Path(root) / name)
        for root, _dirs, names in os.walk(source_dir, onerror=lambda _error: None)
        for name in names
    )

    def display_name(path: Path) -> str:
        try:
            return str(path.relative_to(source_dir))
        except ValueError:
            return path.name
    scan_total_bytes = sum(max(0, path.stat().st_size) for path in candidates)
    scan_bytes_done = 0
    scan_started = time.perf_counter()
    report_progress({"current_stage": "scanning folder", "stage_detail": f"Scanning source folder — 0 of {len(candidates)} files", "progress": 1, "song_progress": 1, "bytes_read": 0, "bytes_total": scan_total_bytes, "heartbeat": time.time()})
    valid: list[tuple[Path, Path, object]] = []
    fragment_paths = [
        path
        for path in candidates
        if path.suffix.lower() in {".aif", ".aiff", ".wav"} and LOGIC_FRAGMENT_RE.fullmatch(path.stem)
    ]
    for file_index, path in enumerate(candidates, 1):
        suffix = path.suffix.lower()
        if suffix not in ACCEPTED_AUDIO_EXTENSIONS:
            skipped.append({"file": display_name(path), "reason": "unsupported format"})
            scan_bytes_done += max(0, path.stat().st_size)
            ratio = scan_bytes_done / max(1, scan_total_bytes)
            elapsed = max(0.001, time.perf_counter() - scan_started)
            report_progress({"current_stage": "scanning folder", "stage_detail": f"Scanned file {file_index} of {len(candidates)}: {path.name} (unsupported format)", "progress": 1 + int(ratio * 14), "song_progress": 1 + int(ratio * 14), "elapsed_seconds": elapsed, "eta_seconds": elapsed * (1.0 - ratio) / ratio if ratio > 0 else None, "bytes_read": scan_bytes_done, "bytes_total": scan_total_bytes, "heartbeat": time.time()})
            continue
        try:
            prepared = _audio_for_processing(path)
            info = sf.info(str(prepared))
            if info.frames <= 0 or info.samplerate <= 0:
                raise ValueError("empty or invalid audio")
            valid.append((path, prepared, info))
        except Exception as exc:
            skipped.append({"file": display_name(path), "reason": f"unreadable/corrupt: {exc}"})
        scan_bytes_done += max(0, path.stat().st_size)
        ratio = scan_bytes_done / max(1, scan_total_bytes)
        elapsed = max(0.001, time.perf_counter() - scan_started)
        report_progress({"current_stage": "scanning folder", "stage_detail": f"Scanning source folder — file {file_index} of {len(candidates)}: {path.name} ({scan_bytes_done / 1048576:.0f}/{scan_total_bytes / 1048576:.0f} MB)", "progress": 1 + int(ratio * 14), "song_progress": 1 + int(ratio * 14), "elapsed_seconds": elapsed, "eta_seconds": elapsed * (1.0 - ratio) / ratio if ratio > 0 else None, "bytes_read": scan_bytes_done, "bytes_total": scan_total_bytes, "heartbeat": time.time()})

    valid_by_path = {path: (prepared, info) for path, prepared, info in valid}
    aligned_named = [
        path
        for path, _prepared, _info in valid
        if "jam.tracks" in path.name.lower()
    ]
    aligned_named_durations = [
        float(valid_by_path[path][1].frames / valid_by_path[path][1].samplerate)
        for path in aligned_named
    ]
    aligned_reference = float(np.median(aligned_named_durations)) if aligned_named_durations else 0.0
    aligned_tolerance = max(2.0, aligned_reference * 0.01)
    aligned_paths = [
        path
        for path in aligned_named
        if abs(float(valid_by_path[path][1].frames / valid_by_path[path][1].samplerate) - aligned_reference) <= aligned_tolerance
    ]
    if len(aligned_paths) < 2:
        longest_duration = max(
            (float(info.frames / info.samplerate) for _path, _prepared, info in valid),
            default=0.0,
        )
        aligned_tolerance = max(2.0, longest_duration * 0.01)
        aligned_paths = [
            path
            for path, _prepared, info in valid
            if float(info.frames / info.samplerate) >= longest_duration * 0.8
            and abs(float(info.frames / info.samplerate) - longest_duration) <= aligned_tolerance
        ]
    fragment_durations = [
        float(valid_by_path[path][1].frames / valid_by_path[path][1].samplerate)
        for path in fragment_paths
        if path in valid_by_path
    ]
    aligned_duration = max(
        (float(valid_by_path[path][1].frames / valid_by_path[path][1].samplerate) for path in aligned_paths),
        default=0.0,
    )
    fragments_confident = bool(
        len(fragment_paths) >= 3
        and len(aligned_paths) >= 2
        and fragment_durations
        and max(fragment_durations) < aligned_duration * 0.8
    )
    scan_mode = AUDIO_SCAN_MODE if AUDIO_SCAN_MODE in {"auto", "aligned_only", "all"} else "auto"
    use_aligned_only = scan_mode == "aligned_only" or (scan_mode == "auto" and fragments_confident)
    # A decodable source is evidence that the track exists. Aligned/fragments
    # are still reported for diagnostics, but never silently removed from the
    # song render because of RMS, duration, or the aligned-only heuristic.
    selected_paths = {path for path, _prepared, _info in valid}
    use_aligned_only = False
    included_warnings = []
    if fragments_confident:
        included_warnings.extend(
            {
                "file": path.name,
                "reason": "available decodable source included; timeline alignment requires review",
            }
            for path in fragment_paths
            if path in valid_by_path
        )
    elif scan_mode == "aligned_only":
        included_warnings.extend(
            {
                "file": path.name,
                "reason": "available decodable source included despite aligned-only preference",
            }
            for path, _prepared, _info in valid
            if path not in aligned_paths
        )
    for path, prepared, _info in valid:
        if path in selected_paths:
            accepted.append(prepared)
    report = {
        "source": str(source_dir),
        "accepted": [{"file": display_name(path), "path": str(path)} for path, _prepared, _info in valid],
        "skipped": skipped,
        "included_warnings": included_warnings,
        "mode": scan_mode,
        "fragment_warning": (
            f"This folder contains {len(fragment_paths)} per-take fragment files from Logic's Media folder "
            f"alongside {len(aligned_paths)} aligned exports. Fragments have no timeline position and will corrupt detection."
            if fragments_confident
            else ""
        ),
        "fragment_files": [path.name for path in fragment_paths],
        "aligned_files": [path.name for path in aligned_paths],
        "using_aligned_only": use_aligned_only,
        "status": f"WAV files found: {sum(1 for path, _prepared, _info in valid if path.suffix.lower() == '.wav')} (accepted audio: {len(valid)})",
        "error": "",
    }
    return accepted, report


def list_audio_files(source_dir: Path) -> list[Path]:
    global AUDIO_SCAN_REPORT
    paths, AUDIO_SCAN_REPORT = scan_audio_files(source_dir)
    AUDIO_SCAN_REPORT["status"] = f"WAV files found: {sum(1 for item in AUDIO_SCAN_REPORT.get('accepted', []) if str(item.get('file', '')).lower().endswith('.wav'))}"
    return paths


def audio_scan_report() -> dict[str, object]:
    return AUDIO_SCAN_REPORT


def read_bext_time_reference(path: Path, sample_rate: int) -> tuple[float | None, str]:
    try:
        with path.open("rb") as f:
            header = f.read(12)
            if len(header) < 12:
                return None, "NO_HEADER"
            if header[:4] not in {b"RIFF", b"RF64"} or header[8:12] != b"WAVE":
                return None, "NO_BEXT_NON_WAVE"
            while True:
                chunk = f.read(8)
                if len(chunk) < 8:
                    return None, "NO_BEXT"
                chunk_id = chunk[:4]
                chunk_size = int.from_bytes(chunk[4:8], "little", signed=False)
                if chunk_id == b"bext":
                    data = f.read(min(chunk_size, 346))
                    if len(data) < 346:
                        return None, "BEXT_TOO_SHORT"
                    low = int.from_bytes(data[338:342], "little", signed=False)
                    high = int.from_bytes(data[342:346], "little", signed=False)
                    samples = (high << 32) | low
                    return samples / sample_rate, f"bext:{samples} samples"
                f.seek(chunk_size + (chunk_size % 2), os.SEEK_CUR)
    except OSError as exc:
        return None, f"READ_ERROR:{exc}"


def inspect_stems(source_dir: Path) -> list[Stem]:
    stems: list[Stem] = []
    excluded: list[Stem] = []
    missing_bext: list[tuple[str, str]] = []
    paths = list_audio_files(source_dir)
    total_bytes = sum(max(0, path.stat().st_size) for path in paths)
    bytes_done = 0
    scan_started = time.perf_counter()
    report_progress({"current_stage": "scanning folder", "stage_detail": f"Scanning source folder — 0 of {len(paths)} files", "progress": 8, "song_progress": 8, "phase_index": 1, "phase_total": 7, "stem_count": len(paths), "elapsed_seconds": 0.0, "eta_seconds": None, "bytes_read": 0, "bytes_total": total_bytes, "heartbeat": time.time()})
    for file_index, path in enumerate(paths, 1):
        try:
            info = sf.info(str(path))
        except Exception as exc:
            print(f"WARNING: skipping unreadable audio file {path.name}: {type(exc).__name__}: {exc}", flush=True)
            bytes_done += max(0, path.stat().st_size)
            continue
        duration = info.frames / info.samplerate
        if NO_BEXT_OFFSETS:
            offset = 0.0
            offset_source = "ignored (--no-bext-offsets)"
        else:
            offset, offset_source = read_bext_time_reference(path, info.samplerate)
            if offset is None:
                missing_bext.append((path.name, offset_source))
                offset = 0.0
        stem = Stem(
            path=path,
            name=path.stem,
            role=classify_role(path.stem),
            samplerate=info.samplerate,
            channels=info.channels,
            frames=info.frames,
            duration=duration,
            timeline_frames=info.frames,
            offset_seconds=offset,
            offset_source=offset_source,
        )
        # A decodable WAV is a real stem even when it is short or mostly
        # silent.  Keep it visible to the application; low activity is a
        # detection warning, never a silent load exclusion.
        stems.append(stem)
        if duration < MIN_SONG_SECONDS:
            excluded.append(stem)
        bytes_done += max(0, path.stat().st_size)
        elapsed = max(0.001, time.perf_counter() - scan_started)
        ratio = bytes_done / max(1, total_bytes)
        report_progress({"current_stage": "scanning folder", "stage_detail": f"Scanning source folder — file {file_index} of {len(paths)}: {path.name} ({bytes_done / 1048576:.0f}/{total_bytes / 1048576:.0f} MB)", "progress": 1 + int(ratio * 14), "song_progress": 1 + int(ratio * 14), "elapsed_seconds": elapsed, "eta_seconds": elapsed * (1.0 - ratio) / ratio if ratio > 0 else None, "bytes_read": bytes_done, "bytes_total": total_bytes, "heartbeat": time.time()})

    print("\nINSPECT")
    print(f"Source: {source_dir}")
    scan = audio_scan_report()
    if scan.get("fragment_warning"):
        print(f"WARNING: {scan['fragment_warning']}")
        print("  Included aligned exports:")
        for name in scan.get("aligned_files", []):
            print(f"    included: {name}")
        print("  Excluded Logic fragments:")
        for name in scan.get("fragment_files", []):
            print(f"    excluded: {name}")
    print(f"Audio files: {len(stems)}")
    if excluded:
        print(f"Short audio files retained for review: {len(excluded)}")
        for stem in excluded:
            print(f"  retained: {stem.path.name:18s} dur={fmt_time(stem.duration)}")
    print(f"Usable stems: {len(stems)}")
    for stem in stems:
        print(
            f"  {stem.path.name:18s} sr={stem.samplerate:<6d} "
            f"ch={stem.channels:<2d} dur={fmt_time(stem.duration)} "
            f"offset={fmt_time(stem.offset_seconds)} role={stem.role}"
        )

    print("\nSTEM TIMELINE OFFSETS")
    print("  filename             offset       source")
    for stem in stems:
            print(f"  {stem.path.name:20s} {fmt_time(stem.offset_seconds):>11s}  {stem.offset_source}")
    if missing_bext:
        print("\nWARNING: files without BWF/bext time_reference were assigned offset 00:00:00.00.")
        print("Verify these offsets before mixing; AIFF files usually do not contain a bext chunk.")
        for name, reason in missing_bext[:30]:
            print(f"  no bext: {name} ({reason})")
        if len(missing_bext) > 30:
            print(f"  ... {len(missing_bext) - 30} more")

    sample_rates = sorted({s.samplerate for s in stems})
    channels = sorted({s.channels for s in stems})
    durations = np.array([s.duration for s in stems], dtype=np.float64)
    if len(sample_rates) != 1:
        print(f"WARNING: sample-rate mismatch: {sample_rates}")
    if len(channels) != 1:
        print(f"Channel counts present: {channels} (expected for mixed mono/stereo exports)")
    if len(durations):
        longest = float(np.max(durations))
        short = [s.path.name for s in stems if longest - s.duration > 2.0]
        if short:
            print(
                "WARNING: not all stems have similar length. "
                f"Longest={fmt_time(longest)}; shorter stems={len(short)}"
            )
            for name in short[:20]:
                print(f"  shorter: {name}")
            if len(short) > 20:
                print(f"  ... {len(short) - 20} more")
    if STRICT_TIMELINE_EXPORT:
        stems = normalize_timeline_stems(stems)
        print("\nEFFECTIVE ALIGNMENT OFFSETS")
        print("  filename             offset       note")
        for stem in stems:
            print(f"  {stem.path.name:20s} {fmt_time(stem.offset_seconds):>11s}  aligned export")
    AUDIO_SCAN_REPORT["status"] = f"Loaded {len(stems)} WAV files"
    return stems


def normalize_timeline_stems(stems: list[Stem]) -> list[Stem]:
    if not stems:
        return stems
    sample_rates = sorted({s.samplerate for s in stems})
    if len(sample_rates) != 1:
        detail = ", ".join(f"{s.path.name}={s.samplerate}" for s in stems)
        raise SystemExit(f"ERROR: timeline export sample-rate mismatch. {detail}")
    frames = {s.frames for s in stems}
    max_frames = max(frames)
    normalized = [
        replace(
            stem,
            timeline_frames=max_frames,
            offset_seconds=0.0,
            offset_source=(
                stem.offset_source
                if stem.offset_seconds == 0.0 and stem.offset_source.startswith("ignored")
                else f"{stem.offset_source}; ignored for aligned timeline export"
            ),
        )
        for stem in stems
    ]
    offsets = {round(s.offset_seconds, 9) for s in stems}
    if NO_BEXT_OFFSETS:
        print("Timeline export check: ignoring all bext offsets by --no-bext-offsets; song times are relative to export start.")
    elif len(offsets) == 1:
        common = next(iter(offsets))
        if common:
            print(
                "Timeline export check: common bext offset "
                f"{fmt_time(common)} ignored; song times are relative to export start."
            )
        else:
            print("Timeline export check: offsets are zero; song times are relative to export start.")
    else:
        print(
            "Timeline export check: mixed/missing bext offsets ignored because this is an aligned timeline export; "
            "song times are relative to export start."
        )

    if len(frames) == 1:
        print("Timeline export check: all usable stems have identical duration.")
        return normalized

    longest = max(stems, key=lambda s: s.frames)
    print("\nTimeline export check: duration mismatches treated as tail truncation.")
    print(f"  Session length: {longest.path.name} {fmt_time(longest.duration)} ({max_frames} frames)")
    for stem in sorted(stems, key=lambda s: (s.frames, s.path.name)):
        pad_frames = max_frames - stem.frames
        if pad_frames:
            print(
                f"  virtual tail pad: {stem.path.name:24s} "
                f"+{fmt_time(pad_frames / stem.samplerate)} ({pad_frames} frames)"
            )
    by_name = {stem.path.name: stem for stem in normalized}
    return [by_name[stem.path.name] for stem in stems]


def mono_rms_by_second(stem: Stem, frame_seconds: float) -> np.ndarray:
    hop = max(1, int(round(stem.samplerate * frame_seconds)))
    read_frames = hop * 60
    rms: list[float] = []
    with sf.SoundFile(str(stem.path), "r") as f:
        while True:
            block = f.read(read_frames, dtype="float32", always_2d=True)
            if len(block) == 0:
                break
            mono = np.mean(block, axis=1)
            full = (len(mono) // hop) * hop
            if full:
                framed = mono[:full].reshape(-1, hop)
                chunk_rms = np.sqrt(np.mean(framed * framed, axis=1) + 1e-12)
                rms.extend(float(v) for v in chunk_rms)
            if full < len(mono):
                tail = mono[full:]
                rms.append(float(np.sqrt(np.mean(tail * tail) + 1e-12)))
    return np.asarray(rms, dtype=np.float64)


NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "twenty-one": 21, "twenty-two": 22, "twenty-three": 23,
    "twenty-four": 24, "twenty-five": 25, "twenty-six": 26, "twenty-seven": 27,
    "twenty-eight": 28, "twenty-nine": 29, "thirty": 30,
}
MUSICIAN_ROLE_TERMS = {
    "bass": "bass", "base": "bass", "drum": "drums", "drums": "drums", "percussion": "percussion",
    "beatbox": "beatbox", "piano": "keys", "keyboard": "keys", "keys": "keys",
    "guitar": "guitar", "guitars": "guitar", "sax": "sax", "saxophone": "sax",
    "trumpet": "horn", "trombone": "horn", "horn": "horn", "brass": "horn",
    "vocal": "vocals", "vocals": "vocals", "voice": "vocals", "singing": "vocals", "flute": "flute",
}
REJECTED_TRANSCRIBED_NAMES = {"me", "myself", "hands", "more", "know", "you", "youre", "you're", "the", "someone"}


def spoken_song_number(text: str) -> int | None:
    low = text.lower().replace("–", "-")
    # The opening announcement is often phrased as “the first slot” rather
    # than “song number one”. It is still the moderator's formal SONG 1
    # anchor, and recognizing it is what lets leading house-band content be
    # assigned SONG 0.
    if re.search(r"\b(?:start|starting|open(?:ing)?)\b[^.]{0,80}\bfirst\s+(?:slot|song|round)\b", low):
        return 1
    match = re.search(r"(?:song|track|number|set|n[uú]mero|no\.?)[\s-]*(?:number|no\.?)?[\s-]*(\d{1,2})\b", low)
    if match:
        return int(match.group(1))
    match = re.search(r"\bnumber\s+(?:the\s+)?(\d{1,2})\b", low)
    if match:
        return int(match.group(1))
    for word, value in sorted(NUMBER_WORDS.items(), key=lambda item: -len(item[0])):
        if re.search(rf"\b(?:song|track|number|set|n[uú]mero)\s+(?:number|no\.?)?\s*{re.escape(word)}\b", low):
            return value
        if re.search(rf"\bnumber\s+(?:the\s+)?{re.escape(word)}\b", low):
            return value
    # Announcers often omit the noun after an introductory phrase: “we have
    # the 14” / “now, the fifteenth”. Keep these constrained to presentation
    # phrasing so ordinary lyric numbers do not become song identities.
    match = re.search(r"\b(?:we have|now we have|go on|going on)\s+(?:the\s+)?(\d{1,2})\b", low)
    if match:
        return int(match.group(1))
    ordinal_words = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
                     "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
                     "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
                     "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
                     "twentieth": 20}
    for word, value in ordinal_words.items():
        if re.search(rf"\b(?:song|track|number|set|n[uú]mero)\s+(?:the\s+)?{word}\b", low):
            return value
    return None


def spoken_last_song(text: str) -> bool:
    return bool(re.search(r"\b(?:the\s+)?(?:very\s+)?last\s+(?:one|song|track)\b", text.lower()))


def extract_musician_labels(text: str) -> tuple[tuple[str, str], ...]:
    found: dict[str, str] = {}
    role_pattern = "|".join(sorted((re.escape(term) for term in MUSICIAN_ROLE_TERMS), key=len, reverse=True))
    patterns = (
        rf"\b([A-Z][A-Za-z'-]+)[,\s]+(?:on|in|playing|plays)(?:\s+the)?\s+({role_pattern})\b",
        rf"\b({role_pattern})\s*[:=-]\s*([A-Z][A-Za-z'-]+)\b",
    )
    for index, pattern in enumerate(patterns):
        for match in re.finditer(pattern, text, re.IGNORECASE):
            name, role_term = (match.group(1), match.group(2)) if index == 0 else (match.group(2), match.group(1))
            role = MUSICIAN_ROLE_TERMS.get(role_term.lower())
            if role and len(name) > 1 and name.strip().lower() not in REJECTED_TRANSCRIBED_NAMES:
                found[role] = name.strip().title()
    return tuple(sorted(found.items()))


def _speech_is_announcement(text: str, speech_confidence: float, avg_logprob: float) -> bool:
    low = text.lower()
    if len(re.findall(r"[A-Za-zÀ-ÿ']+", text)) < 2 or speech_confidence < 0.15:
        return False
    number = spoken_song_number(text) is not None
    role_words = ("bass", "drum", "percussion", "piano", "keyboard", "keys", "guitar", "sax", "flute", "trumpet", "horn", "vocal", "voice", "singer")
    has_roles = any(re.search(rf"\b{re.escape(term)}\b", low) for term in role_words)
    presentation = any(phrase in low for phrase in ("next group", "next round", "we have", "let's", "let us", "track number", "on the", "instrumental slot", "instrumental jam", "so we have", "free for all", "anyone wants to join", "jump in"))
    technical = any(term in low for term in ("sound technician", "technician", "fixing gear", "fix the", "plugging", "setup", "set up", "muted", "feedback", "problem with the", "check the mic"))
    has_content = number or has_roles or presentation
    word_count = len(re.findall(r"[A-Za-zÀ-ÿ']+", text))
    # A trailing fragment such as “and we have a flute” is continuation
    # chatter from the preceding announcement, not a new song introduction.
    # It must not steal the next song's opening boundary.
    if re.match(r"^\s*because\s+the\s+guitar\s+player\b", low) and not number and not extract_musician_labels(text):
        return False
    if re.match(r"^\s*and\s+we\s+have\b", low) and word_count <= 7 and not number and not extract_musician_labels(text):
        return False
    # Candidate windows already require a mic-active/instrument-quiet region.
    # Accept a coherent longer utterance even when tiny Whisper mangles a
    # name or omits the role keyword; short lyric/bleed fragments remain out.
    return bool(avg_logprob > -2.5 and has_content and not (technical and not (number or has_roles or presentation)))


def announcement_reason(text: str, speech_confidence: float, avg_logprob: float) -> str:
    low = text.lower()
    if len(re.findall(r"[A-Za-zÀ-ÿ']+", text)) < 2:
        return "rejected: too few words"
    if speech_confidence < 0.15 or avg_logprob <= -2.5:
        return "rejected: low speech confidence"
    if any(term in low for term in ("sound technician", "technician", "fixing gear", "plugging", "setup", "feedback", "problem with the", "check the mic")) and spoken_song_number(text) is None and not extract_musician_labels(text):
        return "rejected: technical/gear chatter without song introduction"
    if spoken_song_number(text) is not None:
        return "accepted: spoken song number"
    if extract_musician_labels(text):
        return "accepted: musician/instrument assignments"
    if any(phrase in low for phrase in ("next group", "next round", "we have", "track number", "instrumental slot", "let's enjoy")):
        return "accepted: presentation phrasing"
    return "rejected: no introduction content"


def announcement_start(item: dict[str, object]) -> float:
    """Find the first word of the next-song introduction inside a candidate."""
    base = float(item.get("start", 0.0))
    pieces = item.get("segments") or []
    for piece in pieces if isinstance(pieces, list) else []:
        if not isinstance(piece, dict):
            continue
        text = str(piece.get("text") or "")
        if classify_speech_piece(text) != "next-song introduction":
            continue
        words = piece.get("words") or []
        for word in words if isinstance(words, list) else []:
            word_text = str(word.get("word") or "") if isinstance(word, dict) else ""
            probe = text[text.lower().find(word_text.strip().lower()):] if word_text.strip() and word_text.strip().lower() in text.lower() else text
            if _introduction_trigger(probe):
                return max(base, base + float(word.get("start", piece.get("start", 0.0))))
        return max(base, base + float(piece.get("start", 0.0)))
    return base


def select_commentator_announcements(results: list[dict[str, object]]) -> list[dict[str, object]]:
    """Keep one real presenter intro per slot, not every Whisper continuation.

    Whisper candidate windows overlap deliberately so a quiet announcement is
    not missed.  Their results therefore contain duplicates and conversational
    fragments.  A boundary is eligible only when the transcript has an
    introduction trigger, a spoken slot number, or an instrument assignment.
    Nearby eligible results are one presentation and are collapsed without
    moving its first-word timestamp later.
    """
    eligible: list[dict[str, object]] = []
    for item in results:
        if not item.get("announcement") or not item.get("text"):
            continue
        text = str(item.get("text") or "")
        if not (
            item.get("spoken_song_number") is not None
            or _introduction_trigger(text)
            or item.get("musician_labels")
        ):
            continue
        candidate = dict(item)
        candidate["announcement_start"] = float(item.get("announcement_start", item.get("start", 0.0)))
        eligible.append(candidate)
    eligible.sort(key=lambda item: float(item["announcement_start"]))

    clusters: list[list[dict[str, object]]] = []
    for item in eligible:
        # A long presenter introduction is commonly split into several
        # Whisper windows with pauses and side comments. A new slot cannot be
        # validly less than eight minutes after the previous one, so collapse
        # nearby eligible fragments within this conservative 150 s envelope.
        if not clusters or float(item["announcement_start"]) - float(clusters[-1][-1]["announcement_start"]) > 150.0:
            clusters.append([item])
        else:
            clusters[-1].append(item)

    selected: list[dict[str, object]] = []
    for cluster in clusters:
        representative = max(
            cluster,
            key=lambda item: (
                1 if item.get("spoken_song_number") is not None else 0,
                1 if item.get("musician_labels") else 0,
                float(item.get("speech_confidence", 0.0)),
                len(str(item.get("text") or "")),
            ),
        )
        first_word = min(float(item["announcement_start"]) for item in cluster)
        representative = dict(representative)
        representative["announcement_start"] = first_word
        representative["intro_first_word_timestamp"] = first_word
        representative["deduplicated_transcript_count"] = len(cluster)
        selected.append(representative)
    return selected


def _introduction_trigger(text: str) -> bool:
    low = text.lower()
    return bool(
        spoken_song_number(low) is not None
        or any(phrase in low for phrase in ("next group", "next round", "instrumental slot", "instrumental jam", "so we're going to have", "so we are going to have", "let's enjoy", "let us enjoy", "free for all", "anyone wants to join", "jump in"))
        or bool(re.search(r"\b(?:we have|now we have)\b.*\b(?:bass|drum|percussion|piano|keyboard|keys|guitar|sax|flute|trumpet|horn|brass|vocal|voice)\b", low))
        or bool(extract_musician_labels(text))
    )


def classify_speech_piece(text: str) -> str:
    low = text.lower().strip()
    if not low:
        return "non-speech/setup"
    intro = _introduction_trigger(low) or bool(extract_musician_labels(text))
    closing = any(phrase in low for phrase in ("that was", "that is", "amazing", "beautiful", "loved it", "appreciate", "give it up", "thank you", "nice experiment", "how did you like", "feedback"))
    if intro:
        return "next-song introduction"
    if closing:
        return "previous-song closing remarks"
    return "non-speech/setup"


def speech_candidate_windows(
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
    session_end: float,
    boundary_probes: list[float] | None = None,
    focus_regions: list[tuple[float, float]] | None = None,
) -> list[tuple[float, float]]:
    if focus_regions:
        windows: list[tuple[float, float]] = []
        for region_start, region_end in focus_regions:
            cursor = max(0.0, float(region_start))
            end = min(float(session_end), float(region_end))
            while cursor < end:
                piece_end = min(end, cursor + 90.0)
                if piece_end - cursor >= 2.0:
                    windows.append((cursor, piece_end))
                cursor = piece_end
        return sorted(set(windows))
    voices = [stem for stem in stems if stem.role in {"vocal", "room"}]
    instruments = [stem for stem in stems if stem not in voices]
    if not voices or not instruments:
        return []
    levels = {stem.path.name: stem_active_level(timelines[stem.path.name]) for stem in stems}
    levels = {name: level for name, level in levels.items() if level is not None}
    voice_stack = np.vstack([timelines[s.path.name] / max(levels[s.path.name], 1e-9) for s in voices if s.path.name in levels])
    inst_stack = np.vstack([timelines[s.path.name] / max(levels[s.path.name], 1e-9) for s in instruments if s.path.name in levels])
    voice_db = 20.0 * np.log10(np.maximum(np.max(voice_stack, axis=0), 1e-9))
    inst_db = 20.0 * np.log10(np.maximum(np.max(inst_stack, axis=0), 1e-9))
    # One spoken introduction is often broken into several short phrases;
    # retain it as one transcription clip when pauses are under 30 seconds.
    # Keep the high-precision quiet-instrument gate for the normal pass. A
    # missing window is now a hard detection failure; it is never converted
    # into an acoustic-only result.
    candidate_mask = (voice_db > -26.0) & (inst_db < -18.0)
    regions = merge_regions(mask_to_regions(smooth_boolean_activity(candidate_mask, 2.0), 3.0), 30.0)
    windows: list[tuple[float, float]] = []
    for start, end in regions:
        cursor = start
        while cursor < end:
            piece_end = min(end, cursor + 90.0)
            if piece_end - cursor >= 2.0:
                windows.append((max(0.0, cursor), min(session_end, piece_end)))
            cursor = piece_end
    # Acoustic boundaries are only probe locations.  They are never accepted
    # as songs, but probing each one prevents the voice/instrument gate from
    # silently hiding a real introduction (especially a quiet or overlapping
    # announcement).
    for boundary in boundary_probes or []:
        start = max(0.0, float(boundary) - 60.0)
        end = min(session_end, float(boundary) + 60.0)
        if end - start >= 2.0:
            windows.append((start, end))
    # Keep Whisper's native floating-point timestamps.  Boundary precision is
    # resolved only when audio frames are read/written; truncating candidate
    # windows here can strand the introduction on the wrong side of a cut.
    return sorted(set((float(start), float(end)) for start, end in windows))


def transcribe_speech_candidates(
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
    session_end: float,
    boundary_probes: list[float] | None = None,
    focus_regions: list[tuple[float, float]] | None = None,
    timeout_seconds: float | None = None,
) -> tuple[list[dict[str, object]], list[Segment]]:
    global LAST_SPEECH_TRANSCRIPTIONS, LAST_WHISPER_STATUS
    windows = speech_candidate_windows(stems, timelines, session_end, boundary_probes, focus_regions)
    LAST_SPEECH_TRANSCRIPTIONS = []
    effective_timeout = float(timeout_seconds if timeout_seconds is not None else WHISPER_TIMEOUT_SECONDS)
    LAST_WHISPER_STATUS = {"status": "starting", "candidate_windows": len(windows), "timeout_seconds": effective_timeout}
    whisper_python, worker = whisper_runtime_paths()
    if not whisper_python.exists() or not worker.exists():
        LAST_WHISPER_STATUS = {
            "status": "unavailable",
            "reason": "Whisper runtime is not installed",
            "python": str(whisper_python),
            "worker": str(worker),
        }
        raise RuntimeError(
            "Whisper is mandatory for detection but its runtime is unavailable: "
            f"python={whisper_python} worker={worker}"
        )
    if not windows:
        LAST_WHISPER_STATUS = {"status": "unavailable", "reason": "no speech candidate windows"}
        raise RuntimeError(
            "Whisper is mandatory for detection but speech candidate discovery produced no windows; "
            "acoustic-only fallback is disabled."
        )
    transcript_cache_root = DETECTION_CACHE_ROOT or DETECTION_CACHE.parent
    transcript_digest = hashlib.sha256(
        f"{SPEECH_TRANSCRIPTION_CACHE_VERSION}|{cache_signature(stems)}".encode("utf-8")
    ).hexdigest()[:16]
    transcript_cache_path = transcript_cache_root / f"jam_whisper_transcripts_{transcript_digest}.json"
    results: list[dict[str, object]] | None = None

    def compatible_legacy_transcript() -> list[dict[str, object]] | None:
        """Reuse a same-source transcript when a long Whisper pass times out.

        The current source signature may differ only by the cache algorithm
        prefix. The stem/file/frame/offset suffix and exact candidate windows
        must still match, so this cannot mix sessions or selected-slot audio.
        """
        current_tail = cache_signature(stems).split("|", 1)[-1]
        for legacy_path in sorted(transcript_cache_root.glob("jam_whisper_transcripts_*.json")):
            if legacy_path == transcript_cache_path:
                continue
            try:
                legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
                legacy_tail = str(legacy.get("signature") or "").split("|", 1)[-1]
                if (
                    legacy_tail == current_tail
                    and legacy.get("windows") == [[float(start), float(end)] for start, end in windows]
                    and isinstance(legacy.get("results"), list)
                ):
                    print(f"Reusing compatible same-source Whisper transcript after timeout: {legacy_path}", flush=True)
                    return list(legacy["results"])
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return None
    try:
        # Re-detect is an explicit user command to redo the detection work.
        # The per-stem envelope cache remains reusable, but Whisper must see a
        # fresh candidate pass so song boundaries and spoken numbers cannot be
        # inherited from an older detection result.
        if transcript_cache_path.exists() and not DETECTION_RESCAN_MODE:
            payload = json.loads(transcript_cache_path.read_text(encoding="utf-8"))
            if (
                payload.get("version") == SPEECH_TRANSCRIPTION_CACHE_VERSION
                and payload.get("windows") == [[float(start), float(end)] for start, end in windows]
                and isinstance(payload.get("results"), list)
            ):
                results = list(payload["results"])
                print(f"Reusing deterministic Whisper transcript cache: {transcript_cache_path}", flush=True)
    except (OSError, ValueError, TypeError, KeyError):
        results = None
    sr = stems[0].samplerate
    vocal_inputs = [stem.path.name for stem in stems if stem.role in {"vocal", "room"}]
    if results is None:
        print(f"WHISPER INPUT: mixing all vocal/mic stems ({len(vocal_inputs)}): {vocal_inputs}", flush=True)
        with tempfile.TemporaryDirectory(prefix="zucker_whisper_") as temp:
            temp_dir = Path(temp)
            requests = []
            for index, (start, end) in enumerate(windows):
                frames = max(1, int(round((end - start) * sr)))
                mix = np.zeros(frames, dtype=np.float32)
                used = 0
                for stem in [item for item in stems if item.role in {"vocal", "room"}]:
                    audio = read_stem_chunk(stem, Segment(start, end), 0, frames)
                    if audio is None:
                        continue
                    mono = audio if audio.ndim == 1 else np.mean(audio, axis=1)
                    mix[:len(mono)] += np.nan_to_num(mono[:frames], nan=0.0, posinf=0.0, neginf=0.0)
                    used += 1
                if not used or float(np.max(np.abs(mix))) < 1e-6:
                    continue
                mix /= max(1.0, np.sqrt(float(used)))
                path = temp_dir / f"candidate_{index:04d}.wav"
                sf.write(str(path), mix, sr, subtype="PCM_16")
                requests.append({"id": index, "start": start, "end": end, "path": str(path)})
            if not requests:
                return [], []
            input_json, output_json = temp_dir / "requests.json", temp_dir / "results.json"
            input_json.write_text(json.dumps(requests), encoding="utf-8")
            model_dir = Path.home() / "Library" / "Application Support" / "ZuckerMixer" / "whisper"
            report_progress({"current_stage": "transcribing speech", "stage_detail": f"Preparing Whisper for {len(requests)} commentator windows", "progress": 70, "song_progress": 70, "phase_index": 4, "phase_total": 7, "candidate_windows": len(requests), "heartbeat": time.time()})
            if getattr(sys, "frozen", False):
                command = [str(whisper_python), "--whisper-worker", "--input-json", str(input_json), "--output-json", str(output_json), "--model", WHISPER_MODEL_SIZE, "--model-dir", str(model_dir)]
            else:
                command = [str(whisper_python), str(worker), "--input-json", str(input_json), "--output-json", str(output_json), "--model", WHISPER_MODEL_SIZE, "--model-dir", str(model_dir)]
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            progress_events: queue.Queue[tuple[str, str]] = queue.Queue()
            stream_lines: dict[str, list[str]] = {"stdout": [], "stderr": []}

            def drain_stream(name: str, stream: object) -> None:
                try:
                    for line in iter(stream.readline, ""):  # type: ignore[attr-defined]
                        clean = str(line).rstrip()
                        stream_lines[name].append(clean)
                        progress_events.put((name, clean))
                finally:
                    try:
                        stream.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass

            stream_threads = [
                threading.Thread(target=drain_stream, args=("stdout", process.stdout), daemon=True),
                threading.Thread(target=drain_stream, args=("stderr", process.stderr), daemon=True),
            ]
            for thread in stream_threads:
                thread.start()

            whisper_started = time.monotonic()
            last_worker_activity = whisper_started
            completed_windows = 0
            total_windows = max(1, len(requests))
            idle_timeout = max(180.0, float(effective_timeout or 300.0))
            model_start_timeout = max(30.0, WHISPER_MODEL_START_TIMEOUT_SECONDS)
            model_ready = False
            model_loading_started: float | None = None
            watchdog_reason = ""
            while process.poll() is None:
                while True:
                    try:
                        stream_name, line = progress_events.get_nowait()
                    except queue.Empty:
                        break
                    if stream_name != "stdout" or not line.startswith("{"):
                        continue
                    try:
                        event = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    event_name = str(event.get("event") or "")
                    if event_name in {"worker_started", "runtime_ready", "model_loading", "model_ready"}:
                        last_worker_activity = time.monotonic()
                        if event_name == "model_loading" and model_loading_started is None:
                            model_loading_started = time.monotonic()
                        model_ready = model_ready or event_name == "model_ready"
                        stage_detail = {
                            "worker_started": "Whisper worker started",
                            "runtime_ready": "Whisper runtime loaded; loading model",
                            "model_loading": "Whisper is loading the model",
                            "model_ready": f"Whisper ready — processing {total_windows} windows",
                        }.get(event_name, "Whisper starting")
                        report_progress({
                            "current_stage": "transcribing speech",
                            "stage_detail": stage_detail,
                            "progress": 70,
                            "process_progress": 0,
                            "song_progress": 0,
                            "phase_index": 4,
                            "phase_total": 7,
                            "candidate_windows": total_windows,
                            "whisper_completed_windows": completed_windows,
                            "whisper_total_windows": total_windows,
                            "whisper_model_ready": model_ready,
                            "heartbeat": time.time(),
                        })
                    elif event_name == "window_complete":
                        completed_windows = max(
                            completed_windows,
                            min(total_windows, int(event.get("completed") or 0)),
                        )
                        last_worker_activity = time.monotonic()
                        process_progress = int(round(completed_windows / total_windows * 100))
                        report_progress({
                            "current_stage": "transcribing speech",
                            "stage_detail": f"Whisper transcribed window {completed_windows}/{total_windows}",
                            "progress": 70 + int(round(18 * completed_windows / total_windows)),
                            "process_progress": process_progress,
                            "song_progress": process_progress,
                            "phase_index": 4,
                            "phase_total": 7,
                            "candidate_windows": total_windows,
                            "transcript_count": completed_windows,
                            "whisper_completed_windows": completed_windows,
                            "whisper_total_windows": total_windows,
                            "heartbeat": time.time(),
                        })
                # Runtime import and model construction are separate phases.
                # Start the model watchdog when the worker explicitly reports
                # model_loading, so a first-run download/CPU initialization is
                # not mistaken for a dead Whisper worker.
                model_watch_started = model_loading_started or whisper_started
                model_elapsed = time.monotonic() - model_watch_started
                if not model_ready and model_elapsed >= model_start_timeout:
                    watchdog_reason = (
                        f"Whisper model did not become ready after {int(model_elapsed)}s "
                        f"of model loading; automatic fallback before window "
                        f"{completed_windows + 1}/{total_windows}"
                    )
                    process.kill()
                    break
                idle_seconds = time.monotonic() - last_worker_activity
                if idle_seconds >= idle_timeout:
                    watchdog_reason = (
                        f"Whisper produced no worker event for {int(idle_seconds)}s "
                        f"while processing window {completed_windows + 1}/{total_windows}"
                    )
                    process.kill()
                    break
                time.sleep(0.25)

            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            for thread in stream_threads:
                thread.join(timeout=2.0)
            stdout = "\n".join(stream_lines["stdout"])
            stderr = "\n".join(stream_lines["stderr"])

            if watchdog_reason:
                legacy_results = compatible_legacy_transcript()
                if legacy_results is not None:
                    results = legacy_results
                    LAST_WHISPER_STATUS = {
                        "status": "available_cached",
                        "candidate_windows": total_windows,
                        "completed_windows": completed_windows,
                        "reason": f"{watchdog_reason}; reused compatible same-source transcript",
                        "idle_timeout_seconds": idle_timeout,
                    }
                else:
                    LAST_WHISPER_STATUS = {
                        "status": "timed_out",
                        "idle_timeout_seconds": idle_timeout,
                        "model_start_timeout_seconds": model_start_timeout,
                        "model_ready": model_ready,
                        "candidate_windows": total_windows,
                        "completed_windows": completed_windows,
                        "reason": watchdog_reason,
                        "stdout": stdout[-2000:],
                        "stderr": stderr[-2000:],
                    }
                    raise RuntimeError(watchdog_reason)
            elif process.returncode != 0:
                LAST_WHISPER_STATUS = {
                    "status": "error",
                    "returncode": process.returncode,
                    "candidate_windows": total_windows,
                    "completed_windows": completed_windows,
                    "stdout": stdout[-2000:],
                    "stderr": stderr[-2000:],
                }
                raise RuntimeError(f"Whisper exited with code {process.returncode}")
            else:
                results = json.loads(output_json.read_text(encoding="utf-8"))
                report_progress({
                    "current_stage": "transcribing speech",
                    "stage_detail": f"Whisper returned {len(results)} transcript windows",
                    "progress": 88,
                    "process_progress": 100,
                    "song_progress": 100,
                    "phase_index": 4,
                    "phase_total": 7,
                    "transcript_count": len(results),
                    "whisper_completed_windows": len(results),
                    "whisper_total_windows": total_windows,
                    "heartbeat": time.time(),
                })
                LAST_WHISPER_STATUS = {
                    "status": "available",
                    "candidate_windows": total_windows,
                    "completed_windows": len(results),
                    "stdout": stdout[-2000:],
                    "stderr": stderr[-2000:],
                }
        try:
            transcript_cache_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_cache_path.write_text(json.dumps({
                "version": SPEECH_TRANSCRIPTION_CACHE_VERSION,
                "signature": cache_signature(stems),
                "windows": [[float(start), float(end)] for start, end in windows],
                "results": results,
            }, ensure_ascii=False), encoding="utf-8")
            print(f"Saved deterministic Whisper transcript cache: {transcript_cache_path}", flush=True)
        except OSError as exc:
            print(f"WARNING: unable to save Whisper transcript cache: {exc}", flush=True)
    assert results is not None
    for result in results:
        text = str(result.get("text") or "").strip()
        result["spoken_song_number"] = spoken_song_number(text)
        result["spoken_last"] = spoken_last_song(text)
        result["musician_labels"] = dict(extract_musician_labels(text))
        result["announcement"] = _speech_is_announcement(text, float(result.get("speech_confidence", 0.0)), float(result.get("avg_logprob", -1.0)))
        result["announcement_reason"] = announcement_reason(text, float(result.get("speech_confidence", 0.0)), float(result.get("avg_logprob", -1.0)))
        result["announcement_start"] = announcement_start(result)
        result["intro_first_word_timestamp"] = float(result["announcement_start"])
        result["speech_classification"] = [
            {"start": float(piece.get("start", 0.0)) + float(result.get("start", 0.0)), "end": float(piece.get("end", 0.0)) + float(result.get("start", 0.0)), "text": str(piece.get("text") or ""), "classification": classify_speech_piece(str(piece.get("text") or ""))}
            for piece in (result.get("segments") or []) if isinstance(piece, dict)
        ]
        intro_texts = [row["text"] for row in result["speech_classification"] if row["classification"] == "next-song introduction"]
        # An announcement can be a short/garbled Whisper chunk whose piece
        # classifier misses the instrument nouns.  Once the announcement
        # decision is positive, its complete transcript is still the only
        # safe app-facing introduction; leaving this empty would fabricate an
        # introduction-less song and trip the structural invariant.
        result["speech_intro_text"] = " ".join(intro_texts).strip() or (text if result["announcement"] else "")
    LAST_SPEECH_TRANSCRIPTIONS = results
    report_progress({"current_stage": "merging song boundaries", "stage_detail": f"Classifying {len(results)} transcript windows and extracting introductions", "progress": 91, "song_progress": 91, "phase_index": 5, "phase_total": 7, "transcript_count": len(results), "heartbeat": time.time()})
    if not LAST_SPEECH_TRANSCRIPTIONS:
        LAST_WHISPER_STATUS = {"status": "unavailable", "reason": "no transcriptions returned"}
        raise RuntimeError("Whisper is mandatory for detection but produced no transcriptions.")
    print(f"WHISPER STORE CONFIRMED: LAST_SPEECH_TRANSCRIPTIONS={len(LAST_SPEECH_TRANSCRIPTIONS)}", flush=True)
    announcement_windows = select_commentator_announcements(results)
    # Whisper can complete every requested window while still finding too few
    # presenter introductions to build a trustworthy slot list.  Preserve the
    # complete diagnostic state in that case; raising here used to make the
    # caller discard the transcript results and silently create one metadata
    # block for the whole session.
    transcript_count = len(results)
    LAST_WHISPER_STATUS = {
        **LAST_WHISPER_STATUS,
        "transcript_count": transcript_count,
        "results_returned": transcript_count,
        "candidate_windows": len(windows),
        "completed_windows": transcript_count,
        "eligible_introductions": len(announcement_windows),
        "session_segmentation_usable": len(announcement_windows) >= 2,
    }
    if len(announcement_windows) < 2:
        LAST_WHISPER_STATUS = {
            **LAST_WHISPER_STATUS,
            "status": "needs_review",
            "reason": f"fewer than two introductions ({len(announcement_windows)}) after {transcript_count}/{len(windows)} Whisper windows completed",
        }
        print(
            "WHISPER COMPLETED BUT NEEDS REVIEW: "
            f"{transcript_count}/{len(windows)} transcript windows, "
            f"{len(announcement_windows)} eligible introductions; "
            "no valid session segmentation was produced",
            flush=True,
        )
        # Return the transcripts for the diagnostic candidate.  The caller
        # creates one explicitly review-only provisional block rather than
        # treating this as a successful one-song detection.
        return results, []
    segments: list[Segment] = []
    for index, item in enumerate(announcement_windows):
        # The introduction identifies the song and intentionally belongs in
        # that song's export.  The next announcement marks the next song.
        start = float(item.get("announcement_start", item["start"]))
        end = float(announcement_windows[index + 1]["start"]) if index + 1 < len(announcement_windows) else session_end
        if end - start < MC_MIN_FINAL_SONG_SECONDS:
            continue
        labels = tuple(sorted((str(k), str(v)) for k, v in dict(item.get("musician_labels") or {}).items()))
        segments.append(Segment(start, end, core_start=start, core_end=end, nominal_end=end, boundary_source="whisper-announcement", speech_text=str(item.get("text") or ""), speech_reason=str(item.get("announcement_reason") or ""), speech_intro_text=str(item.get("speech_intro_text") or ""), speech_intro_start=float(item.get("intro_first_word_timestamp", start)), speech_confidence=float(item.get("speech_confidence") or 0.0), spoken_song_number=item.get("spoken_song_number"), spoken_last=bool(item.get("spoken_last")), musician_labels=labels))
    print(f"WHISPER: {len(windows)} candidates, {len(results)} transcripts, {len(segments)} announcement boundaries", flush=True)
    for item in results:
        print(f"WHISPER {fmt_time(float(item['start']))}-{fmt_time(float(item['end']))}: {item.get('text','')}", flush=True)
    return results, segments


def finalize_presented_slot_segments(
    announcements: list[dict[str, object]],
    session_end: float,
    lead_in_seconds: float = 0.5,
) -> list[Segment]:
    """Build one export window per commentator presentation.

    A slot is a social/editorial unit, not a detected song.  The next
    presentation closes the current slot even when the slot contains two
    musical pieces or isolated instrument entrances.  Duration violations
    remain visible for Select Cuts; they are never repaired by an acoustic
    split.
    """
    # Production callers pass the complete Whisper store. Keep normalized
    # rows usable for Select Cuts and focused unit tests.
    if any("announcement" in item for item in announcements):
        announcements = select_commentator_announcements(announcements)
    ordered = sorted(
        [item for item in announcements if item.get("text")],
        key=lambda item: float(item.get("announcement_start", item.get("start", 0.0))),
    )
    slots: list[Segment] = []
    for index, item in enumerate(ordered):
        first_word = float(item.get("announcement_start", item.get("start", 0.0)))
        start = max(0.0, first_word - lead_in_seconds)
        if index + 1 < len(ordered):
            next_first_word = float(ordered[index + 1].get("announcement_start", ordered[index + 1].get("start", 0.0)))
            end = max(start, next_first_word - lead_in_seconds)
        else:
            end = float(session_end)
        confidence = float(item.get("speech_confidence") or 0.0)
        reasons: list[str] = []
        if confidence < 0.45:
            reasons.append(f"commentator confidence {confidence:.2f} below 0.45")
        if not str(item.get("speech_intro_text") or item.get("text") or "").strip():
            reasons.append("commentator presentation text unavailable")
        if end - start < HARD_MIN_SONG_SECONDS:
            reasons.append(f"slot duration {end - start:.1f}s below 8:00")
        elif end - start > HARD_MAX_SONG_SECONDS:
            reasons.append(f"slot duration {end - start:.1f}s above 13:00; slot not split automatically")
        labels = tuple(sorted((str(k), str(v)) for k, v in dict(item.get("musician_labels") or {}).items()))
        slots.append(Segment(
            start,
            end,
            core_start=start,
            core_end=end,
            nominal_end=end,
            boundary_source="whisper-presented-slot",
            boundary_validation="needs_review" if reasons else "valid",
            boundary_validation_reason="; ".join(reasons),
            speech_text=str(item.get("text") or ""),
            speech_reason=str(item.get("announcement_reason") or "commentator presentation"),
            speech_intro_text=str(item.get("speech_intro_text") or item.get("text") or ""),
            speech_intro_start=first_word,
            speech_confidence=confidence,
            spoken_song_number=item.get("spoken_song_number"),
            spoken_last=bool(item.get("spoken_last")),
            musician_labels=labels,
        ))
    return slots


def verify_rendered_announcement(path: Path, segment: Segment, song_index: int) -> dict[str, object]:
    """Whisper every rendered opening and compare it with the app-facing intro."""
    expected = str(segment.speech_intro_text or segment.speech_text or "").strip()
    if not expected:
        return {"song": song_index, "checked": False, "reason": "no source announcement metadata"}
    whisper_python, worker = whisper_runtime_paths()
    ffmpeg = resolve_ffmpeg()
    ffprobe = resolve_ffprobe()
    if not whisper_python.exists() or not worker.exists() or not ffmpeg or not ffprobe:
        return {"song": song_index, "checked": False, "reason": "Whisper or ffmpeg unavailable"}
    with tempfile.TemporaryDirectory(prefix="zucker_render_verify_") as temp:
        wav = Path(temp) / "opening.wav"
        request = Path(temp) / "request.json"
        result_path = Path(temp) / "result.json"
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-t", "30", "-i", str(path), "-ar", "16000", "-ac", "1", str(wav)], check=True)
        request.write_text(json.dumps([{"id": 1, "start": 0.0, "end": 30.0, "path": str(wav)}]), encoding="utf-8")
        subprocess.run([str(whisper_python), str(worker), "--input-json", str(request), "--output-json", str(result_path), "--model", WHISPER_MODEL_SIZE, "--model-dir", str(Path.home() / "Library" / "Application Support" / "ZuckerMixer" / "whisper")], check=True)
        rows = json.loads(result_path.read_text(encoding="utf-8"))
    text = str(rows[0].get("text") or "").strip() if rows else ""
    def words(value: str) -> list[str]:
        return [word for word in re.findall(r"[a-z0-9]+", value.lower()) if len(word) >= 3]
    expected_words = words(expected)
    heard_words = words(text)
    expected_set = set(expected_words)
    heard_set = set(heard_words)
    overlap = len(expected_set & heard_set) / max(1, min(10, len(expected_set)))
    # Compare the beginning, not just any word in the 30-second clip. This
    # catches a render that starts at “we have a flute” while the UI says the
    # introduction started earlier.
    prefix_len = min(6, len(expected_words), len(heard_words))
    prefix_overlap = (
        len(set(expected_words[:prefix_len]) & set(heard_words[:prefix_len])) / max(1, prefix_len)
        if prefix_len else 0.0
    )
    matches = bool(text) and (prefix_overlap >= 0.5 or overlap >= 0.6)
    result = {
        "song": song_index,
        "checked": True,
        "matches_displayed_introduction": matches,
        "present": matches,
        "expected_text": expected,
        "rendered_opening_text": text,
        "expected_overlap": round(overlap, 3),
        "prefix_overlap": round(prefix_overlap, 3),
    }
    if not matches:
        print(
            f"!!! BOUNDARY BUG song={song_index}: rendered opening does not match displayed introduction\n"
            f"    DISPLAYED: {expected}\n    RENDERED:  {text}",
            flush=True,
        )
    else:
        print(f"RENDER ANNOUNCEMENT VERIFY song={song_index} match=True text={text}", flush=True)
    return result


def align_transcript_introduction_boundaries(segments: list[Segment]) -> list[Segment]:
    """Make each app-facing introduction opening the shared render boundary.

    This is deliberately after all acoustic/duration heuristics. Those
    heuristics may explain a decision, but they must not strand the first
    words of an introduction in the preceding rendered file.
    """
    if not segments:
        return segments
    aligned = list(segments)
    audit: list[dict[str, object]] = []
    for index in range(1, len(aligned)):
        segment = aligned[index]
        if segment.speech_intro_start is None or not segment.speech_intro_text:
            continue
        target = max(0.0, float(segment.speech_intro_start) - 0.5)
        previous = aligned[index - 1]
        lower = previous.start + MIN_SONG_SECONDS
        upper = segment.end - MIN_SONG_SECONDS
        actual = min(max(target, lower), upper)
        if actual != target:
            print(
                f"!!! INTRO BOUNDARY CLAMP song={index + 1}: requested {fmt_time(target)} "
                f"but usable range is {fmt_time(lower)}-{fmt_time(upper)}",
                flush=True,
            )
        aligned[index - 1] = replace(aligned[index - 1], end=actual, nominal_end=actual, core_end=actual)
        aligned[index] = replace(aligned[index], start=actual, core_start=actual,
                                 boundary_source="whisper-introduction-authoritative")
        audit.append({"song": index + 1, "intro_first_word": float(segment.speech_intro_start),
                      "target": target, "actual": actual, "matches": abs(actual - target) <= DETECTION_FRAME_SECONDS})
        print(f"INTRO BOUNDARY AUTHORITATIVE song={index + 1}: start/end={fmt_time(actual)} "
              f"first_word={fmt_time(float(segment.speech_intro_start))}", flush=True)
    DETECTION_STRATEGY["transcript_introduction_boundaries"] = audit
    return aligned


def require_structural_introductions(segments: list[Segment]) -> None:
    """Reject detection when the session's known intro invariant is broken."""
    missing = [index for index, segment in enumerate(segments, 1)
               if not str(segment.speech_intro_text or '').strip()]
    if not missing:
        return
    evidence = [
        {
            "song": index,
            "start": segments[index - 1].start,
            "end": segments[index - 1].end,
            "boundary_source": segments[index - 1].boundary_source,
            "speech_text": segments[index - 1].speech_text,
        }
        for index in missing
    ]
    DETECTION_STRATEGY["structural_intro_error"] = {
        "missing_songs": missing,
        "evidence": evidence,
        "message": "Every session song must have an introduction at its start; detection is not accepted.",
    }
    print(f"!!! STRUCTURAL INTRO ERROR: missing introduction for songs {missing}; detection rejected", flush=True)
    for item in evidence:
        print(f"    song={item['song']} {fmt_time(float(item['start']))}-{fmt_time(float(item['end']))} "
              f"source={item['boundary_source']} text={item['speech_text']!r}", flush=True)
    raise RuntimeError(
        "Structural introduction invariant failed: "
        f"song(s) {', '.join(map(str, missing))} have no verified introduction. "
        "The missing introduction must be recovered from the preceding tail before accepting detection."
    )


def verify_rendered_pair_edges(paths: dict[int, Path], segments: list[Segment], pair_count: int = 5) -> list[dict[str, object]]:
    """Verify that each next-song introduction is only present at its own opening.

    Both clips are transcribed in one offline Whisper worker invocation so the
    comparison uses the same model/configuration at both sides of every cut.
    """
    pairs = [(index, index + 1) for index in range(1, min(pair_count, len(segments) - 1) + 1)
             if index in paths and index + 1 in paths]
    if not pairs:
        return []
    whisper_python, worker = whisper_runtime_paths()
    ffmpeg = resolve_ffmpeg()
    ffprobe = resolve_ffprobe()
    if not whisper_python.exists() or not worker.exists() or not ffmpeg or not ffprobe:
        print("RENDER EDGE VERIFY unavailable: Whisper or ffmpeg missing", flush=True)
        return [{"song": left, "next_song": right, "checked": False, "reason": "Whisper or ffmpeg unavailable"}
                for left, right in pairs]
    with tempfile.TemporaryDirectory(prefix="zucker_edge_verify_") as temp:
        temp_dir = Path(temp)
        request_rows = []
        clip_ids: dict[tuple[int, str], int] = {}
        next_id = 1
        for left, right in pairs:
            for song, side in ((left, "tail"), (right, "opening")):
                source = paths[song]
                probe = subprocess.run([ffprobe, "-hide_banner", "-loglevel", "error", "-show_entries", "format=duration",
                                        "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
                                       check=True, capture_output=True, text=True)
                duration = max(0.0, float(probe.stdout.strip() or 0.0))
                wav = temp_dir / f"{song:02d}_{side}.wav"
                if side == "tail":
                    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", str(max(0.0, duration - 30.0)),
                               "-t", "30", "-i", str(source), "-ar", "16000", "-ac", "1", str(wav)]
                else:
                    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-t", "30", "-i", str(source),
                               "-ar", "16000", "-ac", "1", str(wav)]
                subprocess.run(command, check=True)
                clip_ids[(song, side)] = next_id
                request_rows.append({"id": next_id, "start": 0.0, "end": 30.0, "path": str(wav)})
                next_id += 1
        request = temp_dir / "request.json"
        result_path = temp_dir / "result.json"
        request.write_text(json.dumps(request_rows), encoding="utf-8")
        subprocess.run([str(whisper_python), str(worker), "--input-json", str(request), "--output-json", str(result_path),
                        "--model", WHISPER_MODEL_SIZE, "--model-dir", str(Path.home() / "Library" / "Application Support" / "ZuckerMixer" / "whisper")], check=True)
        rows = {int(row.get("id", -1)): row for row in json.loads(result_path.read_text(encoding="utf-8"))}

    def words(value: str) -> set[str]:
        return {word for word in re.findall(r"[a-z0-9]+", value.lower()) if len(word) >= 4}

    verified: list[dict[str, object]] = []
    for left, right in pairs:
        expected = str(segments[right - 1].speech_intro_text or segments[right - 1].speech_text or "")
        expected_words = words(expected)
        opening_text = str(rows.get(clip_ids[(right, "opening")], {}).get("text") or "").strip()
        tail_text = str(rows.get(clip_ids[(left, "tail")], {}).get("text") or "").strip()
        opening_words = words(opening_text)
        tail_words = words(tail_text)
        opening_overlap = len(expected_words & opening_words) / max(1, min(8, len(expected_words)))
        tail_overlap = len(expected_words & tail_words) / max(1, min(8, len(expected_words)))
        intro_at_start = bool(opening_text) and (opening_overlap >= 0.25 or float(rows.get(clip_ids[(right, "opening")], {}).get("speech_confidence") or 0.0) >= 0.25)
        intro_at_previous_end = bool(tail_text) and tail_overlap >= 0.25
        result = {"song": left, "next_song": right, "checked": True,
                  "intro_present_at_start": intro_at_start,
                  "intro_wrongly_present_at_previous_end": intro_at_previous_end,
                  "start_text": opening_text, "previous_end_text": tail_text,
                  "start_overlap": round(opening_overlap, 3), "previous_end_overlap": round(tail_overlap, 3)}
        verified.append(result)
        print(f"RENDER EDGE VERIFY {left}->{right} start_intro={intro_at_start} previous_end_intro={intro_at_previous_end} "
              f"start={opening_text!r} previous_end={tail_text!r}", flush=True)
    return verified


def attach_speech_metadata(segments: list[Segment]) -> list[Segment]:
    """Restore transcript metadata after generic trim/validation replacements."""
    if not LAST_SPEECH_TRANSCRIPTIONS:
        return segments
    out = []
    for segment in segments:
        preceding = [item for item in LAST_SPEECH_TRANSCRIPTIONS if float(item.get("end", -1)) <= segment.start + 90.0]
        item = min(preceding, key=lambda candidate: abs(float(candidate.get("end", 0.0)) - segment.start), default=None)
        if item is None or not item.get("announcement") or abs(float(item.get("end", 0.0)) - segment.start) > 90.0:
            out.append(segment)
            continue
        labels = tuple(sorted((str(k), str(v)) for k, v in dict(item.get("musician_labels") or {}).items()))
        out.append(replace(
            segment,
            speech_text=str(item.get("text") or ""),
            speech_reason=str(item.get("announcement_reason") or ""),
            speech_intro_text=str(item.get("speech_intro_text") or ""),
            # Generic duration/split passes can replace the Segment and lose
            # its timestamp while the transcript text is still recoverable.
            # Restore the authoritative first-word time from the same
            # Whisper result; otherwise the shared-boundary pass sees `none`
            # and cannot protect this introduction from acoustic relocation.
            speech_intro_start=float(item.get("intro_first_word_timestamp", item.get("announcement_start", segment.start))),
            speech_confidence=float(item.get("speech_confidence") or 0.0),
            spoken_song_number=item.get("spoken_song_number"),
            spoken_last=bool(item.get("spoken_last")),
            musician_labels=labels,
            boundary_source="whisper-announcement" if segment.boundary_source == "drum-silence" else segment.boundary_source,
        ))
    return out


def restore_final_introduction_lead_ins(segments: list[Segment]) -> list[Segment]:
    """Keep a final song window from stranding its announcement in the prior tail."""
    if not LAST_SPEECH_TRANSCRIPTIONS:
        return segments
    result = list(segments)
    for index, segment in enumerate(result):
        candidates = [
            item for item in LAST_SPEECH_TRANSCRIPTIONS
            if item.get("announcement") and item.get("speech_intro_text")
            and float(item.get("speech_intro_start", item.get("start", 0.0))) <= segment.end
            and float(item.get("speech_intro_start", item.get("start", 0.0))) >= segment.start - 90.0
        ]
        if not candidates:
            continue
        item = min(candidates, key=lambda row: abs(float(row.get("speech_intro_start", row.get("start", 0.0))) - segment.start))
        intro_start = float(item.get("speech_intro_start", item.get("start", 0.0)))
        # Whisper's window start can include a long thank-you/setup passage.
        # Prefer the first word-level segment that actually announces the
        # numbered song, so the displayed introduction is present in the
        # rendered opening clip rather than stranded deep inside it.
        base_start = float(item.get("start", 0.0))
        nested = item.get("segments") or []
        numbered = [
            base_start + float(row.get("start", 0.0))
            for row in nested if isinstance(row, dict)
            and any(term in str(row.get("text", "")).lower() for term in ("number", "we have the"))
        ]
        if numbered:
            intro_start = min(numbered)
        if intro_start > segment.start + 90.0:
            continue
        if intro_start < segment.start:
            proposed = max(0.0, intro_start - 0.5)
            if index > 0 and proposed < result[index - 1].end:
                result[index - 1] = replace(result[index - 1], end=proposed, nominal_end=proposed, core_end=proposed)
            segment = replace(segment, start=proposed, core_start=proposed)
        result[index] = replace(
            segment,
            speech_text=str(item.get("text") or segment.speech_text or ""),
            speech_reason=str(item.get("announcement_reason") or segment.speech_reason or ""),
            speech_intro_text=str(item.get("speech_intro_text") or segment.speech_intro_text or ""),
            speech_intro_start=intro_start,
            speech_confidence=float(item.get("speech_confidence") or segment.speech_confidence or 0.0),
            spoken_song_number=item.get("spoken_song_number", segment.spoken_song_number),
            spoken_last=bool(item.get("spoken_last", segment.spoken_last)),
            boundary_source=segment.boundary_source,
        )
    return result


def _split_pre_session_segment(
    segments: list[Segment],
    stems: list[Stem] | None,
    timelines: dict[str, np.ndarray] | None,
    transcripts: list[dict[str, object]] | None = None,
) -> list[Segment]:
    """Create permanent SONG 0 when music precedes the first spoken `one`."""
    first_index = next((i for i, seg in enumerate(segments) if seg.spoken_song_number == 1), None)
    if first_index is None:
        # Preserve the same opening-anchor fallback even if an older cached
        # Whisper result was serialized before the phrase parser was updated.
        first_index = next((i for i, seg in enumerate(segments)
                            if re.search(r"\bfirst\s+(?:slot|song|round)\b", seg.speech_text.lower())), None)
        if first_index is not None:
            segments[first_index] = replace(segments[first_index], spoken_song_number=1)
    if first_index is None and (transcripts or LAST_SPEECH_TRANSCRIPTIONS):
        opening = next((item for item in (transcripts or LAST_SPEECH_TRANSCRIPTIONS)
                        if re.search(r"\bfirst\s+(?:slot|song|round)\b", str(item.get("text") or "").lower())), None)
        if opening is not None:
            opening_at = float(opening.get("announcement_start", opening.get("start", 0.0)))
            first_index = next((i for i, seg in enumerate(segments)
                                if seg.start <= opening_at < seg.end), None)
            if first_index is None:
                first_index = next((i for i, seg in enumerate(segments) if seg.start >= opening_at), None)
            if first_index is not None:
                segments[first_index] = replace(segments[first_index], spoken_song_number=1,
                                                speech_text=segments[first_index].speech_text or str(opening.get("text") or ""),
                                                speech_intro_start=segments[first_index].speech_intro_start or opening_at)
    if first_index is None:
        return segments
    first = segments[first_index]
    if first_index > 0:
        # Some acoustic passes already isolated the leading jam as its own
        # segment. Preserve it as SONG 0 instead of manufacturing a tiny
        # pre-roll slice from the formal SONG 1 introduction.
        if not (stems and timelines and _has_meaningful_pre_session_content(first, stems, timelines)):
            # Leading acoustic chunks with no sustained instrument energy are
            # silence/analysis artifacts, not a song. Remove them so the
            # first real, Whisper-numbered segment remains SONG 1.
            return segments[first_index:]
        result = list(segments)
        for i in range(first_index):
            result[i] = replace(result[i], assigned_song_number=0,
                                boundary_source="pre-session-content")
        return result
    announcement = first.speech_intro_start
    if announcement is None or announcement <= first.start + 1.0:
        return segments
    has_content = True
    if timelines and stems:
        lo = max(0, int(first.start / DETECTION_FRAME_SECONDS))
        hi = max(lo + 1, int(announcement / DETECTION_FRAME_SECONDS))
        activity = []
        for stem in stems:
            if stem.role in {"vocal", "room"}:
                continue
            values = np.asarray(timelines.get(stem.path.name, np.array([], dtype=np.float32)))
            if len(values):
                activity.extend(values[lo:min(hi, len(values))].tolist())
        has_content = bool(activity and max(activity) > db_to_amp(-50.0))
    if not has_content:
        return segments
    pre = replace(first, end=announcement, nominal_end=announcement, core_end=announcement,
                  speech_text="", speech_intro_text="", speech_intro_start=None,
                  speech_confidence=0.0, spoken_song_number=None, assigned_song_number=0,
                  boundary_source="pre-session-content")
    formal = replace(first, start=announcement, core_start=announcement,
                     assigned_song_number=1)
    result = list(segments)
    result[first_index:first_index + 1] = [pre, formal]
    print(f"SONG 0: pre-session content {fmt_time(pre.start)}-{fmt_time(pre.end)} before spoken number one", flush=True)
    return result


def _has_meaningful_pre_session_content(
    first_formal_segment: Segment,
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
) -> bool:
    """Require sustained instrument energy before SONG 1 for SONG 0."""
    end = float(first_formal_segment.speech_intro_start or first_formal_segment.start)
    if end <= 1.0:
        return False
    hi = max(1, int(end / DETECTION_FRAME_SECONDS))
    minimum_active_seconds = 2.0
    for stem in stems:
        if stem.role in {"vocal", "room"}:
            continue
        values = np.asarray(timelines.get(stem.path.name, np.array([], dtype=np.float32)))[:hi]
        if len(values) and float(np.count_nonzero(values > db_to_amp(-50.0)) * DETECTION_FRAME_SECONDS) >= minimum_active_seconds:
            return True
    return False


def apply_spoken_number_structure(
    segments: list[Segment],
    transcripts: list[dict[str, object]],
    session_end: float,
    stems: list[Stem] | None = None,
    timelines: dict[str, np.ndarray] | None = None,
    voice_stems: list[Stem] | None = None,
    instrument_stems: list[Stem] | None = None,
    mc_data: dict[str, np.ndarray | dict[str, float]] | None = None,
) -> list[Segment]:
    """Make Whisper's explicit numbering an active structural constraint."""
    if not segments:
        return segments
    result = _split_pre_session_segment(segments, stems, timelines, transcripts)
    decisions: list[dict[str, object]] = []
    # A duplicate number on adjacent pieces is a split fragment, not a new song.
    index = 0
    while index < len(result) - 1:
        left, right = result[index], result[index + 1]
        if left.spoken_song_number is not None and left.spoken_song_number == right.spoken_song_number:
            merged = replace(left, end=right.end, nominal_end=right.end, core_end=right.core_end,
                             speech_text=right.speech_text or left.speech_text,
                             speech_intro_text=right.speech_intro_text or left.speech_intro_text,
                             speech_intro_start=right.speech_intro_start or left.speech_intro_start,
                             assigned_song_number=left.spoken_song_number)
            result[index:index + 2] = [merged]
            decisions.append({"action": "merge", "reason": "duplicate spoken song number", "number": left.spoken_song_number, "range": [left.start, right.end]})
            print(f"SPOKEN NUMBER: merged duplicate number {left.spoken_song_number} at {fmt_time(left.end)}", flush=True)
            continue
        index += 1

    numbered = [(i, int(seg.spoken_song_number)) for i, seg in enumerate(result) if seg.spoken_song_number is not None]
    for (left_index, left_number), (right_index, right_number) in zip(numbered, numbered[1:]):
        if right_number > left_number + 1:
            gap = {"action": "focused-rescan", "from_number": left_number, "to_number": right_number,
                   "missing_numbers": list(range(left_number + 1, right_number)),
                   "range": [result[left_index].end, result[right_index].start]}
            decisions.append(gap)
            print(f"SPOKEN NUMBER: gap {left_number}->{right_number}; focused rescan required for "
                  f"{fmt_time(result[left_index].end)}-{fmt_time(result[right_index].start)}", flush=True)
            if stems and timelines and voice_stems and instrument_stems and mc_data and right_index == left_index + 1:
                rescanned = focused_retry_split_oversized(
                    [Segment(result[left_index].end, result[right_index].start,
                             core_start=result[left_index].end, core_end=result[right_index].start)],
                    stems, timelines, voice_stems, instrument_stems, mc_data,
                )
                if len(rescanned) > 1:
                    # Only insert interior pieces. The surrounding numbered
                    # songs remain authoritative and retain their boundaries.
                    interior = [piece for piece in rescanned[1:-1]
                                if piece.end - piece.start >= MC_MIN_FINAL_SONG_SECONDS]
                    if interior:
                        result[left_index + 1:left_index + 1] = interior
                        gap.update({"action": "focused-rescan-inserted", "inserted_count": len(interior),
                                    "inserted_ranges": [[piece.start, piece.end] for piece in interior]})
                        print(f"SPOKEN NUMBER: inserted {len(interior)} focused-rescan piece(s) in gap "
                              f"{left_number}->{right_number}", flush=True)

    # Spoken numbers are absolute identities. Infer only the gaps between
    # confirmed anchors; never overwrite an explicit number with the physical
    # list position. This is the root mapping consumed by the app/UI/export.
    anchors = [(i, int(segment.spoken_song_number)) for i, segment in enumerate(result) if segment.spoken_song_number is not None]
    assigned: list[int] = [0] * len(result)
    for i, number in anchors:
        assigned[i] = number
    for i in range(len(result)):
        if assigned[i]:
            continue
        previous = [(j, assigned[j]) for j in range(i - 1, -1, -1) if assigned[j]]
        following = [(j, assigned[j]) for j in range(i + 1, len(result)) if assigned[j]]
        if previous and following:
            pj, pn = previous[0]
            fj, fn = following[0]
            if fn - pn == fj - pj:
                assigned[i] = pn + (i - pj)
            else:
                assigned[i] = pn + 1
        elif previous:
            assigned[i] = previous[0][1] + (i - previous[0][0])
        elif following:
            assigned[i] = following[0][1] - (following[0][0] - i)
        else:
            assigned[i] = i + 1
    for ordinal, segment in enumerate(result, 1):
        result[ordinal - 1] = replace(segment, assigned_song_number=assigned[ordinal - 1])
    # A nameless, introduction-less island between two numbered songs is an
    # organisational/dead chunk. Remove it rather than exporting it as music.
    removable: list[int] = []
    for i in range(1, len(result) - 1):
        segment = result[i]
        if segment.spoken_song_number is None and not segment.speech_intro_text and not segment.speech_text:
            before, after = result[i - 1], result[i + 1]
            if before.spoken_song_number is not None and after.spoken_song_number is not None:
                removable.append(i)
                decisions.append({"action": "remove", "reason": "unnumbered segment without introduction between numbered songs", "index": i + 1})
    if removable:
        for i in reversed(removable):
            print(f"SPOKEN NUMBER: removed dead unnumbered chunk at provisional song {i + 1}", flush=True)
            del result[i]
    DETECTION_STRATEGY["spoken_number_structure"] = decisions
    DETECTION_STRATEGY["spoken_number_assignments"] = [
        {"app_index": i, "spoken_number": seg.spoken_song_number, "assigned_number": seg.assigned_song_number}
        for i, seg in enumerate(result, 1)
    ]
    return result




def enforce_hard_boundary_gate(
    segments: list[Segment],
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
    session_end: float,
    whisper_segments: list[Segment] | None = None,
) -> list[Segment]:
    """Make the last boundary decision; a playing instrument can never pass."""
    if not segments:
        return segments
    whisper_targets = []
    for item in whisper_segments or []:
        whisper_targets.extend((float(item.start), float(item.end)))
    whisper_targets = sorted(set(float(value) for value in whisper_targets))
    result = list(segments)
    audit: list[dict[str, object]] = []

    def choose(target: float, lower: float, upper: float, source: str) -> float | None:
        acoustic_proposal = target
        nearest_whisper = min(whisper_targets, key=lambda value: abs(value - target), default=None)
        # A confirmed introduction is a speech boundary, not an acoustic
        # silence boundary.  The announcer is often speaking over guitar,
        # drums, or room bleed; moving this cut to the nearest multi-stem
        # quiet point strands the first words in the preceding export.  Keep
        # the exact Whisper lead-in target and reserve acoustic searching for
        # ordinary music-to-music cuts.
        if source == "shared-introduction-boundary":
            valid, instruments, voices = _boundary_activity(
                target, stems, timelines, radius_seconds=2.0, diagnostic=True
            )
            audit.append({
                "whisper_timestamp": nearest_whisper,
                "whisper_target": target,
                "acoustic_proposed": acoustic_proposal,
                "actual_used": target,
                "source": source,
                "instrument_activity_at_target": instruments,
                "vocal_activity_at_target": voices,
                "valid_at_target": valid,
                "authoritative": True,
            })
            print(
                f"BOUNDARY AUDIT source={source} whisper={fmt_time(nearest_whisper) if nearest_whisper is not None else 'none'} "
                f"acoustic={fmt_time(acoustic_proposal)} actual={fmt_time(target)} "
                f"target_instruments={instruments} target_voices={voices} valid={valid} authoritative=True",
                flush=True,
            )
            return target
        if source != "shared-introduction-boundary" and nearest_whisper is not None and abs(nearest_whisper - target) <= 180.0:
            target = nearest_whisper
        valid, instruments, voices = _boundary_activity(target, stems, timelines, radius_seconds=2.0, diagnostic=True)
        actual = target if valid else _nearest_valid_boundary(
            target, lower, upper, stems, timelines, search_seconds=120.0, validation_radius_seconds=2.0
        )[0]
        audit.append({
            "whisper_timestamp": nearest_whisper,
            "whisper_target": target if nearest_whisper is not None and abs(nearest_whisper - target) < 0.01 else None,
            "acoustic_proposed": acoustic_proposal,
            "actual_used": actual,
            "source": source,
            "instrument_activity_at_target": instruments,
            "vocal_activity_at_target": voices,
            "valid_at_target": valid,
        })
        print(
            f"BOUNDARY AUDIT source={source} whisper={fmt_time(nearest_whisper) if nearest_whisper is not None else 'none'} "
            f"acoustic={fmt_time(acoustic_proposal)} actual={fmt_time(actual) if actual is not None else 'REJECTED'} "
            f"target_instruments={instruments} target_voices={voices} valid={valid}", flush=True,
        )
        return actual

    for index, segment in enumerate(result):
        lower = result[index - 1].end + 1.0 if index else 0.0
        upper = result[index + 1].start - 1.0 if index + 1 < len(result) else session_end
        actual = choose(segment.start, lower, min(segment.end, upper), "start")
        if actual is None:
            actual = segment.start
            audit[-1]["rejected"] = True
        result[index] = replace(segment, start=actual, core_start=actual,
                                boundary_validation=("checked" if actual == segment.start else "corrected"),
                                boundary_validation_reason="hard gate: no instrument activity")
    for index, segment in enumerate(result):
        lower = result[index - 1].end + 1.0 if index else segment.start + MIN_SONG_SECONDS
        upper = result[index + 1].start - 1.0 if index + 1 < len(result) else session_end
        actual = choose(segment.end, max(segment.start + MIN_SONG_SECONDS, lower), upper, "end")
        if actual is None:
            actual = segment.end
            audit[-1]["rejected"] = True
        result[index] = replace(segment, end=actual, nominal_end=actual, core_end=actual,
                                boundary_validation=("checked" if actual == segment.end else "corrected"),
                                boundary_validation_reason="hard gate: no instrument activity")
    # Reconcile every transition as one shared edge.  Independent start/end
    # passes can otherwise leave the introduction in the preceding file.
    shared_boundaries: list[dict[str, object]] = []
    for index in range(len(result) - 1):
        left, right = result[index], result[index + 1]
        intro_first_word = right.speech_intro_start
        lead_in = 0.5 if intro_first_word is not None else 0.0
        target = (intro_first_word - lead_in) if intro_first_word is not None else right.start
        actual = choose(target, left.start + MIN_SONG_SECONDS, right.end - MIN_SONG_SECONDS, "shared-introduction-boundary")
        if actual is None:
            actual = target
        result[index] = replace(left, end=actual, nominal_end=actual, core_end=actual)
        result[index + 1] = replace(right, start=actual, core_start=actual)
        shared_boundaries.append({
            "intro_first_word_timestamp": intro_first_word,
            "previous_song_end": actual,
            "next_song_start": actual,
            "intentional_lead_in_seconds": lead_in,
            "matches": intro_first_word is None or abs(actual - target) <= DETECTION_FRAME_SECONDS,
        })
        print(
            f"BOUNDARY MAPPING pair={index + 1}/{index + 2} intro_first_word={fmt_time(intro_first_word) if intro_first_word is not None else 'none'} "
            f"previous_end={fmt_time(actual)} next_start={fmt_time(actual)} lead_in={lead_in:.1f}s",
            flush=True,
        )
    DETECTION_STRATEGY["shared_boundary_mapping"] = shared_boundaries
    DETECTION_STRATEGY["boundary_audit"] = audit
    DETECTION_STRATEGY["hard_gate_rejections"] = sum(bool(item.get("rejected")) for item in audit)
    return result


def validate_final_render_boundaries(
    stems: list[Stem],
    segments: list[Segment],
    song_numbers: list[int] | None = None,
    timelines: dict[str, np.ndarray] | None = None,
) -> list[dict[str, object]]:
    """Non-bypassable last check before any render is allowed.

    It inspects the same global, offset-aware envelope timeline used by
    detection. Unsafe cuts are returned as review rows; they are never
    softened and this function never raises a batch-fatal exception.

    A nearby replacement is accepted only when the replacement is on the
    same global timeline, quiet on every stem, and leaves an 8--13 minute
    window. The caller must apply accepted_cut_seconds to the shared edge of
    the adjacent windows before rendering.
    """
    audit: list[dict[str, object]] = []
    numbers = song_numbers or list(range(1, len(segments) + 1))
    if not stems:
        return audit
    shared_timelines = timelines if timelines is not None else load_cached_timelines_or_die(stems, "Final cut gate")
    session_end = max(float(stem.offset_seconds + stem.timeline_duration) for stem in stems)
    for index, segment in enumerate(segments):
        cut = float(segment.end)
        number = int(numbers[index])
        if segment.boundary_validation == "needs_review":
            audit.append({
                "song": number,
                "cut_seconds": cut,
                "cut_sample": int(round(cut * stems[0].samplerate)),
                "sample_rate": int(stems[0].samplerate),
                "active_instruments": [],
                "active_voices": [],
                "safe": False,
                "needs_review": True,
                "reason": segment.boundary_validation_reason or "commentator-led slot requires Select Cuts review",
                "duration_seconds": float(segment.duration),
            })
            continue
        min_cut = float(segment.start) + HARD_MIN_SONG_SECONDS
        max_cut = min(float(segment.start) + HARD_MAX_SONG_SECONDS, session_end)
        if index + 1 < len(segments):
            next_segment = segments[index + 1]
            min_cut = max(min_cut, float(next_segment.end) - HARD_MAX_SONG_SECONDS)
            max_cut = min(max_cut, float(next_segment.end) - HARD_MIN_SONG_SECONDS)
        safe, active_instruments, active_voices = _boundary_activity(
            cut, stems, shared_timelines, radius_seconds=3.0, diagnostic=True
        )
        slot_duration_valid = HARD_MIN_SONG_SECONDS <= cut - float(segment.start) <= HARD_MAX_SONG_SECONDS
        # A commentator edge is authoritative.  Instrument activity at that
        # edge is diagnostic and must not move the slot into the next
        # presentation; only the slot's confidence/duration can block it.
        semantic_slot_edge = segment.boundary_source == "whisper-presented-slot"
        accepted_cut = cut if (slot_duration_valid and (safe or semantic_slot_edge)) else None
        search = {"lower": min_cut, "upper": max_cut, "radius_seconds": 120.0}
        if accepted_cut is None and not semantic_slot_edge and max_cut >= min_cut:
            candidate, candidate_instruments, candidate_voices = _nearest_valid_boundary(
                cut, min_cut, max_cut, stems, shared_timelines,
                search_seconds=120.0, validation_radius_seconds=3.0,
            )
            if candidate is not None:
                accepted_cut = float(candidate)
                active_instruments, active_voices = candidate_instruments, candidate_voices
        if segment.boundary_source == "metadata-provisional":
            audit.append({
                "song": number,
                "cut_seconds": cut,
                "cut_sample": int(round(cut * stems[0].samplerate)),
                "active_instruments": [],
                "safe": False,
                "needs_review": True,
                "reason": "Whisper unavailable; metadata-only proposal requires Select Cuts review",
            })
            continue
        active = list(active_instruments)
        safe_final = accepted_cut is not None
        row = {
            "song": number,
            "cut_seconds": cut,
            "accepted_cut_seconds": accepted_cut,
            "cut_sample": int(round(cut * stems[0].samplerate)),
            "accepted_cut_sample": int(round(accepted_cut * stems[0].samplerate)) if accepted_cut is not None else None,
            "sample_rate": int(stems[0].samplerate),
            "per_stem_samples": {stem.path.name: int(round((cut - stem.offset_seconds) * stem.samplerate)) for stem in stems},
            "active_instruments": active,
            "active_voices": list(active_voices),
            "safe": safe_final,
            "needs_review": not safe_final,
            "search": search,
            "duration_seconds": float(segment.duration),
            "accepted_duration_seconds": (accepted_cut - float(segment.start)) if accepted_cut is not None else None,
        }
        if not safe_final:
            row["reason"] = f"active {', '.join(active) if active else 'music'} at proposed end"
        audit.append(row)
        print(
            f"FINAL CUT GATE {'PASSED' if safe_final else 'REJECTED'}: song {number} "
            f"proposed={fmt_time(cut)} accepted={fmt_time(accepted_cut) if accepted_cut is not None else 'needs_review'} "
            f"active={active}", flush=True,
        )
    return audit




def enforce_hard_song_duration_bounds(
    segments: list[Segment],
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
    voice_stems: list[Stem],
    instrument_stems: list[Stem],
    mc_data: dict[str, np.ndarray | dict[str, float]],
) -> list[Segment]:
    """Repair transcript windows to the session's explicit 8--16 minute contract."""
    result = list(segments)
    decisions: list[dict[str, object]] = []
    # Merge short windows with the nearest neighbor. These are detection
    # artifacts, not valid songs in this session.
    while True:
        short_index = next((i for i, item in enumerate(result) if item.duration < HARD_MIN_SONG_SECONDS), None)
        if short_index is None or len(result) <= 1:
            break
        if short_index == 0:
            neighbor = 1
        elif short_index == len(result) - 1:
            neighbor = short_index - 1
        else:
            neighbor = short_index - 1 if result[short_index - 1].duration <= result[short_index + 1].duration else short_index + 1
        left_index, right_index = sorted((short_index, neighbor))
        left, right = result[left_index], result[right_index]
        merged = replace(left, end=right.end, nominal_end=right.end, core_end=right.core_end)
        result[left_index:right_index + 1] = [merged]
        decisions.append({"action": "merge", "reason": "hard minimum duration", "source_indexes": [short_index + 1, neighbor + 1], "duration_seconds": merged.duration})

    # A very long transcript window is normally several songs joined by a
    # missed introduction. Reuse the focused drum/activity rescanner, then
    # retain only candidates that satisfy the hard contract.
    for item in list(result):
        if item.duration <= HARD_MAX_SONG_SECONDS:
            continue
        candidates = focused_retry_split_oversized([item], stems, timelines, voice_stems, instrument_stems, mc_data)
        valid = [candidate for candidate in candidates if HARD_MIN_SONG_SECONDS <= candidate.duration <= HARD_MAX_SONG_SECONDS]
        if len(valid) <= 1:
            safe_parts = split_oversized_at_safe_boundaries(item, stems, timelines)
            if len(safe_parts) > len(valid):
                valid = safe_parts
                decisions.append({
                    "action": "safe-envelope-split",
                    "reason": "bounded search found quiet multi-stem boundaries",
                    "source_duration_seconds": item.duration,
                    "result_durations_seconds": [candidate.duration for candidate in valid],
                    "split_points": [candidate.end for candidate in valid[:-1]],
                })
        if len(valid) <= 1:
            # The tight drum-gap sweep can return only an active fragment when
            # the moderator talks over the transition. Apply the same
            # stage-clock quiet-point fallback to the original oversized
            # window, not to that fragment, so a valid 10–16 minute split is
            # still found when Whisper's internal announcement boundary fails
            # the full-band acoustic gate.
            stage_clock_candidates = apply_stage_clock_soft_splits([item], stems, timelines)
            stage_clock_valid = [
                candidate for candidate in stage_clock_candidates
                if HARD_MIN_SONG_SECONDS <= candidate.duration <= HARD_MAX_SONG_SECONDS
            ]
            if len(stage_clock_valid) > len(valid):
                valid = stage_clock_valid
                decisions.append({
                    "action": "stage-clock-fallback-split",
                    "reason": "focused drum sweep returned no valid multi-piece split",
                    "source_duration_seconds": item.duration,
                    "result_durations_seconds": [candidate.duration for candidate in valid],
                    "split_points": [candidate.end for candidate in valid[:-1]],
                })
        if len(valid) <= 1 and item.duration > HARD_MAX_SONG_SECONDS:
            # A long block must never be silently kept as one "song".  If no
            # acoustically verified cut exists, expose the bounded acoustic
            # proposals as needs_review so Select Cuts can decide.  This is a
            # review proposal, not an automatically approved export window.
            review_candidates = apply_stage_clock_soft_splits([item], stems, timelines)
            if len(review_candidates) > 1:
                valid = mark_partition_for_review(review_candidates, stems, timelines)
                decisions.append({
                    "action": "review-only-split",
                    "reason": "no safe internal boundary; preserve proposals for Select Cuts",
                    "source_duration_seconds": item.duration,
                    "result_durations_seconds": [candidate.duration for candidate in valid],
                    "split_points": [candidate.end for candidate in valid[:-1]],
                })
        if len(valid) > 1:
            # Preserve the original introduction metadata on the first piece;
            # later inferred pieces remain explicitly unintroduced unless a
            # separate Whisper boundary was verified for them.
            first_intro_start = None
            if item.speech_intro_text and item.speech_intro_start is not None:
                proposed_intro_start = max(0.0, float(item.speech_intro_start) - 0.5)
                if proposed_intro_start < valid[0].start:
                    first_intro_start = proposed_intro_start
                    source_index = result.index(item)
                    if source_index > 0:
                        previous = result[source_index - 1]
                        result[source_index - 1] = replace(
                            previous,
                            end=proposed_intro_start,
                            nominal_end=proposed_intro_start,
                            core_end=proposed_intro_start,
                        )
                    valid[0] = replace(valid[0], start=proposed_intro_start, core_start=proposed_intro_start)
                    decisions.append({
                        "action": "restore-introduction-lead-in",
                        "start": proposed_intro_start,
                        "intro_first_word": float(item.speech_intro_start),
                    })
            preserved = [replace(candidate, **{
                "speech_text": item.speech_text,
                "speech_reason": item.speech_reason,
                "speech_intro_text": item.speech_intro_text,
                "speech_intro_start": item.speech_intro_start,
                "speech_confidence": item.speech_confidence,
                "spoken_song_number": item.spoken_song_number,
                "spoken_last": item.spoken_last,
                "musician_labels": item.musician_labels,
            }) if index == 0 else candidate for index, candidate in enumerate(valid)]
            result[result.index(item):result.index(item) + 1] = preserved
            decisions.append({"action": "split", "reason": "hard maximum duration", "source_duration_seconds": item.duration, "result_durations_seconds": [candidate.duration for candidate in valid]})
        else:
            decisions.append({"action": "unresolved", "reason": "hard maximum duration", "duration_seconds": item.duration, "candidate_count": len(valid)})
    # If a close-to-limit window has no reliable internal activity boundary,
    # preserve the source boundary.  Moving a cut to an arbitrary 16-minute
    # timestamp can place it in the middle of active playing and will be
    # rejected by the final render gate.  A duration violation is preferable
    # to inventing a mid-performance cut; the full-session rescanner above is
    # responsible for regions where safe internal cuts actually exist.
    for i, item in enumerate(list(result)):
        if item.duration <= HARD_MAX_SONG_SECONDS:
            continue
        decisions.append({"action": "preserve-safe-boundary", "reason": "no validated internal cut; do not cut active playing", "song_position": i + 1, "duration_seconds": item.duration})
    result = sorted(result, key=lambda item: item.start)
    violations = [round(item.duration, 2) for item in result if not HARD_MIN_SONG_SECONDS <= item.duration <= HARD_MAX_SONG_SECONDS]
    DETECTION_STRATEGY["hard_duration_validation"] = {
        "bounds_seconds": [HARD_MIN_SONG_SECONDS, HARD_MAX_SONG_SECONDS],
        "expected_count": EXPECTED_SONG_COUNT,
        "original_count": len(segments),
        "final_count": len(result),
        "decisions": decisions,
        "violations_seconds": violations,
        "count_matches_expected": EXPECTED_SONG_COUNT is None or len(result) == EXPECTED_SONG_COUNT,
    }
    for decision in decisions:
        print("HARD DURATION VALIDATION: " + str(decision), flush=True)
    print(f"HARD DURATION VALIDATION: final_count={len(result)} expected={EXPECTED_SONG_COUNT or 'not configured'} violations={violations}", flush=True)
    return result


def split_oversized_at_safe_boundaries(
    segment: Segment,
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
) -> list[Segment]:
    """Partition a long proposal only at verified quiet multi-stem points.

    This is a bounded acoustic search, not a clock split.  If no safe point
    exists in the legal interval, the original proposal is preserved for
    Select Cuts review rather than cutting active music.
    """
    pieces = [segment]
    while True:
        index = next((i for i, item in enumerate(pieces) if item.duration > HARD_MAX_SONG_SECONDS), None)
        if index is None:
            return pieces if len(pieces) > 1 else []
        item = pieces[index]
        lower = item.start + HARD_MIN_SONG_SECONDS
        upper = item.start + HARD_MAX_SONG_SECONDS
        if upper >= item.end - HARD_MIN_SONG_SECONDS:
            return pieces if len(pieces) > 1 else []
        target = min(item.start + 600.0, item.end - HARD_MIN_SONG_SECONDS)
        cut, _instruments, _voices = _nearest_valid_boundary(
            target,
            lower,
            upper,
            stems,
            timelines,
            search_seconds=180.0,
            validation_radius_seconds=3.0,
        )
        if cut is None:
            return pieces if len(pieces) > 1 else []
        left = replace(
            item,
            end=cut,
            nominal_end=cut,
            core_end=cut,
            boundary_source="acoustic-safe-boundary",
            boundary_validation="valid",
            boundary_validation_reason="quiet multi-stem boundary verified",
        )
        right = replace(
            item,
            start=cut,
            core_start=cut,
            boundary_source="acoustic-safe-boundary",
            boundary_validation="valid",
            boundary_validation_reason="quiet multi-stem boundary verified",
            speech_text="",
            speech_intro_text="",
            speech_intro_start=None,
            speech_reason="inferred acoustic continuation; review if presenter context is ambiguous",
        )
        pieces[index:index + 1] = [left, right]


def mark_partition_for_review(
    pieces: list[Segment],
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
) -> list[Segment]:
    """Annotate a bounded partition that still needs human confirmation.

    Soft acoustic proposals are useful for exposing a 40-minute recording as
    reviewable song-sized windows, but they are never silently promoted to
    valid cuts.  Each shared boundary is checked and every affected window
    carries the review state into Select Cuts and the render manifest.
    """
    ordered = sorted(pieces, key=lambda item: item.start)
    unsafe: list[float] = []
    for right in ordered[1:]:
        safe, instruments, voices = _boundary_activity(
            float(right.start), stems, timelines, radius_seconds=3.0, diagnostic=True
        )
        if not safe:
            unsafe.append(float(right.start))
    annotated: list[Segment] = []
    for index, item in enumerate(ordered):
        boundary = float(item.start) if index else None
        is_review = bool(unsafe) or boundary in unsafe
        annotated.append(replace(
            item,
            boundary_source="needs-review-acoustic-proposal" if is_review else "acoustic-safe-boundary",
            boundary_validation="needs_review" if is_review else "valid",
            boundary_validation_reason=(
                "soft partition requires human confirmation" if is_review
                else "quiet multi-stem boundary verified"
            ),
            speech_reason=(item.speech_reason or "") + (
                "; proposed split requires Select Cuts confirmation" if is_review else ""
            ),
        ))
    return annotated


def bounded_review_partition(
    segment: Segment,
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
) -> list[Segment]:
    """Create a bounded, shared proposal partition for one long block.

    The search is deliberately variable: it chooses the quietest aggregate
    multi-stem point in the legal interval rather than slicing at a fixed
    ten-minute clock position.  A point that is not acoustically safe is still
    exposed, but marked ``needs_review`` for Select Cuts.
    """
    pieces = [segment]
    combined = combine_normalized_envelope(stems, timelines)
    while True:
        index = next((i for i, item in enumerate(pieces) if item.duration > HARD_MAX_SONG_SECONDS), None)
        if index is None:
            break
        item = pieces[index]
        lower = item.start + HARD_MIN_SONG_SECONDS
        upper = min(item.start + HARD_MAX_SONG_SECONDS, item.end - HARD_MIN_SONG_SECONDS)
        if upper <= lower:
            cut = min(item.start + HARD_MIN_SONG_SECONDS, item.end - 1.0)
        elif len(combined):
            lo = max(0, int(round(lower / DETECTION_FRAME_SECONDS)))
            hi = min(len(combined), int(round(upper / DETECTION_FRAME_SECONDS)) + 1)
            cut = float(lo + int(np.argmin(combined[lo:hi]))) * DETECTION_FRAME_SECONDS if hi > lo else (lower + upper) / 2.0
        else:
            cut = (lower + upper) / 2.0
        cut = min(max(cut, item.start + HARD_MIN_SONG_SECONDS), item.end - 1.0)
        left = replace(item, end=cut, core_end=cut, nominal_end=cut)
        right = replace(
            item,
            start=cut,
            core_start=cut,
            speech_text="",
            speech_reason="bounded continuation proposal; review in Select Cuts",
            speech_intro_text="",
            speech_intro_start=None,
        )
        pieces[index:index + 1] = [left, right]
    return mark_partition_for_review(pieces, stems, timelines)


def normalize_global_song_windows(
    segments: list[Segment],
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
) -> list[Segment]:
    """Enforce one legal global topology after all detector passes."""
    partitioned: list[Segment] = []
    audits: list[dict[str, object]] = []
    for segment in sorted(segments, key=lambda item: item.start):
        if segment.duration > HARD_MAX_SONG_SECONDS:
            safe = split_oversized_at_safe_boundaries(segment, stems, timelines)
            parts = safe if len(safe) > 1 else bounded_review_partition(segment, stems, timelines)
            partitioned.extend(parts)
            audits.append({
                "source_start": segment.start,
                "source_end": segment.end,
                "durations": [part.duration for part in parts],
                "mode": "safe" if len(safe) > 1 else "needs_review",
            })
        else:
            partitioned.append(segment)

    # Whisper can produce brief commentary fragments. Remove those artificial
    # song boundaries where the resulting shared window remains legal.
    result = sorted(partitioned, key=lambda item: item.start)
    while True:
        short_index = next((i for i, item in enumerate(result) if item.duration < HARD_MIN_SONG_SECONDS), None)
        if short_index is None or len(result) <= 1:
            break
        options: list[tuple[float, int, int]] = []
        if short_index > 0:
            options.append((result[short_index - 1].duration + result[short_index].duration, short_index - 1, short_index))
        if short_index + 1 < len(result):
            options.append((result[short_index].duration + result[short_index + 1].duration, short_index, short_index + 1))
        legal = [option for option in options if option[0] <= HARD_MAX_SONG_SECONDS]
        if not legal:
            break
        _duration, left_index, right_index = min(legal)
        left, right = result[left_index], result[right_index]
        merged = replace(
            left,
            end=right.end,
            nominal_end=right.end,
            core_end=right.core_end or right.end,
            boundary_source="merged-commentary-fragments",
            boundary_validation="needs_review" if left.boundary_validation == "needs_review" or right.boundary_validation == "needs_review" else "valid",
            boundary_validation_reason="short commentary fragment merged into shared song window",
            speech_reason=(left.speech_reason or "") + "; short fragment merged; review if presenter context is ambiguous",
        )
        result[left_index:right_index + 1] = [merged]

    total_end = max((item.end for item in result), default=0.0)
    minimum_count = max(1, int(math.ceil(total_end / HARD_MAX_SONG_SECONDS)))
    duration_target_count = max(minimum_count, int(round(total_end / 750.0)))
    target_count = max(minimum_count, int(KNOWN_SONG_COUNT or duration_target_count))
    bounded_rebuild = False
    if len(result) != target_count or any(not HARD_MIN_SONG_SECONDS <= item.duration <= HARD_MAX_SONG_SECONDS for item in result):
        result = build_duration_bounded_global_proposals(result, stems, timelines, target_count)
        bounded_rebuild = True

    DETECTION_STRATEGY["global_window_normalization"] = {
        "input_count": len(segments),
        "output_count": len(result),
        "expected_window_seconds": [HARD_MIN_SONG_SECONDS, HARD_MAX_SONG_SECONDS],
        "long_block_partitions": audits,
        "shared_stem_cut_list": True,
        "out_of_range_count": sum(not HARD_MIN_SONG_SECONDS <= item.duration <= HARD_MAX_SONG_SECONDS for item in result),
        "needs_review_count": sum(item.boundary_validation == "needs_review" for item in result),
        "bounded_rebuild": bounded_rebuild,
        "bounded_target_count": target_count,
    }
    return result


def build_duration_bounded_global_proposals(
    existing: list[Segment],
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
    target_count: int,
) -> list[Segment]:
    """Build one feasible global partition when detector fragments disagree.

    This is not a fixed-duration splitter.  The number of windows is derived
    from the shared active-session duration and the legal maximum; each cut is
    then moved to the quietest aggregate point in its feasible interval.
    """
    if not existing or target_count < 1:
        return existing
    start = min(item.start for item in existing)
    end = max(item.end for item in existing)
    if end - start < target_count * HARD_MIN_SONG_SECONDS or end - start > target_count * HARD_MAX_SONG_SECONDS:
        return existing
    combined = combine_normalized_envelope(stems, timelines)
    existing_cuts = sorted({float(item.start) for item in existing if start < float(item.start) < end})
    cuts: list[float] = [start]
    cursor = start
    for index in range(1, target_count):
        remaining = target_count - index
        lower = max(cursor + HARD_MIN_SONG_SECONDS, end - remaining * HARD_MAX_SONG_SECONDS)
        upper = min(cursor + HARD_MAX_SONG_SECONDS, end - remaining * HARD_MIN_SONG_SECONDS)
        desired = cursor + (end - cursor) / float(remaining + 1)
        candidates = [candidate for candidate in existing_cuts if lower <= candidate <= upper]
        if candidates:
            # Prefer a real detector boundary close to the proportional target
            # and give a modest preference to a boundary that passes the
            # shared multi-stem acoustic gate.  This avoids synthetic 13:00
            # cuts when Whisper/energy already supplied a usable candidate.
            scored: list[tuple[float, float]] = []
            for candidate in candidates:
                safe, _instruments, _voices = _boundary_activity(candidate, stems, timelines, radius_seconds=3.0, diagnostic=True)
                scored.append((abs(candidate - desired) + (0.0 if safe else 60.0), candidate))
            cut = min(scored)[1]
        elif upper <= lower:
            cut = lower
        elif len(combined):
            lo = max(0, int(round(lower / DETECTION_FRAME_SECONDS)))
            hi = min(len(combined), int(round(upper / DETECTION_FRAME_SECONDS)) + 1)
            cut = float(lo + int(np.argmin(combined[lo:hi]))) * DETECTION_FRAME_SECONDS if hi > lo else (lower + upper) / 2.0
        else:
            cut = (lower + upper) / 2.0
        cut = min(max(cut, lower), upper)
        cuts.append(cut)
        cursor = cut
    cuts.append(end)

    proposals: list[Segment] = []
    for index, (left, right) in enumerate(zip(cuts, cuts[1:])):
        source = min(existing, key=lambda item: abs(item.start - left))
        safe = True
        reason = "global aggregate boundary verified"
        if index:
            safe, instruments, voices = _boundary_activity(left, stems, timelines, radius_seconds=3.0, diagnostic=True)
            if not safe:
                reason = f"needs review: active stems at proposal ({', '.join(instruments + voices)})"
        proposals.append(replace(
            Segment(
                left,
                right,
                core_start=left,
                core_end=right,
                nominal_end=right,
                boundary_source="global-acoustic-proposal",
                boundary_validation="valid" if safe else "needs_review",
                boundary_validation_reason=reason,
                speech_text=source.speech_text if index == 0 else "",
                speech_reason=source.speech_reason if index == 0 else "global proposal; review introduction context",
                speech_intro_text=source.speech_intro_text if index == 0 else "",
                speech_intro_start=source.speech_intro_start if index == 0 else None,
                speech_confidence=source.speech_confidence if index == 0 else 0.0,
            ),
            boundary_source="global-acoustic-proposal",
        ))
    DETECTION_STRATEGY["global_bounded_partition"] = {
        "target_count": target_count,
        "session_start": start,
        "session_end": end,
        "durations": [item.duration for item in proposals],
        "cut_points": cuts[1:-1],
        "shared_across_stems": True,
        "needs_review_count": sum(item.boundary_validation == "needs_review" for item in proposals),
    }
    return proposals


def preserve_content_coverage(
    segments: list[Segment],
    transcripts: list[dict[str, object]],
    session_end: float,
) -> list[Segment]:
    """Assign every source interval to a neighboring or new song window.

    Detection candidates explain boundaries; they do not own the source
    timeline.  A retry may therefore not replace an interval with only the
    active pieces it happened to find.  This pass restores leading/trailing
    coverage and creates a new window for a confirmed trailing introduction.
    Unconfirmed gaps remain attached to the preceding window.
    """
    if not segments or session_end <= 0:
        return segments
    result = sorted(list(segments), key=lambda item: (float(item.start), float(item.end)))
    decisions: list[dict[str, object]] = []

    if result[0].start > 0.0:
        decisions.append({"action": "assign-leading", "from": result[0].start, "to": 0.0})
        result[0] = replace(result[0], start=0.0, core_start=0.0)

    # Prefer a confirmed introduction for the uncovered tail.  Otherwise the
    # tail is explicitly appended to the last song.  Both branches assign it.
    last = result[-1]
    trailing = [
        item for item in transcripts
        if item.get("announcement") and item.get("text")
        and float(item.get("announcement_start", item.get("start", session_end))) > last.end
    ]
    if trailing:
        item = min(trailing, key=lambda value: float(value.get("announcement_start", value.get("start", session_end))))
        intro_start = min(max(float(item.get("announcement_start", item.get("start", last.end))), last.end), session_end)
        if intro_start > last.end:
            result[-1] = replace(last, end=intro_start, nominal_end=intro_start, core_end=intro_start)
        labels = tuple(sorted((str(k), str(v)) for k, v in dict(item.get("musician_labels") or {}).items()))
        new_segment = Segment(
            intro_start, session_end, core_start=intro_start, core_end=session_end,
            nominal_end=session_end, boundary_source="whisper-trailing-content",
            speech_text=str(item.get("text") or ""),
            speech_reason=str(item.get("announcement_reason") or ""),
            speech_intro_text=str(item.get("speech_intro_text") or str(item.get("text") or "")),
            speech_intro_start=float(item.get("intro_first_word_timestamp", intro_start)),
            speech_confidence=float(item.get("speech_confidence") or 0.0),
            spoken_song_number=item.get("spoken_song_number"),
            spoken_last=bool(item.get("spoken_last")), musician_labels=labels,
        )
        result.append(new_segment)
        decisions.append({"action": "assign-trailing-new-song", "start": intro_start, "end": session_end})
    elif last.end < session_end:
        result[-1] = replace(last, end=session_end, nominal_end=session_end, core_end=session_end)
        decisions.append({"action": "assign-trailing-to-previous", "from": last.end, "to": session_end})

    # Any interior interval is assigned to the preceding window, preserving
    # the following window's start and preventing silent holes.
    for index in range(1, len(result)):
        previous, current = result[index - 1], result[index]
        if current.start > previous.end:
            decisions.append({"action": "assign-interior-gap-to-previous", "from": previous.end, "to": current.start})
            result[index - 1] = replace(previous, end=current.start, nominal_end=current.start, core_end=current.start)

    DETECTION_STRATEGY["content_coverage"] = {
        "decisions": decisions,
        "covered_start": result[0].start,
        "covered_end": result[-1].end,
        "session_end": session_end,
    }
    for decision in decisions:
        print("CONTENT COVERAGE: " + str(decision), flush=True)
    return result


def split_oversized_regions_full_pass(
    segments: list[Segment],
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
    voice_stems: list[Stem],
    instrument_stems: list[Stem],
    mc_data: dict[str, np.ndarray | dict[str, float]],
) -> list[Segment]:
    """Run the complete acoustic/duration pass over restored coverage.

    Coverage restoration deliberately happens late so uncertain source audio
    is never dropped.  That also means a newly restored trailing region must
    be rescanned here; attaching it to the previous song would otherwise turn
    a whole free-for-all hour into one render.  Candidate cuts come from the
    drum-core sweep (including long quiet gaps), with a quiet-point stage-clock
    fallback when the pause is too sparse for the binary activity mask.
    """
    if not segments:
        return segments
    result: list[Segment] = []
    decisions: list[dict[str, object]] = []
    drum_stems = drum_equivalent_stems(stems)
    drum_energy = None
    if drum_stems:
        drum_energy = relative_db(
            smooth_envelope(combine_normalized_envelope(drum_stems, timelines), DETECTION_SMOOTH_SECONDS)
        )
    combined = relative_db(smooth_envelope(combine_normalized_envelope(stems, timelines), 5.0))

    for original in sorted(segments, key=lambda item: item.start):
        duration = original.duration
        if duration <= HARD_MAX_SONG_SECONDS:
            result.append(original)
            continue

        # Existing transcript windows have already gone through their
        # established retry/validation pipeline.  The late coverage pass
        # must force-rescan only the newly restored uncertain tail; otherwise
        # it would replace an older, already-calibrated window with synthetic
        # cuts that can fail the final render gate.
        if original.boundary_source != "whisper-trailing-content":
            result.append(original)
            continue

        candidates = focused_retry_split_oversized(
            [original], stems, timelines, voice_stems, instrument_stems, mc_data
        )
        valid = [
            candidate for candidate in candidates
            if HARD_MIN_SONG_SECONDS <= candidate.duration <= HARD_MAX_SONG_SECONDS
        ]
        if len(valid) < 2 and drum_energy is not None:
            lo = max(0, int(round(original.start / DETECTION_FRAME_SECONDS)))
            hi = min(len(drum_energy), int(round(original.end / DETECTION_FRAME_SECONDS)) + 1)
            for threshold in (-5.0, -8.0, -10.0, -12.0, -15.0):
                for gap in (4.0, 8.0, 12.0, 20.0, 30.0):
                    pieces = active_mask_to_segments(drum_energy[lo:hi] > threshold, gap)
                    probe = [
                        Segment(
                            original.start + piece.start,
                            min(original.end, original.start + piece.end),
                            core_start=original.start + piece.start,
                            core_end=min(original.end, original.start + piece.end),
                            nominal_end=min(original.end, original.start + piece.end),
                            boundary_source="drum-core-gap",
                        )
                        for piece in pieces
                        if HARD_MIN_SONG_SECONDS <= piece.duration <= HARD_MAX_SONG_SECONDS
                    ]
                    if len(probe) > len(valid):
                        valid = probe

        if len(valid) < 2:
            # Deterministic duration-safe fallback.  Choose the lowest-energy
            # point near each ~12-minute target so long pauses are preferred,
            # while guaranteeing that no restored region remains >16 minutes.
            count = max(2, int(math.ceil(duration / (10.0 * 60.0))))
            cuts: list[float] = []
            for part in range(1, count):
                target = original.start + duration * part / count
                radius = min(90.0, duration / (count * 2.0))
                lower = max(original.start + HARD_MIN_SONG_SECONDS, target - radius)
                upper = min(original.end - HARD_MIN_SONG_SECONDS, target + radius)
                if len(combined):
                    lo = max(0, int(round(lower / DETECTION_FRAME_SECONDS)))
                    hi = min(len(combined), int(round(upper / DETECTION_FRAME_SECONDS)) + 1)
                    cut = float(np.argmin(combined[lo:hi]) + lo) * DETECTION_FRAME_SECONDS if hi > lo else target
                else:
                    cut = target
                # Envelope minima can still contain a sustained bass/piano
                # bed. Prefer the nearest boundary that passes the same
                # multi-instrument safety test used before rendering, while
                # keeping every resulting piece within 8--16 minutes.
                safe_cut, _active_instruments, _active_voices = _nearest_valid_boundary(
                    cut,
                    (cuts[-1] if cuts else original.start) + HARD_MIN_SONG_SECONDS,
                    original.end - HARD_MIN_SONG_SECONDS * (count - part),
                    stems,
                    timelines,
                    search_seconds=min(900.0, duration),
                    validation_radius_seconds=2.0,
                )
                if safe_cut is not None:
                    cut = safe_cut
                if cuts and cut <= cuts[-1] + HARD_MIN_SONG_SECONDS:
                    cut = cuts[-1] + HARD_MIN_SONG_SECONDS
                legal_lower = (cuts[-1] if cuts else original.start) + HARD_MIN_SONG_SECONDS
                legal_upper = original.end - HARD_MIN_SONG_SECONDS * (count - part)
                cuts.append(min(legal_upper, max(legal_lower, cut)))
            points = [original.start, *cuts, original.end]
            valid = [
                Segment(
                    points[index], points[index + 1],
                    core_start=points[index], core_end=points[index + 1],
                    nominal_end=points[index + 1],
                    boundary_source="stage-clock-inferred",
                )
                for index in range(len(points) - 1)
            ]
            decisions.append({
                "action": "forced-duration-safe-split",
                "source": [original.start, original.end],
                "result_durations_seconds": [round(item.duration, 2) for item in valid],
            })
        else:
            decisions.append({
                "action": "full-pass-split",
                "source": [original.start, original.end],
                "result_durations_seconds": [round(item.duration, 2) for item in valid],
            })
        result.extend(valid)

    DETECTION_STRATEGY["full_session_oversized_pass"] = decisions
    for decision in decisions:
        print("FULL SESSION OVERSIZED PASS: " + str(decision), flush=True)
    return result


def detect_segments(stems: list[Stem]) -> tuple[list[Segment], np.ndarray]:
    if not stems:
        return [], np.array([], dtype=np.float32)

    # Every detection starts from the same explicit defaults.  Calibration is
    # allowed to choose values for this run, but never to leak them into the
    # next run in the same process.
    global SILENCE_THRESHOLD_DB, SILENCE_GAP_SECONDS, DETECTION_STRATEGY, LAST_DETECTION_CALIBRATION, LAST_WHISPER_STATUS
    USED_SESSION_TITLES.clear()
    SILENCE_THRESHOLD_DB = DEFAULT_SILENCE_THRESHOLD_DB
    SILENCE_GAP_SECONDS = DEFAULT_SILENCE_GAP_SECONDS
    DETECTION_STRATEGY = {"id": "starting", "label": "fresh deterministic detection"}
    LAST_DETECTION_CALIBRATION = {}

    if not WHISPER_ALLOWED:
        # Whisper is optional. Use the acoustic MC/drum boundary detector for
        # the normal pass so sessions without a commentator still get slots.
        LAST_WHISPER_STATUS = {
            "status": "skipped",
            "reason": "optional analysis disabled; acoustic detection used",
        }
        report_progress({
            "current_stage": "reading cached envelopes",
            "stage_detail": f"Reading cached activity envelopes for {len(stems)} parallel stems",
            "progress": 24,
            "song_progress": 24,
            "phase_index": 2,
            "phase_total": 7,
            "stem_count": len(stems),
            "heartbeat": time.time(),
        })
        timelines = load_cached_timelines_or_die(stems, "Acoustic detection")
        durations = [float(stem.offset_seconds + stem.timeline_duration) for stem in stems]
        metadata_session_end = max(durations)
        session_end = active_session_end_from_timelines(stems, timelines, metadata_session_end)
        voice_stems = [stem for stem in stems if stem.role in {"vocal", "room"}]
        instrument_stems = [stem for stem in stems if stem.role not in {"vocal", "room"}]
        if not voice_stems or not instrument_stems:
            segments = provisional_segments_from_metadata(stems)
            DETECTION_STRATEGY = {
                "id": "metadata_provisional",
                "label": "No usable acoustic groups; Select Cuts required",
                "candidate_only": True,
                "session_segmentation_usable": False,
                "detected_count": len(segments),
                "needs_review": True,
                "whisper": LAST_WHISPER_STATUS,
            }
            return segments, np.array([], dtype=np.float32)
        mc_data, segments = auto_calibrate_detection(
            voice_stems, instrument_stems, timelines, session_end
        )
        if not segments:
            segments = auto_calibrate_drum_fallback(
                stems, timelines, session_end, voice_stems, instrument_stems, mc_data
            )
        if not segments:
            segments = provisional_segments_from_metadata(stems)
        else:
            segments = [
                replace(
                    segment,
                    boundary_source="acoustic-break",
                    speech_reason="Whisper skipped; boundary inferred from acoustic activity",
                )
                for segment in segments
            ]
        LAST_DETECTION_CALIBRATION["mode"] = "acoustic_without_whisper"
        DETECTION_STRATEGY = {
            "id": "acoustic_without_whisper",
            "label": "Acoustic MC/drum boundaries; Whisper optional",
            "detected_count": len(segments),
            "candidate_only": len(segments) < 2,
            "session_segmentation_usable": len(segments) > 1,
            "needs_review": True,
            "whisper": LAST_WHISPER_STATUS,
        }
        report_progress({
            "current_stage": "validating cuts",
            "stage_detail": f"Acoustic detection found {len(segments)} slot(s); Whisper was skipped",
            "progress": 90,
            "song_progress": 90,
            "phase_index": 6,
            "phase_total": 7,
            "candidate_count": len(segments),
            "heartbeat": time.time(),
        })
        print(f"ACOUSTIC DETECTION WITHOUT WHISPER: found {len(segments)} slot(s)", flush=True)
        return segments, np.asarray(mc_data.get("mc_mask", np.array([], dtype=np.float32)), dtype=np.float32)

    print("\nSONG DETECTION")
    report_progress({"current_stage": "reading cached envelopes", "stage_detail": f"Reading cached activity envelopes for {len(stems)} parallel stems", "progress": 24, "song_progress": 24, "phase_index": 2, "phase_total": 7, "stem_count": len(stems), "heartbeat": time.time()})
    print("Loading cached per-stem RMS envelopes for MC-break detection...", flush=True)
    timelines = load_cached_timelines_or_die(stems, "Detection")
    report_progress({"current_stage": "analyzing stems", "stage_detail": f"Comparing activity across {len(stems)} stems", "progress": 38, "song_progress": 38, "phase_index": 3, "phase_total": 7, "stem_count": len(stems), "heartbeat": time.time()})
    durations = sorted(s.offset_seconds + s.timeline_duration for s in stems)
    metadata_session_end = max(durations)
    session_end = active_session_end_from_timelines(stems, timelines, metadata_session_end)
    DETECTION_STRATEGY["global_timeline"] = {
        "mode": "shared_parallel_stem_timeline",
        "stem_count": len(stems),
        "metadata_session_end_seconds": metadata_session_end,
        "active_audio_end_seconds": session_end,
        "tail_excluded_seconds": max(0.0, metadata_session_end - session_end),
        "cut_list_scope": "one global list applied to every stem",
    }
    print(
        f"SESSION TIMELINE: {len(stems)} parallel stems; active audio ends at "
        f"{fmt_time(session_end)} (metadata {fmt_time(metadata_session_end)})",
        flush=True,
    )
    if len(durations) >= 4 and session_end > float(np.median(durations)) + 120.0:
        # A lone long export is usually a recorder tail, not a fourth-hour song.
        session_end = float(np.percentile(durations, 90))
        print(f"SESSION END: ignored outlier tail; using {fmt_time(session_end)}", flush=True)
    voice_stems = [s for s in stems if s.role in {"vocal", "room"}]
    instrument_stems = [s for s in stems if s.role not in {"vocal", "room"}]
    rhythm_names = [stem.path.name for stem in drum_equivalent_stems(stems) if is_rhythm_source_stem(stem)]
    if not voice_stems:
        raise RuntimeError("No mic/vocal stems found for MC-break detection.")
    if not instrument_stems:
        raise RuntimeError("No instrument stems found for MC-break detection.")

    report_progress({"current_stage": "analyzing stems", "stage_detail": "Finding commentator transition candidates", "progress": 54, "song_progress": 54, "phase_index": 3, "phase_total": 7, "heartbeat": time.time()})
    mc_data, calibrated_segments = auto_calibrate_detection(voice_stems, instrument_stems, timelines, session_end)
    probe_segments = calibrated_segments
    if not probe_segments:
        # The primary MC detector can legitimately return no usable breaks;
        # obtain probe locations from the drum-silence pass before Whisper,
        # rather than allowing that condition to suppress transcription.
        probe_segments = auto_calibrate_drum_fallback(
            stems, timelines, session_end, voice_stems, instrument_stems, mc_data,
        )
    whisper_available = bool(WHISPER_ALLOWED)
    if whisper_available:
        try:
            whisper_transcripts, whisper_segments = transcribe_speech_candidates(
                stems, timelines, session_end,
                boundary_probes=[segment.start for segment in probe_segments],
            )
        except Exception as exc:
            whisper_available = False
            whisper_transcripts, whisper_segments = [], []
            LAST_SPEECH_TRANSCRIPTIONS.clear()
            LAST_WHISPER_STATUS = {
                **LAST_WHISPER_STATUS,
                "status": LAST_WHISPER_STATUS.get("status", "unavailable"),
                "error": f"{type(exc).__name__}: {exc}",
            }
            print(f"WHISPER OPTIONAL: {LAST_WHISPER_STATUS}", flush=True)
    else:
        whisper_transcripts, whisper_segments = [], []
        LAST_WHISPER_STATUS = {
            "status": "skipped",
            "reason": "optional analysis disabled for initial source registration",
        }
        print("WHISPER OPTIONAL: skipped during initial source registration", flush=True)
    mc_mask = mc_data["mc_mask"]
    raw_mc_breaks = mask_to_regions(mc_mask, MC_BREAK_MIN_SECONDS)
    merged_mc_breaks = merge_regions(raw_mc_breaks, MC_SCAN_MERGE_GAP_SECONDS)
    mc_breaks = merge_short_music_intervals(merged_mc_breaks, mc_data["instrument_playing"], MC_MIN_FINAL_SONG_SECONDS)

    print_active_level_table(stems, mc_data["active_levels"])
    print(
        "\nMC break merge pass:"
        f"\n  raw MC windows: {len(raw_mc_breaks)}"
        f"\n  transition zones after <{MC_SCAN_MERGE_GAP_SECONDS:.0f}s cluster merge: {len(merged_mc_breaks)}"
        f"\n  final transition zones after removing <{fmt_time(MC_MIN_FINAL_SONG_SECONDS)} songs: {len(mc_breaks)}"
    )
    print_mc_breaks(mc_breaks)
    if whisper_available and len(whisper_segments) < 2:
        transcript_count = int(LAST_WHISPER_STATUS.get("transcript_count") or len(whisper_transcripts))
        eligible_count = int(LAST_WHISPER_STATUS.get("eligible_introductions") or len(whisper_segments))
        reason = (
            f"only {eligible_count} commentator presentation(s) detected after "
            f"{transcript_count}/{int(LAST_WHISPER_STATUS.get('candidate_windows') or transcript_count)} "
            "Whisper windows; automatic session replacement is disabled"
        )
        LAST_WHISPER_STATUS = {
            **LAST_WHISPER_STATUS,
            "status": "needs_review",
            "reason": reason,
            "transcript_count": transcript_count,
            "eligible_introductions": eligible_count,
            "session_segmentation_usable": False,
        }
        DETECTION_STRATEGY = {
            "id": "whisper_completed_insufficient_presentations",
            "label": "Whisper completed, but the slot candidate is incomplete",
            "candidate_only": True,
            "session_segmentation_usable": False,
            "whisper_transcript_count": transcript_count,
            "whisper_eligible_introductions": eligible_count,
            "whisper": dict(LAST_WHISPER_STATUS),
            "reason": reason,
            "rhythm_equivalent_sources": rhythm_names,
        }
        whisper_available = False
    if whisper_available:
        # Transcript-first invariant: this ordered list is the complete accepted
        # segmentation. No acoustic, duration, numbering, or correction pass may
        # add, remove, merge, or move one of these song windows.
        segments = list(whisper_segments)
        segments = apply_spoken_number_structure(segments, whisper_transcripts, session_end)
        DETECTION_STRATEGY = {
            "id": "transcript_first",
            "label": "Transcript-first introductions; acoustic cross-check only",
            "detected_count": len(segments),
            "whisper_candidate_count": len(whisper_transcripts),
            "whisper_boundary_count": len(whisper_segments),
            "whisper_spoken_numbers": [item.get("spoken_song_number") for item in whisper_transcripts if item.get("spoken_song_number") is not None],
            "rhythm_equivalent_sources": rhythm_names,
            "whisper": LAST_WHISPER_STATUS,
        }
    else:
        # Without a sufficiently trusted commentator transcript there is no
        # defensible slot boundary. Do not replace it with drum/acoustic or
        # fixed-clock windows; expose one conservative review block instead.
        segments = provisional_segments_from_metadata(stems)
        provisional_reason = str(
            LAST_WHISPER_STATUS.get("reason")
            or "Commentator-led slot boundaries were not available"
        )
        if segments:
            segments = [replace(
                segments[0],
                boundary_source="whisper-incomplete-provisional",
                boundary_validation="needs_review",
                boundary_validation_reason=(
                    "Candidate only; not a valid single-song session: "
                    + provisional_reason
                ),
            )]
        DETECTION_STRATEGY = {
            **(DETECTION_STRATEGY if isinstance(DETECTION_STRATEGY, dict) else {}),
            "id": "commentator_missing_review",
            "label": "Whisper candidate incomplete; Select Cuts required",
            "candidate_only": True,
            "session_segmentation_usable": False,
            "detected_count": len(segments),
            "whisper_transcript_count": int(LAST_WHISPER_STATUS.get("transcript_count") or len(whisper_transcripts)),
            "whisper_eligible_introductions": int(LAST_WHISPER_STATUS.get("eligible_introductions") or len(whisper_segments)),
            "whisper": dict(LAST_WHISPER_STATUS),
            "rhythm_equivalent_sources": rhythm_names,
            "needs_review": True,
        }
        return segments, mc_mask.astype(np.float32)

    # Whisper/VAD is authoritative for this recording.  Return the presented
    # slots before any acoustic duration-repair pass can split a slot into
    # multiple songs.  Activity remains diagnostic only.
    segments = finalize_presented_slot_segments(whisper_transcripts, session_end)
    DETECTION_STRATEGY.update({
        "id": "commentator_presented_slots",
        "label": "One global slot per commentator presentation",
        "detected_count": len(segments),
        "target_slot_count": EXPECTED_SLOT_COUNT,
        "whisper_candidate_count": len(whisper_transcripts),
        "whisper_boundary_count": len(segments),
        "slot_unit": "commentator-led slot; never split on internal instrument activity",
        "needs_review_slots": [index for index, segment in enumerate(segments, 1) if segment.boundary_validation == "needs_review"],
    })
    print(
        f"COMMENTATOR SLOTS: accepted {len(segments)} presented slots; "
        f"target approximately {EXPECTED_SLOT_COUNT}; internal songs are not split", flush=True,
    )
    return segments, mc_mask.astype(np.float32)

    if not mc_breaks:
        acoustic_fallback_segments = auto_calibrate_drum_fallback(stems, timelines, session_end, voice_stems, instrument_stems, mc_data)
        inferred_count = sum(segment.boundary_source == "stage-clock-inferred" for segment in acoustic_fallback_segments)
        announcer_count = sum(segment.boundary_source == "announcer-confirmed" for segment in acoustic_fallback_segments)
        if not whisper_segments:
            DETECTION_STRATEGY = {"id": "drum_fallback_combined", "label": "Drum-silence fallback + announcer-confirmed boundaries + stage-clock inference (no announcer zones in primary detector)", "reason": "MC-announcer detection produced 0 usable transition zones", "detected_count": len(segments), "stage_clock_inferred_boundaries": inferred_count, "announcer_confirmed_boundaries": announcer_count, "rhythm_equivalent_sources": rhythm_names}
        print(f"DETECTION STRATEGY: Drum-silence fallback; {announcer_count} announcer-confirmed, {inferred_count} stage-clock inferred", flush=True)
    else:
        if not whisper_segments:
            DETECTION_STRATEGY = {"id": "mc", "label": "MC-announcer detection", "detected_count": len(segments), "rhythm_equivalent_sources": rhythm_names}
        print("DETECTION STRATEGY: MC-announcer detection", flush=True)
    segments = attach_speech_metadata(segments)
    # Metadata is available only after attachment for acoustic-leading
    # segments; apply the SONG 0 rule before cross-checks and duration repair.
    segments = _split_pre_session_segment(segments, stems, timelines, whisper_transcripts)
    opening = next((item for item in whisper_transcripts
                    if re.search(r"\bfirst\s+(?:slot|song|round)\b", str(item.get("text") or "").lower())), None)
    if opening is not None and segments and segments[0].start <= 1.0:
        opening_at = float(opening.get("announcement_start", opening.get("start", 0.0)))
        formal_index = next((i for i, seg in enumerate(segments) if seg.start >= opening_at - 1.0), 0)
        if formal_index > 0:
            segments[0] = replace(segments[0], spoken_song_number=None, speech_text="", speech_intro_text="",
                                    speech_intro_start=None, assigned_song_number=0, boundary_source="pre-session-content")
            segments[formal_index] = replace(segments[formal_index], spoken_song_number=1, assigned_song_number=1)
            print(f"SONG 0: leading content 00:00:00.00-{fmt_time(segments[0].end)} before opening first slot", flush=True)
    cross_checks = []
    for index, segment in enumerate(segments, 1):
        valid, instruments, voices = _boundary_activity(segment.start, stems, timelines, radius_seconds=2.0, diagnostic=True)
        cross_checks.append({"song": index, "boundary": segment.start, "loud_full_band": not valid, "instrument_activity": instruments, "vocal_activity": voices})
        if not valid:
            print(f"ACOUSTIC CROSS-CHECK: song={index} boundary={fmt_time(segment.start)} loud_full_band instruments={instruments} voices={voices}", flush=True)
    DETECTION_STRATEGY["acoustic_cross_check"] = cross_checks
    DETECTION_STRATEGY["long_intro_gaps"] = [
        {"after_song": index, "gap_seconds": segments[index].start - segments[index - 1].start}
        for index in range(1, len(segments))
        if segments[index].start - segments[index - 1].start > 20 * 60.0
    ]
    print(f"TRANSCRIPT-FIRST: accepted {len(segments)} songs from {len(whisper_segments)} ordered introductions; acoustic checks cannot alter them", flush=True)
    if whisper_available:
        require_structural_introductions(segments)
    segments = auto_retry_bad_detection(
        segments, stems, timelines, session_end, voice_stems, instrument_stems, mc_data,
    )
    segments = enforce_hard_song_duration_bounds(
        segments, stems, timelines, voice_stems, instrument_stems, mc_data,
    )
    segments = attach_speech_metadata(segments)
    segments = align_transcript_introduction_boundaries(segments)
    # A transcript or acoustic proposal is not sufficient evidence by itself.
    # Collapse any boundary that still crosses active music; the resulting
    # longer window remains reviewable in Select Cuts instead of exporting a
    # song split in the middle of a performance.
    segments = merge_unsafe_music_boundaries(segments, stems, timelines)
    # Coverage and duration repair can add the leading pre-session window
    # after the first numbering pass; enforce SONG 0 once more at the final
    # topology boundary.
    segments = _split_pre_session_segment(segments, stems, timelines, whisper_transcripts)
    # Remove long adaptive dead-air lead-ins/trailers before the final shared
    # boundary gate.  This pass is detection-only: it reads the cached
    # envelopes and never touches render-time audio.  Whisper alignment below
    # restores any authoritative spoken introduction lead-in that trimming
    # may have moved past.
    segments = trim_dead_air_segments(segments, stems, timelines)
    segments = enforce_hard_boundary_gate(segments, stems, timelines, session_end, whisper_segments)
    segments = apply_spoken_number_structure(segments, whisper_transcripts, session_end)
    # Never round boundaries to whole seconds.  Preserve the full Whisper /
    # acoustic float through detection; read_stem_chunk performs the final
    # sample-accurate conversion when rendering.
    segments = preserve_content_coverage(segments, whisper_transcripts, session_end)
    # Coverage restoration may expose a previously uncertain trailing region
    # only now.  It must still receive the same Whisper/drum-core/duration
    # treatment as every other part of the session.
    segments = split_oversized_regions_full_pass(
        segments, stems, timelines, voice_stems, instrument_stems, mc_data,
    )
    # A duration-safe fallback may land one piece entirely inside a long
    # non-musical pause.  Keep that pause as a gap between songs, never as an
    # empty renderable song; the surrounding music is still fully covered.
    segments = discard_empty_segments(segments, stems, timelines, floor_dbfs=-55.0)
    segments = enforce_hard_song_duration_bounds(
        segments, stems, timelines, voice_stems, instrument_stems, mc_data,
    )
    segments = restore_final_introduction_lead_ins(segments)
    # Duration repair can create/sort pieces after the earlier alignment pass.
    # Re-apply the authoritative Whisper edges as the final topology pass so
    # metadata restored on a repaired piece cannot leave its speech boundary
    # stranded in the preceding export.
    segments = align_transcript_introduction_boundaries(segments)
    # Duration repair and coverage restoration can introduce new boundaries
    # after the earlier activity gate.  Apply the same conservative gate to
    # the final topology so a late split can never cut through active music.
    segments = merge_unsafe_music_boundaries(segments, stems, timelines)
    # The merge above is authoritative for existing boundaries, but a merged
    # review block can still contain independently verifiable quiet points.
    # Partition those long blocks once, at the very end, so later topology
    # passes cannot move the cut away from the measured safe sample region.
    final_partitioned: list[Segment] = []
    final_partition_audit: list[dict[str, object]] = []
    for segment in segments:
        parts = split_oversized_at_safe_boundaries(segment, stems, timelines) if segment.duration > HARD_MAX_SONG_SECONDS else []
        if len(parts) <= 1 and segment.duration > HARD_MAX_SONG_SECONDS:
            # Do not leave a 20--40 minute source block as one song merely
            # because no boundary passed the automatic acoustic gate. Expose
            # conservative song-sized proposals for Select Cuts instead.
            soft_parts = apply_stage_clock_soft_splits([segment], stems, timelines)
            if len(soft_parts) > 1:
                parts = mark_partition_for_review(soft_parts, stems, timelines)
                final_partition_audit.append({
                    "source_start": segment.start,
                    "source_end": segment.end,
                    "result_durations": [part.duration for part in parts],
                    "split_points": [part.end for part in parts[:-1]],
                    "validation": "needs_review",
                })
        if len(parts) > 1:
            final_partitioned.extend(parts)
            if not any(item.get("source_start") == segment.start and item.get("source_end") == segment.end for item in final_partition_audit):
                final_partition_audit.append({
                    "source_start": segment.start,
                    "source_end": segment.end,
                    "result_durations": [part.duration for part in parts],
                    "split_points": [part.end for part in parts[:-1]],
                    "validation": "valid",
                })
        else:
            final_partitioned.append(segment)
    if final_partition_audit:
        DETECTION_STRATEGY["final_safe_partition"] = final_partition_audit
    segments = sorted(final_partitioned, key=lambda item: item.start)
    # Final topology is one shared cut list for the parallel session.  This
    # pass prevents Whisper fragments and recorder tails from becoming songs
    # and guarantees that no long source block reaches the renderer as one
    # window.  Unsafe inferred boundaries remain needs_review.
    segments = normalize_global_song_windows(segments, stems, timelines)
    print(
        "GLOBAL WINDOWS: "
        f"{len(segments)} shared windows; "
        f"needs_review={sum(item.boundary_validation == 'needs_review' for item in segments)}; "
        f"out_of_range={sum(not HARD_MIN_SONG_SECONDS <= item.duration <= HARD_MAX_SONG_SECONDS for item in segments)}",
        flush=True,
    )
    # Re-check the final post-split topology. Earlier validation happens
    # before duration repair, and synthetic duration-safe pieces must not be
    # reported as if Whisper had verified an introduction for them.
    opening = next((item for item in whisper_transcripts
                    if re.search(r"\bfirst\s+(?:slot|song|round)\b", str(item.get("text") or "").lower())), None)
    if opening is not None and segments and segments[0].start <= 1.0:
        opening_at = float(opening.get("announcement_start", opening.get("start", 0.0)))
        formal_index = next((i for i, seg in enumerate(segments) if seg.start >= opening_at - 1.0), 0)
        if formal_index > 0:
            segments[0] = replace(segments[0], spoken_song_number=None, speech_text="", speech_intro_text="",
                                    speech_intro_start=None, assigned_song_number=0, boundary_source="pre-session-content")
            segments[formal_index] = replace(segments[formal_index], spoken_song_number=1, assigned_song_number=1)
            print(f"SONG 0: leading content 00:00:00.00-{fmt_time(segments[0].end)} before opening first slot", flush=True)
    final_intro_audit = [
        {
            "song": index,
            "introduction_found": bool(str(segment.speech_intro_text or "").strip()),
            "spoken_number": segment.spoken_song_number,
            "assigned_number": segment.assigned_song_number if segment.assigned_song_number is not None else index,
            "boundary_source": segment.boundary_source,
        }
        for index, segment in enumerate(segments, 1)
    ]
    DETECTION_STRATEGY["spoken_number_assignments"] = [
        {"app_index": i, "spoken_number": seg.spoken_song_number,
         "assigned_number": seg.assigned_song_number if seg.assigned_song_number is not None else i}
        for i, seg in enumerate(segments, 1)
    ]
    DETECTION_STRATEGY["final_introduction_audit"] = final_intro_audit
    missing_final_intros = [item["song"] for item in final_intro_audit if not item["introduction_found"]]
    if missing_final_intros:
        print(
            f"FINAL INTRO AUDIT: missing Whisper introduction metadata for songs {missing_final_intros}",
            flush=True,
        )
    if whisper_transcripts:
        mismatches = [
            {"detected_song": index, "spoken": segment.spoken_song_number}
            for index, segment in enumerate(segments, 1)
            if segment.spoken_song_number is not None and int(segment.spoken_song_number) != index
        ]
        DETECTION_STRATEGY.update({
            "whisper_transcripts": whisper_transcripts,
            "whisper_number_mismatches": mismatches,
            "whisper_final_count": len(segments),
        })
    DETECTION_STRATEGY["detected_count"] = len(segments)
    print_detected_segments(segments)
    long_count = sum(((seg.nominal_end if seg.nominal_end is not None else seg.end) - seg.start) > 15 * 60 for seg in segments)
    if (EXPECTED_SONG_COUNT is not None and len(segments) != EXPECTED_SONG_COUNT) or any(not HARD_MIN_SONG_SECONDS <= seg.duration <= HARD_MAX_SONG_SECONDS for seg in segments):
        print(f"CALIBRATION WARNING: found {len(segments)} songs; configured target is {EXPECTED_SONG_COUNT or 'not configured'}, duration policy is {HARD_MIN_SONG_SECONDS/60:.0f}-{HARD_MAX_SONG_SECONDS/60:.0f} minutes.", flush=True)
    else:
        print(f"CALIBRATION: found {len(segments)} songs; expected around 20 or more, looks reasonable.", flush=True)
    return segments, mc_mask.astype(np.float32)


def active_session_end_from_timelines(
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
    metadata_session_end: float,
    threshold_dbfs: float = -55.0,
) -> float:
    """Find the shared session end from all parallel-stem envelopes.

    File duration is not song duration: some recorders leave long silent tails
    and some stems carry a different container length.  The common timeline is
    therefore bounded by the last real activity in any decodable stem, while
    preserving a small analysis-frame margin for the final note/reverb.
    """
    active_ends: list[float] = []
    floor = db_to_amp(threshold_dbfs)
    for stem in stems:
        env = np.asarray(timelines.get(stem.path.name, np.array([], dtype=np.float32)), dtype=np.float64)
        active = np.flatnonzero(env >= floor)
        if active.size:
            active_ends.append(float(active[-1] + 1) * DETECTION_FRAME_SECONDS)
    if not active_ends:
        return float(metadata_session_end)
    # Preserve the last active tail across any one short/quiet stem, but never
    # extend beyond the longest source container.
    return min(float(metadata_session_end), max(active_ends) + 2.0 * DETECTION_FRAME_SECONDS)


def load_cached_timelines_or_die(stems: list[Stem], label: str) -> dict[str, np.ndarray]:
    cache = ensure_detection_cache(stems, label)
    print(f"Loaded detection envelope cache: {detection_cache_path()}", flush=True)
    print("Cache format is per-stem RMS envelopes; regrouping reuses it without audio reads.", flush=True)
    max_bins = int(math.ceil(max(s.offset_seconds + s.timeline_duration for s in stems) / DETECTION_FRAME_SECONDS))
    return {
        stem.path.name: place_envelope_on_timeline(stem, cache.get(stem.path.name, np.array([], dtype=np.float32)), max_bins)
        for stem in stems
    }


def ensure_detection_cache(stems: list[Stem], label: str = "Detection") -> dict[str, np.ndarray]:
    """Load this source's cache, building it when it has not been scanned yet."""
    cache = load_detection_cache(stems)
    if cache:
        print(f"Reusing detection envelope cache: {detection_cache_path()}", flush=True)
        return cache

    print(
        f"No valid detection envelope cache for source folder {SOURCE_DIR}; "
        "building a full per-stem envelope cache...",
        flush=True,
    )
    build_detection_cache(stems)
    cache = load_detection_cache(stems)
    if not cache:
        raise RuntimeError(f"Failed to build a valid detection envelope cache at {detection_cache_path()}.")
    return cache


def stem_active_level(env: np.ndarray) -> float | None:
    region = env[env > db_to_amp(MC_ACTIVE_LEVEL_FLOOR_DBFS)]
    if len(region) == 0:
        return None
    return max(float(np.percentile(region, 75)), 1e-9)


def build_mc_detection_signals(
    voice_stems: list[Stem],
    instrument_stems: list[Stem],
    timelines: dict[str, np.ndarray],
) -> dict[str, np.ndarray | dict[str, float]]:
    active_levels_all = {stem.path.name: stem_active_level(timelines[stem.path.name]) for stem in voice_stems + instrument_stems}
    active_levels = {name: level for name, level in active_levels_all.items() if level is not None}
    active_voice_stems = [stem for stem in voice_stems if stem.path.name in active_levels]
    active_instrument_stems = [stem for stem in instrument_stems if stem.path.name in active_levels]
    if not active_voice_stems:
        raise RuntimeError("No globally active mic/vocal stems above MC active-level floor.")
    if not active_instrument_stems:
        raise RuntimeError("No globally active instrument stems above MC active-level floor.")

    voice_relative = normalized_group_db(active_voice_stems, timelines, active_levels)
    instrument_relative_by_stem = {
        stem.path.name: relative_to_active_db(timelines[stem.path.name], active_levels[stem.path.name])
        for stem in active_instrument_stems
    }
    instrument_names = list(instrument_relative_by_stem)
    instrument_rel_stack = np.vstack([instrument_relative_by_stem[name] for name in instrument_names])
    loudest_idx = np.argmax(instrument_rel_stack, axis=0)
    loudest_instrument_relative = instrument_rel_stack[loudest_idx, np.arange(instrument_rel_stack.shape[1])]
    median_instrument_relative = np.median(instrument_rel_stack, axis=0)
    loud_instrument_count = np.sum(instrument_rel_stack >= -MC_INSTRUMENT_QUIET_BELOW_DB, axis=0)
    instrument_quiet = (
        (median_instrument_relative < -MC_INSTRUMENT_QUIET_BELOW_DB)
        & (loud_instrument_count <= MC_ALLOWED_LOUD_INSTRUMENTS)
    )
    voice_present = voice_relative > -MC_VOICE_ACTIVE_WITHIN_DB
    voice_dominant = voice_relative > loudest_instrument_relative + MC_VOICE_DOMINANCE_DB
    mc_mask = smooth_boolean_activity(voice_present & instrument_quiet & voice_dominant, MC_BREAK_SMOOTH_SECONDS)
    instrument_playing = smooth_boolean_activity(median_instrument_relative > -MC_INSTRUMENT_QUIET_BELOW_DB, MC_BREAK_SMOOTH_SECONDS)
    raw_voice_db = raw_group_db(active_voice_stems, timelines)
    raw_inst_stack = np.vstack([20.0 * np.log10(np.maximum(timelines[stem.path.name], 1e-9)) for stem in active_instrument_stems])
    raw_loudest_instrument_db = raw_inst_stack[loudest_idx, np.arange(raw_inst_stack.shape[1])]
    loudest_instrument_name = np.array([instrument_names[i] for i in loudest_idx], dtype=object)
    return {
        "active_levels": active_levels,
        "inactive_stems": {name: level for name, level in active_levels_all.items() if level is None},
        "voice_relative": voice_relative,
        "loudest_instrument_relative": loudest_instrument_relative,
        "median_instrument_relative": median_instrument_relative,
        "loud_instrument_count": loud_instrument_count,
        "voice_present": voice_present,
        "instrument_quiet": instrument_quiet,
        "voice_dominant": voice_dominant,
        "mc_mask": mc_mask,
        "instrument_playing": instrument_playing,
        "raw_voice_db": raw_voice_db,
        "raw_loudest_instrument_db": raw_loudest_instrument_db,
        "loudest_instrument_name": loudest_instrument_name,
    }


def relative_to_active_db(env: np.ndarray, active_level: float) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(env, 1e-9) / max(active_level, 1e-9))


def normalized_group_db(stems: list[Stem], timelines: dict[str, np.ndarray], active_levels: dict[str, float]) -> np.ndarray:
    if not stems:
        return np.array([], dtype=np.float32)
    norm = []
    for stem in stems:
        norm.append(np.maximum(timelines[stem.path.name], 1e-9) / max(active_levels[stem.path.name], 1e-9))
    combined = np.mean(np.vstack(norm), axis=0)
    return 20.0 * np.log10(np.maximum(combined, 1e-9))


def raw_group_db(stems: list[Stem], timelines: dict[str, np.ndarray]) -> np.ndarray:
    if not stems:
        return np.array([], dtype=np.float32)
    combined = np.mean(np.vstack([timelines[stem.path.name] for stem in stems]), axis=0)
    return 20.0 * np.log10(np.maximum(combined, 1e-9))


def raw_loudest_group_db(stems: list[Stem], timelines: dict[str, np.ndarray]) -> np.ndarray:
    if not stems:
        return np.array([], dtype=np.float32)
    stacked = np.vstack([20.0 * np.log10(np.maximum(timelines[stem.path.name], 1e-9)) for stem in stems])
    return np.max(stacked, axis=0)


def print_active_level_table(stems: list[Stem], active_levels: dict[str, float]) -> None:
    print("\nPer-stem active levels:")
    for stem in stems:
        if stem.path.name in active_levels:
            print(f"  {stem.path.name:24s} role={stem.role:8s} active_p75={amp_to_db(active_levels[stem.path.name]):6.1f} dBFS")
        else:
            print(f"  {stem.path.name:24s} role={stem.role:8s} inactive (<{MC_ACTIVE_LEVEL_FLOOR_DBFS:.0f} dBFS)")


def mask_to_regions(mask: np.ndarray, min_seconds: float) -> list[tuple[float, float]]:
    min_bins = max(1, int(round(min_seconds / DETECTION_FRAME_SECONDS)))
    regions: list[tuple[float, float]] = []
    i = 0
    n = len(mask)
    while i < n:
        while i < n and not mask[i]:
            i += 1
        start = i
        while i < n and mask[i]:
            i += 1
        end = i
        if end - start >= min_bins:
            regions.append((start * DETECTION_FRAME_SECONDS, end * DETECTION_FRAME_SECONDS))
    return regions


def print_mc_breaks(mc_breaks: list[tuple[float, float]]) -> None:
    print("\nDetected merged MC transition zones:")
    if not mc_breaks:
        print("  none")
        return
    for idx, (start, end) in enumerate(mc_breaks, 1):
        print(f"  MC {idx:02d}. {fmt_time(start)} - {fmt_time(end)}  ({fmt_time(end - start)})")


def merge_short_music_intervals(
    mc_breaks: list[tuple[float, float]],
    instrument_playing: np.ndarray,
    min_song_seconds: float,
) -> list[tuple[float, float]]:
    if len(mc_breaks) < 2:
        return mc_breaks
    merged = list(mc_breaks)
    changed = True
    while changed and len(merged) >= 2:
        changed = False
        for idx in range(len(merged) - 1):
            music_start = merged[idx][1]
            music_end = merged[idx + 1][0]
            if music_end - music_start >= min_song_seconds:
                continue
            merged[idx] = (merged[idx][0], max(merged[idx][1], merged[idx + 1][1]))
            del merged[idx + 1]
            changed = True
            break
    return merged


def songs_from_mc_breaks(
    mc_breaks: list[tuple[float, float]],
    instrument_playing: np.ndarray,
    session_end: float,
) -> list[Segment]:
    if not mc_breaks:
        return []
    active_regions = mask_to_regions(instrument_playing, MIN_SONG_SECONDS)
    last_activity_end = active_regions[-1][1] if active_regions else session_end

    segments: list[Segment] = []
    for idx, (mc_start, mc_end) in enumerate(mc_breaks):
        song_start = mc_end
        if idx + 1 < len(mc_breaks):
            next_mc_start, next_mc_end = mc_breaks[idx + 1]
            nominal_end = next_mc_start
            render_end = min(session_end, next_mc_start + min(MC_NEXT_FADE_SECONDS, max(0.0, next_mc_end - next_mc_start)))
        else:
            next_mc_start = next_mc_end = None
            nominal_end = min(session_end, max(last_activity_end, mc_end))
            render_end = nominal_end
        if nominal_end <= song_start:
            continue
        segments.append(
            Segment(
                song_start,
                render_end,
                core_start=song_start,
                core_end=nominal_end,
                nominal_end=nominal_end,
                mc_start=mc_start,
                mc_end=mc_end,
                next_mc_start=next_mc_start,
                next_mc_end=next_mc_end,
            )
        )
    return segments


LAST_DETECTION_CALIBRATION: dict[str, object] = {}
DETECTION_STRATEGY: dict[str, object] = {"id": "mc", "label": "MC-announcer detection"}
READ_WARNINGS_REPORTED: set[tuple[str, str, str]] = set()


def auto_calibrate_detection(
    voice_stems: list[Stem],
    instrument_stems: list[Stem],
    timelines: dict[str, np.ndarray],
    session_end: float,
) -> tuple[dict[str, np.ndarray | dict[str, float]], list[Segment]]:
    """Choose MC/drum boundary sensitivity from the recurring session structure."""
    global MC_VOICE_ACTIVE_WITHIN_DB, MC_BREAK_MIN_SECONDS, MC_SCAN_MERGE_GAP_SECONDS
    target_count = int(KNOWN_SONG_COUNT or max(20, round(session_end / 600.0)))
    best: tuple[float, tuple[float, float, float], dict[str, np.ndarray | dict[str, float]], list[Segment]] | None = None
    configs = [(voice, minimum, gap) for voice in (14.0, 18.0, 22.0, 26.0) for minimum in (15.0, 30.0, 45.0) for gap in (20.0, 35.0, 50.0, 70.0)]
    for config_index, (voice, minimum, gap) in enumerate(configs, 1):
        MC_VOICE_ACTIVE_WITHIN_DB, MC_BREAK_MIN_SECONDS, MC_SCAN_MERGE_GAP_SECONDS = voice, minimum, gap
        data = build_mc_detection_signals(voice_stems, instrument_stems, timelines)
        raw = mask_to_regions(data["mc_mask"], minimum)
        merged = merge_regions(raw, gap)
        breaks = merge_short_music_intervals(merged, data["instrument_playing"], MC_MIN_FINAL_SONG_SECONDS)
        segments = songs_from_mc_breaks(breaks, data["instrument_playing"], session_end)
        durations = [((s.nominal_end if s.nominal_end is not None else s.end) - s.start) for s in segments]
        count_error = abs(len(segments) - target_count) / max(1.0, target_count)
        undercount_penalty = 35.0 if KNOWN_SONG_COUNT and len(segments) < KNOWN_SONG_COUNT else 0.0
        median_error = abs((float(np.median(durations)) if durations else 0.0) - 600.0) / 600.0
        long_fraction = sum(duration > 900.0 for duration in durations) / max(1, len(durations))
        score = 100.0 - 100.0 * count_error - undercount_penalty - 25.0 * min(median_error, 2.0) - 20.0 * long_fraction
        if best is None or score > best[0]:
            best = (score, (voice, minimum, gap), data, segments)
        report_progress({"current_stage": "calibrating detection", "stage_detail": f"Testing boundary sensitivity {config_index} of {len(configs)}", "progress": 82 + int(config_index / len(configs) * 10), "process_progress": int(config_index / len(configs) * 100), "song_progress": int(config_index / len(configs) * 100), "calibration_current": config_index, "calibration_total": len(configs), "heartbeat": time.time()})
    assert best is not None
    score, selected, data, segments = best
    MC_VOICE_ACTIVE_WITHIN_DB, MC_BREAK_MIN_SECONDS, MC_SCAN_MERGE_GAP_SECONDS = selected
    durations = [((s.nominal_end if s.nominal_end is not None else s.end) - s.start) for s in segments]
    median_duration = float(np.median(durations)) if durations else 0.0
    LAST_DETECTION_CALIBRATION = {
        "voice_active_within_db": selected[0],
        "break_min_seconds": selected[1],
        "merge_gap_seconds": selected[2],
        "score": round(score, 1),
        "target_count": target_count,
        "known_song_count": KNOWN_SONG_COUNT,
        "detected_count": len(segments),
        "median_duration_seconds": round(median_duration, 1),
        "message": f"Auto-selected sensitivity: {len(segments)} songs, median {fmt_time(median_duration)} (target ~{target_count} songs / 10:00 each).",
    }
    print("AUTO CALIBRATION: " + str(LAST_DETECTION_CALIBRATION), flush=True)
    # Keep the selected values; the original values are intentionally not restored.
    return data, segments


def provisional_segments_from_metadata(stems: list[Stem]) -> list[Segment]:
    """Create one conservative review window without reading full audio.

    Metadata alone cannot identify song boundaries.  Fixed ten-minute slices
    created false songs and could cut active performances, so source
    registration exposes the complete session as one needs-review block until
    Analyze supplies acoustic evidence or the user confirms Select Cuts.
    """
    if not stems:
        return []
    session_end = max(float(stem.offset_seconds + stem.timeline_duration) for stem in stems)
    return [
        Segment(
            0.0,
            session_end,
            core_start=0.0,
            core_end=session_end,
            nominal_end=session_end,
            boundary_source="metadata-provisional",
            speech_reason="No boundary evidence yet; Analyze or Select Cuts required",
        )
    ]


def merge_unsafe_music_boundaries(
    segments: list[Segment],
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
) -> list[Segment]:
    """Remove proposed cuts that cross active music.

    This is deliberately conservative: a comment/Whisper timestamp can be a
    useful hint, but it never overrides the multi-stem activity check.  When a
    boundary is unsafe, adjacent windows are merged and marked for review.
    """
    if len(segments) < 2:
        return segments
    result = sorted(list(segments), key=lambda item: item.start)
    review_boundaries: list[dict[str, object]] = []
    for index in range(len(result) - 1):
        left, right = result[index], result[index + 1]
        cut = float(right.start)
        safe, instruments, voices = _boundary_activity(
            cut, stems, timelines, radius_seconds=3.0, diagnostic=True
        )
        if safe:
            continue
        review_boundaries.append({
            "cut_seconds": cut,
            "active_instruments": instruments,
            "active_voices": voices,
            "reason": "boundary crosses active music; proposals retained for Select Cuts review",
        })
        result[index] = replace(
            left,
            boundary_source="needs-review-active-music-boundary",
            boundary_validation="needs_review",
            boundary_validation_reason="active music at proposed boundary",
            speech_reason=(left.speech_reason or "") + "; active-music boundary retained as provisional; review in Select Cuts",
        )
        result[index + 1] = replace(
            right,
            boundary_source="needs-review-active-music-boundary",
            boundary_validation="needs_review",
            boundary_validation_reason="active music at proposed boundary",
            speech_reason=(right.speech_reason or "") + "; active-music boundary retained as provisional; review in Select Cuts",
        )
    DETECTION_STRATEGY["unsafe_music_boundary_merge"] = {
        "merged_count": 0,
        "retained_for_review_count": len(review_boundaries),
        "boundaries": review_boundaries,
        "remaining_count": len(result),
        "policy": "never approve active-music cuts; retain proposal and require review",
    }
    return result


def auto_calibrate_drum_fallback(
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
    session_end: float,
    voice_stems: list[Stem] | None = None,
    instrument_stems: list[Stem] | None = None,
    mc_data: dict[str, np.ndarray | dict[str, float]] | None = None,
) -> list[Segment]:
    """Calibrate drum-core silence gaps when the session has no usable MC voice."""
    global SILENCE_THRESHOLD_DB, SILENCE_GAP_SECONDS, LAST_DETECTION_CALIBRATION
    drum_stems = drum_equivalent_stems(stems)
    if not drum_stems:
        return []
    stem_thresholds = {stem.path.name: per_stem_activity_threshold(timelines[stem.path.name], stem) for stem in stems}
    non_drum_stems = [stem for stem in stems if stem not in drum_stems]
    post_stems = [stem for stem in non_drum_stems if stem.role not in {"vocal", "room"}]
    mic_stems = [stem for stem in stems if stem.role in {"vocal", "room"}]
    announcer_mask = None
    if voice_stems and instrument_stems:
        signal_data = mc_data or build_mc_detection_signals(voice_stems, instrument_stems, timelines)
        # The fallback uses the same signature but intentionally does not
        # require voice dominance: an MC may be present on a shared vocal mic
        # while a loud residual instrument keeps the primary mask at zero.
        announcer_mask = smooth_boolean_activity(
            np.asarray(signal_data["voice_present"], dtype=bool)
            & np.asarray(signal_data["instrument_quiet"], dtype=bool),
            MC_BREAK_SMOOTH_SECONDS,
        )
        announcer_mask = np.asarray(announcer_mask, dtype=bool)
    drum_energy = smooth_envelope(combine_normalized_envelope(drum_stems, timelines), DETECTION_SMOOTH_SECONDS)
    drum_energy_db = relative_db(drum_energy)
    target_count = int(KNOWN_SONG_COUNT or max(20, round(session_end / 600.0)))
    best: tuple[float, float, float, list[Segment]] | None = None
    # Include the historical range plus shorter inter-song gaps and higher
    # activity thresholds.  The recurring sessions often have transitions
    # shorter than 30 seconds, and quiet drum bleed can otherwise bridge them.
    thresholds = (-5.0, -8.0, -10.0, -12.0, -15.0, -20.0, -25.0, -30.0, -35.0)
    gaps = (2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0, 30.0, 45.0, 60.0, 75.0, 90.0, 120.0)
    total = len(thresholds) * len(gaps)
    sweep: list[dict[str, float | int]] = []
    for index, (threshold, gap) in enumerate(((threshold, gap) for threshold in thresholds for gap in gaps), 1):
        core = active_mask_to_segments(drum_energy_db > threshold, gap)
        core = merge_or_discard_short_segments(core)
        core = merge_close_segments(core, gap)
        segments = extend_segments_from_non_drums(core, non_drum_stems, post_stems, timelines, stem_thresholds, session_end)
        segments = merge_close_segments(segments, gap)
        segments = attach_post_song_mic_activity(segments, mic_stems, timelines, stem_thresholds, session_end)
        segments = apply_announcer_confirmed_boundaries(segments, announcer_mask)
        segments = pad_segments(segments, session_end)
        segments = protect_early_drum_boundaries(segments, stems, timelines)
        segments = apply_stage_clock_soft_splits(segments, stems, timelines)
        durations = [float(segment.duration) for segment in segments]
        median = float(np.median(durations)) if durations else 0.0
        max_duration = max(durations, default=0.0)
        count_error = abs(len(segments) - target_count) / max(1.0, target_count)
        median_error = abs(median - 600.0) / 600.0
        long_fraction = sum(duration > 900.0 for duration in durations) / max(1, len(durations))
        max_duration_penalty = 35.0 * min(max(0.0, max_duration - 900.0) / 600.0, 4.0)
        score = (
            100.0
            - 65.0 * count_error
            - 25.0 * min(median_error, 2.0)
            - 20.0 * long_fraction
            - max_duration_penalty
        )
        sweep.append({
            "threshold_db": threshold,
            "gap_seconds": gap,
            "song_count": len(segments),
            "median_duration_seconds": round(median, 1),
            "score": round(score, 1),
            "max_duration_seconds": round(max_duration, 1),
            "max_duration_penalty": round(max_duration_penalty, 1),
            "stage_clock_inferred_boundaries": sum(
                segment.boundary_source == "stage-clock-inferred" for segment in segments
            ),
            "announcer_confirmed_boundaries": sum(segment.boundary_source == "announcer-confirmed" for segment in segments),
        })
        if best is None or score > best[0]:
            best = (score, threshold, gap, segments)
        report_progress({"current_stage": "calibrating drum fallback", "stage_detail": f"Testing drum silence threshold/gap {index} of {total}", "progress": 92 + int(index / total * 6), "process_progress": int(index / total * 100), "song_progress": int(index / total * 100), "calibration_current": index, "calibration_total": total, "heartbeat": time.time()})
    if best is None:
        return []
    score, threshold, gap, segments = best
    SILENCE_THRESHOLD_DB = threshold
    SILENCE_GAP_SECONDS = gap
    median = float(np.median([float(segment.duration) for segment in segments])) if segments else 0.0
    LAST_DETECTION_CALIBRATION.update({
        "fallback_threshold_db": threshold,
        "fallback_gap_seconds": gap,
        "fallback_score": round(score, 1),
        "fallback_detected_count": len(segments),
        "fallback_median_duration_seconds": round(median, 1),
        "fallback_stage_clock_inferred_boundaries": sum(
            segment.boundary_source == "stage-clock-inferred" for segment in segments
        ),
        "fallback_announcer_confirmed_boundaries": sum(segment.boundary_source == "announcer-confirmed" for segment in segments),
        "fallback_message": f"Drum fallback selected {len(segments)} songs, median {fmt_time(median)} (target ~{target_count} songs / 10:00 each).",
        "fallback_sweep": sweep,
        "fallback_sweep_thresholds_db": list(thresholds),
        "fallback_sweep_gaps_seconds": list(gaps),
    })
    print("DRUM FALLBACK CALIBRATION: " + str(LAST_DETECTION_CALIBRATION), flush=True)
    print("DRUM FALLBACK SWEEP (all candidates):", flush=True)
    print("threshold_db\tgap_seconds\tsong_count\tmedian_duration_seconds\tscore", flush=True)
    for candidate in sweep:
        print(
            f"{candidate['threshold_db']:.1f}\t{candidate['gap_seconds']:.1f}\t"
            f"{candidate['song_count']}\t{candidate['median_duration_seconds']:.1f}\t"
            f"{candidate['score']:.1f}",
            flush=True,
        )
    return segments


def apply_announcer_confirmed_boundaries(
    segments: list[Segment],
    announcer_mask: np.ndarray | None,
) -> list[Segment]:
    """Split drum-core regions at vocal-active/instrument-quiet MC moments."""
    if not segments or announcer_mask is None or len(announcer_mask) == 0:
        return segments
    regions = mask_to_regions(announcer_mask, MC_BREAK_MIN_SECONDS)
    if not regions:
        return segments
    result: list[Segment] = []
    min_piece = max(MC_MIN_FINAL_SONG_SECONDS, MIN_SONG_SECONDS)
    for segment in segments:
        cuts = [end for start, end in regions if start > segment.start + min_piece and end < segment.end - min_piece]
        if not cuts:
            result.append(segment)
            continue
        piece_start = segment.start
        for cut in cuts:
            if cut - piece_start < min_piece or segment.end - cut < min_piece:
                continue
            result.append(Segment(piece_start, cut, core_start=piece_start, core_end=cut, nominal_end=cut, boundary_source=segment.boundary_source))
            piece_start = cut
            segment = replace(segment, boundary_source="announcer-confirmed")
        result.append(Segment(piece_start, segment.end, core_start=piece_start, core_end=segment.end, nominal_end=segment.end, boundary_source=segment.boundary_source))
    return result


def trim_dead_air_segments(
    segments: list[Segment],
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
) -> list[Segment]:
    """Remove variable pre-roll/post-roll while retaining a small musical pad."""
    if not segments or not stems:
        return segments
    # Drum-core activity is the reliable indicator of the band actually
    # starting; guitars/keys can contain room bleed while the MC is still
    # finishing. Fall back to all non-voice stems only when no drums exist.
    activity_stems = [stem for stem in stems if stem.role in {"kick", "snare", "drums"}]
    activity_stems = activity_stems or [stem for stem in stems if stem.role not in {"vocal", "room"}] or stems
    combined = smooth_envelope(combine_normalized_envelope(activity_stems, timelines), 7.0)
    if len(combined) == 0:
        return segments
    activity_db = relative_db(combined)
    # Require several adjacent seconds above a relative floor. This rejects
    # isolated clicks while adapting to each segment's own loudness.
    pad = 2.0
    look = 5 * 60.0
    minimum = max(MIN_SONG_SECONDS, 90.0)
    trimmed: list[Segment] = []
    for segment in segments:
        start_bin = max(0, int(round(segment.start / DETECTION_FRAME_SECONDS)))
        end_bin = min(len(activity_db), int(round(segment.end / DETECTION_FRAME_SECONDS)))
        if end_bin - start_bin < 2:
            trimmed.append(segment)
            continue
        lo = start_bin
        hi = min(end_bin, start_bin + int(round(look / DETECTION_FRAME_SECONDS)))
        window = activity_db[lo:hi]
        threshold = max(-18.0, float(np.percentile(window, 75)) - 26.0) if len(window) else -18.0
        active = smooth_boolean_activity(window > threshold, 8.0)
        start_candidates = mask_to_regions(active, 8.0)
        new_start = segment.start
        if start_candidates:
            first = start_candidates[0][0] + segment.start
            if first > segment.start + 4.0:
                new_start = max(segment.start, first - pad)
        end_lo = max(start_bin, end_bin - int(round(look / DETECTION_FRAME_SECONDS)))
        end_window = activity_db[end_lo:end_bin]
        end_threshold = max(-18.0, float(np.percentile(end_window, 75)) - 26.0) if len(end_window) else -18.0
        end_active = smooth_boolean_activity(end_window > end_threshold, 8.0)
        end_candidates = mask_to_regions(end_active, 8.0)
        new_end = segment.end
        if end_candidates:
            last = end_candidates[-1][1] + end_lo * DETECTION_FRAME_SECONDS
            if last < segment.end - 4.0:
                new_end = min(segment.end, last + pad)
        if new_end - new_start < minimum:
            new_start, new_end = segment.start, segment.end
        start_trim = max(0.0, new_start - segment.start)
        end_trim = max(0.0, segment.end - new_end)
        updated = replace(segment, start=new_start, end=new_end, nominal_end=new_end, trimmed_start_seconds=start_trim, trimmed_end_seconds=end_trim)
        trimmed.append(updated)
        print(f"DEAD-AIR TRIM song candidate {fmt_time(segment.start)}: start -{start_trim:.1f}s, end -{end_trim:.1f}s", flush=True)
    return trimmed


def _boundary_activity(
    cut_seconds: float,
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
    radius_seconds: float = 10.0,
    diagnostic: bool = False,
) -> tuple[bool, list[str], list[str]]:
    """Classify a cut using sustained, simultaneous per-stem activity.

    A boundary is safe only when every instrument is quiet.  At most one
    vocal/room stem may remain active, which covers an announcer speaking
    into one mic without mistaking a full-band overlap for a transition.
    """
    center = int(round(cut_seconds / DETECTION_FRAME_SECONDS))
    radius = max(1, int(round(radius_seconds / DETECTION_FRAME_SECONDS)))
    active_instruments: list[str] = []
    active_voices: list[str] = []
    for stem in stems:
        env = timelines.get(stem.path.name)
        if env is None or len(env) == 0:
            continue
        lo = max(0, center - radius)
        hi = min(len(env), center + radius + 1)
        if hi <= lo:
            continue
        # The general detector threshold is intentionally sensitive and is
        # not suitable for this safety check: long recordings often contain
        # a low-level noise/tail floor that would make every stem appear
        # active.  Establish a local noise-relative floor instead, with a
        # conservative -60 dBFS minimum.
        env_db = 20.0 * np.log10(np.maximum(env, 1e-9))
        threshold_db = max(-62.0, float(np.percentile(env_db, 90)) - 35.0)
        threshold = db_to_amp(threshold_db)
        values = env[lo:hi]
        # Sustained means activity across most of the validation window, not
        # a single transient landing exactly on the proposed timestamp.
        activity_fraction = float(np.count_nonzero(values > threshold)) / len(values)
        sustained = activity_fraction >= 0.60
        if diagnostic:
            print(
                f"BOUNDARY ACTIVITY stem={stem.path.name} role={stem.role} "
                f"median={float(np.median(values)):.6f} "
                f"median_db={amp_to_db(float(np.median(values))):.1f} "
                f"peak_db={amp_to_db(float(np.max(values))):.1f} "
                f"threshold_db={threshold_db:.1f} sustained={activity_fraction:.0%}",
                flush=True,
            )
        if not sustained:
            continue
        (active_voices if stem.role in {"vocal", "room"} else active_instruments).append(stem.path.name)
    return not active_instruments and len(active_voices) <= 1, active_instruments, active_voices


def _nearest_valid_boundary(
    proposed: float,
    lower: float,
    upper: float,
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
    search_seconds: float = 300.0,
    validation_radius_seconds: float = 10.0,
) -> tuple[float | None, list[str], list[str]]:
    if upper < lower:
        return None, [], []
    step = DETECTION_FRAME_SECONDS
    max_offset = int(round(search_seconds / step))
    proposed = min(max(proposed, lower), upper)
    for offset in range(max_offset + 1):
        candidates = [proposed] if offset == 0 else [proposed - offset * step, proposed + offset * step]
        for candidate in candidates:
            if candidate < lower or candidate > upper:
                continue
            valid, instruments, voices = _boundary_activity(
                candidate, stems, timelines, radius_seconds=validation_radius_seconds
            )
            if valid:
                return candidate, instruments, voices
    return None, [], []




def discard_empty_segments(
    segments: list[Segment],
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
    floor_dbfs: float = -90.0,
) -> list[Segment]:
    """Drop detector blocks that contain no recorded audio on any stem.

    BWF alignment can add a virtual tail to the common timeline.  Silence in
    that tail must not become a renderable song or make the full render abort.
    """
    if not segments or not stems:
        return segments
    kept: list[Segment] = []
    floor = db_to_amp(floor_dbfs)
    for index, segment in enumerate(segments, 1):
        lo = max(0, int(round(segment.start / DETECTION_FRAME_SECONDS)))
        hi = min(
            max((len(timelines.get(stem.path.name, ())) for stem in stems), default=0),
            int(round(segment.end / DETECTION_FRAME_SECONDS)) + 1,
        )
        peaks = []
        sustained = []
        for stem in stems:
            values = timelines.get(stem.path.name, np.array([], dtype=np.float32))[lo:hi]
            if len(values):
                peaks.append(float(np.max(values)))
                sustained.append(float(np.count_nonzero(values >= floor)) / len(values))
        peak = max(peaks, default=0.0)
        # Only discard a block when every stem is below the floor.  Weak or
        # sparse real performances must remain renderable; the renderer's
        # per-stem activity gate handles those without deleting the song.
        if peak < floor:
            print(
                f"EMPTY SEGMENT discarded song candidate {index}: "
                f"{fmt_time(segment.start)}-{fmt_time(segment.end)}; "
                f"no stem sustained above {floor_dbfs:.0f} dBFS",
                flush=True,
            )
            continue
        kept.append(segment)
    return kept


def auto_retry_bad_detection(
    segments: list[Segment],
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
    session_end: float,
    voice_stems: list[Stem],
    instrument_stems: list[Stem],
    mc_data: dict[str, np.ndarray | dict[str, float]],
) -> list[Segment]:
    """Retry only suspiciously merged blocks, with a bounded escalation cap."""
    target = int(KNOWN_SONG_COUNT or max(20, round(session_end / 600.0)))
    attempts: list[dict[str, object]] = []
    current = segments
    for attempt in range(1, 3):
        oversized = [seg for seg in current if seg.duration > 20 * 60]
        low_count = len(current) < (0.85 * target if KNOWN_SONG_COUNT else 0.70 * target)
        if DETECTION_RESCAN_MODE and len(current) < target:
            low_count = True
        if not oversized and not low_count:
            break
        if not oversized:
            # A confirmed-count shortfall means that missed boundaries are
            # suspected even when no single window exceeds 20 minutes. Scan
            # every suspicious/long gap with the tighter acoustic sweep.
            oversized = [seg for seg in current if seg.duration > 11 * 60]
            if not oversized:
                attempts.append({"attempt": attempt, "status": "no suspicious regions to focus", "song_count": len(current)})
                break
        tighter = focused_retry_split_oversized(
            oversized, stems, timelines, voice_stems, instrument_stems, mc_data
        )
        replacements = [seg for seg in current if seg not in oversized] + tighter
        if len(replacements) <= len(current):
            attempts.append({"attempt": attempt, "status": "no improvement", "song_count": len(current)})
            break
        current = replacements
        attempts.append({"attempt": attempt, "status": "improved", "song_count": len(current), "oversized_before": len(oversized)})
    if attempts:
        DETECTION_STRATEGY["auto_retry"] = {"attempts": attempts, "cap": 2, "final_red_flags": {"low_count": len(current) < 0.70 * target, "oversized_segments": sum(seg.duration > 20 * 60 for seg in current)}}
        print("AUTO-RETRY: " + str(DETECTION_STRATEGY["auto_retry"]), flush=True)
    return current


def focused_retry_split_oversized(
    oversized: list[Segment],
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
    voice_stems: list[Stem],
    instrument_stems: list[Stem],
    mc_data: dict[str, np.ndarray | dict[str, float]],
) -> list[Segment]:
    """Re-scan only merged blocks with a tighter drum-gap sweep."""
    drum_stems = drum_equivalent_stems(stems)
    if not drum_stems:
        return []
    energy_db = relative_db(smooth_envelope(combine_normalized_envelope(drum_stems, timelines), DETECTION_SMOOTH_SECONDS))
    announcer_mask = smooth_boolean_activity(
        np.asarray(mc_data["voice_present"], dtype=bool) & np.asarray(mc_data["instrument_quiet"], dtype=bool),
        MC_BREAK_SMOOTH_SECONDS,
    )
    best: list[Segment] = []
    for threshold in (-5.0, -8.0, -10.0, -12.0, -15.0):
        for gap in (2.0, 4.0, 6.0, 8.0, 10.0):
            candidate: list[Segment] = []
            for original in oversized:
                lo = max(0, int(original.start / DETECTION_FRAME_SECONDS))
                hi = min(len(energy_db), int(original.end / DETECTION_FRAME_SECONDS) + 1)
                if hi <= lo:
                    continue
                local = active_mask_to_segments(energy_db[lo:hi] > threshold, gap)
                for part in local:
                    start = original.start + part.start
                    end = min(original.end, original.start + part.end)
                    if end - start >= MC_MIN_FINAL_SONG_SECONDS:
                        candidate.append(Segment(start, end, core_start=start, core_end=end, nominal_end=end))
            candidate = apply_announcer_confirmed_boundaries(candidate, announcer_mask)
            candidate = protect_early_drum_boundaries(candidate, stems, timelines)
            candidate = apply_stage_clock_soft_splits(candidate, stems, timelines)
            if len(candidate) > len(best) or (len(candidate) == len(best) and max((s.duration for s in candidate), default=0) < max((s.duration for s in best), default=float("inf"))):
                best = candidate
    print(f"AUTO-RETRY focused sweep: {len(oversized)} oversized block(s) -> {len(best)} pieces", flush=True)
    return best


def sustained_drum_start(
    segment: Segment,
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
    minimum_seconds: float = 18.0,
) -> float:
    """Return the first sustained rhythm start, ignoring warm-up hits/fills."""
    drum_stems = drum_equivalent_stems(stems)
    if not drum_stems:
        return segment.start
    energy = relative_db(smooth_envelope(combine_normalized_envelope(drum_stems, timelines), 3.0))
    lo = max(0, int(round(segment.start / DETECTION_FRAME_SECONDS)))
    hi = min(len(energy), int(round(segment.end / DETECTION_FRAME_SECONDS)))
    if hi <= lo:
        return segment.start
    active = smooth_boolean_activity(energy[lo:hi] > -18.0, 3.0)
    regions = mask_to_regions(active, minimum_seconds)
    if not regions:
        return segment.start
    return segment.start + float(regions[0][0])


def protect_early_drum_boundaries(
    segments: list[Segment],
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
) -> list[Segment]:
    """Do not accept an early drum pause when other instruments continue.

    A drum-only gap before the first ~9 minutes of the stage clock is a
    breakdown/fill by default.  It remains a boundary only when the shared
    cut satisfies the full-band boundary rule.
    """
    if len(segments) < 2:
        return segments
    result: list[Segment] = []
    for segment in segments:
        if not result:
            result.append(segment)
            continue
        left = result[-1]
        proposed = left.end if abs(left.end - segment.start) <= 8.0 else (left.end + segment.start) / 2.0
        clock_start = sustained_drum_start(left, stems, timelines)
        early = proposed - clock_start < 9.0 * 60.0
        valid, instruments, voices = _boundary_activity(proposed, stems, timelines)
        if early and not valid:
            print(
                f"STAGE CLOCK: merging early drum pause at {fmt_time(proposed)} "
                f"({fmt_time(max(0.0, proposed - clock_start))} after sustained drums; "
                f"instruments={instruments}, vocal/mic={voices})",
                flush=True,
            )
            result[-1] = replace(left, end=segment.end, core_end=segment.core_end or segment.end, nominal_end=segment.end)
        else:
            result.append(segment)
    return result


def apply_stage_clock_soft_splits(
    segments: list[Segment],
    stems: list[Stem],
    timelines: dict[str, np.ndarray],
) -> list[Segment]:
    """Add inferred cuts inside long drum regions using the stage-clock prior.

    Drum-silence segments remain intact unless they exceed roughly 13 minutes.
    For those long regions, choose the quietest combined drum/instrument point
    near 12 minutes after sustained rhythm begins and mark the new segment as
    inferred.  This avoids treating a warm-up hit or early breakdown as t=0.
    """
    if not segments or not stems:
        return segments
    combined = combine_normalized_envelope(stems, timelines)
    if len(combined) == 0:
        return segments
    combined = smooth_envelope(combined, 5.0)
    max_long_seconds = 13.0 * 60.0
    target_seconds = 12.0 * 60.0
    search_radius_seconds = 90.0
    min_piece_seconds = max(90.0, MIN_SONG_SECONDS)
    result: list[Segment] = []
    for original in segments:
        start = float(original.start)
        end = float(original.end)
        if end - start <= max_long_seconds:
            result.append(original)
            continue
        piece_start = start
        inherited_source = original.boundary_source
        while end - piece_start > max_long_seconds:
            clock_start = sustained_drum_start(Segment(piece_start, end), stems, timelines)
            target = clock_start + target_seconds
            lower = max(piece_start + min_piece_seconds, clock_start + 9.0 * 60.0, target - search_radius_seconds)
            upper = min(end - min_piece_seconds, target + search_radius_seconds)
            if upper <= lower:
                break
            lower_bin = max(0, int(round(lower / DETECTION_FRAME_SECONDS)))
            upper_bin = min(len(combined), int(round(upper / DETECTION_FRAME_SECONDS)) + 1)
            if upper_bin <= lower_bin:
                break
            split_bin = lower_bin + int(np.argmin(combined[lower_bin:upper_bin]))
            split = split_bin * DETECTION_FRAME_SECONDS
            if split <= piece_start + min_piece_seconds or split >= end - min_piece_seconds:
                break
            result.append(
                Segment(
                    piece_start,
                    split,
                    core_start=piece_start,
                    core_end=split,
                    nominal_end=split,
                    boundary_source=inherited_source,
                )
            )
            piece_start = split
            inherited_source = "stage-clock-inferred"
        result.append(
            Segment(
                piece_start,
                end,
                core_start=piece_start,
                core_end=end,
                nominal_end=end,
                boundary_source=inherited_source,
            )
        )
    return result




def place_envelope_on_timeline(stem: Stem, env: np.ndarray, max_bins: int) -> np.ndarray:
    # Cache and reload at the precision used by detection.  Float32 here can
    # move borderline activity across a threshold between fresh and cached
    # runs.
    timeline = np.zeros(max_bins, dtype=np.float64)
    if len(env) == 0:
        return timeline
    start_bin = int(round(stem.offset_seconds / DETECTION_FRAME_SECONDS))
    end_bin = min(max_bins, start_bin + len(env))
    if end_bin > start_bin:
        timeline[start_bin:end_bin] = env[: end_bin - start_bin]
    return timeline


def combine_normalized_envelope(selected_stems: list[Stem], timelines: dict[str, np.ndarray]) -> np.ndarray:
    if not selected_stems:
        return np.array([], dtype=np.float64)
    combined = np.zeros_like(timelines[selected_stems[0].path.name], dtype=np.float64)
    contributors = np.zeros_like(combined, dtype=np.float64)
    for stem in selected_stems:
        env = timelines[stem.path.name]
        active = env[env > 1e-9]
        scale = float(np.percentile(active, 95)) if len(active) else 1.0
        scale = max(scale, 1e-9)
        combined += np.clip(env / scale, 0.0, 4.0)
        contributors += 1.0
    return combined / np.maximum(contributors, 1.0)


def smooth_envelope(env: np.ndarray, smooth_seconds: float) -> np.ndarray:
    bins = max(1, int(round(smooth_seconds / DETECTION_FRAME_SECONDS)))
    if bins <= 1 or len(env) == 0:
        return env
    kernel = np.ones(bins, dtype=np.float64) / bins
    return np.convolve(np.asarray(env, dtype=np.float64), kernel, mode="same")


def relative_db(env: np.ndarray) -> np.ndarray:
    max_energy = max(float(np.max(env)), 1e-9)
    return 20.0 * np.log10(np.maximum(env / max_energy, 1e-9))


def print_energy_quantiles(label: str, energy_db: np.ndarray) -> None:
    quantiles = np.percentile(energy_db, [1, 5, 10, 25, 50, 75, 90, 95, 99])
    print(
        f"{label}:",
        " ".join(f"p{p:g}={v:.1f}" for p, v in zip([1, 5, 10, 25, 50, 75, 90, 95, 99], quantiles)),
    )


def print_candidate_gaps(active: np.ndarray, gap_seconds: float) -> None:
    print("\nCandidate close-drum gaps:")
    gap_bins = max(1, int(round(gap_seconds / DETECTION_FRAME_SECONDS)))
    found = False
    i = 0
    n = len(active)
    while i < n:
        while i < n and active[i]:
            i += 1
        start = i
        while i < n and not active[i]:
            i += 1
        end = i
        if end > start and start > 0 and end < n:
            found = True
            duration = (end - start) * DETECTION_FRAME_SECONDS
            marker = "CUT" if end - start >= gap_bins else "short"
            print(
                f"  {fmt_time(start * DETECTION_FRAME_SECONDS)} - "
                f"{fmt_time(end * DETECTION_FRAME_SECONDS)}  {fmt_time(duration)}  {marker}"
            )
    if not found:
        print("  none")


def collect_candidate_gaps(active: np.ndarray) -> list[tuple[float, float, float]]:
    gaps: list[tuple[float, float, float]] = []
    i = 0
    n = len(active)
    while i < n:
        while i < n and active[i]:
            i += 1
        start = i
        while i < n and not active[i]:
            i += 1
        end = i
        if end > start and start > 0 and end < n:
            gaps.append(
                (
                    start * DETECTION_FRAME_SECONDS,
                    end * DETECTION_FRAME_SECONDS,
                    (end - start) * DETECTION_FRAME_SECONDS,
                )
            )
    return gaps


def per_stem_activity_threshold(env: np.ndarray, stem: Stem) -> float:
    active_region = env[env > 1e-9]
    if len(active_region) == 0:
        return db_to_amp(-80.0)
    db = 20.0 * np.log10(np.maximum(active_region, 1e-9))
    floor = float(np.percentile(db, 15))
    high = float(np.percentile(db, 90))
    if high - floor >= 18.0:
        threshold_db = floor + 0.45 * (high - floor)
    else:
        threshold_db = floor + 8.0
    if stem.role in {"kick", "snare", "drums"}:
        threshold_db -= 2.0
    return db_to_amp(threshold_db)


def combine_activity_masks(
    selected_stems: list[Stem],
    timelines: dict[str, np.ndarray],
    thresholds: dict[str, float],
) -> np.ndarray:
    if not selected_stems:
        return np.array([], dtype=bool)
    masks = []
    for stem in selected_stems:
        env = timelines[stem.path.name]
        threshold = thresholds[stem.path.name]
        masks.append(env > threshold)
    return np.any(np.vstack(masks), axis=0)


def smooth_boolean_activity(activity: np.ndarray, smooth_seconds: float) -> np.ndarray:
    bins = max(1, int(round(smooth_seconds / DETECTION_FRAME_SECONDS)))
    if bins <= 1 or len(activity) == 0:
        return activity
    kernel = np.ones(bins, dtype=np.float64) / bins
    density = np.convolve(activity.astype(np.float64), kernel, mode="same")
    return density >= 0.25


def active_mask_to_segments(active: np.ndarray, gap_seconds: float) -> list[Segment]:
    gap_bins = max(1, int(round(gap_seconds / DETECTION_FRAME_SECONDS)))
    segments: list[Segment] = []
    i = 0
    n = len(active)
    while i < n:
        while i < n and not active[i]:
            i += 1
        if i >= n:
            break
        start = i
        last_active = i
        silence_run = 0
        i += 1
        while i < n:
            if active[i]:
                last_active = i
                silence_run = 0
            else:
                silence_run += 1
                if silence_run >= gap_bins:
                    break
            i += 1
        end = last_active + 1
        segments.append(Segment(start * DETECTION_FRAME_SECONDS, end * DETECTION_FRAME_SECONDS))
    return segments


def print_detection_thresholds(stems: list[Stem], thresholds: dict[str, float]) -> None:
    print("\nPer-stem activity thresholds:")
    for stem in stems:
        print(f"  {stem.path.name:24s} role={stem.role:8s} threshold={amp_to_db(thresholds[stem.path.name]):6.1f} dBFS")


def cached_close_drum_energy_db(stems: list[Stem]) -> np.ndarray:
    cache = ensure_detection_cache(stems, "Calibration")
    max_bins = int(math.ceil(max(s.offset_seconds + s.timeline_duration for s in stems) / DETECTION_FRAME_SECONDS))
    timelines = {
        stem.path.name: place_envelope_on_timeline(stem, cache.get(stem.path.name, np.array([], dtype=np.float32)), max_bins)
        for stem in stems
    }
    drum_stems = drum_equivalent_stems(stems)
    if not drum_stems:
        raise SystemExit("Calibration requires BD/kick and/or snare close mic stems.")
    drum_energy = combine_normalized_envelope(drum_stems, timelines)
    drum_energy = smooth_envelope(drum_energy, DETECTION_SMOOTH_SECONDS)
    return relative_db(drum_energy)


def run_calibration(stems: list[Stem]) -> None:
    drum_energy_db = cached_close_drum_energy_db(stems)
    print("\nCALIBRATION: close-drum core from cached per-stem envelopes")
    print_energy_quantiles("Close-drum energy dB relative to max", drum_energy_db)
    thresholds = [-15, -20, -25, -30, -35]
    gaps = [4, 6, 8, 10, 12]
    print("\nSweep summary")
    print("  threshold  gap  songs  min_len  median_len  max_len")
    for threshold in thresholds:
        for gap in gaps:
            active = drum_energy_db > threshold
            segments = active_mask_to_segments(active, gap)
            segments = merge_or_discard_short_segments_with_min(segments, 90.0)
            segments = merge_close_segments(segments, gap)
            durations = [seg.duration for seg in segments]
            if durations:
                min_len = fmt_time(float(np.min(durations)))
                med_len = fmt_time(float(np.median(durations)))
                max_len = fmt_time(float(np.max(durations)))
            else:
                min_len = med_len = max_len = "00:00:00.00"
            print(f"  {threshold:>9}  {gap:>3}  {len(segments):>5}  {min_len:>8}  {med_len:>10}  {max_len:>8}")

    region_start = 5 * 60 + 3
    region_end = 6 * 60 + 4
    print("\nCandidate gaps inside 05:03-06:04 region")
    for threshold in thresholds:
        active = drum_energy_db > threshold
        gaps_in_region = [
            gap for gap in collect_candidate_gaps(active)
            if gap[1] >= region_start and gap[0] <= region_end
        ]
        print(f"  threshold {threshold} dB:")
        if not gaps_in_region:
            print("    none")
            continue
        for start, end, duration in gaps_in_region:
            print(f"    {fmt_time(start)} - {fmt_time(end)}  {fmt_time(duration)}")


def extend_segments_from_non_drums(
    core_segments: list[Segment],
    pre_stems: list[Stem],
    post_stems: list[Stem],
    timelines: dict[str, np.ndarray],
    thresholds: dict[str, float],
    session_end: float,
) -> list[Segment]:
    if not core_segments:
        return []
    if not pre_stems and not post_stems:
        return [Segment(seg.start, seg.end, core_start=seg.start, core_end=seg.end) for seg in core_segments]
    pre_activity = smooth_boolean_activity(
        combine_activity_masks(pre_stems, timelines, thresholds),
        EDGE_EXTENSION_MIN_ACTIVITY_SECONDS,
    ) if pre_stems else np.zeros_like(next(iter(timelines.values())), dtype=bool)
    post_activity = smooth_boolean_activity(
        combine_activity_masks(post_stems, timelines, thresholds),
        EDGE_EXTENSION_MIN_ACTIVITY_SECONDS,
    ) if post_stems else np.zeros_like(next(iter(timelines.values())), dtype=bool)
    extended: list[Segment] = []
    look_bins = int(round(EDGE_EXTENSION_LOOK_SECONDS / DETECTION_FRAME_SECONDS))
    for seg in core_segments:
        core_start_bin = int(round(seg.start / DETECTION_FRAME_SECONDS))
        core_end_bin = int(round(seg.end / DETECTION_FRAME_SECONDS))
        start_bin = core_start_bin
        scan_start = max(0, core_start_bin - look_bins)
        active_before = np.flatnonzero(pre_activity[scan_start:core_start_bin])
        if len(active_before):
            start_bin = scan_start + int(active_before[0])

        end_bin = core_end_bin
        scan_end = min(len(post_activity), core_end_bin + look_bins)
        active_after = np.flatnonzero(post_activity[core_end_bin:scan_end])
        if len(active_after):
            end_bin = core_end_bin + int(active_after[-1]) + 1

        extended.append(
            Segment(
                max(0.0, start_bin * DETECTION_FRAME_SECONDS),
                min(session_end, end_bin * DETECTION_FRAME_SECONDS),
                core_start=seg.start,
                core_end=seg.end,
            )
        )
    return extended


def attach_post_song_mic_activity(
    segments: list[Segment],
    mic_stems: list[Stem],
    timelines: dict[str, np.ndarray],
    thresholds: dict[str, float],
    session_end: float,
) -> list[Segment]:
    if not segments or not mic_stems:
        return segments
    mic_activity = combine_activity_masks(mic_stems, timelines, thresholds)
    mic_activity = smooth_boolean_activity(mic_activity, POST_SONG_MIC_MIN_ACTIVITY_SECONDS)
    look_bins = int(round(POST_SONG_MIC_LOOK_SECONDS / DETECTION_FRAME_SECONDS))
    annotated: list[Segment] = []
    for seg in segments:
        end_bin = int(round(seg.end / DETECTION_FRAME_SECONDS))
        scan_end = min(len(mic_activity), end_bin + look_bins)
        active = np.flatnonzero(mic_activity[end_bin:scan_end])
        if len(active):
            post_start = end_bin + int(active[0])
            post_end = end_bin + int(active[-1]) + 1
            annotated.append(
                Segment(
                    seg.start,
                    seg.end,
                    core_start=seg.core_start,
                    core_end=seg.core_end,
                    post_mic_start=post_start * DETECTION_FRAME_SECONDS,
                    post_mic_end=min(session_end, post_end * DETECTION_FRAME_SECONDS),
                )
            )
        else:
            annotated.append(seg)
    return annotated


def cache_signature(stems: list[Stem]) -> str:
    parts = [DETECTION_CACHE_ALGORITHM_VERSION]
    for stem in stems:
        stat = stem.path.stat()
        parts.append(
            f"{stem.path.name}:{stem.frames}:{stem.samplerate}:"
            f"{stem.timeline_frames}:{stem.offset_seconds:.9f}:{int(stat.st_mtime)}"
        )
    return "|".join(parts)


def load_detection_cache(stems: list[Stem]) -> dict[str, np.ndarray]:
    cache_path = detection_cache_path()
    candidates = [cache_path]
    if LEGACY_DETECTION_CACHE and LEGACY_DETECTION_CACHE != cache_path:
        candidates.append(LEGACY_DETECTION_CACHE)
    try:
        for candidate in candidates:
            if not candidate.exists():
                continue
            with np.load(candidate, allow_pickle=False) as data:
                if str(data["signature"]) != cache_signature(stems):
                    continue
                cache = {name: data[f"env_{i}"] for i, name in enumerate(data["names"].tolist())}
            if candidate != cache_path:
                print(f"Migrating legacy detection envelope cache {candidate} -> {cache_path}", flush=True)
                save_detection_cache(stems, cache)
            return cache
    except Exception:
        pass
    return {}


def build_detection_cache(stems: list[Stem]) -> None:
    """Read each source stem once and save its complete 1-second envelope."""
    if not stems:
        raise RuntimeError("Cannot build a detection cache without source stems.")
    session_start = min(stem.offset_seconds for stem in stems)
    session_end = max(stem.offset_seconds + stem.timeline_duration for stem in stems)
    full_session = Segment(session_start, session_end)
    total_bytes = sum(max(0, stem.path.stat().st_size) for stem in stems)
    bytes_done = 0
    started = time.perf_counter()
    external_source = str(SOURCE_DIR).startswith(("/Volumes/", "/Network/", "smb://", "afp://"))
    report_progress(
        {
            "current_stage": "scanning folder",
            "stage_detail": (
                f"external volume — scanning audio chunks; 0 of {len(stems)} stems"
                if external_source
                else f"scanning audio chunks; 0 of {len(stems)} stems"
            ),
            "progress": 20,
            "song_progress": 20,
            "heartbeat": time.time(),
            "elapsed_seconds": 0.0,
            "bytes_read": 0,
            "bytes_total": total_bytes,
        }
    )
    envelopes: dict[str, np.ndarray] = {}
    for stem_index, stem in enumerate(stems, 1):
        stem_size = max(0, stem.path.stat().st_size)

        def on_progress(update: dict[str, object]) -> None:
            nonlocal bytes_done
            ratio = min(1.0, float(update.get("frames_read") or 0) / max(1, stem.frames))
            bytes_read = min(stem_size, int(stem_size * ratio))
            total_read = min(total_bytes, bytes_done + bytes_read)
            elapsed = max(0.001, time.perf_counter() - started)
            overall = total_read / max(1, total_bytes)
            eta = elapsed * (1.0 - overall) / overall if overall > 0 else None
            percent = 20 + int(overall * 60)
            detail = (
                f"Analyzing {stem_index} of {len(stems)}: {stem.path.name} — "
                f"{overall * 100:.1f}% ({total_read / 1048576:.0f}/{total_bytes / 1048576:.0f} MB) "
                f"· {elapsed / 60:.1f} min elapsed"
            )
            if eta is not None:
                detail += f" · about {eta / 60:.1f} min remaining"
            report_progress(
                {
                    "current_stage": "scanning folder",
                    "stage_detail": detail,
                    "progress": percent,
                    "song_progress": percent,
                    "heartbeat": time.time(),
                    "elapsed_seconds": elapsed,
                    "eta_seconds": eta,
                    "bytes_read": total_read,
                    "bytes_total": total_bytes,
                }
            )

        _rms, _energies, _has_audio, _spread, stem_envelopes, _peaks = scan_segment_activity(
            [stem], full_session, stem.samplerate, chunk_seconds=10.0, progress_callback=on_progress
        )
        envelopes.update(stem_envelopes)
        bytes_done += stem_size
        report_progress(
            {
                "current_stage": "scanning folder",
                "stage_detail": f"Finished {stem_index} of {len(stems)}: {stem.path.name}",
                "progress": 20 + int(bytes_done / max(1, total_bytes) * 60),
                "song_progress": 20 + int(bytes_done / max(1, total_bytes) * 60),
                "heartbeat": time.time(),
                "elapsed_seconds": time.perf_counter() - started,
                "bytes_read": bytes_done,
                "bytes_total": total_bytes,
            }
        )
    save_detection_cache(stems, envelopes)
    report_progress(
        {
            "current_stage": "loading songs",
            "stage_detail": "Audio scan complete — loading songs from the scanned envelopes",
            "progress": 82,
            "song_progress": 82,
            "heartbeat": time.time(),
            "elapsed_seconds": time.perf_counter() - started,
            "eta_seconds": 0.0,
            "bytes_read": total_bytes,
            "bytes_total": total_bytes,
        }
    )


def save_detection_cache(stems: list[Stem], cache: dict[str, np.ndarray]) -> None:
    cache_path = detection_cache_path()
    names = sorted(cache)
    payload = {
        "signature": np.array(cache_signature(stems)),
        "names": np.array(names),
        "source_dir": np.array(str(SOURCE_DIR.expanduser().resolve())),
    }
    for i, name in enumerate(names):
        payload[f"env_{i}"] = cache[name]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    # Pass an open file so NumPy cannot silently append a second `.npz` suffix.
    tmp_path = cache_path.with_name(f".{cache_path.name}.tmp.npz")
    with tmp_path.open("wb") as handle:
        np.savez_compressed(handle, **payload)
        handle.flush()
        os.fsync(handle.fileno())
    tmp_path.replace(cache_path)
    print(f"Saved detection envelope cache: {cache_path}", flush=True)


def merge_or_discard_short_segments(segments: list[Segment]) -> list[Segment]:
    return merge_or_discard_short_segments_with_min(segments, MIN_SONG_SECONDS)


def merge_or_discard_short_segments_with_min(segments: list[Segment], min_seconds: float) -> list[Segment]:
    if not segments:
        return []
    result: list[Segment] = []
    for seg in segments:
        if seg.duration >= min_seconds:
            result.append(seg)
            continue
        if result:
            prev = result[-1]
            result[-1] = merge_segment_pair(prev, seg)
        else:
            result.append(seg)
    return [s for s in result if s.duration >= min_seconds]


def merge_close_segments(segments: list[Segment], max_gap_seconds: float) -> list[Segment]:
    if not segments:
        return []
    merged = [segments[0]]
    for seg in segments[1:]:
        prev = merged[-1]
        if seg.start - prev.end < max_gap_seconds:
            merged[-1] = merge_segment_pair(prev, seg)
        else:
            merged.append(seg)
    return merged


def merge_segment_pair(a: Segment, b: Segment) -> Segment:
    core_starts = [v for v in (a.core_start, b.core_start) if v is not None]
    core_ends = [v for v in (a.core_end, b.core_end) if v is not None]
    return Segment(
        min(a.start, b.start),
        max(a.end, b.end),
        core_start=min(core_starts) if core_starts else None,
        core_end=max(core_ends) if core_ends else None,
        post_mic_start=a.post_mic_start or b.post_mic_start,
        post_mic_end=a.post_mic_end or b.post_mic_end,
        boundary_source=a.boundary_source if a.boundary_source != "drum-silence" else b.boundary_source,
    )


def pad_segments(segments: list[Segment], session_end: float) -> list[Segment]:
    padded: list[Segment] = []
    for seg in segments:
        start = max(0.0, seg.start - SEGMENT_PRE_PAD_SECONDS)
        end = min(session_end, seg.end + SEGMENT_POST_PAD_SECONDS)
        padded.append(
            Segment(
                start,
                end,
                core_start=seg.core_start,
                core_end=seg.core_end,
                post_mic_start=seg.post_mic_start,
                post_mic_end=seg.post_mic_end,
                boundary_source=seg.boundary_source,
            )
        )
    return padded


def print_detected_segments(segments: list[Segment]) -> None:
    print("\nDetected songs before processing:")
    if not segments:
        print("  No segments detected. Try raising SILENCE_THRESHOLD_DB or lowering MIN_SONG_SECONDS.")
        return
    print("  #   preceding transition       music bounds                duration      flags")
    for idx, seg in enumerate(segments, 1):
        mc = f"{fmt_time(seg.mc_start)} - {fmt_time(seg.mc_end)}" if seg.mc_start is not None and seg.mc_end is not None else "n/a"
        nominal_end = seg.nominal_end if seg.nominal_end is not None else seg.end
        duration = nominal_end - seg.start
        flags = []
        if duration < SUSPICIOUS_SHORT_SONG_SECONDS:
            flags.append("SUSPICIOUS <5m")
        if duration > SUSPICIOUS_LONG_SONG_SECONDS:
            flags.append("SUSPICIOUS >20m")
        if seg.boundary_source == "stage-clock-inferred":
            flags.append("boundary inferred from stage-clock, not silence")
        if seg.boundary_source == "announcer-confirmed":
            flags.append("confirmed announcer: vocal active + instruments quiet")
        if seg.trimmed_start_seconds or seg.trimmed_end_seconds:
            flags.append(f"dead-air trimmed start -{seg.trimmed_start_seconds:.1f}s/end -{seg.trimmed_end_seconds:.1f}s")
        if seg.end > nominal_end:
            flags.append(f"render tail to {fmt_time(seg.end)}")
        flag_text = ", ".join(flags) if flags else "-"
        print(f"  {idx:02d}. {mc:27s} {fmt_time(seg.start)} - {fmt_time(nominal_end):11s} {fmt_time(duration):>11s}  {flag_text}")


def build_per_song_flattening(
    segment_envelopes: dict[str, np.ndarray],
) -> dict[str, dict[str, object]]:
    """Create slow, bounded per-song gain curves from each stem's own envelope."""
    result: dict[str, dict[str, object]] = {}
    for name, values in segment_envelopes.items():
        if len(values) < 4:
            result[name] = {"curve": np.ones(max(1, len(values)), dtype=np.float32), "range_db": 0.0, "reference_db": -120.0}
            continue
        safe = np.nan_to_num(np.asarray(values, dtype=np.float64), nan=1e-9, posinf=1e3, neginf=1e-9)
        env_db = 20.0 * np.log10(np.maximum(safe, 1e-9))
        reference = float(np.percentile(env_db, 90))
        # Slow smoothing follows the performance without hard-gating or pumping.
        desired_db = np.clip(reference - env_db, 0.0, 6.0)
        window = max(3, int(round(12.0 / DETECTION_FRAME_SECONDS)))
        kernel = np.ones(window, dtype=np.float64) / window
        curve_db = np.convolve(desired_db, kernel, mode="same")
        curve_db = np.clip(curve_db, 0.0, 6.0)
        result[name] = {
            "curve": np.power(10.0, curve_db / 20.0).astype(np.float32),
            "range_db": float(np.percentile(curve_db, 95) - np.percentile(curve_db, 5)),
            "reference_db": reference,
        }
    return result


def timestamp_label() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def cleanup_report_history(out_dir: Path, keep: int = 10) -> None:
    report_paths = sorted(
        [*out_dir.glob("detection_*.txt"), *out_dir.glob("mix_report_*.txt")],
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for path in report_paths[keep:]:
        try:
            path.unlink()
        except OSError as exc:
            print(f"WARNING: could not clean old report {path}: {exc}", flush=True)


def write_detection_outputs(out_dir: Path, segments: list[Segment]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = timestamp_label()
    detection_path = out_dir / f"detection_{stamp}.txt"
    mc_path = out_dir / "mc_breaks_final.txt"
    with detection_path.open("w", encoding="utf-8") as f:
        rhythm_names = DETECTION_STRATEGY.get("rhythm_equivalent_sources", [])
        f.write(f"Detection report\nDate: {SESSION_DATE}\nSource: {SOURCE_DIR}\nGenerated: {stamp}\nStrategy: {DETECTION_STRATEGY.get('label', 'unknown')}\nRhythm-equivalent sources: {', '.join(rhythm_names) if rhythm_names else 'none'}\n\n")
        f.write("Final detected songs:\n")
        for idx, seg in enumerate(segments, 1):
            nominal_end = seg.nominal_end if seg.nominal_end is not None else seg.end
            f.write(
                f"{idx:02d}. transition {fmt_time(seg.mc_start) if seg.mc_start is not None else 'n/a'} - "
                f"{fmt_time(seg.mc_end) if seg.mc_end is not None else 'n/a'}; "
                f"music {fmt_time(seg.start)} - {fmt_time(nominal_end)} "
                f"({fmt_time(nominal_end - seg.start)}); render through {fmt_time(seg.end)}; "
                f"boundary {seg.boundary_source}\n"
            )
        if LAST_SPEECH_TRANSCRIPTIONS:
            f.write("\nWhisper candidate transcript log:\n")
            for item in LAST_SPEECH_TRANSCRIPTIONS:
                f.write(
                    f"- {fmt_time(float(item.get('start', 0)))} - {fmt_time(float(item.get('end', 0)))}; "
                    f"announcement={bool(item.get('announcement'))}; reason={item.get('announcement_reason') or '-'}; spoken song={item.get('spoken_song_number') or '-'}; "
                    f"musicians={item.get('musician_labels') or {}}; {str(item.get('text') or '').strip()}\n"
                )
                for speech_piece in item.get("speech_classification", []) or []:
                    f.write(
                        f"  [{fmt_time(float(speech_piece.get('start', 0)))}-{fmt_time(float(speech_piece.get('end', 0)))}] "
                        f"{speech_piece.get('classification')}: {speech_piece.get('text', '')}\n"
                    )
        audit = DETECTION_STRATEGY.get("boundary_audit", [])
        if audit:
            f.write("\nBoundary decision audit (Whisper target vs acoustic proposal vs actual):\n")
            for index, item in enumerate(audit, 1):
                def audit_time(value: object) -> str:
                    return fmt_time(float(value)) if value is not None else "-"
                f.write(
                    f"{index:02d}. {item.get('source')}: Whisper {audit_time(item.get('whisper_timestamp'))}; "
                    f"acoustic {audit_time(item.get('acoustic_proposed'))}; actual {audit_time(item.get('actual_used'))}; "
                    f"valid={item.get('valid_at_target')}; instruments={item.get('instrument_activity_at_target') or []}; "
                    f"voices={item.get('vocal_activity_at_target') or []}"
                    f"{'; REJECTED' if item.get('rejected') else ''}\n"
                )
        mapping = DETECTION_STRATEGY.get("shared_boundary_mapping", [])
        if mapping:
            f.write("\nExplicit introduction boundary mapping:\n")
            for index, item in enumerate(mapping, 1):
                f.write(
                    f"{index:02d}. intro first word {fmt_time(float(item['intro_first_word_timestamp'])) if item.get('intro_first_word_timestamp') is not None else '-'}; "
                    f"previous end {fmt_time(float(item['previous_song_end']))}; next start {fmt_time(float(item['next_song_start']))}; "
                    f"lead-in {float(item.get('intentional_lead_in_seconds') or 0):.1f}s; matches={item.get('matches')}\n"
                )
        assignments = DETECTION_STRATEGY.get("spoken_number_assignments", [])
        if assignments:
            f.write("\nAutomatic introduction/number audit:\n")
            corrections = DETECTION_STRATEGY.get("previous_tail_intro_corrections", [])
            corrected_indices = {int(item.get("song_index")) for item in corrections}
            for item in assignments:
                index = int(item.get("app_index", 0))
                segment = segments[index - 1] if 0 < index <= len(segments) else None
                has_intro = bool(segment and segment.speech_intro_text)
                f.write(
                    f"{index:02d}. spoken={item.get('spoken_number') or '-'}; app index={index}; "
                    f"introduction at start={'yes' if has_intro else 'no'}; "
                    f"correction={'yes' if index in corrected_indices else 'no'}\n"
                )
    with mc_path.open("w", encoding="utf-8") as f:
        f.write(f"Final merged MC transition zones\nGenerated: {stamp}\n\n")
        seen: set[tuple[float | None, float | None]] = set()
        count = 0
        for seg in segments:
            key = (seg.mc_start, seg.mc_end)
            if key in seen or seg.mc_start is None or seg.mc_end is None:
                continue
            seen.add(key)
            count += 1
            f.write(f"{count:02d}. {fmt_time(seg.mc_start)} - {fmt_time(seg.mc_end)} ({fmt_time(seg.mc_end - seg.mc_start)})\n")
    cleanup_report_history(out_dir)
    print(f"\nDetection stage files: {detection_path} and {mc_path}")


def read_segment(stem: Stem, segment: Segment) -> tuple[np.ndarray, int] | None:
    stem_start = stem.offset_seconds
    stem_end = stem.offset_seconds + stem.timeline_duration
    overlap_start = max(segment.start, stem_start)
    overlap_end = min(segment.end, stem_end)
    if overlap_end <= overlap_start:
        return None
    start_frame = int(round((overlap_start - stem.offset_seconds) * stem.samplerate))
    if start_frame >= stem.frames:
        return None
    end_frame = int(round((overlap_end - stem.offset_seconds) * stem.samplerate))
    end_frame = min(end_frame, stem.frames)
    if end_frame <= start_frame:
        return None
    if (end_frame - start_frame) / stem.samplerate < MIN_ACTIVE_STEM_SECONDS:
        return None
    with sf.SoundFile(str(stem.path), "r") as f:
        f.seek(start_frame)
        audio = f.read(end_frame - start_frame, dtype="float32", always_2d=True)
    if audio.shape[1] == 1:
        segment_audio = audio[:, 0].astype(np.float32)
    elif audio.shape[1] >= 2:
        segment_audio = audio[:, :2].astype(np.float32)
    else:
        return None
    dest_start_frame = int(round((overlap_start - segment.start) * stem.samplerate))
    return segment_audio, dest_start_frame


def read_stem_chunk(
    stem: Stem,
    segment: Segment,
    chunk_start_frame: int,
    chunk_frames: int,
    handle: sf.SoundFile | None = None,
) -> np.ndarray | None:
    sr = stem.samplerate
    chunk_start_time = segment.start + chunk_start_frame / sr
    chunk_end_time = chunk_start_time + chunk_frames / sr
    stem_start = stem.offset_seconds
    stem_end = stem.offset_seconds + stem.timeline_duration
    overlap_start = max(chunk_start_time, stem_start)
    overlap_end = min(chunk_end_time, stem_end)
    channels = min(max(stem.channels, 1), 2)
    out = np.zeros((chunk_frames, channels), dtype=np.float32)
    if overlap_end <= overlap_start:
        return None

    file_start = int(round((overlap_start - stem.offset_seconds) * sr))
    if file_start >= stem.frames:
        return None
    file_end = min(int(round((overlap_end - stem.offset_seconds) * sr)), stem.frames)
    if file_end <= file_start:
        return None
    dest = int(round((overlap_start - chunk_start_time) * sr))
    try:
        if handle is None:
            with sf.SoundFile(str(stem.path), "r") as f:
                f.seek(file_start)
                audio = f.read(file_end - file_start, dtype="float32", always_2d=True)
        else:
            if handle.tell() != file_start:
                handle.seek(file_start)
            audio = handle.read(file_end - file_start, dtype="float32", always_2d=True)
    except Exception as exc:
        warning_key = (str(stem.path), type(exc).__name__, str(exc))
        if warning_key not in READ_WARNINGS_REPORTED:
            READ_WARNINGS_REPORTED.add(warning_key)
            print(f"WARNING: unreadable audio block in {stem.path.name} at frame {file_start}: {type(exc).__name__}: {exc}", flush=True)
        return None
    if audio.shape[1] == 1 and channels == 2:
        audio = np.repeat(audio, 2, axis=1)
    elif audio.shape[1] > channels:
        audio = audio[:, :channels]
    n = min(len(audio), chunk_frames - dest)
    if n <= 0:
        return None
    out[dest : dest + n] = audio[:n]
    if channels == 1:
        return out[:, 0]
    return out


def scan_segment_activity(
    stems: list[Stem],
    segment: Segment,
    sr: int,
    *,
    chunk_seconds: float = RENDER_CHUNK_SECONDS,
    progress_callback=None,
) -> tuple[dict[str, float], dict[str, float], dict[str, bool], dict[str, float], dict[str, np.ndarray], dict[str, float]]:
    chunk_frames = max(1, int(round(chunk_seconds * sr)))
    total_frames = int(round(segment.duration * sr))
    started = time.perf_counter()
    sums: dict[str, float] = {s.path.name: 0.0 for s in stems}
    counts: dict[str, int] = {s.path.name: 0 for s in stems}
    has_audio: dict[str, bool] = {s.path.name: False for s in stems}
    envelope_values: dict[str, list[float]] = {s.path.name: [] for s in stems}
    peak_values: dict[str, float] = {s.path.name: 0.0 for s in stems}
    env_hop = max(1, int(round(DETECTION_FRAME_SECONDS * sr)))
    with ExitStack() as stack:
        handles = {stem.path.name: stack.enter_context(sf.SoundFile(str(stem.path), "r")) for stem in stems}
        frames_read_by_stem = {stem.path.name: 0 for stem in stems}
        for chunk_index, start in enumerate(range(0, total_frames, chunk_frames), 1):
            nframes = min(chunk_frames, total_frames - start)
            for stem_index, stem in enumerate(stems, 1):
                chunk = read_stem_chunk(stem, segment, start, nframes, handles[stem.path.name])
                if chunk is None:
                    continue
                chunk_start_time = segment.start + start / sr
                overlap_start = max(chunk_start_time, stem.offset_seconds)
                overlap_end = min(chunk_start_time + nframes / sr, stem.offset_seconds + stem.timeline_duration)
                frames_read_by_stem[stem.path.name] += max(0, int(round((overlap_end - overlap_start) * stem.samplerate)))
                has_audio[stem.path.name] = True
                # Some WAV exports contain non-finite or unexpectedly hot samples.
                # Detection only needs a stable envelope; never allow float32
                # multiplication to create inf/NaN and poison the cache.
                analysis_chunk = np.nan_to_num(
                    np.asarray(chunk, dtype=np.float64),
                    nan=0.0,
                    posinf=8.0,
                    neginf=-8.0,
                )
                analysis_chunk = np.clip(analysis_chunk, -8.0, 8.0)
                peak_values[stem.path.name] = max(peak_values[stem.path.name], float(np.max(np.abs(analysis_chunk))))
                sums[stem.path.name] += float(np.sum(np.square(analysis_chunk), dtype=np.float64))
                counts[stem.path.name] += int(chunk.size)
                mono = analysis_chunk if analysis_chunk.ndim == 1 else np.mean(analysis_chunk, axis=1, dtype=np.float64)
                full = (len(mono) // env_hop) * env_hop
                if full:
                    framed = mono[:full].reshape(-1, env_hop)
                    envelope_values[stem.path.name].extend(
                        float(v) for v in np.sqrt(np.maximum(np.mean(np.square(framed), axis=1, dtype=np.float64), 0.0) + 1e-12)
                    )
                if full < len(mono):
                    tail = mono[full:]
                    if len(tail):
                        envelope_values[stem.path.name].append(float(np.sqrt(max(float(np.mean(np.square(tail), dtype=np.float64)), 0.0) + 1e-12)))
                if progress_callback is not None:
                    progress_callback(
                        {
                            "stem": stem.path.name,
                            "stem_index": stem_index,
                            "stem_count": len(stems),
                            "chunk_index": chunk_index,
                            "frames_read": frames_read_by_stem[stem.path.name],
                            "total_frames": stem.frames,
                            "elapsed_seconds": time.perf_counter() - started,
                        }
                    )
    rms_db = {
        name: amp_to_db(math.sqrt(sums[name] / counts[name])) if counts[name] else -120.0
        for name in sums
    }
    energies = {name: db_to_amp(rms_db[name]) for name in rms_db}
    dynamic_spread_db = {}
    for name, values in envelope_values.items():
        if len(values) < 3:
            dynamic_spread_db[name] = 0.0
            continue
        env_db = 20.0 * np.log10(np.maximum(np.asarray(values, dtype=np.float64), 1e-12))
        dynamic_spread_db[name] = float(np.percentile(env_db, 95) - np.percentile(env_db, 20))
    envelopes = {name: np.asarray(values, dtype=np.float64) for name, values in envelope_values.items()}
    peak_db = {name: amp_to_db(value) if value > 0.0 else -120.0 for name, value in peak_values.items()}
    return rms_db, energies, has_audio, dynamic_spread_db, envelopes, peak_db


def segment_stem_activity_decision(
    stem: Stem,
    rms_db: float,
    has_audio: bool,
    dynamic_spread_db: float,
    loudest_db: float,
    envelope: np.ndarray | None = None,
) -> tuple[bool, str]:
    """Decide whether a stem carries musical content in one song segment.

    The decision deliberately combines absolute level, level relative to the
    session's loudest stem, envelope movement, and (when available) sustained
    envelope evidence.  A single global relative gate is unreliable for
    quiet-but-playing bass and keys.
    """
    if not has_audio:
        return False, "no audio in segment"
    role_limits = STEM_ACTIVITY_ROLE_OVERRIDES.get(stem.role, {})
    floor_dbfs = float(role_limits.get("floor_dbfs", STEM_INACTIVE_FLOOR_DBFS))
    relative_db = float(role_limits.get("relative_db", STEM_RELATIVE_INACTIVE_DB))
    spread_db = float(role_limits.get("spread_db", STEM_DYNAMIC_ACTIVE_SPREAD_DB))
    if dynamic_spread_db >= spread_db:
        return True, f"envelope spread {dynamic_spread_db:.1f} dB"
    if rms_db >= floor_dbfs and rms_db >= loudest_db - relative_db:
        return True, f"rms {rms_db:.1f} dBFS within {relative_db:.0f} dB of loudest"
    if envelope is not None and len(envelope) >= 3:
        env_db = 20.0 * np.log10(np.maximum(np.asarray(envelope, dtype=np.float32), 1e-12))
        sustained_floor = max(floor_dbfs, float(np.percentile(env_db, 20)) + 5.0)
        sustained_fraction = float(np.count_nonzero(env_db >= sustained_floor)) / len(env_db)
        if rms_db >= floor_dbfs and sustained_fraction >= 0.20:
            return True, f"sustained envelope {sustained_fraction:.0%} above {sustained_floor:.1f} dBFS"
    reasons = []
    if rms_db < floor_dbfs:
        reasons.append(f"below {floor_dbfs:.0f} dBFS floor")
    if rms_db < loudest_db - relative_db:
        reasons.append(f">{relative_db:.0f} dB below loudest")
    reasons.append(f"flat envelope spread {dynamic_spread_db:.1f} dB")
    return False, "; ".join(reasons)


def classify_noise_stems(stems: list[Stem], segment: Segment, sr: int) -> dict[str, dict[str, object]]:
    """Classify sustained broadband noise, empty noisy inputs, and mains hum.

    The detector uses quiet frames inside each song to estimate a broadband
    noise-floor profile.  It deliberately keeps hum separate: a narrow peak
    at 50/60 Hz (and harmonics) is not treated as broadband hiss.
    """
    result: dict[str, dict[str, object]] = {}
    for stem in stems:
        audio = _analysis_mono(stem, segment, max_seconds=NOISE_ANALYSIS_MAX_SECONDS)
        if len(audio) < RHYTHM_ANALYSIS_SR * 2:
            result[stem.path.name] = {"flagged": False, "is_noise": False, "case": "none", "reason": "insufficient audio"}
            continue

        frame = 2048
        hop = 1024
        frames = np.lib.stride_tricks.sliding_window_view(audio, frame)[::hop]
        if len(frames) == 0:
            result[stem.path.name] = {"flagged": False, "is_noise": False, "case": "none", "reason": "insufficient frames"}
            continue
        windowed = frames * np.hanning(frame).astype(np.float32)
        magnitude = np.abs(np.fft.rfft(windowed, axis=1)) + 1e-12
        power = magnitude * magnitude
        frame_rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
        frame_db = 20.0 * np.log10(np.maximum(frame_rms, 1e-12))
        flatness = np.exp(np.mean(np.log(power), axis=1)) / np.maximum(np.mean(power, axis=1), 1e-12)
        flux = np.maximum(0.0, np.diff(np.mean(magnitude, axis=1), prepend=np.mean(magnitude[:1], axis=1)))
        flux_threshold = float(np.percentile(flux, 85)) if len(flux) else 0.0
        transient_ratio = float(np.mean(flux > max(1e-12, flux_threshold * 1.4))) if len(flux) else 0.0
        quiet_cut = float(np.percentile(frame_db, 25))
        quiet = frame_db <= quiet_cut + 1.5
        noise_mag = np.median(magnitude[quiet], axis=0) if np.any(quiet) else np.median(magnitude, axis=0)
        noise_power = noise_mag * noise_mag
        noise_floor_db = amp_to_db(float(np.sqrt(np.mean(noise_mag * noise_mag) / max(1.0, frame))))
        noise_flatness = float(np.exp(np.mean(np.log(noise_power))) / max(np.mean(noise_power), 1e-12))
        sustained_fraction = float(np.mean(frame_db >= quiet_cut - 3.0))

        freqs = np.fft.rfftfreq(frame, 1.0 / RHYTHM_ANALYSIS_SR)
        band_power = np.mean(power, axis=0)
        baseline = float(np.median(band_power[(freqs >= 30) & (freqs <= 2000)]))
        hum_peaks = []
        for fundamental in HUM_FUNDAMENTALS_HZ:
            ratios = []
            for harmonic in range(1, 7):
                target = fundamental * harmonic
                if target >= RHYTHM_ANALYSIS_SR / 2:
                    continue
                idx = int(np.argmin(np.abs(freqs - target)))
                ratios.append(float(band_power[idx] / max(baseline, 1e-18)))
            hum_peaks.append((fundamental, max(ratios, default=0.0), sum(r > HUM_PEAK_RATIO_THRESHOLD for r in ratios)))
        hum_fundamental, hum_ratio, hum_harmonics = max(hum_peaks, key=lambda item: item[1], default=(0.0, 0.0, 0))
        is_hum = bool(noise_floor_db >= HUM_MIN_RMS_DBFS and hum_ratio >= HUM_PEAK_RATIO_THRESHOLD and hum_harmonics >= 2)
        flat_broadband = bool(noise_flatness >= NOISE_BROADBAND_FLATNESS_THRESHOLD)
        sustained = bool(sustained_fraction >= NOISE_SUSTAINED_FRACTION_THRESHOLD)
        avg_rms = amp_to_db(float(np.sqrt(np.mean(frame_rms * frame_rms) + 1e-12)))
        peak_to_floor_db = float(np.percentile(frame_db, 90) - noise_floor_db)
        empty_noise = bool(flat_broadband and sustained and avg_rms <= NOISE_EMPTY_RMS_THRESHOLD_DBFS and peak_to_floor_db < 12.0)
        broadband_overlay = bool(flat_broadband and sustained and noise_floor_db >= NOISE_FLOOR_MIN_DBFS and peak_to_floor_db >= 12.0 and not is_hum)
        if is_hum:
            case = "hum"
            treatment = f"notch {hum_fundamental:.0f} Hz and harmonics 2–6"
            reason = "narrowband mains-frequency peaks"
        elif empty_noise:
            case = "B"
            treatment = "fully muted as empty noisy input"
            reason = "sustained broadband floor without musical transients"
        elif broadband_overlay:
            case = "A"
            treatment = "spectral subtraction from quiet-frame noise profile"
            reason = "musical level variation above sustained broadband floor"
        else:
            case = "none"
            treatment = "none"
            reason = "no actionable broadband-noise or hum signature"
        flagged = case != "none"
        result[stem.path.name] = {
            "flagged": flagged,
            "is_noise": case == "B",
            "case": case,
            "treatment": treatment,
            "rms_dbfs": float(avg_rms),
            "noise_floor_dbfs": float(noise_floor_db),
            "noise_floor_reduction_target_db": -10.0 if case == "A" else -0.0,
            "level_spread_db": float(np.percentile(frame_db, 95) - np.percentile(frame_db, 5)),
            "spectral_flatness": float(np.mean(flatness)),
            "noise_floor_flatness": noise_flatness,
            "sustained_fraction": sustained_fraction,
            "transient_ratio": transient_ratio,
            "hum_fundamental_hz": float(hum_fundamental) if is_hum else None,
            "hum_peak_ratio": float(hum_ratio),
            "hum_harmonic_count": int(hum_harmonics),
            "noise_profile": noise_mag.astype(np.float32),
            "noise_profile_sr": RHYTHM_ANALYSIS_SR,
            "reason": reason,
        }
    return result


def apply_chunk_fades(chunk: np.ndarray, sr: int, chunk_start_frame: int, total_frames: int) -> np.ndarray:
    y = chunk.copy()
    fade_in = int(round(RENDER_FADE_IN_SECONDS * sr))
    fade_out = int(round(RENDER_FADE_OUT_SECONDS * sr))
    if fade_in > 1:
        start = chunk_start_frame
        end = chunk_start_frame + len(y)
        overlap_start = max(start, 0)
        overlap_end = min(end, fade_in)
        if overlap_end > overlap_start:
            local_start = overlap_start - start
            local_end = overlap_end - start
            gains = np.arange(overlap_start, overlap_end, dtype=np.float32) / fade_in
            y[local_start:local_end] *= gains[:, None]
    if fade_out > 1:
        fade_start = max(0, total_frames - fade_out)
        start = chunk_start_frame
        end = chunk_start_frame + len(y)
        overlap_start = max(start, fade_start)
        overlap_end = min(end, total_frames)
        if overlap_end > overlap_start:
            local_start = overlap_start - start
            local_end = overlap_end - start
            gains = (total_frames - np.arange(overlap_start, overlap_end, dtype=np.float32)) / fade_out
            y[local_start:local_end] *= np.clip(gains, 0.0, 1.0)[:, None]
    return y.astype(np.float32)


def build_section_gate(envelope: np.ndarray) -> tuple[np.ndarray, list[tuple[float, float]]]:
    if len(envelope) < int(INSTRUMENT_SECTION_GATE_MIN_SECONDS):
        return np.ones(len(envelope), dtype=np.float32), []
    active = envelope[envelope > db_to_amp(STEM_INACTIVE_FLOOR_DBFS)]
    if len(active) == 0:
        return np.zeros(len(envelope), dtype=np.float32), [(0.0, len(envelope) * DETECTION_FRAME_SECONDS)]
    playing_level = max(float(np.percentile(active, 75)), 1e-9)
    rel_db = 20.0 * np.log10(np.maximum(envelope, 1e-12) / playing_level)
    quiet = rel_db < -INSTRUMENT_SECTION_GATE_DROP_DB
    min_bins = max(1, int(round(INSTRUMENT_SECTION_GATE_MIN_SECONDS / DETECTION_FRAME_SECONDS)))
    gate = np.ones(len(envelope), dtype=np.float32)
    muted: list[tuple[float, float]] = []
    idx = 0
    while idx < len(quiet):
        while idx < len(quiet) and not quiet[idx]:
            idx += 1
        start = idx
        while idx < len(quiet) and quiet[idx]:
            idx += 1
        end = idx
        if end - start >= min_bins:
            gate[start:end] = 0.0
            muted.append((start * DETECTION_FRAME_SECONDS, end * DETECTION_FRAME_SECONDS))
    fade_bins = max(1, int(round(INSTRUMENT_SECTION_GATE_FADE_SECONDS / DETECTION_FRAME_SECONDS)))
    if fade_bins > 1 and len(gate):
        kernel = np.ones(fade_bins, dtype=np.float32) / fade_bins
        gate = np.convolve(gate, kernel, mode="same").astype(np.float32)
    return np.clip(gate, 0.0, 1.0), muted


def section_gate_for_chunk(gate: np.ndarray, sr: int, chunk_start_frame: int, nframes: int) -> np.ndarray:
    if len(gate) == 0:
        return np.ones(nframes, dtype=np.float32)
    start_time = chunk_start_frame / sr
    times = start_time + np.arange(nframes, dtype=np.float32) / sr
    gate_times = np.arange(len(gate), dtype=np.float32) * DETECTION_FRAME_SECONDS
    return np.interp(times, gate_times, gate, left=gate[0], right=gate[-1]).astype(np.float32)


def biquad_filter(x: np.ndarray, sr: int, kind: str, freq: float, q: float = 0.707, gain_db: float = 0.0) -> np.ndarray:
    if len(x) < 32:
        return x
    sos = make_sos(sr, kind, freq, q, gain_db)
    return signal.sosfiltfilt(sos, x).astype(np.float32)


def make_sos(sr: int, kind: str, freq: float, q: float = 0.707, gain_db: float = 0.0) -> np.ndarray:
    if kind == "highpass":
        return signal.butter(2, freq, btype="highpass", fs=sr, output="sos")
    elif kind == "lowshelf":
        return shelf_sos(sr, freq, q, gain_db, high=False)
    elif kind == "highshelf":
        return shelf_sos(sr, freq, q, gain_db, high=True)
    elif kind == "peaking":
        return peak_sos(sr, freq, q, gain_db)
    elif kind == "notch":
        b, a = signal.iirnotch(freq, Q=q, fs=sr)
        return signal.tf2sos(b, a)
    else:
        raise ValueError(kind)


def peak_sos(sr: int, freq: float, q: float, gain_db: float) -> np.ndarray:
    a = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * freq / sr
    alpha = np.sin(w0) / (2 * q)
    b0 = 1 + alpha * a
    b1 = -2 * np.cos(w0)
    b2 = 1 - alpha * a
    a0 = 1 + alpha / a
    a1 = -2 * np.cos(w0)
    a2 = 1 - alpha / a
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]], dtype=np.float64)


def shelf_sos(sr: int, freq: float, q: float, gain_db: float, high: bool) -> np.ndarray:
    a = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * freq / sr
    cosw = np.cos(w0)
    sinw = np.sin(w0)
    alpha = sinw / (2 * q)
    beta = 2 * np.sqrt(a) * alpha
    if high:
        b0 = a * ((a + 1) + (a - 1) * cosw + beta)
        b1 = -2 * a * ((a - 1) + (a + 1) * cosw)
        b2 = a * ((a + 1) + (a - 1) * cosw - beta)
        a0 = (a + 1) - (a - 1) * cosw + beta
        a1 = 2 * ((a - 1) - (a + 1) * cosw)
        a2 = (a + 1) - (a - 1) * cosw - beta
    else:
        b0 = a * ((a + 1) - (a - 1) * cosw + beta)
        b1 = 2 * a * ((a - 1) - (a + 1) * cosw)
        b2 = a * ((a + 1) - (a - 1) * cosw - beta)
        a0 = (a + 1) + (a - 1) * cosw + beta
        a1 = -2 * ((a - 1) + (a + 1) * cosw)
        a2 = (a + 1) + (a - 1) * cosw - beta
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]], dtype=np.float64)


def compressor(
    x: np.ndarray,
    sr: int,
    ratio: float,
    threshold_db: float,
    attack_ms: float,
    release_ms: float,
) -> np.ndarray:
    release_coeff = math.exp(-1.0 / max(1.0, release_ms * 0.001 * sr))
    env = signal.lfilter([1.0 - release_coeff], [1.0, -release_coeff], np.abs(x).astype(np.float32))
    env_db = 20.0 * np.log10(np.maximum(env, 1e-12))
    over = np.maximum(0.0, env_db - threshold_db)
    gr = over * (1.0 - 1.0 / ratio)
    gain = np.power(10.0, -gr / 20.0).astype(np.float32)
    return (x * gain).astype(np.float32)


def one_pole_env(x: np.ndarray, coeff: float, state: dict[str, np.ndarray], key: str) -> np.ndarray:
    zi = state.get(key)
    if zi is None:
        zi = np.zeros(1, dtype=np.float32)
    env, zf = signal.lfilter([1.0 - coeff], [1.0, -coeff], np.abs(x).astype(np.float32), zi=zi)
    state[key] = zf.astype(np.float32)
    return env.astype(np.float32)


def compressor_streaming(
    x: np.ndarray,
    sr: int,
    ratio: float,
    threshold_db: float,
    release_ms: float,
    state: dict[str, np.ndarray],
    key: str,
) -> np.ndarray:
    coeff = math.exp(-1.0 / max(1.0, release_ms * 0.001 * sr))
    env = one_pole_env(x, coeff, state, f"{key}:comp_env")
    env_db = 20.0 * np.log10(np.maximum(env, 1e-12))
    over = np.maximum(0.0, env_db - threshold_db)
    gr = over * (1.0 - 1.0 / ratio)
    gain = np.power(10.0, -gr / 20.0).astype(np.float32)
    return (x * gain).astype(np.float32)


def rms_compressor_streaming(
    x: np.ndarray,
    sr: int,
    ratio: float,
    threshold_db: float,
    attack_ms: float,
    release_ms: float,
    state: dict[str, np.ndarray],
    key: str,
) -> np.ndarray:
    # A smooth RMS-ish leveler. The lfilter envelope is vectorized and stateful across chunks.
    coeff = math.exp(-1.0 / max(1.0, release_ms * 0.001 * sr))
    env = one_pole_env(np.mean(np.abs(x), axis=1) if x.ndim == 2 else np.abs(x), coeff, state, f"{key}:rms_env")
    env_db = 20.0 * np.log10(np.maximum(env, 1e-12))
    over = np.maximum(0.0, env_db - threshold_db)
    gr = over * (1.0 - 1.0 / ratio)
    gain = np.power(10.0, -gr / 20.0).astype(np.float32)
    if x.ndim == 2:
        return (x * gain[:, None]).astype(np.float32)
    return (x * gain).astype(np.float32)


def sosfilt_streaming(
    x: np.ndarray,
    sr: int,
    kind: str,
    freq: float,
    state: dict[str, np.ndarray],
    key: str,
    q: float = 0.707,
    gain_db: float = 0.0,
) -> np.ndarray:
    if len(x) < 2:
        return x
    sos = make_sos(sr, kind, freq, q, gain_db)
    zi = state.get(key)
    if zi is None:
        zi = np.zeros((sos.shape[0], 2), dtype=np.float32)
    y, zf = signal.sosfilt(sos, x.astype(np.float32), zi=zi)
    state[key] = zf.astype(np.float32)
    return y.astype(np.float32)


def rms_dbfs(x: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(x * x) + 1e-12))
    return amp_to_db(rms)




def spectral_subtract_noise(
    x: np.ndarray,
    sr: int,
    noise_profile: np.ndarray,
    profile_sr: int,
    noise_floor_dbfs: float,
) -> tuple[np.ndarray, float]:
    """Reduce broadband noise using a quiet-frame spectral profile.

    This is deliberately a conservative Wiener-like spectral subtraction:
    musical bins retain a minimum gain, while bins matching the estimated
    quiet noise floor are attenuated.  It is not a frequency notch.
    """
    if len(x) < 2048 or len(noise_profile) < 4:
        return x.astype(np.float32), 0.0
    n_fft = 2048
    hop = 512
    f, _, z = signal.stft(x.astype(np.float32), fs=sr, nperseg=n_fft, noverlap=n_fft - hop, boundary="zeros")
    profile_freqs = np.linspace(0.0, profile_sr / 2.0, len(noise_profile))
    profile = np.interp(f, profile_freqs, np.asarray(noise_profile, dtype=np.float64), left=float(noise_profile[0]), right=float(noise_profile[-1]))
    profile /= max(float(np.median(profile[1:])), 1e-12)
    noise_amp = db_to_amp(noise_floor_dbfs)
    noise_mag = noise_amp * profile * math.sqrt(n_fft)
    power = np.abs(z) ** 2
    noise_power = noise_mag[:, None] ** 2
    gain = 1.0 - NOISE_REDUCTION_STRENGTH * noise_power / np.maximum(power, 1e-12)
    gain = np.clip(gain, NOISE_REDUCTION_FLOOR, 1.0)
    y = signal.istft(z * gain, fs=sr, nperseg=n_fft, noverlap=n_fft - hop, input_onesided=True, boundary=True)[1]
    y = np.asarray(y[: len(x)], dtype=np.float32)
    before = rms_dbfs(x)
    residual_noise = np.asarray(x - y, dtype=np.float32)
    reduction = max(0.0, before - rms_dbfs(residual_noise)) if np.any(residual_noise) else 0.0
    return y, float(min(24.0, reduction))


def apply_noise_watchdog_streaming(
    x: np.ndarray,
    sr: int,
    diagnostics: dict[str, object],
    state: dict[str, np.ndarray],
    key: str,
) -> tuple[np.ndarray, float]:
    case = str(diagnostics.get("case", "none"))
    if case == "A":
        profile = diagnostics.get("noise_profile")
        if isinstance(profile, np.ndarray):
            return spectral_subtract_noise(
                x, sr, profile, int(diagnostics.get("noise_profile_sr", RHYTHM_ANALYSIS_SR)),
                float(diagnostics.get("noise_floor_dbfs", -80.0)),
            )
    if case == "hum":
        y = x.astype(np.float32)
        fundamental = float(diagnostics.get("hum_fundamental_hz") or 0.0)
        for harmonic in range(1, 7):
            freq = fundamental * harmonic
            if freq <= 0.0 or freq >= sr * 0.45:
                continue
            y = sosfilt_streaming(y, sr, "notch", freq, state, f"{key}:hum:{harmonic}", q=18.0)
        return y, float(diagnostics.get("hum_peak_ratio", 0.0))
    return x.astype(np.float32), 0.0


def downward_expander_streaming(
    x: np.ndarray,
    sr: int,
    state: dict[str, np.ndarray],
    key: str,
    threshold_db: float = MIC_EXPANDER_THRESHOLD_DBFS,
    ratio: float = MIC_EXPANDER_RATIO,
    attack_ms: float = MIC_EXPANDER_ATTACK_MS,
    release_ms: float = MIC_EXPANDER_RELEASE_MS,
    hysteresis_db: float = MIC_EXPANDER_HYSTERESIS_DB,
    max_attenuation_db: float = MIC_EXPANDER_MAX_ATTENUATION_DB,
) -> np.ndarray:
    release = math.exp(-1.0 / max(1.0, release_ms * 0.001 * sr))
    env = one_pole_env(x, release, state, f"{key}:expand_env")
    env_db = 20.0 * np.log10(np.maximum(env, 1e-12))
    below = np.maximum(0.0, threshold_db - env_db)
    knee = np.clip(below / max(hysteresis_db, 1e-6), 0.0, 1.0)
    gain_db = -below * (1.0 - 1.0 / ratio) * knee
    gain_db = np.maximum(gain_db, max_attenuation_db)
    target = db_to_amp(0.0) * np.power(10.0, gain_db / 20.0).astype(np.float32)
    coeff = math.exp(-1.0 / max(1.0, attack_ms * 0.001 * sr))
    zi = state.get(f"{key}:expand_gain")
    if zi is None:
        zi = np.ones(1, dtype=np.float32)
    gain, zf = signal.lfilter([1.0 - coeff], [1.0, -coeff], target, zi=zi)
    state[f"{key}:expand_gain"] = zf.astype(np.float32)
    return (x * gain.astype(np.float32)).astype(np.float32)


def noise_gate_streaming(
    x: np.ndarray,
    sr: int,
    state: dict[str, np.ndarray],
    key: str,
    threshold_db: float = MIC_GATE_THRESHOLD_DBFS,
    release_ms: float = 200.0,
) -> np.ndarray:
    """Gate raw wind/mic signal before automatic makeup gain."""
    release = math.exp(-1.0 / max(1.0, release_ms * 0.001 * sr))
    env = one_pole_env(x, release, state, f"{key}:gate_env")
    target = (env >= db_to_amp(threshold_db)).astype(np.float32)
    zi = state.get(f"{key}:gate_gain")
    if zi is None:
        zi = np.zeros(1, dtype=np.float32)
    gate, zf = signal.lfilter([1.0 - release], [1.0, -release], target, zi=zi)
    state[f"{key}:gate_gain"] = zf.astype(np.float32)
    return (x * np.clip(gate, 0.0, 1.0)).astype(np.float32)


def role_eq_bands(role: str, eq_overrides: dict[str, float] | None = None) -> list[tuple[str, str, float, float, float]]:
    if role == "kick":
        bands = [("low", "highpass", 30, 0.707, 0.0), ("fixed", "peaking", 60, 0.9, 2.5), ("mid", "peaking", 350, 1.0, -2.5), ("fixed", "peaking", 3500, 0.9, 2.0), ("air", "highshelf", 10000, 0.707, 0.0)]
    elif role == "bass":
        bands = [("low", "highpass", 35, 0.707, 0.0), ("mid", "peaking", 300, 1.0, -2.5), ("fixed", "peaking", 100, 0.8, 1.0), ("air", "highshelf", 10000, 0.707, 0.0)]
    elif role == "vocal":
        bands = [("low", "highpass", 115, 0.707, 0.0), ("mid", "peaking", 3000, 0.9, 2.0), ("air", "highshelf", 11000, 0.707, 1.0)]
    elif role == "guitar":
        bands = [("low", "highpass", 90, 0.707, 0.0), ("mid", "peaking", 250, 1.0, -2.0), ("fixed", "peaking", 2800, 1.0, 0.0), ("air", "highshelf", 10000, 0.707, 0.0)]
    elif role in {"keys", "keys_l", "keys_r"}:
        bands = [("low", "highpass", 80, 0.707, 0.0), ("mid", "peaking", 320, 1.0, -2.0), ("air", "highshelf", 10000, 0.707, 0.0)]
    elif role in {"sax", "horn"}:
        bands = [("low", "highpass", 90, 0.707, 0.0), ("mid", "peaking", 2500, 0.9, 1.5), ("air", "highshelf", 10000, 0.707, 0.0)]
    elif role == "synth":
        bands = [("low", "highpass", 90, 0.707, 0.0), ("mid", "peaking", 2500, 0.9, 1.5), ("fixed", "peaking", 320, 1.0, -1.5), ("air", "highshelf", 10000, 0.707, 0.0)]
    elif role == "drums":
        bands = [("low", "highpass", 150, 0.707, 0.0), ("mid", "peaking", 300, 1.0, 0.0), ("air", "highshelf", 9000, 0.707, 1.0)]
    elif role == "snare":
        bands = [("low", "highpass", 80, 0.707, 0.0), ("mid", "peaking", 220, 1.0, -1.5), ("fixed", "peaking", 5000, 0.8, 1.5), ("air", "highshelf", 10000, 0.707, 0.0)]
    else:
        bands = [("low", "highpass", 80, 0.707, 0.0), ("mid", "peaking", 300, 1.0, 0.0), ("air", "highshelf", 10000, 0.707, 0.0)]
    if not eq_overrides:
        return bands
    resolved = []
    for slot, kind, freq, q, gain_db in bands:
        if slot == "low":
            freq = float(eq_overrides.get("eq_low_cut_hz", freq))
        elif slot == "mid":
            gain_db = float(eq_overrides.get("eq_mid_gain_db", gain_db))
        elif slot == "air":
            gain_db = float(eq_overrides.get("eq_air_gain_db", gain_db))
        resolved.append((slot, kind, freq, q, gain_db))
    return resolved


def role_eq_defaults(role: str) -> dict[str, float]:
    bands = role_eq_bands(role)
    low = next((band for band in bands if band[0] == "low"), None)
    mid = next((band for band in bands if band[0] == "mid"), None)
    air = next((band for band in bands if band[0] == "air"), None)
    return {
        "eq_low_cut_hz": float(low[2] if low else 80.0),
        "eq_mid_gain_db": float(mid[4] if mid else 0.0),
        "eq_air_gain_db": float(air[4] if air else 0.0),
    }


def apply_role_eq(x: np.ndarray, sr: int, role: str, eq_overrides: dict[str, float] | None = None) -> np.ndarray:
    for _slot, kind, freq, q, gain_db in role_eq_bands(role, eq_overrides):
        x = biquad_filter(x, sr, kind, freq, q=q, gain_db=gain_db)
    return x.astype(np.float32)


def apply_role_eq_streaming(
    x: np.ndarray,
    sr: int,
    role: str,
    state: dict[str, np.ndarray],
    key: str,
    eq_overrides: dict[str, float] | None = None,
) -> np.ndarray:
    for idx, (slot, kind, freq, q, gain_db) in enumerate(role_eq_bands(role, eq_overrides)):
        x = sosfilt_streaming(x, sr, kind, freq, state, f"{key}:role_eq:{idx}:{slot}:{kind}:{freq}", q=q, gain_db=gain_db)
    return x.astype(np.float32)


def process_track_streaming(
    x: np.ndarray,
    sr: int,
    role: str,
    makeup_gain_db: float,
    state: dict[str, np.ndarray],
    key: str,
    eq_overrides: dict[str, float] | None = None,
    preserve_vocal_speech: bool = False,
) -> np.ndarray:
    x = (x * db_to_amp(makeup_gain_db)).astype(np.float32)
    x = apply_role_eq_streaming(x, sr, role, state, key, eq_overrides)
    if role == "vocal":
        if preserve_vocal_speech:
            return x.astype(np.float32)
        x = downward_expander_streaming(x, sr, state, key)
        return x.astype(np.float32)

    if role == "bass":
        x = compressor_streaming(x, sr, ratio=3.0, threshold_db=-22.0, release_ms=140, state=state, key=f"{key}:bass")
    elif role in {"kick", "snare", "drums"}:
        x = compressor_streaming(x, sr, ratio=4.0, threshold_db=-20.0, release_ms=100, state=state, key=f"{key}:drums")
    elif role in {"keys", "keys_l", "keys_r", "guitar", "synth"}:
        x = compressor_streaming(x, sr, ratio=2.0, threshold_db=-21.0, release_ms=140, state=state, key=f"{key}:other")
    elif role in {"horn", "sax"}:
        x = compressor_streaming(x, sr, ratio=2.0, threshold_db=-22.0, release_ms=120, state=state, key=f"{key}:lead")
    else:
        x = compressor_streaming(x, sr, ratio=2.0, threshold_db=-22.0, release_ms=150, state=state, key=f"{key}:misc")
    return x.astype(np.float32)


def base_level_db(role: str) -> float:
    return {
        "kick": 0.0,
        "snare": -7.0,
        "drums": -8.0,
        "bass": -3.5,
        "keys_l": -7.0,
        "keys_r": -7.0,
        "keys": -8.0,
        "guitar": -10.5,
        "synth": -11.0,
        "sax": -8.0,
        "horn": -8.0,
        "vocal": -5.0,
    }.get(role, -12.0)


def automatic_makeup_gain_db(
    raw_rms_db: float,
    role: str,
    include_vocal_mic_lift: bool = True,
    role_norm_db: float | None = None,
) -> float:
    """Return the automatic gain plus the role hierarchy trim.

    The hierarchy used to be reported in diagnostics but never included in
    the DSP gain, which left an unedited bass at the same target as a kick or
    vocal.  Keep this calculation in one place for preview and export paths.
    """
    requested = TARGET_TRACK_RMS_DBFS - float(raw_rms_db)
    cap = MAX_DRUM_MAKEUP_GAIN_DB if role in {"kick", "snare", "drums"} else MAX_TRACK_MAKEUP_GAIN_DB
    gain = min(requested, cap) + base_level_db(role) + AUTO_MIX_ROLE_TRIMS_DB.get(role, 0.0)
    if role_norm_db is not None and raw_rms_db > -90.0:
        # Move unusually loud/quiet performances toward the session's own
        # role norm without erasing genuine dynamics.
        performance_deviation_db = float(raw_rms_db) - float(role_norm_db)
        gain -= float(np.clip(performance_deviation_db, -3.0, 3.0))
    if include_vocal_mic_lift and role == "vocal":
        gain += AUTOMATIC_VOCAL_MIC_LIFT_DB
    return gain


def automatic_drum_peak_guard_gain_db(role: str, computed_gain_db: float, raw_peak_db: float) -> float:
    """Keep automatic drum input peaks below -8 dBFS before role DSP."""
    if role not in {"kick", "snare", "drums"} or not np.isfinite(raw_peak_db):
        return computed_gain_db
    return min(float(computed_gain_db), -8.0 - float(raw_peak_db))


def vocal_gain_reference_db(raw_rms_db: float, envelope: np.ndarray | None) -> float:
    """Use active vocal level for makeup gain, not silence-diluted song RMS.

    Announcer and vocal stems can contain many minutes of silence around a
    song.  Using the whole-window RMS in that case requests the maximum track
    makeup gain and leaves the vocal-bus safety stage to absorb the resulting
    transient.  The active-envelope percentile keeps the intended hierarchy
    while still allowing normal dynamics through.
    """
    if envelope is None:
        return float(raw_rms_db)
    values = np.asarray(envelope, dtype=np.float64)
    active = values[np.isfinite(values) & (values > db_to_amp(STEM_INACTIVE_FLOOR_DBFS))]
    if active.size < 4:
        return float(raw_rms_db)
    return float(amp_to_db(float(np.percentile(active, 75))))


def active_level_db(raw_rms_db: float, envelope: np.ndarray | None) -> float:
    """Robust musical level for one stem inside one song window.

    The upper active-envelope percentile ignores long silence and isolated
    transients while retaining the sustained playing level. The raw RMS is
    retained separately for diagnostics and is only the fallback when the
    envelope has insufficient evidence.
    """
    if envelope is None:
        return float(raw_rms_db)
    values = np.asarray(envelope, dtype=np.float64)
    active = values[np.isfinite(values) & (values > db_to_amp(STEM_INACTIVE_FLOOR_DBFS))]
    if active.size < 4:
        return float(raw_rms_db)
    return float(amp_to_db(float(np.percentile(active, 75))))


def per_song_auto_mix_gain_db(
    role: str,
    stem_active_level_db: float,
    accompaniment_reference_db: float | None,
) -> float:
    """Calculate a bounded balance correction for one stem in one song."""
    reference = float(accompaniment_reference_db) if accompaniment_reference_db is not None else TARGET_TRACK_RMS_DBFS
    target = reference + (2.0 if role == "vocal" else 0.5 if role == "bass" else 0.0)
    requested = target - float(stem_active_level_db) + AUTO_MIX_ROLE_TRIMS_DB.get(role, 0.0)
    upper = float(AUTO_MIX_ROLE_BOOST_LIMITS_DB.get(role, AUTO_MIX_MAX_BOOST_DB))
    return float(np.clip(requested, AUTO_MIX_MAX_ATTENUATION_DB, upper))


def vocal_pair_key(name: str) -> str:
    """Return a stable logical pair key without changing channel panning."""
    low = re.sub(r"\.[^.]+$", "", str(name or "")).lower()
    # Generic two-channel vocal captures are commonly named ``Vox 1`` and
    # ``Vox 2`` (sometimes with a take suffix). They are one vocal pair for
    # analysis, while remaining separate channels for pan and rendering.
    if re.match(r"^(?:vox|vocal|voice|mic)[ _-]*[12](?:[_ -]?\d+)?$", low):
        return "vocal_pair"
    if re.match(r"^(?:vox|vocal|voice|mic)[ _-]*(?:l|left|r|right)(?:[_ -]?\d+)?$", low):
        return "vocal_pair"
    low = re.sub(r"[_ -]?\d+$", "", low)
    low = re.sub(r"(?:^|[ _-])(left|right|l|r)$", "", low).strip(" _-")
    return re.sub(r"[^a-z0-9]+", "_", low).strip("_") or "vocal"


def mix_role_group(role: str, name: str = "") -> str:
    """Group stems for balance while preserving their individual channels."""
    if role in {"vocal", "room"}:
        return "vocals"
    if role in {"keys", "keys_l", "keys_r", "synth"}:
        return "harmonic_keys"
    if role == "guitar":
        return "harmonic_guitar"
    if role in {"sax", "horn"}:
        return "melodic_winds"
    if role == "bass":
        return "bass"
    if role in {"kick", "snare", "drums"}:
        return "rhythm"
    return role or "other"


def group_active_level_db(names: list[str], active_levels_db: dict[str, float]) -> float | None:
    """Combine active RMS levels in power, never by arithmetic dB averaging."""
    values = [
        float(active_levels_db[name])
        for name in names
        if name in active_levels_db and np.isfinite(active_levels_db[name]) and active_levels_db[name] > -100.0
    ]
    if not values:
        return None
    return amp_to_db(float(np.sqrt(np.sum(np.power(10.0, np.asarray(values) / 10.0)))))


def vocal_harmonic_balance(
    effective_roles: dict[str, str],
    active_levels_db: dict[str, float],
    envelopes: dict[str, np.ndarray] | None = None,
) -> dict[str, object]:
    """Measure the per-song vocal-vs-harmonic relationship."""
    vocal_names = [name for name, role in effective_roles.items() if role in {"vocal", "room"}]
    harmonic_names = [
        name for name, role in effective_roles.items()
        if role in {"keys", "keys_l", "keys_r", "synth"}
    ]
    vocal_level = group_active_level_db(vocal_names, active_levels_db)
    harmonic_level = group_active_level_db(harmonic_names, active_levels_db)
    overlap = None
    if envelopes and vocal_names and harmonic_names:
        def group_env(names: list[str]) -> np.ndarray | None:
            arrays = [np.asarray(envelopes[n], dtype=np.float64) for n in names if n in envelopes and len(envelopes[n])]
            if not arrays:
                return None
            length = min(len(a) for a in arrays)
            return np.sqrt(np.sum([np.square(a[:length]) for a in arrays], axis=0))
        vocal_env = group_env(vocal_names)
        harmonic_env = group_env(harmonic_names)
        if vocal_env is not None and harmonic_env is not None and len(vocal_env):
            voice_mask = vocal_env > db_to_amp(STEM_INACTIVE_FLOOR_DBFS)
            overlap = float(np.mean(harmonic_env[voice_mask] > db_to_amp(STEM_INACTIVE_FLOOR_DBFS))) if np.any(voice_mask) else 0.0
    gap = None if vocal_level is None or harmonic_level is None else float(vocal_level - harmonic_level)
    vocal_correction = 0.0
    if gap is not None and gap < VOCAL_PRIORITY_MARGIN_DB:
        vocal_correction = float(np.clip((VOCAL_PRIORITY_MARGIN_DB + 0.5) - gap, 0.0, 3.0))
    harmonic_correction = 0.0
    if gap is not None and gap < VOCAL_PRIORITY_MARGIN_DB and (overlap is None or overlap >= 0.35):
        harmonic_correction = -float(np.clip(VOCAL_PRIORITY_MARGIN_DB - gap, 2.0, 4.0))
    return {
        "vocal_names": vocal_names,
        "harmonic_names": harmonic_names,
        "vocal_group_level_db": vocal_level,
        "harmonic_group_level_db": harmonic_level,
        "vocal_minus_harmonic_db": gap,
        "vocal_harmonic_overlap_fraction": overlap,
        "vocal_group_correction_db": vocal_correction,
        "harmonic_group_correction_db": harmonic_correction,
        "balance_method": "per-song active RMS power groups plus active-envelope overlap",
    }


def per_song_role_balance_corrections(
    effective_roles: dict[str, str],
    active_levels_db: dict[str, float],
    envelopes: dict[str, np.ndarray] | None = None,
) -> dict[str, object]:
    """Calculate per-song vocal-pair and harmonic-role balance trims.

    This is deliberately a relative-balance pass.  It never targets the
    master loudness and it never replaces a confirmed user fader.  Each role
    is compared with the vocal group only during vocal-active material so a
    loud instrumental-only passage cannot cause a permanent trim.
    """
    vocal_names = [name for name, role in effective_roles.items() if role in {"vocal", "room"}]
    vocal_level = group_active_level_db(vocal_names, active_levels_db)

    def group_overlap(names: list[str]) -> float:
        if not envelopes or not vocal_names or not names:
            return 0.0
        def env_for(items: list[str]) -> np.ndarray | None:
            arrays = [np.asarray(envelopes[name], dtype=np.float64) for name in items if name in envelopes and len(envelopes[name])]
            if not arrays:
                return None
            length = min(len(item) for item in arrays)
            return np.sqrt(np.sum([np.square(item[:length]) for item in arrays], axis=0))
        vocal_env = env_for(vocal_names)
        role_env = env_for(names)
        if vocal_env is None or role_env is None or not len(vocal_env):
            return 0.0
        mask = vocal_env > db_to_amp(STEM_INACTIVE_FLOOR_DBFS)
        return float(np.mean(role_env[mask] > db_to_amp(STEM_INACTIVE_FLOOR_DBFS))) if np.any(mask) else 0.0

    role_groups = {
        "guitar": [name for name, role in effective_roles.items() if role == "guitar"],
        "keys": [name for name, role in effective_roles.items() if role in {"keys", "keys_l", "keys_r", "synth"}],
    }
    role_levels = {
        role: group_active_level_db(names, active_levels_db)
        for role, names in role_groups.items()
    }
    role_corrections: dict[str, float] = {}
    role_reasons: dict[str, str] = {}
    for role, names in role_groups.items():
        level = role_levels[role]
        overlap = group_overlap(names)
        if vocal_level is None or level is None or overlap < 0.25:
            correction = 0.0
            reason = "no reliable vocal overlap evidence"
        else:
            excess = float(level - (vocal_level - VOCAL_PRIORITY_MARGIN_DB))
            if excess <= 0.5:
                correction = 0.0
                reason = "already balanced during vocal-active material"
            else:
                minimum = 2.0 if role == "guitar" else 1.5
                maximum = 6.0 if role == "guitar" else 3.0
                correction = -float(np.clip(max(excess, minimum), minimum, maximum))
                reason = f"{role} masks vocal group during vocal-active material"
        for name in names:
            role_corrections[name] = correction
            role_reasons[name] = reason

    vocal_pair_corrections: dict[str, float] = {}
    vocal_pair_keys = {name: vocal_pair_key(name) for name in vocal_names}
    for pair in sorted(set(vocal_pair_keys.values())):
        names = [name for name in vocal_names if vocal_pair_keys[name] == pair]
        if len(names) < 2:
            continue
        levels = [float(active_levels_db[name]) for name in names if name in active_levels_db]
        if not levels:
            continue
        target = float(np.median(levels))
        for name in names:
            vocal_pair_corrections[name] = float(np.clip(target - float(active_levels_db.get(name, target)), -3.0, 3.0))

    return {
        "vocal_pair_corrections_db": vocal_pair_corrections,
        "role_corrections_db": role_corrections,
        "role_reasons": role_reasons,
        "role_levels_db": role_levels,
        "vocal_group_level_db": vocal_level,
        "guitar_original_level_db": role_levels.get("guitar"),
        "guitar_reduction_db": min(
            (float(role_corrections[name]) for name in role_groups["guitar"]),
            default=0.0,
        ),
        "guitar_reduction_reason": next(
            (role_reasons[name] for name in role_groups["guitar"] if role_reasons.get(name)),
            "no reliable vocal overlap evidence",
        ),
        "role_balance_method": "per-song vocal-active envelope balance; guitar correction is independent from keys/harmonic correction",
    }


def role_norms_from_detection_cache(stems: list[Stem]) -> dict[str, float]:
    """Return per-role active-level norms from the current session cache."""
    cache = load_detection_cache(stems)
    by_role: dict[str, list[float]] = {}
    for stem in stems:
        values = np.asarray(cache.get(stem.path.name, np.array([], dtype=np.float32)))
        active = values[values > db_to_amp(STEM_INACTIVE_FLOOR_DBFS)]
        if len(active):
            by_role.setdefault(stem.role, []).append(amp_to_db(float(np.percentile(active, 75))))
    return {role: float(np.median(values)) for role, values in by_role.items() if values}


def pan_for_role(role: str, name: str) -> float:
    # These are invariants of the automatic mix.  Callers must not replace
    # them with a saved per-song pan override: a stereo image that changes
    # sides from song to song is harder to audit and can collapse the guitar
    # and keys into one speaker.
    if role == "guitar":
        return -0.35
    if role in {"keys", "keys_r"}:
        return 0.35
    if role == "keys_l":
        return -0.35
    if role == "synth":
        return 0.0
    if role in {"sax", "horn"}:
        return 0.25
    if role == "vocal" and re.search(r"mic\s*2", name.lower()):
        return 0.15
    return 0.0


def enforced_pan(role: str, name: str, requested: object = None) -> float:
    """Return a pan while preserving the non-negotiable stereo assignments."""
    if role in {"guitar", "keys", "keys_l", "keys_r", "synth"}:
        return pan_for_role(role, name)
    try:
        return float(np.clip(float(requested), -1.0, 1.0)) if requested is not None else pan_for_role(role, name)
    except (TypeError, ValueError):
        return pan_for_role(role, name)


def pan_mono(x: np.ndarray, pan: float) -> np.ndarray:
    pan = float(np.clip(pan, -1.0, 1.0))
    angle = (pan + 1.0) * np.pi / 4.0
    left = np.cos(angle)
    right = np.sin(angle)
    return np.column_stack((x * left, x * right)).astype(np.float32)


def apply_stereo_pan(x: np.ndarray, pan: float) -> np.ndarray:
    if x.ndim == 1:
        return pan_mono(x, pan)
    pan = float(np.clip(pan, -1.0, 1.0))
    if abs(pan) < 1e-4:
        return x.astype(np.float32)
    angle = (pan + 1.0) * np.pi / 4.0
    scale = np.array([np.cos(angle), np.sin(angle)], dtype=np.float32) * np.sqrt(2.0)
    return (x[:, :2] * scale[None, :]).astype(np.float32)


def current_song_overrides(index: int) -> dict[str, object]:
    songs = MIX_OVERRIDES.get("songs", {}) if isinstance(MIX_OVERRIDES, dict) else {}
    if not isinstance(songs, dict):
        return {}
    value = songs.get(str(index), {})
    return value if isinstance(value, dict) else {}


def mix_source_label(song_overrides: dict[str, object]) -> str:
    """Return the user-facing source of a song's mix settings."""
    if not isinstance(song_overrides, dict):
        return "Automatic mix"
    if abs(override_float(song_overrides.get("vocal_bus_db"), 0.0)) > 0.01:
        return "Manual mix"
    if abs(override_float(song_overrides.get("master_db"), 0.0)) > 0.01:
        return "Manual mix"
    if abs(override_float(song_overrides.get("target_lufs"), -14.0) + 14.0) > 0.01:
        return "Manual mix"
    stems = song_overrides.get("stems", {})
    if not isinstance(stems, dict):
        return "Automatic mix"
    for settings in stems.values():
        if not isinstance(settings, dict):
            continue
        if override_bool(settings.get("manual_makeup_gain_db"), False):
            return "Manual mix"
        if abs(override_float(settings.get("gain_db"), 0.0)) > 0.01:
            return "Manual mix"
        if abs(override_float(settings.get("fader_db"), 0.0)) > 0.01:
            return "Manual mix"
        if override_bool(settings.get("mute"), False) or override_bool(settings.get("solo"), False):
            return "Manual mix"
        if abs(override_float(settings.get("reverb_send_db"), 0.0)) > 0.01 or abs(override_float(settings.get("delay_send_db"), 0.0)) > 0.01:
            return "Manual mix"
        if "fx_enabled" in settings and not override_bool(settings.get("fx_enabled"), True):
            return "Manual mix"
    return "Automatic mix"


def stem_override(song_overrides: dict[str, object], stem_name: str) -> dict[str, object]:
    stems = song_overrides.get("stems", {})
    if not isinstance(stems, dict):
        return {}
    value = stems.get(stem_name, {})
    return value if isinstance(value, dict) else {}


def current_override_verify_trace(index: int) -> dict[str, object]:
    value = MIX_OVERRIDE_VERIFY_TRACE.get(str(index), {}) if isinstance(MIX_OVERRIDE_VERIFY_TRACE, dict) else {}
    return value if isinstance(value, dict) else {}


def override_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def override_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    return default


def reverb_send_level_db(role: str) -> float | None:
    return {
        "vocal": -10.0,
        "sax": -14.0,
        "horn": -14.0,
        "guitar": -15.0,
        "keys": -16.0,
        "keys_l": -16.0,
        "keys_r": -16.0,
        "synth": -17.0,
        "snare": -20.0,
        "drums": -24.0,
        "kick": -42.0,
        "bass": -42.0,
    }.get(role)


def delay_send_level_db(role: str, lead_bonus: float) -> float | None:
    if role == "vocal":
        return -16.0
    if role in {"sax", "horn", "guitar"}:
        return -18.0 if lead_bonus > 0.0 else -24.0
    if role in {"keys", "keys_l", "keys_r", "synth"} and lead_bonus > 0.0:
        return -22.0
    return None


def effective_mix_snapshot(
    index: int,
    song_overrides: dict[str, object],
    stem_report: list[dict[str, object]],
    used: list[str],
    vocal_bus_trim_db: float,
) -> dict[str, object]:
    active_files = {
        str(row.get("name", ""))
        for row in stem_report
        if row.get("name") and str(row.get("status", "")) == "active"
    }
    snapshot: dict[str, object] = {
        "schema": "zucker-effective-mix/v1",
        "source": "python-render",
        "reason": "render-segment",
        "song": index,
        "mix_source": mix_source_label(song_overrides),
        "activeStemFiles": sorted(active_files),
        "mixedStemFiles": sorted(used),
        "vocal_bus_db": vocal_bus_trim_db,
        "target_lufs": TARGET_LUFS,
        "mastering_intensity": MASTERING_INTENSITY,
        "master_db": override_float(song_overrides.get("master_db"), 0.0),
        "bus_processing": {
            "vocal_bus": {"type": "rms_compressor_streaming", "threshold_db": -12, "ratio": 2, "attack_ms": 150, "release_ms": 600},
            "mix_bus": {"type": "mix_bus_streaming"},
            "mastering": {
                "type": "matchering" if MATCHERING_REFERENCE is not None else "master_temp_wav_streaming",
                "target_lufs": TARGET_LUFS,
                "intensity": MASTERING_INTENSITY,
                "reference": str(MATCHERING_REFERENCE) if MATCHERING_REFERENCE is not None else None,
            },
        },
        "stems": {},
    }
    stems_payload: dict[str, object] = {}
    for row in sorted(stem_report, key=lambda item: str(item.get("name", ""))):
        name = str(row.get("name", ""))
        makeup_gain_db = override_float(row.get("makeup_gain_db"), 0.0)
        user_gain_db = override_float(row.get("user_gain_db"), 0.0)
        fader_db = override_float(row.get("override_gain_db"), override_float(row.get("level_gain_db"), 0.0))
        pre_gain_db = makeup_gain_db + user_gain_db
        final_gain_db = pre_gain_db + fader_db
        muted = bool(row.get("override_mute")) or str(row.get("status", "")) != "active" or fader_db <= FADER_HARD_SILENCE_DB
        reverb_total = row.get("reverb_total_db")
        delay_total = row.get("delay_total_db")
        role = str(row.get("effective_role") or row.get("role", ""))
        fx_enabled = override_bool(stem_override(song_overrides, name).get("fx_enabled"), True)
        gate_enabled = override_bool(stem_override(song_overrides, name).get("gate_enabled"), False)
        space_enabled = override_bool(stem_override(song_overrides, name).get("space_enabled"), False)
        echo_enabled = override_bool(stem_override(song_overrides, name).get("echo_enabled"), False)
        stems_payload[name] = {
            "file": name,
            "label": name,
            "role": role,
            "source_role": row.get("role"),
            "content_classification": row.get("content_classification"),
            "rhythm_analysis": row.get("rhythm_analysis"),
            "rhythmic_attenuation_db": row.get("rhythmic_attenuation_db", 0.0),
            "harmonic_analysis": row.get("harmonic_analysis"),
            "harmonic_attenuation_db": row.get("harmonic_attenuation_db", 0.0),
            "vocal_priority_attenuation_db": row.get("vocal_priority_attenuation_db", 0.0),
            "hierarchy_ceiling_attenuation_db": row.get("hierarchy_ceiling_attenuation_db", 0.0),
            "makeup_gain_db": makeup_gain_db,
            "automatic_gain_before_vocal_mic_lift_db": override_float(row.get("automatic_gain_before_vocal_mic_lift_db"), makeup_gain_db),
            "automatic_vocal_mic_lift_db": override_float(row.get("automatic_vocal_mic_lift_db"), 0.0),
            "role_norm_db": row.get("role_norm_db"),
            "performance_deviation_db": row.get("performance_deviation_db"),
            "automatic_performance_correction_db": row.get("automatic_performance_correction_db"),
            "vocal_group_correction_db": override_float(row.get("vocal_group_correction_db"), 0.0),
            "vocal_pair_key": row.get("vocal_pair_key"),
            "mix_role_group": row.get("mix_role_group") or mix_role_group(role, name),
            "active_level_db": row.get("active_level_db"),
            "accompaniment_reference_db": row.get("accompaniment_reference_db"),
            "automatic_gain_reason": row.get("automatic_gain_reason"),
            "user_fader_db": override_float(row.get("user_fader_db"), fader_db),
            "legacy_fader_db": override_float(row.get("legacy_fader_db"), 0.0),
            "legacy_gain_db": override_float(row.get("legacy_gain_db"), 0.0),
            "legacy_gain_state": row.get("legacy_gain_state", "none"),
            "user_confirmed": bool(row.get("user_confirmed", False)),
            "noise_detected": bool(row.get("noise_detected")),
            "noise_watchdog_flagged": bool(row.get("noise_watchdog_flagged")),
            "noise_case": row.get("noise_case", "none"),
            "noise_treatment": row.get("noise_treatment", "none"),
            "user_gain_db": user_gain_db,
            "pre_gain_db": pre_gain_db,
            "fader_db": fader_db,
            "final_gain_db": final_gain_db,
            "final_linear_gain": 0.0 if muted else db_to_amp(final_gain_db),
            "mute": muted,
            "solo": override_bool(stem_override(song_overrides, name).get("solo"), False),
            "muted_by_solo": "not soloed" in str(row.get("reason", "")),
            "fx_enabled": fx_enabled,
            "gate_enabled": gate_enabled,
            "space_enabled": space_enabled,
            "echo_enabled": echo_enabled,
            "pan": override_float(row.get("pan"), 0.0),
            "eq_low_cut_hz": override_float(row.get("eq_low_cut_hz"), role_eq_defaults(role)["eq_low_cut_hz"]),
            "eq_mid_gain_db": override_float(row.get("eq_mid_gain_db"), role_eq_defaults(role)["eq_mid_gain_db"]),
            "eq_air_gain_db": override_float(row.get("eq_air_gain_db"), role_eq_defaults(role)["eq_air_gain_db"]),
            "lead_bonus_db": override_float(row.get("lead_bonus_db"), 0.0),
            "reverb_base_db": row.get("reverb_base_db"),
            "reverb_send_db": override_float(row.get("reverb_send_db"), 0.0),
            "reverb_total_db": reverb_total,
            "reverb_linear_gain": 0.0 if (reverb_total is None or not fx_enabled or not space_enabled) else db_to_amp(override_float(reverb_total, 0.0)),
            "delay_base_db": row.get("delay_base_db"),
            "delay_send_db": override_float(row.get("delay_send_db"), 0.0),
            "delay_total_db": delay_total,
            "delay_linear_gain": 0.0 if (delay_total is None or not fx_enabled or not echo_enabled) else db_to_amp(override_float(delay_total, 0.0)),
            "track_processing": "raw_dry_bypass" if not fx_enabled else ("downward_expander_streaming" if role == "vocal" else "compressor_streaming"),
        }
    snapshot["auditedExcludedActiveStemFiles"] = sorted(
        str(row.get("name", ""))
        for row in stem_report
        if row.get("name")
        and str(row.get("status", "")) == "active"
        and (bool(row.get("override_mute")) or override_float(row.get("override_gain_db"), 0.0) <= FADER_HARD_SILENCE_DB)
    )
    snapshot["stems"] = stems_payload
    return snapshot


def plate_reverb_streaming(
    send: np.ndarray,
    sr: int,
    decay_seconds: float,
    state: dict[str, np.ndarray],
) -> np.ndarray:
    if len(send) == 0:
        return send
    predelay = int(round(0.025 * sr))
    wet = np.zeros_like(send, dtype=np.float32)
    delayed = delay_line_streaming(send, predelay, 0.0, state, "plate:predelay")
    comb_delays = [0.0297, 0.0371, 0.0411, 0.0437]
    feedback = float(np.clip(10.0 ** (-3.0 * max(comb_delays) / max(decay_seconds, 0.3)), 0.25, 0.82))
    for idx, delay_seconds in enumerate(comb_delays):
        comb = delay_line_streaming(delayed, int(round(delay_seconds * sr)), feedback, state, f"plate:comb:{idx}")
        wet += comb * 0.25
    for ch in range(2):
        wet[:, ch] = sosfilt_streaming(wet[:, ch], sr, "highpass", 180, state, f"plate:hp:{ch}")
        wet[:, ch] = sosfilt_streaming(wet[:, ch], sr, "highshelf", 4500, state, f"plate:dark:{ch}", q=0.707, gain_db=-4.0)
    return (wet * 0.32).astype(np.float32)


def delay_line_streaming(
    x: np.ndarray,
    delay_samples: int,
    feedback: float,
    state: dict[str, np.ndarray],
    key: str,
) -> np.ndarray:
    delay_samples = max(1, int(delay_samples))
    buf = state.get(key)
    if buf is None or buf.shape != (delay_samples, x.shape[1]):
        buf = np.zeros((delay_samples, x.shape[1]), dtype=np.float32)
    combined = np.vstack([buf, x.astype(np.float32)])
    out = combined[: len(x)].copy()
    state[key] = (combined[len(x) : len(x) + delay_samples] + out[-delay_samples:] * feedback if len(out) >= delay_samples else combined[-delay_samples:]).astype(np.float32)
    return out.astype(np.float32)


def slap_delay_streaming(
    send: np.ndarray,
    sr: int,
    bpm: float,
    state: dict[str, np.ndarray],
) -> np.ndarray:
    if len(send) == 0 or bpm <= 0:
        return np.zeros_like(send, dtype=np.float32)
    beat_seconds = 60.0 / float(np.clip(bpm, 60.0, 200.0))
    delay_seconds = np.clip(beat_seconds * 0.75, 0.16, 0.42)
    wet = delay_line_streaming(send, int(round(delay_seconds * sr)), 0.20, state, "slap:delay")
    for ch in range(2):
        wet[:, ch] = sosfilt_streaming(wet[:, ch], sr, "highpass", 180, state, f"slap:hp:{ch}")
        wet[:, ch] = sosfilt_streaming(wet[:, ch], sr, "highshelf", 4000, state, f"slap:lp:{ch}", q=0.707, gain_db=-8.0)
    return (wet * 0.35).astype(np.float32)


def reverb_decay_for_bpm(bpm: float) -> float:
    if bpm <= 0:
        return 1.7
    return float(np.clip(np.interp(bpm, [60.0, 200.0], [2.2, 1.2]), 1.2, 2.2))


def mix_bus(x: np.ndarray, sr: int) -> np.ndarray:
    mono = np.mean(x, axis=1)
    comp = compressor(mono, sr, ratio=2.0, threshold_db=-14.0, attack_ms=30, release_ms=250)
    gain = np.divide(comp, mono, out=np.ones_like(comp), where=np.abs(mono) > 1e-8)
    gain = np.clip(gain, 0.25, 1.0)
    x = x * gain[:, None]
    x = np.tanh(x * 1.15) / np.tanh(1.15)
    for ch in range(2):
        x[:, ch] = biquad_filter(x[:, ch].astype(np.float32), sr, "highshelf", 10000, q=0.707, gain_db=1.0)
    return x.astype(np.float32)


def mix_bus_streaming(x: np.ndarray, sr: int, state: dict[str, np.ndarray]) -> np.ndarray:
    mono = np.mean(x, axis=1)
    comp = compressor_streaming(mono, sr, ratio=2.0, threshold_db=-14.0, release_ms=250, state=state, key="bus")
    gain = np.divide(comp, mono, out=np.ones_like(comp), where=np.abs(mono) > 1e-8)
    gain = np.clip(gain, 0.25, 1.0)
    x = x * gain[:, None]
    x = np.tanh(x * 1.15) / np.tanh(1.15)
    for ch in range(2):
        x[:, ch] = sosfilt_streaming(x[:, ch].astype(np.float32), sr, "highshelf", 10000, state, f"bus:air:{ch}", q=0.707, gain_db=1.0)
    return x.astype(np.float32)


def apply_render_fades(x: np.ndarray, sr: int) -> np.ndarray:
    y = x.copy()
    fade_in = min(len(y), int(round(RENDER_FADE_IN_SECONDS * sr)))
    if fade_in > 1:
        y[:fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=np.float32)[:, None]
    fade_out = min(len(y), int(round(RENDER_FADE_OUT_SECONDS * sr)))
    if fade_out > 1:
        y[-fade_out:] *= np.linspace(1.0, 0.0, fade_out, dtype=np.float32)[:, None]
    return y.astype(np.float32)


def lookahead_limiter(x: np.ndarray, sr: int, ceiling_db: float = TRUE_PEAK_CEILING_DBFS) -> np.ndarray:
    ceiling = db_to_amp(ceiling_db)
    lookahead = max(1, int(0.005 * sr))
    peak = np.max(np.abs(x), axis=1)
    future = ndimage.maximum_filter1d(peak, size=lookahead, mode="nearest", origin=-(lookahead // 2))
    gain = np.minimum(1.0, ceiling / np.maximum(future, 1e-9))
    if len(gain) > 1003:
        gain = signal.savgol_filter(gain, 1001, 2)
    y = x * gain[:, None]
    return np.clip(y, -ceiling, ceiling).astype(np.float32)




def measure_stream_rms_lufs(path: Path, progress_callback=None) -> tuple[float, float]:
    total_sq = 0.0
    total_count = 0
    peak = 0.0
    started = time.perf_counter()
    with sf.SoundFile(str(path), "r") as f:
        total_frames = max(1, int(f.frames))
        frames_read = 0
        for block in f.blocks(blocksize=int(RENDER_CHUNK_SECONDS * f.samplerate), dtype="float32", always_2d=True):
            total_sq += float(np.sum(block * block))
            total_count += int(block.size)
            peak = max(peak, float(np.max(np.abs(block))))
            frames_read += len(block)
            if progress_callback is not None:
                ratio = min(1.0, frames_read / total_frames)
                elapsed = time.perf_counter() - started
                progress_callback({"phase": "measuring loudness", "ratio": ratio, "elapsed_seconds": elapsed})
    if total_count == 0:
        return -120.0, -120.0
    # Bounded-memory approximation for integrated loudness. It deliberately
    # avoids loading multi-hour renders into RAM; final verification remains peak-safe.
    loudness = amp_to_db(math.sqrt(total_sq / total_count))
    return loudness, amp_to_db(peak)


def apply_master_pass(input_path: Path, output_path: Path, sr: int, gain_db: float, progress_callback=None, attempt: int = 1) -> tuple[float, float]:
    ceiling = db_to_amp(TRUE_PEAK_CEILING_DBFS)
    final_peak = 0.0
    total_sq = 0.0
    total_count = 0
    started = time.perf_counter()
    with sf.SoundFile(str(input_path), "r") as reader, sf.SoundFile(
        str(output_path), "w", samplerate=sr, channels=2, subtype="PCM_24"
    ) as writer:
        total_frames = max(1, int(reader.frames))
        frames_read = 0
        for block in reader.blocks(blocksize=int(RENDER_CHUNK_SECONDS * sr), dtype="float32", always_2d=True):
            y = block * db_to_amp(gain_db)
            if MASTERING_INTENSITY == "loud":
                y = np.tanh(y * 1.08).astype(np.float32) / np.tanh(np.float32(1.08))
            peak = float(np.max(np.abs(y)))
            if peak > ceiling:
                # Peak clipping alone leaves sparse/dynamic songs far below
                # the requested LUFS target. Compress only this over-ceiling
                # path so ordinary mixes remain transparent and the target
                # remains reachable consistently across songs.
                threshold = ceiling * db_to_amp(-6.0)
                magnitude = np.abs(y)
                over = np.maximum(magnitude / max(threshold, 1e-9), 1.0)
                compressed = threshold * np.power(over, 0.25)
                y = np.where(magnitude > threshold, np.sign(y) * compressed, y)
                peak = float(np.max(np.abs(y)))
                if peak > ceiling:
                    y *= ceiling / peak
            y = np.clip(y, -ceiling, ceiling).astype(np.float32)
            writer.write(y)
            final_peak = max(final_peak, float(np.max(np.abs(y))))
            total_sq += float(np.sum(y * y))
            total_count += int(y.size)
            frames_read += len(block)
            if progress_callback is not None:
                ratio = min(1.0, frames_read / total_frames)
                elapsed = time.perf_counter() - started
                progress_callback({"phase": f"mastering pass {attempt} — writing", "ratio": ratio, "elapsed_seconds": elapsed})
    final_lufs = amp_to_db(math.sqrt(total_sq / total_count)) if total_count else -120.0
    return final_lufs, amp_to_db(final_peak)


def master_temp_wav_streaming(premaster_path: Path, master_path: Path, sr: int) -> tuple[float, float]:
    started = time.perf_counter()

    def report_master_progress(update: dict[str, object]) -> None:
        ratio = float(update.get("ratio") or 0.0)
        phase = str(update.get("phase") or "mastering")
        elapsed = float(update.get("elapsed_seconds") or (time.perf_counter() - started))
        detail = f"{phase} — {ratio * 100:.0f}% · {elapsed / 60:.1f} min elapsed"
        report_progress(
            {
                "current_stage": "mastering",
                "stage_detail": detail,
                "song_progress": 90 + int(ratio * 5),
                "heartbeat": time.time(),
                "elapsed_seconds": elapsed,
            }
        )

    if MATCHERING_REFERENCE is not None:
        reference = Path(MATCHERING_REFERENCE).expanduser()
        if matchering_api is None:
            raise RuntimeError(
                "Matchering reference is configured but Matchering is unavailable: "
                f"{MATCHERING_IMPORT_ERROR}"
            )
        if not reference.is_file():
            raise RuntimeError(f"Matchering reference file not found: {reference}")
        print(f"  mastering backend: Matchering reference={reference}", flush=True)
        matchering_api.log(print)
        try:
            matchering_api.process(
                target=str(premaster_path),
                reference=str(reference),
                results=[matchering_api.pcm24(str(master_path))],
            )
        except Exception as exc:
            if "Track length is exceeded in the TARGET file" not in str(exc):
                raise
            # A reference shorter than a song cannot be processed by
            # Matchering. Keep the render usable and loudness-controlled via
            # the same bounded streaming master used when no reference exists.
            print(f"  Matchering target-length fallback: {exc}", flush=True)
            premaster_audio, premaster_sr = sf.read(str(premaster_path), dtype="float32", always_2d=True)
            measured_lufs = float(pyln.Meter(premaster_sr).integrated_loudness(premaster_audio))
            gain_db = TARGET_LUFS - measured_lufs if np.isfinite(measured_lufs) else 0.0
            return apply_master_pass(premaster_path, master_path, sr, gain_db, report_master_progress, 1)
        mastered_audio, mastered_sr = sf.read(str(master_path), dtype="float32", always_2d=True)
        mastered_peak = float(np.max(np.abs(mastered_audio))) if mastered_audio.size else 0.0
        ceiling = db_to_amp(TRUE_PEAK_CEILING_DBFS)
        if mastered_peak > ceiling:
            # Matchering is an optional external backend and does not inherit
            # the local limiter contract.  Pull its result below ceiling
            # before it becomes the delivered file or the stage meter.
            mastered_audio *= ceiling / mastered_peak
            sf.write(str(master_path), mastered_audio, mastered_sr, subtype="PCM_24")
            print(
                f"MASTER SAFETY: Matchering peak {amp_to_db(mastered_peak):.2f} dBFS "
                f"-> {amp_to_db(float(np.max(np.abs(mastered_audio)))):.2f} dBFS",
                flush=True,
            )
        final_lufs = float(pyln.Meter(mastered_sr).integrated_loudness(mastered_audio))
        final_peak = measure_stream_rms_lufs(master_path)[1]
        print(f"  Matchering result: LUFS={final_lufs:.2f} peak={final_peak:.2f} dBFS", flush=True)
        return final_lufs, final_peak

    # Use integrated LUFS for gain staging. The previous RMS proxy could be
    # many dB away from the loudness of a sparse or highly dynamic song.
    premaster_audio, premaster_sr = sf.read(str(premaster_path), dtype="float32", always_2d=True)
    measured_lufs = float(pyln.Meter(premaster_sr).integrated_loudness(premaster_audio))
    gain_db = TARGET_LUFS - measured_lufs if np.isfinite(measured_lufs) else 0.0
    final_lufs = measured_lufs
    final_peak = -120.0
    for attempt in range(3):
        final_lufs, final_peak = apply_master_pass(
            premaster_path, master_path, sr, gain_db, report_master_progress, attempt + 1
        )
        mastered_audio, mastered_sr = sf.read(str(master_path), dtype="float32", always_2d=True)
        final_lufs = float(pyln.Meter(mastered_sr).integrated_loudness(mastered_audio))
        delta = TARGET_LUFS - final_lufs if np.isfinite(final_lufs) else 0.0
        print(f"  mastering pass {attempt + 1}: LUFS={final_lufs:.2f}, target={TARGET_LUFS:.2f}, adjust={delta:+.2f} dB")
        if abs(delta) <= 0.5:
            break
        gain_db += float(np.clip(delta, -6.0, 6.0))
    return final_lufs, final_peak


def measure_encoded_lufs(path: Path) -> float:
    """Measure encoded loudness without buffering the complete MP3 in Python."""
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        return float("nan")
    # Let FFmpeg's ebur128 filter perform the integrated-loudness pass in
    # streaming mode. The previous implementation decoded the whole MP3 into
    # a Python bytes object and then into a NumPy array for every song.
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return float("nan")
    matches = re.findall(r"\bI:\s*([-+]?\d+(?:\.\d+)?)\s*LUFS", result.stderr or "")
    if matches:
        return float(matches[-1])
    # Keep a compatibility fallback for FFmpeg builds that omit the summary
    # line from stderr.
    try:
        decoded = subprocess.check_output(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path), "-f", "wav", "-"],
        )
        audio, sample_rate = sf.read(io.BytesIO(decoded), dtype="float32", always_2d=True)
        return float(pyln.Meter(sample_rate).integrated_loudness(audio))
    except Exception:
        return float("nan")

def measure_true_peak_db(path: Path) -> float:
    """Measure the delivered file with ffmpeg's independent true-peak meter."""
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for independent true-peak validation")
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"true-peak measurement failed for {path}: {(result.stderr or '').strip()}")
    matches = re.findall(r"Peak:\s*([-+]?\d+(?:\.\d+)?)\s*dBFS", result.stderr or "")
    if not matches:
        raise RuntimeError(f"ffmpeg did not return a true-peak value for {path}")
    return float(matches[-1])


def attenuate_wav_in_place(path: Path, attenuation_db: float) -> None:
    """Apply an explicit, logged safety trim to a temporary master WAV."""
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for true-peak safety trim")
    temporary = Path(tempfile.mkstemp(prefix="zucker_safe_master_", suffix=".wav")[1])
    try:
        subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
             "-af", f"volume={float(attenuation_db):.6f}dB", "-c:a", "pcm_f32le", str(temporary)],
            check=True,
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def analyze_volume_anomalies(path: Path, sr: int, window_seconds: float = 3.0, jump_db: float = 6.0) -> list[tuple[float, float]]:
    window = max(1, int(round(window_seconds * sr)))
    levels: list[float] = []
    with sf.SoundFile(str(path), "r") as reader:
        for block in reader.blocks(blocksize=window, dtype="float32", always_2d=True):
            levels.append(amp_to_db(float(np.sqrt(np.mean(block * block) + 1e-12))))
    anomalies: list[tuple[float, float]] = []
    for idx, (prev, cur) in enumerate(zip(levels, levels[1:]), 1):
        delta = cur - prev
        if abs(delta) > jump_db:
            anomalies.append((idx * window_seconds, delta))
    return anomalies


def read_title_probe(path: Path, sr: int, max_seconds: float = 300.0) -> np.ndarray:
    max_frames = int(round(max_seconds * sr))
    with sf.SoundFile(str(path), "r") as f:
        audio = f.read(max_frames, dtype="float32", always_2d=True)
    if len(audio) == 0:
        return np.zeros((sr, 2), dtype=np.float32)
    return audio


def analyze_title_features(mix: np.ndarray, sr: int) -> tuple[float, str, str]:
    mono = np.mean(mix, axis=1)
    librosa_result = analyze_with_librosa_if_available(mono, sr)
    if librosa_result is not None:
        bpm, key, centroid = librosa_result
        rms_db = amp_to_db(float(np.sqrt(np.mean(mono * mono) + 1e-12)))
        return bpm, key, mood_from_features(rms_db, bpm, centroid)

    if sr != 22050:
        mono_ds = signal.resample_poly(mono, 22050, sr)
        analysis_sr = 22050
    else:
        mono_ds = mono
        analysis_sr = sr

    bpm = estimate_bpm(mono_ds, analysis_sr)
    key = estimate_key(mono_ds, analysis_sr)
    rms_db = amp_to_db(float(np.sqrt(np.mean(mono_ds * mono_ds) + 1e-12)))
    centroid = spectral_centroid(mono_ds, analysis_sr)
    return bpm, key, mood_from_features(rms_db, bpm, centroid)


def analyze_with_librosa_if_available(mono: np.ndarray, sr: int) -> tuple[float, str, float] | None:
    try:
        import librosa  # type: ignore
    except Exception:
        return None
    try:
        y = librosa.resample(mono.astype(np.float32), orig_sr=sr, target_sr=22050) if sr != 22050 else mono.astype(np.float32)
        tempo, _ = librosa.beat.beat_track(y=y, sr=22050)
        chroma = librosa.feature.chroma_cqt(y=y, sr=22050)
        key = key_from_chroma(np.mean(chroma, axis=1))
        centroid = float(np.mean(librosa.feature.spectral_centroid(y=y, sr=22050)))
        return round(float(np.asarray(tempo).ravel()[0]), 1), key, centroid
    except Exception:
        return None


def key_from_chroma(chroma: np.ndarray) -> str:
    chroma = chroma.astype(np.float64)
    chroma /= np.linalg.norm(chroma) + 1e-9
    major = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    minor = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    names = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
    scores = []
    for root in range(12):
        scores.append((float(np.dot(chroma, np.roll(major, root))), f"{names[root]} major"))
        scores.append((float(np.dot(chroma, np.roll(minor, root))), f"{names[root]} minor"))
    return max(scores, key=lambda item: item[0])[1]


def mood_from_features(rms_db: float, bpm: float, centroid: float) -> str:
    if rms_db > -14 and bpm >= 115:
        return "High-Energy Groove"
    elif bpm < 85:
        return "Slow Groove"
    elif centroid > 2500:
        return "Bright Jam"
    elif bpm >= 100:
        return "Midtempo Funk Jam"
    return "Loose Live Jam"


USED_SESSION_TITLES: set[str] = set()


def creative_title_from_features(mix: np.ndarray, sr: int, index: int, bpm: float, key: str, dominant_role: str = "") -> str:
    """Make a varied, deterministic, music-derived title for this session."""
    mono = np.mean(mix, axis=1).astype(np.float32)
    rms = amp_to_db(float(np.sqrt(np.mean(mono * mono) + 1e-12)))
    peak = amp_to_db(float(np.max(np.abs(mono)) + 1e-12))
    crest = peak - rms
    centroid = spectral_centroid(mono, sr) if len(mono) > sr else 0.0
    if bpm >= 135:
        pool = ["Chrome Weather", "Running Through Neon", "The Floor Remembers", "Silver Sparks in Motion", "After the First Downbeat", "Electric Footprints"]
    elif bpm <= 85:
        pool = ["A Window Left Open", "Blue Hour, Still Warm", "Lanterns Beneath the Rain", "The Long Way Home", "Quiet Water Rising", "Moonlight on the Cable"]
    elif centroid >= 2400:
        pool = ["Glass in the Sun", "Lucid Machinery", "A Brightness Between Us", "Prism Through Dust", "Daylight on Reeds", "Gold Thread in the Air"]
    elif dominant_role in {"bass", "kick"}:
        pool = ["Ground Wire Bloom", "The Low Road Turns", "Underneath the Engine", "Copper in the Floorboards", "Orbit Below the Room", "Weight of the Evening"]
    elif dominant_role in {"horn", "sax"}:
        pool = ["Brass After Midnight", "A Signal in Blue", "Reeds Across the Hall", "Weather for Brass", "The Broadcast Fades", "Smoke on the Bell"]
    elif dominant_role in {"keys", "keys_l", "keys_r", "synth"}:
        pool = ["Rooms Made of Light", "Soft Circuitry", "Keys Underwater", "The Arcade at Closing", "Electric Rooms", "A Door in the Chord"]
    elif crest >= 14:
        pool = ["Sparks at the Edge", "The Sudden Weather", "Footprints in Voltage", "A Wildcard in the Room", "Loose Change Lightning", "When the Walls Lean In"]
    elif rms > -18:
        pool = ["A Room Full of Signal", "Parade Without a Map", "The Color of Transit", "Ritual in the Afternoon", "Common Ground, Uncommon Sky", "A Little More Forever"]
    else:
        pool = ["Postcard from Nowhere", "The Hush Between Notes", "Undertow Lantern", "Paper Moon Rehearsal", "A Small Beautiful Detour", "Where the Echo Sleeps"]
    # Feature-derived rotation plus collision avoidance makes the complete
    # batch reviewable and prevents the old repeated adjective+noun pattern.
    offset = int(abs(round(bpm * 3 + centroid / 100 + rms * 7 + crest * 11))) % len(pool)
    candidates = pool[offset:] + pool[:offset]
    title = next((candidate for candidate in candidates if candidate not in USED_SESSION_TITLES), candidates[0])
    USED_SESSION_TITLES.add(title)
    return title


def estimate_bpm(x: np.ndarray, sr: int) -> float:
    bpm, _ = estimate_bpm_with_confidence(x, sr)
    return bpm


def estimate_bpm_with_confidence(x: np.ndarray, sr: int, min_bpm: float = 60.0, max_bpm: float = 200.0) -> tuple[float, float]:
    hop = 512
    frame = 2048
    if len(x) < frame * 4:
        return 0.0, 0.0
    energy = np.array(
        [np.sum(x[i : i + frame] ** 2) for i in range(0, len(x) - frame, hop)],
        dtype=np.float32,
    )
    novelty = np.maximum(np.diff(energy, prepend=energy[0]), 0)
    novelty -= np.mean(novelty)
    if np.max(np.abs(novelty)) < 1e-9:
        return 0.0, 0.0
    ac = signal.correlate(novelty, novelty, mode="full")[len(novelty) - 1 :]
    min_lag = int((60 / max_bpm) * sr / hop)
    max_lag = int((60 / min_bpm) * sr / hop)
    if max_lag >= len(ac):
        max_lag = len(ac) - 1
    search = ac[min_lag:max_lag]
    if len(search) == 0:
        return 0.0, 0.0
    peak_idx = int(np.argmax(search))
    lag = min_lag + peak_idx
    peak = float(search[peak_idx])
    baseline = float(np.median(search) + 1e-9)
    confidence = float(np.clip((peak / baseline - 1.0) / 8.0, 0.0, 1.0))
    return round(60 * sr / (lag * hop), 1), confidence


def estimate_segment_drum_bpm(stems: list[Stem], segment: Segment, sr: int, max_seconds: float = 300.0) -> tuple[float, float]:
    drum_stems = drum_equivalent_stems(stems)
    if not drum_stems:
        return 0.0, 0.0
    frames = min(int(round(max_seconds * sr)), int(round(segment.duration * sr)))
    if frames <= sr:
        return 0.0, 0.0
    mix = np.zeros(frames, dtype=np.float32)
    with ExitStack() as stack:
        handles = {stem.path.name: stack.enter_context(sf.SoundFile(str(stem.path), "r")) for stem in drum_stems}
        for stem in drum_stems:
            chunk = read_stem_chunk(stem, segment, 0, frames, handles[stem.path.name])
            if chunk is None:
                continue
            mono = chunk if chunk.ndim == 1 else np.mean(chunk, axis=1)
            mix[: len(mono)] += mono[:frames].astype(np.float32)
    if np.max(np.abs(mix)) < 1e-6:
        return 0.0, 0.0
    if sr != 22050:
        mix = signal.resample_poly(mix, 22050, sr).astype(np.float32)
        analysis_sr = 22050
    else:
        analysis_sr = sr
    return estimate_bpm_with_confidence(mix, analysis_sr, min_bpm=60.0, max_bpm=200.0)


def _analysis_mono(stem: Stem, segment: Segment, max_seconds: float = RHYTHM_ANALYSIS_MAX_SECONDS) -> np.ndarray:
    frames = min(int(round(max_seconds * stem.samplerate)), int(round(segment.duration * stem.samplerate)))
    if frames <= 0:
        return np.array([], dtype=np.float32)
    chunk = read_stem_chunk(stem, segment, 0, frames)
    if chunk is None:
        return np.array([], dtype=np.float32)
    mono = np.asarray(chunk if chunk.ndim == 1 else np.mean(chunk, axis=1), dtype=np.float32)
    if stem.samplerate != RHYTHM_ANALYSIS_SR and len(mono):
        mono = signal.resample_poly(mono, RHYTHM_ANALYSIS_SR, stem.samplerate).astype(np.float32)
    return np.nan_to_num(mono)


def _onset_positions(audio: np.ndarray, sr: int) -> np.ndarray:
    if len(audio) < sr:
        return np.array([], dtype=np.float64)
    frame, hop = 1024, 256
    count = 1 + (len(audio) - frame) // hop
    frames = np.lib.stride_tricks.sliding_window_view(audio, frame)[::hop][:count]
    windowed = frames * np.hanning(frame).astype(np.float32)
    spectrum = np.abs(np.fft.rfft(windowed, axis=1))
    flux = np.maximum(0.0, np.diff(spectrum, axis=0)).mean(axis=1)
    if len(flux) < 4 or float(np.max(flux)) <= 1e-8:
        return np.array([], dtype=np.float64)
    threshold = max(float(np.percentile(flux, 82)), float(np.median(flux) * 2.0))
    peaks, _ = signal.find_peaks(flux, height=threshold, distance=max(1, int(0.12 * sr / hop)))
    return peaks.astype(np.float64) * hop / sr


def classify_mic_content(stem: Stem, segment: Segment, sr: int) -> dict[str, object]:
    """Classify vocal-role mic content using wind purity vs voice formants/transients."""
    audio = _analysis_mono(stem, segment, max_seconds=180.0)
    label = stem.path.name.lower()
    explicit_wind = any(term in label for term in VOICE_WIND_EXPLICIT_TERMS)
    if len(audio) < RHYTHM_ANALYSIS_SR * 2:
        return {"classification": "wind" if explicit_wind else "voice", "confidence": 0.55 if explicit_wind else 0.35, "reason": "filename hint" if explicit_wind else "insufficient spectral evidence"}
    frame, hop = 2048, 512
    frames = np.lib.stride_tricks.sliding_window_view(audio, frame)[::hop]
    windowed = frames * np.hanning(frame).astype(np.float32)
    power = np.abs(np.fft.rfft(windowed, axis=1)) ** 2 + 1e-12
    freqs = np.fft.rfftfreq(frame, 1.0 / RHYTHM_ANALYSIS_SR)
    band = (freqs >= 100.0) & (freqs <= 4000.0)
    energy = power[:, band].sum(axis=1)
    active = energy > np.percentile(energy, 45)
    if not np.any(active):
        return {"classification": "wind" if explicit_wind else "voice", "confidence": 0.55 if explicit_wind else 0.25, "reason": "no stable active frames"}
    selected = power[active][:, band]
    selected_freqs = freqs[band]
    flatness = np.exp(np.mean(np.log(selected), axis=1)) / np.mean(selected, axis=1)
    centroid = (selected * selected_freqs[None, :]).sum(axis=1) / selected.sum(axis=1)
    peak_counts = []
    harmonicity = []
    for spectrum in selected:
        peaks, props = signal.find_peaks(spectrum, prominence=max(float(np.max(spectrum)) * 0.025, 1e-9), distance=3)
        peak_counts.append(len(peaks))
        harmonicity.append(float(np.max(spectrum) / np.sum(spectrum)))
    mean_flatness = float(np.median(flatness))
    mean_centroid = float(np.median(centroid))
    mean_peaks = float(np.median(peak_counts))
    mean_harmonicity = float(np.median(harmonicity))
    # Flute and similar winds concentrate energy in one fundamental plus a
    # small number of harmonics. Voice normally has several formant regions,
    # more upper-band energy, and noisier consonant/breath transients. The
    # pure-tone gate is deliberately strict so a vocal mic is not promoted to
    # vocal priority merely because one vowel happens to be tonal.
    upper = selected[:, selected_freqs >= 1800.0].sum(axis=1)
    total = selected.sum(axis=1) + 1e-12
    upper_ratio = float(np.median(upper / total))
    peak_stability = float(np.std(np.asarray(peak_counts, dtype=np.float64)))
    pure_wind = bool(
        mean_flatness < 0.18
        and mean_harmonicity > 0.27
        and mean_peaks <= 7.0
        and upper_ratio < 0.24
    )
    wind_score = (1.5 if pure_wind else 0.0) + (1.0 if mean_flatness < 0.22 else 0.0) + (1.0 if mean_harmonicity > 0.20 else 0.0) + (0.5 if mean_centroid > 1800.0 else 0.0)
    # A stable narrow spectrum is stronger evidence for wind than a single
    # high harmonicity frame; voice formants move and broaden over time.
    if pure_wind and peak_stability < 2.5:
        wind_score += 0.5
    if explicit_wind:
        wind_score += 1.5
    # A stem whose filename already identifies it as a vocal microphone must
    # remain vocal unless the filename explicitly names a wind instrument.
    # Spectral purity alone is not sufficient: sustained vowels can resemble
    # narrow-band wind material and otherwise lose vocal-bus processing.
    is_wind = bool(explicit_wind and wind_score >= 2.0)
    return {
        "classification": "wind" if is_wind else "voice",
        "confidence": float(np.clip(0.45 + abs(wind_score - 1.5) * 0.18, 0.0, 0.95)),
        "reason": "tonal narrow-spectrum wind profile" if is_wind else "formant-like broadband vocal profile",
        "spectral_flatness": mean_flatness,
        "spectral_centroid_hz": mean_centroid,
        "median_peak_count": mean_peaks,
        "harmonicity": mean_harmonicity,
        "upper_harmonic_energy_ratio": upper_ratio,
        "peak_count_stability": peak_stability,
        "pure_wind_profile": pure_wind,
    }


def analyze_rhythmic_consistency(stem: Stem, segment: Segment, bpm: float) -> dict[str, object]:
    """Measure onset/grid alignment and activity continuity for one stem/song."""
    audio = _analysis_mono(stem, segment)
    onsets = _onset_positions(audio, RHYTHM_ANALYSIS_SR)
    frame = max(1, RHYTHM_ANALYSIS_SR)
    activity = []
    for start in range(0, len(audio), frame):
        block = audio[start : start + frame]
        activity.append(float(np.sqrt(np.mean(block * block) + 1e-12)) if len(block) else 0.0)
    active_values = np.asarray(activity, dtype=np.float64)
    active_fraction = float(np.mean(active_values > max(1e-5, np.percentile(active_values, 25)))) if len(active_values) else 0.0
    alignment = 1.0
    if bpm > 0 and len(onsets) >= 4:
        beat = 60.0 / float(bpm)
        distances = np.abs(((onsets / beat + 0.5) % 1.0) - 0.5)
        alignment = float(np.mean(distances <= RHYTHM_ONSET_TOLERANCE_BEATS))
    eligible = stem.role not in {"kick", "snare", "drums", "vocal", "room"} and len(onsets) >= 4
    inconsistent = bool(eligible and ((bpm > 0 and alignment < 0.35) or active_fraction < 0.25))
    attenuation = RHYTHM_INCONSISTENT_ATTENUATION_DB if inconsistent and alignment < 0.35 else RHYTHM_SPARSE_ATTENUATION_DB if inconsistent else 0.0
    return {
        "flagged": inconsistent,
        "attenuation_db": attenuation,
        "onset_count": int(len(onsets)),
        "beat_alignment": alignment,
        "active_fraction": active_fraction,
        "bpm": float(bpm),
        "reason": "poor beat-grid alignment" if alignment < 0.35 and bpm > 0 else "sporadic activity" if active_fraction < 0.25 else "consistent",
    }


def analyze_harmonic_consistency(stem: Stem, segment: Segment, reference: np.ndarray, reference_sr: int, rms_db: float) -> dict[str, object]:
    """Heuristic clash test for melodic stems against the ensemble key.

    This is intentionally conservative: a stem must be loud, sufficiently
    pitched, and persistently outside the reference scale before it is trimmed.
    """
    if stem.role not in {"guitar", "keys", "keys_l", "keys_r", "synth"} or rms_db < -45.0:
        return {"flagged": False, "attenuation_db": 0.0, "clash_ratio": 0.0, "reason": "not eligible or quiet"}
    audio = _analysis_mono(stem, segment)
    if len(audio) < RHYTHM_ANALYSIS_SR * 3 or len(reference) < reference_sr * 3:
        return {"flagged": False, "attenuation_db": 0.0, "clash_ratio": 0.0, "reason": "insufficient pitched material"}
    key_name = estimate_key(reference, reference_sr)
    root_name = key_name.split()[0] if key_name and key_name != "Unknown key" else ""
    names = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
    if root_name not in names:
        return {"flagged": False, "attenuation_db": 0.0, "key": key_name, "clash_ratio": 0.0, "reason": "unknown ensemble key"}
    root = names.index(root_name)
    is_minor = "minor" in key_name.lower()
    allowed = {(root + step) % 12 for step in ([0, 2, 3, 5, 7, 8, 10] if is_minor else [0, 2, 4, 5, 7, 9, 11])}
    frame, hop = 4096, 1024
    if len(audio) < frame:
        return {"flagged": False, "attenuation_db": 0.0, "key": key_name, "clash_ratio": 0.0, "reason": "short stem"}
    frames = np.lib.stride_tricks.sliding_window_view(audio, frame)[::hop] * np.hanning(frame).astype(np.float32)
    spectrum = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(frame, 1.0 / RHYTHM_ANALYSIS_SR)
    band = (freqs >= 70.0) & (freqs <= 2200.0)
    magnitudes = spectrum[:, band]
    frame_rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    active = frame_rms > np.percentile(frame_rms, 55)
    pitches: list[int] = []
    for row, is_active in zip(magnitudes, active):
        if not is_active or not np.any(row > 0):
            continue
        frequency = float(freqs[band][int(np.argmax(row))])
        pitches.append(int(round(69.0 + 12.0 * math.log2(max(frequency, 1.0) / 440.0))) % 12)
    if len(pitches) < 8:
        return {"flagged": False, "attenuation_db": 0.0, "key": key_name, "clash_ratio": 0.0, "reason": "insufficient pitched frames"}
    clash_ratio = float(np.mean([pitch not in allowed for pitch in pitches]))
    flagged = bool(clash_ratio >= 0.58 and len(pitches) >= 12)
    attenuation = -3.0 if flagged else 0.0
    return {"flagged": flagged, "attenuation_db": attenuation, "key": key_name,
            "clash_ratio": round(clash_ratio, 3), "pitched_frames": len(pitches),
            "reason": "persistent out-of-key content" if flagged else "harmonically compatible"}


def analyze_song_mix_controls(
    stems: list[Stem],
    segment: Segment,
    sr: int,
    rms_values_db: dict[str, float],
    role_norms_db: dict[str, float],
    active_levels_db: dict[str, float] | None = None,
    segment_envelopes: dict[str, np.ndarray] | None = None,
) -> dict[str, object]:
    """Return independent per-song content roles, rhythm trims, and vocal priority trims."""
    bpm, bpm_confidence = estimate_segment_drum_bpm(stems, segment, sr)
    mic_content = {
        stem.path.name: classify_mic_content(stem, segment, sr)
        for stem in stems if stem.role == "vocal"
    }
    effective_roles = {
        stem.path.name: (
            "horn"
            if stem.role == "vocal"
            and mic_content.get(stem.path.name, {}).get("classification") == "wind"
            and any(term in stem.path.name.lower() for term in VOICE_WIND_EXPLICIT_TERMS)
            else stem.role
        )
        for stem in stems
    }
    rhythm = {
        stem.path.name: analyze_rhythmic_consistency(stem, segment, bpm)
        for stem in stems
    }
    reference_parts = []
    for stem in stems:
        if stem.role in {"bass", "kick", "snare", "drums"}:
            reference_parts.append(_analysis_mono(stem, segment))
    reference = np.mean(np.vstack([part[:min(map(len, reference_parts))] for part in reference_parts]), axis=0) if reference_parts and min(map(len, reference_parts)) > 0 else np.zeros(0, dtype=np.float32)
    harmonic = {
        stem.path.name: analyze_harmonic_consistency(stem, segment, reference, RHYTHM_ANALYSIS_SR, rms_values_db.get(stem.path.name, -120.0))
        for stem in stems
    }
    active_levels_db = active_levels_db or dict(rms_values_db)
    balance = vocal_harmonic_balance(effective_roles, active_levels_db, segment_envelopes)
    role_balance = per_song_role_balance_corrections(effective_roles, active_levels_db, segment_envelopes)
    vocal_names = set(balance["vocal_names"])
    harmonic_names = set(balance["harmonic_names"])
    vocal_group_correction = float(balance["vocal_group_correction_db"] or 0.0)
    harmonic_group_correction = float(balance["harmonic_group_correction_db"] or 0.0)
    initial_levels = {}
    for stem in stems:
        role = effective_roles[stem.path.name]
        initial_levels[stem.path.name] = active_levels_db.get(stem.path.name, -120.0)
    voice_floor = None if balance["vocal_group_level_db"] is None else float(balance["vocal_group_level_db"]) - VOCAL_PRIORITY_MARGIN_DB
    priority = {
        name: harmonic_group_correction if name in harmonic_names else 0.0
        for name in effective_roles
    }
    vocal_gain = {
        name: vocal_group_correction if name in vocal_names else 0.0
        for name in effective_roles
    }
    pair_keys = {name: vocal_pair_key(name) for name in vocal_names}
    synth_floor = None
    return {
        "bpm": bpm,
        "bpm_confidence": bpm_confidence,
        "mic_content": mic_content,
        "effective_roles": effective_roles,
        "rhythm": rhythm,
        "harmonic": harmonic,
        "vocal_priority": priority,
        "vocal_group_gain": vocal_gain,
        "vocal_pair_keys": pair_keys,
        "balance": balance,
        "role_balance": role_balance,
        "voice_floor_db": voice_floor,
        "synth_floor_db": synth_floor,
        "pan_assignments": {
            name: enforced_pan(role, name)
            for name, role in effective_roles.items()
            if role in {"guitar", "keys", "keys_l", "keys_r", "synth"}
        },
    }


def estimate_key(x: np.ndarray, sr: int) -> str:
    if len(x) < sr:
        return "Unknown key"
    f, t, z = signal.stft(x, fs=sr, nperseg=4096, noverlap=3072)
    mag = np.abs(z)
    chroma = np.zeros(12, dtype=np.float64)
    valid = (f >= 40) & (f <= 5000)
    for freq, row in zip(f[valid], mag[valid]):
        midi = int(round(69 + 12 * math.log2(freq / 440.0)))
        chroma[midi % 12] += float(np.sum(row))
    if np.max(chroma) <= 0:
        return "Unknown key"
    return key_from_chroma(chroma)


def spectral_centroid(x: np.ndarray, sr: int) -> float:
    f, _, z = signal.stft(x, fs=sr, nperseg=2048, noverlap=1024)
    mag = np.abs(z)
    return float(np.sum(f[:, None] * mag) / (np.sum(mag) + 1e-9))


def render_segment(
    stems: list[Stem],
    segment: Segment,
    index: int,
    out_dir: Path,
    output_path: Path | None = None,
    verify_announcement: bool = True,
    prepared_plan: dict[str, object] | None = None,
    artifact_dir: Path | None = None,
) -> dict[str, object]:
    render_t0 = time.perf_counter()
    song_overrides = current_song_overrides(index)
    override_trace = {str(name): dict(value) for name, value in current_override_verify_trace(index).items() if isinstance(value, dict)}
    sr = stems[0].samplerate
    target_frames = int(round(segment.duration * sr))
    used: list[str] = []
    stem_report: list[dict[str, object]] = []

    scan_t0 = time.perf_counter()
    def report_scan_progress(update: dict[str, object]) -> None:
        elapsed = time.perf_counter() - scan_t0
        stem_count = int(update.get("stem_count") or len(stems))
        stem_index = int(update.get("stem_index") or 0)
        chunk_index = int(update.get("chunk_index") or 0)
        detail = (
            f"scanning stem {stem_index} of {stem_count}: {update.get('stem')} "
            f"· chunk {chunk_index} · {elapsed / 60:.1f} min elapsed"
        )
        report_progress(
            {
                "current_stage": "scanning",
                "stage_detail": detail,
                "song_progress": min(20, max(1, int(elapsed / max(1.0, segment.duration) * 20))),
                "heartbeat": time.time(),
                "elapsed_seconds": elapsed,
            }
        )

    lightweight_render = bool(isinstance(prepared_plan, dict) and prepared_plan.get("lightweight_render"))
    if lightweight_render:
        # Fast render fallback: use current controls and conservative defaults.
        # It never scans source audio, runs Whisper, computes thresholds, or
        # rebuilds Auto-Mix. The full analysis remains an explicit Analyze step.
        names = [stem.path.name for stem in stems]
        analysis_cache = {
            "version": 2,
            "song_id": int(index),
            "selection": {"start_sec": float(segment.start), "end_sec": float(segment.end)},
            "rms_values_db": {name: -30.0 for name in names},
            "energies": {name: 1.0 for name in names},
            "has_audio": {name: True for name in names},
            "dynamic_spread_db": {name: 0.0 for name in names},
            "segment_envelopes": {name: np.array([], dtype=np.float32) for name in names},
            "segment_peaks_db": {name: -6.0 for name in names},
            "role_norms_db": {},
            "mix_controls": {
                "effective_roles": {name: stem.role for name, stem in zip(names, stems)},
                "rhythm": {},
                "harmonic": {},
                "vocal_priority": {},
                "vocal_group_gain": {},
                "vocal_pair_keys": {},
                "mic_content": {},
                "balance": {},
                "bpm": 0.0,
            },
            "noise_diagnostics": {},
            "flattening": {},
            "drum_bpm": (0.0, 0.0),
        }
        print(f"RENDER ANALYSIS CACHE: lightweight settings snapshot for song {index}", flush=True)
    else:
        if not isinstance(prepared_plan, dict):
            raise RuntimeError("Render did not receive a usable settings snapshot.")
        cache_path_value = prepared_plan.get("analysis_cache_path")
        cache_signature = prepared_plan.get("analysis_cache_signature")
        if not cache_path_value or not cache_signature:
            raise RuntimeError("Render analysis snapshot is incomplete.")
        cache_path = Path(str(cache_path_value))
        try:
            with cache_path.open("rb") as cache_file:
                analysis_cache = load_analysis_cache(cache_path)
        except Exception as exc:
            raise RuntimeError(f"Render analysis snapshot is unavailable: {exc}") from exc
        if not isinstance(analysis_cache, dict) or int(analysis_cache.get("version", 0)) != 2:
            raise RuntimeError("Render analysis snapshot version is unsupported.")
    cached_selection = analysis_cache.get("selection", {})
    if (
        int(analysis_cache.get("song_id", index)) != int(index)
        or abs(float(cached_selection.get("start_sec", -1.0)) - float(segment.start)) > 1e-6
        or abs(float(cached_selection.get("end_sec", -1.0)) - float(segment.end)) > 1e-6
    ):
        raise RuntimeError("Analyze required: analysis snapshot does not match the selected song window.")
    rms_values_db = analysis_cache.get("rms_values_db", {})
    energies = analysis_cache.get("energies", {})
    has_audio = analysis_cache.get("has_audio", {})
    dynamic_spread_db = analysis_cache.get("dynamic_spread_db", {})
    segment_envelopes = analysis_cache.get("segment_envelopes", {})
    segment_peaks_db = analysis_cache.get("segment_peaks_db", {})
    role_norms_db = analysis_cache.get("role_norms_db", {})
    mix_controls = analysis_cache.get("mix_controls", {})
    noise_diagnostics = analysis_cache.get("noise_diagnostics", {})
    flattening = analysis_cache.get("flattening", {})
    drum_bpm, drum_bpm_confidence = analysis_cache.get("drum_bpm", (0.0, 0.0))
    if not isinstance(mix_controls, dict):
        raise RuntimeError("Analyze required: Auto-Mix analysis snapshot is incomplete.")
    if not isinstance(noise_diagnostics, dict):
        noise_diagnostics = {}
    noise_names = [name for name, info in noise_diagnostics.items() if info.get("case") == "B"]
    if noise_names:
        print(f"NOISE DETECTION song={index}: empty noisy inputs muted: {', '.join(noise_names)}", flush=True)
    watchdog_names = [name for name, info in noise_diagnostics.items() if info.get("flagged")]
    if watchdog_names:
        print("NOISE WATCHDOG " + json.dumps(
            {name: noise_diagnostics[name] for name in watchdog_names},
            default=lambda value: "<profile>" if isinstance(value, np.ndarray) else value,
            sort_keys=True,
        ), flush=True)
    print("RENDER ANALYSIS CACHE: using frozen per-song snapshot; no source analysis", flush=True)
    scan_seconds = time.perf_counter() - scan_t0
    loudest_db = max(rms_values_db.values()) if rms_values_db else -120.0
    activity_decisions: dict[str, tuple[bool, str]] = {
        stem.path.name: (
            True,
            "decodable source included; activity measurement is diagnostic only",
        )
        for stem in stems
    }
    for name in noise_names:
        activity_decisions[name] = (
            False,
            "source broadband noise detected; kept in manifest and muted by safety policy",
        )
    active_names = {name for name, (active, _reason) in activity_decisions.items() if active}
    active_energies = [energies[name] for name in active_names]
    if not active_energies:
        raise RuntimeError(f"No decodable stems in segment {index:02d}")
    median_energy = np.median(active_energies)
    effective_roles = mix_controls["effective_roles"]
    rhythm_controls = mix_controls["rhythm"]
    harmonic_controls = mix_controls["harmonic"]
    vocal_priority = mix_controls["vocal_priority"]
    vocal_group_gain = mix_controls.get("vocal_group_gain", {})
    vocal_pair_keys = mix_controls.get("vocal_pair_keys", {})
    role_balance = mix_controls.get("role_balance", {})
    role_corrections = role_balance.get("role_corrections_db", {}) if isinstance(role_balance, dict) else {}
    vocal_pair_corrections = role_balance.get("vocal_pair_corrections_db", {}) if isinstance(role_balance, dict) else {}
    role_balance_reasons = role_balance.get("role_reasons", {}) if isinstance(role_balance, dict) else {}
    mic_content = mix_controls["mic_content"]
    active_levels_db = {
        name: active_level_db(rms_values_db.get(name, -120.0), segment_envelopes.get(name))
        for name in active_names
    }
    accompaniment_levels = [
        active_levels_db[name]
        for name in active_names
        if effective_roles.get(name, "") not in {"vocal", "room"}
    ]
    accompaniment_reference_db = float(np.median(accompaniment_levels)) if accompaniment_levels else None
    print("SONG_CONTENT_ANALYSIS " + json.dumps({
        "song": index,
        "bpm": mix_controls["bpm"],
        "mic_content": mic_content,
        "mic_voice_vs_wind": {name: info.get("classification") for name, info in mic_content.items()},
        "synth_floor_db": mix_controls.get("synth_floor_db"),
        "rhythm_flags": {name: value for name, value in rhythm_controls.items() if value.get("flagged")},
        "harmonic_flags": {name: value for name, value in harmonic_controls.items() if value.get("flagged")},
        "vocal_priority": {name: value for name, value in vocal_priority.items() if value < 0},
    }, sort_keys=True), flush=True)
    print(f"MIC CLASSIFICATION song={index}: " + ", ".join(
        f"{name}={info.get('classification')}" for name, info in sorted(mic_content.items())
    ), flush=True)

    track_settings: dict[str, dict[str, float | str | bool]] = {}
    solo_names = {
        stem.path.name
        for stem in stems
        if override_bool(stem_override(song_overrides, stem.path.name).get("solo"), False)
    }
    for stem in stems:
        mix_role = str(effective_roles.get(stem.path.name, stem.role))
        rhythm_adjustment_db = float(rhythm_controls.get(stem.path.name, {}).get("attenuation_db", 0.0))
        harmonic_adjustment_db = float(harmonic_controls.get(stem.path.name, {}).get("attenuation_db", 0.0))
        priority_adjustment_db = float(vocal_priority.get(stem.path.name, 0.0))
        vocal_group_adjustment_db = float(vocal_group_gain.get(stem.path.name, 0.0))
        role_balance_adjustment_db = float(role_corrections.get(stem.path.name, 0.0))
        vocal_pair_adjustment_db = float(vocal_pair_corrections.get(stem.path.name, 0.0))
        overrides = stem_override(song_overrides, stem.path.name)
        trace_row = override_trace.setdefault(stem.path.name, {})
        raw_rms_db = rms_values_db[stem.path.name]
        dynamic_spread = dynamic_spread_db.get(stem.path.name, 0.0)
        is_dynamic = dynamic_spread >= STEM_DYNAMIC_ACTIVE_SPREAD_DB
        stem_activity, activity_reason = activity_decisions[stem.path.name]
        inactive_reasons = []
        if override_bool(overrides.get("mute"), False):
            inactive_reasons.append("muted by override")
        if override_float(overrides.get("fader_db"), 0.0) <= FADER_HARD_SILENCE_DB:
            inactive_reasons.append("silenced by fader")
        if solo_names and stem.path.name not in solo_names:
            inactive_reasons.append("not soloed")
        if inactive_reasons:
            eq_defaults = role_eq_defaults(mix_role)
            requested_makeup_db = TARGET_TRACK_RMS_DBFS - raw_rms_db
            makeup_cap_db = MAX_DRUM_MAKEUP_GAIN_DB if mix_role in {"kick", "snare", "drums"} else MAX_TRACK_MAKEUP_GAIN_DB
            role_norm_db = role_norms_db.get(mix_role)
            gain_level_db = active_levels_db.get(stem.path.name, raw_rms_db)
            computed_makeup_gain_before_lift_db = per_song_auto_mix_gain_db(mix_role, gain_level_db, accompaniment_reference_db)
            computed_makeup_gain_db = float(np.clip(
                computed_makeup_gain_before_lift_db + rhythm_adjustment_db + harmonic_adjustment_db + priority_adjustment_db + vocal_group_adjustment_db + role_balance_adjustment_db + vocal_pair_adjustment_db,
                AUTO_MIX_MAX_ATTENUATION_DB,
                AUTO_MIX_ROLE_BOOST_LIMITS_DB.get(mix_role, AUTO_MIX_MAX_BOOST_DB),
            ))
            if override_bool(overrides.get("manual_makeup_gain_db"), False):
                makeup_gain_db = override_float(overrides.get("makeup_gain_db"), 0.0)
            else:
                makeup_gain_db = override_float(overrides.get("auto_mix_gain_db", overrides.get("makeup_gain_db")), computed_makeup_gain_db)
                makeup_gain_db = min(makeup_gain_db, AUTO_MIX_ROLE_BOOST_LIMITS_DB.get(mix_role, AUTO_MIX_MAX_BOOST_DB))
            user_gain_db = override_float(overrides.get("gain_db"), 0.0)
            eq_settings = {
                "eq_low_cut_hz": override_float(overrides.get("eq_low_cut_hz"), eq_defaults["eq_low_cut_hz"]),
                "eq_mid_gain_db": override_float(overrides.get("eq_mid_gain_db"), eq_defaults["eq_mid_gain_db"]),
                "eq_air_gain_db": override_float(overrides.get("eq_air_gain_db"), eq_defaults["eq_air_gain_db"]),
            }
            trace_row["dsp_applied"] = {
                "fader_db": override_float(overrides.get("fader_db"), 0.0),
                "mute": override_bool(overrides.get("mute"), False),
                "linear_gain": 0.0,
                "level_gain_db": 0.0,
                "makeup_gain_db": makeup_gain_db,
                "user_gain_db": user_gain_db,
                "total_gain_db": makeup_gain_db + user_gain_db,
                "status": "not mixed",
            }
            stem_report.append(
                {
                    "name": stem.path.name,
                    "role": stem.role,
                    "source_role": stem.role,
                    "effective_role": mix_role,
                    "content_classification": mic_content.get(stem.path.name),
                    "rhythm_analysis": rhythm_controls.get(stem.path.name),
                    "rhythmic_attenuation_db": rhythm_adjustment_db,
                    "harmonic_analysis": harmonic_controls.get(stem.path.name),
                    "harmonic_attenuation_db": harmonic_adjustment_db,
                    "vocal_priority_attenuation_db": priority_adjustment_db,
                    "vocal_group_correction_db": vocal_group_adjustment_db,
                    "vocal_pair_correction_db": vocal_pair_adjustment_db,
                    "role_balance_correction_db": role_balance_adjustment_db,
                    "role_balance_reason": role_balance_reasons.get(stem.path.name),
                    "vocal_pair_key": vocal_pair_keys.get(stem.path.name),
                    "mix_role_group": mix_role_group(mix_role, stem.path.name),
                    "rms_dbfs": raw_rms_db,
                    "active_level_db": gain_level_db,
                    "accompaniment_reference_db": accompaniment_reference_db,
                    "automatic_gain_reason": "per-song active-envelope balance",
                    "dynamic_spread_db": dynamic_spread,
                    "base_level_db": base_level_db(stem.role),
                    "lead_bonus_db": 0.0,
                    "computed_makeup_gain_db": computed_makeup_gain_db,
                    "automatic_gain_before_vocal_mic_lift_db": computed_makeup_gain_before_lift_db,
                    "automatic_vocal_mic_lift_db": AUTOMATIC_VOCAL_MIC_LIFT_DB if stem.role == "vocal" else 0.0,
                    "role_norm_db": role_norm_db,
                    "performance_deviation_db": None if role_norm_db is None else raw_rms_db - role_norm_db,
                    "automatic_performance_correction_db": None if role_norm_db is None else -float(np.clip(raw_rms_db - role_norm_db, -3.0, 3.0)),
                    "noise_detected": bool(noise_diagnostics.get(stem.path.name, {}).get("is_noise")),
                    "noise_watchdog_flagged": bool(noise_diagnostics.get(stem.path.name, {}).get("flagged")),
                    "noise_case": noise_diagnostics.get(stem.path.name, {}).get("case", "none"),
                    "noise_treatment": noise_diagnostics.get(stem.path.name, {}).get("treatment", "none"),
                    "noise_diagnostics": noise_diagnostics.get(stem.path.name, {}),
                    "makeup_gain_db": makeup_gain_db,
                    "user_gain_db": user_gain_db,
                    "level_gain_db": 0.0,
                    "total_gain_db": makeup_gain_db + user_gain_db,
                    "override_gain_db": override_float(overrides.get("fader_db"), 0.0),
                    "override_mute": override_bool(overrides.get("mute"), False),
                    "fx_enabled": override_bool(overrides.get("fx_enabled"), False),
                    "activity_decision": activity_reason,
                    "flattening_range_db": float(flattening.get(stem.path.name, {}).get("range_db", 0.0)),
                    "pan": enforced_pan(mix_role, stem.name, overrides.get("pan")),
                    "gain_overridden": abs(user_gain_db) > 0.001,
                    "reverb_base_db": reverb_send_level_db(stem.role),
                    "reverb_send_db": override_float(overrides.get("reverb_send_db"), 0.0),
                    "reverb_total_db": None if reverb_send_level_db(stem.role) is None else reverb_send_level_db(stem.role) + override_float(overrides.get("reverb_send_db"), 0.0),
                    "delay_base_db": delay_send_level_db(stem.role, 0.0),
                    "delay_send_db": override_float(overrides.get("delay_send_db"), 0.0),
                    "delay_total_db": None if delay_send_level_db(stem.role, 0.0) is None else delay_send_level_db(stem.role, 0.0) + override_float(overrides.get("delay_send_db"), 0.0),
                    **eq_settings,
                    "status": "muted (inactive)",
                    "reason": "; ".join(inactive_reasons),
                    "muted_regions": [],
                }
            )
            continue

        requested_makeup_db = TARGET_TRACK_RMS_DBFS - raw_rms_db
        makeup_cap_db = MAX_DRUM_MAKEUP_GAIN_DB if mix_role in {"kick", "snare", "drums"} else MAX_TRACK_MAKEUP_GAIN_DB
        role_norm_db = role_norms_db.get(mix_role)
        gain_level_db = active_levels_db.get(stem.path.name, raw_rms_db)
        computed_makeup_gain_before_lift_db = per_song_auto_mix_gain_db(mix_role, gain_level_db, accompaniment_reference_db)
        computed_makeup_gain_db = float(np.clip(
            computed_makeup_gain_before_lift_db + rhythm_adjustment_db + harmonic_adjustment_db + priority_adjustment_db + vocal_group_adjustment_db + role_balance_adjustment_db + vocal_pair_adjustment_db,
            AUTO_MIX_MAX_ATTENUATION_DB,
            AUTO_MIX_ROLE_BOOST_LIMITS_DB.get(mix_role, AUTO_MIX_MAX_BOOST_DB),
        ))
        if not override_bool(overrides.get("manual_makeup_gain_db"), False):
            computed_makeup_gain_db = automatic_drum_peak_guard_gain_db(
                mix_role, computed_makeup_gain_db, segment_peaks_db.get(stem.path.name, -120.0)
            )
        if override_bool(overrides.get("manual_makeup_gain_db"), False):
            makeup_gain_db = override_float(overrides.get("makeup_gain_db"), 0.0)
        else:
            makeup_gain_db = override_float(overrides.get("auto_mix_gain_db", overrides.get("makeup_gain_db")), computed_makeup_gain_db)
            makeup_gain_db = min(makeup_gain_db, AUTO_MIX_ROLE_BOOST_LIMITS_DB.get(mix_role, AUTO_MIX_MAX_BOOST_DB))
        user_gain_db = override_float(overrides.get("gain_db"), 0.0)
        lead_bonus = 1.5 if energies[stem.path.name] > median_energy * 1.35 and mix_role not in {"kick", "snare", "drums", "bass"} else 0.0
        fader_gain_db = override_float(overrides.get("fader_db"), 0.0)
        level_gain_db = fader_gain_db
        trace_row["dsp_applied"] = {
            "fader_db": fader_gain_db,
            "mute": False,
            "linear_gain": db_to_amp(makeup_gain_db + user_gain_db + level_gain_db),
            "level_gain_db": level_gain_db,
            "makeup_gain_db": makeup_gain_db,
            "user_gain_db": user_gain_db,
            "total_gain_db": makeup_gain_db + user_gain_db + level_gain_db,
            "status": "mixed",
        }
        used.append(stem.path.name)
        reason = activity_reason
        if requested_makeup_db > makeup_cap_db:
            reason += f"; gain capped at +{makeup_cap_db:.0f} dB"
        eq_defaults = role_eq_defaults(mix_role)
        eq_settings = {
            "eq_low_cut_hz": override_float(overrides.get("eq_low_cut_hz"), eq_defaults["eq_low_cut_hz"]),
            "eq_mid_gain_db": override_float(overrides.get("eq_mid_gain_db"), eq_defaults["eq_mid_gain_db"]),
            "eq_air_gain_db": override_float(overrides.get("eq_air_gain_db"), eq_defaults["eq_air_gain_db"]),
        }
        track_settings[stem.path.name] = {
            "processing_role": mix_role,
            "noise_watchdog": noise_diagnostics.get(stem.path.name, {}),
            "makeup_gain_db": makeup_gain_db + user_gain_db,
            "stored_makeup_gain_db": makeup_gain_db,
            "user_gain_db": user_gain_db,
            "level_gain_db": level_gain_db,
            "lead_bonus_db": lead_bonus,
            "reverb_send_db": override_float(overrides.get("reverb_send_db"), 0.0),
            "delay_send_db": override_float(overrides.get("delay_send_db"), 0.0),
            "fx_enabled": bool(overrides.get("fx_enabled", True)),
            "gate_enabled": override_bool(overrides.get("gate_enabled"), False),
            "space_enabled": override_bool(overrides.get("space_enabled"), False),
            "echo_enabled": override_bool(overrides.get("echo_enabled"), False),
            "activity_decision": activity_reason,
            "flattening_range_db": float(flattening.get(stem.path.name, {}).get("range_db", 0.0)),
            "pan": enforced_pan(mix_role, stem.name, overrides.get("pan")),
            "gain_overridden": abs(user_gain_db) > 0.001,
            **eq_settings,
            "active": True,
        }
        muted_regions: list[tuple[float, float]] = []
        if stem.role != "vocal":
            gate, muted_regions = build_section_gate(segment_envelopes.get(stem.path.name, np.array([], dtype=np.float32)))
            track_settings[stem.path.name]["section_gate"] = gate
        stem_report.append(
            {
                "name": stem.path.name,
                "role": stem.role,
                "source_role": stem.role,
                "effective_role": mix_role,
                "content_classification": mic_content.get(stem.path.name),
                "rhythm_analysis": rhythm_controls.get(stem.path.name),
                "rhythmic_attenuation_db": rhythm_adjustment_db,
                "harmonic_analysis": harmonic_controls.get(stem.path.name),
                "harmonic_attenuation_db": harmonic_adjustment_db,
                "vocal_priority_attenuation_db": priority_adjustment_db,
                "vocal_group_correction_db": vocal_group_adjustment_db,
                "vocal_pair_correction_db": vocal_pair_adjustment_db,
                "role_balance_correction_db": role_balance_adjustment_db,
                "role_balance_reason": role_balance_reasons.get(stem.path.name),
                "vocal_pair_key": vocal_pair_keys.get(stem.path.name),
                "mix_role_group": mix_role_group(mix_role, stem.path.name),
                "rms_dbfs": raw_rms_db,
                "active_level_db": gain_level_db,
                "accompaniment_reference_db": accompaniment_reference_db,
                "automatic_gain_reason": "per-song active-envelope balance",
                "dynamic_spread_db": dynamic_spread,
                "base_level_db": base_level_db(stem.role),
                "lead_bonus_db": lead_bonus,
                "computed_makeup_gain_db": computed_makeup_gain_db,
                "automatic_gain_before_vocal_mic_lift_db": computed_makeup_gain_before_lift_db,
                "automatic_vocal_mic_lift_db": AUTOMATIC_VOCAL_MIC_LIFT_DB if stem.role == "vocal" else 0.0,
                "role_norm_db": role_norm_db,
                "performance_deviation_db": None if role_norm_db is None else raw_rms_db - role_norm_db,
                "automatic_performance_correction_db": None if role_norm_db is None else -float(np.clip(raw_rms_db - role_norm_db, -3.0, 3.0)),
                "noise_detected": bool(noise_diagnostics.get(stem.path.name, {}).get("is_noise")),
                "noise_watchdog_flagged": bool(noise_diagnostics.get(stem.path.name, {}).get("flagged")),
                "noise_case": noise_diagnostics.get(stem.path.name, {}).get("case", "none"),
                "noise_treatment": noise_diagnostics.get(stem.path.name, {}).get("treatment", "none"),
                "noise_diagnostics": noise_diagnostics.get(stem.path.name, {}),
                "makeup_gain_db": makeup_gain_db,
                "user_gain_db": user_gain_db,
                "level_gain_db": level_gain_db,
                "total_gain_db": makeup_gain_db + user_gain_db + level_gain_db,
                "override_gain_db": fader_gain_db,
                "override_mute": override_bool(overrides.get("mute"), False),
                "fx_enabled": override_bool(overrides.get("fx_enabled"), False),
                "pan": enforced_pan(mix_role, stem.name, overrides.get("pan")),
                "gain_overridden": abs(user_gain_db) > 0.001,
                "reverb_base_db": reverb_send_level_db(stem.role),
                "reverb_send_db": override_float(overrides.get("reverb_send_db"), 0.0),
                "reverb_total_db": None if reverb_send_level_db(stem.role) is None else reverb_send_level_db(stem.role) + override_float(overrides.get("reverb_send_db"), 0.0),
                "delay_base_db": delay_send_level_db(stem.role, lead_bonus),
                "delay_send_db": override_float(overrides.get("delay_send_db"), 0.0),
                "delay_total_db": None if delay_send_level_db(stem.role, lead_bonus) is None else delay_send_level_db(stem.role, lead_bonus) + override_float(overrides.get("delay_send_db"), 0.0),
                **eq_settings,
                "status": "active",
                "reason": reason,
                "muted_regions": muted_regions,
            }
        )

    # Final, non-bypassable hierarchy ceiling. This runs after automatic gain,
    # rhythm/harmonic trims, and manual fader/makeup decisions. It is applied
    # to the actual DSP settings before any audio chunk is rendered.
    melodic_rows = [row for row in stem_report if row.get("status") == "active" and row.get("effective_role") in {"guitar", "keys", "keys_l", "keys_r"}]
    synth_rows = [row for row in stem_report if row.get("status") == "active" and row.get("effective_role") == "synth"]
    if melodic_rows and synth_rows:
        melodic_levels = [float(row.get("rms_dbfs", -120.0)) + float(row.get("makeup_gain_db", 0.0)) + float(row.get("user_gain_db", 0.0)) + float(row.get("level_gain_db", 0.0)) for row in melodic_rows]
        synth_target = min(melodic_levels) - SYNTH_BELOW_MELODIC_MARGIN_DB
        for row in synth_rows:
            synth_level = float(row.get("rms_dbfs", -120.0)) + float(row.get("makeup_gain_db", 0.0)) + float(row.get("user_gain_db", 0.0)) + float(row.get("level_gain_db", 0.0))
            excess = synth_level - synth_target
            if excess <= 0.0:
                continue
            name = str(row["name"])
            row["synth_tier_attenuation_db"] = -round(excess, 2)
            row["level_gain_db"] = float(row.get("level_gain_db", 0.0)) - excess
            row["total_gain_db"] = float(row.get("total_gain_db", 0.0)) - excess
            if name in track_settings:
                track_settings[name]["level_gain_db"] = float(track_settings[name].get("level_gain_db", 0.0)) - excess
                track_settings[name]["synth_tier_attenuation_db"] = -excess
            trace = override_trace.get(name, {})
            dsp = trace.get("dsp_applied") if isinstance(trace, dict) else None
            if isinstance(dsp, dict):
                dsp["level_gain_db"] = float(dsp.get("level_gain_db", 0.0)) - excess
                dsp["total_gain_db"] = float(dsp.get("total_gain_db", 0.0)) - excess
                dsp["synth_tier_attenuation_db"] = -excess
    vocal_rows = [row for row in stem_report if row.get("status") == "active" and row.get("effective_role") == "vocal"]
    vocal_level = float(np.median([
        float(row.get("rms_dbfs", -120.0)) + float(row.get("makeup_gain_db", 0.0)) +
        float(row.get("user_gain_db", 0.0)) + float(row.get("level_gain_db", 0.0))
        for row in vocal_rows
    ])) if vocal_rows else None
    ceiling_hits: list[dict[str, object]] = []
    if vocal_level is not None:
        for row in stem_report:
            if row.get("status") != "active" or row.get("effective_role") in {"vocal", "kick", "snare", "drums"}:
                continue
            effective_level = float(row.get("rms_dbfs", -120.0)) + float(row.get("makeup_gain_db", 0.0)) + float(row.get("user_gain_db", 0.0)) + float(row.get("level_gain_db", 0.0))
            excess = effective_level - vocal_level
            if excess <= 0.0:
                continue
            name = str(row["name"])
            row["hierarchy_ceiling_attenuation_db"] = -round(excess, 2)
            row["level_gain_db"] = float(row.get("level_gain_db", 0.0)) - excess
            row["total_gain_db"] = float(row.get("total_gain_db", 0.0)) - excess
            if name in track_settings:
                track_settings[name]["level_gain_db"] = float(track_settings[name].get("level_gain_db", 0.0)) - excess
                track_settings[name]["hierarchy_ceiling_attenuation_db"] = -excess
            trace = override_trace.get(name, {})
            dsp = trace.get("dsp_applied") if isinstance(trace, dict) else None
            if isinstance(dsp, dict):
                dsp["level_gain_db"] = float(dsp.get("level_gain_db", 0.0)) - excess
                dsp["total_gain_db"] = float(dsp.get("total_gain_db", 0.0)) - excess
                dsp["hierarchy_ceiling_attenuation_db"] = -excess
            ceiling_hits.append({"stem": name, "before_dbfs": round(effective_level, 2), "vocal_dbfs": round(vocal_level, 2), "reduced_db": round(excess, 2)})
    for row in stem_report:
        row.setdefault("hierarchy_ceiling_attenuation_db", 0.0)
    print(f"HIERARCHY CEILING song={index}: vocal={vocal_level if vocal_level is not None else 'n/a'} hits={json.dumps(ceiling_hits, sort_keys=True)}", flush=True)

    chunk_frames = int(round(RENDER_CHUNK_SECONDS * sr))
    total_chunks = max(1, int(math.ceil(target_frames / chunk_frames)))
    track_dsp_state: dict[str, np.ndarray] = {}
    bus_dsp_state: dict[str, np.ndarray] = {}
    vocal_bus_state: dict[str, np.ndarray] = {}
    fx_state: dict[str, np.ndarray] = {}
    reverb_decay = reverb_decay_for_bpm(drum_bpm)
    vocal_bus_trim_db = override_float(song_overrides.get("vocal_bus_db"), VOCAL_BUS_TRIM_DB)
    effective_mix = effective_mix_snapshot(index, song_overrides, stem_report, used, vocal_bus_trim_db)
    print("PYTHON_EFFECTIVE_MIX_JSON " + json.dumps(effective_mix, sort_keys=True), flush=True)
    with tempfile.TemporaryDirectory(prefix=f"jam_song_{index:02d}_") as tmp:
        tmp_dir = Path(tmp)
        premaster_path = tmp_dir / f"song_{index:02d}_premaster.wav"
        master_path = tmp_dir / f"song_{index:02d}_master.wav"
        mix_t0 = time.perf_counter()
        stage_meters = {
            "post_stem_gain": {"peak": 0.0, "sumsq": 0.0, "count": 0},
            "post_sum": {"peak": 0.0, "sumsq": 0.0, "count": 0},
            "post_headroom": {"peak": 0.0, "sumsq": 0.0, "count": 0},
            "post_bus_compression": {"peak": 0.0, "sumsq": 0.0, "count": 0},
            "vocal_bus_pre_compression": {"peak": 0.0, "sumsq": 0.0, "count": 0},
            "vocal_bus_post_compression": {"peak": 0.0, "sumsq": 0.0, "count": 0},
            "vocal_bus_post_trim": {"peak": 0.0, "sumsq": 0.0, "count": 0},
            "vocal_bus_post_safety": {"peak": 0.0, "sumsq": 0.0, "count": 0},
        }

        def meter_stage(name: str, audio: np.ndarray) -> None:
            values = np.asarray(audio, dtype=np.float64)
            meter = stage_meters[name]
            if values.size:
                meter["peak"] = max(float(meter["peak"]), float(np.max(np.abs(values))))
                meter["sumsq"] += float(np.sum(values * values))
                meter["count"] += int(values.size)

        def finish_stage_meters() -> dict[str, dict[str, float]]:
            result: dict[str, dict[str, float]] = {}
            for name, meter in stage_meters.items():
                rms = math.sqrt(float(meter["sumsq"]) / max(1, int(meter["count"])))
                result[name] = {"peak_dbfs": amp_to_db(float(meter["peak"])), "rms_dbfs": amp_to_db(rms)}
            return result

        with sf.SoundFile(str(premaster_path), "w", samplerate=sr, channels=2, subtype="FLOAT") as writer:
            with ExitStack() as stack:
                handles = {stem.path.name: stack.enter_context(sf.SoundFile(str(stem.path), "r")) for stem in stems}
                for chunk_start in range(0, target_frames, chunk_frames):
                    nframes = min(chunk_frames, target_frames - chunk_start)
                    chunk_timing = {"read": 0.0, "filter_compress": 0.0, "sum_bus_write": 0.0}
                    mix = np.zeros((nframes, 2), dtype=np.float32)
                    vocal_bus = np.zeros((nframes, 2), dtype=np.float32)
                    reverb_send = np.zeros((nframes, 2), dtype=np.float32)
                    delay_send = np.zeros((nframes, 2), dtype=np.float32)
                    bass_items: list[np.ndarray] = []
                    kick_control = np.zeros(nframes, dtype=np.float32)
                    for stem in stems:
                        if stem.path.name not in track_settings:
                            continue
                        t0 = time.perf_counter()
                        chunk = read_stem_chunk(stem, segment, chunk_start, nframes, handles[stem.path.name])
                        chunk_timing["read"] += time.perf_counter() - t0
                        if chunk is None:
                            continue
                        # Some late virtual-tail blocks contain NaN/Inf values
                        # even though the analysis path has already sanitized
                        # them.  Keep render DSP and activity decisions on the
                        # same finite signal.
                        chunk = np.nan_to_num(
                            np.asarray(chunk, dtype=np.float32),
                            nan=0.0,
                            posinf=1.0,
                            neginf=-1.0,
                        )
                        chunk = np.clip(chunk, -8.0, 8.0)
                        settings = track_settings[stem.path.name]
                        processing_role = str(settings.get("processing_role", stem.role))
                        watchdog = settings.get("noise_watchdog")
                        if isinstance(watchdog, dict) and str(watchdog.get("case", "none")) in {"A", "hum"}:
                            if chunk.ndim == 2:
                                filtered_channels = []
                                for channel in range(chunk.shape[1]):
                                    filtered, _ = apply_noise_watchdog_streaming(
                                        chunk[:, channel], stem.samplerate, watchdog, track_dsp_state, f"{stem.path.name}:{channel}"
                                    )
                                    filtered_channels.append(filtered)
                                chunk = np.column_stack(filtered_channels).astype(np.float32)
                            else:
                                chunk, _ = apply_noise_watchdog_streaming(
                                    chunk, stem.samplerate, watchdog, track_dsp_state, stem.path.name
                                )
                        # A mic channel classified as wind must be gated on
                        # the raw signal, before its large automatic makeup
                        # gain.  Applying the expander after makeup raises a
                        # -50 dBFS hiss above the -45 dBFS threshold and lets
                        # it pass as an audible wash.
                        wind_mic = (
                            stem.role == "vocal"
                            and str((mic_content.get(stem.path.name) or {}).get("classification", "")) == "wind"
                        )
                        if wind_mic:
                            if chunk.ndim == 2:
                                gated_channels = [
                                    noise_gate_streaming(
                                        chunk[:, channel], stem.samplerate, track_dsp_state,
                                        f"{stem.path.name}:{channel}:raw-wind",
                                    )
                                    for channel in range(chunk.shape[1])
                                ]
                                chunk = np.column_stack(gated_channels).astype(np.float32)
                            else:
                                chunk = noise_gate_streaming(
                                    chunk, stem.samplerate, track_dsp_state,
                                    f"{stem.path.name}:raw-wind",
                                )
                        makeup_gain_db = float(settings["makeup_gain_db"])
                        level_gain_db = float(settings["level_gain_db"])
                        lead_bonus = float(settings.get("lead_bonus_db", 0.0))
                        pan = float(settings.get("pan", pan_for_role(processing_role, stem.name)))
                        eq_overrides = {
                            "eq_low_cut_hz": float(settings.get("eq_low_cut_hz", role_eq_defaults(processing_role)["eq_low_cut_hz"])),
                            "eq_mid_gain_db": float(settings.get("eq_mid_gain_db", role_eq_defaults(processing_role)["eq_mid_gain_db"])),
                            "eq_air_gain_db": float(settings.get("eq_air_gain_db", role_eq_defaults(processing_role)["eq_air_gain_db"])),
                        }
                        intro_at = segment.speech_intro_start
                        preserve_vocal_speech = (
                            processing_role == "vocal"
                            and intro_at is not None
                            and segment.start - 1.0 <= float(intro_at) <= segment.end
                            and segment.start + chunk_start / sr <= float(intro_at) + 30.0
                            and segment.start + (chunk_start + nframes) / sr >= float(intro_at) - 0.5
                        )
                        flatten_info = flattening.get(stem.path.name, {})
                        flatten_curve = flatten_info.get("curve")
                        if isinstance(flatten_curve, np.ndarray) and len(flatten_curve) > 1:
                            flatten_curve = np.nan_to_num(
                                np.asarray(flatten_curve, dtype=np.float64),
                                nan=1.0,
                                posinf=1.0,
                                neginf=0.0,
                            )
                            flatten_curve = np.clip(flatten_curve, 0.0, 4.0)
                            curve_positions = np.arange(len(chunk), dtype=np.float64) / max(1, stem.samplerate) + chunk_start / max(1, stem.samplerate)
                            curve = np.interp(curve_positions, np.arange(len(flatten_curve), dtype=np.float64) * DETECTION_FRAME_SECONDS, flatten_curve, left=float(flatten_curve[0]), right=float(flatten_curve[-1]))
                            if chunk.ndim == 2:
                                chunk = (np.asarray(chunk, dtype=np.float64) * curve[:, None]).astype(np.float32)
                            else:
                                chunk = (np.asarray(chunk, dtype=np.float64) * curve).astype(np.float32)
                        t0 = time.perf_counter()
                        # Automatic/un-edited songs use the full role-aware
                        # DSP chain. An explicit user setting still wins.
                        fx_enabled = bool(settings.get("fx_enabled", True))
                        if not fx_enabled:
                            # Preserve gain/fader/pan/mute semantics while bypassing
                            # all per-stem EQ, dynamics, section gating and sends.
                            y = (np.asarray(chunk, dtype=np.float64) * db_to_amp(makeup_gain_db)).astype(np.float32)
                        elif chunk.ndim == 1:
                            key = f"{stem.path.name}:0"
                            y = process_track_streaming(chunk, stem.samplerate, processing_role, makeup_gain_db, track_dsp_state, key, eq_overrides, preserve_vocal_speech)
                        else:
                            y = np.column_stack(
                                [
                                    process_track_streaming(
                                        chunk[:, ch],
                                        stem.samplerate,
                                        processing_role,
                                        makeup_gain_db,
                                        track_dsp_state,
                                        f"{stem.path.name}:{ch}",
                                        eq_overrides,
                                        preserve_vocal_speech,
                                    )
                                    for ch in range(chunk.shape[1])
                                ]
                            ).astype(np.float32)
                        y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)
                        chunk_timing["filter_compress"] += time.perf_counter() - t0
                        t0 = time.perf_counter()
                        y *= db_to_amp(level_gain_db)
                        if fx_enabled and bool(settings.get("gate_enabled", False)) and processing_role != "vocal":
                            gate = settings.get("section_gate")
                            if isinstance(gate, np.ndarray):
                                gain = section_gate_for_chunk(gate, stem.samplerate, chunk_start, len(y))
                                if y.ndim == 2:
                                    y *= gain[:, None]
                                else:
                                    y *= gain
                        stereo = apply_stereo_pan(y, pan)
                        stereo = stereo[:nframes]
                        meter_stage("post_stem_gain", stereo)
                        send_db = reverb_send_level_db(processing_role) if fx_enabled and bool(settings.get("space_enabled", False)) else None
                        if send_db is not None:
                            send_db += float(settings.get("reverb_send_db", 0.0))
                            reverb_send[: len(stereo)] += stereo * db_to_amp(send_db)
                        delay_db = delay_send_level_db(processing_role, lead_bonus) if fx_enabled and bool(settings.get("echo_enabled", False)) else None
                        if delay_db is not None:
                            delay_db += float(settings.get("delay_send_db", 0.0))
                            delay_send[: len(stereo)] += stereo * db_to_amp(delay_db)
                        if processing_role == "vocal":
                            vocal_bus[: len(stereo)] += stereo
                        elif processing_role == "bass":
                            bass_items.append(stereo.copy())
                        else:
                            mix[: len(stereo)] += stereo
                            if processing_role == "kick":
                                kick_control[: len(stereo)] += np.mean(np.abs(stereo), axis=1)
                        chunk_timing["sum_bus_write"] += time.perf_counter() - t0
                    t0 = time.perf_counter()
                    if bass_items:
                        kick_env = signal.lfilter([0.04], [1.0, -0.96], kick_control.astype(np.float32))
                        if np.max(kick_env) > 1e-8:
                            norm = kick_env / np.max(kick_env)
                            duck = np.power(10.0, (-2.0 * norm) / 20.0).astype(np.float32)
                        else:
                            duck = np.ones(nframes, dtype=np.float32)
                        for bass_stereo in bass_items:
                            mix[: len(bass_stereo)] += bass_stereo * duck[: len(bass_stereo), None]
                    if np.max(np.abs(vocal_bus)) > 1e-8:
                        meter_stage("vocal_bus_pre_compression", vocal_bus)
                        vocal_bus = rms_compressor_streaming(
                            vocal_bus,
                            sr,
                            ratio=2.0,
                            threshold_db=-12.0,
                            attack_ms=150,
                            release_ms=600,
                            state=vocal_bus_state,
                            key="vocal_bus",
                        )
                        meter_stage("vocal_bus_post_compression", vocal_bus)
                        vocal_bus *= db_to_amp(vocal_bus_trim_db)
                        meter_stage("vocal_bus_post_trim", vocal_bus)
                        # A vocal bus can overload independently of the mix
                        # bus when several vocal/room stems or a per-song
                        # vocal trim are hot. Keep the normal path unchanged,
                        # but apply only the attenuation required for this
                        # song/chunk before the bus reaches the mix.
                        # Safety is a true peak catch only: attenuate by the
                        # exact excess above -1 dBFS, never toward a fixed
                        # program level. Upstream gain staging owns vocal
                        # hierarchy; this stage only prevents clipping.
                        vocal_ceiling = db_to_amp(-1.0)
                        vocal_peak = float(np.max(np.abs(vocal_bus)))
                        if vocal_peak > vocal_ceiling:
                            # Do not scale the entire chunk by the single
                            # worst sample: that turns a transient into a
                            # 5-minute vocal fade.  Compress only samples
                            # above the ceiling, preserving normal vocal
                            # RMS/hierarchy while guaranteeing no clipping.
                            magnitude = np.abs(vocal_bus)
                            limited = vocal_ceiling * np.tanh(magnitude / vocal_ceiling) / np.tanh(1.0)
                            vocal_bus = np.sign(vocal_bus) * np.minimum(
                                magnitude, np.minimum(limited, vocal_ceiling)
                            )
                            print(
                                f"VOCAL BUS SAFETY song={index}: "
                                f"post-trim peak {amp_to_db(vocal_peak):.2f} dBFS "
                                f"-> {amp_to_db(float(np.max(np.abs(vocal_bus)))):.2f} dBFS "
                                f"(sample-wise peak limiting; RMS preserved)",
                                flush=True,
                            )
                        meter_stage("vocal_bus_post_safety", vocal_bus)
                        mix += vocal_bus
                    mix += plate_reverb_streaming(reverb_send, sr, reverb_decay, fx_state)
                    mix += slap_delay_streaming(delay_send, sr, drum_bpm, fx_state)
                    meter_stage("post_sum", mix)
                    # Keep the summed premaster comfortably below full scale
                    # before the bus compressor and mastering gain. This is a
                    # deliberate fixed stage so the browser preview and Python
                    # export can share the same loudness model.
                    mix *= db_to_amp(PREMASTER_HEADROOM_DB)
                    # The fixed trim assumes the summed stems are reasonably
                    # staged.  A high-precision cache or a hot source must
                    # never turn that assumption into nonlinear bus overload.
                    # Apply only the additional attenuation actually needed,
                    # preserving normal mixes while keeping the bus input
                    # below the true-peak ceiling.
                    premaster_peak = float(np.max(np.abs(mix))) if mix.size else 0.0
                    premaster_ceiling = db_to_amp(TRUE_PEAK_CEILING_DBFS)
                    if premaster_peak > premaster_ceiling:
                        mix *= premaster_ceiling / premaster_peak
                        print(
                            f"HEADROOM SAFETY: post-sum peak {amp_to_db(premaster_peak):.2f} dBFS "
                            f"-> {amp_to_db(float(np.max(np.abs(mix)))):.2f} dBFS",
                            flush=True,
                        )
                    meter_stage("post_headroom", mix)
                    mix = apply_chunk_fades(mix, sr, chunk_start, target_frames)
                    mix = mix_bus_streaming(mix, sr, bus_dsp_state)
                    meter_stage("post_bus_compression", mix)
                    writer.write(mix)
                    chunk_timing["sum_bus_write"] += time.perf_counter() - t0
                    chunk_index = chunk_start // chunk_frames + 1
                    report_progress(
                        {
                            "current_stage": "mixing",
                            "stage_detail": (
                                f"chunk {chunk_index} of {total_chunks} · "
                                f"{(time.perf_counter() - render_t0) / 60:.1f} min elapsed"
                            ),
                            "song_progress": int(chunk_index / total_chunks * 88),
                            "heartbeat": time.time(),
                            "elapsed_seconds": time.perf_counter() - render_t0,
                        }
                    )
                    print(
                        f"  timing chunk {chunk_index:02d}: read={chunk_timing['read']:.2f}s "
                        f"filter+compress={chunk_timing['filter_compress']:.2f}s "
                        f"sum/bus/write={chunk_timing['sum_bus_write']:.2f}s"
                    )
        mix_seconds = time.perf_counter() - mix_t0
        volume_anomalies = analyze_volume_anomalies(premaster_path, sr)
        report_progress(
            {
                "current_stage": "mastering",
                "stage_detail": "measuring and limiting",
                "song_progress": 90,
                "heartbeat": time.time(),
            }
        )
        print("  stage mastering")
        master_t0 = time.perf_counter()
        final_lufs, final_peak = master_temp_wav_streaming(premaster_path, master_path, sr)
        stage_meters_final = finish_stage_meters()
        vocal_trim_peak = float(stage_meters_final["vocal_bus_post_trim"]["peak_dbfs"])
        vocal_safe_peak = float(stage_meters_final["vocal_bus_post_safety"]["peak_dbfs"])
        stage_meters_final["vocal_bus_safety_reduction_db"] = max(0.0, vocal_trim_peak - vocal_safe_peak)
        post_master_rms, post_master_peak = measure_stream_rms_lufs(master_path)
        stage_meters_final["post_master"] = {"peak_dbfs": post_master_peak, "rms_dbfs": post_master_rms}
        wav_true_peak_dbfs = measure_true_peak_db(master_path)
        codec_safety_trim_db = 0.0
        if wav_true_peak_dbfs > TRUE_PEAK_CEILING_DBFS:
            codec_safety_trim_db = float(TRUE_PEAK_CEILING_DBFS - wav_true_peak_dbfs - 0.2)
            attenuate_wav_in_place(master_path, codec_safety_trim_db)
            post_master_rms, post_master_peak = measure_stream_rms_lufs(master_path)
            wav_true_peak_dbfs = measure_true_peak_db(master_path)
        if wav_true_peak_dbfs > TRUE_PEAK_CEILING_DBFS:
            raise RuntimeError(
                f"Master WAV true peak {wav_true_peak_dbfs:.2f} dBFS exceeds "
                f"the configured ceiling {TRUE_PEAK_CEILING_DBFS:.2f} dBFS"
            )
        stage_meters_final["post_master"] = {
            "peak_dbfs": post_master_peak,
            "rms_dbfs": post_master_rms,
            "true_peak_dbfs": wav_true_peak_dbfs,
            "true_peak_tool": "ffmpeg ebur128=peak=true",
            "safety_trim_db": codec_safety_trim_db,
        }
        final_peak = post_master_peak
        print("STAGE METRICS " + json.dumps(stage_meters_final, sort_keys=True), flush=True)
        master_seconds = time.perf_counter() - master_t0
        title_probe = read_title_probe(master_path, sr)
        mix_bpm, key, mood = analyze_title_features(title_probe, sr)
        bpm = drum_bpm if drum_bpm_confidence >= 0.15 and drum_bpm > 0 else mix_bpm
        custom_title = str(song_overrides.get("title", "")).strip()
        dominant_role = max(
            (row for row in stem_report if str(row.get("status", "")) == "active"),
            key=lambda row: float(row.get("rms_dbfs", -120.0)) + float(row.get("lead_bonus_db", 0.0)),
            default={},
        ).get("role", "")
        title = custom_title or creative_title_from_features(title_probe, sr, index, bpm, key, str(dominant_role))
        numbered_title = f"{index:02d} - {title}"
        mp3_path = output_path or (out_dir / f"{sanitize_filename(numbered_title)}.mp3")
        mp3_path = Path(mp3_path)
        mp3_path.parent.mkdir(parents=True, exist_ok=True)
        report_progress(
            {
                "current_stage": "encoding",
                "stage_detail": "writing MP3",
                "song_progress": 96,
                "heartbeat": time.time(),
            }
        )
        print("  stage encoding")
        encode_t0 = time.perf_counter()
        mp3_true_peak_dbfs = float("nan")
        mp3_codec_trim_db = 0.0
        for encode_attempt in range(1, 4):
            encode_mp3(master_path, mp3_path, numbered_title)
            mp3_true_peak_dbfs = measure_true_peak_db(mp3_path)
            if mp3_true_peak_dbfs <= TRUE_PEAK_CEILING_DBFS:
                break
            if encode_attempt >= 3:
                raise RuntimeError(
                    f"MP3 true peak {mp3_true_peak_dbfs:.2f} dBFS exceeds "
                    f"the configured ceiling {TRUE_PEAK_CEILING_DBFS:.2f} dBFS after {encode_attempt} attempts"
                )
            trim_db = float(TRUE_PEAK_CEILING_DBFS - mp3_true_peak_dbfs - 0.2)
            mp3_codec_trim_db += trim_db
            attenuate_wav_in_place(master_path, trim_db)
            wav_true_peak_dbfs = measure_true_peak_db(master_path)
            print(
                f"MP3 TRUE-PEAK SAFETY attempt={encode_attempt} "
                f"mp3_peak={mp3_true_peak_dbfs:.2f} dBFS trim={trim_db:+.2f} dB",
                flush=True,
            )
        encode_seconds = time.perf_counter() - encode_t0
        encoded_lufs = measure_encoded_lufs(mp3_path)
        if np.isfinite(encoded_lufs):
            final_lufs = encoded_lufs
        announcement_verification = (
            verify_rendered_announcement(mp3_path, segment, index)
            if verify_announcement else
            {"checked": False, "matches_displayed_introduction": True, "reason": "preview excerpt; no song-introduction expectation"}
        )
        if verify_announcement:
            if not announcement_verification.get("checked") or not announcement_verification.get("matches_displayed_introduction"):
                announcement_verification["verification_warning"] = (
                    announcement_verification.get("reason") or "rendered audio did not match the displayed introduction"
                )
                print(f"  Announcement verification warning for song {index}: {announcement_verification['verification_warning']}", flush=True)

        preserved_artifacts: dict[str, str] = {}
        if artifact_dir is not None:
            artifact_root = Path(artifact_dir)
            artifact_root.mkdir(parents=True, exist_ok=True)
            premaster_artifact = artifact_root / f"song_{index:02d}_premaster.wav"
            master_artifact = artifact_root / f"song_{index:02d}_master.wav"
            shutil.copy2(premaster_path, premaster_artifact)
            shutil.copy2(master_path, master_artifact)
            preserved_artifacts = {
                "premaster_wav": str(premaster_artifact),
                "master_wav": str(master_artifact),
            }

    total_seconds = time.perf_counter() - render_t0
    print(
        f"  timing totals: scan={scan_seconds:.2f}s mix={mix_seconds:.2f}s "
        f"master={master_seconds:.2f}s encode={encode_seconds:.2f}s total={total_seconds:.2f}s"
    )
    print(f"  {index:02d}: {mp3_path.name}  LUFS={final_lufs:.2f} peak={final_peak:.2f} dBFS")
    return {
        "index": index,
        "start": segment.start,
        "end": segment.end,
        "duration": segment.duration,
        "core_start": segment.core_start,
        "core_end": segment.core_end,
        "nominal_end": segment.nominal_end,
        "mc_start": segment.mc_start,
        "mc_end": segment.mc_end,
        "next_mc_start": segment.next_mc_start,
        "next_mc_end": segment.next_mc_end,
        "post_mic_start": segment.post_mic_start,
        "post_mic_end": segment.post_mic_end,
        "speech_text": segment.speech_text,
        "speech_confidence": segment.speech_confidence,
        "spoken_song_number": segment.spoken_song_number,
        "assigned_song_number": segment.assigned_song_number,
        "musician_labels": dict(segment.musician_labels),
        "title": numbered_title,
        "mix_source": mix_source_label(song_overrides),
        "bpm": bpm,
        "bpm_confidence": drum_bpm_confidence,
        "bpm_source": "drums" if drum_bpm_confidence >= 0.15 and drum_bpm > 0 else "mix fallback",
        "key": key,
        "mood": mood,
        "lufs": final_lufs,
        "peak_dbfs": final_peak,
        "master_true_peak_dbfs": wav_true_peak_dbfs,
        "master_true_peak_tool": "ffmpeg ebur128=peak=true",
        "mp3_true_peak_dbfs": mp3_true_peak_dbfs,
        "mp3_true_peak_tool": "ffmpeg ebur128=peak=true",
        "mp3_codec_safety_trim_db": mp3_codec_trim_db,
        "file": str(mp3_path),
        "stems": used,
        "missing_stems": [
            {"name": row.get("name"), "role": row.get("role"), "reason": row.get("reason"), "status": row.get("status")}
            for row in stem_report if str(row.get("status", "")) != "active"
        ],
        "stem_report": sorted(stem_report, key=lambda item: str(item["name"])),
        "applied_overrides_verified": override_trace,
        "applied_mix_state": song_overrides,
        "effective_mix": effective_mix,
        "volume_anomalies": volume_anomalies,
        "mastering_intensity": MASTERING_INTENSITY,
        "target_lufs": TARGET_LUFS,
        "announcement_verification": announcement_verification,
        "vocal_bus_db": vocal_bus_trim_db,
        "stage_metrics": stage_meters_final,
        "premaster_headroom_db": PREMASTER_HEADROOM_DB,
        "preserved_artifacts": preserved_artifacts,
        "timings": {
            "scan_seconds": round(float(scan_seconds), 3),
            "mix_seconds": round(float(mix_seconds), 3),
            "master_seconds": round(float(master_seconds), 3),
            "encode_seconds": round(float(encode_seconds), 3),
            "total_seconds": round(float(total_seconds), 3),
        },
    }


def resolve_ffprobe() -> str | None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        return ffprobe
    ffmpeg = resolve_ffmpeg()
    if ffmpeg:
        sibling = Path(ffmpeg).with_name("ffprobe")
        if sibling.exists():
            return str(sibling)
    return None


def render_file_duration(path: Path) -> float:
    ffprobe = resolve_ffprobe()
    if ffprobe:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        duration = float(result.stdout.strip())
        if np.isfinite(duration) and duration > 0:
            return duration
        raise RuntimeError("audio duration is missing or invalid")
    try:
        info = sf.info(str(path))
    except Exception as exc:
        raise RuntimeError("ffprobe is unavailable and the output is not readable by soundfile") from exc
    duration = info.frames / info.samplerate
    if not np.isfinite(duration) or duration <= 0:
        raise RuntimeError("audio duration is missing or invalid")
    return float(duration)


def validate_render_file(path: Path, expected_duration: float, tolerance_seconds: float = 2.0) -> float:
    path = Path(path)
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"render output was not created: {path}")
    if path.stat().st_size <= 0:
        raise RuntimeError(f"render output is empty: {path}")
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found while validating render output")
    decoded = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    if decoded.returncode != 0:
        detail = (decoded.stderr or "audio decode failed").strip()
        raise RuntimeError(f"render output could not be decoded: {detail}")
    duration = render_file_duration(path)
    tolerance = max(float(tolerance_seconds), float(expected_duration) * 0.01)
    if abs(duration - float(expected_duration)) > tolerance:
        raise RuntimeError(
            f"render duration {duration:.2f}s is outside expected {float(expected_duration):.2f}s ± {tolerance:.2f}s"
        )
    return duration


def promote_render_file(source: Path, destination: Path, expected_duration: float) -> None:
    source = Path(source)
    destination = Path(destination)
    validate_render_file(source, expected_duration)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(
        f".{destination.name}.{os.getpid()}.{time.time_ns()}.partial"
    )
    try:
        shutil.copy2(source, partial)
        validate_render_file(partial, expected_duration)
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def encode_mp3(wav_path: Path, mp3_path: Path, title: str) -> None:
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found. Install it with: brew install ffmpeg")
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(wav_path),
        "-ar",
        str(EXPORT_SAMPLE_RATE),
        "-b:a",
        MP3_BITRATE,
        "-metadata",
        "artist=ZuckerZauberSessions",
        "-metadata",
        f"album=Jam {SESSION_DATE}",
        "-metadata",
        f"title={title}",
        str(mp3_path),
    ]
    subprocess.run(cmd, check=True)


def write_report(out_dir: Path, rows: list[dict[str, object]], segments: list[Segment]) -> None:
    report = out_dir / f"mix_report_{timestamp_label()}.txt"
    with report.open("w", encoding="utf-8") as f:
        f.write(f"Jam mix report\nDate: {SESSION_DATE}\nSource: {SOURCE_DIR}\n\n")
        f.write("Detected song list:\n")
        for i, seg in enumerate(segments, 1):
            mc = f"{fmt_time(seg.mc_start)} - {fmt_time(seg.mc_end)}" if seg.mc_start is not None and seg.mc_end is not None else "n/a"
            nominal_end = seg.nominal_end if seg.nominal_end is not None else seg.end
            duration = nominal_end - seg.start
            flags = []
            if duration < SUSPICIOUS_SHORT_SONG_SECONDS:
                flags.append("SUSPICIOUS <5m")
            if duration > SUSPICIOUS_LONG_SONG_SECONDS:
                flags.append("SUSPICIOUS >20m")
            if duration < HARD_MIN_SONG_SECONDS or duration > HARD_MAX_SONG_SECONDS:
                flags.append("HARD DURATION VIOLATION")
            if seg.boundary_source == "stage-clock-inferred":
                flags.append("boundary inferred from stage-clock, not silence")
            flag_text = ", ".join(flags) if flags else "-"
            f.write(
                f"{i:02d}. MC {mc}; song {fmt_time(seg.start)} - {fmt_time(nominal_end)} "
                f"({fmt_time(duration)}); render through {fmt_time(seg.end)}; {flag_text}\n"
            )
        f.write("\nRender results:\n")
        for row in rows:
            if row.get("error"):
                f.write(
                    f"{int(row.get('index', 0)):02d}. FAILED — {row.get('mix_source', 'Automatic mix')}\n"
                    f"    Error: {row['error']}\n"
                )
                continue
            f.write(
                f"{row['index']:02d}. {row['title']}\n"
                f"    Time: {fmt_time(row['start'])} - {fmt_time(row['end'])}\n"
                f"    MC intro: {fmt_time(row['mc_start']) if row['mc_start'] is not None else 'n/a'} - "
                f"{fmt_time(row['mc_end']) if row['mc_end'] is not None else 'n/a'}\n"
                f"    Nominal song end: {fmt_time(row['nominal_end']) if row['nominal_end'] is not None else fmt_time(row['end'])}\n"
                f"    Next MC break: {fmt_time(row['next_mc_start']) if row['next_mc_start'] is not None else '-'}"
                f"{' - ' + fmt_time(row['next_mc_end']) if row['next_mc_end'] is not None else ''}\n"
                f"    BPM/key: {row['bpm']} / {row['key']} "
                f"(tempo source: {row.get('bpm_source', 'n/a')}, confidence {float(row.get('bpm_confidence', 0.0)):.2f})\n"
                f"    Mix source: {row.get('mix_source', 'Automatic mix')}\n"
                f"    Master: {row.get('mastering_intensity', 'natural')} target {float(row.get('target_lufs', TARGET_LUFS)):.1f} LUFS\n"
                f"    LUFS/peak: {row['lufs']:.2f} LUFS / {row['peak_dbfs']:.2f} dBFS\n"
                f"    File: {row['file']}\n"
                f"    Stems used: {', '.join(row['stems'])}\n"
            )
            missing_stems = row.get("missing_stems", [])
            if missing_stems:
                f.write("    Stems omitted:\n")
                for missing in missing_stems:
                    f.write(f"      {missing.get('name')}: {missing.get('reason') or missing.get('status') or 'not active'}\n")
            else:
                f.write("    Stems omitted: none\n")
            if row.get("speech_text"):
                f.write(f"    Whisper announcement: {row['speech_text']}\n")
            if row.get("musician_labels"):
                f.write(f"    Musician labels: {row['musician_labels']}\n")
            anomalies = row.get("volume_anomalies", [])
            if anomalies:
                f.write(
                    "    Volume watchdog: "
                    + ", ".join(
                        f"{fmt_time(float(when))} ({float(delta):+.1f} dB)" for when, delta in anomalies[:12]
                    )
                    + (f", +{len(anomalies) - 12} more" if len(anomalies) > 12 else "")
                    + "\n"
                )
            else:
                f.write("    Volume watchdog: no >6 dB adjacent 3s jumps detected\n")
            override_lines = []
            muted_overrides = []
            for stem_row in row.get("stem_report", []):
                override_gain = float(stem_row.get("override_gain_db", 0.0) or 0.0)
                if abs(override_gain) > 0.01:
                    override_lines.append(f"{stem_row['name']}: {override_gain:+.1f} dB")
                if bool(stem_row.get("override_mute")):
                    muted_overrides.append(str(stem_row["name"]))
            f.write(
                "    Overrides applied: "
                + (", ".join(override_lines) if override_lines else "none")
                + f"; muted: {', '.join(muted_overrides) if muted_overrides else 'none'}\n"
            )
            adaptive = [
                f"{item['name']} deviation {float(item['performance_deviation_db']):+.1f} dB -> correction {float(item['automatic_performance_correction_db']):+.1f} dB"
                for item in row.get("stem_report", [])
                if item.get("performance_deviation_db") is not None
            ]
            f.write("    Per-song role adaptation: " + ("; ".join(adaptive) if adaptive else "none") + "\n")
            voice_mics = []
            for item in row.get("stem_report", []):
                if item.get("source_role") != "vocal":
                    continue
                classification = item.get("content_classification")
                if not isinstance(classification, dict):
                    classification = {}
                voice_mics.append(
                    f"{item['name']}={classification.get('classification', 'unknown')}"
                )
            f.write("    Mic content (per song): " + (", ".join(voice_mics) if voice_mics else "none") + "\n")
            rhythm_flags = [
                f"{item['name']} {float(item.get('rhythmic_attenuation_db', 0.0)):+.1f} dB ({item.get('rhythm_analysis', {}).get('reason', 'flagged')})"
                for item in row.get("stem_report", [])
                if float(item.get("rhythmic_attenuation_db", 0.0) or 0.0) < 0
            ]
            f.write("    Rhythmic consistency trims: " + (", ".join(rhythm_flags) if rhythm_flags else "none") + "\n")
            harmonic_flags = [
                f"{item['name']} {float(item.get('harmonic_attenuation_db', 0.0)):+.1f} dB ({item.get('harmonic_analysis', {}).get('reason', 'flagged')})"
                for item in row.get("stem_report", [])
                if float(item.get("harmonic_attenuation_db", 0.0) or 0.0) < 0
            ]
            f.write("    Harmonic clash trims (heuristic): " + (", ".join(harmonic_flags) if harmonic_flags else "none") + "\n")
            ceiling_flags = [
                f"{item['name']} {float(item.get('hierarchy_ceiling_attenuation_db', 0.0)):+.1f} dB"
                for item in row.get("stem_report", [])
                if float(item.get("hierarchy_ceiling_attenuation_db", 0.0) or 0.0) < 0
            ]
            f.write("    Hard vocal hierarchy ceiling: " + (", ".join(ceiling_flags) if ceiling_flags else "none") + "\n")
            priority_flags = [
                f"{item['name']} {float(item.get('vocal_priority_attenuation_db', 0.0)):+.1f} dB; applied pre-fader {float(item.get('rms_dbfs', -120.0)) + float(item.get('makeup_gain_db', 0.0)):+.1f} dBFS"
                for item in row.get("stem_report", [])
                if float(item.get("vocal_priority_attenuation_db", 0.0) or 0.0) < 0
            ]
            f.write("    Vocal-priority trims: " + (", ".join(priority_flags) if priority_flags else "none") + "\n")
            noise = [str(item["name"]) for item in row.get("stem_report", []) if item.get("noise_detected")]
            f.write("    Noise-only stems muted: " + (", ".join(noise) if noise else "none") + "\n")
            noise_watchdog = [
                f"{item['name']} case {item.get('noise_case', 'none')}: {item.get('noise_treatment', 'none')}"
                for item in row.get("stem_report", [])
                if item.get("noise_watchdog_flagged")
            ]
            f.write("    Noise watchdog (per song): " + ("; ".join(noise_watchdog) if noise_watchdog else "none") + "\n")
            verified = row.get("applied_overrides_verified", {})
            if isinstance(verified, dict):
                changed_verified = []
                for stem_name, trace in sorted(verified.items()):
                    if not isinstance(trace, dict):
                        continue
                    file_write = trace.get("file_write", {}) if isinstance(trace.get("file_write"), dict) else {}
                    worker_read = trace.get("worker_read", {}) if isinstance(trace.get("worker_read"), dict) else {}
                    dsp = trace.get("dsp_applied", {}) if isinstance(trace.get("dsp_applied"), dict) else {}
                    fader_write = override_float(file_write.get("fader_db"), 0.0)
                    fader_read = override_float(worker_read.get("fader_db"), 0.0)
                    fader_dsp = override_float(dsp.get("fader_db"), 0.0)
                    mute_write = override_bool(file_write.get("mute"), False)
                    mute_read = override_bool(worker_read.get("mute"), False)
                    mute_dsp = override_bool(dsp.get("mute"), False)
                    if not (abs(fader_write) > 0.01 or mute_write or abs(fader_read) > 0.01 or mute_read or abs(fader_dsp) > 0.01 or mute_dsp):
                        continue
                    matches = (
                        abs(fader_write - fader_read) <= 0.001
                        and abs(fader_read - fader_dsp) <= 0.001
                        and mute_write == mute_read == mute_dsp
                    )
                    changed_verified.append(
                        f"      {stem_name}: "
                        f"file_write fader={fader_write:+.1f} dB mute={mute_write}; "
                        f"worker_read fader={fader_read:+.1f} dB mute={mute_read}; "
                        f"dsp_applied fader={fader_dsp:+.1f} dB mute={mute_dsp} "
                        f"level={override_float(dsp.get('level_gain_db'), 0.0):+.1f} dB "
                        f"linear={override_float(dsp.get('linear_gain'), 0.0):.6f} "
                        f"status={dsp.get('status', 'unknown')}; "
                        f"match={'yes' if matches else 'NO'}\n"
                    )
                if changed_verified:
                    f.write("    Applied overrides (verified):\n")
                    for line in changed_verified:
                        f.write(line)
                else:
                    f.write("    Applied overrides (verified): none\n")
            f.write(
                "    Applied mix parameters (complete): "
                f"vocal_bus={float(row.get('vocal_bus_db', 0.0)):+.1f} dB, "
                f"target_lufs={float(row.get('target_lufs', TARGET_LUFS)):.1f}\n"
            )
            applied_mix_state = row.get("applied_mix_state", {})
            if isinstance(applied_mix_state, dict):
                f.write(
                    "    Applied mix state JSON: "
                    + json.dumps(applied_mix_state, sort_keys=True, separators=(",", ":"))
                    + "\n"
                )
            for stem_row in row.get("stem_report", []):
                reverb_base = stem_row.get("reverb_base_db")
                delay_base = stem_row.get("delay_base_db")
                reverb_total = stem_row.get("reverb_total_db")
                delay_total = stem_row.get("delay_total_db")
                reverb_override = float(stem_row.get("reverb_send_db", 0.0) or 0.0)
                delay_override = float(stem_row.get("delay_send_db", 0.0) or 0.0)
                f.write(
                    f"      {stem_row['name']}: "
                    f"role={stem_row.get('role')}, "
                    f"base={float(stem_row.get('base_level_db', 0.0)):+.1f} dB, "
                    f"lead_bonus={float(stem_row.get('lead_bonus_db', 0.0)):+.1f} dB, "
                    f"computed_gain={float(stem_row.get('computed_makeup_gain_db', stem_row.get('makeup_gain_db', 0.0))):+.1f} dB, "
                    f"auto_before_vocal_mic_lift={float(stem_row.get('automatic_gain_before_vocal_mic_lift_db', stem_row.get('computed_makeup_gain_db', 0.0))):+.1f} dB, "
                    f"vocal_mic_lift={float(stem_row.get('automatic_vocal_mic_lift_db', 0.0)):+.1f} dB, "
                    f"makeup={float(stem_row.get('makeup_gain_db', 0.0)):+.1f} dB, "
                    f"gain={float(stem_row.get('user_gain_db', 0.0)):+.1f} dB, "
                    f"gain_overridden={bool(stem_row.get('gain_overridden'))}, "
                    f"fader={float(stem_row.get('override_gain_db', 0.0)):+.1f} dB, "
                    f"total_gain={float(stem_row.get('total_gain_db', 0.0)):+.1f} dB, "
                    f"mute={bool(stem_row.get('override_mute'))}, "
                    f"pan={float(stem_row.get('pan', 0.0)):+.2f}, "
                    f"eq_low_cut={float(stem_row.get('eq_low_cut_hz', 0.0)):.0f} Hz, "
                    f"eq_mid={float(stem_row.get('eq_mid_gain_db', 0.0)):+.1f} dB, "
                    f"eq_air={float(stem_row.get('eq_air_gain_db', 0.0)):+.1f} dB, "
                    f"reverb={reverb_override:+.1f} dB"
                    f"{'' if reverb_total is None else f' total={float(reverb_total):+.1f} dB'}, "
                    f"delay={delay_override:+.1f} dB"
                    f"{'' if delay_total is None else f' total={float(delay_total):+.1f} dB'}, "
                    f"status={stem_row.get('status')}\n"
                )
            f.write("    Stem diagnostics:\n")
            for stem_row in row.get("stem_report", []):
                muted_regions = stem_row.get("muted_regions", [])
                muted_text = ""
                if muted_regions:
                    muted_text = "; muted sections " + ", ".join(
                        f"{fmt_time(float(start))}-{fmt_time(float(end))}" for start, end in muted_regions[:12]
                    )
                    if len(muted_regions) > 12:
                        muted_text += f", +{len(muted_regions) - 12} more"
                f.write(
                    f"      {stem_row['name']}: "
                    f"rms={stem_row['rms_dbfs']:.1f} dBFS, "
                    f"spread={float(stem_row.get('dynamic_spread_db', 0.0)):.1f} dB, "
                    f"makeup={stem_row['makeup_gain_db']:+.1f} dB, "
                    f"level={stem_row['level_gain_db']:+.1f} dB, "
                    f"override={float(stem_row.get('override_gain_db', 0.0)):+.1f} dB, "
                    f"pan={float(stem_row.get('pan', 0.0)):+.2f}, "
                    f"{stem_row['status']} ({stem_row['reason']}{muted_text})\n"
                )
        final_cut = DETECTION_STRATEGY.get("final_render_boundary_audit", [])
        if final_cut:
            f.write("\nFinal render cut gate audit (exact source cut sample):\n")
            for item in final_cut:
                active = ", ".join(str(x.get("stem")) for x in item.get("active_instruments", [])) or "none"
                f.write(f"  song {item.get('song')}: sample {item.get('cut_sample')} at {fmt_time(float(item.get('cut_seconds', 0)))}; safe={item.get('safe')}; active instruments={active}\n")
        titles = [str(row.get("title")) for row in rows if row.get("title")]
        if titles:
            f.write("\nBatch title review (unique within this session):\n")
            for number, title in enumerate(titles, 1):
                f.write(f"  {number:02d}. {title}\n")
        f.write("\nSettings used:\n")
        f.write("Detection strategy: merged MC transition zones; each rendered song starts after its preceding MC zone and fades into the following MC zone.\n")
        f.write("Detection cache: per-stem RMS envelopes; detector retuning reuses the cache when source stems are unchanged.\n")
        f.write(f"MC break minimum: {MC_BREAK_MIN_SECONDS}s\n")
        f.write(f"Next-MC render fade tail: {MC_NEXT_FADE_SECONDS}s\n")
        f.write(f"Silence gap: {SILENCE_GAP_SECONDS}s\n")
        f.write(f"Silence threshold: {SILENCE_THRESHOLD_DB} dB relative\n")
        f.write(f"Detection smoothing: {DETECTION_SMOOTH_SECONDS}s\n")
        f.write(f"Segment padding: -{SEGMENT_PRE_PAD_SECONDS}s / +{SEGMENT_POST_PAD_SECONDS}s\n")
        f.write(f"Render fades: in {RENDER_FADE_IN_SECONDS}s / out {RENDER_FADE_OUT_SECONDS}s\n")
        f.write(f"Minimum song length: {MIN_SONG_SECONDS}s\n")
        f.write(f"Track RMS target: {TARGET_TRACK_RMS_DBFS} dBFS\n")
        f.write(f"Inactive stem floor: {STEM_INACTIVE_FLOOR_DBFS} dBFS\n")
        f.write(f"Inactive relative threshold: {STEM_RELATIVE_INACTIVE_DB} dB below loudest stem\n")
        f.write(f"Inactive dynamic-spread guard: stems with >={STEM_DYNAMIC_ACTIVE_SPREAD_DB} dB p95-p20 envelope spread stay active even when quiet\n")
        f.write(f"Max track makeup gain: {MAX_TRACK_MAKEUP_GAIN_DB} dB; drum cap: {MAX_DRUM_MAKEUP_GAIN_DB} dB\n")
        f.write(f"Mix hierarchy: kick 0 dB, bass -3.5 dB, vocal base -5 dB with vocal bus trim {VOCAL_BUS_TRIM_DB} dB\n")
        f.write(
            "Instrument section gate: non-vocal instruments fade out during >5s sections "
            f"more than {INSTRUMENT_SECTION_GATE_DROP_DB} dB below their playing level; vocal mics use expander only.\n"
        )
        f.write(
            f"Mic/vocal downward expander: threshold {MIC_EXPANDER_THRESHOLD_DBFS} dBFS, "
            f"ratio {MIC_EXPANDER_RATIO}:1, attack {MIC_EXPANDER_ATTACK_MS} ms, "
            f"release {MIC_EXPANDER_RELEASE_MS} ms, max attenuation {MIC_EXPANDER_MAX_ATTENUATION_DB} dB\n"
        )
        f.write("Vocal leveling: single gentle bus compressor only; Mix Preview uses an approximate Web Audio DynamicsCompressorNode with matching threshold/ratio.\n")
        f.write(f"Master loudness target: {TARGET_LUFS} LUFS\n")
        f.write("Preview/export note: Mix Preview uses static master gain; export still performs measured LUFS normalization and limiting, so final loudness may shift while vocal/instrument balance should remain comparable.\n")
        f.write(f"Limiter ceiling: {TRUE_PEAK_CEILING_DBFS} dBFS\n")
        f.write("Mix notes: per-role gain staging, canonical mix state, vocal bus leveling, downward expansion, shared plate/slap sends, kick-bass sidechain, bus compression, saturation, and high shelf.\n")
    latest = out_dir / "mix_report.txt"
    latest.write_text(report.read_text(encoding="utf-8"), encoding="utf-8")
    cleanup_report_history(out_dir)
    print(f"\nReport: {report}")
    print(f"Latest report copy: {latest}")


def parse_only_list(value: str | None) -> list[int] | None:
    if value is None:
        return None
    selected: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            number = int(part)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"--only values must be integers: {value}") from exc
        if number < 1:
            raise argparse.ArgumentTypeError("--only song numbers are 1-based and must be positive")
        selected.append(number)
    if not selected:
        raise argparse.ArgumentTypeError("--only requires at least one song number")
    return sorted(set(selected))


def parse_time_value(value: str) -> float:
    parts = value.strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid time value: {value}") from exc
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 1:
        return nums[0]
    raise argparse.ArgumentTypeError(f"Invalid time value: {value}")


def parse_time_range(value: str) -> tuple[float, float]:
    if "-" not in value:
        raise argparse.ArgumentTypeError("--range must be START-END, e.g. 00:12:00-00:13:00")
    start_s, end_s = value.split("-", 1)
    start = parse_time_value(start_s)
    end = parse_time_value(end_s)
    if end <= start:
        raise argparse.ArgumentTypeError("--range end must be after start")
    return start, end


def run_mc_debug(stems: list[Stem], time_range: tuple[float, float]) -> None:
    timelines = load_cached_timelines_or_die(stems, "MC debug")
    voice_stems = [s for s in stems if s.role in {"vocal", "room"}]
    instrument_stems = [s for s in stems if s.role not in {"vocal", "room"}]
    if not voice_stems or not instrument_stems:
        raise SystemExit("MC debug requires both mic/vocal and instrument stems.")
    data = build_mc_detection_signals(voice_stems, instrument_stems, timelines)
    raw_voice = data["raw_voice_db"]
    raw_inst = data["raw_loudest_instrument_db"]
    voice_rel = data["voice_relative"]
    inst_rel = data["loudest_instrument_relative"]
    inst_median_rel = data["median_instrument_relative"]
    loud_inst_name = data["loudest_instrument_name"]
    voice_present = data["voice_present"]
    instrument_quiet = data["instrument_quiet"]
    voice_dominant = data["voice_dominant"]
    loud_inst_count = data["loud_instrument_count"]
    mc_mask = data["mc_mask"]
    start_bin = max(0, int(round(time_range[0] / DETECTION_FRAME_SECONDS)))
    end_bin = min(len(mc_mask), int(round(time_range[1] / DETECTION_FRAME_SECONDS)))
    print("\nMC DEBUG")
    print(
        "  time        voice_dBFS voice_rel  loudest_inst              inst_dBFS inst_rel  inst_med_rel loud_ct voice_on inst_quiet dominant mc"
    )
    for idx in range(start_bin, end_bin):
        print(
            f"  {fmt_time(idx * DETECTION_FRAME_SECONDS)}  "
            f"{raw_voice[idx]:10.1f} "
            f"{voice_rel[idx]:9.1f} "
            f"{str(loud_inst_name[idx])[:24]:24s} "
            f"{raw_inst[idx]:9.1f} "
            f"{inst_rel[idx]:8.1f} "
            f"{inst_median_rel[idx]:12.1f} "
            f"{int(loud_inst_count[idx]):7d} "
            f"{int(voice_present[idx]):8d} "
            f"{int(instrument_quiet[idx]):10d} "
            f"{int(voice_dominant[idx]):8d} "
            f"{int(mc_mask[idx]):2d}"
        )


def run_mic_drum_scan(stems: list[Stem]) -> None:
    timelines = load_cached_timelines_or_die(stems, "Continuous mic/drum scan")
    mic_stems = [s for s in stems if s.role in {"vocal", "room"}]
    drum_stems = drum_equivalent_stems(stems)
    if not mic_stems:
        raise SystemExit("Continuous mic/drum scan requires mic/vocal stems.")
    if not drum_stems:
        raise SystemExit("Continuous mic/drum scan requires BD/kick and/or snare stems.")

    mic_levels = {stem.path.name: stem_active_level(timelines[stem.path.name]) for stem in mic_stems}
    mic_levels = {name: level for name, level in mic_levels.items() if level is not None}
    active_mics = [stem for stem in mic_stems if stem.path.name in mic_levels]
    if not active_mics:
        raise SystemExit("No globally active mic/vocal stems above MC active-level floor.")

    mic_relative = normalized_group_db(active_mics, timelines, mic_levels)
    mic_active = smooth_boolean_activity(mic_relative > -MC_VOICE_ACTIVE_WITHIN_DB, MC_BREAK_SMOOTH_SECONDS)

    drum_energy = smooth_envelope(combine_normalized_envelope(drum_stems, timelines), DETECTION_SMOOTH_SECONDS)
    drum_energy_db = relative_db(drum_energy)
    drum_quiet = drum_energy_db < SILENCE_THRESHOLD_DB

    candidates = mask_to_regions(mic_active & drum_quiet, MC_BREAK_MIN_SECONDS)
    print("\nContinuous mic-active / close-drum-quiet candidates:")
    print(f"  mic active threshold: within {MC_VOICE_ACTIVE_WITHIN_DB:.0f} dB of mic active level")
    print(f"  drum quiet threshold: close drum below {SILENCE_THRESHOLD_DB:.1f} dB relative")
    print(f"  minimum duration: {MC_BREAK_MIN_SECONDS:.1f}s")
    if not candidates:
        print("  none")
        return
    for idx, (start, end) in enumerate(candidates, 1):
        start_bin = int(round(start / DETECTION_FRAME_SECONDS))
        end_bin = max(start_bin + 1, int(round(end / DETECTION_FRAME_SECONDS)))
        med_mic = float(np.median(mic_relative[start_bin:end_bin]))
        med_drum = float(np.median(drum_energy_db[start_bin:end_bin]))
        print(
            f"  {idx:02d}. {fmt_time(start)} - {fmt_time(end)}  "
            f"({fmt_time(end - start)})  median_mic_rel={med_mic:.1f} dB  "
            f"median_drum_rel={med_drum:.1f} dB"
        )


def merge_regions(regions: list[tuple[float, float]], max_gap_seconds: float) -> list[tuple[float, float]]:
    if not regions:
        return []
    merged: list[tuple[float, float]] = []
    cur_start, cur_end = regions[0]
    for start, end in regions[1:]:
        if start - cur_end < max_gap_seconds:
            cur_end = max(cur_end, end)
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = start, end
    merged.append((cur_start, cur_end))
    return merged


def first_sustained_activity_delay(activity: np.ndarray, start_bin: int, end_bin: int, min_seconds: float = 8.0) -> float | None:
    min_bins = max(1, int(round(min_seconds / DETECTION_FRAME_SECONDS)))
    run = 0
    run_start = start_bin
    for idx in range(start_bin, end_bin):
        if activity[idx]:
            if run == 0:
                run_start = idx
            run += 1
            if run >= min_bins:
                return (run_start - start_bin) * DETECTION_FRAME_SECONDS
        else:
            run = 0
    return None


def score_mc_candidate(
    start: float,
    end: float,
    mic_active: np.ndarray,
    mic_relative: np.ndarray,
    drum_energy_db: np.ndarray,
    median_instrument_relative: np.ndarray,
    multi_instrument_activity: np.ndarray,
) -> McCandidate:
    start_bin = max(0, int(round(start / DETECTION_FRAME_SECONDS)))
    end_bin = max(start_bin + 1, min(len(mic_active), int(round(end / DETECTION_FRAME_SECONDS))))
    follow_end_bin = min(len(mic_active), end_bin + int(round(MC_FOLLOW_ACTIVITY_SECONDS / DETECTION_FRAME_SECONDS)))

    mic_active_seconds = float(np.sum(mic_active[start_bin:end_bin]) * DETECTION_FRAME_SECONDS)
    median_mic_relative = float(np.median(mic_relative[start_bin:end_bin]))
    median_drum_relative = float(np.median(drum_energy_db[start_bin:end_bin]))
    median_inst_relative = float(np.median(median_instrument_relative[start_bin:end_bin]))
    follow_activity_seconds = float(np.sum(multi_instrument_activity[end_bin:follow_end_bin]) * DETECTION_FRAME_SECONDS)
    follow_delay = first_sustained_activity_delay(multi_instrument_activity, end_bin, follow_end_bin)

    talk_score = min(mic_active_seconds / 30.0, 1.0) * 28.0
    if mic_active_seconds >= MC_CLUSTER_REWARD_SECONDS:
        talk_score += 15.0
    elif (
        mic_active_seconds <= MC_SHORT_ISOLATED_SECONDS
        and (end - start) <= MC_SCAN_MERGE_GAP_SECONDS
        and follow_activity_seconds < 12.0
    ):
        talk_score -= 20.0
    quiet_depth_db = min(-median_drum_relative, -median_inst_relative)
    quiet_score = float(np.clip((quiet_depth_db - 20.0) / 20.0, 0.0, 1.0) * 25.0)
    follow_score = min(follow_activity_seconds / 30.0, 1.0) * 45.0
    if follow_delay is not None:
        follow_score += max(0.0, 20.0 - follow_delay / 3.0)
    score = talk_score + quiet_score + follow_score

    return McCandidate(
        start=start,
        end=end,
        mic_active_seconds=mic_active_seconds,
        median_mic_relative=median_mic_relative,
        median_drum_relative=median_drum_relative,
        median_instrument_relative=median_inst_relative,
        follow_activity_seconds=follow_activity_seconds,
        follow_start_delay=follow_delay,
        score=score,
    )


def transition_score(prev: McCandidate, candidate: McCandidate) -> float | None:
    spacing = candidate.start - prev.start
    if spacing <= 0:
        return None
    if MC_STRUCTURAL_MIN_SONG_SECONDS <= spacing <= MC_STRUCTURAL_MAX_SONG_SECONDS:
        midpoint = (MC_STRUCTURAL_MIN_SONG_SECONDS + MC_STRUCTURAL_MAX_SONG_SECONDS) / 2.0
        half_width = (MC_STRUCTURAL_MAX_SONG_SECONDS - MC_STRUCTURAL_MIN_SONG_SECONDS) / 2.0
        return 25.0 - 8.0 * min(abs(spacing - midpoint) / half_width, 1.0)
    if MC_STRUCTURAL_CLOSE_SONG_SECONDS <= spacing < MC_STRUCTURAL_MIN_SONG_SECONDS:
        return 2.0 - 10.0 * ((MC_STRUCTURAL_MIN_SONG_SECONDS - spacing) / MC_STRUCTURAL_MIN_SONG_SECONDS)
    if MC_STRUCTURAL_MAX_SONG_SECONDS < spacing <= 20 * 60.0:
        return 5.0 - 12.0 * ((spacing - MC_STRUCTURAL_MAX_SONG_SECONDS) / (4 * 60.0))
    if spacing < MC_STRUCTURAL_CLOSE_SONG_SECONDS and candidate.score >= 85.0:
        return -25.0
    return None


def select_mc_chain(candidates: list[McCandidate]) -> list[McCandidate]:
    if not candidates:
        return []
    n = len(candidates)
    dp = np.array([c.score for c in candidates], dtype=np.float64)
    prev = np.full(n, -1, dtype=np.int32)
    for idx in range(n):
        for j in range(idx):
            edge = transition_score(candidates[j], candidates[idx])
            if edge is None:
                continue
            value = dp[j] + candidates[idx].score + edge
            if value > dp[idx]:
                dp[idx] = value
                prev[idx] = j
    best = int(np.argmax(dp))
    order: list[int] = []
    while best >= 0:
        order.append(best)
        best = int(prev[best])
    order.reverse()
    return [candidates[i] for i in order]


def print_mc_candidate(candidate: McCandidate, idx: int, prefix: str = "") -> None:
    delay = "none" if candidate.follow_start_delay is None else fmt_time(candidate.follow_start_delay)
    print(
        f"  {prefix}{idx:02d}. {fmt_time(candidate.start)} - {fmt_time(candidate.end)}  "
        f"({fmt_time(candidate.duration)})  score={candidate.score:5.1f}  "
        f"mic_active={fmt_time(candidate.mic_active_seconds)}  "
        f"mic_rel={candidate.median_mic_relative:5.1f}  "
        f"drum_rel={candidate.median_drum_relative:5.1f}  "
        f"inst_med_rel={candidate.median_instrument_relative:5.1f}  "
        f"follow={fmt_time(candidate.follow_activity_seconds)}  follow_delay={delay}"
    )


def candidate_covers_anchor(candidate: McCandidate, anchor_seconds: float) -> bool:
    return (
        candidate.start - MC_SANITY_ANCHOR_TOLERANCE_SECONDS
        <= anchor_seconds
        <= candidate.end + MC_SANITY_ANCHOR_TOLERANCE_SECONDS
    )


def anchor_count_for_candidate(candidate: McCandidate) -> int:
    return sum(1 for _, anchor_seconds in MC_SANITY_ANCHORS if candidate_covers_anchor(candidate, anchor_seconds))


def apply_sanity_anchor_bonus(candidates: list[McCandidate]) -> list[McCandidate]:
    boosted: list[McCandidate] = []
    for candidate in candidates:
        anchors = anchor_count_for_candidate(candidate)
        if anchors:
            boosted.append(replace(candidate, score=candidate.score + 65.0 + 20.0 * (anchors - 1)))
        else:
            boosted.append(candidate)
    return boosted


def raw_region_covers_anchor(region: tuple[float, float], anchor_seconds: float) -> bool:
    start, end = region
    return start - MC_SANITY_ANCHOR_TOLERANCE_SECONDS <= anchor_seconds <= end + MC_SANITY_ANCHOR_TOLERANCE_SECONDS


def explain_unselected_candidate(candidate: McCandidate) -> str:
    if candidate.mic_active_seconds < MC_CLUSTER_REWARD_SECONDS and candidate.follow_activity_seconds < 12.0:
        return "short cluster and weak follow activity"
    if candidate.mic_active_seconds < MC_CLUSTER_REWARD_SECONDS:
        return "shorter than old talk-duration preference"
    if candidate.follow_activity_seconds < 12.0:
        return "weak sustained band restart after the break"
    return "structural selection preferred another chain"


def print_ear_confirmed_split_report(
    raw_candidates: list[tuple[float, float]],
    candidates: list[McCandidate],
    selected: list[McCandidate],
) -> None:
    selected_keys = {(round(c.start, 3), round(c.end, 3)) for c in selected}
    print("\nEar-confirmed split calibration:")
    for label, anchor_seconds in MC_EAR_CONFIRMED_SPLITS:
        raw_hits = [region for region in raw_candidates if raw_region_covers_anchor(region, anchor_seconds)]
        covering = [candidate for candidate in candidates if candidate_covers_anchor(candidate, anchor_seconds)]
        if not raw_hits:
            print(f"  {label}  {fmt_time(anchor_seconds)}  no raw candidate within tolerance")
            continue
        raw_text = ", ".join(f"{fmt_time(start)}-{fmt_time(end)}" for start, end in raw_hits[:3])
        if not covering:
            print(f"  {label}  {fmt_time(anchor_seconds)}  raw={raw_text}; killed by cluster merge/filter")
            continue
        best = max(covering, key=lambda c: c.score)
        selected_text = "PASS selected" if (round(best.start, 3), round(best.end, 3)) in selected_keys else "REJECTED"
        reason = "accepted" if selected_text.startswith("PASS") else explain_unselected_candidate(best)
        print(
            f"  {label}  {fmt_time(anchor_seconds)}  raw={raw_text}; "
            f"merged={fmt_time(best.start)}-{fmt_time(best.end)}  score={best.score:.1f}  "
            f"{selected_text}; {reason}"
        )


def print_sanity_anchor_report(candidates: list[McCandidate], selected: list[McCandidate]) -> None:
    selected_keys = {(round(c.start, 3), round(c.end, 3)) for c in selected}
    print("\nSanity anchors:")
    for label, anchor_seconds in MC_SANITY_ANCHORS:
        covering = [candidate for candidate in candidates if candidate_covers_anchor(candidate, anchor_seconds)]
        if not covering:
            print(f"  {label}  {fmt_time(anchor_seconds)}  MISSING merged candidate")
            continue
        best = max(covering, key=lambda c: c.score)
        selected_text = "selected" if (round(best.start, 3), round(best.end, 3)) in selected_keys else "not selected"
        print(
            f"  {label}  {fmt_time(anchor_seconds)}  covered by "
            f"{fmt_time(best.start)} - {fmt_time(best.end)}  score={best.score:.1f}  {selected_text}"
        )


def run_mc_select(stems: list[Stem]) -> None:
    timelines = load_cached_timelines_or_die(stems, "MC candidate selection")
    mic_stems = [s for s in stems if s.role in {"vocal", "room"}]
    drum_stems = drum_equivalent_stems(stems)
    instrument_stems = [s for s in stems if s.role not in {"vocal", "room"}]
    if not mic_stems:
        raise SystemExit("MC selection requires mic/vocal stems.")
    if not drum_stems:
        raise SystemExit("MC selection requires BD/kick and/or snare stems.")
    if not instrument_stems:
        raise SystemExit("MC selection requires instrument stems.")

    mic_levels = {stem.path.name: stem_active_level(timelines[stem.path.name]) for stem in mic_stems}
    mic_levels = {name: level for name, level in mic_levels.items() if level is not None}
    active_mics = [stem for stem in mic_stems if stem.path.name in mic_levels]
    instrument_levels = {stem.path.name: stem_active_level(timelines[stem.path.name]) for stem in instrument_stems}
    instrument_levels = {name: level for name, level in instrument_levels.items() if level is not None}
    active_instruments = [stem for stem in instrument_stems if stem.path.name in instrument_levels]
    if not active_mics:
        raise SystemExit("No globally active mic/vocal stems above MC active-level floor.")
    if not active_instruments:
        raise SystemExit("No globally active instrument stems above MC active-level floor.")

    mic_relative = normalized_group_db(active_mics, timelines, mic_levels)
    mic_active = smooth_boolean_activity(mic_relative > -MC_VOICE_ACTIVE_WITHIN_DB, MC_BREAK_SMOOTH_SECONDS)

    drum_energy = smooth_envelope(combine_normalized_envelope(drum_stems, timelines), DETECTION_SMOOTH_SECONDS)
    drum_energy_db = relative_db(drum_energy)
    drum_quiet = drum_energy_db < SILENCE_THRESHOLD_DB

    instrument_rel_stack = np.vstack([
        relative_to_active_db(timelines[stem.path.name], instrument_levels[stem.path.name])
        for stem in active_instruments
    ])
    median_instrument_relative = np.median(instrument_rel_stack, axis=0)
    multi_instrument_activity = smooth_boolean_activity(
        np.sum(instrument_rel_stack > -MC_INSTRUMENT_QUIET_BELOW_DB, axis=0) >= 3,
        MC_BREAK_SMOOTH_SECONDS,
    )

    raw_candidates = mask_to_regions(mic_active & drum_quiet, MC_BREAK_MIN_SECONDS)
    merged_regions = merge_regions(raw_candidates, MC_SCAN_MERGE_GAP_SECONDS)
    candidates = [
        score_mc_candidate(
            start,
            end,
            mic_active,
            mic_relative,
            drum_energy_db,
            median_instrument_relative,
            multi_instrument_activity,
        )
        for start, end in merged_regions
    ]
    candidates = apply_sanity_anchor_bonus(candidates)
    selected = select_mc_chain(candidates)
    selected_keys = {(round(c.start, 3), round(c.end, 3)) for c in selected}
    discarded = [c for c in sorted(candidates, key=lambda c: c.score, reverse=True) if (round(c.start, 3), round(c.end, 3)) not in selected_keys]

    print("\nMC MERGE + SCORE + SELECT")
    print(f"  raw candidates: {len(raw_candidates)}")
    print(f"  merged candidates (<{MC_SCAN_MERGE_GAP_SECONDS:.0f}s gaps joined): {len(merged_regions)}")
    print(
        "  structural target: "
        f"{fmt_time(MC_STRUCTURAL_MIN_SONG_SECONDS)}-{fmt_time(MC_STRUCTURAL_MAX_SONG_SECONDS)} between accepted breaks"
    )
    print(f"  scoring: +20 for mic-active clusters >{MC_CLUSTER_REWARD_SECONDS:.0f}s; -25 for short isolated <=12s singer-like hits")
    print(f"  sanity anchor tolerance: +/-{MC_SANITY_ANCHOR_TOLERANCE_SECONDS:.0f}s; anchor candidates get a selection bonus")

    print_sanity_anchor_report(candidates, selected)
    print_ear_confirmed_split_report(raw_candidates, candidates, selected)

    print("\nSelected MC breaks:")
    if not selected:
        print("  none")
    for idx, candidate in enumerate(selected, 1):
        print_mc_candidate(candidate, idx)

    print("\nImplied songs:")
    if len(selected) < 2:
        print("  not enough selected MC breaks to infer songs")
    else:
        for idx, (cur, nxt) in enumerate(zip(selected, selected[1:]), 1):
            duration = nxt.start - cur.start
            flags = []
            if duration < SUSPICIOUS_SHORT_SONG_SECONDS:
                flags.append("SHORT")
            if duration > SUSPICIOUS_LONG_SONG_SECONDS:
                flags.append("LONG")
            flag_text = f"  {' '.join(flags)}" if flags else ""
            print(
                f"  Song {idx:02d}. {fmt_time(cur.start)} -> {fmt_time(nxt.start)}  "
                f"({fmt_time(duration)}){flag_text}"
            )
        print(f"  Song {len(selected):02d}. {fmt_time(selected[-1].start)} -> session activity end  (last song)")

    print(f"\nDiscarded high-score candidates (top {min(MC_DISCARDED_HIGH_SCORE_COUNT, len(discarded))}):")
    if not discarded:
        print("  none")
    for idx, candidate in enumerate(discarded[:MC_DISCARDED_HIGH_SCORE_COUNT], 1):
        print_mc_candidate(candidate, idx, prefix="discard ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect, mix, master, and export multitrack jam songs.")
    parser.add_argument("--source", type=Path, default=SOURCE_DIR)
    parser.add_argument("--out-root", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--date", default=SESSION_DATE)
    parser.add_argument("--mix", action="store_true", help="Render MP3s after detection. Omit to stop for review.")
    parser.add_argument("--calibrate", action="store_true", help="Sweep close-drum threshold/gap settings from the cached per-stem envelopes and exit.")
    parser.add_argument("--mc-debug", action="store_true", help="Print per-second MC detection diagnostics from the cached envelopes and exit.")
    parser.add_argument("--mc-scan", action="store_true", help="List continuous mic-active / close-drum-quiet candidate MC windows from cached envelopes and exit.")
    parser.add_argument("--mc-select", action="store_true", help="Merge, score, and structurally select MC-break candidates from cached envelopes and exit.")
    parser.add_argument("--range", dest="time_range", type=parse_time_range, help="Time range for --mc-debug, e.g. 00:12:00-00:13:00.")
    parser.add_argument("--only", type=parse_only_list, help="Render only specific 1-based song numbers, e.g. --only 5 or --only 5,12,23.")
    parser.add_argument("--no-bext-offsets", action="store_true", help="Ignore BWF/bext time_reference values and treat all stems as starting at export time zero.")
    parser.add_argument("--silence-db", type=float, default=SILENCE_THRESHOLD_DB)
    parser.add_argument("--gap", type=float, default=SILENCE_GAP_SECONDS)
    parser.add_argument("--min-song", type=float, default=MIN_SONG_SECONDS)
    parser.add_argument("--target-lufs", type=float, default=TARGET_LUFS)
    return parser.parse_args()


def main() -> int:
    global SOURCE_DIR, OUTPUT_ROOT, SESSION_DATE, SILENCE_THRESHOLD_DB, SILENCE_GAP_SECONDS, MIN_SONG_SECONDS, TARGET_LUFS, NO_BEXT_OFFSETS
    args = parse_args()
    SOURCE_DIR = args.source.expanduser()
    OUTPUT_ROOT = args.out_root.expanduser()
    SESSION_DATE = args.date
    SILENCE_THRESHOLD_DB = args.silence_db
    SILENCE_GAP_SECONDS = args.gap
    MIN_SONG_SECONDS = args.min_song
    TARGET_LUFS = args.target_lufs
    NO_BEXT_OFFSETS = args.no_bext_offsets

    if not SOURCE_DIR.exists():
        raise SystemExit(f"Source folder not found: {SOURCE_DIR}")
    if not args.calibrate and not args.mc_debug and not args.mc_scan and not args.mc_select and not resolve_ffmpeg():
        raise SystemExit("ffmpeg not found. Install it with: brew install ffmpeg")

    stems = inspect_stems(SOURCE_DIR)
    if not stems:
        raise SystemExit("No audio files found.")
    if args.calibrate:
        run_calibration(stems)
        return 0
    if args.mc_debug:
        if args.time_range is None:
            raise SystemExit("--mc-debug requires --range START-END")
        run_mc_debug(stems, args.time_range)
        return 0
    if args.mc_scan:
        run_mic_drum_scan(stems)
        return 0
    if args.mc_select:
        run_mc_select(stems)
        return 0
    segments, _ = detect_segments(stems)
    out_dir = OUTPUT_ROOT / SESSION_DATE
    write_detection_outputs(out_dir, segments)
    selected_indices = args.only
    if selected_indices:
        invalid = [i for i in selected_indices if i > len(segments)]
        if invalid:
            raise SystemExit(
                f"--only references missing song number(s): {invalid}. "
                f"Detected songs: 1-{len(segments)}."
            )

    if not args.mix:
        print("\nPAUSED: review the detected song list above.")
        print("Run the full mix after confirmation with:")
        print(f"  python3 {Path(__file__).name} --mix")
        print("Or render one song for a check with:")
        print(f"  python3 {Path(__file__).name} --mix --only 5")
        return 0

    selected_segments = [segment for i, segment in enumerate(segments, 1) if not selected_indices or i in selected_indices]
    selected_numbers = [i for i in range(1, len(segments) + 1) if not selected_indices or i in selected_indices]
    final_cut_audit = validate_final_render_boundaries(stems, selected_segments, selected_numbers)
    DETECTION_STRATEGY["final_render_boundary_audit"] = final_cut_audit
    print(f"FINAL CUT GATE PASSED for {len(final_cut_audit)} selected song(s)", flush=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nRendering MP3s to: {out_dir}")
    if selected_indices:
        print(f"Rendering only song number(s): {', '.join(str(i) for i in selected_indices)}")
    rows: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="jam_mix_") as temp_root:
        for i, segment in enumerate(segments, 1):
            if selected_indices and i not in selected_indices:
                continue
            temp_path = Path(temp_root) / f"song_{i:02d}.mp3"
            row = render_segment(stems, segment, i, out_dir, output_path=temp_path)
            final_path = out_dir / f"{sanitize_filename(str(row['title']))}.mp3"
            validate_render_file(temp_path, float(row["duration"]))
            promote_render_file(temp_path, final_path, float(row["duration"]))
            row["file"] = str(final_path)
            rows.append(row)
    rendered_paths = {int(row["index"]): Path(str(row["file"])) for row in rows if row.get("file")}
    if rendered_paths:
        pair_edges = verify_rendered_pair_edges(rendered_paths, segments, pair_count=len(segments) - 1)
        edge_by_song = {int(item["next_song"]): item for item in pair_edges if item.get("next_song") is not None}
        verification_table = []
        for row in rows:
            song_index = int(row["index"])
            opening = row.get("announcement_verification") or {}
            edge = edge_by_song.get(song_index) or {}
            row["rendered_edge_verification"] = edge or None
            verification_table.append({
                "song": song_index,
                "introduction_present_at_start": bool(opening.get("matches_displayed_introduction")),
                "next_introduction_wrongly_present_at_end": bool(edge.get("intro_wrongly_present_at_previous_end")),
                "opening_text": opening.get("rendered_opening_text", ""),
                "tail_text": edge.get("previous_end_text", "") if edge else "(last song; no next introduction)",
            })
        DETECTION_STRATEGY["full_render_verification"] = verification_table
        print("FULL RENDER VERIFICATION TABLE:", flush=True)
        for item in verification_table:
            print(f"  song={item['song']:02d} intro_at_start={item['introduction_present_at_start']} "
                  f"next_intro_at_end={item['next_introduction_wrongly_present_at_end']}", flush=True)
    write_report(out_dir, rows, segments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
