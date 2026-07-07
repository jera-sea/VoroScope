import os
from PIL import Image

def batch_convert_jpeg_to_tiff(source_dir, output_dir_name='tiff'):
    """
    Converts all JPEG/JPG images in a source directory to TIFF format
    and saves them to a subdirectory.

    Args:
        source_dir (str): The directory containing the JPEG images.
        output_dir_name (str): The name of the subdirectory to save the TIFF files.
    """
    # 1. Create the full path for the output directory
    output_dir = os.path.join(source_dir, output_dir_name)

    # 2. Check if the output directory exists, and create it if it doesn't
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"Created output directory: {output_dir}")

    conversion_count = 0
    # 3. Iterate through all files in the source directory
    for filename in os.listdir(source_dir):
        # 4. Check if the file is a JPEG/JPG image
        if filename.lower().endswith(('.jpeg', '.jpg')):
            try:
                # 5. Construct the full path for the input file
                input_path = os.path.join(source_dir, filename)

                # 6. Define the new filename with the .tiff extension
                #    Uses os.path.splitext to get the base name without the original extension
                base_name = os.path.splitext(filename)[0]
                output_filename = base_name + '.tiff'
                output_path = os.path.join(output_dir, output_filename)

                # 7. Open the image, save it in TIFF format, and close it
                with Image.open(input_path) as img:
                    img.save(output_path, format='TIFF')
                    conversion_count += 1
                    print(f"Converted '{filename}' to '{output_filename}'")

            except Exception as e:
                print(f"Error converting {filename}: {e}")

    print("-" * 30)
    print(f"Batch conversion complete. Total files converted: {conversion_count}")
    print(f"TIFF files saved in: {output_dir}")

# --- USAGE EXAMPLE ---

# IMPORTANT: Replace 'path/to/your/image/folder' with the actual path
# to the folder containing your JPEG images.
# Example: source_folder = r'C:\Users\YourName\Pictures\Photos'
source_folder = r'T:\Photos From Camera\Odonata Scanner Test 14-10-2025\3x test\Modified laplacian'

batch_convert_jpeg_to_tiff(source_folder)