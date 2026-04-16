"""
深空巡天参考图 — DeepSkySurveyMap
Interactive star map with deep-sky photography overlays.
Viewport-centered stereographic projection, WCS-accurate image positioning.
"""

import csv, io, math, os, sys
from PIL import Image as PILImage

import numpy as np

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QPointF
from PyQt6.QtGui import (QPixmap, QPainter, QColor, QBrush, QCursor, QPen,
                          QTransform, QPolygonF)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QProgressBar, QTextEdit, QSplitter,
    QSizePolicy, QMessageBox, QGroupBox,
)

from processor import (
    AstrometryClient, MetadataTracker, scan_reference_images,
    process_image, PREVIEW_SCALE, DETAIL_SCALE,
)
from constellations import CONSTELLATION_LINES

# ─── Paths ───
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REF_DIR = os.path.join(BASE_DIR, "ReferenceImage")
PROC_DIR = os.path.join(BASE_DIR, "ProcessedImage")
METADATA_PATH = os.path.join(BASE_DIR, "metadata.json")
STAR_CSV = os.path.join(BASE_DIR, "stars.csv")

# ─── Star map constants ───
MAG_LIMIT = 6.0
MAX_PIXMAP_DIM = 8000

# ═══════════════ Dark theme ═══════════════
DARK_STYLE = """
QMainWindow, QWidget {
    background-color: #1e1e2e; color: #cdd6f4;
    font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif; font-size: 13px;
}
QGroupBox {
    border: 1px solid #45475a; border-radius: 8px;
    margin-top: 14px; padding-top: 18px;
    font-weight: bold; color: #89b4fa;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
QPushButton {
    background-color: #89b4fa; color: #1e1e2e; border: none;
    border-radius: 6px; padding: 8px 20px; font-weight: bold;
}
QPushButton:hover { background-color: #b4d0fb; }
QPushButton:disabled { background-color: #45475a; color: #6c7086; }
QPushButton#processBtn { background-color: #a6e3a1; }
QPushButton#processBtn:hover { background-color: #b8f0b4; }
QProgressBar {
    border: none; border-radius: 4px; background-color: #313244; height: 8px;
}
QProgressBar::chunk { background-color: #89b4fa; border-radius: 4px; }
QTextEdit {
    background-color: #11111b; color: #a6adc8; border: 1px solid #45475a;
    border-radius: 6px; font-family: Consolas, monospace; font-size: 12px;
}
QLabel#statusLabel { color: #a6adc8; font-size: 12px; }
QLabel#infoLabel {
    background-color: #313244; border-radius: 6px; padding: 8px;
    color: #cdd6f4; font-size: 12px;
}
"""


# ═══════════════ Star data ═══════════════
def load_stars(csv_path, mag_limit):
    hip_map = {}
    ra_list, dec_list, mag_list = [], [], []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                mag = float(row["mag"])
                ra = float(row["ra"])
                dec = float(row["dec"])
            except (ValueError, KeyError):
                continue
            hip = row.get("hip", "").strip()
            if hip:
                hip_map[int(hip)] = {"ra": ra, "dec": dec, "mag": mag}
            if mag <= mag_limit:
                ra_list.append(ra)
                dec_list.append(dec)
                mag_list.append(mag)
    return hip_map, ra_list, dec_list, mag_list


# ═══════════════ Stereographic projection ═══════════════
def _stereo_fwd(ra_deg, dec_deg, ra0, dec0, sin0, cos0):
    """Forward stereographic: (RA°, Dec°) → (x, y, cos_c) on unit sphere."""
    ra = math.radians(ra_deg)
    dec = math.radians(dec_deg)
    dra = ra - ra0
    cos_dec = math.cos(dec)
    sin_dec = math.sin(dec)
    cos_dra = math.cos(dra)
    cos_c = sin0 * sin_dec + cos0 * cos_dec * cos_dra
    if cos_c < -0.9999:
        return 0.0, 0.0, cos_c
    k = 2.0 / (1.0 + cos_c)
    x = k * cos_dec * math.sin(dra)
    y = k * (cos0 * sin_dec - sin0 * cos_dec * cos_dra)
    return x, y, cos_c


