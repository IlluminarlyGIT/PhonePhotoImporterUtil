#!/usr/bin/env python3
"""
Phone Photo Importer & Sorter
=============================
A modern, production-ready desktop utility built with Python and Tkinter
to recursively scan, organize, and sort JPEG photos from an Android device
(via ADB or local/mounted folders) into a customizable date-structured external drive.

Key Capabilities:
- Safe Clean-Up & Storage Free-Up:
    * Human-in-the-loop post-verification deletion: Verify copied photos first, then
      safely delete originals from the phone.
    * Selective folder targeting: Choose the exact folder tree to clean (e.g. WhatsApp Images
      to save space, while leaving DCIM/Camera untouched).
    * Double-Check Safety: A file on the phone is ONLY deleted if an identical copy
      (same name and exact matching size) is verified in the target backup drive!
- Full WhatsApp Media Support:
    * Targets Camera roll, Pictures, and WhatsApp folders including:
      '/Android/media/com.whatsapp/WhatsApp/accounts/1005/media/WhatsApp Images/'
    * Auto-normalizes paths with or without '/sdcard' and handles spaces smoothly.
    * Auto-extracts dates from WhatsApp filenames (IMG-YYYYMMDD-WA...) even when EXIF is stripped!
    * Auto-detects active WhatsApp folders on your connected phone.
- Smart Incremental Backup ("Dumb-Dumb" Proof):
    * Checks if files already exist in your target drive before transferring.
    * If a photo has already been backed up, it skips it instantly without downloading
      a single byte across USB.
- Dual Source Modes:
    1. Android USB via ADB (Direct USB connection with auto-discovery).
    2. Local / Mapped PC Folder (e.g. drive letter, SD card, Wi-Fi SMB).
- Pre-Check & Smart Thumbnail Protection:
    * Quick one-click photo counter before transfer.
    * Automatically filters out .thumbnails, .trashed, .cache, and hidden folders.
- Date Range Filtering:
    * Optional "From" and "To" date filters with 1-click presets.
- Chronological Numbered Folder Hierarchy:
    * Months named "1. January", "2. February", ..., "12. December" so Windows File Explorer
      sorts them chronologically instead of jumbled alphabetically!
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
import struct
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext


# ==============================================================================
# TARGET DIRECTORY INDEXING (INCREMENTAL BACKUP & VERIFIED CLEANUP)
# ==============================================================================

def index_target_folder(target_dir: str) -> dict[str, set[int]]:
    """
    Builds a fast lookup dictionary of all existing files in the target directory:
    lowercase_filename -> set of file sizes in bytes.

    Also indexes base names for suffixed duplicates (e.g. 'IMG_001_1.jpg' -> 'img_001.jpg')
    so that existing copies are matched reliably in sub-millisecond time.
    """
    index: dict[str, set[int]] = {}
    if not target_dir or not os.path.isdir(target_dir):
        return index

    for root, _, files in os.walk(target_dir):
        for fname in files:
            if fname.lower().endswith((".jpg", ".jpeg")):
                full_path = os.path.join(root, fname)
                try:
                    sz = os.path.getsize(full_path)
                    lower_name = fname.lower()
                    index.setdefault(lower_name, set()).add(sz)

                    m = re.match(r"^(.*?)(?:_\d+)(\.[^.]+)$", fname, re.IGNORECASE)
                    if m:
                        base_lower = f"{m.group(1)}{m.group(2)}".lower()
                        index.setdefault(base_lower, set()).add(sz)
                except OSError:
                    pass
    return index


def format_bytes(num_bytes: int) -> str:
    """Formats a byte count into a human-readable string (B, KB, MB, GB)."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    kb = num_bytes / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f} MB"
    gb = mb / 1024
    return f"{gb:.2f} GB"


def get_free_disk_space(target_path: str) -> int | None:
    """
    Returns available free space in bytes on the drive/volume hosting target_path.
    Walks up parent directories if target_path does not exist on disk yet.
    """
    if not target_path or not target_path.strip():
        return None
    try:
        curr = Path(target_path.strip()).resolve()
        while not curr.exists() and curr.parent != curr:
            curr = curr.parent
        if curr.exists():
            return shutil.disk_usage(str(curr)).free
    except Exception:
        pass
    return None


# ==============================================================================
# ADB (ANDROID DEBUG BRIDGE) HELPERS & PATH NORMALIZATION
# ==============================================================================

def get_resource_path(relative_path: str) -> str:
    """
    Resolves path to bundled resources for both standard Python execution
    and PyInstaller frozen builds (--onedir / --onefile).
    """
    base_dirs = []
    if hasattr(sys, "_MEIPASS"):
        base_dirs.append(getattr(sys, "_MEIPASS"))
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        base_dirs.append(exe_dir)
        base_dirs.append(os.path.join(exe_dir, "_internal"))
    base_dirs.append(os.path.dirname(os.path.abspath(__file__)))

    for base in base_dirs:
        candidate = os.path.join(base, relative_path)
        if os.path.exists(candidate):
            return candidate

    return os.path.join(base_dirs[-1], relative_path)


def find_adb_executable() -> str | None:
    """
    Locates the adb executable, prioritizing the bundled platform-tools/adb.exe
    first (zero-setup portable mode), then falling back to system PATH or Android SDK.
    """
    bundled_adb = get_resource_path(os.path.join("platform-tools", "adb.exe"))
    if os.path.isfile(bundled_adb):
        return bundled_adb

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


_ADB_SERVER_INITIALIZED = False


