import os
import sys
import time
import json
import argparse
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from hardware import PrinterInterface

def run_scan(config_file):
    # 1. Load Configuration
    with open(config_file, 'r') as f:
        cfg = json.load(f)

    # Convert numeric config values that might be strings
    numeric_keys = [
        'start_x', 'end_x', 'start_y', 'end_y',
        'stack_start_z', 'stack_end_z', 'stack_speed',
        'framerate'
    ]
    for key in numeric_keys:
        if key in cfg:
            cfg[key] = float(cfg[key])

    # 2. Load Calibration (if exists, otherwise defaults)
    try:
        with open("calibration.json", 'r') as f:
            cal = json.load(f)
    except FileNotFoundError:
        print("No calibration found, using defaults.")
        cal = {"exposure_us": 20000, "analogue_gain": 1.0, "awb_red": 2.0, "awb_blue": 2.0}

    # 3. Setup Paths
    save_path = os.path.join(cfg['output_folder'], cfg['sample_name'])
    os.makedirs(save_path, exist_ok=True)
    
    # 4. Initialize Hardware
    printer = PrinterInterface()
    
    # 5. Initialize Camera
    print("Initializing Camera...")
    cam = Picamera2()
    # Parse resolution string "W x H"
    w, h = map(int, cfg['resolution'].lower().split('x'))
    vid_config = cam.create_video_configuration(main={"size": (w, h), "format": "RGB888"})
    cam.configure(vid_config)
    cam.start()

    # Apply Manual Controls (Critical for consistent microscopy)
    controls = {
        "FrameRate": cfg['framerate'],
        "AeEnable": False,
        "AwbEnable": False,
        "ExposureTime": int(cal['exposure_us']),
        "AnalogueGain": float(cal['analogue_gain']),
        "ColourGains": (float(cal['awb_red']), float(cal['awb_blue']))
    }
    cam.set_controls(controls)
    time.sleep(1) # Settle

    # 6. Calculate Grid
    width_mm = cfg['end_x'] - cfg['start_x']
    height_mm = cfg['end_y'] - cfg['start_y']
    
    # Basic logic: Ensure at least 1 step if distance is 0
    # You might want to add 'step_size' to the UI inputs, for now inferring or fixed
    # Assuming a FOV step or fixed count for this example:
    steps_x = 5  # Placeholder: In real usage, calculate based on FOV
    steps_y = 5  
    
    x_step_size = width_mm / steps_x if steps_x > 0 else 0
    y_step_size = height_mm / steps_y if steps_y > 0 else 0
    
    # Z-Stack calculations
    z_dist = abs(cfg['stack_end_z'] - cfg['stack_start_z'])
    z_speed = cfg['stack_speed'] * 60 # Convert mm/s to mm/min if needed
    
    print(f"Starting Scan: {steps_x}x{steps_y} grid.")

    try:
        # Move to safe Z before XY travel? usually good practice
        printer.move_to(z=cfg['stack_start_z'] + 2, speed=1000)

        for yi in range(steps_y + 1):
            curr_y = cfg['start_y'] + (yi * y_step_size)
            
            # ZigZag Logic
            x_range = range(steps_x + 1) if yi % 2 == 0 else reversed(range(steps_x + 1))
            
            for xi in x_range:
                curr_x = cfg['start_x'] + (xi * x_step_size)
                
                # Move XY
                printer.move_to(x=curr_x, y=curr_y)
                time.sleep(0.2) # Settle

                # Prepare Z-Stack
                printer.move_to(z=cfg['stack_start_z'], speed=1000)
                time.sleep(0.2)
                
                # Record
                filename = os.path.join(save_path, f"tile_{yi}_{xi}.h264")
                encoder = H264Encoder(bitrate=25000000)
                cam.start_recording(encoder, filename)
                
                # Perform Z movement (The "Scan" axis)
                printer.move_to(z=cfg['stack_end_z'], speed=z_speed)
                
                # Stop Record
                cam.stop_recording()
                
    finally:
        cam.stop()
        cam.close()
        printer.move_to(z=cfg['stack_start_z'] + 5) # Lift head
        print("Scan Finished.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("config", help="Path to JSON configuration file")
    args = parser.parse_args()
    
    run_scan(args.config)