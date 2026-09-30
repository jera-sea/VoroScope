import os
import sys
import time
import json
import math
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from hardware import PrinterInterface

# ==========================================
# CONFIGURATION - edit this path
# ==========================================
CONFIG_FILE = "/home/pi/camera/interface/configs/first_batch_Jan_2026.json"

# HTTP timeout for the Z repositioning moves (approach and final lift). Every
# move blocks until M400 returns, and a descent from a parked high Z at
# max_z_velocity (15 mm/s) can take ~20s - well past hardware.py's 5s default.
MOVE_TIMEOUT = 30

# ==========================================

def run_single_zstack(config_file):
    # 1. Load Configuration
    print(f"Loading config from: {config_file}")
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

    # 2. Setup Output Path
    save_path = os.path.join(cfg['output_folder'], cfg['sample_name'])
    os.makedirs(save_path, exist_ok=True)
    output_file = os.path.join(save_path, f"{cfg['sample_name']}_zstack.h264")
    print(f"Output will be saved to: {output_file}")

    # 3. Initialize Hardware
    printer = PrinterInterface()

    # 4. Initialize Camera
    print("Initializing camera...")
    cam = Picamera2()
    w, h = map(int, cfg['resolution'].lower().split('x'))
    vid_config = cam.create_video_configuration(main={"size": (w, h), "format": "RGB888"})
    cam.configure(vid_config)
    cam.start()

    # Apply manual controls
    controls = {
        "FrameRate": cfg['framerate'],
        "AeEnable": False,
        "AwbEnable": False,
        "ExposureTime": int(cfg.get('exposure_us', 50000)),
        "AnalogueGain": float(cfg.get('analogue_gain', 2.3540)),
        "ColourGains": (float(cfg.get('awb_red', 2.1316)), float(cfg.get('awb_blue', 3.3847)))
    }
    cam.set_controls(controls)
    print("Camera settings applied. Settling for 1s...")
    time.sleep(1)

    # 5. Calculate Z speed (same logic as scanner.py)
    z_dist = abs(cfg['stack_end_z'] - cfg['stack_start_z'])
    duration = cfg['stack_frames'] / cfg['framerate']
    z_speed = (z_dist / duration) * 60 if duration > 0 else 1000
    # The sweep move blocks until M400 returns, so the HTTP timeout has to
    # outlast the sweep or recording stops while the head is still moving.
    sweep_timeout = duration + 10

    print(f"Z stack: {cfg['stack_start_z']}mm -> {cfg['stack_end_z']}mm")
    print(f"Z distance: {z_dist:.2f}mm over {duration:.2f}s at {z_speed:.1f}mm/min")
    print(f"Frames: {int(cfg['stack_frames'])} @ {cfg['framerate']}fps")

    try:
        # Move to safe Z first
        print("Moving to start Z...")
        printer.move_to(z=cfg['stack_start_z'] + 2, speed=1000, timeout=MOVE_TIMEOUT)
        printer.move_to(z=cfg['stack_start_z'], speed=1000, timeout=MOVE_TIMEOUT)
        time.sleep(0.2)

        # Record
        print("Starting recording...")
        encoder = H264Encoder(bitrate=25000000)
        cam.start_recording(encoder, output_file)

        # Z sweep
        swept = printer.move_to(z=cfg['stack_end_z'], speed=z_speed,
                                timeout=sweep_timeout)

        # Stop recording
        cam.stop_recording()

        if swept:
            print("Recording complete.")
        else:
            # move_to swallows the error and returns False, so without this the
            # run would report success over a stack covering only part of the
            # Z range - the exact failure this check exists to make obvious.
            print("")
            print("*** WARNING: the Z sweep was not acknowledged. ***")
            print(f"*** {output_file} is almost certainly INCOMPLETE and covers")
            print("*** only part of the Z range. The head may still be moving.")
            print("*** Discard this capture; see the [PRINTER ERROR] line above.")
            print("")

    finally:
        cam.stop()
        cam.close()
        # Lift head clear
        printer.move_to(z=cfg['stack_start_z'] + 5, speed=1000, timeout=MOVE_TIMEOUT)
        print(f"Done. File saved to: {output_file}")

if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else CONFIG_FILE
    run_single_zstack(config_path)
