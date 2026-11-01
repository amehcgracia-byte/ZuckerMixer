"""Instrument dictionary: entries are valid and the pipeline applies them per track."""
import numpy as np

import instrument_dictionary as dictionary
import jam_mix_pipeline as pipeline

ROLES = {"kick", "snare", "hh", "overhead", "drums", "bass", "guitar", "keys", "synth", "sax", "horn", "vocal", "room", "other"}
SLOTS = {"low", "mid", "air", "high", "fixed"}


def test_every_entry_is_well_formed_and_cites_its_sources():
    for key, entry in dictionary.INSTRUMENTS.items():
        assert entry["role"] in ROLES, key
        for slot, kind, freq, q, gain in entry.get("eq") or []:
            assert slot in SLOTS and kind in {"highpass", "lowpass", "peaking", "highshelf", "lowshelf"} and 20 <= freq <= 20000 and abs(gain) <= 6, key
        if entry.get("eq"):
            assert entry.get("sources") and all(s in dictionary.SOURCES for s in entry["sources"]), key
        comp = entry.get("compressor")
        if comp:
            assert 1 <= comp["ratio"] <= 4 and -40 <= comp["threshold_db"] <= -10, key


def test_pipeline_uses_the_dictionary_only_for_marked_tracks():
    pipeline.STEM_INSTRUMENTS = {"Host.wav": "podcast_voice", "BV1.wav": "backing_vocal", "BV2.wav": "backing_vocal", "Trp.wav": "trumpet"}
    try:
        assert pipeline.role_eq_bands("vocal", name="Host")[0][2] == 80          # podcast high-pass, not the singer's 115 Hz
        assert pipeline.role_eq_bands("vocal", name="Lead")[0][2] == 115         # unmarked track: role EQ
        assert pipeline.role_eq_defaults("vocal", "BV1.wav")["eq_high_cut_hz"] == 7000
        assert pipeline.reverb_send_level_db("vocal", "Host.wav") is None        # spoken voice: dry
        assert pipeline.reverb_send_level_db("vocal", "BV1.wav") == pipeline.reverb_send_level_db("vocal") + 2
        assert {pipeline.pan_for_role("vocal", "BV1"), pipeline.pan_for_role("vocal", "BV2")} == {-0.4, 0.4}
        assert pipeline.pan_for_role("horn", "Trp") == -0.25
        lead = pipeline.per_song_auto_mix_gain_db("vocal", -30.0, -24.0)
        assert pipeline.per_song_auto_mix_gain_db("vocal", -30.0, -24.0, "BV1.wav") == lead - 4
        # The speech chain compresses after the expander: loud speech comes out quieter than without it.
        sr = 22050
        voice = (0.5 * np.sin(2 * np.pi * 300 * np.arange(sr) / sr)).astype(np.float32)
        plain = pipeline.process_track_streaming(voice, sr, "vocal", 0.0, {}, "Lead.wav:0")
        spoken = pipeline.process_track_streaming(voice, sr, "vocal", 0.0, {}, "Host.wav:0")
        assert np.sqrt(np.mean(spoken[sr // 2:] ** 2)) < np.sqrt(np.mean(plain[sr // 2:] ** 2))
    finally:
        pipeline.STEM_INSTRUMENTS = {}
