"""
Roboflow overlap pruning
========================

Thins a Roboflow dataset of snake-pattern raster-scan tiles so that no two
images remaining in the dataset overlap, keeping the best tile from each
overlapping neighbourhood.

The script NEVER deletes anything and never modifies annotations. Its only
remote write (when APPLY = True) is adding tags to the images that should
leave the dataset. You then do the actual move in the Roboflow UI:
    Dataset -> filter by the "move_to_annotate" tag -> select all ->
    move back to Annotate (annotations are preserved by Roboflow).

Run it straight from VSCode. With API_KEY left empty it only runs the
offline self-tests. With a key and APPLY = False it does a full dry run:
fetches everything, writes a JSON backup of all image records/annotations,
and writes a decisions CSV — but changes nothing on Roboflow.

Selection logic (per sample+side group, 8-neighbourhood = sides + diagonals):
  1. images already tagged "no_overlap" are excluded from all processing
     (their sample was scanned with a non-overlapping grid)
  2. priority to keep: has thrip_yellow > distinct non-dirt classes >
     non-dirt annotation count > lowest (row, col)
  3. blank images (zero annotations of any class) are never kept
  4. a kept tile blocks all 8 neighbours; blocked tiles' own neighbours
     then compete in turn (greedy, in global priority order)
"""

import csv
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime

import requests

# ---------------------------------------------------------------------------
# CONFIG — edit these, run the file. Do NOT save the file with API_KEY filled
# in (avoids accidentally committing/sharing the key).
# ---------------------------------------------------------------------------
API_KEY = ""                # paste your Roboflow API key here; leave empty to run self-tests only
WORKSPACE = "yst-guocz"
PROJECT = "arthropod_segmentations"

APPLY = True               # False = dry run (no remote writes). True = add tags on Roboflow.
SELF_TEST = False           # True = run offline self-tests instead of touching the API

MOVE_TAGS = ["move_to_annotate", "overlap"]  # tags added to images that should leave the dataset
PREFILTER_TAG = "no_overlap"                 # images with this tag are excluded from all processing

THRIP_CLASS = "thrip_yellow"
DIRT_CLASS = "dirt"

SKIP_RAGGED_GROUPS = False  # True = don't touch groups whose rows have inconsistent tile counts
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prune_output")

API_BASE = "https://api.roboflow.com"
SEARCH_PAGE_SIZE = 250      # API maximum
TAG_CHUNK_SIZE = 500        # batch metadata endpoint allows up to 1000

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

RETRY_STATUSES = {429, 500, 502, 503, 504}


def api_request(method, url, json_body=None, max_attempts=5):
    """Request with retry/backoff. Raises RuntimeError after exhausting retries."""
    last_err = None
    for attempt in range(max_attempts):
        try:
            resp = requests.request(
                method, url, params={"api_key": API_KEY},
                json=json_body, timeout=60,
            )
            if resp.status_code in RETRY_STATUSES:
                last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
            else:
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"{method} {url} failed with HTTP {resp.status_code}: {resp.text[:500]}"
                    )
                return resp
        except requests.RequestException as exc:
            last_err = str(exc)
        wait = 2 ** attempt
        print(f"  retrying in {wait}s ({last_err})")
        time.sleep(wait)
    raise RuntimeError(f"{method} {url} failed after {max_attempts} attempts: {last_err}")


def search_page(extra_body):
    url = f"{API_BASE}/{WORKSPACE}/{PROJECT}/search"
    data = api_request("POST", url, extra_body).json()
    results = data.get("results") or data.get("images") or []
    total = data.get("total", data.get("count"))
    return results, total


def count_tagged_in_dataset(tag):
    """Server-side count of dataset images carrying `tag` (consistency check)."""
    _, total = search_page({"in_dataset": True, "tag": tag, "fields": ["id"], "limit": 1})
    return total


