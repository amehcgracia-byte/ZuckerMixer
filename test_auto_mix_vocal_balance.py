import unittest

import jam_mix_pipeline as pipeline
from jam_app import persisted_fader_values


class AutoMixVocalBalanceTests(unittest.TestCase):
    def test_vocal_pair_is_one_logical_group(self):
        roles = {
            "Vox L.wav": "vocal",
            "Vox R.wav": "vocal",
            "Keys L.wav": "keys_l",
            "Keys R.wav": "keys_r",
        }
        levels = {
            "Vox L.wav": -28.0,
            "Vox R.wav": -29.0,
            "Keys L.wav": -18.0,
            "Keys R.wav": -18.0,
        }
        result = pipeline.vocal_harmonic_balance(roles, levels)
        self.assertEqual(result["vocal_names"], ["Vox L.wav", "Vox R.wav"])
        self.assertGreater(result["vocal_group_correction_db"], 0.0)
        self.assertLessEqual(result["vocal_group_correction_db"], 3.0)
        self.assertLessEqual(result["harmonic_group_correction_db"], -2.0)

    def test_balanced_song_gets_no_vocal_or_harmonic_boost(self):
        roles = {"vox.wav": "vocal", "keys.wav": "keys", "guit.wav": "guitar"}
        levels = {"vox.wav": -17.0, "keys.wav": -24.0, "guit.wav": -24.0}
        result = pipeline.vocal_harmonic_balance(roles, levels)
        self.assertEqual(result["vocal_group_correction_db"], 0.0)
        self.assertEqual(result["harmonic_group_correction_db"], 0.0)

    def test_pair_key_preserves_left_right_identity(self):
        self.assertEqual(pipeline.vocal_pair_key("vox L_1.wav"), "vocal_pair")
        self.assertEqual(pipeline.vocal_pair_key("vox R_1.wav"), "vocal_pair")
        self.assertEqual(pipeline.vocal_pair_key("Vox 1_1.wav"), "vocal_pair")
        self.assertEqual(pipeline.vocal_pair_key("Vox 2_1.wav"), "vocal_pair")

    def test_explicit_vocal_stem_is_not_reclassified_by_spectral_heuristic(self):
        self.assertEqual(pipeline.classify_role("vox 1_1.wav"), "vocal")
        self.assertEqual(pipeline.classify_role("Vox 2_1.wav"), "vocal")

    def test_legacy_fader_is_preserved_but_not_applied(self):
        trusted, legacy, confirmed = persisted_fader_values({"fader_db": -60.0})
        self.assertEqual(trusted, 0.0)
        self.assertEqual(legacy, -60.0)
        self.assertFalse(confirmed)
        trusted, legacy, confirmed = persisted_fader_values({"fader_db": -6.0, "user_confirmed": True})
        self.assertEqual(trusted, -6.0)
        self.assertEqual(legacy, 0.0)
        self.assertTrue(confirmed)


if __name__ == "__main__":
    unittest.main()
