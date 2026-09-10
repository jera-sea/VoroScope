import os
import sys
import time
import json
import csv
import argparse
import math

# picamera2 and hardware are imported inside run_scan so that --dry-run works
# off-Pi on a bare Python (it needs neither the camera nor Moonraker).

# Output folder suffixes. Every scan produces an "-infer" set (the one the
# vision model runs over); the optional second, non-overlapping grid used for
# training data goes to "-train".
INFER_SUFFIX = "-infer"
TRAIN_SUFFIX = "-train"

# Feedrate for the combined XY + Z repositioning move between tiles. Klipper
# clamps the Z component of any move to max_z_velocity (15 mm/s in
# printer.cfg), so this is effectively an XY-only speed request.
TRAVEL_SPEED = 5000

# Feedrate for Z-only repositioning (pre-scan approach, between-set lift).
Z_SPEED = 1000

# Wall-clock cost of everything in a tile other than the Z sweep itself:
# combined travel move, settle, encoder open/close and HTTP round-trips.
# Only used for the duration estimate. Measured 2026-09-10: 339 tiles in ~19 min
# with a 3.0s sweep = 3.36s/tile, so ~0.36s overhead; 0.4 stays a shade
# conservative. Re-measure if the per-tile work changes.
PER_TILE_OVERHEAD_S = 0.4

NUMERIC_KEYS = [
    'start_x', 'end_x', 'start_y', 'end_y',
    'step_size_x', 'step_size_y',
    'step_size_x_train', 'step_size_y_train',
    'stack_start_z', 'stack_end_z', 'stack_frames', 'framerate',
    'exposure_us', 'analogue_gain', 'awb_red', 'awb_blue'
]


def load_config(config_file):
    """Load the scan config, coercing the numeric values the UI stores as strings."""
    with open(config_file, 'r') as f:
        cfg = json.load(f)

    for key in NUMERIC_KEYS:
        if key in cfg:
            cfg[key] = float(cfg[key])
    return cfg


def build_sets(cfg):
    """Return the grids to scan, in capture order.

    Always produces the overlapping "-infer" set from step_size_x/y. When
    dual_set is on, a second non-overlapping "-train" set is appended using
    step_size_x_train/y_train. Both sets share the same area, Z range and
    camera settings by construction - that is the point of running them in one
    invocation rather than as two separate scans.
    """
    base = os.path.join(cfg['output_folder'], cfg['sample_name'])

    sets = [{
        "name": "infer",
        "save_path": base + INFER_SUFFIX,
        "step_x": abs(cfg.get('step_size_x', 5.0)),
        "step_y": abs(cfg.get('step_size_y', 5.0)),
    }]

    if str(cfg.get('dual_set', 'off')).strip().lower() in ('on', 'true', '1', 'yes'):
        sets.append({
            "name": "train",
            "save_path": base + TRAIN_SUFFIX,
            "step_x": abs(cfg.get('step_size_x_train', 8.0)),
            "step_y": abs(cfg.get('step_size_y_train', 11.0)),
        })

    return sets


def build_grid(cfg, step_x, step_y):
    """Grid geometry for one set: (steps_x, steps_y, x_step, y_step).

    Same ceil/sign semantics as the original inline version - note steps_* is a
    count of *intervals* used as a count of *points*, so the last tile sits one
    full step short of end_x/end_y.
    """
    width_mm = cfg['end_x'] - cfg['start_x']
    height_mm = cfg['end_y'] - cfg['start_y']

    if step_x == 0 or step_y == 0:
        raise ValueError("Step sizes cannot be zero.")

    steps_x = max(1, math.ceil(abs(width_mm) / step_x))
    steps_y = max(1, math.ceil(abs(height_mm) / step_y))

    # The actual step value for movement must have the correct sign
    x_step = step_x * (1 if width_mm >= 0 else -1)
    y_step = step_y * (1 if height_mm >= 0 else -1)

    return steps_x, steps_y, x_step, y_step


def iter_tiles(cfg, steps_x, steps_y, x_step, y_step):
    """Yield (yi, xi, x, y) in snake order.

    xi is the true column index, not the traversal position - reversing the
    range on odd rows changes only the visiting order, so tile_{yi}_{xi} stays
    a clean row/column lattice.
    """
    for yi in range(steps_y):
        curr_y = cfg['start_y'] + (yi * y_step)

        # ZigZag Logic
        x_range = range(steps_x) if yi % 2 == 0 else reversed(range(steps_x))

        for xi in x_range:
            yield yi, xi, cfg['start_x'] + (xi * x_step), curr_y


