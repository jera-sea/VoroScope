# VoroScope — Integrated Scanning Interface

## Project Overview

A Flask web interface that drives the VoroScope: a Voron 2.4 printer converted into a
microscope-carrying lab-automation scanner. It runs on a Raspberry Pi bolted to the machine,
talks to the motion system over Moonraker HTTP, and captures focus-stack video with a
Picamera2 sensor. The operator jogs the head, defines a scan area, tunes camera settings and
launches a grid scan from a browser on the lab network.

Current state: functional and in use for real sample scans. The 2026-09-08 dual-set rewrite was
verified on hardware on 2026-09-10 with a full infer + train run launched from the web UI. See
*Current State* below for what that run covered and what is still unproven.

## Architecture & Key Decisions

- **Flask + embedded HTML, single file.** `app.py` holds both the API and the entire frontend
  in one `HTML_TEMPLATE` string (roughly lines 470–1000) rendered by `render_template_string`.
  This is deliberate — one file to copy onto the Pi, no static/ or templates/ directories to
  keep in sync. There is no build step and no JS framework.
- **Motion via Moonraker, not serial.** `hardware.py`'s `PrinterInterface` POSTs G-code to
  `http://127.0.0.1:7125/printer/gcode/script`. Every move ends in `M400`, so the HTTP request
  blocks until Klipper has drained the move queue. **A timeout does not stop the machine** —
  the move keeps running while the caller carries on, which silently desynchronises a scan.
  That is why `send_gcode`/`move_to` take an explicit `timeout` and the Z sweep passes
  `duration + 10`.
- **Captures are launched as subprocesses, not threads.** `/api/scan/start` and
  `/api/scan/zstack_test` each `subprocess.Popen(["python3", ...])`. Picamera2 does not
  tolerate two owners in one process, and a separate process can be SIGINT-ed cleanly and
  cannot take the web server down with it. `scan_process` and `zstack_test_process` are kept
  as **separate globals** so a test run cannot corrupt a real scan's status/stop tracking.
- **Camera is single-owner, guarded by `camera_lock` + `streaming_active`.** The live MJPEG
  preview and any capture script are mutually exclusive; the scan/test/calibrate routes all
  return HTTP 409 if the stream is up. `/api/stream/force_release` is the escape hatch for a
  stuck lock.
- **All camera settings are manual during capture.** `AeEnable`/`AwbEnable` are forced off and
  exposure, analogue gain and colour gains come from config. Auto-exposure between tiles would
  make tiles photometrically inconsistent and unusable for stitching or model training.
- **Config values are stored as strings.** The UI writes whatever the form fields hold, so
  every consumer coerces via a `NUMERIC_KEYS` list (`scanner.load_config`,
  `single_zstack.run_single_zstack`). Don't assume a config value is a float.
- **Two config layers.** `scan_config.json` is the live working config, overwritten by the UI
  on every save/scan/test. `configs/*.json` are named presets, loaded and saved through
  `/api/configs/*`. Preset filenames are sanitised against path traversal.
- **Dual-set capture in one camera session.** A scan always emits an overlapping `-infer`
  grid; with `dual_set: on` it also captures a non-overlapping `-train` grid, back-to-back
  under the same camera object so exposure/gain/AWB are identical by construction.
  *Rejected alternative:* interleaving the two grids into a single pass. The pitches are
  incommensurate (5.5/8.6 vs 8/11 mm), so over a half-card the grids share only 2 of 295
  positions and merging saves ~9 s of a scan that measures ~17 min. Per-tile Z-sweep time
  dominates; XY travel is ~10% of the scan. **Don't revisit the merge without new
  information.**

## Current State

### Working

- Jog / position readout / move-to-corner, live MJPEG preview, white-balance calibration,
  named config presets, scan start/stop/status polling.
- The grid preview in the UI (`gridFor`/`updateScanGrid`) mirrors `scanner.build_grid`
  exactly, including the interval-vs-point quirk below, and shows a duration estimate.

### Verified on hardware — 2026-09-10

A full dual-set run, launched from the web UI, confirmed the 2026-09-08 rewrite:

- **Dual-set capture works.** Infer and train grids captured back-to-back in one camera
  session, each into its own suffixed folder with a `manifest.csv`.
