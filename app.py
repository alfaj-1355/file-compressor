import io
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from PIL import Image
import pymupdf as fitz

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

BASE = Path(__file__).parent
OUT = BASE / "outputs"
OUT.mkdir(exist_ok=True)

ALLOWED_IMAGES = {"jpg", "jpeg", "png", "webp", "bmp", "gif"}
ALLOWED_VIDEO = {
    "mp4", "mov", "mkv", "avi", "webm", "m4v", "3gp", "mpeg", "mpg", "ts", "mts", "m2ts"
}


def target_bytes(value, unit):
    value = max(float(value or 1), 1)
    return int(value * (1024 * 1024 if unit == "MB" else 1024))


def safe_range(form):
    unit = form.get("unit", "KB")
    minimum = target_bytes(form.get("min_target", form.get("target", "150")), unit)
    maximum = target_bytes(form.get("max_target", form.get("target", "2048")), unit)
    if minimum > maximum:
        minimum, maximum = maximum, minimum
    return minimum, maximum


def pad_to_minimum(blob, minimum, kind="generic"):
    """Increase file size without changing the decoded media as far as practical.

    For MP4/MOV, append valid ISO-BMFF `free` boxes. For other common media,
    trailing bytes are generally ignored by decoders; the original content is
    not re-encoded when only increasing size.
    """
    if len(blob) >= minimum:
        return blob
    need = minimum - len(blob)

    if kind in {"mp4", "mov", "m4v", "3gp"}:
        chunks = []
        while need >= 8:
            chunk = min(need, 1024 * 1024)
            chunk -= chunk % 8
            if chunk < 8:
                break
            chunks.append(chunk.to_bytes(4, "big") + b"free" + b"\0" * (chunk - 8))
            need -= chunk
        if need:
            chunks.append(b"\0" * need)
        return blob + b"".join(chunks)

    return blob + (b"\0" * need)


def image_compress(data, filename, minimum, maximum, ratio, quality, fmt):
    src = Image.open(io.BytesIO(data))
    src.load()
    if src.mode in ("P", "RGBA"):
        if fmt == "JPEG":
            rgba = src.convert("RGBA")
            bg = Image.new("RGB", rgba.size, "white")
            bg.paste(rgba, mask=rgba.getchannel("A"))
            src = bg
        else:
            src = src.convert("RGBA")
    elif fmt == "JPEG":
        src = src.convert("RGB")

    ratio = max(0.05, min(float(ratio), 1))
    src = src.resize(
        (max(1, round(src.width * ratio)), max(1, round(src.height * ratio))),
        Image.Resampling.LANCZOS,
    ) if ratio != 1 else src

    quality = max(10, min(int(quality), 100))
    current = quality
    best = None
    best_score = float("inf")

    # Search quality/dimensions for the smallest result that still reaches the
    # lower bound, while never intentionally exceeding the upper bound.
    work = src
    for _ in range(18):
        buf = io.BytesIO()
        save_kwargs = {}
        if fmt in ("JPEG", "WEBP"):
            save_kwargs = {"quality": current, "optimize": True}
        elif fmt == "PNG":
            save_kwargs = {"optimize": True, "compress_level": 9}
        work.save(buf, format=fmt, **save_kwargs)
        blob = buf.getvalue()
        size = len(blob)

        if minimum <= size <= maximum:
            best = blob
            break

        if size < minimum:
            best = blob
            # If below the requested range, increase quality first. If already
            # at max quality, gently increase dimensions.
            if fmt in ("JPEG", "WEBP") and current < 100:
                current = min(100, current + 5)
            else:
                nw = max(work.width + 1, int(work.width * 1.10))
                nh = max(work.height + 1, int(work.height * 1.10))
                if nw > src.width * 2.2 or nh > src.height * 2.2:
                    break
                work = work.resize((nw, nh), Image.Resampling.LANCZOS)
        else:
            if best is None or size < len(best) or abs(size - maximum) < abs(len(best) - maximum):
                best = blob
            if fmt == "PNG":
                nw = max(1, int(work.width * 0.88))
                nh = max(1, int(work.height * 0.88))
                work = work.resize((nw, nh), Image.Resampling.LANCZOS)
            else:
                current = max(10, current - 6)

    if best is None:
        best = blob

    if len(best) > maximum:
        # One final quality/resize pass for oversized output.
        for _ in range(10):
            if len(best) <= maximum:
                break
            buf = io.BytesIO()
            if fmt == "PNG":
                work = work.resize((max(1, int(work.width * .85)), max(1, int(work.height * .85))), Image.Resampling.LANCZOS)
                work.save(buf, format=fmt, optimize=True, compress_level=9)
            else:
                current = max(10, current - 7)
                work.save(buf, format=fmt, quality=current, optimize=True)
            best = buf.getvalue()

    # Size increaser: bring the result into the requested minimum if necessary.
    best = pad_to_minimum(best, minimum, fmt.lower())
    if len(best) > maximum:
        # Padding is never used beyond max; this branch means the source itself
        # could not be represented inside the selected range.
        raise ValueError("This range is too small for the selected image settings. Try a larger maximum size or lower quality/ratio.")

    ext = {"JPEG": ".jpg", "WEBP": ".webp", "PNG": ".png"}[fmt]
    return best, Path(filename).stem + "-optimized" + ext


