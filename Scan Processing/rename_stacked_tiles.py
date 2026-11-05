"""
VoroScope Rename Stacked Tiles
==============================

Renames stacked images made with the old batch_stack_scans.py naming to the
merged_workflow.py naming. Files stay in their scan folder:

    <scan name>\\tile_r_c.jpg  ->  <scan name>\\<scan name>_tile_r_c.jpg

User Guide:
-----------
1. Run as is (APPLY = False) to preview the renames. Nothing is changed.
2. Set APPLY = True and run again to rename the files.

- Only .jpg files named exactly tile_r_c.jpg, directly inside a scan folder, are
  renamed. Manifests, logs, zip files and already renamed images are left alone.
- If any new name already exists, the run stops before renaming anything.
- Stopping and re-running is safe: images that were renamed no longer match.
- Each rename is recorded in ROOT\\rename_log.csv.
"""
import csv
import datetime
import re
import sys
from pathlib import Path

# --- CONFIGURATION ---
ROOT = Path(r"T:\PhD Ecology\Data\Yellow Sticky Traps\August 2026\stacked_scans")
APPLY = True  # False = preview only, True = rename the files

OLD_NAME = re.compile(r"^tile_\d+_\d+\.jpg$", re.IGNORECASE)
LOG_NAME = "rename_log.csv"


def plan_renames(root):
    """Returns (source, target) paths for every old-named image, scan by scan."""
    renames = []
    for scan_dir in sorted(d for d in root.iterdir() if d.is_dir()):
        for image in sorted(f for f in scan_dir.iterdir() if f.is_file()):
            if OLD_NAME.match(image.name):
                renames.append((image, scan_dir / f"{scan_dir.name}_{image.name}"))
    return renames


def apply_renames(renames, log_path):
    """Renames the files, logging each one. Returns the number renamed."""
    write_header = not log_path.exists()
    done = 0
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["time", "folder", "old_name", "new_name"])
        for source, target in renames:
            source.rename(target)
            writer.writerow([datetime.datetime.now().isoformat(timespec="seconds"),
                             source.parent.name, source.name, target.name])
            f.flush()
            done += 1
    return done


def main():
    if not ROOT.is_dir():
        print(f"Folder not found: {ROOT}")
        return 1

    renames = plan_renames(ROOT)
    scans = len({source.parent for source, _ in renames})
    print(f"Found {len(renames)} images to rename in {scans} scan folders.")
    if not renames:
        return 0

    for source, target in renames[:5]:
        print(f"  {source.parent.name}\\{source.name} -> {target.name}")
    if len(renames) > 5:
        print(f"  ... and {len(renames) - 5} more")

    collisions = [target for _, target in renames if target.exists()]
    if collisions:
        print(f"[Error] {len(collisions)} new names already exist, e.g. {collisions[0]}")
        print("Nothing was renamed.")
        return 1

    if not APPLY:
        print("Preview only, nothing was renamed. Set APPLY = True to rename the files.")
        return 0

    try:
        done = apply_renames(renames, ROOT / LOG_NAME)
    except OSError as e:
        print(f"[Error] Stopped early: {e}")
        print(f"Completed renames are in {ROOT / LOG_NAME}. Re-run to continue.")
        return 1

    print(f"Renamed {done} images. Log: {ROOT / LOG_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
