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
    state_root = Path(os.environ.get("ZUCKER_MIXER_STATE_ROOT", str(Path.home() / "Music" / "JamMixes" / "ZuckerMixerState"))).expanduser().resolve()
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


if "--update-helper" in sys.argv:
    import runpy
    helper_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "update_helper.py"
    runpy.run_path(str(helper_path), run_name="__main__")
    raise SystemExit(0)

def ensure_pipeline_module() -> None:
    """Load jam_mix_pipeline from the frozen bundle when import discovery misses it."""
    try:
        import jam_mix_pipeline  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    module_path = bundle_root / "jam_mix_pipeline.py"
    if not module_path.is_file():
        raise ModuleNotFoundError(
            f"jam_mix_pipeline.py is missing from frozen bundle: {module_path}"
        )

    import importlib.util

    module_spec = importlib.util.spec_from_file_location(
        "jam_mix_pipeline", str(module_path)
    )
    if module_spec is None or module_spec.loader is None:
        raise ImportError(f"Cannot load frozen module: {module_path}")

    module = importlib.util.module_from_spec(module_spec)
    sys.modules["jam_mix_pipeline"] = module
    module_spec.loader.exec_module(module)


ensure_pipeline_module()

if "--whisper-worker" in sys.argv:
    # The frozen app carries whisper_transcribe.py as a data resource. Running
    # it through the same executable keeps the DMG self-contained and avoids
    # depending on a user's unrelated .whisperenv checkout.
    import runpy

    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    worker_path = bundle_root / "whisper_transcribe.py"
    if not worker_path.is_file():
        raise FileNotFoundError(f"Bundled Whisper worker is missing: {worker_path}")
    runpy.run_path(str(worker_path), run_name="__main__")
    raise SystemExit(0)

import webview

import jam_app
import jam_mix_pipeline as pipeline


if "--self-check" in sys.argv:
    import update_manager
    resources = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    assert (resources / "update_helper.py").is_file()
    # The system fallback hides a missing bundle on build machines; require
    # certifi itself so update checks work on every user's Mac.
    try:
        import certifi
        if not Path(certifi.where()).is_file():
            raise FileNotFoundError(certifi.where())
    except Exception as exc:
        raise SystemExit(f"ZuckerMixer update certificate self-check failed: {type(exc).__name__}: {exc}")
    try:
        import faster_whisper  # noqa: F401
    except Exception as exc:
        raise SystemExit(f"ZuckerMixer Whisper self-check failed: {type(exc).__name__}: {exc}")
    pipeline.ensure_matchering_available()
    if pipeline.matchering_api is None:
        raise SystemExit(f"Matchering self-check failed: {pipeline.MATCHERING_IMPORT_ERROR}")
    with pipeline.bounded_reference_eq():
        probe = pipeline.bound_reference_eq_fir(pipeline.np.zeros(2048), 44100, "side")
        if pipeline.np.any(probe):
            raise SystemExit("Reference EQ safety self-check failed")
    print("ZuckerMixer bounded reference EQ self-check: OK", flush=True)
    print("ZuckerMixer Matchering import self-check: OK", flush=True)
    print("ZuckerMixer frozen import self-check: OK", flush=True)
    print("ZuckerMixer bundled Whisper import self-check: OK", flush=True)
    print("ZuckerMixer update certificate self-check: OK", flush=True)
    raise SystemExit(0)


class ZuckerMixerApi:
    def __init__(self):
        from update_manager import Updater
        self.updater = Updater(jam_app.runtime_build_metadata()['app_version'], jam_app.STATE_ROOT)

    def check_update(self):
        return self.updater.check()

    def update_status(self):
        return self.updater.status()

    def install_update(self):
        def finish(process):
            window = webview.windows[0]
            try:
                if jam_app.has_active_jobs() or not window.evaluate_js("window.canInstallUpdate()"):
                    raise RuntimeError("Finish current work and save pending changes before updating")
                window.destroy()
            except Exception:
                process.terminate()
                process.wait(timeout=30)
                raise
        return self.updater.install(jam_app.has_active_jobs, finish)

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


REPOSITORY_URL = "https://github.com/amehcgracia-byte/ZuckerMixer"

