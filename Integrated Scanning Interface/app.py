import os
import json
import threading
import subprocess
import signal
from flask import Flask, render_template_string, request, jsonify, Response
from hardware import PrinterInterface
import cv2
import time
# We import Picamera2 only inside the streaming thread to allow releasing it

app = Flask(__name__)
printer = PrinterInterface()

# --- STATE MANAGEMENT ---
CONFIG_FILE = "scan_config.json"
CALIB_FILE = "calibration.json"

# Process handles
scan_process = None

# Global flag for camera streaming
camera_lock = threading.Lock()
streaming_active = False

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {
        "start_x": 0, "start_y": 0, "end_x": 10, "end_y": 10,
        "step_size_x": 5, "step_size_y": 5,
        "stack_start_z": 5, "stack_end_z": 0, "stack_frames": 150,
        "framerate": 10, "resolution": "4056x3040",
        "exposure_us": 50000, "analogue_gain": 2.3540,
        "awb_red": 2.1316, "awb_blue": 3.3847,
        "sample_name": "scan_001", "output_folder": "/home/pi/camera/ssd/scans"
    }

# --- CAMERA STREAMING GENERATOR ---
def generate_frames():
    global streaming_active
    from picamera2 import Picamera2
    import cv2
    import time

    # 1. Acquire the Lock
    # If the scanner is running, we cannot start the stream
    if not camera_lock.acquire(blocking=False):
        print("Camera is busy (Scanner is running). Cannot start stream.")
        return

    cam = None
    try:
        print("Stream Client Connected. Initializing Camera...")
        cam = Picamera2()
        config = cam.create_video_configuration(main={"size": (640, 480), "format": "RGB888"})
        cam.configure(config)
        cam.start()

        # Apply your specific tuning
        cam.set_controls({
            "AeEnable": False, 
            "AwbEnable": False,
            "ExposureTime": 20000,
            "AnalogueGain": 1.0,
            "ColourGains": (2.1, 1.6)
        })
        
        streaming_active = True
        
        while True:
            # Capture
            frame = cam.capture_array()
            
            # Encode
            frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            ret, buffer = cv2.imencode('.jpg', frame_bgr)
            
            if not ret:
                continue
                
            frame_bytes = buffer.tobytes()
            
            # Yield Frame
            # If the client (browser tab) disconnects, this yield will eventually 
            # raise a GeneratorExit or BrokenPipeError, triggering the 'finally' block.
            yield (b'--FRAME\r\n'
                   b'Content-Type: image/jpeg\r\n'
                   b'Content-Length: ' + str(len(frame_bytes)).encode() + b'\r\n\r\n' + 
                   frame_bytes + b'\r\n')
                   
            time.sleep(0.04)

    except GeneratorExit:
        print("Client closed the tab. Stopping stream.")
    except Exception as e:
        print(f"Streaming Error: {e}")
    finally:
        # --- CLEANUP ---
        # This runs automatically when the tab is closed
        if cam:
            cam.stop()
            cam.close()
            print("Camera hardware released.")
        
        streaming_active = False
        camera_lock.release()
        print("Lock released.")

# --- ROUTES ---

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'POST':
        with open(CONFIG_FILE, 'w') as f:
            json.dump(request.json, f, indent=4)
        return jsonify({"status": "saved"})
    return jsonify(load_config())

@app.route('/api/config/save_named', methods=['POST'])
def save_named_config():
    data = request.json
    filename = data.get('filename')
    config_data = data.get('config')

    if not filename or not config_data:
        return jsonify({"status": "error", "message": "Filename and config data are required"}), 400

    # Basic sanitization to prevent path traversal
    if '/' in filename or '\\' in filename or '..' in filename:
        return jsonify({"status": "error", "message": "Invalid filename (contains path characters)"}), 400
    
    if not filename.endswith('.json'):
        filename += '.json'

    CONFIGS_DIR = "configs"
    os.makedirs(CONFIGS_DIR, exist_ok=True)
    filepath = os.path.join(CONFIGS_DIR, filename)

    try:
        with open(filepath, 'w') as f:
            json.dump(config_data, f, indent=4)
        return jsonify({"status": "saved", "filepath": filepath})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/configs/list', methods=['GET'])