- **The low-Z inter-tile traverse is clear.** This was the rewrite's main hardware risk — the
  head now traverses in XY *while* Z ramps back to `stack_start_z`, instead of rising clear
  first, so it crosses the bed roughly 6 mm lower than it used to. It ran without touching
  anything. That is a fact about *this* fixture at the tested Z range, not a property of the
  code: re-check it if the card holder, card height or Z range changes.
- **The 3-panel UI drove the scan on-device** — config table, two-grid preview and
  `/api/scan/start` all worked against real hardware for the first time.
- **Timing:** 339 tiles in ~19 min with a 3.0 s sweep (30 frames @ 10 fps) — see the per-tile
  overhead note under *Known quirks*.

### ⚠️ Unverified on hardware (older)

- **"Test Z Motion (Focus Stack)" button** (2026-07-07) — the route and subprocess launch were
  never exercised on-device. Confirm: config save → subprocess launch → output file appears →
  re-click while running is blocked (409) → doesn't disturb a real scan's status/stop.
- **`card_change.py`** — standalone, **not imported by `app.py` or anything else**; runs only
  via its `__main__` block. `POS_SCAN_AREA` / `POS_CARTRIDGE` coordinates are placeholders
  needing physical calibration, and the gripper needs `[servo gripper]` in `printer.cfg`.
  Card-change is *not* live in the UI.

### Known quirks / open items

- **`steps_x`/`steps_y` count intervals but are used as a count of points**, so the last tile
  sits one full step short of `end_x`/`end_y`. Both `scanner.build_grid` and the JS preview do
  this consistently, and the UI reports the "actual end" — it is a known behaviour, not a bug
  to silently fix.
- **All scans write to `{sample_name}-infer/`.** The `-infer` suffix is unconditional, so even
  a single-grid scan lands in the suffixed folder rather than the bare `{sample_name}/` it used
  before 2026-09-08. `single_zstack.py` still writes unsuffixed — that inconsistency is known
  and unresolved. `/api/validate_path` checks the *suffixed* folders for collisions.
- `PER_TILE_OVERHEAD_S = 0.4` (in both `scanner.py` and the JS) is everything in a tile other
  than the Z sweep. Measured 2026-09-10: 339 tiles in ~19 min with a 3.0 s sweep = 3.36 s per
  tile, so ~0.36 s of real overhead; 0.4 keeps the estimate a shade conservative. The previous
  1.1 s guess over-predicted that run by ~22% (23.2 min vs 19). Re-measure if the per-tile work
  changes.
- `__pycache__/` is untracked and there is no `.gitignore` anywhere in the repo.

## File Structure

```
Integrated Scanning Interface/
├── app.py              Flask server + entire embedded frontend (~1000 lines)
├── hardware.py         PrinterInterface — Moonraker HTTP wrapper (get_position/send_gcode/move_to)
├── scanner.py          Full grid scan; the subprocess /api/scan/start launches. Has --dry-run.
├── single_zstack.py    One focus-stack column; the subprocess the Z-stack test button launches
├── calibrate.py        One-shot 3A capture → calibration.json (exposure/gain/AWB)
├── card_change.py      Card swap sequence (gripper servo). Standalone, uncalibrated, unwired.
├── configs/            Named presets (*.json)
├── scan_config.json    Live working config, rewritten by the UI (not in configs/)
└── calibration.json    Written by calibrate.py, read by the UI
```

### `scanner.py` internals

`load_config` → `build_sets` (which grids, where they land) → per set `build_grid`
(steps + signed step, so a reversed area still scans) → `iter_tiles` (snake order; `xi` is the
**true column index**, not traversal position, so `tile_{yi}_{xi}` stays a clean lattice) →
`scan_set` (per-tile: combined travel move, 0.2 s settle, record, Z sweep, `stop_recording` in
a `finally` so an abort leaves no truncated `.h264`).

Each set folder gets a **`manifest.csv`** — `filename, set, row, col, x_mm, y_mm, z_start,
z_end`, flushed per tile. Nothing previously recorded tile positions; downstream de-duplication
of objects seen in overlapping tiles needs it.

`picamera2` and `hardware` are imported *inside* `run_scan`, not at module level, so
`--dry-run` works on a bare off-Pi Python.

### Key API routes

