import requests
import os
import time
import sys
import json
import threading
import queue
from PIL import Image  # Replaces cv2
from picamera2 import Picamera2

# --- 1. CONFIGURATION ---

# Klipper (Moonraker) API URL
MOONRAKER_URL = "http://127.0.0.1:7125"

# XY Zig-Zag Scan Parameters
WIDTH = 95.0        # mm (Total width of scan area)
HEIGHT = 190.0      # mm (Total height of scan area)
STEPS_W = 19        # Number of points in width
STEPS_H = 24        # Number of points in height
XY_SPEED = 5000     # mm/min for moving between points

# Z-Axis / Focus Parameters
Z_DROP_TOTAL = 6.0      # mm (Total depth to scan)
STACK_DURATION = 3.0    # Seconds (How long the Z move should take)
Z_SPEED_DOWN = ((Z_DROP_TOTAL / STACK_DURATION) * 60) + 0.3
Z_SPEED_UP = 1800       # mm/min (Fast return speed)

# Delays
PRE_STACK_DELAY = 0.3   # Time to settle before starting the Z move

# --- MANUAL CAMERA CALIBRATION ---
CAM_EXPOSURE_US = 30000
CAM_ANALOGUE_GAIN = 1.5657
CAM_AWB_RED = 3.8484
CAM_AWB_BLUE = 1.3040
# ---------------------------------------------------

# General Capture Settings
IMAGE_RESOLUTION = (4056, 3040) # 4K (Full Sensor)
FRAME_RATE = 10                 # Target FPS
SCAN_OUTPUT_DIR = os.path.expanduser("/home/pi/camera/ssd/scan_continuous_burst")

# --- BACKGROUND WRITER SETUP ---
# Queue stores tuples of: (list_of_numpy_arrays, output_folder_path)
write_queue = queue.Queue()

def background_writer():
    """Thread that continuously checks for buffers to save."""
    while True:
        task = write_queue.get()
        if task is None:
            break
        
        buffer, folder = task
        frame_count = len(buffer)
        print(f"  [DISK] Background saving {frame_count} images to {os.path.basename(folder)}...")
        
        try:
            os.makedirs(folder, exist_ok=True)
            for i, array in enumerate(buffer):
                # Convert Numpy Array -> PIL Image -> JPEG
                # PIL naturally handles RGB arrays correctly (No blue shift)
                img = Image.fromarray(array) 
                filename = os.path.join(folder, f"frame_{i:05d}.jpg")
                img.save(filename, quality=95)
                
        except Exception as e:
            print(f"  [ERROR] Background write failed: {e}")
        
        # Explicitly clear memory
        del buffer
        import gc
        gc.collect()
        
        print(f"  [DISK] Save complete.")
        write_queue.task_done()

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

def init_camera_manual():
    """
    Initializes the camera for still capture with manual controls.
    """
    print("Initializing camera with MANUAL settings...")
    cam = Picamera2()
    
    # Configure as RGB888. PIL expects RGB, so this fixes the Blue Shift.
    config = cam.create_still_configuration(main={"size": IMAGE_RESOLUTION, "format": "RGB888"})
    cam.configure(config)
    cam.start()
    
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
    time.sleep(1.0) # Settle time
    
    return cam, manual_controls

