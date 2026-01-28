import requests
import time

MOONRAKER_URL = "http://127.0.0.1:7125"

class PrinterInterface:
    def __init__(self, url=MOONRAKER_URL):
        self.url = url

    def get_position(self):
        """Returns (x, y, z) or None if error."""
        try:
            url = f"{self.url}/printer/objects/query?toolhead=position"
            response = requests.get(url, timeout=2)
            response.raise_for_status()
            pos = response.json()['result']['status']['toolhead']['position']
            return pos[0], pos[1], pos[2]
        except Exception as e:
            print(f"[PRINTER ERROR] Get Position: {e}")
            return None, None, None

    def send_gcode(self, command):
        """Sends a G-code command and waits for acknowledgement."""
        try:
            url = f"{self.url}/printer/gcode/script"
            response = requests.post(url, json={"script": command}, timeout=5)
            response.raise_for_status()
            return True
        except Exception as e:
            print(f"[PRINTER ERROR] Send GCode '{command}': {e}")
            return False

    def move_to(self, x=None, y=None, z=None, speed=5000):
        """Smart move command. Only moves axes that are not None."""
        cmd = "G90\n" # Absolute positioning
        move_parts = []
        if x is not None: move_parts.append(f"X{x:.2f}")
        if y is not None: move_parts.append(f"Y{y:.2f}")
        if z is not None: move_parts.append(f"Z{z:.2f}")
        
        if move_parts:
            cmd += f"G0 {' '.join(move_parts)} F{speed}\nM400"
            return self.send_gcode(cmd)
        return True