"""Winds lead gently, synth brands are synths, and bass has an adjustable high cut."""
import unittest

import jam_mix_pipeline as pipeline


class WindsSynthHighCut(unittest.TestCase):
    def test_wind_led_song_gets_small_boost_and_small_keys_cut(self):
        roles = {"sax.wav": "sax", "keys.wav": "keys"}
        balance = pipeline.vocal_harmonic_balance(roles, {"sax.wav": -30.0, "keys.wav": -28.0})
        self.assertTrue(balance["winds_lead"])
        self.assertLessEqual(balance["vocal_group_correction_db"], pipeline.WIND_LEAD_MAX_BOOST_DB)
        self.assertGreaterEqual(balance["harmonic_group_correction_db"], -pipeline.WIND_LEAD_MAX_HARMONIC_CUT_DB)

    def test_voice_leads_and_winds_are_not_boosted_with_it(self):
        roles = {"vox.wav": "vocal", "sax.wav": "sax", "keys.wav": "keys"}
        balance = pipeline.vocal_harmonic_balance(roles, {"vox.wav": -30.0, "sax.wav": -30.0, "keys.wav": -28.0})
        self.assertFalse(balance["winds_lead"])
        self.assertEqual(balance["vocal_names"], ["vox.wav"])
        self.assertGreater(balance["vocal_group_correction_db"], pipeline.WIND_LEAD_MAX_BOOST_DB)

    def test_synth_brands_are_synths_not_overheads_or_bass(self):
        for name in ("Novation", "NovationL", "Bass Station", "Nord Stage 3", "Moog Sub 37", "Prophet 6"):
            self.assertEqual(pipeline.classify_role(name), "synth", name)
        for name, role in (("OVL", "overhead"), ("Bass", "bass"), ("KeysL", "keys_l"), ("northern lights", "other")):
            self.assertEqual(pipeline.classify_role(name), role, name)

    def test_bass_high_cut_default_override_and_off(self):
        self.assertEqual(pipeline.role_eq_defaults("bass")["eq_high_cut_hz"], 6000.0)
        self.assertEqual(pipeline.role_eq_defaults("keys")["eq_high_cut_hz"], 20000.0)
        bands = pipeline.role_eq_bands("bass", {"eq_high_cut_hz": 3500.0})
        self.assertIn(("high", "lowpass", 3500.0, 0.707, 0.0), bands)
        self.assertFalse(any(b[0] == "high" for b in pipeline.role_eq_bands("bass", {"eq_high_cut_hz": 20000.0})))
        self.assertIn(("high", "lowpass", 8000.0, 0.707, 0.0), pipeline.role_eq_bands("keys", {"eq_high_cut_hz": 8000.0}))


if __name__ == "__main__":
    unittest.main()


class KeysNotBuried(unittest.TestCase):
    def test_failed_beat_grid_and_keys_out_of_key_give_no_cuts(self):
        roles = {"bd": "kick", "bass": "bass", "keys": "keys_r", "sax": "sax"}
        flagged = {"flagged": True, "attenuation_db": -3.0, "reason": "poor beat-grid alignment"}
        rhythm = {name: dict(flagged) for name in roles}
        harmonic = {"keys": {"flagged": True, "attenuation_db": -3.0}, "sax": {"flagged": True, "attenuation_db": -3.0}}
        rhythm, harmonic = pipeline.trusted_rhythm_harmonic_controls(rhythm, harmonic, roles)
        self.assertTrue(all(info["attenuation_db"] == 0.0 for info in rhythm.values()))
        self.assertEqual(harmonic["keys"]["attenuation_db"], 0.0)
        self.assertEqual(harmonic["sax"]["attenuation_db"], -3.0)  # only harmony instruments are spared

    def test_a_few_off_grid_tracks_keep_their_cut(self):
        roles = {"a": "kick", "b": "bass", "c": "keys", "d": "sax"}
        rhythm = {"a": {"flagged": True, "attenuation_db": -3.0}, "b": {"flagged": False, "attenuation_db": 0.0},
                  "c": {"flagged": False, "attenuation_db": 0.0}, "d": {"flagged": False, "attenuation_db": 0.0}}
        rhythm, _ = pipeline.trusted_rhythm_harmonic_controls(rhythm, {}, roles)
        self.assertEqual(rhythm["a"]["attenuation_db"], -3.0)

    def test_keys_cuts_are_bounded(self):
        self.assertEqual(pipeline.bounded_harmonic_cut("keys_l", -3.0, -3.0, -4.0), pipeline.MAX_HARMONIC_AUTOMATIC_CUT_DB)
        self.assertEqual(pipeline.bounded_harmonic_cut("sax", -3.0, -3.0), -6.0)
