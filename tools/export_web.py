"""
Export star data, metadata, and preview images for web deployment.

Usage:
    python tools/export_web.py [--out <output_dir>]

Default output: ../creat website/public/skymap
"""
import argparse
import csv
import json
import os
import shutil
import sys

# Resolve paths relative to project root (parent of tools/)
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_WEB_DIR = os.path.normpath(os.path.join(PROJECT_DIR, "..", "CreatWebsite", "public", "skymap"))


def main():
    parser = argparse.ArgumentParser(description="Export DeepSkySurveyMap data for web.")
    parser.add_argument("--out", default=DEFAULT_WEB_DIR, help="Web output directory")
    args = parser.parse_args()

    csv_path = os.path.join(PROJECT_DIR, "stars.csv")
    meta_path = os.path.join(PROJECT_DIR, "metadata.json")
    preview_dir = os.path.join(PROJECT_DIR, "ProcessedImage", "preview")
    out_dir = args.out

    os.makedirs(os.path.join(out_dir, "previews"), exist_ok=True)

    # ── Stars ──
    stars = []
    hip_map = {}
    with open(csv_path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                mag = float(row["mag"])
                ra = float(row["ra"])
                dec = float(row["dec"])
            except (ValueError, KeyError):
                continue
            hip = row.get("hip", "").strip()
            if hip:
                hip_map[int(hip)] = [ra * 15.0, dec]
            if mag <= 6.0:
                entry = [round(ra * 15, 4), round(dec, 4), round(mag, 2)]
                if hip:
                    entry.append(int(hip))
                stars.append(entry)

    stars_path = os.path.join(out_dir, "stars.json")
    with open(stars_path, "w") as f:
        json.dump(stars, f, separators=(",", ":"))
    print(f"stars.json: {len(stars)} stars, {os.path.getsize(stars_path) // 1024}KB")

    with open(os.path.join(out_dir, "hip_map.json"), "w") as f:
        json.dump({str(k): [round(v[0], 4), round(v[1], 4)] for k, v in hip_map.items()}, f, separators=(",", ":"))
    print(f"hip_map.json: {len(hip_map)} entries")

    # ── Metadata ──
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    web_meta = []
    for name, m in meta["images"].items():
        if "corners" not in m:
            print(f"  skip {name} (no WCS corners)")
            continue
        web_meta.append({
            "name": m["name"],
            "ra": m["ra"],
            "dec": m["dec"],
            "corners": m["corners"],
            "field_w_deg": m["field_w_deg"],
            "field_h_deg": m["field_h_deg"],
            "objects": m.get("objects_in_field", []),
            "pixscale": m["pixscale"],
            "orientation": m.get("orientation", 0),
        })

    with open(os.path.join(out_dir, "metadata.json"), "w") as f:
        json.dump(web_meta, f, separators=(",", ":"))
    print(f"metadata.json: {len(web_meta)} images")

    # ── Preview images ──
    for m in web_meta:
        src = os.path.join(preview_dir, f"{m['name']}.webp")
        dst = os.path.join(out_dir, "previews", f"{m['name']}.webp")
        if os.path.exists(src):
            shutil.copy2(src, dst)
            print(f"  copied {m['name']}.webp")
        else:
            print(f"  missing {m['name']}.webp")

    print("Done!")


if __name__ == "__main__":
    main()
