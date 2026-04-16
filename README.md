# DeepSkySurveyMap — 深空巡天参考图

交互式全天星图，叠加显示已拍摄深空照片，精确 WCS 定位。

## 功能

- **立体投影 (Stereographic)** — 无边界拖动漫游、滚轮缩放（0.5°–180°）
- **5070 颗恒星 + 88 星座连线** — HYG v4.2 星表
- **Astrometry.net plate solving** — 自动识别星场、获取精确 WCS 坐标
- **WCS 四角定位** — 极区和大视场照片精确叠加，无畸变
- **双分辨率** — 预览 20"/px（星图叠加），点击切换 5"/px 高清
- **图库同步** — 自动检测新增、删除、重命名（MD5 匹配免重处理）
- **悬停提示** — 鼠标悬停显示文件名

## 使用方法

```bash
pip install -r requirements.txt
```

1. 将天文照片放入 `ReferenceImage/` 文件夹
2. 运行 `python main.py`
3. 点击 **「处理新图片」** 按钮

## 文件结构

```
DeepSkySurveyMap/
├── main.py              # PyQt6 主程序
├── processor.py         # 图像处理管线（plate solve、WCS、缩放）
├── constellations.py    # 88 星座连线数据
├── stars.csv            # HYG v4.2 星表
├── requirements.txt     # Python 依赖
├── metadata.json        # 已处理图片元数据（自动生成）
├── ReferenceImage/      # 原始天文照片（用户放入）
├── ProcessedImage/      # 处理后图片（自动生成）
│   ├── preview/         #   20"/px WebP
│   ├── detail/          #   5"/px WebP
│   └── wcs/             #   WCS FITS 头信息
└── tools/
    └── export_web.py    # 导出数据供网页版使用
```

## 网页版部署

```bash
python tools/export_web.py --out ../creat\ website/public/skymap
```
