"""
VoroScope Batch Scan Stacking
=============================

Batch version of merged_workflow.py for long, unattended runs over a whole
month of scans. It processes one scan at a time, writes each scan's stacked
tiles into a folder of its own, can be stopped and restarted (it resumes),
and deletes each scan's extracted frames once that scan has been stacked.

User Guide:
-----------
1. **Prerequisites**:
   - 'ffmpeg' on PATH or in the active Conda environment (Library\\bin).
   - 'Helicon Focus' installed at the path defined in HELICON_PATH.
   - Python 3.8+ with piexif.

2. **Input Structure** (the root folder you select):
   - Option A (Raw Videos): subfolders, one per scan, each holding that
     scan's tile .h264 videos and its manifest.csv. A single scan folder
     (videos directly inside the root) also works.
   - Option B (Image Stacks): subfolders that are each a ready-made image
     stack (no .h264 videos anywhere under the root).

3. **Output Structure** (created inside the root folder):
       stacked_scans\\<scan name>\\<scan name>_tile_r_c.jpg   one stacked image per video
       stacked_scans\\<scan name>\\manifest.csv               copy of the scan's manifest
       stacked_scans\\batch_log.txt                          log of the run(s)
   Option B writes stacked_scans\\<stack name>.jpg instead.
   The input scan folders are never modified.

4. **Workflow per scan** (Option A):
   - **Extraction**: frames of each tile that has no stacked image yet are
     extracted to Extracted_Frames\\<scan name>\\<scan name>_<tile>\\ (FFmpeg, in parallel).
   - **Timestamping**: updates EXIF times so the frames are time-sequential,
     which stacking software uses for ordering.
   - **Stacking**: runs Helicon Focus on each tile's frames.
   - **Cleanup**: deletes the scan's extracted frames. Frames of tiles that
     failed to stack are kept so they can be inspected.
   Option B only timestamps and stacks; it never deletes anything.

5. **Usage**:
   - python batch_stack_scans.py "<root folder>"
     or run it without an argument and pick the root folder in the dialog.
   - Stopping (Ctrl+C or closing the window) and re-running on the same root
     is safe: tiles that already have a stacked image are skipped.
   - The PC is kept awake while the script runs.
   - A message box appears when the run ends.

6. **Configuration**:
   - Adjust 'HELICON_PATH' if your installation location differs.
   - 'HELICON_TIMEOUT' is how long one stack may take before it is treated as
     failed (guards against Helicon hanging on a hidden dialog).
   - 'MIN_FREE_GB': the run stops cleanly before a scan if the drive has less
     free space than this.
   - Adjust 'INTRA_STACK_GAP' and 'INTER_STACK_GAP' to change timestamp intervals.
"""
import os
import re
import sys
import shutil
import subprocess
import time
import datetime
import ctypes
import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
import piexif

# --- CONFIGURATION ---
HELICON_PATH = r"C:\Program Files\Helicon Software\Helicon Focus 8\HeliconFocus.exe"
HELICON_TIMEOUT = 600   # Seconds before a single stack is treated as failed
INTRA_STACK_GAP = 0.1   # Seconds between images in a stack
INTER_STACK_GAP = 10.0  # Seconds between stacks
MIN_FREE_GB = 5         # Stop before the next scan if less space than this is free

FRAMES_DIRNAME = "Extracted_Frames"
OUTPUT_DIRNAME = "stacked_scans"
MANIFEST_NAME = "manifest.csv"
IMAGE_EXTS = ('.jpg', '.jpeg', '.tiff', '.tif')
MAX_WORKERS = min(os.cpu_count() or 4, 61)  # ProcessPoolExecutor limit on Windows

LOG_FILE = None  # Set in main(); only the main process writes to it


class LowDiskSpace(Exception):
    pass

# --- GENERAL HELPERS ---

def log(msg):
    """Prints a message and appends it, timestamped, to the run log."""
    print(msg)
    if LOG_FILE:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{stamp}  {msg}\n")
        except OSError:
            pass