def list_configs():
    CONFIGS_DIR = "configs"
    if not os.path.exists(CONFIGS_DIR):
        return jsonify([])
    
    try:
        files = [f for f in os.listdir(CONFIGS_DIR) if f.endswith('.json') and os.path.isfile(os.path.join(CONFIGS_DIR, f))]
        return jsonify(files)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/configs/load/<string:filename>', methods=['GET'])
def load_named_config(filename):
    # Basic sanitization to prevent path traversal
    if '/' in filename or '\\' in filename or '..' in filename:
        return jsonify({"status": "error", "message": "Invalid filename"}), 400

    CONFIGS_DIR = "configs"
    filepath = os.path.join(CONFIGS_DIR, filename)

    if not os.path.exists(filepath):
        return jsonify({"status": "error", "message": "File not found"}), 404

    try:
        with open(filepath, 'r') as f:
            config_data = json.load(f)
        return jsonify(config_data)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/move', methods=['POST'])
def move_printer():
    data = request.json
    # Handle specific axis moves or full moves
    printer.move_to(x=data.get('x'), y=data.get('y'), z=data.get('z'), speed=data.get('speed', 3000))
    return jsonify({"status": "moved"})

@app.route('/api/position', methods=['GET'])
def get_pos():
    x, y, z = printer.get_position()
    return jsonify({"x": x, "y": y, "z": z})

@app.route('/api/stream/toggle', methods=['POST'])
def toggle_stream():
    global streaming_active
    target_state = request.json.get('active', False)
    
    # If turning off, the generator loop checks the flag and exits
    streaming_active = target_state
    return jsonify({"status": "ok", "active": streaming_active})

@app.route('/video_feed')
def video_feed():
    # Directly stream the response. 
    # The browser tab keeps this connection open.
    return Response(generate_frames(), 
                    mimetype='multipart/x-mixed-replace; boundary=FRAME')

@app.route('/api/calibrate', methods=['POST'])
def calibrate():
    global streaming_active
    if streaming_active:
        return jsonify({"status": "error", "message": "Turn off video stream first"}), 409
        
    subprocess.Popen(["python3", "calibrate.py"])
    return jsonify({"status": "calibration_started"})

@app.route('/api/calibration_status', methods=['GET'])
def calib_status():
    if os.path.exists(CALIB_FILE):
        with open(CALIB_FILE, 'r') as f:
            return jsonify(json.load(f))
    return jsonify({})

@app.route('/api/scan/start', methods=['POST'])
def start_scan():
    global scan_process, streaming_active
    
    if streaming_active:
        return jsonify({"status": "error", "message": "Stop video stream before scanning"}), 409

    if scan_process and scan_process.poll() is None:
        return jsonify({"status": "error", "message": "Scan already running"}), 409

    # Save current UI settings to config file first
    with open(CONFIG_FILE, 'w') as f:
        json.dump(request.json, f, indent=4)

    scan_process = subprocess.Popen(["python3", "scanner.py", CONFIG_FILE])
    return jsonify({"status": "started"})

@app.route('/api/scan/status', methods=['GET'])
def scan_status():
    running = (scan_process is not None and scan_process.poll() is None)
    return jsonify({"running": running})

@app.route('/api/scan/stop', methods=['POST'])
def stop_scan():
    global scan_process
    if scan_process and scan_process.poll() is None:
        scan_process.send_signal(signal.SIGINT) # Gentle stop
        return jsonify({"status": "stopping"})
    return jsonify({"status": "not_running"})

@app.route('/api/scan/z_dry_run', methods=['POST'])
def z_dry_run():
    """Moves Z between focus points at scan speed without stopping video."""
    data = request.json
    start_z = float(data.get('stack_start_z'))
    end_z = float(data.get('stack_end_z'))
    
    framerate = float(data.get('framerate', 30))
    frames = float(data.get('stack_frames', 150))
    z_dist = abs(end_z - start_z)
    duration = frames / framerate
    speed = (z_dist / duration) * 60 if duration > 0 else 1000
    
    def run_motion():
        # Move to start
        printer.move_to(z=start_z, speed=2000)
        time.sleep(1)
        # Execute the slow focus movement
        printer.move_to(z=end_z, speed=speed)
        print("Z Dry Run Complete")

    # Run in a background thread so the UI doesn't hang
    threading.Thread(target=run_motion).start()
    return jsonify({"status": "motion_started"})

