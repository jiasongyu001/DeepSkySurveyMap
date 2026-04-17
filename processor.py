"""
DeepSkySurveyMap — Image processing pipeline.
Plate solve via Astrometry.net, generate preview (20"/px) and detail (5"/px),
compute accurate sky-corner coordinates via WCS.
"""

import hashlib
import io
import json
import math
import os
import time
from pathlib import Path

import numpy as np
from PIL import Image
Image.MAX_IMAGE_PIXELS = None  # astro images can be very large (>100 Mpx)
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── Constants ───
PREVIEW_SCALE = 20.0   # arcsec/pixel for preview layer
DETAIL_SCALE = 5.0     # arcsec/pixel for detail layer
WEBP_MAX_DIM = 16383   # WebP format pixel limit
QPIXMAP_MAX_DIM = 8000 # QPixmap reliable loading limit
UPLOAD_MAX_DIM = 2000   # max pixel dimension for plate-solve upload

ASTROMETRY_API_BASE = "https://nova.astrometry.net/api"
API_KEY = "ucwahopobeleagmr"

SUPPORTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".gif",
    ".fits", ".fit", ".fts",
    ".cr2", ".cr3", ".nef", ".arw", ".dng",
}


# ═══════════════════════════════════════════════════════════════════════
#  Astrometry.net API Client
# ═══════════════════════════════════════════════════════════════════════
def _prepare_upload(file_path):
    """Downscale image for faster upload. Returns (bytes, filename).
    Uses JPEG draft mode to avoid loading full-res into memory."""
    ext = Path(file_path).suffix.lower()
    if ext in {".fits", ".fit", ".fts"}:
        with open(file_path, "rb") as f:
            return f.read(), os.path.basename(file_path)
    try:
        img = Image.open(file_path)
        # JPEG draft mode: ask decoder to load at reduced resolution directly
        # This is ~8x faster and uses ~1/8 memory for very large images
        if ext in {".jpg", ".jpeg"} and max(img.size) > UPLOAD_MAX_DIM * 2:
            img.draft("RGB", (UPLOAD_MAX_DIM, UPLOAD_MAX_DIM))
            img.load()
        img.thumbnail((UPLOAD_MAX_DIM, UPLOAD_MAX_DIM), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return buf.getvalue(), Path(file_path).stem + ".jpg"
    except Exception:
        with open(file_path, "rb") as f:
            return f.read(), os.path.basename(file_path)


class AstrometryClient:
    """Minimal Astrometry.net API client for plate solving."""

    def __init__(self, api_key=API_KEY, base_url=ASTROMETRY_API_BASE):
        self.api_key = api_key
        self.base_url = base_url
        self.session_key = None
        self.http = requests.Session()
        self.http.trust_env = False
        retry = Retry(total=3, backoff_factor=2,
                      status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)

    def login(self):
        url = f"{self.base_url}/login"
        payload = {"request-json": json.dumps({"apikey": self.api_key})}
        resp = self.http.post(url, data=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("status") != "success":
            raise RuntimeError(f"Login failed: {result.get('errormessage')}")
        self.session_key = result["session"]

    def upload(self, file_path, callback=None, **kwargs):
        if not self.session_key:
            self.login()
        url = f"{self.base_url}/upload"
        args = {
            "session": self.session_key,
            "allow_commercial_use": "n",
            "allow_modifications": "n",
        }
        args.update(kwargs)
        upload_bytes, upload_name = _prepare_upload(file_path)
        if callback:
            callback(f"上传 {len(upload_bytes)/1024/1024:.1f} MB...")
        files = {"file": (upload_name, upload_bytes)}
        payload = {"request-json": json.dumps(args)}
        resp = self.http.post(url, data=payload, files=files, timeout=300)
        resp.raise_for_status()
        result = resp.json()
        if result.get("status") != "success":
            raise RuntimeError(f"Upload failed: {result.get('errormessage')}")
        return result["subid"]

    def get_submission_status(self, sub_id):
        resp = self.http.get(f"{self.base_url}/submissions/{sub_id}", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_job_status(self, job_id):
        resp = self.http.get(f"{self.base_url}/jobs/{job_id}", timeout=30)
        resp.raise_for_status()
        return resp.json().get("status", "unknown")

    def get_job_calibration(self, job_id):
        resp = self.http.get(f"{self.base_url}/jobs/{job_id}/calibration", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_job_info(self, job_id):
        resp = self.http.get(f"{self.base_url}/jobs/{job_id}/info", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_wcs_file(self, job_id):
        """Download WCS FITS header for accurate coordinate transforms."""
        url = f"https://nova.astrometry.net/wcs_file/{job_id}"
        resp = self.http.get(
            url, timeout=60,
            headers={"Referer": "https://nova.astrometry.net/api/login"},
        )
        resp.raise_for_status()
        return resp.content

    def solve(self, file_path, timeout=600, callback=None, **solve_kwargs):
        """Full plate-solving workflow: login → upload → wait → results.
        Extra kwargs (center_ra, center_dec, radius) are passed as hints."""
        if callback:
            callback("登录 Astrometry.net...")
        self.login()

        if callback:
            callback("上传图片...")
        sub_id = self.upload(file_path, callback=callback, **solve_kwargs)
        if callback:
            callback(f"上传成功 (Submission: {sub_id})，等待任务分配...")

        start = time.time()
        job_id = None
        while time.time() - start < timeout:
            status = self.get_submission_status(sub_id)
            jobs = [j for j in status.get("jobs", []) if j is not None]
            if jobs:
                job_id = jobs[0]
                break
            time.sleep(5)
            if callback:
                callback(f"等待任务分配... ({int(time.time() - start)}s)")
        if job_id is None:
            raise TimeoutError("任务分配超时")

        if callback:
            callback(f"Job {job_id}，正在解析...")

        while time.time() - start < timeout:
            status = self.get_job_status(job_id)
            if status == "success":
                break
            if status == "failure":
                raise RuntimeError("解析失败，无法识别星场")
            time.sleep(5)
            if callback:
                callback(f"解析中... ({int(time.time() - start)}s)")

        if callback:
            callback("获取结果...")
        cal = self.get_job_calibration(job_id)
        info = self.get_job_info(job_id)

        if callback:
            callback("下载 WCS 头信息...")
        wcs_bytes = self.get_wcs_file(job_id)

        return {
            "job_id": job_id,
            "ra": cal.get("ra"),
            "dec": cal.get("dec"),
            "orientation": cal.get("orientation", 0),
            "pixscale": cal.get("pixscale"),
            "radius": cal.get("radius"),
            "parity": cal.get("parity"),
            "objects_in_field": info.get("objects_in_field", []),
            "wcs_fits": wcs_bytes,
        }


# ═══════════════════════════════════════════════════════════════════════
#  Utility functions
# ═══════════════════════════════════════════════════════════════════════
def file_hash(path):
    """Compute MD5 hash for change detection."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def load_image(path, max_dim=None):
    """Load image as PIL RGB Image, handling FITS if needed.
    Returns (img, original_w, original_h).
    For JPEG, uses draft mode when image is much larger than max_dim
    to avoid loading full resolution into memory."""
    ext = Path(path).suffix.lower()
    if ext in {".fits", ".fit", ".fts"}:
        from astropy.io import fits

        with fits.open(path) as hdul:
            for hdu in hdul:
                if hdu.data is not None and hdu.data.ndim >= 2:
                    data = hdu.data.astype(float)
                    break
            else:
                raise ValueError("FITS 文件中没有 2D 图像数据")

        if data.ndim == 3:
            if data.shape[0] == 3:
                data = np.moveaxis(data, 0, -1)
            else:
                data = data[0]

        if data.ndim == 2:
            ow, oh = data.shape[1], data.shape[0]
            vmin, vmax = np.percentile(data[np.isfinite(data)], [1, 99])
            data = np.clip((data - vmin) / (vmax - vmin + 1e-10), 0, 1)
            data = (data * 255).astype(np.uint8)
            return Image.fromarray(data, mode="L").convert("RGB"), ow, oh
        else:
            ow, oh = data.shape[1], data.shape[0]
            for ch in range(data.shape[2]):
                c = data[:, :, ch]
                finite = c[np.isfinite(c)]
                if len(finite) == 0:
                    continue
                vmin, vmax = np.percentile(finite, [1, 99])
                data[:, :, ch] = np.clip((c - vmin) / (vmax - vmin + 1e-10), 0, 1)
            data = (data * 255).astype(np.uint8)
            return Image.fromarray(data, mode="RGB"), ow, oh
    else:
        img = Image.open(path)
        ow, oh = img.size  # original dimensions from header (no pixel decode yet)
        # JPEG draft: decode at reduced resolution when much larger than needed
        if max_dim and ext in {".jpg", ".jpeg"} and max(ow, oh) > max_dim * 2:
            img.draft("RGB", (max_dim, max_dim))
        img = img.convert("RGB")
        return img, ow, oh


def generate_scaled_image(img, original_pixscale, target_pixscale, max_dim=None):
    """Resize image to target pixel scale (no rotation — projection warp handles it)."""
    scale_factor = original_pixscale / target_pixscale
    new_w = max(1, int(img.width * scale_factor))
    new_h = max(1, int(img.height * scale_factor))
    if max_dim and max(new_w, new_h) > max_dim:
        ratio = max_dim / max(new_w, new_h)
        new_w = max(1, int(new_w * ratio))
        new_h = max(1, int(new_h * ratio))
    return img.resize((new_w, new_h), Image.LANCZOS)


def save_webp_capped(img, path, max_bytes, quality_start=90, quality_min=40):
    """Save image as WebP under max_bytes.
    Strategy: first reduce quality (90→40), then shrink resolution in 10% steps.
    Returns (quality, file_size, final_width, final_height)."""
    cur = img
    q = quality_start
    # Phase 1: reduce quality
    while q >= quality_min:
        buf = io.BytesIO()
        cur.save(buf, "WEBP", quality=q)
        if buf.tell() <= max_bytes:
            with open(path, "wb") as f:
                f.write(buf.getvalue())
            return q, buf.tell(), cur.width, cur.height
        q -= 5
    # Phase 2: shrink resolution at quality_min until under budget
    q = quality_min
    for _ in range(20):
        new_w = max(1, int(cur.width * 0.9))
        new_h = max(1, int(cur.height * 0.9))
        cur = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        cur.save(buf, "WEBP", quality=q)
        if buf.tell() <= max_bytes:
            with open(path, "wb") as f:
                f.write(buf.getvalue())
            return q, buf.tell(), cur.width, cur.height
    # Fallback
    buf = io.BytesIO()
    cur.save(buf, "WEBP", quality=quality_min)
    with open(path, "wb") as f:
        f.write(buf.getvalue())
    return quality_min, buf.tell(), cur.width, cur.height


def compute_corners_wcs(wcs_fits_bytes, img_w=None, img_h=None):
    """
    Compute RA/Dec of 4 image corners using the WCS solution from
    Astrometry.net.  Accurate at all declinations and field widths,
    including polynomial distortion terms.

    Astrometry.net WCS for non-FITS images uses IMAGE convention:
    pixel (1,1) = top-left, x increases right, y increases DOWN.
    IMAGEW/IMAGEH headers store the uploaded image dimensions.

    Returns [[ra,dec], ...] for TL, TR, BR, BL matching pixmap corners.
    """
    from astropy.io import fits
    from astropy.wcs import WCS

    hdu_list = fits.open(io.BytesIO(wcs_fits_bytes))
    header = hdu_list[0].header
    wcs = WCS(header)
    w = header.get("IMAGEW") or header.get("NAXIS1") or img_w
    h = header.get("IMAGEH") or header.get("NAXIS2") or img_h
    hdu_list.close()

    if not w or not h:
        raise ValueError("Cannot determine WCS image dimensions")

    pixel_corners = np.array([
        [0.5,     0.5],      # TL (image top-left)
        [w + 0.5, 0.5],      # TR (image top-right)
        [w + 0.5, h + 0.5],  # BR (image bottom-right)
        [0.5,     h + 0.5],  # BL (image bottom-left)
    ])

    sky = wcs.all_pix2world(pixel_corners, 1)  # origin=1
    return [[float(row[0]), float(row[1])] for row in sky]


# ═══════════════════════════════════════════════════════════════════════
#  Processing pipeline
# ═══════════════════════════════════════════════════════════════════════
def process_image(file_path, output_dir, client, callback=None):
    """
    Full pipeline for one image:
    1. Plate solve → get WCS calibration
    2. Generate preview and detail images
    3. Compute accurate sky-corner coordinates
    4. Return metadata dict
    """
    name = Path(file_path).stem
    preview_dir = os.path.join(output_dir, "preview")
    detail_dir = os.path.join(output_dir, "detail")
    wcs_dir = os.path.join(output_dir, "wcs")
    os.makedirs(preview_dir, exist_ok=True)
    os.makedirs(detail_dir, exist_ok=True)
    os.makedirs(wcs_dir, exist_ok=True)

    # Plate solve
    if callback:
        callback(f"[{name}] 开始解析...")
    result = client.solve(
        file_path,
        callback=lambda msg: callback(f"[{name}] {msg}") if callback else None,
    )

    if callback:
        callback(
            f"[{name}] 解析成功! RA={result['ra']:.4f}° Dec={result['dec']:.4f}°"
        )

    # Load image (draft mode for large JPEGs — only decode needed resolution)
    img, orig_w, orig_h = load_image(file_path, max_dim=QPIXMAP_MAX_DIM)
    pixscale = result["pixscale"]
    orientation = result.get("orientation", 0)
    # Field dimensions from ORIGINAL size (before any draft reduction)
    field_w = orig_w * pixscale / 3600.0
    field_h = orig_h * pixscale / 3600.0
    # Effective pixscale of the loaded (possibly draft-reduced) image
    eff_pixscale = pixscale * orig_w / img.width

    # Generate preview (20"/px) — capped at WebP limit, file ≤ 2MB
    preview = generate_scaled_image(img, eff_pixscale, PREVIEW_SCALE, max_dim=WEBP_MAX_DIM)
    preview_path = os.path.join(preview_dir, f"{name}.webp")
    pq, psz, pw, ph = save_webp_capped(preview, preview_path, max_bytes=2 * 1024 * 1024)

    # Generate detail (5"/px) — capped at QPixmap limit, file ≤ 5MB
    detail = generate_scaled_image(img, eff_pixscale, DETAIL_SCALE, max_dim=QPIXMAP_MAX_DIM)
    detail_path = os.path.join(detail_dir, f"{name}.webp")
    dq, dsz, dw, dh = save_webp_capped(detail, detail_path, max_bytes=5 * 1024 * 1024)

    # Save WCS FITS file for future reference
    wcs_path = os.path.join(wcs_dir, f"{name}.wcs")
    with open(wcs_path, "wb") as wf:
        wf.write(result["wcs_fits"])

    # Compute 4-corner sky coordinates using ORIGINAL dimensions
    corners = compute_corners_wcs(result["wcs_fits"], orig_w, orig_h)

    if callback:
        callback(
            f"[{name}] 预览 {pw}×{ph} q={pq} {psz/1024:.0f}KB | "
            f"详情 {dw}×{dh} q={dq} {dsz/1024:.0f}KB"
        )

    return {
        "filename": os.path.basename(file_path),
        "name": name,
        "hash": file_hash(file_path),
        "job_id": result["job_id"],
        "ra": result["ra"],
        "dec": result["dec"],
        "orientation": orientation,
        "pixscale": pixscale,
        "radius": result.get("radius"),
        "parity": result.get("parity", 0),
        "img_w": orig_w,
        "img_h": orig_h,
        "field_w_deg": field_w,
        "field_h_deg": field_h,
        "field_area_sq_deg": field_w * field_h,
        "corners": corners,
        "preview_w": pw,
        "preview_h": ph,
        "detail_w": dw,
        "detail_h": dh,
        "objects_in_field": result.get("objects_in_field", []),
        "processed_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ═══════════════════════════════════════════════════════════════════════
#  Metadata tracker
# ═══════════════════════════════════════════════════════════════════════
class MetadataTracker:
    """JSON-based tracker for processed image metadata."""

    def __init__(self, json_path):
        self.path = json_path
        self.data = {"images": {}}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def is_processed(self, file_path):
        """Check if file was already processed (by name + hash)."""
        name = Path(file_path).stem
        entry = self.data["images"].get(name)
        if entry is None:
            return False
        return entry.get("hash") == file_hash(file_path)

    def add(self, metadata):
        self.data["images"][metadata["name"]] = metadata
        self.save()

    def remove(self, name):
        self.data["images"].pop(name, None)

    def get_all(self):
        return list(self.data["images"].values())

    def get(self, name):
        return self.data["images"].get(name)

    def sync_library(self, ref_dir, output_dir, callback=None):
        """Synchronise metadata & processed files with ReferenceImage/.

        Handles three cases:
          - DELETE: file removed from ReferenceImage
          - RENAME: same content (MD5), different filename — no re-processing
          - UPDATE: same filename, different content — re-process

        Returns (deleted_names, renamed_pairs, new_files).
        """
        def _log(msg):
            if callback:
                callback(msg)

        ref_files = scan_reference_images(ref_dir)
        ref_name_hash = {}
        ref_hash_name = {}
        for fpath in ref_files:
            stem = Path(fpath).stem
            h = file_hash(fpath)
            ref_name_hash[stem] = h
            ref_hash_name[h] = stem

        meta_names = set(self.data["images"].keys())
        ref_names = set(ref_name_hash.keys())

        deleted_names = []
        renamed_pairs = []

        # Detect deletions and renames
        for old_name in sorted(meta_names - ref_names):
            entry = self.data["images"][old_name]
            old_hash = entry.get("hash", "")
            new_name = ref_hash_name.get(old_hash)
            if new_name and new_name not in meta_names:
                renamed_pairs.append((old_name, new_name))
            else:
                deleted_names.append(old_name)

        # Apply deletions
        for name in deleted_names:
            _log(f"[删除] {name}")
            self.remove(name)
            for sub in ("preview", "detail", "wcs"):
                ext = ".wcs" if sub == "wcs" else ".webp"
                p = os.path.join(output_dir, sub, f"{name}{ext}")
                if os.path.exists(p):
                    os.remove(p)

        # Apply renames
        for old_name, new_name in renamed_pairs:
            _log(f"[重命名] {old_name} → {new_name}")
            entry = self.data["images"].pop(old_name)
            entry["name"] = new_name
            entry["filename"] = next(
                (os.path.basename(f) for f in ref_files if Path(f).stem == new_name),
                entry.get("filename", ""),
            )
            self.data["images"][new_name] = entry
            for sub in ("preview", "detail", "wcs"):
                ext = ".wcs" if sub == "wcs" else ".webp"
                old_p = os.path.join(output_dir, sub, f"{old_name}{ext}")
                new_p = os.path.join(output_dir, sub, f"{new_name}{ext}")
                if os.path.exists(old_p):
                    os.makedirs(os.path.dirname(new_p), exist_ok=True)
                    os.rename(old_p, new_p)

        # Detect new / updated images
        processed_hashes = {e.get("hash") for e in self.data["images"].values()}
        new_files = []
        for fpath in ref_files:
            stem = Path(fpath).stem
            if stem in self.data["images"]:
                if self.data["images"][stem].get("hash") != ref_name_hash[stem]:
                    _log(f"[更新] {stem} (内容已变化)")
                    self.remove(stem)
                    for sub in ("preview", "detail", "wcs"):
                        ext = ".wcs" if sub == "wcs" else ".webp"
                        p = os.path.join(output_dir, sub, f"{stem}{ext}")
                        if os.path.exists(p):
                            os.remove(p)
                    new_files.append(fpath)
            else:
                if ref_name_hash[stem] not in processed_hashes:
                    new_files.append(fpath)

        if deleted_names or renamed_pairs:
            self.save()

        return deleted_names, renamed_pairs, new_files


def scan_reference_images(ref_dir):
    """Scan directory for supported image files."""
    results = []
    if not os.path.isdir(ref_dir):
        return results
    for f in os.listdir(ref_dir):
        ext = Path(f).suffix.lower()
        if ext in SUPPORTED_EXTENSIONS:
            results.append(os.path.join(ref_dir, f))
    return sorted(results)
