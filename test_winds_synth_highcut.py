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
