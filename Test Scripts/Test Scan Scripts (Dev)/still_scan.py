import requests
import os
import time
from picamera2 import Picamera2
import threading  # <-- Added for async saving
import queue      # <-- Added for async saving
import cv2        # <-- Added for saving arrays as images

# --- 1. CONFIGURATION ---

# Klipper (Moonraker) API URL
MOONRAKER_URL = "http://127.0.0.1:7125"

# Zig-Zag Scan Parameters
WIDTH = 16.0        # mm
HEIGHT = 20.0       # mm
STEPS_W = 4          # number of points in width
STEPS_H = 4          # number of points in height
SPEED = 5000         # mm/min for XY moves

# Delays from original macro (in seconds)
PRE_DELAY = 0.1      # Delay *before* stack
POST_DELAY = 0.1     # Delay *after* stack

# Focus Stack Parameters
Z_DROP_TOTAL = 10.0   # mm
Z_STEP_SIZE = 0.5    # mm
Z_SPEED_DOWN = 300   # mm/min (slow and steady)
Z_SPEED_UP = 1800    # mm/min (faster return)
VIBRATION_SETTLE_TIME = 0.2 # seconds to wait after Z move

# Camera & Output
CAMERA_RESOLUTION = (4056, 3040) 
SCAN_OUTPUT_DIR = os.path.expanduser("/home/pi/camera/ssd/scan_img")

# --- 2. HELPER FUNCTIONS ---

def send_gcode(command):
    """Sends a single G-code command (or block) to Moonraker and waits."""
    url = f"{MOONRAKER_URL}/printer/gcode/script"
    try:
        # Print only the first line if it's a block
        print(f"Sending: {command.splitlines()[0]}...") 
        response = requests.post(url, json={"script": command})
        response.raise_for_status() 
    except requests.exceptions.RequestException as e:
        print(f"Error sending G-code '{command}': {e}")
        print("Is Moonraker running and reachable?")

def get_toolhead_position():
    """Queries Moonraker for the toolhead's current position."""
    url = f"{MOONRAKER_URL}/printer/objects/query?toolhead=position"
    try:
        response = requests.get(url)
        response.raise_for_status()
        pos = response.json()['result']['status']['toolhead']['position']
        return pos[0], pos[1], pos[2] # Return x, y, z
    except Exception as e:
        print(f"Error getting toolhead position: {e}")
        return None, None, None

def init_camera():
    """Initializes the Picamera2 object and locks 3A controls."""
    print("Initializing camera...")
    cam = Picamera2()
    config = cam.create_still_configuration(main={"size": CAMERA_RESOLUTION})
    cam.configure(config)
    cam.start()
    
    print("Waiting for 3A controls (AE/AWB) to stabilize...")
    time.sleep(2.0) 
    
    # --- Optimization 2: Lock 3A controls ---
    print("Locking 3A controls.")
    cam.set_controls({"AeEnable": False, "AwbEnable": False})
    
    print("Camera initialized and controls locked.")
    return cam

# --- Optimization 1: New save_worker function ---
def save_worker(save_queue):
    """A worker thread that saves images from a queue."""
    print("Save worker thread started.")
    while True:
        job = save_queue.get()
        if job is None:
            break # Exit signal
        
        array, filename = job
        try:
            # Save the NumPy array as a JPEG
            # Convert RGB (picamera2) to BGR (OpenCV)
            cv2.imwrite(filename, cv2.cvtColor(array, cv2.COLOR_RGB2BGR))
            print(f"Saved: {os.path.basename(filename)}")
        except Exception as e:
            print(f"Error saving image {filename}: {e}")
        finally:
            save_queue.task_done()
    print("Save worker thread finished.")