def natural_key(text):
    """Sort key that orders tile_0_2 before tile_0_10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', str(text))]

def format_elapsed(seconds):
    return str(datetime.timedelta(seconds=int(seconds)))

def is_done(output_file):
    """A tile counts as stacked once its output image exists and is not empty."""
    return output_file.is_file() and output_file.stat().st_size > 0

def check_free_space(path):
    free_gb = shutil.disk_usage(str(path)).free / 1024 ** 3
    if free_gb < MIN_FREE_GB:
        raise LowDiskSpace(f"Only {free_gb:.1f} GB free on the drive (minimum {MIN_FREE_GB} GB). "
                           f"Stopped before the next scan.")

def keep_awake(enable):
    """Stops Windows from sleeping while the run is in progress (no-op elsewhere)."""
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    try:
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enable else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass

# --- FFMPEG & EXTRACTION LOGIC ---

def get_ffmpeg_path():
    """Locates the ffmpeg executable."""
    found_path = shutil.which("ffmpeg")
    if found_path:
        return found_path

    # Check Conda environment
    conda_ffmpeg = Path(sys.prefix) / "Library" / "bin" / "ffmpeg.exe"
    if conda_ffmpeg.exists():
        return str(conda_ffmpeg)

    raise FileNotFoundError("FFmpeg not found. Activate the environment that contains it, "
                            "or add its folder to PATH.")

def get_stack_id(video_path):
    """Names a tile's frames folder and stacked image, same as merged_workflow.py."""
    return f"{video_path.parent.name}_{video_path.stem}"

def extract_single_video(video_info):
    """
    Extracts frames from a single video into frames_dir/<stack id>/.
    video_info: (video_path, frames_dir, ffmpeg_exe, num_threads)
    Returns (stack_id, ok, message).
    """
    video_path, frames_dir, ffmpeg_exe, num_threads = video_info
    start_time = time.time()

    stack_id = get_stack_id(video_path)
    current_stack_dir = frames_dir / stack_id
    os.makedirs(current_stack_dir, exist_ok=True)

    output_pattern = str(current_stack_dir / f"{stack_id}_%04d.jpg")

    # FFmpeg Command: High quality JPEG extraction
    cmd = [
        ffmpeg_exe,
        '-y',
        '-i', str(video_path),
        '-q:v', '1',
        '-qmin', '1',
        '-pix_fmt', 'yuvj444p',
        '-threads', str(num_threads),
        output_pattern
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elapsed = time.time() - start_time
        return stack_id, True, f"Extracted: {stack_id} ({elapsed:.2f}s)"
    except Exception as e:
        return stack_id, False, f"[Error] Extraction failed for {stack_id}: {e}"

# --- TIMESTAMPING LOGIC ---

def get_exif_timestamp(dt):
    """Formats datetime object into EXIF standard strings."""
    date_str = dt.strftime("%Y:%m:%d %H:%M:%S")
    subsec_str = str(int(dt.microsecond / 1000)).zfill(3)
    return date_str, subsec_str

def modify_exif(image_path, new_time):
    """Writes the new timestamp to the image EXIF data."""
    try:
        try:
            exif_dict = piexif.load(image_path)
        except Exception:
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

        time_str, subsec_str = get_exif_timestamp(new_time)

        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = time_str.encode('utf-8')
        exif_dict["Exif"][piexif.ExifIFD.SubSecTimeOriginal] = subsec_str.encode('utf-8')
        exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = time_str.encode('utf-8')
        exif_dict["Exif"][piexif.ExifIFD.SubSecTimeDigitized] = subsec_str.encode('utf-8')

        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, image_path)
        return True
    except Exception:
        # Let the caller handle error reporting for cleaner parallel logs
        return False

