"""Local clipping: ffmpeg subclip + vertical reframe + optional burned-in captions.

Two layouts:
  * "blur" (default) — the full original frame, centred over a blurred,
    zoomed copy of itself. Nothing is cropped away, so on-screen overlays
    (bodycam timestamp / device ID) stay visible. One ffmpeg pass.
  * "crop" — cut with ffmpeg, then slide a window across the frame to keep
    the largest face centred (Haar cascade). Suits talking-head footage.

Both produce H.264/AAC mp4s and can burn in captions from the transcript.
"""
import json
import os
import subprocess
from typing import Dict, List, Optional, Tuple

from ..config import LOCAL_OUTPUT_DIR
from .captions import build_ass, caption_events, ffmpeg_filter_path

LAYOUTS = ("blur", "crop")


def _ratio(aspect_ratio: str) -> float:
    """Parse '9:16' → 9/16, '1:1' → 1.0."""
    try:
        w, h = aspect_ratio.split(":")
        return float(w) / float(h)
    except (ValueError, ZeroDivisionError):
        return 9.0 / 16.0


def _cut_subclip(source_path: str, start: float, end: float, out_path: str) -> str:
    """ffmpeg -ss start -t duration → re-encoded mp4 with audio.

    -ss goes before -i so ffmpeg seeks instead of decoding from the start of
    the file (matters for hour-long sources); re-encoding keeps it frame-exact.
    """
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}",
        "-i", source_path,
        "-t", f"{end - start:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    return out_path