def save_scan_parameters(output_dir, start_pos, cam_settings):
    """Saves a verbose log of configuration."""
    filepath = os.path.join(output_dir, "scan_parameters.txt")
    try:
        with open(filepath, "w") as f:
            f.write("=== VERBOSE SCAN PARAMETERS (BURST RAM + THREADED IO) ===\n")
            f.write(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write("--- 1. AXIS POSITIONS (START) ---\n")
            f.write(f"Start X: {start_pos[0]}\n")
            f.write(f"Start Y: {start_pos[1]}\n")
            f.write(f"Start Z: {start_pos[2]}\n\n")
            f.write("--- 2. MANUAL CAMERA SETTINGS ---\n")
            if isinstance(cam_settings, dict):
                for key, value in cam_settings.items():
                    f.write(f"{key}: {value}\n")
            else:
                f.write(str(cam_settings))
            f.write("\n--- 3. CONFIGURATION VARIABLES ---\n")
            g = globals()
            for key in g:
                if key.isupper() and isinstance(g[key], (int, float, str, tuple)):
                    f.write(f"{key} = {g[key]}\n")
                    
        print(f"  [INFO] Parameters saved to {os.path.basename(filepath)}")
    except Exception as e:
        print(f"  [ERROR] Failed to save parameters file: {e}")

def perform_burst_capture_ram(cam, original_z):
    """Captures a sequence of images to RAM while moving Z."""
    
    # Check Queue Depth to prevent OOM (Out of Memory)
    # If disk is too slow, we pause here until the previous batch finishes.
    # 1 Batch (~30 frames 4K) is approx 1.1 GB RAM.
    while write_queue.qsize() > 0:
        print("  [RAM] Waiting for previous batch to write to disk...")
        time.sleep(0.1)
    
    frame_buffer = []
    
    # Calculate timing
    frame_interval = 1.0 / FRAME_RATE
    total_frames = int(STACK_DURATION * FRAME_RATE)
    
    print(f"    [BURST] Capturing {total_frames} frames to RAM...")

    # --- Start Z Move ---
    send_gcode("G91") 
    cmd = f"G1 Z-{Z_DROP_TOTAL} F{Z_SPEED_DOWN}" # Non-blocking move
    send_gcode(cmd)
    
    # --- Capture Loop ---
    start_time = time.time()
    
    for i in range(total_frames):
        loop_start = time.time()
        
        # Capture directly to numpy array (RGB format)
        image = cam.capture_array("main")
        frame_buffer.append(image)
        
        # Throttle
        elapsed = time.time() - loop_start
        sleep_time = frame_interval - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)
            
    # --- End Z Move ---
    send_gcode("M400") # Block until Z move is physically done
    
    # Return Z (Absolute)
    send_gcode("G90") 
    send_gcode(f"G1 Z{original_z} F{Z_SPEED_UP}")

    actual_duration = time.time() - start_time
    print(f"    [BURST] RAM Capture finished in {actual_duration:.2f}s")
    
    return frame_buffer

# --- 3. MAIN SCRIPT LOGIC ---

def main():
    camera = None
    start_x, start_y, start_z = None, None, None
    
    # Start Background Thread
    writer = threading.Thread(target=background_writer, daemon=True)
    writer.start()
    print("  [SYSTEM] Background IO thread started.")
    
    try:
        # 1. Initialize Printer and Get Position
        send_gcode("G90") 
        start_x, start_y, start_z = get_toolhead_position()
        
        if start_x is None:
            raise Exception("Could not get toolhead position from Moonraker.")
        print(f"Start Position: X{start_x} Y{start_y} Z{start_z}")

        # 2. Initialize Camera
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
                
                # 1. Move XY (While previous data writes to disk)
                send_gcode(f"G0 X{x_pos:.2f} Y{y_pos:.2f} F{XY_SPEED}")
                
                # 2. Ensure we arrived before starting capture
                send_gcode("M400")
                time.sleep(PRE_STACK_DELAY)
                
                # 3. Perform Burst Capture (Blocking, fills RAM)
                ram_buffer = perform_burst_capture_ram(camera, start_z)
                
                # 4. Offload to Background Thread (Non-blocking)
                stack_folder = os.path.join(run_dir, f"point_{h_step:02d}_{w_step:02d}")
                write_queue.put((ram_buffer, stack_folder))
                
                # Loop continues immediately to next XY move

        # Success Completion
        print("\n--- Scan Complete ---")
        print("Returning to start position...")
        send_gcode(f"G0 X{start_x} Y{start_y} F{XY_SPEED}")
        
        # Wait for final writes
        pending = write_queue.qsize()
        if pending > 0:
            print(f"  [SYSTEM] Waiting for background writes to finish...")
        write_queue.join()

    except KeyboardInterrupt:
        print("\n\n!!! USER INTERRUPT (CTRL+C) !!!")
        print("Halting operations immediately.")
        
    except Exception as e:
        print(f"\n!!! CRITICAL ERROR: {e}")

    finally:
        # Stop Writer Thread
        write_queue.put(None)
        writer.join()
        
        print("\n--- Shutting Down ---")
        if camera:
            try:
                camera.stop()
            except Exception:
                pass
        
        print("  [PRINTER] Resetting to Absolute Positioning (G90).")
        send_gcode("G90")
        print("Done.")
        sys.exit(0)

if __name__ == "__main__":
    main()