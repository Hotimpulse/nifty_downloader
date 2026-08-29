import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
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


SUPPORTED_PLATFORM_NAMES = "YouTube, X, Instagram, Facebook, and TikTok"
UPDATE_LOG_NAME = "yt_downloader_update.log"


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


def run_update_worker(
    remote: str,
    branch: str,
    target_executable: Path,
) -> int:
    """Clone, rebuild, replace, and relaunch the application."""
    log_path = target_executable.parent / UPDATE_LOG_NAME
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

        built_executable = source_root / "dist" / "yt_downloader.exe"
        if not built_executable.is_file():
            raise RuntimeError("The rebuilt executable was not produced.")

        target_executable.parent.mkdir(parents=True, exist_ok=True)
        last_error = None
        for _attempt in range(20):
            try:
                shutil.copy2(built_executable, target_executable)
                last_error = None
                break
            except PermissionError as error:
                last_error = error
                time.sleep(0.5)
        if last_error:
            raise last_error

        subprocess.Popen([str(target_executable)], cwd=target_executable.parent)
        return 0
    except Exception as error:
        try:
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(f"\nUPDATE FAILED: {error}\n")
        except OSError:
            pass
        if target_executable.is_file():
            subprocess.Popen(
                [str(target_executable), "--update-error", str(log_path)],
                cwd=target_executable.parent,
            )
        return 1


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


