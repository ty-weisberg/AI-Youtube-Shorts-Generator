"""Burned-in captions for local mode.

Builds an ASS subtitle file for one clip from the Whisper transcript:
  * caption lines (bottom), split into short chunks so they read on a phone
  * an optional title bar (top), e.g. the highlight's title

When segments carry Whisper word timings (`words`), each caption chunk uses
the real start/end of its words. Otherwise long segments are split into chunks
of a few words and the segment's time is shared out by word count.
"""
from typing import Dict, List, Optional

MAX_WORDS_PER_CAPTION = 6


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\n", " ")
        .strip()
    )


def caption_events(
    segments: List[Dict],
    clip_start: float,
    clip_end: float,
    max_words: int = MAX_WORDS_PER_CAPTION,
) -> List[Dict]:
    """Return [{start, end, text}] relative to the clip start."""
    events: List[Dict] = []
    for seg in segments:
        s_start = float(seg["start"])
        s_end = float(seg["end"])
        if s_end <= clip_start or s_start >= clip_end:
            continue
        timed = seg.get("words") or []
        if timed:
            for i in range(0, len(timed), max_words):
                chunk = timed[i:i + max_words]
                c_start = max(float(chunk[0]["start"]), clip_start)
                c_end = min(float(chunk[-1]["end"]), clip_end)
                if c_end - c_start < 0.05:
                    continue
                events.append({
                    "start": c_start - clip_start,
                    "end": c_end - clip_start,
                    "text": " ".join(str(w["word"]).strip() for w in chunk),
                })
            continue
        words = str(seg.get("text", "")).split()
        if not words:
            continue
        seg_dur = max(0.01, s_end - s_start)
        per_word = seg_dur / len(words)
        for i in range(0, len(words), max_words):
            chunk = words[i:i + max_words]
            c_start = s_start + i * per_word
            c_end = s_start + (i + len(chunk)) * per_word
            # Clamp to the clip window.
            c_start = max(c_start, clip_start)
            c_end = min(c_end, clip_end)
            if c_end - c_start < 0.05:
                continue
            events.append({
                "start": c_start - clip_start,
                "end": c_end - clip_start,
                "text": " ".join(chunk),
            })
    return events


def build_ass(
    events: List[Dict],
    width: int,
    height: int,
    duration: float,
    title: Optional[str] = None,
    caption_margin_v: Optional[int] = None,
    title_margin_v: Optional[int] = None,
) -> str:
    """Render an ASS document sized to the output video."""
    scale = height / 1920.0
    cap_size = max(18, int(round(64 * scale)))
    title_size = max(18, int(round(72 * scale)))
    outline = max(2, int(round(5 * scale)))
    cap_mv = caption_margin_v if caption_margin_v is not None else int(260 * scale)
    title_mv = title_margin_v if title_margin_v is not None else int(260 * scale)
    side = int(60 * scale)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        # White text, thick black outline, bottom-centre.
        f"Style: Caption,DejaVu Sans,{cap_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},0,2,{side},{side},{cap_mv},1",
        # Yellow title, top-centre.
        f"Style: Title,DejaVu Sans,{title_size},&H0000E5FF,&H0000E5FF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},0,8,{side},{side},{title_mv},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if title:
        lines.append(
            f"Dialogue: 1,{_ass_time(0)},{_ass_time(duration)},Title,,0,0,0,,{_ass_escape(title).upper()}"
        )
    for ev in events:
        lines.append(
            f"Dialogue: 0,{_ass_time(ev['start'])},{_ass_time(ev['end'])},Caption,,0,0,0,,{_ass_escape(ev['text'])}"
        )
    return "\n".join(lines) + "\n"


def ffmpeg_filter_path(path: str) -> str:
    """Escape a path for use inside an ffmpeg filter argument (ass=...)."""
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