def run_adb_command(adb_bin: str, args: list[str], timeout: int = 15) -> tuple[int, str, str]:
    """
    Runs an ADB subprocess quietly without popping up a console window.
    Pre-starts the ADB daemon with DEVNULL stdio on first invocation so that
    Windows 'adb fork-server' never inherits open stdout/stderr pipe handles.
    """
    global _ADB_SERVER_INITIALIZED
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0

    if not _ADB_SERVER_INITIALIZED:
        try:
            subprocess.run(
                [adb_bin, "start-server"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=creation_flags
            )
            _ADB_SERVER_INITIALIZED = True
        except Exception:
            pass

    cmd = [adb_bin] + args
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
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


def normalize_phone_path(phone_folder: str) -> str:
    """
    Normalizes a user-specified phone path to a valid Android storage path.
    e.g. '/Android/media/com.whatsapp/...' -> '/sdcard/Android/media/com.whatsapp/...'
    """
    path = phone_folder.strip().replace("\\", "/").rstrip("/")
    if not path.startswith("/"):
        path = "/" + path

    if path.startswith("/Android/"):
        path = "/sdcard" + path
    elif not path.startswith("/sdcard/") and not path.startswith("/storage/"):
        path = "/sdcard/" + path.lstrip("/")

    return path


def get_connected_adb_devices(adb_bin: str) -> list[dict]:
    """Parses 'adb devices -l' output to return all connected Android devices."""
    code, stdout, _ = run_adb_command(adb_bin, ["devices", "-l"])
    if code != 0:
        return []

    devices = []
    lines = stdout.splitlines()
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            serial = parts[0]
            state = parts[1]
            info = " ".join(parts[2:])

            model_match = re.search(r"model:([^\s]+)", info)
            model_name = model_match.group(1).replace("_", " ") if model_match else serial

            devices.append({
                "serial": serial,
                "state": state,
                "model": model_name,
                "raw": line
            })
    return devices


def detect_phone_media_folders(adb_bin: str, serial: str | None, default_presets: list[str]) -> list[str]:
    """Queries connected phone for active media folders."""
    cmd_args = []
    if serial:
        cmd_args.extend(["-s", serial])

    test_sh = "for p in " + " ".join([f'"{p}"' for p in default_presets]) + '; do [ -d "$p" ] && echo "$p"; done'
    cmd_args.extend(["shell", test_sh])

    code, stdout, _ = run_adb_command(adb_bin, cmd_args, timeout=10)
    if code == 0 and stdout:
        existing = [line.strip() for line in stdout.splitlines() if line.strip()]
        if existing:
            for p in default_presets:
                if p not in existing:
                    existing.append(p)
            return existing

    return default_presets


def parse_date_from_filename(filename: str) -> datetime.date | None:
    """Extracts date from camera & WhatsApp photo filenames."""
    match = re.search(r"(?:IMG[-_]|PANO[-_]|Screenshot[-_]|VID[-_]|WA[-_])?(\d{4})[-_]?(\d{2})[-_]?(\d{2})", filename, re.IGNORECASE)
    if match:
        try:
            year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
            if 1990 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
                return datetime.date(year, month, day)
        except ValueError:
            pass
    return None


def adb_scan_camera_photos(
    adb_bin: str,
    serial: str | None,
    phone_folder: str,
    date_filter: tuple[datetime.date, datetime.date] | None = None,
    target_index: dict[str, set[int]] | None = None
) -> tuple[list[tuple[str, int]], int, int, int]:
    """
    Finds valid JPEG photos on the Android device along with their file sizes.
    Correctly handles spaces in directory names (e.g. 'WhatsApp Images').
    """
    norm_folder = normalize_phone_path(phone_folder)

    cmd_args = []
    if serial:
        cmd_args.extend(["-s", serial])
    cmd_args.extend(["shell", "find", f'"{norm_folder}"', "-type", "f", "-exec", "stat", "-c", "'%s %n'", "{}", "+"])

    code, stdout, _ = run_adb_command(adb_bin, cmd_args, timeout=40)

    if code != 0 or not stdout:
        plain_args = []
        if serial:
            plain_args.extend(["-s", serial])
        plain_args.extend(["shell", "find", f'"{norm_folder}"', "-type", "f"])
        code, stdout, _ = run_adb_command(adb_bin, plain_args, timeout=35)
        has_size_prefix = False
    else:
        has_size_prefix = True

    valid_candidates: list[tuple[str, int]] = []
    ignored_thumbnails = 0
    outside_date_range = 0
    already_in_target_count = 0

    start_d, end_d = date_filter if date_filter else (None, None)

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        file_size = -1
        if has_size_prefix:
            parts_split = line.split(" ", 1)
            if len(parts_split) == 2 and parts_split[0].isdigit():
                file_size = int(parts_split[0])
                file_path = parts_split[1]
            else:
                file_path = line
        else:
            file_path = line

        if not file_path.lower().endswith((".jpg", ".jpeg")):
            continue

        path_parts = file_path.split("/")
        if any(p.startswith(".") for p in path_parts) or any(ign in p.lower() for p in path_parts for ign in ("thumbnail", "cache", "trash")):
            ignored_thumbnails += 1
            continue

        filename = path_parts[-1]
        lower_fn = filename.lower()

        # Date Range Filter
        if start_d and end_d:
            fn_date = parse_date_from_filename(filename)
            if fn_date and not (start_d <= fn_date <= end_d):
                outside_date_range += 1
                continue

        # Incremental Backup Filter
        if target_index and file_size > 0:
            existing_sizes = target_index.get(lower_fn)
            if existing_sizes and file_size in existing_sizes:
                already_in_target_count += 1
                continue

        valid_candidates.append((file_path, file_size))

    return valid_candidates, ignored_thumbnails, outside_date_range, already_in_target_count


# ==============================================================================
# CORE LOGIC & PURE-PYTHON EXIF PARSER
# ==============================================================================

def read_jpeg_exif_datetime(filepath: str) -> str | None:
    """
    Pure-Python binary JPEG EXIF header parser using standard 'struct'.
    Reads only the header bytes to extract DateTimeOriginal (0x9003),
    DateTimeDigitized (0x9004), or DateTime (0x0132) without needing Pillow.
    """
    try:
        with open(filepath, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return None

            while True:
                b = f.read(1)
                if not b:
                    return None
                if b != b"\xff":
                    continue

                while b == b"\xff":
                    b = f.read(1)
                    if not b:
                        return None

                marker = b[0]
                if marker in (0xDA, 0xD9):  # Start of Scan or End of Image
                    return None
                if marker == 0x00 or (0xD0 <= marker <= 0xD7) or marker == 0x01:
                    continue

                len_bytes = f.read(2)
                if len(len_bytes) < 2:
                    return None
                seg_len = struct.unpack(">H", len_bytes)[0]
                if seg_len < 2:
                    return None

                if marker == 0xE1:  # APP1 Segment
                    payload = f.read(seg_len - 2)
                    if len(payload) >= 14 and payload[:6] == b"Exif\x00\x00":
                        tiff = payload[6:]
                        byte_order = tiff[:2]
                        if byte_order == b"II":
                            endian = "<"
                        elif byte_order == b"MM":
                            endian = ">"
                        else:
                            continue

                        if struct.unpack(endian + "H", tiff[2:4])[0] != 42:
                            continue

                        ifd0_offset = struct.unpack(endian + "I", tiff[4:8])[0]

                        def _parse_ifd(ifd_off: int) -> dict[int, tuple[int, int, bytes]]:
                            tags: dict[int, tuple[int, int, bytes]] = {}
                            if ifd_off + 2 > len(tiff):
                                return tags
                            count_entries = struct.unpack(endian + "H", tiff[ifd_off : ifd_off + 2])[0]
                            pos = ifd_off + 2
                            for _ in range(count_entries):
                                if pos + 12 > len(tiff):
                                    break
                                tag_id, fmt, cnt = struct.unpack(endian + "HHI", tiff[pos : pos + 8])
                                raw_val = tiff[pos + 8 : pos + 12]
                                tags[tag_id] = (fmt, cnt, raw_val)
                                pos += 12
                            return tags

                        def _read_ascii(entry: tuple[int, int, bytes]) -> str | None:
                            fmt, cnt, raw_val = entry
                            if fmt != 2 or cnt <= 0:
                                return None
                            if cnt <= 4:
                                data = raw_val[:cnt]
                            else:
                                str_off = struct.unpack(endian + "I", raw_val)[0]
                                if str_off + cnt > len(tiff):
                                    return None
                                data = tiff[str_off : str_off + cnt]
                            return data.decode("ascii", errors="ignore").strip("\x00").strip()

                        ifd0 = _parse_ifd(ifd0_offset)
                        # Check Exif SubIFD (0x8769 = 34665) first for DateTimeOriginal (36867) / DateTimeDigitized (36868)
                        if 34665 in ifd0:
                            exif_sub_off = struct.unpack(endian + "I", ifd0[34665][2])[0]
                            exif_ifd = _parse_ifd(exif_sub_off)
                            for t_id in (36867, 36868):
                                if t_id in exif_ifd:
                                    dt_str = _read_ascii(exif_ifd[t_id])
                                    if dt_str:
                                        return dt_str

                        # Fallback to IFD0 DateTimeOriginal / DateTime (306)
                        for t_id in (36867, 36868, 306):
                            if t_id in ifd0:
                                dt_str = _read_ascii(ifd0[t_id])
                                if dt_str:
                                    return dt_str
                else:
                    f.seek(seg_len - 2, os.SEEK_CUR)
    except Exception:
        pass
    return None


def extract_photo_date(filepath: str) -> tuple[datetime.datetime, str]:
    """Reads capture date from EXIF, WhatsApp filename date, or filesystem timestamp."""
    raw_date_str = read_jpeg_exif_datetime(filepath)

    if raw_date_str and isinstance(raw_date_str, str):
        cleaned_str = raw_date_str.strip().replace("\x00", "")
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d", "%Y-%m-%d"):
            try:
                parsed_dt = datetime.datetime.strptime(cleaned_str, fmt)
                if parsed_dt.year > 1970:
                    return parsed_dt, "EXIF DateTimeOriginal"
            except ValueError:
                continue

    # Fallback: Date from filename (WhatsApp photos)
    fn = os.path.basename(filepath)
    fn_date = parse_date_from_filename(fn)
    if fn_date:
        return datetime.datetime(fn_date.year, fn_date.month, fn_date.day, 12, 0, 0), "Filename Date (WhatsApp/Camera)"

    # Fallback: Filesystem timestamp
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
    """Constructs a human-readable nested relative subfolder path with numbered months."""
    parts = []

    if include_year:
        parts.append(photo_date.strftime("%Y"))

    if include_month:
        month_name = f"{photo_date.month}. {photo_date.strftime('%B')}"
        if include_year:
            parts.append(month_name)
        else:
            parts.append(f"{month_name} {photo_date.year}")

    if include_week:
        iso_year, iso_week, _ = photo_date.isocalendar()
        if include_year:
            parts.append(f"Week {iso_week:02d}")
        else:
            parts.append(f"Week {iso_week:02d}, {iso_year}")

    if include_day:
        day_name = f"{photo_date.day}. {photo_date.strftime('%B')}"
        if include_month or include_year:
            parts.append(day_name)
        else:
            parts.append(f"{day_name} {photo_date.year}")

    return os.path.join(*parts) if parts else ""


def resolve_collision_path(target_folder: str, filename: str, src_size: int) -> tuple[str, bool]:
    """Collision resolution with exact matching skip and sequential suffixing."""
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


def parse_date_string(date_text: str) -> datetime.date | None:
    """Parses user-entered date string in YYYY-MM-DD or YYYY/MM/DD."""
    text = date_text.strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


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
        "/sdcard/Android/media/com.whatsapp/WhatsApp/accounts/1005/media/WhatsApp Images",
        "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images",
        "/sdcard/Pictures/WhatsApp",
        "/sdcard/Pictures",
        "/sdcard/Download",
    ]

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Phone Photo Importer & Sorter")
        self.root.geometry("880x920")
        self.root.minsize(760, 760)

        icon_path = get_resource_path("app_icon.ico")
        if os.path.isfile(icon_path):
            try:
                self.root.iconbitmap(icon_path)
            except Exception:
                pass

        # Threading and Communication primitives
        self.msg_queue: queue.Queue = queue.Queue()
        self.cancel_event: threading.Event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.is_running: bool = False

        # ADB detection
        self.adb_bin = find_adb_executable()
        self.adb_devices: list[dict] = []
        self.selected_device_serial: str | None = None

        # Source mode: "ADB" or "LOCAL"
        self.source_mode_var = tk.StringVar(value="ADB" if self.adb_bin else "LOCAL")

        # Directories
        self.source_dir_var = tk.StringVar(value="")
        self.phone_dir_var = tk.StringVar(value=self.PHONE_PRESET_FOLDERS[0])
        self.target_dir_var = tk.StringVar(value="")
        self.clean_folder_var = tk.StringVar(value=self.PHONE_PRESET_FOLDERS[2])  # Default to WhatsApp multi-account
        self.adb_status_var = tk.StringVar(value="Searching for ADB...")
        self.precheck_status_var = tk.StringVar(value="Not checked yet")

        # Date Range Filter variables
        self.use_date_filter_var = tk.BooleanVar(value=False)
        self.date_from_var = tk.StringVar(value="")
        self.date_to_var = tk.StringVar(value="")

        # Incremental Skip Duplicate variables
        self.skip_existing_var = tk.BooleanVar(value=True)

        # Hierarchy builder variables
        self.opt_year_var = tk.BooleanVar(value=True)
        self.opt_month_var = tk.BooleanVar(value=True)
        self.opt_week_var = tk.BooleanVar(value=False)
        self.opt_day_var = tk.BooleanVar(value=False)

        self.preview_path_var = tk.StringVar(value="")
        self.copy_mode_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Ready. Configure options to begin.")
        self.progress_percent_var = tk.StringVar(value="0%")

        # Build GUI Layout & Apply Modern Styling
        self._apply_styles()
        self._build_widgets()

        # Initialize UI states
        self._update_preview()
        self._on_source_mode_changed()
        self._on_date_filter_toggled()

        # Start periodic queue polling on the Tkinter main thread
        self.root.after(50, self._process_queue)

        # Log environment status
        if self.adb_bin:
            self._log("INFO", f"ADB detected at: {self.adb_bin}")

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
        main_container = tk.Frame(self.root, bg=self.bg_color, padx=16, pady=6)
        main_container.pack(fill=tk.BOTH, expand=True)

        # ---------------- HEADER ----------------
        header_frame = tk.Frame(main_container, bg=self.bg_color)
        header_frame.pack(fill=tk.X, pady=(0, 4))

        title_lbl = ttk.Label(header_frame, text="📸 Phone Photo Importer & Sorter", style="Header.TLabel")
        title_lbl.pack(anchor="w")

        subtitle_lbl = ttk.Label(
            header_frame,
            text="Organize photos from Android (Camera & WhatsApp) into chronological archives with verified safe cleanup.",
            style="SubHeader.TLabel"
        )
        subtitle_lbl.pack(anchor="w", pady=(1, 0))

        # ---------------- SECTION 1: SOURCE & DESTINATION ----------------
        dir_card = ttk.LabelFrame(main_container, text=" 1. Source & Target Selection ", padding=(12, 5))
        dir_card.pack(fill=tk.X, pady=(0, 5))

        # Source Mode Switcher
        mode_select_row = tk.Frame(dir_card, bg=self.card_bg)
        mode_select_row.pack(fill=tk.X, pady=(0, 4))
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

        device_status_row = tk.Frame(self.adb_frame, bg=self.card_bg)
        device_status_row.pack(fill=tk.X, pady=(1, 3))

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

        phone_folder_row = tk.Frame(self.adb_frame, bg=self.card_bg)
        phone_folder_row.pack(fill=tk.X, pady=(2, 3))
        ttk.Label(phone_folder_row, text="Phone Folder:", width=14, anchor="w", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)

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

        # --- Target Storage Folder Row ---
        dest_label_row = tk.Frame(dir_card, bg=self.card_bg)
        dest_label_row.pack(fill=tk.X, pady=(3, 1))
        ttk.Label(dest_label_row, text="Target Storage Folder (External Drive / PC):", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)

        dest_input_row = tk.Frame(dir_card, bg=self.card_bg)
        dest_input_row.pack(fill=tk.X, pady=(0, 2))
        self.dest_entry = ttk.Entry(dest_input_row, textvariable=self.target_dir_var, font=("Segoe UI", 9))
        self.dest_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self.btn_browse_dest = ttk.Button(dest_input_row, text="Browse Target...", command=self._browse_target)
        self.btn_browse_dest.pack(side=tk.RIGHT)

        # ---------------- SECTION 2: DATE RANGE FILTER ----------------
        date_card = ttk.LabelFrame(main_container, text=" 2. Date Range Filter (Optional) ", padding=(12, 5))
        date_card.pack(fill=tk.X, pady=(0, 5))

        date_toggle_row = tk.Frame(date_card, bg=self.card_bg)
        date_toggle_row.pack(fill=tk.X, pady=(0, 3))

        self.chk_use_date = ttk.Checkbutton(
            date_toggle_row,
            text="Filter by Date Range (Only import photos taken within a specific timeframe)",
            variable=self.use_date_filter_var,
            style="Level.TCheckbutton",
            command=self._on_date_filter_toggled
        )
        self.chk_use_date.pack(side=tk.LEFT)

        self.date_inputs_frame = tk.Frame(date_card, bg=self.card_bg)
        self.date_inputs_frame.pack(fill=tk.X, pady=(1, 2))

        date_fields_row = tk.Frame(self.date_inputs_frame, bg=self.card_bg)
        date_fields_row.pack(fill=tk.X, pady=(1, 3))

        ttk.Label(date_fields_row, text="From Date:").pack(side=tk.LEFT, padx=(0, 4))
        self.ent_date_from = ttk.Entry(date_fields_row, textvariable=self.date_from_var, width=13, font=("Segoe UI", 9))
        self.ent_date_from.pack(side=tk.LEFT, padx=(0, 16))

        ttk.Label(date_fields_row, text="To Date:").pack(side=tk.LEFT, padx=(0, 4))
        self.ent_date_to = ttk.Entry(date_fields_row, textvariable=self.date_to_var, width=13, font=("Segoe UI", 9))
        self.ent_date_to.pack(side=tk.LEFT, padx=(0, 16))

        ttk.Label(date_fields_row, text="(Format: YYYY-MM-DD)", font=("Segoe UI", 8, "italic"), foreground=self.text_muted).pack(side=tk.LEFT)

        presets_row = tk.Frame(self.date_inputs_frame, bg=self.card_bg)
        presets_row.pack(fill=tk.X, pady=(1, 2))

        ttk.Label(presets_row, text="Quick Presets:", font=("Segoe UI", 8, "bold"), foreground=self.text_muted).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(presets_row, text="Today", command=lambda: self._set_date_preset("today")).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(presets_row, text="Last 7 Days", command=lambda: self._set_date_preset("7days")).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(presets_row, text="Last 30 Days", command=lambda: self._set_date_preset("30days")).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(presets_row, text="This Month", command=lambda: self._set_date_preset("this_month")).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(presets_row, text="This Year", command=lambda: self._set_date_preset("this_year")).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(presets_row, text="Clear (All Time)", command=lambda: self._set_date_preset("all")).pack(side=tk.LEFT, padx=(0, 4))

        # ---------------- SECTION 3: FOLDER HIERARCHY BUILDER ----------------
        struct_card = ttk.LabelFrame(main_container, text=" 3. Folder Hierarchy Builder ", padding=(12, 5))
        struct_card.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(
            struct_card,
            text="Select which date levels to nest (Months numbered '1. January', '2. February' for chronological Windows sorting):",
            foreground=self.text_dark
        ).pack(anchor="w", pady=(0, 2))

        # Checkboxes Row
        checks_row = tk.Frame(struct_card, bg=self.card_bg)
        checks_row.pack(fill=tk.X, pady=(1, 3))

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
            text="Month  (e.g., 6. June)",
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
            text="Day  (e.g., 10. June)",
            variable=self.opt_day_var,
            style="Level.TCheckbutton",
            command=self._update_preview
        )
        self.chk_day.pack(side=tk.LEFT)

        # Hierarchy Presets
        preset_row = tk.Frame(struct_card, bg=self.card_bg)
        preset_row.pack(fill=tk.X, pady=(1, 3))
        ttk.Label(preset_row, text="Presets:", font=("Segoe UI", 8, "bold"), foreground=self.text_muted).pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(preset_row, text="Year + Month", command=self._preset_year_month).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_row, text="Year + Month + Day", command=self._preset_year_month_day).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_row, text="All (Fully Nested)", command=self._preset_all).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_row, text="Month Only", command=self._preset_month).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_row, text="Day Only", command=self._preset_day).pack(side=tk.LEFT, padx=(0, 4))

        # Live Preview Banner
        preview_container = tk.Frame(struct_card, bg="#F1F5F9", relief=tk.SOLID, bd=1, padx=8, pady=3)
        preview_container.pack(fill=tk.X, pady=(2, 1))

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

        # ---------------- SECTION 4: TRANSFER MODE & INCREMENTAL CHECK ----------------
        opt_card = ttk.LabelFrame(main_container, text=" 4. Transfer Mode & Incremental Backup ", padding=(12, 4))
        opt_card.pack(fill=tk.X, pady=(0, 5))

        inc_row = tk.Frame(opt_card, bg=self.card_bg)
        inc_row.pack(fill=tk.X, pady=(1, 1))
        self.chk_skip_existing = ttk.Checkbutton(
            inc_row,
            text="⚡ Smart Incremental Backup: Skip photos already present in target drive (Never re-downloads existing files)",
            variable=self.skip_existing_var,
            style="Level.TCheckbutton"
        )
        self.chk_skip_existing.pack(side=tk.LEFT)

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

        # ---------------- SECTION 5: PROGRESS & EXECUTION ----------------
        progress_card = ttk.LabelFrame(main_container, text=" 5. Execution & Status ", padding=(12, 5))
        progress_card.pack(fill=tk.X, pady=(0, 5))

        btn_row = tk.Frame(progress_card, bg=self.card_bg)
        btn_row.pack(fill=tk.X, pady=(0, 3))

        self.btn_start = tk.Button(
            btn_row,
            text="▶  Start Photo Import",
            bg="#2563EB",
            fg="white",
            activebackground="#1D4ED8",
            activeforeground="white",
            font=("Segoe UI", 10, "bold"),
            padx=16,
            pady=4,
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
            pady=4,
            relief=tk.FLAT,
            state=tk.DISABLED,
            command=self.cancel_import
        )
        self.btn_cancel.pack(side=tk.LEFT)

        status_row = tk.Frame(progress_card, bg=self.card_bg)
        status_row.pack(fill=tk.X, pady=(1, 2))
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

        # ---------------- SECTION 6: SAFE CLEAN-UP (POST-VERIFICATION DELETION) ----------------
        clean_card = ttk.LabelFrame(main_container, text=" 6. Safe Clean-Up: Free Phone Storage (Verified Deletion) ", padding=(12, 5))
        clean_card.pack(fill=tk.X, pady=(0, 5))

        clean_info_lbl = ttk.Label(
            clean_card,
            text="Select which folder on your phone to clean up (e.g., delete WhatsApp Images to save space, keeping DCIM/Camera safe).\n"
                 "DOUBLE-SAFETY: A photo is ONLY deleted if an identical backup is confirmed in your target drive!",
            foreground="#334155",
            font=("Segoe UI", 8, "italic")
        )
        clean_info_lbl.pack(anchor="w", pady=(0, 3))

        # Quick Target Toggle Buttons
        clean_preset_row = tk.Frame(clean_card, bg=self.card_bg)
        clean_preset_row.pack(fill=tk.X, pady=(0, 3))
        ttk.Label(clean_preset_row, text="Target Toggle:", font=("Segoe UI", 8, "bold"), foreground=self.text_muted).pack(side=tk.LEFT, padx=(0, 6))

        self.btn_preset_clean_wa = ttk.Button(
            clean_preset_row,
            text="💬 WhatsApp Images",
            command=lambda: self._set_clean_preset("whatsapp")
        )
        self.btn_preset_clean_wa.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_preset_clean_cam = ttk.Button(
            clean_preset_row,
            text="📷 DCIM / Camera",
            command=lambda: self._set_clean_preset("camera")
        )
        self.btn_preset_clean_cam.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_preset_clean_dcim = ttk.Button(
            clean_preset_row,
            text="📁 Entire DCIM",
            command=lambda: self._set_clean_preset("dcim")
        )
        self.btn_preset_clean_dcim.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_preset_clean_pic = ttk.Button(
            clean_preset_row,
            text="🖼️ Pictures / WhatsApp",
            command=lambda: self._set_clean_preset("pictures_wa")
        )
        self.btn_preset_clean_pic.pack(side=tk.LEFT, padx=(0, 4))

        clean_controls_row = tk.Frame(clean_card, bg=self.card_bg)
        clean_controls_row.pack(fill=tk.X, pady=(1, 2))

        ttk.Label(clean_controls_row, text="Folder to Clean:", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(0, 6))

        self.combo_clean_dir = ttk.Combobox(
            clean_controls_row,
            textvariable=self.clean_folder_var,
            values=self.PHONE_PRESET_FOLDERS,
            font=("Segoe UI", 9)
        )
        self.combo_clean_dir.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        self.btn_browse_clean = ttk.Button(
            clean_controls_row,
            text="Browse...",
            command=self._browse_clean_folder
        )
        self.btn_browse_clean.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_safe_delete = tk.Button(
            clean_controls_row,
            text="🗑️  Safe Delete Verified Photos from Phone",
            bg="#DC2626",
            fg="white",
            activebackground="#B91C1C",
            activeforeground="white",
            font=("Segoe UI", 9, "bold"),
            padx=12,
            pady=4,
            relief=tk.FLAT,
            cursor="hand2",
            command=self.start_safe_delete
        )
        self.btn_safe_delete.pack(side=tk.RIGHT)

        # ---------------- SECTION 7: ACTIVITY LOG ----------------
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
            pady=5
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self.log_text.tag_configure("INFO", foreground="#93C5FD")
        self.log_text.tag_configure("SUCCESS", foreground="#4ADE80")
        self.log_text.tag_configure("SKIP", foreground="#FCD34D")
        self.log_text.tag_configure("WARNING", foreground="#FB923C")
        self.log_text.tag_configure("ERROR", foreground="#F87171")
        self.log_text.tag_configure("DELETED", foreground="#F472B6")
        self.log_text.tag_configure("TIMESTAMP", foreground="#94A3B8")

    # ---------------- DATE RANGE PRESETS & TOGGLES ----------------

    def _on_date_filter_toggled(self):
        if self.use_date_filter_var.get():
            self.date_inputs_frame.pack(fill=tk.X, pady=(1, 2))
            if not self.date_from_var.get() and not self.date_to_var.get():
                self._set_date_preset("30days")
        else:
            self.date_inputs_frame.pack_forget()

    def _set_date_preset(self, preset_type: str):
        today = datetime.date.today()
        self.use_date_filter_var.set(True)
        self.date_inputs_frame.pack(fill=tk.X, pady=(1, 2))

        if preset_type == "today":
            self.date_from_var.set(today.strftime("%Y-%m-%d"))
            self.date_to_var.set(today.strftime("%Y-%m-%d"))
        elif preset_type == "7days":
            start = today - datetime.timedelta(days=7)
            self.date_from_var.set(start.strftime("%Y-%m-%d"))
            self.date_to_var.set(today.strftime("%Y-%m-%d"))
        elif preset_type == "30days":
            start = today - datetime.timedelta(days=30)
            self.date_from_var.set(start.strftime("%Y-%m-%d"))
            self.date_to_var.set(today.strftime("%Y-%m-%d"))
        elif preset_type == "this_month":
            start = today.replace(day=1)
            self.date_from_var.set(start.strftime("%Y-%m-%d"))
            self.date_to_var.set(today.strftime("%Y-%m-%d"))
        elif preset_type == "this_year":
            start = today.replace(month=1, day=1)
            self.date_from_var.set(start.strftime("%Y-%m-%d"))
            self.date_to_var.set(today.strftime("%Y-%m-%d"))
        elif preset_type == "all":
            self.date_from_var.set("")
            self.date_to_var.set("")
            self.use_date_filter_var.set(False)
            self.date_inputs_frame.pack_forget()

    def _get_active_date_filter(self) -> tuple[datetime.date, datetime.date] | None:
        if not self.use_date_filter_var.get():
            return None

        d_from = parse_date_string(self.date_from_var.get())
        d_to = parse_date_string(self.date_to_var.get())

        if not d_from or not d_to:
            raise ValueError("Please provide both a valid 'From' and 'To' date in YYYY-MM-DD format.")
        if d_from > d_to:
            raise ValueError("'From Date' cannot be after 'To Date'.")

        return (d_from, d_to)

    # ---------------- SOURCE MODE TOGGLE & ADB DETECTION ----------------

    def _on_source_mode_changed(self):
        mode = self.source_mode_var.get()
        if mode == "ADB":
            self.local_frame.pack_forget()
            self.adb_frame.pack(fill=tk.X, pady=(0, 4))
            if not self.adb_devices and self.adb_bin:
                self.refresh_adb_devices()
        else:
            self.adb_frame.pack_forget()
            self.local_frame.pack(fill=tk.X, pady=(0, 4))

    def refresh_adb_devices(self):
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

                def _detect_folders_thread():
                    detected = detect_phone_media_folders(self.adb_bin, self.selected_device_serial, self.PHONE_PRESET_FOLDERS)
                    try:
                        self.root.after(0, lambda: (
                            self.combo_phone_dir.config(values=detected),
                            self.combo_clean_dir.config(values=detected)
                        ))
                    except Exception:
                        pass

                threading.Thread(target=_detect_folders_thread, daemon=True).start()

            elif state == "unauthorized":
                self.adb_status_var.set("⚠️ Phone Unauthorized! Unlock screen & tap 'Allow USB debugging'.")
                self.lbl_adb_status.config(fg="#D97706")
                self._log("WARNING", "Device is unauthorized. Check your phone's screen and tap 'Always allow'.")
            else:
                self.adb_status_var.set(f"⚠️ Device state: {state} ({first_dev['serial']})")
                self.lbl_adb_status.config(fg="#D97706")

    def run_precheck_count(self):
        if not self.adb_bin or not self.selected_device_serial:
            messagebox.showwarning("Device Not Ready", "Please connect your phone and ensure it is detected before pre-checking.")
            return

        try:
            date_filter = self._get_active_date_filter()
        except ValueError as ve:
            messagebox.showwarning("Invalid Date Range", str(ve))
            return

        raw_phone_folder = self.phone_dir_var.get().strip()
        phone_folder = normalize_phone_path(raw_phone_folder)
        target_dir = self.target_dir_var.get().strip()
        filter_str = f" [{date_filter[0]} to {date_filter[1]}]" if date_filter else " [All Dates]"

        self.precheck_status_var.set(f"Scanning '{phone_folder}' on phone and checking target storage...")
        self._log("INFO", f"Running pre-check scan on: {phone_folder}{filter_str}")

        def _scan_thread():
            target_index = None
            if self.skip_existing_var.get() and target_dir and os.path.isdir(target_dir):
                target_index = index_target_folder(target_dir)

            candidates, ignored_thumbs, outside_dates, already_in_target = adb_scan_camera_photos(
                self.adb_bin,
                self.selected_device_serial,
                phone_folder,
                date_filter,
                target_index
            )
            count = len(candidates)
            required_bytes = sum(sz for _, sz in candidates if sz > 0)
            req_str = format_bytes(required_bytes)
            free_bytes = get_free_disk_space(target_dir)

            msg = f"📊 Pre-check: {count:,} NEW photo(s) ({req_str}){filter_str}."
            if already_in_target > 0:
                msg += f" ({already_in_target:,} already in target skipped)"
            if outside_dates > 0:
                msg += f" ({outside_dates:,} outside date range skipped)"
            if ignored_thumbs > 0:
                msg += f" ({ignored_thumbs:,} thumbnails excluded)"

            not_enough_space = False
            free_str = ""
            shortfall_str = ""

            if free_bytes is not None:
                free_str = format_bytes(free_bytes)
                if required_bytes > free_bytes:
                    not_enough_space = True
                    shortfall_str = format_bytes(required_bytes - free_bytes)
                    msg += f" | ⚠️ LOW SPACE: Need {req_str}, only {free_str} free (Short by {shortfall_str})!"
                else:
                    msg += f" | ✅ Storage OK ({free_str} free on target)"
            else:
                msg += " | (Select Target Folder to verify free disk space)"

            def _apply_ui():
                self.precheck_status_var.set(msg)
                if not_enough_space:
                    self.precheck_banner.config(fg="#B91C1C", bg="#FEE2E2")
                    self._log(
                        "ERROR",
                        f"Pre-check Storage Warning: {count:,} new photos require {req_str}, "
                        f"but target drive only has {free_str} available (short by {shortfall_str})!"
                    )
                    messagebox.showwarning(
                        "Insufficient Target Storage",
                        f"⚠️ Your target drive does NOT have enough free space for this backup!\n\n"
                        f"• New Photos to Import: {count:,}\n"
                        f"• Space Required: {req_str}\n"
                        f"• Available on Target Drive: {free_str}\n"
                        f"• Shortfall: {shortfall_str}\n\n"
                        f"Please free up space on your target drive or narrow your Date Range filter before importing."
                    )
                else:
                    self.precheck_banner.config(fg="#0369A1", bg="#E0F2FE")
                    space_log = f" | Target Free Space: {free_str} (OK)" if free_bytes is not None else ""
                    self._log(
                        "SUCCESS",
                        f"Pre-check complete: {count:,} new photos ({req_str}) ready to transfer{space_log}. "
                        f"{already_in_target:,} already backed up, {outside_dates:,} outside date filter, {ignored_thumbs:,} thumbnails excluded."
                    )

            self.root.after(0, _apply_ui)

        threading.Thread(target=_scan_thread, daemon=True).start()

    # ---------------- SAFE DELETE (POST-VERIFICATION CLEANUP) ----------------

    def start_safe_delete(self):
        """
        Executes safe deletion of backed-up photos from a specific phone directory.
        Checks target directory index first: only files confirmed to exist in the target
        with exact matching sizes will be deleted!
        """
        target_dir = self.target_dir_var.get().strip()
        if not target_dir or not os.path.isdir(target_dir):
            messagebox.showwarning(
                "Target Folder Required",
                "Please select your Target Storage Folder first.\n\n"
                "Safe Delete needs to verify that your backup photos exist before allowing any deletions from the phone!"
            )
            return

        mode = self.source_mode_var.get()
        raw_clean_folder = self.clean_folder_var.get().strip()
        if not raw_clean_folder:
            messagebox.showwarning("Missing Folder", "Please select or enter the specific folder on your phone to clean.")
            return

        clean_folder = normalize_phone_path(raw_clean_folder) if mode == "ADB" else raw_clean_folder

        if mode == "ADB" and (not self.adb_bin or not self.selected_device_serial):
            messagebox.showwarning("Phone Not Connected", "Please connect phone via ADB first.")
            return

        # Fast Pre-Deletion Scan: Find verified photos vs unbacked photos
        self._log("INFO", "=" * 55)
        self._log("INFO", f"Safe Clean-Up requested on: {clean_folder}")
        self._log("INFO", "Verifying existing backup copies in target storage...")

        target_index = index_target_folder(target_dir)
        if not target_index:
            messagebox.showwarning(
                "Empty Backup Folder",
                f"The target storage folder:\n{target_dir}\n\ncontains no backed-up photos. Deletion aborted for safety!"
            )
            return

        # Scan candidate photos in clean_folder
        if mode == "ADB":
            candidates, _, _, _ = adb_scan_camera_photos(
                self.adb_bin,
                self.selected_device_serial,
                phone_folder=clean_folder,
                date_filter=None,
                target_index=None
            )
        else:
            candidates = []
            for root_path, _, filenames in os.walk(clean_folder):
                for fname in filenames:
                    if fname.lower().endswith((".jpg", ".jpeg")):
                        fp = os.path.join(root_path, fname)
                        candidates.append((fp, os.path.getsize(fp)))

        verified_delete_list: list[str] = []
        unbacked_count = 0
        total_bytes_to_free = 0

        for fpath, fsize in candidates:
            fname = os.path.basename(fpath).lower()
            if fname not in target_index:
                unbacked_count += 1
                continue

            if mode == "ADB" and fsize <= 0:
                sz_args = []
                if self.selected_device_serial:
                    sz_args.extend(["-s", self.selected_device_serial])
                sz_args.extend(["shell", "stat", "-c", "%s", f'"{fpath}"'])
                code, sz_out, _ = run_adb_command(self.adb_bin, sz_args, timeout=5)
                if code == 0 and sz_out.isdigit():
                    fsize = int(sz_out)

            if fsize in target_index[fname]:
                verified_delete_list.append(fpath)
                if fsize > 0:
                    total_bytes_to_free += fsize
            else:
                unbacked_count += 1

        verified_count = len(verified_delete_list)

        if verified_count == 0:
            messagebox.showinfo(
                "No Verified Photos Found",
                f"In '{clean_folder}':\n\n"
                f"No photos matched the files in your backup folder.\n"
                f"Nothing was deleted. (Preserved {unbacked_count} unbacked photos)."
            )
            self._log("WARNING", "Safe Delete: No matching verified files found in target. Nothing deleted.")
            return

        mb_freed = total_bytes_to_free / (1024 * 1024)
        confirm_msg = (
            f"⚠️ CONFIRM SAFE DELETION FROM PHONE\n\n"
            f"• Folder to Clean on Phone:\n  {clean_folder}\n\n"
            f"• Verified Backed-up Photos to Delete: {verified_count:,}\n"
            f"  (Estimated space freed: ~{mb_freed:.1f} MB)\n\n"
            f"• Unbacked Photos Preserved (Safe): {unbacked_count:,}\n"
            f"• Other Folders (e.g. DCIM/Camera): UNTOUCHED\n\n"
            f"These {verified_count:,} photos have been verified to 100% exist in your target drive.\n"
            f"Are you sure you want to permanently delete them from your phone to free up space?"
        )

        proceed = messagebox.askyesno("Confirm Phone Storage Clean-Up", confirm_msg, icon="warning")
        if not proceed:
            self._log("INFO", "Safe Clean-Up cancelled by user.")
            return

        # Execute Deletion in background worker
        self._set_ui_state(running=True)
        self.cancel_event.clear()
        self.progress_bar["maximum"] = verified_count
        self.progress_bar["value"] = 0
        self.status_var.set(f"Deleting {verified_count:,} verified photos from phone...")

        self.worker_thread = threading.Thread(
            target=self._worker_execute_safe_delete,
            args=(verified_delete_list, clean_folder, mode == "ADB"),
            daemon=True
        )
        self.worker_thread.start()

    def _worker_execute_safe_delete(self, files_to_delete: list[str], clean_folder: str, is_adb: bool):
        total = len(files_to_delete)
        deleted_count = 0
        error_count = 0

        for idx, fpath in enumerate(files_to_delete, start=1):
            if self.cancel_event.is_set():
                break

            fname = os.path.basename(fpath)
            self.msg_queue.put(("PROGRESS_UPDATE", (idx, total, f"Deleting: {fname}")))

            try:
                if is_adb:
                    rm_args = []
                    if self.selected_device_serial:
                        rm_args.extend(["-s", self.selected_device_serial])
                    rm_args.extend(["shell", "rm", f'"{fpath}"'])
                    code, _, err = run_adb_command(self.adb_bin, rm_args, timeout=10)
                    if code != 0:
                        error_count += 1
                        self.msg_queue.put(("LOG", ("ERROR", f"Failed to delete {fname} on phone: {err}")))
                        continue
                else:
                    os.remove(fpath)

                deleted_count += 1
                self.msg_queue.put(("LOG", ("DELETED", f"Safe deleted: {fname} (Backup confirmed)")))
            except Exception as e:
                error_count += 1
                self.msg_queue.put(("LOG", ("ERROR", f"Error deleting {fname}: {e}")))

        was_cancelled = self.cancel_event.is_set()
        self.msg_queue.put(("CLEAN_DONE", (deleted_count, total, clean_folder, error_count, was_cancelled)))

    def _browse_clean_folder(self):
        folder = filedialog.askdirectory(title="Select Folder to Clean")
        if folder:
            self.clean_folder_var.set(os.path.normpath(folder))
            self._log("INFO", f"Clean-up target folder selected: {self.clean_folder_var.get()}")

    def _set_clean_preset(self, preset: str):
        if preset == "whatsapp":
            for p in self.combo_clean_dir["values"]:
                if "com.whatsapp" in p.lower() and "media" in p.lower():
                    self.clean_folder_var.set(p)
                    self._log("INFO", f"Clean target toggled to WhatsApp Images: {p}")
                    return
            self.clean_folder_var.set(self.PHONE_PRESET_FOLDERS[2])
            self._log("INFO", f"Clean target toggled to WhatsApp Images: {self.PHONE_PRESET_FOLDERS[2]}")
        elif preset == "camera":
            self.clean_folder_var.set("/sdcard/DCIM/Camera")
            self._log("INFO", "Clean target toggled to Phone Camera: /sdcard/DCIM/Camera")
        elif preset == "dcim":
            self.clean_folder_var.set("/sdcard/DCIM")
            self._log("INFO", "Clean target toggled to Phone DCIM: /sdcard/DCIM")
        elif preset == "pictures_wa":
            self.clean_folder_var.set("/sdcard/Pictures/WhatsApp")
            self._log("INFO", "Clean target toggled to Pictures/WhatsApp: /sdcard/Pictures/WhatsApp")

    # ---------------- PRESET & PREVIEW CONTROLS ----------------

    def _update_preview(self):
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

    def _preset_year_month(self):
        self.opt_year_var.set(True)
        self.opt_month_var.set(True)
        self.opt_week_var.set(False)
        self.opt_day_var.set(False)
        self._update_preview()

    def _preset_year_month_day(self):
        self.opt_year_var.set(True)
        self.opt_month_var.set(True)
        self.opt_week_var.set(False)
        self.opt_day_var.set(True)
        self._update_preview()

    def _preset_all(self):
        self.opt_year_var.set(True)
        self.opt_month_var.set(True)
        self.opt_week_var.set(True)
        self.opt_day_var.set(True)
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
        self.chk_skip_existing.config(state=state)
        self.chk_use_date.config(state=state)
        self.ent_date_from.config(state=state)
        self.ent_date_to.config(state=state)
        self.rb_mode_adb.config(state=state)
        self.rb_mode_local.config(state=state)
        self.btn_refresh_adb.config(state=state)
        self.btn_precheck.config(state=state)
        self.btn_safe_delete.config(state=state)
        self.btn_browse_clean.config(state=state)
        self.btn_preset_clean_wa.config(state=state)
        self.btn_preset_clean_cam.config(state=state)
        self.btn_preset_clean_dcim.config(state=state)
        self.btn_preset_clean_pic.config(state=state)
        self.combo_phone_dir.config(state="disabled" if running else "normal")
        self.combo_clean_dir.config(state="disabled" if running else "normal")

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

        try:
            date_filter = self._get_active_date_filter()
        except ValueError as ve:
            messagebox.showwarning("Invalid Date Range", str(ve))
            return

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

        skip_existing = self.skip_existing_var.get()

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
            if date_filter:
                self._log("INFO", f"Date Range Active: {date_filter[0]} to {date_filter[1]}")
            if skip_existing:
                self._log("INFO", "Incremental mode: Files already in target folder will be skipped automatically.")

            self.worker_thread = threading.Thread(
                target=self._worker_local_transfer,
                args=(src, dest, y, m, w, d, is_copy_mode, date_filter, skip_existing),
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

            raw_phone_dir = self.phone_dir_var.get().strip()
            if not raw_phone_dir:
                messagebox.showwarning("Missing Folder", "Please specify a camera or WhatsApp folder on the phone.")
                return

            phone_dir = normalize_phone_path(raw_phone_dir)

            self._set_ui_state(running=True)
            self.cancel_event.clear()
            self.progress_bar["value"] = 0
            self.progress_percent_var.set("0%")
            self.status_var.set(f"Scanning phone folder: {phone_dir}...")

            self._log("INFO", "=" * 55)
            self._log("INFO", f"Starting ADB USB {'COPY' if is_copy_mode else 'MOVE'} import...")
            self._log("INFO", f"Phone Folder: {phone_dir} ➔ Target: {dest}")
            if date_filter:
                self._log("INFO", f"Date Range Active: {date_filter[0]} to {date_filter[1]}")
            if skip_existing:
                self._log("INFO", "Incremental mode: Files already in target storage will be skipped (no re-downloading).")

            self.worker_thread = threading.Thread(
                target=self._worker_adb_transfer,
                args=(phone_dir, dest, y, m, w, d, is_copy_mode, date_filter, skip_existing),
                daemon=True
            )
            self.worker_thread.start()

    def cancel_import(self):
        if self.is_running:
            self.cancel_event.set()
            self.status_var.set("Cancelling... finishing active file...")
            self._log("WARNING", "Cancellation requested by user. Terminating process safely...")

    # ---------------- BACKGROUND WORKERS ----------------

    def _worker_local_transfer(self, source_dir: str, target_dir: str, y: bool, m: bool, w: bool, d: bool, is_copy_mode: bool, date_filter, skip_existing: bool):
        try:
            candidate_files = []
            for root_path, _, filenames in os.walk(source_dir):
                if self.cancel_event.is_set():
                    break
                for fname in filenames:
                    if fname.lower().endswith((".jpg", ".jpeg")):
                        fp = os.path.join(root_path, fname)
                        try:
                            f_sz = os.path.getsize(fp)
                        except OSError:
                            f_sz = -1
                        candidate_files.append((fp, f_sz))

            target_index = index_target_folder(target_dir) if skip_existing else None
            self._process_file_batch(candidate_files, target_dir, y, m, w, d, is_copy_mode, is_adb=False, date_filter=date_filter, target_index=target_index)
        except Exception as ex:
            self.msg_queue.put(("FATAL_ERROR", str(ex)))

    def _worker_adb_transfer(self, phone_dir: str, target_dir: str, y: bool, m: bool, w: bool, d: bool, is_copy_mode: bool, date_filter, skip_existing: bool):
        try:
            self.msg_queue.put(("LOG", ("INFO", "Indexing target folder for incremental backup check...")))
            target_index = index_target_folder(target_dir) if skip_existing else None

            self.msg_queue.put(("LOG", ("INFO", f"Scanning phone folder '{phone_dir}' via ADB...")))
            candidate_files, ignored_count, outside_date_count, already_in_target = adb_scan_camera_photos(
                self.adb_bin,
                self.selected_device_serial,
                phone_folder=phone_dir,
                date_filter=date_filter,
                target_index=target_index
            )

            if already_in_target > 0:
                self.msg_queue.put(("LOG", ("SKIP", f"Incremental check: {already_in_target:,} photos already exist in target drive (skipped transfer).")))
            if ignored_count > 0:
                self.msg_queue.put(("LOG", ("INFO", f"Protected from thumbnails: {ignored_count:,} thumbnail/hidden files excluded.")))
            if outside_date_count > 0:
                self.msg_queue.put(("LOG", ("INFO", f"Date filter applied: {outside_date_count:,} photos outside date range excluded.")))

            self._process_file_batch(candidate_files, target_dir, y, m, w, d, is_copy_mode, is_adb=True, date_filter=date_filter, target_index=target_index)
        except Exception as ex:
            self.msg_queue.put(("FATAL_ERROR", str(ex)))

    def _process_file_batch(self, candidate_files: list[tuple[str, int]], target_dir: str, y: bool, m: bool, w: bool, d: bool, is_copy_mode: bool, is_adb: bool, date_filter, target_index: dict[str, set[int]] | None):
        if self.cancel_event.is_set():
            self.msg_queue.put(("CANCELLED", {"processed": 0, "total": 0}))
            return

        total_files = len(candidate_files)
        if total_files == 0:
            self.msg_queue.put(("NO_FILES", None))
            return

        # Pre-check available disk space on the target drive before transferring
        required_bytes = 0
        for fp, sz in candidate_files:
            lower_fn = os.path.basename(fp).lower()
            if target_index and sz > 0 and lower_fn in target_index and sz in target_index[lower_fn]:
                continue
            if sz > 0:
                required_bytes += sz

        free_bytes = get_free_disk_space(target_dir)
        if free_bytes is not None:
            if required_bytes > free_bytes:
                self.msg_queue.put(("INSUFFICIENT_SPACE", (required_bytes, free_bytes, target_dir)))
                return
            self.msg_queue.put((
                "LOG",
                ("INFO", f"Storage capacity check passed: Need ~{format_bytes(required_bytes)} | Available on target drive: {format_bytes(free_bytes)}.")
            ))

        self.msg_queue.put(("SCAN_DONE", total_files))

        stats = {"total": total_files, "processed": 0, "copied": 0, "moved": 0, "skipped": 0, "errors": 0}
        temp_cache_dir = tempfile.mkdtemp(prefix="phone_photo_import_")

        start_d, end_d = date_filter if date_filter else (None, None)

        try:
            for idx, item in enumerate(candidate_files, start=1):
                if self.cancel_event.is_set():
                    self.msg_queue.put(("CANCELLED", stats))
                    return

                file_path, known_remote_size = item
                filename = os.path.basename(file_path)
                lower_fn = filename.lower()

                self.msg_queue.put(("PROGRESS_UPDATE", (idx, total_files, filename)))

                try:
                    if is_adb and target_index and known_remote_size > 0:
                        if lower_fn in target_index and known_remote_size in target_index[lower_fn]:
                            stats["skipped"] += 1
                            self.msg_queue.put(("LOG", ("SKIP", f"Skipped (already in target archive): {filename}")))
                            continue

                    if not is_adb and target_index:
                        src_sz = os.path.getsize(file_path)
                        if lower_fn in target_index and src_sz in target_index[lower_fn]:
                            stats["skipped"] += 1
                            self.msg_queue.put(("LOG", ("SKIP", f"Skipped (already in target archive): {filename}")))
                            continue

                    if is_adb:
                        local_temp_file = os.path.join(temp_cache_dir, filename)
                        pull_args = []
                        if self.selected_device_serial:
                            pull_args.extend(["-s", self.selected_device_serial])
                        pull_args.extend(["pull", file_path, local_temp_file])

                        code, _, err = run_adb_command(self.adb_bin, pull_args, timeout=45)
                        if code != 0 or not os.path.isfile(local_temp_file):
                            stats["errors"] += 1
                            self.msg_queue.put(("LOG", ("ERROR", f"Failed pulling {filename} via ADB: {err}")))
                            continue

                        src_file_for_ops = local_temp_file
                    else:
                        src_file_for_ops = file_path

                    src_size = os.path.getsize(src_file_for_ops)
                    photo_date, date_source = extract_photo_date(src_file_for_ops)

                    if start_d and end_d:
                        p_date = photo_date.date()
                        if not (start_d <= p_date <= end_d):
                            stats["skipped"] += 1
                            self.msg_queue.put(("LOG", ("SKIP", f"Skipped (outside date range {p_date}): {filename}")))
                            if is_adb and os.path.isfile(src_file_for_ops):
                                os.remove(src_file_for_ops)
                            continue

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
                        if is_adb:
                            shutil.move(src_file_for_ops, dest_filepath)
                        else:
                            shutil.copy2(src_file_for_ops, dest_filepath)

                        if target_index is not None:
                            target_index.setdefault(lower_fn, set()).add(src_size)

                        if not is_copy_mode:
                            if os.path.exists(dest_filepath) and os.path.getsize(dest_filepath) == src_size:
                                if is_adb:
                                    rm_args = []
                                    if self.selected_device_serial:
                                        rm_args.extend(["-s", self.selected_device_serial])
                                    rm_args.extend(["shell", "rm", f'"{file_path}"'])
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
                    self.status_var.set(f"Found {total} new JPEG images to import. Processing...")
                    self._log("INFO", f"Scan complete: {total} new photos ready to import.")

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

                elif msg_type == "CLEAN_DONE":
                    deleted_count, total, folder, err_count, was_cancelled = payload
                    self._set_ui_state(running=False)
                    self.progress_bar["value"] = total
                    self.progress_percent_var.set("100%")
                    if was_cancelled:
                        self.status_var.set(f"Clean-up stopped by user. {deleted_count} of {total} photos removed.")
                        self._log("WARNING", f"Safe Clean-Up stopped by user after deleting {deleted_count} photos.")
                        messagebox.showwarning(
                            "Clean-up Stopped",
                            f"Safe Clean-Up was stopped by user.\n\n"
                            f"• Cleaned Folder: {folder}\n"
                            f"• Photos Deleted before Stop: {deleted_count:,} of {total:,}\n"
                            f"• Errors: {err_count}\n\n"
                            f"Remaining photos and all other folders were left untouched."
                        )
                    else:
                        self.status_var.set(f"Clean-up complete! {deleted_count} photos removed from phone.")
                        self._log("SUCCESS", f"Clean-up finished: {deleted_count} verified photos deleted from '{folder}'. Errors: {err_count}")
                        messagebox.showinfo(
                            "Clean-up Complete",
                            f"Safe Clean-Up Finished!\n\n"
                            f"• Cleaned Folder: {folder}\n"
                            f"• Verified Photos Deleted from Phone: {deleted_count:,}\n"
                            f"• Errors: {err_count}\n\n"
                            f"All other folders (such as DCIM/Camera) were left untouched."
                        )

                elif msg_type == "NO_FILES":
                    self._set_ui_state(running=False)
                    self.status_var.set("No new matching JPEG photos found.")
                    self._log("INFO", "All photos in this folder are already backed up or outside the selected date range.")
                    messagebox.showinfo("Scan Result", "No new JPEG photos to import!\n\nAll photos are either already in your target folder, outside the date range, or thumbnails.")

                elif msg_type == "INSUFFICIENT_SPACE":
                    req_bytes, free_bytes, t_dir = payload
                    req_s = format_bytes(req_bytes)
                    free_s = format_bytes(free_bytes)
                    short_s = format_bytes(req_bytes - free_bytes)
                    self._set_ui_state(running=False)
                    self.status_var.set(f"Stopped: Insufficient disk space on target ({free_s} free, need {req_s}).")
                    self._log("ERROR", f"ABORTED: Target drive has insufficient storage! Required: {req_s}, Available: {free_s} (Short by {short_s}).")
                    messagebox.showerror(
                        "Insufficient Disk Space",
                        f"Not enough free storage space on target drive!\n\n"
                        f"• Target Folder: {t_dir}\n"
                        f"• Space Required for Backup: {req_s}\n"
                        f"• Available Free Space: {free_s}\n"
                        f"• Shortfall: {short_s}\n\n"
                        f"Import aborted before transferring files so your drive doesn't fill up mid-backup."
                    )

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
        self._log("INFO", f"Summary: {stats['copied']} copied, {stats['moved']} moved, {stats['skipped']} skipped (already backed up), {stats['errors']} errors.")

        summary_msg = (
            f"Photo import finished!\n\n"
            f"• Total photos processed: {stats['total']}\n"
            f"• Successfully copied: {stats['copied']}\n"
            f"• Successfully moved: {stats['moved']}\n"
            f"• Skipped (already in target folder): {stats['skipped']}\n"
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
    self_test = "--self-test" in sys.argv
    root = tk.Tk()
    if self_test:
        root.withdraw()
    app = PhotoImporterApp(root)
    if self_test:
        root.update_idletasks()
        assert app.adb_bin and os.path.isfile(app.adb_bin), "Bundled ADB not found!"
        root.destroy()
        sys.exit(0)
    root.mainloop()


if __name__ == "__main__":
    main()
