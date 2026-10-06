## 2.8.1

- Scan detection stems with up to four bounded workers and reuse individual envelopes when files and their alignment remain unchanged.
- Preserve exact sequential detection results; recover corrupt cache entries and report aggregate scan progress.
- Include the faster cut editor, complete saved-song counts, automatic per-song render preparation and correct successful batch status from 2.1.71.

## 2.1.70

- Save both cut edges atomically, including the first and last edges; persist a source-fingerprinted manual timeline across restarts and project changes.
- Add cuts in uncovered opening, interior and ending regions. Preserve short and long manual selections, with duration warnings instead of blocked saves.
- Recover previously saved opening cuts when a later automatic snapshot omitted them. Preserve opening music during acoustic detection.
- Support song 0 with independent internal slot IDs and source-scoped numbering. Keep names, skip decisions and mix settings attached when cuts are inserted or undone.
- Stream ranged source preview audio without preparing whole-song WAV files. Space toggles playback, arrows seek, wheel zooms, Command/Control-wheel pans. Keep native text-field shortcuts and add Command/Control-S to save.
- Ask Yes/No to save pending edits when closing or changing slots, and visibly acknowledge successful saves.

## 2.1.69

- Compare vocal and instrument activity over their common duration when recordings end at different times, preventing batch failures.

- Bound reference mastering EQ to avoid raising recording hiss, especially in the stereo side signal. Preserve reference cuts, phase and instrument panning; taper positive high-frequency boosts to unity.
- Full-song listening comparison for “Much to me”: listener confirmed the snow noise disappears with bounded reference EQ after bass gating.

## 2.1.68

- Automatically gate a bass input with a detected noise floor during long non-playing passages, while preserving musical frames and explicit Gate choices.
- Connect instrument Gate to Play Preview, including seek timing and FX bypass, using the same gate curve as export.
- Derive gate activity from each song rather than an absolute input level, so softly recorded instruments remain audible.
- Process audio in 30-second blocks instead of 5-minute blocks to bound mix memory; retain existing analysis caches.
- Invalidate old preview audio caches and test Gate timing, quiet players, and explicit choices.

## 2.1.67

- Replace the Play Preview microphone compressor with a real downward expander. Preserve loud vocals and attenuate quiet microphone floor, with independent state per channel.
- Enable vocal expansion whenever vocal FX is enabled, matching the export path instead of bypassing it behind the unrelated section-gate toggle.
- Measure the summed stereo preview signal instead of averaging across tracks; silent channels no longer increase normalization gain.
- Add silence, quiet-noise, loud-voice, channel-independence and summed-level checks to desktop CI.

## 2.1.66

- Treat low raw noise floors before the large final mastering lift; preview and export share noise treatment. Correct spectral profile scaling so musical content is preserved.
- Preserve quiet-source precision in Play Preview: float WAV staging, normalized encoding with source-level restoration, and a fresh cache format.
- Includes shared noise-only input safety, wrapping Fine Tune channels and the Play Preview label from 2.1.65.

## 2.1.65

- Silence stationary broadband noise inputs even when their level is high.
- Measure noise away from resampling roll-off and sample across the song to preserve later instrument entries.
- Invalidate prior automatic mix analysis so the new safety decision is applied.

# Changes

## 2.1.64

- Show registered renders saved to user-selected external folders in Finished Mixes.
- Includes the Windows startup/cancellation repairs and every-song render fixes below.

## 2.1.63

- Render every requested, non-skipped song, including song 1 and songs with boundary warnings.
- Preserve exact selected windows; final audit no longer moves cuts behind the editor, preventing analysis/render window mismatches.
- Continue after individual render failures and count every attempt exactly once.
- Include the independent per-song drum/guitar balance, automatic effects, preview caching, shorter reports and ASCII X closures from 2.1.62.
- Update Windows packaging, frozen Matchering verification, user/build documentation and release artifacts.
