"""The UI opens without importing DSP; first use retains the real engine."""
import subprocess
import sys
import numpy as np
from dsp_imports import LazyDspModule


def test_interface_import_defers_scientific_dsp():
    result = subprocess.run([sys.executable, '-c',
        "import sys; import jam_app; assert 'scipy.signal' not in sys.modules; assert 'pyloudnorm' not in sys.modules"],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_first_filter_uses_real_scipy_and_reuses_module():
    from scipy import signal
    lazy = LazyDspModule('signal')
    expected = signal.butter(2, 1000, fs=48000, output='sos')
    np.testing.assert_array_equal(lazy.butter(2, 1000, fs=48000, output='sos'), expected)
    assert lazy.module is signal
    assert lazy.sosfilt is signal.sosfilt


def test_loudness_meter_is_original_library():
    import pyloudnorm
    lazy = LazyDspModule('pyloudnorm')
    assert lazy.Meter is pyloudnorm.Meter
