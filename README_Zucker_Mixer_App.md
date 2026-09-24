# Zucker Mixer

Zucker Mixer is a local macOS app for mixing the ZuckerSession jam recordings.

## Install

Drag `Zucker Mixer.app` into the `Applications` folder.

## First Launch

This app is unsigned. The first time you open it, macOS Gatekeeper may block a normal double-click.

Use:

1. Right-click `Zucker Mixer.app`
2. Choose `Open`
3. Confirm `Open`

After that, it opens normally.

## ffmpeg

Zucker Mixer needs `ffmpeg` to make MP3 files. If the app says ffmpeg is missing, install it in Terminal:

```bash
brew install ffmpeg
```

## Local Files

Everything stays on this Mac.

The app reads stems from:

```text
Choose the stem folder with the app's **Change source folder** control. The
selection is remembered in `app_settings.json`.
```

Mixes and app settings are stored under:

```text
~/Music/JamMixes
```

No cloud services, accounts, or uploads are used.
