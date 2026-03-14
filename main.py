import os
import re
import sys
import time
import queue
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


APP_TITLE = "Talal's YT Extractor"
APP_GEOMETRY = "980x780"


class DownloadCancelled(Exception):
    pass


def get_ffmpeg_path():
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "ffmpeg")
    return os.path.join(os.path.dirname(__file__), "ffmpeg")


@dataclass
class DownloadOptions:
    url: str
    output_dir: str
    mode: str  # video | audio
    quality: str
    audio_format: str


class UILogger:
    def __init__(self, emit_callback):
        self.emit = emit_callback

    def debug(self, msg):
        self.emit("log", str(msg))

    def warning(self, msg):
        self.emit("log", f"WARNING: {msg}")

    def error(self, msg):
        self.emit("log", f"ERROR: {msg}")


class YTDLPApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry(APP_GEOMETRY)
        self.root.minsize(940, 700)

        self.ui_queue = queue.Queue()
        self.worker_thread = None
        self.stop_event = threading.Event()
        self.last_output_path = None
        self.extractors = []

        self.url_var = tk.StringVar()
        self.output_dir_var = tk.StringVar(value=str(Path.home() / "Downloads"))
        self.mode_var = tk.StringVar(value="video")
        self.quality_var = tk.StringVar(value="Best")
        self.audio_format_var = tk.StringVar(value="mp3")
        self.status_var = tk.StringVar(value="Ready")
        self.percent_var = tk.StringVar(value="0%")
        self.speed_var = tk.StringVar(value="Speed: --")
        self.eta_var = tk.StringVar(value="ETA: --")
        self.title_var = tk.StringVar(value="Title: --")
        self.file_var = tk.StringVar(value="Saved file: --")
        self.progress_var = tk.DoubleVar(value=0.0)

        self.search_var = tk.StringVar()
        self.search_count_var = tk.StringVar(value="Supported sites: loading...")
        self.selected_site_var = tk.StringVar(value="Selected site: --")

        self._setup_style()
        self._build_ui()
        self._set_mode_defaults()
        self._poll_ui_queue()

        self._load_extractors()
        self._refresh_site_search()

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Subtle.TLabel", font=("Segoe UI", 9))
        style.configure("Status.TLabel", font=("Consolas", 10))
        style.configure("Action.TButton", padding=(10, 8))

    def _build_ui(self):
        container = ttk.Frame(self.root, padding=14)
        container.pack(fill="both", expand=True)

        header = ttk.Frame(container)
        header.pack(fill="x", pady=(0, 12))

        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="yt-dlp desktop wrapper with supported-site search, live progress, and printable logs.",
            style="Subtle.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        form = ttk.LabelFrame(container, text="Download Settings", padding=12)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        form.columnconfigure(2, weight=0)
        form.columnconfigure(3, weight=1)

        # Supported site search
        ttk.Label(form, text="Search Supported Sites").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=6
        )

        self.search_entry = ttk.Entry(form, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=1, sticky="ew", pady=6)

        self.search_btn = ttk.Button(form, text="Search", command=self._refresh_site_search)
        self.search_btn.grid(row=0, column=2, sticky="ew", padx=(8, 8), pady=6)

        self.clear_search_btn = ttk.Button(form, text="Clear", command=self._clear_site_search)
        self.clear_search_btn.grid(row=0, column=3, sticky="w", pady=6)

        ttk.Label(form, textvariable=self.search_count_var, style="Subtle.TLabel").grid(
            row=1, column=1, columnspan=3, sticky="w", pady=(0, 4)
        )

        search_results_frame = ttk.Frame(form)
        search_results_frame.grid(row=2, column=1, columnspan=3, sticky="ew", pady=(0, 8))
        search_results_frame.columnconfigure(0, weight=1)

        self.search_results = tk.Listbox(
            search_results_frame,
            height=7,
            font=("Consolas", 10),
            activestyle="dotbox",
            exportselection=False,
        )
        self.search_results.grid(row=0, column=0, sticky="ew")

        self.search_scrollbar = ttk.Scrollbar(
            search_results_frame, orient="vertical", command=self.search_results.yview
        )
        self.search_scrollbar.grid(row=0, column=1, sticky="ns")
        self.search_results.configure(yscrollcommand=self.search_scrollbar.set)

        self.search_results.bind("<<ListboxSelect>>", self._on_site_select)
        self.search_results.bind("<Double-Button-1>", self._on_site_double_click)
        self.search_entry.bind("<KeyRelease>", self._on_search_keyrelease)
        self.search_entry.bind("<Return>", lambda event: self._refresh_site_search())

        ttk.Label(form, textvariable=self.selected_site_var, style="Subtle.TLabel").grid(
            row=3, column=1, columnspan=3, sticky="w", pady=(0, 8)
        )

        # URL
        ttk.Label(form, text="Media URL").grid(row=4, column=0, sticky="w", padx=(0, 10), pady=6)
        self.url_entry = ttk.Entry(form, textvariable=self.url_var)
        self.url_entry.grid(row=4, column=1, columnspan=3, sticky="ew", pady=6)

        # Output folder
        ttk.Label(form, text="Output Folder").grid(row=5, column=0, sticky="w", padx=(0, 10), pady=6)
        self.output_entry = ttk.Entry(form, textvariable=self.output_dir_var)
        self.output_entry.grid(row=5, column=1, columnspan=2, sticky="ew", pady=6)
        self.browse_btn = ttk.Button(form, text="Browse", command=self._choose_output_dir)
        self.browse_btn.grid(row=5, column=3, sticky="ew", pady=6, padx=(8, 0))

        # Mode row
        ttk.Label(form, text="Mode").grid(row=6, column=0, sticky="w", padx=(0, 10), pady=6)
        mode_row = ttk.Frame(form)
        mode_row.grid(row=6, column=1, sticky="w", pady=6)
        self.video_radio = ttk.Radiobutton(
            mode_row, text="Video", value="video", variable=self.mode_var, command=self._set_mode_defaults
        )
        self.audio_radio = ttk.Radiobutton(
            mode_row, text="Audio Only", value="audio", variable=self.mode_var, command=self._set_mode_defaults
        )
        self.video_radio.pack(side="left")
        self.audio_radio.pack(side="left", padx=(14, 0))

        ttk.Label(form, text="Quality").grid(row=6, column=2, sticky="w", padx=(16, 10), pady=6)
        self.quality_combo = ttk.Combobox(form, textvariable=self.quality_var, state="readonly", width=22)
        self.quality_combo.grid(row=6, column=3, sticky="ew", pady=6)

        ttk.Label(form, text="Audio Format").grid(row=7, column=0, sticky="w", padx=(0, 10), pady=6)
        self.audio_format_combo = ttk.Combobox(
            form,
            textvariable=self.audio_format_var,
            state="readonly",
            values=["mp3", "m4a", "wav"],
            width=12,
        )
        self.audio_format_combo.grid(row=7, column=1, sticky="w", pady=6)

        controls = ttk.Frame(container)
        controls.pack(fill="x", pady=12)

        self.download_btn = ttk.Button(
            controls, text="Start Download", style="Action.TButton", command=self.start_download
        )
        self.download_btn.pack(side="left")

        self.cancel_btn = ttk.Button(controls, text="Cancel", command=self.cancel_download, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))

        self.open_btn = ttk.Button(controls, text="Open Folder", command=self.open_output_folder)
        self.open_btn.pack(side="left", padx=(8, 0))

        progress_frame = ttk.LabelFrame(container, text="Status", padding=12)
        progress_frame.pack(fill="x")

        self.progress = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress.pack(fill="x", pady=(0, 8))

        top_status = ttk.Frame(progress_frame)
        top_status.pack(fill="x")
        ttk.Label(top_status, textvariable=self.status_var).pack(side="left")
        ttk.Label(top_status, textvariable=self.percent_var).pack(side="right")

        meta_status = ttk.Frame(progress_frame)
        meta_status.pack(fill="x", pady=(6, 0))
        ttk.Label(meta_status, textvariable=self.speed_var, style="Status.TLabel").pack(side="left")
        ttk.Label(meta_status, textvariable=self.eta_var, style="Status.TLabel").pack(side="left", padx=(16, 0))

        ttk.Label(progress_frame, textvariable=self.title_var, style="Subtle.TLabel").pack(anchor="w", pady=(8, 0))
        ttk.Label(progress_frame, textvariable=self.file_var, style="Subtle.TLabel").pack(anchor="w", pady=(2, 0))

        logs_frame = ttk.LabelFrame(container, text="Live Logs", padding=8)
        logs_frame.pack(fill="both", expand=True, pady=(12, 0))
        logs_frame.rowconfigure(0, weight=1)
        logs_frame.columnconfigure(0, weight=1)

        self.log_text = tk.Text(logs_frame, wrap="word", height=18, font=("Consolas", 10), state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(logs_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        log_btns = ttk.Frame(logs_frame)
        log_btns.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(log_btns, text="Clear Logs", command=self.clear_logs).pack(side="left")
        ttk.Button(log_btns, text="Copy Logs", command=self.copy_logs).pack(side="left", padx=(8, 0))

    def _load_extractors(self):
        if yt_dlp is None:
            self.extractors = []
            self.search_count_var.set("Supported sites: yt-dlp not installed")
            return

        try:
            from yt_dlp.extractor import gen_extractors

            loaded = []
            seen = set()

            for extractor in gen_extractors():
                name = getattr(extractor, "IE_NAME", "") or ""
                desc = getattr(extractor, "IE_DESC", None)
                if not name or name == "generic":
                    continue
                key = (name.lower(), str(desc or name).lower())
                if key in seen:
                    continue
                seen.add(key)
                loaded.append((name, str(desc or name)))

            loaded.sort(key=lambda item: item[0].lower())
            self.extractors = loaded
            self.search_count_var.set(f"Supported sites loaded: {len(self.extractors)}")

        except Exception as exc:
            self.extractors = []
            self.search_count_var.set("Supported sites: failed to load")
            self.append_log(f"Could not load extractor list: {exc}")

    def search_sites(self, query: str):
        query = (query or "").strip().lower()

        if not query:
            return self.extractors[:200]

        results = []
        for name, desc in self.extractors:
            name_l = name.lower()
            desc_l = desc.lower()

            if query in name_l or query in desc_l:
                results.append((name, desc))
                continue

            query_parts = query.split()
            if query_parts and all(part in f"{name_l} {desc_l}" for part in query_parts):
                results.append((name, desc))

        return results[:200]

    def _refresh_site_search(self):
        self.search_results.delete(0, tk.END)

        if yt_dlp is None:
            self.search_results.insert(tk.END, "yt-dlp is not installed")
            self.search_count_var.set("Supported sites: unavailable")
            return

        matches = self.search_sites(self.search_var.get())

        if not matches:
            self.search_results.insert(tk.END, "No supported sites found")
            self.search_count_var.set("Matches: 0")
            self.selected_site_var.set("Selected site: --")
            return

        for name, desc in matches:
            self.search_results.insert(tk.END, f"{name}  |  {desc}")

        self.search_count_var.set(f"Matches: {len(matches)} / {len(self.extractors)}")
        self.search_results.selection_clear(0, tk.END)
        self.search_results.selection_set(0)
        self._on_site_select()

    def _clear_site_search(self):
        self.search_var.set("")
        self._refresh_site_search()

    def _on_search_keyrelease(self, event=None):
        self._refresh_site_search()

    def _on_site_select(self, event=None):
        selection = self.search_results.curselection()
        if not selection:
            self.selected_site_var.set("Selected site: --")
            return

        selected_text = self.search_results.get(selection[0])
        self.selected_site_var.set(f"Selected site: {selected_text}")

    def _on_site_double_click(self, event=None):
        selection = self.search_results.curselection()
        if not selection:
            return

        selected_text = self.search_results.get(selection[0])
        self.append_log(f"Selected supported site: {selected_text}")

    def detect_site(self, url):
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(url, download=False)
                return info.get("extractor_key")
        except Exception:
            return None

    def _set_mode_defaults(self):
        if self.mode_var.get() == "video":
            qualities = ["Best", "1080p", "720p", "480p"]
            self.quality_combo.configure(values=qualities)
            if self.quality_var.get() not in qualities:
                self.quality_var.set("Best")
            self.audio_format_combo.configure(state="disabled")
        else:
            qualities = ["Best Audio"]
            self.quality_combo.configure(values=qualities)
            self.quality_var.set("Best Audio")
            self.audio_format_combo.configure(state="readonly")

    def _choose_output_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.output_dir_var.get() or str(Path.home()))
        if chosen:
            self.output_dir_var.set(chosen)

    def _emit(self, event_type, payload=None):
        self.ui_queue.put((event_type, payload))

    def _poll_ui_queue(self):
        try:
            while True:
                event_type, payload = self.ui_queue.get_nowait()
                self._handle_ui_event(event_type, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_ui_queue)

    def _handle_ui_event(self, event_type, payload):
        if event_type == "log":
            self.append_log(payload)
        elif event_type == "status":
            self.status_var.set(payload)
        elif event_type == "title":
            self.title_var.set(f"Title: {payload}")
        elif event_type == "file":
            self.file_var.set(f"Saved file: {payload}")
            self.last_output_path = payload
        elif event_type == "progress":
            self.progress_var.set(payload.get("percent", 0.0))
            self.percent_var.set(payload.get("percent_text", "0%"))
            self.speed_var.set(f"Speed: {payload.get('speed', '--')}")
            self.eta_var.set(f"ETA: {payload.get('eta', '--')}")
        elif event_type == "controls":
            self._set_controls_running(payload == "running")
        elif event_type == "done":
            self._set_controls_running(False)
            self.status_var.set("Completed")
            messagebox.showinfo(APP_TITLE, "Download completed successfully.")
        elif event_type == "cancelled":
            self._set_controls_running(False)
            self.status_var.set("Cancelled")
            self.append_log("Download cancelled by user.")
            messagebox.showwarning(APP_TITLE, "Download cancelled.")
        elif event_type == "error":
            self._set_controls_running(False)
            self.status_var.set("Error")
            self.append_log(f"ERROR: {payload}")
            messagebox.showerror(APP_TITLE, payload)

    def _set_controls_running(self, running: bool):
        state_normal = "disabled" if running else "normal"

        self.search_entry.configure(state=state_normal)
        self.search_btn.configure(state=state_normal)
        self.clear_search_btn.configure(state=state_normal)

        self.url_entry.configure(state=state_normal)
        self.output_entry.configure(state=state_normal)
        self.browse_btn.configure(state=state_normal)
        self.video_radio.configure(state=state_normal)
        self.audio_radio.configure(state=state_normal)

        self.quality_combo.configure(state="disabled" if running else "readonly")
        if self.mode_var.get() == "audio":
            self.audio_format_combo.configure(state="disabled" if running else "readonly")
        else:
            self.audio_format_combo.configure(state="disabled")

        self.download_btn.configure(state="disabled" if running else "normal")
        self.cancel_btn.configure(state="normal" if running else "disabled")

    def append_log(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_logs(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def copy_logs(self):
        content = self.log_text.get("1.0", "end").strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.append_log("Logs copied to clipboard.")

    def open_output_folder(self):
        path = self.output_dir_var.get().strip()
        if not path:
            return
        if not os.path.isdir(path):
            messagebox.showwarning(APP_TITLE, "Output folder does not exist.")
            return

        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not open folder.\n\n{exc}")

    def validate_form(self):
        if yt_dlp is None:
            raise RuntimeError("yt-dlp is not installed. Run: pip install yt-dlp")

        url = self.url_var.get().strip()
        output_dir = self.output_dir_var.get().strip()

        if not url:
            raise ValueError("Please enter a URL.")
        if not re.match(r"^https?://", url, re.IGNORECASE):
            raise ValueError("Please enter a valid URL starting with http:// or https://")
        if not output_dir:
            raise ValueError("Please choose an output folder.")

        os.makedirs(output_dir, exist_ok=True)

        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(url, download=False)
                extractor = info.get("extractor_key", "Unknown")
                self.append_log(f"Detected site: {extractor}")
        except Exception as e:
            raise ValueError("This URL may not be supported by yt-dlp.") from e

        return DownloadOptions(
            url=url,
            output_dir=output_dir,
            mode=self.mode_var.get(),
            quality=self.quality_var.get(),
            audio_format=self.audio_format_var.get(),
        )

    def start_download(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning(APP_TITLE, "A download is already running.")
            return

        try:
            opts = self.validate_form()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        self.stop_event.clear()
        self.progress_var.set(0)
        self.percent_var.set("0%")
        self.speed_var.set("Speed: --")
        self.eta_var.set("ETA: --")
        self.file_var.set("Saved file: --")
        self.status_var.set("Starting...")

        self.append_log("Starting new job.")
        self.append_log(f"URL: {opts.url}")
        self.append_log(f"Mode: {opts.mode} | Quality: {opts.quality}")
        self.append_log(f"Output folder: {opts.output_dir}")
        self._emit("controls", "running")

        self.worker_thread = threading.Thread(target=self._download_worker, args=(opts,), daemon=True)
        self.worker_thread.start()

    def cancel_download(self):
        if self.worker_thread and self.worker_thread.is_alive():
            self.stop_event.set()
            self.append_log("Cancellation requested...")
            self.status_var.set("Cancelling...")

    def _build_ydl_options(self, opts: DownloadOptions):
        outtmpl = os.path.join(opts.output_dir, "%(title).200s [%(id)s].%(ext)s")

        common = {
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "logger": UILogger(self._emit),
            "progress_hooks": [self._progress_hook],
            "restrictfilenames": False,
            "windowsfilenames": True,
            "concurrent_fragment_downloads": 4,
            "retries": 10,
            "fragment_retries": 10,
            "ignoreerrors": False,
            "merge_output_format": "mp4",
            "ffmpeg_location": get_ffmpeg_path(),
        }

        if opts.mode == "video":
            if opts.quality == "1080p":
                fmt = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
            elif opts.quality == "720p":
                fmt = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
            elif opts.quality == "480p":
                fmt = "bestvideo[height<=480]+bestaudio/best[height<=480]/best"
            else:
                fmt = "bestvideo+bestaudio/best"

            common.update(
                {
                    "format": fmt,
                    "postprocessors": [
                        {
                            "key": "FFmpegMetadata",
                            "add_metadata": True,
                        }
                    ],
                }
            )
        else:
            common.update(
                {
                    "format": "bestaudio/best",
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": opts.audio_format,
                            "preferredquality": "0",
                        },
                        {
                            "key": "FFmpegMetadata",
                            "add_metadata": True,
                        },
                    ],
                }
            )

        return common

    def _progress_hook(self, d):
        if self.stop_event.is_set():
            raise DownloadCancelled("Download cancelled by user.")

        status = d.get("status")

        if status == "downloading":
            percent = self._safe_percent(d)
            speed = self._format_speed(d.get("speed"))
            eta = self._format_eta(d.get("eta"))
            filename = os.path.basename(d.get("filename", ""))

            self._emit(
                "progress",
                {
                    "percent": percent,
                    "percent_text": f"{percent:.1f}%",
                    "speed": speed,
                    "eta": eta,
                },
            )
            self._emit("status", f"Downloading {filename or 'media'}")

        elif status == "finished":
            filename = os.path.basename(d.get("filename", ""))
            self._emit(
                "progress",
                {"percent": 100.0, "percent_text": "100.0%", "speed": "--", "eta": "00:00"},
            )
            self._emit("log", f"Download phase finished for {filename}. Processing with ffmpeg if needed...")
            self._emit("status", "Post-processing...")

    def _download_worker(self, opts: DownloadOptions):
        try:
            ydl_opts = self._build_ydl_options(opts)
            self._emit("log", "Fetching metadata...")
            self._emit("status", "Fetching info...")

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(opts.url, download=False)
                if self.stop_event.is_set():
                    raise DownloadCancelled("Download cancelled before start.")

                title = info.get("title", "Unknown title")
                uploader = info.get("uploader", "Unknown uploader")
                duration = self._format_eta(info.get("duration")) if info.get("duration") else "--"
                extractor = info.get("extractor_key", "Unknown")

                self._emit("title", title)
                self._emit("log", f"Extractor: {extractor}")
                self._emit("log", f"Title: {title}")
                self._emit("log", f"Uploader: {uploader}")
                self._emit("log", f"Duration: {duration}")
                self._emit("log", "Starting media download...")
                self._emit("status", "Downloading...")

                ydl.download([opts.url])

                final_path = self._predict_final_path(info, opts)
                self._emit("file", final_path)
                self._emit("log", f"Saved to: {final_path}")
                self._emit("done")

        except DownloadCancelled:
            self._emit("cancelled")
        except Exception as exc:
            tb = traceback.format_exc()
            self._emit("log", tb)
            self._emit("error", str(exc))

    def _predict_final_path(self, info, opts: DownloadOptions):
        title = info.get("title", "video")
        video_id = info.get("id", "unknown")
        safe_title = self._safe_filename(title)
        ext = opts.audio_format if opts.mode == "audio" else "mp4"
        return os.path.join(opts.output_dir, f"{safe_title} [{video_id}].{ext}")

    @staticmethod
    def _safe_filename(name: str):
        name = re.sub(r'[<>:"/\\|?*]', "_", name)
        name = re.sub(r"\s+", " ", name).strip()
        return name[:200] or "video"

    @staticmethod
    def _safe_percent(d):
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes", 0)
        if total and total > 0:
            return min(100.0, (downloaded / total) * 100)
        percent_str = d.get("_percent_str", "").strip().replace("%", "")
        try:
            return float(percent_str)
        except Exception:
            return 0.0

    @staticmethod
    def _format_speed(speed):
        if not speed:
            return "--"
        units = ["B/s", "KiB/s", "MiB/s", "GiB/s"]
        idx = 0
        speed = float(speed)
        while speed >= 1024 and idx < len(units) - 1:
            speed /= 1024
            idx += 1
        return f"{speed:.2f} {units[idx]}"

    @staticmethod
    def _format_eta(seconds):
        if seconds is None:
            return "--"
        try:
            seconds = int(seconds)
        except Exception:
            return "--"
        minutes, sec = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{sec:02d}"
        return f"{minutes:02d}:{sec:02d}"


def main():
    root = tk.Tk()
    app = YTDLPApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()