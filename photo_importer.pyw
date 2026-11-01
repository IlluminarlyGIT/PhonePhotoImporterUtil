#!/usr/bin/env python3
"""
Phone Photo Importer & Sorter
=============================
A modern, production-ready desktop utility built with Python and Tkinter
to recursively scan, organize, and sort JPEG photos from an Android device
(via ADB or local/mounted folders) into a customizable date-structured external drive.

Key Capabilities:
- Dual Source Modes:
    1. Local / Mapped PC Folder (e.g. drive letter, SD card, Wi-Fi SMB).
    2. Android USB via ADB (Direct USB connection with auto-discovery).
- Pre-check & Smart Thumbnail Protection:
    * Quick one-click photo counter before transfer.
    * Automatically filters out .thumbnails, .trashed, .cache, and hidden folders.
- EXIF DateTimeOriginal extraction with fallback to filesystem timestamps.
- Modular Folder Hierarchy Picker:
    * Toggle Year, Month, Week, Day individually or nest them all.
    * Human-readable names (e.g., "June 2026", "Week 24", "June 10") instead of numeric codes.
    * Live interactive folder path preview before running.
- Safe Copy vs. Verified Move operations.
- Non-destructive file collision handling with sequential suffixes.
- Responsive Tkinter GUI powered by Python multithreading and a thread-safe Queue.
"""

import os
import sys
import shutil
import queue
import threading
import datetime
import subprocess
import tempfile
import re
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# Ensure Pillow is available for EXIF extraction
try:
    from PIL import Image, ExifTags
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


# ==============================================================================
# ADB (ANDROID DEBUG BRIDGE) HELPERS
# ==============================================================================

def find_adb_executable() -> str | None:
    """
    Locates the adb executable on the system:
    1. Checks the system PATH.
    2. Checks standard Android SDK platform-tools installation paths.
    """
    found = shutil.which("adb")
    if found:
        return found

    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Android", "platform-tools", "adb.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Android", "platform-tools", "adb.exe"),
        r"C:\platform-tools\adb.exe",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate

    return None


def run_adb_command(adb_bin: str, args: list[str], timeout: int = 15) -> tuple[int, str, str]:
    """
    Runs an ADB subprocess quietly without popping up a console window.
    """
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
    cmd = [adb_bin] + args
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=timeout,
            creationflags=creation_flags
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out."
    except Exception as e:
        return -1, "", str(e)


def get_connected_adb_devices(adb_bin: str) -> list[dict]:
    """
    Parses 'adb devices -l' output to return all connected Android devices and their status.
    """
    code, stdout, _ = run_adb_command(adb_bin, ["devices", "-l"])
    if code != 0:
        return []

    devices = []
    lines = stdout.splitlines()
    for line in lines[1:]:  # skip 'List of devices attached'
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            serial = parts[0]
            state = parts[1]
            info = " ".join(parts[2:])

            # Extract model name if available
            model_match = re.search(r"model:([^\s]+)", info)
            model_name = model_match.group(1).replace("_", " ") if model_match else serial

            devices.append({
                "serial": serial,
                "state": state,
                "model": model_name,
                "raw": line
            })
    return devices


def adb_scan_camera_photos(adb_bin: str, serial: str | None, phone_folder: str) -> tuple[list[str], int]:
    """
    Finds all valid JPEG camera photos on the Android device.
    Pre-check & Safety Filtering:
    - Filters for .jpg and .jpeg files only.
    - Strictly ignores .thumbnails, .trashed, .cache, and any hidden folders or files.
    Returns: (list_of_valid_remote_paths, ignored_thumbnail_count)
    """
    cmd_args = []
    if serial:
        cmd_args.extend(["-s", serial])
    cmd_args.extend(["shell", "find", f'"{phone_folder}"', "-type", "f"])

    code, stdout, _ = run_adb_command(adb_bin, cmd_args, timeout=30)
    if code != 0:
        return [], 0

    valid_photos: list[str] = []
    ignored_count = 0

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if not line.lower().endswith((".jpg", ".jpeg")):
            continue

        parts = line.split("/")
        # Smart Filter: skip hidden dirs/files (.thumbnails, .trashed, .pending, etc.)
        # and anything with 'thumbnail' or 'cache' in path
        is_hidden_or_thumbnail = (
            any(part.startswith(".") for part in parts) or
            any(ign in part.lower() for part in parts for ign in ("thumbnail", "cache", "trash"))
        )

        if is_hidden_or_thumbnail:
            ignored_count += 1
            continue

        valid_photos.append(line)

    return valid_photos, ignored_count


# ==============================================================================
# CORE LOGIC & HELPER FUNCTIONS
# ==============================================================================

def extract_photo_date(filepath: str) -> tuple[datetime.datetime, str]:
    """
    Reads the embedded EXIF metadata to determine when the photo was taken.
    Specifically searches for 'DateTimeOriginal' (tag 36867) or 'DateTimeDigitized'.

    Threading & EXIF Logic:
    - Pillow 6.0+ stores EXIF sub-tags under IFD.Exif. We inspect both the root
      EXIF dictionary and the EXIF sub-IFD for maximum compatibility.
    - If EXIF metadata is missing, corrupted, or unparseable, this function
      gracefully falls back to the filesystem modification/creation timestamp.

    Returns:
        tuple: (datetime_object, source_description_string)
    """
    raw_date_str = None

    if HAS_PILLOW:
        try:
            with Image.open(filepath) as img:
                exif = img.getexif()
                if exif:
                    # 1. Search in modern Pillow IFD.Exif sub-dictionary
                    try:
                        exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
                        raw_date_str = exif_ifd.get(36867) or exif_ifd.get(36868)
                    except Exception:
                        pass

                    # 2. Search root EXIF dictionary (DateTimeOriginal or standard DateTime)
                    if not raw_date_str:
                        raw_date_str = exif.get(36867) or exif.get(306)

                    # 3. Fallback for older Pillow versions via private _getexif()
                    if not raw_date_str and hasattr(img, "_getexif"):
                        legacy_exif = img._getexif()
                        if legacy_exif:
                            raw_date_str = legacy_exif.get(36867) or legacy_exif.get(36868) or legacy_exif.get(306)
        except Exception:
            pass

    # Attempt to parse EXIF date string (typically "YYYY:MM:DD HH:MM:SS")
    if raw_date_str and isinstance(raw_date_str, str):
        cleaned_str = raw_date_str.strip().replace("\x00", "")
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d", "%Y-%m-%d"):
            try:
                parsed_dt = datetime.datetime.strptime(cleaned_str, fmt)
                if parsed_dt.year > 1970:
                    return parsed_dt, "EXIF DateTimeOriginal"
            except ValueError:
                continue

    # Graceful Fallback: Use filesystem timestamps
    try:
        stat_info = os.stat(filepath)
        timestamps = [t for t in (stat_info.st_mtime, getattr(stat_info, "st_ctime", None)) if t and t > 0]
        chosen_timestamp = min(timestamps) if timestamps else stat_info.st_mtime
        return datetime.datetime.fromtimestamp(chosen_timestamp), "File Timestamp (Fallback)"
    except Exception:
        return datetime.datetime.now(), "Current Timestamp (Fallback)"


