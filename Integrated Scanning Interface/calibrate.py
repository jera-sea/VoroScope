import time
import json
import os
import sys
from picamera2 import Picamera2

# Output file for the web UI and Scanner to read
CALIBRATION_FILE = "calibration.json"

def run_calibration():
    print("Initializing Camera for Calibration...")
    try:
        cam = Picamera2()
        config = cam.create_video_configuration(main={"size": (4056, 3040), "format": "RGB888"})
        cam.configure(config)
        
        # Enable Auto Algorithms
        cam.set_controls({"AeEnable": True, "AwbEnable": True})
        cam.start()
        
        print("Waiting 3 seconds for 3A settling...")
        time.sleep(3)
        
        metadata = cam.capture_metadata()
        cam.stop()
        cam.close() # Vital to release resource
        
        if metadata and 'ExposureTime' in metadata and 'ColourGains' in metadata:
            # Prepare data structure
            cal_data = {
                "exposure_us": metadata['ExposureTime'],
                "analogue_gain": metadata.get('AnalogueGain', 1.0),
                "awb_red": metadata['ColourGains'][0],
                "awb_blue": metadata['ColourGains'][1],
                "timestamp": time.time()
            }
            
            with open(CALIBRATION_FILE, 'w') as f:
                json.dump(cal_data, f, indent=4)
                
            print(f"Calibration successful. Data saved to {CALIBRATION_FILE}")
            return True
        else:
            print("Failed to capture necessary metadata.")
            return False

    except Exception as e:
        print(f"Calibration Error: {e}")
        return False

if __name__ == "__main__":
    success = run_calibration()
    sys.exit(0 if success else 1)