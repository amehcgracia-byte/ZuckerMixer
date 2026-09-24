import unittest

import jam_mix_pipeline as pipeline
from jam_app import stem_display_label


class DummyStem:
    def __init__(self, name: str):
        self.path = type("PathLike", (), {"name": name})()


class StemLabelTest(unittest.TestCase):
    def test_logic_export_roles(self):
        cases = {
            "2016072026(01_00_00 - 10_59_02)BDJAM.tracks": "kick",
            "2016072026(01_00_00 - 10_59_02)SnareJAM.tracks": "snare",
            "2016072026(01_00_00 - 10_59_02)OVJAM.tracks": "drums",
            "2016072026(01_00_00 - 10_59_02)BassJAM.tracks": "bass",
            "2016072026(01_00_00 - 10_59_02)GuitJAM.tracks": "guitar",
            "2016072026(01_00_00 - 10_59_02)Keys JAM.tracks": "keys",
            "2016072026(01_00_00 - 10_59_02)Mic1JAM.tracks": "vocal",
            "2016072026(01_00_00 - 10_59_02)SynthJAM.tracks": "synth",
            "2016072026(01_00_00 - 10_59_02)SaxJAM.tracks": "sax",
        }
        for name, role in cases.items():
            with self.subTest(name=name):
                self.assertEqual(pipeline.classify_role(name), role)

    def test_ui_labels(self):
        cases = {
            "BDJAM.tracks.wav": "BD",
            "SnareJAM.tracks.wav": "Snare",
            "OVJAM.tracks.wav": "OV",
            "BassJAM.tracks.wav": "Bass",
            "GuitJAM.tracks.wav": "Guit",
            "Keys JAM.tracks.wav": "Keys ",
            "Mic1JAM.tracks.wav": "Mic1",
            "Mic2JAM.tracks.wav": "Mic2",
            "Mic3JAM.tracks.wav": "Mic3",
            "SynthJAM.tracks.wav": "Synth",
            "SaxJAM.tracks.wav": "Sax",
            "Audio 10JAM.tracks.wav": "Audio 10",
            "2016072026(01_00_00 - 10_59_02)BDJAM.tracks.wav": "BD",
        }
        for name, label in cases.items():
            with self.subTest(name=name):
                self.assertEqual(stem_display_label(DummyStem(name)), label)


if __name__ == "__main__":
    unittest.main()