def _timestamp_task_wrapper(task_info):
    """Helper for ProcessPoolExecutor to run modify_exif."""
    image_path, new_time = task_info
    if not modify_exif(image_path, new_time):
        return image_path  # Return failed path for reporting
    return None

def list_images(folder):
    return sorted(f for f in os.listdir(folder) if f.lower().endswith(IMAGE_EXTS))

# --- MAIN WORKFLOW STEPS ---

def run_extraction(videos, frames_dir, ffmpeg_exe):
    """Extracts the given videos in parallel. Returns the set of stack ids that failed."""
    print(f"\n--- Step 1: Video Extraction ({len(videos)} videos) ---")

    # Many short videos (~30 frames): one video per CPU core, 1 thread each,
    # to avoid CPU oversubscription and overhead on short clips.
    tasks = [(v, frames_dir, ffmpeg_exe, 1) for v in videos]

    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
        results = list(executor.map(extract_single_video, tasks))

    failed = set()
    for stack_id, ok, message in results:
        if not ok:
            log(f"  {message}")
            failed.add(stack_id)

    print(f"Extracted {len(videos) - len(failed)}/{len(videos)} videos.")
    return failed

def run_timestamping(stack_dirs):
    """Gives the images in the given stack folders sequential EXIF timestamps."""
    print(f"\n--- Step 2: Timestamping ---")
    tasks = []
    current_time = datetime.datetime.now()

    for folder in stack_dirs:
        images = list_images(folder)
        if not images:
            continue

        for img_name in images:
            tasks.append((os.path.join(str(folder), img_name), current_time))
            current_time += datetime.timedelta(seconds=INTRA_STACK_GAP)

        current_time += datetime.timedelta(seconds=INTER_STACK_GAP)

    if not tasks:
        print("No images found to timestamp.")
        return

    print(f"Timestamping {len(tasks)} images across {len(stack_dirs)} stacks...")

    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
        results = list(executor.map(_timestamp_task_wrapper, tasks, chunksize=32))

    errors = [r for r in results if r]
    if errors:
        log(f"  [Error] Failed to timestamp {len(errors)} images, e.g. {os.path.basename(errors[0])}")

    print("Timestamping complete.")

def stack_one(input_folder, output_file):
    """Runs Helicon Focus on one stack folder. Returns an error message, or None on success."""
    cmd = [
        HELICON_PATH,
        "-silent",
        "-m:B", "-r:50", "-s:4",
        str(input_folder),
        f"-save:{output_file}"
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=HELICON_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"Helicon Focus timed out after {HELICON_TIMEOUT}s"
    except subprocess.CalledProcessError as e:
        return f"Helicon Focus exited with code {e.returncode}"

    if not is_done(output_file):
        return "Helicon Focus wrote no output image"
    return None

def run_stacking(stack_dirs, output_dir):
    """Stacks each folder into output_dir/<folder name>.jpg. Returns the names that failed."""
    print(f"\n--- Step 3: Helicon Focus Stacking ---")
    output_dir.mkdir(parents=True, exist_ok=True)

    failed = []
    total = len(stack_dirs)
    for i, folder in enumerate(stack_dirs, 1):
        output_file = output_dir / f"{folder.name}.jpg"
        if is_done(output_file):
            continue

        print(f"[{i}/{total}] Stacking: {folder.name} -> {output_file.name}")
        error = stack_one(folder, output_file)
        if error:
            log(f"  [Error] {folder.name}: {error}")
            failed.append(folder.name)

    return failed

def copy_manifest(scan_dir, output_dir):
    source = scan_dir / MANIFEST_NAME
    if not source.is_file():
        log(f"  [Warning] No {MANIFEST_NAME} found in {scan_dir}")
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(output_dir / MANIFEST_NAME))

# --- FRAME CLEANUP ---

def _is_inside(path, parent):
    """True if path is strictly inside parent (both resolved)."""
    path = os.path.realpath(str(path))
    parent = os.path.realpath(str(parent))
    try:
        return path != parent and os.path.commonpath([path, parent]) == parent
    except ValueError:  # Different drives
        return False

