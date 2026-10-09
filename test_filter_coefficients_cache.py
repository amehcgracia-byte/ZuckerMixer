import numpy as np
import pytest
import jam_mix_pipeline as p


@pytest.mark.parametrize('kind', ['highpass', 'lowpass', 'lowshelf', 'highshelf', 'peaking', 'notch'])
def test_cached_coefficients_match_engine_and_are_independent(kind):
    p._cached_sos.cache_clear()
    expected = p._cached_sos.__wrapped__(48000, kind, 1000, .9, 2.)
    first = p.make_sos(48000, kind, 1000, .9, 2.)
    np.testing.assert_array_equal(first, expected)
    first[:] = 0
    np.testing.assert_array_equal(p.make_sos(48000, kind, 1000, .9, 2.), expected)
    assert p._cached_sos.cache_info().misses == 1
    assert not np.array_equal(p.make_sos(48000, kind, 2000, .9, 2.), expected)
