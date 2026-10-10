# Third-party components

ZuckerMixer uses Matchering 2.0.6 (GPL-3.0), FFmpeg (LGPL/GPL depending on the binary configuration), pywebview (BSD), Flask (BSD), NumPy/SciPy (BSD), SoundFile (BSD and libsndfile LGPL), pyloudnorm (MIT), Numba/llvmlite (BSD), Pillow (HPND), PyInstaller (GPL with distribution exception), and faster-whisper/CTranslate2 (MIT).

The packaged `third-party-licenses` folder contains license and notice files from the installed build dependencies. The GitHub release identifies the exact source revision. Matching source code for ZuckerMixer is available from its release tag. Matchering source: https://github.com/sergree/matchering. FFmpeg source/build information: https://github.com/imageio/imageio-ffmpeg and https://ffmpeg.org.

ZuckerMixer is licensed under the GNU General Public License v3.0 (see `LICENSE`), in part because it bundles Matchering, which is itself licensed under GPL-3.0. Redistribution of the integrated application must comply with the GPL-3.0 terms.

The Windows executable installer also embeds Microsoft’s signed Evergreen WebView2 Runtime installer. WebView2 is provided under Microsoft’s terms, separately from ZuckerMixer’s GPL-3.0 license. See https://developer.microsoft.com/microsoft-edge/webview2/.