def fetch_dataset_images(max_passes=15):
    """
    Fetch every image in the dataset via the search endpoint.

    The search API's result ordering is nondeterministic between requests, so
    offset pagination skips/repeats images within a single sweep. We therefore
    sweep repeatedly, unioning results by image id, until the unique count
    matches the server-reported total. Aborts rather than return a partial set:
    an unfetched image is a missing grid neighbour and would corrupt decisions.
    """
    fields = ["id", "name", "tags", "split", "annotations", "labels", "created"]
    by_id, total = {}, None
    for pass_no in range(1, max_passes + 1):
        offset, rows, new = 0, 0, 0
        while True:
            results, pass_total = search_page(
                {"in_dataset": True, "fields": fields,
                 "limit": SEARCH_PAGE_SIZE, "offset": offset})
            if total is not None and pass_total not in (None, total):
                print(f"  WARNING: server total changed {total} -> {pass_total} "
                      "(project edited mid-run?); using the new value")
            total = pass_total if pass_total is not None else total
            rows += len(results)
            for rec in results:
                if rec.get("id") not in by_id:
                    by_id[rec["id"]] = rec
                    new += 1
            offset += len(results)
            if not results or (total is not None and offset >= total):
                break
        print(f"  pass {pass_no}: {rows} rows, {new} new -> {len(by_id)}"
              + (f" / {total} unique" if total is not None else " unique"))
        if total is None or len(by_id) >= total:
            return list(by_id.values()), pass_no
    raise RuntimeError(
        f"incomplete fetch: {len(by_id)} / {total} unique images after {max_passes} "
        "passes. Refusing to continue — decisions on a partial grid would be wrong. "
        "Re-run the script; if it persists, raise max_passes in fetch_dataset_images()."
    )


def fetch_image_detail(image_id):
    url = f"{API_BASE}/{WORKSPACE}/{PROJECT}/images/{image_id}"
    return api_request("GET", url).json()


def apply_tags(image_ids, tags):
    """Add tags to images via the batch metadata endpoint (falls back to per-image)."""
    url = f"{API_BASE}/{WORKSPACE}/images/metadata"
    tagged = 0
    for i in range(0, len(image_ids), TAG_CHUNK_SIZE):
        chunk = image_ids[i:i + TAG_CHUNK_SIZE]
        body = {"updates": [{"imageId": iid, "addTags": tags} for iid in chunk]}
        try:
            api_request("POST", url, body)
            tagged += len(chunk)
            print(f"  tagged {tagged} / {len(image_ids)}")
        except RuntimeError as exc:
            print(f"  batch tag update failed ({exc}); falling back to per-image requests")
            for iid in chunk:
                api_request("POST", f"{API_BASE}/{WORKSPACE}/images/{iid}/metadata",
                            {"addTags": tags})
                tagged += 1
                if tagged % 50 == 0:
                    print(f"  tagged {tagged} / {len(image_ids)}")
    return tagged


# ---------------------------------------------------------------------------
# Annotation extraction
# ---------------------------------------------------------------------------

def extract_class_counts(record):
    """Return {class_name: count} from a search result, or None if unavailable."""
    ann = record.get("annotations")
    if isinstance(ann, dict):
        classes = ann.get("classes", ann)
        if isinstance(classes, dict):
            counts = {}
            for name, val in classes.items():
                if isinstance(val, (int, float)):
                    counts[str(name)] = int(val)
            # a dict that held no numeric values but has labels is not usable
            if counts or not classes:
                return counts
    if isinstance(ann, list):  # some responses return a box list directly
        counts = defaultdict(int)
        for box in ann:
            if isinstance(box, dict) and "label" in box:
                counts[str(box["label"])] += 1
        return dict(counts)
    if ann in (None, 0) and record.get("labels") == []:
        return {}  # explicitly no annotations
    return None


def counts_from_detail(detail):
    """Return {class_name: count} from a GET image-details response."""
    image = detail.get("image", detail)
    ann = image.get("annotation") or image.get("annotations") or {}
    boxes = ann.get("boxes") if isinstance(ann, dict) else None
    counts = defaultdict(int)
    for box in boxes or []:
        label = box.get("label")
        if label is not None:
            counts[str(label)] += 1
    return dict(counts)


# ---------------------------------------------------------------------------
# Filename parsing & grid reconstruction
# ---------------------------------------------------------------------------

SAMPLE_RE = re.compile(r"([A-Za-z]+\d+P\d+)")
SIDE_RE = re.compile(r"Side[\s_]?([AB])", re.IGNORECASE)
POS_RE = re.compile(r"(?:point|tile)[\s_]*(\d+)[\s_]+(\d+)", re.IGNORECASE)


def parse_filename(name):
    """Return (sample, side, row, num) or None if the name doesn't parse."""
    sample = SAMPLE_RE.search(name)
    side = SIDE_RE.search(name)
    pos = POS_RE.search(name)
    if not (sample and side and pos):
        return None
    return (sample.group(1).upper(), side.group(1).upper(),
            int(pos.group(1)), int(pos.group(2)))


