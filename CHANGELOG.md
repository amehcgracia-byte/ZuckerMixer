## 2.8.8

- Preserve manual fader/gain confirmation in both live Fine Tune state and the persisted render snapshot. Previously the preview state overwrote the confirmation marker, causing Render to discard edited levels as untrusted legacy values.
- Confirmed manual levels are applied after automatic balancing; automatic synth/vocal hierarchy ceilings no longer undo a user’s Fine Tune mix. Automatic mixes retain their previous balance policy and exported audio retains the existing noise and peak protection.
- Report manual confirmation accurately in the effective render mix.
- Regression checks exercise the actual fader/gain handlers, live snapshot cloning, API normalization and worker override preparation. Real song 12 comparison produces different automatic/manual MP3s and identical repeat manual exports with the requested synth gains.

## 2.8.7

- Fine Tune faders show each prepared song’s automatic level plus saved edits. Moving one fader stores only the change relative to its automatic level, preserving the other tracks and avoiding double gain.
- Feed full-song preview chunks directly to FFmpeg, avoiding a full temporary WAV write and reread; retain identical noise treatment, source level and MP3 sample timing.
- Reuse up to four file-versioned song analysis snapshots in memory while preparing preview tracks. Changed source/config/cuts or rewritten analysis invalidate reuse.
- Show completed-track progress while preparing the preview.
- Preserve macOS signed resource attributes when staging automatic updates.

## 2.8.6

- Keep application startup available when the external project disk is disconnected; saved project data remains on its original disk.

## 2.8.5

- Reuse filter coefficients by exact parameters, with independent writable copies and separate per-track DSP state.

- Los controles de preescucha no cierran el vídeo de carga; al ocultarlo se pausa el audio.
- Los datos de cada proyecto se migran con verificación a ZuckerMixer junto a las sesiones; renders nuevos usan su carpeta de proyecto como destino inicial.
- El trabajador se detiene si desaparece la aplicación, incluso con escritura del estado bloqueada.
- Las bibliotecas DSP se cargan al usarse; el actualizador elimina el respaldo antiguo tras confirmar el arranque correcto.

- Reuse one independent FFmpeg loudness/true-peak measurement per exact file revision instead of rescanning unchanged exports. Invalidate on trims, rewrites and file replacements.
- Reduce diagnostic-meter allocations while preserving float64 RMS accumulation and exact peaks.
- Synthetic 60-second check: duplicate measurement 4.61s → 2.08s with identical loudness/peak; diagnostic meter 0.089s → 0.068s. Whole-batch speedup depends on DSP, mastering and concurrent disk/CPU use.

## 2.8.4

- Show Canceling… until workers stop; preserve cancellation across late progress events and stop workers before status I/O.
- Bound native application shutdown and cover cancellation during worker launch.

- Restore the loading video during batch renders.
- Put completed-song play/pause controls and Open in Finder/Explorer inside the loading screen, rather than the song cards.

## 2.8.3

- Show a numbered play/pause button in each song card as soon as a completed export is registered, while other songs continue rendering.
- Stream the exported MP3 without rebuilding cards, seeking controls or automatic next-song playback; preserve playback as further outputs arrive.
- Pin listening to the selected render, prevent overlapping preview audio and clear playback when projects change.
- Keep batch progress visible without covering the song list, and add a platform-specific button to open the completed renders folder.

## 2.8.2

- Check public GitHub releases at desktop startup without blocking project loading. Ask before downloading and installing updates.
- Verify package checksums and frozen startup before replacement; restart via an independent helper and restore the previous application if startup fails.
- Preserve user projects and refuse updates during active jobs or unsaved cut edits.

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