def build_custom_subfolder(
    photo_date: datetime.datetime,
    include_year: bool,
    include_month: bool,
    include_week: bool,
    include_day: bool
) -> str:
    """
    Constructs a human-readable nested relative subfolder path based on the user's
    selected hierarchy levels. Avoids technical formats like 'YYYY-MM-DD'.

    Formatting rules:
    - Year: "2026"
    - Month: "June" (when nested under Year) or "June 2026" (standalone)
    - Week: "Week 24" (when nested under Year) or "Week 24, 2026" (standalone)
    - Day: "June 10" (when nested under Year/Month) or "June 10, 2026" (standalone)
    """
    parts = []

    if include_year:
        parts.append(photo_date.strftime("%Y"))

    if include_month:
        if include_year:
            parts.append(photo_date.strftime("%B"))
        else:
            parts.append(photo_date.strftime("%B %Y"))

    if include_week:
        iso_year, iso_week, _ = photo_date.isocalendar()
        if include_year:
            parts.append(f"Week {iso_week:02d}")
        else:
            parts.append(f"Week {iso_week:02d}, {iso_year}")

    if include_day:
        if include_month or include_year:
            parts.append(f"{photo_date.strftime('%B')} {photo_date.day}")
        else:
            parts.append(f"{photo_date.strftime('%B')} {photo_date.day}, {photo_date.strftime('%Y')}")

    return os.path.join(*parts) if parts else ""


def resolve_collision_path(target_folder: str, filename: str, src_size: int) -> tuple[str, bool]:
    """
    Evaluates potential file collisions in target folder.
    - If a file with the same name exists:
      * If sizes match exactly: skip it to save time (returns is_skip=True).
      * If sizes differ: append sequential suffix (_1, _2, etc.) until a non-conflicting
        slot is found or an identical file size is detected.

    Returns:
        tuple: (destination_filepath, is_skip_flag)
    """
    candidate_path = os.path.join(target_folder, filename)

    if not os.path.exists(candidate_path):
        return candidate_path, False

    try:
        dest_size = os.path.getsize(candidate_path)
        if dest_size == src_size:
            return candidate_path, True
    except OSError:
        pass

    stem, ext = os.path.splitext(filename)
    counter = 1

    while True:
        suffixed_name = f"{stem}_{counter}{ext}"
        candidate_path = os.path.join(target_folder, suffixed_name)

        if not os.path.exists(candidate_path):
            return candidate_path, False

        try:
            if os.path.getsize(candidate_path) == src_size:
                return candidate_path, True
        except OSError:
            pass

        counter += 1


# ==============================================================================
# MAIN APPLICATION GUI CLASS
# ==============================================================================

