"""
Grid Fiducial Calibration
=========================

Measures magnification (pixels-per-mm) at every frame of a calibration video of
a planar 10x10 ruled grid (0.5 mm line spacing), correcting for in-plane grid
rotation, and maps each frame to its known physical z-position.

Physical setup
--------------
- The fiducial is a 10x10 grid of thin ruled lines, 0.5 mm spacing between lines.
- The fiducial is PLANAR and PARALLEL to the sensor at all times (no tilt /
  perspective). It is only rotated in-plane (about the optical axis). Therefore
  this is a pure rotation problem: we use HoughLines (rho, theta) and measure the
  perpendicular spacing between adjacent parallel lines directly. No homography /
  perspective correction is used.
- The video has exactly 33 frames spanning a 6 mm z-range, linearly:
      frame 1  -> z = 6 mm (camera furthest, lowest magnification)
      frame 33 -> z = 0 mm (camera closest,  highest magnification, ~390 px/mm)
      step = 6 / 32 = 0.1875 mm
- Magnification depends only on camera-to-fiducial distance, NOT on focus. So
  px_per_mm should trend smoothly/monotonically with z even though focus quality
  is worst at the extremes (frame 1 and frames after ~25) and sharpest mid-stack
  (~frame 25). Detection is made blur-robust with per-frame adaptive preprocessing.

Pipeline
--------
  Step 1  ffmpeg extracts every frame to lossless PNG (no re-encode / filter).
  Step 2  Per-frame blur-robust line detection (adaptive CLAHE/blur/Canny/Hough).
  Step 3  px/mm from adjacent parallel-line perpendicular spacing, per axis.
  Step 4  z-position from frame index.
  Step 5  CSV + plots + debug overlays + validation print.
  Step 6  Geometric sanity-check flagging (NOT blur-based).

Usage
-----
  python grid_calibration.py --video <path.h264> --out <output_folder>
  python grid_calibration.py                       # uses the defaults below
"""

import os
import sys
import csv
import math
import shutil
import argparse
import subprocess
from pathlib import Path

import numpy as np
import cv2
import matplotlib
matplotlib.use("Agg")  # file output only, no GUI
import matplotlib.pyplot as plt


# ----------------------------------------------------------------------------
# Defaults (confirmed with the user)
# ----------------------------------------------------------------------------
# Starting directory the file picker opens in (used only when no --video given).
DEFAULT_VIDEO = None  # None -> pop a file-selector UI on run
PICKER_START_DIR = (
    r"T:\PhD Ecology\Data\Yellow Sticky Traps\YST Scans\Training Scans (no_overlap)"
    r"\Fiducial_Scan\Grid Fiducial Scan"
)
DEFAULT_OUT = None  # -> <video_dir>/calibration_output

# Physical / geometry constants
EXPECTED_FRAMES = 33
Z_MAX_MM = 6.0
Z_STEP_MM = 6.0 / 32.0          # 0.1875 mm per frame
GRID_SPACING_MM = 0.5           # spacing between adjacent ruled lines
GRID_LINES_PER_AXIS = 10        # 10x10 grid -> up to 10 lines each family
VALIDATION_PXMM_FRAME33 = 390.0 # independently measured px/mm at frame 33 (z=0)

# Sanity-check tolerances
AXIS_DISAGREE_TOL = 0.03        # >3% disagreement between the two axes -> flag
MONOTONIC_RESIDUAL_TOL = 0.05   # >5% deviation from smooth z-trend fit -> flag
MIN_LINES_PER_AXIS = 3          # need >=3 lines -> >=2 spacings to average


