# Video Downloader (Tkinter)

A simple GUI app powered by `yt-dlp` that lets you:
- Download public videos from YouTube, Instagram, Facebook, and TikTok
- Paste a video URL and inspect available video/audio quality formats
- Choose formats and click **DOWNLOAD**
- Select only an audio format to download and convert it to MP3
- Optionally download only a start/end time range from a video or audio clip
- Download the most recent N videos from a channel/playlist
- Track active downloads with the progress bar at the bottom of the window
- Open the download folder from the button at the top of the window
- Build a native desktop app on Windows or macOS

## Folder structure

```txt
yt_downloader/
  main.py
  pyproject.toml
  uv.lock
  README.md
  build_app.py
  build_exe.bat
  version_info.txt
  app.ico                 (optional Windows icon)
  app.icns                (optional macOS icon)
  dist/
    downloads/
```

## Run from source

### Windows

1. Install uv:
   - `winget install --id=astral-sh.uv -e`
2. Install FFmpeg and add it to PATH:
   - `winget install Gyan.FFmpeg`
   - Restart terminal after install

### macOS

1. Install [Homebrew](https://brew.sh) if it is not already present.
2. Install uv and FFmpeg:

```zsh
brew install uv ffmpeg
```

The project needs Python 3.11 or later. uv installs a compatible managed Python
automatically when necessary.

### Install and launch

Open a terminal in the `yt_downloader` folder and run:

```sh
uv sync --locked
uv run --locked python main.py
```

`uv sync` creates/manages `.venv` and installs the exact versions from `uv.lock`.
FFmpeg must remain installed. It is used to merge separate video/audio streams
and to create MP3 files.

## Build a native desktop app

Build on the operating system where the app will run. The same command selects
the appropriate native format:

```sh
uv run --locked --group dev python build_app.py
```

| Build machine | Output | Notes |
| --- | --- | --- |
| Windows | `dist/yt_downloader.exe` | A portable, double-clickable executable. |
| macOS | `dist/yt_downloader.app` | A native app bundle. Build and test separately on Apple Silicon and Intel Macs if both need to be supported. |

`build_exe.bat` remains available as a Windows shortcut for the same command.
PyInstaller must run on its target platform: a Windows PC cannot make a macOS
app, and a Mac cannot make a Windows executable.

### macOS build notes

- The `.app` stores its downloads in `~/Downloads/Video Downloader/downloads`.
  This avoids writing inside an app bundle installed in `/Applications`.
- Place an optional `app.icns` in the project root to customize the macOS app
  icon. The existing `app.ico` applies only to Windows.
- The app detects Homebrew FFmpeg at `/opt/homebrew/bin` (Apple Silicon) and
  `/usr/local/bin` (Intel), including when launched via Finder.
- For distribution to other people, sign and notarize the final `.app` with a
  Developer ID. Unsigned apps can show Gatekeeper warnings.

### Windows build notes

- Edit `version_info.txt` to change product name, company, and version number shown in Windows file properties.
- Put your icon file as `app.ico` in the project root to brand the executable.
- Recommended icon sizes inside `.ico`: 16x16, 32x32, 48x48, 256x256.

## Usage

### Single video
1. Paste a public YouTube, Instagram, Facebook, or TikTok video URL
2. Click **Paste & Check** after copying a URL, or paste into the field and press Enter.
   The app checks qualities automatically after a normal paste too.
3. Select a video format, an audio format, or both. A video-only stream gets
   the best available audio automatically; selecting only audio creates an MP3.
4. Optionally enter Start and End as seconds or `HH:MM:SS` to extract only that range.
5. Click **DOWNLOAD**

### Batch (recent videos)
1. Paste channel or playlist URL in batch section
2. Enter count (e.g., `100`)
3. Click **Download Recent**

Files are saved to:

```txt
Windows source:            yt_downloader/dist/downloads
Windows packaged app:      next to yt_downloader.exe/downloads
macOS packaged app:        ~/Downloads/Video Downloader/downloads
```

## Notes

- This app relies on `yt-dlp` + `ffmpeg` for best compatibility.
- Instagram, Facebook, and TikTok support depends on each site's current public
  page format and on `yt-dlp`'s extractors. Use a direct public post/reel/video
  URL; private, age-restricted, or login-only content may require browser
  cookies and is not supported by the app UI.
- The available quality list is supplied by the platform. Some social-media
  videos expose only one combined video-and-audio format, while others may not
  offer separate audio formats.
- Audio-only extracts download the selected source audio before trimming it during MP3 conversion. This is more reliable than seeking inside remote streams.
- Some URLs may be geo/age-restricted and can fail.
- Downloading content should comply with each platform's terms and your local laws.