def scan_set(cam, encoder_cls, printer, cfg, scan_set_def,
             set_idx, set_total, z_speed, sweep_timeout):
    """Capture one grid into its own folder, writing a manifest as it goes."""
    steps_x, steps_y, x_step, y_step = build_grid(
        cfg, scan_set_def['step_x'], scan_set_def['step_y'])

    save_path = scan_set_def['save_path']
    os.makedirs(save_path, exist_ok=True)

    total = steps_x * steps_y
    label = f"set {set_idx}/{set_total} ({scan_set_def['name']})"
    print(f"[{label}] {steps_x}x{steps_y} = {total} tiles -> {save_path}")

    manifest_path = os.path.join(save_path, "manifest.csv")
    with open(manifest_path, 'w', newline='') as mf:
        writer = csv.writer(mf)
        writer.writerow(["filename", "set", "row", "col",
                         "x_mm", "y_mm", "z_start", "z_end"])

        tiles = iter_tiles(cfg, steps_x, steps_y, x_step, y_step)
        for n, (yi, xi, curr_x, curr_y) in enumerate(tiles, start=1):
            # Combined travel: move XY to the next tile while Z returns to the
            # sweep start. Klipper clamps the Z component to max_z_velocity, so
            # this costs the same wall time as the Z move alone and the XY
            # travel comes for free.
            printer.move_to(x=curr_x, y=curr_y, z=cfg['stack_start_z'],
                            speed=TRAVEL_SPEED)
            time.sleep(0.2)  # Settle

            name = f"tile_{yi}_{xi}.h264"
            encoder = encoder_cls(bitrate=25000000)
            cam.start_recording(encoder, os.path.join(save_path, name))
            try:
                # Perform Z movement (The "Scan" axis)
                printer.move_to(z=cfg['stack_end_z'], speed=z_speed,
                                timeout=sweep_timeout)
            finally:
                # In a finally so an abort mid-sweep still closes the file
                # cleanly instead of leaving a truncated .h264 behind.
                cam.stop_recording()

            writer.writerow([name, scan_set_def['name'], yi, xi,
                             f"{curr_x:.3f}", f"{curr_y:.3f}",
                             cfg['stack_start_z'], cfg['stack_end_z']])
            mf.flush()

            if n % 10 == 0 or n == total:
                print(f"  [{label}] tile {n}/{total}")

    # Lift clear before the next set's long diagonal traverse back to the
    # grid origin (the per-tile travel move deliberately stays low).
    printer.move_to(z=cfg['stack_start_z'] + 2, speed=Z_SPEED)


def run_scan(config_file):
    # Imported here rather than at module level so --dry-run works off-Pi
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder
    from hardware import PrinterInterface

    # 1. Load Configuration
    cfg = load_config(config_file)

    # 2. Work out which grids to run and where each one lands
    sets = build_sets(cfg)

    # 3. Initialize Hardware
    printer = PrinterInterface()

    # 4. Initialize Camera - once, for every set, so exposure/gain/AWB are
    #    identical across the two sets.
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
    time.sleep(1)  # Settle

    # 5. Z-Stack calculations (shared by every set)
    z_dist = abs(cfg['stack_end_z'] - cfg['stack_start_z'])
    duration = cfg['stack_frames'] / cfg['framerate']
    z_speed = (z_dist / duration) * 60 if duration > 0 else 1000
    # The sweep move blocks until M400 returns, so the HTTP timeout has to
    # outlast the sweep or the scan silently runs on while the head is moving.
    sweep_timeout = duration + 10

    try:
        # Move to safe Z before XY travel
        printer.move_to(z=cfg['stack_start_z'] + 2, speed=Z_SPEED)

        for set_idx, s in enumerate(sets, start=1):
            scan_set(cam, H264Encoder, printer, cfg, s,
                     set_idx, len(sets), z_speed, sweep_timeout)

    finally:
        cam.stop()
        cam.close()
        printer.move_to(z=cfg['stack_start_z'] + 5, speed=Z_SPEED)  # Lift head
        print("Scan Finished.")


def dry_run(config_file):
    """Print the full tile list and duration estimate without touching hardware."""
    cfg = load_config(config_file)
    sets = build_sets(cfg)

    duration = cfg['stack_frames'] / cfg['framerate']
    grand_tiles = 0
    grand_seconds = 0.0

    print(f"Config: {config_file}")
    print(f"Area:   X {cfg['start_x']} -> {cfg['end_x']}, Y {cfg['start_y']} -> {cfg['end_y']}")
    print(f"Z:      {cfg['stack_start_z']} -> {cfg['stack_end_z']}  "
          f"({cfg['stack_frames']:.0f} frames @ {cfg['framerate']:.0f} fps = {duration:.2f}s sweep)")
    print()

    for set_idx, s in enumerate(sets, start=1):
        steps_x, steps_y, x_step, y_step = build_grid(cfg, s['step_x'], s['step_y'])
        total = steps_x * steps_y
        seconds = total * (duration + PER_TILE_OVERHEAD_S)

        print(f"=== set {set_idx}/{len(sets)}: {s['name']} ===")
        print(f"  folder: {s['save_path']}")
        print(f"  step:   {s['step_x']} x {s['step_y']} mm")
        print(f"  grid:   {steps_x} x {steps_y} = {total} tiles")

        for yi, xi, x, y in iter_tiles(cfg, steps_x, steps_y, x_step, y_step):
            print(f"    tile_{yi}_{xi}.h264  row={yi:<3} col={xi:<3} X={x:8.3f} Y={y:8.3f}")

        print(f"  actual end: X={cfg['start_x'] + (steps_x - 1) * x_step:.2f}, "
              f"Y={cfg['start_y'] + (steps_y - 1) * y_step:.2f}")
        print(f"  estimated:  {seconds / 60:.1f} min")
        print()

        grand_tiles += total
        grand_seconds += seconds

    print(f"TOTAL: {grand_tiles} tiles, estimated {grand_seconds / 60:.1f} min "
          f"({duration:.2f}s sweep + {PER_TILE_OVERHEAD_S}s overhead per tile)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("config", help="Path to JSON configuration file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the tile list and duration estimate; touch no hardware")
    args = parser.parse_args()

    if args.dry_run:
        dry_run(args.config)
    else:
        run_scan(args.config)
