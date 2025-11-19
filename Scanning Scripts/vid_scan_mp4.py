import requests
import os
import time
import sys
import subprocess
import json
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder

# --- 1. CONFIGURATION ---

# Klipper (Moonraker) API URL
MOONRAKER_URL = "http://127.0.0.1:7125"

# XY Zig-Zag Scan Parameters
WIDTH = 95.0        # mm (Total width of scan area)
HEIGHT = 190.0      # mm (Total height of scan area)
STEPS_W = 10        # Number of points in width
STEPS_H = 15        # Number of points in height
XY_SPEED = 5000     # mm/min for moving between points

# Z-Axis / Focus Parameters
Z_DROP_TOTAL = 8.0       # mm (Total depth to scan)
STACK_DURATION = 3.0     # Seconds (How long the Z move should take)
Z_SPEED_DOWN = (Z_DROP_TOTAL / STACK_DURATION) * 60 
Z_SPEED_UP = 1800        # mm/min (Fast return speed)

# Delays
PRE_STACK_DELAY = 0.5     # Time to settle before starting the Z move
POST_STACK_DELAY = 0.1    # Time to wait after recording before moving XY

# --- MANUAL CAMERA CALIBRATION ---
# Update these values based on your manual tests!
# ---------------------------------------------------
CAM_EXPOSURE_US = 19714
CAM_ANALOGUE_GAIN = 1.4993
CAM_AWB_RED = 3.8374
CAM_AWB_BLUE = 1.3009

# CAM_EXPOSURE_US = 20000       # Exposure time in Microseconds (e.g., 20000 = 20ms)
# CAM_ANALOGUE_GAIN = 1.0       # Gain multiplier (1.0 = Base ISO)
# CAM_AWB_RED = 2.1             # Red Balance Gain (approx 0.0 to 4.0)
# CAM_AWB_BLUE = 1.6            # Blue Balance Gain (approx 0.0 to 4.0)
# ---------------------------------------------------

# General Video Settings
VIDEO_RESOLUTION = (4056, 3040) # 4K (Full Sensor)
FRAME_RATE = 10                 # FPS
BITRATE = 25000000              # 25Mbps
SCAN_OUTPUT_DIR = os.path.expanduser("/home/pi/camera/ssd/scan_continuous_4k")

# --- 2. HELPER FUNCTIONS ---

def get_toolhead_position():
    """Queries Moonraker for the toolhead's current position."""
    url = f"{MOONRAKER_URL}/printer/objects/query?toolhead=position"
    try:
        response = requests.get(url)
        response.raise_for_status()
        pos = response.json()['result']['status']['toolhead']['position']
        return pos[0], pos[1], pos[2] # Return x, y, z
    except Exception as e:
        print(f"  [ERROR] Getting position failed: {e}")
        return None, None, None

def send_gcode(command):
    """Sends a single G-code command (or block) to Moonraker and waits."""
    url = f"{MOONRAKER_URL}/printer/gcode/script"
    try:
        # Print only the first line for cleanliness
        print(f"  [PRINTER] Sending: {command.splitlines()[0]}...") 
        response = requests.post(url, json={"script": command})
        response.raise_for_status() 
    except requests.exceptions.RequestException as e:
        print(f"  [ERROR] G-code failed: {e}")

