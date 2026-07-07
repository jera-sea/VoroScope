import cv2
import os

# --- CONFIGURATION ---
# Replace this path with the folder containing your video files
SOURCE_DIRECTORY = r"T:/Photos From Camera/Odonata/HT5P3 December 2025/20251118_201010"

def extract_frames_from_folder(source_dir):
    """
    Scans the source directory for .mp4 and .h264 files, 
    and extracts every frame, saving them to subdirectories in an 'images' folder.
    """
    # 1. Verify source directory exists
    if not os.path.exists(source_dir):
        print(f"Error: The directory '{source_dir}' does not exist.")
        return

    # 2. Create the main 'images' output folder
    images_main_dir = os.path.join(source_dir, "images")
    os.makedirs(images_main_dir, exist_ok=True)
    print(f"Output directory created at: {images_main_dir}")

    # 3. List all files in the directory, checking for both extensions
    # Use a tuple for the extensions check
    VIDEO_EXTENSIONS = (".mp4", ".h264")
    
    files = [f for f in os.listdir(source_dir) if f.lower().endswith(VIDEO_EXTENSIONS)]

    if not files:
        print(f"No {', '.join(VIDEO_EXTENSIONS)} files found in the source directory.")
        return

    print(f"Found {len(files)} videos to process.")

    # 4. Loop through each video file
    for video_file in files:
        video_path = os.path.join(source_dir, video_file)
        
        # Get filename without extension for the subfolder name
        video_name_stem = os.path.splitext(video_file)[0]
        
        # Create the subfolder for this specific video
        specific_output_dir = os.path.join(images_main_dir, video_name_stem)
        os.makedirs(specific_output_dir, exist_ok=True)

        print(f"\nProcessing: {video_file} -> {specific_output_dir}")

        # Open the video
        cap = cv2.VideoCapture(video_path)
        
        if not cap.isOpened():
            print(f"  Error opening video file: {video_file}. Check file path and codecs.")
            continue

        frame_count = 0
        
        while True:
            # Read a new frame
            ret, frame = cap.read()

            # If frame is not returned, we reached the end of the video
            if not ret:
                break

            # Construct filename (e.g., frame_00001.jpg)
            frame_filename = f"frame_{frame_count:05d}.jpg"
            frame_path = os.path.join(specific_output_dir, frame_filename)

            # Save the frame as a JPEG
            cv2.imwrite(frame_path, frame)
            
            frame_count += 1

        cap.release()
        print(f"  -> Completed. Extracted {frame_count} frames.")

    print("\nAll processing finished.")

if __name__ == "__main__":
    extract_frames_from_folder(SOURCE_DIRECTORY)