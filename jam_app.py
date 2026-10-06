#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import mimetypes
import os
import queue
import re
try:
    import resource
except ImportError:  # Windows has no POSIX resource module.
    resource = None
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
import signal
import struct
from functools import wraps
from scipy import signal as audio_signal
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from flask import Flask, Response, g, jsonify, render_template, request, send_file
from werkzeug.serving import make_server

import jam_mix_pipeline as pipeline


source_edit_lock = threading.RLock()


def source_edit_locked(function):
    @wraps(function)
    def serialized(*args, **kwargs):
        with source_edit_lock:
            return function(*args, **kwargs)
    return serialized


PROJECT_ROOT = Path(__file__).resolve().parent
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
STATE_ROOT = Path(os.environ.get("ZUCKER_MIXER_STATE_ROOT", str(Path.home() / "Music" / "JamMixes" / "ZuckerMixerState"))).expanduser().resolve()
STATE_ROOT.mkdir(parents=True, exist_ok=True)
ACTIVE_SOURCE_STATE_ROOT = STATE_ROOT / "sources" / "default"
ACTIVE_SOURCE_STATE_ROOT.mkdir(parents=True, exist_ok=True)
OVERRIDES_PATH = ACTIVE_SOURCE_STATE_ROOT / "mix_overrides.json"
HISTORY_PATH = ACTIVE_SOURCE_STATE_ROOT / "render_history.json"
SETTINGS_PATH = STATE_ROOT / "app_settings.json"
MANUAL_SPLITS_PATH = ACTIVE_SOURCE_STATE_ROOT / "manual_splits.json"
SEGMENT_SELECTIONS_PATH = ACTIVE_SOURCE_STATE_ROOT / "segment_selections.json"
WAVEFORM_CACHE_PATH = ACTIVE_SOURCE_STATE_ROOT / "waveform_cache.json"
MIX_PLANS_PATH = ACTIVE_SOURCE_STATE_ROOT / "mix_plans.json"
SONG_NAMES_PATH = ACTIVE_SOURCE_STATE_ROOT / "song_names.json"
DETECTION_STATE_PATH = ACTIVE_SOURCE_STATE_ROOT / "detection_state.json"
EDITOR_HISTORY_PATH = ACTIVE_SOURCE_STATE_ROOT / "editor_history.json"
MANUAL_EDITOR_STATE_PATH = ACTIVE_SOURCE_STATE_ROOT / "manual_editor_state.json"
REDETECTION_CANDIDATE_PATH = ACTIVE_SOURCE_STATE_ROOT / "redetect_candidate.json"
REDETECTION_BACKUP_PATH = ACTIVE_SOURCE_STATE_ROOT / "redetect_backup.json"
SECOND_PASS_CANDIDATE_PATH = ACTIVE_SOURCE_STATE_ROOT / "whisper_second_pass_candidate.json"
PREVIEW_CACHE_ROOT = Path(tempfile.gettempdir()) / "ZuckerMixerPreviewCache"
PREVIEW_DIR = PREVIEW_CACHE_ROOT / "default"
SOURCE_CONFIG_PATH = ACTIVE_SOURCE_STATE_ROOT / "session_config.json"
SKIPPED_SEGMENTS_PATH = ACTIVE_SOURCE_STATE_ROOT / "skipped_segments.json"
CUT_AUDIO_CACHE_ROOT = Path(tempfile.gettempdir()) / "ZuckerMixerCutAudioCache"
JOB_STATUS_DIR = STATE_ROOT / "job_status"
JOB_STATUS_DIR.mkdir(parents=True, exist_ok=True)
LIFECYCLE_LOG_PATH = STATE_ROOT / "redetect_worker_lifecycle.jsonl"
RENDER_DIAGNOSTICS_ROOT = STATE_ROOT / "render_diagnostics"
RENDER_DIAGNOSTICS_ROOT.mkdir(parents=True, exist_ok=True)
ORPHANED_ACTIVE_JOB_SECONDS = 90.0
ACTIVE_STEM_DETECTION_VERSION = 5
BUILD_METADATA_PATH = RESOURCE_ROOT / "build" / "build_metadata.json"
HOST = "127.0.0.1"
DEFAULT_TARGET_LUFS = pipeline.TARGET_LUFS
MIN_RENDER_DURATION_SECONDS = 480.0
MAX_RENDER_DURATION_SECONDS = 780.0

pipeline.SOURCE_DIR = Path.home() / "Music" / "JamStems"
pipeline.OUTPUT_ROOT = Path.home() / "Music" / "JamMixes"
pipeline.LEGACY_DETECTION_CACHE = STATE_ROOT / "jam_detection_envelopes.npz"
pipeline.configure_detection_cache(pipeline.SOURCE_DIR, STATE_ROOT)
pipeline.TRANSCODE_CACHE_ROOT = STATE_ROOT / "audio_cache"

app = Flask(
    __name__,
    static_folder=str(RESOURCE_ROOT / "static"),
    template_folder=str(RESOURCE_ROOT / "templates"),
)

state_lock = threading.RLock()
pipeline_state_build_lock = threading.Lock()
job_queue: queue.Queue[dict[str, Any]] = queue.Queue()
jobs: list[dict[str, Any]] = []
log_lines: list[dict[str, Any]] = []
pipeline_state: dict[str, Any] | None = None
pipeline_state_signature: tuple[float | None, tuple[float, ...]] | None = None
last_load_error: str = ""
cancel_requested = False
active_job_by_id: dict[str, dict[str, Any]] = {}
server_ref: Any | None = None
child_processes: dict[str, subprocess.Popen[str]] = {}
child_status_path: Path | None = None
child_status_snapshot: dict[str, Any] = {}


def lifecycle_log(event: str, job_id: str | None = None, **fields: Any) -> None:
    """Append a process-lifecycle record that survives child termination."""
    record = {
        "ts": time.time(),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "event": event,
        **({"job_id": str(job_id)} if job_id else {}),
        **fields,
    }
    try:
        LIFECYCLE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LIFECYCLE_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception as exc:
        print("LIFECYCLE_LOG_FAILED " + json.dumps({"event": event, "error": str(exc)}), flush=True)


def _remove_file(path: Path, cleaned: dict[str, int]) -> None:
    try:
        size = path.stat().st_size
        path.unlink()
        cleaned["files"] += 1
        cleaned["bytes"] += size
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"STARTUP_CLEANUP_FAILED path={path} error={exc}", flush=True)


def startup_hygiene() -> None:
    """Quietly remove app-owned diagnostic/cache buildup without touching user audio."""
    now = time.time()
    cleaned = {"files": 0, "bytes": 0}

    # Keep the newest status/job records. These are diagnostic and are never
    # needed to reconstruct source, override, naming, or render data.
    for directory, patterns, keep_count in (
        (JOB_STATUS_DIR, ("*.json",), 20),
        (STATE_ROOT, ("job_*.json",), 20),
    ):
        files = sorted(
            (p for pattern in patterns for p in directory.glob(pattern) if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        keep = {p.resolve() for p in files[:keep_count]}
        for path in files[keep_count:]:
            if path.resolve() not in keep:
                _remove_file(path, cleaned)

    # Worker stderr is diagnostic only. Keep it aligned with the retained job
    # ids, so it cannot grow once the status files are bounded.
    retained_ids = {p.stem for p in JOB_STATUS_DIR.glob("*.json") if p.is_file()}
    for path in STATE_ROOT.glob("worker_*.stderr.log"):
        job_id = path.name[len("worker_") : -len(".stderr.log")]
        if job_id not in retained_ids:
            _remove_file(path, cleaned)

    # Detection caches now carry their source path. Remove only caches whose
    # recorded source has disappeared; keep the current cache and legacy files
    # without metadata for explicit review.
    for path in STATE_ROOT.glob("*.npz"):
        try:
            with np.load(path, allow_pickle=False) as data:
                source_dir = str(data["source_dir"]) if "source_dir" in data.files else ""
            if source_dir and not Path(source_dir).expanduser().exists():
                _remove_file(path, cleaned)
        except (OSError, ValueError, KeyError):
            print(f"STARTUP_CLEANUP_SKIPPED invalid detection cache: {path}", flush=True)

    # Preview files belong in the OS temp area, never app state. Clean both the
    # new location and legacy state previews after seven days.
    preview_roots = [PREVIEW_CACHE_ROOT, STATE_ROOT / "previews"]
    preview_roots.extend(STATE_ROOT.glob("sources/*/previews"))
    cutoff = now - 7 * 24 * 60 * 60
    for root in preview_roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                _remove_file(path, cleaned)

    # Remove only app-named abandoned staging files from temp and the legacy
    # output staging location. TemporaryDirectory handles the normal case.
    temp_roots = [Path(tempfile.gettempdir()), STATE_ROOT, pipeline.OUTPUT_ROOT]
    temp_names = (".zucker_render_*", ".zucker_preview_*", ".zucker_whisper_*", ".zucker_render_verify_*", ".zucker_edge_verify_*", ".zucker_*partial")
    for root in temp_roots:
        if not root.exists():
            continue
        for pattern in temp_names:
            for path in root.glob(pattern):
                if path.exists() and path.stat().st_mtime < now - 24 * 60 * 60:
                    if path.is_dir():
                        try:
                            size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
                            shutil.rmtree(path)
                            cleaned["files"] += 1
                            cleaned["bytes"] += size
                        except OSError as exc:
                            print(f"STARTUP_CLEANUP_FAILED path={path} error={exc}", flush=True)
                    else:
                        _remove_file(path, cleaned)

    # Lifecycle output is useful while diagnosing a run, but an unbounded
    # append-only log defeats the rest of the hygiene policy. Keep it small by
    # resetting only after it exceeds the diagnostic budget.
    try:
        lifecycle_size = LIFECYCLE_LOG_PATH.stat().st_size
        if lifecycle_size > 10 * 1024 * 1024:
            LIFECYCLE_LOG_PATH.write_text("", encoding="utf-8")
            cleaned["files"] += 1
            cleaned["bytes"] += lifecycle_size
            print(f"Startup cleanup: reset oversized lifecycle log ({lifecycle_size / 1048576:.1f} MB)", flush=True)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"STARTUP_CLEANUP_FAILED path={LIFECYCLE_LOG_PATH} error={exc}", flush=True)

    if cleaned["files"]:
        message = f"Startup cleanup: removed {cleaned['files']} app-owned files ({cleaned['bytes'] / 1048576:.1f} MB)"
        print(message, flush=True)
        lifecycle_log("startup_hygiene", files=cleaned["files"], bytes=cleaned["bytes"])
    else:
        print("Startup cleanup: nothing to remove", flush=True)


class JobLog(io.TextIOBase):
    def __init__(self, job_id: str):
        self.job_id = job_id
        self._buf = ""

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            append_log(self.job_id, line)
        return len(text)

    def flush(self) -> None:
        if self._buf:
            append_log(self.job_id, self._buf)
            self._buf = ""


def append_log(job_id: str, line: str) -> None:
    if not line.strip():
        return
    if child_status_path is not None:
        lifecycle_log("child_output", job_id, line=line.rstrip())
    with state_lock:
        match = re.search(r"timing chunk\s+(\d+)", line)
        job = active_job_by_id.get(job_id)
        if match and job:
            total = max(int(job.get("current_total_chunks") or 1), 1)
            chunk = min(int(match.group(1)), total)
            job.update(
                {
                    "song_progress": int(chunk / total * 100),
                    "current_stage": "mixing",
                    "stage_detail": f"chunk {chunk} of {total}",
                    "heartbeat": time.time(),
                }
            )
            write_job_status(job)
        stage_match = re.search(r"stage\s+([A-Za-z0-9 _-]+)", line)
        if stage_match and job:
            stage = stage_match.group(1).strip().lower()
            job["current_stage"] = stage
            if stage == "mastering":
                job["song_progress"] = max(int(job.get("song_progress") or 0), 90)
            elif stage == "encoding":
                job["song_progress"] = max(int(job.get("song_progress") or 0), 96)
            job["stage_detail"] = stage
            job["heartbeat"] = time.time()
            write_job_status(job)
        log_lines.append({"id": len(log_lines), "job_id": job_id, "ts": time.time(), "line": line})
        del log_lines[:-2000]


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_build_metadata() -> dict[str, str]:
    payload = load_json(BUILD_METADATA_PATH, {})
    if not isinstance(payload, dict):
        payload = {}
    return {
        "app_version": str(payload.get("app_version") or "development"),
        "build_timestamp": str(payload.get("build_timestamp") or "development build"),
        "source_revision": str(payload.get("source_revision") or "unbuilt"),
    }


BUILD_METADATA = load_build_metadata()


reference_backend_thread: threading.Thread | None = None
reference_backend_lock = threading.Lock()


def reference_mastering_status(settings: dict[str, Any]) -> dict[str, Any]:
    global reference_backend_thread
    reference = settings.get("matchering_reference", "")
    if reference and pipeline.matchering_api is None and not pipeline.MATCHERING_IMPORT_ERROR:
        with reference_backend_lock:
            if reference_backend_thread is None:
                reference_backend_thread = threading.Thread(target=pipeline.ensure_matchering_available, daemon=True, name="reference-backend-load")
                reference_backend_thread.start()
    loading = bool(reference_backend_thread and reference_backend_thread.is_alive())
    return {"available": pipeline.matchering_api is not None, "loading": loading, "import_error": pipeline.MATCHERING_IMPORT_ERROR, "reference": reference}


request_timings: dict[str, list[float]] = {}
request_timings_lock = threading.Lock()


@app.before_request
def start_request_timer() -> None:
    g.request_started = time.perf_counter()


@app.after_request
def finish_request_timer(response: Response) -> Response:
    duration = (time.perf_counter() - g.request_started) * 1000
    response.headers["Server-Timing"] = f"app;dur={duration:.2f}"
    if request.path.startswith("/api/"):
        key = request.url_rule.rule if request.url_rule else request.path
        with request_timings_lock:
            values = request_timings.setdefault(key, [])
            values.append(round(duration, 2))
            del values[:-100]
    return response


@app.get("/api/performance")
def api_performance() -> Response:
    with request_timings_lock:
        return jsonify({key: {"requests": len(values), "last_ms": values[-1], "mean_ms": round(sum(values) / len(values), 2), "max_ms": max(values)} for key, values in request_timings.items() if values})


def runtime_build_metadata() -> dict[str, str]:
    """Identify the exact frozen resources serving the current WebView."""
    payload = dict(BUILD_METADATA)
    for name, key in (("app.js", "app_js_sha256"), ("app.css", "app_css_sha256")):
        path = RESOURCE_ROOT / "static" / name
        try:
            payload[key] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            payload[key] = "unavailable"
    payload["runtime_pid"] = str(os.getpid())
    payload["runtime_bundle"] = str(RESOURCE_ROOT)
    return payload


def json_default(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def save_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=json_default), encoding="utf-8")


def save_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=json_default), encoding="utf-8")
    tmp.replace(path)


def fmt_time(seconds: float) -> str:
    return pipeline.fmt_time(seconds)