def convert_to_mp4(h264_path, mp4_path):
    """Converts raw H264 to MP4 using ffmpeg (subprocess)."""
    try:
        # -y overwrites output, -c copy is extremely fast (no re-encoding)
        cmd = [
            "ffmpeg", 
            "-framerate", str(FRAME_RATE), 
            "-i", h264_path, 
            "-c", "copy", 
            "-y", 
            mp4_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
        if os.path.exists(h264_path):
            os.remove(h264_path)
            
    except subprocess.CalledProcessError:
        print(f"  [WARN] FFMPEG conversion failed. Keeping {os.path.basename(h264_path)}")
    except Exception as e:
        print(f"  [WARN] MP4 Conversion error: {e}")

def init_camera_manual():
    """
    Initializes the camera and applies hardcoded MANUAL controls immediately.
    """
    print("Initializing camera with MANUAL settings...")
    cam = Picamera2()
    config = cam.create_video_configuration(main={"size": VIDEO_RESOLUTION, "format": "RGB888"})
    cam.configure(config)
    cam.start()
    
    # Define the Manual Controls
    # We set AeEnable and AwbEnable to False to ensure the camera doesn't override us
    manual_controls = {
        "FrameRate": FRAME_RATE,
        "AeEnable": False,
        "AwbEnable": False,
        "ExposureTime": CAM_EXPOSURE_US,
        "AnalogueGain": CAM_ANALOGUE_GAIN,
        "ColourGains": (CAM_AWB_RED, CAM_AWB_BLUE)
    }
    
    print(f"Applying Manual Controls: {manual_controls}")
    cam.set_controls(manual_controls)
    
    return cam, manual_controls

def save_scan_parameters(output_dir, start_pos, cam_settings):
    """
    Saves a verbose log of configuration variables and the manual camera settings applied.
    """
    filepath = os.path.join(output_dir, "scan_parameters.txt")
    
    try:
        with open(filepath, "w") as f:
            f.write("=== VERBOSE SCAN PARAMETERS (MANUAL MODE) ===\n")
            f.write(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            
            f.write("--- 1. AXIS POSITIONS (START) ---\n")
            f.write(f"Start X: {start_pos[0]}\n")
            f.write(f"Start Y: {start_pos[1]}\n")
            f.write(f"Start Z: {start_pos[2]}\n\n")
            
            f.write("--- 2. MANUAL CAMERA SETTINGS APPLIED ---\n")
            if isinstance(cam_settings, dict):
                for key, value in cam_settings.items():
                    f.write(f"{key}: {value}\n")
            else:
                f.write(str(cam_settings))

            f.write("\n--- 3. CONFIGURATION VARIABLES ---\n")
            # Dynamically grab all uppercase global variables
            g = globals()
            for key in g:
                if key.isupper() and isinstance(g[key], (int, float, str, tuple)):
                    f.write(f"{key} = {g[key]}\n")
                    
        print(f"  [INFO] Verbose parameters saved to {os.path.basename(filepath)}")
    except Exception as e:
        print(f"  [ERROR] Failed to save parameters file: {e}")

def perform_continuous_stack(cam, filename_mp4, original_z):
    """Records video to H264, then converts to MP4."""
    
    filename_h264 = filename_mp4.replace(".mp4", ".h264")
    encoder = H264Encoder(bitrate=BITRATE)
    
    print(f"    [REC] Starting recording: {os.path.basename(filename_mp4)}")
    
    # Start Recording
    cam.start_recording(encoder, filename_h264)

    # Move Z (Relative)
    send_gcode("G91") 
    cmd = f"G1 Z-{Z_DROP_TOTAL} F{Z_SPEED_DOWN}\nM400"
    send_gcode(cmd)
    
    # Stop Recording
    cam.stop_recording()
    
    # Return Z (Absolute)
    send_gcode("G90") 
    send_gcode(f"G1 Z{original_z} F{Z_SPEED_UP}")

    # Convert to MP4
    convert_to_mp4(filename_h264, filename_mp4)
    print("    [REC] Saved & Converted.")

# --- 3. MAIN SCRIPT LOGIC ---

def main():
    camera = None
    start_x, start_y, start_z = None, None, None
    
    try:
        # 1. Initialize Printer and Get Position
        send_gcode("G90") # Ensure Absolute Mode
        start_x, start_y, start_z = get_toolhead_position()
        
        if start_x is None:
            raise Exception("Could not get toolhead position from Moonraker.")
        print(f"Start Position: X{start_x} Y{start_y} Z{start_z}")

        # 2. Initialize Camera with Hardcoded Settings
        camera, manual_settings = init_camera_manual()
        
        # 3. Setup Output Directory
        run_id = time.strftime("%Y%m%d_%H%M%S")
        run_dir = os.path.join(SCAN_OUTPUT_DIR, run_id)
        os.makedirs(run_dir, exist_ok=True)
        print(f"Saving output to: {run_dir}")
        
        # 4. Save Verbose Parameters
        save_scan_parameters(run_dir, (start_x, start_y, start_z), manual_settings)
        
        x_step_size = WIDTH / (STEPS_W) if STEPS_W > 0 else 0
        y_step_size = HEIGHT / (STEPS_H) if STEPS_H > 0 else 0

        # --- Main Loop ---
        for h_step in range(STEPS_H):
            y_pos = start_y + (h_step * y_step_size)
            w_range = range(STEPS_W)
            if h_step % 2 != 0: w_range = reversed(w_range) 
                
            for w_step in w_range:
                x_pos = start_x + (w_step * x_step_size)
                print(f"\n--- Point {h_step}-{w_step} [X{x_pos:.2f} Y{y_pos:.2f}] ---")
                
                # Move XY
                send_gcode(f"G0 X{x_pos:.2f} Y{y_pos:.2f} F{XY_SPEED}\nM400")
                time.sleep(PRE_STACK_DELAY)
                
                # Record Stack (.mp4)
                video_filename = os.path.join(run_dir, f"point_{h_step:02d}_{w_step:02d}.mp4")
                perform_continuous_stack(camera, video_filename, start_z)
                
                time.sleep(POST_STACK_DELAY)

        # Success Completion
        print("\n--- Scan Complete ---")
        print("Returning to start position...")
        send_gcode(f"G0 X{start_x} Y{start_y} F{XY_SPEED}")

    except KeyboardInterrupt:
        print("\n\n!!! USER INTERRUPT (CTRL+C) !!!")
        print("Halting operations immediately.")
        
    except Exception as e:
        print(f"\n!!! CRITICAL ERROR: {e}")

    finally:
        print("\n--- Shutting Down ---")
        
        # 1. Secure the Camera
        if camera:
            try:
                camera.stop_recording()
            except Exception:
                pass 
            try:
                camera.stop()
            except Exception:
                pass
        
        # 2. Secure the Printer
        print("  [PRINTER] Resetting to Absolute Positioning (G90).")
        send_gcode("G90")
        
        print("Done.")
        sys.exit(0)

if __name__ == "__main__":
    main()