import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import Tk, StringVar, IntVar, DoubleVar, END
from tkinter import ttk, messagebox

import yt_dlp

try:
    from build_metadata import BUILD_BRANCH, BUILD_COMMIT, BUILD_REMOTE
except ImportError:
    BUILD_BRANCH = "main"
    BUILD_COMMIT = "unknown"
    BUILD_REMOTE = ""


if getattr(sys, "frozen", False):
    if sys.platform == "darwin":
        # A .app bundle may be installed in /Applications, which is not a suitable
        # writable location. Keep a Mac app's files in the user's Downloads folder.
        APP_ROOT = Path.home() / "Downloads" / "Video Downloader"
    else:
        # Running as a bundled Windows executable: keep downloads next to it.
        APP_ROOT = Path(sys.executable).resolve().parent
else:
    # Source runs use the same folder as the Windows executable built into ./dist.
    APP_ROOT = Path(__file__).resolve().parent / "dist"

DOWNLOAD_DIR = APP_ROOT / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
PREFERENCES_PATH = APP_ROOT / "settings.json"
DOWNLOAD_HISTORY_PATH = APP_ROOT / "download_history.json"
DEFAULT_PREFERENCES = {
    "preferred_video_height": 1080,
    "preferred_audio_bitrate": 128,
}
DOWNLOAD_HISTORY_LIMIT = 200
DOWNLOAD_HISTORY_LOCK = threading.Lock()


SUPPORTED_PLATFORM_NAMES = "YouTube, X, Instagram, Facebook, and TikTok"
UPDATE_LOG_NAME = "yt_downloader_update.log"