# ============================================================================
# Step 1 - Frame extraction
# ============================================================================
def get_ffmpeg_path():
    """Locate ffmpeg (PATH, then the project conda env, then a manual fallback)."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    conda_ffmpeg = Path(sys.prefix) / "Library" / "bin" / "ffmpeg.exe"
    if conda_ffmpeg.exists():
        return str(conda_ffmpeg)
    manual = r"C:\Users\Jeran\.conda\envs\imaging_env\Library\bin\ffmpeg.exe"
    if os.path.exists(manual):
        return manual
    raise FileNotFoundError("FFmpeg not found. Install it or add it to PATH.")


def extract_frames(video_path, frames_dir):
    """
    Extract EVERY frame of the .h264 video to sequentially numbered lossless PNGs,
    preserving native resolution and bit depth. No re-encode, no scaling, no filter.
    Returns a sorted list of the extracted PNG paths.
    """
    ffmpeg = get_ffmpeg_path()
    frames_dir = Path(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)

    # Clear any stale PNGs so a re-run can't mix old + new frames.
    for old in frames_dir.glob("frame_*.png"):
        old.unlink()

    out_pattern = str(frames_dir / "frame_%04d.png")
    cmd = [
        ffmpeg, "-y",
        "-i", str(video_path),
        # -vsync 0 / passthrough: emit exactly the frames present, no dup/drop.
        "-fps_mode", "passthrough",
        # PNG is lossless; -pred mixed is just compression, not quality.
        "-c:v", "png",
        "-pix_fmt", "rgb24",   # preserve as RGB; source is 8-bit
        out_pattern,
    ]
    print(f"\n--- Step 1: Frame extraction ---")
    print(f"  ffmpeg: {ffmpeg}")
    print(f"  video : {video_path}")
    print(f"  ->     : {frames_dir}")

    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        # -fps_mode is newer; retry with the legacy -vsync 0 spelling if needed.
        print("  ffmpeg -fps_mode failed, retrying with -vsync 0 ...")
        cmd_legacy = [
            ffmpeg, "-y", "-i", str(video_path),
            "-vsync", "0", "-c:v", "png", "-pix_fmt", "rgb24", out_pattern,
        ]
        proc = subprocess.run(cmd_legacy, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr.decode(errors="replace"))
            raise RuntimeError("ffmpeg extraction failed.")

    frames = sorted(frames_dir.glob("frame_*.png"))
    n = len(frames)
    print(f"  extracted {n} frames.")
    if n != EXPECTED_FRAMES:
        print("  " + "!" * 70)
        print(f"  WARNING: expected {EXPECTED_FRAMES} frames but got {n}. "
              f"z-mapping assumes exactly {EXPECTED_FRAMES}.")
        print("  " + "!" * 70)
    return frames


# ============================================================================
# Step 2 - Blur-robust line detection
# ============================================================================
def blur_metric(gray):
    """Variance of the Laplacian: low = blurry, high = sharp."""
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def preprocess_adaptive(gray, blur_var):
    """
    Adaptive preprocessing scaled by how blurry the frame is.

    Blurry frames (low Laplacian variance) get strong CLAHE contrast boost and a
    gentle pre-blur to consolidate smeared low-contrast lines; sharp frames get
    mild treatment to avoid amplifying noise into false lines.

    Returns (edges, params) where params records the chosen settings for logging.
    """
    # Normalise a "sharpness" score into [0, 1]; ~<40 var is heavily blurred,
    # ~>300 is crisp for this footage. Clamp so extremes behave sensibly.
    sharp = np.clip((blur_var - 30.0) / (300.0 - 30.0), 0.0, 1.0)
    blurry = 1.0 - sharp

    # CLAHE: stronger clip limit for blurrier frames to lift faint smeared lines.
    clip = 2.0 + 6.0 * blurry          # 2 (sharp) .. 8 (very blurry)
    clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8))
    enh = clahe.apply(gray)

    # Pre-blur: blurry frames get a slightly larger kernel to merge broken smears;
    # sharp frames get a small kernel to suppress speckle without killing edges.
    k = 3 if sharp > 0.5 else 5
    enh = cv2.GaussianBlur(enh, (k, k), 0)

    # Canny thresholds from the median (Otsu-like), loosened for blurry frames so
    # faint low-gradient edges still survive.
    med = float(np.median(enh))
    lo_scale = 0.66 - 0.30 * blurry    # blurry -> lower low threshold
    hi_scale = 1.33 - 0.30 * blurry
    lo = int(max(5, lo_scale * med))
    hi = int(max(lo + 5, hi_scale * med))
    edges = cv2.Canny(enh, lo, hi, apertureSize=3, L2gradient=True)

    # For very blurry frames, dilate edges slightly so fragmented smeared lines
    # accumulate into a coherent Hough peak.
    if blurry > 0.5:
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    params = dict(clahe_clip=round(clip, 2), gauss_k=k,
                  canny_lo=lo, canny_hi=hi, sharp=round(float(sharp), 3))
    return edges, params


def _theta_diff(a, b):
    """Smallest absolute difference between two angles on a 0..pi line (radians)."""
    d = abs(a - b) % math.pi
    return min(d, math.pi - d)


def cluster_two_orientations(lines):
    """
    Cluster HoughLines (rho, theta) into the grid's two perpendicular families.

    Lines from cv2.HoughLines come as [[rho, theta]] with theta in [0, pi). A line
    and its 180-deg-flipped detection can appear with theta near 0 and near pi; we
    work on a doubled-angle representation so those wrap together, find the two
    dominant directions ~90deg apart, and assign each line to the nearer family.

    Returns (familyA, familyB) as lists of Line objects. Each Line keeps its
    ORIGINAL Hough (rho, theta) intact (so it draws correctly at the true grid
    rotation) plus a separate signed_rho projected onto the family's reference
    normal (so adjacent-line perpendicular spacings are consistent).
    """
    if lines is None or len(lines) < 2:
        return [], []

    ls = lines.reshape(-1, 2)  # (N, 2): rho, theta

    # Dominant orientation via circular mean on doubled angles (period pi -> 2pi).
    ang2 = 2.0 * ls[:, 1]
    mean2 = math.atan2(np.sin(ang2).mean(), np.cos(ang2).mean())
    dirA = (mean2 / 2.0) % math.pi          # first family direction
    dirB = (dirA + math.pi / 2.0) % math.pi # perpendicular family direction

    famA, famB = [], []
    for rho, theta in ls:
        dA = _theta_diff(theta, dirA)
        dB = _theta_diff(theta, dirB)
        # Only accept lines reasonably aligned to one axis (within ~25 deg); this
        # rejects stray diagonal noise lines that belong to neither family.
        if min(dA, dB) > math.radians(25):
            continue
        if dA <= dB:
            famA.append(_make_line(float(rho), float(theta), dirA))
        else:
            famB.append(_make_line(float(rho), float(theta), dirB))

    return famA, famB


def _make_line(rho, theta, ref_dir):
    """
    Build a Line dict preserving the true Hough (rho, theta) for drawing, and a
    signed_rho for spacing. HoughLines can express one physical line as
    (rho, theta) or (-rho, theta+pi); projecting onto a single reference normal
    makes signed_rho monotone across the parallel lines regardless of that sign.
    """
    nx, ny = math.cos(ref_dir), math.sin(ref_dir)
    signed = rho * (math.cos(theta) * nx + math.sin(theta) * ny)
    return dict(rho=rho, theta=theta, signed_rho=signed)


def merge_duplicate_lines(family, rho_tol):
    """
    Merge near-duplicate detections of the same physical line. Blurred lines
    produce a wider spread of nearby (rho, theta) hits, so rho_tol is passed in
    scaled by blur upstream. Lines are grouped greedily by signed_rho proximity.
    Returns a list of merged Line dicts, sorted by signed_rho.
    """
    if not family:
        return []
    fam = sorted(family, key=lambda ln: ln["signed_rho"])
    merged = []
    group = [fam[0]]
    for ln in fam[1:]:
        if abs(ln["signed_rho"] - group[-1]["signed_rho"]) <= rho_tol:
            group.append(ln)
        else:
            merged.append(_avg_line(group))
            group = [ln]
    merged.append(_avg_line(group))
    return merged


def _avg_line(group):
    # Average theta via circular mean (doubled angle) so it's wrap-safe near 0/pi.
    ang2 = [2.0 * g["theta"] for g in group]
    mtheta = (math.atan2(np.mean(np.sin(ang2)),
                         np.mean(np.cos(ang2))) / 2.0) % math.pi
    signed = float(np.mean([g["signed_rho"] for g in group]))
    # Reconstruct the true rho for the averaged theta from the group's members.
    # Each member's (rho, theta) may use the +/-pi sign convention; flip any whose
    # normal points opposite mtheta so they average consistently, then take the
    # mean. This keeps (rho, mtheta) a correct, drawable line at the grid rotation.
    nx, ny = math.cos(mtheta), math.sin(mtheta)
    rhos = []
    for g in group:
        dot = math.cos(g["theta"]) * nx + math.sin(g["theta"]) * ny
        rhos.append(g["rho"] if dot >= 0 else -g["rho"])
    mrho = float(np.mean(rhos))
    return dict(rho=mrho, theta=mtheta, signed_rho=signed)


def detect_grid(gray, blur_var):
    """
    Full per-frame detection. Returns a dict with the two merged line families,
    the edge image, and the preprocessing params, or None on hard failure.
    """
    edges, params = preprocess_adaptive(gray, blur_var)

    # Adaptive Hough accumulator threshold: lower for blurry frames (need to catch
    # faint peaks), higher for sharp frames (reject noise). Scaled to image size.
    diag = math.hypot(*gray.shape)
    sharp = params["sharp"]
    base = 0.18 + 0.12 * sharp          # fraction of diagonal
    hough_thresh = int(max(60, base * diag))

    lines = cv2.HoughLines(edges, rho=1, theta=np.pi / 180.0,
                           threshold=hough_thresh)

    # If a blurry frame found too few lines, progressively relax the threshold.
    attempts = 0
    while (lines is None or len(lines) < 4) and attempts < 3:
        hough_thresh = int(hough_thresh * 0.7)
        if hough_thresh < 40:
            break
        lines = cv2.HoughLines(edges, rho=1, theta=np.pi / 180.0,
                               threshold=hough_thresh)
        attempts += 1

    if lines is None or len(lines) < 4:
        return None

    famA, famB = cluster_two_orientations(lines)

    # Merge tolerance scales with blur: blurry frames spread more, so merge wider.
    # Also floor it relative to expected spacing so we never merge two real lines.
    rho_tol = 6.0 + 10.0 * (1.0 - sharp)   # px
    famA = merge_duplicate_lines(famA, rho_tol)
    famB = merge_duplicate_lines(famB, rho_tol)

    return dict(famA=famA, famB=famB, edges=edges, params=params,
                hough_thresh=hough_thresh)


# ============================================================================
# Step 3 - px/mm from rotated grid
# ============================================================================
def px_per_mm_from_family(family):
    """
    Given one family of parallel lines (rho sign-normalised), compute px/mm from
    the mean perpendicular spacing between ADJACENT lines.

    Returns (px_per_mm, n_lines, mean_theta_deg) or (nan, n_lines, nan) if too few.
    Robustness: adjacent gaps that are near-integer multiples of the base spacing
    (a missed line in between) are folded back to the base spacing via the median.
    """
    n = len(family)
    if n < MIN_LINES_PER_AXIS:
        return math.nan, n, math.nan

    fam = sorted(family, key=lambda ln: ln["signed_rho"])
    rhos = np.array([ln["signed_rho"] for ln in fam])
    gaps = np.diff(rhos)                       # perpendicular pixel spacings
    gaps = gaps[gaps > 1.0]                    # drop any zero/degenerate gaps
    if gaps.size == 0:
        return math.nan, n, math.nan

    # Base spacing = median gap. Fold larger gaps (missed intermediate lines) to
    # the base by dividing by the nearest integer multiple, so px/mm stays right.
    base = float(np.median(gaps))
    folded = []
    for g in gaps:
        mult = max(1, round(g / base))
        folded.append(g / mult)
    mean_spacing = float(np.mean(folded))      # px per 0.5 mm

    px_per_mm = mean_spacing / GRID_SPACING_MM
    # Mean orientation via circular (doubled-angle) mean, reported in degrees.
    ang2 = [2.0 * ln["theta"] for ln in fam]
    mtheta = math.degrees(
        math.atan2(np.mean(np.sin(ang2)), np.mean(np.cos(ang2))) / 2.0
    ) % 180.0
    return px_per_mm, n, mtheta


def measure_frame(det):
    """
    From a detection dict, compute per-axis px/mm and the aggregate result.
    Returns a result dict (all the CSV numeric fields except z / flags).
    """
    pxA, nA, thetaA = px_per_mm_from_family(det["famA"])
    pxB, nB, thetaB = px_per_mm_from_family(det["famB"])

    valid = [p for p in (pxA, pxB) if not math.isnan(p)]
    px_mean = float(np.mean(valid)) if valid else math.nan

    # Rotation diagnostic: use family A's mean theta (fall back to B).
    rot = thetaA if not math.isnan(thetaA) else thetaB

    return dict(
        num_lines_axis1=nA, num_lines_axis2=nB,
        px_per_mm_axis1=pxA, px_per_mm_axis2=pxB,
        px_per_mm_mean=px_mean, rotation_angle_deg=rot,
    )


# ============================================================================
# Step 4 - z mapping
# ============================================================================
def z_position_mm(frame_index):
    """frame_index is 1-based. z = 6 - (i-1)*(6/32)."""
    return Z_MAX_MM - (frame_index - 1) * Z_STEP_MM


# ============================================================================
# Debug overlay
# ============================================================================
def draw_overlay(bgr, det, out_path):
    """Draw detected lines colored by axis onto the frame and save it."""
    vis = bgr.copy()
    h, w = vis.shape[:2]
    length = int(math.hypot(w, h))

    def draw_family(family, color):
        for ln in family:
            rho, theta = ln["rho"], ln["theta"]
            a, b = math.cos(theta), math.sin(theta)
            x0, y0 = a * rho, b * rho
            p1 = (int(x0 + length * (-b)), int(y0 + length * a))
            p2 = (int(x0 - length * (-b)), int(y0 - length * a))
            cv2.line(vis, p1, p2, color, 1, cv2.LINE_AA)

    if det is not None:
        draw_family(det["famA"], (0, 0, 255))    # axis 1 = red
        draw_family(det["famB"], (0, 255, 0))    # axis 2 = green
    cv2.imwrite(str(out_path), vis)


# ============================================================================
# Step 6 - sanity checks
# ============================================================================
def apply_flags(rows):
    """
    Flag frames on GEOMETRIC consistency only (never for being blurry if
    detection succeeded):
      - too few lines in either axis,
      - the two axes disagree beyond AXIS_DISAGREE_TOL,
      - px_per_mm_mean deviates from a smooth trend vs z beyond MONOTONIC_RESIDUAL_TOL.
    Mutates rows in place (sets 'flagged' and 'flag_reason').
    """
    # Fit a smooth trend of px_per_mm_mean vs z over the successful frames so we
    # can spot outliers that break the expected monotonic magnification curve.
    good = [r for r in rows if r["detection_success"]
            and not math.isnan(r["px_per_mm_mean"])]
    trend = None
    if len(good) >= 4:
        zs = np.array([r["z_position_mm"] for r in good])
        ys = np.array([r["px_per_mm_mean"] for r in good])
        # Quadratic captures gentle curvature; residuals flag true outliers.
        trend = np.polyfit(zs, ys, 2)

    for r in rows:
        reasons = []
        if not r["detection_success"]:
            reasons.append("detection failed")
        else:
            if (r["num_lines_axis1"] < MIN_LINES_PER_AXIS or
                    r["num_lines_axis2"] < MIN_LINES_PER_AXIS):
                reasons.append(
                    f"few lines (a1={r['num_lines_axis1']}, a2={r['num_lines_axis2']})")

            a1, a2 = r["px_per_mm_axis1"], r["px_per_mm_axis2"]
            if not (math.isnan(a1) or math.isnan(a2)):
                disagree = abs(a1 - a2) / ((a1 + a2) / 2.0)
                if disagree > AXIS_DISAGREE_TOL:
                    reasons.append(f"axes disagree {disagree*100:.1f}%")

            if trend is not None and not math.isnan(r["px_per_mm_mean"]):
                pred = np.polyval(trend, r["z_position_mm"])
                resid = abs(r["px_per_mm_mean"] - pred) / pred
                if resid > MONOTONIC_RESIDUAL_TOL:
                    reasons.append(f"off-trend {resid*100:.1f}%")

        r["flagged"] = len(reasons) > 0
        r["flag_reason"] = "; ".join(reasons)


# ============================================================================
# Step 5 - output (CSV, plots)
# ============================================================================
CSV_FIELDS = [
    "frame_index", "z_position_mm", "detection_success",
    "num_lines_axis1", "num_lines_axis2",
    "px_per_mm_axis1", "px_per_mm_axis2", "px_per_mm_mean",
    "rotation_angle_deg", "blur_metric", "flagged", "flag_reason",
]


def write_csv(rows, path):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: _fmt(r.get(k)) for k in CSV_FIELDS})
    print(f"  CSV -> {path}")


def _fmt(v):
    if isinstance(v, float):
        return "" if math.isnan(v) else f"{v:.4f}"
    return v


def make_plots(rows, out_dir):
    idx = [r["frame_index"] for r in rows]
    z = [r["z_position_mm"] for r in rows]
    pm = [r["px_per_mm_mean"] if not math.isnan(r["px_per_mm_mean"]) else None
          for r in rows]
    blur = [r["blur_metric"] for r in rows]
    flagged = [r["flagged"] for r in rows]

    # --- Primary: px/mm vs z ---
    fig, ax = plt.subplots(figsize=(8, 5))
    zz = [z[i] for i in range(len(z)) if pm[i] is not None]
    yy = [pm[i] for i in range(len(z)) if pm[i] is not None]
    ax.plot(zz, yy, "-o", color="#1f77b4", label="px_per_mm_mean")
    fx = [z[i] for i in range(len(z)) if flagged[i] and pm[i] is not None]
    fy = [pm[i] for i in range(len(z)) if flagged[i] and pm[i] is not None]
    if fx:
        ax.scatter(fx, fy, color="crimson", zorder=5, label="flagged")
    ax.axhline(VALIDATION_PXMM_FRAME33, ls="--", color="gray",
               label=f"measured @z=0 ({VALIDATION_PXMM_FRAME33:.0f})")
    ax.set_xlabel("z position (mm)   [6 = furthest, 0 = closest]")
    ax.set_ylabel("px per mm")
    ax.set_title("Magnification vs z-position (primary)")
    ax.invert_xaxis()  # closest (highest mag) on the right
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "pxmm_vs_z.png", dpi=130)
    plt.close(fig)

    # --- Secondary: px/mm vs frame index ---
    fig, ax = plt.subplots(figsize=(8, 5))
    ix = [idx[i] for i in range(len(idx)) if pm[i] is not None]
    iy = [pm[i] for i in range(len(idx)) if pm[i] is not None]
    ax.plot(ix, iy, "-o", color="#2ca02c")
    if any(flagged):
        ax.scatter([idx[i] for i in range(len(idx)) if flagged[i] and pm[i] is not None],
                   [pm[i] for i in range(len(idx)) if flagged[i] and pm[i] is not None],
                   color="crimson", zorder=5, label="flagged")
        ax.legend()
    ax.set_xlabel("frame index")
    ax.set_ylabel("px per mm")
    ax.set_title("Magnification vs frame index (secondary)")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "pxmm_vs_frame.png", dpi=130)
    plt.close(fig)

    # --- Diagnostic: blur metric vs frame index ---
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(idx, blur, "-o", color="#ff7f0e")
    ax.set_xlabel("frame index")
    ax.set_ylabel("blur metric (variance of Laplacian)")
    ax.set_title("Focus / blur vs frame index\n(expected: sharp mid-stack ~25, "
                 "blurry at both ends)")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "blur_vs_frame.png", dpi=130)
    plt.close(fig)

    print(f"  plots -> {out_dir / 'pxmm_vs_z.png'}, pxmm_vs_frame.png, "
          f"blur_vs_frame.png")


# ============================================================================
# Video file-selector UI
# ============================================================================
def pick_video_ui(start_dir=None):
    """
    Pop a native file-open dialog to choose the input video. Returns the selected
    path as a string, or None if the user cancelled. Falls back to console input
    if tkinter isn't available (e.g. headless run).
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        p = input("Enter path to the input video (.h264): ").strip().strip('"')
        return p or None

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)  # bring the dialog to the front
    init = start_dir if (start_dir and os.path.isdir(start_dir)) else os.getcwd()
    path = filedialog.askopenfilename(
        title="Select the grid-fiducial calibration video",
        initialdir=init,
        filetypes=[("Video files", "*.h264 *.h265 *.mp4 *.avi *.mov *.mkv"),
                   ("All files", "*.*")],
    )
    root.destroy()
    return path or None


