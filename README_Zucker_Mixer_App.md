ZUCKER MIXER — READ ME FIRST
============================

1. INSTALL
   Drag ZuckerMixer.app into the Applications folder.

2. THE FIRST TIME, macOS WILL BLOCK IT (THIS IS EXPECTED)
   You will see a warning like:
   "ZuckerMixer Not Opened. Apple could not verify ZuckerMixer is free
   of malware..."

   This is not a virus or a bug. Zucker Mixer is free and we have not
   paid Apple's developer fee (99 USD/year), so Apple has not
   "notarized" it. Your recordings never leave your computer. The app
   only goes online to check GitHub for updates and, the first time it
   detects songs, to download the Whisper speech model.

   To open it (only needed once):
     a) In the warning, click "Done" (NOT "Move to Trash").
     b) Open  System Settings  >  Privacy & Security.
     c) Scroll down to "Security". You will see:
        "ZuckerMixer was blocked to protect your Mac."
     d) Click  "Open Anyway"  and enter your Mac password.
     e) In the final prompt, click "Open Anyway" again.

   After that it opens normally with a double-click.
   Automatic updates do not require this step again.

   On macOS 14 (Sonoma) or earlier you can also:
   right-click ZuckerMixer.app  >  Open  >  Open.

   Plan B (Terminal), if the option above does not appear:
     xattr -dr com.apple.quarantine /Applications/ZuckerMixer.app

3. APPLE SILICON MACS (M1, M2, M3...)
   If macOS asks to install Rosetta, accept. The app is built for Intel
   and Rosetta runs it on these Macs.

4. FFMPEG (needed to create MP3 files)
   If the app says ffmpeg is missing, install it in Terminal:
     brew install ffmpeg

5. WINDOWS
   Extract the whole ZIP and run ZuckerMixer\ZuckerMixer.exe; keep the
   _internal folder next to it. If Windows shows "Windows protected your
   PC", click "More info" and then "Run anyway". The Windows package
   already includes ffmpeg.

6. YOUR FILES
   Everything stays on your computer. Settings and diagnostic reports:
     ~/Music/JamMixes/ZuckerMixerState

Zucker Mixer is free software licensed under the GNU GPL v3.0
(see LICENSE.txt). Copyright (C) 2026 José Manuel García.
