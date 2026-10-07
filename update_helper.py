"""Standalone updater entry point, dispatched before loading the audio engine."""
from pathlib import Path
import ctypes
import json
import os
import subprocess
import sys
import time


def wait_for_parent(pid, timeout=120):
    if os.name == 'nt':
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if handle:
            try:
                if kernel.WaitForSingleObject(ctypes.c_void_p(handle), int(timeout*1000)) != 0:
                    raise TimeoutError('Application did not close; update cancelled')
            finally:
                kernel.CloseHandle(ctypes.c_void_p(handle))
        return
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(.5)
    raise TimeoutError('Application did not close; update cancelled')


def executable(folder):
    return folder/'Contents/MacOS/ZuckerMixer' if folder.suffix == '.app' else folder/'ZuckerMixer.exe'


def launch(folder, receipt=None):
    args = [str(executable(folder))]
    if receipt:
        args += ['--update-receipt', str(receipt)]
    return subprocess.Popen(args, cwd=str(folder.parent), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def apply_update(manifest, *, wait=wait_for_parent, start=launch, receipt_timeout=180):
    target = Path(manifest['target']); stage = Path(manifest['stage'])
    backup = Path(manifest['backup']); receipt = Path(manifest['receipt'])
    if stage.parent != target.parent or backup.parent != target.parent or backup.exists():
        raise ValueError('Invalid update locations')
    if not executable(stage).is_file() or not executable(target).is_file():
        raise ValueError('Incomplete application')
    wait(int(manifest['parent_pid']))
    target.rename(backup)
    process = None
    try:
        stage.rename(target)
        process = start(target, receipt)
        deadline = time.monotonic()+receipt_timeout
        while time.monotonic() < deadline:
            if receipt.exists():
                data = json.loads(receipt.read_text())
                if data.get('version') != manifest['version']:
                    raise RuntimeError('Updated application reported the wrong version')
                return {'status': 'installed', 'version': manifest['version'], 'backup': str(backup)}
            if process.poll() is not None:
                raise RuntimeError('Updated application could not start')
            time.sleep(.5)
        raise TimeoutError('Updated application did not confirm startup')
    except Exception:
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=30)
        if target.exists():
            target.rename(stage)
        backup.rename(target)
        start(target)
        raise


def main():
    if sys.argv[-1] == '--self-check':
        print('ZuckerMixer standalone update helper: OK', flush=True)
        return
    manifest_path = Path(sys.argv[-1]).resolve()
    data = json.loads(manifest_path.read_text())
    result_path = manifest_path.parent/'result.json'
    try:
        result = apply_update(data)
    except Exception as exc:
        result = {'status': 'error', 'error': str(exc)}
    result_path.write_text(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