class VideoDownloaderGUI:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("Video Downloader")
        self.root.geometry("980x760")
        self.root.minsize(900, 700)

        self.url_var = StringVar()
        self.start_time_var = StringVar()
        self.end_time_var = StringVar()
        self.batch_url_var = StringVar()
        self.batch_count_var = IntVar(value=10)
        self.progress_var = DoubleVar(value=0.0)
        self.progress_text_var = StringVar(value="Ready")
        self._progress_value = 0.0
        self._last_progress_update = 0.0
        self._is_checking_qualities = False
        self._update_remote = ""
        self._update_branch = "main"

        self.video_formats = []
        self.audio_formats = []
        self.video_id_to_format = {}
        self.audio_id_to_format = {}

        self._build_ui()
        if sys.platform == "win32":
            self._run_in_thread(self._check_for_update_worker)

    def _build_ui(self):
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

        ttk.Label(single_card, text="Video URL (YouTube, X, Instagram, Facebook, or TikTok):").grid(row=0, column=0, sticky="w")
        self.url_entry = ttk.Entry(single_card, textvariable=self.url_var, width=95)
        self.url_entry.grid(row=1, column=0, columnspan=2, sticky="we", pady=(4, 8))
        self.url_entry.bind("<Return>", self._check_url_from_entry)
        # Schedule after Tk has applied the standard paste operation, whether it
        # came from Ctrl+V, Shift+Insert, or the entry's context menu.
        self.url_entry.bind("<<Paste>>", self._check_url_after_paste, add=True)
        ttk.Button(
            single_card,
            text="Paste & Check",
            command=self.paste_and_check_url,
        ).grid(row=1, column=2, sticky="e", padx=(8, 0), pady=(4, 8))

        clip_frame = ttk.Frame(single_card)
        clip_frame.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(clip_frame, text="Optional extract — Start:").pack(side="left")
        ttk.Entry(clip_frame, textvariable=self.start_time_var, width=10).pack(
            side="left", padx=(5, 10)
        )
        ttk.Label(clip_frame, text="End:").pack(side="left")
        ttk.Entry(clip_frame, textvariable=self.end_time_var, width=10).pack(
            side="left", padx=(5, 8)
        )
        ttk.Label(clip_frame, text="seconds or HH:MM:SS").pack(side="left")

        self.check_qualities_button = ttk.Button(
            single_card,
            text="Check Qualities",
            command=self.check_qualities,
        )
        self.check_qualities_button.grid(row=3, column=0, sticky="w")
        ttk.Button(single_card, text="DOWNLOAD", command=self.download_selected).grid(row=3, column=1, sticky="w", padx=(8, 0))

        lists_frame = ttk.Frame(single_card)
        lists_frame.grid(row=4, column=0, columnspan=3, sticky="we", pady=(10, 0))

        video_frame = ttk.LabelFrame(lists_frame, text="Video Quality", padding=8)
        video_frame.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self.video_list = ttk.Treeview(video_frame, columns=("label",), show="headings", height=8)
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
        self.audio_list = ttk.Treeview(audio_frame, columns=("label",), show="headings", height=8)
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
        self.log_box = ttk.Treeview(logs_card, columns=("log",), show="headings", height=12)
        self.log_box.heading("log", text="Status")
        self.log_box.column("log", width=920)
        self.log_box.pack(fill="both", expand=True)
        self.log_box.bind("<ButtonRelease-1>", self.copy_selected_log)

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

    def _check_for_update_worker(self):
        """Check GitHub without blocking the Tk event loop."""
        try:
            local_commit, remote, branch = current_update_identity()
            if not remote:
                return
            remote_commit = remote_head_commit(remote, branch)
            if not update_is_available(local_commit, remote_commit):
                return

            self._update_remote = remote
            self._update_branch = branch
            self.root.after(0, self._show_update_button)
        except Exception as error:
            self.log(f"GitHub update check unavailable: {self._clean_error(error)}")

    def _show_update_button(self):
        if not self.update_button.winfo_ismapped():
            self.update_button.pack(side="right", padx=(0, 8))
        self.log("A different source revision is available on GitHub.")

    def download_update_and_rebuild(self):
        """Hand the rebuild to another process, then close this app."""
        if not self._update_remote:
            return

        if getattr(sys, "frozen", False):
            target_executable = Path(sys.executable).resolve()
            updater_dir = Path(tempfile.mkdtemp(prefix="yt_downloader_updater_"))
            updater_executable = updater_dir / "yt_downloader_updater.exe"
            try:
                shutil.copy2(target_executable, updater_executable)
            except OSError as error:
                messagebox.showerror("Update Failed", self._clean_error(error))
                return
            command = [str(updater_executable)]
        else:
            source_root = find_source_root()
            if not source_root:
                messagebox.showerror("Update Failed", "The project source was not found.")
                return
            target_executable = source_root / "dist" / "yt_downloader.exe"
            command = [sys.executable, str(Path(__file__).resolve())]

        command.extend([
            "--self-update-worker",
            "--remote",
            self._update_remote,
            "--branch",
            self._update_branch,
            "--target-executable",
            str(target_executable),
        ])

        self.update_button.state(["disabled"])
        self.update_button.configure(text="STARTING UPDATER...")
        try:
            subprocess.Popen(command, cwd=target_executable.parent)
        except OSError as error:
            self.update_button.state(["!disabled"])
            self.update_button.configure(text="DOWNLOAD UPDATE & REBUILD")
            messagebox.showerror("Update Failed", self._clean_error(error))
            return
        self.root.after(100, self.root.destroy)

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
                "Copy a video URL first, then click Paste & Check.",
            )
            return

        if not url:
            messagebox.showwarning(
                "Empty Clipboard",
                "Copy a video URL first, then click Paste & Check.",
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

        self._is_checking_qualities = True
        self.check_qualities_button.state(["disabled"])
        self._set_progress(0, "Checking available formats...")
        self._run_in_thread(lambda: self._check_qualities_worker(url))

    def _check_qualities_worker(self, url: str):
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
                ),
            )

        except Exception as e:
            cleaned = self._clean_error(e)
            self.log(f"Error fetching formats: {cleaned}")
            self.root.after(
                0,
                lambda: messagebox.showerror(
                    "Error",
                    f"Failed to fetch qualities:\n{cleaned}",
                ),
            )
        finally:
            self.root.after(0, self._finish_checking_qualities)

    def _display_formats(self, video_formats: list[dict], audio_formats: list[dict], title: str):
        """Render formats on Tk's main thread after a background probe."""
        self.video_list.delete(*self.video_list.get_children())
        self.audio_list.delete(*self.audio_list.get_children())
        self.video_id_to_format.clear()
        self.audio_id_to_format.clear()

        for vf in video_formats:
            height = self._numeric_value(vf.get("height"))
            resolution = f"{int(height)}p" if height > 0 else "resolution unavailable"
            fps_value = self._numeric_value(vf.get("fps"))
            fps = f" {fps_value:g}fps" if fps_value > 0 else ""
            label = (
                f"id={vf.get('format_id')} | {vf.get('ext')} | "
                f"{resolution}{fps} | "
                f"~{int(self._numeric_value(vf.get('tbr')))}kbps"
            )
            iid = self.video_list.insert("", END, values=(label,))
            self.video_id_to_format[iid] = vf

        for af in audio_formats:
            label = (
                f"id={af.get('format_id')} | {af.get('ext')} | "
                f"{af.get('acodec')} | {int(self._numeric_value(af.get('abr')))}kbps"
            )
            iid = self.audio_list.insert("", END, values=(label,))
            self.audio_id_to_format[iid] = af

        # Formats are sorted best-first before they reach this method. Select
        # the top video and audio entries so a pasted link is ready to download
        # without requiring extra clicks.
        video_items = self.video_list.get_children()
        if video_items:
            self.video_list.selection_set(video_items[0])
            self.video_list.focus(video_items[0])

        audio_items = self.audio_list.get_children()
        if audio_items:
            self.audio_list.selection_set(audio_items[0])
            self.audio_list.focus(audio_items[0])

        self._set_progress(0, "Select a quality, then download")
        self.log(f"Loaded formats for: {title}")

    def _finish_checking_qualities(self):
        self._is_checking_qualities = False
        self.check_qualities_button.state(["!disabled"])

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

            if (clip_range and not audio_only) or (video_only and video_has_audio):
                filepath = self._find_downloaded_filepath(
                    info or {},
                    prepared_filepath,
                    ydl_opts.get("merge_output_format"),
                )
                self._process_local_video(
                    filepath,
                    clip_range,
                    remove_audio=video_only and video_has_audio,
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
    parser.add_argument("--target-executable", default="")
    parser.add_argument("--update-error", default="")
    args, _unknown = parser.parse_known_args()

    if args.self_update_worker:
        if sys.platform != "win32" or not args.remote or not args.target_executable:
            return 2
        return run_update_worker(
            args.remote,
            args.branch,
            Path(args.target_executable).resolve(),
        )

    root = Tk()
    app = VideoDownloaderGUI(root)
    if args.update_error:
        error_log = Path(args.update_error)
        root.after(
            200,
            lambda: messagebox.showerror(
                "Update Failed",
                f"The previous version was reopened. Details are in:\n{error_log}",
            ),
        )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
