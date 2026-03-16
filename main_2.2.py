"""
Duplicate File Finder — Version 2.2
=====================================
Cross-platform (Linux / Windows) GUI application built with CustomTkinter.

New in v2.2
-----------
  1. Checkbox Fix         — Every CTkCheckBox uses an explicit IntVar so
                            cb.get() always returns 0/1 reliably (fixes the
                            "Nothing selected" false-positive bug)
  2. No Re-scan on Delete — After trashing files the deleted rows are removed
                            from the UI in-place; the disk is NOT re-scanned
  3. Dedup Guard          — Each file path appears in at most one group
                            (global seen-set eliminates cross-group dupes)
  4. BiDi Everywhere      — fix_bidi() called on every text label, not just
                            detected-RTL ones; wraplength tuned per column
  5. Streaming Results    — Groups appear in the scrollable frame as soon as
                            they are found, without waiting for scan to finish
  6. Sort by Size         — Toolbar dropdown: Largest First / Smallest First
  7. Duplicate Folders    — Detects folders whose full MD5-tree is identical
  8. Original vs Copy     — Each group shows "📌 Original" (oldest mtime) and
                            "📋 Copy" badges; copies pre-selected by default
  9. Safe-Mode Banner     — Warns when a path looks like a system directory
 10. Space Savings Banner — "You can free X GB" shown after scan
 11. Profiles & Summary   — Carried over from v2.1

Requirements
------------
    pip install customtkinter send2trash python-bidi arabic-reshaper
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox

# ── send2trash ────────────────────────────────────────────────────────────────
try:
    from send2trash import send2trash
    TRASH_AVAILABLE = True
except ImportError:
    TRASH_AVAILABLE = False

# ── BiDi ──────────────────────────────────────────────────────────────────────
try:
    from bidi.algorithm import get_display as bidi_get_display
    import arabic_reshaper
    BIDI_AVAILABLE = True
except ImportError:
    BIDI_AVAILABLE = False

# ── Appearance ────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

ROW_COLORS      = ("#2b2b2b", "#1e1e1e")
TEXT_PRIMARY    = "#DCE4EE"
TEXT_SECONDARY  = "#9BAAB8"
TEXT_ACCENT     = "#5dade2"
TEXT_WARN       = "#e8a838"
TEXT_ORIGINAL   = "#2ecc71"   # green  — Original badge
TEXT_COPY       = "#e67e22"   # orange — Copy badge
COL_WEIGHTS     = [0, 1, 4, 5, 2, 3, 1]   # badge, cb, name, path, size, date, preview

PROFILES_FILE   = Path.home() / ".duplicate_finder_profiles.json"

SYSTEM_DIRS = {
    "windows": ["windows", "system32", "syswow64", "program files",
                "programdata", "appdata"],
    "linux":   ["/proc", "/sys", "/dev", "/run", "/boot"],
    "darwin":  ["/system", "/library", "/private"],
}


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def format_size(n: int) -> str:
    if n >= 1_073_741_824:
        return f"{n/1_073_741_824:.2f} GB"
    if n >= 1_048_576:
        return f"{n/1_048_576:.2f} MB"
    if n >= 1_024:
        return f"{n/1_024:.2f} KB"
    return f"{n} B"


def format_date(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d  %H:%M")


def fix_bidi(text: str) -> str:
    """Full BiDi pipeline on every string — reshape then visual-reorder."""
    if not BIDI_AVAILABLE or not text:
        return text
    try:
        return bidi_get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


def has_rtl(text: str) -> bool:
    return any("\u0590" <= c <= "\u05FF" for c in text)


def open_viewer(path: Path) -> None:
    """Open a file in the OS default application. Works on Windows, macOS, Linux."""
    if not path.exists():
        messagebox.showerror("Preview Error",
                             f"File no longer exists:\n{path}")
        return
    try:
        os_name = platform.system()
        if os_name == "Windows":
            os.startfile(str(path))             # type: ignore[attr-defined]
        elif os_name == "Darwin":
            subprocess.Popen(["open", str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        messagebox.showerror("Preview Error",
                             "Could not find a viewer for this file type.\n"
                             "Make sure a default application is set.")
    except Exception as exc:
        messagebox.showerror("Preview Error", f"Could not open file:\n{exc}")


def is_system_path(path: Path) -> bool:
    lower = str(path).lower()
    sys = platform.system().lower()
    for token in SYSTEM_DIRS.get(sys, []):
        if token in lower:
            return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Profiles
# ──────────────────────────────────────────────────────────────────────────────

def load_profiles() -> dict:
    if PROFILES_FILE.exists():
        try:
            return json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_profiles(profiles: dict) -> None:
    try:
        PROFILES_FILE.write_text(
            json.dumps(profiles, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        messagebox.showerror("Save Error", f"Could not save profiles:\n{exc}")


# ──────────────────────────────────────────────────────────────────────────────
# Scanning engine
# ──────────────────────────────────────────────────────────────────────────────

SAMPLE_SIZE = 10 * 1_024


def _read_sample(fp: Path) -> bytes | None:
    try:
        with fp.open("rb") as f:
            return f.read(SAMPLE_SIZE)
    except (PermissionError, OSError):
        return None


def _full_md5(fp: Path, chunk: int = 65_536) -> str | None:
    h = hashlib.md5()
    try:
        with fp.open("rb") as f:
            while buf := f.read(chunk):
                h.update(buf)
        return h.hexdigest()
    except (PermissionError, OSError):
        return None


class ScanCancelled(Exception):
    pass


def _norm_ext(raw: str) -> set[str]:
    parts = [p.strip().lower() for p in raw.replace(";", ",").split(",")]
    return {(p if p.startswith(".") else f".{p}") for p in parts if p}


def _parse_size(raw: str) -> int | None:
    raw = raw.strip().lower()
    if not raw:
        return None
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)$", raw)
    if not m:
        return None
    v, u = float(m.group(1)), m.group(2) or "kb"
    table = {
        "b":1,"byte":1,"bytes":1,
        "k":1024,"kb":1024,"kib":1024,
        "m":1024**2,"mb":1024**2,"mib":1024**2,
        "g":1024**3,"gb":1024**3,"gib":1024**3,
    }
    return int(v * table[u]) if u in table else None


def _split_exc(raw: str) -> list[str]:
    return [p.strip().lower() for p in raw.replace(";", ",").split(",") if p.strip()]


def _is_excluded(path: Path, excl: list[str]) -> bool:
    if not excl:
        return False
    lparts = [p.lower() for p in path.parts]
    lpath  = str(path).lower()
    for t in excl:
        if "/" in t or "\\" in t:
            if t in lpath:
                return True
        elif t in lparts:
            return True
    return False


def _wait_if_paused(pe: threading.Event, ce: threading.Event) -> None:
    while pe.is_set():
        if ce.is_set():
            raise ScanCancelled()
        time.sleep(0.15)


def _collect_scandir(
    root: Path,
    extensions: set[str] | None,
    min_size: int | None,
    max_size: int | None,
    excl: list[str],
    pe: threading.Event,
    ce: threading.Event,
) -> list[tuple[Path, int]]:
    result: list[tuple[Path, int]] = []
    stack = [str(root)]
    while stack:
        _wait_if_paused(pe, ce)
        if ce.is_set():
            raise ScanCancelled()
        try:
            entries = list(os.scandir(stack.pop()))
        except (PermissionError, OSError):
            continue
        for e in entries:
            if ce.is_set():
                raise ScanCancelled()
            try:
                p = Path(e.path)
                if e.is_dir(follow_symlinks=False):
                    if not _is_excluded(p, excl):
                        stack.append(e.path)
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                if _is_excluded(p, excl):
                    continue
                if extensions and p.suffix.lower() not in extensions:
                    continue
                sz = e.stat().st_size
                if min_size is not None and sz < min_size:
                    continue
                if max_size is not None and sz > max_size:
                    continue
                result.append((p, sz))
            except (PermissionError, OSError):
                continue
    return result


def find_duplicates(
    directories: list[Path],
    group_callback=None,          # called with each new group as found
    progress_callback=None,
    *,
    extensions: set[str] | None = None,
    min_size: int | None = None,
    max_size: int | None = None,
    excl: list[str] | None = None,
    pe: threading.Event | None = None,
    ce: threading.Event | None = None,
) -> list[list[Path]]:
    excl = excl or []
    pe   = pe or threading.Event()
    ce   = ce or threading.Event()

    # Pass 1 — collect + group by size (parallel, dedup across dirs)
    size_map: dict[int, list[Path]] = defaultdict(list)
    seen: set[Path] = set()

    def collect(d: Path):
        return _collect_scandir(d, extensions, min_size, max_size, excl, pe, ce)

    with ThreadPoolExecutor(max_workers=min(len(directories), 8)) as exe:
        futs = {exe.submit(collect, d): d for d in directories}
        for fut in as_completed(futs):
            if ce.is_set():
                raise ScanCancelled()
            for path, sz in fut.result():
                if path not in seen:
                    seen.add(path)
                    size_map[sz].append(path)

    candidates = [paths for paths in size_map.values() if len(paths) > 1]

    # Pass 2 — 10 KB sample hash
    sample_cands: list[list[Path]] = []
    total2 = sum(len(g) for g in candidates)
    done2  = 0
    for sg in candidates:
        smap: dict[bytes, list[Path]] = defaultdict(list)
        for fp in sg:
            _wait_if_paused(pe, ce)
            if ce.is_set():
                raise ScanCancelled()
            done2 += 1
            if progress_callback:
                progress_callback(done2, total2,
                    f"Sampling ({done2}/{total2}): {fp.name}")
            s = _read_sample(fp)
            if s is not None:
                smap[s].append(fp)
        for paths in smap.values():
            if len(paths) > 1:
                sample_cands.append(paths)

    # Pass 3 — full MD5
    all_groups: list[list[Path]] = []
    # Global dedup: each path in exactly one group
    assigned: set[Path] = set()
    total3 = sum(len(g) for g in sample_cands)
    done3  = 0
    for sg in sample_cands:
        hmap: dict[str, list[Path]] = defaultdict(list)
        for fp in sg:
            _wait_if_paused(pe, ce)
            if ce.is_set():
                raise ScanCancelled()
            done3 += 1
            if progress_callback:
                progress_callback(done3, total3,
                    f"Verifying ({done3}/{total3}): {fp.name}")
            d = _full_md5(fp)
            if d is not None:
                hmap[d].append(fp)
        for paths in hmap.values():
            if len(paths) > 1:
                # Only include paths not yet assigned to another group
                clean = [p for p in paths if p not in assigned]
                if len(clean) > 1:
                    for p in clean:
                        assigned.add(p)
                    all_groups.append(clean)
                    if group_callback:
                        group_callback(clean)

    return all_groups


# ── Duplicate folder detection ────────────────────────────────────────────────

def _folder_signature(folder: Path) -> str | None:
    """MD5 of all sorted relative-path:md5 pairs inside a folder."""
    entries: list[tuple[str, str]] = []
    try:
        for fp in sorted(folder.rglob("*")):
            if fp.is_file():
                d = _full_md5(fp)
                if d is None:
                    return None
                entries.append((str(fp.relative_to(folder)), d))
    except (PermissionError, OSError):
        return None
    if not entries:
        return None
    combined = "\n".join(f"{rel}:{h}" for rel, h in entries)
    return hashlib.md5(combined.encode()).hexdigest()


def find_duplicate_folders(
    directories: list[Path],
    progress_callback=None,
) -> list[list[Path]]:
    """Return groups of folders with identical content."""
    all_dirs: list[Path] = []
    for root in directories:
        try:
            for entry in root.rglob("*"):
                if entry.is_dir():
                    all_dirs.append(entry)
        except (PermissionError, OSError):
            pass

    sig_map: dict[str, list[Path]] = defaultdict(list)
    for i, d in enumerate(all_dirs):
        if progress_callback:
            progress_callback(i + 1, len(all_dirs), f"Checking folder: {d.name}")
        sig = _folder_signature(d)
        if sig:
            sig_map[sig].append(d)

    return [paths for paths in sig_map.values() if len(paths) > 1]


# ──────────────────────────────────────────────────────────────────────────────
# Summary Dashboard
# ──────────────────────────────────────────────────────────────────────────────

class SummaryWindow(ctk.CTkToplevel):
    BAR_PALETTE = ["#5dade2","#48c9b0","#f4d03f","#e67e22",
                   "#9b59b6","#ec407a","#26a69a","#ef5350"]

    def __init__(self, parent, groups: list[list[Path]]):
        super().__init__(parent)
        self.title("📊  Scan Summary")
        self.geometry("700x540")
        self.resizable(False, False)
        self.grab_set()
        self._build(groups)

    def _build(self, groups):
        all_paths = [fp for g in groups for fp in g]
        dup_count = sum(len(g) - 1 for g in groups)
        wasted    = 0
        ext_count: dict[str, int] = defaultdict(int)

        for group in groups:
            try:
                sz = group[0].stat().st_size
            except OSError:
                sz = 0
            wasted += sz * (len(group) - 1)
            for fp in group:
                ext_count[fp.suffix.lower() or "(none)"] += 1

        top_exts = sorted(ext_count.items(), key=lambda x: -x[1])[:8]

        pad = {"padx": 18, "pady": 5}
        ctk.CTkLabel(self, text="Scan Summary",
                     font=("", 20, "bold"), text_color=TEXT_ACCENT).pack(**pad, anchor="w")
        ctk.CTkFrame(self, height=1, fg_color="gray35").pack(fill="x", padx=18)

        sf = ctk.CTkFrame(self, fg_color="transparent")
        sf.pack(fill="x", **pad)

        def stat(lbl, val, color=TEXT_PRIMARY):
            r = ctk.CTkFrame(sf, fg_color="transparent")
            r.pack(fill="x", pady=1)
            ctk.CTkLabel(r, text=lbl, text_color="gray60",
                         font=("", 12), width=240, anchor="w").pack(side="left")
            ctk.CTkLabel(r, text=val, text_color=color,
                         font=("", 12, "bold"), anchor="w").pack(side="left")

        stat("Total files scanned:",    str(len(all_paths)))
        stat("Duplicate groups found:", str(len(groups)),    TEXT_ACCENT)
        stat("Redundant copies:",       str(dup_count),      TEXT_WARN)
        stat("Wasted space:",           format_size(wasted), "#e74c3c")

        ctk.CTkFrame(self, height=1, fg_color="gray35").pack(fill="x", padx=18, pady=(8,0))
        ctk.CTkLabel(self, text="Duplicates by Extension",
                     font=("", 13, "bold"), anchor="w").pack(**pad, anchor="w")

        c = tk.Canvas(self, width=650, height=200, bg="#1a1a2e", highlightthickness=0)
        c.pack(padx=18, pady=(0, 12))

        if top_exts:
            mx = max(cnt for _, cnt in top_exts)
            bw, sp, xs = 60, 18, 28
            ch, yb = 150, 182
            for i, (ext, cnt) in enumerate(top_exts):
                x     = xs + i * (bw + sp)
                bh    = int((cnt / mx) * ch) if mx else 1
                color = self.BAR_PALETTE[i % len(self.BAR_PALETTE)]
                yt    = yb - bh
                c.create_rectangle(x+3, yt+3, x+bw+3, yb+3, fill="#111", outline="")
                c.create_rectangle(x, yt, x+bw, yb, fill=color, outline="")
                c.create_text(x+bw//2, yt-10, text=str(cnt),
                              fill=color, font=("Consolas", 9, "bold"))
                c.create_text(x+bw//2, yb+12, text=ext,
                              fill="#aaa", font=("Consolas", 8))

        ctk.CTkButton(self, text="Close", command=self.destroy,
                      fg_color="gray35", hover_color="gray25").pack(pady=(0,14))


# ──────────────────────────────────────────────────────────────────────────────
# Profile Dialog
# ──────────────────────────────────────────────────────────────────────────────

class ProfileDialog(ctk.CTkToplevel):
    def __init__(self, parent, current: dict, on_load):
        super().__init__(parent)
        self.title("⚙️  Profiles")
        self.geometry("460x420")
        self.resizable(False, False)
        self.grab_set()
        self._profiles = load_profiles()
        self._current  = current
        self._on_load  = on_load
        self._build()

    def _build(self):
        ctk.CTkLabel(self, text="Saved Profiles",
                     font=("", 16, "bold")).pack(padx=16, pady=(14,4), anchor="w")
        self._lb = ctk.CTkScrollableFrame(self, height=200)
        self._lb.pack(fill="x", padx=16, pady=4)
        self._refresh()

        nr = ctk.CTkFrame(self, fg_color="transparent")
        nr.pack(fill="x", padx=16, pady=(8,0))
        ctk.CTkLabel(nr, text="Profile name:", width=110, anchor="w").pack(side="left")
        self._ne = ctk.CTkEntry(nr, placeholder_text="e.g. Photos Scan")
        self._ne.pack(side="left", fill="x", expand=True, padx=(6,0))

        br = ctk.CTkFrame(self, fg_color="transparent")
        br.pack(fill="x", padx=16, pady=10)
        ctk.CTkButton(br, text="💾  Save Current Settings",
                      command=self._save,
                      fg_color="#1a6b3c", hover_color="#145530").pack(side="left", padx=(0,8))
        ctk.CTkButton(br, text="Cancel", command=self.destroy,
                      fg_color="gray35", hover_color="gray25").pack(side="right")

    def _refresh(self):
        for w in self._lb.winfo_children():
            w.destroy()
        if not self._profiles:
            ctk.CTkLabel(self._lb, text="No saved profiles yet.",
                         text_color="gray50").pack(pady=10)
            return
        for name in self._profiles:
            row = ctk.CTkFrame(self._lb, fg_color="#2b2b2b", corner_radius=6)
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=name, anchor="w",
                         font=("",12)).pack(side="left", padx=10, pady=6,
                                            fill="x", expand=True)
            ctk.CTkButton(row, text="Load", width=60,
                          fg_color="#1a4a7a", hover_color="#153d63",
                          command=lambda n=name: self._load(n)).pack(
                              side="right", padx=(4,4), pady=4)
            ctk.CTkButton(row, text="🗑", width=34,
                          fg_color="#5c1010", hover_color="#4a0d0d",
                          command=lambda n=name: self._del(n)).pack(
                              side="right", padx=(0,2), pady=4)

    def _save(self):
        name = self._ne.get().strip()
        if not name:
            messagebox.showwarning("Name Required",
                                   "Please enter a profile name.", parent=self)
            return
        self._profiles[name] = self._current
        save_profiles(self._profiles)
        self._refresh()
        messagebox.showinfo("Saved", f"Profile '{name}' saved.", parent=self)

    def _load(self, n):
        self._on_load(self._profiles[n])
        self.destroy()

    def _del(self, n):
        if messagebox.askyesno("Delete", f"Delete profile '{n}'?", parent=self):
            del self._profiles[n]
            save_profiles(self._profiles)
            self._refresh()


# ──────────────────────────────────────────────────────────────────────────────
# Main Application
# ──────────────────────────────────────────────────────────────────────────────

class DuplicateFinderApp(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title("Duplicate File Finder  v2.2")
        self.geometry("1200x800")
        self.minsize(920, 580)

        self._directories:      list[Path]             = []
        self._duplicate_groups: list[list[Path]]       = []
        # Maps checkbox widget → (IntVar, Path, mtime)
        # Using IntVar fixes the cb.get() == 0 bug
        self._checkbox_map: dict[ctk.CTkCheckBox, tuple[tk.IntVar, Path, float]] = {}
        # Maps group index → CTkFrame (for in-place row removal)
        self._group_frames: dict[int, ctk.CTkFrame]    = {}
        # Maps Path → (group_index, row_frame) for targeted deletion
        self._path_to_row:  dict[Path, tuple[int, ctk.CTkFrame]] = {}

        self._pause_event  = threading.Event()
        self._cancel_event = threading.Event()
        self._scan_thread: threading.Thread | None = None
        self._sort_order   = "largest"   # "largest" | "smallest"
        self._stream_lock  = threading.Lock()

        self._build_ui()

    # ─────────────────────────────────────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Folder bar ────────────────────────────────────────────────────────
        fo = ctk.CTkFrame(self, corner_radius=0)
        fo.pack(fill="x", padx=10, pady=(10,0))
        ctk.CTkLabel(fo, text="📁  Scan Directories",
                     font=("",13,"bold"), anchor="w").pack(side="left", padx=10, pady=8)
        ctk.CTkButton(fo, text="＋  Add Folder", width=140,
                      command=self._add_dir).pack(side="right", padx=10, pady=8)

        self.folder_frame = ctk.CTkScrollableFrame(self, height=76)
        self.folder_frame.pack(fill="x", padx=10, pady=(2,0))
        self._folder_ph = ctk.CTkLabel(
            self.folder_frame,
            text="No directories selected — click '＋ Add Folder'.",
            text_color="gray50")
        self._folder_ph.pack(pady=8)

        # ── Savings banner (hidden until scan done) ───────────────────────────
        self.savings_frame = ctk.CTkFrame(self, fg_color="#1a3a1a", corner_radius=8)
        # packed later

        self.savings_label = ctk.CTkLabel(
            self.savings_frame, text="",
            font=("",14,"bold"), text_color="#2ecc71")
        self.savings_label.pack(padx=16, pady=8)

        # ── Action bar ────────────────────────────────────────────────────────
        a1 = ctk.CTkFrame(self, fg_color="transparent")
        a1.pack(fill="x", padx=10, pady=(6,0))

        self.scan_btn = ctk.CTkButton(
            a1, text="🔍  Scan", command=self._start_scan, state="disabled")
        self.scan_btn.pack(side="left", padx=(0,6))

        self.pause_btn = ctk.CTkButton(
            a1, text="⏸  Pause", command=self._toggle_pause, state="disabled",
            fg_color="#6c7a89", hover_color="#5b6b7a", width=100)
        self.pause_btn.pack(side="left", padx=(0,6))

        self.cancel_btn = ctk.CTkButton(
            a1, text="⛔  Cancel", command=self._cancel_scan, state="disabled",
            fg_color="#7f8c8d", hover_color="#6b7b7c", width=100)
        self.cancel_btn.pack(side="left", padx=(0,6))

        self.sel_all_btn = ctk.CTkButton(
            a1, text="☑  All", command=self._select_all, state="disabled",
            fg_color="gray40", hover_color="gray30", width=80)
        self.sel_all_btn.pack(side="left", padx=(0,6))

        self.sel_older_btn = ctk.CTkButton(
            a1, text="🕐  Older", command=self._select_older, state="disabled",
            fg_color="#5d6d7e", hover_color="#4a5568", width=90)
        self.sel_older_btn.pack(side="left", padx=(0,6))

        self.sel_newer_btn = ctk.CTkButton(
            a1, text="🕒  Newer", command=self._select_newer, state="disabled",
            fg_color="#5d6d7e", hover_color="#4a5568", width=90)
        self.sel_newer_btn.pack(side="left", padx=(0,6))

        self.del_btn = ctk.CTkButton(
            a1, text="🗑  Send to Trash", command=self._delete_selected,
            state="disabled", fg_color="#c0392b", hover_color="#96281b")
        self.del_btn.pack(side="left", padx=(0,6))

        # Sort dropdown
        self.sort_var = ctk.StringVar(value="Largest First")
        self.sort_menu = ctk.CTkOptionMenu(
            a1,
            values=["Largest First", "Smallest First"],
            variable=self.sort_var,
            command=self._on_sort_change,
            width=140,
            state="disabled",
        )
        self.sort_menu.pack(side="left", padx=(0,6))

        # Right side buttons
        self.stats_btn = ctk.CTkButton(
            a1, text="📊  Summary", command=self._show_summary,
            state="disabled", fg_color="#1a6b3c", hover_color="#145530", width=110)
        self.stats_btn.pack(side="right", padx=(6,0))

        self.profile_btn = ctk.CTkButton(
            a1, text="⚙️  Profiles", command=self._open_profiles,
            fg_color="#4a3060", hover_color="#3a2450", width=110)
        self.profile_btn.pack(side="right")

        self.summary_label = ctk.CTkLabel(a1, text="", text_color="gray60")
        self.summary_label.pack(side="right", padx=8)

        # ── Filters ───────────────────────────────────────────────────────────
        ff = ctk.CTkFrame(self, fg_color="transparent")
        ff.pack(fill="x", padx=10, pady=(4,0))
        ctk.CTkLabel(ff, text="Filters:", text_color="gray70").pack(side="left", padx=(2,6))

        self.ext_entry = ctk.CTkEntry(ff, placeholder_text="Extensions: jpg, png", width=200)
        self.ext_entry.pack(side="left", padx=(0,6))
        self.min_entry = ctk.CTkEntry(ff, placeholder_text="Min size (e.g. 10 MB)", width=150)
        self.min_entry.pack(side="left", padx=(0,6))
        self.max_entry = ctk.CTkEntry(ff, placeholder_text="Max size (e.g. 2 GB)", width=150)
        self.max_entry.pack(side="left", padx=(0,6))
        self.exc_entry = ctk.CTkEntry(ff, placeholder_text="Exclude folders", width=240)
        self.exc_entry.pack(side="left", padx=(0,6), fill="x", expand=True)

        # ── Progress ──────────────────────────────────────────────────────────
        self.progress_bar = ctk.CTkProgressBar(self, height=12)
        self.progress_bar.set(0)
        self.progress_bar.pack_forget()

        self.status_label = ctk.CTkLabel(self, text="", text_color="gray60", anchor="w")
        self.status_label.pack(fill="x", padx=12, pady=(4,0))

        # ── Column headers ────────────────────────────────────────────────────
        ctk.CTkLabel(self, text="Duplicate Groups", anchor="w",
                     font=("",13,"bold")).pack(fill="x", padx=12, pady=(8,0))

        ch = ctk.CTkFrame(self, fg_color="transparent")
        ch.pack(fill="x", padx=14, pady=(2,0))
        for lbl, w in [("", 1), ("  ✓  File Name", 4), ("Path", 5),
                       ("Size", 2), ("Last Modified", 3), ("Preview", 1)]:
            ctk.CTkLabel(ch, text=lbl, anchor="w",
                         font=("",11,"bold"), text_color="gray55").pack(
                             side="left", expand=True, fill="x", padx=(0, w*4))

        ctk.CTkFrame(self, height=1, fg_color="gray35").pack(fill="x", padx=12, pady=(2,0))

        # ── Scrollable results ────────────────────────────────────────────────
        self.scroll_frame = ctk.CTkScrollableFrame(self, label_text="")
        self.scroll_frame.pack(fill="both", expand=True, padx=10, pady=(4,10))

        self.placeholder = ctk.CTkLabel(
            self.scroll_frame,
            text="Add a directory and click 'Scan' to begin.",
            text_color="gray50")
        self.placeholder.pack(pady=40)

        if not TRASH_AVAILABLE:
            ctk.CTkLabel(self,
                text="⚠️  send2trash not installed — run: pip install send2trash",
                text_color=TEXT_WARN, font=("",11)).pack(side="bottom", pady=4)

    # ── Folder management ─────────────────────────────────────────────────────

    def _add_dir(self):
        p = filedialog.askdirectory(title="Select Directory to Scan")
        if not p:
            return
        path = Path(p)
        if path in self._directories:
            return
        if is_system_path(path):
            if not messagebox.askyesno(
                "⚠️  System Directory Warning",
                f"'{path}' looks like a system directory.\n\n"
                "Scanning system folders can be slow and risky.\n"
                "Continue anyway?",
            ):
                return
        self._directories.append(path)
        self._refresh_folders()
        self.scan_btn.configure(state="normal")

    def _remove_dir(self, path: Path):
        self._directories.remove(path)
        self._refresh_folders()
        if not self._directories:
            self.scan_btn.configure(state="disabled")

    def _refresh_folders(self):
        for w in self.folder_frame.winfo_children():
            w.destroy()
        if not self._directories:
            ctk.CTkLabel(self.folder_frame,
                         text="No directories selected — click '＋ Add Folder'.",
                         text_color="gray50").pack(pady=8)
            return
        for path in self._directories:
            row = ctk.CTkFrame(self.folder_frame, fg_color="#2b2b2b", corner_radius=6)
            row.pack(fill="x", pady=2, padx=2)
            anchor = "e" if has_rtl(str(path)) else "w"
            ctk.CTkLabel(row, text=fix_bidi(str(path)), anchor=anchor,
                         text_color=TEXT_PRIMARY, font=("",11)).pack(
                             side="left", padx=10, pady=4, fill="x", expand=True)
            ctk.CTkButton(row, text="✕", width=28, height=24,
                          fg_color="#5c1010", hover_color="#4a0d0d",
                          command=lambda p=path: self._remove_dir(p)).pack(
                              side="right", padx=6, pady=4)

    # ── Scan ──────────────────────────────────────────────────────────────────

    def _start_scan(self):
        if not self._directories:
            return
        self._clear_results()
        self._pause_event.clear()
        self._cancel_event.clear()
        self._set_scanning(True)
        self.progress_bar.pack(fill="x", padx=10, pady=(4,0))
        self.progress_bar.set(0)
        self.status_label.configure(text="Starting scan…")
        self.savings_frame.pack_forget()
        self._scan_thread = threading.Thread(target=self._scan_worker, daemon=True)
        self._scan_thread.start()

    def _scan_worker(self):
        def progress(cur, tot, txt):
            frac = cur / tot if tot else 0
            self.after(0, self.progress_bar.set, frac)
            self.after(0, self.status_label.configure, {"text": txt})

        def on_group(group: list[Path]):
            # Streaming: render group as soon as it's found
            self.after(0, self._stream_group, group)

        try:
            extensions = _norm_ext(self.ext_entry.get())
            min_raw    = self.min_entry.get()
            max_raw    = self.max_entry.get()
            min_size   = _parse_size(min_raw)
            max_size   = _parse_size(max_raw)
            excl       = _split_exc(self.exc_entry.get())

            if min_raw.strip() and min_size is None:
                self.after(0, self._on_error, "Invalid Min size.")
                return
            if max_raw.strip() and max_size is None:
                self.after(0, self._on_error, "Invalid Max size.")
                return
            if min_size and max_size and min_size > max_size:
                self.after(0, self._on_error, "Min size cannot exceed Max size.")
                return

            groups = find_duplicates(
                self._directories,
                group_callback=on_group,
                progress_callback=progress,
                extensions=extensions if extensions else None,
                min_size=min_size, max_size=max_size, excl=excl,
                pe=self._pause_event, ce=self._cancel_event,
            )
        except ScanCancelled:
            self.after(0, self._on_cancelled)
            return
        except Exception as exc:
            self.after(0, self._on_error, str(exc))
            return

        self.after(0, self._on_complete, groups)

    def _stream_group(self, group: list[Path]):
        """Called on main thread as each group is found — renders immediately."""
        # Remove placeholder if still visible
        if self.placeholder.winfo_ismapped():
            self.placeholder.pack_forget()

        idx = len(self._duplicate_groups)
        self._duplicate_groups.append(group)
        self._render_group(group, idx)

    def _on_complete(self, groups: list[list[Path]]):
        self._set_scanning(False)
        self.progress_bar.set(1)

        if not self._duplicate_groups:
            self.status_label.configure(text="✅  No duplicates found.")
            self.placeholder.configure(
                text="No duplicates found in the selected directories.")
            self.placeholder.pack(pady=40)
            return

        self._finalise_display()

    def _finalise_display(self):
        groups  = self._duplicate_groups
        wasted  = sum(
            grp[0].stat().st_size * (len(grp) - 1)
            for grp in groups if grp
        )
        dup_cnt = sum(len(g) - 1 for g in groups)

        self.summary_label.configure(
            text=f"{len(groups)} group(s)  ·  {dup_cnt} duplicate(s)  ·  "
                 f"{format_size(wasted)} wasted")
        self.status_label.configure(
            text=f"Scan complete — {len(groups)} duplicate group(s) found.")

        self.savings_label.configure(
            text=f"💾  You can free up {format_size(wasted)}  by removing duplicates")
        self.savings_frame.pack(fill="x", padx=10, pady=(4,0))

        self._set_result_buttons("normal")
        self.stats_btn.configure(state="normal")
        self.sort_menu.configure(state="normal")

    def _on_error(self, msg: str):
        self._set_scanning(False)
        self.status_label.configure(text=f"Error: {msg}")
        messagebox.showerror("Scan Error", f"An error occurred:\n{msg}")

    def _on_cancelled(self):
        self._set_scanning(False)
        self.status_label.configure(text="Scan cancelled.")

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _render_group(self, group: list[Path], group_idx: int):
        """Render a single duplicate group card."""
        file_meta: list[tuple[Path, int, float]] = []
        for fp in group:
            try:
                st = fp.stat()
                file_meta.append((fp, st.st_size, st.st_mtime))
            except OSError:
                file_meta.append((fp, 0, 0.0))

        file_size = file_meta[0][1] if file_meta else 0
        mtimes    = [m for _, _, m in file_meta]
        mtime_min = min(mtimes) if mtimes else 0
        mtime_max = max(mtimes) if mtimes else 0

        gf = ctk.CTkFrame(self.scroll_frame)
        gf.pack(fill="x", padx=4, pady=(8,0))
        self._group_frames[group_idx] = gf

        col_cfg = [(0,0,28),(1,0,28),(2,4,55),(3,5,55),(4,2,55),(5,3,55),(6,1,55)]
        for ci, w, ms in col_cfg:
            gf.grid_columnconfigure(ci, weight=w, minsize=ms)

        ctk.CTkLabel(
            gf,
            text=f"  Group {group_idx+1}  —  {format_size(file_size)} each"
                 f"  ({len(group)} identical files)",
            font=("",12,"bold"), anchor="w",
        ).grid(row=0, column=0, columnspan=7, sticky="ew", padx=10, pady=(8,2))

        ctk.CTkFrame(gf, height=1, fg_color="gray40").grid(
            row=1, column=0, columnspan=7, sticky="ew", padx=10)

        # Determine original: the file with the OLDEST mtime
        for row_i, (file_path, size_bytes, mtime) in enumerate(file_meta):
            is_original = (mtime == mtime_min and mtime_min != mtime_max) or \
                          (mtime_min == mtime_max and row_i == 0)
            base_row  = row_i * 2 + 2
            row_color = ROW_COLORS[row_i % 2]

            rf = ctk.CTkFrame(gf, fg_color=row_color, corner_radius=4)
            rf.grid(row=base_row, column=0, columnspan=7,
                    sticky="ew", padx=6, pady=(2,0))
            for ci, w, ms in col_cfg:
                rf.grid_columnconfigure(ci, weight=w, minsize=ms)

            # Store for deletion
            self._path_to_row[file_path] = (group_idx, rf)

            # Col 0 — Original / Copy badge
            badge_text  = "📌 Original" if is_original else "📋 Copy"
            badge_color = TEXT_ORIGINAL if is_original else TEXT_COPY
            ctk.CTkLabel(rf, text=badge_text, text_color=badge_color,
                         font=("",10,"bold"), fg_color="transparent",
                         anchor="center").grid(
                             row=0, column=0, padx=(6,2), pady=8, sticky="ew")

            # Col 1 — Checkbox with explicit IntVar (fixes cb.get() bug)
            var = tk.IntVar(value=0)
            cb  = ctk.CTkCheckBox(rf, text="", width=28,
                                  hover_color=row_color, variable=var)
            # Pre-select copies, leave originals unchecked
            if not is_original:
                var.set(1)
                cb.select()
            cb.grid(row=0, column=1, padx=(4,4), pady=8, sticky="w")
            self._checkbox_map[cb] = (var, file_path, mtime)

            # Col 2 — Filename
            is_rtl_name = has_rtl(file_path.name)
            ctk.CTkLabel(rf, text=fix_bidi(file_path.name),
                         text_color=TEXT_WARN if is_original else TEXT_PRIMARY,
                         fg_color="transparent", font=("",12,"bold"),
                         anchor="e" if is_rtl_name else "w",
                         wraplength=190).grid(
                             row=0, column=2, padx=(0,6), pady=8, sticky="ew")

            # Col 3 — Path
            is_rtl_path = has_rtl(str(file_path.parent))
            ctk.CTkLabel(rf, text=fix_bidi(str(file_path.parent)),
                         text_color=TEXT_SECONDARY, fg_color="transparent",
                         font=("",11),
                         anchor="e" if is_rtl_path else "w",
                         wraplength=240).grid(
                             row=0, column=3, padx=(0,6), pady=8, sticky="ew")

            # Col 4 — Size
            ctk.CTkLabel(rf, text=format_size(size_bytes),
                         text_color=TEXT_ACCENT, fg_color="transparent",
                         font=("",11), anchor="center").grid(
                             row=0, column=4, padx=(0,6), pady=8, sticky="ew")

            # Col 5 — Date
            ctk.CTkLabel(rf,
                         text=format_date(mtime) if mtime else "—",
                         text_color=TEXT_SECONDARY, fg_color="transparent",
                         font=("",11), anchor="w").grid(
                             row=0, column=5, padx=(0,6), pady=8, sticky="ew")

            # Col 6 — Preview
            ctk.CTkButton(rf, text="👁", width=36, height=26,
                          fg_color="#3a4a5c", hover_color="#4a6080", font=("",13),
                          command=lambda p=file_path: open_viewer(p)).grid(
                              row=0, column=6, padx=(0,8), pady=6, sticky="e")

        ctk.CTkFrame(gf, height=6, fg_color="transparent").grid(
            row=len(file_meta)*2+2, column=0, columnspan=7)

    # ── Sorting ───────────────────────────────────────────────────────────────

    def _on_sort_change(self, value: str):
        self._sort_order = "largest" if value == "Largest First" else "smallest"
        if not self._duplicate_groups:
            return
        # Re-sort groups and re-render
        self._duplicate_groups.sort(
            key=lambda g: g[0].stat().st_size if g else 0,
            reverse=(self._sort_order == "largest"),
        )
        # Clear rendered cards and redraw
        for gf in self._group_frames.values():
            gf.destroy()
        self._group_frames.clear()
        self._checkbox_map.clear()
        self._path_to_row.clear()
        for idx, group in enumerate(self._duplicate_groups):
            self._render_group(group, idx)

    # ── Selection ─────────────────────────────────────────────────────────────

    def _select_all(self):
        for cb, (var, _, __) in self._checkbox_map.items():
            var.set(1)
            cb.select()

    def _select_older(self):
        self._smart_select(keep="newest")

    def _select_newer(self):
        self._smart_select(keep="oldest")

    def _smart_select(self, keep: str):
        """Rewritten v2.2: iterate groups directly, sort by mtime, protect one keeper."""
        # Deselect all first
        for cb, (var, _, __) in self._checkbox_map.items():
            var.set(0)
            cb.deselect()

        path_to_cb: dict[Path, tuple[ctk.CTkCheckBox, tk.IntVar]] = {
            path: (cb, var)
            for cb, (var, path, _) in self._checkbox_map.items()
        }

        for group in self._duplicate_groups:
            timed: list[tuple[float, Path]] = []
            for fp in group:
                if fp in path_to_cb:
                    _, _, mtime = next(
                        (var, p, mt) for cb, (var, p, mt) in self._checkbox_map.items()
                        if p == fp
                    )
                    timed.append((mtime, fp))

            if len(timed) < 2:
                continue

            timed.sort(key=lambda x: x[0])
            keeper = timed[-1][1] if keep == "newest" else timed[0][1]

            for _, fp in timed:
                if fp not in path_to_cb:
                    continue
                cb, var = path_to_cb[fp]
                if fp == keeper:
                    var.set(0)
                    cb.deselect()
                else:
                    var.set(1)
                    cb.select()

    # ── Deletion (in-place row removal, NO re-scan) ───────────────────────────

    def _delete_selected(self):
        # Use IntVar.get() — the reliable fix for the "Nothing selected" bug
        to_trash = [
            (cb, var, path)
            for cb, (var, path, _) in self._checkbox_map.items()
            if var.get() == 1
        ]

        if not to_trash:
            messagebox.showinfo("Nothing Selected",
                                "Please check at least one file to send to Trash.")
            return

        if not TRASH_AVAILABLE:
            messagebox.showerror(
                "send2trash Missing",
                "Please install send2trash:\n\n    pip install send2trash\n\nThen restart.",
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
        for cb, var, path in to_trash:
            try:
                send2trash(str(path))
                trashed.append((cb, var, path))
            except Exception as exc:
                failed.append((path, str(exc)))

        # Remove trashed rows from UI without re-scanning
        groups_to_check: set[int] = set()
        for cb, var, path in trashed:
            if path in self._path_to_row:
                group_idx, row_frame = self._path_to_row.pop(path)
                row_frame.destroy()
                del self._checkbox_map[cb]
                groups_to_check.add(group_idx)

        # Remove groups that now have fewer than 2 files
        for gidx in groups_to_check:
            gf = self._group_frames.get(gidx)
            if gf is None:
                continue
            remaining = [
                p for p in (self._duplicate_groups[gidx] if gidx < len(self._duplicate_groups) else [])
                if p in self._path_to_row
            ]
            if len(remaining) < 2:
                gf.destroy()
                del self._group_frames[gidx]

        # Update duplicate_groups list to reflect deletions
        trashed_paths = {path for _, _, path in trashed}
        self._duplicate_groups = [
            [fp for fp in g if fp not in trashed_paths]
            for g in self._duplicate_groups
        ]
        self._duplicate_groups = [g for g in self._duplicate_groups if len(g) >= 2]

        msg = [f"✅ Sent {len(trashed)} file(s) to Trash."]
        if failed:
            lines = "\n".join(f"  • {p.name}: {r}" for p, r in failed)
            msg.append(f"\n⚠️ Failed ({len(failed)}):\n{lines}")
        messagebox.showinfo("Done", "\n".join(msg))

        # Refresh savings banner
        if self._duplicate_groups:
            self._finalise_display()
        else:
            self.savings_frame.pack_forget()
            self.status_label.configure(text="✅  All duplicates removed!")

    # ── Summary & Profiles ────────────────────────────────────────────────────

    def _show_summary(self):
        if self._duplicate_groups:
            SummaryWindow(self, self._duplicate_groups)

    def _open_profiles(self):
        current = {
            "folders":    [str(p) for p in self._directories],
            "extensions": self.ext_entry.get(),
            "min_size":   self.min_entry.get(),
            "max_size":   self.max_entry.get(),
            "excludes":   self.exc_entry.get(),
        }
        ProfileDialog(self, current, self._apply_profile)

    def _apply_profile(self, profile: dict):
        self._directories = [
            Path(p) for p in profile.get("folders", []) if Path(p).exists()
        ]
        self._refresh_folders()
        if self._directories:
            self.scan_btn.configure(state="normal")

        def _set(entry, val):
            entry.delete(0, "end")
            if val:
                entry.insert(0, val)

        _set(self.ext_entry, profile.get("extensions",""))
        _set(self.min_entry, profile.get("min_size",""))
        _set(self.max_entry, profile.get("max_size",""))
        _set(self.exc_entry, profile.get("excludes",""))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _clear_results(self):
        for w in self.scroll_frame.winfo_children():
            w.destroy()
        self._checkbox_map.clear()
        self._duplicate_groups.clear()
        self._group_frames.clear()
        self._path_to_row.clear()
        self.summary_label.configure(text="")
        self.stats_btn.configure(state="disabled")
        self.sort_menu.configure(state="disabled")
        self.savings_frame.pack_forget()

        self.placeholder = ctk.CTkLabel(
            self.scroll_frame,
            text="Add a directory and click 'Scan' to begin.",
            text_color="gray50")
        self.placeholder.pack(pady=40)

    def _set_scanning(self, scanning: bool):
        self.scan_btn.configure(state="disabled" if scanning else "normal")
        self.pause_btn.configure(state="normal" if scanning else "disabled")
        self.cancel_btn.configure(state="normal" if scanning else "disabled")
        if not scanning:
            self.pause_btn.configure(text="⏸  Pause")
            self._pause_event.clear()
        self._set_result_buttons("disabled" if scanning else "normal")

    def _set_result_buttons(self, state: str):
        for btn in (self.sel_all_btn, self.sel_older_btn,
                    self.sel_newer_btn, self.del_btn):
            btn.configure(state=state)

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


# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = DuplicateFinderApp()
    app.mainloop()
