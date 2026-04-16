# DeepSkySurveyMap — 技术交接文档

> 最后更新: 2026-04-16  
> 版本: v1.0 (Production)

---

## 一、项目概述

**深空巡天参考图 (DeepSkySurveyMap)** 是一个基于 PyQt6 的桌面天文应用，用于：
1. 在交互式全天星图上叠加显示深空摄影照片
2. 通过 Astrometry.net 云端 plate solving 自动定位照片在天球上的精确位置
3. 使用 WCS（World Coordinate System）精确计算图像四角天球坐标
4. 管理图片库的增删改名

同时提供 Web 导出工具，数据可部署到 Next.js 网站。

---

## 二、文件结构

```
DeepSkySurveyMap/
├── main.py                  # 主程序入口 — PyQt6 GUI、星图渲染、交互逻辑
├── processor.py             # 图像处理管线 — API 客户端、WCS 计算、元数据管理
├── constellations.py        # 88 星座连线数据（HIP 编号对）
├── stars.csv                # HYG v4.2 星表（ra/dec/mag/hip 等字段，小时制 RA）
├── requirements.txt         # Python 依赖
├── metadata.json            # 已处理图片元数据（自动生成/维护）
├── DeepSkySurveyMap.spec    # PyInstaller 打包配置
├── README.md                # 用户使用说明
├── HANDOVER.md              # 本文档
│
├── ReferenceImage/          # 用户放入的原始天文照片
├── ProcessedImage/          # 自动生成的处理结果
│   ├── preview/             #   20″/px WebP 预览（星图叠加用）
│   ├── detail/              #   5″/px WebP 高清（点击查看用）
│   └── wcs/                 #   WCS FITS 头文件（精确坐标变换）
│
├── tools/
│   └── export_web.py        # Web 数据导出工具
│
├── dist/                    # PyInstaller 输出（EXE 发行版）
│   └── DeepSkySurveyMap/
│       ├── DeepSkySurveyMap.exe
│       └── ...（运行时依赖）
└── build/                   # PyInstaller 中间文件（可删除）
```

---

## 三、核心模块详解

### 3.1 `processor.py` — 图像处理管线

#### 常量

| 常量 | 值 | 说明 |
|------|---|------|
| `PREVIEW_SCALE` | 20.0 | 预览图像素比例（arcsec/px） |
| `DETAIL_SCALE` | 5.0 | 高清图像素比例（arcsec/px） |
| `WEBP_MAX_DIM` | 16383 | WebP 格式单边最大像素 |
| `QPIXMAP_MAX_DIM` | 8000 | QPixmap 可靠加载的最大像素 |
| `UPLOAD_MAX_DIM` | 2000 | 上传 plate solve 时缩放到的最大尺寸 |
| `API_KEY` | `ucwahopobeleagmr` | Astrometry.net API Key |
| `SUPPORTED_EXTENSIONS` | jpg/png/tiff/fits/cr2/nef/arw/dng 等 | 支持的图片格式 |

#### `class AstrometryClient`

Astrometry.net REST API 封装，自带 retry 和 proxy 忽略。

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `login()` | — | None | 获取 session key |
| `upload(file_path, **kwargs)` | 文件路径 + 可选 hints | sub_id: int | 上传图片，自动降采样 |
| `get_submission_status(sub_id)` | submission ID | dict | 查询提交状态 |
| `get_job_status(job_id)` | job ID | str | `"success"` / `"failure"` / `"solving"` |
| `get_job_calibration(job_id)` | job ID | dict | 返回 ra/dec/pixscale/orientation/parity |
| `get_job_info(job_id)` | job ID | dict | 返回 objects_in_field 等 |
| `get_wcs_file(job_id)` | job ID | bytes | 下载 WCS FITS 原始字节 |
| `solve(file_path, timeout, callback, **solve_kwargs)` | 文件路径 | dict | **完整工作流**: login→upload→poll→results→WCS |

`solve()` 返回字典:
```python
{
    "job_id": int,
    "ra": float,          # 中心 RA（度）
    "dec": float,         # 中心 Dec（度）
    "orientation": float, # 旋转角（度，E of N）
    "pixscale": float,    # 像素比例（arcsec/px）
    "radius": float,      # 视场半径（度）
    "parity": float,      # 奇偶性（镜像检测）
    "objects_in_field": list[str],
    "wcs_fits": bytes,    # WCS FITS 头原始数据
}
```