def resolve_columns(group_tiles, warnings, group_key):
    """
    Convert snake-scan image numbers to true columns, in place.
    group_tiles: list of dicts with 'row' and 'num'; adds 'col' to each.
    First (lowest-numbered) row is normal order; every second row reversed.
    """
    min_row = min(t["row"] for t in group_tiles)
    min_num = min(t["num"] for t in group_tiles)
    max_num = max(t["num"] for t in group_tiles)

    by_row = defaultdict(list)
    for t in group_tiles:
        by_row[t["row"]].append(t["num"])

    ragged = False
    for row, nums in sorted(by_row.items()):
        if min(nums) != min_num or max(nums) != max_num:
            ragged = True
            warnings.append(
                f"{group_key}: row {row} spans image numbers {min(nums)}-{max(nums)} "
                f"but the group spans {min_num}-{max_num}; snake reversal may misalign this row"
            )
        dupes = {n for n in nums if nums.count(n) > 1}
        if dupes:
            warnings.append(f"{group_key}: row {row} has duplicate image numbers {sorted(dupes)}")

    for t in group_tiles:
        reversed_row = (t["row"] - min_row) % 2 == 1
        t["col"] = (min_num + max_num - t["num"]) if reversed_row else t["num"]
    return ragged


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

NEIGHBOUR_OFFSETS = [(dr, dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1) if (dr, dc) != (0, 0)]


def priority_stats(class_counts):
    """(has_thrip, non-dirt diversity, non-dirt count, total count) for one image."""
    has_thrip = class_counts.get(THRIP_CLASS, 0) > 0
    non_dirt = {c: n for c, n in class_counts.items() if c != DIRT_CLASS and n > 0}
    diversity = len(non_dirt)
    individuals = sum(non_dirt.values())
    total = sum(n for n in class_counts.values() if n > 0)
    return has_thrip, diversity, individuals, total