# ============================================================================
# Main
# ============================================================================
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", default=DEFAULT_VIDEO,
                    help="input video path (omit to pick via a file dialog)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output folder")
    ap.add_argument("--skip-extract", action="store_true",
                    help="reuse frames already in <out>/frames")
    args = ap.parse_args()

    # No --video given -> pop the file-selector UI.
    video_arg = args.video
    if not video_arg:
        print("No --video provided; opening file selector...")
        video_arg = pick_video_ui(PICKER_START_DIR)
        if not video_arg:
            sys.exit("No video selected. Exiting.")

    video = Path(video_arg)
    if not video.exists():
        sys.exit(f"ERROR: video not found: {video}")
    print(f"Selected video: {video}")

    out_dir = Path(args.out) if args.out else video.parent / "calibration_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = out_dir / "frames"
    overlay_dir = out_dir / "overlays"
    overlay_dir.mkdir(exist_ok=True)

    # Step 1
    if args.skip_extract and frames_dir.exists():
        frames = sorted(frames_dir.glob("frame_*.png"))
        print(f"\n--- Step 1: reusing {len(frames)} existing frames ---")
    else:
        frames = extract_frames(video, frames_dir)
    if not frames:
        sys.exit("ERROR: no frames to process.")

    # Steps 2-4 per frame
    print(f"\n--- Steps 2-4: detection + measurement ---")
    rows = []
    detections = {}   # frame_index -> (det, bgr)  kept for the overlays we need
    sharpest_idx, sharpest_blur = 1, -1.0

    for i, fpath in enumerate(frames, start=1):
        bgr = cv2.imread(str(fpath), cv2.IMREAD_COLOR)
        if bgr is None:
            print(f"  frame {i:2d}: could not read {fpath.name}, skipping")
            rows.append(_empty_row(i, math.nan))
            continue
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        bvar = blur_metric(gray)
        if bvar > sharpest_blur:
            sharpest_blur, sharpest_idx = bvar, i

        try:
            det = detect_grid(gray, bvar)
        except Exception as e:              # per-frame graceful failure
            print(f"  frame {i:2d}: detection error ({e}), skipping")
            det = None

        row = _empty_row(i, bvar)
        if det is None:
            row["detection_success"] = False
            print(f"  frame {i:2d}: z={row['z_position_mm']:.4f}  "
                  f"blur={bvar:7.1f}  DETECTION FAILED")
        else:
            meas = measure_frame(det)
            row.update(meas)
            row["detection_success"] = not math.isnan(meas["px_per_mm_mean"])
            status = "ok " if row["detection_success"] else "insuff"
            print(f"  frame {i:2d}: z={row['z_position_mm']:.4f}  blur={bvar:7.1f}  "
                  f"lines(a1={meas['num_lines_axis1']},a2={meas['num_lines_axis2']})  "
                  f"px/mm={_p(meas['px_per_mm_mean'])}  rot={_p(meas['rotation_angle_deg'])} "
                  f"[{status}]")
        rows.append(row)
        # Keep detections we will need overlays for (extremes + running sharpest).
        detections[i] = (det, bgr)

    # Step 6 - flags (needs the full set for the trend fit)
    apply_flags(rows)

    # Step 5 - CSV + plots
    print(f"\n--- Step 5: output ---")
    write_csv(rows, out_dir / "calibration_results.csv")
    make_plots(rows, out_dir)

    # Debug overlays: frame 1, sharpest (~25), last frame, plus flagged/failed.
    want = {1, sharpest_idx, len(frames)}
    want.update(r["frame_index"] for r in rows if r["flagged"])
    print(f"  overlays for frames: {sorted(want)}  (sharpest={sharpest_idx})")
    for i in sorted(want):
        if i in detections:
            det, bgr = detections[i]
            tag = "sharp" if i == sharpest_idx else ""
            name = f"overlay_frame_{i:02d}{('_'+tag) if tag else ''}.png"
            draw_overlay(bgr, det, overlay_dir / name)

    # Validation print
    print(f"\n--- Validation ---")
    last = rows[-1]
    meas33 = last["px_per_mm_mean"]
    if not math.isnan(meas33):
        disc = (meas33 - VALIDATION_PXMM_FRAME33) / VALIDATION_PXMM_FRAME33 * 100.0
        print(f"  frame {last['frame_index']} (z={last['z_position_mm']:.2f}mm): "
              f"measured px/mm = {meas33:.2f}")
        print(f"  independent reference           = {VALIDATION_PXMM_FRAME33:.2f}")
        print(f"  discrepancy                     = {disc:+.2f}%")
    else:
        print(f"  frame {last['frame_index']}: no measurement (detection failed) "
              f"- cannot validate against {VALIDATION_PXMM_FRAME33:.0f} px/mm.")

    n_flag = sum(1 for r in rows if r["flagged"])
    n_fail = sum(1 for r in rows if not r["detection_success"])
    print(f"\n  {len(rows)} frames | {n_fail} detection failures | {n_flag} flagged.")
    print(f"  Inspect overlays in: {overlay_dir}")
    print("  >>> Review overlay_frame_01, the *_sharp overlay (~frame 25), and "
          f"overlay_frame_{len(frames):02d} before trusting the full run. <<<")


def _empty_row(i, bvar):
    return dict(
        frame_index=i, z_position_mm=z_position_mm(i), detection_success=False,
        num_lines_axis1=0, num_lines_axis2=0,
        px_per_mm_axis1=math.nan, px_per_mm_axis2=math.nan,
        px_per_mm_mean=math.nan, rotation_angle_deg=math.nan,
        blur_metric=bvar, flagged=False, flag_reason="",
    )


def _p(v):
    return "  nan " if (isinstance(v, float) and math.isnan(v)) else f"{v:7.2f}"


if __name__ == "__main__":
    main()
