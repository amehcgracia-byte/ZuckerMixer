"""Encode preview chunks without writing and rereading a full temporary WAV."""
from pathlib import Path
import subprocess
import tempfile
import uuid
import contextlib
import numpy as np


def encode_float_chunks(ffmpeg, chunks, samplerate, channels, bitrate, destination):
    destination = Path(destination)
    stage = destination.with_name(destination.stem + '-' + uuid.uuid4().hex + '.mp3')
    command = [ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
               '-f', 'f32le', '-ar', str(samplerate), '-ac', str(channels),
               '-i', 'pipe:0', '-vn', '-codec:a', 'libmp3lame', '-b:a', bitrate, str(stage)]
    try:
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL, stderr=errors)
            try:
                for chunk in chunks:
                    process.stdin.write(np.asarray(chunk, dtype='<f4').tobytes(order='C'))
                process.stdin.close()
                code = process.wait()
                if code:
                    errors.seek(0)
                    raise RuntimeError(errors.read().decode('utf-8', errors='replace'))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if not process.stdin.closed:
                    with contextlib.suppress(BrokenPipeError):
                        process.stdin.close()
        stage.replace(destination)
    finally:
        stage.unlink(missing_ok=True)
