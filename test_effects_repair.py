import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import jam_mix_pipeline as p
import jam_app

class EffectsRepairTest(unittest.TestCase):
    def test_delay_repeats_inside_large_chunks_and_is_chunk_invariant(self):
        x=np.zeros((1000,2),np.float32);x[0]=1
        full=p.delay_line_streaming(x,100,.5,{},'test')
        self.assertEqual(full[100,0],1)
        self.assertEqual(full[200,0],.5)
        self.assertEqual(full[300,0],.25)
        state={}
        chunks=np.concatenate([p.delay_line_streaming(a,100,.5,state,'test') for a in np.array_split(x,17)])
        np.testing.assert_allclose(full,chunks,atol=1e-7)

    def test_plate_has_decay_beyond_initial_reflections(self):
        x=np.zeros((88200,2),np.float32);x[0]=1
        wet=p.plate_reverb_streaming(x,44100,1.7,{})
        self.assertGreater(float(np.sum(wet[4410:22050]**2)),.0001)
        state={}
        chunks=np.concatenate([p.plate_reverb_streaming(a,44100,1.7,state) for a in np.array_split(x,19)])
        np.testing.assert_allclose(wet,chunks,atol=1e-6)

    def test_legacy_off_defaults_do_not_disable_automatic_effects(self):
        profile=p.per_song_effect_profile({'v':'vocal'},{},90,.5,{})
        settings=p.resolved_effect_settings({'space_enabled':False,'echo_enabled':False},'vocal',profile)
        self.assertTrue(settings['space_enabled']);self.assertTrue(settings['echo_enabled'])
        manual=p.resolved_effect_settings({'effects_user_confirmed':True,'space_enabled':False,'echo_enabled':False,'fx_enabled':False},'vocal',profile)
        self.assertFalse(manual['space_enabled']);self.assertFalse(manual['echo_enabled']);self.assertFalse(manual['fx_enabled'])

    def test_manual_effect_choice_survives_normalization(self):
        payload={'songs':{'1':{'stems':{'voice.wav':{'effects_user_confirmed':True,'space_enabled':False}}}}}
        self.assertEqual(jam_app.normalize_overrides(payload),payload)

    def test_report_handles_legacy_strings_and_structured_stems(self):
        audit=[{'song':1,'cut_sample':100,'cut_sec':1,'active_instruments':['guitar',{'stem':'bass'}]}]
        with tempfile.TemporaryDirectory() as d, patch.dict(p.DETECTION_STRATEGY,{'final_render_boundary_audit':audit}):
            p.write_report(Path(d),[],[])
            report=next(Path(d).glob('mix_report*')).read_text()
            self.assertIn('guitar, bass',report)

if __name__=='__main__':unittest.main()