def cleanup_frames(frames_root, frames_dir, keep=()):
    """Deletes a scan's extracted frames, except the stack folders named in keep."""
    if not frames_dir.is_dir():
        return
    if not _is_inside(frames_dir, frames_root):
        log(f"  [Warning] Not deleting {frames_dir}: it is outside {frames_root}")
        return

    try:
        if not keep:
            shutil.rmtree(str(frames_dir))
            print("Deleted extracted frames.")
        else:
            for sub in frames_dir.iterdir():
                if sub.is_dir() and sub.name not in keep:
                    shutil.rmtree(str(sub))
            print(f"Deleted extracted frames, kept {len(keep)} failed stacks for inspection.")
    except OSError as e:
        log(f"  [Warning] Could not delete frames in {frames_dir}: {e}")

def remove_empty_dirs(top):
    """Removes top and any folders below it that are empty (bottom-up)."""
    if not os.path.isdir(top):
        return
    for dirpath, _, _ in os.walk(str(top), topdown=False):
        try:
            os.rmdir(dirpath)  # Only succeeds if the folder is empty
        except OSError:
            pass

# --- PER-SCAN PROCESSING ---

def find_scans(root):
    """Groups the .h264 videos under root by the folder they are in (one folder = one scan)."""
    scans = {}
    for video in root.glob("**/*.h264"):
        if video.relative_to(root).parts[0] in (FRAMES_DIRNAME, OUTPUT_DIRNAME):
            continue
        scans.setdefault(video.parent, []).append(video)

    return {scan_dir: sorted(scans[scan_dir], key=lambda v: natural_key(v.name))
            for scan_dir in sorted(scans, key=lambda d: natural_key(d.relative_to(root)))}

def process_scan(root, scan_dir, videos, ffmpeg_exe, label):
    """Extracts, timestamps and stacks one scan's unfinished tiles, then deletes its frames.
    Returns (stacked, already_done, failed) tile counts."""
    rel = scan_dir.relative_to(root)
    if not rel.parts:  # Root itself is the scan folder
        rel = Path(root.name)
    output_dir = root / OUTPUT_DIRNAME / rel
    frames_root = root / FRAMES_DIRNAME
    frames_dir = frames_root / rel

    pending = [v for v in videos if not is_done(output_dir / f"{get_stack_id(v)}.jpg")]
    already_done = len(videos) - len(pending)

    if not pending:
        log(f"=== {label}: {rel} [skip] all {len(videos)} tiles already stacked ===")
        copy_manifest(scan_dir, output_dir)
        cleanup_frames(frames_root, frames_dir)  # Leftovers from an interrupted run
        return 0, already_done, 0

    check_free_space(root)
    log(f"=== {label}: {rel} ({len(pending)} of {len(videos)} tiles to stack) ===")
    start_time = time.time()

    frames_dir.mkdir(parents=True, exist_ok=True)
    extract_failed = run_extraction(pending, frames_dir, ffmpeg_exe)

    stack_dirs = [frames_dir / get_stack_id(v) for v in pending if get_stack_id(v) not in extract_failed]
    run_timestamping(stack_dirs)
    stack_failed = run_stacking(stack_dirs, output_dir)

    copy_manifest(scan_dir, output_dir)
    cleanup_frames(frames_root, frames_dir, keep=set(stack_failed))

    failed = len(extract_failed) + len(stack_failed)
    stacked = len(pending) - failed
    log(f"    {rel}: {stacked} stacked, {failed} failed, {already_done} already done "
        f"({format_elapsed(time.time() - start_time)})")
    return stacked, already_done, failed