def stem_display_label(stem: pipeline.Stem) -> str:
    name = stem.path.name
    for suffix in ("JAM.tracks.wav", ".tracks.wav", "JAM.tracks", ".tracks"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    match = re.match(r"^\d{10}\([^)]*\)(.*)$", name)
    if match:
        name = match.group(1)
    return name


def active_stems_for_segment(stems: list[pipeline.Stem], segment: pipeline.Segment) -> list[str]:
    # A decodable source is available to the user even when it is quiet or
    # sparse in this song. Activity is diagnostic, not an eligibility filter.
    return [stem.path.name for stem in stems]


MIX_PLAN_VERSION = 13


def mix_plan_signature(segment_id: int, segment: pipeline.Segment, song_overrides: dict[str, Any]) -> str:
    identity = detection_state_signature()
    payload = {
        "version": MIX_PLAN_VERSION,
        "song_id": int(segment_id),
        "start_sec": round(float(segment.start), 6),
        "end_sec": round(float(segment.end), 6),
        "overrides": song_overrides,
        "dsp_revision": MIX_PLAN_VERSION,
        "source_identity": (identity[0], identity[2]),
        "source_config": load_source_config(),
        "auto_mix_profile": getattr(pipeline, "AUTO_MIX_PROFILE_VERSION", 1),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def load_mix_plan(
    segment_id: int,
    *,
    require_current: bool = True,
    overrides_snapshot: dict[str, Any] | None = None,
    state_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    payload = load_json(MIX_PLANS_PATH, {})
    entry = payload.get(str(segment_id)) if isinstance(payload, dict) else None
    if not isinstance(entry, dict):
        return None
    if int(entry.get("version", 0)) != MIX_PLAN_VERSION:
        return None
    if not require_current:
        return entry.get("plan") if isinstance(entry.get("plan"), dict) else None
    state = state_snapshot if isinstance(state_snapshot, dict) else load_render_state()
    if not 1 <= segment_id <= len(state["segments"]):
        return None
    overrides = normalize_overrides(
        overrides_snapshot if isinstance(overrides_snapshot, dict) else load_json(OVERRIDES_PATH, {"songs": {}})
    )
    song_overrides = overrides.get("songs", {}).get(str(segment_id), {})
    expected = mix_plan_signature(segment_id, state["segments"][segment_id - 1], song_overrides)
    if entry.get("signature") != expected:
        return None
    plan = entry.get("plan")
    if isinstance(plan, dict):
        profile = plan.get("auto_mix_balance", {}).get("effect_profile", {})
        if not profile:
            profile = pipeline.per_song_effect_profile({name: params.get("role", "") for name, params in plan.get("stems", {}).items()}, {}, 0.0, 0.0, {})
        for name, params in plan.get("stems", {}).items():
            if isinstance(params, dict):
                params.update(pipeline.resolved_effect_settings(song_overrides.get("stems", {}).get(name, {}), str(params.get("role", "")), profile))
        return plan
    return None


def save_mix_plan(segment_id: int, segment: pipeline.Segment, song_overrides: dict[str, Any], plan: dict[str, Any]) -> None:
    payload = load_json(MIX_PLANS_PATH, {})
    if not isinstance(payload, dict):
        payload = {}
    payload[str(segment_id)] = {
        "version": MIX_PLAN_VERSION,
        "signature": mix_plan_signature(segment_id, segment, song_overrides),
        "created_at": time.time(),
        "selection": {"start_sec": float(segment.start), "end_sec": float(segment.end)},
        "plan": plan,
    }
    save_json_atomic(MIX_PLANS_PATH, payload)


@source_edit_locked
def load_render_state() -> dict[str, Any]:
    """Load Analyze output for a worker without running detection again."""
    settings = load_settings()
    configure_source_folder(settings.get("source_folder") or pipeline.SOURCE_DIR)
    signature = detection_state_signature()
    snapshot = load_detection_snapshot(signature)
    if not isinstance(snapshot, dict):
        raise RuntimeError("Analyze required: no current detection snapshot exists for this source.")
    segments = list(snapshot.get("segments", []))
    # Select Cuts already persists the exact editor segments in this snapshot.
    # Reapplying older ordinal-only selections can move newer confirmed cuts.
    return snapshot


def stem_is_active_for_preview(
    stem: pipeline.Stem,
    rms_values_db: dict[str, float],
    has_audio: dict[str, bool],
    dynamic_spread_db: dict[str, float],
    loudest_db: float,
    envelopes: dict[str, np.ndarray] | None = None,
) -> bool:
    name = stem.path.name
    active, _reason = pipeline.segment_stem_activity_decision(
        stem,
        rms_values_db.get(name, -120.0),
        has_audio.get(name, False),
        dynamic_spread_db.get(name, 0.0),
        loudest_db,
        (envelopes or {}).get(name) if isinstance(envelopes, dict) else None,
    )
    return active


def persisted_fader_values(stem_ov: dict[str, Any]) -> tuple[float, float, bool]:
    """Return trusted user fader, legacy fader, and provenance flag.

    Older state files used one ambiguous fader field. Those values remain
    available for audit/undo, but must not silently attenuate a new Auto-Mix.
    The UI marks an explicit edit with ``user_confirmed``.
    """
    raw = float(stem_ov.get("fader_db", 0.0) or 0.0)
    trusted = bool(stem_ov.get("user_confirmed", False) or stem_ov.get("user_fader_confirmed", False))
    return (raw if trusted else 0.0, raw if not trusted else 0.0, trusted)


analysis_snapshot_lock = threading.RLock()


def song_analysis_snapshot(state: dict, segment: pipeline.Segment, segment_id: int) -> dict:
    signature = mix_plan_signature(segment_id, segment, {})
    path = ACTIVE_SOURCE_STATE_ROOT / f"song_analysis_{segment_id}_{signature}.npz"
    with analysis_snapshot_lock:
        if path.is_file():
            try:
                cached = pipeline.load_analysis_cache(path)
            except (OSError, ValueError, RuntimeError):
                cached = None
            if isinstance(cached, dict) and cached.get("input_signature") == signature:
                return cached
        sr = state["stems"][0].samplerate
        chunks = max(1, math.ceil(segment.duration / pipeline.RENDER_CHUNK_SECONDS))
        def scan_progress(update):
            ratio = ((int(update["stem_index"]) - 1) + min(1, int(update["chunk_index"]) / chunks)) / max(1, len(state["stems"]))
            app_progress({"current_stage": "analyzing", "song_progress": round(ratio * 15), "heartbeat": time.time(),
                "stage_detail": f"Analyzing song {segment_id}: {update['stem']} ({update['stem_index']}/{len(state['stems'])}), block {update['chunk_index']}/{chunks}"})
        rms_values_db, energies, has_audio, dynamic_spread_db, segment_envelopes, segment_peaks = pipeline.scan_segment_activity(state["stems"], segment, sr, progress_callback=scan_progress)
        app_progress({"current_stage": "analyzing", "song_progress": 15, "heartbeat": time.time(), "stage_detail": f"Song {segment_id}: preparing balance, gates and effects"})
        role_norms_db = pipeline.role_norms_from_detection_cache(state["stems"])
        mix_controls = pipeline.analyze_song_mix_controls(state["stems"], segment, sr, rms_values_db, role_norms_db, active_levels_db={name: pipeline.active_level_db(rms_values_db.get(name, -120.0), env) for name, env in segment_envelopes.items()}, segment_envelopes=segment_envelopes)
        result = {
            "version": 2, "song_id": int(segment_id), "input_signature": signature,
            "selection": {"start_sec": float(segment.start), "end_sec": float(segment.end)},
            "rms_values_db": rms_values_db, "energies": energies, "has_audio": has_audio,
            "dynamic_spread_db": dynamic_spread_db, "segment_envelopes": segment_envelopes,
            "segment_peaks_db": segment_peaks, "role_norms_db": role_norms_db, "mix_controls": mix_controls,
            "mix_profile_version": getattr(pipeline, "AUTO_MIX_PROFILE_VERSION", 1),
            "noise_diagnostics": pipeline.classify_noise_stems(state["stems"], segment, sr),
            "flattening": pipeline.build_per_song_flattening(segment_envelopes),
            "drum_bpm": pipeline.estimate_segment_drum_bpm(state["stems"], segment, sr),
        }
        pipeline.save_analysis_cache(path, result)
        return result


def canonical_mix_params_for_song(segment_id: int, *, state_snapshot: dict | None = None, overrides_snapshot: dict | None = None) -> dict[str, Any]:
    state = state_snapshot if isinstance(state_snapshot, dict) else ensure_pipeline_state()
    if segment_id < 1 or segment_id > len(state["segments"]):
        raise IndexError("song not found")
    if state.get("active_stems_detection_version") != ACTIVE_STEM_DETECTION_VERSION:
        state["active_stems_by_song"] = {}
        state["active_stems_detection_version"] = ACTIVE_STEM_DETECTION_VERSION
    current_plan = load_mix_plan(segment_id, state_snapshot=state, overrides_snapshot=overrides_snapshot)
    if isinstance(current_plan, dict):
        state.setdefault("active_stems_by_song", {})[str(segment_id)] = current_plan["active_stems"]
        return current_plan
    cache = state.setdefault("active_stems_by_song", {})
    key = str(segment_id)
    active_files: set[str] = set()
    overrides = normalize_overrides(overrides_snapshot if isinstance(overrides_snapshot, dict) else load_json(OVERRIDES_PATH, {"songs": {}}))
    songs_payload = overrides.setdefault("songs", {})
    stale_song_ids = []
    for raw_id in list(songs_payload):
        try:
            valid_id = 1 <= int(raw_id) <= len(state["segments"])
        except (TypeError, ValueError):
            valid_id = False
        if not valid_id:
            stale_song_ids.append(str(raw_id))
            songs_payload.pop(raw_id, None)
    if stale_song_ids:
        print(f"STALE OVERRIDES DROPPED: source={pipeline.SOURCE_DIR} song_ids={stale_song_ids}", flush=True)
        append_log("system", f"Ignored stale per-song overrides after re-detection: {', '.join(stale_song_ids)}")
        overrides["_write_trace"] = override_write_trace(overrides)
        if overrides_snapshot is None:
            save_json(OVERRIDES_PATH, overrides)
    song_overrides = songs_payload.setdefault(str(segment_id), {})
    changed = False
    stems_overrides = song_overrides.get("stems", {}) if isinstance(song_overrides, dict) else {}
    if not isinstance(stems_overrides, dict):
        stems_overrides = {}
        song_overrides["stems"] = stems_overrides
        changed = True
    segment = state["segments"][segment_id - 1]
    musician_labels = dict(segment.musician_labels)
    sr = state["stems"][0].samplerate
    analysis_cache = song_analysis_snapshot(state, segment, segment_id)
    rms_values_db = analysis_cache["rms_values_db"]
    energies = analysis_cache["energies"]
    has_audio = analysis_cache["has_audio"]
    dynamic_spread_db = analysis_cache["dynamic_spread_db"]
    segment_envelopes = analysis_cache["segment_envelopes"]
    segment_peaks = analysis_cache["segment_peaks_db"]
    role_norms_db = analysis_cache["role_norms_db"]
    mix_controls = analysis_cache["mix_controls"]
    effective_roles = mix_controls["effective_roles"]
    rhythm_controls = mix_controls["rhythm"]
    vocal_priority = mix_controls["vocal_priority"]
    mic_content = mix_controls["mic_content"]
    role_balance = mix_controls.get("role_balance", {}) if isinstance(mix_controls, dict) else {}
    role_corrections = role_balance.get("role_corrections_db", {}) if isinstance(role_balance, dict) else {}
    vocal_pair_corrections = role_balance.get("vocal_pair_corrections_db", {}) if isinstance(role_balance, dict) else {}
    role_balance_reasons = role_balance.get("role_reasons", {}) if isinstance(role_balance, dict) else {}
    vocal_pair_diagnostics = role_balance.get("vocal_pair_diagnostics", {}) if isinstance(role_balance, dict) else {}
    automatic_mix_profile = mix_controls.get("automatic_mix_profile", {}) if isinstance(mix_controls, dict) else {}
    effect_profile = mix_controls.get("effect_profile", {}) if isinstance(mix_controls, dict) else {}
    effect_space_roles = effect_profile.get("role_space_enabled", {}) if isinstance(effect_profile, dict) else {}
    effect_echo_roles = effect_profile.get("role_echo_enabled", {}) if isinstance(effect_profile, dict) else {}
    effect_role_offsets = effect_profile.get("role_offsets_db", {}) if isinstance(effect_profile, dict) else {}
    loudest_db = max(rms_values_db.values()) if rms_values_db else -120.0
    # Preview and export must exclude the same empty/noisy inputs.
    excluded_noise = pipeline.empty_noise_stem_names(analysis_cache)
    active_names = {stem.path.name for stem in state["stems"] if stem.path.name not in excluded_noise}
    active_files = set(active_names)
    cache[key] = sorted(active_files)
    active_energies = [energies[name] for name in active_names]
    median_energy = float(np.median(active_energies)) if active_energies else 0.0
    active_levels_db = {
        name: pipeline.active_level_db(rms_values_db.get(name, -120.0), segment_envelopes.get(name))
        for name in active_names
    }
    accompaniment_levels = [
        active_levels_db[name]
        for name in active_names
        if effective_roles.get(name, "") not in {"vocal", "room"}
    ]
    accompaniment_reference_db = float(np.median(accompaniment_levels)) if accompaniment_levels else None
    # The expensive Analyze-only products are persisted as one immutable
    # per-song snapshot. Render consumes this snapshot and never recomputes
    # thresholds, Whisper, activity, or Auto-Mix from the source files.
    initialized_makeup_names: set[str] = set()
    solo_files = {
        stem.path.name
        for stem in state["stems"]
        if bool(stems_overrides.get(stem.path.name, {}).get("solo")) and stem.path.name in active_files
    }
    stem_params: dict[str, Any] = {}
    for stem in state["stems"]:
        if stem.path.name not in active_files:
            continue
        stem_ov = stems_overrides.get(stem.path.name, {})
        if not isinstance(stem_ov, dict):
            stem_ov = {}
        user_fader_db, legacy_fader_db, fader_trusted = persisted_fader_values(stem_ov)
        raw_gain_db = float(stem_ov.get("gain_db", 0.0) or 0.0)
        user_gain_db = raw_gain_db if fader_trusted else 0.0
        legacy_gain_db = raw_gain_db if not fader_trusted else 0.0
        mix_role = str(effective_roles.get(stem.path.name, stem.role))
        rhythm_adjustment_db = float(rhythm_controls.get(stem.path.name, {}).get("attenuation_db", 0.0))
        priority_adjustment_db = float(vocal_priority.get(stem.path.name, 0.0))
        vocal_group_adjustment_db = float(mix_controls.get("vocal_group_gain", {}).get(stem.path.name, 0.0))
        role_balance_adjustment_db = float(role_corrections.get(stem.path.name, 0.0))
        vocal_pair_adjustment_db = float(vocal_pair_corrections.get(stem.path.name, 0.0))
        eq_defaults = pipeline.role_eq_defaults(mix_role)
        raw_rms_db = rms_values_db.get(stem.path.name, -120.0)
        role_norm_db = role_norms_db.get(mix_role)
        active_level = active_levels_db.get(stem.path.name, raw_rms_db)
        computed_gain_before_lift_db = pipeline.per_song_auto_mix_gain_db(mix_role, active_level, accompaniment_reference_db)
        computed_gain_db = computed_gain_before_lift_db + rhythm_adjustment_db + priority_adjustment_db + vocal_group_adjustment_db + role_balance_adjustment_db + vocal_pair_adjustment_db
        computed_gain_db = float(np.clip(
            computed_gain_db,
            pipeline.AUTO_MIX_MAX_ATTENUATION_DB,
            pipeline.AUTO_MIX_ROLE_BOOST_LIMITS_DB.get(mix_role, pipeline.AUTO_MIX_MAX_BOOST_DB),
        ))
        computed_gain_db = float(mix_controls.get("rhythm_harmonic_balance", {}).get("gains_db", {}).get(stem.path.name, computed_gain_db))
        computed_gain_db = pipeline.automatic_drum_peak_guard_gain_db(mix_role, computed_gain_db, segment_peaks.get(stem.path.name, -120.0))
        lead_bonus = 1.5 if energies.get(stem.path.name, 0.0) > median_energy * 1.35 and mix_role not in {"kick", "snare", "drums", "bass"} else 0.0
        if "makeup_gain_db" not in stem_ov and "gain_db" in stem_ov and fader_trusted:
            stem_ov["makeup_gain_db"] = user_gain_db
            stem_ov["gain_db"] = 0.0
            initialized_makeup_names.add(stem.path.name)
            changed = True
        if "makeup_gain_db" not in stem_ov:
            stem_ov["makeup_gain_db"] = computed_gain_db
            initialized_makeup_names.add(stem.path.name)
            changed = True
        elif not bool(stem_ov.get("manual_makeup_gain_db", False)) and abs(float(stem_ov.get("makeup_gain_db", computed_gain_db)) - computed_gain_db) > 0.001:
            # Refresh legacy auto values after hierarchy changes; fader/gain
            # overrides remain intact and are applied on top.
            stem_ov["makeup_gain_db"] = computed_gain_db
            initialized_makeup_names.add(stem.path.name)
            changed = True
        if "gain_db" not in stem_ov:
            stem_ov["gain_db"] = 0.0
            changed = True
        if "fader_db" not in stem_ov:
            stem_ov["fader_db"] = 0.0
            changed = True
        if "mute" not in stem_ov:
            stem_ov["mute"] = False
            changed = True
        if "solo" not in stem_ov:
            stem_ov["solo"] = False
            changed = True
        resolved_gate = pipeline.resolved_instrument_gate(stem_ov, mix_role, analysis_cache.get("noise_diagnostics", {}).get(stem.path.name, {}))
        if stem_ov.get("gate_enabled") != resolved_gate:
            stem_ov["gate_enabled"] = resolved_gate
            changed = True
        # Automatic effects are content-aware by song. Once the user toggles
        # an effect in the editor, the explicit marker keeps that choice.
        effects_user_confirmed = bool(stem_ov.get("effects_user_confirmed", False))
        if not effects_user_confirmed:
            auto_space = bool(effect_space_roles.get(mix_role, False))
            auto_echo = bool(effect_echo_roles.get(mix_role, False))
            if stem_ov.get("space_enabled") != auto_space:
                stem_ov["space_enabled"] = auto_space
                changed = True
            if stem_ov.get("echo_enabled") != auto_echo:
                stem_ov["echo_enabled"] = auto_echo
                changed = True
        else:
            if "space_enabled" not in stem_ov:
                stem_ov["space_enabled"] = False
                changed = True
            if "echo_enabled" not in stem_ov:
                stem_ov["echo_enabled"] = False
                changed = True
        if "pan" not in stem_ov:
            stem_ov["pan"] = pipeline.enforced_pan(stem.role, stem.name)
            changed = True
        for eq_key, default_value in eq_defaults.items():
            if eq_key not in stem_ov:
                stem_ov[eq_key] = default_value
                changed = True
        if "reverb_send_db" not in stem_ov:
            stem_ov["reverb_send_db"] = 0.0
            changed = True
        if "delay_send_db" not in stem_ov:
            stem_ov["delay_send_db"] = 0.0
            changed = True
        stems_overrides[stem.path.name] = stem_ov
        makeup_gain_db = float(stem_ov.get("makeup_gain_db", computed_gain_db))
        gain_db = user_gain_db
        fader_db = user_fader_db
        mute = bool(stem_ov.get("mute", False)) or fader_db <= pipeline.FADER_HARD_SILENCE_DB
        solo = bool(stem_ov.get("solo", False))
        base_reverb = pipeline.reverb_send_level_db(mix_role)
        base_delay = pipeline.delay_send_level_db(mix_role, lead_bonus)
        effect_offsets = effect_role_offsets.get(mix_role, {}) if isinstance(effect_role_offsets, dict) else {}
        reverb_scene_offset_db = float(effect_offsets.get("reverb_db", 0.0) or 0.0)
        delay_scene_offset_db = float(effect_offsets.get("delay_db", 0.0) or 0.0)
        stem_params[stem.path.name] = {
            "file": stem.path.name,
            "label": f"{musician_labels.get(stem.role)} — {stem_display_label(stem)}" if musician_labels.get(stem.role) else stem_display_label(stem),
            "role": mix_role,
            "source_role": stem.role,
            "content_classification": mic_content.get(stem.path.name),
            "rhythm_analysis": rhythm_controls.get(stem.path.name),
            "rhythmic_attenuation_db": rhythm_adjustment_db,
            "vocal_priority_attenuation_db": priority_adjustment_db,
            "vocal_group_correction_db": vocal_group_adjustment_db,
            "vocal_pair_correction_db": vocal_pair_adjustment_db,
            "role_balance_correction_db": role_balance_adjustment_db,
            "role_balance_reason": role_balance_reasons.get(stem.path.name),
            "vocal_pair_key": mix_controls.get("vocal_pair_keys", {}).get(stem.path.name),
            "mix_role_group": pipeline.mix_role_group(mix_role, stem.path.name),
            "base_level_db": pipeline.base_level_db(mix_role),
            "makeup_gain_db": makeup_gain_db,
            "gain_db": gain_db,
            "user_gain_db": user_gain_db,
            "user_fader_db": user_fader_db,
            "legacy_gain_db": legacy_gain_db,
            "legacy_fader_db": legacy_fader_db,
            "legacy_gain_state": "legacy_untrusted" if (abs(legacy_gain_db) > 0.001 or abs(legacy_fader_db) > 0.001) else "none",
            "user_confirmed": fader_trusted,
            "computed_gain_db": computed_gain_db,
            "active_level_db": active_level,
            "accompaniment_reference_db": accompaniment_reference_db,
            "automatic_gain_reason": "per-song kit and guitar balance" if stem.path.name in mix_controls.get("rhythm_harmonic_balance", {}).get("gains_db", {}) else "per-song active-envelope balance",
            "automatic_fader_db": computed_gain_db,
            "automatic_gain_before_vocal_mic_lift_db": computed_gain_before_lift_db,
            "automatic_vocal_mic_lift_db": pipeline.AUTOMATIC_VOCAL_MIC_LIFT_DB if mix_role == "vocal" else 0.0,
            "role_norm_db": role_norm_db,
            "performance_deviation_db": None if role_norm_db is None else raw_rms_db - role_norm_db,
            "automatic_performance_correction_db": None if role_norm_db is None else -float(np.clip(raw_rms_db - role_norm_db, -3.0, 3.0)),
            "lead_bonus_db": lead_bonus,
            "fader_db": fader_db,
            "level_gain_db": fader_db,
            "total_gain_db": makeup_gain_db + gain_db + fader_db,
            "mute": mute,
            "solo": solo,
            "fx_enabled": bool(stem_ov.get("fx_enabled", True)),
            "gate_enabled": bool(stem_ov.get("gate_enabled", False)),
            "gate_points": pipeline.section_gate_points(segment_envelopes.get(stem.path.name, np.array([], dtype=np.float32)), pipeline.section_gate_noise_floor(mix_role, analysis_cache.get("noise_diagnostics", {}).get(stem.path.name, {}), segment_envelopes.get(stem.path.name, np.array([], dtype=np.float32)))) if mix_role != "vocal" else [],
            "space_enabled": bool(stem_ov.get("space_enabled", False)),
            "echo_enabled": bool(stem_ov.get("echo_enabled", False)),
            "effects_user_confirmed": bool(stem_ov.get("effects_user_confirmed", False)),
            "effect_scene": effect_profile.get("scene") if isinstance(effect_profile, dict) else None,
            "reverb_scene_offset_db": reverb_scene_offset_db,
            "delay_scene_offset_db": delay_scene_offset_db,
            "muted_by_solo": bool(solo_files and stem.path.name not in solo_files),
            "pan": pipeline.enforced_pan(mix_role, stem.name, stem_ov.get("pan")),
            "eq_low_cut_hz": float(stem_ov.get("eq_low_cut_hz", eq_defaults["eq_low_cut_hz"])),
            "eq_mid_gain_db": float(stem_ov.get("eq_mid_gain_db", eq_defaults["eq_mid_gain_db"])),
            "eq_air_gain_db": float(stem_ov.get("eq_air_gain_db", eq_defaults["eq_air_gain_db"])),
            "reverb_base_db": base_reverb,
            "reverb_send_db": float(stem_ov.get("reverb_send_db", 0.0) or 0.0),
            "reverb_total_db": None if base_reverb is None else base_reverb + reverb_scene_offset_db + float(stem_ov.get("reverb_send_db", 0.0) or 0.0),
            "delay_base_db": base_delay,
            "delay_send_db": float(stem_ov.get("delay_send_db", 0.0) or 0.0),
            "delay_total_db": None if base_delay is None else base_delay + delay_scene_offset_db + float(stem_ov.get("delay_send_db", 0.0) or 0.0),
        }
    active_vocal_names = [
        stem.path.name
        for stem in state["stems"]
        if stem.role == "vocal" and stem.path.name in stem_params and not bool(stem_params[stem.path.name].get("muted_by_solo"))
    ]
    if len(active_vocal_names) > 1:
        post_gain = np.array(
            [active_levels_db.get(name, rms_values_db[name]) + float(stem_params[name]["makeup_gain_db"]) for name in active_vocal_names],
            dtype=np.float32,
        )
        target_vocal_rms = float(np.median(post_gain))
        for name in active_vocal_names:
            if name not in initialized_makeup_names:
                continue
            current = active_levels_db.get(name, rms_values_db[name]) + float(stem_params[name]["makeup_gain_db"])
            adjustment = float(np.clip(target_vocal_rms - current, -6.0, 6.0))
            stem_params[name]["makeup_gain_db"] = float(stem_params[name]["makeup_gain_db"]) + adjustment
            stem_params[name]["vocal_balance_db"] = adjustment
            stems_overrides[name]["makeup_gain_db"] = stem_params[name]["makeup_gain_db"]
            changed = True
    target_lufs = float(song_overrides.get("target_lufs", -14.0) if isinstance(song_overrides, dict) else -14.0)
    if "vocal_bus_db" not in song_overrides:
        song_overrides["vocal_bus_db"] = 0.0
        changed = True
    if "target_lufs" not in song_overrides:
        song_overrides["target_lufs"] = -14.0
        changed = True
    if "master_db" not in song_overrides:
        song_overrides["master_db"] = 0.0
        changed = True
    if changed and overrides_snapshot is None:
        overrides["_write_trace"] = override_write_trace(overrides)
        save_json(OVERRIDES_PATH, overrides)
    plan = {
        "song": segment_id,
        "mix_source": pipeline.mix_source_label(song_overrides),
        "active_stems": cache[key],
        "vocal_bus_db": float(song_overrides.get("vocal_bus_db", 0.0) if isinstance(song_overrides, dict) else 0.0),
        "target_lufs": target_lufs,
        "preview_master_gain_db": target_lufs - (-14.0),
        "mastering_intensity": str(song_overrides.get("mastering_intensity", "natural") if isinstance(song_overrides, dict) else "natural"),
        "auto_mix_balance": {
            **(mix_controls.get("balance", {}) if isinstance(mix_controls.get("balance", {}), dict) else {}),
            "role_balance": role_balance,
            "vocal_pair_diagnostics": vocal_pair_diagnostics,
            "automatic_mix_profile": automatic_mix_profile,
            "effect_profile": effect_profile,
            "guitar_original_level_db": role_balance.get("guitar_original_level_db"),
            "rhythm_harmonic_balance": mix_controls.get("rhythm_harmonic_balance", {}),
            "guitar_reduction_db": min((float(value) for name, value in mix_controls.get("rhythm_harmonic_balance", {}).get("gains_db", {}).items() if effective_roles.get(name) == "guitar"), default=float(role_balance.get("guitar_reduction_db", 0.0))),
            "guitar_reduction_reason": "individual song balance against musical reference" if mix_controls.get("rhythm_harmonic_balance") else role_balance.get("guitar_reduction_reason", "no reliable vocal overlap evidence"),
        },
        "auto_mix_method": "independent song analysis, combined kit power balance and guitar recovery; confirmed controls applied last",
        "stems": stem_params,
    }
    plan_signature = mix_plan_signature(segment_id, segment, song_overrides)
    cache_path = ACTIVE_SOURCE_STATE_ROOT / f"song_analysis_{segment_id}_{analysis_cache['input_signature']}.npz"
    plan["analysis_cache_path"] = str(cache_path)
    plan["analysis_cache_signature"] = plan_signature
    plan["analysis_cache_version"] = 2
    plan["mix_profile_version"] = getattr(pipeline, "AUTO_MIX_PROFILE_VERSION", 1)
    plan["effective_dsp_plan_hash"] = plan_signature
    save_mix_plan(segment_id, segment, song_overrides, plan)
    return plan


def override_value_snapshot(settings: Any) -> dict[str, Any]:
    if not isinstance(settings, dict):
        settings = {}
    return {
        "fader_db": float(settings.get("fader_db", 0.0) or 0.0),
        "mute": bool(settings.get("mute", False)),
        "solo": bool(settings.get("solo", False)),
    }


def override_write_trace(payload: dict[str, Any]) -> dict[str, Any]:
    trace: dict[str, Any] = {}
    for song_id, song in payload.get("songs", {}).items():
        stems = song.get("stems", {}) if isinstance(song, dict) else {}
        if not isinstance(stems, dict):
            continue
        song_trace = {}
        for stem_name, settings in stems.items():
            snap = override_value_snapshot(settings)
            if abs(float(snap["fader_db"])) > 0.01 or bool(snap["mute"]) or bool(snap["solo"]):
                song_trace[str(stem_name)] = snap
        if song_trace:
            trace[str(song_id)] = song_trace
    return trace


def out_dir() -> Path:
    return pipeline.OUTPUT_ROOT / pipeline.SESSION_DATE


def render_diagnostics_dir(job_id: str) -> Path:
    """Return the private per-job directory for verification artifacts.

    The user-selected directory is deliberately reserved for final MP3 files.
    WAVs, manifests and batch summaries remain outside it for traceability.
    """
    path = RENDER_DIAGNOSTICS_ROOT / pipeline.sanitize_filename(str(job_id))
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate_render_target(path_value: str | Path) -> Path:
    """Require a user-selected destination outside app resources and state."""
    candidate = Path(path_value).expanduser().resolve()
    forbidden_roots = (RESOURCE_ROOT.resolve(), STATE_ROOT.resolve(), PREVIEW_CACHE_ROOT.resolve())
    if any(candidate == root or root in candidate.parents for root in forbidden_roots):
        raise ValueError("Render destination must be outside the Zucker Mixer app and state folders.")
    return candidate


def default_settings() -> dict[str, Any]:
    return {
        "skipped_segments": [],
        "last_render_dir": "",
        "source_folder": str(pipeline.SOURCE_DIR),
        "audio_scan_mode": "auto",
        "known_song_count": None,
        "matchering_reference": "",
    }


def load_settings() -> dict[str, Any]:
    settings = load_json(SETTINGS_PATH, default_settings())
    if not isinstance(settings, dict):
        settings = default_settings()
    settings.setdefault("skipped_segments", [])
    # Skip state is source-scoped. The legacy field remains in the settings
    # file for backward compatibility, but it must never hide slots from a
    # different jam.
    settings["skipped_segments"] = sorted(load_skipped_segments())
    settings.setdefault("last_render_dir", "")
    settings.setdefault("source_folder", str(pipeline.SOURCE_DIR))
    if child_status_snapshot and child_status_snapshot.get("source_folder"):
        settings["source_folder"] = child_status_snapshot["source_folder"]
    settings.setdefault("audio_scan_mode", "auto")
    settings.setdefault("known_song_count", None)
    settings.setdefault("matchering_reference", "")
    return settings



def load_skipped_segments() -> set[int]:
    payload = load_json(SKIPPED_SEGMENTS_PATH, None)
    if payload is None:
        # A global legacy list has no reliable source identity after project
        # switching. Only explicit source-scoped skip decisions may hide songs.
        payload = {"segments": []}
    values = payload.get("segments", []) if isinstance(payload, dict) else payload
    result: set[int] = set()
    for value in values if isinstance(values, list) else []:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def save_skipped_segments(values: Any) -> None:
    result: set[int] = set()
    for value in values if isinstance(values, list) else []:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            raise ValueError("skipped_segments must contain whole numbers")
    save_json_atomic(SKIPPED_SEGMENTS_PATH, {"version": 1, "segments": sorted(result)})

def save_settings(settings: dict[str, Any]) -> None:
    save_json(SETTINGS_PATH, settings)


def default_source_config() -> dict[str, Any]:
    return {
        "config_version": 1,
        "expected_song_count": None,
        "min_song_seconds": 480.0,
        "max_song_seconds": 780.0,
        "structural_min_song_seconds": 90.0,
        "structural_max_song_seconds": 780.0,
        "structural_close_song_seconds": 90.0,
        "min_final_song_seconds": 480.0,
        "suspicious_short_song_seconds": 5 * 60.0,
        "suspicious_long_song_seconds": 20 * 60.0,
        "whisper_model": os.environ.get("ZUCKER_WHISPER_MODEL", "tiny"),
        "whisper_idle_timeout_seconds": float(os.environ.get("ZUCKER_WHISPER_IDLE_TIMEOUT_SECONDS", os.environ.get("ZUCKER_WHISPER_TIMEOUT_SECONDS", "300"))),
        "ear_confirmed_splits": [],
        "sanity_anchors": [],
    }


def load_source_config() -> dict[str, Any]:
    config = load_json(SOURCE_CONFIG_PATH, {})
    if not isinstance(config, dict):
        config = {}
    merged = default_source_config()
    merged.update(config)
    # Preserve a legacy target only for the source that owned the old setting.
    if not SOURCE_CONFIG_PATH.exists():
        legacy = load_settings()
        legacy_source = str(legacy.get("source_folder") or "").strip()
        if legacy_source and Path(legacy_source).expanduser().resolve() == Path(pipeline.SOURCE_DIR).expanduser().resolve():
            raw_count = legacy.get("known_song_count")
            if raw_count not in (None, ""):
                try:
                    merged["expected_song_count"] = int(raw_count)
                except (TypeError, ValueError):
                    pass
    return merged


def save_source_config(config: dict[str, Any]) -> None:
    save_json(SOURCE_CONFIG_PATH, {**default_source_config(), **dict(config), "config_version": 1})


def load_manual_splits() -> list[float]:
    payload = load_json(MANUAL_SPLITS_PATH, {"splits": []})
    values = payload.get("splits", []) if isinstance(payload, dict) else payload
    out: list[float] = []
    for value in values:
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def save_manual_splits(values: list[float]) -> None:
    save_json(MANUAL_SPLITS_PATH, {"splits": sorted(set(float(v) for v in values))})


@source_edit_locked
def configure_source_folder(source_folder: str | Path) -> Path:
    global MANUAL_EDITOR_STATE_PATH, ACTIVE_SOURCE_STATE_ROOT, OVERRIDES_PATH, HISTORY_PATH, MANUAL_SPLITS_PATH, SEGMENT_SELECTIONS_PATH, WAVEFORM_CACHE_PATH, MIX_PLANS_PATH, SONG_NAMES_PATH, DETECTION_STATE_PATH, EDITOR_HISTORY_PATH, REDETECTION_CANDIDATE_PATH, REDETECTION_BACKUP_PATH, SECOND_PASS_CANDIDATE_PATH, PREVIEW_DIR, SOURCE_CONFIG_PATH, SKIPPED_SEGMENTS_PATH
    source = Path(source_folder).expanduser().resolve()
    pipeline.SOURCE_DIR = source
    pipeline.AUDIO_SCAN_REPORT = {
        "source": str(source),
        "accepted": [],
        "skipped": [],
        "included_warnings": [],
        "status": "Scanning folder",
        "error": "",
    }
    pipeline.configure_detection_cache(source, STATE_ROOT)
    source_key = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:20]
    ACTIVE_SOURCE_STATE_ROOT = STATE_ROOT / "sources" / source_key
    ACTIVE_SOURCE_STATE_ROOT.mkdir(parents=True, exist_ok=True)
    OVERRIDES_PATH = ACTIVE_SOURCE_STATE_ROOT / "mix_overrides.json"
    HISTORY_PATH = ACTIVE_SOURCE_STATE_ROOT / "render_history.json"
    MANUAL_SPLITS_PATH = ACTIVE_SOURCE_STATE_ROOT / "manual_splits.json"
    SEGMENT_SELECTIONS_PATH = ACTIVE_SOURCE_STATE_ROOT / "segment_selections.json"
    WAVEFORM_CACHE_PATH = ACTIVE_SOURCE_STATE_ROOT / "waveform_cache.json"
    MIX_PLANS_PATH = ACTIVE_SOURCE_STATE_ROOT / "mix_plans.json"
    SONG_NAMES_PATH = ACTIVE_SOURCE_STATE_ROOT / "song_names.json"
    DETECTION_STATE_PATH = ACTIVE_SOURCE_STATE_ROOT / "detection_state.json"
    EDITOR_HISTORY_PATH = ACTIVE_SOURCE_STATE_ROOT / "editor_history.json"
    MANUAL_EDITOR_STATE_PATH = ACTIVE_SOURCE_STATE_ROOT / "manual_editor_state.json"
    REDETECTION_CANDIDATE_PATH = ACTIVE_SOURCE_STATE_ROOT / "redetect_candidate.json"
    REDETECTION_BACKUP_PATH = ACTIVE_SOURCE_STATE_ROOT / "redetect_backup.json"
    SECOND_PASS_CANDIDATE_PATH = ACTIVE_SOURCE_STATE_ROOT / "whisper_second_pass_candidate.json"
    PREVIEW_DIR = PREVIEW_CACHE_ROOT / source_key
    SOURCE_CONFIG_PATH = ACTIVE_SOURCE_STATE_ROOT / "session_config.json"
    SKIPPED_SEGMENTS_PATH = ACTIVE_SOURCE_STATE_ROOT / "skipped_segments.json"
    pipeline.configure_detection_profile(load_source_config())
    return source


def restore_missing_saved_opening(state: dict[str, Any]) -> bool:
    """Recover source-scoped human cuts omitted by a later automatic snapshot."""
    segments = state.get("segments", [])
    if not segments or segments[0].start <= 0.1:
        return False
    saved = load_json(SEGMENT_SELECTIONS_PATH, {}).get("segments", {})
    candidates = []
    for value in saved.values():
        try:
            start, end = float(value["start_sec"]), float(value["end_sec"])
        except (KeyError, ValueError, TypeError):
            continue
        if 0 <= start < end <= segments[0].start and value.get("source") == "manual":
            candidates.append((start, end, value))
    candidates.sort(key=lambda item: item[0])
    prefix, rows = [], []
    for start, end, value in candidates:
        if prefix and start < prefix[-1].end:
            continue
        seg = pipeline.Segment(start, end, core_start=start, core_end=end, nominal_end=end,
            boundary_source="manual-recovered", boundary_validation="manual-selection",
            boundary_validation_reason="recovered saved human cut from this source")
        prefix.append(seg)
        rows.append({"slot_id": value.get("slot_id"), "session_id": value.get("session_id"),
                     "source_start": value.get("source_start", start), "source_end": value.get("source_end", end)})
    if not prefix:
        return False
    # Recover other human selections by time overlap, never by their old
    # ordinal. Recent manual split/move operations remain authoritative.
    proposed = list(segments)
    used = set()
    for value in saved.values():
        try:
            start, end = float(value["start_sec"]), float(value["end_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        if value.get("source") != "manual" or end <= segments[0].start or start >= end:
            continue
        scores = [(overlap_ratio(start,end,seg.start,seg.end), i) for i,seg in enumerate(segments)
                  if i not in used and not seg.boundary_source.startswith("manual")]
        if not scores:
            continue
        score, index = max(scores)
        if score < 0.7:
            continue
        proposed[index] = replace(segments[index],start=start,end=end,core_start=start,core_end=end,
            nominal_end=end,boundary_source="manual-recovered",boundary_validation="manual-selection",
            boundary_validation_reason="recovered saved human selection from this source")
        used.add(index)
    if all(left.end <= right.start for left,right in zip(proposed,proposed[1:])):
        segments = proposed
    # A saved opening before the commentator's known first introduction is song 0.
    history = load_json(EDITOR_HISTORY_PATH, {})
    introductions = [seg for snap in history.get("undo", []) for seg in snap.get("segments", [])
                     if seg.get("spoken_song_number") == 1]
    zero = any(prefix[0].end <= float(seg.get("speech_intro_start") or seg["start"]) < (prefix[1].end if len(prefix)>1 else segments[0].start) for seg in introductions)
    state["segments"] = prefix + segments
    state["raw_songs"] = rows + state.get("raw_songs", [])
    for ordinal, (row, seg) in enumerate(zip(state["raw_songs"],state["segments"]), 1):
        number = ordinal - 1 if zero else ordinal
        seg = replace(seg, assigned_song_number=number)
        state["segments"][ordinal - 1] = seg
        row.update(id=ordinal, start=seg.start, end=seg.end, render_end=seg.end,
                   duration=seg.duration, duration_text=fmt_time(seg.duration),
                   assigned_song_number=number, segment=asdict(seg))
    return True


def load_detection_snapshot(signature: tuple[str, float | None, tuple[tuple[str, int, int], ...]]) -> dict[str, Any] | None:
    payload = load_json(DETECTION_STATE_PATH, None)
    manual = load_json(MANUAL_EDITOR_STATE_PATH, None)
    manual_matches = isinstance(manual, dict) and manual.get("fingerprint") == source_job_identity(signature)["fingerprint"] and not pipeline.DETECTION_RESCAN_MODE
    if manual_matches:
        payload = dict(manual)
        payload["source_signature"] = [signature[0], list(signature[1]) if isinstance(signature[1], tuple) else signature[1], [list(item) for item in signature[2]]]
    expected_signature = [
        signature[0],
        [signature[1][0], signature[1][1]] if isinstance(signature[1], tuple) else signature[1],
        [list(item) for item in signature[2]],
    ]
    if not isinstance(payload, dict) or payload.get("source_signature") != expected_signature:
        return None
    if not manual_matches and legacy_single_slot_snapshot_reason(payload):
        return None
    try:
        stems = [pipeline.Stem(path=Path(item["path"]), **{key: item[key] for key in ("name", "role", "samplerate", "channels", "frames", "duration", "timeline_frames", "offset_seconds", "offset_source")}) for item in payload["stems"]]
        segments = []
        for item in payload["segments"]:
            value = dict(item)
            value["musician_labels"] = tuple(tuple(pair) for pair in value.get("musician_labels", []))
            segments.append(pipeline.Segment(**value))
        if not stems or not segments:
            return None
        state = {
            "stems": stems,
            "segments": segments,
            "raw_songs": payload["raw_songs"],
            "stem_info": payload["stem_info"],
            "active_stems_by_song": {},
            "manual_editor_authoritative": bool(manual_matches),
            "candidate_pending": bool(payload.get("candidate_pending")),
            "segmentation_status": payload.get("segmentation_status"),
            "audio_scan": payload.get("audio_scan", {}),
            "detection_calibration": payload.get("detection_calibration", {}),
        }
        if not isinstance(manual, dict) and restore_missing_saved_opening(state):
            backup = DETECTION_STATE_PATH.with_name("detection_state.before-opening-recovery.json")
            if not backup.exists():
                save_json_atomic(backup, payload)
            persist_manual_editor_state(state)
        return state
    except (KeyError, TypeError, ValueError):
        return None


def legacy_single_slot_snapshot_reason(snapshot: dict[str, Any] | None) -> str:
    """Explain why a one-window multi-stem snapshot cannot be a session."""
    if not isinstance(snapshot, dict):
        return ""
    slots = snapshot.get("raw_songs", [])
    stems = snapshot.get("stems", [])
    if not isinstance(slots, list) or not isinstance(stems, list):
        return ""
    if len(slots) != 1 or len(stems) <= 1:
        return ""
    if slots[0].get("proposal_status") == "manual":
        return ""
    duration = max(
        (
            float(item.get("offset_seconds", 0.0))
            + float(item.get("timeline_duration", item.get("duration", 0.0)))
            if isinstance(item, dict)
            else float(item.offset_seconds) + float(item.timeline_duration)
            for item in stems
        ),
        default=0.0,
    )
    if duration <= pipeline.HARD_MAX_SONG_SECONDS:
        return ""
    return (
        f"Rejected legacy one-slot snapshot: {len(stems)} parallel stems span "
        f"{duration:.1f}s. It is a diagnostic candidate, not a song session."
    )


def snapshot_has_valid_segment_windows(snapshot: dict[str, Any]) -> bool:
    """Return whether a cached detection can be exposed without redetecting.

    Duration-invalid proposals are still useful application state: they must
    be shown as ``needs_review`` in Select Cuts, not discarded and replaced
    with an empty song list.  The render guard remains responsible for
    refusing an unconfirmed invalid window.
    """
    for item in snapshot.get("segments", []) if isinstance(snapshot, dict) else []:
        try:
            start = float(item.get("start", 0.0)) if isinstance(item, dict) else float(item.start)
            end = float(item.get("end", 0.0)) if isinstance(item, dict) else float(item.end)
        except (AttributeError, TypeError, ValueError):
            return False
    return bool(snapshot.get("stems")) and bool(snapshot.get("segments"))


def _ensure_snapshot_scan_report(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Backfill scan details for older snapshots created before scan metadata."""
    report = snapshot.get("audio_scan")
    if isinstance(report, dict) and report.get("accepted"):
        return report
    stems = snapshot.get("stems", [])
    accepted = []
    source = Path(pipeline.SOURCE_DIR)
    for stem in stems:
        path = Path(stem.path)
        try:
            name = str(path.relative_to(source))
        except ValueError:
            name = path.name
        accepted.append({"file": name, "path": str(path)})
    return {
        "source": str(source),
        "accepted": accepted,
        "skipped": [],
        "included_warnings": [],
        "mode": "cached",
        "fragment_warning": "",
        "fragment_files": [],
        "aligned_files": [],
        "using_aligned_only": False,
        "status": f"Loaded {len(accepted)} WAV files",
        "error": "",
    }


def save_detection_snapshot(state: dict[str, Any], signature: tuple[str, float | None, tuple[tuple[str, int, int], ...]]) -> None:
    payload = {
        "version": 1,
        "job_id": (child_status_snapshot or {}).get("id"),
        **source_job_identity(signature),
        "candidate_pending": bool(state.get("candidate_pending")),
        "segmentation_status": state.get("segmentation_status"),
        "source_signature": [signature[0], signature[1], [list(item) for item in signature[2]]],
        "stems": [{**asdict(stem), "path": str(stem.path)} for stem in state["stems"]],
        "segments": [asdict(segment) for segment in state["segments"]],
        "raw_songs": state["raw_songs"],
        "stem_info": state["stem_info"],
        "audio_scan": state.get("audio_scan", {}),
        "detection_calibration": state.get("detection_calibration", {}),
    }
    target = REDETECTION_CANDIDATE_PATH if pipeline.DETECTION_RESCAN_MODE else DETECTION_STATE_PATH
    save_json_atomic(target, payload)


def session_id_for_signature(signature: tuple[str, float | None, tuple[tuple[str, int, int], ...]]) -> str:
    return hashlib.sha256(json.dumps(signature, sort_keys=True, default=str).encode()).hexdigest()[:20]


def source_job_identity(signature: tuple) -> dict[str, str]:
    source = str(Path(pipeline.SOURCE_DIR).expanduser().resolve())
    return {
        "source_id": hashlib.sha256(source.encode()).hexdigest()[:20],
        "fingerprint": hashlib.sha256(json.dumps([source, signature[2]], default=str).encode()).hexdigest(),
        "session_id": session_id_for_signature(signature),
    }


def detection_state_signature() -> tuple[str, float | None, tuple[tuple[str, int, int], ...]]:
    mtimes = [path.stat().st_mtime if path.exists() else None for path in (MANUAL_SPLITS_PATH, SEGMENT_SELECTIONS_PATH)]
    source = Path(pipeline.SOURCE_DIR)
    files: list[tuple[str, int, int]] = []
    if source.is_dir():
        for root, _dirs, names in os.walk(source, onerror=lambda _error: None):
            for name in names:
                path = Path(root) / name
                if path.suffix.lower() in pipeline.ACCEPTED_AUDIO_EXTENSIONS:
                    try:
                        stat = path.stat()
                        files.append((str(path.relative_to(source)), stat.st_size, stat.st_mtime_ns))
                    except (OSError, ValueError):
                        continue
    files.sort()
    return (f"{source}|profile={pipeline.DETECTION_PROFILE_SIGNATURE}", tuple(mtimes), tuple(files))


def apply_saved_segment_selections(segments: list[pipeline.Segment], raw_songs: list[dict[str, Any]] | None = None) -> list[pipeline.Segment]:
    """Apply explicit timeline selections without reusing invalid windows."""
    payload = load_json(SEGMENT_SELECTIONS_PATH, {})
    saved = payload.get("segments", {}) if isinstance(payload, dict) else {}
    if not isinstance(saved, dict):
        return segments
    result = list(segments)
    raw_by_slot_id = {
        str(row.get("slot_id")): index
        for index, row in enumerate(raw_songs or [])
        if row.get("slot_id")
    }
    for raw_id, selection in saved.items():
        try:
            slot_id = selection.get("slot_id") if isinstance(selection, dict) else None
            index = raw_by_slot_id.get(str(slot_id), int(raw_id) - 1)
            start = float(selection["start_sec"])
            end = float(selection["end_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= index < len(result):
            continue
        if not math.isfinite(start) or not math.isfinite(end) or end - start < 0.1:
            continue
        original = result[index]
        result[index] = replace(
            original,
            start=start,
            end=end,
            core_start=start,
            core_end=end,
            nominal_end=end,
            boundary_source="manual-selection",
            boundary_validation="manual-selection",
            boundary_validation_reason="user timeline selection",
        )
    return result


def _waveform_identity() -> dict[str, Any]:
    """Cheap source identity used to invalidate the low-resolution waveform cache."""
    files = []
    source = Path(pipeline.SOURCE_DIR)
    if source.is_dir():
        for root, _dirs, names in os.walk(source, onerror=lambda _error: None):
            for name in names:
                path = Path(root) / name
                if path.suffix.lower() in pipeline.ACCEPTED_AUDIO_EXTENSIONS:
                    try:
                        stat = path.stat()
                        files.append({"name": str(path.relative_to(source)), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
                    except (OSError, ValueError):
                        continue
    files.sort(key=lambda item: item["name"])
    return {"source": str(source), "files": files, "version": 1, "points": 2400}


def slot_waveform(start_sec: float, end_sec: float, points: int = 1400) -> dict[str, Any]:
    """Build/cache only the selected slot waveform from the original stems."""
    identity = _waveform_identity()
    cache = load_json(WAVEFORM_CACHE_PATH, {})
    if not isinstance(cache, dict) or cache.get("identity") != identity:
        cache = {"identity": identity, "version": 2, "slots": {}}
    key = f"{start_sec:.3f}:{end_sec:.3f}:{int(points)}"
    cached = cache.get("slots", {}).get(key)
    if isinstance(cached, dict):
        return {**cached, "cached": True}
    state = ensure_pipeline_state()
    stems = state["stems"]
    start_sec = max(0.0, float(start_sec))
    end_sec = max(start_sec, float(end_sec))
    duration = end_sec - start_sec
    envelope = np.zeros(points, dtype=np.float32)
    activity = pipeline.load_detection_cache(stems)
    if activity:
        frame_seconds = pipeline.DETECTION_FRAME_SECONDS
        sampled_bins = np.zeros(points, dtype=bool)
        for stem in stems:
            values = activity.get(stem.path.name, np.array([], dtype=np.float32))
            positions = min(float(item.offset_seconds) for item in stems) + (np.arange(len(values)) + 0.5) * frame_seconds
            mask = (positions >= start_sec) & (positions <= end_sec)
            bins = np.clip(((positions[mask] - start_sec) / max(duration, 1e-9) * points).astype(int), 0, points - 1)
            np.maximum.at(envelope, bins, values[mask])
            sampled_bins[bins] = True
        # Retain true silent bins while interpolating presentation gaps.
        occupied = np.flatnonzero(sampled_bins)
        if len(occupied):
            envelope = np.interp(np.arange(points), occupied, envelope[occupied], left=0, right=0).astype(np.float32)
    else:
        # Bounded visual sampling on a cache miss, without allocating a whole song.
        for stem in stems:
            try:
                with sf.SoundFile(str(stem.path)) as audio_file:
                    for index in range(points):
                        seconds = start_sec + (index + 0.5) / points * duration - float(stem.offset_seconds)
                        frame = int(seconds * audio_file.samplerate)
                        if not 0 <= frame < audio_file.frames:
                            continue
                        audio_file.seek(frame)
                        block = audio_file.read(256, dtype="float32", always_2d=True)
                        if block.size:
                            envelope[index] = max(envelope[index], float(np.max(np.abs(block))))
            except (OSError, RuntimeError, ValueError):
                continue
    peak = float(np.max(envelope)) if len(envelope) else 0.0
    values = (envelope / peak).round(6).tolist() if peak > 0 else envelope.tolist()
    result = {"window_start_sec": start_sec, "window_end_sec": end_sec, "duration_sec": duration, "peaks": values, "cached": False}
    cache.setdefault("slots", {})[key] = result
    save_json_atomic(WAVEFORM_CACHE_PATH, cache)
    return result


def full_session_waveform(points: int = 1800) -> dict[str, Any]:
    """Return a cached low-resolution envelope for the complete session."""
    identity = _waveform_identity()
    cache = load_json(WAVEFORM_CACHE_PATH, {})
    if not isinstance(cache, dict) or cache.get("identity") != identity:
        cache = {"identity": identity, "version": 2, "slots": {}}
    state = ensure_pipeline_state()
    end_sec = max((float(stem.offset_seconds) + float(stem.timeline_duration) for stem in state["stems"]), default=0.0)
    existing = cache.get("global")
    if isinstance(existing, dict) and len(existing.get("peaks", [])) >= points and abs(float(existing.get("window_end_sec", 0)) - end_sec) < 0.01:
        return {**existing, "cached": True}
    activity = pipeline.load_detection_cache(state["stems"])
    detail_points = min(60000, max(points, int(math.ceil(end_sec / pipeline.DETECTION_FRAME_SECONDS)))) if activity else min(points, 512)
    existing = cache.get("global")
    if isinstance(existing, dict) and len(existing.get("peaks", [])) >= detail_points:
        return {**existing, "cached": True}
    result = slot_waveform(0.0, end_sec, detail_points)
    cache = load_json(WAVEFORM_CACHE_PATH, {})
    cache["global"] = result
    save_json_atomic(WAVEFORM_CACHE_PATH, cache)
    return result


def load_song_names() -> dict[str, Any]:
    payload = load_json(SONG_NAMES_PATH, {"songs": {}})
    if not isinstance(payload, dict):
        return {"songs": {}}
    payload.setdefault("songs", {})
    return payload


def save_song_names(payload: dict[str, Any]) -> None:
    payload.setdefault("songs", {})
    save_json(SONG_NAMES_PATH, payload)


def overlap_ratio(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    overlap = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    denom = max(1.0, min(a_end - a_start, b_end - b_start))
    return overlap / denom


def song_name_for(raw: dict[str, Any], names: dict[str, Any]) -> str:
    songs = names.get("songs", {}) if isinstance(names, dict) else {}
    direct = songs.get(str(raw["id"]))
    if isinstance(direct, dict) and direct.get("name"):
        try:
            score = overlap_ratio(float(raw["start"]), float(raw["end"]), float(direct["start"]), float(direct["end"]))
        except (KeyError, TypeError, ValueError):
            score = 1.0
        if score >= 0.75:
            return str(direct["name"])
    best_name = ""
    best_score = 0.0
    for value in songs.values():
        if not isinstance(value, dict) or not value.get("name"):
            continue
        try:
            score = overlap_ratio(float(raw["start"]), float(raw["end"]), float(value["start"]), float(value["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if score > best_score:
            best_score = score
            best_name = str(value["name"])
    return best_name if best_score >= 0.75 else ""


def apply_manual_splits(segments: list[pipeline.Segment], split_times: list[float]) -> list[pipeline.Segment]:
    if not split_times:
        return segments
    result: list[pipeline.Segment] = []
    for seg in segments:
        nominal_end = seg.nominal_end if seg.nominal_end is not None else seg.end
        inside = [t for t in split_times if seg.start + 1.0 < t < nominal_end - 1.0]
        if not inside:
            result.append(seg)
            continue
        bounds = [seg.start, *inside, nominal_end]
        for idx, (start, end) in enumerate(zip(bounds, bounds[1:])):
            is_last = idx == len(bounds) - 2
            result.append(
                pipeline.Segment(
                    start=start,
                    end=seg.end if is_last else end,
                    core_start=start,
                    core_end=end,
                    nominal_end=end,
                    mc_start=seg.mc_start if idx == 0 else None,
                    mc_end=seg.mc_end if idx == 0 else None,
                    next_mc_start=seg.next_mc_start if is_last else None,
                    next_mc_end=seg.next_mc_end if is_last else None,
                    post_mic_start=seg.post_mic_start if is_last else None,
                    post_mic_end=seg.post_mic_end if is_last else None,
                    boundary_source=seg.boundary_source,
                )
            )
    return result


def history_entry(segment_id: int, slot: int) -> dict[str, Any] | None:
    entries = disk_versions().get(int(segment_id), [])
    if not entries:
        history = load_json(HISTORY_PATH, {})
        raw_entries = history.get(str(segment_id), []) if isinstance(history, dict) else []
        if isinstance(raw_entries, list):
            entries = [
                entry
                for entry in raw_entries
                if isinstance(entry, dict)
                and entry.get("path")
                and Path(str(entry["path"])).exists()
            ]
            entries.sort(key=lambda item: (int(item.get("version", 0) or 0), str(item.get("created", ""))))
    if not entries:
        return None
    if slot == 0:
        return entries[-1]
    if slot == 1 and len(entries) >= 2:
        return entries[-2]
    for entry in entries:
        if int(entry.get("version", -1)) == int(slot):
            return entry
    return entries[-1]


def disk_versions() -> dict[int, list[dict[str, Any]]]:
    render_roots = [out_dir() / "app_renders", out_dir()]
    patterns = (
        re.compile(r"song_(\d+)_v(\d+)_(\d{8}_\d{6})\.mp3$"),
        re.compile(r"(\d+)\s+-\s+.+_v(\d+)_(\d{8}_\d{6})\.mp3$"),
    )
    versions: dict[int, list[dict[str, Any]]] = {}
    history = load_json(HISTORY_PATH, {})
    metadata_by_path: dict[str, dict[str, Any]] = {}
    if isinstance(history, dict):
        for entries in history.values():
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("path"):
                        metadata_by_path[str(Path(str(entry["path"])).resolve())] = entry
    seen: set[Path] = set()
    # Registered renders can live in any user-selected destination. Include
    # those exact files without recursively scanning external drives.
    if isinstance(history, dict):
        for raw_song, entries in history.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("path"):
                    continue
                try:
                    path = Path(str(entry["path"])).resolve()
                    song_no = int(entry.get("song_id") or raw_song)
                    if path in seen or path.suffix.lower() != ".mp3" or not path.is_file():
                        continue
                    registered = dict(entry)
                    registered.update(path=str(path), size=path.stat().st_size)
                    registered.setdefault("version", 1)
                    registered.setdefault("created", "")
                    registered.setdefault("title", path.stem)
                except (OSError, TypeError, ValueError):
                    continue
                seen.add(path)
                versions.setdefault(song_no, []).append(registered)
    for root in render_roots:
        if not root.exists():
            continue
        for path in root.glob("*.mp3"):
            resolved_path = path.resolve()
            if resolved_path in seen:
                continue
            seen.add(resolved_path)
            match = None
            for pattern in patterns:
                match = pattern.match(path.name)
                if match:
                    break
            if not match:
                continue
            song_no = int(match.group(1))
            version = int(match.group(2))
            stamp = match.group(3)
            resolved = str(resolved_path)
            title = path.stem
            suffix = f"_v{version}_{stamp}"
            if title.endswith(suffix):
                title = title[: -len(suffix)]
            entry = {
                "path": resolved,
                "version": version,
                "created": stamp,
                "title": title,
                "size": path.stat().st_size,
            }
            entry.update(
                {k: v for k, v in metadata_by_path.get(resolved, {}).items() if k not in {"path", "version", "created"}}
            )
            versions.setdefault(song_no, []).append(entry)
    for entries in versions.values():
        entries.sort(key=lambda item: (int(item["version"]), str(item["created"])))
    return versions


def ranged_file_response(path: Path, mimetype: str | None = None, as_attachment: bool = False) -> Response:
    if not path.exists() or not path.is_file():
        return Response(status=404)
    file_size = path.stat().st_size
    mimetype = mimetype or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    range_header = request.headers.get("Range")
    if as_attachment or not range_header:
        response = send_file(path, mimetype=mimetype, as_attachment=as_attachment, download_name=path.name, conditional=True)
        response.headers["Accept-Ranges"] = "bytes"
        return response

    match = re.match(r"bytes=(\d*)-(\d*)", range_header)
    if not match:
        return Response(status=416)
    start_text, end_text = match.groups()
    if start_text == "" and end_text == "":
        return Response(status=416)
    if start_text == "":
        length = int(end_text)
        start = max(file_size - length, 0)
        end = file_size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else file_size - 1
    end = min(end, file_size - 1)
    if start >= file_size or start > end:
        return Response(status=416, headers={"Content-Range": f"bytes */{file_size}"})

    length = end - start + 1
    with path.open("rb") as f:
        f.seek(start)
        data = f.read(length)
    response = Response(data, 206, mimetype=mimetype, direct_passthrough=True)
    response.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Length"] = str(length)
    return response


def ffmpeg_status() -> dict[str, str | bool]:
    path = pipeline.resolve_ffmpeg()
    return {
        "ok": bool(path),
        "path": path or "",
        "message": "" if path else "ffmpeg is missing. Install it with: brew install ffmpeg",
    }


def request_cancel() -> None:
    global cancel_requested
    cancel_requested = True
    lifecycle_log("cancel_requested", reason="explicit_cancel_or_shutdown")
    to_stop: list[tuple[str, subprocess.Popen[str]]] = []
    with state_lock:
        for job in jobs:
            if job.get("status") in {"queued", "running"}:
                lifecycle_log("job_cancel_mark", str(job.get("id")), previous_status=job.get("status"))
                job["status"] = "stopping" if job.get("status") == "running" else "cancelled"
                job["heartbeat"] = time.time()
                write_job_status(job)
        for job_id, proc in child_processes.items():
            if proc.poll() is None:
                to_stop.append((job_id, proc))
    for job_id, proc in to_stop:
        # Workers are isolated in their own process group so a render and any
        # helper it spawned stop together. Escalate after two seconds: the UI
        # must never wait for a multi-minute DSP checkpoint.
        try:
            signal_owned_process_group(proc)
            lifecycle_log("child_group_sigterm", job_id, child_pid=proc.pid)
        except ProcessLookupError:
            pass
        except OSError as exc:
            lifecycle_log("child_group_sigterm_failed", job_id, child_pid=proc.pid, error=str(exc))
            try:
                proc.terminate()
            except OSError:
                pass

        def escalate(target: subprocess.Popen[str] = proc, target_job_id: str = job_id) -> None:
            if target.poll() is None:
                try:
                    signal_owned_process_group(target, force=True)
                    lifecycle_log("child_group_sigkill", target_job_id, child_pid=target.pid)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    lifecycle_log("child_group_sigkill_failed", target_job_id, child_pid=target.pid, error=str(exc))

        threading.Timer(2.0, escalate).start()
    append_log("system", "Stop requested. The current mix process is being terminated.")


def _ensure_pipeline_state_impl() -> dict[str, Any]:
    global pipeline_state, pipeline_state_signature
    settings = load_settings()
    pipeline.AUDIO_SCAN_MODE = str(settings.get("audio_scan_mode") or "auto")
    configure_source_folder(settings.get("source_folder") or pipeline.SOURCE_DIR)
    source_config = load_source_config()
    pipeline.configure_detection_profile(source_config)
    signature = detection_state_signature()
    with state_lock:
        if not pipeline.DETECTION_RESCAN_MODE and pipeline_state is not None and pipeline_state_signature == signature:
            return pipeline_state
    snapshot = None if pipeline.DETECTION_RESCAN_MODE else load_detection_snapshot(signature)
    legacy_snapshot_reason = legacy_single_slot_snapshot_reason(snapshot)
    if legacy_snapshot_reason:
        lifecycle_log(
            "legacy_one_slot_snapshot_ignored",
            source_folder=str(pipeline.SOURCE_DIR),
            reason=legacy_snapshot_reason,
        )
        print("DETECTION SNAPSHOT IGNORED: " + legacy_snapshot_reason, flush=True)
        snapshot = None
    if snapshot is not None:
        snapshot_segments = snapshot.get("segments", []) if isinstance(snapshot, dict) else []
        invalid_snapshot_durations = []
        for item in snapshot_segments:
            start = float(item.get("start", 0.0)) if isinstance(item, dict) else float(getattr(item, "start", 0.0))
            end = float(item.get("end", 0.0)) if isinstance(item, dict) else float(getattr(item, "end", 0.0))
            duration = end - start
            if not pipeline.HARD_MIN_SONG_SECONDS <= duration <= pipeline.HARD_MAX_SONG_SECONDS:
                invalid_snapshot_durations.append(round(duration, 3))
        if invalid_snapshot_durations:
            print(
                "DETECTION SNAPSHOT: exposing invalid windows as needs_review "
                + json.dumps(invalid_snapshot_durations)
                + "; Select Cuts is required before render",
                flush=True,
            )
    if snapshot is not None:
        # Migrate legacy one-slot snapshots into an explicit review-only
        # candidate state. Older builds persisted the conservative fallback
        # without recording that Whisper had failed to produce a usable slot
        # topology, which made the UI present the whole jam as one valid song.
        snapshot_stems = snapshot.get("stems", []) if isinstance(snapshot, dict) else []
        snapshot_slots = snapshot.get("raw_songs", []) if isinstance(snapshot, dict) else []
        snapshot_duration = max(
            (
                float(item.get("offset_seconds", 0.0))
                + float(item.get("timeline_duration", item.get("duration", 0.0)))
                for item in snapshot_stems
                if isinstance(item, dict)
            ),
            default=0.0,
        )
        if (
            len(snapshot_slots) == 1
            and len(snapshot_stems) > 1
            and snapshot_duration > pipeline.HARD_MAX_SONG_SECONDS
        ):
            migration_reason = (
                f"Legacy snapshot contains one slot for {len(snapshot_stems)} parallel stems "
                f"spanning {snapshot_duration:.1f}s; full-session segmentation is still pending."
            )
            snapshot["candidate_pending"] = True
            snapshot["segmentation_status"] = "candidate_pending"
            calibration = dict(snapshot.get("detection_calibration") or {})
            calibration["candidate_pending"] = True
            calibration["session_segmentation_usable"] = False
            strategy = dict(calibration.get("strategy") or {})
            strategy.update({
                "id": "legacy_one_slot_candidate",
                "candidate_only": True,
                "session_segmentation_usable": False,
                "needs_review": True,
                "reason": migration_reason,
            })
            calibration["strategy"] = strategy
            snapshot["detection_calibration"] = calibration
            print("DETECTION SNAPSHOT MIGRATION: " + migration_reason, flush=True)
        snapshot["audio_scan"] = _ensure_snapshot_scan_report(snapshot)
        with state_lock:
            pipeline_state = snapshot
            pipeline_state_signature = signature
        if isinstance(snapshot.get("audio_scan"), dict):
            pipeline.AUDIO_SCAN_REPORT = dict(snapshot["audio_scan"])
        print(f"DETECTION SNAPSHOT: reused {DETECTION_STATE_PATH}; render/preview will not recalibrate", flush=True)
        return snapshot
    capture = JobLog("detect")
    with contextlib.redirect_stdout(capture):
        stems = pipeline.inspect_stems(pipeline.SOURCE_DIR)
        segments, _ = pipeline.detect_segments(stems)
        source_duration = max((stem.offset_seconds + stem.timeline_duration for stem in stems), default=0.0)
        if len(segments) == 1 and len(stems) > 1 and source_duration > pipeline.HARD_MAX_SONG_SECONDS:
            # A one-window fallback after Whisper failure is not a complete
            # session. Keep it diagnostic/review-only; never present it as a
            # valid source segmentation or use it as a redetect input.
            segments = [replace(
                segments[0],
                boundary_source="incomplete-source-segmentation",
                boundary_validation="needs_review",
                boundary_validation_reason=(
                    f"Incomplete source segmentation: {len(stems)} original stems span "
                    f"{source_duration:.1f}s but detection returned one slot"
                ),
            )]
            pipeline.DETECTION_STRATEGY = {
                **(pipeline.DETECTION_STRATEGY if isinstance(pipeline.DETECTION_STRATEGY, dict) else {}),
                "id": "incomplete_source_segmentation",
                "candidate_only": True,
                "session_segmentation_usable": False,
                "needs_review": True,
                "reason": segments[0].boundary_validation_reason,
            }
        # A redetect candidate is an automatic view of the original session.
        # Manual cuts remain a separate override layer and are reapplied only
        # after the user confirms the candidate replacement.
        if not pipeline.DETECTION_RESCAN_MODE:
            segments = apply_manual_splits(segments, load_manual_splits())
            segments = apply_saved_segment_selections(segments)
        # Public numbering is the stable session order. When the detector has
        # identified leading recorded material as SONG 0, expose that number
        # and continue 1, 2, 3... through the app and exported filenames.
        if segments and segments[0].assigned_song_number == 0:
            segments = [replace(segment, assigned_song_number=ordinal - 1) for ordinal, segment in enumerate(segments, 1)]
        pipeline.write_detection_outputs(out_dir(), segments)
    capture.flush()

    session_id = session_id_for_signature(signature)
    raw_songs = []
    strategy = getattr(pipeline, "DETECTION_STRATEGY", {})
    candidate_pending = bool(
        isinstance(strategy, dict)
        and (
            strategy.get("candidate_only")
            or strategy.get("session_segmentation_usable") is False
        )
    )
    boundary_audit = strategy.get("boundary_audit", []) if isinstance(strategy, dict) else []
    for index, segment in enumerate(segments, 1):
        nominal_end = segment.nominal_end if segment.nominal_end is not None else segment.end
        duration = nominal_end - segment.start
        raw_songs.append(
            {
                "id": index,
                "session_id": session_id,
                "slot_id": f"{session_id}:slot-{index:03d}",
                "source_start": float(segment.start),
                "source_end": float(segment.end),
                "manual_start": None,
                "manual_end": None,
                "revision": 0,
                "detection_version": getattr(pipeline, "SPEECH_TRANSCRIPTION_CACHE_VERSION", "unknown"),
                "proposal_status": "needs_review" if segment.boundary_validation == "needs_review" else ("manual" if segment.boundary_source == "manual-selection" else "auto"),
                "cut_reason": segment.speech_reason or segment.boundary_source,
                "whisper_confidence": float(segment.speech_confidence or 0.0),
                "start": segment.start,
                "end": segment.end,
                "duration": duration,
                "render_end": segment.end,
                "time": f"{fmt_time(segment.start)} - {fmt_time(nominal_end)}",
                "duration_text": fmt_time(duration),
                "spoken_song_number": segment.spoken_song_number,
                "assigned_song_number": segment.assigned_song_number if segment.assigned_song_number is not None else index,
                "spoken_last": bool(segment.spoken_last),
                "number_mismatch": bool((segment.assigned_song_number if segment.assigned_song_number is not None else index) != index),
                "introduction_status": segment.speech_reason or "suspicious: no verified introduction",
                "suspicious": duration < pipeline.HARD_MIN_SONG_SECONDS or duration > pipeline.HARD_MAX_SONG_SECONDS,
                "decision_evidence": {
                    "spoken_number": segment.spoken_song_number,
                    "introduction_found": bool(segment.speech_intro_text),
                    "introduction_text": segment.speech_intro_text,
                    "boundary_source": segment.boundary_source,
                    "duration_rule": (
                        "needs_review: >13 min / presented slot preserved; no automatic split" if duration > pipeline.HARD_MAX_SONG_SECONDS
                        else "needs_review: <8 min / presented slot preserved; Select Cuts required" if duration < pipeline.HARD_MIN_SONG_SECONDS
                        else "normal duration"
                    ),
                    "acoustic_gap": next((item for item in boundary_audit if item.get("actual_used") in {segment.start, segment.end}), None),
                },
                "segment": asdict(segment),
            }
        )
    durations = [float(song["duration"]) for song in raw_songs if float(song["duration"]) > 0]
    long_count = sum(duration > 15 * 60 for duration in durations)
    detected_count = len(raw_songs)
    session_end = max((stem.offset_seconds + stem.timeline_duration for stem in stems), default=0.0)
    expected_count = pipeline.EXPECTED_SLOT_COUNT
    invalid_duration_count = sum(not pipeline.HARD_MIN_SONG_SECONDS <= duration <= pipeline.HARD_MAX_SONG_SECONDS for duration in durations)
    needs_review_count = sum(
        isinstance(song.get("segment"), dict)
        and (
            song["segment"].get("boundary_validation") == "needs_review"
            or not pipeline.HARD_MIN_SONG_SECONDS <= float(song["duration"]) <= pipeline.HARD_MAX_SONG_SECONDS
        )
        for song in raw_songs
    )
    if expected_count is not None and detected_count != expected_count:
        calibration_warning = f"Found {detected_count} commentator-led slots — target is approximately {expected_count}; review the slot list before export."
        calibration_level = "warning"
    elif invalid_duration_count:
        calibration_warning = f"Found {detected_count} commentator-led slots, but {invalid_duration_count} violate the hard 8–13 minute duration bounds."
        calibration_level = "warning"
    else:
        calibration_warning = f"Found {detected_count} commentator-led slots — duration contract satisfied: {pipeline.HARD_MIN_SONG_SECONDS/60:.0f}–{pipeline.HARD_MAX_SONG_SECONDS/60:.0f} minutes each."
        calibration_level = "ok"
    stems_payload = [
        {
            "index": idx,
            "file": stem.path.name,
            "name": stem.name,
            "label": stem_display_label(stem),
            "role": stem.role,
            "base_level_db": pipeline.base_level_db(stem.role),
            "reverb_send_db": pipeline.reverb_send_level_db(stem.role),
            "delay_send_db": pipeline.delay_send_level_db(stem.role, 0.0),
        }
        for idx, stem in enumerate(stems)
    ]
    with state_lock:
        pipeline_state = {
            "stems": stems,
            "segments": segments,
            "raw_songs": raw_songs,
            "stem_info": stems_payload,
            "active_stems_by_song": {},
            "audio_scan": pipeline.audio_scan_report(),
            "segmentation_status": "candidate_pending" if candidate_pending else "ready",
            "candidate_pending": candidate_pending,
            "detection_calibration": {
                "count": detected_count,
                "expected_minimum": pipeline.EXPECTED_SLOT_COUNT,
                "candidate_pending": candidate_pending,
                "session_segmentation_usable": not candidate_pending,
                "expected_target": expected_count,
                "unit": "commentator-led slot",
                "ready_count": detected_count - needs_review_count,
                "needs_review_count": needs_review_count,
                "exported_count": 0,
                "pending_count": needs_review_count,
                "invalid_duration_count": invalid_duration_count,
                "longer_than_15_minutes": long_count,
                "level": calibration_level,
                "message": calibration_warning,
                "auto_selected": getattr(pipeline, "LAST_DETECTION_CALIBRATION", {}),
                "strategy": getattr(pipeline, "DETECTION_STRATEGY", {}),
            },
        }
        pipeline_state_signature = signature
        save_detection_snapshot(pipeline_state, signature)
        return pipeline_state


@source_edit_locked
def ensure_pipeline_state() -> dict[str, Any]:
    """Build detection once, even when multiple UI requests arrive together."""
    with pipeline_state_build_lock:
        return _ensure_pipeline_state_impl()


def rebuild_detection_state(job_id: str = "detect") -> dict[str, Any]:
    global pipeline_state, pipeline_state_signature
    is_redetect = job_id != "detect"
    settings = load_settings()
    configure_source_folder(settings.get("source_folder") or pipeline.SOURCE_DIR)
    had_previous_snapshot = DETECTION_STATE_PATH.exists()
    previous_count = 0
    if is_redetect and had_previous_snapshot:
        previous_snapshot = load_json(DETECTION_STATE_PATH, {})
        if isinstance(previous_snapshot, dict):
            previous_count = len(previous_snapshot.get("raw_songs", []) or [])
        shutil.copyfile(DETECTION_STATE_PATH, REDETECTION_BACKUP_PATH)
        archive = STATE_ROOT / "migrations" / ACTIVE_SOURCE_STATE_ROOT.name
        archive.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DETECTION_STATE_PATH, archive / f"detection_state-{time.time_ns()}.json")
    append_log(job_id, "Searching again from a clean deterministic detection pass; suspicious regions will be rescanned.")
    app_progress({
        "current_stage": "preparing session",
        "stage_detail": "Preparing full original session — loading source signature and source configuration",
        "progress": 3,
        "song_progress": 3,
        "phase_index": 0,
        "phase_total": 7,
        "heartbeat": time.time(),
    })
    pipeline.DETECTION_RESCAN_MODE = True
    with state_lock:
        pipeline_state = None
        pipeline_state_signature = None
    app_progress({"current_stage": "detecting songs", "stage_detail": "inspecting stems", "heartbeat": time.time(), "progress": 6})
    try:
        state = ensure_pipeline_state()
        candidate_count = len(state.get("raw_songs", []))
        if is_redetect:
            if candidate_count < 2 or state.get("candidate_pending"):
                incomplete_reason = (
                    f"Fresh detection is incomplete ({candidate_count} candidate slot(s)); "
                    f"the current session with {previous_count} slot(s) was preserved."
                )
                # Keep the one-slot result as a diagnostic candidate, but never
                # let it replace the usable session or turn a recoverable
                # Whisper miss into a worker crash.
                with state_lock:
                    pipeline_state = None
                    pipeline_state_signature = None
                append_log(job_id, "INCOMPLETE REDetect: " + incomplete_reason)
                app_progress({
                    "status": "pending_review",
                    "current_stage": "pending_review",
                    "stage_detail": incomplete_reason,
                    "warning": incomplete_reason,
                    "candidate_count": candidate_count,
                    "previous_count": previous_count,
                    "heartbeat": time.time(),
                    "progress": 100,
                    "song_progress": 100,
                })
                incomplete_state = dict(state)
                incomplete_state["_redetect_outcome"] = "incomplete"
                incomplete_state["_redetect_candidate_count"] = candidate_count
                incomplete_state["_redetect_previous_count"] = previous_count
                incomplete_state["_redetect_warning"] = incomplete_reason
                return incomplete_state
            with state_lock:
                pipeline_state = None
                pipeline_state_signature = None
            app_progress({
                "status": "pending_confirmation",
                "current_stage": "validating cuts",
                "stage_detail": f"Detected {candidate_count} slots; compare before replacing the current session",
                "candidate_count": candidate_count,
                "heartbeat": time.time(),
                "progress": 100,
                "song_progress": 100,
            })
    finally:
        pipeline.DETECTION_RESCAN_MODE = False
    if not is_redetect:
        app_progress({"current_stage": "detecting songs", "stage_detail": "detection state ready", "heartbeat": time.time(), "progress": 90})
    append_log(job_id, f"Found {len(state['raw_songs'])} commentator-led slots.")
    return state


def visible_songs(
    state: dict[str, Any],
    settings: dict[str, Any],
    history: dict[str, Any],
    disk: dict[int, list[dict[str, Any]]] | None = None,
    names: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    disk = disk or disk_versions()
    names = names or load_song_names()
    skipped = load_skipped_segments()
    songs = []
    visible_index = 1
    for raw in state["raw_songs"]:
        item = dict(raw)
        segment_id = int(item["id"])
        custom_name = song_name_for(item, names)
        item["custom_name"] = custom_name
        display_number = item.get("assigned_song_number") if item.get("assigned_song_number") is not None else item.get("spoken_song_number") or segment_id
        item["display_number"] = display_number
        item["display_title"] = custom_name or f"Slot {int(display_number):02d}"
        if item.get("number_mismatch"):
            item["number_warning"] = f"Absolute song number {int(display_number)} overrides detected position {segment_id}; inferred positions fill the gaps."
        user_skipped = segment_id in skipped
        item["skipped"] = bool(user_skipped)
        item["skip_reason"] = "skipped" if user_skipped else ""
        duration = float(item.get("duration") or 0.0)
        segment_payload = item.get("segment") if isinstance(item.get("segment"), dict) else {}
        boundary_review = (
            segment_payload.get("boundary_validation") == "needs_review"
            or str(segment_payload.get("boundary_source") or "").startswith("needs-review")
            or segment_payload.get("boundary_source") == "metadata-provisional"
        )
        item["needs_review"] = bool(boundary_review)
        item["render_valid"] = (
            not user_skipped
            and math.isfinite(duration) and duration >= 0.1
        )
        item["render_validation"] = (
            "valid"
            if item["render_valid"]
            else ("needs_review: unsafe or unconfirmed boundary" if boundary_review
            else ("skipped" if user_skipped else f"duration_out_of_range:{duration:.3f}s")
            )
        )
        if str(segment_id) in state.get("active_stems_by_song", {}):
            item["active_stems"] = state["active_stems_by_song"][str(segment_id)]
        if not item["skipped"]:
            item["index"] = int(display_number)
            visible_index += 1
        else:
            item["index"] = None
        renders = disk.get(int(item["index"]) if item["index"] is not None else -1, [])
        item["renders"] = renders[-2:]
        item["latest_render"] = renders[-1] if renders else None
        item["previous_render"] = renders[-2] if len(renders) > 1 else None
        if renders:
            latest = renders[-1]
            item["bpm"] = latest.get("bpm")
            item["key"] = latest.get("key")
        songs.append(item)
    return songs


def _loading_state(error: str = "", detection_job: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a useful state even when detection has not completed."""
    settings = load_settings()
    source = str(pipeline.SOURCE_DIR)
    scan = dict(pipeline.audio_scan_report() or {})
    if scan.get("source") != source:
        scan["source"] = source
    scan.setdefault("accepted", [])
    scan.setdefault("skipped", [])
    scan.setdefault("included_warnings", [])
    scan["status"] = "Error loading folder" if error else scan.get("status", "Scanning folder")
    scan["error"] = error
    stem_info = []
    for index, item in enumerate(scan.get("accepted", []), 1):
        file_name = str(item.get("file") or Path(str(item.get("path") or "")).name)
        stem_info.append({
            "index": index,
            "file": file_name,
            "name": Path(file_name).stem,
            "label": Path(file_name).stem,
            "role": pipeline.classify_role(Path(file_name).stem),
            "base_level_db": pipeline.base_level_db(pipeline.classify_role(Path(file_name).stem)),
            "reverb_send_db": 0.0,
            "delay_send_db": 0.0,
        })
    return {
        "songs": [],
        "transitions": [],
        "stems": stem_info,
        "overrides": load_json(OVERRIDES_PATH, {"songs": {}}),
        "settings": settings,
        "source_config": load_source_config(),
        "jobs": current_jobs(),
        "detection_job": detection_job,
        "output_dir": str(out_dir()),
        "average_render_seconds": 180.0,
        "ffmpeg": ffmpeg_status(),
        "source_folder": source,
        "audio_scan": scan,
        "detection_calibration": {"status": "not_available", "count": 0},
        "segmentation_status": "not_available",
        "last_error": error,
        "whisper": pipeline.LAST_WHISPER_STATUS,
        "build": runtime_build_metadata(),
    }


@source_edit_locked
def public_state() -> dict[str, Any]:
    global last_load_error
    try:
        state = ensure_pipeline_state()
        last_load_error = ""
    except Exception as exc:
        last_load_error = f"{type(exc).__name__}: {exc}"
        traceback_text = traceback.format_exc()
        lifecycle_log("state_load_failed", error=last_load_error, traceback=traceback_text)
        print("STATE_LOAD_FAILED " + last_load_error + "\n" + traceback_text, flush=True)
        return _loading_state(last_load_error)
    history = load_json(HISTORY_PATH, {})
    disk = disk_versions()
    names = load_song_names()
    overrides = load_json(OVERRIDES_PATH, {"songs": {}})
    settings = load_settings()
    render_seconds: list[float] = []
    for renders in history.values():
        for entry in renders:
            if entry.get("elapsed_seconds"):
                render_seconds.append(float(entry["elapsed_seconds"]))
    songs = visible_songs(state, settings, history, disk, names)
    raw_slots = state.get("raw_songs", [])
    session_ids = sorted({str(item.get("session_id")) for item in raw_slots if item.get("session_id")})
    slot_audit = {
        "session_id": session_ids[0] if len(session_ids) == 1 else None,
        "session_ids": session_ids,
        "state_slot_count": len(raw_slots),
        "backend_slot_count": len(songs),
        "slot_ids": [item.get("slot_id") for item in raw_slots],
        "visible_slot_count": len(songs),
        "integrity_warning": "Persisted state contains only one slot; full-session re-detection is required before replacement." if len(raw_slots) == 1 and not state.get("manual_editor_authoritative") else "",
    }
    source_stems = state.get("stems", [])
    source_duration = max((float(stem.offset_seconds) + float(stem.timeline_duration) for stem in source_stems), default=0.0)
    expected_slot_count = len(raw_slots) if state.get("manual_editor_authoritative") else (pipeline.KNOWN_SONG_COUNT or pipeline.EXPECTED_SLOT_COUNT)
    incomplete_source = not state.get("manual_editor_authoritative") and len(raw_slots) == 1 and len(source_stems) > 1 and source_duration > pipeline.HARD_MAX_SONG_SECONDS
    count_mismatch = expected_slot_count is not None and len(raw_slots) != expected_slot_count
    candidate_pending = bool(
        state.get("candidate_pending")
        or state.get("detection_calibration", {}).get("candidate_pending")
    )
    source_integrity = {
        "status": (
            "Candidate pending review"
            if candidate_pending
            else ("Incomplete source segmentation" if incomplete_source else ("Needs review: candidate count differs from configured target" if count_mismatch else "ok"))
        ),
        "original_wav_required": True,
        "stem_count": len(source_stems),
        "duration_sec": source_duration,
        "saved_slot_count": len(raw_slots),
        "expected_slot_count": expected_slot_count,
        "warning": (
            "Whisper completed, but the slot list is only a review candidate; it is not a valid single-song session. Original WAVs and Edit Cuts are required."
            if candidate_pending
            else ("Source state incomplete; Original WAVs required; no destructive changes made." if incomplete_source else (f"Current slots: {len(raw_slots)}; configured target: {expected_slot_count}. Review Whisper proposals before export." if count_mismatch else ""))
        ),
    }
    transitions = [
        {
            "id": int(song["id"]),
            "label": f"Song {str(song['index']).zfill(2)}" if song.get("index") is not None else f"Skipped {song['id']}",
            "start": song["segment"].get("mc_start"),
            "end": song["segment"].get("mc_end"),
            "duration": (
                float(song["segment"].get("mc_end")) - float(song["segment"].get("mc_start"))
                if song["segment"].get("mc_start") is not None and song["segment"].get("mc_end") is not None
                else None
            ),
            "duration_text": (
                fmt_time(float(song["segment"].get("mc_end")) - float(song["segment"].get("mc_start")))
                if song["segment"].get("mc_start") is not None and song["segment"].get("mc_end") is not None
                else ""
            ),
            "time": (
                f"{fmt_time(song['segment'].get('mc_start'))} - {fmt_time(song['segment'].get('mc_end'))}"
                if song["segment"].get("mc_start") is not None and song["segment"].get("mc_end") is not None
                else "No intro found"
            ),
        }
        for song in songs
    ]
    whisper_status = pipeline.LAST_WHISPER_STATUS
    strategy = state.get("detection_calibration", {}).get("strategy", {})
    if whisper_status.get("status") == "not_started" and isinstance(strategy, dict):
        strategy_whisper = strategy.get("whisper")
        if isinstance(strategy_whisper, dict):
            whisper_status = strategy_whisper
    return {
        "songs": songs,
        "transitions": transitions,
        "stems": state["stem_info"],
        "overrides": overrides,
        "settings": settings,
        "source_config": load_source_config(),
        "jobs": current_jobs(),
        "output_dir": str(out_dir()),
        "average_render_seconds": sum(render_seconds) / len(render_seconds) if render_seconds else 180.0,
        "ffmpeg": ffmpeg_status(),
        "source_folder": str(pipeline.SOURCE_DIR),
        "audio_scan": state.get("audio_scan") or pipeline.audio_scan_report(),
        "segmentation_status": "candidate_pending" if candidate_pending else "ready",
        "candidate_pending": candidate_pending,
        "last_error": last_load_error,
        "whisper": whisper_status,
        "detection_calibration": state.get("detection_calibration", {}),
        "slot_audit": slot_audit,
        "source_integrity": source_integrity,
        "matchering": reference_mastering_status(settings),
        "build": runtime_build_metadata(),
    }


def current_jobs() -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    with state_lock:
        source = [dict(job) for job in jobs[-30:]]
        live_job_ids = {str(job.get("id", "")) for job in source}
    for job in source:
        status = read_job_status_for_job(job)
        if os.environ.get("ZUCKER_DEBUG_JOBS") == "1":
            print(
                "JOB_STATUS_API_READ "
                + json.dumps(
                    {
                        "source": "memory-job",
                        "job_id": job.get("id"),
                        "memory": job_status_debug_payload(job),
                        "status_file": job_status_debug_payload(status),
                        "status_path": job.get("status_path") or str(job_status_path(str(job.get("id", "")))),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        if status:
            job.update(status)
        reconciled = reconcile_completed_job_from_disk(job)
        if reconciled:
            if os.environ.get("ZUCKER_DEBUG_JOBS") == "1":
                print(
                    "JOB_STATUS_RECONCILE "
                    + json.dumps({"job_id": job.get("id"), "before": job_status_debug_payload(job), "reconciled": reconciled}, sort_keys=True),
                    flush=True,
                )
            job.update(reconciled)
            write_job_status(job)
        seen.add(str(job.get("id", "")))
        merged.append(job)
    status_files = sorted(JOB_STATUS_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in status_files[:30]:
        status = load_json(path, None)
        if not isinstance(status, dict):
            continue
        job_id = str(status.get("id", ""))
        if not job_id or job_id in seen:
            continue
        if os.environ.get("ZUCKER_DEBUG_JOBS") == "1":
            print(
                "JOB_STATUS_API_READ "
                + json.dumps(
                    {
                        "source": "status-file-only",
                        "job_id": job_id,
                        "status_file": job_status_debug_payload(status),
                        "status_path": str(path),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        if status.get("status") in {"queued", "running", "stopping"} and job_id not in live_job_ids:
            last_seen = float(status.get("heartbeat") or status.get("updated_at") or path.stat().st_mtime)
            if time.time() - last_seen > ORPHANED_ACTIVE_JOB_SECONDS:
                status.update(
                    {
                        "status": "error",
                        "current_stage": "interrupted",
                        "stage_detail": "job process is no longer attached",
                        "error": "This job was left active in job_status but is not attached to a running worker. Start the render again if the finished file is not listed.",
                        "updated_at": time.time(),
                    }
                )
                save_json_atomic(path, status)
        reconciled = reconcile_completed_job_from_disk(status)
        if reconciled:
            if os.environ.get("ZUCKER_DEBUG_JOBS") == "1":
                print(
                    "JOB_STATUS_RECONCILE "
                    + json.dumps({"job_id": job_id, "before": job_status_debug_payload(status), "reconciled": reconciled}, sort_keys=True),
                    flush=True,
                )
            status.update(reconciled)
            save_json_atomic(path, status)
        merged.append(status)
        seen.add(job_id)
    merged.sort(key=lambda item: float(item.get("created") or item.get("updated_at") or 0.0))
    if os.environ.get("ZUCKER_DEBUG_JOBS") == "1":
        print(
            "JOB_STATUS_API_RETURN "
            + json.dumps([job_status_debug_payload(item) for item in merged[-30:]], sort_keys=True),
            flush=True,
        )
    return merged[-30:]


def job_status_debug_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    keys = (
        "id",
        "kind",
        "status",
        "current",
        "current_segment_id",
        "progress",
        "song_progress",
        "current_stage",
        "stage_detail",
        "done_count",
        "total_count",
        "started",
        "heartbeat",
        "updated_at",
        "status_path",
        "error",
        "pid",
        "ppid",
        "launch_pid",
        "child_pid",
        "exit_code",
        "termination",
        "started_at",
        "finished_at",
        "stderr_path",
        "lifecycle_log_path",
        "last_event",
        "last_event_at",
        "progress_updated_at",
        "memory_mb",
    )
    return {key: payload.get(key) for key in keys if key in payload}


def reconcile_completed_job_from_disk(job: dict[str, Any]) -> dict[str, Any] | None:
    if job.get("kind") not in {"render", "mix"}:
        return None
    if job.get("status") not in {"queued", "running", "stopping"}:
        return None
    songs = [int(song) for song in job.get("songs", []) if str(song).isdigit()]
    if not songs:
        return None
    created = float(job.get("created") or 0.0)
    disk = disk_versions()
    completed = 0
    for segment_id in songs:
        render_index = visible_index_for_segment(segment_id)
        entries = disk.get(int(render_index), [])
        found = False
        for entry in entries:
            path = Path(str(entry.get("path", "")))
            if path.exists() and path.stat().st_mtime >= max(0.0, created - 2.0):
                found = True
                break
        if found:
            completed += 1
    if completed < len(songs):
        return None
    now = time.time()
    return {
        "status": "done",
        "current": None,
        "progress": 100,
        "song_progress": 100,
        "current_stage": "finished",
        "stage_detail": "done from rendered file on disk",
        "heartbeat": now,
        "updated_at": now,
        "done_count": len(songs),
        "total_count": len(songs),
    }


def job_status_path(job_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(job_id))
    return JOB_STATUS_DIR / f"{safe}.json"


def write_job_status(job: dict[str, Any]) -> None:
    job_id = str(job.get("id") or "")
    if not job_id:
        return
    payload = dict(job)
    payload["updated_at"] = time.time()
    path = job_status_path(job_id)
    try:
        save_json_atomic(path, payload)
        try:
            readback = load_json(path, None)
        except Exception as read_exc:
            readback = {"readback_error": str(read_exc)}
        print(
            "JOB_STATUS_WRITE "
            + json.dumps(
                {
                    "job_id": job_id,
                    "path": str(path),
                    "payload": job_status_debug_payload(payload),
                    "file_after_write": job_status_debug_payload(readback) if isinstance(readback, dict) else readback,
                },
                sort_keys=True,
            ),
            flush=True,
        )
    except Exception as exc:
        # Status files are diagnostic/progress only; never fail audio work because of them.
        print("JOB_STATUS_WRITE_FAILED " + json.dumps({"job_id": job_id, "path": str(path), "error": str(exc)}, sort_keys=True), flush=True)


def read_job_status(job_id: str) -> dict[str, Any] | None:
    if not job_id:
        return None
    payload = load_json(job_status_path(job_id), None)
    return payload if isinstance(payload, dict) else None


def read_job_status_for_job(job: dict[str, Any]) -> dict[str, Any] | None:
    status_path = job.get("status_path")
    if status_path:
        payload = load_json(Path(str(status_path)), None)
        if isinstance(payload, dict):
            return payload
    return read_job_status(str(job.get("id", "")))


def worker_stderr_path(job_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(job_id))
    return STATE_ROOT / f"worker_{safe}.stderr.log"


def tail_text(path: Path, lines: int = 50) -> str:
    if not path.exists():
        return ""
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except Exception as exc:
        return f"Could not read stderr log {path}: {exc}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_batch_song_ids(requested: Any, visible_ids: set[int]) -> list[int]:
    """Validate the complete batch without silently dropping or reordering it."""
    if not isinstance(requested, list) or not requested:
        raise ValueError("Choose at least one song.")
    result: list[int] = []
    invalid: list[Any] = []
    duplicates: list[int] = []
    for raw in requested:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            invalid.append(raw)
            continue
        if value not in visible_ids:
            invalid.append(value)
        elif value in result:
            duplicates.append(value)
        else:
            result.append(value)
    if invalid:
        raise ValueError(f"Render batch contains unavailable song IDs: {invalid}")
    if duplicates:
        raise ValueError(f"Render batch contains duplicate song IDs: {duplicates}")
    if not result:
        raise ValueError("Choose at least one song.")
    return result


def compact_batch_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = (
        "index", "song_id", "segment_id", "child_job_id", "title", "start", "end",
        "duration", "lufs", "peak_dbfs", "master_true_peak_dbfs", "mp3_true_peak_dbfs",
        "file", "artifacts", "manifest_path", "error",
    )
    return [{key: row.get(key) for key in keys if key in row} for row in rows]


def render_batch_summary(requested: int, rows: list[dict], errors: list[dict], reviews: list[dict]) -> dict:
    completed = [row for row in rows if not row.get("error")]
    return {
        "requested": requested,
        "started": len(rows),
        "completed": len(completed),
        "failed": len(errors),
        "needs_review": len(reviews),
        "review_songs": [row.get("song") for row in reviews],
        "songs": compact_batch_rows(completed),
        "errors": errors,
    }


def enqueue(
    kind: str,
    songs: list[int],
    render_target: str | None = None,
    preview_effective_mix: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    global cancel_requested
    if not ffmpeg_status()["ok"]:
        raise RuntimeError("ffmpeg is missing. Install it with: brew install ffmpeg")
    with state_lock:
        for existing in jobs:
            if existing.get("kind") not in {"render", "mix"}:
                continue
            if existing.get("status") not in {"queued", "running", "stopping"}:
                continue
            existing_id = str(existing.get("id"))
            if existing.get("status") == "running":
                child = child_processes.get(existing_id)
                heartbeat = float(existing.get("heartbeat") or existing.get("started") or existing.get("created") or 0)
                if child is None and heartbeat and time.time() - heartbeat > ORPHANED_ACTIVE_JOB_SECONDS:
                    set_job(
                        existing,
                        status="error",
                        error="Render job was orphaned before a worker remained attached.",
                        current_stage="error",
                        stage_detail="orphaned render recovered",
                        finished_at=time.time(),
                    )
                    continue
            raise RuntimeError(f"Render already in progress ({existing_id}). Finish or cancel it before starting another render.")
        # A prior explicit Cancel can leave the process-wide flag set after
        # its job is already terminal. Do not let it cancel a new job.
        cancel_requested = False
    job_id = f"{int(time.time())}-{len(jobs) + 1}"
    job = {
        "id": job_id,
        "kind": kind,
        "songs": songs,
        "status_path": str(job_status_path(job_id)),
        "status": "queued",
        "progress": 0,
        "song_progress": 0,
        "created": time.time(),
        "current": None,
        "current_stage": "waiting",
        "stage_detail": "",
        "started": None,
        "heartbeat": None,
        "done_count": 0,
        "total_count": len(songs),
        "requested_song_ids": list(songs),
        "validated_song_ids": list(songs),
        "batch_summary": {
            "requested": len(songs),
            "started": 0,
            "completed": 0,
            "failed": 0,
            "songs": [],
        },
        "lifecycle_log_path": str(LIFECYCLE_LOG_PATH),
    }
    if render_target:
        job["render_target_dir"] = str(render_target)
    if preview_effective_mix and kind == "render" and len(songs) == 1:
        job["preview_effective_mix"] = preview_effective_mix
        print("PREVIEW_EFFECTIVE_MIX_JSON " + json.dumps(preview_effective_mix, sort_keys=True), flush=True)
    if extra:
        job.update(extra)
    if render_target:
        job["render_destination_trace"] = {
            "save_dialog_return": str(render_target),
            "job_stored_target": str(render_target),
        }
    with state_lock:
        jobs.append(job)
        write_job_status(job)
    job_queue.put(job)
    append_log(job["id"], f"Queued {kind}: songs {', '.join(str(i).zfill(2) for i in songs)}")
    if render_target:
        append_log(job["id"], "RENDER DESTINATION save_dialog_return=" + str(render_target))
        append_log(job["id"], "RENDER DESTINATION job_stored_target=" + str(job["render_target_dir"]))
    return job


def find_job(job_id: str) -> dict[str, Any] | None:
    with state_lock:
        for job in jobs:
            if job["id"] == job_id:
                return job
    return None


def set_job(job: dict[str, Any], **updates: Any) -> None:
    with state_lock:
        job.update(updates)
        if any(key in updates for key in ("status", "current_stage", "stage_detail", "current", "done_count", "error")):
            job["last_event_at"] = time.time()
            stage = job.get("current_stage") or job.get("status") or "working"
            detail = job.get("stage_detail") or ""
            job["last_event"] = f"{stage}: {detail}".rstrip(": ")
        try:
            job["memory_mb"] = round(float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / (1024 * 1024), 1)
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        write_job_status(job)


def record_render(
    song_index: int,
    row: dict[str, Any],
    elapsed_seconds: float,
    render_target_dir: str | None = None,
    job_id: str | None = None,
    child_job_id: str | None = None,
    segment_id: int | None = None,
    diagnostics_dir: str | Path | None = None,
) -> dict[str, Any]:
    source = Path(str(row["file"]))
    expected_duration = float(row.get("duration") or 0.0)
    if expected_duration <= 0:
        raise RuntimeError("render has no valid expected duration")
    if not render_target_dir:
        raise RuntimeError("Choose a destination folder before rendering.")
    render_dir = validate_render_target(render_target_dir)
    render_dir.mkdir(parents=True, exist_ok=True)
    if not job_id:
        raise RuntimeError("render is missing its job id")
    diagnostic_root = Path(diagnostics_dir) if diagnostics_dir else render_diagnostics_dir(job_id)
    diagnostic_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    history = load_json(HISTORY_PATH, {})
    entries = history.setdefault(str(song_index), [])
    existing_versions = disk_versions().get(int(song_index), [])
    version = max([int(entry.get("version", 0)) for entry in existing_versions] + [0]) + 1
    base_title = str(row.get("title") or f"{song_index:02d} - Mix")
    dest = render_dir / f"{pipeline.sanitize_filename(base_title)}_v{version}_{stamp}.mp3"
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"RENDER DESTINATION final_atomic_promotion_target={dest}", flush=True)
    pipeline.promote_render_file(source, dest, expected_duration)
    preserved = row.get("preserved_artifacts") if isinstance(row.get("preserved_artifacts"), dict) else {}
    artifact_paths: dict[str, str] = {"mp3": str(dest.resolve())}
    for key, suffix in (("premaster_wav", "_premaster.wav"), ("master_wav", "_master.wav")):
        source_artifact = Path(str(preserved.get(key, ""))) if preserved.get(key) else None
        if source_artifact is None and row.get("diagnostic_audio_retained") is False:
            continue
        if source_artifact is None or not source_artifact.exists():
            raise RuntimeError(f"Missing {key} artifact for song {song_index}.")
        artifact_dest = diagnostic_root / f"{dest.stem}{suffix}"
        pipeline.promote_render_file(source_artifact, artifact_dest, expected_duration)
        artifact_paths[key] = str(artifact_dest.resolve())
    entry = {
        "path": str(dest.resolve()),
        "version": version,
        "title": row.get("title"),
        "mix_source": row.get("mix_source", "Automatic mix"),
        "bpm": row.get("bpm"),
        "key": row.get("key"),
        "lufs": row.get("lufs"),
        "peak_dbfs": row.get("peak_dbfs"),
        "duration": row.get("duration"),
        "elapsed_seconds": elapsed_seconds,
        "created": stamp,
        "announcement_verification": row.get("announcement_verification"),
        "job_id": job_id,
        "child_job_id": child_job_id,
        "song_id": song_index,
        "segment_id": segment_id if segment_id is not None else song_index,
        "start_sec": row.get("start"),
        "end_sec": row.get("end"),
        "artifacts": artifact_paths,
    }
    entries.append(entry)
    history[str(song_index)] = entries[-8:]
    save_json(HISTORY_PATH, history)
    manifest_path = diagnostic_root / f"{dest.stem}_manifest.json"
    manifest = {
        "job_id": job_id,
        "child_job_id": child_job_id,
        "song_id": song_index,
        "segment_id": segment_id if segment_id is not None else song_index,
        "title": row.get("title"),
        "start_sec": row.get("start"),
        "end_sec": row.get("end"),
        "duration_sec": row.get("duration"),
        "commit": BUILD_METADATA.get("commit"),
        "effective_mix_hash": hashlib.sha256(json.dumps(row.get("effective_mix", {}), sort_keys=True, default=str).encode("utf-8")).hexdigest(),
        "effective_mix": row.get("effective_mix"),
        "artifacts": artifact_paths,
        "diagnostics_dir": str(diagnostic_root.resolve()),
        "final_destination_contains_only": "mp3",
        "hashes": {name: sha256_file(Path(path)) for name, path in artifact_paths.items()},
        "lufs": row.get("lufs"),
        "master_true_peak_dbfs": row.get("master_true_peak_dbfs"),
        "mp3_true_peak_dbfs": row.get("mp3_true_peak_dbfs"),
        "target_lufs": row.get("target_lufs"),
        "stems": row.get("stems", []),
        "stage_execution_counts": {
            "auto_mix": 1,
            "pre_gain": 1,
            "trim": 1,
            "master_gain": 1,
            "compressor": 1,
            "limiter": 1,
            "lufs_normalization": 1,
        },
    }
    save_json_atomic(manifest_path, manifest)
    entry["manifest_path"] = str(manifest_path.resolve())
    history[str(song_index)] = entries[-8:]
    save_json(HISTORY_PATH, history)
    return entry


def visible_index_for_segment(segment_id: int, state_snapshot: dict[str, Any] | None = None) -> int:
    state = state_snapshot if isinstance(state_snapshot, dict) else ensure_pipeline_state()
    songs = visible_songs(state, load_settings(), load_json(HISTORY_PATH, {}))
    for song in songs:
        if int(song["id"]) == int(segment_id):
            return int(song["index"] if song["index"] is not None else segment_id)
    return segment_id


def resolve_segment_for_split(state: dict[str, Any], segment_id: int, payload: dict[str, Any]) -> tuple[int, pipeline.Segment] | None:
    song_start = payload.get("song_start")
    song_end = payload.get("song_end")
    try:
        start_value = float(song_start)
        end_value = float(song_end)
    except (TypeError, ValueError):
        start_value = end_value = None
    if start_value is not None and end_value is not None:
        best: tuple[int, pipeline.Segment, float] | None = None
        for idx, seg in enumerate(state["segments"], 1):
            nominal_end = seg.nominal_end if seg.nominal_end is not None else seg.end
            overlap = max(0.0, min(nominal_end, end_value) - max(seg.start, start_value))
            if best is None or overlap > best[2]:
                best = (idx, seg, overlap)
        if best and best[2] > 1.0:
            return best[0], best[1]
    if 1 <= segment_id <= len(state["segments"]):
        return segment_id, state["segments"][segment_id - 1]
    songs = visible_songs(state, load_settings(), load_json(HISTORY_PATH, {}))
    for song in songs:
        if int(song.get("index") if song.get("index") is not None else -1) == int(segment_id):
            raw_id = int(song["id"])
            return raw_id, state["segments"][raw_id - 1]
    return None


def parse_split_offset_seconds(value: Any) -> float | None:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"\d+\.\d{2}", text):
            text = text.replace(".", ":")
        parts = text.split(":")
        try:
            if len(parts) == 2:
                minutes = float(parts[0])
                seconds = float(parts[1])
                if seconds < 0 or seconds >= 60:
                    return None
                return minutes * 60.0 + seconds
            if len(parts) == 3:
                hours = float(parts[0])
                minutes = float(parts[1])
                seconds = float(parts[2])
                if minutes < 0 or minutes >= 60 or seconds < 0 or seconds >= 60:
                    return None
                return hours * 3600.0 + minutes * 60.0 + seconds
        except ValueError:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def apply_overrides_for_song(
    segment_id: int,
    render_index: int,
    use_saved_mixes: bool = True,
    overrides_snapshot: dict[str, Any] | None = None,
    state_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = state_snapshot if isinstance(state_snapshot, dict) else load_render_state()
    disk_payload = overrides_snapshot if isinstance(overrides_snapshot, dict) else load_json(OVERRIDES_PATH, {"songs": {}})
    selected_payload = disk_payload if use_saved_mixes else {"songs": {}}
    prepared_mix = load_mix_plan(segment_id, overrides_snapshot=selected_payload, state_snapshot=state)
    if not isinstance(prepared_mix, dict):
        app_progress({"current_stage": "analyzing", "stage_detail": f"Preparing independent mix for song {segment_id}", "heartbeat": time.time()})
        analysis_finished = threading.Event()
        def analysis_heartbeat() -> None:
            while not analysis_finished.wait(2.0):
                app_progress({"heartbeat": time.time()})
        heartbeat_thread = threading.Thread(target=analysis_heartbeat, daemon=True, name="song-analysis-heartbeat")
        heartbeat_thread.start()
        try:
            prepared_mix = canonical_mix_params_for_song(segment_id, state_snapshot=state, overrides_snapshot=selected_payload)
        finally:
            analysis_finished.set()
            heartbeat_thread.join(timeout=0.2)
    prepared_mix = dict(prepared_mix)
    prepared_mix["analysis_song_id"] = int(segment_id)
    write_trace = disk_payload.get("_write_trace", {}) if isinstance(disk_payload, dict) else {}
    overrides = normalize_overrides(disk_payload)
    source_song = overrides.get("songs", {}).get(str(segment_id), {}) if use_saved_mixes else {}
    if 1 <= int(segment_id) <= len(state["raw_songs"]):
        custom_name = song_name_for(state["raw_songs"][int(segment_id) - 1], load_song_names())
        if custom_name:
            source_song = dict(source_song)
            source_song["title"] = custom_name
    render_overrides = dict(overrides)
    render_songs = dict(render_overrides.get("songs", {}))
    render_songs[str(render_index)] = source_song
    render_overrides["songs"] = render_songs
    pipeline.MIX_OVERRIDES = render_overrides
    verify_trace: dict[str, Any] = {}
    write_song_trace = write_trace.get(str(segment_id), {}) if isinstance(write_trace, dict) else {}
    changed = []
    stems = source_song.setdefault("stems", {}) if isinstance(source_song, dict) else {}
    for stem_name, prepared_stem in prepared_mix.get("stems", {}).items():
        settings = stems.setdefault(stem_name, {})
        for key in ("pan", "eq_low_cut_hz", "eq_mid_gain_db", "eq_air_gain_db", "space_enabled", "echo_enabled", "gate_enabled", "fx_enabled", "reverb_send_db", "delay_send_db"):
            settings.setdefault(key, prepared_stem.get(key))
        if not bool(settings.get("effects_user_confirmed", False)):
            for effect in ("space_enabled", "echo_enabled"):
                if effect in prepared_stem:
                    settings[effect] = prepared_stem[effect]
        settings["fader_db"] = prepared_stem.get("user_fader_db", 0.0)
        settings["gain_db"] = prepared_stem.get("user_gain_db", 0.0)
    if isinstance(stems, dict):
        for stem_name, settings in stems.items():
            if not isinstance(settings, dict):
                continue
            prepared_stem = (prepared_mix.get("stems", {}) or {}).get(stem_name, {})
            if isinstance(prepared_stem, dict) and not bool(settings.get("manual_makeup_gain_db", False)):
                prepared_gain = prepared_stem.get("makeup_gain_db")
                if prepared_gain is not None:
                    settings["auto_mix_gain_db"] = float(prepared_gain)
                    settings["makeup_gain_db"] = float(prepared_gain)
            worker_read = override_value_snapshot(settings)
            file_write = (
                override_value_snapshot(write_song_trace.get(stem_name, {}))
                if isinstance(write_song_trace, dict)
                else {"fader_db": 0.0, "mute": False, "solo": False}
            )
            verify_trace[str(stem_name)] = {
                "file_write": file_write,
                "worker_read": worker_read,
            }
            fader = float(worker_read["fader_db"])
            muted = bool(worker_read["mute"])
            if abs(fader) > 0.01 or muted:
                changed.append(f"{stem_name}: {fader:+.1f} dB" + (" muted" if muted else ""))
    print("OVERRIDE TRACE " + json.dumps({
        "song": int(segment_id),
        "render_index": int(render_index),
        "use_saved_mixes": bool(use_saved_mixes),
        "saved_override_values": {
            name: values.get("file_write", {}).get("fader_db")
            for name, values in verify_trace.items()
        },
        "worker_override_values": {
            name: values.get("worker_read", {}).get("fader_db")
            for name, values in verify_trace.items()
        },
    }, sort_keys=True), flush=True)
    pipeline.MIX_OVERRIDE_VERIFY_TRACE = {str(render_index): verify_trace}
    app_progress(
        {
            "stage_detail": "settings: " + (", ".join(changed[:8]) if changed else "no fader/mute overrides"),
            "heartbeat": time.time(),
        }
    )
    song = source_song
    if "target_lufs" in song:
        try:
            pipeline.TARGET_LUFS = float(song["target_lufs"])
        except (TypeError, ValueError):
            app_progress(
                {
                    "warning": f"Ignoring invalid loudness value for song {segment_id}.",
                    "heartbeat": time.time(),
                }
            )
            pipeline.TARGET_LUFS = DEFAULT_TARGET_LUFS
    else:
        intensity = str(song.get("mastering_intensity", "natural")).lower() if isinstance(song, dict) else "natural"
        pipeline.TARGET_LUFS = float(pipeline.MASTERING_TARGETS.get(intensity, pipeline.MASTERING_TARGETS["natural"]))
    pipeline.MASTERING_INTENSITY = str(song.get("mastering_intensity", "natural")).lower() if isinstance(song, dict) else "natural"
    reference = str(load_settings().get("matchering_reference") or "").strip()
    pipeline.MATCHERING_REFERENCE = Path(reference).expanduser() if reference else None
    pipeline.validate_mastering_reference(pipeline.MATCHERING_REFERENCE)
    return prepared_mix


def normalize_overrides(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"songs": {}}
    raw_songs = payload.get("songs", {})
    if not isinstance(raw_songs, dict):
        return {"songs": {}}
    clean_songs: dict[str, Any] = {}
    for song_id, song_payload in raw_songs.items():
        if not isinstance(song_payload, dict):
            continue
        clean_song: dict[str, Any] = {}
        for key in ("vocal_bus_db", "target_lufs", "master_db", "title", "mastering_intensity"):
            if key in song_payload:
                clean_song[key] = song_payload[key]
        for key in ("_seq", "_client_updated_at"):
            if key in song_payload:
                try:
                    clean_song[key] = float(song_payload[key])
                except (TypeError, ValueError):
                    continue
        raw_stems = song_payload.get("stems", {})
        clean_stems: dict[str, Any] = {}
        if isinstance(raw_stems, dict):
            for stem_name, stem_payload in raw_stems.items():
                if not isinstance(stem_payload, dict):
                    continue
                clean_stem: dict[str, Any] = {}
                for key in (
                    "makeup_gain_db",
                    "gain_db",
                    "fader_db",
                    "reverb_send_db",
                    "delay_send_db",
                    "pan",
                    "eq_low_cut_hz",
                    "eq_mid_gain_db",
                    "eq_air_gain_db",
                ):
                    if key in stem_payload:
                        try:
                            clean_stem[key] = float(stem_payload[key])
                        except (TypeError, ValueError):
                            continue
                for key in ("mute", "solo", "fx_enabled", "gate_enabled", "space_enabled", "echo_enabled", "effects_user_confirmed", "gate_user_confirmed", "manual_makeup_gain_db", "user_confirmed", "user_fader_confirmed"):
                    if isinstance(stem_payload.get(key), bool):
                        clean_stem[key] = stem_payload[key]
                if clean_stem:
                    clean_stems[str(stem_name)] = clean_stem
        if clean_stems:
            clean_song["stems"] = clean_stems
        clean_songs[str(song_id)] = clean_song
    return {"songs": clean_songs}


def child_command(job_path: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker", str(job_path)]
    return [sys.executable, "-u", str(PROJECT_ROOT / "mac_app.py"), "--worker", str(job_path)]


def app_progress(payload: dict[str, Any]) -> None:
    now = time.time()
    payload = json.loads(json.dumps(dict(payload), default=json_default))
    payload.setdefault("heartbeat", now)
    payload["progress_updated_at"] = now
    payload["last_event_at"] = now
    payload.setdefault(
        "last_event",
        f"{payload.get('current_stage', 'working')}: {payload.get('stage_detail', '')}".rstrip(": "),
    )
    try:
        payload["memory_mb"] = round(float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / (1024 * 1024), 1)
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    started = payload.get("started") or child_status_snapshot.get("started")
    if started:
        elapsed = max(0.0, now - float(started))
        payload.setdefault("elapsed_seconds", elapsed)
        progress = float(payload.get("progress") or child_status_snapshot.get("progress") or 0)
        if progress > 0:
            payload.setdefault("eta_seconds", elapsed * max(0.0, 100.0 - progress) / progress)
    if child_status_path is not None:
        child_status_snapshot.update(payload)
        child_status_snapshot["updated_at"] = time.time()
        try:
            save_json_atomic(child_status_path, child_status_snapshot)
            readback = load_json(child_status_path, None)
            print(
                "APP_PROGRESS_WRITE "
                + json.dumps(
                    {
                        "path": str(child_status_path),
                        "payload": job_status_debug_payload(child_status_snapshot),
                        "file_after_write": job_status_debug_payload(readback) if isinstance(readback, dict) else readback,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        except Exception as exc:
            print(f"APP_PROGRESS_DEBUG failed to write status file: {exc}", flush=True)
    print("APP_PROGRESS " + json.dumps(payload, sort_keys=True), flush=True)


def run_child_job(job_path: Path) -> int:
    payload = load_json(job_path, {})
    job_id = str(payload.get("id", "child"))
    lifecycle_log("child_run_enter", job_id, argv=sys.argv, job_path=str(job_path))
    try:
        signal.signal(signal.SIGTERM, lambda _signum, _frame: (_ for _ in ()).throw(SystemExit(143)))
        signal.signal(signal.SIGINT, lambda _signum, _frame: (_ for _ in ()).throw(KeyboardInterrupt()))
    except Exception as exc:
        lifecycle_log("child_signal_handler_install_failed", job_id, error=repr(exc))
    try:
        return _run_child_job(job_path)
    except BaseException as exc:
        # Compatibility guard for bundles built from the pre-2.1.34 detector.
        # That code raised after producing a diagnostic one-slot candidate;
        # this is an incomplete detection, not a worker crash.
        legacy_incomplete = (
            payload.get("kind") == "redetect"
            and isinstance(exc, RuntimeError)
            and "re-detect produced only" in str(exc).lower()
            and "slot" in str(exc).lower()
        )
        if legacy_incomplete:
            status_path = Path(str(payload.get("status_path") or job_status_path(str(payload.get("id", "child")))))
            now = time.time()
            match = re.search(r"onlys+(d+)s+slot", str(exc), re.IGNORECASE)
            candidate_count = int(match.group(1)) if match else 0
            snapshot = load_json(DETECTION_STATE_PATH, {})
            previous_count = len(snapshot.get("raw_songs", []) or []) if isinstance(snapshot, dict) else 0
            warning = (
                f"Fresh detection is incomplete ({candidate_count} candidate slot(s)); "
                f"the current session with {previous_count} slot(s) was preserved."
            )
            recovery = {
                **payload,
                "status": "pending_review",
                "error": None,
                "warning": warning,
                "candidate_count": candidate_count,
                "previous_count": previous_count,
                "current_stage": "pending_review",
                "stage_detail": warning,
                "progress": 100,
                "song_progress": 100,
                "done_count": 1,
                "total_count": 1,
                "exit_code": 0,
                "finished_at": now,
                "heartbeat": now,
                "updated_at": now,
                "termination": "recovered incomplete detection",
            }
            save_json_atomic(status_path, recovery)
            print("APP_PROGRESS " + json.dumps(recovery, sort_keys=True), flush=True)
            lifecycle_log(
                "legacy_incomplete_redetect_recovered",
                job_id,
                candidate_count=candidate_count,
                previous_count=previous_count,
                original_error=str(exc),
            )
            return 0
        status_path = Path(str(payload.get("status_path") or job_status_path(str(payload.get("id", "child")))))
        signal_code = exc.code if isinstance(exc, SystemExit) else None
        cancelled = isinstance(signal_code, int) and signal_code in {130, 143, 145, 146}
        error = f"{type(exc).__name__}: {exc}".strip()
        now = time.time()
        failure = {
            **payload,
            "status": "cancelled" if cancelled else "error",
            "error": None if cancelled else error,
            "current_stage": "cancelled" if cancelled else "error",
            "stage_detail": "Cancelled by user" if cancelled else "redetect failed; see details",
            "heartbeat": now,
            "updated_at": now,
        }
        try:
            save_json_atomic(status_path, failure)
        except Exception as status_exc:
            print(f"JOB_STATUS_WRITE_FAILED while reporting child failure: {status_exc}", flush=True)
        print("APP_PROGRESS " + json.dumps(failure, sort_keys=True), flush=True)
        if not cancelled:
            traceback.print_exc()
        lifecycle_log("child_exception", job_id, exception_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
        return 1


def _run_child_job(job_path: Path) -> int:
    global child_status_path, child_status_snapshot
    payload = load_json(job_path, {})
    status_path = payload.get("status_path")
    child_status_path = Path(str(status_path)) if status_path else job_status_path(str(payload.get("id", "child")))
    child_status_snapshot = dict(payload)
    child_status_snapshot["pid"] = os.getpid()
    child_status_snapshot["updated_at"] = time.time()
    save_json_atomic(child_status_path, child_status_snapshot)
    try:
        readback = load_json(child_status_path, None)
    except Exception as read_exc:
        readback = {"readback_error": str(read_exc)}
    print(
        "APP_PROGRESS_WRITE "
        + json.dumps(
            {
                "path": str(child_status_path),
                "payload": job_status_debug_payload(child_status_snapshot),
                "file_after_write": job_status_debug_payload(readback) if isinstance(readback, dict) else readback,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    print(f"APP_PROGRESS_DEBUG child status file {child_status_path}", flush=True)
    pipeline.PROGRESS_HOOK = app_progress
    # Render jobs must never enter optional Whisper analysis.  Source
    # registration may explicitly request it, but a normal render defaults to
    # the already-registered WAV metadata and persisted plan.
    pipeline.WHISPER_ALLOWED = bool(payload.get("allow_whisper", False))
    kind = payload.get("kind")
    if kind in {"mix", "render", "real-preview"}:
        pipeline.validate_mastering_reference(load_settings().get("matchering_reference"))
    songs = [int(x) for x in payload.get("songs", [])]
    job_id = str(payload.get("id", "child"))
    lifecycle_log("child_started", job_id, argv=sys.argv, job_path=str(job_path), status_path=str(child_status_path))
    if kind == "real-preview":
        state = load_render_state()
        song_id = int(payload["preview_song_id"])
        preview_start = float(payload["preview_start"])
        preview_duration = float(payload["preview_duration"])
        preview_segment = pipeline.Segment(start=preview_start, end=preview_start + preview_duration)
        app_progress({
            "status": "running",
            "current": song_id,
            "current_segment_id": song_id,
            "current_stage": "starting",
            "stage_detail": "preparing exact Python render",
            "progress": 1,
            "song_progress": 0,
            "started": time.time(),
            "heartbeat": time.time(),
        })
        prepared_plan = apply_overrides_for_song(song_id, song_id, state_snapshot=state)
        prepared_plan = {**prepared_plan, "preview_window": {"start_sec": preview_segment.start, "end_sec": preview_segment.end}}
        render_started = time.perf_counter()
        pipeline.render_segment(
            state["stems"],
            preview_segment,
            song_id,
            PREVIEW_DIR,
            output_path=Path(str(payload["preview_path"])),
            verify_announcement=False,
            prepared_plan=prepared_plan,
        )
        elapsed = time.perf_counter() - render_started
        app_progress({
            "status": "done",
            "current": None,
            "progress": 100,
            "song_progress": 100,
            "current_stage": "finished",
            "stage_detail": "exact real preview ready",
            "heartbeat": time.time(),
            "preview_url": f"/real-preview/{song_id}/{Path(str(payload['preview_path'])).name}",
            "preview_elapsed_seconds": elapsed,
            "preview_duration": preview_duration,
            "done_count": 1,
            "total_count": 1,
        })
        return 0
    if kind == "redetect":
        configure_source_folder(payload.get("source_folder") or pipeline.SOURCE_DIR)
        actual = source_job_identity(detection_state_signature())
        if payload.get("fingerprint") and payload["fingerprint"] != actual["fingerprint"]:
            raise RuntimeError("Source fingerprint changed after the job was queued; run Detect Songs again.")
        app_progress(
            {
                "status": "running",
                "current_stage": "preparing session",
                "stage_detail": "Preparing full original session — checking source signature",
                "started": time.time(),
                "heartbeat": time.time(),
                "progress": 2,
                "song_progress": 2,
                "phase_index": 0,
                "phase_total": 7,
            }
        )
        redetect_result = rebuild_detection_state(job_id)
        if redetect_result.get("_redetect_outcome") == "incomplete":
            warning = str(redetect_result.get("_redetect_warning") or "Fresh detection was incomplete; current session preserved.")
            app_progress({
                "status": "pending_review",
                "current_stage": "pending_review",
                "stage_detail": warning,
                "warning": warning,
                "candidate_count": redetect_result.get("_redetect_candidate_count", 0),
                "previous_count": redetect_result.get("_redetect_previous_count", 0),
                "heartbeat": time.time(),
                "progress": 100,
                "song_progress": 100,
                "done_count": 1,
            })
        else:
            app_progress({
                "status": "pending_confirmation",
                "current_stage": "validating cuts",
                "stage_detail": "New full-session detection ready for comparison; current session unchanged",
                "heartbeat": time.time(),
                "progress": 100,
                "song_progress": 100,
                "done_count": 1,
            })
        return 0

    state = load_render_state()
    # Audit warnings are diagnostic. Render the exact selected windows for
    # every requested song; never silently rewrite cuts or omit a song.
    selected_segments = [state["segments"][segment_id - 1] for segment_id in songs]
    selected_numbers = [visible_index_for_segment(segment_id, state) for segment_id in songs]
    try:
        final_timelines = pipeline.load_cached_timelines_or_die(state["stems"], "Final cut audit")
        final_cut_audit = pipeline.validate_final_render_boundaries(
            state["stems"], selected_segments, selected_numbers, final_timelines
        )
    except Exception as exc:
        final_cut_audit = [{"song": number, "safe": False, "needs_review": True,
                            "reason": f"Boundary audit unavailable: {type(exc).__name__}: {exc}"}
                           for number in selected_numbers]
    pipeline.DETECTION_STRATEGY["final_render_boundary_audit"] = final_cut_audit
    review_rows = [row for row in final_cut_audit if row.get("needs_review") or not row.get("safe")]
    accepted_rows = [row for row in final_cut_audit if row.get("safe")]
    corrected_segments = list(selected_segments)
    append_log(job_id, f"Final cut audit: {len(accepted_rows)} clear, {len(review_rows)} warnings; rendering all {len(songs)} selected songs unchanged")
    for row in review_rows:
        append_log(job_id, f"Rendering with warning: song {row.get('song')} — {row.get('reason')}")
    out_dir().mkdir(parents=True, exist_ok=True)
    diagnostic_job_dir = render_diagnostics_dir(job_id)
    append_log(job_id, "RENDER DIAGNOSTICS directory=" + str(diagnostic_job_dir))
    rows = []
    batch_errors: list[dict[str, Any]] = []
    for pos, (original_position, segment_id) in enumerate(
        enumerate(songs), 1
    ):
        child_job_id = f"{job_id}-song-{pos:02d}"
        render_index = visible_index_for_segment(segment_id, state)
        render_segment = corrected_segments[original_position]
        total_chunks = max(1, int(math.ceil(render_segment.duration / pipeline.RENDER_CHUNK_SECONDS)))
        app_progress(
            {
                "status": "running",
                "current": render_index,
                "current_segment_id": segment_id,
                "current_item": f"Song {pos} of {len(songs)} — {render_index:02d}",
                "child_job_id": child_job_id,
                "current_total_chunks": total_chunks,
                "current_stage": "starting",
                "stage_detail": "opening files",
                "started": time.time(),
                "heartbeat": time.time(),
                "done_count": pos - 1,
                "progress": int((pos - 1) / max(len(songs), 1) * 100),
                "song_progress": 0,
                "safe_count": len(accepted_rows),
                "needs_review_count": len(review_rows),
                "needs_review_songs": [row.get("song") for row in review_rows],
                "needs_review_details": [
                    {"song": row.get("song"), "reason": row.get("reason"), "stems": row.get("active_instruments", [])}
                    for row in review_rows
                ],
            }
        )
        print(f"Mixing Song {render_index:02d}", flush=True)
        started = time.time()
        render_tmp_dir = Path(tempfile.mkdtemp(prefix=f".zucker_render_{render_index:02d}_"))
        render_tmp_path = render_tmp_dir / f"song_{render_index:02d}.mp3"
        artifact_tmp_dir = render_tmp_dir / "artifacts"
        render_target_dir = str(payload.get("render_target_dir") or "")
        append_log(job_id, "RENDER DESTINATION worker_temp_file=" + str(render_tmp_path))
        append_log(job_id, "RENDER DESTINATION worker_target=" + render_target_dir)
        try:
            prepared_plan = apply_overrides_for_song(
                segment_id,
                render_index,
                bool(payload.get("use_saved_mixes", True)),
                payload.get("overrides_snapshot") if isinstance(payload.get("overrides_snapshot"), dict) else None,
                state,
            )
            row = pipeline.render_segment(
                state["stems"],
                render_segment,
                render_index,
                out_dir(),
                output_path=render_tmp_path,
                prepared_plan=prepared_plan,
                artifact_dir=artifact_tmp_dir,
                retain_diagnostic_audio=False,
            )
            elapsed = time.time() - started
            entry = record_render(
                render_index,
                row,
                elapsed,
                render_target_dir or None,
                job_id=job_id,
                child_job_id=child_job_id,
                segment_id=segment_id,
                diagnostics_dir=diagnostic_job_dir,
            )
            append_log(job_id, "RENDER DESTINATION final_atomic_promotion_target=" + str(entry["path"]))
            append_log(job_id, f"BATCH child_completed child_job_id={child_job_id} song_id={segment_id} position={pos}/{len(songs)}")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}".strip()
            batch_errors.append({"song": render_index, "segment_id": segment_id, "child_job_id": child_job_id, "error": error_text})
            append_log(job_id, f"Song {render_index:02d} failed; continuing batch: {error_text}")
            rows.append({"index": render_index, "error": error_text, "mix_source": pipeline.mix_source_label(pipeline.current_song_overrides(render_index))})
            app_progress(
                {
                    "progress": int(pos / max(len(songs), 1) * 100),
                    "song_progress": 100,
                    "current_stage": "error",
                    "stage_detail": f"Song {render_index:02d} failed; continuing",
                    "heartbeat": time.time(),
                    "done_count": pos,
                    "current_total_chunks": None,
                    "batch_errors": batch_errors,
                }
            )
            continue
        finally:
            shutil.rmtree(render_tmp_dir, ignore_errors=True)
        row["file"] = entry["path"]
        row["artifacts"] = entry.get("artifacts", {})
        row["manifest_path"] = entry.get("manifest_path")
        row["child_job_id"] = child_job_id
        rows.append(row)
        app_progress(
            {
                "progress": int(pos / max(len(songs), 1) * 100),
                "song_progress": 100,
                "current_stage": "finished",
                "stage_detail": f"Song {pos} of {len(songs)} complete",
                "heartbeat": time.time(),
                "done_count": pos,
                "current_total_chunks": None,
                "batch_summary": {
                    "requested": len(songs),
                    "started": pos,
                    "completed": sum(not row.get("error") for row in rows),
                    "failed": len(batch_errors),
                    "needs_review": len(review_rows),
                    "safe_songs": len(accepted_rows),
                    "review_songs": [row.get("song") for row in review_rows],
                    "songs": compact_batch_rows(rows),
                },
            }
        )
    if rows:
        pipeline.write_report(out_dir(), rows, corrected_segments)
    # A child render is successful only when every requested song produced a
    # promoted result.  Previously batch_errors were reported through the
    # progress file but the child still returned zero, so the parent worker
    # overwrote the visible error with `done` and the UI appeared idle.
    if batch_errors:
        failure_text = "; ".join(
            f"song {item.get('song')}: {item.get('error')}" for item in batch_errors
        )
        lifecycle_log(
            "render_failed",
            job_id,
            batch_errors=batch_errors,
            stage="render",
        )
        successful_rows = [row for row in rows if not row.get("error")]
        partial_summary = {
            "job_id": job_id,
            "status": "partial_failed",
            "batch_summary": render_batch_summary(len(songs), rows, batch_errors, review_rows),
            "commit": BUILD_METADATA.get("commit"),
        }
        summary_path = diagnostic_job_dir / f"ZuckerMixer_batch_{job_id}_summary.json"
        save_json_atomic(summary_path, partial_summary)
        app_progress(
            {
                "status": "partial_failed",
                "current": None,
                "progress": 100,
                "song_progress": 100,
                "current_stage": "partial_failed",
                "stage_detail": failure_text,
                "error": failure_text,
                "batch_errors": batch_errors,
                "heartbeat": time.time(),
                "done_count": sum(not row.get("error") for row in rows),
                "total_count": len(songs),
                "batch_summary": render_batch_summary(len(songs), rows, batch_errors, review_rows),
            }
        )
        return 1
    if len(rows) != len(songs):
        error_text = f"Render completed without all requested outputs ({len(rows)}/{len(songs)})."
        lifecycle_log("render_failed", job_id, error=error_text, stage="artifact_validation")
        app_progress({
            "status": "partial_failed",
            "current": None,
            "current_stage": "partial_failed",
            "stage_detail": error_text,
            "error": error_text,
            "heartbeat": time.time(),
            "done_count": sum(not row.get("error") for row in rows),
            "total_count": len(songs),
        })
        return 1
    lifecycle_log("render_completed", job_id, songs=len(rows), stage="artifact_validation")
    batch_summary = render_batch_summary(len(songs), rows, batch_errors, review_rows)
    summary_path = diagnostic_job_dir / f"ZuckerMixer_batch_{job_id}_summary.json"
    save_json_atomic(summary_path, {"job_id": job_id, "status": "done", "batch_summary": batch_summary, "commit": BUILD_METADATA.get("commit")})
    app_progress(
        {
            "status": "error" if batch_errors else "done",
            "current": None,
            "progress": 100,
            "song_progress": 100,
            "current_stage": "finished",
            "stage_detail": "done",
            "heartbeat": time.time(),
            "done_count": len(songs),
            "total_count": len(songs),
            "batch_errors": batch_errors,
            "batch_summary": batch_summary,
            "stage_detail": "done with errors" if batch_errors else "done",
        }
    )
    return 0


def handle_child_line(job: dict[str, Any], line: str) -> None:
    if line.startswith("APP_PROGRESS "):
        try:
            updates = json.loads(line[len("APP_PROGRESS "):])
        except json.JSONDecodeError:
            return
        set_job(job, **updates)
        return
    append_log(job["id"], line)


def worker() -> None:
    global cancel_requested, pipeline_state, pipeline_state_signature
    while True:
        job = job_queue.get()
        try:
            if job.get("status") in {"cancelled", "error"}:
                lifecycle_log("queued_job_skipped_cancelled", str(job.get("id")))
                continue
            set_job(job, status="running", progress=0, started=time.time(), heartbeat=time.time())
            with state_lock:
                active_job_by_id[job["id"]] = job
            job_path = STATE_ROOT / f"job_{job['id']}.json"
            stderr_path = worker_stderr_path(str(job["id"]))
            set_job(job, stderr_path=str(stderr_path), launch_pid=os.getpid(), lifecycle_log_path=str(LIFECYCLE_LOG_PATH))
            save_json(job_path, job)
            command = child_command(job_path)
            lifecycle_log("spawn_begin", job["id"], argv=command, job_path=str(job_path), stderr_path=str(stderr_path))
            with stderr_path.open("w", encoding="utf-8") as stderr_file:
                try:
                    proc = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=stderr_file,
                    text=True,
                    bufsize=1,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                    start_new_session=True,
                    )
                except BaseException as exc:
                    lifecycle_log("spawn_failed", job["id"], argv=command, exception_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
                    raise
                set_job(job, child_pid=proc.pid, spawned_at=time.time())
                lifecycle_log("spawned", job["id"], child_pid=proc.pid, argv=command)
                with state_lock:
                    child_processes[job["id"]] = proc
                if proc.stdout:
                    for line in proc.stdout:
                        handle_child_line(job, line.rstrip("\n"))
                code = proc.wait()
            termination = f"signal {-code}" if code < 0 else f"exit {code}"
            lifecycle_log("child_reaped", job["id"], child_pid=proc.pid, exit_code=code, termination=termination, stderr_bytes=stderr_path.stat().st_size if stderr_path.exists() else None)
            set_job(job, exit_code=code, termination=termination, finished_at=time.time(), stderr_path=str(stderr_path))
            with state_lock:
                child_processes.pop(job["id"], None)
            if job.get("kind") == "redetect" and code == 0 and job.get("status") != "pending_confirmation":
                with state_lock:
                    # The detector ran in the child process. Never leave the
                    # parent's old song list serving after that child has
                    # completed successfully; the next state request must
                    # rebuild from the fresh detection pass.
                    pipeline_state = None
                    pipeline_state_signature = None
                append_log(job["id"], "Fresh detection completed; invalidated the parent song list and boundaries.")
            cancelled = cancel_requested or job.get("status") in {"stopping", "cancelled"} or code in {130, 143, 145, 146}
            if cancelled:
                set_job(job, status="cancelled", error=None, current=None, current_stage="cancelled", stage_detail="Cancelled by user", progress=job.get("progress", 0))
                append_log(job["id"], "Mixing stopped.")
            elif code == 0 and job.get("kind") == "redetect" and job.get("status") == "pending_confirmation":
                set_job(
                    job,
                    status="pending_confirmation",
                    current=None,
                    progress=100,
                    song_progress=100,
                    current_stage="validating cuts",
                    stage_detail="New detection ready; confirmation required before replacing the current session",
                    heartbeat=time.time(),
                    done_count=1,
                    total_count=1,
                )
                append_log(job["id"], "New detection is pending confirmation; current slot list was preserved.")
            elif code == 0:
                song_count = len(job.get("songs", []))
                pending_review = job.get("status") == "pending_review" or int(job.get("needs_review_count", 0) or 0) > 0
                terminal_status = "pending_review" if pending_review else "done"
                lifecycle_log("render_completed", job["id"], parent_pid=os.getpid(), child_pid=proc.pid, songs=song_count, pending_review=pending_review)
                set_job(
                    job,
                    status=terminal_status,
                    current=None,
                    progress=100,
                    song_progress=100,
                    current_stage="finished",
                    stage_detail="pending review" if pending_review else "done",
                    heartbeat=time.time(),
                    done_count=job.get("done_count", song_count),
                    total_count=song_count,
                )
                append_log(job["id"], "Pending review" if pending_review else "Done")
            else:
                stderr_tail = tail_text(stderr_path)
                error = f"worker exited with code {code}"
                if stderr_tail:
                    error = f"{error}\n\n{stderr_tail}"
                lifecycle_log(
                    "render_failed",
                    job["id"],
                    parent_pid=os.getpid(),
                    child_pid=proc.pid,
                    exit_code=code,
                    termination=termination,
                    error=error,
                )
                partial = job.get("status") == "partial_failed" or bool(job.get("batch_errors"))
                final_status = "partial_failed" if partial else "error"
                set_job(
                    job,
                    status=final_status,
                    error=error,
                    current_stage=final_status,
                    stage_detail="see details",
                    heartbeat=time.time(),
                    stderr_path=str(stderr_path),
                )
                append_log(job["id"], f"Song failed: {error}")
        except Exception as exc:
            lifecycle_log("parent_worker_exception", str(job.get("id")), exception_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
            set_job(job, status="error", error=f"parent worker exception: {type(exc).__name__}: {exc}", current_stage="error", stage_detail="parent worker failure", heartbeat=time.time())
            append_log(job["id"], f"Song failed: {exc}")
        finally:
            cancel_requested = False
            with state_lock:
                active_job_by_id.pop(job["id"], None)
            job_queue.task_done()


@app.get("/")
def index() -> str:
    build = runtime_build_metadata()
    return render_template(
        "index.html",
        static_version=f"{build.get('app_version', 'dev')}-{build.get('source_revision', 'unbuilt')}-{build.get('app_js_sha256', '')[:16]}",
        build_version=build.get("app_version", "development"),
    )


@app.get("/favicon.ico")
def favicon() -> Response:
    return send_file(RESOURCE_ROOT / "static" / "zucker_logo_orange.png")


@app.get("/api/state")
@source_edit_locked
def api_state() -> Response:
    global pipeline_state, pipeline_state_signature
    # Loading/detection failures are application state, not a server crash.
    # Always return a JSON diagnostic so the UI can show the exact cause and
    # the files found before the failure.
    with state_lock:
        state_ready = pipeline_state is not None
    settings = load_settings()
    source = Path(settings.get("source_folder") or pipeline.SOURCE_DIR).expanduser().resolve()
    current_source = Path(pipeline.SOURCE_DIR).expanduser().resolve()
    # The first /api/state must initialize the same source-scoped paths as
    # Change Folder. Without this, a stale/default pipeline source can scan
    # one folder while the settings and cache belong to another.
    if source != current_source or str(pipeline.audio_scan_report().get("source") or "") != str(source):
        configure_source_folder(source)
        with state_lock:
            # Settings may point at a new source while the process still holds
            # the previous source's in-memory snapshot.
            pipeline_state = None
            pipeline_state_signature = None
        state_ready = False
    if not state_ready:
        # A valid snapshot can be hydrated synchronously and cheaply. Only
        # a cache miss starts the expensive detection worker; this keeps the
        # file list visible instead of making /api/state wait for Whisper.
        try:
            signature = detection_state_signature()
            snapshot = None if pipeline.DETECTION_RESCAN_MODE else load_detection_snapshot(signature)
            legacy_snapshot_reason = legacy_single_slot_snapshot_reason(snapshot)
            if legacy_snapshot_reason:
                lifecycle_log(
                    "legacy_one_slot_snapshot_ignored",
                    source_folder=str(source),
                    reason=legacy_snapshot_reason,
                )
                print("API STATE: ignoring legacy one-slot snapshot: " + legacy_snapshot_reason, flush=True)
                snapshot = None
        except Exception as exc:
            snapshot = None
            lifecycle_log("source_scan_signature_failed", error=f"{type(exc).__name__}: {exc}")
        if snapshot is not None:
            snapshot["audio_scan"] = _ensure_snapshot_scan_report(snapshot)
            with state_lock:
                pipeline_state = snapshot
                pipeline_state_signature = signature
            pipeline.AUDIO_SCAN_REPORT = dict(snapshot["audio_scan"])
            return jsonify(public_state()), 200
        if not pipeline.audio_scan_report().get("accepted") and source.is_dir():
            try:
                _paths, report = pipeline.scan_audio_files(source)
                pipeline.AUDIO_SCAN_REPORT = report
            except Exception as exc:
                return jsonify(_loading_state(f"{type(exc).__name__}: {exc}")), 200
        detection_job = _active_redetect_job(source)
        if detection_job is None:
            recent_incomplete = None
            for candidate in reversed(current_jobs()):
                if (
                    candidate.get("kind") == "redetect"
                    and str(Path(candidate.get("source_folder") or "").expanduser().resolve()) == str(source)
                    and candidate.get("status") in {"pending_review", "pending_confirmation"}
                    and candidate.get("fingerprint") == source_job_identity(signature)["fingerprint"]
                ):
                    recent_incomplete = candidate
                    break
            if recent_incomplete is not None:
                warning = str(
                    recent_incomplete.get("warning")
                    or recent_incomplete.get("stage_detail")
                    or "Detection produced no usable multi-song session."
                )
                return jsonify(_loading_state(warning, recent_incomplete)), 200
            # Initial loading uses acoustic detection. Optional Whisper analysis
            # is a separate explicit UI action and cannot block registration.
            detection_job = _queue_redetect_job(False)
        return jsonify(_loading_state(detection_job=detection_job)), 200
    return jsonify(public_state()), 200

@app.get("/api/cuts/<int:song_id>")
@source_edit_locked
def api_cuts(song_id: int) -> Response:
    """Return cached waveform plus automatic boundary evidence for Select Cuts."""
    state = ensure_pipeline_state()
    if song_id < 1 or song_id > len(state["segments"]):
        return jsonify({"error": "song not found"}), 404
    selected = state["segments"][song_id - 1]
    waveform_key = hashlib.sha256(json.dumps(_waveform_identity(), sort_keys=True).encode()).hexdigest()
    global_waveform = full_session_waveform() if request.args.get("waveform_key") != waveform_key else None
    # The canvas draws the global curve. Building an unused per-slot waveform
    # incurred thousands of random disk reads on every edited interval.
    duration = float(selected.end - selected.start)
    peaks = (global_waveform or {}).get("peaks", [])
    positions = np.linspace(float((global_waveform or {}).get("window_start_sec", 0)), float((global_waveform or {}).get("window_end_sec", 0)), len(peaks))
    selected_peaks = np.interp(np.linspace(float(selected.start), float(selected.end), min(1400, max(2, len(peaks)))), positions, peaks).tolist() if peaks else []
    waveform = {"window_start_sec": float(selected.start), "window_end_sec": float(selected.end), "duration_sec": duration, "peaks": selected_peaks, "cached": bool((global_waveform or {}).get("cached"))}
    markers = []
    for index, segment in enumerate(state["segments"], 1):
        markers.append({
            "song_id": index,
            "display_number": segment.assigned_song_number if segment.assigned_song_number is not None else index,
            "start_sec": float(segment.start),
            "end_sec": float(segment.end),
            "duration_sec": float(segment.duration),
            "comment_start_sec": segment.mc_start,
            "comment_end_sec": segment.mc_end,
            "speech_text": segment.speech_text,
            "boundary_source": segment.boundary_source,
            "confidence": float(segment.speech_confidence or 0.0),
            "status": "valid" if pipeline.HARD_MIN_SONG_SECONDS <= segment.duration <= pipeline.HARD_MAX_SONG_SECONDS else "needs_review",
        })
    return jsonify({
        "song_id": song_id,
        "waveform": waveform,
        "global_waveform": global_waveform,
        "waveform_key": waveform_key,
        "selection": {"start_sec": float(selected.start), "end_sec": float(selected.end)},
        "markers": markers,
        "bounds": {"min_sec": pipeline.HARD_MIN_SONG_SECONDS, "max_sec": pipeline.HARD_MAX_SONG_SECONDS},
        "slot": {
            "session_id": state.get("raw_songs", [{}])[song_id - 1].get("session_id"),
            "slot_id": state.get("raw_songs", [{}])[song_id - 1].get("slot_id"),
            "source_start": state.get("raw_songs", [{}])[song_id - 1].get("source_start"),
            "source_end": state.get("raw_songs", [{}])[song_id - 1].get("source_end"),
            "revision": state.get("raw_songs", [{}])[song_id - 1].get("revision", 0),
        },
    })


def cut_source_audio_blocks(stems: list, sample_rate: int, first_frame: int, frames: int):
    """Downmix requested timeline frames only; never decode/cache a complete song."""
    with contextlib.ExitStack() as stack:
        sources = []
        for stem in stems:
            try:
                sources.append((stem, stack.enter_context(sf.SoundFile(str(stem.path)))))
            except (OSError, RuntimeError):
                continue
        cursor = first_frame
        remaining = frames
        while remaining > 0:
            count = min(sample_rate, remaining)
            mix = np.zeros(count, dtype=np.float32)
            for stem, audio in sources:
                start_sec = cursor / sample_rate
                end_sec = (cursor + count) / sample_rate
                offset = float(stem.offset_seconds)
                lo = max(start_sec, offset)
                hi = min(end_sec, offset + audio.frames / audio.samplerate)
                if hi <= lo:
                    continue
                divisor = math.gcd(audio.samplerate, sample_rate)
                down = audio.samplerate // divisor
                margin = max(down, audio.samplerate // 50)
                first_native = max(0, int((lo - offset) * audio.samplerate) - margin)
                first_native -= first_native % down
                last_native = min(audio.frames, int(math.ceil((hi - offset) * audio.samplerate)) + margin)
                audio.seek(first_native)
                values = audio.read(last_native - first_native, dtype="float32", always_2d=True)
                if not len(values):
                    continue
                mono = audio_signal.resample_poly(np.mean(values, axis=1), sample_rate // divisor, down)
                at = int(round((offset + first_native / audio.samplerate - start_sec) * sample_rate))
                if at < 0:
                    mono = mono[-at:]; at = 0
                used = min(len(mono), count - at)
                if used > 0:
                    mix[at:at + used] += mono[:used]
            mix /= max(1.0, math.sqrt(len(sources)))
            yield (np.clip(mix, -1, 1) * 32767).astype("<i2").tobytes()
            cursor += count; remaining -= count


@app.get("/api/cuts/audio/<int:song_id>")
@source_edit_locked
def api_cut_audio(song_id: int) -> Response:
    state = ensure_pipeline_state()
    if song_id < 1 or song_id > len(state["segments"]):
        return Response(status=404)
    stems = list(state.get("stems", []))
    if not stems:
        return jsonify({"error": "No source stems available for playback."}), 404
    rate = 16000
    duration = max(float(stem.offset_seconds) + float(stem.timeline_duration) for stem in stems)
    frames = int(math.ceil(duration * rate)); data_size = frames * 2
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + data_size, b"WAVE", b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16, b"data", data_size)
    total = len(header) + data_size
    first, last = 0, total - 1
    value = request.headers.get("Range")
    if value:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
        if not match or not any(match.groups()):
            return Response(status=416, headers={"Content-Range": f"bytes */{total}"})
        lo, hi = match.groups()
        if lo:
            first = int(lo); last = min(int(hi), last) if hi else last
        else:
            first = max(0, total - int(hi))
        if first > last or first >= total:
            return Response(status=416, headers={"Content-Range": f"bytes */{total}"})
    def stream():
        if first < 44:
            yield header[first:min(44, last + 1)]
        low = max(first, 44) - 44
        high = last - 44 + 1
        if high <= low:
            return
        frame = low // 2; byte_offset = low % 2
        remaining = high - low
        for block in cut_source_audio_blocks(stems, rate, frame, math.ceil((byte_offset + remaining) / 2)):
            block = block[byte_offset:]; byte_offset = 0
            chunk = block[:remaining]
            yield chunk
            remaining -= len(chunk)
            if remaining <= 0:
                break
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(last - first + 1), "Cache-Control": "no-store"}
    if value:
        headers["Content-Range"] = f"bytes {first}-{last}/{total}"
    return Response(stream(), status=206 if value else 200, mimetype="audio/wav", headers=headers)


@app.post("/api/segment-selection/<int:song_id>")
@source_edit_locked
def api_segment_selection(song_id: int) -> Response:
    """Persist the human selection; automatic detection remains untouched."""
    state = ensure_pipeline_state()
    if song_id < 1 or song_id > len(state["segments"]):
        return jsonify({"error": "song not found"}), 404
    payload = request.get_json(force=True, silent=True) or {}
    if payload.get("source_folder") and Path(payload["source_folder"]).resolve() != Path(pipeline.SOURCE_DIR).resolve():
        return jsonify({"error": "Project changed; reopen Edit Cuts."}), 409
    try:
        start = float(payload["start_sec"])
        end = float(payload["end_sec"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "start_sec and end_sec must be numbers"}), 400
    duration = end - start
    if not math.isfinite(start) or not math.isfinite(end) or duration < 0.1:
        return jsonify({"error": "Selection must have a positive duration."}), 400
    source_duration = max((float(stem.offset_seconds) + float(stem.timeline_duration) for stem in state["stems"]), default=0.0)
    if start < 0 or end > source_duration:
        return jsonify({"error": "Selection is outside the source duration."}), 400
    history = load_json(EDITOR_HISTORY_PATH, {"undo": [], "redo": []})
    history.setdefault("undo", []).append(_editor_snapshot(state))
    history["undo"] = history["undo"][-20:]
    history["redo"] = []
    previous = state["segments"][song_id - 1]
    if song_id > 1:
        left = state["segments"][song_id - 2]
        shared = abs(left.end - previous.start) < 0.01
        if (shared and start <= left.start) or (not shared and start < left.end):
            return jsonify({"error": "Start overlaps the previous song."}), 400
    if song_id < len(state["segments"]):
        right = state["segments"][song_id]
        shared = abs(right.start - previous.end) < 0.01
        if (shared and end >= right.end) or (not shared and end > right.start):
            return jsonify({"error": "End overlaps the next song."}), 400
    saved = load_json(SEGMENT_SELECTIONS_PATH, {"version": 1, "segments": {}})
    if not isinstance(saved, dict):
        saved = {"version": 1, "segments": {}}
    saved["version"] = 1
    saved.setdefault("segments", {})[str(song_id)] = {
        "slot_id": (state.get("raw_songs", [{}])[song_id - 1].get("slot_id") if state.get("raw_songs") else None),
        "session_id": (state.get("raw_songs", [{}])[song_id - 1].get("session_id") if state.get("raw_songs") else None),
        "source_start": float(state.get("raw_songs", [{}])[song_id - 1].get("source_start", start)) if state.get("raw_songs") else start,
        "source_end": float(state.get("raw_songs", [{}])[song_id - 1].get("source_end", end)) if state.get("raw_songs") else end,
        "start_sec": start,
        "end_sec": end,
        "source": "manual",
        "revision": int(saved.get("revision", 0) or 0) + 1,
        "saved_at": time.time(),
    }
    saved["revision"] = int(saved.get("revision", 0) or 0) + 1
    save_json_atomic(SEGMENT_SELECTIONS_PATH, saved)
    # Update only this slot in the in-memory and persisted snapshot. Do not
    # invalidate the whole detection list: saving a manual cut is not a new
    # detection pass and must never collapse the session to one window.
    updated = replace(
        state["segments"][song_id - 1],
        start=start,
        end=end,
        core_start=start,
        core_end=end,
        nominal_end=end,
        boundary_source="manual-selection",
        boundary_validation="manual-selection",
        boundary_validation_reason="user timeline selection",
    )
    if song_id > 1 and abs(state["segments"][song_id - 2].end - previous.start) < 0.01:
        state["segments"][song_id - 2] = replace(state["segments"][song_id - 2], end=start, core_end=start, nominal_end=start)
    if song_id < len(state["segments"]) and abs(state["segments"][song_id].start - previous.end) < 0.01:
        state["segments"][song_id] = replace(state["segments"][song_id], start=end, core_start=end)
    state["segments"][song_id - 1] = updated
    if song_id <= len(state.get("raw_songs", [])):
        row = state["raw_songs"][song_id - 1]
        row["start"] = start; row["end"] = end; row["render_end"] = end
        row["duration"] = duration; row["duration_text"] = fmt_time(duration)
        row["manual_start"] = start; row["manual_end"] = end
        row["revision"] = int(row.get("revision", 0) or 0) + 1
        row["segment"] = asdict(updated)
    with state_lock:
        global pipeline_state, pipeline_state_signature
        pipeline_state = state
        pipeline_state_signature = detection_state_signature()
    persist_manual_editor_state(state)
    save_json_atomic(EDITOR_HISTORY_PATH, history)
    append_log("ui", f"Saved manual cut for song {song_id}: {start:.3f}-{end:.3f}s")
    return jsonify({"ok": True, "song_id": song_id, "start_sec": start, "end_sec": end, "duration_sec": duration, "source": "manual", **editor_result_state(state)})


def _editor_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps({"segments": [asdict(segment) for segment in state.get("segments", [])], "raw_songs": state.get("raw_songs", []),
        "overrides": load_json(OVERRIDES_PATH, {"songs": {}}),
        "names": load_json(SONG_NAMES_PATH, {"songs": {}}),
        "skipped": sorted(load_skipped_segments())}))


def _restore_editor_snapshot(state: dict[str, Any], snapshot: dict[str, Any]) -> None:
    segments = [pipeline.Segment(**item) for item in snapshot.get("segments", [])]
    state["segments"] = segments
    state["raw_songs"] = snapshot.get("raw_songs", [])
    if "overrides" in snapshot: save_json_atomic(OVERRIDES_PATH, snapshot["overrides"])
    if "names" in snapshot: save_json_atomic(SONG_NAMES_PATH, snapshot["names"])
    if "skipped" in snapshot: save_skipped_segments(snapshot["skipped"])


def editor_result_state(state: dict[str, Any]) -> dict[str, Any]:
    """The persisted timeline is authoritative immediately after every edit."""
    return {"slot_count": len(state["segments"]), "songs": visible_songs(state, load_settings(), load_json(HISTORY_PATH, {})),
            "overrides": load_json(OVERRIDES_PATH, {"songs": {}}),
            "segments": [{"start": s.start, "end": s.end} for s in state["segments"]]}


def persist_manual_editor_state(state: dict[str, Any]) -> None:
    state["manual_editor_authoritative"] = True
    state.setdefault("detection_calibration", {}).update(count=len(state["segments"]), expected_target=len(state["segments"]), candidate_pending=False, session_segmentation_usable=True)
    # Raw rows must reflect all shared-boundary edits, including neighbouring slots.
    for row, segment in zip(state.get("raw_songs", []), state["segments"]):
        row.update(start=segment.start, end=segment.end, render_end=segment.end,
                   duration=segment.duration, duration_text=fmt_time(segment.duration),
                   segment=asdict(segment), assigned_song_number=segment.assigned_song_number)
    if state.get("raw_songs") and all(hasattr(stem, "path") for stem in state.get("stems", [])):
        save_detection_snapshot(state, detection_state_signature())
        save_json_atomic(MANUAL_EDITOR_STATE_PATH, load_json(DETECTION_STATE_PATH, {}))


@app.post("/api/editor-cut-operation")
@source_edit_locked
def api_editor_cut_operation() -> Response:
    """Apply one explicit global-editor operation without running detection."""
    payload = request.get_json(force=True, silent=True) or {}
    if payload.get("source_folder") and Path(payload["source_folder"]).resolve() != Path(pipeline.SOURCE_DIR).resolve():
        return jsonify({"error": "Project changed; reopen Edit Cuts."}), 409
    operation = str(payload.get("operation") or "").strip().lower()
    state = ensure_pipeline_state()
    history = load_json(EDITOR_HISTORY_PATH, {"undo": [], "redo": []})
    if not isinstance(history, dict):
        history = {"undo": [], "redo": []}
    history.setdefault("undo", []); history.setdefault("redo", [])
    current = _editor_snapshot(state)
    row_hints: list[dict[str, Any] | None] | None = None
    if operation == "numbering":
        first = payload.get("first_song_number")
        if first not in (0, 1):
            return jsonify({"error": "first_song_number must be 0 or 1"}), 400
        history["undo"].append(current); history["redo"] = []
        state["segments"] = [replace(seg, assigned_song_number=i + first) for i, seg in enumerate(state["segments"])]
        row_hints = list(state.get("raw_songs", []))
    elif operation in {"undo", "redo"}:
        source = history["undo"] if operation == "undo" else history["redo"]
        target = history["redo"] if operation == "undo" else history["undo"]
        if not source:
            return jsonify({"error": f"Nothing to {operation}."}), 409
        target.append(current)
        _restore_editor_snapshot(state, source.pop())
    else:
        try:
            at = float(payload.get("at_sec", payload.get("old_sec")))
        except (TypeError, ValueError):
            return jsonify({"error": "at_sec is required for this editor operation."}), 400
        index = next((i for i, segment in enumerate(state["segments"]) if segment.start < at < segment.end), None)
        if operation == "add":
            source_end = max((float(stem.offset_seconds) + float(stem.timeline_duration) for stem in state["stems"]), default=0.0)
            if not math.isfinite(at) or at <= 0 or at >= source_end:
                return jsonify({"error": "Cut is outside the recording."}), 400
            if any(abs(at - edge) < 0.1 for seg in state["segments"] for edge in (seg.start, seg.end)):
                return jsonify({"error": "A cut already exists here."}), 409
            if index is None:
                index = next((i for i, seg in enumerate(state["segments"]) if seg.start > at), len(state["segments"]))
                low = state["segments"][index - 1].end if index else 0.0
                high = state["segments"][index].start if index < len(state["segments"]) else source_end
                state["segments"].insert(index, pipeline.Segment(low, high, boundary_source="manual-uncovered-region"))
                rows = list(state.get("raw_songs", [])); rows.insert(index, {}); state["raw_songs"] = rows
            segment = state["segments"][index]
            left = replace(segment, end=at, core_end=at, nominal_end=at, boundary_source="manual-add-cut", boundary_validation="manual-selection", boundary_validation_reason="user added cut")
            right = replace(segment, start=at, core_start=at, boundary_source="manual-add-cut", boundary_validation="manual-selection", boundary_validation_reason="user added cut")
            state["segments"][index:index + 1] = [left, right]
            row_hints = list(state.get("raw_songs", []))
            row_hints[index:index + 1] = [state.get("raw_songs", [])[index] if index < len(state.get("raw_songs", [])) else None, None]
        elif operation == "move":
            try:
                old_boundary = float(payload.get("old_sec"))
                new_boundary = float(payload.get("new_sec"))
            except (TypeError, ValueError):
                return jsonify({"error": "old_sec and new_sec are required to move a cut."}), 400
            pair = next((i for i in range(len(state["segments"]) - 1) if abs(state["segments"][i].end - old_boundary) <= 1.0), None)
            if pair is None:
                return jsonify({"error": "No slot boundary near the selected marker."}), 400
            left, right = state["segments"][pair], state["segments"][pair + 1]
            if not left.start + 0.1 <= new_boundary <= right.end - 0.1:
                return jsonify({"error": "The moved cut would create an empty slot."}), 400
            state["segments"][pair:pair + 2] = [
                replace(left, end=new_boundary, core_end=new_boundary, nominal_end=new_boundary, boundary_source="manual-move-cut", boundary_validation="manual-selection", boundary_validation_reason="user moved shared cut"),
                replace(right, start=new_boundary, core_start=new_boundary, boundary_source="manual-move-cut", boundary_validation="manual-selection", boundary_validation_reason="user moved shared cut"),
            ]
            row_hints = list(state.get("raw_songs", []))
        elif operation in {"delete", "merge"}:
            boundary = at
            pair = next((i for i in range(len(state["segments"]) - 1) if abs(state["segments"][i].end - boundary) <= 1.0), None)
            if pair is None:
                return jsonify({"error": "No slot boundary near the playhead."}), 400
            left, right = state["segments"][pair], state["segments"][pair + 1]
            state["segments"][pair:pair + 2] = [replace(left, end=right.end, core_end=right.end, nominal_end=right.end, boundary_source="manual-merge", boundary_validation="manual-selection", boundary_validation_reason="user merged adjacent slots")]
        else:
            return jsonify({"error": f"Unknown editor operation: {operation}"}), 400
        history["undo"].append(current)
        history["undo"] = history["undo"][-20:]
        history["redo"] = []

    # Rebuild only the editor rows. Match unchanged rows by their original
    # interval, never by ordinal, so inserting a cut cannot rename every
    # following slot.
    old_rows = state.get("raw_songs", [])
    session_id = next((row.get("session_id") for row in old_rows if row.get("session_id")), session_id_for_signature(detection_state_signature()))
    rows = []
    for ordinal, segment in enumerate(state["segments"], 1):
        old = row_hints[ordinal - 1] if row_hints is not None and ordinal - 1 < len(row_hints) else next((candidate for candidate in old_rows if abs(float(candidate.get("start", -999999)) - segment.start) <= 0.01 and abs(float(candidate.get("end", -999999)) - segment.end) <= 0.01), None)
        row = dict(old or {})
        row.update({"id": ordinal, "session_id": row.get("session_id") or session_id, "slot_id": row.get("slot_id") or f"{session_id}:manual-{hashlib.sha256(f'{segment.start:.6f}:{segment.end:.6f}:{time.time_ns()}'.encode()).hexdigest()[:16]}", "start": segment.start, "end": segment.end, "source_start": row.get("source_start", segment.start), "source_end": row.get("source_end", segment.end), "duration": segment.duration, "duration_text": fmt_time(segment.duration), "render_end": segment.end, "segment": asdict(segment), "proposal_status": "manual", "revision": int(row.get("revision", 0) or 0) + 1})
        rows.append(row)
    if operation not in {"undo", "redo", "numbering"}:
        new_ids = {row.get("slot_id"): str(row["id"]) for row in rows if row.get("slot_id")}
        mapping = {str(row.get("id")): new_ids[row["slot_id"]] for row in current.get("raw_songs", []) if row.get("slot_id") in new_ids}
        for path, key in ((OVERRIDES_PATH, "overrides"), (SONG_NAMES_PATH, "names")):
            stored = json.loads(json.dumps(current.get(key, {"songs": {}})))
            stored["songs"] = {mapping[old]: value for old, value in stored.get("songs", {}).items() if old in mapping}
            save_json_atomic(path, stored)
        save_skipped_segments([int(mapping[str(old)]) for old in current.get("skipped", []) if str(old) in mapping])
    if operation in {"add", "delete", "merge", "move"}:
        first_number = 0 if current.get("segments", [{}])[0].get("assigned_song_number") == 0 else 1
        state["segments"] = [replace(seg, assigned_song_number=i + first_number) for i, seg in enumerate(state["segments"])]
    state["raw_songs"] = rows
    with state_lock:
        global pipeline_state, pipeline_state_signature
        pipeline_state = state
        pipeline_state_signature = detection_state_signature()
    persist_manual_editor_state(state)
    save_json_atomic(EDITOR_HISTORY_PATH, history)
    append_log("ui", f"Global editor operation {operation} applied at {payload.get('at_sec')}")
    return jsonify({"ok": True, "operation": operation, **editor_result_state(state)})


@app.post("/api/overrides")
@source_edit_locked
def api_overrides() -> Response:
    payload = request.get_json(force=True, silent=True) or {"songs": {}}
    if payload.get("source_folder") and Path(payload["source_folder"]).resolve() != Path(pipeline.SOURCE_DIR).resolve():
        return jsonify({"error": "Project changed; reopen Fine Tune."}), 409
    payload = normalize_overrides(payload)
    current = normalize_overrides(load_json(OVERRIDES_PATH, {"songs": {}}))
    current_songs = current.setdefault("songs", {})
    accepted: dict[str, float | None] = {}
    ignored: dict[str, dict[str, float | None]] = {}
    for song_id, song_payload in payload.get("songs", {}).items():
        if not isinstance(song_payload, dict):
            continue
        current_song = current_songs.setdefault(str(song_id), {})
        incoming_seq = song_payload.get("_seq")
        current_seq = current_song.get("_seq") if isinstance(current_song, dict) else None
        if current_seq is not None and incoming_seq is None:
            ignored[str(song_id)] = {"incoming_seq": None, "current_seq": float(current_seq)}
            continue
        if current_seq is not None and incoming_seq is not None and float(incoming_seq) < float(current_seq):
            ignored[str(song_id)] = {"incoming_seq": float(incoming_seq), "current_seq": float(current_seq)}
            continue
        for key, value in song_payload.items():
            if key == "stems" or not isinstance(current_song, dict):
                continue
            current_song[key] = value
        current_stems = current_song.setdefault("stems", {})
        incoming_stems = song_payload.get("stems", {})
        if isinstance(incoming_stems, dict):
            for stem_name, stem_payload in incoming_stems.items():
                if not isinstance(stem_payload, dict):
                    continue
                current_stems.setdefault(str(stem_name), {}).update(stem_payload)
        accepted[str(song_id)] = float(incoming_seq) if incoming_seq is not None else None
    current["_write_trace"] = override_write_trace(current)
    save_json(OVERRIDES_PATH, current)
    active = []
    for song_id, song in current.get("songs", {}).items():
        stems = song.get("stems", {}) if isinstance(song, dict) else {}
        touched = [
            name
            for name, settings in stems.items()
            if isinstance(settings, dict)
            and (
                abs(float(settings.get("gain_db", 0.0) or 0.0)) > 0.01
                or abs(float(settings.get("fader_db", 0.0) or 0.0)) > 0.01
                or bool(settings.get("mute"))
                or bool(settings.get("solo"))
            )
        ]
        if touched:
            active.append(f"song {song_id}: {len(touched)} stem overrides")
    append_log("ui", "Saved mix settings: " + ("; ".join(active) if active else "no fader/mute changes"))
    return jsonify({"ok": True, "accepted": accepted, "ignored": ignored})


@app.get("/api/overrides")
def api_get_overrides() -> Response:
    """Return the authoritative persisted override snapshot for Render."""
    return jsonify(normalize_overrides(load_json(OVERRIDES_PATH, {"songs": {}})))


@app.post("/api/analyze-mix/<int:segment_id>")
def api_analyze_mix(segment_id: int) -> Response:
    """Explicit Analyze action: create the frozen per-song DSP snapshot."""
    try:
        plan = canonical_mix_params_for_song(segment_id)
    except IndexError:
        return jsonify({"error": "song not found"}), 404
    except Exception as exc:
        append_log("ui", f"Analyze failed for song {segment_id}: {type(exc).__name__}: {exc}")
        return jsonify({"error": f"Analyze failed: {type(exc).__name__}: {exc}"}), 500
    return jsonify({
        "ok": True,
        "song_id": segment_id,
        "effective_dsp_plan_hash": plan.get("effective_dsp_plan_hash"),
        "analysis_cache_path": plan.get("analysis_cache_path"),
    })


@app.get("/api/mix-plan-status/<int:segment_id>")
def api_mix_plan_status(segment_id: int) -> Response:
    """Report whether the frozen per-song DSP plan is usable for Render.

    This is deliberately read-only: Render may ask whether preparation is
    required. A worker prepares a missing per-song analysis before mixing.
    """
    try:
        state = load_render_state()
    except RuntimeError as exc:
        return jsonify({"valid": False, "song_id": segment_id, "reason": str(exc)}), 200
    if not 1 <= int(segment_id) <= len(state.get("segments", [])):
        return jsonify({"valid": False, "song_id": segment_id, "reason": "song not found"}), 200
    plan = load_mix_plan(segment_id, state_snapshot=state)
    if not isinstance(plan, dict):
        return jsonify({
            "valid": False,
            "song_id": segment_id,
            "reason": "Analyze required: the Auto-Mix plan is missing or stale.",
        }), 200
    return jsonify({
        "valid": True,
        "song_id": segment_id,
        "effective_dsp_plan_hash": plan.get("effective_dsp_plan_hash"),
    })


@app.post("/api/settings")
@source_edit_locked
def api_settings() -> Response:
    global pipeline_state, pipeline_state_signature, last_load_error
    payload = request.get_json(force=True, silent=True) or {}
    settings = load_settings()
    if "skipped_segments" in payload:
        try:
            save_skipped_segments(payload.get("skipped_segments", []))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        settings["skipped_segments"] = sorted(load_skipped_segments())
    if "last_render_dir" in payload:
        path = Path(str(payload.get("last_render_dir") or "")).expanduser()
        if str(path):
            settings["last_render_dir"] = str(path)
    if "source_folder" in payload:
        raw_source = str(payload.get("source_folder") or "").strip()
        source = Path(raw_source).expanduser().resolve() if raw_source else None
        if source is None or not source.is_dir():
            return jsonify({"error": "source folder not found"}), 400
        configure_source_folder(source)
        last_load_error = ""
        settings["source_folder"] = str(source)
        with state_lock:
            pipeline_state = None
            pipeline_state_signature = None
    source_config = load_source_config()
    source_config_changed = False
    if "audio_scan_mode" in payload:
        mode = str(payload.get("audio_scan_mode") or "auto")
        if mode not in {"auto", "aligned_only", "all"}:
            return jsonify({"error": "invalid audio scan mode"}), 400
        settings["audio_scan_mode"] = mode
        pipeline.AUDIO_SCAN_MODE = mode
        last_load_error = ""
        with state_lock:
            pipeline_state = None
            pipeline_state_signature = None
    if "known_song_count" in payload or "expected_song_count" in payload:
        raw_count = payload.get("expected_song_count", payload.get("known_song_count"))
        if raw_count in (None, ""):
            source_config["expected_song_count"] = None
        else:
            try:
                count = int(raw_count)
            except (TypeError, ValueError):
                return jsonify({"error": "expected song count must be a whole number"}), 400
            if count < 1:
                return jsonify({"error": "expected song count must be positive"}), 400
            source_config["expected_song_count"] = count
        source_config_changed = True
    for key in ("min_song_seconds", "max_song_seconds", "structural_min_song_seconds", "structural_max_song_seconds", "structural_close_song_seconds", "min_final_song_seconds", "suspicious_short_song_seconds", "suspicious_long_song_seconds", "whisper_timeout_seconds"):
        if key not in payload:
            continue
        try:
            value = float(payload[key])
        except (TypeError, ValueError):
            return jsonify({"error": f"{key} must be numeric"}), 400
        if value <= 0:
            return jsonify({"error": f"{key} must be positive"}), 400
        source_config[key] = value
        source_config_changed = True
    if "whisper_model" in payload:
        model = str(payload.get("whisper_model") or "").strip()
        if not model:
            return jsonify({"error": "whisper_model cannot be empty"}), 400
        source_config["whisper_model"] = model
        source_config_changed = True
    for key in ("ear_confirmed_splits", "sanity_anchors"):
        if key in payload:
            if not isinstance(payload[key], list):
                return jsonify({"error": f"{key} must be a list"}), 400
            source_config[key] = payload[key]
            source_config_changed = True
    if source_config_changed:
        save_source_config(source_config)
        pipeline.configure_detection_profile(source_config)
        with state_lock:
            pipeline_state = None
            pipeline_state_signature = None
    if "matchering_reference" in payload:
        reference = str(payload.get("matchering_reference") or "").strip()
        if reference and not Path(reference).expanduser().is_file():
            return jsonify({"error": "Matchering reference file not found"}), 400
        settings["matchering_reference"] = str(Path(reference).expanduser().resolve()) if reference else ""
    save_settings(settings)
    return jsonify({"ok": True, "settings": settings, "source_config": load_source_config()})


@app.post("/api/reset-automatic/<int:song_id>")
def api_reset_automatic(song_id: int) -> Response:
    state = ensure_pipeline_state()
    if song_id < 1 or song_id > len(state["segments"]):
        return jsonify({"error": "song not found"}), 404
    overrides = normalize_overrides(load_json(OVERRIDES_PATH, {"songs": {}}))
    songs = overrides.setdefault("songs", {})
    removed = songs.pop(str(song_id), None) is not None
    overrides["_write_trace"] = override_write_trace(overrides)
    save_json(OVERRIDES_PATH, overrides)
    append_log("ui", f"Reset song {song_id} to automatic mix" + (" (saved overrides cleared)" if removed else " (already automatic)"))
    return jsonify({"ok": True, "song": song_id, "removed": removed})



@app.post("/api/song-name/<int:segment_id>")
def api_song_name(segment_id: int) -> Response:
    state = ensure_pipeline_state()
    if segment_id < 1 or segment_id > len(state["raw_songs"]):
        return jsonify({"error": "song not found"}), 404
    payload = request.get_json(force=True, silent=True) or {}
    name = str(payload.get("name", "")).strip()
    names = load_song_names()
    songs = names.setdefault("songs", {})
    if name:
        raw = state["raw_songs"][segment_id - 1]
        songs[str(segment_id)] = {"name": name, "start": raw["start"], "end": raw["end"]}
    else:
        songs.pop(str(segment_id), None)
    save_song_names(names)
    return jsonify({"ok": True, "name": name})


def _pid_is_alive(value: Any) -> bool:
    """Return whether a recorded worker PID still exists."""
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _active_redetect_job(source: str | Path | None = None) -> dict[str, Any] | None:
    """Return a genuinely live detection job for the selected source.

    A stale in-memory job used to block both initial loading and Redetect
    Songs forever. The status file is authoritative when available, and an
    active job is only retained while its worker is alive or its heartbeat is
    still fresh.
    """
    expected = str(Path(source or pipeline.SOURCE_DIR).expanduser().resolve())
    active_statuses = {"queued", "running", "stopping"}
    terminal_statuses = {
        "done",
        "pending_review",
        "pending_confirmation",
        "error",
        "cancelled",
        "finished",
    }
    with state_lock:
        candidates = [
            job for job in jobs
            if job.get("kind") == "redetect"
            and str(Path(job.get("source_folder") or "").expanduser().resolve()) == expected
            and job.get("status") in active_statuses
        ]

    live: list[dict[str, Any]] = []
    now = time.time()
    for job in candidates:
        status = read_job_status_for_job(job)
        if isinstance(status, dict):
            with state_lock:
                job.update(status)

        current_status = str(job.get("status") or "")
        if current_status in terminal_statuses or current_status not in active_statuses:
            continue

        child_pid = job.get("child_pid") or job.get("pid")
        heartbeat_raw = (
            job.get("heartbeat")
            or job.get("progress_updated_at")
            or job.get("updated_at")
            or job.get("created")
            or 0
        )
        try:
            heartbeat = float(heartbeat_raw)
        except (TypeError, ValueError):
            heartbeat = 0.0

        orphaned = False
        orphan_reason = ""
        if job.get("fingerprint") != source_job_identity(detection_state_signature())["fingerprint"]:
            orphaned = True
            orphan_reason = "source fingerprint changed or is missing"
        elif heartbeat and now - heartbeat > ORPHANED_ACTIVE_JOB_SECONDS:
            orphaned = True
            orphan_reason = "worker heartbeat expired"
        elif child_pid not in (None, ""):
            if not _pid_is_alive(child_pid):
                orphaned = True
                orphan_reason = f"recorded worker PID {child_pid} is no longer alive"
        elif heartbeat and now - heartbeat > ORPHANED_ACTIVE_JOB_SECONDS:
            orphaned = True
            orphan_reason = "no worker PID or fresh heartbeat was recorded"

        if orphaned:
            # Signal only a child owned by this process, never an arbitrary
            # persisted PID that could have been reused by macOS.
            owned_child = child_processes.get(str(job.get("id")))
            if owned_child is not None and owned_child.poll() is None:
                try:
                    signal_owned_process_group(owned_child)
                except ProcessLookupError:
                    pass
            finished_at = time.time()
            recovered = {
                "status": "error",
                "error": (
                    "Redetect job was recovered as orphaned; its worker is no longer running. "
                    "Start Redetect Songs again."
                ),
                "current_stage": "error",
                "stage_detail": "stale/orphaned detection recovered",
                "finished_at": finished_at,
                "updated_at": finished_at,
                "heartbeat": finished_at,
                "termination": "orphaned redetect recovered",
                "orphan_reason": orphan_reason,
            }
            with state_lock:
                job.update(recovered)
            write_job_status(job)
            lifecycle_log(
                "redetect_orphaned_recovered",
                str(job.get("id") or ""),
                source_folder=expected,
                reason=orphan_reason,
            )
            continue

        live.append(job)

    return dict(live[-1]) if live else None


@source_edit_locked
def _queue_redetect_job(allow_whisper: bool = True) -> dict[str, Any]:
    """Serialize simultaneous requests so double clicks cannot queue duplicates."""
    with state_lock:
        return _queue_redetect_job_locked(allow_whisper)


def _queue_redetect_job_locked(allow_whisper: bool) -> dict[str, Any]:
    """Queue exactly one source-scoped detection job and return its snapshot."""
    source = str(Path(load_settings().get("source_folder") or pipeline.SOURCE_DIR).expanduser().resolve())
    configure_source_folder(source)
    signature = detection_state_signature()
    identity = source_job_identity(signature)
    existing = _active_redetect_job(source)
    if existing:
        return existing
    job_id = f"{int(time.time())}-{len(jobs) + 1}"
    job = {
        "id": job_id,
        "kind": "redetect",
        "songs": [],
        "status_path": str(job_status_path(job_id)),
        "status": "queued",
        "progress": 0,
        "song_progress": 0,
        "created": time.time(),
        "current": None,
        "current_stage": "scanning folder",
        "stage_detail": "Scanning source folder before detection",
        "started": None,
        "heartbeat": None,
        "done_count": 0,
        "total_count": 1,
        "lifecycle_log_path": str(LIFECYCLE_LOG_PATH),
        "allow_whisper": bool(allow_whisper),
        "source_folder": source,
        **identity,
        "detection_mode": "fresh",
        "build": runtime_build_metadata(),
    }
    with state_lock:
        jobs.append(job)
        write_job_status(job)
    lifecycle_log(
        "redetect_queued",
        job_id,
        source_folder=source,
        audio_scan_mode=str(load_settings().get("audio_scan_mode") or "auto"),
        cache_path=str(pipeline.detection_cache_path()),
        allow_whisper=bool(allow_whisper),
    )
    job_queue.put(job)
    append_log(job["id"], "Queued song search.")
    return dict(job)


@app.post("/api/redetect")
def api_redetect(allow_whisper: bool = True) -> Response:
    payload = request.get_json(force=True, silent=True) or {}
    if "allow_whisper" in payload:
        allow_whisper = bool(payload.get("allow_whisper"))
    return jsonify(_queue_redetect_job(allow_whisper))
def configured_slot_target() -> int | None:
    raw = getattr(pipeline, "EXPECTED_SLOT_COUNT", None)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


@app.get("/api/redetect/candidate")
def api_redetect_candidate() -> Response:
    candidate = load_json(REDETECTION_CANDIDATE_PATH, None)
    if not isinstance(candidate, dict):
        return jsonify({"available": False})
    requested_job = request.args.get("job_id")
    if requested_job and candidate.get("job_id") != requested_job:
        return jsonify({"available": False, "error": "Candidate belongs to another detection job."}), 409
    current = load_json(DETECTION_STATE_PATH, {})
    old_count = len(current.get("raw_songs", [])) if isinstance(current, dict) else 0
    new_count = len(candidate.get("raw_songs", []))
    return jsonify({
        "available": True,
        "old_count": old_count,
        "new_count": new_count,
        "old_slots": current.get("raw_songs", []) if isinstance(current, dict) else [],
        "new_slots": candidate.get("raw_songs", []),
        "source_stem_count": len(candidate.get("stems", [])),
        "source_duration_sec": max((float(item.get("frames", 0)) / float(item.get("samplerate", 1)) + float(item.get("offset_seconds", 0.0)) for item in candidate.get("stems", []) if isinstance(item, dict)), default=0.0),
        "warning": "Candidate slot count differs from the configured session target; review required before replacement." if configured_slot_target() is not None and new_count != configured_slot_target() else "",
    })


@app.post("/api/redetect/commit")
@source_edit_locked
def api_redetect_commit() -> Response:
    candidate = load_json(REDETECTION_CANDIDATE_PATH, None)
    if not isinstance(candidate, dict):
        return jsonify({"error": "No pending full-session detection is available."}), 409
    requested_job = (request.get_json(silent=True) or {}).get("job_id")
    if requested_job and candidate.get("job_id") != requested_job:
        return jsonify({"error": "Candidate belongs to another detection job."}), 409
    count = len(candidate.get("raw_songs", []))
    if count < 2:
        return jsonify({"error": f"Refusing to replace the current session with only {count} detected slot(s)."}), 409
    if candidate.get("candidate_pending") or legacy_single_slot_snapshot_reason(candidate):
        return jsonify({"error": "Detection is incomplete; current session preserved."}), 409
    expected = detection_state_signature()
    if candidate.get("source_signature") != [expected[0], list(expected[1]), [list(item) for item in expected[2]]]:
        return jsonify({"error": "Source changed since detection; run Detect Songs again."}), 409
    candidate["fingerprint"] = source_job_identity(expected)["fingerprint"]
    previous_manual = load_json(MANUAL_EDITOR_STATE_PATH, None)
    if isinstance(previous_manual, dict):
        save_json_atomic(ACTIVE_SOURCE_STATE_ROOT / f"manual-editor-before-redetect-{time.time_ns()}.json", previous_manual)
    save_json_atomic(DETECTION_STATE_PATH, candidate)
    # Only explicit confirmation may replace the authoritative human timeline.
    save_json_atomic(MANUAL_EDITOR_STATE_PATH, candidate)
    try:
        REDETECTION_CANDIDATE_PATH.unlink()
    except FileNotFoundError:
        pass
    try:
        REDETECTION_BACKUP_PATH.unlink()
    except FileNotFoundError:
        pass
    with state_lock:
        global pipeline_state, pipeline_state_signature
        pipeline_state = None
        pipeline_state_signature = None
    append_log("ui", f"Confirmed replacement with full-session detection: {count} slots.")
    return jsonify({"ok": True, "count": count})


@app.post("/api/redetect/discard")
def api_redetect_discard() -> Response:
    for path in (REDETECTION_CANDIDATE_PATH,):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    try:
        REDETECTION_BACKUP_PATH.unlink()
    except FileNotFoundError:
        pass
    append_log("ui", "Discarded pending full-session detection; current session preserved.")
    return jsonify({"ok": True})


@app.post("/api/redetect/second-pass")
def api_redetect_second_pass() -> Response:
    """Run Whisper only in long uncovered source intervals and save proposals."""
    state = ensure_pipeline_state()
    stems = state.get("stems", [])
    if not stems:
        return jsonify({"error": "Original WAV stems are not loaded."}), 409
    existing = sorted((float(segment.start), float(segment.end)) for segment in state.get("segments", []))
    source_end = max((float(stem.offset_seconds) + float(stem.timeline_duration) for stem in stems), default=0.0)
    regions: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in existing:
        if start - cursor >= 180.0:
            regions.append((max(0.0, cursor - 30.0), min(source_end, start + 30.0)))
        cursor = max(cursor, end)
    if source_end - cursor >= 180.0:
        regions.append((max(0.0, cursor - 30.0), source_end))
    expected_target = configured_slot_target()
    if not regions:
        missing_count = max(0, expected_target - len(existing)) if expected_target is not None else None
        return jsonify({"ok": True, "status": "no_suspicious_intervals", "new_proposals": [], "missing_count": missing_count})
    try:
        app_progress({"current_stage": "second Whisper pass", "stage_detail": f"Scanning {len(regions)} uncovered interval(s)", "progress": 10, "song_progress": 10, "heartbeat": time.time()})
        timelines = pipeline.load_cached_timelines_or_die(stems, "Whisper second pass")
        _transcripts, focused_segments = pipeline.transcribe_speech_candidates(
            stems, timelines, source_end, focus_regions=regions, timeout_seconds=240.0,
        )
    except Exception as exc:
        candidate = {"status": "incomplete", "reason": f"{type(exc).__name__}: {exc}", "regions": regions, "new_proposals": []}
        save_json_atomic(SECOND_PASS_CANDIDATE_PATH, candidate)
        app_progress({"status": "error", "current_stage": "second Whisper pass", "stage_detail": candidate["reason"], "progress": 100, "song_progress": 100, "heartbeat": time.time()})
        return jsonify({"ok": False, **candidate}), 200
    new_proposals = []
    proposal_session_id = next((str(row.get("session_id")) for row in state.get("raw_songs", []) if row.get("session_id")), session_id_for_signature(detection_state_signature()))
    for segment in focused_segments:
        if any(abs(float(segment.start) - start) < 25.0 for start, _end in existing):
            continue
        new_proposals.append({
            "slot_id": f"{proposal_session_id}:whisper-second-pass-{len(new_proposals) + 1:03d}",
            "start": float(segment.start),
            "end": float(segment.end),
            "duration": float(segment.duration),
            "text": segment.speech_text,
            "confidence": float(segment.speech_confidence or 0.0),
            "reason": segment.speech_reason or "additional commentator presentation in uncovered interval",
            "status": "needs_review",
        })
    missing_count = max(0, expected_target - len(existing) - len(new_proposals)) if expected_target is not None else None
    candidate = {"status": "pending_confirmation", "regions": regions, "new_proposals": new_proposals, "missing_count": missing_count}
    save_json_atomic(SECOND_PASS_CANDIDATE_PATH, candidate)
    app_progress({"status": "pending_confirmation", "current_stage": "second Whisper pass", "stage_detail": f"Found {len(new_proposals)} new proposal(s); manual review required", "progress": 100, "song_progress": 100, "heartbeat": time.time()})
    return jsonify({"ok": True, **candidate})


@app.get("/api/redetect/second-pass")
def api_redetect_second_pass_status() -> Response:
    candidate = load_json(SECOND_PASS_CANDIDATE_PATH, None)
    return jsonify(candidate if isinstance(candidate, dict) else {"available": False})



@app.post("/api/render/<int:song_index>")
def api_render_song(song_index: int) -> Response:
    try:
        state = load_render_state()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400
    # Duration and confidence warnings must remain visible and renderable.
    # Only an explicit user skip removes a song from the batch.
    visible_ids = {
        int(song["id"])
        for song in visible_songs(state, load_settings(), load_json(HISTORY_PATH, {}))
        if not song.get("skipped")
    }
    if song_index not in visible_ids:
        return jsonify({"error": "song not found"}), 404
    payload = request.get_json(force=True, silent=True) or {}
    render_target = payload.get("render_target_dir") or payload.get("render_target")
    if not render_target:
        return jsonify({"error": "Choose a destination folder before rendering."}), 400
    try:
        render_target = str(validate_render_target(str(render_target)))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    preview_effective_mix = payload.get("preview_effective_mix")
    if not isinstance(preview_effective_mix, dict):
        preview_effective_mix = None
    if render_target:
        render_target = str(render_target)
        settings = load_settings()
        settings["last_render_dir"] = render_target
        save_settings(settings)
    try:
        trace = payload.get("render_destination_trace") if isinstance(payload.get("render_destination_trace"), dict) else None
        extra = {"render_destination_trace_request": trace} if trace else {}
        extra["use_saved_mixes"] = bool(payload.get("use_saved_mixes", True))
        if isinstance(payload.get("overrides_snapshot"), dict):
            extra["overrides_snapshot"] = normalize_overrides(payload["overrides_snapshot"])
        return jsonify(enqueue("render", [song_index], render_target=render_target, preview_effective_mix=preview_effective_mix, extra=extra))
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400


def real_preview_cache_path(song_index: int, start: float, duration: float) -> Path:
    overrides = normalize_overrides(load_json(OVERRIDES_PATH, {"songs": {}}))
    song_overrides = overrides.get("songs", {}).get(str(song_index), {})
    signature = {
        "song": song_index,
        "start": round(start, 3),
        "duration": round(duration, 3),
        "overrides": song_overrides,
        "source": str(pipeline.SOURCE_DIR),
        "source_signature": detection_state_signature(),
    }
    digest = hashlib.sha256(json.dumps(signature, sort_keys=True, default=str).encode()).hexdigest()[:16]
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    return PREVIEW_DIR / f"real_song_{song_index:03d}_{int(round(duration))}s_{digest}.mp3"


@app.post("/api/real-preview/<int:song_index>")
def api_real_preview(song_index: int) -> Response:
    state = ensure_pipeline_state()
    if song_index < 1 or song_index > len(state["segments"]):
        return jsonify({"error": "song not found"}), 404
    payload = request.get_json(force=True, silent=True) or {}
    requested_duration = float(payload.get("duration", 60.0) or 60.0)
    duration = max(30.0, min(90.0, requested_duration))
    song_segment = state["segments"][song_index - 1]
    song_duration = max(0.0, song_segment.duration)
    max_start = max(0.0, song_duration - duration)
    # Real previews are deliberately centered; the UI does not expose arbitrary offsets.
    start_offset = max_start / 2.0
    start = float(song_segment.start + start_offset)
    end = min(float(song_segment.end), start + duration)
    duration = max(1.0, end - start)
    cache_path = real_preview_cache_path(song_index, start, duration)
    if cache_path.exists():
        return jsonify({
            "cached": True,
            "status": "done",
            "preview_url": f"/real-preview/{song_index}/{cache_path.name}",
            "preview_path": str(cache_path),
            "preview_start": start_offset,
            "preview_duration": duration,
            "elapsed_seconds": 0.0,
        })
    try:
        job = enqueue(
            "real-preview",
            [],
            extra={
                "preview_song_id": song_index,
                "preview_start": start,
                "preview_start_offset": start_offset,
                "preview_duration": duration,
                "preview_path": str(cache_path),
            },
        )
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400
    append_log(job["id"], f"Queued exact real preview: {duration:.0f}s from {start_offset:.1f}s")
    return jsonify(job)


@app.get("/real-preview/<int:song_index>/<path:filename>")
def real_preview_audio(song_index: int, filename: str) -> Response:
    safe = Path(filename).name
    path = (PREVIEW_DIR / safe).resolve()
    if path.parent != PREVIEW_DIR.resolve() or not path.name.startswith(f"real_song_{song_index:03d}_"):
        return Response(status=404)
    return ranged_file_response(path, mimetype="audio/mpeg")


@app.post("/api/render")
def api_render_many() -> Response:
    try:
        state = load_render_state()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400
    settings = load_settings()
    # Mix everything is deliberately warning-tolerant: every detected,
    # non-skipped song is prepared. Select Cuts can repair suspicious cuts;
    # detection must never make them disappear from the batch.
    visible_ids = {
        int(song["id"])
        for song in visible_songs(state, settings, load_json(HISTORY_PATH, {}))
        if not song.get("skipped")
    }
    payload = request.get_json(force=True, silent=True) or {}
    try:
        songs = validate_batch_song_ids(payload.get("songs", []), visible_ids)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        render_target = payload.get("render_target_dir")
        if not render_target:
            return jsonify({"error": "Choose a destination folder before rendering."}), 400
        try:
            render_target = str(validate_render_target(str(render_target)))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if render_target:
            settings["last_render_dir"] = render_target
            save_settings(settings)
        trace = payload.get("render_destination_trace") if isinstance(payload.get("render_destination_trace"), dict) else None
        extra = {"render_destination_trace_request": trace} if trace else {}
        extra["use_saved_mixes"] = bool(payload.get("use_saved_mixes", True))
        if isinstance(payload.get("overrides_snapshot"), dict):
            extra["overrides_snapshot"] = normalize_overrides(payload["overrides_snapshot"])
        job = enqueue("mix", songs, render_target=str(render_target) if render_target else None, extra=extra)
        append_log(job["id"], f"Validated complete batch: {songs} ({len(songs)} songs)")
        return jsonify(job)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400



@app.post("/api/cancel")
def api_cancel() -> Response:
    # Do not make the browser wait for filesystem writes or process cleanup.
    # The cancellation routine marks the job and signals its process group in
    # the background; the UI can close its loading surface immediately.
    threading.Thread(
        target=request_cancel,
        name="zucker-cancel",
        daemon=True,
    ).start()
    return jsonify({"ok": True, "status": "stopping"}), 202


@app.get("/api/jobs")
def api_jobs() -> Response:
    return jsonify(current_jobs())


@app.get("/api/logs")
def api_logs() -> Response:
    since = int(request.args.get("since", "-1"))
    with state_lock:
        lines = [line for line in log_lines if line["id"] > since]
    return jsonify(lines)


@app.get("/api/active-stems/<int:segment_id>")
def api_active_stems(segment_id: int) -> Response:
    try:
        params = canonical_mix_params_for_song(segment_id)
    except IndexError:
        return jsonify({"error": "song not found"}), 404
    return jsonify({"song": segment_id, "active_stems": params["active_stems"], "mix_params": params})


@app.get("/api/mix-preview-params/<int:segment_id>")
def api_mix_preview_params(segment_id: int) -> Response:
    try:
        return jsonify(canonical_mix_params_for_song(segment_id))
    except IndexError:
        return jsonify({"error": "song not found"}), 404


@app.get("/preview/<int:segment_id>")
def preview(segment_id: int) -> Response:
    state = ensure_pipeline_state()
    if segment_id < 1 or segment_id > len(state["segments"]):
        return Response(status=404)
    segment = state["segments"][segment_id - 1]
    boundary = segment.mc_start if segment.mc_start is not None else segment.start
    start = max(0.0, float(boundary) - 10.0)
    end = min(max(stem.timeline_duration for stem in state["stems"]), float(boundary) + 10.0)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    path = PREVIEW_DIR / f"transition_{segment_id:03d}_{int(round(start * 1000))}_{int(round(end * 1000))}.wav"
    if not path.exists():
        sr = state["stems"][0].samplerate
        frames = int(round((end - start) * sr))
        audio = np.zeros((frames, 2), dtype=np.float32)
        preview_segment = pipeline.Segment(start=start, end=end)
        for stem in state["stems"]:
            chunk = pipeline.read_stem_chunk(stem, preview_segment, 0, frames)
            if chunk is None:
                continue
            stereo = chunk if chunk.ndim == 2 else pipeline.pan_mono(chunk, pipeline.pan_for_role(stem.role, stem.name))
            audio[: len(stereo)] += stereo[:frames]
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        if peak > 0:
            audio *= min(0.9 / peak, 8.0)
        sf.write(path, audio, sr, subtype="PCM_16")
    return ranged_file_response(path, mimetype="audio/wav")



@app.get("/stem-full/<int:segment_id>/<int:stem_index>")
def stem_full_preview(segment_id: int, stem_index: int) -> Response:
    state = ensure_pipeline_state()
    if segment_id < 1 or segment_id > len(state["segments"]):
        return Response(status=404)
    if stem_index < 0 or stem_index >= len(state["stems"]):
        return Response(status=404)
    stem = state["stems"][stem_index]
    if state.get("active_stems_detection_version") != ACTIVE_STEM_DETECTION_VERSION:
        state["active_stems_by_song"] = {}
        state["active_stems_detection_version"] = ACTIVE_STEM_DETECTION_VERSION
    if stem.path.name not in state.setdefault("active_stems_by_song", {}).get(str(segment_id), []):
        cache = state.setdefault("active_stems_by_song", {})
        if str(segment_id) not in cache:
            cache[str(segment_id)] = active_stems_for_segment(state["stems"], state["segments"][segment_id - 1])
        if stem.path.name not in cache[str(segment_id)]:
            return Response(status=204)
    ffmpeg = pipeline.resolve_ffmpeg()
    if not ffmpeg:
        return jsonify({"error": "ffmpeg not found"}), 500

    segment = state["segments"][segment_id - 1]
    sr = stem.samplerate
    target_frames = int(round(segment.duration * sr))
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem.path.stem)
    start_ms = int(round(segment.start * 1000))
    end_ms = int(round(segment.end * 1000))
    channels = 1 if stem.channels == 1 else 2
    bitrate = "128k" if channels == 1 else "192k"
    # Encode quiet sources near full scale, then restore their original level
    # in the browser. PCM16 before makeup adds audible quantization hiss.
    analysis = song_analysis_snapshot(state, segment, segment_id)
    source_peak_db = float(analysis.get("segment_peaks_db", {}).get(stem.path.name, 0.0))
    encoding_gain_db = float(np.clip(-6.0 - source_peak_db, 0.0, 60.0))
    if not np.isfinite(encoding_gain_db):
        encoding_gain_db = 0.0
    encoding_gain = pipeline.db_to_amp(encoding_gain_db)
    path = PREVIEW_DIR / f"song_{segment_id:03d}_full_v4_stem_{stem_index:02d}_{start_ms}_{end_ms}_{encoding_gain_db:.2f}_{safe_stem}.mp3"
    if not path.exists():
        chunk_frames = int(round(30.0 * sr))
        with tempfile.TemporaryDirectory(prefix=f"zucker_preview_{segment_id:03d}_{stem_index:02d}_") as tmp:
            wav_path = Path(tmp) / "stem.wav"
            noise_info = analysis.get("noise_diagnostics", {}).get(stem.path.name, {})
            noise_state = {}
            with sf.SoundFile(str(wav_path), "w", samplerate=sr, channels=channels, subtype="FLOAT") as writer:
                for chunk_start in range(0, target_frames, chunk_frames):
                    nframes = min(chunk_frames, target_frames - chunk_start)
                    chunk = pipeline.read_stem_chunk(stem, segment, chunk_start, nframes)
                    if chunk is None:
                        chunk = np.zeros((nframes, channels), dtype=np.float32) if channels > 1 else np.zeros(nframes, dtype=np.float32)
                    if channels == 1 and chunk.ndim == 2:
                        chunk = np.mean(chunk, axis=1).astype(np.float32)
                    elif channels == 2 and chunk.ndim == 1:
                        chunk = np.column_stack((chunk, chunk)).astype(np.float32)
                    if noise_info.get("case") in {"A", "hum"}:
                        if chunk.ndim == 2:
                            chunk = np.column_stack([
                                pipeline.apply_noise_watchdog_streaming(chunk[:, channel], sr, noise_info,
                                    noise_state, f"preview:{stem.path.name}:{channel}")[0]
                                for channel in range(chunk.shape[1])])
                        else:
                            chunk, _ = pipeline.apply_noise_watchdog_streaming(
                                chunk, sr, noise_info, noise_state, f"preview:{stem.path.name}")
                    writer.write(np.clip(np.nan_to_num(chunk[:nframes]) * encoding_gain, -1.0, 1.0))
            cmd = [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(wav_path),
                "-vn",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                bitrate,
                str(path),
            ]
            subprocess.run(cmd, check=True)
    response = ranged_file_response(path, mimetype="audio/mpeg")
    response.headers["X-Preview-Cache-Bytes"] = str(path.stat().st_size)
    response.headers["X-Preview-Source-Gain-Db"] = str(encoding_gain_db)
    return response


@app.get("/audio/<int:song_index>/<int:slot>")
def audio(song_index: int, slot: int) -> Response:
    selected = history_entry(song_index, slot)
    if not selected:
        return Response(status=404)
    path = Path(selected["path"])
    return ranged_file_response(path, mimetype="audio/mpeg")


@app.get("/download/<int:song_index>/<int:slot>")
def download(song_index: int, slot: int) -> Response:
    selected = history_entry(song_index, slot)
    if not selected:
        return Response(status=404)
    path = Path(selected["path"])
    return ranged_file_response(path, mimetype="audio/mpeg", as_attachment=True)


@app.post("/api/open/<int:song_index>")
def api_open(song_index: int) -> Response:
    selected = history_entry(song_index, 0)
    path = Path(selected["path"]) if selected else out_dir()
    import subprocess

    subprocess.Popen(["open", "-R", str(path)])
    return jsonify({"ok": True})


def start_worker_once() -> None:
    if getattr(start_worker_once, "_started", False):
        return
    threading.Thread(target=worker, daemon=True).start()
    start_worker_once._started = True


def start_server(port: int | None = None) -> tuple[str, Any]:
    global server_ref
    startup_hygiene()
    start_worker_once()
    requested_port = 0 if port is None else int(port)
    server = make_server(HOST, requested_port, app, threaded=True)
    actual_port = int(server.server_port)
    server_ref = server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://{HOST}:{actual_port}", server


def signal_owned_process_group(proc: subprocess.Popen, force: bool = False) -> None:
    """Stop only an owned worker and its helpers on either desktop platform."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)


def stop_server() -> None:
    # Signal workers synchronously so closing the native window cannot leave a
    # render orphaned. Shutdown of the WSGI loop is deliberately asynchronous:
    # Werkzeug may wait for an in-flight request and must never block app exit.
    request_cancel()
    server = server_ref
    if server is not None:
        threading.Thread(
            target=server.shutdown,
            name="zucker-server-shutdown",
            daemon=True,
        ).start()


def has_active_jobs() -> bool:
    with state_lock:
        return any(job.get("status") in {"queued", "running", "stopping"} for job in jobs)


def wait_for_jobs_to_stop(timeout_seconds: float | None = None) -> None:
    started = time.time()
    while has_active_jobs():
        if timeout_seconds is not None and time.time() - started > timeout_seconds:
            return
        time.sleep(0.5)


def main() -> None:
    url, _server = start_server()
    if os.environ.get("ZUCKER_MIXER_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"Zucker Mixer running at {url}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop_server()


if __name__ == "__main__":
    main()
