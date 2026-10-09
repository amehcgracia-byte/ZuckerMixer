import jam_app as a
import jam_mix_pipeline as p


def test_confirmed_fader_and_gain_survive_api_normalization_and_render_preparation(monkeypatch):
    payload = {'songs': {'12': {'stems': {'Synth.wav': {
        'fader_db': 6.0, 'gain_db': 9.0, 'user_confirmed': True,
    }}}}}
    snapshot = a.normalize_overrides(payload)
    monkeypatch.setattr(a, 'load_mix_plan', lambda *args, **kwargs: {
        'stems': {'Synth.wav': {'user_fader_db': 6., 'user_gain_db': 9., 'makeup_gain_db': -10.}},
    })
    monkeypatch.setattr(a, 'load_settings', lambda: {})
    monkeypatch.setattr(p, 'validate_mastering_reference', lambda *args: None)
    monkeypatch.setattr(p, 'MIX_OVERRIDES', {})
    monkeypatch.setattr(p, 'MIX_OVERRIDE_VERIFY_TRACE', {})
    a.apply_overrides_for_song(12, 12, overrides_snapshot=snapshot,
                               state_snapshot={'raw_songs': []})
    settings = p.MIX_OVERRIDES['songs']['12']['stems']['Synth.wav']
    assert settings['user_confirmed'] is True
    assert settings['fader_db'] == 6.
    assert settings['gain_db'] == 9.
    assert settings['auto_mix_gain_db'] == -10.
    assert p.has_confirmed_manual_levels(p.MIX_OVERRIDES['songs']['12'])


def test_untrusted_legacy_levels_do_not_disable_automatic_balance():
    assert not p.has_confirmed_manual_levels({'stems': {'Synth.wav': {'fader_db': 6}}})
    assert not p.has_confirmed_manual_levels({'stems': {'Synth.wav': {'fader_db': 0, 'user_confirmed': True}}})
    assert p.has_confirmed_manual_levels({'stems': {'Synth.wav': {'fader_db': 6, 'user_confirmed': True}}})
    assert p.has_confirmed_manual_levels({'stems': {'Synth.wav': {'gain_db': 2, 'user_fader_confirmed': True}}})