| Route | Purpose |
|---|---|
| `GET/POST /api/config` | read / overwrite `scan_config.json` |
| `GET /api/configs/list`, `GET /api/configs/load/<f>`, `POST /api/config/save_named` | named presets |
| `POST /api/move`, `GET /api/position` | jog and position readout |
| `GET /video_feed`, `POST /api/stream/toggle`, `POST /api/stream/force_release` | MJPEG preview + lock escape hatch |
| `POST /api/calibrate`, `GET /api/calibration_status` | white-balance calibration |
| `POST /api/validate_path` | collision guard — refuses a scan over existing **suffixed** folders |
| `POST /api/scan/start`, `GET /api/scan/status`, `POST /api/scan/stop` | full scan (stop = SIGINT) |
| `POST /api/scan/zstack_test`, `GET /api/scan/zstack_test/status` | single-column focus-stack test |

### Frontend layout

Three CSS-grid panels: **Motion Control** (position, jog, move-to-corner), **Configuration**
(preset load/save + a JS-generated 4-column table), **Camera & Scan Control** (`sample_name`,
preview, calibration, Z-stack test, scan start/stop/progress).

The config table is **fully generated from the `CONFIG_PARAMS` array** by `buildConfigTable()`
— *edit that array, never hand-write table rows*. Each entry carries `id`, `label`, `desc`,
and optionally `axis` (adds an inline "Get Current" button that fills the row from live machine
position), `isSelect` + `options`, or `isText` (needed for `output_folder`, which would
otherwise render as a number input).

Dirty-state tracking: any edit to a New-Value cell calls `markConfigDirty()`, which turns the
Save button red via a `.dirty` class; `saveConfig()`/`loadSelectedConfig()` sync the
Current-Value column and clear it. `sample_name` is deliberately **excluded** — it lives in
another panel and shouldn't flag the config as unsaved.

`CONFIG_FALLBACKS` (`dual_set: 'off'`, train steps 8/11) is merged under loaded config data so
a legacy preset with no `dual_set` key cannot silently inherit `on` from whatever was in the
form.

## Build / Run / Upload Instructions

There is no build step. On the Pi, from this directory:

```bash
python3 app.py            # serves on 0.0.0.0:5000 — open http://<pi-ip>:5000
```

Threaded mode is required so the video stream doesn't block API calls. Subprocesses are spawned
as literal `python3`, so it must be the same interpreter that has `picamera2`.

Scan / test scripts directly, without the UI:

```bash
python3 scanner.py configs/dual_set_half_card.json --dry-run   # tile list + estimate, no hardware
python3 scanner.py scan_config.json                            # real scan
python3 single_zstack.py scan_config.json                      # one focus-stack column
python3 calibrate.py                                           # writes calibration.json
```

**On the Windows dev machine** the app cannot run (no `picamera2`, no Moonraker) — only
`scanner.py --dry-run` will. Note conda is not on PATH; call an interpreter directly, e.g.
`C:\Users\Jeran\.conda\envs\imaging_env\python.exe` (that env has cv2/numpy/matplotlib).

## Dependencies & Environment

- **Pi runtime:** Python 3, `flask`, `requests`, `opencv-python` (cv2, for MJPEG encoding),
  `picamera2` (+ `H264Encoder`). Capture is H.264 at 25 Mbit/s; preview is 640×480 MJPEG.
- **Motion:** Klipper + Moonraker at `http://127.0.0.1:7125` on the same Pi. Klipper clamps the
  Z component of any move to `max_z_velocity` (15 mm/s in `printer.cfg`), which is why the
  combined XY+Z travel move costs the same wall time as the Z move alone.
- **Machine config** lives at `../Voron 2.4 Config/printer.cfg`.
- Typical scan geometry: half card ≈ X 100→193, Y 50→140; infer step 5.5 × 8.6 mm; train step
  8 × 11 mm; Z sweep 43→37 mm, 30 frames @ 10 fps.
- Output normally goes to `/home/pi/camera/ssd/scans` (external SSD).

## Related, outside this folder

- `../Scan Processing/` — post-processing: `grid_calibration.py` (per-frame px/mm vs z from a
  grid fiducial video; validated to +1.37%; **run with the `imaging_env` python**),
  `process_scan.py`, `merged_workflow.py`, h264/jpg/tiff converters.
- `../Roboflow Dataset Editing/prune_overlaps.py` — thins overlapping snake-scan tiles out of
  the Roboflow dataset by **tagging only, never deleting** (Roboflow's delete API is
  permanent). This is why the `-train` set must be non-overlapping.
- `../Test Scripts/`, `../Voron 2.4 Config/`.

## Working Preferences