def _reframe_vertical(
    in_path: str,
    out_path: str,
    aspect_ratio: str,
    ass_builder=None,
) -> str:
    """Crop the cut clip to the target aspect ratio, tracking faces if possible."""
    try:
        import cv2  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "opencv-python is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    target_ratio = _ratio(aspect_ratio)
    cap = cv2.VideoCapture(in_path)
    if not cap.isOpened():
        raise RuntimeError(f"could not open {in_path}")

    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

    # Compute the largest crop that fits inside the frame at the target ratio.
    if target_ratio < src_w / src_h:
        crop_h = src_h
        crop_w = int(crop_h * target_ratio)
    else:
        crop_w = src_w
        crop_h = int(crop_w / target_ratio)
    crop_w = max(2, crop_w - (crop_w % 2))
    crop_h = max(2, crop_h - (crop_h % 2))

    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    silent_path = out_path + ".silent.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(silent_path, fourcc, fps, (crop_w, crop_h))

    last_center: Optional[Tuple[int, int]] = None
    smoothing = 0.15  # how aggressively to chase a new face position
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
        if len(faces) > 0:
            # Pick the largest face — usually the speaker.
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            cx = x + w // 2
            cy = y + h // 2
            if last_center is None:
                last_center = (cx, cy)
            else:
                lx, ly = last_center
                last_center = (
                    int(lx + (cx - lx) * smoothing),
                    int(ly + (cy - ly) * smoothing),
                )
        if last_center is None:
            last_center = (src_w // 2, src_h // 2)

        cx, cy = last_center
        x0 = max(0, min(src_w - crop_w, cx - crop_w // 2))
        y0 = max(0, min(src_h - crop_h, cy - crop_h // 2))
        cropped = frame[y0:y0 + crop_h, x0:x0 + crop_w]
        writer.write(cropped)

    cap.release()
    writer.release()

    # Mux audio back on, re-encoding OpenCV's mp4v stream to H.264 and
    # burning in captions if requested.
    ass_path = None
    if ass_builder is not None:
        ass_path = out_path + ".ass"
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_builder(crop_w, crop_h, None, None))
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", silent_path,
        "-i", in_path,
    ]
    if ass_path:
        cmd += ["-vf", f"ass='{ffmpeg_filter_path(ass_path)}'"]
    cmd += [
        *_H264_ARGS,
        "-c:a", "aac", "-b:a", "128k",
        "-map", "0:v:0", "-map", "1:a:0?",
        "-shortest",
        out_path,
    ]
    try:
        subprocess.run(cmd, check=True)
    finally:
        os.remove(silent_path)
        if ass_path and os.path.exists(ass_path):
            os.remove(ass_path)
    return out_path


_H264_ARGS = [
    "-c:v", "libx264", "-preset", "medium", "-crf", "20",
    "-pix_fmt", "yuv420p", "-movflags", "+faststart",
]


def _probe_size(path: str) -> Tuple[int, int]:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:stream_side_data=rotation",
            "-of", "json", path,
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    stream = json.loads(out)["streams"][0]
    w, h = int(stream["width"]), int(stream["height"])
    rotation = 0
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    if abs(rotation) in (90, 270):
        w, h = h, w
    return w, h


def _output_size(aspect_ratio: str) -> Tuple[int, int]:
    """1080x1920 for 9:16, 1080x1080 for 1:1, 1920x1080 for 16:9, etc."""
    r = _ratio(aspect_ratio)
    if r <= 1:
        h = 1920 if r < 1 else 1080
        w = int(round(h * r))
    else:
        w = 1920
        h = int(round(w / r))
    return w - (w % 2), h - (h % 2)


def _render_blur(
    source_path: str,
    start: float,
    end: float,
    aspect_ratio: str,
    out_path: str,
    ass_builder=None,
) -> str:
    """Full frame centred over a blurred fill, in one ffmpeg pass."""
    out_w, out_h = _output_size(aspect_ratio)
    src_w, src_h = _probe_size(source_path)
    # Height of the sharp foreground once fitted into the output width.
    scale = min(out_w / src_w, out_h / src_h)
    fg_h = int(src_h * scale)
    band = max(0, (out_h - fg_h) // 2)  # empty space above and below

    ass_path = None
    if ass_builder is not None:
        ass_path = out_path + ".ass"
        # Captions sit just under the video, title just above it.
        text_px = int(round(70 * out_h / 1920))
        cap_mv = max(20, band - int(text_px * 2.6))
        title_mv = max(20, band - int(text_px * 2.6))
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_builder(out_w, out_h, cap_mv, title_mv))

    # Blur at 1/8 size then upscale: much cheaper than blurring at full res.
    bg_w, bg_h = max(2, out_w // 16 * 2), max(2, out_h // 16 * 2)
    graph = (
        f"[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={bg_w}:{bg_h}:force_original_aspect_ratio=increase,"
        f"crop={bg_w}:{bg_h},boxblur=10:2,eq=brightness=-0.12,"
        f"scale={out_w}:{out_h},setsar=1[bg];"
        f"[fgsrc]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,setsar=1[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )
    if ass_path:
        graph += f",ass='{ffmpeg_filter_path(ass_path)}'"
    graph += "[v]"

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}",
        "-i", source_path,
        "-t", f"{end - start:.3f}",
        "-filter_complex", graph,
        "-map", "[v]", "-map", "0:a:0?",
        *_H264_ARGS,
        "-c:a", "aac", "-b:a", "160k",
        out_path,
    ]
    try:
        subprocess.run(cmd, check=True)
    finally:
        if ass_path and os.path.exists(ass_path):
            os.remove(ass_path)
    return out_path


def _make_ass_builder(
    transcript: Optional[Dict],
    start: float,
    end: float,
    title: Optional[str],
    captions: bool,
):
    """Return fn(width, height, caption_margin_v, title_margin_v) -> ASS text, or None."""
    events = []
    if captions and transcript:
        events = caption_events(transcript.get("segments", []), start, end)
    if not events and not title:
        return None

    def builder(width, height, cap_mv, title_mv):
        return build_ass(
            events, width, height, end - start,
            title=title, caption_margin_v=cap_mv, title_margin_v=title_mv,
        )
    return builder


def crop_clip_local(
    source_path: str,
    start_time: float,
    end_time: float,
    aspect_ratio: str,
    out_path: str,
    layout: str = "blur",
    transcript: Optional[Dict] = None,
    captions: bool = True,
    title: Optional[str] = None,
) -> str:
    """Cut + reframe one highlight, returning the local mp4 path."""
    if layout not in LAYOUTS:
        raise ValueError(f"Unknown layout {layout!r}. Use one of {LAYOUTS}.")
    ass_builder = _make_ass_builder(transcript, start_time, end_time, title, captions)
    if layout == "blur":
        return _render_blur(source_path, start_time, end_time, aspect_ratio, out_path, ass_builder)

    cut_path = out_path + ".cut.mp4"
    try:
        _cut_subclip(source_path, start_time, end_time, cut_path)
        _reframe_vertical(cut_path, out_path, aspect_ratio, ass_builder)
    finally:
        if os.path.exists(cut_path):
            os.remove(cut_path)
    return out_path


def crop_highlights_local(
    source_path: str,
    highlights: List[Dict],
    aspect_ratio: str = "9:16",
    out_dir: Optional[str] = None,
    layout: str = "blur",
    transcript: Optional[Dict] = None,
    captions: bool = True,
    title_overlay: bool = True,
) -> List[Dict]:
    out_dir = out_dir or LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    results: List[Dict] = []
    for i, h in enumerate(highlights, 1):
        out_path = os.path.join(out_dir, f"short_{i:02d}.mp4")
        print(f"[clip/local] {i}/{len(highlights)}: {h.get('title', '(untitled)')}", flush=True)
        try:
            crop_clip_local(
                source_path,
                float(h["start_time"]),
                float(h["end_time"]),
                aspect_ratio,
                out_path,
                layout=layout,
                transcript=transcript,
                captions=captions,
                title=h.get("title") if title_overlay else None,
            )
            results.append({**h, "clip_url": out_path})
        except Exception as e:
            print(f"[clip/local] {i} failed: {e}", flush=True)
            results.append({**h, "clip_url": None, "error": str(e)})
    return results
