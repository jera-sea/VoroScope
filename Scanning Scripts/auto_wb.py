import time
import sys
from picamera2 import Picamera2

# --- CONFIGURATION (Matching your main script's video configuration) ---
VIDEO_RESOLUTION = (4056, 3040)
CALIBRATION_WAIT_TIME = 3.0     

def autocalibrate_wb_and_exposure():
    """
    Initializes the camera, lets the 3A algorithms settle, and extracts 
    the calculated Exposure Time, Analogue Gain, and Colour Gains.
    """
    print("🎬 Initializing camera for FULL calibration...")
    
    cam = Picamera2()
    
    # 1. Use the video configuration from your main script
    config = cam.create_video_configuration(main={"size": VIDEO_RESOLUTION, "format": "RGB888"})
    cam.configure(config)
    
    # 2. Start the camera, explicitly enabling AE and AWB
    cam.set_controls({"AeEnable": True, "AwbEnable": True})
    cam.start()

    print(f"⏱️ Waiting {CALIBRATION_WAIT_TIME} seconds for Auto Exposure and Auto White Balance to settle...")
    time.sleep(CALIBRATION_WAIT_TIME) 
    
    exp_time, gain, r_gain, b_gain = None, None, None, None
    try:
        # 3. Retrieve metadata using the dedicated method
        metadata = cam.capture_metadata()
        
        if metadata:
            # Check for Exposure and Gain
            if 'ExposureTime' in metadata:
                exp_time = metadata['ExposureTime']
            if 'AnalogueGain' in metadata:
                gain = metadata['AnalogueGain']
            
            # Check for Colour Gains (AWB)
            if 'ColourGains' in metadata:
                r_gain, b_gain = metadata['ColourGains']
                
            if all([exp_time, gain, r_gain, b_gain]):
                print("✅ SUCCESS: Found all four required values in metadata.")
            else:
                raise KeyError("One or more key camera settings were missing from the metadata.")
            
    except Exception as e:
        print(f"❌ Critical Error during metadata capture: {e}")
        
    finally:
        # 4. Stop and close the camera
        if cam.started:
            cam.stop()
        cam.close()
        print("✅ Calibration complete.")

    return exp_time, gain, r_gain, b_gain

# --- MAIN EXECUTION ---

if __name__ == "__main__":
    exp_time, gain, r_gain, b_gain = autocalibrate_wb_and_exposure() 

    print("\n" + "="*70)
    
    if exp_time is not None:
        print("🎉 SUCCESS! Optimal Manual Calibration Settings Found:")
        print("   Copy the following lines directly into your 'MANUAL CAMERA CALIBRATION' section:\n")
        
        # Print the values formatted for easy copy/paste
        print(f"CAM_EXPOSURE_US = {exp_time}")
        print(f"CAM_ANALOGUE_GAIN = {gain:.4f}")
        print(f"CAM_AWB_RED = {r_gain:.4f}")
        print(f"CAM_AWB_BLUE = {b_gain:.4f}")
        
        print("\nNOTE: These are the values your camera auto-selected for a balanced image.")
    else:
        print("🔴 FAILED to retrieve ALL calibration values. Please check camera connections.")
        
    print("="*70)
