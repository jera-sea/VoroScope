"""
===============================================================================
BATCH H264 FRAME EXTRACTOR FOR HELICON FOCUS STACKING
===============================================================================

WHAT IT DOES
------------
Recursively scans an input folder for .h264 video files (e.g. Z-axis sweep
recordings from the sticky card scanner), and extracts every frame from each
video as a sequence of high-quality .jpg images, ready to feed into Helicon
Focus for stacking.

For each video found, it creates a matching output subfolder named from the
video's path (grandparent_parent_filename, e.g. "Batch1_LeftSide_Card003"),
and writes frames into it as Card003_0001.jpg, Card003_0002.jpg, etc.

Frames are extracted at maximum JPEG quality (-q:v 1, -qmin 1) using the
yuvj444p pixel format (full chroma, no 4:2:0 subsampling) to preserve as much
detail as possible for focus stacking.

Videos are processed in parallel (4 at a time) using a process pool, with
each ffmpeg extraction running 2 threads, to speed up large batches.

HOW TO USE
----------
1. Make sure ffmpeg is installed and available. The script looks for it in
   this order:
     a) System PATH
     b) The active conda environment's Library/bin folder
     c) A manual fallback path hardcoded in get_ffmpeg_path()
   If none are found, it will tell you to run:
       conda install -c conda-forge ffmpeg -y

2. Edit INPUT_DIR and OUTPUT_DIR at the bottom of the script (under
   `if __name__ == "__main__":`) to point to your source video folder and
   desired output folder.

3. Run the script directly:
       python extract_frames.py

   It will find all .h264 files under INPUT_DIR (any depth), extract frames
   for each into its own subfolder under OUTPUT_DIR, and print a per-video
   success/error line plus total elapsed time.

NOTES
-----
- Existing frames in an output subfolder will be overwritten (-y flag).
- If a video fails to extract, the batch continues; check the printed
  "Error on ..." lines for details.
- Folder naming assumes a 3-level structure: batch_folder/side_folder/video.h264
  If your folder structure differs, the stack_id naming logic in
  extract_single_video() will need adjusting.
===============================================================================
"""


import os
import subprocess
import time
import shutil
import sys
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor

def get_ffmpeg_path():
    """Locates the ffmpeg executable with absolute precision for Conda/Windows."""
    # 1. Check if it's already in the system PATH
    found_path = shutil.which("ffmpeg")
    if found_path:
        return found_path

    # 2. Look inside the current Conda environment (Standard Windows location)
    # sys.prefix gives the root of your conda env: c:\Users\Jeran\.conda\envs\imaging_env
    conda_ffmpeg = Path(sys.prefix) / "Library" / "bin" / "ffmpeg.exe"
    if conda_ffmpeg.exists():
        return str(conda_ffmpeg)

    # 3. Manual Fallback (Edit this if the above fails)
    manual_path = r'C:\Users\Jeran\.conda\envs\imaging_env\Library\bin\ffmpeg.exe'
    if os.path.exists(manual_path):
        return manual_path

    raise FileNotFoundError(
        f"FFmpeg not found at {conda_ffmpeg}.\n"
        "Please run: conda install -c conda-forge ffmpeg -y"
    )

def extract_single_video(video_info):
    """Function to be run in parallel."""
    video_path, output_root, ffmpeg_exe = video_info
    
    start_time = time.time()
    
    # Metadata for naming
    lvl3_filename = video_path.stem
    lvl2_side = video_path.parent.name
    lvl1_folder = video_path.parent.parent.name
    stack_id = f"{lvl1_folder}_{lvl2_side}_{lvl3_filename}"
    
    current_stack_dir = output_root / stack_id
    os.makedirs(current_stack_dir, exist_ok=True)

    output_pattern = str(current_stack_dir / f"{stack_id}_%04d.jpg")
    
    # FFmpeg Command: q:v 1 + yuvj444p for 100% Scientific Quality
    cmd = [
        ffmpeg_exe,
        '-y',
        '-i', str(video_path),
        '-q:v', '1',
        '-qmin', '1',
        '-pix_fmt', 'yuvj444p',
        '-threads', '2',
        output_pattern
    ]

    try:
        # We use shell=False for security and speed on Windows
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elapsed = time.time() - start_time
        return f"Success: {stack_id} ({elapsed:.2f}s)"
    except Exception as e:
        return f"Error on {stack_id}: {e}"

def process_helicon_batch_parallel(input_root, output_root):
    input_path = Path(input_root)
    output_path = Path(output_root)
    
    try:
        ffmpeg_exe = get_ffmpeg_path()
        print(f"Using FFmpeg at: {ffmpeg_exe}")
    except FileNotFoundError as e:
        print(e)
        return
    
    video_files = list(input_path.glob("**/*.h264"))
    if not video_files:
        print(f"No .h264 files found in {input_root}")
        return

    tasks = [(v, output_path, ffmpeg_exe) for v in video_files]

    print(f"Found {len(video_files)} videos. Processing with 4 parallel workers...")
    total_start = time.time()

    # We use 4 workers. Each worker processes one video at a time.
    # Total throughput should be 4x your previous speed.
    with ProcessPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(extract_single_video, tasks))

    for r in results:
        print(r)

    total_elapsed = time.time() - total_start
    print(f"\n--- Batch Complete in {total_elapsed:.2f}s ---")

if __name__ == "__main__":
    # Absolute paths for your machine
    INPUT_DIR = r'T:/PhD Ecology/Data/Yellow Sticky Traps/YST Scans/June 2026 Emergence Chambers/emergence_chambers_June_2026_Part_2'
    OUTPUT_DIR = r'T:/PhD Ecology/Data/Yellow Sticky Traps/YST Scans/June 2026 Emergence Chambers/emergence_chambers_June_2026_Part_2/frames'
    
    process_helicon_batch_parallel(INPUT_DIR, OUTPUT_DIR)