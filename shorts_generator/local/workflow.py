"""Step-by-step workflow: prepare -> (pick clips) -> review -> render.

Splits the one-shot pipeline into steps so a person (or Claude) can choose the
clips between transcription and rendering:

  prepare  video -> work dir with transcript.json, transcript.txt, frames/
  (pick)   someone writes work dir/highlights.json (see HIGHLIGHTS_FORMAT)
  review   local web page to watch each planned clip and approve / skip it
  render   cut + caption only the approved clips into work dir/clips/

Work dir layout:
  <work>/source.json        {"source": "/abs/path/to/video.mp4"}
  <work>/transcript.json    {duration, segments[{start, end, text, words[]}]}
  <work>/transcript.txt     one "[mm:ss.s] text" line per segment, easy to read
  <work>/frames/000001.jpg  a small frame every FRAME_INTERVAL seconds
  <work>/highlights.json    the planned clips (written by the picker)
  <work>/clips/             rendered shorts
"""
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

HIGHLIGHTS_FORMAT = """{
  "clips": [
    {
      "id": "c1",
      "title": "Driver Has The Worst Excuse",
      "start_time": 312.4,
      "end_time": 361.0,
      "hook_sentence": "...",
      "virality_reason": "...",
      "flags": ["minor visible 330-340s"],
      "status": "pending",          // pending | approved | skipped
      "note": ""                    // reviewer's note, e.g. "start 5s earlier"
    }
  ]
}"""

STATUSES = ("pending", "approved", "skipped")
FRAME_INTERVAL = 5.0


def default_work_dir(source: str) -> Path:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(source).stem).strip("_") or "video"
    return Path("work") / stem


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(max(0.0, seconds), 60)
    return f"{int(m):02d}:{s:04.1f}"


def _read_json(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def source_path(work: Path) -> str:
    return _read_json(work / "source.json")["source"]


def extract_frames(source: str, frames_dir: Path, interval: float = FRAME_INTERVAL) -> int:
    """One 320px-wide JPEG every `interval` seconds; frame n is at (n-1)*interval."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-i", source,
            "-vf", f"fps=1/{interval},scale=320:-2", "-q:v", "6",
            str(frames_dir / "%06d.jpg"),
        ],
        check=True,
    )
    return len(list(frames_dir.glob("*.jpg")))


def prepare(
    source: str,
    work: Optional[Path] = None,
    language: Optional[str] = None,
    frame_interval: float = FRAME_INTERVAL,
) -> Path:
    """Download/resolve the video, transcribe it, and extract preview frames."""
    from .downloader import download_youtube_local
    from .transcriber import transcribe_local

    src = os.path.abspath(download_youtube_local(source))
    work = Path(work) if work else default_work_dir(src)
    work.mkdir(parents=True, exist_ok=True)
    _write_json(work / "source.json", {"source": src, "frame_interval": frame_interval})

    transcript = transcribe_local(src, language=language)
    _write_json(work / "transcript.json", transcript)
    lines = [f"[{_fmt_ts(s['start'])}] {s['text']}" for s in transcript.get("segments", [])]
    (work / "transcript.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    n = extract_frames(src, work / "frames", frame_interval)
    print(f"[prepare] {len(lines)} transcript lines, {n} frames -> {work}", flush=True)
    print(f"[prepare] next: write {work / 'highlights.json'}, then run: python main.py review {work}", flush=True)
    return work


def load_highlights(work: Path) -> Dict:
    path = work / "highlights.json"
    if not path.exists():
        raise RuntimeError(f"{path} not found. Expected format:\n{HIGHLIGHTS_FORMAT}")
    data = _read_json(path)
    clips = data.get("clips")
    if not isinstance(clips, list):
        raise RuntimeError(f"{path} must have a top-level 'clips' list.")
    for i, c in enumerate(clips, 1):
        c.setdefault("id", f"c{i}")
        c.setdefault("status", "pending")
        c.setdefault("note", "")
        c.setdefault("flags", [])
        if float(c["end_time"]) <= float(c["start_time"]):
            raise RuntimeError(f"clip {c['id']}: end_time must be after start_time")
    return data


def save_highlights(work: Path, data: Dict) -> None:
    _write_json(work / "highlights.json", data)


def render(
    work: Path,
    aspect_ratio: str = "9:16",
    layout: str = "blur",
    captions: bool = True,
    title_overlay: bool = True,
    include_pending: bool = False,
) -> List[Dict]:
    """Render approved clips (plus pending ones if include_pending) into work/clips/."""
    from .clipper import crop_clip_local

    data = load_highlights(work)
    src = source_path(work)
    transcript = _read_json(work / "transcript.json")
    wanted = {"approved", "pending"} if include_pending else {"approved"}
    todo = [c for c in data["clips"] if c.get("status") in wanted]
    if not todo:
        print("[render] no approved clips. Approve some with: python main.py review " + str(work), flush=True)
        return []

    out_dir = work / "clips"
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for i, c in enumerate(todo, 1):
        out = out_dir / f"{c['id']}.mp4"
        print(f"[render] {i}/{len(todo)} {c['id']}: {c.get('title', '')}", flush=True)
        try:
            crop_clip_local(
                src, float(c["start_time"]), float(c["end_time"]), aspect_ratio, str(out),
                layout=layout, transcript=transcript, captions=captions,
                title=c.get("title") if title_overlay else None,
            )
            c["clip_path"] = str(out)
            c.pop("render_error", None)
        except Exception as e:  # keep going; record the failure on the clip
            print(f"[render] {c['id']} failed: {e}", flush=True)
            c["render_error"] = str(e)
        results.append(c)
    save_highlights(work, data)
    return results
