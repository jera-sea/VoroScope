import os
import sys
import json
import threading
import subprocess
import signal
from flask import Flask, render_template_string, request, jsonify, Response
from hardware import PrinterInterface
import scanner  # for the output-folder suffixes; safe to import (no picamera2 at module level)
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
zstack_test_process = None

# Capture-script output. The scripts print their own diagnostics (computed sweep
# duration, Z feedrate, [PRINTER ERROR] lines); the log file makes those visible
# in the browser instead of only on the terminal that started app.py.
#
# Anchored to this file, NOT the process cwd: a systemd unit without
# WorkingDirectory= starts the app in "/", where creating a relative "logs" is a
# permission error.
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

# Open log file handles for running subprocesses, keyed by log name. Held so the
# file stays open for the child, and closed once the child has exited.
log_handles = {}

# Global flag for camera streaming
camera_lock = threading.Lock()
streaming_active = False


def launch_logged(name, args):
    """Spawn a capture script with stdout+stderr captured to logs/<name>.log.

    sys.executable, not "python3": the app runs from the venv at
    /home/pi/camera/interface/ui_env, and the systemd unit sets VIRTUAL_ENV
    without putting the venv's bin on PATH. A bare "python3" therefore resolves
    to the system interpreter, which lacks the venv's packages - so the child
    must inherit whatever interpreter is running this process.

    '-u' is not optional: without it Python block-buffers when stdout is not a
    tty, so the log would stay empty until the process exited - useless both for
    following a 20-minute scan and for diagnosing a script that died early.

    Log setup is wrapped in try/except on purpose: logging is a convenience and
    must never be able to stop a scan from starting. If the file can't be
    opened, the run still goes ahead with output going to our stdout only.
    """
    handle = None
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        close_log(name)  # release the previous run's handle before truncating
        handle = open(os.path.join(LOG_DIR, f"{name}.log"), 'w')
        log_handles[name] = handle
    except Exception as e:
        print(f"[app] WARNING: no log file for '{name}' ({e}); "
              f"output goes to this stream only")

    proc = subprocess.Popen([sys.executable, "-u"] + args,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # A pump thread rather than stdout=handle, so the output reaches BOTH the log
    # file and our own stdout - which is the journal under systemd. Redirecting
    # straight to the file silenced 'journalctl -u <unit> -f' entirely.
    threading.Thread(target=_pump_output, args=(proc.stdout, name),
                     daemon=True).start()
    return proc


def _pump_output(stream, name):
    """Copy one child's output to its log file and to our stdout, line by line.

    Something must keep reading the pipe or the child blocks once the OS buffer
    fills, so this thread runs for the life of the process. It owns the log
    handle's lifetime: EOF on the pipe means the child exited, so it closes.
    """
    try:
        for raw in iter(stream.readline, b''):
            line = raw.decode('utf-8', errors='replace')
            handle = log_handles.get(name)
            if handle:
                try:
                    handle.write(line)
                    handle.flush()
                except Exception:
                    pass  # keep pumping; a dead log must not stall the child
            try:
                sys.stdout.write(f"[{name}] {line}")
                sys.stdout.flush()
            except Exception:
                pass
    finally:
        try:
            stream.close()
        except Exception:
            pass
        close_log(name)


def close_log(name):
    """Release the log handle for a finished subprocess. Safe to call twice."""
    handle = log_handles.pop(name, None)
    if handle:
        try:
            handle.close()
        except Exception:
            pass


def read_log_tail(name, lines=100):
    """Last N lines of a capture script's log, or '' if it has never run."""
    path = os.path.join(LOG_DIR, f"{name}.log")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, 'r', errors='replace') as f:
            return "".join(f.readlines()[-lines:])
    except Exception as e:
        return f"[could not read {path}: {e}]"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {
        "start_x": 0, "start_y": 0, "end_x": 10, "end_y": 10,
        "step_size_x": 5, "step_size_y": 5,
        "dual_set": "off", "step_size_x_train": 8, "step_size_y_train": 11,
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
        # cam.set_controls({
        #     "AeEnable": False, 
        #     "AwbEnable": False,
        #     "ExposureTime": 20000,
        #     "AnalogueGain": 1.0,
        #     "ColourGains": (2.1, 1.6)
        # })
        
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
        
    launch_logged("calibrate", ["calibrate.py"])
    return jsonify({"status": "calibration_started"})

@app.route('/api/calibration_status', methods=['GET'])
def calib_status():
    if os.path.exists(CALIB_FILE):
        with open(CALIB_FILE, 'r') as f:
            return jsonify(json.load(f))
    return jsonify({})

@app.route('/api/validate_path', methods=['POST'])
def validate_path():
    """Refuse to start a scan over folders that already hold tiles.

    scanner.py uses makedirs(exist_ok=True), so a name collision silently
    overwrites a previous card. The suffixed folders are what actually get
    written - the bare {sample_name} folder is only used by the Z-stack test.
    """
    data = request.json
    folder = data.get('output_folder')
    name = data.get('sample_name')

    if not folder or not name:
        return jsonify({"exists": False})

    targets = [name + scanner.INFER_SUFFIX]
    if str(data.get('dual_set', 'off')).strip().lower() in ('on', 'true', '1', 'yes'):
        targets.append(name + scanner.TRAIN_SUFFIX)

    collisions = [t for t in targets if os.path.exists(os.path.join(folder, t))]
    return jsonify({"exists": bool(collisions), "collisions": collisions})

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

    scan_process = launch_logged("scan", ["scanner.py", CONFIG_FILE])
    return jsonify({"status": "started"})

@app.route('/api/scan/status', methods=['GET'])
def scan_status():
    # The pump thread closes the log on EOF, so nothing to clean up here.
    running = (scan_process is not None and scan_process.poll() is None)
    return jsonify({"running": running})

@app.route('/api/scan/log', methods=['GET'])
def scan_log():
    return jsonify({"log": read_log_tail("scan")})

@app.route('/api/scan/stop', methods=['POST'])
def stop_scan():
    global scan_process
    if scan_process and scan_process.poll() is None:
        scan_process.send_signal(signal.SIGINT) # Gentle stop
        return jsonify({"status": "stopping"})
    return jsonify({"status": "not_running"})

@app.route('/api/scan/zstack_test', methods=['POST'])
def zstack_test():
    """Runs a single-column Z focus-stack capture using the current scan config."""
    global zstack_test_process, streaming_active

    if streaming_active:
        return jsonify({"status": "error", "message": "Stop video stream before testing"}), 409

    if scan_process and scan_process.poll() is None:
        return jsonify({"status": "error", "message": "Scan already running"}), 409

    if zstack_test_process and zstack_test_process.poll() is None:
        return jsonify({"status": "error", "message": "Z-stack test already running"}), 409

    zstack_test_process = launch_logged("zstack_test", ["single_zstack.py", CONFIG_FILE])
    return jsonify({"status": "started"})

@app.route('/api/scan/zstack_test/status', methods=['GET'])
def zstack_test_status():
    running = (zstack_test_process is not None and zstack_test_process.poll() is None)
    return jsonify({"running": running})

@app.route('/api/scan/zstack_test/log', methods=['GET'])
def zstack_test_log():
    return jsonify({"log": read_log_tail("zstack_test")})

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
        .container { max-width: 1600px; margin: 0 auto; display: grid; grid-template-columns: 0.8fr 1.4fr 0.8fr; gap: 20px; align-items: start; }
        .card { background: #333; padding: 20px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        h2 { border-bottom: 1px solid #555; padding-bottom: 10px; margin-top: 0; }
        h3 { color: #ccc; font-size: 1em; margin: 20px 0 5px; padding-top: 15px; border-top: 1px solid #555; }
        h3:first-of-type { margin-top: 0; padding-top: 0; border-top: none; }
        label { display: block; margin: 10px 0 5px; font-size: 0.9em; color: #ccc; }
        input, select { width: 100%; padding: 8px; background: #444; border: 1px solid #555; color: white; border-radius: 4px; box-sizing: border-box;}
        button { background: #007bff; color: white; border: none; padding: 10px 15px; border-radius: 4px; cursor: pointer; margin-top: 10px; width: 100%; }
        button:hover { background: #0056b3; }
        button.danger { background: #dc3545; }
        button.success { background: #28a745; }
        button.secondary { background: #6c757d; }
        button.dirty { background: #dc3545; }
        .row { display: flex; gap: 10px; }
        .video-box { width: 100%; height: 480px; background: #000; display: flex; align-items: center; justify-content: center; border: 2px solid #555; }
        .pos-display { font-family: monospace; font-size: 1.2em; color: #0f0; margin-bottom: 10px; text-align: center; }
        #progressBarContainer { width: 100%; background: #444; height: 20px; border-radius: 10px; margin-top: 20px; display:none;}
        #progressBar { width: 0%; height: 100%; background: #28a745; border-radius: 10px; transition: width 0.5s; }
        #runLogBox { display: none; margin-top: 12px; }
        #runLog { max-height: 220px; overflow: auto; background: #1a1a1a; border: 1px solid #555;
                  border-radius: 4px; padding: 8px; margin: 0; font-family: monospace; font-size: 11px;
                  line-height: 1.4; white-space: pre-wrap; word-break: break-word; color: #ccc; }
        #runLog .warn { color: #ffc107; }
        .config-table { width: 100%; border-collapse: collapse; font-size: 0.9em; }
        .config-table th { text-align: left; color: #ccc; font-size: 0.85em; padding: 6px 8px; border-bottom: 1px solid #555; }
        .config-table td { padding: 6px 8px; border-bottom: 1px solid #444; vertical-align: middle; }
        .config-table tr:nth-child(even) { background: #3a3a3a; }
        .config-table input { padding: 5px; margin: 0; width: 100px; }
        .config-table .cur-val { color: #aaa; font-family: monospace; }
        .config-table .param-name { font-family: monospace; color: #9cdcfe; }
        .config-table .param-desc { color: #ccc; font-size: 0.9em; }
        .config-table .get-current-btn { width: auto; padding: 5px 8px; margin: 0 0 0 6px; font-size: 0.8em; }
        .config-table .new-val-cell { display: flex; align-items: center; }
    </style>
</head>
<body>
<div class="container">

    <div class="card">
        <h2>Motion Control</h2>

        <div class="pos-display">X: <span id="curX">0.00</span> Y: <span id="curY">0.00</span> Z: <span id="curZ">0.00</span></div>
        <div class="row">
            <input type="number" id="jogX" placeholder="X">
            <input type="number" id="jogY" placeholder="Y">
            <input type="number" id="jogZ" placeholder="Z">
        </div>
        <button onclick="manualMove()">Move to Coordinates</button>
        <button class="secondary" onclick="updatePos()">Refresh Position</button>

        <h3>Jog</h3>
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

        <h3>Move to Scan Area Corners</h3>
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
        <h2>Configuration</h2>

        <div class="row">
            <select id="config_file_select" style="flex-grow: 1;"></select>
            <button class="secondary" style="width: auto;" onclick="loadSelectedConfig()">Load Config</button>
        </div>
        <div class="row">
            <input type="text" id="config_filename" placeholder="e.g., my_scan_settings">
            <button class="secondary" id="saveConfigBtn" style="width: auto;" onclick="saveConfig()">Save Config</button>
        </div>

        <table class="config-table" id="configTable">
            <thead>
                <tr>
                    <th>Parameter</th>
                    <th>Current Value</th>
                    <th>New Value</th>
                    <th>Description</th>
                </tr>
            </thead>
            <tbody>
                <!-- Rows generated by buildConfigTable() -->
            </tbody>
        </table>

        <div style="margin-top: 15px; padding-top: 15px; border-top: 1px solid #555; font-size: 0.9em; color: #ccc;">
            <span style="color:#8ab4f8;">&#9632;</span> Infer grid (<code>-infer</code>):
            <strong id="calc_steps_x">0</strong> (X) &times; <strong id="calc_steps_y">0</strong> (Y)
            = <strong id="calc_tiles_infer">0</strong> tiles,
            ends X=<strong id="actual_end_x">0.00</strong>, Y=<strong id="actual_end_y">0.00</strong><br>
            <span id="train_grid_line" style="display:none;">
                <span style="color:#81c995;">&#9632;</span> Train grid (<code>-train</code>):
                <strong id="calc_steps_x_train">0</strong> (X) &times; <strong id="calc_steps_y_train">0</strong> (Y)
                = <strong id="calc_tiles_train">0</strong> tiles,
                ends X=<strong id="actual_end_x_train">0.00</strong>, Y=<strong id="actual_end_y_train">0.00</strong><br>
            </span>
            <span style="color:#fff;">Total: <strong id="calc_tiles_total">0</strong> tiles,
            estimated <strong id="calc_duration">0.0</strong> min</span>
        </div>
    </div>

    <div class="card">
        <h2>Camera & Scan Control</h2>

        <label>Sample Name</label>
        <input type="text" id="sample_name" value="scan_001">

        <h3>Camera Feed</h3>
        <p style="color: #aaa; font-size: 0.9em;">
            Clicking the button below will open the raw video feed in a new tab.
            <br>
            <strong>Close the tab to release the camera for scanning.</strong>
        </p>
        <button onclick="window.open('/video_feed', '_blank')">Open Live Stream</button>
        <button onclick="forceRelease()" class="danger">Force Release Camera</button>

        <h3>Calibration</h3>
        <div class="row">
            <button class="secondary" onclick="runCalibration()">Run White Balance</button>
            <div id="calibResult" style="align-self: center; font-size: 0.8em; color: #aaa;"></div>
        </div>

        <h3>Z-Stack Test</h3>
        <button class="secondary" id="zstackTestBtn" onclick="runZStackTest()">Test Z Motion (Focus Stack)</button>

        <h3>Scan</h3>
        <button class="success" onclick="startScan()">START SCANNING</button>
        <button class="danger" onclick="stopScan()">ABORT SCAN</button>

        <div id="progressBarContainer">
            <div id="progressBar"></div>
        </div>
        <div id="scanStatus" style="text-align: center; margin-top: 10px; color: #ffc107;"></div>

        <div id="runLogBox">
            <h3>Script Output</h3>
            <pre id="runLog"></pre>
        </div>
    </div>
</div>

<script>
    // Configuration table parameters: id, label, optional position-axis for "Get Current" button, description
    const CONFIG_PARAMS = [
        { id: 'start_x', label: 'Start X', axis: 'x', desc: 'X coordinate (mm) where the scan grid begins.' },
        { id: 'start_y', label: 'Start Y', axis: 'y', desc: 'Y coordinate (mm) where the scan grid begins.' },
        { id: 'end_x', label: 'Desired End X', axis: 'x', desc: 'Desired X coordinate (mm) where the scan grid ends.' },
        { id: 'end_y', label: 'Desired End Y', axis: 'y', desc: 'Desired Y coordinate (mm) where the scan grid ends.' },
        { id: 'step_size_x', label: 'Step Size X', desc: 'Distance (mm) between adjacent scan columns along X. This is the overlapping "-infer" grid.' },
        { id: 'step_size_y', label: 'Step Size Y', desc: 'Distance (mm) between adjacent scan rows along Y. This is the overlapping "-infer" grid.' },
        { id: 'dual_set', label: 'Dual Set Scan', desc: 'Also capture a second, non-overlapping grid into a "-train" folder, back-to-back in the same camera session.', isSelect: true, options: [
            { value: 'off', label: 'Off (infer set only)' },
            { value: 'on', label: 'On (infer + train)' }
        ] },
        { id: 'step_size_x_train', label: 'Train Step Size X', desc: 'Distance (mm) between columns of the non-overlapping "-train" grid. Set to the field of view (no overlap).' },
        { id: 'step_size_y_train', label: 'Train Step Size Y', desc: 'Distance (mm) between rows of the non-overlapping "-train" grid. Set to the field of view (no overlap).' },
        { id: 'stack_start_z', label: 'Stack Start Z', axis: 'z', desc: 'Z height (mm) where each focus stack begins.' },
        { id: 'stack_end_z', label: 'Stack End Z', axis: 'z', desc: 'Z height (mm) where each focus stack ends.' },
        { id: 'stack_frames', label: 'Stack Frames', desc: 'Number of video frames captured during each Z sweep.' },
        { id: 'framerate', label: 'Framerate', desc: 'Camera capture rate (frames per second) during recording.' },
        { id: 'resolution', label: 'Resolution', desc: 'Camera capture resolution (width x height).', isSelect: true, options: [
            { value: '4056x3040', label: '4056x3040 (Full)' },
            { value: '1920x1080', label: '1920x1080 (1080p)' }
        ] },
        { id: 'exposure_us', label: 'Exposure (us)', desc: 'Manual camera exposure time, in microseconds.' },
        { id: 'analogue_gain', label: 'Analogue Gain', desc: 'Manual camera analogue gain (sensor sensitivity).' },
        { id: 'awb_red', label: 'AWB Red', desc: 'Manual white-balance gain, red channel.' },
        { id: 'awb_blue', label: 'AWB Blue', desc: 'Manual white-balance gain, blue channel.' },
        { id: 'output_folder', label: 'Output Folder', desc: 'Base directory on disk where scan output is saved.', isText: true }
    ];

    function buildConfigTable() {
        const tbody = document.querySelector('#configTable tbody');
        tbody.innerHTML = '';
        CONFIG_PARAMS.forEach(p => {
            const tr = document.createElement('tr');

            const nameTd = document.createElement('td');
            nameTd.innerHTML = '<span class="param-name">' + p.label + '</span>';
            tr.appendChild(nameTd);

            const curTd = document.createElement('td');
            curTd.innerHTML = '<span class="cur-val" id="cur_cfg_' + p.id + '"></span>';
            tr.appendChild(curTd);

            const newTd = document.createElement('td');
            const wrapper = document.createElement('div');
            wrapper.className = 'new-val-cell';
            let inputEl;
            if (p.isSelect) {
                inputEl = document.createElement('select');
                p.options.forEach(opt => {
                    const o = document.createElement('option');
                    o.value = opt.value;
                    o.innerText = opt.label;
                    inputEl.appendChild(o);
                });
            } else {
                inputEl = document.createElement('input');
                inputEl.type = p.isText ? 'text' : 'number';
            }
            inputEl.id = p.id;
            inputEl.oninput = markConfigDirty;
            wrapper.appendChild(inputEl);

            if (p.axis) {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'secondary get-current-btn';
                btn.innerText = 'Get Current';
                btn.onclick = () => getCurrentIntoRow(p.id, p.axis);
                wrapper.appendChild(btn);
            }
            newTd.appendChild(wrapper);
            tr.appendChild(newTd);

            const descTd = document.createElement('td');
            descTd.className = 'param-desc';
            descTd.innerText = p.desc;
            tr.appendChild(descTd);

            tbody.appendChild(tr);
        });

        // start_x/end_x/step_size_x etc. also drive the scan-grid preview
        ['start_x', 'start_y', 'end_x', 'end_y', 'step_size_x', 'step_size_y',
         'step_size_x_train', 'step_size_y_train', 'stack_frames', 'framerate'].forEach(id => {
            document.getElementById(id).addEventListener('input', updateScanGrid);
        });
        document.getElementById('dual_set').addEventListener('change', updateScanGrid);
    }

    function markConfigDirty() {
        document.getElementById('saveConfigBtn').classList.add('dirty');
    }

    function getCurrentIntoRow(fieldId, axis) {
        fetch('/api/position').then(r => r.json()).then(pos => {
            document.getElementById(fieldId).value = pos[axis];
            markConfigDirty();
        });
    }

    // Keys added after the original presets were written. A preset that predates
    // them must fall back to these rather than inheriting whatever the previously
    // loaded config left in the form - otherwise loading an old single-grid preset
    // could silently keep dual_set on and scan a second grid nobody asked for.
    const CONFIG_FALLBACKS = {
        dual_set: 'off',
        step_size_x_train: 8,
        step_size_y_train: 11
    };

    function applyConfigValues(data) {
        const merged = Object.assign({}, CONFIG_FALLBACKS, data);
        for (const [key, value] of Object.entries(merged)) {
            const el = document.getElementById(key);
            if (el) el.value = value;
            const curEl = document.getElementById('cur_cfg_' + key);
            if (curEl) curEl.innerText = value;
        }
        updateScanGrid();
    }

    // Load config on startup
    window.onload = function() {
        buildConfigTable();
        updatePos();
        fetch('/api/config').then(r=>r.json()).then(data => {
            applyConfigValues(data);
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
            applyConfigValues(data);
            document.getElementById('saveConfigBtn').classList.remove('dirty');
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
                        markConfigDirty();
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
            'dual_set', 'step_size_x_train', 'step_size_y_train',
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
                CONFIG_PARAMS.forEach(p => {
                    const curEl = document.getElementById('cur_cfg_' + p.id);
                    if (curEl) curEl.innerText = document.getElementById(p.id).value;
                });
                document.getElementById('saveConfigBtn').classList.remove('dirty');
                alert('Configuration saved to ' + data.filepath);
                populateConfigList();
            } else {
                alert('Error saving configuration: ' + data.message);
            }
        });
    }

    function startScan() {
        if(streamActive) { alert("Please turn off video feed first."); return; }
        
        const sampleName = document.getElementById('sample_name').value;
        const outputFolder = document.getElementById('output_folder').value;

        if (!sampleName) {
            alert("Please enter a sample name.");
            return;
        }

        if (!confirm("Confirm Sample Name: " + sampleName)) {
            return;
        }

        const dualSet = document.getElementById('dual_set').value;

        fetch('/api/validate_path', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({output_folder: outputFolder, sample_name: sampleName, dual_set: dualSet})
        })
        .then(r => r.json())
        .then(validation => {
            if (validation.exists) {
                alert("These output folders already exist:\n\n  " + validation.collisions.join("\n  ") +
                      "\n\nPlease enter a different sample name.");
                return;
            }

            // Gather data
            let config = {};
            const inputs = document.querySelectorAll('input, select');
            inputs.forEach(i => config[i.id] = i.value);
            
            fetch('/api/scan/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(config)
            }).then(jsonOrThrow).then(data => {
                if(data.status === 'error') alert(data.message);
                else {
                    monitorScan();
                }
            }).catch(err => alert("Could not start the scan.\n\n" + err.message));
        });
    }

    // A Flask 500 returns an HTML traceback, not JSON. Without this, r.json()
    // rejects, nothing catches it, and the button appears to do nothing at all.
    function jsonOrThrow(r) {
        if (r.ok) return r.json();
        return r.text().then(t => {
            throw new Error("Server returned " + r.status + ".\n\n" + t.slice(0, 600));
        });
    }
    
    function stopScan() {
        fetch('/api/scan/stop', {method: 'POST'});
    }

    // Log endpoint the Script Output pane is currently following, or null.
    let activeLogUrl = null;

    function refreshLog() {
        if (!activeLogUrl) return;
        fetch(activeLogUrl).then(r => r.json()).then(d => {
            const pre = document.getElementById('runLog');
            document.getElementById('runLogBox').style.display = 'block';
            // Don't yank the view back down if the user has scrolled up to read.
            const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30;
            pre.textContent = d.log || '(no output yet)';
            if (atBottom) pre.scrollTop = pre.scrollHeight;
        }).catch(() => {});
    }

    function monitorScan() {
        document.getElementById('progressBarContainer').style.display = 'block';
        document.getElementById('scanStatus').innerText = "SCANNING...";
        activeLogUrl = '/api/scan/log';
        refreshLog();

        let check = setInterval(() => {
            fetch('/api/scan/status').then(r=>r.json()).then(d => {
                refreshLog();
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
    
    function runZStackTest() {
        if(streamActive) { alert("Please turn off video feed first."); return; }

        const sampleName = document.getElementById('sample_name').value;
        if (!sampleName) {
            alert("Please enter a sample name.");
            return;
        }

        // Save current form state to the scan config so the test uses up-to-date settings
        let config = {};
        const inputs = document.querySelectorAll('input, select');
        inputs.forEach(i => config[i.id] = i.value);

        fetch('/api/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(config)
        })
        .then(jsonOrThrow)
        .then(() => fetch('/api/scan/zstack_test', {method: 'POST'}))
        .then(jsonOrThrow)
        .then(data => {
            if (data.status === 'error') {
                alert(data.message);
            } else {
                monitorZStackTest();
            }
        })
        .catch(err => alert("Could not start the Z-stack test.\n\n" + err.message));
    }

    function monitorZStackTest() {
        const btn = document.getElementById('zstackTestBtn');
        btn.disabled = true;
        btn.innerText = "Running Z-Stack Test...";
        activeLogUrl = '/api/scan/zstack_test/log';
        refreshLog();

        let check = setInterval(() => {
            fetch('/api/scan/zstack_test/status').then(r=>r.json()).then(d => {
                refreshLog();
                if (!d.running) {
                    clearInterval(check);
                    btn.disabled = false;
                    btn.innerText = "Test Z Motion (Focus Stack)";
                    // Re-read the log before reporting the outcome: the script's
                    // own warning is the only signal that a capture was
                    // truncated, and a bare "complete" would hide it.
                    fetch('/api/scan/zstack_test/log').then(r=>r.json()).then(l => {
                        const log = l.log || '';
                        document.getElementById('runLogBox').style.display = 'block';
                        document.getElementById('runLog').textContent = log || '(no output)';
                        if (log.indexOf('WARNING') !== -1 || log.indexOf('[PRINTER ERROR]') !== -1) {
                            alert("Z-stack test finished WITH ERRORS - see Script Output under the scan buttons. The capture is probably incomplete.");
                        } else {
                            alert("Z-stack test complete.");
                        }
                    });
                }
            });
        }, 2000);
    }

    // Per-tile cost of everything other than the Z sweep: combined travel move,
    // settle, encoder open/close, HTTP round-trips. Mirrors PER_TILE_OVERHEAD_S
    // in scanner.py - keep both in sync. Measured from a real run 2026-09-10.
    const PER_TILE_OVERHEAD_S = 0.4;

    // Mirrors build_grid() in scanner.py. Note steps is a count of intervals
    // used as a count of points, so the last tile lands one step short of end.
    function gridFor(startX, startY, endX, endY, stepX, stepY) {
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

        return {
            stepsX: stepsX,
            stepsY: stepsY,
            tiles: stepsX * stepsY,
            endX: startX + ((stepsX - 1) * absStepX * Math.sign(width)),
            endY: startY + ((stepsY - 1) * absStepY * Math.sign(height))
        };
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

        const infer = gridFor(startX, startY, endX, endY, stepX, stepY);

        document.getElementById('calc_steps_x').innerText = infer.stepsX;
        document.getElementById('calc_steps_y').innerText = infer.stepsY;
        document.getElementById('calc_tiles_infer').innerText = infer.tiles;
        document.getElementById('actual_end_x').innerText = infer.endX.toFixed(2);
        document.getElementById('actual_end_y').innerText = infer.endY.toFixed(2);

        let totalTiles = infer.tiles;

        const dualSet = document.getElementById('dual_set').value === 'on';
        const trainLine = document.getElementById('train_grid_line');
        const stepXTrain = parseFloat(document.getElementById('step_size_x_train').value);
        const stepYTrain = parseFloat(document.getElementById('step_size_y_train').value);

        if (dualSet && !isNaN(stepXTrain) && !isNaN(stepYTrain)) {
            const train = gridFor(startX, startY, endX, endY, stepXTrain, stepYTrain);
            document.getElementById('calc_steps_x_train').innerText = train.stepsX;
            document.getElementById('calc_steps_y_train').innerText = train.stepsY;
            document.getElementById('calc_tiles_train').innerText = train.tiles;
            document.getElementById('actual_end_x_train').innerText = train.endX.toFixed(2);
            document.getElementById('actual_end_y_train').innerText = train.endY.toFixed(2);
            trainLine.style.display = '';
            totalTiles += train.tiles;
        } else {
            trainLine.style.display = 'none';
        }

        const frames = parseFloat(document.getElementById('stack_frames').value);
        const fps = parseFloat(document.getElementById('framerate').value);
        const sweep = (!isNaN(frames) && !isNaN(fps) && fps > 0) ? frames / fps : 0;

        document.getElementById('calc_tiles_total').innerText = totalTiles;
        document.getElementById('calc_duration').innerText =
            (totalTiles * (sweep + PER_TILE_OVERHEAD_S) / 60).toFixed(1);
    }

</script>
</body>
</html>
"""

if __name__ == '__main__':
    # Threaded mode is required to handle video streaming requests alongside API calls
    app.run(host='0.0.0.0', port=5000, threaded=True)