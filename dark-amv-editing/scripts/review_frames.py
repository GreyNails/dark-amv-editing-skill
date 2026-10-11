#!/usr/bin/env python3
"""Extract review frames from a rendered video; requires ffmpeg and ffprobe."""
import argparse
import html
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys


def run(command):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr.strip()[-1800:] or "Media command failed")
    return result.stdout


def finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Time and rate values must be finite numbers")
    return float(value)


def build_samples(data, samples, include_lyrics):
    """Return integer timeline-frame targets, not source-video frame numbers."""
    fps = finite_number(data["fps"])
    if fps <= 0:
        raise ValueError("fps must be positive")
    entries = []
    shots = data.get("shots", [])
    if not shots:
        raise ValueError("Timeline has no shots")
    for shot in shots:
        a, b = shot["start_frame"], shot["end_frame"]
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (a, b)) or not 0 <= a < b:
            raise ValueError("Shot frame ranges must be nonnegative integer intervals")
        choices = {"start": a, "mid": a + (b - a) // 2, "end": b - 1}
        for kind in samples:
            entries.append((choices[kind], str(shot["id"]) + ":" + kind))
    if include_lyrics:
        for cue in data.get("lyrics", []):
            start, end = finite_number(cue["start"]), finite_number(cue["end"])
            if not 0 <= start < end:
                raise ValueError("Lyric intervals must have positive duration")
            a = math.ceil(start * fps - 1e-8)
            b = math.ceil(end * fps - 1e-8)
            if a >= b:
                continue
            label = str(cue["id"])
            entries += [(a, label + ":entry"), (a + (b - a) // 2, label + ":mid"),
                        (b - 1, label + ":exit")]
            landing = cue.get("entry_landing_frame")
            if landing is not None:
                if isinstance(landing, bool) or not isinstance(landing, int):
                    raise ValueError("entry_landing_frame must be an integer")
                if a <= landing < b:
                    entries.append((landing, label + ":landing"))
    return fps, entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--timeline", type=Path)
    mode.add_argument("--at", type=float, action="append", help="Seconds; repeat for multiple samples")
    parser.add_argument("--samples", default="start,mid,end", help="Comma-separated start,mid,end")
    parser.add_argument("--include-lyrics", action="store_true")
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--out", type=Path, required=True, help="New or empty output directory")
    args = parser.parse_args()
    try:
        if not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")):
            raise ValueError("Install ffmpeg and ffprobe first")
        if not args.video.is_file():
            raise ValueError("Input video does not exist")
        if args.width < 64 or args.width > 7680:
            raise ValueError("width must be between 64 and 7680")
        kinds = args.samples.split(",")
        if not kinds or set(kinds) - {"start", "mid", "end"}:
            raise ValueError("samples must contain start,mid,end only")
        probe = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                "-show_streams", "-show_format", "-of", "json", str(args.video)]))
        streams = probe.get("streams", [])
        if not streams:
            raise ValueError("Input has no video stream")
        duration = float(streams[0].get("duration") or probe["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Cannot determine a positive video duration")
        if args.timeline:
            data = json.loads(args.timeline.read_text(encoding="utf-8"))
            fps, entries = build_samples(data, kinds, args.include_lyrics)
            targets = [(frame / fps, frame, label) for frame, label in entries]
        else:
            targets = [(finite_number(t), None, "manual") for t in args.at]
        if not targets or any(t < 0 or t >= duration for t, _, _ in targets):
            raise ValueError("Every sample time must lie within the rendered video")
        grouped = {}
        for time, frame, label in targets:
            key = round(time, 9)
            entry = grouped.setdefault(key, {"requested_time_seconds": time,
                                            "timeline_frame": frame, "labels": []})
            if label not in entry["labels"]:
                entry["labels"].append(label)
        if args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
            raise ValueError("Output must be a new or empty directory; existing review files are preserved")
        args.out.mkdir(parents=True, exist_ok=True)
        rows, cards = [], []
        for index, (_, row) in enumerate(sorted(grouped.items())):
            filename = f"frame-{index:04d}.jpg"
            run(["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-threads", "2",
                 "-ss", f"{row['requested_time_seconds']:.9f}", "-i", str(args.video),
                 "-map", "0:v:0", "-frames:v", "1", "-an", "-vf",
                 f"scale={args.width}:-2", "-q:v", "2", "-update", "1", str(args.out / filename)])
            if not (args.out / filename).is_file() or (args.out / filename).stat().st_size == 0:
                raise ValueError("Decoder produced no frame at a requested time")
            row["file"] = filename
            rows.append(row)
            label = html.escape(" / ".join(row["labels"]))
            cards.append(f'<figure><a href="{filename}"><img loading="lazy" src="{filename}" '
                         f'alt="{label}"></a><figcaption>{row["requested_time_seconds"]:.3f}s · '
                         f'{label}</figcaption></figure>')
        report = {"video_name": args.video.name, "video_duration": duration, "frames": rows,
                  "note": "Times are requested seek positions; for variable-rate video inspect decoded PTS. "
                          "Frame sampling does not certify lyric readability or continuous audiovisual review."}
        (args.out / "frames.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        page = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<title>成片抽帧审查</title><style>body{background:#111318;color:#eee;'
                'font:15px system-ui;margin:24px}main{display:grid;grid-template-columns:'
                'repeat(auto-fit,minmax(min(100%,420px),1fr));gap:18px}figure{margin:0;'
                'background:#20232a}img{width:100%;display:block}figcaption{padding:10px;'
                'overflow-wrap:anywhere}p{color:#aeb4c1}</style><h1>成片抽帧审查</h1>'
                '<p>时间为抽帧目标时间。点击图片放大；抽帧检查不等于连续视听审片。</p><main>'
                + "\n".join(cards) + '</main></html>')
        (args.out / "index.html").write_text(page, encoding="utf-8")
        print(json.dumps({"images": len(rows), "index": str(args.out / "index.html")}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        print(f"Review error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