def load_preferences() -> dict:
    """Load quality defaults, falling back safely when settings are missing."""
    preferences = dict(DEFAULT_PREFERENCES)
    try:
        stored = json.loads(PREFERENCES_PATH.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            for key in preferences:
                value = stored.get(key)
                if isinstance(value, (int, float, str)):
                    try:
                        preferences[key] = int(value)
                    except (TypeError, ValueError):
                        pass
    except (OSError, ValueError, TypeError):
        pass

    preferences["preferred_video_height"] = max(
        0, preferences["preferred_video_height"]
    )
    preferences["preferred_audio_bitrate"] = max(
        0, preferences["preferred_audio_bitrate"]
    )
    return preferences


def save_preferences(preferences: dict) -> None:
    """Persist the small set of user-selectable quality defaults."""
    normalized = {
        key: max(0, int(preferences.get(key, default)))
        for key, default in DEFAULT_PREFERENCES.items()
    }
    try:
        PREFERENCES_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREFERENCES_PATH.write_text(
            json.dumps(normalized, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        # A read-only install should not make downloading fail.
        pass


def load_download_history() -> list[dict]:
    """Return the newest recorded downloads first."""
    with DOWNLOAD_HISTORY_LOCK:
        try:
            stored = json.loads(DOWNLOAD_HISTORY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []

    if not isinstance(stored, list):
        return []
    return [entry for entry in stored if isinstance(entry, dict)][:DOWNLOAD_HISTORY_LIMIT]


def append_download_history(
    filepath: str | Path,
    title: str,
    download_kind: str,
    source_url: str = "",
    clip_range: tuple[float, float] | None = None,
) -> dict | None:
    """Record one completed file for the History page."""
    path = Path(filepath).resolve()
    if not path.is_file():
        return None

    entry = {
        "path": str(path),
        "title": title or path.stem,
        "kind": download_kind,
        "source_url": source_url,
        "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if clip_range:
        entry["range"] = [float(clip_range[0]), float(clip_range[1])]

    with DOWNLOAD_HISTORY_LOCK:
        try:
            stored = json.loads(DOWNLOAD_HISTORY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            stored = []
        if not isinstance(stored, list):
            stored = []

        stored = [
            old
            for old in stored
            if not isinstance(old, dict) or old.get("path") != str(path)
        ]
        stored.insert(0, entry)
        try:
            DOWNLOAD_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            DOWNLOAD_HISTORY_PATH.write_text(
                json.dumps(stored[:DOWNLOAD_HISTORY_LIMIT], indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            return None
    return entry


def subprocess_window_options() -> dict:
    """Prevent helper commands from flashing console windows on Windows."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def github_https_remote(remote: str) -> str:
    """Convert a common GitHub SSH remote to a non-interactive HTTPS URL."""
    remote = remote.strip()
    match = re.fullmatch(r"git@github\.com:(.+?)(?:\.git)?", remote)
    if match:
        return f"https://github.com/{match.group(1)}.git"
    match = re.fullmatch(r"ssh://git@github\.com/(.+?)(?:\.git)?", remote)
    if match:
        return f"https://github.com/{match.group(1)}.git"
    return remote


def git_output(source_root: Path, *args: str) -> str:
    """Run a read-only git command and return stripped stdout."""
    result = subprocess.run(
        ["git", "-C", str(source_root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=True,
        **subprocess_window_options(),
    )
    return result.stdout.strip()


def find_source_root() -> Path | None:
    """Locate the checkout used to build or launch the app, when available."""
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates = [executable_dir.parent, executable_dir]
    else:
        candidates = [Path(__file__).resolve().parent]

    for candidate in candidates:
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "build_app.py"
        ).is_file():
            return candidate
    return None


def remote_head_commit(remote: str, branch: str) -> str:
    """Return the commit currently published for a remote branch."""
    result = subprocess.run(
        ["git", "ls-remote", github_https_remote(remote), f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=True,
        **subprocess_window_options(),
    )
    line = result.stdout.strip().splitlines()
    if not line:
        raise RuntimeError(f"GitHub branch '{branch}' was not found.")
    return line[0].split()[0]


def current_update_identity() -> tuple[str, str, str]:
    """Return the commit, remote, and branch represented by this app."""
    source_root = find_source_root()
    if not getattr(sys, "frozen", False) and source_root:
        try:
            return (
                git_output(source_root, "rev-parse", "HEAD"),
                git_output(source_root, "remote", "get-url", "origin"),
                git_output(source_root, "branch", "--show-current") or "main",
            )
        except (OSError, subprocess.SubprocessError):
            pass
    return BUILD_COMMIT, BUILD_REMOTE, BUILD_BRANCH


def update_is_available(local_commit: str, remote_commit: str) -> bool:
    """Treat any different published commit as a rebuild-worthy update."""
    return bool(
        local_commit
        and remote_commit
        and local_commit != "unknown"
        and local_commit != remote_commit
    )


def installed_application_path() -> Path:
    """Return the replaceable native artifact for this running app."""
    executable = Path(sys.executable).resolve()
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        for candidate in (executable.parent, *executable.parents):
            if candidate.suffix == ".app" and candidate.is_dir():
                return candidate
    return executable


def native_build_artifact(source_root: Path) -> Path:
    """Return the artifact produced by build_app.py on this platform."""
    if sys.platform == "win32":
        return source_root / "dist" / "yt_downloader.exe"
    if sys.platform == "darwin":
        return source_root / "dist" / "yt_downloader.app"
    raise RuntimeError(
        "Native desktop updates are currently supported on Windows and macOS."
    )


def update_log_path(target_path: Path) -> Path:
    """Keep updater diagnostics in the app's writable data directory."""
    if sys.platform == "win32":
        return target_path.parent / UPDATE_LOG_NAME
    return APP_ROOT / UPDATE_LOG_NAME


def launch_updated_application(target_path: Path, error_log: Path | None = None):
    """Launch an installed artifact, optionally asking it to show update errors."""
    if error_log and sys.platform == "darwin" and target_path.suffix == ".app":
        executable = target_path / "Contents" / "MacOS" / "yt_downloader"
        command = [str(executable), "--update-error", str(error_log)]
    elif error_log:
        command = [str(target_path), "--update-error", str(error_log)]
    elif sys.platform == "darwin" and target_path.suffix == ".app":
        command = ["open", str(target_path)]
    else:
        command = [str(target_path)]
    subprocess.Popen(command, cwd=target_path.parent)


def replace_native_artifact(built_artifact: Path, target_path: Path) -> None:
    """Stage the new build, remove the old artifact, and install the replacement."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path = target_path.parent / f".{target_path.name}.new"
    if staged_path.is_dir():
        shutil.rmtree(staged_path, ignore_errors=True)
    elif staged_path.exists():
        staged_path.unlink()

    try:
        if built_artifact.suffix == ".app":
            shutil.copytree(built_artifact, staged_path)
            # The running macOS app can safely be unlinked while its process is
            # alive. The staged bundle is complete before the old one is removed.
            if target_path.is_dir():
                shutil.rmtree(target_path)
            os.replace(staged_path, target_path)
            return

        shutil.copy2(built_artifact, staged_path)
        last_error = None
        for _attempt in range(20):
            try:
                os.replace(staged_path, target_path)
                last_error = None
                break
            except PermissionError as error:
                last_error = error
                time.sleep(0.5)
        if last_error:
            raise last_error
    finally:
        if staged_path.is_dir():
            shutil.rmtree(staged_path, ignore_errors=True)
        elif staged_path.exists():
            staged_path.unlink()


def run_update_worker(
    remote: str,
    branch: str,
    target_path: Path,
) -> int:
    """Clone GitHub, rebuild the native app, replace the old version, and relaunch."""
    log_path = update_log_path(target_path)
    source_parent = Path(tempfile.mkdtemp(prefix="yt_downloader_update_source_"))
    source_root = source_parent / "source"

    try:
        git = shutil.which("git")
        uv = shutil.which("uv")
        if not git or not uv:
            missing = "Git" if not git else "uv"
            raise RuntimeError(f"{missing} is required to rebuild the application.")

        with log_path.open("w", encoding="utf-8") as log_file:
            commands = [
                [
                    git,
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    branch,
                    "--single-branch",
                    github_https_remote(remote),
                    str(source_root),
                ],
                [uv, "sync", "--locked", "--group", "dev"],
                [uv, "run", "--locked", "--group", "dev", "python", "build_app.py"],
            ]
            for command in commands:
                log_file.write(f"> {' '.join(command)}\n")
                log_file.flush()
                subprocess.run(
                    command,
                    cwd=source_root if source_root.is_dir() else source_parent,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=True,
                    **subprocess_window_options(),
                )

        built_artifact = native_build_artifact(source_root)
        if not built_artifact.exists():
            raise RuntimeError(f"The rebuilt artifact was not produced: {built_artifact}")

        replace_native_artifact(built_artifact, target_path)
        launch_updated_application(target_path)
        return 0
    except Exception as error:
        try:
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(f"\nUPDATE FAILED: {error}\n")
        except OSError:
            pass
        if target_path.exists():
            launch_updated_application(target_path, error_log=log_path)
        return 1
    finally:
        shutil.rmtree(source_parent, ignore_errors=True)


def build_download_plan(
    video_id: str | None,
    audio_id: str | None,
) -> tuple[str, str]:
    """Return the exact yt-dlp format selector and requested download mode."""
    if video_id and audio_id:
        return f"{video_id}+{audio_id}", "video_audio"
    if video_id:
        return video_id, "video_only"
    if audio_id:
        return audio_id, "audio_only"
    raise ValueError("A video or audio format must be selected.")


def find_ffmpeg_location() -> str | None:
    """Find FFmpeg when a macOS app is launched outside a terminal.

    Finder does not necessarily inherit Homebrew's PATH, so check its usual
    installation locations as well as the active PATH. yt-dlp expects the
    directory containing both ffmpeg and ffprobe.
    """
    candidates = []
    on_path = shutil.which("ffmpeg")
    if on_path:
        candidates.append(Path(on_path))

    if sys.platform == "darwin":
        candidates.extend(
            [Path("/opt/homebrew/bin/ffmpeg"), Path("/usr/local/bin/ffmpeg")]
        )

    for ffmpeg in candidates:
        if ffmpeg.is_file() and (ffmpeg.parent / "ffprobe").is_file():
            return str(ffmpeg.parent)
    return None


FFMPEG_LOCATION = find_ffmpeg_location()


class RoundedCard(tk.Frame):
    """Canvas-backed card with a real rounded border and inset content area."""

    def __init__(
        self,
        parent,
        *,
        background: str,
        fill: str,
        border: str,
        radius: int = 16,
        padding: int = 14,
    ):
        super().__init__(parent, background=background, borderwidth=0, highlightthickness=0)
        self._background = background
        self._fill = fill
        self._border = border
        self._radius = radius
        # Keep the content comfortably inside the curve without adding a full
        # radius of vertical whitespace to every card on smaller screens.
        self._inset = max(8, radius // 2)
        self._canvas = tk.Canvas(
            self,
            background=background,
            highlightthickness=0,
            borderwidth=0,
        )
        self._canvas.pack(fill="both", expand=True)
        self.body = ttk.Frame(
            self._canvas,
            style="Card.TFrame",
            padding=padding,
        )
        self._body_window = self._canvas.create_window(
            self._inset,
            self._inset,
            anchor="nw",
            window=self.body,
        )
        self.bind("<Configure>", self._redraw)

    def refresh_size(self):
        """Give the geometry manager a useful natural size before expansion."""
        self.body.update_idletasks()
        width = self.body.winfo_reqwidth() + (self._inset * 2)
        height = self.body.winfo_reqheight() + (self._inset * 2)
        self._canvas.configure(width=width, height=height)
        self.configure(width=width, height=height)

    def _redraw(self, event=None):
        width = max(1, event.width if event else self.winfo_width())
        height = max(1, event.height if event else self.winfo_height())
        radius = min(self._radius, max(1, width // 2), max(1, height // 2))
        self._canvas.delete("card-background")
        self._draw_rounded_rectangle(
            1,
            1,
            width - 1,
            height - 1,
            radius,
            fill=self._fill,
            outline=self._border,
            width=1,
        )
        self._canvas.tag_lower("card-background")
        inner_width = max(1, width - (self._inset * 2))
        inner_height = max(1, height - (self._inset * 2))
        self._canvas.coords(self._body_window, self._inset, self._inset)
        self._canvas.itemconfigure(
            self._body_window,
            width=inner_width,
            height=inner_height,
        )

    def _draw_rounded_rectangle(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        radius: int,
        *,
        fill: str,
        outline: str,
        width: int,
    ):
        """Draw a rounded rectangle using native Tk canvas primitives."""
        tag = "card-background"
        self._canvas.create_rectangle(
            x1 + radius,
            y1,
            x2 - radius,
            y2,
            fill=fill,
            outline=fill,
            tags=tag,
        )
        self._canvas.create_rectangle(
            x1,
            y1 + radius,
            x2,
            y2 - radius,
            fill=fill,
            outline=fill,
            tags=tag,
        )
        self._canvas.create_arc(
            x1,
            y1,
            x1 + (radius * 2),
            y1 + (radius * 2),
            start=90,
            extent=90,
            fill=fill,
            outline=outline,
            width=width,
            tags=tag,
        )
        self._canvas.create_arc(
            x2 - (radius * 2),
            y1,
            x2,
            y1 + (radius * 2),
            start=0,
            extent=90,
            fill=fill,
            outline=outline,
            width=width,
            tags=tag,
        )
        self._canvas.create_arc(
            x1,
            y2 - (radius * 2),
            x1 + (radius * 2),
            y2,
            start=180,
            extent=90,
            fill=fill,
            outline=outline,
            width=width,
            tags=tag,
        )
        self._canvas.create_arc(
            x2 - (radius * 2),
            y2 - (radius * 2),
            x2,
            y2,
            start=270,
            extent=90,
            fill=fill,
            outline=outline,
            width=width,
            tags=tag,
        )
        self._canvas.create_line(
            x1 + radius,
            y1,
            x2 - radius,
            y1,
            fill=outline,
            width=width,
            tags=tag,
        )
        self._canvas.create_line(
            x2,
            y1 + radius,
            x2,
            y2 - radius,
            fill=outline,
            width=width,
            tags=tag,
        )
        self._canvas.create_line(
            x2 - radius,
            y2,
            x1 + radius,
            y2,
            fill=outline,
            width=width,
            tags=tag,
        )
        self._canvas.create_line(
            x1,
            y2 - radius,
            x1,
            y1 + radius,
            fill=outline,
            width=width,
            tags=tag,
        )


class VideoDownloaderGUI:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("Video Downloader")
        self.root.geometry("1080x1000")
        self.root.minsize(940, 820)

        self.url_var = StringVar()
        self.start_time_var = StringVar()
        self.end_time_var = StringVar()
        self.range_status_var = StringVar(value="Paste a link to load the FFmpeg preview")
        self.video_title_var = StringVar(value="No video loaded yet")
        self.video_meta_var = StringVar(
            value="Paste a link above to ingest its available formats"
        )
        self.quality_status_var = StringVar(
            value="Video and audio qualities will appear here automatically"
        )
        self.batch_url_var = StringVar()
        self.batch_count_var = IntVar(value=10)
        self.progress_var = DoubleVar(value=0.0)
        self.progress_text_var = StringVar(value="Ready")
        self._progress_value = 0.0
        self._last_progress_update = 0.0
        self._is_checking_qualities = False
        self._update_remote = ""
        self._update_branch = "main"
        self._update_available = False

        # FFmpeg-backed preview and range-picker state.
        self.preview_duration = 0.0
        self.preview_path: Path | None = None
        self.preview_dir: Path | None = None
        self.preview_dirs: set[Path] = set()
        self.preview_photo = None
        self.preview_generation = 0
        self.preview_request_id = 0
        self.preview_frame_after = None
        self.preview_player_process = None
        self.preview_player_stop = threading.Event()
        self.preview_player_token = 0
        self.preview_playing = False
        self._closing = False
        self.preview_playhead = 0.0
        self.range_start_seconds = 0.0
        self.range_end_seconds = 0.0
        self.range_drag_handle = None

        self.video_formats = []
        self.audio_formats = []
        self.video_id_to_format = {}
        self.audio_id_to_format = {}

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if sys.platform in ("win32", "darwin"):
            self.update_button.configure(text="↓  Checking GitHub...")
            self._run_in_thread(self._check_for_update_worker)

    def _build_ui_legacy(self):
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill="both", expand=True)

        # Top toolbar
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Label(toolbar, text=f"Download folder: {DOWNLOAD_DIR}").pack(side="left")
        ttk.Button(
            toolbar,
            text="Open Download Folder",
            command=self.open_download_folder,
        ).pack(side="right")
        self.update_button = ttk.Button(
            toolbar,
            text="DOWNLOAD UPDATE & REBUILD",
            command=self.download_update_and_rebuild,
        )

        # Download progress (kept at the bottom of the window)
        progress_frame = ttk.Frame(frame)
        progress_frame.pack(side="bottom", fill="x", pady=(10, 0))
        ttk.Label(progress_frame, textvariable=self.progress_text_var).pack(
            fill="x", pady=(0, 4)
        )
        style = ttk.Style(self.root)
        style.configure(
            "Green.Horizontal.TProgressbar",
            background="#22a447",
            troughcolor="#d9d9d9",
            thickness=18,
        )
        self.progress_bar = ttk.Progressbar(
            progress_frame,
            variable=self.progress_var,
            maximum=100,
            mode="determinate",
            style="Green.Horizontal.TProgressbar",
        )
        self.progress_bar.pack(fill="x", ipady=3)

        # Single-video section. yt-dlp chooses the matching site extractor from
        # the URL, so no separate platform selector is needed.
        single_card = ttk.LabelFrame(frame, text="Single Video", padding=12)
        single_card.pack(fill="x", pady=(0, 10))
        single_card.columnconfigure(0, weight=1)
        single_card.columnconfigure(1, weight=1)
        single_card.columnconfigure(2, weight=0)

        ttk.Label(single_card, text="Video URL (YouTube, X, Instagram, Facebook, or TikTok):").grid(row=0, column=0, sticky="w")
        self.url_entry = ttk.Entry(single_card, textvariable=self.url_var, width=95)
        self.url_entry.grid(row=1, column=0, columnspan=2, sticky="we", pady=(4, 8))
        self.url_entry.bind("<Return>", self._check_url_from_entry)
        # Schedule after Tk has applied the standard paste operation, whether it
        # came from Ctrl+V, Shift+Insert, or the entry's context menu.
        self.url_entry.bind("<<Paste>>", self._check_url_after_paste, add=True)
        ttk.Button(
            single_card,
            text="Paste & Analyze",
            command=self.paste_and_check_url,
        ).grid(row=1, column=2, sticky="e", padx=(8, 0), pady=(4, 8))

        # The preview is rendered from a small local video downloaded by yt-dlp
        # and decoded frame-by-frame by FFmpeg. Keeping it local makes seeking
        # reliable even when the platform's media URLs expire while dragging.
        preview_card = ttk.LabelFrame(
            single_card,
            text="FFmpeg Preview — drag either handle to choose the range",
            padding=8,
        )
        preview_card.grid(row=2, column=0, columnspan=3, sticky="we", pady=(0, 8))

        preview_surface = tk.Frame(preview_card, background="#111827", height=360)
        preview_surface.pack(fill="x", expand=True)
        preview_surface.pack_propagate(False)
        self.preview_label = tk.Label(
            preview_surface,
            background="#111827",
            foreground="#e5e7eb",
            text="Check a URL to load a video preview",
            font=("TkDefaultFont", 12),
            anchor="center",
        )
        self.preview_label.pack(fill="both", expand=True)

        self.range_canvas = tk.Canvas(
            preview_card,
            height=76,
            background="#1f2937",
            highlightthickness=0,
            cursor="hand2",
        )
        self.range_canvas.pack(fill="x", pady=(8, 0))
        self.range_canvas.bind("<Configure>", lambda _event: self._draw_range_picker())
        self.range_canvas.bind("<ButtonPress-1>", self._range_picker_press)
        self.range_canvas.bind("<B1-Motion>", self._range_picker_drag)
        self.range_canvas.bind("<ButtonRelease-1>", self._range_picker_release)

        range_controls = ttk.Frame(preview_card)
        range_controls.pack(fill="x", pady=(6, 0))
        ttk.Label(range_controls, text="Start:").pack(side="left")
        self.start_entry = ttk.Entry(
            range_controls, textvariable=self.start_time_var, width=12
        )
        self.start_entry.pack(side="left", padx=(5, 12))
        ttk.Label(range_controls, text="End:").pack(side="left")
        self.end_entry = ttk.Entry(
            range_controls, textvariable=self.end_time_var, width=12
        )
        self.end_entry.pack(side="left", padx=(5, 12))
        self.start_entry.bind("<Return>", self._sync_range_from_entries)
        self.end_entry.bind("<Return>", self._sync_range_from_entries)
        self.start_entry.bind("<FocusOut>", self._sync_range_from_entries)
        self.end_entry.bind("<FocusOut>", self._sync_range_from_entries)
        ttk.Label(range_controls, text="seconds or HH:MM:SS").pack(side="left")
        ttk.Button(
            range_controls,
            text="Reset to full video",
            command=self._reset_range_to_full_video,
        ).pack(side="right")

        preview_actions = ttk.Frame(preview_card)
        preview_actions.pack(fill="x", pady=(6, 0))
        self.preview_play_button = ttk.Button(
            preview_actions,
            text="▶ Play preview",
            command=self._toggle_preview_playback,
        )
        self.preview_play_button.pack(side="left")
        self.preview_play_button.state(["disabled"])
        ttk.Label(
            preview_actions,
            textvariable=self.range_status_var,
        ).pack(side="left", padx=(10, 0))

        clip_frame = ttk.Frame(single_card)
        clip_frame.grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(
            clip_frame,
            text="The selected range is used for video-only, audio-only, or combined downloads.",
        ).pack(side="left")

        ttk.Button(single_card, text="DOWNLOAD", command=self.download_selected).grid(row=4, column=0, sticky="w")

        lists_frame = ttk.Frame(single_card)
        lists_frame.grid(row=5, column=0, columnspan=3, sticky="we", pady=(10, 0))

        video_frame = ttk.LabelFrame(lists_frame, text="Video Quality", padding=8)
        video_frame.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self.video_list = ttk.Treeview(video_frame, columns=("label",), show="headings", height=6)
        self.video_list.heading("label", text="Format")
        self.video_list.column("label", width=420)
        self.video_list.pack(fill="both", expand=True)
        ttk.Button(
            video_frame,
            text="Deselect Video",
            command=lambda: self.video_list.selection_remove(
                *self.video_list.selection()
            ),
        ).pack(anchor="w", pady=(6, 0))

        audio_frame = ttk.LabelFrame(lists_frame, text="Audio Quality", padding=8)
        audio_frame.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.audio_list = ttk.Treeview(audio_frame, columns=("label",), show="headings", height=6)
        self.audio_list.heading("label", text="Format")
        self.audio_list.column("label", width=420)
        self.audio_list.pack(fill="both", expand=True)
        ttk.Button(
            audio_frame,
            text="Deselect Audio",
            command=lambda: self.audio_list.selection_remove(
                *self.audio_list.selection()
            ),
        ).pack(anchor="w", pady=(6, 0))

        # Batch section
        batch_card = ttk.LabelFrame(frame, text="Batch Download (Channels / Playlists, where supported)", padding=12)
        batch_card.pack(fill="x", pady=(0, 10))

        ttk.Label(batch_card, text="Channel/Playlist URL:").grid(row=0, column=0, sticky="w")
        ttk.Entry(batch_card, textvariable=self.batch_url_var, width=95).grid(row=1, column=0, columnspan=3, sticky="we", pady=(4, 8))

        ttk.Label(batch_card, text="How many recent videos:").grid(row=2, column=0, sticky="w")
        ttk.Spinbox(batch_card, from_=1, to=1000, textvariable=self.batch_count_var, width=8).grid(row=2, column=1, sticky="w")
        ttk.Button(batch_card, text="Download Recent", command=self.download_recent).grid(row=2, column=2, sticky="w")

        # Logs
        logs_card = ttk.LabelFrame(frame, text="Log", padding=12)
        logs_card.pack(fill="both", expand=True)
        self.log_box = ttk.Treeview(logs_card, columns=("log",), show="headings", height=6)
        self.log_box.heading("log", text="Status")
        self.log_box.column("log", width=920)
        self.log_box.pack(fill="both", expand=True)
        self.log_box.bind("<ButtonRelease-1>", self.copy_selected_log)

    def _build_ui(self):
        """Build the dark, step-based downloader workspace."""
        colors = {
            "bg": "#0b1020",
            "sidebar": "#080d1a",
            "surface": "#131b2f",
            "surface_alt": "#19243a",
            "field": "#0d1424",
            "border": "#263554",
            "text": "#f8fafc",
            "muted": "#94a3b8",
            "cyan": "#38bdf8",
            "blue": "#2563eb",
            "violet": "#9333ea",
        }
        self._ui_colors = colors
        self.root.configure(background=colors["bg"])
        self.root.geometry("1280x920")
        self.root.minsize(1080, 760)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("App.TFrame", background=colors["bg"])
        style.configure("Card.TFrame", background=colors["surface"])
        style.configure("Title.TLabel", background=colors["bg"], foreground=colors["text"], font=("Segoe UI", 23, "bold"))
        style.configure("Subtitle.TLabel", background=colors["bg"], foreground=colors["muted"], font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background=colors["surface"], foreground=colors["text"], font=("Segoe UI", 12, "bold"))
        style.configure("CardText.TLabel", background=colors["surface"], foreground=colors["muted"], font=("Segoe UI", 9))
        style.configure("Step.TLabel", background=colors["blue"], foreground=colors["text"], font=("Segoe UI", 10, "bold"), padding=(9, 5))
        style.configure(
            "URL.TEntry",
            fieldbackground=colors["field"],
            foreground=colors["text"],
            insertcolor=colors["text"],
            bordercolor=colors["border"],
            lightcolor=colors["cyan"],
            darkcolor=colors["border"],
            padding=(12, 9),
        )
        style.configure(
            "TEntry",
            fieldbackground=colors["field"],
            foreground=colors["text"],
            insertcolor=colors["text"],
            bordercolor=colors["border"],
            lightcolor=colors["border"],
            darkcolor=colors["border"],
            padding=(8, 6),
        )
        style.configure(
            "Primary.TButton",
            background=colors["violet"],
            foreground=colors["text"],
            bordercolor=colors["violet"],
            lightcolor=colors["violet"],
            darkcolor=colors["violet"],
            padding=(16, 10),
            font=("Segoe UI", 10, "bold"),
        )
        style.map("Primary.TButton", background=[("pressed", colors["blue"]), ("active", "#a855f7")])
        style.configure(
            "Download.TButton",
            background=colors["blue"],
            foreground=colors["text"],
            bordercolor=colors["blue"],
            lightcolor=colors["blue"],
            darkcolor=colors["blue"],
            padding=(22, 11),
            font=("Segoe UI", 11, "bold"),
        )
        style.map("Download.TButton", background=[("pressed", colors["violet"]), ("active", colors["violet"])])
        style.configure(
            "Secondary.TButton",
            background=colors["surface_alt"],
            foreground=colors["text"],
            bordercolor=colors["border"],
            lightcolor=colors["border"],
            darkcolor=colors["border"],
            padding=(12, 8),
            font=("Segoe UI", 9),
        )
        style.map("Secondary.TButton", background=[("pressed", colors["border"]), ("active", "#243452")])
        style.configure(
            "Ghost.TButton",
            background=colors["bg"],
            foreground=colors["muted"],
            bordercolor=colors["bg"],
            lightcolor=colors["bg"],
            darkcolor=colors["bg"],
            padding=(8, 6),
            font=("Segoe UI", 9),
        )
        style.map("Ghost.TButton", foreground=[("active", colors["text"])], background=[("active", colors["surface"])])
        style.configure(
            "Card.TLabelframe",
            background=colors["surface"],
            foreground=colors["text"],
            bordercolor=colors["border"],
            lightcolor=colors["border"],
            darkcolor=colors["border"],
            borderwidth=1,
        )
        style.configure("Card.TLabelframe.Label", background=colors["surface"], foreground=colors["text"], font=("Segoe UI", 10, "bold"))
        style.configure(
            "Treeview",
            background=colors["field"],
            fieldbackground=colors["field"],
            foreground=colors["muted"],
            bordercolor=colors["border"],
            rowheight=28,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Treeview.Heading",
            background=colors["surface_alt"],
            foreground=colors["muted"],
            bordercolor=colors["border"],
            font=("Segoe UI", 9, "bold"),
            padding=(8, 6),
        )
        style.map("Treeview", background=[("selected", "#24506c")], foreground=[("selected", colors["text"])])
        style.configure(
            "Accent.Horizontal.TProgressbar",
            background=colors["cyan"],
            troughcolor=colors["field"],
            bordercolor=colors["border"],
            lightcolor=colors["cyan"],
            darkcolor=colors["violet"],
            thickness=12,
        )
        style.configure(
            "TSpinbox",
            fieldbackground=colors["field"],
            foreground=colors["text"],
            bordercolor=colors["border"],
            lightcolor=colors["border"],
            darkcolor=colors["border"],
            padding=(7, 5),
        )

        def step_header(parent, number: str, title: str, subtitle: str = ""):
            row = ttk.Frame(parent, style="Card.TFrame")
            ttk.Label(row, text=number, style="Step.TLabel").pack(side="left")
            copy = ttk.Frame(row, style="Card.TFrame")
            copy.pack(side="left", padx=(10, 0))
            ttk.Label(copy, text=title, style="CardTitle.TLabel").pack(anchor="w")
            if subtitle:
                ttk.Label(copy, text=subtitle, style="CardText.TLabel").pack(anchor="w", pady=(2, 0))
            return row

        def nav_item(parent, icon: str, label: str, active: bool = False):
            background = colors["surface"] if active else colors["sidebar"]
            item = tk.Frame(parent, background=background, height=42)
            item.pack(fill="x", pady=(0, 5))
            item.pack_propagate(False)
            tk.Frame(item, background=colors["violet"] if active else background, width=3).pack(side="left", fill="y")
            tk.Label(item, text=icon, background=background, foreground=colors["text"] if active else colors["muted"], font=("Segoe UI Symbol", 14), width=3).pack(side="left", padx=(9, 0))
            tk.Label(item, text=label, background=background, foreground=colors["text"] if active else colors["muted"], font=("Segoe UI", 10, "bold" if active else "normal"), anchor="w").pack(side="left", fill="x", expand=True)

        rounded_cards = []

        def rounded_card(parent, padding=14):
            card = RoundedCard(
                parent,
                background=colors["bg"],
                fill=colors["surface"],
                border=colors["border"],
                radius=16,
                padding=padding,
            )
            rounded_cards.append(card)
            return card

        shell = ttk.Frame(self.root, style="App.TFrame")
        shell.pack(fill="both", expand=True)

        sidebar = tk.Frame(shell, background=colors["sidebar"], width=224)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, background=colors["sidebar"])
        brand.pack(fill="x", padx=14, pady=(26, 32))
        logo = tk.Canvas(brand, width=48, height=48, background=colors["sidebar"], highlightthickness=0)
        logo.pack(side="left")
        logo.create_rectangle(2, 2, 46, 46, fill=colors["violet"], outline=colors["cyan"], width=2)
        logo.create_text(24, 24, text="↓", fill=colors["text"], font=("Segoe UI", 25, "bold"))
        brand_copy = tk.Frame(brand, background=colors["sidebar"])
        brand_copy.pack(side="left", padx=(11, 0))
        tk.Label(brand_copy, text="Video Downloader", background=colors["sidebar"], foreground=colors["text"], font=("Segoe UI", 10, "bold"), anchor="w").pack(anchor="w")
        tk.Label(brand_copy, text="Fast. Reliable. High quality.", background=colors["sidebar"], foreground=colors["muted"], font=("Segoe UI", 8), anchor="w").pack(anchor="w", pady=(3, 0))

        nav = tk.Frame(sidebar, background=colors["sidebar"])
        nav.pack(fill="x", padx=14)
        nav_item(nav, "⌂", "Downloader", active=True)
        nav_item(nav, "◷", "Recent activity")
        nav_item(nav, "▣", "Batch downloads")
        nav_item(nav, "⚙", "Settings")

        content = ttk.Frame(shell, style="App.TFrame", padding=(22, 18, 22, 16))
        content.pack(side="left", fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(2, weight=1)
        content.rowconfigure(4, weight=0)

        header = ttk.Frame(content, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 15))
        header.columnconfigure(0, weight=1)
        heading = ttk.Frame(header, style="App.TFrame")
        heading.grid(row=0, column=0, sticky="w")
        ttk.Label(heading, text="Download anything, your way", style="Title.TLabel").pack(anchor="w")
        ttk.Label(heading, text="Paste a link, choose the moment and quality, then save it locally.", style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))
        header_actions = ttk.Frame(header, style="App.TFrame")
        header_actions.grid(row=0, column=1, sticky="e")
        self.update_button = ttk.Button(
            header_actions,
            text="↓  Checking GitHub...",
            command=self.download_update_and_rebuild,
            style="Secondary.TButton",
        )
        self.update_button.pack(side="right")
        self.update_button.state(["disabled"])
        ttk.Button(header_actions, text="Open downloads", command=self.open_download_folder, style="Ghost.TButton").pack(side="right", padx=(8, 0))

        url_card = rounded_card(content, padding=14)
        url_card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        url_body = url_card.body
        url_body.columnconfigure(0, weight=1)
        step_header(url_body, "1", "Paste a video link", "Formats are ingested automatically after you submit the URL.").grid(row=0, column=0, sticky="w")
        url_row = ttk.Frame(url_body, style="Card.TFrame")
        url_row.grid(row=1, column=0, sticky="ew", pady=(15, 0))
        url_row.columnconfigure(0, weight=1)
        self.url_entry = ttk.Entry(url_row, textvariable=self.url_var, style="URL.TEntry")
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_entry.bind("<Return>", self._check_url_from_entry)
        self.url_entry.bind("<<Paste>>", self._check_url_after_paste, add=True)
        ttk.Button(url_row, text="Paste & Analyze", command=self.paste_and_check_url, style="Primary.TButton").grid(row=0, column=1, sticky="e", padx=(10, 0))
        ttk.Label(url_body, text=f"Supports {SUPPORTED_PLATFORM_NAMES} and other sites supported by yt-dlp", style="CardText.TLabel").grid(row=2, column=0, sticky="w", pady=(9, 0))

        workspace = ttk.Frame(content, style="App.TFrame")
        workspace.grid(row=2, column=0, sticky="nsew", pady=(0, 12))
        workspace.columnconfigure(0, weight=3)
        workspace.columnconfigure(1, weight=4)
        workspace.rowconfigure(0, weight=1)

        preview_card = rounded_card(workspace, padding=10)
        preview_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        preview_body = preview_card.body
        preview_body.columnconfigure(0, weight=1)
        preview_body.rowconfigure(2, weight=1, minsize=150)
        step_header(preview_body, "2", "Scrub and choose a range", "Drag either handle or edit the timestamps below.").grid(row=0, column=0, sticky="w")
        metadata = ttk.Frame(preview_body, style="Card.TFrame")
        metadata.grid(row=1, column=0, sticky="ew", pady=(12, 10))
        metadata.columnconfigure(0, weight=1)
        ttk.Label(metadata, textvariable=self.video_title_var, style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(metadata, textvariable=self.video_meta_var, style="CardText.TLabel").grid(row=0, column=1, sticky="e", padx=(10, 0))
        preview_surface = tk.Frame(preview_body, background=colors["field"], height=130, highlightbackground=colors["border"], highlightthickness=1)
        preview_surface.grid(row=2, column=0, sticky="nsew")
        self.preview_label = tk.Label(preview_surface, background=colors["field"], foreground=colors["muted"], text="Paste a link to load the preview", font=("Segoe UI", 11), anchor="center")
        self.preview_label.pack(fill="both", expand=True)
        self.range_canvas = tk.Canvas(preview_body, height=68, background=colors["surface_alt"], highlightthickness=0, cursor="hand2")
        self.range_canvas.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.range_canvas.bind("<Configure>", lambda _event: self._draw_range_picker())
        self.range_canvas.bind("<ButtonPress-1>", self._range_picker_press)
        self.range_canvas.bind("<B1-Motion>", self._range_picker_drag)
        self.range_canvas.bind("<ButtonRelease-1>", self._range_picker_release)
        range_controls = ttk.Frame(preview_body, style="Card.TFrame")
        range_controls.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(range_controls, text="Start", style="CardText.TLabel").pack(side="left")
        self.start_entry = ttk.Entry(range_controls, textvariable=self.start_time_var, width=11)
        self.start_entry.pack(side="left", padx=(6, 12))
        ttk.Label(range_controls, text="End", style="CardText.TLabel").pack(side="left")
        self.end_entry = ttk.Entry(range_controls, textvariable=self.end_time_var, width=11)
        self.end_entry.pack(side="left", padx=(6, 8))
        self.start_entry.bind("<Return>", self._sync_range_from_entries)
        self.end_entry.bind("<Return>", self._sync_range_from_entries)
        self.start_entry.bind("<FocusOut>", self._sync_range_from_entries)
        self.end_entry.bind("<FocusOut>", self._sync_range_from_entries)
        ttk.Button(range_controls, text="Reset", command=self._reset_range_to_full_video, style="Ghost.TButton").pack(side="right")
        preview_footer = ttk.Frame(preview_body, style="Card.TFrame")
        preview_footer.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(preview_footer, textvariable=self.range_status_var, style="CardText.TLabel").pack(side="left")
        self.preview_play_button = ttk.Button(preview_footer, text="▶  Play preview", command=self._toggle_preview_playback, style="Secondary.TButton")
        self.preview_play_button.pack(side="right")
        self.preview_play_button.state(["disabled"])

        quality_card = rounded_card(workspace, padding=10)
        quality_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        quality_body = quality_card.body
        quality_body.columnconfigure(0, weight=1)
        quality_body.columnconfigure(1, weight=1)
        quality_body.rowconfigure(2, weight=1)
        step_header(quality_body, "3", "Choose your quality", "Select video, audio, or both. The best options are preselected.").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Label(quality_body, textvariable=self.quality_status_var, style="CardText.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))

        video_frame = ttk.LabelFrame(quality_body, text="Video", padding=8, style="Card.TLabelframe")
        video_frame.grid(row=2, column=0, sticky="nsew", padx=(0, 5))
        video_frame.rowconfigure(0, weight=1)
        video_frame.columnconfigure(0, weight=1)
        self.video_list = ttk.Treeview(video_frame, columns=("label",), show="headings", height=2, selectmode="browse")
        self.video_list.heading("label", text="Available formats")
        self.video_list.column("label", width=270, anchor="w")
        self.video_list.grid(row=0, column=0, sticky="nsew")
        ttk.Button(video_frame, text="Clear", command=lambda: self.video_list.selection_remove(*self.video_list.selection()), style="Ghost.TButton").grid(row=1, column=0, sticky="w", pady=(5, 0))

        audio_frame = ttk.LabelFrame(quality_body, text="Audio", padding=8, style="Card.TLabelframe")
        audio_frame.grid(row=2, column=1, sticky="nsew", padx=(5, 0))
        audio_frame.rowconfigure(0, weight=1)
        audio_frame.columnconfigure(0, weight=1)
        self.audio_list = ttk.Treeview(audio_frame, columns=("label",), show="headings", height=2, selectmode="browse")
        self.audio_list.heading("label", text="Available formats")
        self.audio_list.column("label", width=270, anchor="w")
        self.audio_list.grid(row=0, column=0, sticky="nsew")
        ttk.Button(audio_frame, text="Clear", command=lambda: self.audio_list.selection_remove(*self.audio_list.selection()), style="Ghost.TButton").grid(row=1, column=0, sticky="w", pady=(5, 0))

        quality_footer = ttk.Frame(quality_body, style="Card.TFrame")
        quality_footer.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        quality_footer.columnconfigure(1, weight=1)
        ttk.Label(quality_footer, text="Save to", style="CardText.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(quality_footer, text=str(DOWNLOAD_DIR), style="CardText.TLabel").grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Button(quality_footer, text="DOWNLOAD", command=self.download_selected, style="Download.TButton").grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        progress_card = rounded_card(content, padding=10)
        progress_card.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        progress_body = progress_card.body
        progress_body.columnconfigure(0, weight=1)
        progress_header = ttk.Frame(progress_body, style="Card.TFrame")
        progress_header.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_header, text="4", style="Step.TLabel").pack(side="left")
        ttk.Label(progress_header, text="Download progress", style="CardTitle.TLabel").pack(side="left", padx=(10, 0))
        ttk.Label(progress_header, textvariable=self.progress_text_var, style="CardText.TLabel").pack(side="right")
        self.progress_bar = ttk.Progressbar(progress_body, variable=self.progress_var, maximum=100, mode="determinate", style="Accent.Horizontal.TProgressbar")
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=(12, 0))

        activity_row = ttk.Frame(content, style="App.TFrame")
        activity_row.grid(row=4, column=0, sticky="nsew")
        activity_row.columnconfigure(0, weight=1)
        activity_row.columnconfigure(1, weight=1)
        activity_row.rowconfigure(0, weight=1)

        batch_card = rounded_card(activity_row, padding=10)
        batch_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        batch_body = batch_card.body
        batch_body.columnconfigure(1, weight=1)
        ttk.Label(batch_body, text="Batch download", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        batch_controls = ttk.Frame(batch_body, style="Card.TFrame")
        batch_controls.grid(row=0, column=1, sticky="ew", padx=(12, 0))
        batch_controls.columnconfigure(0, weight=1)
        ttk.Entry(batch_controls, textvariable=self.batch_url_var, style="TEntry").grid(row=0, column=0, sticky="ew")
        ttk.Label(batch_controls, text="Recent", style="CardText.TLabel").grid(row=0, column=1, sticky="w", padx=(10, 6))
        ttk.Spinbox(batch_controls, from_=1, to=1000, textvariable=self.batch_count_var, width=5).grid(row=0, column=2, sticky="w")
        ttk.Button(batch_controls, text="Download recent", command=self.download_recent, style="Secondary.TButton").grid(row=0, column=3, sticky="w", padx=(8, 0))

        logs_card = rounded_card(activity_row, padding=10)
        logs_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        logs_body = logs_card.body
        logs_body.columnconfigure(0, weight=1)
        logs_body.rowconfigure(1, weight=1)
        logs_header = ttk.Frame(logs_body, style="Card.TFrame")
        logs_header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(logs_header, text="Activity log", style="CardTitle.TLabel").pack(side="left")
        ttk.Label(logs_header, text="Click an entry to copy", style="CardText.TLabel").pack(side="right")
        self.log_box = ttk.Treeview(logs_body, columns=("log",), show="headings", height=1)
        self.log_box.heading("log", text="Status")
        self.log_box.column("log", width=480, anchor="w")
        self.log_box.grid(row=1, column=0, sticky="nsew")
        self.log_box.bind("<ButtonRelease-1>", self.copy_selected_log)
        for card in rounded_cards:
            card.refresh_size()

    def open_download_folder(self):
        """Open the downloads directory in the platform's file manager."""
        try:
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                # /n asks Explorer to create a new window.
                subprocess.Popen(["explorer.exe", "/n,", str(DOWNLOAD_DIR)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(DOWNLOAD_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(DOWNLOAD_DIR)])
        except Exception as e:
            cleaned = self._clean_error(e)
            self.log(f"Could not open download folder: {cleaned}")
            messagebox.showerror(
                "Open Folder Failed",
                f"Could not open the download folder:\n{cleaned}",
            )

    def reveal_download_path(self, filepath: str | Path):
        """Reveal one downloaded file in Explorer, Finder, or the file manager."""
        path = Path(filepath).resolve()
        try:
            if sys.platform == "win32":
                if path.is_file():
                    subprocess.Popen(["explorer.exe", f"/select,{path}"])
                else:
                    self.open_download_folder()
            elif sys.platform == "darwin":
                if path.exists():
                    subprocess.Popen(["open", "-R", str(path)])
                else:
                    self.open_download_folder()
            else:
                subprocess.Popen(["xdg-open", str(path.parent if not path.exists() else path)])
        except Exception as e:
            cleaned = self._clean_error(e)
            self.log(f"Could not reveal download: {cleaned}")
            messagebox.showerror(
                "Open File Failed",
                f"Could not open the downloaded file:\n{cleaned}",
            )

    def _record_download_history(
        self,
        filepath: str | Path | None,
        title: str,
        download_kind: str,
        source_url: str = "",
        clip_range: tuple[float, float] | None = None,
    ):
        """Persist a completed file and notify a native History page if present."""
        if not filepath:
            return
        entry = append_download_history(
            filepath,
            title,
            download_kind,
            source_url,
            clip_range,
        )
        if entry and callable(getattr(self, "_history_updated", None)):
            self.root.after(0, self._history_updated)

    def _check_for_update_worker(self):
        """Check GitHub without blocking the Tk event loop."""
        try:
            local_commit, remote, branch = current_update_identity()
            if not remote:
                self.root.after(0, self._show_no_update_button)
                return
            remote_commit = remote_head_commit(remote, branch)
            if not update_is_available(local_commit, remote_commit):
                self.root.after(0, self._show_no_update_button)
                return

            self._update_remote = remote
            self._update_branch = branch
            self.root.after(0, self._show_update_button)
        except Exception as error:
            self.log(f"GitHub update check unavailable: {self._clean_error(error)}")
            self.root.after(0, self._show_update_check_failed)

    def _show_no_update_button(self):
        if self._closing or self._update_available:
            return
        self.update_button.configure(
            text="↓  Up to date",
            style="Secondary.TButton",
        )
        self.update_button.state(["disabled"])

    def _show_update_check_failed(self):
        if self._closing or self._update_available:
            return
        self.update_button.configure(
            text="↓  GitHub check failed",
            style="Secondary.TButton",
        )
        self.update_button.state(["disabled"])

    def _show_update_button(self):
        if self._closing:
            return
        self._update_available = True
        self.update_button.configure(
            text="↓  Update available",
            style="Download.TButton",
        )
        self.update_button.state(["!disabled"])
        self.log("A new GitHub build is available.")

    def download_update_and_rebuild(self):
        """Hand the rebuild to another process, then close this app."""
        if not self._update_remote or not self._update_available:
            return

        if getattr(sys, "frozen", False):
            target_path = installed_application_path()
            if sys.platform == "win32":
                updater_dir = Path(tempfile.mkdtemp(prefix="yt_downloader_updater_"))
                updater_executable = updater_dir / "yt_downloader_updater.exe"
                try:
                    shutil.copy2(target_path, updater_executable)
                except OSError as error:
                    messagebox.showerror("Update Failed", self._clean_error(error))
                    return
                command = [str(updater_executable)]
            elif sys.platform == "darwin":
                # A macOS onedir bundle can run a second worker process from its
                # own Contents/MacOS executable while the visible app closes.
                command = [str(sys.executable)]
            else:
                messagebox.showerror(
                    "Update Failed",
                    "Native updates are currently supported on Windows and macOS.",
                )
                return
        else:
            source_root = find_source_root()
            if not source_root:
                messagebox.showerror("Update Failed", "The project source was not found.")
                return
            target_path = native_build_artifact(source_root)
            command = [sys.executable, str(Path(__file__).resolve())]

        command.extend([
            "--self-update-worker",
            "--remote",
            self._update_remote,
            "--branch",
            self._update_branch,
            "--target-path",
            str(target_path),
        ])

        self.update_button.state(["disabled"])
        self.update_button.configure(text="↓  Installing update...")
        try:
            subprocess.Popen(command, cwd=target_path.parent)
        except OSError as error:
            self.update_button.state(["!disabled"])
            self.update_button.configure(text="↓  Update available")
            messagebox.showerror("Update Failed", self._clean_error(error))
            return
        self.root.after(100, self._on_close)

    def _on_close(self):
        """Stop FFmpeg helpers and remove temporary preview files on exit."""
        self._closing = True
        self._stop_preview_playback()
        for preview_dir in list(self.preview_dirs):
            shutil.rmtree(preview_dir, ignore_errors=True)
        self.preview_dirs.clear()
        self.root.destroy()

    def _preview_after(self, callback):
        """Schedule a preview UI callback while the Tk window still exists."""
        if self._closing:
            return
        try:
            self.root.after(0, callback)
        except tk.TclError:
            pass

    @staticmethod
    def _format_timestamp(seconds: float) -> str:
        """Format a picker value in a compact, editable HH:MM:SS form."""
        seconds = max(0.0, float(seconds))
        rounded = round(seconds, 1)
        whole_seconds = int(rounded)
        tenths = int(round((rounded - whole_seconds) * 10))
        if tenths == 10:
            whole_seconds += 1
            tenths = 0
        hours, remainder = divmod(whole_seconds, 3600)
        minutes, display_seconds = divmod(remainder, 60)
        value = f"{hours:02d}:{minutes:02d}:{display_seconds:02d}"
        return f"{value}.{tenths}" if tenths else value

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format a duration for the range status line."""
        seconds = max(0.0, float(seconds))
        if seconds >= 3600:
            return VideoDownloaderGUI._format_timestamp(seconds)
        minutes, remainder = divmod(int(round(seconds)), 60)
        return f"{minutes}m {remainder:02d}s"

    def _ffmpeg_executable(self) -> str | None:
        if FFMPEG_LOCATION:
            located = shutil.which("ffmpeg", path=FFMPEG_LOCATION)
            if located:
                return located
        return shutil.which("ffmpeg")

    def _ffprobe_executable(self) -> str | None:
        if FFMPEG_LOCATION:
            located = shutil.which("ffprobe", path=FFMPEG_LOCATION)
            if located:
                return located
        return shutil.which("ffprobe")

    def _probe_preview_duration(self, filepath: Path) -> float:
        """Read duration from the downloaded preview when the extractor omitted it."""
        ffprobe = self._ffprobe_executable()
        if not ffprobe:
            return 0.0
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(filepath),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
                **subprocess_window_options(),
            )
            return max(0.0, float(result.stdout.strip()))
        except (OSError, ValueError, subprocess.SubprocessError):
            return 0.0

    def _set_preview_duration_from_probe(self, duration: float, generation: int):
        if self._closing or generation != self.preview_generation or self.preview_duration or not duration:
            return
        self.preview_duration = duration
        self.range_start_seconds = 0.0
        self.range_end_seconds = duration
        self.preview_playhead = 0.0
        self.start_time_var.set(self._format_timestamp(0.0))
        self.end_time_var.set(self._format_timestamp(duration))
        self._update_range_status()
        self._draw_range_picker()

    def _reset_preview_ui(self, generation: int):
        """Clear the old preview before a new URL is checked."""
        self._stop_preview_playback()
        self.preview_duration = 0.0
        self.preview_path = None
        self.preview_dir = None
        self.preview_photo = None
        self.preview_request_id += 1
        if self.preview_frame_after:
            try:
                self.root.after_cancel(self.preview_frame_after)
            except tk.TclError:
                pass
            self.preview_frame_after = None
        self.preview_playhead = 0.0
        self.range_start_seconds = 0.0
        self.range_end_seconds = 0.0
        self.range_drag_handle = None
        self.start_time_var.set("")
        self.end_time_var.set("")
        if hasattr(self, "video_list"):
            self.video_list.delete(*self.video_list.get_children())
            self.audio_list.delete(*self.audio_list.get_children())
            self.video_id_to_format.clear()
            self.audio_id_to_format.clear()
        self.video_title_var.set("Loading video information…")
        self.video_meta_var.set("Ingesting available formats and preparing the preview")
        self.quality_status_var.set("Loading video and audio qualities…")
        self.range_status_var.set("Loading video duration and FFmpeg preview...")
        self.preview_label.configure(image="", text="Loading video duration and FFmpeg preview...")
        self.preview_play_button.configure(text="▶ Play preview")
        self.preview_play_button.state(["disabled"])
        self._draw_range_picker()

        # Workers clean up any directory they are still using when they notice
        # their generation is stale. These are exact mkdtemp paths, not globs.
        for preview_dir in list(self.preview_dirs):
            if preview_dir != self.preview_dir:
                shutil.rmtree(preview_dir, ignore_errors=True)
                self.preview_dirs.discard(preview_dir)

    def _configure_preview(self, duration: float, generation: int, url: str):
        """Prepopulate the range and start the asynchronous preview download."""
        if generation != self.preview_generation:
            return

        self.preview_duration = max(0.0, duration)
        self.range_start_seconds = 0.0
        self.range_end_seconds = self.preview_duration
        self.preview_playhead = 0.0
        self.start_time_var.set(self._format_timestamp(0.0))
        if self.preview_duration:
            self.end_time_var.set(self._format_timestamp(self.preview_duration))
            self._update_range_status()
        else:
            self.end_time_var.set("")
            self.range_status_var.set(
                "The platform did not provide a duration; enter Start and End manually."
            )
        self._draw_range_picker()
        self._run_in_thread(lambda: self._prepare_preview_worker(url, generation))

    def _update_range_status(self):
        if not self.preview_duration:
            return
        selected_duration = max(0.0, self.range_end_seconds - self.range_start_seconds)
        self.range_status_var.set(
            "Selected "
            f"{self._format_timestamp(self.range_start_seconds)} → "
            f"{self._format_timestamp(self.range_end_seconds)}"
            f"  •  {self._format_duration(selected_duration)}"
        )

    def _range_picker_bounds(self) -> tuple[float, float]:
        width = max(self.range_canvas.winfo_width(), 320)
        return 20.0, float(width - 20)

    def _range_x_for_seconds(self, seconds: float) -> float:
        left, right = self._range_picker_bounds()
        if not self.preview_duration:
            return left
        ratio = max(0.0, min(1.0, seconds / self.preview_duration))
        return left + ratio * (right - left)

    def _seconds_for_range_x(self, x: float) -> float:
        left, right = self._range_picker_bounds()
        if right <= left or not self.preview_duration:
            return 0.0
        ratio = max(0.0, min(1.0, (x - left) / (right - left)))
        # Tenths of a second gives the user a useful amount of precision while
        # keeping the timestamps readable.
        return round(ratio * self.preview_duration, 1)

    def _draw_range_picker(self):
        if not hasattr(self, "range_canvas"):
            return
        canvas = self.range_canvas
        canvas.delete("all")
        left, right = self._range_picker_bounds()
        center_y = 30
        canvas.create_line(
            left,
            center_y,
            right,
            center_y,
            fill="#64748b",
            width=8,
            capstyle="round",
        )
        if not self.preview_duration:
            canvas.create_text(
                left,
                60,
                anchor="w",
                fill="#cbd5e1",
                text="Time range becomes draggable after the video duration is loaded",
            )
            return

        start_x = self._range_x_for_seconds(self.range_start_seconds)
        end_x = self._range_x_for_seconds(self.range_end_seconds)
        canvas.create_line(
            start_x,
            center_y,
            end_x,
            center_y,
            fill="#38bdf8",
            width=8,
            capstyle="round",
        )

        playhead_x = self._range_x_for_seconds(self.preview_playhead)
        canvas.create_line(
            playhead_x,
            10,
            playhead_x,
            50,
            fill="#fbbf24",
            width=2,
        )
        for seconds in (0.0, self.preview_duration / 2, self.preview_duration):
            tick_x = self._range_x_for_seconds(seconds)
            canvas.create_line(tick_x, 43, tick_x, 49, fill="#94a3b8")
            canvas.create_text(
                tick_x,
                64,
                fill="#cbd5e1",
                text=self._format_timestamp(seconds),
            )

        for x, fill in ((start_x, "#22c55e"), (end_x, "#ef4444")):
            canvas.create_oval(
                x - 10,
                center_y - 10,
                x + 10,
                center_y + 10,
                fill=fill,
                outline="#f8fafc",
                width=2,
            )

    def _set_range_handle(self, seconds: float):
        if not self.preview_duration or not self.range_drag_handle:
            return
        minimum_gap = min(0.1, max(self.preview_duration / 1000, 0.01))
        if self.range_drag_handle == "start":
            self.range_start_seconds = max(
                0.0,
                min(seconds, self.range_end_seconds - minimum_gap),
            )
            preview_timestamp = self.range_start_seconds
        else:
            self.range_end_seconds = min(
                self.preview_duration,
                max(seconds, self.range_start_seconds + minimum_gap),
            )
            # Seeking exactly to the final timestamp can produce no frame.
            preview_timestamp = max(self.range_start_seconds, self.range_end_seconds - 0.1)

        self.start_time_var.set(self._format_timestamp(self.range_start_seconds))
        self.end_time_var.set(self._format_timestamp(self.range_end_seconds))
        self.preview_playhead = preview_timestamp
        self._update_range_status()
        self._draw_range_picker()
        self._request_preview_frame(preview_timestamp)

    def _range_picker_press(self, event):
        if not self.preview_duration:
            return
        start_x = self._range_x_for_seconds(self.range_start_seconds)
        end_x = self._range_x_for_seconds(self.range_end_seconds)
        if abs(event.x - start_x) <= 15:
            self.range_drag_handle = "start"
        elif abs(event.x - end_x) <= 15:
            self.range_drag_handle = "end"
        else:
            midpoint = (start_x + end_x) / 2
            self.range_drag_handle = "start" if event.x < midpoint else "end"
        self._stop_preview_playback()
        self._set_range_handle(self._seconds_for_range_x(event.x))

    def _range_picker_drag(self, event):
        self._set_range_handle(self._seconds_for_range_x(event.x))

    def _range_picker_release(self, _event):
        self.range_drag_handle = None

    def _sync_range_from_entries(self, _event=None):
        if not self.preview_duration:
            return
        try:
            start = self._parse_timestamp(self.start_time_var.get(), "Start time")
            end = self._parse_timestamp(self.end_time_var.get(), "End time")
            start = 0.0 if start is None else start
            end = self.preview_duration if end is None else end
            if end <= start:
                raise ValueError("End time must be greater than start time.")
            if end > self.preview_duration:
                raise ValueError("End time cannot be after the video duration.")
        except ValueError as error:
            self.range_status_var.set(str(error))
            self.start_time_var.set(self._format_timestamp(self.range_start_seconds))
            self.end_time_var.set(self._format_timestamp(self.range_end_seconds))
            return "break"

        self._stop_preview_playback()
        self.range_start_seconds = start
        self.range_end_seconds = end
        self.preview_playhead = start
        self.start_time_var.set(self._format_timestamp(start))
        self.end_time_var.set(self._format_timestamp(end))
        self._update_range_status()
        self._draw_range_picker()
        self._request_preview_frame(start)
        return "break"

    def _reset_range_to_full_video(self):
        if not self.preview_duration:
            return
        self._stop_preview_playback()
        self.range_start_seconds = 0.0
        self.range_end_seconds = self.preview_duration
        self.preview_playhead = 0.0
        self.start_time_var.set(self._format_timestamp(0.0))
        self.end_time_var.set(self._format_timestamp(self.preview_duration))
        self._update_range_status()
        self._draw_range_picker()
        self._request_preview_frame(0.0)

    def _set_preview_status(self, text: str, generation: int):
        if self._closing or generation != self.preview_generation:
            return
        self.range_status_var.set(text)

    def _prepare_preview_worker(self, url: str, generation: int):
        """Download a small preview source that can be seeked locally."""
        preview_dir = Path(tempfile.mkdtemp(prefix="yt_downloader_preview_"))
        self.preview_dirs.add(preview_dir)
        try:
            if self._closing or generation != self.preview_generation:
                return
            ffmpeg = self._ffmpeg_executable()
            if not ffmpeg:
                self._preview_after(
                    lambda: self._set_preview_status(
                        "FFmpeg is not available; install it and add it to PATH.",
                        generation,
                    ),
                )
                return

            self._preview_after(
                lambda: self._set_preview_status("Downloading low-resolution preview...", generation),
            )
            ydl_opts = {
                "format": "bestvideo[height<=360]/best[height<=360]/best",
                "outtmpl": str(preview_dir / "preview.%(ext)s"),
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "ignoreconfig": True,
                "overwrites": True,
                "continuedl": False,
            }
            if FFMPEG_LOCATION:
                ydl_opts["ffmpeg_location"] = FFMPEG_LOCATION

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                prepared_filepath = ydl.prepare_filename(info)
            preview_path = Path(
                self._find_downloaded_filepath(info or {}, prepared_filepath, None)
            )

            if self._closing or generation != self.preview_generation:
                return
            if not self.preview_duration:
                probed_duration = self._probe_preview_duration(preview_path)
                if probed_duration:
                    self._preview_after(
                        lambda: self._set_preview_duration_from_probe(
                            probed_duration,
                            generation,
                        ),
                    )
            self.preview_path = preview_path
            self.preview_dir = preview_dir
            self._preview_after(lambda: self._preview_ready(preview_path, generation))
        except Exception as error:
            cleaned = self._clean_error(error)
            if not self._closing:
                self.log(f"FFmpeg preview unavailable: {cleaned}")
            self._preview_after(
                lambda: self._set_preview_status(
                    "Preview unavailable; the time fields are still editable.",
                    generation,
                ),
            )
        finally:
            if generation != self.preview_generation or self.preview_dir != preview_dir:
                shutil.rmtree(preview_dir, ignore_errors=True)
                self.preview_dirs.discard(preview_dir)

    def _preview_ready(self, preview_path: Path, generation: int):
        if self._closing or generation != self.preview_generation or self.preview_path != preview_path:
            return
        self.preview_play_button.state(["!disabled"])
        self._update_range_status()
        self.range_status_var.set(
            f"{self.range_status_var.get()}  •  Preview ready"
        )
        self._request_preview_frame(self.range_start_seconds)

    def _request_preview_frame(self, timestamp: float):
        if not self.preview_path or not self.preview_path.is_file():
            return
        self.preview_request_id += 1
        request_id = self.preview_request_id
        generation = self.preview_generation
        if self.preview_frame_after:
            try:
                self.root.after_cancel(self.preview_frame_after)
            except tk.TclError:
                pass
        self.preview_frame_after = self.root.after(
            80,
            lambda: self._run_in_thread(
                lambda: self._render_preview_frame_worker(
                    timestamp,
                    generation,
                    request_id,
                )
            ),
        )

    def _render_preview_frame_worker(
        self,
        timestamp: float,
        generation: int,
        request_id: int,
    ):
        if self._closing or generation != self.preview_generation or request_id != self.preview_request_id:
            return
        ffmpeg = self._ffmpeg_executable()
        preview_path = self.preview_path
        if not ffmpeg or not preview_path:
            return
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, timestamp):g}",
            "-i",
            str(preview_path),
            "-frames:v",
            "1",
            "-an",
            "-vf",
            "scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2:color=black",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "pipe:1",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=30,
                check=False,
                **subprocess_window_options(),
            )
            if result.returncode != 0 or not result.stdout:
                return
            encoded = base64.b64encode(result.stdout).decode("ascii")
            self._preview_after(
                lambda: self._display_preview_frame(
                    encoded,
                    timestamp,
                    generation,
                    request_id,
                ),
            )
        except (OSError, subprocess.SubprocessError):
            return

    def _display_preview_frame(
        self,
        encoded: str,
        timestamp: float,
        generation: int,
        request_id: int,
    ):
        if self._closing or generation != self.preview_generation or request_id != self.preview_request_id:
            return
        try:
            photo = tk.PhotoImage(data=encoded)
        except tk.TclError:
            return
        self.preview_photo = photo
        self.preview_label.configure(image=photo, text="")
        self.preview_playhead = timestamp
        self._draw_range_picker()

    @staticmethod
    def _png_frames(stream):
        """Yield complete PNG images from FFmpeg's image2pipe output."""
        signature = b"\x89PNG\r\n\x1a\n"
        buffer = bytearray()
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                start = buffer.find(signature)
                if start < 0:
                    if len(buffer) > len(signature):
                        del buffer[:-len(signature)]
                    break
                if start:
                    del buffer[:start]
                chunk_offset = len(signature)
                frame_end = None
                while len(buffer) >= chunk_offset + 8:
                    chunk_length = int.from_bytes(
                        buffer[chunk_offset:chunk_offset + 4], "big"
                    )
                    next_chunk = chunk_offset + 12 + chunk_length
                    if len(buffer) < next_chunk:
                        break
                    chunk_type = bytes(buffer[chunk_offset + 4:chunk_offset + 8])
                    chunk_offset = next_chunk
                    if chunk_type == b"IEND":
                        frame_end = chunk_offset
                        break
                if frame_end is None:
                    break
                yield bytes(buffer[:frame_end])
                del buffer[:frame_end]

    def _toggle_preview_playback(self):
        if self.preview_playing:
            self._stop_preview_playback()
        else:
            self._start_preview_playback()

    def _start_preview_playback(self):
        if not self.preview_path or not self.preview_path.is_file() or not self.preview_duration:
            return
        if self.preview_playhead < self.range_start_seconds or self.preview_playhead >= self.range_end_seconds - 0.05:
            self.preview_playhead = self.range_start_seconds
        self.preview_playing = True
        self.preview_player_stop = threading.Event()
        stop_event = self.preview_player_stop
        self.preview_player_token += 1
        token = self.preview_player_token
        generation = self.preview_generation
        preview_path = self.preview_path
        start = self.preview_playhead
        end = self.range_end_seconds
        self.preview_play_button.configure(text="❚❚ Pause preview")
        self._run_in_thread(
            lambda: self._preview_play_worker(
                preview_path,
                start,
                end,
                generation,
                token,
                stop_event,
            )
        )

    def _stop_preview_playback(self):
        self.preview_playing = False
        self.preview_player_token += 1
        self.preview_player_stop.set()
        process = self.preview_player_process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        self.preview_player_process = None
        if hasattr(self, "preview_play_button"):
            self.preview_play_button.configure(text="▶ Play preview")

    def _preview_play_worker(
        self,
        preview_path: Path,
        start: float,
        end: float,
        generation: int,
        token: int,
        stop_event: threading.Event,
    ):
        ffmpeg = self._ffmpeg_executable()
        if not ffmpeg or stop_event.is_set():
            self._preview_after(
                lambda: self._preview_playback_finished(generation, token, end),
            )
            return
        fps = 8
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, start):g}",
            "-i",
            str(preview_path),
            "-t",
            f"{max(0.05, end - start):g}",
            "-an",
            "-vf",
            f"scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2:color=black,fps={fps}",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "pipe:1",
        ]
        process = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                **subprocess_window_options(),
            )
            self.preview_player_process = process
            for index, frame in enumerate(self._png_frames(process.stdout)):
                if stop_event.is_set():
                    break
                timestamp = min(end, start + index / fps)
                encoded = base64.b64encode(frame).decode("ascii")
                self._preview_after(
                    lambda encoded=encoded, timestamp=timestamp: self._display_playback_frame(
                        encoded,
                        timestamp,
                        generation,
                        token,
                    ),
                )
            if process.stdout:
                process.stdout.close()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            if process and process.poll() is None:
                process.terminate()
        finally:
            if self.preview_player_process is process:
                self.preview_player_process = None
            self._preview_after(
                lambda: self._preview_playback_finished(generation, token, end),
            )

    def _display_playback_frame(
        self,
        encoded: str,
        timestamp: float,
        generation: int,
        token: int,
    ):
        if (
            self._closing
            or generation != self.preview_generation
            or token != self.preview_player_token
            or not self.preview_playing
        ):
            return
        try:
            photo = tk.PhotoImage(data=encoded)
        except tk.TclError:
            return
        self.preview_photo = photo
        self.preview_label.configure(image=photo, text="")
        self.preview_playhead = timestamp
        self._draw_range_picker()

    def _preview_playback_finished(self, generation: int, token: int, end: float):
        if self._closing or generation != self.preview_generation or token != self.preview_player_token:
            return
        self.preview_playing = False
        self.preview_playhead = end
        self.preview_play_button.configure(text="▶ Play preview")
        self._draw_range_picker()

    def _set_progress(self, value: float, text: str):
        """Safely update download progress from a yt-dlp worker thread."""
        value = max(0.0, min(100.0, value))
        self._progress_value = value

        def update_ui():
            self.progress_var.set(value)
            self.progress_text_var.set(text)

        if threading.current_thread() is threading.main_thread():
            update_ui()
        else:
            self.root.after(0, update_ui)

    def _progress_hook(self, status: dict):
        """Translate yt-dlp byte/fragment progress into a percentage."""
        state = status.get("status")
        if state == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            downloaded = status.get("downloaded_bytes", 0)

            if total:
                percent = downloaded / total * 100
            else:
                fragment_count = status.get("fragment_count")
                fragment_index = status.get("fragment_index", 0)
                percent = fragment_index / fragment_count * 100 if fragment_count else 0

            # yt-dlp can report updates many times per second; avoid flooding Tk's queue.
            now = time.monotonic()
            if now - self._last_progress_update < 0.1 and percent < 100:
                return
            self._last_progress_update = now

            info = status.get("info_dict") or {}
            title = info.get("title") or "file"
            self._set_progress(percent, f"Downloading {title}: {percent:.1f}%")
        elif state == "finished":
            self._set_progress(100, "Download data complete. Finalizing file...")
        elif state == "error":
            self._set_progress(self._progress_value, "Download failed")

    def log(self, text: str):
        def add_log_entry():
            self.log_box.insert("", END, values=(text,))
            self.log_box.yview_moveto(1.0)

        if threading.current_thread() is threading.main_thread():
            add_log_entry()
        else:
            self.root.after(0, add_log_entry)

    def copy_selected_log(self, _event=None):
        selected = self.log_box.selection()
        if not selected:
            return
        value = self.log_box.item(selected[0], "values")
        if not value:
            return
        text = str(value[0])
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.log(f"Copied log: {text[:80]}")

    def _run_in_thread(self, target):
        t = threading.Thread(target=target, daemon=True)
        t.start()

    def _check_url_from_entry(self, _event=None):
        """Fetch formats when a manually entered URL is submitted with Enter."""
        self.check_qualities()
        return "break"

    def _check_url_after_paste(self, _event=None):
        """Wait for Tk to update the entry, then fetch the pasted URL's formats."""
        self.root.after_idle(self.check_qualities)

    def paste_and_check_url(self):
        """Paste a video URL from the system clipboard and immediately inspect it."""
        try:
            url = self.root.clipboard_get().strip()
        except Exception:
            messagebox.showwarning(
                "Clipboard Unavailable",
                "Copy a video URL first, then click Paste & Analyze.",
            )
            return

        if not url:
            messagebox.showwarning(
                "Empty Clipboard",
                "Copy a video URL first, then click Paste & Analyze.",
            )
            return

        self.url_var.set(url)
        self.url_entry.focus_set()
        self.check_qualities()

    @staticmethod
    def _clean_error(err: Exception) -> str:
        # Strip ANSI color codes from yt-dlp errors for readability
        return re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", str(err))

    @staticmethod
    def _numeric_value(value, default: float = 0.0) -> float:
        """Return a sortable number for optional yt-dlp format metadata.

        Social-media extractors commonly leave fields such as height, fps, or
        bitrate as None. Those values must not be compared directly with the
        numeric values from other formats.
        """
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if number == number else default  # also treat NaN as missing

    @staticmethod
    def _parse_timestamp(value: str, field_name: str) -> float | None:
        """Parse seconds or an HH:MM:SS-style timestamp."""
        value = value.strip()
        if not value:
            return None

        parts = value.split(":")
        if len(parts) > 3:
            raise ValueError(f"{field_name} must be seconds or HH:MM:SS.")

        try:
            numbers = [float(part) for part in parts]
        except ValueError as e:
            raise ValueError(f"{field_name} must be seconds or HH:MM:SS.") from e

        if any(number < 0 for number in numbers):
            raise ValueError(f"{field_name} cannot be negative.")
        if len(numbers) > 1 and any(number >= 60 for number in numbers[1:]):
            raise ValueError(f"Minutes and seconds in {field_name} must be below 60.")

        seconds = 0.0
        for number in numbers:
            seconds = seconds * 60 + number
        return seconds

    @staticmethod
    def _range_filename_suffix(clip_range: tuple[float, float]) -> str:
        start, end = clip_range

        def format_time(value: float) -> str:
            if value == float("inf"):
                return "end"
            return f"{value:g}".replace(".", "_")

        return f" [extract {format_time(start)}-{format_time(end)}s]"

    @staticmethod
    def _process_local_video(
        filepath: str,
        clip_range: tuple[float, float] | None,
        remove_audio: bool,
    ):
        """Trim and/or remove audio from an already downloaded video."""
        source = Path(filepath)
        if not source.is_file():
            raise RuntimeError(f"Downloaded video file was not found: {source}")

        ffmpeg = None
        if FFMPEG_LOCATION:
            ffmpeg = shutil.which("ffmpeg", path=FFMPEG_LOCATION)
        ffmpeg = ffmpeg or shutil.which("ffmpeg")
        if not ffmpeg:
            action = "create a silent video" if remove_audio else "extract a video clip"
            raise RuntimeError(f"FFmpeg is required to {action}.")

        processed = source.with_name(f"{source.stem}.processed{source.suffix}")
        command = [ffmpeg, "-y"]
        if clip_range:
            start, end = clip_range
            command.extend(["-ss", f"{start:g}"])
        command.extend(["-i", str(source)])
        if clip_range and end != float("inf"):
            command.extend(["-t", f"{end - start:g}"])
        command.extend(["-map", "0:v:0"] if remove_audio else ["-map", "0"])
        command.extend([
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            str(processed),
        ])

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if result.returncode != 0:
                details = (result.stderr or result.stdout or "").strip()
                if len(details) > 2000:
                    details = details[-2000:]
                raise RuntimeError(
                    f"FFmpeg could not process the video (exit code {result.returncode})."
                    + (f"\n{details}" if details else "")
                )
            os.replace(processed, source)
        finally:
            if processed.exists():
                processed.unlink()

    @staticmethod
    def _find_downloaded_filepath(
        info: dict,
        prepared_filepath: str | None,
        merge_extension: str | None,
    ) -> str:
        """Resolve yt-dlp's final file, including a post-merge extension change."""
        candidates = [info.get("filepath"), info.get("_filename"), prepared_filepath]
        if prepared_filepath and merge_extension:
            candidates.insert(0, str(Path(prepared_filepath).with_suffix(
                f".{merge_extension}"
            )))

        for requested in info.get("requested_downloads") or []:
            candidates.append(requested.get("filepath"))

        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return candidate
        raise RuntimeError("yt-dlp did not report the downloaded video path.")

    @staticmethod
    def _find_audio_download_filepath(
        info: dict,
        prepared_filepath: str | None,
    ) -> str:
        """Resolve the MP3 produced by the audio postprocessor."""
        candidates: list[Path] = []
        raw_candidates = [
            info.get("filepath"),
            info.get("_filename"),
            prepared_filepath,
        ]
        raw_candidates.extend(
            requested.get("filepath")
            for requested in info.get("requested_downloads") or []
            if isinstance(requested, dict)
        )
        for raw in raw_candidates:
            if not raw:
                continue
            path = Path(raw)
            candidates.extend((path.with_suffix(".mp3"), path))

        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        raise RuntimeError("yt-dlp did not report the downloaded audio path.")

    def _extract_info_with_fallback(self, url: str):
        base_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "extract_flat": False,
            "ignoreconfig": True,
        }

        attempts = [
            ("default", base_opts),
            ("fallback: force generic format", {**base_opts, "format": "best"}),
        ]

        last_error = None
        for name, opts in attempts:
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                self.log(f"Format probe OK ({name})")
                return info
            except Exception as e:
                last_error = e
                self.log(f"Format probe failed ({name}): {self._clean_error(e)}")

        raise RuntimeError(self._clean_error(last_error) if last_error else "Unknown yt-dlp error")

    def check_qualities(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning(
                "Missing URL",
                f"Please enter a video URL from {SUPPORTED_PLATFORM_NAMES}.",
            )
            return

        if self._is_checking_qualities:
            return

        self.preview_generation += 1
        generation = self.preview_generation
        self._reset_preview_ui(generation)
        self._is_checking_qualities = True
        self._set_progress(0, "Checking available formats...")
        self._run_in_thread(lambda: self._check_qualities_worker(url, generation))

    def _check_qualities_worker(self, url: str, generation: int):
        try:
            self.log("Fetching formats...")
            info = self._extract_info_with_fallback(url)

            if info.get("_type") == "playlist" and info.get("entries"):
                info = info["entries"][0]

            formats = info.get("formats", [])
            if not formats:
                raise RuntimeError(
                    "No formats returned. Try updating yt-dlp and verify this is a single video URL."
                )

            self.video_formats = [
                f for f in formats
                if f.get("vcodec") != "none"
            ]
            self.audio_formats = [
                f for f in formats
                if f.get("acodec") != "none" and f.get("vcodec") == "none"
            ]

            self.video_formats.sort(
                key=lambda x: (
                    self._numeric_value(x.get("height")),
                    self._numeric_value(x.get("fps")),
                    self._numeric_value(x.get("tbr")),
                ),
                reverse=True,
            )
            self.audio_formats.sort(
                key=lambda x: (
                    self._numeric_value(x.get("abr")),
                    self._numeric_value(x.get("asr")),
                ),
                reverse=True,
            )

            self.root.after(
                0,
                lambda: self._display_formats(
                    self.video_formats,
                    self.audio_formats,
                    info.get("title", "unknown title"),
                    self._numeric_value(info.get("duration")),
                    url,
                    generation,
                ),
            )

        except Exception as e:
            cleaned = self._clean_error(e)
            self.log(f"Error fetching formats: {cleaned}")
            self.root.after(
                0,
                lambda: self.quality_status_var.set(
                    "Could not ingest formats. Check the link and try again."
                ),
            )
            self.root.after(
                0,
                lambda: messagebox.showerror(
                    "Error",
                    f"Failed to fetch qualities:\n{cleaned}",
                ),
            )
        finally:
            self.root.after(0, self._finish_checking_qualities)

    def _preferred_format_index(self, formats: list[dict], _kind: str) -> int | None:
        """Return the default format index; native UIs can override this."""
        return 0 if formats else None

    def _display_formats(
        self,
        video_formats: list[dict],
        audio_formats: list[dict],
        title: str,
        duration: float,
        url: str,
        generation: int,
    ):
        """Render formats on Tk's main thread after a background probe."""
        if generation != self.preview_generation:
            return
        self.video_title_var.set(title or "Untitled video")
        duration_text = self._format_duration(duration) if duration else "Duration unavailable"
        self.video_meta_var.set(
            f"{duration_text}  •  {len(video_formats)} video options  •  {len(audio_formats)} audio options"
        )
        self.quality_status_var.set(
            f"{len(video_formats)} video and {len(audio_formats)} audio formats ingested"
        )
        self.video_list.delete(*self.video_list.get_children())
        self.audio_list.delete(*self.audio_list.get_children())
        self.video_id_to_format.clear()
        self.audio_id_to_format.clear()

        for vf in video_formats:
            height = self._numeric_value(vf.get("height"))
            resolution = f"{int(height)}p" if height > 0 else "resolution unavailable"
            fps_value = self._numeric_value(vf.get("fps"))
            fps = f" {fps_value:g}fps" if fps_value > 0 else ""
            bitrate = self._numeric_value(vf.get("tbr"))
            label = (
                f"{resolution}{fps} · {vf.get('ext', '?')} · "
                f"{int(bitrate):,} kbps" if bitrate > 0 else
                f"{resolution}{fps} · {vf.get('ext', '?')} · bitrate n/a"
            )
            iid = self.video_list.insert("", END, values=(label,))
            self.video_id_to_format[iid] = vf

        for af in audio_formats:
            bitrate = self._numeric_value(af.get("abr"))
            label = (
                f"{int(bitrate):,} kbps · {af.get('acodec', '?')} · "
                f"{af.get('ext', '?')}" if bitrate > 0 else
                f"bitrate n/a · {af.get('acodec', '?')} · {af.get('ext', '?')}"
            )
            iid = self.audio_list.insert("", END, values=(label,))
            self.audio_id_to_format[iid] = af

        # Formats are sorted best-first before they reach this method. Select
        # the configured defaults so a pasted link is ready to download without
        # requiring extra clicks.
        video_items = self.video_list.get_children()
        video_index = self._preferred_format_index(video_formats, "video")
        if video_items and video_index is not None:
            video_index = max(0, min(video_index, len(video_items) - 1))
            self.video_list.selection_set(video_items[video_index])
            self.video_list.focus(video_items[video_index])

        audio_items = self.audio_list.get_children()
        audio_index = self._preferred_format_index(audio_formats, "audio")
        if audio_items and audio_index is not None:
            audio_index = max(0, min(audio_index, len(audio_items) - 1))
            self.audio_list.selection_set(audio_items[audio_index])
            self.audio_list.focus(audio_items[audio_index])

        self._set_progress(0, "Select a quality, then download")
        self.log(f"Loaded formats for: {title}")
        self._configure_preview(duration, generation, url)

    def _finish_checking_qualities(self):
        self._is_checking_qualities = False

    def download_selected(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning(
                "Missing URL",
                f"Please enter a video URL from {SUPPORTED_PLATFORM_NAMES}.",
            )
            return

        selected_video = self.video_list.selection()
        selected_audio = self.audio_list.selection()

        if not selected_video and not selected_audio:
            messagebox.showwarning(
                "Missing Selection",
                "Please select a video format, an audio format, or both.",
            )
            return

        try:
            start_time = self._parse_timestamp(self.start_time_var.get(), "Start time")
            end_time = self._parse_timestamp(self.end_time_var.get(), "End time")
        except ValueError as e:
            messagebox.showwarning("Invalid Extract Range", str(e))
            return

        clip_range = None
        if start_time is not None or end_time is not None:
            range_start = start_time if start_time is not None else 0.0
            range_end = end_time if end_time is not None else float("inf")
            if range_end <= range_start:
                messagebox.showwarning(
                    "Invalid Extract Range",
                    "End time must be greater than start time.",
                )
                return
            # The picker is prefilled with the complete video. Treat that
            # default as "no extract" so a normal full download keeps its
            # original filename and does not run an unnecessary FFmpeg pass.
            is_full_video = (
                self.preview_duration > 0
                and range_start <= 0.0
                and range_end != float("inf")
                and abs(range_end - self.preview_duration) <= 0.1
            )
            if not is_full_video:
                clip_range = (range_start, range_end)

        video_id = None
        video_has_audio = False
        if selected_video:
            video_format = self.video_id_to_format.get(selected_video[0], {})
            video_id = video_format.get("format_id")
            # Unknown audio metadata is treated conservatively: if the user
            # requests video only, FFmpeg will verify the result by retaining
            # only its video stream.
            video_has_audio = video_format.get("acodec") != "none"

        audio_id = None
        if selected_audio:
            audio_format = self.audio_id_to_format.get(selected_audio[0], {})
            audio_id = audio_format.get("format_id")

        status = "Preparing MP3 download..." if not video_id else "Preparing download..."
        if clip_range:
            status = "Preparing clip extract..."
        self._set_progress(0, status)
        self._run_in_thread(
            lambda: self._download_selected_worker(
                url,
                video_id,
                audio_id,
                video_has_audio,
                clip_range,
            )
        )

    def _download_selected_worker(
        self,
        url: str,
        video_id: str | None,
        audio_id: str | None,
        video_has_audio: bool,
        clip_range: tuple[float, float] | None = None,
    ):
        try:
            fmt, download_mode = build_download_plan(video_id, audio_id)
            audio_only = download_mode == "audio_only"
            video_only = download_mode == "video_only"

            if audio_only:
                download_kind = "MP3 audio"
            elif video_only:
                download_kind = "silent video"
            else:
                download_kind = "selected video and audio"
            if clip_range:
                download_kind += " extract"
            self.log(f"Downloading {download_kind} ({fmt})...")

            filename_suffix = self._range_filename_suffix(clip_range) if clip_range else ""

            ydl_opts = {
                "format": fmt,
                "outtmpl": str(
                    DOWNLOAD_DIR
                    / f"%(title)s [%(id)s]{filename_suffix}.%(ext)s"
                ),
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "ignoreconfig": True,
                "progress_hooks": [self._progress_hook],
            }
            if FFMPEG_LOCATION:
                ydl_opts["ffmpeg_location"] = FFMPEG_LOCATION

            if audio_only:
                ydl_opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "0",
                }]

                if clip_range:
                    # Download a valid source audio file first, then trim while
                    # converting it. Remote range seeking can leave an empty file
                    # that FFmpegExtractAudio cannot inspect with ffprobe.
                    # Always replace a same-named intermediate left by an older
                    # failed attempt; otherwise yt-dlp may reuse the corrupt stub.
                    ydl_opts["overwrites"] = True
                    ydl_opts["continuedl"] = False
                    start, end = clip_range
                    postprocessor_args = {}
                    if start > 0:
                        postprocessor_args["extractaudio+ffmpeg_i"] = [
                            "-ss",
                            f"{start:g}",
                        ]
                    if end != float("inf"):
                        postprocessor_args["extractaudio+ffmpeg_o"] = [
                            "-t",
                            f"{end - start:g}",
                        ]
                    ydl_opts["postprocessor_args"] = postprocessor_args
            elif not video_only:
                ydl_opts["merge_output_format"] = "mp4"

            info = None
            prepared_filepath = None
            for attempt in range(2):
                try:
                    # Re-extract on every attempt. YouTube media URLs are signed
                    # and temporary, so retrying with the same extracted URL can
                    # repeat an otherwise transient HTTP 403.
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        prepared_filepath = ydl.prepare_filename(info)
                    break
                except Exception as e:
                    error_text = self._clean_error(e)
                    is_forbidden = "403" in error_text or "Forbidden" in error_text
                    if attempt == 1 or not is_forbidden:
                        raise
                    self.log(
                        "YouTube returned HTTP 403; refreshing its temporary "
                        "media URL and retrying..."
                    )
                    self._set_progress(
                        self._progress_value,
                        "Refreshing the media URL after HTTP 403...",
                    )

            final_filepath = (
                self._find_audio_download_filepath(info or {}, prepared_filepath)
                if audio_only
                else self._find_downloaded_filepath(
                    info or {},
                    prepared_filepath,
                    ydl_opts.get("merge_output_format"),
                )
            )
            if (clip_range and not audio_only) or (video_only and video_has_audio):
                self._process_local_video(
                    final_filepath,
                    clip_range,
                    remove_audio=video_only and video_has_audio,
                )

            self._record_download_history(
                final_filepath,
                (info or {}).get("title", ""),
                download_kind,
                url,
                clip_range,
            )

            if clip_range:
                completion_text = "Clip extract complete"
            else:
                if audio_only:
                    completion_text = "MP3 download complete"
                elif video_only:
                    completion_text = "Silent video download complete"
                else:
                    completion_text = "Download complete"
            self._set_progress(100, completion_text)
            self.log(f"Done. Saved to: {DOWNLOAD_DIR}")
            messagebox.showinfo(
                "Success",
                f"{completion_text}.\nSaved to:\n{DOWNLOAD_DIR}",
            )

        except Exception as e:
            self._set_progress(self._progress_value, "Download failed")
            self.log(f"Download failed: {e}")
            messagebox.showerror("Error", f"Download failed:\n{e}")

    def download_recent(self):
        source_url = self.batch_url_var.get().strip()
        count = self.batch_count_var.get()

        if not source_url:
            messagebox.showwarning("Missing URL", "Please enter a channel or playlist URL.")
            return

        if count < 1:
            messagebox.showwarning("Invalid Number", "Count must be at least 1.")
            return

        self._set_progress(0, "Preparing batch download...")
        self._run_in_thread(lambda: self._download_recent_worker(source_url, count))

    def _download_recent_worker(self, source_url: str, count: int):
        try:
            self.log(f"Downloading {count} most recent videos...")
            before = {
                path.resolve(): path.stat().st_mtime_ns
                for path in DOWNLOAD_DIR.iterdir()
                if path.is_file()
            }
            ydl_opts = {
                "format": "bestvideo+bestaudio/best",
                "outtmpl": str(DOWNLOAD_DIR / "%(title)s [%(id)s].%(ext)s"),
                "merge_output_format": "mp4",
                "playlistreverse": True,
                "playlistend": count,
                "ignoreerrors": True,
                "quiet": True,
                "no_warnings": True,
                "ignoreconfig": True,
                "progress_hooks": [self._progress_hook],
            }
            if FFMPEG_LOCATION:
                ydl_opts["ffmpeg_location"] = FFMPEG_LOCATION

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([source_url])

            for path in DOWNLOAD_DIR.iterdir():
                if not path.is_file() or path.suffix in {".part", ".ytdl"}:
                    continue
                resolved = path.resolve()
                if resolved not in before or path.stat().st_mtime_ns > before[resolved]:
                    self._record_download_history(
                        path,
                        path.stem,
                        "Batch download",
                        source_url,
                    )

            self._set_progress(100, "Batch download complete")
            self.log(f"Batch complete. Saved to: {DOWNLOAD_DIR}")
            messagebox.showinfo("Success", f"Batch download completed.\nSaved to:\n{DOWNLOAD_DIR}")

        except Exception as e:
            self._set_progress(self._progress_value, "Batch download failed")
            self.log(f"Batch download failed: {e}")
            messagebox.showerror("Error", f"Batch download failed:\n{e}")


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--self-update-worker", action="store_true")
    parser.add_argument("--remote", default="")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--target-path", default="")
    # Keep accepting the old worker argument so an updater launched from an
    # older Windows build can still finish its in-flight replacement.
    parser.add_argument("--target-executable", default="")
    parser.add_argument("--update-error", default="")
    args, _unknown = parser.parse_known_args()

    if args.self_update_worker:
        target_path = args.target_path or args.target_executable
        if (
            sys.platform not in ("win32", "darwin")
            or not args.remote
            or not target_path
        ):
            return 2
        return run_update_worker(
            args.remote,
            args.branch,
            Path(target_path).resolve(),
        )

    # The downloader workers remain in this module, while the application now
    # uses the native Qt presentation defined in qt_main.py.
    from qt_main import run_qt_app

    return run_qt_app(args.update_error)


if __name__ == "__main__":
    raise SystemExit(main())
