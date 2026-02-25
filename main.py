"""
Duplicate File Finder — Version 2.01
=====================================
Cross-platform (Linux / Windows) GUI application built with CustomTkinter.

New in v2.0
-----------
  1. Send to Trash     — uses send2trash instead of permanent deletion
  2. Smart Selection   — 'Select Older' and 'Select Newer' buttons
  3. Hash Sampling     — 3-pass hashing (size → 10 KB sample → full MD5)
                         for dramatically faster scans on large folders
  4. Progress Bar      — live 'File X of Y' progress during scanning
  5. Image Preview     — per-file Preview button opens system viewer
  6. Hebrew BiDi       — arabic-reshaper + python-bidi pipeline preserved

Requirements
------------
    pip install customtkinter send2trash python-bidi arabic-reshaper
"""

import hashlib
import os
import platform
import re
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import customtkinter as ctk
from tkinter import filedialog, messagebox

# ── Safety: send2trash (Recycle Bin / Trash) ──────────────────────────────────
# NOTE: run  pip install send2trash  before using this app.
try:
    from send2trash import send2trash
    TRASH_AVAILABLE = True
except ImportError:
    TRASH_AVAILABLE = False

# ── Hebrew / RTL BiDi support ─────────────────────────────────────────────────
# pip install python-bidi arabic-reshaper
try:
    from bidi.algorithm import get_display as bidi_get_display
    import arabic_reshaper
    BIDI_AVAILABLE = True
except ImportError:
    BIDI_AVAILABLE = False


# ──────────────────────────────────────────────────────────────────────────────
# Appearance
# ──────────────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

# ── Design tokens ─────────────────────────────────────────────────────────────
ROW_COLORS   = ("#2b2b2b", "#1e1e1e")   # alternating dark row backgrounds
TEXT_PRIMARY  = "#DCE4EE"               # bright white-blue — filenames
TEXT_SECONDARY = "#9BAAB8"              # muted — paths, dates
TEXT_ACCENT   = "#5dade2"              # blue — file sizes
TEXT_WARN     = "#e8a838"              # amber — oldest copy tag
COL_WEIGHTS   = [0, 4, 5, 2, 3, 1]    # checkbox, name, path, size, date, preview


# ──────────────────────────────────────────────────────────────────────────────
# Helper utilities
# ──────────────────────────────────────────────────────────────────────────────

def format_size(size_bytes: int) -> str:
    """Human-readable file size."""
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.2f} MB"
    elif size_bytes >= 1_024:
        return f"{size_bytes / 1_024:.2f} KB"
    return f"{size_bytes} B"


def format_date(timestamp: float) -> str:
    """Unix mtime → 'YYYY-MM-DD HH:MM'."""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d  %H:%M")


def fix_bidi(text: str) -> str:
    """
    Prepare RTL / mixed-direction text for Tkinter's LTR-only label renderer.

    Pipeline:
      1. arabic_reshaper.reshape() — contextual letter shaping
      2. bidi_get_display()        — visual reordering (BiDi algorithm)

    Degrades gracefully if libraries are missing.
    """
    if not BIDI_AVAILABLE:
        return text
    try:
        return bidi_get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


def has_rtl_chars(text: str) -> bool:
    """True if the string contains any Hebrew Unicode characters."""
    return any("\u0590" <= ch <= "\u05FF" for ch in text)


def open_with_system_viewer(file_path: Path) -> None:
    """
    Open a file in the OS default application (image viewer, PDF reader, etc.).
    Works on Linux, macOS, and Windows without extra dependencies.
    """
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(str(file_path))            # type: ignore[attr-defined]
        elif system == "Darwin":                     # macOS
            subprocess.Popen(["open", str(file_path)])
        else:                                        # Linux / BSD
            subprocess.Popen(["xdg-open", str(file_path)])
    except Exception as exc:
        messagebox.showerror("Preview Error", f"Could not open file:\n{exc}")