def size_increase(data, filename, minimum, maximum):
    ext = Path(filename).suffix.lower().lstrip(".")
    if len(data) > maximum:
        raise ValueError("This file is already larger than the selected maximum size. Use a larger maximum or compression mode.")
    if ext in {"mp4", "mov", "m4v", "3gp"}:
        kind = ext
    else:
        kind = "generic"
    blob = pad_to_minimum(data, minimum, kind)
    if len(blob) > maximum:
        raise ValueError("The selected range is too narrow.")
    return blob, Path(filename).stem + "-size-adjusted" + Path(filename).suffix


def pdf_compress(data, filename, minimum, maximum):
    src = fitz.open(stream=data, filetype="pdf")
    original = len(data)

    out = io.BytesIO()
    src.save(
        out,
        garbage=4,
        clean=True,
        deflate=True,
        deflate_images=True,
        deflate_fonts=True,
        use_objstms=True,
    )
    result = out.getvalue()

    if len(result) > maximum or len(result) > original * 0.98:
        out_doc = fitz.open()
        ratio = max(0.40, min(1.35, (maximum / max(original, 1)) ** 0.35))
        zoom = 1.25 * ratio
        jpg_quality = 62
        for page in src:
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            img_bytes = pix.tobytes("jpeg", jpg_quality)
            new_page = out_doc.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(new_page.rect, stream=img_bytes)
        result = out_doc.tobytes(garbage=4, clean=True, deflate=True, use_objstms=True)
        out_doc.close()

    src.close()

    if len(result) > maximum:
        raise ValueError("The PDF could not be reduced into this range. Try a larger maximum size.")

    result = pad_to_minimum(result, minimum, "pdf")
    if len(result) > maximum:
        raise ValueError("The selected PDF range is too narrow for this file.")
    return result, Path(filename).stem + "-optimized.pdf"


def ffmpeg_path():
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def video_process(data, filename, minimum, maximum, quality_mode="balanced"):
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("FFmpeg is not installed on the server. Install FFmpeg or use the Render build in this project.")

    suffix = Path(filename).suffix.lower() or ".mp4"
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / ("input" + suffix)
        out = Path(td) / "output.mp4"
        inp.write_bytes(data)

        # CRF is quality-based. We iterate to fit the requested range.
        crf = 27 if quality_mode == "small" else 23
        if quality_mode == "quality":
            crf = 20

        best = None
        for _ in range(7):
            cmd = [
                ffmpeg, "-y", "-i", str(inp),
                "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(out),
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900)
            if proc.returncode != 0 or not out.exists():
                raise RuntimeError(proc.stderr.decode("utf-8", errors="ignore")[-800:])
            blob = out.read_bytes()
            best = blob
            if minimum <= len(blob) <= maximum:
                break
            if len(blob) > maximum:
                crf = min(40, crf + 3)
            else:
                crf = max(18, crf - 2)
            out.unlink(missing_ok=True)

        if best is None:
            raise RuntimeError("Video conversion failed.")
        if len(best) > maximum:
            raise ValueError("The video could not be reduced into this range. Try a larger maximum size.")

        # Video size increaser: add valid-ish MP4 free space when below minimum.
        best = pad_to_minimum(best, minimum, "mp4")
        if len(best) > maximum:
            raise ValueError("The selected video range is too narrow for this file.")

        return best, Path(filename).stem + "-optimized.mp4"


@app.route("/")
def index():
    return render_template("index.html")


@app.post("/compress")
def compress():
    files = request.files.getlist("files")
    mode = request.form.get("mode", "image")
    minimum, maximum = safe_range(request.form)

    if not files:
        return jsonify(error="No files selected."), 400

    outputs = []
    for f in files:
        if not f.filename:
            continue
        data = f.read()
        try:
            if mode == "increase":
                ext = Path(f.filename).suffix.lower().lstrip(".")
                if ext in ALLOWED_IMAGES or ext == "pdf" or ext in ALLOWED_VIDEO:
                    blob, name = size_increase(data, f.filename, minimum, maximum)
                else:
                    continue
            elif mode == "pdf":
                if not f.filename.lower().endswith(".pdf"):
                    continue
                blob, name = pdf_compress(data, f.filename, minimum, maximum)
            elif mode == "video":
                ext = Path(f.filename).suffix.lower().lstrip(".")
                if ext not in ALLOWED_VIDEO:
                    continue
                blob, name = video_process(data, f.filename, minimum, maximum, request.form.get("video_quality", "balanced"))
            else:
                ext = Path(f.filename).suffix.lower().lstrip(".")
                if ext not in ALLOWED_IMAGES:
                    continue
                blob, name = image_compress(
                    data,
                    f.filename,
                    minimum,
                    maximum,
                    request.form.get("ratio", "1"),
                    request.form.get("quality", "82"),
                    request.form.get("format", "JPEG"),
                )
        except Exception as exc:
            return jsonify(error=f"{f.filename}: {exc}"), 400

        token = uuid.uuid4().hex
        path = OUT / f"{token}_{name}"
        path.write_bytes(blob)
        outputs.append({
            "name": name,
            "size": len(blob),
            "original": len(data),
            "url": f"/download/{token}/{name}",
        })

    if not outputs:
        return jsonify(error="No supported files were supplied."), 400
    return jsonify(outputs=outputs)


@app.get("/download/<token>/<name>")
def download(token, name):
    matches = list(OUT.glob(f"{token}_*"))
    if not matches:
        return "File not found", 404
    return send_file(matches[0], as_attachment=True, download_name=name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
