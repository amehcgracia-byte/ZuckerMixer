# ZuckerMixer

Copyright (C) 2026 José Manuel García. Zucker Mixer is free software licensed under the GNU GPL v3.0 (see LICENSE).

Desktop application for mixing multitrack jam recordings locally. Each song has its own audio analysis, instrument balance, drum-kit control, effects and mastering. Source recordings stay on your computer.

## Downloads

Use [GitHub Releases](https://github.com/amehcgracia-byte/ZuckerMixer/releases) for versioned installers. A passing build artifact is a test package; it becomes a public download only when its release is published.

- **macOS:** open `ZuckerMixer-VERSION.dmg` and drag `ZuckerMixer.app` into Applications. Current automated builds target Intel Macs; Apple Silicon can use Rosetta. Packages are locally signed, without Apple notarization (we do not pay for an Apple Developer account), so macOS blocks the first launch. To open it once: click **Done** in the warning, go to **System Settings → Privacy & Security**, and click **Open Anyway** next to "ZuckerMixer was blocked". The DMG includes `READ ME FIRST.txt` with step-by-step instructions. Install FFmpeg with `brew install ffmpeg` if it is not already available.
- **Windows 10/11, x64:** extract the entire `ZuckerMixer-VERSION-Windows.zip`, then run `ZuckerMixer/ZuckerMixer.exe`. Keep its `_internal` folder beside the executable. Microsoft Edge WebView2 Runtime is required. This package includes FFmpeg; no Python installation is required.

## Use

1. Choose a folder of aligned instrument WAV exports. Avoid duplicate stereo/full-session exports.
2. Review detected songs in **Select Cuts**. Existing cuts are retained until you save a change.
3. Use **Fine-tune** for individual song controls. Exact preview uses the Python render; browser mix preview is approximate.
4. Choose a mastering reference if desired. **Mix everything** attempts all visible songs except those you explicitly marked **Skip this one**. Boundary warnings do not remove songs or alter saved cuts. An actual failed render is named in **Copy report**, and the batch continues with the remaining songs.
5. Choose an output folder. Completed MP3 files appear there and in the app. Settings and diagnostic reports are under `~/Music/JamMixes/ZuckerMixerState`.

Initial analysis of long multitrack songs can take time. Valid analysis and mix plans are cached, so reopening controls is faster. Silent microphones are not automatically boosted.

## Build from source

Python 3.12 is used for the supported build because the Matchering/Numba versions are pinned for compatibility.

```sh
python -m pip install -r requirements_app.txt -r requirements_whisper.txt pytest
python -m pytest -q
```

macOS uses a framework Python: `./build_app.sh`. Windows PowerShell: `./build_windows.ps1`. The build records the application version, UTC build time and Git commit. `--self-check` validates frozen Whisper and Matchering imports. GitHub Actions builds and uploads both platform packages; tagged builds prepare a draft release with checksums and source archive.

## License and notices

ZuckerMixer is licensed under the GNU General Public License v3.0; see `LICENSE` and `THIRD_PARTY_NOTICES.md`. The integrated Matchering dependency uses GPL-3.0; public distribution must retain its license and matching source information. No recording samples, user settings or personal reference tracks are included in the source or installers.
