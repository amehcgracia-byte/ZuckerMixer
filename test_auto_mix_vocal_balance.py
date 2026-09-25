import unittest

import jam_mix_pipeline as pipeline


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
        self.assertEqual(pipeline.vocal_pair_key("vox L_1.wav"), "vox")
        self.assertEqual(pipeline.vocal_pair_key("vox R_1.wav"), "vox")

    def test_explicit_vocal_stem_is_not_reclassified_by_spectral_heuristic(self):
        self.assertEqual(pipeline.classify_role("vox 1_1.wav"), "vocal")
        self.assertEqual(pipeline.classify_role("Vox 2_1.wav"), "vocal")


if __name__ == "__main__":
    unittest.main()