**The user runs and tests scripts themselves.** After making edits, hand back a summary of what
changed, what to look for, and the exact command — then stop. Do not auto-run, including
`--dry-run`, `--help` or import checks; the preference is about who initiates execution, not
about side effects. Offer the run, don't perform it.

## User Direction History

- **Card change automation** — Feb 2026
  - *What was done:* Added `card_change.py`, a standalone unload/load sequence using
    `PrinterInterface` G-code moves plus a gripper servo, with a cartridge slot list.
  - *Why this approach:* Kept out of `app.py` until the physical gripper and coordinates are
    calibrated, so an unverified motion sequence can't be triggered from the UI by accident.

- **Z-stack test button repurposed** — 2026-07-07
  - *What was done:* Replaced `/api/scan/z_dry_run` (motion-only, no camera) with
    `/api/scan/zstack_test` + `/status`, launching `single_zstack.py` as its own subprocess
    tracked by a separate global. `single_zstack.py` gained a `sys.argv[1]` config path and now
    saves to `<output_folder>/<sample_name>/`.
  - *Why this approach:* The user wanted to validate focus/exposure/Z-range by actually
    capturing a column before committing to a full XY scan, reusing the proven
    Z-sweep-while-recording logic rather than writing a motion-only test.

- **UI restructured into 3 functional panels** — 2026-07-08
  - *What was done:* Collapsed 5 topic-grouped cards into Motion Control / Configuration /
    Camera & Scan Control, with the config table generated from `CONFIG_PARAMS`, inline "Get
    Current" buttons, per-row descriptions, and dirty-state tracking on the Save button. Fixed
    a pre-existing duplicate-id bug (`calc_steps_x`/`calc_steps_y` each appeared twice, so one
    copy never updated) and deleted the dead `toggleStream()` function.
  - *Why this approach:* The user wanted parameters presented uniformly (current vs new value,
    with inline docs) and clear unsaved-state feedback, with the UI split by *function* rather
    than by hardware topic.

- **Repo reorganised** — commit `3947c54`
  - *What was done:* Moved the current working files into `Integrated Scanning Interface/`,
    removing the duplicate `Scanning Scripts/` and `Machine_version/` copies of
    `single_zstack.py` that had drifted apart.
  - *Why this approach:* Three near-identical copies with different hardcoded config paths were
    a standing source of "fixed it in the wrong file" errors.

- **Dual-set scan rewrite** — 2026-09-08
  - *What was done:* `scanner.py` restructured into `build_sets`/`build_grid`/`iter_tiles`/
    `scan_set` to capture an overlapping `-infer` grid and an optional non-overlapping `-train`
    grid back-to-back in one camera session; added per-tile `manifest.csv`; cut per-tile
    Moonraker POSTs 4 → 2 by folding the Z return into the next tile's travel; added `timeout`
    to `hardware.send_gcode`/`move_to` and passed `duration + 10` for the sweep; added
    `dual_set` + train-step config rows, a two-grid preview with duration estimate, a collision
    guard over the suffixed folders, and `CONFIG_FALLBACKS`.
  - *Why this approach:* Inference wants overlap so no object is cut across a tile edge;
    training data wants no overlap so the same object isn't learned twice. Running both in one
    camera session guarantees identical exposure/gain/AWB. Interleaving into one pass was
    considered and rejected (incommensurate pitches — see Architecture). The `timeout` addition
    fixed a real latent bug: any move over 5 s previously raised, was swallowed, and the scan
    carried on while the head was still moving.

- **Dual-set scan verified on hardware** — 2026-09-10
  - *What was done:* Ran a full infer + train scan from the web UI: 339 tiles in ~19 min at
    30 frames @ 10 fps. Both suffixed folders and their manifests were produced, the low-Z
    inter-tile traverse cleared the fixture, and the 3-panel UI drove real hardware for the
    first time. `PER_TILE_OVERHEAD_S` was corrected 1.1 → 0.4 in `scanner.py` and the JS mirror
    from the measured 3.36 s per tile.
  - *Why this approach:* The rewrite had been sitting unrun since 2026-09-08, which made it
    unsafe to build on — any later failure would have been ambiguous between the two changes.
    Testing it first cleared that. An early read of the run suggested the infer and train
    labels were swapped, but the output folders had simply been misread; `build_sets` maps
    `step_size_x/y` → infer and `step_size_x_train/y_train` → train, and that is correct.