#### 工具函数

| 函数 | 说明 |
|------|------|
| `file_hash(path)` | MD5 哈希，用于变更检测 |
| `load_image(path)` | 加载任意格式图片为 PIL RGB Image（含 FITS 支持） |
| `generate_scaled_image(img, orig_scale, target_scale, max_dim)` | 按像素比例缩放，可选最大尺寸限制 |
| `compute_corners_wcs(wcs_fits_bytes, img_w, img_h)` | **核心**: WCS 精确四角坐标计算 |
| `process_image(file_path, output_dir, client, callback)` | 完整处理管线（解析→缩放→保存→计算坐标） |
| `scan_reference_images(ref_dir)` | 扫描支持格式的图片文件 |

#### `compute_corners_wcs` 关键细节

```
Astrometry.net WCS 坐标约定（非 FITS 图像）:
- 像素 (1,1) = 图像左上角
- x 向右递增，y 向下递增（IMAGE 约定，非 FITS y-up 约定）
- IMAGEW/IMAGEH 存储上传图片的实际尺寸
- NAXIS1/NAXIS2 为 0（不可用于尺寸判断）

四角像素坐标（FITS origin=1）:
  TL = (0.5,     0.5)
  TR = (W+0.5,   0.5)
  BR = (W+0.5,   H+0.5)
  BL = (0.5,     H+0.5)

返回顺序: [TL, TR, BR, BL] 对应 pixmap 的 (0,0), (W,0), (W,H), (0,H)
```

#### `class MetadataTracker`

JSON 文件管理器，追踪已处理图片。

| 方法 | 说明 |
|------|------|
| `load()` / `save()` | 读写 `metadata.json` |
| `is_processed(file_path)` | 按 name + MD5 判断是否已处理 |
| `add(metadata)` / `remove(name)` / `get(name)` / `get_all()` | CRUD |
| `sync_library(ref_dir, output_dir, callback)` | **核心**: 图库同步 |

#### `sync_library` 同步逻辑

```
输入: ReferenceImage/ 目录 vs metadata.json
输出: (deleted_names, renamed_pairs, new_files)

1. 扫描 ReferenceImage，计算每个文件的 MD5
2. 对比 metadata 中的条目:
   - metadata 有、ref 没有 → 检查 hash 是否匹配 ref 中的其他文件
     - hash 匹配新文件名 → RENAME（不重新处理）
     - hash 无匹配 → DELETE（删除 preview/detail/wcs）
3. 对比 ref 中的文件:
   - 同名但 hash 不同 → UPDATE（删旧文件，加入新处理队列）
   - ref 有、metadata 没有、hash 也没匹配 → NEW（加入处理队列）
```

### 3.2 `main.py` — GUI 主程序

#### 投影系统

使用**视口中心立体投影 (Stereographic Projection)**，数学上等价于 Aladin/Telescopius。

| 函数 | 类型 | 说明 |
|------|------|------|
| `_stereo_fwd(ra°, dec°, ra0, dec0, sin0, cos0)` | 标量 | 前向投影 → (x, y, cos_c) |
| `_stereo_fwd_np(ra°, dec°, ...)` | numpy 向量化 | 批量前向投影（星图渲染用） |
| `_stereo_inv(x, y, ra0, dec0, sin0, cos0)` | 标量 | 反向投影 → (RA°, Dec°) |

投影公式:
```
cos_c = sin(dec0)·sin(dec) + cos(dec0)·cos(dec)·cos(ra-ra0)
k = 2 / (1 + cos_c)
x = k · cos(dec) · sin(ra-ra0)
y = k · (cos(dec0)·sin(dec) - sin(dec0)·cos(dec)·cos(ra-ra0))

屏幕坐标: screen_x = cx - x * scale, screen_y = cy - y * scale
scale = width / (4 · tan(fov/4))
```

特性:
- 无边界，可无限拖动（RA 360° wrap, Dec ±90° clamp）
- FOV 范围 0.5° ~ 180°
- cos_c < -0.3 的点在背面，不渲染

#### `class SkyMapWidget(QWidget)`

核心渲染组件，所有内容在 `paintEvent()` 中实时绘制。

