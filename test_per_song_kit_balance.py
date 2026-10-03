import unittest
import numpy as np
import jam_mix_pipeline as p

class PerSongKitBalance(unittest.TestCase):
    roles={'kick':'kick','snare':'snare','hh':'hh','ov':'overhead','guitar':'guitar','bass':'bass','keys':'keys'}
    def balance(self,levels):
        return p.per_song_rhythm_harmonic_gains(self.roles,levels,{k:np.full(16,p.db_to_amp(v)) for k,v in levels.items()})
    def test_different_drummers_receive_different_gains(self):
        loud={'kick':-15,'snare':-20,'hh':-22,'ov':-20,'guitar':-30,'bass':-30,'keys':-30}
        quiet={**loud,'kick':-40,'snare':-40,'hh':-46,'ov':-44}
        a=self.balance(loud);b=self.balance(quiet)
        self.assertLess(a['gains_db']['kick'],b['gains_db']['kick'])
        self.assertGreater(b['gains_db']['kick'],0)
        self.assertLessEqual(b['gains_db']['kick'],3)
    def test_complete_kit_cannot_overwhelm_musical_reference(self):
        x=self.balance({'kick':-15,'snare':-15,'hh':-15,'ov':-15,'guitar':-30,'bass':-30,'keys':-30})
        self.assertLessEqual(x['kit_level_after_bus_db'],x['reference_db']-1+1e-8)
        self.assertLess(x['kit_trim_db'],0)
    def test_quiet_guitar_recovers_without_fixed_cut(self):
        x=self.balance({'kick':-20,'snare':-25,'hh':-32,'ov':-30,'guitar':-36,'bass':-30,'keys':-30})
        self.assertGreater(x['gains_db']['guitar'],0)
        self.assertLessEqual(x['gains_db']['guitar'],4)
    def test_inactive_inputs_do_not_change_the_reference_or_get_boosted(self):
        levels={'kick':-20,'snare':-25,'hh':-32,'ov':-30,'guitar':-36,'bass':-30,'keys':-30}
        x=self.balance(levels)
        roles={**self.roles,'unused':'sax'};levels['unused']=-95
        y=p.per_song_rhythm_harmonic_gains(roles,levels)
        self.assertEqual(x['reference_db'],y['reference_db'])
        levels['guitar']=-95;z=self.balance(levels);self.assertEqual(z['gains_db']['guitar'],0)
    def test_missing_evidence_does_not_invent_an_auto_mix(self):
        x=p.per_song_rhythm_harmonic_gains(self.roles,{name:-40 for name in self.roles},{name:np.zeros(32) for name in self.roles})
        self.assertIsNone(x['reference_db']);self.assertEqual(set(x['gains_db'].values()),{0.0})

if __name__=='__main__':unittest.main()

class ManualMixControls(unittest.TestCase):
    def test_normalization_keeps_confirmed_fader_and_manual_makeup(self):
        import jam_app as a
        original={'songs':{'2':{'stems':{'Guitar.wav':{'fader_db':-2.,'user_confirmed':True,'manual_makeup_gain_db':True,'makeup_gain_db':1.}}}}}
        clean=a.normalize_overrides(original)['songs']['2']['stems']['Guitar.wav']
        self.assertTrue(clean['user_confirmed']);self.assertTrue(clean['manual_makeup_gain_db'])
        self.assertEqual(a.persisted_fader_values(clean),(-2.,0.,True))
    def test_missing_plan_prepares_only_the_requested_song(self):
        import jam_app as a
        from unittest.mock import patch
        state={'raw_songs':[]}
        plan={'stems':{'Guitar.wav':{'makeup_gain_db':2.,'user_fader_db':-1.,'user_gain_db':0.}}}
        snapshot={'songs':{'2':{'stems':{'Guitar.wav':{'fader_db':-1.,'user_confirmed':True}}}}}
        with patch.object(a,'load_mix_plan',return_value=None), patch.object(a,'canonical_mix_params_for_song',return_value=plan) as prepare, patch.object(a,'load_settings',return_value={}), patch.object(a,'app_progress'), patch.object(p,'validate_mastering_reference'), patch.object(p,'MIX_OVERRIDES',{}):
            result=a.apply_overrides_for_song(2,2,overrides_snapshot=snapshot,state_snapshot=state)
            prepare.assert_called_once_with(2,state_snapshot=state,overrides_snapshot=snapshot)
            self.assertIs(result,plan)
            self.assertEqual(p.MIX_OVERRIDES['songs']['2']['stems']['Guitar.wav']['auto_mix_gain_db'],2.)

class ExactPreviewAnalysis(unittest.TestCase):
    def test_preview_keeps_whole_song_analysis_and_correct_time_offset(self):
        cache={'song_id':2,'selection':{'start_sec':100.,'end_sec':700.}}
        clip=p.Segment(300.,330.)
        self.assertEqual(p.analysis_window_offset(cache,clip,2,{'start_sec':300.,'end_sec':330.}),200.)
        with self.assertRaises(RuntimeError):p.analysis_window_offset(cache,clip,2)
        with self.assertRaises(RuntimeError):p.analysis_window_offset(cache,clip,3,{'start_sec':300.,'end_sec':330.})
        with self.assertRaises(RuntimeError):p.analysis_window_offset(cache,p.Segment(690.,720.),2,{'start_sec':690.,'end_sec':720.})
