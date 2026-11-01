# Phone Photo Importer & Sorter

A high-performance, safety-first desktop utility built with Python and Tkinter designed to scan, organize, and back up JPEG photos from Android phones (via ADB) or local/mounted drives into neatly structured, chronological date folders.

---

## Table of Contents
- [Key Features](#key-features)
- [How Safety & Space Verification Works](#how-safety--space-verification-works)
- [System Requirements](#system-requirements)
- [Quick Start & Installation](#quick-start--installation)
- [User Guide (Walkthrough)](#user-guide-walkthrough)
  - [Section 1: Source & Target Selection](#section-1-source--target-selection)
  - [Section 2: Date Range Filter (Optional)](#section-2-date-range-filter-optional)
  - [Section 3: Folder Hierarchy Builder](#section-3-folder-hierarchy-builder)
  - [Section 4: Transfer Mode & Incremental Backup](#section-4-transfer-mode--incremental-backup)
  - [Section 5: Execution & Progress](#section-5-execution--progress)
  - [Section 6: Safe Clean-Up (Verified Deletion)](#section-6-safe-clean-up-verified-deletion)
- [Architecture & Internal Details (For Developers & AI)](#architecture--internal-details-for-developers--ai)
- [Troubleshooting & FAQ](#troubleshooting--faq)

---

## Key Features

1. **Dual Source Modes:**
   - **Android USB via ADB:** Direct high-speed connection over USB. Automatically detects connected devices and scans device folders.
   - **Local / Mapped PC Folder:** Point to any Windows drive letter, thumb drive, SD card reader, or Wi-Fi network share (SMB).
2. **Pre-flight Disk Capacity Validator:**
   - Calculates the total size of photos queued for transfer.
   - Checks target drive free space before writing anything to disk.
   - **Aborts immediately** if the drive does not have enough capacity, avoiding partial or corrupted transfers.
3. **Smart Incremental Backup ("Dumb-Proof"):**
   - Indexes files in the destination directory using file names and byte sizes.
   - Skips already-backed-up photos instantly over USB without transferring unnecessary bytes.
4. **Human-in-the-Loop Safe Clean-Up:**
   - Deletion is completely decoupled from copying.
   - Files on the phone are **only eligible for deletion if an exact copy (matching filename and identical byte size) is verified in the backup folder**.
   - Selective targeting allows you to clear WhatsApp images to reclaim space while keeping your Camera roll untouched.
5. **Specialized WhatsApp & Android Support:**
   - Detects standard and multi-account WhatsApp paths (e.g., `/Android/media/com.whatsapp/WhatsApp/accounts/...`).
   - Parses dates from filenames (e.g., `IMG-YYYYMMDD-WA...`) even when EXIF metadata has been stripped by messaging apps.
   - Automatically ignores `.thumbnails`, `.trashed`, `.cache`, and hidden system files.
6. **Chronological Folder Organization:**
   - Sorts photos by year, month, week, or day.
   - Uses numbered month names (e.g., `1. January`, `2. February`, ..., `12. December`) so that Windows File Explorer displays them chronologically rather than alphabetically.

---

## How Safety & Space Verification Works

### 1. Storage Capacity Validator (Before Copying)
```
[User clicks "Start Transfer"]
          │
          ▼
Calculate total bytes required for all new candidate photos
          │
          ▼
Query destination disk free space (shutil.disk_usage)
          │
    ┌─────┴───────────────────────────────────────────┐
    │                                                 │
    ▼                                                 ▼
[Required Bytes > Free Disk Space]           [Free Disk Space >= Required Bytes]
    │                                                 │
    ▼                                                 ▼
• Abort immediately                               • Log storage confirmation
• Display error dialog with shortfall info        • Proceed with backup transfer
• No files written, USB transfer stopped
```

### 2. Double-Check Deletion Guard (Safe Clean-Up)
```
[User initiates Safe Delete]
          │
          ▼
Index all backup photos in target drive: { filename -> set of sizes in bytes }
          │
          ▼
Scan phone/source folder candidates
          │
    ┌─────┴───────────────────────────────────────────┐
    │                                                 │
    ▼                                                 ▼
[Size & Name match backup copy]             [No exact match in target folder]
    │                                                 │
    ▼                                                 ▼
Added to deletion candidate list            PROTECTED (Never deleted)
    │
    ▼
User prompted with confirmation modal & exact file count
    │
    ▼
Files deleted only after explicit user approval
```

---

## System Requirements

- **Operating System:** Windows 10 or Windows 11 (64-bit).
- **Python:** Python 3.10+ (if running from source code).
- **Android Phone Requirements (for ADB Mode):**
  - Developer Options enabled.
  - USB Debugging turned ON.
  - USB connection set to File Transfer (MTP) / ADB.
- **Included Tools:** Comes with a bundled `platform-tools/` directory containing `adb.exe`.

---

## Quick Start & Installation

### Option A: Running the Pre-built Standalone App
If you have the compiled executable:
1. Navigate to the `dist/` folder.
2. Launch `PhonePhotoImporter.exe`.

### Option B: Running from Source
1. Clone or download this repository.
2. Ensure Python 3.10 or newer is installed:
   ```powershell
   python --version
   ```
3. (Optional) Create and activate a virtual environment:
   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   ```
4. Run the program:
   ```powershell
   python photo_importer.py
   ```

---

## User Guide (Walkthrough)

The interface is structured into six numbered sections:

### Section 1: Source & Target Selection
- **Source Mode:** Choose **Android USB (ADB)** for directly connected Android phones, or **Local / Mapped Folder** for SD cards, flash drives, or network folders.
- **Android Device & Folders:** Click **"Detect Phone"** to identify your connected phone. Use preset dropdown buttons to select common photo locations (`/sdcard/DCIM/Camera`, `WhatsApp Images`, `Pictures`, etc.).
- **Target Folder:** Click **"Browse Target..."** to select your backup destination (e.g., an external hard drive or USB thumb drive).

### Section 2: Date Range Filter (Optional)
- Check **"Enable Date Filter"** to only copy photos taken within a specific timeframe.
- Click quick presets like **"Today"**, **"Last 7 Days"**, **"Last 30 Days"**, **"This Month"**, or **"This Year"**, or enter custom dates in `YYYY-MM-DD` format.

### Section 3: Folder Hierarchy Builder
- Customize how destination folders are organized on your backup drive:
  - `[x] Year` → `2026/`
  - `[x] Month (Numbered)` → `2026/10. October/`
  - `[ ] Week` → `Week 42/`
  - `[ ] Day` → `Day 13/`
- Use quick preset buttons (such as **"Year + Month"** or **"Year + Month + Day"**) to configure common structures.
- A live preview updates at the bottom of the card showing an example path.

### Section 4: Transfer Mode & Incremental Backup
- **Operation:**
  - **Copy (Safe Backup - Leaves phone files intact):** Default and recommended mode.
  - **Move (Copy + Delete from source):** Transfers and immediately removes source files.
- **Skip Already Imported:** Checked by default. Compares incoming files against destination files to prevent duplicate copies.

### Section 5: Execution & Progress
- **Pre-Check Count:** Click to run a dry-run check. Reports how many new photos will be imported, how many will be skipped, and verifies disk space without transferring files.
- **Start Transfer:** Begins the import process.
- **Cancel Transfer:** Safely stops the queue at any time.

### Section 6: Safe Clean-Up (Verified Deletion)
- Designed to free up internal phone storage (such as bulky WhatsApp image folders) safely.
- Select the folder to clean up.
- Click **"Verify & Clean Phone Storage"**. The utility verifies that every candidate exists in your backup target folder with an identical file size before prompting you to confirm deletion.

---

## Architecture & Internal Details (For Developers & AI)

- **Entry Point:** [`photo_importer.py`](photo_importer.py) / [`photo_importer.pyw`](photo_importer.pyw)
- **GUI Engine:** Python `tkinter` & `ttk` with custom styling and light-mode color palette.
- **Threading Model:** Background worker threads communicate with the UI thread using a thread-safe `queue.Queue()`, preventing UI freezes during long transfers.
- **Key Subsystems:**
  - `find_adb_executable()`: Resolves local, system, and PyInstaller bundled `adb.exe` paths.
  - `index_target_folder()`: Creates an in-memory dictionary (`lowercase_filename -> set(file_sizes)`) for instant O(1) duplicate checks.
  - `get_free_disk_space()`: Uses `shutil.disk_usage` on the target volume.
  - `extract_photo_date()`: Multi-tier fallback date detector:
    1. EXIF metadata via direct binary parsing (`read_jpeg_exif_datetime`).
    2. Filename timestamp patterns (`IMG_YYYYMMDD_...`, `IMG-YYYYMMDD-WA...`).
    3. File system modification time.
  - `resolve_collision_path()`: Safely appends incremental counter suffixes (`_1`, `_2`) if different images share identical file names.

---

## Troubleshooting & FAQ

#### Q: The program says "No ADB found" or doesn't detect my phone.
1. Make sure your phone has **USB Debugging** enabled under Developer Options.
2. Unplug and reconnect the USB cable, then unlock your phone screen and tap **"Always allow from this computer"** when prompted.
3. Ensure the `platform-tools/` folder containing `adb.exe` is located in the application directory, or that `adb` is in your Windows system PATH.

#### Q: What happens if my target drive runs out of space?
The application performs a pre-flight disk check before any files are copied. If the total size of pending files exceeds the target drive's free capacity, the operation aborts with a clear alert displaying the required space and shortfall.

#### Q: Why are my WhatsApp photos dated correctly even though WhatsApp strips EXIF data?
The tool includes a dedicated filename parser (`IMG-YYYYMMDD-WA...`) that extracts the date directly from WhatsApp's standard file naming scheme when EXIF tags are absent.
