import requests
import os
import time
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder 
from libcamera import controls

# --- 1. CONFIGURATION ---

# Klipper (Moonraker) API URL
MOONRAKER_URL = "http://127.0.0.1:7125"

# Zig-Zag Scan Parameters
WIDTH = 16.0        # mm
HEIGHT = 20.0       # mm
STEPS_W = 4          # number of points in width
STEPS_H = 4          # number of points in height
SPEED = 5000         # mm/min for XY moves

# Delays
PRE_DELAY = 0.1      # Delay *before* stack
POST_DELAY = 0.1     # Delay *after* stack

# Focus Stack Parameters
Z_DROP_TOTAL = 10.0   # mm
Z_STEP_SIZE = 0.5    # mm
Z_SPEED_DOWN = 300   # mm/min (slow and steady)
Z_SPEED_UP = 1800    # mm/min (faster return)
# This time MUST be long enough for the printer vibration to settle (e.g., 0.2s)
# AND to capture at least one full frame (0.17s for 10 FPS). 0.2s is sufficient.
VIBRATION_SETTLE_TIME = 0.2 # seconds to wait after Z move 

# Camera & Output
# Maximum Resolution for Video Stream
VIDEO_RESOLUTION = (4056, 3040) 
FRAME_RATE = 10                     # Frames per second
SCAN_OUTPUT_DIR = os.path.expanduser("/home/pi/camera/ssd/scan_video")


# --- 2. HELPER FUNCTIONS ---

def send_gcode(command):
    """Sends a single G-code command (or block) to Moonraker and waits."""
    url = f"{MOONRAKER_URL}/printer/gcode/script"
    try:
        # Optimization: Only print the first command if it's a block
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
    """Initializes the Picamera2 object for max-resolution video and locks 3A controls."""
    print("Initializing camera for max-resolution video...")
    cam = Picamera2()
    
    # Configure for max-resolution video streaming
    config = cam.create_video_configuration(main={"size": VIDEO_RESOLUTION, "format": "RGB888"})
    cam.configure(config)
    
    # Set the frame rate
    cam.set_controls({"FrameRate": FRAME_RATE})
    
    cam.start()
    
    print("Waiting for 3A controls (AE/AWB) to stabilize...")
    time.sleep(2.0) 
    
    # Optimization: Lock 3A controls for consistent exposure and faster capture
    print("Locking 3A controls.")
    cam.set_controls({"AeEnable": False, "AwbEnable": False})
    
    print(f"Camera initialized at {VIDEO_RESOLUTION} @ {FRAME_RATE} FPS.")
    return cam

def perform_video_stack(cam, filename, original_z):
    """
    Starts video recording, moves Z down in steps, relying on the Settle Time 
    to capture stationary frames, then returns to the original Z height.
    """
    num_steps = int(Z_DROP_TOTAL / Z_STEP_SIZE)
    print(f"Starting video stack: {num_steps + 1} stationary points.")
    
    # Initialize the H264 Encoder (25Mbps bitrate for high quality at max resolution)
    encoder = H264Encoder(bitrate=25000000) 

    # Start Recording
    print(f"Starting recording to {os.path.basename(filename)}")
    cam.start_recording(encoder, filename)

    send_gcode("G91") # Relative positioning
    
    for i in range(num_steps + 1): 
        
        # The capture of the stationary frame occurs during the settling window (VIBRATION_SETTLE_TIME)
        print(f"Step {i:02d}/{num_steps}: Capturing stationary frame...")
        
        # The first stationary frame for position i is captured immediately after start_recording (and after the previous move/settle)
        
        # Move Z for the *next* step (if not the last one)
        if i < num_steps:
            # Optimization: Batched G-code for move and wait (M400)
            gcode_block = f"G1 Z-{Z_STEP_SIZE} F{Z_SPEED_DOWN}\nM400"
            send_gcode(gcode_block)
            
            # Settle time after movement: This is the critical window where 
            # the stationary frame(s) for the new position (i+1) are recorded.
            time.sleep(VIBRATION_SETTLE_TIME) 
    
    # Stop Recording
    cam.stop_recording()
    print("Recording stopped.")
    
    # Return to the original Z height
    send_gcode("G90") # Absolute positioning
    # Optimization: Batched G-code
    gcode_block = f"G1 Z{original_z} F{Z_SPEED_UP}\nM400"
    send_gcode(gcode_block)

# --- 3. MAIN SCRIPT LOGIC ---

def main():
    camera = init_camera()
    run_id = time.strftime("%Y%m%d_%H%M%S")
    # Updated directory path to reflect video output
    run_dir = os.path.join(SCAN_OUTPUT_DIR, run_id) 
    os.makedirs(run_dir, exist_ok=True)
    print(f"Saving all videos to: {run_dir}")
    
    send_gcode("G90") # Absolute positioning

    start_x, start_y, start_z = get_toolhead_position()
    if start_x is None:
        print("Failed to get printer position. Aborting.")
        camera.stop()
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
            
            # 1. Optimization: Batched G-code for XY move and wait (M400)
            gcode_block = f"G0 X{x_pos:.2f} Y{y_pos:.2f} F{SPEED}\nM400"
            send_gcode(gcode_block)
            
            # 2. Pre-action delay
            time.sleep(PRE_DELAY)
            
            # 3. Define output video file path
            video_filename = os.path.join(run_dir, f"point_{h_step:02d}_{w_step:02d}.h264")
            
            # 4. Perform the video focus stack
            perform_video_stack(camera, video_filename, start_z)
            
            # 5. Post-action delay
            time.sleep(POST_DELAY)

    # --- Scan Complete ---
    print("\n--- Scan complete. Returning to start position. ---")
    
    # Final Optimization: Batched G-code
    gcode_block = f"G0 X{start_x} Y{start_y} F{SPEED}\nM400"
    send_gcode(gcode_block)
    
    # Cleanup
    camera.stop()
    print("Camera stopped. Script finished.")

if __name__ == "__main__":
    main()