# Menu bar: each action is the id of a button in templates/index.html, so a
# menu item does exactly what that button does. "tutorial", "about" and
# "github" are handled by static/menu.js or here.
MENU_LAYOUT: list[tuple[str, list[tuple[str, str] | None]]] = [
    ("File", [
        ("Change Source Folder…", "changeSourceFolder"),
        ("Choose Mastering Reference…", "chooseMatcheringReference"),
        None,
        ("Open Renders Folder", "openRenderFolder"),
    ]),
    ("Songs", [
        ("Select All Songs", "selectAllSongs"),
        ("Edit All Cuts", "editAllCuts"),
        None,
        ("Re-detect Songs (Acoustic)", "redetectSongs"),
        ("Analyze with Whisper (Optional)", "secondWhisperPass"),
    ]),
    ("Mix", [
        ("Mix Selected", "mixSelected"),
        ("Mix Everything", "mixAll"),
        None,
        ("Cancel Current Job", "cancelJob"),
    ]),
    ("Help", [
        ("Tutorial with Einstein", "tutorial"),
        ("Check for Updates…", "checkUpdate"),
        None,
        ("About ZuckerMixer", "about"),
        ("ZuckerMixer on GitHub", "github"),
    ]),
]


def build_menu(run_action) -> list:
    from webview.menu import Menu, MenuAction, MenuSeparator

    def action(title: str, name: str):
        # Cocoa calls menu items on the UI thread, where evaluate_js would
        # wait on itself; always dispatch from a worker thread.
        return MenuAction(title, lambda: threading.Thread(target=run_action, args=(name,), daemon=True).start())

    menus = [Menu(title, [MenuSeparator() if item is None else action(*item) for item in items]) for title, items in MENU_LAYOUT]
    if sys.platform == "darwin":
        # macOS convention: the app menu (next to the Apple menu) also offers updates.
        menus.insert(0, Menu("__app__", [action("Check for Updates…", "checkUpdate")]))
    return menus


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
        if "--update-receipt" in sys.argv:
            receipt = Path(sys.argv[sys.argv.index("--update-receipt") + 1])
            receipt.write_text(json.dumps({"version": jam_app.runtime_build_metadata()['app_version']}))
        if not pipeline.resolve_ffmpeg():
            window.create_confirmation_dialog(
                "ffmpeg is missing",
                ("ZuckerMixer needs ffmpeg to create MP3 files.\n\nInstall it in Terminal with:\n\nbrew install ffmpeg" if sys.platform == "darwin" else "FFmpeg is missing from the Windows package. Download the complete Windows ZIP and extract it before opening ZuckerMixer.exe."),
            )

    def on_closing() -> bool:
        if jam_app.has_active_jobs():
            proceed = window.create_confirmation_dialog(
                "Work in progress",
                "A mix or analysis is still running. Closing now will cancel it; "
                "results already saved will be kept. Close anyway?",
            )
            if not proceed:
                return False
        threading.Thread(target=jam_app.stop_server, daemon=True).start()
        # A stuck WebView or disk request must not keep the desktop alive after
        # an accepted close. Give worker termination/reaping a bounded interval.
        def force_exit() -> None:
            for proc in list(jam_app.child_processes.values()):
                try:
                    jam_app.signal_owned_process_group(proc, force=True)
                except (OSError, subprocess.SubprocessError):
                    pass
            os._exit(0)
        timer = threading.Timer(6.0, force_exit)
        timer.daemon = True
        timer.start()
        return True

    def run_menu_action(name: str) -> None:
        if name == "github":
            import webbrowser
            webbrowser.open(REPOSITORY_URL)
            return
        window.evaluate_js(f"window.zuckerMenu && window.zuckerMenu({json.dumps(name)})")

    window.events.loaded += on_loaded
    window.events.closing += on_closing
    webview.start(debug=False, menu=build_menu(run_menu_action))
    # Closing the WebView must never wait for a multi-minute Whisper/ffmpeg
    # worker. Process groups were already signalled by on_closing.
    jam_app.wait_for_jobs_to_stop(timeout_seconds=2.5)


if __name__ == "__main__":
    main()
