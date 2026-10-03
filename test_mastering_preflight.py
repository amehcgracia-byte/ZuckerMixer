import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import jam_mix_pipeline as p
import jam_app

class MasteringPreflightTest(unittest.TestCase):
    def test_missing_dependency_fails_before_audio_scan(self):
        with patch.object(p,'MATCHERING_REFERENCE',Path('/missing/reference.wav')), patch.object(p,'ensure_matchering_available'), patch.object(p,'matchering_api',None), patch.object(p,'MATCHERING_IMPORT_ERROR','missing matchering'), patch.object(p,'scan_segment_activity') as scan:
            with self.assertRaisesRegex(RuntimeError,'Reference mastering is unavailable'):
                p.render_segment([],p.Segment(0,30),1,Path('/tmp'))
            scan.assert_not_called()

    def test_no_reference_needs_no_optional_backend(self):
        with patch.object(p,'ensure_matchering_available') as load:
            p.validate_mastering_reference(None)
            load.assert_not_called()

    def test_configured_reference_must_exist(self):
        with patch.object(p,'ensure_matchering_available'), patch.object(p,'matchering_api',object()):
            with self.assertRaisesRegex(RuntimeError,'reference file not found'):
                p.validate_mastering_reference('/missing/reference.wav')

    def test_failed_attempts_are_counted_once_and_review_is_reported(self):
        rows=[{'index':1,'error':'bad'},{'index':2,'file':'ok.mp3'},{'index':3,'error':'bad'}]
        errors=[{'song':1,'error':'bad'},{'song':3,'error':'bad'}]
        result=jam_app.render_batch_summary(5,rows,errors,[{'song':4},{'song':5}])
        self.assertEqual((result['started'],result['completed'],result['failed'],result['needs_review']),(3,1,2,2))
        self.assertEqual(result['review_songs'],[4,5])
        self.assertEqual(len(result['songs']),1)
        self.assertEqual(len(result['errors']),2)

if __name__=='__main__':unittest.main()
