## 2.1.66

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
