import tempfile, threading, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import jam_app as a
import jam_mix_pipeline as p

class LoadPerformanceTest(unittest.TestCase):
    def test_analysis_is_reused_and_cut_changes_invalidate_it(self):
        stem=SimpleNamespace(samplerate=44100,path=Path('voice.wav'))
        state={'stems':[stem]};segment=p.Segment(0,30)
        values={'voice.wav':-20.};scan_result=(values,{'voice.wav':1.},{'voice.wav':True},{'voice.wav':0.},{'voice.wav':np.ones(30,np.float32)},values)
        with tempfile.TemporaryDirectory() as d, patch.object(a,'ACTIVE_SOURCE_STATE_ROOT',Path(d)), patch.object(a,'detection_state_signature',return_value=('source',None,())), patch.object(a,'load_source_config',return_value={}), patch.object(p,'scan_segment_activity',return_value=scan_result) as scan, patch.object(p,'role_norms_from_detection_cache',return_value={}), patch.object(p,'analyze_song_mix_controls',return_value={}), patch.object(p,'classify_noise_stems',return_value={}), patch.object(p,'build_per_song_flattening',return_value={}), patch.object(p,'estimate_segment_drum_bpm',return_value=(90.,.5)):
            first=a.song_analysis_snapshot(state,segment,1)
            second=a.song_analysis_snapshot(state,segment,1)
            self.assertEqual(scan.call_count,1)
            np.testing.assert_array_equal(first['segment_envelopes']['voice.wav'],second['segment_envelopes']['voice.wav'])
            a.song_analysis_snapshot(state,p.Segment(0,31),1)
            self.assertEqual(scan.call_count,2)

    def test_ui_build_does_not_invalidate_dsp_but_source_changes_do(self):
        with patch.object(a,'detection_state_signature',return_value=('source',None,())) as identity, patch.object(a,'load_source_config',return_value={}):
            first=a.mix_plan_signature(1,p.Segment(0,30),{})
            with patch.dict(a.BUILD_METADATA,{'source_revision':'ui-change'}):self.assertEqual(first,a.mix_plan_signature(1,p.Segment(0,30),{}))
            identity.return_value=('source',None,(('voice.wav',1,2),))
            self.assertNotEqual(first,a.mix_plan_signature(1,p.Segment(0,30),{}))

    def test_waveform_uses_cache_without_reading_wavs(self):
        stem=SimpleNamespace(path=Path('voice.wav'),offset_seconds=0.)
        with tempfile.TemporaryDirectory() as d, patch.object(a,'WAVEFORM_CACHE_PATH',Path(d)/'wave.json'), patch.object(a,'_waveform_identity',return_value={'source':'test'}), patch.object(a,'ensure_pipeline_state',return_value={'stems':[stem]}), patch.object(p,'load_detection_cache',return_value={'voice.wav':np.array([0.,1.,.5,0.],np.float32)}), patch.object(a.sf,'SoundFile') as read:
            waveform=a.slot_waveform(0,4,8)
            self.assertEqual(max(waveform['peaks']),1.)
            read.assert_not_called()
            self.assertTrue(a.slot_waveform(0,4,8)['cached'])

    def test_reference_import_does_not_block_state(self):
        done=threading.Event()
        with patch.object(a,'reference_backend_thread',None), patch.object(p,'matchering_api',None), patch.object(p,'MATCHERING_IMPORT_ERROR',''), patch.object(p,'ensure_matchering_available',side_effect=lambda:done.wait(2)):
            start=time.perf_counter();status=a.reference_mastering_status({'matchering_reference':'ref.wav'});thread=a.reference_backend_thread
            try:
                self.assertLess(time.perf_counter()-start,.2)
                self.assertTrue(status['loading'])
            finally:done.set();thread.join(2)

    def test_request_timing_is_available(self):
        response=a.app.test_client().get('/api/performance')
        self.assertEqual(response.status_code,200)
        self.assertIn('app;dur=',response.headers['Server-Timing'])

if __name__=='__main__':unittest.main()
