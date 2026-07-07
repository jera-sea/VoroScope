import os
import datetime
import piexif
from PIL import Image

# ================= CONFIGURATION =================
# Replace this with the path to your folder containing the subfolders
ROOT_FOLDER = r"T:/PhD Ecology/Data/YST Scans/Odonata/December 2025 frames" 

# Time gap between images within the same stack (in seconds)
# We use a small number (0.1s) to ensure the whole stack fits within the 3-second limit.
INTRA_STACK_GAP = 0.1 

# Time gap between the last image of one folder and the start of the next (in seconds)
INTER_STACK_GAP = 10.0
# =================================================

def get_exif_timestamp(dt):
    """Formats datetime object into EXIF standard strings."""
    # Main format: "YYYY:MM:DD HH:MM:SS"
    date_str = dt.strftime("%Y:%m:%d %H:%M:%S")
    # Subsecond format: "ms" (milliseconds)
    subsec_str = str(int(dt.microsecond / 1000)).zfill(3)
    return date_str, subsec_str

def modify_exif(image_path, new_time):
    """Writes the new timestamp to the image EXIF data."""
    try:
        # Load EXIF data (or create empty dict if none exists)
        try:
            exif_dict = piexif.load(image_path)
        except Exception:
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

        time_str, subsec_str = get_exif_timestamp(new_time)

        # Update DateTimeOriginal (Tag 36867) in Exif IFD
        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = time_str.encode('utf-8')
        exif_dict["Exif"][piexif.ExifIFD.SubSecTimeOriginal] = subsec_str.encode('utf-8')
        
        # Also update DateTimeDigitized (Tag 36868) for consistency
        exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = time_str.encode('utf-8')
        exif_dict["Exif"][piexif.ExifIFD.SubSecTimeDigitized] = subsec_str.encode('utf-8')

        # Convert back to bytes and save
        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, image_path)
        
        return True
    except Exception as e:
        print(f"  [Error] Could not process {os.path.basename(image_path)}: {e}")
        return False

def main():
    if not os.path.exists(ROOT_FOLDER):
        print(f"Error: The folder '{ROOT_FOLDER}' does not exist.")
        return

    # Start time reference (Current time)
    current_time = datetime.datetime.now()
    
    # Get all subfolders and sort them to ensure sequence
    subfolders = sorted([f for f in os.listdir(ROOT_FOLDER) if os.path.isdir(os.path.join(ROOT_FOLDER, f))])
    
    print(f"Found {len(subfolders)} subfolders. Processing...")

    for folder_name in subfolders:
        folder_path = os.path.join(ROOT_FOLDER, folder_name)
        
        # Get all images in the subfolder
        # Filter for common image extensions
        valid_exts = ('.jpg', '.jpeg', '.tiff', '.tif')
        images = sorted([
            f for f in os.listdir(folder_path) 
            if f.lower().endswith(valid_exts)
        ])
        
        if not images:
            print(f"Skipping empty folder: {folder_name}")
            continue

        print(f"Processing stack: {folder_name} ({len(images)} images)")
        print(f"  - Start time: {current_time.strftime('%H:%M:%S.%f')[:-3]}")

        # --- PROCESS IMAGES IN THIS STACK ---
        for img_name in images:
            img_path = os.path.join(folder_path, img_name)
            modify_exif(img_path, current_time)
            
            # Increment time slightly for the next image in this stack
            # ensuring they stay "within 3 seconds" of each other.
            current_time += datetime.timedelta(seconds=INTRA_STACK_GAP)

        # --- PREPARE FOR NEXT STACK ---
        # The prompt requires the NEXT folder to start 10 seconds LATER 
        # than the timestamps from the previous folder.
        # current_time is currently at the end of the last stack.
        # We add the gap to push it forward.
        current_time += datetime.timedelta(seconds=INTER_STACK_GAP)

    print("\nProcessing complete!")

if __name__ == "__main__":
    main()