def _stereo_fwd_np(ra_deg, dec_deg, ra0, dec0, sin0, cos0):
    """Vectorised forward stereographic for numpy arrays."""
    ra = np.radians(ra_deg)
    dec = np.radians(dec_deg)
    dra = ra - ra0
    cos_dec = np.cos(dec)
    sin_dec = np.sin(dec)
    cos_dra = np.cos(dra)
    cos_c = sin0 * sin_dec + cos0 * cos_dec * cos_dra
    k = 2.0 / np.maximum(1.0 + cos_c, 1e-6)
    x = k * cos_dec * np.sin(dra)
    y = k * (cos0 * sin_dec - sin0 * cos_dec * cos_dra)
    return x, y, cos_c


def _stereo_inv(x, y, ra0, dec0, sin0, cos0):
    """Inverse stereographic: projection-plane (x, y) → (RA°, Dec°)."""
    rho = math.sqrt(x * x + y * y)
    if rho < 1e-12:
        return math.degrees(ra0) % 360.0, math.degrees(dec0)
    c = 2.0 * math.atan2(rho, 2.0)
    sin_c = math.sin(c)
    cos_c = math.cos(c)
    dec = math.asin(max(-1.0, min(1.0, cos_c * sin0 + y * sin_c * cos0 / rho)))
    ra = ra0 + math.atan2(x * sin_c, rho * cos0 * cos_c - y * sin0 * sin_c)
    return math.degrees(ra) % 360.0, math.degrees(dec)


def prepare_sky_data(ra_list, dec_list, mag_list, hip_map):
    """Prepare numpy arrays for fast vectorised projection."""
    star_ra = np.array(ra_list) * 15.0
    star_dec = np.array(dec_list)
    star_mag = np.array(mag_list)
    t = np.maximum(0.0, (MAG_LIMIT - star_mag)) / (MAG_LIMIT + 1.0)
    star_rad = 1.0 + 5.0 * t ** 2.0
    star_alpha = np.clip((80 + 175 * (1.0 - star_mag / MAG_LIMIT)), 0, 255).astype(int)

    cra1, cdec1, cra2, cdec2 = [], [], [], []
    for _, hips in CONSTELLATION_LINES:
        for k in range(0, len(hips) - 1, 2):
            s1, s2 = hip_map.get(hips[k]), hip_map.get(hips[k + 1])
            if not s1 or not s2:
                continue
            cra1.append(s1["ra"] * 15.0)
            cdec1.append(s1["dec"])
            cra2.append(s2["ra"] * 15.0)
            cdec2.append(s2["dec"])
    star_data = (star_ra, star_dec, star_mag, star_rad, star_alpha)
    const_data = (np.array(cra1), np.array(cdec1), np.array(cra2), np.array(cdec2))
    return star_data, const_data


def safe_load_pixmap(path):
    """Load image as QPixmap; fall back to PIL + downscale if too large."""
    px = QPixmap(path)
    if not px.isNull():
        return px
    try:
        img = PILImage.open(path).convert("RGB")
        w, h = img.size
        if max(w, h) > MAX_PIXMAP_DIM:
            ratio = MAX_PIXMAP_DIM / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), PILImage.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        px = QPixmap()
        px.loadFromData(buf.getvalue())
        return px
    except Exception:
        return QPixmap()


