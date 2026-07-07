"""
VoroScope Scan Processing Workflow
==================================

User Guide:
-----------
1. **Prerequisites**:
   - Ensure 'ffmpeg' is installed and accessible (or in the Conda environment).
   - Ensure 'Helicon Focus' is installed at the path defined in HELICON_PATH.

2. **Input Structure**:
   - Option A (Raw Videos): A folder containing .h264 video files.
     The script will extract frames into a subfolder named 'Extracted_Frames'.
   - Option B (Image Stacks): A folder containing subfolders, where each subfolder
     is a stack of images ready for processing.

3. **Workflow Steps**:
   - **Extraction**: (If videos are present) Extracts frames in parallel using FFmpeg.
   - **Timestamping**: Updates EXIF metadata of images to ensure they are time-sequential.
     This is often required for correct ordering in stacking software.
   - **Stacking**: Runs Helicon Focus on each stack folder to generate a single focused output image.
     Output images are saved in 'Processed_Stacks'.

4. **Usage**:
   - Run this script.
   - A dialog window will appear. Select the root folder containing your data.
   - Monitor the console for progress.
   - A message box will appear upon completion.

5. **Configuration**:
   - Adjust 'HELICON_PATH' if your installation location differs.
   - Adjust 'INTRA_STACK_GAP' and 'INTER_STACK_GAP' to change timestamp intervals.
"""
import os
import sys
import shutil
import subprocess
import time
import datetime
import re
import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
import piexif
from PIL import Image

# --- CONFIGURATION ---
HELICON_PATH = r"C:\Program Files\Helicon Software\Helicon Focus 8\HeliconFocus.exe"
INTRA_STACK_GAP = 0.1  # Seconds between images in a stack
INTER_STACK_GAP = 10.0 # Seconds between stacks

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

    # Manual Fallback
    manual_path = r'C:\Users\Jeran\.conda\envs\imaging_env\Library\bin\ffmpeg.exe'
    if os.path.exists(manual_path):
        return manual_path

    raise FileNotFoundError("FFmpeg not found. Please install it or check the path.")

def extract_single_video(video_info):
    """
    Extracts frames from a single video.
    video_info: (video_path, output_root, ffmpeg_exe, num_threads)
    """
    video_path, output_root, ffmpeg_exe, num_threads = video_info
    start_time = time.time()

    # Use the video filename (stem) as the stack ID (Folder Name)
    # This ensures the folder name starts with the Sample ID (e.g. HT5P3...)
    stack_id = f"{video_path.parent.name}_{video_path.stem}"
    
    current_stack_dir = output_root / stack_id
    os.makedirs(current_stack_dir, exist_ok=True)

    output_pattern = str(current_stack_dir / f"{stack_id}_%04d.jpg")
    
    # FFmpeg Command: High quality JPEG extraction
    cmd = [
        ffmpeg_exe,
        '-y',
        #'-hwaccel', 'cuda',
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
        return f"Extracted: {stack_id} ({elapsed:.2f}s)"
    except Exception as e:
        return f"Error extracting {stack_id}: {e}"

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

# --- PARSING LOGIC ---

def _timestamp_task_wrapper(task_info):
    """Helper for ProcessPoolExecutor to run modify_exif."""
    image_path, new_time = task_info
    if not modify_exif(image_path, new_time):
        return image_path # Return failed path for reporting
    return None

def parse_stack_filename(folder_name):
    """
    Parses the folder name to construct the output filename.
    Format: <Letter>T<number>P<number>_ ... _<string>_<number>
    Example: HT5P3_Odonata_December_2025 -> HT5P3_December_2025
    """
    return folder_name

# --- MAIN WORKFLOW STEPS ---

def run_extraction(input_dir, output_dir):
    print(f"\n--- Step 1: Video Extraction ---")
    try:
        ffmpeg_exe = get_ffmpeg_path()
    except FileNotFoundError as e:
        print(e)
        return False

    input_path = Path(input_dir)
    output_path = Path(output_dir)
    
    # Find all .h264 files
    video_files = list(input_path.glob("**/*.h264"))
    if not video_files:
        print("No .h264 files found. Skipping extraction.")
        return False

    print(f"Found {len(video_files)} videos. Extracting to: {output_dir}")
    
    # Optimization for multiple short videos (~30 frames):
    # Use all available CPU cores to process videos in parallel.
    # Use 1 thread per video to avoid CPU oversubscription and overhead on short clips.
    cpu_count = os.cpu_count() or 4
    workers = cpu_count
    threads_per_video = 1

    print(f"Starting extraction with {workers} workers and {threads_per_video} threads per video.")
    
    tasks = [(v, output_path, ffmpeg_exe, threads_per_video) for v in video_files]
    
    with ProcessPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(extract_single_video, tasks))
        
    for r in results:
        print(r)
        
    return True

