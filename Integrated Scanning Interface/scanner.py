import os
import sys
import time
import json
import argparse
import math
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
        'step_size_x', 'step_size_y',
        'stack_start_z', 'stack_end_z', 'stack_frames', 'framerate',
        'exposure_us', 'analogue_gain', 'awb_red', 'awb_blue'
    ]
    for key in numeric_keys:
        if key in cfg:
            cfg[key] = float(cfg[key])

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
        "ExposureTime": int(cfg.get('exposure_us', 50000)),
        "AnalogueGain": float(cfg.get('analogue_gain', 2.3540)),
        "ColourGains": (float(cfg.get('awb_red', 2.1316)), float(cfg.get('awb_blue', 3.3847)))
    }
    cam.set_controls(controls)
    time.sleep(1) # Settle

    # 6. Calculate Grid
    width_mm = cfg['end_x'] - cfg['start_x']
    height_mm = cfg['end_y'] - cfg['start_y']
    
    step_size_x_abs = abs(cfg.get('step_size_x', 5.0))
    step_size_y_abs = abs(cfg.get('step_size_y', 5.0))

    if step_size_x_abs == 0 or step_size_y_abs == 0:
        raise ValueError("Step sizes cannot be zero.")

    # Calculate number of intervals/steps needed to cover the area
    steps_x = max(1, math.ceil(abs(width_mm) / step_size_x_abs))
    steps_y = max(1, math.ceil(abs(height_mm) / step_size_y_abs))

    # The actual step value for movement must have the correct sign
    x_step_size = step_size_x_abs * (1 if width_mm >= 0 else -1)
    y_step_size = step_size_y_abs * (1 if height_mm >= 0 else -1)
    
    # Z-Stack calculations
    z_dist = abs(cfg['stack_end_z'] - cfg['stack_start_z'])
    # Calculate Z speed based on frames and framerate
    duration = cfg['stack_frames'] / cfg['framerate']
    z_speed = (z_dist / duration) * 60 if duration > 0 else 1000
    
    print(f"Starting Scan: {steps_x}x{steps_y} grid of points.")

    try:
        # Move to safe Z before XY travel? usually good practice
        printer.move_to(z=cfg['stack_start_z'] + 2, speed=1000)

        for yi in range(steps_y):
            curr_y = cfg['start_y'] + (yi * y_step_size)
            
            # ZigZag Logic
            x_range = range(steps_x) if yi % 2 == 0 else reversed(range(steps_x))
            
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

                # Return Z to start position before moving XY
                printer.move_to(z=cfg['stack_start_z'], speed=1000)
                
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