# ═══════════════ Sky map widget ═══════════════
class SkyMapWidget(QWidget):
    """Interactive sky map using viewport-centered stereographic projection."""
    image_clicked = pyqtSignal(dict)
    mouse_moved = pyqtSignal(float, float)

    _MIN_FOV = 0.5
    _MAX_FOV = 180.0

    def __init__(self):
        super().__init__()
        self._center_ra = 0.0
        self._center_dec = 0.0
        self._fov = 120.0

        self._star_ra = np.empty(0)
        self._star_dec = np.empty(0)
        self._star_rad = np.empty(0)
        self._star_alpha = np.empty(0, dtype=int)
        self._cra1 = np.empty(0)
        self._cdec1 = np.empty(0)
        self._cra2 = np.empty(0)
        self._cdec2 = np.empty(0)

        self._overlays = []       # [(pixmap, metadata), ...]
        self._detail = None       # (pixmap, metadata) or None
        self._detail_name = None
        self._hover_name = None

        self._press_pos = None
        self._drag_last = None

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

    # ── data ──
    def set_sky_data(self, star_data, const_data):
        ra, dec, _mag, rad, alpha = star_data
        self._star_ra = ra
        self._star_dec = dec
        self._star_rad = rad
        self._star_alpha = alpha
        self._cra1, self._cdec1, self._cra2, self._cdec2 = const_data
        self.update()

    def add_overlay(self, pixmap, metadata):
        self._overlays.append((pixmap, metadata))
        self.update()

    def show_detail(self, pixmap, metadata):
        self._detail = (pixmap, metadata)
        self._detail_name = metadata["name"]
        self.update()

    def clear_overlays(self):
        self._overlays.clear()
        self._detail = None
        self._detail_name = None
        self.update()

    def clear_detail(self):
        self._detail = None
        self._detail_name = None
        self.update()

    # ── projection helpers ──
    def _scale(self):
        fov_rad = math.radians(max(self._fov, 0.01))
        return self.width() / (4.0 * math.tan(fov_rad / 4.0))

    def _proj(self):
        ra0 = math.radians(self._center_ra)
        dec0 = math.radians(self._center_dec)
        return ra0, dec0, math.sin(dec0), math.cos(dec0)

    def screen_to_sky(self, px, py):
        """Screen pixel → (RA°, Dec°)."""
        s = self._scale()
        cx, cy = self.width() / 2.0, self.height() / 2.0
        x = (cx - px) / s
        y = (cy - py) / s
        ra0, dec0, s0, c0 = self._proj()
        return _stereo_inv(x, y, ra0, dec0, s0, c0)

    # ── painting ──
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(8, 8, 18))

        sc = self._scale()
        ra0, dec0, s0, c0 = self._proj()
        cx, cy = w / 2.0, h / 2.0

        self._draw_graticule(p, sc, ra0, dec0, s0, c0, cx, cy, w, h)
        self._draw_constellations(p, sc, ra0, dec0, s0, c0, cx, cy, w, h)
        self._draw_stars(p, sc, ra0, dec0, s0, c0, cx, cy, w, h)
        self._draw_overlays(p, sc, ra0, dec0, s0, c0, cx, cy, w, h)

        # Hover filename label (top-left)
        if self._hover_name:
            font = p.font()
            font.setPixelSize(14)
            p.setFont(font)
            fm = p.fontMetrics()
            parts = self._hover_name.rsplit("_", 3)
            text = parts[0] if len(parts) == 4 else self._hover_name
            tw = fm.horizontalAdvance(text) + 12
            th = fm.height() + 6
            p.setBrush(QColor(0, 0, 0, 180))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(6, 6, tw, th, 4, 4)
            p.setPen(QColor(220, 220, 255))
            p.drawText(12, 6 + fm.ascent() + 3, text)

        # FoV indicator (bottom-left)
        p.setPen(QColor(100, 100, 160, 140))
        p.drawText(8, h - 8, f"FoV {self._fov:.1f}\u00b0")
        p.end()

    # ── graticule ──
    def _draw_graticule(self, p, sc, ra0, dec0, s0, c0, cx, cy, w, h):
        p.setPen(QPen(QColor(34, 34, 68, 150), 1))
        decs = np.arange(-90, 91, 2, dtype=float)
        for ra_h in range(0, 24, 2):
            ras = np.full_like(decs, ra_h * 15.0)
            x, y, cc = _stereo_fwd_np(ras, decs, ra0, dec0, s0, c0)
            sx = cx - x * sc
            sy = cy - y * sc
            vis = (cc > -0.2) & (sx > -w) & (sx < 2 * w) & (sy > -h) & (sy < 2 * h)
            self._draw_path(p, sx, sy, vis)
        ras = np.arange(0, 361, 2, dtype=float)
        for dec_d in range(-60, 90, 30):
            dcs = np.full_like(ras, float(dec_d))
            x, y, cc = _stereo_fwd_np(ras, dcs, ra0, dec0, s0, c0)
            sx = cx - x * sc
            sy = cy - y * sc
            vis = (cc > -0.2) & (sx > -w) & (sx < 2 * w) & (sy > -h) & (sy < 2 * h)
            self._draw_path(p, sx, sy, vis)
        # RA labels
        p.setPen(QColor(100, 100, 160, 180))
        for ra_h in range(0, 24, 2):
            x, y, cc = _stereo_fwd(ra_h * 15.0, 0.0, ra0, dec0, s0, c0)
            if cc > 0:
                px, py = cx - x * sc, cy - y * sc
                if 20 < px < w - 20 and 20 < py < h - 20:
                    p.drawText(QPointF(px + 4, py - 4), f"{ra_h}h")

    @staticmethod
    def _draw_path(p, sx, sy, vis):
        n = len(sx)
        i = 0
        while i < n:
            while i < n and not vis[i]:
                i += 1
            start = i
            while i < n and vis[i]:
                i += 1
            for j in range(start, i - 1):
                p.drawLine(QPointF(float(sx[j]), float(sy[j])),
                           QPointF(float(sx[j + 1]), float(sy[j + 1])))

    # ── constellations ──
    def _draw_constellations(self, p, sc, ra0, dec0, s0, c0, cx, cy, w, h):
        if len(self._cra1) == 0:
            return
        x1, y1, cc1 = _stereo_fwd_np(self._cra1, self._cdec1, ra0, dec0, s0, c0)
        x2, y2, cc2 = _stereo_fwd_np(self._cra2, self._cdec2, ra0, dec0, s0, c0)
        sx1, sy1 = cx - x1 * sc, cy - y1 * sc
        sx2, sy2 = cx - x2 * sc, cy - y2 * sc
        vis = (cc1 > -0.2) & (cc2 > -0.2)
        dist = np.sqrt((sx2 - sx1) ** 2 + (sy2 - sy1) ** 2)
        vis &= dist < max(w, h) * 1.5
        p.setPen(QPen(QColor(68, 136, 170, 130), 1))
        for i in np.where(vis)[0]:
            if ((0 <= sx1[i] <= w or 0 <= sx2[i] <= w) and
                    (0 <= sy1[i] <= h or 0 <= sy2[i] <= h)):
                p.drawLine(QPointF(float(sx1[i]), float(sy1[i])),
                           QPointF(float(sx2[i]), float(sy2[i])))

    # ── stars ──
    def _draw_stars(self, p, sc, ra0, dec0, s0, c0, cx, cy, w, h):
        if len(self._star_ra) == 0:
            return
        x, y, cc = _stereo_fwd_np(self._star_ra, self._star_dec, ra0, dec0, s0, c0)
        sx = cx - x * sc
        sy = cy - y * sc
        vis = (cc > -0.2) & (sx > -20) & (sx < w + 20) & (sy > -20) & (sy < h + 20)
        p.setPen(Qt.PenStyle.NoPen)
        for i in np.where(vis)[0]:
            a = int(self._star_alpha[i])
            r = float(self._star_rad[i])
            p.setBrush(QBrush(QColor(255, 255, 255, a)))
            p.drawEllipse(QPointF(float(sx[i]), float(sy[i])), r, r)

    # ── overlays ──
    def _draw_overlays(self, p, sc, ra0, dec0, s0, c0, cx, cy, w, h):
        items = list(self._overlays)
        if self._detail:
            items.append(self._detail)
        for pixmap, metadata in items:
            corners = metadata.get("corners")
            if not corners or len(corners) != 4:
                continue
            screen_pts = []
            ok = True
            for ra_d, dec_d in corners:
                x, y, cc = _stereo_fwd(ra_d, dec_d, ra0, dec0, s0, c0)
                if cc < -0.3:
                    ok = False
                    break
                screen_pts.append(QPointF(cx - x * sc, cy - y * sc))
            if not ok or len(screen_pts) != 4:
                continue
            pw, ph = pixmap.width(), pixmap.height()
            if pw == 0 or ph == 0:
                continue
            src = QPolygonF([QPointF(0, 0), QPointF(pw, 0),
                             QPointF(pw, ph), QPointF(0, ph)])
            dst = QPolygonF(screen_pts)
            xform = QTransform()
            if QTransform.quadToQuad(src, dst, xform):
                p.save()
                is_det = self._detail and metadata is self._detail[1]
                p.setOpacity(1.0 if is_det else 0.85)
                p.setTransform(xform)
                p.drawPixmap(0, 0, pixmap)
                p.restore()

    # ── interaction ──
    def wheelEvent(self, event):
        factor = 0.8 if event.angleDelta().y() > 0 else 1.25
        self._fov = max(self._MIN_FOV, min(self._MAX_FOV, self._fov * factor))
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.pos()
            self._drag_last = event.pos()
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._press_pos is not None:
                delta = event.pos() - self._press_pos
                if delta.manhattanLength() < 5:
                    self._handle_click(event.pos())
            self._press_pos = None
            self._drag_last = None
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

    def mouseMoveEvent(self, event):
        if self._drag_last is not None and event.buttons() & Qt.MouseButton.LeftButton:
            dx = event.pos().x() - self._drag_last.x()
            dy = event.pos().y() - self._drag_last.y()
            fov_per_px = self._fov / self.width()
            cos_dec = math.cos(math.radians(self._center_dec))
            self._center_ra = (self._center_ra + dx * fov_per_px / max(cos_dec, 0.05)) % 360.0
            self._center_dec = max(-90.0, min(90.0, self._center_dec + dy * fov_per_px))
            self._drag_last = event.pos()
            self.update()
        ra_d, dec_d = self.screen_to_sky(event.pos().x(), event.pos().y())
        self.mouse_moved.emit(ra_d / 15.0, dec_d)
        self._update_hover(event.pos())

    def _hit_overlay(self, pos):
        """Return metadata of the overlay under *pos*, or None."""
        sc = self._scale()
        ra0, dec0, s0, c0 = self._proj()
        cx, cy = self.width() / 2.0, self.height() / 2.0
        for _pixmap, metadata in reversed(self._overlays):
            corners = metadata.get("corners")
            if not corners or len(corners) != 4:
                continue
            pts = []
            ok = True
            for ra_d, dec_d in corners:
                x, y, cc = _stereo_fwd(ra_d, dec_d, ra0, dec0, s0, c0)
                if cc < -0.3:
                    ok = False
                    break
                pts.append(QPointF(cx - x * sc, cy - y * sc))
            if not ok:
                continue
            poly = QPolygonF(pts)
            if poly.containsPoint(QPointF(float(pos.x()), float(pos.y())),
                                  Qt.FillRule.WindingFill):
                return metadata
        return None

    def _update_hover(self, pos):
        md = self._hit_overlay(pos)
        name = md["name"] if md else None
        if name != self._hover_name:
            self._hover_name = name
            self.update()

    def _handle_click(self, pos):
        md = self._hit_overlay(pos)
        if md:
            self.image_clicked.emit(md)
        else:
            self.clear_detail()


