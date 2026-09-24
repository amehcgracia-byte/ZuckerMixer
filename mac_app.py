#!/usr/bin/env python3
from __future__ import annotations

import sys
import threading
import json
import os
import time
from pathlib import Path
from typing import Any


def record_worker_bootstrap() -> None:
    """Leave a record even if importing the application fails."""
    if len(sys.argv) < 3 or sys.argv[1] != "--worker":
        return
    state_root = Path.home() / "Music" / "JamMixes" / "ZuckerMixerState"
    record = {
        "ts": time.time(),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "event": "child_bootstrap_before_import",
        "argv": sys.argv,
        "job_path": sys.argv[2],
    }
    try:
        state_root.mkdir(parents=True, exist_ok=True)
        with (state_root / "redetect_worker_lifecycle.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        pass


record_worker_bootstrap()
import webview

import jam_app
import jam_mix_pipeline as pipeline


class ZuckerMixerApi:
    def choose_source_folder(self, default_dir: str) -> dict[str, Any]:
        window = webview.windows[0] if webview.windows else None
        if window is None:
            return {"cancelled": True}
        directory = Path(default_dir or Path.home()).expanduser()
        result = window.create_file_dialog(webview.FOLDER_DIALOG, directory=str(directory))
        if not result:
            return {"cancelled": True}
        folder = Path(result[0] if isinstance(result, (list, tuple)) else result).expanduser().resolve()
        return {"cancelled": False, "folder": str(folder)}

    def choose_save_path(self, default_name: str, default_dir: str) -> dict[str, Any]:
        window = webview.windows[0] if webview.windows else None
        if window is None:
            return {"cancelled": True}
        folder = Path(default_dir or jam_app.out_dir()).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        name = default_name or "mix.mp3"
        if not name.lower().endswith(".mp3"):
            name += ".mp3"
        result = window.create_file_dialog(
            webview.SAVE_DIALOG,
            directory=str(folder),
            save_filename=name,
            file_types=("MP3 Audio (*.mp3)",),
        )
        if not result:
            return {"cancelled": True}
        path = Path(result[0] if isinstance(result, (list, tuple)) else result).expanduser()
        if path.suffix.lower() != ".mp3":
            path = path.with_suffix(".mp3")
        return {"cancelled": False, "path": str(path), "folder": str(path.parent)}

    def choose_reference_file(self, default_path: str) -> dict[str, Any]:
        window = webview.windows[0] if webview.windows else None
        if window is None:
            return {"cancelled": True}
        path = Path(default_path).expanduser() if default_path else Path.home()
        directory = str(path.parent if path.suffix else path)
        result = window.create_file_dialog(
            webview.OPEN_DIALOG,
            directory=directory,
            allow_multiple=False,
            file_types=("Audio files (*.wav;*.aif;*.aiff;*.flac;*.mp3;*.m4a)",),
        )
        if not result:
            return {"cancelled": True}
        selected = Path(result[0] if isinstance(result, (list, tuple)) else result).expanduser().resolve()
        return {"cancelled": False, "path": str(selected)}

    def choose_save_folder(self, default_dir: str) -> dict[str, Any]:
        window = webview.windows[0] if webview.windows else None
        if window is None:
            return {"cancelled": True}
        directory = Path(default_dir or jam_app.out_dir()).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        result = window.create_file_dialog(webview.FOLDER_DIALOG, directory=str(directory))
        if not result:
            return {"cancelled": True}
        folder = Path(result[0] if isinstance(result, (list, tuple)) else result).expanduser().resolve()
        return {"cancelled": False, "folder": str(folder)}


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker":
        raise SystemExit(jam_app.run_child_job(Path(sys.argv[2])))
    url, _server = jam_app.start_server()
    window = webview.create_window(
        "ZuckerMixer",
        url,
        width=1200,
        height=800,
        min_size=(960, 680),
        resizable=True,
        js_api=ZuckerMixerApi(),
    )

    def on_loaded() -> None:
        if not pipeline.resolve_ffmpeg():
            window.create_confirmation_dialog(
                "ffmpeg is missing",
                "ZuckerMixer needs ffmpeg to create MP3 files.\n\nInstall it in Terminal with:\n\nbrew install ffmpeg",
            )

    def on_closing() -> None:
        threading.Thread(target=jam_app.stop_server, daemon=True).start()

    window.events.loaded += on_loaded
    window.events.closing += on_closing
    webview.start(debug=False)
    jam_app.wait_for_jobs_to_stop()


if __name__ == "__main__":
    main()
