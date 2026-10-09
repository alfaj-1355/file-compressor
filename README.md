# Ready Compressor — Python / Flask

Responsive web compressor for **images, PDFs and videos** with a target **size range** such as `150 KB – 2 MB`.

### Features
- Image compression: JPG, PNG, WebP, BMP, GIF input
- PDF compression with raster fallback for image-heavy PDFs
- Video compression using FFmpeg (MP4 output)
- **Minimum + maximum target size**, e.g. 150 KB–2 MB
- **Size Increaser** mode for images, PDFs and videos
- Resize ratio and image quality controls
- Multiple file upload, drag & drop, mobile-friendly UI
- Runs on Android/iOS/Windows/macOS through a browser after deployment

## Run locally with Python 3.14

```powershell
& "C:\Program Files\Python314\python.exe" -m pip install -r requirements.txt
& "C:\Program Files\Python314\python.exe" app.py
```

Open `http://127.0.0.1:5000`.

### FFmpeg
Video compression needs FFmpeg. On Windows, install FFmpeg and make sure `ffmpeg` is on PATH. On Render, `render.yaml` installs FFmpeg during the build.

### Important size-range behavior
The compressor tries to produce a file between the selected minimum and maximum. When the compressed result is below the minimum, the app uses safe-ish padding so the file reaches the requested minimum without intentionally changing the decoded media. This is most useful for upload portals that reject files below a minimum size.

For very small ranges, the original content may not be able to fit inside the requested maximum. In that case the app returns an error telling you to increase the maximum or reduce quality/ratio.