| 方法 | 说明 |
|------|------|
| `set_sky_data(star_data, const_data)` | 设置星图数据（numpy 数组） |
| `add_overlay(pixmap, metadata)` | 添加图片叠加层 |
| `clear_overlays()` | 清空所有叠加层 |
| `show_detail(pixmap, metadata)` / `clear_detail()` | 高清图切换 |
| `screen_to_sky(px, py)` | 屏幕坐标 → 天球坐标 |

绘制层级（从底到顶）:
1. `_draw_graticule` — 经纬网格（RA 每 2h，Dec 每 30°）+ RA 标签
2. `_draw_constellations` — 星座连线（HIP 编号查表）
3. `_draw_stars` — 恒星（亮度→大小/透明度映射）
4. `_draw_overlays` — 图片叠加（`QTransform.quadToQuad` 四角变换）
5. 悬停文件名标签 + FoV 指示器

交互:
- **拖动**: `mouseMoveEvent` 增量更新 center_ra/center_dec
- **缩放**: `wheelEvent` 缩放 _fov（×0.8 / ×1.25）
- **点击**: `_handle_click` → `_hit_overlay` 碰撞检测 → `image_clicked` 信号
- **悬停**: `_update_hover` → 更新 `_hover_name` → paintEvent 显示标签

图片叠加实现:
```
1. metadata["corners"] 四角 [TL, TR, BR, BL] 各为 [ra°, dec°]
2. 每个角通过 _stereo_fwd 投影到屏幕坐标
3. QTransform.quadToQuad(src_polygon, dst_polygon) 计算仿射+透视变换
4. QPainter.setTransform + drawPixmap 绘制
```

#### `class ProcessWorker(QThread)`

后台线程，逐个处理图片，通过信号通知 GUI:

| 信号 | 参数 | 说明 |
|------|------|------|
| `progress` | str | 日志消息 |
| `image_done` | dict | 单张处理完成，传回 metadata |
| `all_done` | int, int | (成功数, 失败数) |

#### `class MainWindow(QMainWindow)`

| 方法 | 说明 |
|------|------|
| `_build_ui()` | 构建界面: 左侧星图 + 右侧面板（坐标/信息/按钮/日志） |
| `_render_bg()` | 加载星表，准备 numpy 投影数组 |
| `_load_overlays()` | 启动时同步图库 + 加载已有叠加层 |
| `_process()` | 点击"处理新图片"→ sync_library → 启动 worker |
| `_on_click(md)` | 点击图片: 切换 preview ↔ detail |
| `_on_done(meta)` | worker 回调: 添加新叠加层到星图 |
| `_on_all_done(ok, fail)` | worker 完成: 更新状态栏 |

#### `safe_load_pixmap(path)`

QPixmap 加载大图时可能失败（>32K 像素或内存限制），此函数:
1. 先尝试 QPixmap 直接加载
2. 失败则用 PIL 加载→缩放到 8000px→转 PNG bytes→loadFromData

### 3.3 `constellations.py`

纯数据文件，来自 Stellarium (GPL)。格式:
```python
CONSTELLATION_LINES = [
    ("Aql", [hip1, hip2, hip3, hip4, ...]),  # 成对连线
    ...
]
```
88 个星座，HIP 编号成对排列（hip1→hip2 一条线段，hip3→hip4 一条线段）。

### 3.4 `stars.csv`

HYG v4.2 星表，关键字段:
- `ra`: 赤经（**小时制**, 需 ×15 转度）
- `dec`: 赤纬（度）
- `mag`: 视星等
- `hip`: Hipparcos 编号（星座连线查表用）

程序加载时过滤 mag ≤ 6.0，得到约 5070 颗恒星。

### 3.5 `tools/export_web.py`

导出数据供 Next.js 网站使用:
- `stars.json`: 紧凑数组 `[[ra°, dec°, mag, hip?], ...]`
- `hip_map.json`: `{"hip_id": [ra°, dec°], ...}`
- `metadata.json`: 图片元数据（corners/field/objects/pixscale/orientation）
- `previews/*.webp`: 预览图复制

默认输出到 `../CreatWebsite/public/skymap/`。

---

## 四、metadata.json 数据结构