def perform_focus_stack(cam, base_filename, original_z, save_queue):
    """
    Moves Z down, capturing to RAM at each step, and queues for saving.
    """
    num_steps = int(Z_DROP_TOTAL / Z_STEP_SIZE)
    print(f"Starting focus stack: {num_steps + 1} images.")

    send_gcode("G91") # Relative positioning
    
    for i in range(num_steps + 1):
        img_path = f"{base_filename}_z{i:03d}.jpg"
        
        # --- Optimization 1: Capture to RAM and queue ---
        print(f"Capturing to RAM: {os.path.basename(img_path)}")
        try:
            array = cam.capture_array("main") 
            save_queue.put((array, img_path))
        except Exception as e:
            print(f"Error capturing image to array: {e}")
        
        if i < num_steps:
            # --- Optimization 3: Batched G-code ---
            gcode_block = f"G1 Z-{Z_STEP_SIZE} F{Z_SPEED_DOWN}\nM400"
            send_gcode(gcode_block)
            time.sleep(VIBRATION_SETTLE_TIME)
    
    print("Stack complete. Returning to original Z height...")
    send_gcode("G90") # Absolute positioning
    
    # --- Optimization 3: Batched G-code ---
    gcode_block = f"G1 Z{original_z} F{Z_SPEED_UP}\nM400"
    send_gcode(gcode_block)

# --- 3. MAIN SCRIPT LOGIC ---

def main():
    # --- Optimization 1: Start save worker thread ---
    save_queue = queue.Queue()
    worker = threading.Thread(target=save_worker, args=(save_queue,), daemon=True)
    worker.start()

    # Setup camera and output directory
    camera = init_camera()
    run_id = time.strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(SCAN_OUTPUT_DIR, run_id)
    os.makedirs(run_dir, exist_ok=True)
    print(f"Saving all images to: {run_dir}")
    
    send_gcode("G90") # Absolute positioning

    start_x, start_y, start_z = get_toolhead_position()
    if start_x is None:
        print("Failed to get printer position. Aborting.")
        camera.stop()
        # Tell worker to exit even on failure
        save_queue.put(None)
        worker.join()
        return
        
    print(f"Scan starting from position: X{start_x} Y{start_y} Z{start_z}")
    
    x_step_size = WIDTH / (STEPS_W ) if STEPS_W > 1 else 0
    y_step_size = HEIGHT / (STEPS_H ) if STEPS_H > 1 else 0

    # --- Start Zig-Zag Loop ---
    for h_step in range(STEPS_H):
        y_pos = start_y + (h_step * y_step_size)
        is_even_row = (h_step % 2 == 0)
        
        w_range = range(STEPS_W)
        if not is_even_row:
            w_range = reversed(w_range) 
            
        for w_step in w_range:
            x_pos = start_x + (w_step * x_step_size)
                
            print(f"\n--- Moving to point (Row: {h_step}, Col: {w_step}) ---")
            print(f"Target: X{x_pos:.2f} Y{y_pos:.2f}")
            
            # --- Optimization 3: Batched G-code ---
            gcode_block = f"G0 X{x_pos:.2f} Y{y_pos:.2f} F{SPEED}\nM400"
            send_gcode(gcode_block)
            
            time.sleep(PRE_DELAY)
            
            point_dir = os.path.join(run_dir, f"point_{h_step:02d}_{w_step:02d}")
            os.makedirs(point_dir, exist_ok=True)
            base_filename = os.path.join(point_dir, "stack")
            
            # --- Optimization 1: Pass queue to function ---
            perform_focus_stack(camera, base_filename, start_z, save_queue)
            
            time.sleep(POST_DELAY)

    # --- Scan Complete ---
    print("\n--- Scan complete. Waiting for all images to save... ---")
    
    # --- Optimization 1: Wait for queue to empty and stop worker ---
    save_queue.join() 
    print("All images saved.")
    save_queue.put(None) # Send exit signal
    worker.join()        # Wait for thread to finish

    print("Returning to start position.")
    # --- Optimization 3: Batched G-code ---
    gcode_block = f"G0 X{start_x} Y{start_y} F{SPEED}\nM400"
    send_gcode(gcode_block)
    
    camera.stop()
    print("Camera stopped. Script finished.")

if __name__ == "__main__":
    main()
