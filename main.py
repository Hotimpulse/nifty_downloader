import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from tkinter import Tk, StringVar, IntVar, DoubleVar, END
from tkinter import ttk, messagebox

import yt_dlp
from yt_dlp.utils import download_range_func


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


SUPPORTED_PLATFORM_NAMES = "YouTube, Instagram, Facebook, and TikTok"


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

        self.video_formats = []
        self.audio_formats = []
        self.video_id_to_format = {}
        self.audio_id_to_format = {}

        self._build_ui()

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

        ttk.Label(single_card, text="Video URL (YouTube, Instagram, Facebook, or TikTok):").grid(row=0, column=0, sticky="w")
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

        audio_frame = ttk.LabelFrame(lists_frame, text="Audio Quality", padding=8)
        audio_frame.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.audio_list = ttk.Treeview(audio_frame, columns=("label",), show="headings", height=8)
        self.audio_list.heading("label", text="Format")
        self.audio_list.column("label", width=420)
        self.audio_list.pack(fill="both", expand=True)

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

            self.video_formats.sort(key=lambda x: (x.get("height", 0), x.get("fps", 0), x.get("tbr", 0)), reverse=True)
            self.audio_formats.sort(key=lambda x: (x.get("abr", 0), x.get("asr", 0)), reverse=True)

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
            resolution = (
                f"{vf['height']}p"
                if vf.get("height")
                else "resolution unavailable"
            )
            fps = f" {vf['fps']}fps" if vf.get("fps") else ""
            label = (
                f"id={vf.get('format_id')} | {vf.get('ext')} | "
                f"{resolution}{fps} | "
                f"~{int(vf.get('tbr', 0))}kbps"
            )
            iid = self.video_list.insert("", END, values=(label,))
            self.video_id_to_format[iid] = vf

        for af in audio_formats:
            label = (
                f"id={af.get('format_id')} | {af.get('ext')} | "
                f"{af.get('acodec')} | {int(af.get('abr', 0))}kbps"
            )
            iid = self.audio_list.insert("", END, values=(label,))
            self.audio_id_to_format[iid] = af

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
            video_has_audio = video_format.get("acodec") not in (None, "none")

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
            audio_only = video_id is None

            if audio_only:
                fmt = audio_id
            elif audio_id:
                fmt = f"{video_id}+{audio_id}"
            elif video_has_audio:
                # A combined social-media stream already has its own audio.
                fmt = video_id
            else:
                # Most high-quality video streams are video-only. When the
                # user chooses one without an explicit audio selection, add
                # the platform's best available audio rather than silently
                # producing a mute download.
                fmt = f"{video_id}+bestaudio/best"

            download_kind = "MP3 audio" if audio_only else "selected format"
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

            if clip_range and not audio_only:
                ydl_opts["download_ranges"] = download_range_func(None, [clip_range])
                ydl_opts["force_keyframes_at_cuts"] = True
                # Prefer direct HTTP formats; FFmpeg can produce empty section files
                # when seeking in some HLS streams.
                ydl_opts["format_sort"] = ["proto:https"]

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
            else:
                ydl_opts["merge_output_format"] = "mp4"

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            if clip_range:
                completion_text = "Clip extract complete"
            else:
                completion_text = "MP3 download complete" if audio_only else "Download complete"
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
    root = Tk()
    app = VideoDownloaderGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
