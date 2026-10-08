import subprocess
import numpy as np
import pytest
import soundfile as sf
import jam_mix_pipeline as pipeline
from preview_encoding import encode_float_chunks


@pytest.mark.parametrize('channels', [1, 2])
def test_pipe_encoding_matches_float_wav_and_keeps_sample_timing(tmp_path, channels):
    ffmpeg = pipeline.resolve_ffmpeg()
    if not ffmpeg:
        pytest.skip('FFmpeg unavailable')
    sr = 44100
    samples = np.random.default_rng(12).normal(0, .02, (sr * 3 + 137, channels)).astype(np.float32)
    wav = tmp_path / 'baseline.wav'
    sf.write(wav, samples, sr, subtype='FLOAT')
    baseline = tmp_path / 'baseline.mp3'
    streamed = tmp_path / 'streamed.mp3'
    subprocess.run([ffmpeg, '-y', '-v', 'error', '-i', str(wav), '-vn',
                    '-codec:a', 'libmp3lame', '-b:a', '192k', str(baseline)], check=True)
    encode_float_chunks(ffmpeg, (samples[i:i+10000] for i in range(0, len(samples), 10000)),
                        sr, channels, '192k', streamed)
    assert streamed.read_bytes() == baseline.read_bytes()
    decoded = subprocess.check_output([ffmpeg, '-v', 'error', '-i', str(streamed),
                                       '-f', 'f32le', '-'])
    assert len(decoded) == len(samples) * channels * 4


def test_failed_generator_never_publishes_partial_preview(tmp_path):
    ffmpeg = pipeline.resolve_ffmpeg()
    if not ffmpeg:
        pytest.skip('FFmpeg unavailable')
    def broken():
        yield np.zeros(1000, dtype=np.float32)
        raise ValueError('source disconnected')
    destination = tmp_path / 'preview.mp3'
    with pytest.raises(ValueError, match='source disconnected'):
        encode_float_chunks(ffmpeg, broken(), 44100, 1, '128k', destination)
    assert list(tmp_path.iterdir()) == []