def process_stack_folders(root):
    """Option B: root holds ready-made image-stack folders. Nothing is deleted.
    Returns (stacked, already_done, failed) stack counts, or None if there are none."""
    output_dir = root / OUTPUT_DIRNAME
    stack_dirs = sorted((d for d in root.iterdir()
                         if d.is_dir() and d.name not in (FRAMES_DIRNAME, OUTPUT_DIRNAME)
                         and list_images(d)),
                        key=lambda d: natural_key(d.name))
    if not stack_dirs:
        return None

    pending = [d for d in stack_dirs if not is_done(output_dir / f"{d.name}.jpg")]
    already_done = len(stack_dirs) - len(pending)
    log(f"No .h264 videos found; {len(stack_dirs)} image stack folders "
        f"({len(pending)} to stack, {already_done} already done).")
    if not pending:
        return 0, already_done, 0

    check_free_space(root)
    run_timestamping(pending)
    failed = run_stacking(pending, output_dir)
    return len(pending) - len(failed), already_done, len(failed)

def main():
    global LOG_FILE

    # 1. Root folder: command-line argument, or UI selection
    tk_root = tk.Tk()
    tk_root.withdraw()  # Hide main window

    if len(sys.argv) > 1:
        selected_dir = sys.argv[1]
    else:
        print("Please select the Root Folder containing scan folders or image stacks...")
        selected_dir = filedialog.askdirectory(title="Select Root Folder to Process")

    if not selected_dir:
        print("No folder selected. Exiting.")
        return

    root = Path(os.path.abspath(selected_dir))
    if not root.is_dir():
        print(f"Folder not found: {root}")
        return

    (root / OUTPUT_DIRNAME).mkdir(exist_ok=True)
    LOG_FILE = root / OUTPUT_DIRNAME / "batch_log.txt"
    log(f"##### Run started on: {root}")

    if not os.path.exists(HELICON_PATH):
        log(f"[Error] Helicon Focus executable not found at: {HELICON_PATH}")
        messagebox.showerror("Helicon Focus not found", f"Not found at:\n{HELICON_PATH}\n\n"
                             "Edit HELICON_PATH at the top of the script.")
        return

    keep_awake(True)
    run_start = time.time()
    stacked = already_done = failed = 0
    stopped = None

    try:
        scans = find_scans(root)
        if scans:
            # 2. Option A: one scan folder at a time
            ffmpeg_exe = get_ffmpeg_path()
            total_tiles = sum(len(v) for v in scans.values())
            log(f"Found {len(scans)} scans with {total_tiles} videos.")

            for i, (scan_dir, videos) in enumerate(scans.items(), 1):
                s, d, f = process_scan(root, scan_dir, videos, ffmpeg_exe, f"Scan {i}/{len(scans)}")
                stacked += s
                already_done += d
                failed += f
                print(f"Overall: {stacked + already_done}/{total_tiles} tiles stacked, "
                      f"{failed} failed, {format_elapsed(time.time() - run_start)} elapsed.")

            remove_empty_dirs(root / FRAMES_DIRNAME)
        else:
            # 3. Option B: ready-made image stacks
            counts = process_stack_folders(root)
            if counts is None:
                stopped = "No .h264 videos or image stack folders found."
            else:
                stacked, already_done, failed = counts
    except FileNotFoundError as e:
        stopped = str(e)
    except LowDiskSpace as e:
        stopped = str(e)
    except KeyboardInterrupt:
        stopped = "Interrupted by user."
    finally:
        keep_awake(False)

    log(f"##### Run ended after {format_elapsed(time.time() - run_start)}: "
        f"{stacked} stacked, {already_done} already done, {failed} failed.")

    if stopped:
        log(f"[Stopped] {stopped}")
        messagebox.showwarning("Stopped", f"{stopped}\n\nRerun on the same folder to continue.")
    elif failed:
        messagebox.showwarning("Finished with errors",
                               f"{failed} tiles failed to stack.\nDetails: {LOG_FILE}\n\n"
                               "Rerun on the same folder to retry them.")
    else:
        messagebox.showinfo("Complete", "Processing finished successfully!")

if __name__ == "__main__":
    main()
