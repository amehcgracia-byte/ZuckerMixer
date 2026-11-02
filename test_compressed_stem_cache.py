"""MP3/M4A stems are transcoded to a PCM cache that soundfile can read."""
import shutil
import subprocess

import pytest
import soundfile as sf

import jam_mix_pipeline as p


@pytest.mark.parametrize("suffix", [".mp3", ".m4a"])
def test_compressed_stem_is_cached_as_readable_wav(tmp_path, monkeypatch, suffix):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not installed")
    source = tmp_path / f"Bass{suffix}"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=110:duration=2", str(source)], check=True)
    monkeypatch.setattr(p, "TRANSCODE_CACHE_ROOT", tmp_path / "cache", raising=False)
    cached = p._audio_for_processing(source)
    info = sf.info(str(cached))
    assert info.duration == pytest.approx(2.0, abs=0.1)