# ──────────────────────────────────────────────────────────────────────────────
# v2.01 — Three-pass hashing (size → 10 KB sample → full MD5)
#         with filters, excludes, and pause/cancel
# ──────────────────────────────────────────────────────────────────────────────

SAMPLE_SIZE = 10 * 1_024    # 10 KB


def _read_sample(file_path: Path) -> bytes | None:
    """Read the first SAMPLE_SIZE bytes of a file. Returns None on error."""
    try:
        with file_path.open("rb") as f:
            return f.read(SAMPLE_SIZE)
    except (PermissionError, OSError):
        return None


def _full_md5(file_path: Path, chunk_size: int = 65_536) -> str | None:
    """Full MD5 hash of a file, read in chunks. Returns None on error."""
    hasher = hashlib.md5()
    try:
        with file_path.open("rb") as f:
            while chunk := f.read(chunk_size):
                hasher.update(chunk)
        return hasher.hexdigest()
    except (PermissionError, OSError):
        return None


class ScanCancelled(Exception):
    """Raised when the user cancels an active scan."""


def _normalize_extensions(raw: str) -> set[str]:
    """Parse a comma/space-separated list into normalized .ext entries."""
    parts = [p.strip().lower() for p in raw.replace(";", ",").split(",")]
    exts = set()
    for p in parts:
        if not p:
            continue
        if not p.startswith("."):
            p = f".{p}"
        exts.add(p)
    return exts


def _parse_size(raw: str) -> int | None:
    """Parse a size like '10 MB', '500kb', or '1.5g' into bytes."""
    raw = raw.strip().lower()
    if not raw:
        return None

    match = re.match(r"^([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)$", raw)
    if not match:
        return None

    value = float(match.group(1))
    unit = match.group(2) or "kb"
    if value < 0:
        return None

    units = {
        "b": 1,
        "byte": 1,
        "bytes": 1,
        "k": 1024,
        "kb": 1024,
        "kib": 1024,
        "m": 1024 ** 2,
        "mb": 1024 ** 2,
        "mib": 1024 ** 2,
        "g": 1024 ** 3,
        "gb": 1024 ** 3,
        "gib": 1024 ** 3,
    }

    if unit not in units:
        return None

    return int(value * units[unit])


def _split_excludes(raw: str) -> list[str]:
    """Split excludes on commas/semicolons; keep non-empty, lowercase tokens."""
    parts = [p.strip().lower() for p in raw.replace(";", ",").split(",")]
    return [p for p in parts if p]


def _is_excluded(path: Path, excludes: list[str]) -> bool:
    """True if any exclude token matches a folder in the path or substring."""
    if not excludes:
        return False
    lowered_parts = [p.lower() for p in path.parts]
    lowered_path = str(path).lower()
    for token in excludes:
        if "/" in token or "\\" in token:
            if token in lowered_path:
                return True
        else:
            if token in lowered_parts:
                return True
    return False


def _wait_if_paused(pause_event: threading.Event, cancel_event: threading.Event) -> None:
    """Block while paused, but allow cancellation to break out."""
    while pause_event.is_set():
        if cancel_event.is_set():
            raise ScanCancelled()
        time.sleep(0.2)