```json
{
  "images": {
    "<image_name>": {
      "filename": "原始文件名.jpg",
      "name": "不含扩展名",
      "hash": "MD5 十六进制",
      "job_id": 15637416,
      "ra": 116.6918,
      "dec": 19.5599,
      "orientation": 0.11,
      "pixscale": 24.38,
      "radius": 7.04,
      "parity": 1.0,
      "img_w": 4524,
      "img_h": 15824,
      "field_w_deg": 30.64,
      "field_h_deg": 107.19,
      "field_area_sq_deg": 3284.66,
      "corners": [
        [ra_TL, dec_TL],
        [ra_TR, dec_TR],
        [ra_BR, dec_BR],
        [ra_BL, dec_BL]
      ],
      "preview_w": 4683,
      "preview_h": 16383,
      "detail_w": 2287,
      "detail_h": 8000,
      "objects_in_field": ["NGC xxx", "..."],
      "processed_time": "2026-04-16 11:22:02"
    }
  }
}
```

---

## 五、已知约束和注意事项

### 5.1 WebP 像素限制
WebP 单边最大 16383px。超大视场图片（如 Silk Nebula 107°）的 preview 会被自动 cap 到此限制。

### 5.2 QPixmap 内存限制
Qt 默认分配限制 256MB。超过此限制的图片通过 `safe_load_pixmap` 的 PIL fallback 自动缩放。

### 5.3 WCS 坐标约定
Astrometry.net 对非 FITS 图片使用 **IMAGE 约定**（y 向下），而非 FITS 标准约定（y 向上）。
`compute_corners_wcs` 中的像素角点必须使用 y-down 排列，否则图片会上下翻转。

### 5.4 Astrometry.net API
- 免费版有排队时间（通常 30-120 秒）
- API Key: `ucwahopobeleagmr`
- WCS 下载需要 `Referer` 头: `https://nova.astrometry.net/api/login`
- 上传前自动降采样到 2000px（plate solve 不需要全分辨率）

### 5.5 图库同步
- 重命名检测基于 MD5 hash 匹配，如果两张不同照片碰巧 hash 相同会误判（极不可能）
- sync 在程序启动时和点击"处理新图片"时都会执行

---

## 六、EXE 发行版

使用 PyInstaller 打包:
```bash
pyinstaller DeepSkySurveyMap.spec --noconfirm
```

输出在 `dist/DeepSkySurveyMap/`，需要整个文件夹分发。用户需要:
1. 复制整个 `dist/DeepSkySurveyMap/` 文件夹到目标电脑
2. 在文件夹旁边创建 `ReferenceImage/` 和 `stars.csv`
3. 双击 `DeepSkySurveyMap.exe` 运行

**注意**: `stars.csv` 和 `constellations.py` 已通过 spec 的 `datas` 打包进 dist。
运行时 `ReferenceImage/`、`ProcessedImage/`、`metadata.json` 相对于 EXE 所在目录。

---

## 七、Web 版接口

网站项目路径: `d:\AI\windsurf_workspace\CreatWebsite`

相关文件:
- `src/app/projects/sky-map/page.tsx` — 页面入口
- `src/app/projects/sky-map/SkyMapWrapper.tsx` — 客户端加载包装器
- `src/components/sky-map/SkyMapCanvas.tsx` — Canvas 渲染组件
- `src/components/sky-map/projection.ts` — 投影函数（TypeScript 版）
- `src/components/sky-map/constellations.ts` — 星座数据
- `src/lib/projects.ts` — 项目配置（slug: "sky-map", category: "survey"）
- `public/skymap/` — 静态数据目录

Web 版使用 HTML Canvas + 三角剖分仿射变换（而非 QTransform.quadToQuad）实现图片叠加。
投影数学与桌面版完全一致。

---

## 八、开发历程

1. **初版**: 等距圆柱投影静态星图 + matplotlib 渲染
2. **重写投影**: 改为视口中心立体投影，QPainter 实时渲染，无边界拖动
3. **Plate solving**: 集成 Astrometry.net API，自动识别星场
4. **WCS 精确定位**: 从 API 下载 WCS FITS，用 astropy.wcs 计算精确四角坐标
5. **修复坐标约定**: 发现 Astrometry.net 对非 FITS 图片使用 y-down 约定，修正像素角点
6. **修复尺寸读取**: 发现 NAXIS1/NAXIS2 为 0，改用 IMAGEW/IMAGEH
7. **WebP 限制修复**: 大视场图片 preview 超过 16383px 限制，加入 max_dim cap
8. **图库同步**: 实现增删改名自动检测（MD5 匹配免重处理）
9. **Web 部署**: 移植到 Next.js + Canvas，部署到 Vercel/Cloudflare
10. **生产版**: 清理 debug 文件，整合到 DeepSkySurveyMap 正式目录