class PhotoImporterApp:
    """
    Main Tkinter GUI controller for the Phone Photo Importer utility.
    """

    PHONE_PRESET_FOLDERS = [
        "/sdcard/DCIM/Camera",
        "/sdcard/DCIM",
        "/sdcard/Pictures",
        "/sdcard/Download",
    ]

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Phone Photo Importer & Sorter")
        self.root.geometry("880x820")
        self.root.minsize(760, 700)

        # Threading and Communication primitives
        self.msg_queue: queue.Queue = queue.Queue()
        self.cancel_event: threading.Event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.is_running: bool = False

        # ADB binary auto-detection
        self.adb_bin = find_adb_executable()
        self.adb_devices: list[dict] = []
        self.selected_device_serial: str | None = None

        # Source mode: "LOCAL" or "ADB"
        self.source_mode_var = tk.StringVar(value="ADB" if self.adb_bin else "LOCAL")

        # UI State Variables
        self.source_dir_var = tk.StringVar(value="")
        self.phone_dir_var = tk.StringVar(value=self.PHONE_PRESET_FOLDERS[0])
        self.target_dir_var = tk.StringVar(value="")
        self.adb_status_var = tk.StringVar(value="Searching for ADB...")
        self.precheck_status_var = tk.StringVar(value="Not checked yet")

        # Hierarchy builder variables
        self.opt_year_var = tk.BooleanVar(value=True)
        self.opt_month_var = tk.BooleanVar(value=True)
        self.opt_week_var = tk.BooleanVar(value=False)
        self.opt_day_var = tk.BooleanVar(value=True)

        self.preview_path_var = tk.StringVar(value="")
        self.copy_mode_var = tk.BooleanVar(value=True)  # Default: Copy (safe)
        self.status_var = tk.StringVar(value="Ready. Configure your source and target folders to begin.")
        self.progress_percent_var = tk.StringVar(value="0%")

        # Build GUI Layout & Apply Modern Styling
        self._apply_styles()
        self._build_widgets()

        # Initial checks
        self._update_preview()
        self._on_source_mode_changed()
        if self.adb_bin:
            self.refresh_adb_devices()

        # Start periodic queue polling on the Tkinter main thread
        self.root.after(50, self._process_queue)

        # Inform user of environment status
        if not HAS_PILLOW:
            self._log("WARNING", "Pillow (PIL) is not installed. Falling back to filesystem timestamps.")
        if self.adb_bin:
            self._log("INFO", f"ADB detected at: {self.adb_bin}")
        else:
            self._log("WARNING", "ADB not found. You can still use the 'Local Folder' mode for mounted drives.")

    def _apply_styles(self):
        """Configures modern styling for ttk widgets."""
        self.style = ttk.Style()
        available_themes = self.style.theme_names()
        if "vista" in available_themes:
            self.style.theme_use("vista")
        elif "clam" in available_themes:
            self.style.theme_use("clam")

        self.bg_color = "#F8FAFC"
        self.card_bg = "#FFFFFF"
        self.border_color = "#E2E8F0"
        self.accent_color = "#2563EB"
        self.text_dark = "#1E293B"
        self.text_muted = "#64748B"

        self.root.configure(bg=self.bg_color)

        self.style.configure(".", font=("Segoe UI", 9))
        self.style.configure("TLabelframe", background=self.card_bg)
        self.style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"), foreground=self.text_dark, background=self.card_bg)
        self.style.configure("TFrame", background=self.card_bg)
        self.style.configure("TLabel", background=self.card_bg, foreground=self.text_dark)
        self.style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"), foreground="#0F172A", background=self.bg_color)
        self.style.configure("SubHeader.TLabel", font=("Segoe UI", 9), foreground=self.text_muted, background=self.bg_color)
        self.style.configure("Status.TLabel", font=("Segoe UI", 9, "italic"), foreground="#334155", background=self.card_bg)
        self.style.configure("TCheckbutton", background=self.card_bg, font=("Segoe UI", 9))
        self.style.configure("Level.TCheckbutton", background=self.card_bg, font=("Segoe UI", 9, "bold"))
        self.style.configure("TRadiobutton", background=self.card_bg, font=("Segoe UI", 9, "bold"))
        self.style.configure("Horizontal.TProgressbar", thickness=16)

    def _build_widgets(self):
        """Constructs all UI panels and widgets."""
        main_container = tk.Frame(self.root, bg=self.bg_color, padx=16, pady=8)
        main_container.pack(fill=tk.BOTH, expand=True)

        # ---------------- HEADER ----------------
        header_frame = tk.Frame(main_container, bg=self.bg_color)
        header_frame.pack(fill=tk.X, pady=(0, 6))

        title_lbl = ttk.Label(header_frame, text="📸 Phone Photo Importer & Sorter", style="Header.TLabel")
        title_lbl.pack(anchor="w")

        subtitle_lbl = ttk.Label(
            header_frame,
            text="Organize photos directly from Android USB (ADB) or PC folders into clean, human-readable date archives.",
            style="SubHeader.TLabel"
        )
        subtitle_lbl.pack(anchor="w", pady=(1, 0))

        # ---------------- SECTION 1: SOURCE & DESTINATION ----------------
        dir_card = ttk.LabelFrame(main_container, text=" 1. Source & Target Selection ", padding=(12, 6))
        dir_card.pack(fill=tk.X, pady=(0, 6))

        # Source Mode Switcher (Radiobuttons)
        mode_select_row = tk.Frame(dir_card, bg=self.card_bg)
        mode_select_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(mode_select_row, text="Source Method:", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(0, 12))

        self.rb_mode_adb = ttk.Radiobutton(
            mode_select_row,
            text="📱 Android Phone via USB (ADB)",
            value="ADB",
            variable=self.source_mode_var,
            command=self._on_source_mode_changed
        )
        self.rb_mode_adb.pack(side=tk.LEFT, padx=(0, 16))

        self.rb_mode_local = ttk.Radiobutton(
            mode_select_row,
            text="📁 Local / Mapped Folder (PC / SD Card)",
            value="LOCAL",
            variable=self.source_mode_var,
            command=self._on_source_mode_changed
        )
        self.rb_mode_local.pack(side=tk.LEFT)

        # --- Sub-panel: Local Folder Picker ---
        self.local_frame = tk.Frame(dir_card, bg=self.card_bg)
        local_lbl_row = tk.Frame(self.local_frame, bg=self.card_bg)
        local_lbl_row.pack(fill=tk.X, pady=(1, 2))
        ttk.Label(local_lbl_row, text="Source Directory:", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)

        local_input_row = tk.Frame(self.local_frame, bg=self.card_bg)
        local_input_row.pack(fill=tk.X, pady=(0, 4))
        self.src_entry = ttk.Entry(local_input_row, textvariable=self.source_dir_var, font=("Segoe UI", 9))
        self.src_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self.btn_browse_src = ttk.Button(local_input_row, text="Browse...", command=self._browse_source)
        self.btn_browse_src.pack(side=tk.RIGHT)

        # --- Sub-panel: ADB Phone Controls ---
        self.adb_frame = tk.Frame(dir_card, bg=self.card_bg)

        # Device connection status row
        device_status_row = tk.Frame(self.adb_frame, bg=self.card_bg)
        device_status_row.pack(fill=tk.X, pady=(1, 4))

        self.lbl_adb_status = tk.Label(
            device_status_row,
            textvariable=self.adb_status_var,
            font=("Segoe UI", 9, "bold"),
            fg="#1E293B",
            bg=self.card_bg,
            anchor="w"
        )
        self.lbl_adb_status.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.btn_refresh_adb = ttk.Button(device_status_row, text="🔄 Detect Phone", command=self.refresh_adb_devices)
        self.btn_refresh_adb.pack(side=tk.RIGHT)

        # Phone Folder Picker & Pre-check row
        phone_folder_row = tk.Frame(self.adb_frame, bg=self.card_bg)
        phone_folder_row.pack(fill=tk.X, pady=(2, 4))
        ttk.Label(phone_folder_row, text="Phone Camera Folder:", width=20, anchor="w").pack(side=tk.LEFT)

        self.combo_phone_dir = ttk.Combobox(
            phone_folder_row,
            textvariable=self.phone_dir_var,
            values=self.PHONE_PRESET_FOLDERS,
            font=("Segoe UI", 9)
        )
        self.combo_phone_dir.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        self.btn_precheck = tk.Button(
            phone_folder_row,
            text="🔍 Check & Count Files",
            bg="#0284C7",
            fg="white",
            activebackground="#0369A1",
            activeforeground="white",
            font=("Segoe UI", 9, "bold"),
            padx=10,
            pady=2,
            relief=tk.FLAT,
            cursor="hand2",
            command=self.run_precheck_count
        )
        self.btn_precheck.pack(side=tk.RIGHT)

        # Precheck result banner
        self.precheck_banner = tk.Label(
            self.adb_frame,
            textvariable=self.precheck_status_var,
            font=("Segoe UI", 8, "italic"),
            fg="#0369A1",
            bg="#E0F2FE",
            padx=6,
            pady=2,
            anchor="w"
        )
        self.precheck_banner.pack(fill=tk.X, pady=(1, 2))

        # --- Target Storage Folder Row (Shared) ---
        dest_label_row = tk.Frame(dir_card, bg=self.card_bg)
        dest_label_row.pack(fill=tk.X, pady=(4, 2))
        ttk.Label(dest_label_row, text="Target Storage Folder (External Drive / PC):", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)

        dest_input_row = tk.Frame(dir_card, bg=self.card_bg)
        dest_input_row.pack(fill=tk.X, pady=(0, 2))
        self.dest_entry = ttk.Entry(dest_input_row, textvariable=self.target_dir_var, font=("Segoe UI", 9))
        self.dest_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self.btn_browse_dest = ttk.Button(dest_input_row, text="Browse Target...", command=self._browse_target)
        self.btn_browse_dest.pack(side=tk.RIGHT)

        # ---------------- SECTION 2: FOLDER HIERARCHY BUILDER ----------------
        struct_card = ttk.LabelFrame(main_container, text=" 2. Folder Hierarchy Builder ", padding=(12, 6))
        struct_card.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(
            struct_card,
            text="Select which date levels to nest (Human-readable names used automatically):",
            foreground=self.text_dark
        ).pack(anchor="w", pady=(0, 3))

        # Checkboxes Row
        checks_row = tk.Frame(struct_card, bg=self.card_bg)
        checks_row.pack(fill=tk.X, pady=(1, 4))

        self.chk_year = ttk.Checkbutton(
            checks_row,
            text="Year  (e.g., 2026)",
            variable=self.opt_year_var,
            style="Level.TCheckbutton",
            command=self._update_preview
        )
        self.chk_year.pack(side=tk.LEFT, padx=(0, 16))

        self.chk_month = ttk.Checkbutton(
            checks_row,
            text="Month  (e.g., June)",
            variable=self.opt_month_var,
            style="Level.TCheckbutton",
            command=self._update_preview
        )
        self.chk_month.pack(side=tk.LEFT, padx=(0, 16))

        self.chk_week = ttk.Checkbutton(
            checks_row,
            text="Week  (e.g., Week 24)",
            variable=self.opt_week_var,
            style="Level.TCheckbutton",
            command=self._update_preview
        )
        self.chk_week.pack(side=tk.LEFT, padx=(0, 16))

        self.chk_day = ttk.Checkbutton(
            checks_row,
            text="Day  (e.g., June 10)",
            variable=self.opt_day_var,
            style="Level.TCheckbutton",
            command=self._update_preview
        )
        self.chk_day.pack(side=tk.LEFT)

        # Presets Row
        preset_row = tk.Frame(struct_card, bg=self.card_bg)
        preset_row.pack(fill=tk.X, pady=(1, 4))
        ttk.Label(preset_row, text="Quick Presets:", font=("Segoe UI", 8, "bold"), foreground=self.text_muted).pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(preset_row, text="All (Fully Nested)", command=self._preset_all).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_row, text="Year + Month + Day", command=self._preset_year_month_day).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_row, text="Year + Month", command=self._preset_year_month).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_row, text="Month Only", command=self._preset_month).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_row, text="Day Only", command=self._preset_day).pack(side=tk.LEFT, padx=(0, 4))

        # Live Preview Banner
        preview_container = tk.Frame(struct_card, bg="#F1F5F9", relief=tk.SOLID, bd=1, padx=8, pady=4)
        preview_container.pack(fill=tk.X, pady=(2, 2))

        tk.Label(
            preview_container,
            text="📁 LIVE FOLDER STRUCTURE PREVIEW:",
            font=("Segoe UI", 8, "bold"),
            fg="#475569",
            bg="#F1F5F9"
        ).pack(anchor="w")

        self.preview_lbl = tk.Label(
            preview_container,
            textvariable=self.preview_path_var,
            font=("Consolas", 9, "bold"),
            fg="#1D4ED8",
            bg="#F1F5F9",
            anchor="w"
        )
        self.preview_lbl.pack(anchor="w", pady=(1, 0))

        # ---------------- SECTION 3: TRANSFER MODE ----------------
        opt_card = ttk.LabelFrame(main_container, text=" 3. Transfer Mode ", padding=(12, 5))
        opt_card.pack(fill=tk.X, pady=(0, 6))

        mode_row = tk.Frame(opt_card, bg=self.card_bg)
        mode_row.pack(fill=tk.X, pady=(1, 1))
        self.chk_transfer_mode = ttk.Checkbutton(
            mode_row,
            text="Copy files (Keep originals safe on phone / source)",
            variable=self.copy_mode_var,
            command=self._on_mode_toggled
        )
        self.chk_transfer_mode.pack(side=tk.LEFT)

        self.mode_hint_lbl = ttk.Label(
            opt_card,
            text="Safe mode: Originals are preserved unchanged on your phone.",
            foreground="#16A34A",
            font=("Segoe UI", 8, "italic")
        )
        self.mode_hint_lbl.pack(anchor="w", padx=(24, 0), pady=(0, 1))

        # ---------------- SECTION 4: PROGRESS & CONTROLS ----------------
        progress_card = ttk.LabelFrame(main_container, text=" 4. Execution & Status ", padding=(12, 6))
        progress_card.pack(fill=tk.X, pady=(0, 6))

        btn_row = tk.Frame(progress_card, bg=self.card_bg)
        btn_row.pack(fill=tk.X, pady=(0, 4))

        self.btn_start = tk.Button(
            btn_row,
            text="▶  Start Photo Import",
            bg="#2563EB",
            fg="white",
            activebackground="#1D4ED8",
            activeforeground="white",
            font=("Segoe UI", 10, "bold"),
            padx=16,
            pady=5,
            relief=tk.FLAT,
            cursor="hand2",
            command=self.start_import
        )
        self.btn_start.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_cancel = tk.Button(
            btn_row,
            text="⏹  Cancel Process",
            bg="#E2E8F0",
            fg="#475569",
            activebackground="#CBD5E1",
            activeforeground="#1E293B",
            font=("Segoe UI", 9, "bold"),
            padx=14,
            pady=5,
            relief=tk.FLAT,
            state=tk.DISABLED,
            command=self.cancel_import
        )
        self.btn_cancel.pack(side=tk.LEFT)

        status_row = tk.Frame(progress_card, bg=self.card_bg)
        status_row.pack(fill=tk.X, pady=(2, 2))
        self.lbl_status = ttk.Label(status_row, textvariable=self.status_var, style="Status.TLabel")
        self.lbl_status.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.lbl_percent = ttk.Label(status_row, textvariable=self.progress_percent_var, font=("Segoe UI", 9, "bold"))
        self.lbl_percent.pack(side=tk.RIGHT)

        self.progress_bar = ttk.Progressbar(
            progress_card,
            orient=tk.HORIZONTAL,
            mode="determinate",
            style="Horizontal.TProgressbar"
        )
        self.progress_bar.pack(fill=tk.X, pady=(2, 1))

        # ---------------- SECTION 5: ACTIVITY LOG ----------------
        log_frame = tk.Frame(main_container, bg=self.bg_color)
        log_frame.pack(fill=tk.BOTH, expand=True)

        log_header = tk.Frame(log_frame, bg=self.bg_color)
        log_header.pack(fill=tk.X, pady=(1, 2))
        ttk.Label(log_header, text="Activity Log & Details:", font=("Segoe UI", 9, "bold"), background=self.bg_color).pack(side=tk.LEFT)

        btn_clear_log = ttk.Button(log_header, text="Clear Log", command=self._clear_log)
        btn_clear_log.pack(side=tk.RIGHT)

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            wrap=tk.WORD,
            font=("Consolas", 9),
            bg="#1E293B",
            fg="#E2E8F0",
            insertbackground="#FFFFFF",
            relief=tk.FLAT,
            padx=8,
            pady=6
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self.log_text.tag_configure("INFO", foreground="#93C5FD")
        self.log_text.tag_configure("SUCCESS", foreground="#4ADE80")
        self.log_text.tag_configure("SKIP", foreground="#FCD34D")
        self.log_text.tag_configure("WARNING", foreground="#FB923C")
        self.log_text.tag_configure("ERROR", foreground="#F87171")
        self.log_text.tag_configure("TIMESTAMP", foreground="#94A3B8")

    # ---------------- SOURCE MODE TOGGLE & ADB DETECTION ----------------

    def _on_source_mode_changed(self):
        """Switches between Local Folder view and ADB Phone view."""
        mode = self.source_mode_var.get()
        if mode == "ADB":
            self.local_frame.pack_forget()
            self.adb_frame.pack(fill=tk.X, pady=(0, 6))
            if not self.adb_devices and self.adb_bin:
                self.refresh_adb_devices()
        else:
            self.adb_frame.pack_forget()
            self.local_frame.pack(fill=tk.X, pady=(0, 6))

    def refresh_adb_devices(self):
        """Queries ADB and updates the device status label."""
        if not self.adb_bin:
            self.adb_bin = find_adb_executable()

        if not self.adb_bin:
            self.adb_status_var.set("❌ ADB not found. Please install Android Platform Tools.")
            self.lbl_adb_status.config(fg="#DC2626")
            return

        self.adb_devices = get_connected_adb_devices(self.adb_bin)

        if not self.adb_devices:
            self.selected_device_serial = None
            self.adb_status_var.set("❌ No Android device found. Plug phone via USB & enable 'USB Debugging'.")
            self.lbl_adb_status.config(fg="#DC2626")
            self._log("WARNING", "No device found. Make sure USB Debugging is turned on in Android Developer Options.")
        else:
            first_dev = self.adb_devices[0]
            self.selected_device_serial = first_dev["serial"]
            state = first_dev["state"]

            if state == "device":
                self.adb_status_var.set(f"✅ Connected: {first_dev['model']} ({first_dev['serial']})")
                self.lbl_adb_status.config(fg="#16A34A")
                self._log("SUCCESS", f"Phone connected: {first_dev['model']} [Serial: {first_dev['serial']}]")
            elif state == "unauthorized":
                self.adb_status_var.set("⚠️ Phone Unauthorized! Unlock screen & tap 'Allow USB debugging'.")
                self.lbl_adb_status.config(fg="#D97706")
                self._log("WARNING", "Device is unauthorized. Check your phone's screen and tap 'Always allow'.")
            else:
                self.adb_status_var.set(f"⚠️ Device state: {state} ({first_dev['serial']})")
                self.lbl_adb_status.config(fg="#D97706")

    def run_precheck_count(self):
        """
        Runs a quick count on the phone directory over ADB.
        Strictly ignores .thumbnails, .trashed, and hidden folders.
        """
        if not self.adb_bin or not self.selected_device_serial:
            messagebox.showwarning("Device Not Ready", "Please connect your phone and ensure it is detected before pre-checking.")
            return

        phone_folder = self.phone_dir_var.get().strip()
        self.precheck_status_var.set(f"Scanning '{phone_folder}' on phone (excluding thumbnails)...")
        self._log("INFO", f"Running pre-check scan on: {phone_folder}")

        # Run scan in a fast background worker so UI doesn't hitch
        def _scan_thread():
            files, ignored = adb_scan_camera_photos(self.adb_bin, self.selected_device_serial, phone_folder)
            count = len(files)
            msg = f"📊 Pre-check: {count:,} photo(s) found. (Filtered out {ignored:,} thumbnails/hidden files)"
            self.precheck_status_var.set(msg)
            self._log("SUCCESS", f"Pre-check complete: {count:,} JPEG photos ready. Filtered out {ignored:,} thumbnail/cache files.")

        threading.Thread(target=_scan_thread, daemon=True).start()

    # ---------------- PRESET & PREVIEW CONTROLS ----------------

    def _update_preview(self):
        """Generates a dynamic preview path using a realistic sample date."""
        sample_date = datetime.datetime.now()
        sub = build_custom_subfolder(
            sample_date,
            self.opt_year_var.get(),
            self.opt_month_var.get(),
            self.opt_week_var.get(),
            self.opt_day_var.get()
        )

        if sub:
            display_path = f"TargetStorage \\ {sub} \\ photo.jpg"
        else:
            display_path = "TargetStorage \\ photo.jpg (No subfolders; saved directly in target root)"

        self.preview_path_var.set(display_path)

    def _preset_all(self):
        self.opt_year_var.set(True)
        self.opt_month_var.set(True)
        self.opt_week_var.set(True)
        self.opt_day_var.set(True)
        self._update_preview()

    def _preset_year_month_day(self):
        self.opt_year_var.set(True)
        self.opt_month_var.set(True)
        self.opt_week_var.set(False)
        self.opt_day_var.set(True)
        self._update_preview()

    def _preset_year_month(self):
        self.opt_year_var.set(True)
        self.opt_month_var.set(True)
        self.opt_week_var.set(False)
        self.opt_day_var.set(False)
        self._update_preview()

    def _preset_month(self):
        self.opt_year_var.set(False)
        self.opt_month_var.set(True)
        self.opt_week_var.set(False)
        self.opt_day_var.set(False)
        self._update_preview()

    def _preset_day(self):
        self.opt_year_var.set(False)
        self.opt_month_var.set(False)
        self.opt_week_var.set(False)
        self.opt_day_var.set(True)
        self._update_preview()

    # ---------------- UI EVENT HANDLERS ----------------

    def _browse_source(self):
        folder = filedialog.askdirectory(title="Select Source Folder (Mounted Phone / DCIM)")
        if folder:
            self.source_dir_var.set(os.path.normpath(folder))
            self._log("INFO", f"Source folder selected: {self.source_dir_var.get()}")

    def _browse_target(self):
        folder = filedialog.askdirectory(title="Select Target Storage Folder (External Drive / PC)")
        if folder:
            self.target_dir_var.set(os.path.normpath(folder))
            self._log("INFO", f"Target storage folder selected: {self.target_dir_var.get()}")

    def _on_mode_toggled(self):
        if self.copy_mode_var.get():
            self.mode_hint_lbl.config(
                text="Safe mode: Originals are preserved unchanged on your phone.",
                foreground="#16A34A"
            )
        else:
            self.mode_hint_lbl.config(
                text="⚠️ Move mode: Originals will be safely verified then deleted from the source.",
                foreground="#DC2626"
            )

    def _clear_log(self):
        self.log_text.delete("1.0", tk.END)

    def _log(self, level: str, message: str):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] ", "TIMESTAMP")
        self.log_text.insert(tk.END, f"[{level:<7}] ", level)
        self.log_text.insert(tk.END, f"{message}\n")
        self.log_text.see(tk.END)

    def _set_ui_state(self, running: bool):
        self.is_running = running
        state = tk.DISABLED if running else tk.NORMAL

        self.btn_browse_src.config(state=state)
        self.btn_browse_dest.config(state=state)
        self.src_entry.config(state=state)
        self.dest_entry.config(state=state)
        self.chk_year.config(state=state)
        self.chk_month.config(state=state)
        self.chk_week.config(state=state)
        self.chk_day.config(state=state)
        self.chk_transfer_mode.config(state=state)
        self.rb_mode_adb.config(state=state)
        self.rb_mode_local.config(state=state)
        self.btn_refresh_adb.config(state=state)
        self.btn_precheck.config(state=state)
        self.combo_phone_dir.config(state="disabled" if running else "normal")

        if running:
            self.btn_start.config(state=tk.DISABLED, bg="#94A3B8", cursor="arrow")
            self.btn_cancel.config(state=tk.NORMAL, bg="#EF4444", fg="white", cursor="hand2")
        else:
            self.btn_start.config(state=tk.NORMAL, bg="#2563EB", cursor="hand2")
            self.btn_cancel.config(state=tk.DISABLED, bg="#E2E8F0", fg="#475569", cursor="arrow")

    # ---------------- THREADING & WORKER DISPATCH ----------------

    def start_import(self):
        dest = self.target_dir_var.get().strip()
        mode = self.source_mode_var.get()

        if not dest:
            messagebox.showwarning("Missing Target", "Please select a Target Storage Folder before starting.")
            return

        if not os.path.isdir(dest):
            try:
                os.makedirs(dest, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Invalid Target", f"Could not create target folder:\n{e}")
                return

        # Hierarchy configuration
        y, m, w, d = self.opt_year_var.get(), self.opt_month_var.get(), self.opt_week_var.get(), self.opt_day_var.get()
        if not any((y, m, w, d)):
            if not messagebox.askyesno("No Subfolders Selected", "No hierarchy levels are selected. Photos will be saved directly into target root.\n\nContinue?"):
                return

        is_copy_mode = self.copy_mode_var.get()
        if not is_copy_mode:
            confirmed = messagebox.askyesno(
                "Confirm Move Operation",
                "You have chosen to MOVE files.\n\n"
                "Photos will be verified on target and then PERMANENTLY deleted from your phone.\n\n"
                "Are you sure you want to proceed?",
                icon="warning"
            )
            if not confirmed:
                return

        if mode == "LOCAL":
            src = self.source_dir_var.get().strip()
            if not src or not os.path.isdir(src):
                messagebox.showwarning("Missing Source", "Please select a valid local Source Folder.")
                return
            if Path(src).resolve() == Path(dest).resolve():
                messagebox.showerror("Conflict", "Source and Target cannot be the same directory.")
                return

            self._set_ui_state(running=True)
            self.cancel_event.clear()
            self.progress_bar["value"] = 0
            self.progress_percent_var.set("0%")
            self.status_var.set("Scanning local directory for JPEG photos...")

            self._log("INFO", "=" * 55)
            self._log("INFO", f"Starting LOCAL {'COPY' if is_copy_mode else 'MOVE'} import...")
            self._log("INFO", f"Source: {src} ➔ Target: {dest}")

            self.worker_thread = threading.Thread(
                target=self._worker_local_transfer,
                args=(src, dest, y, m, w, d, is_copy_mode),
                daemon=True
            )
            self.worker_thread.start()

        else:  # ADB Mode
            if not self.adb_bin:
                messagebox.showerror("ADB Missing", "ADB executable was not detected on this system.")
                return
            if not self.selected_device_serial:
                messagebox.showwarning("Phone Not Connected", "No active phone detected. Please connect phone and click 'Detect Phone'.")
                return

            phone_dir = self.phone_dir_var.get().strip()
            if not phone_dir:
                messagebox.showwarning("Missing Folder", "Please specify a camera folder on the phone.")
                return

            self._set_ui_state(running=True)
            self.cancel_event.clear()
            self.progress_bar["value"] = 0
            self.progress_percent_var.set("0%")
            self.status_var.set(f"Scanning phone folder: {phone_dir}...")

            self._log("INFO", "=" * 55)
            self._log("INFO", f"Starting ADB USB {'COPY' if is_copy_mode else 'MOVE'} import...")
            self._log("INFO", f"Phone Folder: {phone_dir} ➔ Target: {dest}")

            self.worker_thread = threading.Thread(
                target=self._worker_adb_transfer,
                args=(phone_dir, dest, y, m, w, d, is_copy_mode),
                daemon=True
            )
            self.worker_thread.start()

    def cancel_import(self):
        if self.is_running:
            self.cancel_event.set()
            self.status_var.set("Cancelling... finishing active file...")
            self._log("WARNING", "Cancellation requested by user. Terminating process safely...")

    # ---------------- BACKGROUND WORKERS ----------------

    def _worker_local_transfer(self, source_dir: str, target_dir: str, y: bool, m: bool, w: bool, d: bool, is_copy_mode: bool):
        try:
            candidate_files = []
            for root_path, _, filenames in os.walk(source_dir):
                if self.cancel_event.is_set():
                    break
                for fname in filenames:
                    if fname.lower().endswith((".jpg", ".jpeg")):
                        candidate_files.append(os.path.join(root_path, fname))

            self._process_file_batch(candidate_files, target_dir, y, m, w, d, is_copy_mode, is_adb=False)
        except Exception as ex:
            self.msg_queue.put(("FATAL_ERROR", str(ex)))

    def _worker_adb_transfer(self, phone_dir: str, target_dir: str, y: bool, m: bool, w: bool, d: bool, is_copy_mode: bool):
        try:
            self.msg_queue.put(("LOG", ("INFO", "Scanning phone via ADB and filtering out thumbnail folders...")))
            candidate_files, ignored_count = adb_scan_camera_photos(self.adb_bin, self.selected_device_serial, phone_dir)

            if ignored_count > 0:
                self.msg_queue.put(("LOG", ("INFO", f"Protected from thumbnails: {ignored_count:,} thumbnail/hidden files skipped.")))

            self._process_file_batch(candidate_files, target_dir, y, m, w, d, is_copy_mode, is_adb=True)
        except Exception as ex:
            self.msg_queue.put(("FATAL_ERROR", str(ex)))

    def _process_file_batch(self, candidate_files: list[str], target_dir: str, y: bool, m: bool, w: bool, d: bool, is_copy_mode: bool, is_adb: bool):
        if self.cancel_event.is_set():
            self.msg_queue.put(("CANCELLED", {"processed": 0, "total": 0}))
            return

        total_files = len(candidate_files)
        if total_files == 0:
            self.msg_queue.put(("NO_FILES", None))
            return

        self.msg_queue.put(("SCAN_DONE", total_files))

        stats = {"total": total_files, "processed": 0, "copied": 0, "moved": 0, "skipped": 0, "errors": 0}
        temp_cache_dir = tempfile.mkdtemp(prefix="phone_photo_import_")

        try:
            for idx, file_item in enumerate(candidate_files, start=1):
                if self.cancel_event.is_set():
                    self.msg_queue.put(("CANCELLED", stats))
                    return

                filename = os.path.basename(file_item)
                self.msg_queue.put(("PROGRESS_UPDATE", (idx, total_files, filename)))

                try:
                    if is_adb:
                        # Pull file to temporary local staging buffer
                        local_temp_file = os.path.join(temp_cache_dir, filename)
                        pull_args = []
                        if self.selected_device_serial:
                            pull_args.extend(["-s", self.selected_device_serial])
                        pull_args.extend(["pull", file_item, local_temp_file])

                        code, _, err = run_adb_command(self.adb_bin, pull_args, timeout=45)
                        if code != 0 or not os.path.isfile(local_temp_file):
                            stats["errors"] += 1
                            self.msg_queue.put(("LOG", ("ERROR", f"Failed pulling {filename} via ADB: {err}")))
                            continue

                        src_file_for_ops = local_temp_file
                    else:
                        src_file_for_ops = file_item

                    src_size = os.path.getsize(src_file_for_ops)
                    photo_date, date_source = extract_photo_date(src_file_for_ops)

                    # Build destination folder
                    subfolder = build_custom_subfolder(photo_date, y, m, w, d)
                    full_dest_folder = os.path.join(target_dir, subfolder) if subfolder else target_dir
                    os.makedirs(full_dest_folder, exist_ok=True)

                    dest_filepath, is_skip = resolve_collision_path(full_dest_folder, filename, src_size)

                    if is_skip:
                        stats["skipped"] += 1
                        self.msg_queue.put(("LOG", ("SKIP", f"Skipped (identical size exists): {filename}")))
                        if is_adb and os.path.isfile(src_file_for_ops):
                            os.remove(src_file_for_ops)
                    else:
                        # Place file in destination
                        if is_adb:
                            shutil.move(src_file_for_ops, dest_filepath)
                        else:
                            shutil.copy2(src_file_for_ops, dest_filepath)

                        if not is_copy_mode:
                            # Verify destination copy integrity before deleting source
                            if os.path.exists(dest_filepath) and os.path.getsize(dest_filepath) == src_size:
                                if is_adb:
                                    rm_args = []
                                    if self.selected_device_serial:
                                        rm_args.extend(["-s", self.selected_device_serial])
                                    rm_args.extend(["shell", "rm", f'"{file_item}"'])
                                    run_adb_command(self.adb_bin, rm_args, timeout=15)
                                else:
                                    os.remove(src_file_for_ops)

                                stats["moved"] += 1
                                self.msg_queue.put(("LOG", ("SUCCESS", f"Moved: {filename} ➔ {os.path.relpath(dest_filepath, target_dir)} [{date_source}]")))
                            else:
                                raise IOError(f"Integrity check failed for {filename}; source preserved.")
                        else:
                            stats["copied"] += 1
                            self.msg_queue.put(("LOG", ("SUCCESS", f"Copied: {filename} ➔ {os.path.relpath(dest_filepath, target_dir)} [{date_source}]")))

                except Exception as ex:
                    stats["errors"] += 1
                    self.msg_queue.put(("LOG", ("ERROR", f"Error on {filename}: {ex}")))
                finally:
                    stats["processed"] = idx

            self.msg_queue.put(("COMPLETED", stats))

        finally:
            shutil.rmtree(temp_cache_dir, ignore_errors=True)

    # ---------------- QUEUE DISPATCHER ----------------

    def _process_queue(self):
        try:
            for _ in range(50):
                if self.msg_queue.empty():
                    break

                msg_type, payload = self.msg_queue.get_nowait()

                if msg_type == "LOG":
                    level, text = payload
                    self._log(level, text)

                elif msg_type == "SCAN_DONE":
                    total = payload
                    self.progress_bar["maximum"] = total
                    self.progress_bar["value"] = 0
                    self.status_var.set(f"Found {total} JPEG images. Processing...")
                    self._log("INFO", f"Scan complete: {total} camera photos ready to import.")

                elif msg_type == "PROGRESS_UPDATE":
                    idx, total, filename = payload
                    self.progress_bar["value"] = idx
                    pct = int((idx / total) * 100)
                    self.progress_percent_var.set(f"{pct}%")
                    self.status_var.set(f"Processing {idx} of {total}: {filename}")

                elif msg_type == "COMPLETED":
                    self._handle_completion(payload)

                elif msg_type == "CANCELLED":
                    self._handle_cancellation(payload)

                elif msg_type == "NO_FILES":
                    self._set_ui_state(running=False)
                    self.status_var.set("No JPEG photos found.")
                    self._log("WARNING", "No .jpg/.jpeg files discovered (thumbnail & cache files were excluded).")
                    messagebox.showinfo("Scan Result", "No JPEG camera photos were found in the selected folder.")

                elif msg_type == "FATAL_ERROR":
                    self._set_ui_state(running=False)
                    self.status_var.set("Fatal error occurred.")
                    self._log("ERROR", f"FATAL ERROR: {payload}")
                    messagebox.showerror("Fatal Error", f"A fatal error occurred:\n\n{payload}")

        except queue.Empty:
            pass
        finally:
            self.root.after(50, self._process_queue)

    def _handle_completion(self, stats: dict):
        self._set_ui_state(running=False)
        self.progress_bar["value"] = stats["total"]
        self.progress_percent_var.set("100%")
        self.status_var.set(f"Done! Processed {stats['processed']} of {stats['total']} photos.")

        self._log("INFO", "=" * 55)
        self._log("SUCCESS", "Process completed successfully!")
        self._log("INFO", f"Summary: {stats['copied']} copied, {stats['moved']} moved, {stats['skipped']} skipped, {stats['errors']} errors.")

        summary_msg = (
            f"Photo import finished!\n\n"
            f"• Total photos processed: {stats['total']}\n"
            f"• Successfully copied: {stats['copied']}\n"
            f"• Successfully moved: {stats['moved']}\n"
            f"• Skipped (identical size): {stats['skipped']}\n"
            f"• Errors: {stats['errors']}"
        )
        messagebox.showinfo("Import Complete", summary_msg)

    def _handle_cancellation(self, stats: dict):
        self._set_ui_state(running=False)
        self.status_var.set("Operation cancelled by user.")
        self._log("WARNING", f"Operation stopped. Processed {stats.get('processed', 0)} photos before stopping.")
        messagebox.showwarning(
            "Cancelled",
            f"The import was cancelled.\n\n"
            f"Photos processed: {stats.get('processed', 0)}\n"
            f"Copied: {stats.get('copied', 0)} | Moved: {stats.get('moved', 0)}"
        )


# ==============================================================================
# ENTRY POINT
# ==============================================================================

def main():
    root = tk.Tk()
    app = PhotoImporterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
