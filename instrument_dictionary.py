"""Instrument dictionary: how each kind of track is mixed.

A track's *role* (kick, bass, vocal, horn...) drives the mixer's hierarchy and balance logic.
An *instrument* refines the processing of that track on top of its role: EQ bands, compressor,
pan, level and effect sends. Montara (or any caller) tells the pipeline which instrument each
track is through ``jam_mix_pipeline.STEM_INSTRUMENTS`` ({file or stem name: instrument key}).

Rules for entries
- Every value is a conservative *starting point* taken from mixing references (see SOURCES);
  per-song analysis and the person's own controls still adjust them.
- ``eq`` uses the pipeline's band format (slot, kind, freq_hz, q, gain_db). Slots "low"
  (high-pass), "mid", "air" and "high" (low-pass) are the ones the person can edit; "fixed"
  bands are not exposed. ``None`` keeps the role's EQ.
- ``compressor`` is {ratio, threshold_db, release_ms}; ``None`` keeps the role's (vocals: none,
  only the downward expander).
- ``level_offset_db`` is added to the automatic level of the role (negative sits further back).
- ``pan`` replaces the role pan for roles whose pan is not enforced; ``"alternate"`` spreads
  several tracks of the same instrument left and right.
- ``reverb_offset_db`` / ``delay_offset_db`` shift the role's sends; ``None`` switches that send off.
"""
from __future__ import annotations

SOURCES = {
    "samson-podcast": "Samson, 'Post Processing for Podcasts' — HPF 80 Hz, intelligibility 300–3,400 Hz, de-ess 6–10 kHz, ratio below 3:1. https://samsontech.com/blog/post-processing-for-podcasts/",
    "izotope-acoustic": "iZotope, 'How to mix acoustic guitar' — cut 200–400 Hz, gentle 4–6 kHz cut, 10 kHz shelf +1–2 dB, low-ratio 'warm hug' compression. https://www.izotope.com/en/learn/how-to-mix-acoustic-guitar",
    "mgm-strings": "Music Guy Mixing, 'How to EQ strings' — HPF (higher for violins), 250 Hz body, 500 Hz mud, ≤2 dB at 2 kHz, harshness near 4 kHz, 8 kHz shelf. https://www.musicguymixing.com/eq-strings/",
    "mgm-bv": "Music Guy Mixing, 'Background vocal EQ' — HPF up to 300 Hz, low-pass 5–6 kHz to sit behind the lead. https://www.musicguymixing.com/background-vocal-eq/",
    "mixingaband-eq": "Mixing a Band, 'How to EQ all instruments' — brass bite 1–2 kHz, woodwinds 2–5 kHz, strings 200–400 Hz body / 5 kHz presence, cymbal harshness 2–5 kHz. https://mixingaband.com/?p=328",
    "plap-14": "Produce Like A Pro (after Owsinski, Mixing Engineer's Handbook) — vocal boom ~240 Hz, presence ~5 kHz. https://producelikeapro.com/blog/how-to-eq-14-common-instruments/",
}