@app.route('/api/stream/force_release', methods=['POST'])
def force_release():
    global streaming_active
    # This is a brute-force reset if the lock gets stuck
    if camera_lock.locked():
        try:
            camera_lock.release()
        except:
            pass
    streaming_active = False
    return jsonify({"status": "released"})

# --- FRONTEND TEMPLATE (Embedded for single-file portability) ---
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Microscope Scanner Control</title>
    <style>
        body { font-family: sans-serif; background: #222; color: #eee; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        .card { background: #333; padding: 20px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        h2 { border-bottom: 1px solid #555; padding-bottom: 10px; margin-top: 0; }
        label { display: block; margin: 10px 0 5px; font-size: 0.9em; color: #ccc; }
        input, select { width: 100%; padding: 8px; background: #444; border: 1px solid #555; color: white; border-radius: 4px; box-sizing: border-box;}
        button { background: #007bff; color: white; border: none; padding: 10px 15px; border-radius: 4px; cursor: pointer; margin-top: 10px; width: 100%; }
        button:hover { background: #0056b3; }
        button.danger { background: #dc3545; }
        button.success { background: #28a745; }
        button.secondary { background: #6c757d; }
        .row { display: flex; gap: 10px; }
        .video-box { width: 100%; height: 480px; background: #000; display: flex; align-items: center; justify-content: center; border: 2px solid #555; }
        .pos-display { font-family: monospace; font-size: 1.2em; color: #0f0; margin-bottom: 10px; text-align: center; }
        #progressBarContainer { width: 100%; background: #444; height: 20px; border-radius: 10px; margin-top: 20px; display:none;}
        #progressBar { width: 0%; height: 100%; background: #28a745; border-radius: 10px; transition: width 0.5s; }
    </style>
</head>
<body>
<div class="container">
    
    <div style="display: flex; flex-direction: column; gap: 20px;">
        
        <div class="card">
            <h2>Coordinates & Jog</h2>
            <div class="pos-display">X: <span id="curX">0.00</span> Y: <span id="curY">0.00</span> Z: <span id="curZ">0.00</span></div>
            <div class="row">
                <input type="number" id="jogX" placeholder="X">
                <input type="number" id="jogY" placeholder="Y">
                <input type="number" id="jogZ" placeholder="Z">
            </div>
            <button onclick="manualMove()">Move to Coordinates</button>
            <button class="secondary" onclick="updatePos()">Refresh Position</button>

            <div style="border-top: 1px solid #555; margin-top: 20px; padding-top: 15px;">
                <label for="jog_step">Jog Step (mm)</label>
                <input type="number" id="jog_step" value="1" step="0.1">
                <div class="row">
                    <button class="secondary" onclick="jogAxis('x', 1)">X+</button>
                    <button class="secondary" onclick="jogAxis('x', -1)">X-</button>
                    <button class="secondary" onclick="jogAxis('y', 1)">Y+</button>
                    <button class="secondary" onclick="jogAxis('y', -1)">Y-</button>
                    <button class="secondary" onclick="jogAxis('z', 1)">Z+</button>
                    <button class="secondary" onclick="jogAxis('z', -1)">Z-</button>
                </div>
            </div>
        </div>

        <div class="card">
            <h2>XY Scan Area</h2>
            <div class="row" style="align-items: flex-end;">
                <div><label for="start_x">Start X</label><input type="number" id="start_x" placeholder="mm" oninput="updateScanGrid()"></div>
                <div><label for="start_y">Start Y</label><input type="number" id="start_y" placeholder="mm" oninput="updateScanGrid()"></div>
                <button class="secondary" style="width: auto;" onclick="setFromCurrent('start')">Get Current</button>
            </div>
            <div class="row" style="align-items: flex-end; margin-top: 15px;">
                <div><label for="end_x">Desired End X</label><input type="number" id="end_x" placeholder="mm" oninput="updateScanGrid()"></div>
                <div><label for="end_y">Desired End Y</label><input type="number" id="end_y" placeholder="mm" oninput="updateScanGrid()"></div>
                <button class="secondary" style="width: auto;" onclick="setFromCurrent('end')">Get Current</button>
            </div>
            <div class="row" style="margin-top: 15px;">
                <div><label for="step_size_x">Step Size X (mm)</label><input type="number" id="step_size_x" value="5" oninput="updateScanGrid()"></div>
                <div><label for="step_size_y">Step Size Y (mm)</label><input type="number" id="step_size_y" value="5" oninput="updateScanGrid()"></div>
            </div>
            <div style="margin-top: 15px; padding-top: 15px; border-top: 1px solid #555; font-size: 0.9em; color: #ccc;">
                Calculated Steps: <strong id="calc_steps_x">0</strong> (X) &times; <strong id="calc_steps_y">0</strong> (Y)<br>
                Calculated Points: <strong id="calc_steps_x">0</strong> (X) &times; <strong id="calc_steps_y">0</strong> (Y)<br>
                Actual Scan Area End: X=<strong id="actual_end_x">0.00</strong>, Y=<strong id="actual_end_y">0.00</strong>
            </div>
            <label style="margin-top: 15px;">Move to Scan Area Corners</label>
            <div class="row">
                <button class="secondary" onclick="moveToCorner('start', 'start')">Start X, Start Y</button>
                <button class="secondary" onclick="moveToCorner('end', 'start')">Desired End X, Start Y</button>
            </div>
            <div class="row">
                <button class="secondary" onclick="moveToCorner('start', 'end')">Start X, End Y</button>
                <button class="secondary" onclick="moveToCorner('end', 'end')">Desired End X, End Y</button>
            </div>
        </div>

        <div class="card">
            <h2>Z Focus Stack</h2>
            <div class="row">
                <div><label>Start Z</label><input type="number" id="stack_start_z"></div>
                <div><label>End Z</label><input type="number" id="stack_end_z"></div>
            </div>
            <div class="row">
                <button class="secondary" onclick="setZFromCurrent('start')">Set Start Z</button>
                <button class="secondary" onclick="setZFromCurrent('end')">Set End Z</button>
            </div>
            <label>Stack Frames</label>
            <input type="number" id="stack_frames" value="150">
            <button class="secondary" onclick="testZStack()">Test Z Motion (Dry Run)</button>
        </div>
        
    </div>

    <div style="display: flex; flex-direction: column; gap: 20px;">
        
        <div class="card">
            <h2>Camera Feed</h2>
            

            <div class="card mb-4">
                <div class="card-header bg-secondary">Camera Feed</div>
                <div class="card-body text-center">
                    <p class="text-muted">
                        Clicking the button below will open the raw video feed in a new tab.
                        <br>
                        <strong>Close the tab to release the camera for scanning.</strong>
                    </p>
                    
                    <button onclick="window.open('/video_feed', '_blank')">Open Live Stream</button>

                    <button onclick="forceRelease()" class="danger">Force Release Camera</button>
                </div>
            </div>
            
            <div style="margin-top: 15px; border-top: 1px solid #555; padding-top: 10px;">
                <label>Calibration</label>
                <div class="row">
                    <button class="secondary" onclick="runCalibration()">Run White Balance</button>
                    <div id="calibResult" style="align-self: center; font-size: 0.8em; color: #aaa;"></div>
                </div>
            </div>
        </div>

        <div class="card">
            <h2>Scan Configuration</h2>
            <div style="border-bottom: 1px solid #555; padding-bottom: 15px; margin-bottom: 15px;">
                <label>Camera Calibration Settings</label>
                <div class="row">
                    <div><label style="font-size:0.8em">Exposure (us)</label><input type="number" id="exposure_us"></div>
                    <div><label style="font-size:0.8em">Gain</label><input type="number" id="analogue_gain" step="0.1"></div>
                    <div><label style="font-size:0.8em">Red</label><input type="number" id="awb_red" step="0.01"></div>
                    <div><label style="font-size:0.8em">Blue</label><input type="number" id="awb_blue" step="0.01"></div>
                </div>
            </div>
            <div style="border-bottom: 1px solid #555; padding-bottom: 15px; margin-bottom: 15px;">
                <label>Load Saved Configuration</label>
                <div class="row">
                    <select id="config_file_select" style="flex-grow: 1;"></select>
                    <button class="secondary" style="width: auto;" onclick="loadSelectedConfig()">Load Config</button>
                </div>
            </div>
            <div style="border-bottom: 1px solid #555; padding-bottom: 15px; margin-bottom: 15px;">
                <label>Configuration File Name</label>
                <div class="row">
                    <input type="text" id="config_filename" placeholder="e.g., my_scan_settings">
                    <button class="secondary" style="width: auto;" onclick="saveConfig()">Save Config</button>
                </div>
            </div>
            <div class="row">
                <div><label>Framerate</label><input type="number" id="framerate" value="30"></div>
                <div><label>Resolution</label>
                    <select id="resolution">
                        <option value="4056x3040">4056x3040 (Full)</option>
                        <option value="1920x1080">1920x1080 (1080p)</option>
                    </select>
                </div>
            </div>
            <label>Sample Name</label>
            <input type="text" id="sample_name" value="scan_001">
            <label>Output Folder</label>
            <input type="text" id="output_folder" value="/home/pi/camera/ssd/scans">

            <div style="margin-top: 20px;">
                <button class="success" onclick="startScan()">START SCANNING</button>
                <button class="danger" onclick="stopScan()">ABORT SCAN</button>
            </div>
            
            <div id="progressBarContainer">
                <div id="progressBar"></div>
            </div>
            <div id="scanStatus" style="text-align: center; margin-top: 10px; color: #ffc107;"></div>
        </div>

    </div>
</div>

<script>
    // Load config on startup
    window.onload = function() {
        updatePos();
        fetch('/api/config').then(r=>r.json()).then(data => {
            for (const [key, value] of Object.entries(data)) {
                if(document.getElementById(key)) document.getElementById(key).value = value;
            }
            updateScanGrid();
        });
        fetch('/api/calibration_status').then(r=>r.json()).then(d => {
            if(d.timestamp) document.getElementById('calibResult').innerText = "Last: " + new Date(d.timestamp*1000).toLocaleString();
        });
        populateConfigList();
    };

    function populateConfigList() {
        fetch('/api/configs/list').then(r => r.json()).then(files => {
            const select = document.getElementById('config_file_select');
            select.innerHTML = ''; // Clear existing options
            if (files.length === 0) {
                const option = document.createElement('option');
                option.disabled = true;
                option.selected = true;
                option.innerText = "No saved configs found";
                select.appendChild(option);
            } else {
                files.forEach(file => {
                    const option = document.createElement('option');
                    option.value = file;
                    option.innerText = file;
                    select.appendChild(option);
                });
            }
        });
    }

    function loadSelectedConfig() {
        const select = document.getElementById('config_file_select');
        const filename = select.value;
        if (!filename || select.options[select.selectedIndex].disabled) {
            alert('Please select a valid configuration to load.');
            return;
        }

        fetch('/api/configs/load/' + filename)
        .then(r => {
            if (!r.ok) { throw new Error('Failed to load config file: ' + r.statusText); }
            return r.json();
        })
        .then(data => {
            for (const [key, value] of Object.entries(data)) {
                const el = document.getElementById(key);
                if(el) {
                    el.value = value;
                }
            }
            // Also update the save filename input for convenience
            document.getElementById('config_filename').value = filename.replace('.json', '');
            alert('Configuration "' + filename + '" loaded.');
        })
        .catch(error => {
            alert(error.message);
        });
    }

    function forceRelease() {
        fetch('/api/stream/force_release', { method: 'POST' })
        .then(r => r.json())
        .then(d => alert("Camera lock forcefully released."));
    }

    function updatePos() {
        fetch('/api/position').then(r=>r.json()).then(pos => {
            document.getElementById('curX').innerText = pos.x.toFixed(2);
            document.getElementById('curY').innerText = pos.y.toFixed(2);
            document.getElementById('curZ').innerText = pos.z.toFixed(2);
        });
    }

    function jogAxis(axis, direction) {
        const stepEl = document.getElementById('jog_step');
        const step = parseFloat(stepEl.value);

        if (isNaN(step) || step <= 0) {
            alert('Please enter a valid positive jog step amount.');
            return;
        }

        // Fetch current position to ensure we're moving relative to the real position
        fetch('/api/position').then(r => r.json()).then(pos => {
            let payload = {};
            
            if (axis === 'x') payload.x = pos.x + (step * direction);
            else if (axis === 'y') payload.y = pos.y + (step * direction);
            else if (axis === 'z') payload.z = pos.z + (step * direction);

            fetch('/api/move', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            }).then(() => setTimeout(updatePos, 500)); // Refresh position after move
        });
    }

    function manualMove() {
        const x = document.getElementById('jogX').value;
        const y = document.getElementById('jogY').value;
        const z = document.getElementById('jogZ').value;
        
        let payload = {};
        if(x) payload.x = parseFloat(x);
        if(y) payload.y = parseFloat(y);
        if(z) payload.z = parseFloat(z);
        
        fetch('/api/move', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        }).then(() => setTimeout(updatePos, 500));
    }

    function setFromCurrent(prefix) {
        fetch('/api/position').then(r=>r.json()).then(pos => {
            document.getElementById(prefix+'_x').value = pos.x;
            document.getElementById(prefix+'_y').value = pos.y;
        });
    }
    
    function setZFromCurrent(prefix) {
        fetch('/api/position').then(r=>r.json()).then(pos => {
            document.getElementById('stack_'+prefix+'_z').value = pos.z;
        });
    }

    function moveToCorner(x_type, y_type) { // x_type, y_type are 'start' or 'end'
        const x_val = document.getElementById(x_type + '_x').value;
        const y_val = document.getElementById(y_type + '_y').value;

        if (x_val === '' || y_val === '') {
            alert('Please ensure start and end X and Y coordinates are set.');
            return;
        }

        let payload = {
            x: parseFloat(x_val),
            y: parseFloat(y_val)
        };

        fetch('/api/move', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        }).then(() => setTimeout(updatePos, 500));
    }

    let streamActive = false;
    function toggleStream() {
        streamActive = !streamActive;
        const img = document.getElementById('videoStream');
        const txt = document.getElementById('videoPlaceholder');
        const btn = document.getElementById('btnStream');

        fetch('/api/stream/toggle', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({active: streamActive})
        }).then(() => {
            if(streamActive) {
                img.src = "/video_feed?" + new Date().getTime(); // cache bust
                img.style.display = 'block';
                txt.style.display = 'none';
                btn.innerText = "Turn Off Feed";
                btn.classList.add('danger');
            } else {
                img.src = "";
                img.style.display = 'none';
                txt.style.display = 'block';
                btn.innerText = "Turn On Feed";
                btn.classList.remove('danger');
            }
        });
    }

    function runCalibration() {
        if(streamActive) { alert("Please turn off video feed first."); return; }
        document.getElementById('calibResult').innerText = "Calibrating (wait ~5s)...";
        fetch('/api/calibrate', {method: 'POST'}).then(() => {
            setTimeout(() => {
                fetch('/api/calibration_status').then(r=>r.json()).then(d => {
                    if(d.timestamp) {
                        document.getElementById('exposure_us').value = d.exposure_us;
                        document.getElementById('analogue_gain').value = d.analogue_gain;
                        document.getElementById('awb_red').value = d.awb_red;
                        document.getElementById('awb_blue').value = d.awb_blue;
                        document.getElementById('calibResult').innerText = "Updated: " + new Date(d.timestamp*1000).toLocaleTimeString();
                    } else {
                        document.getElementById('calibResult').innerText = "Calibration failed.";
                    }
                });
            }, 6000);
        });
    }

    function saveConfig() {
        const filename = document.getElementById('config_filename').value;
        if (!filename) {
            alert('Please enter a filename for the configuration.');
            return;
        }

        // Gather data from relevant inputs
        let config = {};
        const config_ids = [
            'start_x', 'start_y', 'end_x', 'end_y', 'step_size_x', 'step_size_y',
            'stack_start_z', 'stack_end_z', 'stack_frames',
            'framerate', 'resolution', 'sample_name', 'output_folder',
            'exposure_us', 'analogue_gain', 'awb_red', 'awb_blue'
        ];
        
        config_ids.forEach(id => {
            const el = document.getElementById(id);
            if (el) config[id] = el.value;
        });

        const payload = { filename: filename, config: config };

        fetch('/api/config/save_named', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        }).then(r => r.json()).then(data => {
            if (data.status === 'saved') {
                alert('Configuration saved to ' + data.filepath);
                populateConfigList();
            } else {
                alert('Error saving configuration: ' + data.message);
            }
        });
    }

    function startScan() {
        if(streamActive) { alert("Please turn off video feed first."); return; }
        
        // Gather data
        let config = {};
        const inputs = document.querySelectorAll('input, select');
        inputs.forEach(i => config[i.id] = i.value);
        
        // Pre-calculate approx duration for progress bar (Simple estimate)
        // In reality, this should be better calculated in Python
        let areaX = Math.abs(config.end_x - config.start_x);
        let areaY = Math.abs(config.end_y - config.start_y);
        // Estimate logic hidden for brevity
        
        fetch('/api/scan/start', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(config)
        }).then(r => r.json()).then(data => {
            if(data.status === 'error') alert(data.message);
            else {
                monitorScan();
            }
        });
    }
    
    function stopScan() {
        fetch('/api/scan/stop', {method: 'POST'});
    }

    function monitorScan() {
        document.getElementById('progressBarContainer').style.display = 'block';
        document.getElementById('scanStatus').innerText = "SCANNING...";
        
        let check = setInterval(() => {
            fetch('/api/scan/status').then(r=>r.json()).then(d => {
                if(!d.running) {
                    clearInterval(check);
                    document.getElementById('scanStatus').innerText = "COMPLETE / IDLE";
                    document.getElementById('progressBar').style.width = '100%';
                } else {
                    // Ideally fetch progress % from server. 
                    // For now we just animate undefined or set manually.
                }
            });
        }, 2000);
    }
    
    function testZStack() {
        // Implementation for Z-only scan logic
        // This would call a specific endpoint similar to /move but for Z sweep
        alert("Functionality to be implemented in Phase 2: Calls Z-sweep logic");
    }

    function updateScanGrid() {
        const startX = parseFloat(document.getElementById('start_x').value);
        const startY = parseFloat(document.getElementById('start_y').value);
        const endX = parseFloat(document.getElementById('end_x').value);
        const endY = parseFloat(document.getElementById('end_y').value);
        const stepX = parseFloat(document.getElementById('step_size_x').value);
        const stepY = parseFloat(document.getElementById('step_size_y').value);

        if (isNaN(startX) || isNaN(startY) || isNaN(endX) || isNaN(endY) || isNaN(stepX) || isNaN(stepY)) {
            return; // Don't calculate if inputs are not valid numbers yet
        }

        const width = endX - startX;
        const height = endY - startY;

        const absStepX = Math.abs(stepX);
        const absStepY = Math.abs(stepY);

        let stepsX = 0;
        if (absStepX > 0) {
            stepsX = Math.max(1, Math.ceil(Math.abs(width) / absStepX));
        }

        let stepsY = 0;
        if (absStepY > 0) {
            stepsY = Math.max(1, Math.ceil(Math.abs(height) / absStepY));
        }

        const actualEndX = startX + ((stepsX - 1) * absStepX * Math.sign(width));
        const actualEndY = startY + ((stepsY - 1) * absStepY * Math.sign(height));

        document.getElementById('calc_steps_x').innerText = stepsX;
        document.getElementById('calc_steps_y').innerText = stepsY;
        document.getElementById('actual_end_x').innerText = actualEndX.toFixed(2);
        document.getElementById('actual_end_y').innerText = actualEndY.toFixed(2);
    }

</script>
</body>
</html>
"""

if __name__ == '__main__':
    # Threaded mode is required to handle video streaming requests alongside API calls
    app.run(host='0.0.0.0', port=5000, threaded=True)