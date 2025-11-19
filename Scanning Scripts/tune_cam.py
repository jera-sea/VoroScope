import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from picamera2 import Picamera2
import json
import cv2 # Requirement: pip install opencv-python

# --- DEFAULTS ---
current_params = {
    "exposure": 20000,   # us
    "gain": 1.0,         # multiplier
    "red": 2.1,          # gain
    "blue": 1.6          # gain
}

# --- CAMERA SETUP ---
print("Initializing Camera...")
cam = Picamera2()
# We use a smaller resolution for the preview stream to keep it fast
config = cam.create_video_configuration(main={"size": (1024, 768), "format": "RGB888"})
cam.configure(config)
cam.start()

# Helper to apply settings
def apply_camera_settings():
    """Applies the global current_params to the camera."""
    try:
        # We explicitly enforce Auto-Exposure and Auto-WB are OFF
        ctrls = {
            "AeEnable": False, 
            "AwbEnable": False,
            "ExposureTime": int(current_params["exposure"]),
            "AnalogueGain": float(current_params["gain"]),
            "ColourGains": (float(current_params["red"]), float(current_params["blue"]))
        }
        cam.set_controls(ctrls)
        print(f"Updated Controls: Exp={ctrls['ExposureTime']}, Gain={ctrls['AnalogueGain']}, WB={ctrls['ColourGains']}")
    except Exception as e:
        print(f"Error setting controls: {e}")

# Apply initial defaults immediately
apply_camera_settings()

# --- HTML UI ---
HTML_PAGE = """
<html>
<head>
    <title>PiCamera Tuning</title>
    <style>
        body { font-family: sans-serif; background: #222; color: #eee; text-align: center; }
        .container { width: 90%; max-width: 800px; margin: 0 auto; }
        img { width: 100%; border: 2px solid #555; }
        .controls { background: #333; padding: 20px; margin-top: 10px; border-radius: 8px; }
        .slider-group { margin: 15px 0; text-align: left; display: flex; justify-content: space-between; align-items: center; }
        label { width: 150px; text-align: left;}
        input[type=range] { flex-grow: 1; margin: 0 15px; }
        span { width: 60px; font-weight: bold; color: #4CAF50; text-align: right;}
        .code-block { background: #111; padding: 15px; text-align: left; font-family: monospace; margin-top: 20px; border: 1px solid #444; }
    </style>
</head>
<body>
    <div class="container">
        <h2>Headless Camera Tuner</h2>
        <img src="/stream.mjpg" id="videoFeed" />
        
        <div class="controls">
            <div class="slider-group">
                <label>Exposure (us)</label>
                <input type="range" min="100" max="50000" value="20000" step="100" id="exp" oninput="update('exposure', this.value)">
                <span id="val_exposure">20000</span>
            </div>
            <div class="slider-group">
                <label>Analogue Gain</label>
                <input type="range" min="1.0" max="16.0" value="1.0" step="0.1" id="gain" oninput="update('gain', this.value)">
                <span id="val_gain">1.0</span>
            </div>
            <div class="slider-group">
                <label>Red Balance</label>
                <input type="range" min="0.0" max="8.0" value="2.1" step="0.05" id="red" oninput="update('red', this.value)">
                <span id="val_red">2.1</span>
            </div>
            <div class="slider-group">
                <label>Blue Balance</label>
                <input type="range" min="0.0" max="8.0" value="1.6" step="0.05" id="blue" oninput="update('blue', this.value)">
                <span id="val_blue">1.6</span>
            </div>
        </div>

        <div class="code-block">
            <h3>Copy to your script:</h3>
            <div id="python_code"></div>
        </div>
    </div>

    <script>
        // Update UI and send request to server
        function update(name, value) {
            document.getElementById('val_' + name).innerText = value;
            
            // Send request asynchronously (fire and forget)
            fetch('/set?' + name + '=' + value);
            
            updateCode();
        }

        // Generate Python code snippet
        function updateCode() {
            let e = document.getElementById('exp').value;
            let g = document.getElementById('gain').value;
            let r = document.getElementById('red').value;
            let b = document.getElementById('blue').value;
            
            let code = `CAM_EXPOSURE_US = ${e}<br>CAM_ANALOGUE_GAIN = ${g}<br>CAM_AWB_RED = ${r}<br>CAM_AWB_BLUE = ${b}`;
            document.getElementById('python_code').innerHTML = code;
        }
        
        // Run once on load
        updateCode();
    </script>
</body>
</html>
"""

class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode('utf-8'))
        
        elif self.path.startswith('/set'):
            # Parse query params (e.g., /set?exposure=15000)
            try:
                query = self.path.split('?')[1]
                key, value = query.split('=')
                
                # Update global params
                global current_params
                current_params[key] = float(value)
                
                # Apply to camera
                apply_camera_settings()
                
                self.send_response(200)
                self.end_headers()
            except Exception as e:
                print(f"Bad Request: {e}")
                self.send_response(400)
                self.end_headers()
            
        elif self.path == '/stream.mjpg':
            self.send_response(200)
            self.send_header('Age', '0')
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
            self.end_headers()
            
            try:
                while True:
                    # Capture frame from Picamera2
                    frame = cam.capture_array()
                    
                    # Convert to JPEG
                    frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                    ret, jpeg = cv2.imencode('.jpg', frame_bgr)
                    
                    if ret:
                        self.wfile.write(b'--FRAME\r\n')
                        self.send_header('Content-Type', 'image/jpeg')
                        self.send_header('Content-Length', len(jpeg))
                        self.end_headers()
                        self.wfile.write(jpeg.tobytes())
                        self.wfile.write(b'\r\n')
                    
                    # Limit FPS slightly to save CPU
                    time.sleep(0.05)
            except Exception as e:
                print(f"Stream client disconnected: {e}")

def start_server():
    # Use ThreadingHTTPServer to handle stream and control requests simultaneously
    server = ThreadingHTTPServer(('0.0.0.0', 8000), WebHandler)
    print("----------------------------------------------------------------")
    print(" Tuning Server Started!")
    print(" Open your browser at: http://<YOUR-PI-IP>:8000")
    print(" Press CTRL+C to stop.")
    print("----------------------------------------------------------------")
    server.serve_forever()

if __name__ == '__main__':
    try:
        start_server()
    except KeyboardInterrupt:
        print("\nStopping camera...")
        cam.stop()
        print("Done.")