def select_keepers(group_tiles):
    """
    Greedy maximum-priority independent set on the tile grid.
    Each tile dict needs 'row', 'col', 'counts'. Sets tile['decision'] to
    KEEP / MOVE / BLANK_MOVE and tile['stats'].
    """
    for t in group_tiles:
        t["stats"] = priority_stats(t["counts"])

    order = sorted(
        group_tiles,
        key=lambda t: (-int(t["stats"][0]), -t["stats"][1], -t["stats"][2],
                       t["row"], t["col"]),
    )
    kept = set()
    for t in order:
        has_thrip, diversity, individuals, total = t["stats"]
        if total == 0:
            t["decision"] = "BLANK_MOVE"  # criterion 4: blanks are never kept
            continue
        pos = (t["row"], t["col"])
        blocked = any((pos[0] + dr, pos[1] + dc) in kept for dr, dc in NEIGHBOUR_OFFSETS)
        if blocked:
            t["decision"] = "MOVE"
        else:
            t["decision"] = "KEEP"
            kept.add(pos)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run():
    if not API_KEY:
        print("API_KEY is empty — running offline self-tests only.\n")
        return run_self_tests()

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    warnings = []

    print(f"Fetching dataset images from {WORKSPACE}/{PROJECT} ...")
    records, passes = fetch_dataset_images()
    if not records:
        print("No images returned — check WORKSPACE / PROJECT / API key.")
        return

    # Consistency check: the server's count of prefilter-tagged dataset images
    # must match what we fetched, otherwise the fetch missed something.
    local_prefiltered = sum(1 for r in records if PREFILTER_TAG in (r.get("tags") or []))
    server_prefiltered = count_tagged_in_dataset(PREFILTER_TAG)
    if server_prefiltered is not None and server_prefiltered != local_prefiltered:
        raise RuntimeError(
            f"consistency check failed: server reports {server_prefiltered} dataset "
            f"images tagged '{PREFILTER_TAG}' but the fetch contains {local_prefiltered}. "
            "Refusing to continue with incomplete data — re-run the script."
        )
    print(f"Fetched all {len(records)} dataset images ({passes} pass(es)); "
          f"{local_prefiltered} tagged '{PREFILTER_TAG}' (server agrees).\n")

    # Build tile records, filling in class counts (fallback to per-image detail).
    tiles, details_fetched = [], 0
    for rec in records:
        name = rec.get("name") or rec.get("filename") or ""
        tags = rec.get("tags") or []
        tile = {"id": rec.get("id"), "name": name, "tags": tags, "record": rec}

        if PREFILTER_TAG in tags:
            tile["decision"] = "SKIPPED_NO_OVERLAP_TAG"
            tiles.append(tile)
            continue

        parsed = parse_filename(name)
        if parsed is None:
            tile["decision"] = "SKIPPED_UNPARSEABLE"
            warnings.append(f"unparseable filename: {name!r} (id {tile['id']})")
            tiles.append(tile)
            continue
        tile["sample"], tile["side"], tile["row"], tile["num"] = parsed

        counts = extract_class_counts(rec)
        if counts is None:
            detail = fetch_image_detail(tile["id"])
            rec["_detail"] = detail  # keep in the backup too
            counts = counts_from_detail(detail)
            details_fetched += 1
            if details_fetched % 50 == 0:
                print(f"  fetched per-image details for {details_fetched} images ...")
        tile["counts"] = counts
        tiles.append(tile)

    if details_fetched:
        print(f"Fetched per-image annotation details for {details_fetched} images "
              "(search results lacked class counts).")

    # Backup everything BEFORE deciding or writing anything remotely.
    backup_path = os.path.join(OUT_DIR, f"backup_{stamp}.json")
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=1)
    print(f"Backup of all {len(records)} image records written to {backup_path}\n")

    # Group by (sample, side), resolve snake columns, select keepers.
    groups = defaultdict(list)
    for t in tiles:
        if "sample" in t and "decision" not in t:
            groups[(t["sample"], t["side"])].append(t)

    for key in sorted(groups):
        group_key = f"{key[0]}_Side{key[1]}"
        group = groups[key]
        ragged = resolve_columns(group, warnings, group_key)
        if ragged and SKIP_RAGGED_GROUPS:
            for t in group:
                t["decision"] = "SKIPPED_RAGGED"
            warnings.append(f"{group_key}: skipped entirely (ragged rows, SKIP_RAGGED_GROUPS=True)")
            continue
        select_keepers(group)

    # Report.
    csv_path = os.path.join(OUT_DIR, f"decisions_{stamp}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["group", "filename", "image_id", "row", "num", "col",
                         "decision", "has_thrip_yellow", "diversity_no_dirt",
                         "count_no_dirt", "total_annotations", "tags"])
        for t in sorted(tiles, key=lambda t: (t.get("sample", "~"), t.get("side", ""),
                                              t.get("row", -1), t.get("col", -1))):
            stats = t.get("stats", ("", "", "", ""))
            writer.writerow([
                f"{t.get('sample', '')}_Side{t.get('side', '')}" if "sample" in t else "",
                t["name"], t["id"], t.get("row", ""), t.get("num", ""), t.get("col", ""),
                t["decision"], stats[0], stats[1], stats[2], stats[3],
                ";".join(t["tags"]),
            ])
    print(f"Decisions written to {csv_path}")

    warn_path = os.path.join(OUT_DIR, f"warnings_{stamp}.txt")
    with open(warn_path, "w", encoding="utf-8") as f:
        f.write("\n".join(warnings) if warnings else "no warnings")
    if warnings:
        print(f"{len(warnings)} warning(s) — see {warn_path}:")
        for w in warnings[:10]:
            print(f"  - {w}")
        if len(warnings) > 10:
            print(f"  ... and {len(warnings) - 10} more")

    summary = defaultdict(int)
    for t in tiles:
        summary[t["decision"]] += 1
    print("\nSummary:")
    for decision, n in sorted(summary.items()):
        print(f"  {decision:24s} {n}")

    to_move = [t for t in tiles if t["decision"] in ("MOVE", "BLANK_MOVE")]
    if not APPLY:
        print(f"\nDRY RUN — nothing changed on Roboflow. {len(to_move)} images would be "
              f"tagged with {MOVE_TAGS}. Review the CSV, then set APPLY = True to tag them.")
        return

    print(f"\nAPPLY = True — adding tags {MOVE_TAGS} to {len(to_move)} images ...")
    tagged = apply_tags([t["id"] for t in to_move], MOVE_TAGS)
    print(f"Done: {tagged} images tagged.")
    print("Next step (in the Roboflow UI): open the Dataset view, filter by the "
          f"'{MOVE_TAGS[0]}' tag, select all, and move them back to Annotate. "
          "Verify the count matches before moving. Nothing has been deleted.")