def find_duplicates(
    directory: Path,
    progress_callback=None,
    *,
    extensions: set[str] | None = None,
    min_size: int | None = None,
    max_size: int | None = None,
    excludes: list[str] | None = None,
    pause_event: threading.Event | None = None,
    cancel_event: threading.Event | None = None,
) -> list[list[Path]]:
    """
    Three-pass duplicate detection optimised for large folders.

    Pass 1 — Group by exact byte size.
              Files with a unique size cannot be duplicates → discarded.

    Pass 2 — Group by 10 KB sample hash.
              Most near-misses (same size, different content) are ruled out
              cheaply without reading the whole file.

    Pass 3 — Full MD5 hash only for files that survived pass 2.
              Guarantees byte-for-byte identity.

    Args:
        directory:         Root path to scan recursively.
        progress_callback: Optional callable(current, total, status_str).

    Returns:
        List of duplicate groups; each group is ≥2 identical Path objects.
    """

    # ── Pass 1: group by size ─────────────────────────────────────────────────
    size_map: dict[int, list[Path]] = defaultdict(list)
    excludes = excludes or []
    pause_event = pause_event or threading.Event()
    cancel_event = cancel_event or threading.Event()

    for file_path in directory.rglob("*"):
        _wait_if_paused(pause_event, cancel_event)
        if cancel_event.is_set():
            raise ScanCancelled()
        if not file_path.is_file():
            continue
        if _is_excluded(file_path, excludes):
            continue
        if extensions and file_path.suffix.lower() not in extensions:
            continue
        try:
            size = file_path.stat().st_size
        except (PermissionError, OSError):
            continue
        if min_size is not None and size < min_size:
            continue
        if max_size is not None and size > max_size:
            continue
        size_map[size].append(file_path)

    candidates = [paths for paths in size_map.values() if len(paths) > 1]

    # ── Pass 2: group by 10 KB sample hash ───────────────────────────────────
    sample_candidates: list[list[Path]] = []
    total_pass2 = sum(len(g) for g in candidates)
    processed = 0

    for size_group in candidates:
        sample_map: dict[bytes, list[Path]] = defaultdict(list)
        for file_path in size_group:
            _wait_if_paused(pause_event, cancel_event)
            if cancel_event.is_set():
                raise ScanCancelled()
            processed += 1
            if progress_callback:
                progress_callback(
                    processed, total_pass2,
                    f"Sampling ({processed}/{total_pass2}): {file_path.name}"
                )
            sample = _read_sample(file_path)
            if sample is not None:
                sample_map[sample].append(file_path)

        for paths in sample_map.values():
            if len(paths) > 1:
                sample_candidates.append(paths)

    # ── Pass 3: full MD5 on sample survivors only ─────────────────────────────
    duplicate_groups: list[list[Path]] = []
    total_pass3 = sum(len(g) for g in sample_candidates)
    processed = 0

    for sample_group in sample_candidates:
        hash_map: dict[str, list[Path]] = defaultdict(list)
        for file_path in sample_group:
            _wait_if_paused(pause_event, cancel_event)
            if cancel_event.is_set():
                raise ScanCancelled()
            processed += 1
            if progress_callback:
                progress_callback(
                    processed, total_pass3,
                    f"Verifying ({processed}/{total_pass3}): {file_path.name}"
                )
            digest = _full_md5(file_path)
            if digest is not None:
                hash_map[digest].append(file_path)

        for paths in hash_map.values():
            if len(paths) > 1:
                duplicate_groups.append(paths)

    return duplicate_groups


# ──────────────────────────────────────────────────────────────────────────────
# Main Application Window
# ──────────────────────────────────────────────────────────────────────────────

