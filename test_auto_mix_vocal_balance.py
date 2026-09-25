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

    def test_role_corrections_are_per_song_and_only_for_overlapping_harmonics(self):
        roles = {
            "Vox L.wav": "vocal",
            "Vox R.wav": "vocal",
            "Guitar.wav": "guitar",
            "Piano.wav": "keys",
            "Bass.wav": "bass",
        }
        levels = {
            "Vox L.wav": -24.0,
            "Vox R.wav": -24.0,
            "Guitar.wav": -14.0,
            "Piano.wav": -18.0,
            "Bass.wav": -20.0,
        }
        envelopes = {name: pipeline.np.ones(32, dtype=pipeline.np.float32) for name in roles}
        result = pipeline.per_song_role_balance_corrections(roles, levels, envelopes)
        self.assertEqual(result["vocal_pair_corrections_db"]["Vox L.wav"], 0.0)
        self.assertEqual(result["vocal_pair_corrections_db"]["Vox R.wav"], 0.0)
        self.assertEqual(result["role_corrections_db"]["Guitar.wav"], -6.0)
        self.assertEqual(result["role_corrections_db"]["Piano.wav"], -3.0)
        self.assertEqual(result["role_corrections_db"].get("Bass.wav", 0.0), 0.0)

    def test_vocal_pair_is_equalized_from_active_level_per_song(self):
        roles = {"Mic L.wav": "vocal", "Mic R.wav": "vocal", "Keys.wav": "keys"}
        levels = {"Mic L.wav": -28.0, "Mic R.wav": -34.0, "Keys.wav": -24.0}
        envelopes = {name: pipeline.np.ones(16, dtype=pipeline.np.float32) for name in roles}
        result = pipeline.per_song_role_balance_corrections(roles, levels, envelopes)
        self.assertAlmostEqual(result["vocal_pair_corrections_db"]["Mic L.wav"], -3.0)
        self.assertAlmostEqual(result["vocal_pair_corrections_db"]["Mic R.wav"], 3.0)

    def test_already_balanced_harmonics_are_not_trimmed(self):
        roles = {"Vox.wav": "vocal", "Guitar.wav": "guitar", "Piano.wav": "keys"}
        levels = {"Vox.wav": -16.0, "Guitar.wav": -20.0, "Piano.wav": -21.0}
        envelopes = {name: pipeline.np.ones(16, dtype=pipeline.np.float32) for name in roles}
        result = pipeline.per_song_role_balance_corrections(roles, levels, envelopes)
        self.assertEqual(result["role_corrections_db"]["Guitar.wav"], 0.0)
        self.assertEqual(result["role_corrections_db"]["Piano.wav"], 0.0)

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