# ---------------------------------------------------------------------------
# Offline self-tests (no API needed)
# ---------------------------------------------------------------------------

def run_self_tests():
    failures = []

    def check(label, got, want):
        if got != want:
            failures.append(f"{label}: got {got!r}, wanted {want!r}")

    # filename parsing — the two example formats
    check("parse tile-style",
          parse_filename("HT4P2_SideA_April_2026_tile_4_2.jpg"), ("HT4P2", "A", 4, 2))
    check("parse point-style",
          parse_filename("MT1P4 December 2025_Side B_point_10_15.jpg"), ("MT1P4", "B", 10, 15))
    check("unparseable", parse_filename("random_photo_123.jpg"), None)

    # snake reversal: rows 1-2, numbers 1-3; row 2 is reversed
    tiles = [{"row": r, "num": n} for r in (1, 2) for n in (1, 2, 3)]
    resolve_columns(tiles, [], "test")
    cols = {(t["row"], t["num"]): t["col"] for t in tiles}
    check("row1 normal", [cols[(1, n)] for n in (1, 2, 3)], [1, 2, 3])
    check("row2 reversed", [cols[(2, n)] for n in (1, 2, 3)], [3, 2, 1])

    # ragged row detection
    warns = []
    ragged = resolve_columns([{"row": 1, "num": 1}, {"row": 1, "num": 2},
                              {"row": 2, "num": 1}], warns, "ragged-test")
    check("ragged flagged", ragged, True)

    # selection: 2x3 grid. (1,2) has a thrip so it must win and block all
    # neighbours in row 1 and 2; (2,1)/(2,3) are diagonal neighbours -> MOVE.
    def tile(row, col, counts):
        return {"row": row, "col": col, "num": col, "counts": counts}

    grid = [
        tile(1, 1, {"aphid": 5, "dirt": 9}),
        tile(1, 2, {THRIP_CLASS: 1}),
        tile(1, 3, {}),
        tile(2, 1, {"aphid": 1, "mite": 1}),
        tile(2, 2, {"aphid": 2}),
        tile(2, 3, {DIRT_CLASS: 4}),
    ]
    select_keepers(grid)
    decisions = {(t["row"], t["col"]): t["decision"] for t in grid}
    check("thrip tile kept", decisions[(1, 2)], "KEEP")
    check("side neighbour blocked", decisions[(1, 1)], "MOVE")
    check("blank moved", decisions[(1, 3)], "BLANK_MOVE")
    check("diagonal neighbour blocked", decisions[(2, 1)], "MOVE")
    check("below neighbour blocked", decisions[(2, 2)], "MOVE")
    check("dirt-only diagonal blocked", decisions[(2, 3)], "MOVE")

    # dirt-only images are eligible to keep when unblocked (user decision)
    lone = [tile(1, 1, {DIRT_CLASS: 3})]
    select_keepers(lone)
    check("dirt-only eligible", lone[0]["decision"], "KEEP")

    # priority: diversity beats raw count when no thrip present, in a
    # non-adjacent pair so both could in principle be kept
    far = [tile(1, 1, {"aphid": 10}), tile(1, 5, {"aphid": 1, "mite": 1})]
    select_keepers(far)
    check("non-adjacent both kept", {t["decision"] for t in far}, {"KEEP"})
    near = [tile(1, 1, {"aphid": 10}), tile(1, 2, {"aphid": 1, "mite": 1})]
    select_keepers(near)
    check("diversity wins", near[1]["decision"], "KEEP")
    check("count loses to diversity", near[0]["decision"], "MOVE")

    # class-count extraction from a search-style record
    check("counts from classes dict",
          extract_class_counts({"annotations": {"classes": {"dirt": 2, "aphid": 1}}}),
          {"dirt": 2, "aphid": 1})
    check("counts from box list",
          extract_class_counts({"annotations": [{"label": "aphid"}, {"label": "aphid"}]}),
          {"aphid": 2})

    if failures:
        print(f"SELF-TEST FAILURES ({len(failures)}):")
        for msg in failures:
            print(f"  FAIL {msg}")
        sys.exit(1)
    print("All self-tests passed.")


if __name__ == "__main__":
    if SELF_TEST:
        run_self_tests()
    else:
        run()
