import unittest
import numpy as np
from unittest.mock import patch
import jam_mix_pipeline as p


class ReferenceEqSafetyTests(unittest.TestCase):
    def test_bounds_boost_preserves_cuts_and_phase(self):
        size = 32768
        frequencies = np.fft.rfftfreq(size, 1 / 44100)
        original = np.where(frequencies < 1000, 0.5, 32.0).astype(complex)
        original *= np.exp(-2j * np.pi * np.arange(len(original)) * 7 / size)
        fir = np.fft.fftshift(np.fft.irfft(original, n=size))
        for channel, low_cap in [('mid', 12), ('side', 6)]:
            with self.subTest(channel=channel):
                result = np.fft.rfft(np.fft.ifftshift(p.bound_reference_eq_fir(fir, 44100, channel)))
                np.testing.assert_allclose(result[frequencies < 1000], original[frequencies < 1000], atol=1e-12)
                self.assertLessEqual(float(np.max(np.abs(result))), 10 ** (low_cap / 20) + 1e-12)
                self.assertLessEqual(float(np.max(np.abs(result[frequencies >= 6000]))), 1 + 1e-12)
                np.testing.assert_allclose(np.angle(result / original), 0, atol=1e-12)

    def test_silent_fir_stays_silent(self):
        np.testing.assert_array_equal(p.bound_reference_eq_fir(np.zeros(2048), 44100, 'side'), np.zeros(2048))

    def test_hook_restored_after_failure(self):
        import matchering.stages as stages
        original = stages.get_fir
        with self.assertRaises(RuntimeError):
            with p.bounded_reference_eq():
                self.assertIsNot(stages.get_fir, original)
                raise RuntimeError('render failure')
        self.assertIs(stages.get_fir, original)

    def test_real_backend_hook_uses_bounded_fir(self):
        import matchering.stages as stages
        from types import SimpleNamespace
        fir = np.zeros(2048); fir[1024] = 32
        with patch.object(stages, 'get_fir', return_value=fir):
            with p.bounded_reference_eq():
                result = stages.get_fir(None, None, 'side', SimpleNamespace(internal_sample_rate=44100))
        self.assertLessEqual(float(np.max(np.abs(np.fft.rfft(np.fft.ifftshift(result))))), 10 ** (6 / 20) + 1e-12)


class UnequalTrackDurationTests(unittest.TestCase):
    def test_balance_uses_common_duration(self):
        roles = {'v': 'vocal', 'k': 'keys', 'g': 'guitar'}
        levels = {'v': -30., 'k': -32., 'g': -32.}
        for vocal_length, keys_length in [(752, 600), (600, 906)]:
            with self.subTest(lengths=(vocal_length, keys_length)):
                envelopes = {'v': np.ones(vocal_length) * .1,
                             'k': np.ones(keys_length) * .1,
                             'g': np.ones(keys_length) * .1}
                result = p.vocal_harmonic_balance(roles, levels, envelopes)
                self.assertEqual(result['vocal_harmonic_overlap_fraction'], 1.)
                p.per_song_role_balance_corrections(roles, levels, envelopes)
