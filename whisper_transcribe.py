#!/usr/bin/env python3
"""Offline faster-whisper batch worker used by Zucker Mixer detection."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--model", default="base")
    parser.add_argument("--model-dir", type=Path, required=True)
    args = parser.parse_args()

    from faster_whisper import WhisperModel

    requests = json.loads(args.input_json.read_text(encoding="utf-8"))
    total = len(requests)
    print(json.dumps({"event": "model_loading", "total": total}), flush=True)
    model = WhisperModel(
        args.model,
        device="cpu",
        compute_type="int8",
        download_root=str(args.model_dir),
    )
    print(json.dumps({"event": "model_ready", "total": total}), flush=True)

    results = []

    def checkpoint() -> None:
        temporary = args.output_json.with_name(args.output_json.name + ".partial")
        temporary.write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(args.output_json)

    for completed, request in enumerate(requests, 1):
        segments, info = model.transcribe(
            str(request["path"]),
            beam_size=3,
            vad_filter=True,
            condition_on_previous_text=False,
            word_timestamps=True,
        )
        pieces = []
        no_speech = []
        logprobs = []
        whisper_segments = []
        for item in segments:
            text = str(item.text or "").strip()
            if text:
                pieces.append(text)
            no_speech.append(float(getattr(item, "no_speech_prob", 0.0)))
            logprobs.append(float(getattr(item, "avg_logprob", -1.0)))
            whisper_segments.append({
                "start": float(item.start),
                "end": float(item.end),
                "text": text,
                "no_speech_prob": no_speech[-1],
                "avg_logprob": logprobs[-1],
                "words": [
                    {"word": str(word.word or ""), "start": float(word.start), "end": float(word.end)}
                    for word in (getattr(item, "words", None) or [])
                ],
            })
        results.append({
            "id": request["id"],
            "start": request["start"],
            "end": request["end"],
            "text": " ".join(pieces).strip(),
            "language": str(getattr(info, "language", "unknown")),
            "language_probability": float(getattr(info, "language_probability", 0.0)),
            "speech_confidence": float(1.0 - (sum(no_speech) / len(no_speech) if no_speech else 1.0)),
            "avg_logprob": float(sum(logprobs) / len(logprobs) if logprobs else -1.0),
            "segments": whisper_segments,
        })
        checkpoint()
        print(
            json.dumps({
                "event": "window_complete",
                "completed": completed,
                "total": total,
                "id": request["id"],
                "start": request["start"],
                "end": request["end"],
            }),
            flush=True,
        )

    checkpoint()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
