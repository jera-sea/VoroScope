import time
from hardware import PrinterInterface

# ==========================================
# CONFIGURATION
# ==========================================

# Servo Configuration
# Ensure this servo is defined in your Klipper printer.cfg
# e.g. [servo gripper]
SERVO_NAME = "gripper" 
SERVO_ANGLE_OPEN = 0
SERVO_ANGLE_CLOSED = 90
SERVO_DELAY = 0.5  # Seconds to wait for servo to move

# Speed Settings (mm/min)
TRAVEL_SPEED_XY = 6000
TRAVEL_SPEED_Z = 1500
SLOW_SPEED_Y = 2000  # For precise insertion/retraction

# --- Coordinate Definitions ---
# These need to be carefully calibrated for the physical setup.

# 1. Scan Area (where the card is held for scanning)
POS_SCAN_AREA = {
    "x": 100.0,
    "y": 100.0,
    "z_pickup": 5.0,   # Z height to grab/drop the card
    "z_safe": 50.0,    # Safe Z height for travel over the scan area
    "y_clear": 90.0    # Y position to move to after dropping card
}

# 2. Cartridge (List of slots for cards)
# Used for both picking up new cards and returning used ones.
# For a full sequence, you would iterate through this list.
# This example assumes a dispenser where cards are pulled out along the Y axis.
POS_CARTRIDGE = [
    { "x": 200.0, "z": 15.0, "y_approach": 50.0, "y_grab": 20.0, "y_retract": 150.0 },
    { "x": 200.0, "z": 10.0, "y_approach": 50.0, "y_grab": 20.0, "y_retract": 150.0 }, # Next card might be lower
    # { "x": 200.0, "z": 13.0, "y_approach": 50.0, "y_grab": 60.0, "y_retract": 40.0 },
]

# ==========================================
# HELPER FUNCTIONS
# ==========================================

def set_servo(printer, angle):
    """Sets the servo angle via G-code."""
    print(f"  > Servo: {angle}°")
    printer.send_gcode(f"SET_SERVO SERVO={SERVO_NAME} ANGLE={angle}")
    time.sleep(SERVO_DELAY)

def disable_servo(printer):
    """Turns off the servo PWM to prevent jitter/heating."""
    printer.send_gcode(f"SET_SERVO SERVO={SERVO_NAME} WIDTH=0")

# ==========================================
# MAIN SEQUENCE
# ==========================================

def run_card_change(unload_index=None, load_index=None):
    """
    Executes the full card change sequence.
    :param unload_index: The index of the card currently on the scanner (to return). If None, skips unload.
    :param load_index: The index of the next card to pick up. If None, skips load.
    """
    printer = PrinterInterface()
    print("Connected to Printer. Starting Card Change Sequence...")

    # --- PART 1: RETURN USED CARD ---
    if unload_index is not None:
        if unload_index >= len(POS_CARTRIDGE):
            print(f"ERROR: Unload index {unload_index} out of bounds.")
            return
        
        return_pos = POS_CARTRIDGE[unload_index]
        print(f"\n[1/4] Picking up used card (Index {unload_index}) from scan area...")
        
        # 1. Move to active_card_pickup safe position
        printer.move_to(z=POS_SCAN_AREA["z_safe"], speed=TRAVEL_SPEED_Z)
        printer.move_to(x=POS_SCAN_AREA["x"], y=POS_SCAN_AREA["y_clear"], speed=TRAVEL_SPEED_XY)

        # 2. Descend to pickup
        printer.move_to(z=POS_SCAN_AREA["z_pickup"], speed=TRAVEL_SPEED_Z)

        #move gripper over card
        printer.move_to(y=POS_SCAN_AREA["y"], speed=TRAVEL_SPEED_XY)

        # 3. Grip
        set_servo(printer, SERVO_ANGLE_CLOSED)

        # 4. Lift
        printer.move_to(z=POS_SCAN_AREA["z_safe"], speed=TRAVEL_SPEED_Z)

        print(f"\n[2/4] Returning card to cartridge slot {unload_index}...")
        
        # 5. Move to cartridge slot approach
        #    Move Z safe, then XY approach, then Z slot height
        printer.move_to(z=POS_SCAN_AREA["z_safe"], speed=TRAVEL_SPEED_Z)
        printer.move_to(x=return_pos["x"], y=return_pos["y_retract"], speed=TRAVEL_SPEED_XY)
        printer.move_to(z=return_pos["z"], speed=TRAVEL_SPEED_Z)
        

        # 6. Insert card (Move Y to grab/insert depth)
        printer.move_to(y=return_pos["y_grab"], speed=SLOW_SPEED_Y)

        # 7. Release
        set_servo(printer, SERVO_ANGLE_OPEN)

        # 8. Retract gripper
        printer.move_to(y=return_pos["y_retract"], speed=SLOW_SPEED_Y)
    else:
        print("\n[1-2/4] Skipping Unload (No unload index provided).")

    # --- PART 2: PICKUP NEW CARD ---
    if load_index is not None:
        if load_index >= len(POS_CARTRIDGE):
            print(f"ERROR: Load index {load_index} out of bounds.")
            disable_servo(printer)
            return

        new_card_pos = POS_CARTRIDGE[load_index]
        print(f"\n[3/4] Picking up new card (Index {load_index}) from cartridge...")

        # 9. Move to cartridge slot approach
        #    (We might already be near the cartridge, but safety first)
        printer.move_to(z=POS_SCAN_AREA["z_safe"], speed=TRAVEL_SPEED_Z)
        printer.move_to(x=new_card_pos["x"], y=new_card_pos["y_approach"], speed=TRAVEL_SPEED_XY)
        printer.move_to(z=new_card_pos["z"], speed=TRAVEL_SPEED_Z)

        # 10. Move gripper over card
        printer.move_to(y=new_card_pos["y_grab"], speed=SLOW_SPEED_Y)

        # 11. Grip
        set_servo(printer, SERVO_ANGLE_CLOSED)

        # 12. Retract card
        printer.move_to(y=new_card_pos["y_retract"], speed=SLOW_SPEED_Y)

        # --- PART 3: PLACE NEW CARD ---
        print("\n[4/4] Placing new card in scan area...")
        # 13. Move to scan area
        printer.move_to(z=POS_SCAN_AREA["z_safe"], speed=TRAVEL_SPEED_Z)
        printer.move_to(x=POS_SCAN_AREA["x"], y=POS_SCAN_AREA["y"], speed=TRAVEL_SPEED_XY)
        printer.move_to(z=POS_SCAN_AREA["z_pickup"], speed=TRAVEL_SPEED_Z)

        # 14. Release
        set_servo(printer, SERVO_ANGLE_OPEN)

        # 15. Clear active zone
        printer.move_to(y=POS_SCAN_AREA["y_clear"], speed=SLOW_SPEED_Y)
    else:
        print("\n[3-4/4] Skipping Load (No load index provided).")

    # 16. Finish at Safe Z
    printer.move_to(z=POS_SCAN_AREA["z_safe"], speed=TRAVEL_SPEED_Z)

    # --- FINAL CLEANUP ---
    disable_servo(printer)
    print("\nSequence Complete.")

if __name__ == "__main__":
    # Standalone Test:
    # Assume we are swapping Card 0 (on scanner) for Card 1 (in cartridge).
    run_card_change(unload_index=0, load_index=1)