# ═══════════════ Worker ═══════════════
class ProcessWorker(QThread):
    progress = pyqtSignal(str)
    image_done = pyqtSignal(dict)
    all_done = pyqtSignal(int, int)

    def __init__(self, file_paths, output_dir, tracker):
        super().__init__()
        self.file_paths = file_paths
        self.output_dir = output_dir
        self.tracker = tracker

    def run(self):
        client = AstrometryClient()
        ok, fail = 0, 0
        for path in self.file_paths:
            try:
                meta = process_image(
                    path, self.output_dir, client,
                    callback=lambda msg: self.progress.emit(msg),
                )
                if meta:
                    self.tracker.add(meta)
                    self.image_done.emit(meta)
                    ok += 1
            except Exception as e:
                self.progress.emit(f"[{os.path.basename(path)}] 失败: {e}")
                fail += 1
        self.all_done.emit(ok, fail)


# ═══════════════ Main window ═══════════════
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("深空巡天参考图 — DeepSkySurveyMap")
        self.setMinimumSize(1100, 700)
        self.resize(1280, 800)

        self.tracker = MetadataTracker(METADATA_PATH)
        self.worker = None
        self.hip_map, self.ra, self.dec, self.mag = load_stars(STAR_CSV, MAG_LIMIT)

        self._build_ui()
        self._render_bg()
        self._load_overlays()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(8)

        title = QLabel("深空巡天参考图  DeepSkySurveyMap")
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #cba6f7;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(title)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.map_view = SkyMapWidget()
        self.map_view.image_clicked.connect(self._on_click)
        self.map_view.mouse_moved.connect(self._on_move)
        splitter.addWidget(self.map_view)

        panel = QWidget()
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(8, 0, 0, 0)

        self.coord_label = QLabel("RA: --h --m   Dec: --°")
        self.coord_label.setObjectName("infoLabel")
        pl.addWidget(self.coord_label)

        self.info_label = QLabel("单击图片切换高低分辨率")
        self.info_label.setObjectName("infoLabel")
        self.info_label.setWordWrap(True)
        self.info_label.setMinimumHeight(140)
        pl.addWidget(self.info_label)

        grp = QGroupBox("图片处理")
        gl = QVBoxLayout(grp)
        self.btn_process = QPushButton("处理新图片")
        self.btn_process.setObjectName("processBtn")
        self.btn_process.clicked.connect(self._process)
        gl.addWidget(self.btn_process)
        self.pbar = QProgressBar()
        self.pbar.setRange(0, 0)
        self.pbar.setVisible(False)
        gl.addWidget(self.pbar)
        pl.addWidget(grp)

        n = len(self.tracker.get_all())
        self.stats_label = QLabel(f"已处理: {n} 张")
        self.stats_label.setObjectName("statusLabel")
        pl.addWidget(self.stats_label)

        log_grp = QGroupBox("日志")
        ll = QVBoxLayout(log_grp)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(220)
        ll.addWidget(self.log)
        pl.addWidget(log_grp)

        pl.addStretch()
        panel.setFixedWidth(320)
        splitter.addWidget(panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        root.addWidget(splitter, stretch=1)

        self.status = QLabel("")
        self.status.setObjectName("statusLabel")
        root.addWidget(self.status)

    def _log(self, msg):
        self.log.append(msg)
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _render_bg(self):
        self._log("准备星图数据...")
        star_data, const_data = prepare_sky_data(self.ra, self.dec, self.mag, self.hip_map)
        self.map_view.set_sky_data(star_data, const_data)
        self._log(f"星图就绪: {len(star_data[0])} 颗恒星, {len(const_data[0])} 星座线段")

    def _load_overlays(self):
        deleted, renamed, _ = self.tracker.sync_library(
            REF_DIR, PROC_DIR, callback=self._log
        )
        if deleted:
            self._log(f"启动同步: 删除 {len(deleted)} 张过期图片")
        if renamed:
            self._log(f"启动同步: 重命名 {len(renamed)} 张图片")

        count = 0
        for meta in self.tracker.get_all():
            path = os.path.join(PROC_DIR, "preview", f"{meta['name']}.webp")
            if os.path.exists(path):
                px = safe_load_pixmap(path)
                if not px.isNull():
                    self.map_view.add_overlay(px, meta)
                    count += 1
        if count:
            self._log(f"已加载 {count} 张叠加层")
        self.status.setText(f"{len(self.ra)} 颗恒星, {count} 张深空照片")

    def _on_move(self, ra_h, dec):
        h = int(ra_h)
        m = int((ra_h - h) * 60)
        s = (ra_h - h - m / 60.0) * 3600
        sign = "+" if dec >= 0 else "-"
        d = int(abs(dec))
        dm = int((abs(dec) - d) * 60)
        self.coord_label.setText(
            f"RA: {h:02d}h {m:02d}m {s:04.1f}s   Dec: {sign}{d:02d}\u00b0 {dm:02d}'"
        )

    @staticmethod
    def _parse_filename(name):
        """Parse 'Target_Fratio_Exposure_Author' naming convention."""
        parts = name.rsplit("_", 3)
        if len(parts) == 4:
            return {
                "target": parts[0],
                "telescope": parts[1],
                "exposure": parts[2] + "h",
                "author": parts[3],
            }
        return {"target": name, "telescope": "", "exposure": "", "author": ""}

    def _on_click(self, md):
        name = md["name"]
        if self.map_view._detail_name == name:
            self.map_view.clear_detail()
            self._log(f"[{name}] 切换回预览模式")
        else:
            detail_path = os.path.join(PROC_DIR, "detail", f"{name}.webp")
            if os.path.exists(detail_path):
                px = safe_load_pixmap(detail_path)
                if not px.isNull():
                    self.map_view.show_detail(px, md)
                    self._log(f"[{name}] 切换到高分辨率 (5\"/px)")
                else:
                    self._log(f"[{name}] 详情图加载失败")
            else:
                self._log(f"[{name}] 详情图不存在: {detail_path}")

        info = self._parse_filename(name)
        objs = ", ".join(md.get("objects_in_field", [])) or "无"
        lines = [f"目标: {info['target']}"]
        if info["telescope"]:
            lines.append(f"望远镜焦比: {info['telescope']}")
        if info["exposure"]:
            lines.append(f"单块曝光时间: {info['exposure']}")
        if info["author"]:
            lines.append(f"作者: {info['author']}")
        lines += [
            f"RA: {md['ra']:.4f}\u00b0  Dec: {md['dec']:.4f}\u00b0",
            f"视场: {md['field_w_deg']:.2f}\u00b0 x {md['field_h_deg']:.2f}\u00b0",
            f"像素比例: {md['pixscale']:.2f}\"/px",
            f"方位角: {md.get('orientation', 0):.1f}\u00b0",
            f"天体: {objs}",
        ]
        self.info_label.setText("\n".join(lines))

    def _process(self):
        if not os.path.isdir(REF_DIR):
            QMessageBox.warning(self, "提示", "ReferenceImage 文件夹不存在")
            return

        self._log("同步图库...")
        deleted, renamed, new_f = self.tracker.sync_library(
            REF_DIR, PROC_DIR, callback=self._log
        )

        if deleted or renamed:
            self.map_view.clear_overlays()
            count = 0
            for meta in self.tracker.get_all():
                path = os.path.join(PROC_DIR, "preview", f"{meta['name']}.webp")
                if os.path.exists(path):
                    px = safe_load_pixmap(path)
                    if not px.isNull():
                        self.map_view.add_overlay(px, meta)
                        count += 1
            self._log(f"图层已刷新: {count} 张")
            n = len(self.tracker.get_all())
            self.stats_label.setText(f"已处理: {n} 张")
            self.status.setText(f"{len(self.ra)} 颗恒星, {count} 张深空照片")

        if not new_f:
            summary_parts = []
            if deleted:
                summary_parts.append(f"删除 {len(deleted)} 张")
            if renamed:
                summary_parts.append(f"重命名 {len(renamed)} 张")
            if summary_parts:
                QMessageBox.information(
                    self, "同步完成",
                    "、".join(summary_parts) + "。\n没有需要处理的新图片。",
                )
            else:
                all_f = scan_reference_images(REF_DIR)
                QMessageBox.information(
                    self, "提示",
                    f"没有新图片。已处理 {len(all_f)} 张。\n请在 ReferenceImage 中放入新图片。",
                )
            return

        self._log(f"发现 {len(new_f)} 张新图片，开始处理...")
        self.btn_process.setEnabled(False)
        self.pbar.setVisible(True)

        self.worker = ProcessWorker(new_f, PROC_DIR, self.tracker)
        self.worker.progress.connect(self._log)
        self.worker.image_done.connect(self._on_done)
        self.worker.all_done.connect(self._on_all_done)
        self.worker.start()

    def _on_done(self, meta):
        path = os.path.join(PROC_DIR, "preview", f"{meta['name']}.webp")
        if os.path.exists(path):
            px = safe_load_pixmap(path)
            if not px.isNull():
                self.map_view.add_overlay(px, meta)
        n = len(self.tracker.get_all())
        self.stats_label.setText(f"已处理: {n} 张")

    def _on_all_done(self, ok, fail):
        self.btn_process.setEnabled(True)
        self.pbar.setVisible(False)
        self._log(f"完成: {ok} 成功, {fail} 失败")
        n = len(self.tracker.get_all())
        self.status.setText(f"{len(self.ra)} 颗恒星, {n} 张深空照片")


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_STYLE)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