# Montara's track types -> how they are mixed. Entries without overrides use the role as it is.
INSTRUMENTS: dict[str, dict] = {
    "kick": {"role": "kick"},
    "snare": {"role": "snare"},
    "hh": {"role": "hh"},
    "overhead": {"role": "overhead"},
    "drums": {"role": "drums"},
    "percussion": {
        "role": "drums",
        "eq": [("low", "highpass", 100, 0.707, 0.0), ("mid", "peaking", 400, 1.0, -2.0), ("fixed", "peaking", 5000, 0.9, 1.5),
               ("air", "highshelf", 10000, 0.707, 1.0)],
        "compressor": {"ratio": 3.0, "threshold_db": -20.0, "release_ms": 100.0},
        "pan": "alternate", "pan_amount": 0.35, "level_offset_db": -2.0,
        "sources": ["mixingaband-eq"],
        "notes": "Hand percussion: boxiness out, attack and air up, off centre so the kit keeps the middle.",
    },
    "bass": {"role": "bass"},
    "guitar_electric": {"role": "guitar"},
    "guitar_acoustic": {
        "role": "guitar",
        "eq": [("low", "highpass", 90, 0.707, 0.0), ("mid", "peaking", 250, 1.0, -2.5), ("fixed", "peaking", 5000, 1.0, -1.0),
               ("air", "highshelf", 10000, 0.707, 1.5)],
        "compressor": {"ratio": 2.5, "threshold_db": -22.0, "release_ms": 120.0},
        "sources": ["izotope-acoustic", "mixingaband-eq"],
        "notes": "Body swelling 200–400 Hz cut, brittleness 4–6 kHz eased, air shelf; gentle compression.",
    },
    "keys": {"role": "keys"},
    "synth": {"role": "synth"},
    "sax": {"role": "sax"},
    "trumpet": {
        "role": "horn",
        "eq": [("low", "highpass", 150, 0.707, 0.0), ("mid", "peaking", 400, 1.0, -2.0), ("fixed", "peaking", 1500, 1.0, 1.5),
               ("fixed", "peaking", 3500, 1.2, -1.5), ("air", "highshelf", 10000, 0.707, 0.0)],
        "compressor": {"ratio": 2.5, "threshold_db": -20.0, "release_ms": 100.0},
        "pan": -0.25,  # the opposite side of the saxophone's default
        "sources": ["mixingaband-eq"],
        "notes": "Bite at 1–2 kHz, mud and the harsh 2–4 kHz edge trimmed.",
    },
    "trombone": {
        "role": "horn",
        "eq": [("low", "highpass", 80, 0.707, 0.0), ("mid", "peaking", 300, 1.0, -2.0), ("fixed", "peaking", 1200, 1.0, 1.5),
               ("air", "highshelf", 9000, 0.707, 0.0)],
        "compressor": {"ratio": 2.5, "threshold_db": -22.0, "release_ms": 120.0},
        "pan": -0.15,
        "sources": ["mixingaband-eq"],
        "notes": "Lower brass: keeps its low body above 80 Hz, low-mid mud out, bite around 1.2 kHz.",
    },
    "flute": {
        "role": "horn",
        "eq": [("low", "highpass", 250, 0.707, 0.0), ("mid", "peaking", 600, 1.0, -1.5), ("fixed", "peaking", 3000, 0.9, 1.5),
               ("air", "highshelf", 10000, 0.707, 1.0)],
        "compressor": {"ratio": 2.0, "threshold_db": -24.0, "release_ms": 120.0},
        "sources": ["mixingaband-eq"],
        "notes": "Nothing useful below its lowest note (~260 Hz); woodwind presence 2–5 kHz.",
    },
    "violin": {
        "role": "horn",
        "eq": [("low", "highpass", 180, 0.707, 0.0), ("mid", "peaking", 500, 1.0, -2.0), ("fixed", "peaking", 2000, 1.0, 1.0),
               ("fixed", "peaking", 4000, 1.2, -1.5), ("air", "highshelf", 8000, 0.707, 1.0)],
        "compressor": {"ratio": 2.0, "threshold_db": -24.0, "release_ms": 150.0},
        "sources": ["mgm-strings", "mixingaband-eq"],
        "notes": "High-pass under the open G (196 Hz), mud at 500 Hz, at most +1–2 dB of presence, scratch near 4 kHz eased.",
    },
    "strings": {
        "role": "synth",
        "eq": [("low", "highpass", 60, 0.707, 0.0), ("fixed", "peaking", 250, 1.0, 1.5), ("mid", "peaking", 500, 1.0, -2.0),
               ("fixed", "peaking", 2000, 1.0, 1.0), ("air", "highshelf", 8000, 0.707, 1.0)],
        "compressor": {"ratio": 1.5, "threshold_db": -26.0, "release_ms": 200.0},
        "sources": ["mgm-strings"],
        "notes": "Section or pad: low end kept for cellos, body at 250 Hz, mud out, light presence and air.",
    },
    "vocal": {"role": "vocal"},
    "backing_vocal": {
        "role": "vocal",
        "eq": [("low", "highpass", 250, 0.707, 0.0), ("mid", "peaking", 3000, 0.9, 1.0), ("air", "highshelf", 11000, 0.707, 1.0),
               ("high", "lowpass", 7000, 0.707, 0.0)],
        "compressor": {"ratio": 3.0, "threshold_db": -24.0, "release_ms": 120.0},
        "pan": "alternate", "pan_amount": 0.4, "level_offset_db": -4.0, "reverb_offset_db": 2.0,
        "sources": ["mgm-bv"],
        "notes": "Thinner and darker than the lead so the lead keeps its body and edge; compressed harder, spread, a bit more space.",
    },
    "podcast_voice": {
        "role": "vocal",
        "eq": [("low", "highpass", 80, 0.707, 0.0), ("mid", "peaking", 250, 1.0, -1.5), ("fixed", "peaking", 3000, 1.0, 1.5),
               ("fixed", "peaking", 7000, 3.0, -2.0), ("air", "highshelf", 12000, 0.707, 0.5)],
        "compressor": {"ratio": 2.8, "threshold_db": -20.0, "release_ms": 100.0},
        "pan": 0.0, "level_offset_db": 2.0, "reverb_offset_db": None, "delay_offset_db": None,
        "sources": ["samson-podcast", "plap-14"],
        "notes": "Spoken word: rumble out, boom eased, intelligibility 3 kHz, a narrow cut where sibilance sits; dry and centred, on top.",
    },
    "narration": {
        "role": "vocal",
        "eq": [("low", "highpass", 70, 0.707, 0.0), ("mid", "peaking", 300, 1.0, -1.0), ("fixed", "peaking", 3000, 1.0, 1.0),
               ("fixed", "peaking", 7000, 3.0, -2.0), ("air", "highshelf", 12000, 0.707, 0.5)],
        "compressor": {"ratio": 3.0, "threshold_db": -22.0, "release_ms": 120.0},
        "pan": 0.0, "level_offset_db": 2.0, "reverb_offset_db": None, "delay_offset_db": None,
        "sources": ["samson-podcast"],
        "notes": "Narration over music: like the podcast voice, a little warmer and steadier.",
    },
    "room": {"role": "room"},
    "other": {"role": "other"},
}


def profile(instrument: str | None) -> dict | None:
    """The entry for an instrument key, or None."""
    return INSTRUMENTS.get(str(instrument)) if instrument else None