def run_timestamping(target_dir):
    print(f"\n--- Step 2: Timestamping ---")
    if not os.path.exists(target_dir):
        print(f"Directory not found: {target_dir}")
        return
    
    subfolders = sorted([f for f in os.listdir(target_dir) if os.path.isdir(os.path.join(target_dir, f))])
    if not subfolders:
        print("No subfolders found for timestamping.")
        return

    print("Pre-calculating timestamps for all images...")
    tasks = []
    current_time = datetime.datetime.now()
    
    for folder_name in subfolders:
        folder_path = os.path.join(target_dir, folder_name)
        valid_exts = ('.jpg', '.jpeg', '.tiff', '.tif')
        images = sorted([f for f in os.listdir(folder_path) if f.lower().endswith(valid_exts)])
        
        if not images:
            continue

        for img_name in images:
            img_path = os.path.join(folder_path, img_name)
            tasks.append((img_path, current_time))
            current_time += datetime.timedelta(seconds=INTRA_STACK_GAP)
            
        current_time += datetime.timedelta(seconds=INTER_STACK_GAP)

    if not tasks:
        print("No images found to timestamp.")
        return

    print(f"Timestamping {len(tasks)} images across {len(subfolders)} stacks using {os.cpu_count()} workers...")
    
    errors = []
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as executor:
        results = list(executor.map(_timestamp_task_wrapper, tasks))
    
    for result in results:
        if result:  # A path was returned, indicating an error
            errors.append(result)

    if errors:
        print(f"  [Error] Failed to timestamp {len(errors)} images:")
        for img_path in errors:
            print(f"    - {os.path.basename(img_path)}")
            
    print("Timestamping complete.")

def run_stacking(source_dir, output_dir):
    print(f"\n--- Step 3: Helicon Focus Stacking ---")
    if not os.path.exists(HELICON_PATH):
        print(f"  [Error] Helicon Focus executable not found at: {HELICON_PATH}")
        return

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    subfolders = sorted([f for f in os.listdir(source_dir) if os.path.isdir(os.path.join(source_dir, f))])
    if not subfolders:
        print("No stacks found to process.")
        return

    total = len(subfolders)
    print(f"Found {total} stacks to process.")
    
    for i, folder_name in enumerate(subfolders, 1):
        input_folder_path = os.path.join(source_dir, folder_name)
        output_filename_base = parse_stack_filename(folder_name)
        output_file_path = os.path.join(output_dir, f"{output_filename_base}.jpg")
        
        print(f"[{i}/{total}] Stacking: {folder_name} -> {output_filename_base}.jpg")
        
        cmd = [
            HELICON_PATH,
            "-silent",
            "-m:B", "-r:50", "-s:4",
            input_folder_path,
            f"-save:{output_file_path}"
        ]
        
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError as e:
            print(f"  [Error] Helicon Focus failed for {folder_name}: {e}")
        except FileNotFoundError:
            print(f"  [Error] Helicon Focus executable not found at: {HELICON_PATH}")
            return

def main():
    # 1. UI Selection
    root = tk.Tk()
    root.withdraw() # Hide main window
    
    print("Please select the Root Folder containing videos or image stacks...")
    selected_dir = filedialog.askdirectory(title="Select Root Folder to Process")
    
    if not selected_dir:
        print("No folder selected. Exiting.")
        return

    print(f"Selected: {selected_dir}")
    
    # Define paths
    frames_dir = os.path.join(selected_dir, "Extracted_Frames")
    stacks_dir = os.path.join(selected_dir, "Processed_Stacks")

    # 2. Extraction
    # Check if we need to extract (look for h264 in selected dir)
    extracted = run_extraction(selected_dir, frames_dir)
    
    # If extraction happened, we work on frames_dir. 
    # If not, we assume the selected folder already contains the stack subfolders.
    working_dir = frames_dir if extracted else selected_dir
    
    # 3. Timestamping
    run_timestamping(working_dir)
    
    # 4. Stacking
    run_stacking(working_dir, stacks_dir)
    
    print("\n--- All Operations Complete ---")
    messagebox.showinfo("Complete", "Processing finished successfully!")

if __name__ == "__main__":
    main()