class DuplicateFinderApp(ctk.CTk):
    """Root window — Duplicate File Finder v2.0."""

    def __init__(self):
        super().__init__()

        self.title("Duplicate File Finder  v2.01")
        self.geometry("1100x720")
        self.minsize(860, 540)

        # ── Internal state ────────────────────────────────────────────────────
        self._selected_directory: Path | None = None
        self._duplicate_groups:   list[list[Path]] = []
        # checkbox widget → (Path, mtime) for smart-selection logic
        self._checkbox_map: dict[ctk.CTkCheckBox, tuple[Path, float]] = {}
        self._pause_event = threading.Event()
        self._cancel_event = threading.Event()
        self._scan_thread: threading.Thread | None = None

        self._build_ui()

    # ── UI Construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        """Create and lay out all top-level widgets."""

        # ── Directory selector bar ────────────────────────────────────────────
        top_frame = ctk.CTkFrame(self, corner_radius=0)
        top_frame.pack(fill="x", padx=10, pady=(10, 0))

        self.dir_label = ctk.CTkLabel(
            top_frame, text="No directory selected", anchor="w", wraplength=700,
        )
        self.dir_label.pack(side="left", padx=10, pady=10, fill="x", expand=True)

        ctk.CTkButton(
            top_frame, text="📂  Choose Directory",
            width=170, command=self._choose_directory,
        ).pack(side="right", padx=10, pady=10)

        # ── Action bar — primary actions ──────────────────────────────────────
        action1 = ctk.CTkFrame(self, fg_color="transparent")
        action1.pack(fill="x", padx=10, pady=(6, 0))

        self.scan_btn = ctk.CTkButton(
            action1, text="🔍  Scan for Duplicates",
            command=self._start_scan, state="disabled",
        )
        self.scan_btn.pack(side="left", padx=(0, 8))

        self.pause_btn = ctk.CTkButton(
            action1, text="⏸  Pause",
            command=self._toggle_pause, state="disabled",
            fg_color="#6c7a89", hover_color="#5b6b7a", width=110,
        )
        self.pause_btn.pack(side="left", padx=(0, 8))

        self.cancel_btn = ctk.CTkButton(
            action1, text="⛔  Cancel",
            command=self._cancel_scan, state="disabled",
            fg_color="#7f8c8d", hover_color="#6b7b7c", width=110,
        )
        self.cancel_btn.pack(side="left", padx=(0, 8))

        self.select_all_btn = ctk.CTkButton(
            action1, text="☑  Select All",
            command=self._select_all, state="disabled",
            fg_color="gray40", hover_color="gray30",
        )
        self.select_all_btn.pack(side="left", padx=(0, 8))

        # Smart selection buttons — new in v2.0
        self.select_older_btn = ctk.CTkButton(
            action1, text="🕐  Select Older",
            command=self._select_older, state="disabled",
            fg_color="#5d6d7e", hover_color="#4a5568", width=130,
        )
        self.select_older_btn.pack(side="left", padx=(0, 8))

        self.select_newer_btn = ctk.CTkButton(
            action1, text="🕒  Select Newer",
            command=self._select_newer, state="disabled",
            fg_color="#5d6d7e", hover_color="#4a5568", width=130,
        )
        self.select_newer_btn.pack(side="left", padx=(0, 8))

        self.delete_btn = ctk.CTkButton(
            action1, text="🗑  Send to Trash",
            command=self._delete_selected, state="disabled",
            fg_color="#c0392b", hover_color="#96281b",
        )
        self.delete_btn.pack(side="left")

        self.summary_label = ctk.CTkLabel(action1, text="", text_color="gray60")
        self.summary_label.pack(side="right", padx=10)

        # ── Filters bar ───────────────────────────────────────────────────────
        filter_frame = ctk.CTkFrame(self, fg_color="transparent")
        filter_frame.pack(fill="x", padx=10, pady=(4, 0))

        ctk.CTkLabel(
            filter_frame, text="Filters:", text_color="gray70"
        ).pack(side="left", padx=(2, 6))

        self.ext_entry = ctk.CTkEntry(
            filter_frame, placeholder_text="Extensions: jpg, png, pdf",
            width=220,
        )
        self.ext_entry.pack(side="left", padx=(0, 6))

        self.min_size_entry = ctk.CTkEntry(
            filter_frame, placeholder_text="Min size (e.g. 10 MB)", width=160
        )
        self.min_size_entry.pack(side="left", padx=(0, 6))

        self.max_size_entry = ctk.CTkEntry(
            filter_frame, placeholder_text="Max size (e.g. 2 GB)", width=160
        )
        self.max_size_entry.pack(side="left", padx=(0, 6))

        self.exclude_entry = ctk.CTkEntry(
            filter_frame,
            placeholder_text="Exclude folders (comma-separated)",
            width=280,
        )
        self.exclude_entry.pack(side="left", padx=(0, 6), fill="x", expand=True)

        # ── Progress bar (hidden until scan starts) ───────────────────────────
        self.progress_bar = ctk.CTkProgressBar(self, height=12)
        self.progress_bar.set(0)
        self.progress_bar.pack_forget()

        self.status_label = ctk.CTkLabel(
            self, text="", text_color="gray60", anchor="w",
        )
        self.status_label.pack(fill="x", padx=12, pady=(4, 0))

        # ── Column header row ─────────────────────────────────────────────────
        ctk.CTkLabel(
            self, text="Duplicate Groups", anchor="w", font=("", 13, "bold"),
        ).pack(fill="x", padx=12, pady=(8, 0))

        col_header = ctk.CTkFrame(self, fg_color="transparent")
        col_header.pack(fill="x", padx=14, pady=(2, 0))

        for label, weight in [
            ("  ✓  File Name", 4),
            ("Path",           5),
            ("Size",           2),
            ("Last Modified",  3),
            ("Preview",        1),
        ]:
            ctk.CTkLabel(
                col_header, text=label, anchor="w",
                font=("", 11, "bold"), text_color="gray55",
            ).pack(side="left", expand=True, fill="x", padx=(0, weight * 4))

        ctk.CTkFrame(self, height=1, fg_color="gray35").pack(
            fill="x", padx=12, pady=(2, 0)
        )

        # ── Scrollable results area ───────────────────────────────────────────
        self.scroll_frame = ctk.CTkScrollableFrame(self, label_text="")
        self.scroll_frame.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        self.placeholder = ctk.CTkLabel(
            self.scroll_frame,
            text="Choose a directory and click 'Scan for Duplicates' to begin.",
            text_color="gray50",
        )
        self.placeholder.pack(pady=40)

        # Non-blocking warning if send2trash is not installed
        if not TRASH_AVAILABLE:
            ctk.CTkLabel(
                self,
                text="⚠️  send2trash not installed — run: pip install send2trash",
                text_color="#e8a838", font=("", 11),
            ).pack(side="bottom", pady=4)

    # ── Event handlers ────────────────────────────────────────────────────────

    def _choose_directory(self):
        path_str = filedialog.askdirectory(title="Select Directory to Scan")
        if path_str:
            self._selected_directory = Path(path_str)
            self.dir_label.configure(text=str(self._selected_directory))
            self.scan_btn.configure(state="normal")
            self._clear_results()

    def _start_scan(self):
        if not self._selected_directory:
            return
        self._clear_results()
        self._pause_event.clear()
        self._cancel_event.clear()
        self._set_buttons_scanning(True)
        self.progress_bar.pack(fill="x", padx=10, pady=(4, 0))
        self.progress_bar.set(0)
        self.status_label.configure(text="Starting scan…")
        self._scan_thread = threading.Thread(target=self._scan_worker, daemon=True)
        self._scan_thread.start()

    def _scan_worker(self):
        """Runs find_duplicates() off the main thread."""
        def progress(current, total, status_text):
            fraction = current / total if total else 0
            self.after(0, self.progress_bar.set, fraction)
            self.after(0, self.status_label.configure, {"text": status_text})

        try:
            extensions = _normalize_extensions(self.ext_entry.get())
            min_raw = self.min_size_entry.get()
            max_raw = self.max_size_entry.get()
            min_size = _parse_size(min_raw)
            max_size = _parse_size(max_raw)
            excludes = _split_excludes(self.exclude_entry.get())

            if min_raw.strip() and min_size is None:
                self.after(
                    0,
                    self._on_scan_error,
                    "Invalid Min size. Use values like '10 MB', '500 KB', or '1.5 GB'.",
                )
                return

            if max_raw.strip() and max_size is None:
                self.after(
                    0,
                    self._on_scan_error,
                    "Invalid Max size. Use values like '10 MB', '500 KB', or '1.5 GB'.",
                )
                return

            if min_size is not None and max_size is not None and min_size > max_size:
                self.after(
                    0,
                    self._on_scan_error,
                    "Min size cannot be larger than Max size.",
                )
                return

            groups = find_duplicates(
                self._selected_directory,
                progress_callback=progress,
                extensions=extensions if extensions else None,
                min_size=min_size,
                max_size=max_size,
                excludes=excludes,
                pause_event=self._pause_event,
                cancel_event=self._cancel_event,
            )
        except ScanCancelled:
            self.after(0, self._on_scan_cancelled)
            return
        except Exception as exc:
            self.after(0, self._on_scan_error, str(exc))
            return

        self.after(0, self._on_scan_complete, groups)

    def _on_scan_complete(self, groups: list[list[Path]]):
        self._duplicate_groups = groups
        self._set_buttons_scanning(False)
        self.progress_bar.set(1)

        if not groups:
            self.status_label.configure(text="✅  No duplicates found.")
            self.placeholder.configure(text="No duplicates found in the selected directory.")
            self.placeholder.pack(pady=40)
            return

        total_wasted = sum(
            grp[0].stat().st_size * (len(grp) - 1)
            for grp in groups if grp
        )
        dup_count = sum(len(g) - 1 for g in groups)
        self.summary_label.configure(
            text=(
                f"{len(groups)} group(s)  ·  "
                f"{dup_count} duplicate(s)  ·  "
                f"{format_size(total_wasted)} wasted"
            )
        )
        self.status_label.configure(
            text=f"Scan complete — {len(groups)} duplicate group(s) found."
        )
        self._set_smart_buttons("normal")
        self._render_results(groups)

    def _on_scan_error(self, message: str):
        self._set_buttons_scanning(False)
        self.status_label.configure(text=f"Error: {message}")
        messagebox.showerror("Scan Error", f"An error occurred:\n{message}")

    def _on_scan_cancelled(self):
        self._set_buttons_scanning(False)
        self.status_label.configure(text="Scan cancelled.")

    # ── Results rendering ─────────────────────────────────────────────────────

    def _render_results(self, groups: list[list[Path]]):
        """
        Build one card per duplicate group inside the scrollable frame.

        Grid columns per file row:
          [0] checkbox  [1] filename  [2] path  [3] size  [4] date  [5] preview
        """
        self._checkbox_map.clear()

        for group_index, group in enumerate(groups, start=1):

            # Gather metadata
            file_meta: list[tuple[Path, int, float]] = []
            for fp in group:
                try:
                    st = fp.stat()
                    file_meta.append((fp, st.st_size, st.st_mtime))
                except OSError:
                    file_meta.append((fp, 0, 0.0))

            file_size  = file_meta[0][1] if file_meta else 0
            mtimes     = [m for _, _, m in file_meta]
            mtime_min  = min(mtimes) if mtimes else 0
            mtime_max  = max(mtimes) if mtimes else 0

            # ── Group header card ──────────────────────────────────────────
            group_frame = ctk.CTkFrame(self.scroll_frame)
            group_frame.pack(fill="x", padx=4, pady=(8, 0))

            for col_idx, weight in enumerate(COL_WEIGHTS):
                group_frame.grid_columnconfigure(
                    col_idx, weight=weight,
                    minsize=28 if col_idx == 0 else 55,
                )

            ctk.CTkLabel(
                group_frame,
                text=(
                    f"  Group {group_index}  —  {format_size(file_size)} each"
                    f"  ({len(group)} identical files)"
                ),
                font=("", 12, "bold"), anchor="w",
            ).grid(row=0, column=0, columnspan=6, sticky="ew", padx=10, pady=(8, 2))

            ctk.CTkFrame(group_frame, height=1, fg_color="gray40").grid(
                row=1, column=0, columnspan=6, sticky="ew", padx=10
            )

            # ── File rows ─────────────────────────────────────────────────
            for row_idx, (file_path, size_bytes, mtime) in enumerate(file_meta):
                base_row  = row_idx * 2 + 2
                row_color = ROW_COLORS[row_idx % 2]

                row_frame = ctk.CTkFrame(
                    group_frame, fg_color=row_color, corner_radius=4
                )
                row_frame.grid(
                    row=base_row, column=0, columnspan=6,
                    sticky="ew", padx=6, pady=(2, 0),
                )
                for col_idx, weight in enumerate(COL_WEIGHTS):
                    row_frame.grid_columnconfigure(
                        col_idx, weight=weight,
                        minsize=28 if col_idx == 0 else 55,
                    )

                # Col 0 — Checkbox
                cb = ctk.CTkCheckBox(
                    row_frame, text="", width=28, hover_color=row_color,
                )
                cb.grid(row=0, column=0, padx=(8, 4), pady=8, sticky="w")
                self._checkbox_map[cb] = (file_path, mtime)

                # Col 1 — File name (BiDi-corrected; amber if oldest copy)
                is_rtl       = has_rtl_chars(file_path.name)
                display_name = fix_bidi(file_path.name)
                name_color   = (
                    TEXT_WARN
                    if (mtime == mtime_min and mtime_min != mtime_max)
                    else TEXT_PRIMARY
                )

                ctk.CTkLabel(
                    row_frame,
                    text=display_name,
                    text_color=name_color,
                    fg_color="transparent",
                    font=("", 12, "bold"),
                    anchor="e" if is_rtl else "w",
                    wraplength=220,
                ).grid(row=0, column=1, padx=(0, 8), pady=8, sticky="ew")

                # Col 2 — Parent path (BiDi-corrected)
                ctk.CTkLabel(
                    row_frame,
                    text=fix_bidi(str(file_path.parent)),
                    text_color=TEXT_SECONDARY,
                    fg_color="transparent",
                    font=("", 11),
                    anchor="e" if is_rtl else "w",
                    wraplength=260,
                ).grid(row=0, column=2, padx=(0, 8), pady=8, sticky="ew")

                # Col 3 — File size
                ctk.CTkLabel(
                    row_frame,
                    text=format_size(size_bytes),
                    text_color=TEXT_ACCENT,
                    fg_color="transparent",
                    font=("", 11),
                    anchor="center",
                ).grid(row=0, column=3, padx=(0, 8), pady=8, sticky="ew")

                # Col 4 — Last modified date
                ctk.CTkLabel(
                    row_frame,
                    text=format_date(mtime) if mtime else "—",
                    text_color=TEXT_SECONDARY,
                    fg_color="transparent",
                    font=("", 11),
                    anchor="w",
                ).grid(row=0, column=4, padx=(0, 8), pady=8, sticky="ew")

                # Col 5 — Preview button (opens system default viewer) ── v2.0
                ctk.CTkButton(
                    row_frame,
                    text="👁",
                    width=36, height=26,
                    fg_color="#3a4a5c",
                    hover_color="#4a6080",
                    font=("", 13),
                    command=lambda p=file_path: open_with_system_viewer(p),
                ).grid(row=0, column=5, padx=(0, 8), pady=6, sticky="e")

            # Bottom spacer
            ctk.CTkFrame(
                group_frame, height=6, fg_color="transparent"
            ).grid(row=len(file_meta) * 2 + 2, column=0, columnspan=6)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _select_all(self):
        """Check every checkbox."""
        for cb in self._checkbox_map:
            cb.select()

    def _select_older(self):
        """Mark the older copy/copies in each group for trashing; keep newest."""
        self._smart_select(keep="newest")

    def _select_newer(self):
        """Mark the newer copy/copies in each group for trashing; keep oldest."""
        self._smart_select(keep="oldest")

    def _smart_select(self, keep: str):
        """
        Check all files except the one to keep in each duplicate group.

        Args:
            keep: 'newest' → uncheck the file with the highest mtime.
                  'oldest' → uncheck the file with the lowest mtime.
        """
        # Start clean
        for cb in self._checkbox_map:
            cb.deselect()

        # Build a path → checkbox reverse map for fast lookup
        path_to_cb: dict[Path, ctk.CTkCheckBox] = {
            path: cb for cb, (path, _) in self._checkbox_map.items()
        }

        for group in self._duplicate_groups:
            # Collect (mtime, path) for each file in this group
            timed: list[tuple[float, Path]] = []
            for fp in group:
                for cb, (path, mtime) in self._checkbox_map.items():
                    if path == fp:
                        timed.append((mtime, fp))
                        break

            if not timed:
                continue

            keep_mtime = max(t for t, _ in timed) if keep == "newest" \
                    else min(t for t, _ in timed)

            for mtime, fp in timed:
                cb = path_to_cb.get(fp)
                if cb is None:
                    continue
                # If multiple files share keep_mtime, only the first is kept
                if mtime == keep_mtime and all(
                    not path_to_cb[p].get()
                    for t, p in timed
                    if t == keep_mtime and p != fp
                ):
                    cb.deselect()
                else:
                    cb.select()

    def _delete_selected(self):
        """Send all checked files to the system Recycle Bin / Trash."""
        to_trash = [
            (cb, path)
            for cb, (path, _) in self._checkbox_map.items()
            if cb.get()
        ]

        if not to_trash:
            messagebox.showinfo(
                "Nothing Selected",
                "Please check at least one file to send to Trash.",
            )
            return

        if not TRASH_AVAILABLE:
            messagebox.showerror(
                "send2trash Missing",
                "Please install send2trash:\n\n    pip install send2trash\n\n"
                "Then restart the app.",
            )
            return

        confirm = messagebox.askyesno(
            "Confirm",
            f"Send {len(to_trash)} file(s) to the Recycle Bin / Trash?\n\n"
            "You can restore them from Trash if needed.",
        )
        if not confirm:
            return

        trashed, failed = [], []
        for cb, path in to_trash:
            try:
                send2trash(str(path))
                trashed.append(path)
            except Exception as exc:
                failed.append((path, str(exc)))

        msg = [f"✅ Sent {len(trashed)} file(s) to Trash."]
        if failed:
            lines = "\n".join(f"  • {p.name}: {r}" for p, r in failed)
            msg.append(f"\n⚠️ Failed ({len(failed)}):\n{lines}")

        messagebox.showinfo("Done", "\n".join(msg))
        self._start_scan()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _clear_results(self):
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()
        self._checkbox_map.clear()
        self._duplicate_groups.clear()
        self.summary_label.configure(text="")

        self.placeholder = ctk.CTkLabel(
            self.scroll_frame,
            text="Choose a directory and click 'Scan for Duplicates' to begin.",
            text_color="gray50",
        )
        self.placeholder.pack(pady=40)

    def _set_buttons_scanning(self, scanning: bool):
        self.scan_btn.configure(state="disabled" if scanning else "normal")
        self.pause_btn.configure(state="normal" if scanning else "disabled")
        self.cancel_btn.configure(state="normal" if scanning else "disabled")
        if not scanning:
            self.pause_btn.configure(text="⏸  Pause")
            self._pause_event.clear()
        self._set_smart_buttons("disabled" if scanning else "normal")

    def _toggle_pause(self):
        if self._pause_event.is_set():
            self._pause_event.clear()
            self.pause_btn.configure(text="⏸  Pause")
            self.status_label.configure(text="Resuming scan…")
        else:
            self._pause_event.set()
            self.pause_btn.configure(text="▶  Resume")
            self.status_label.configure(text="Scan paused.")

    def _cancel_scan(self):
        if self._cancel_event.is_set():
            return
        self._cancel_event.set()
        self._pause_event.clear()
        self.status_label.configure(text="Cancelling scan…")

    def _set_smart_buttons(self, state: str):
        for btn in (
            self.select_all_btn,
            self.select_older_btn,
            self.select_newer_btn,
            self.delete_btn,
        ):
            btn.configure(state=state)


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = DuplicateFinderApp()
    app.mainloop()
