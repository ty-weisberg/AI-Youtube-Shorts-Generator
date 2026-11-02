"""CLI entry point.

One-shot (an LLM picks the clips):
    python main.py "/path/to/video.mp4" --num-clips 3

Step by step (a person or Claude picks the clips):
    python main.py prepare "/path/to/video.mp4"     # transcript + frames -> work/<video>/
    # write work/<video>/highlights.json
    python main.py review work/<video>              # approve / skip in the browser
    python main.py render work/<video>              # renders approved clips only
"""
import argparse
import json
import sys

# Windows uses 'charmap' by default, which can't encode Unicode characters
# like →. Reconfigure stdout/stderr to UTF-8 so output works on all platforms.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from shorts_generator import generate_shorts


STEP_COMMANDS = ("prepare", "review", "render")


def steps_main(argv) -> int:
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="main.py", description="Step-by-step shorts workflow")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="Transcribe a video and extract preview frames into a work dir")
    p.add_argument("source", help="Local video path, file:// URL, or YouTube URL")
    p.add_argument("--work", default=None, help="Work dir (default: work/<video name>)")
    p.add_argument("--language", default="en", help="Whisper language code (default: en)")
    p.add_argument("--frame-interval", type=float, default=5.0, help="Seconds between preview frames (default: 5)")

    r = sub.add_parser("review", help="Open the clip review page to approve / skip planned clips")
    r.add_argument("work", help="Work dir created by prepare")
    r.add_argument("--port", type=int, default=8765)
    r.add_argument("--no-browser", action="store_true", help="Don't open a browser tab")

    d = sub.add_parser("render", help="Render approved clips from highlights.json")
    d.add_argument("work", help="Work dir created by prepare")
    d.add_argument("--aspect-ratio", default="9:16")
    d.add_argument("--layout", choices=["blur", "crop"], default="blur")
    d.add_argument("--no-captions", action="store_true")
    d.add_argument("--no-title", action="store_true")
    d.add_argument("--include-pending", action="store_true", help="Also render clips not yet reviewed")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "prepare":
            from shorts_generator.local.workflow import prepare
            prepare(args.source, Path(args.work) if args.work else None, args.language, args.frame_interval)
        elif args.cmd == "review":
            from shorts_generator.local.review import serve
            serve(Path(args.work), port=args.port, open_browser=not args.no_browser)
        else:
            from shorts_generator.local.workflow import render
            done = render(
                Path(args.work), aspect_ratio=args.aspect_ratio, layout=args.layout,
                captions=not args.no_captions, title_overlay=not args.no_title,
                include_pending=args.include_pending,
            )
            for c in done:
                print(f"  {c['id']}: {c.get('clip_path') or 'FAILED: ' + c.get('render_error', '')}")
    except Exception as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in STEP_COMMANDS:
        return steps_main(sys.argv[1:])
    parser = argparse.ArgumentParser(
        description="AI YouTube Shorts Generator (one-shot). For the step-by-step workflow see: "
        "main.py prepare -h, main.py review -h, main.py render -h"
    )
    parser.add_argument("url", help="YouTube URL, file:// URL, or local file path")
    parser.add_argument(
        "--mode",
        choices=["api", "local"],
        default="local",
        help="local (default: remote URL, file://, or local path + faster-whisper + LLM provider + ffmpeg) or api (MuAPI).",
    )
    parser.add_argument(
        "--profile",
        choices=["bodycam", "general"],
        default="bodycam",
        help="Highlight prompt: bodycam (default, police body-worn camera footage) or general (podcasts/interviews).",
    )
    parser.add_argument(
        "--layout",
        choices=["blur", "crop"],
        default="blur",
        help="Local mode: blur (default, full frame over a blurred fill) or crop (face-tracking crop).",
    )
    parser.add_argument("--no-captions", action="store_true", help="Local mode: don't burn in captions")
    parser.add_argument("--no-title", action="store_true", help="Local mode: don't burn the clip title at the top")
    parser.add_argument("--num-clips", type=int, default=3, help="How many shorts to render (default: 3)")
    parser.add_argument("--aspect-ratio", default="9:16", help="Output aspect ratio (default: 9:16)")
    parser.add_argument("--format", default="720", help="Source download resolution: 360 / 480 / 720 / 1080 (default: 720)")
    parser.add_argument("--language", default=None, help="Force Whisper language code, e.g. 'en' (default: auto-detect)")
    parser.add_argument("--output-json", default=None, help="Write the full result JSON to this path")
    args = parser.parse_args()

    try:
        result = generate_shorts(
            youtube_url=args.url,
            num_clips=args.num_clips,
            aspect_ratio=args.aspect_ratio,
            download_format=args.format,
            language=args.language,
            mode=args.mode,
            profile=args.profile,
            layout=args.layout,
            captions=not args.no_captions,
            title_overlay=not args.no_title,
        )
    except Exception as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        return 1

    print("\n" + "=" * 72)
    print(f"Mode:          {result.get('mode', args.mode)}")
    print(f"Source video:  {result['source_video_url']}")
    print(f"Highlights:    {len(result['highlights'])} candidates → kept top {len(result['shorts'])}")
    print("=" * 72)
    for i, s in enumerate(result["shorts"], 1):
        print(f"\n#{i}  score={s.get('score')}  {s.get('start_time'):.1f}s → {s.get('end_time'):.1f}s")
        print(f"     title:  {s.get('title')}")
        print(f"     hook:   {s.get('hook_sentence')}")
        if s.get("clip_url"):
            print(f"     clip:   {s['clip_url']}")
        else:
            print(f"     clip:   FAILED ({s.get('error')})")

    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nFull JSON written to {args.output_json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
