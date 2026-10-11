#!/usr/bin/env python3
"""Audit declared AMV timeline data using only the Python standard library.

Shot beat_frames are local; lyric beat_frames and entry_landing_frame are global.
This does not decode media, verify measured audio events, or inspect glyphs/mattes.
"""

import argparse
import json
import math
import os
from pathlib import Path
import sys

SCHEMA_VERSION = 1
EPS = 1e-6
RAMP_PRESETS = {
    "gather": [[0, 0], [0.40, 0.18], [0.70, 0.80], [1, 1]],
    "strike": [[0, 0], [0.22, 0.12], [0.58, 0.86], [1, 1]],
}


def _report():
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "summary": {
            "errors": 0, "warnings": 0, "sources": 0, "shots": 0,
            "lyrics": 0, "total_frames": None, "fps": None,
            "duration_seconds": None,
        },
        "errors": [],
        "warnings": [],
        "checks": {
            "scope": "Declared JSON structure, timing, geometry coordinates and source windows only; no media decoding, audio-event verification, glyph readability or matte registration.",
            "files_checked": 0,
            "file_scope": "sources[].path only",
            "reuse_policy": "warning",
            "lyric_beat_boundary_warning_tolerance_frames": 1,
            "ramp_presets": RAMP_PRESETS,
        },
    }


def _finish(report):
    report["summary"]["errors"] = len(report["errors"])
    report["summary"]["warnings"] = len(report["warnings"])
    report["ok"] = not report["errors"]
    return report


class _Audit:
    def __init__(self, project_root, check_files, forbid_reuse):
        self.report = _report()
        self.root = Path(project_root or ".").expanduser().absolute()
        self.check_files = check_files
        self.forbid_reuse = forbid_reuse
        self.report["checks"]["reuse_policy"] = "error" if forbid_reuse else "warning"

    def issue(self, code, path, message, warning=False):
        self.report["warnings" if warning else "errors"].append(
            {"code": code, "path": path, "message": message}
        )

    def number(self, value, path, minimum=None, positive=False):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.issue("number_type", path, "Expected a finite number, excluding booleans.")
            return None
        try:
            value = float(value)
        except (OverflowError, ValueError):
            self.issue("non_finite_number", path, "Number exceeds the finite numeric range.")
            return None
        if not math.isfinite(value):
            self.issue("non_finite_number", path, "NaN and infinity are not allowed.")
            return None
        if positive and value <= 0:
            self.issue("number_range", path, "Expected a number greater than zero.")
            return None
        if minimum is not None and value < minimum:
            self.issue("number_range", path, f"Expected a number >= {minimum}.")
            return None
        return value

    def integer(self, value, path, minimum=0):
        if isinstance(value, bool) or not isinstance(value, int):
            self.issue("integer_type", path, "Expected an integer frame number, excluding booleans.")
            return None
        if value < minimum:
            self.issue("integer_range", path, f"Expected an integer >= {minimum}.")
            return None
        return value

    def identity(self, value, path, known):
        if not isinstance(value, str) or not value.strip():
            self.issue("id_type", path, "Expected a nonempty string ID.")
            return None
        if value in known:
            self.issue("duplicate_id", path, f"Duplicate ID: {value!r}.")
            return None
        known.add(value)
        return value

    def items(self, data, key, required=True, nonempty=False):
        if key not in data and not required:
            return []
        value = data.get(key)
        if not isinstance(value, list):
            self.issue("list_type", key, "Expected a JSON array.")
            return []
        if nonempty and not value:
            self.issue("empty_list", key, "At least one item is required.")
        return value

    def coordinates(self, value, path, count):
        if not isinstance(value, list) or len(value) != count:
            self.issue("coordinate_shape", path, f"Expected an array of {count} numbers.")
            return None
        result = [self.number(v, f"{path}[{i}]") for i, v in enumerate(value)]
        if any(v is None for v in result):
            return None
        if any(v < 0 or v > 1 for v in result):
            self.issue("coordinate_range", path, "Normalized coordinates must be between 0 and 1.")
        return result

    def ramp(self, value, path):
        if value is None:
            return
        if isinstance(value, str):
            if value not in RAMP_PRESETS:
                self.issue("ramp_preset", path, "Unknown ramp preset; supported names are gather and strike, or provide explicit point pairs.")
                return
            value = RAMP_PRESETS[value]
        if not isinstance(value, list) or len(value) < 2:
            self.issue("ramp_shape", path, "Expected at least two [output_progress, source_progress] pairs.")
            return
        points = [self.coordinates(v, f"{path}[{i}]", 2) for i, v in enumerate(value)]
        if any(p is None for p in points):
            return
        if any(abs(v) > EPS for v in points[0]) or any(abs(v - 1) > EPS for v in points[-1]):
            self.issue("ramp_endpoints", path, "A normalized ramp must begin at [0, 0] and end at [1, 1].")
        if any(b[0] <= a[0] for a, b in zip(points, points[1:])):
            self.issue("ramp_output_order", path, "Output progress must increase strictly; duplicate knots are invalid.")
        if any(b[1] < a[1] for a, b in zip(points, points[1:])):
            self.issue("ramp_source_order", path, "Source progress must not decrease; equal values can represent a hold.")

    def beat_frames(self, value, path, upper, lower=0, local=False):
        if not isinstance(value, list):
            self.issue("list_type", path, "Expected an array of integer beat frame numbers.")
            return []
        result = []
        for i, v in enumerate(value):
            frame = self.integer(v, f"{path}[{i}]")
            if frame is None:
                continue
            result.append(frame)
            if upper is not None and not lower <= frame < upper:
                unit = "shot-local" if local else "timeline-global"
                self.issue("beat_frame_range", f"{path}[{i}]", f"Expected a {unit} beat frame in [{lower}, {upper}).")
        if len(result) != len(set(result)):
            self.issue("duplicate_beat_frame", path, "Repeated beat frame references may duplicate an effect.", warning=True)
        return result


def audit_timeline(data, project_root=None, check_files=False, forbid_reuse=False):
    """Return a JSON-compatible report; unknown metadata fields are preserved/ignored.

    IDs must be unique within sources, shots and lyrics respectively. Source reuse
    means positive overlap of declared windows for the same normalized file path.
    Adjacent source windows are allowed. Relative paths use project_root, or cwd
    for this Python API (the CLI instead defaults to the timeline's directory).
    """
    try:
        a = _Audit(project_root, check_files, forbid_reuse)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        return _input_error("project_root_path", "project_root", str(exc))
    if not isinstance(data, dict):
        a.issue("root_type", "$", "Expected a JSON object at the document root.")
        return _finish(a.report)
    fps = a.number(data.get("fps"), "fps", positive=True)
    total = a.integer(data.get("frames"), "frames", minimum=1)
    duration = None
    if fps is not None and total is not None:
        try:
            duration = total / fps
        except OverflowError:
            duration = math.inf
        if not math.isfinite(duration):
            a.issue("duration_range", "frames/fps", "The declared duration exceeds the finite numeric range.")
            duration = None
    a.report["summary"].update(fps=fps, total_frames=total, duration_seconds=duration)

    raw_sources = a.items(data, "sources", nonempty=True)
    raw_shots = a.items(data, "shots", nonempty=True)
    raw_lyrics = a.items(data, "lyrics", required=False)
    a.report["summary"].update(sources=len(raw_sources), shots=len(raw_shots), lyrics=len(raw_lyrics))
    sources, shots, windows = {}, {}, []
    source_ids, shot_ids, lyric_ids = set(), set(), set()
    for i, source in enumerate(raw_sources):
        p = f"sources[{i}]"
        if not isinstance(source, dict):
            a.issue("object_type", p, "Expected a source object.")
            continue
        sid = a.identity(source.get("id"), p + ".id", source_ids)
        length = a.number(source.get("duration"), p + ".duration", positive=True)
        raw_path = source.get("path")
        canonical = None
        if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
            a.issue("path_type", p + ".path", "Expected a nonempty local file path without NUL characters.")
        else:
            try:
                path = Path(raw_path).expanduser()
                if not path.is_absolute():
                    path = a.root / path
                canonical = os.path.normcase(os.path.abspath(path))
            except (OSError, ValueError, RuntimeError) as exc:
                a.issue("source_path", p + ".path", str(exc))
            else:
                if check_files:
                    a.report["checks"]["files_checked"] += 1
                    try:
                        exists = path.is_file()
                    except OSError:
                        exists = False
                    if not exists:
                        a.issue("source_file_missing", p + ".path", f"Source is not an accessible file: {path}")
        if sid is not None:
            sources[sid] = {"duration": length, "canonical_path": canonical}

    previous_end = 0
    for i, shot in enumerate(raw_shots):
        p = f"shots[{i}]"
        if not isinstance(shot, dict):
            a.issue("object_type", p, "Expected a shot object.")
            previous_end = None
            continue
        sid = a.identity(shot.get("id"), p + ".id", shot_ids)
        start = a.integer(shot.get("start_frame"), p + ".start_frame")
        end = a.integer(shot.get("end_frame"), p + ".end_frame")
        if start is not None and end is not None:
            if end <= start:
                a.issue("shot_empty_or_reversed", p, "end_frame must be greater than start_frame.")
            if total is not None and (start >= total or end > total):
                a.issue("shot_frame_range", p, "Shot frames extend outside the declared timeline.")
            if previous_end is not None and start != previous_end:
                code = "timeline_gap" if start > previous_end else "timeline_overlap"
                a.issue(code, p + ".start_frame", f"Expected frame {previous_end}; found {start}. Shots must be listed in continuous timeline order.")
        previous_end = end
        if sid is not None:
            shots[sid] = {"start": start, "end": end}
        source_id = shot.get("source_id")
        source = sources.get(source_id) if isinstance(source_id, str) else None
        if source is None:
            a.issue("unknown_source", p + ".source_id", "Expected the ID of a declared valid source.")
        source_in = a.number(shot.get("source_in"), p + ".source_in", minimum=0)
        source_span = a.number(shot.get("source_span"), p + ".source_span", positive=True)
        if source_in is not None and source_span is not None:
            source_end = source_in + source_span
            if not math.isfinite(source_end):
                a.issue("source_window_range", p, "The source window end is not finite.")
            elif source is not None:
                if source["duration"] is not None and source_end > source["duration"] + EPS:
                    a.issue("source_window_range", p, f"Source window ends at {source_end:g}s, after declared duration {source['duration']:g}s.")
                if source["canonical_path"] is not None:
                    windows.append((source["canonical_path"], source_in, source_end, p, sid))
        if "ramp" in shot:
            a.ramp(shot["ramp"], p + ".ramp")
        if "beat_frames" in shot:
            length = end - start if start is not None and end is not None else None
            a.beat_frames(shot["beat_frames"], p + ".beat_frames", length, local=True)
    if raw_shots and total is not None and previous_end is not None and previous_end != total:
        a.issue("timeline_total", "shots", f"Final shot ends at frame {previous_end}; timeline declares {total} frames.")

    active = []
    previous_file = None
    for file, start, end, path, sid in sorted(windows, key=lambda w: (w[0], w[1], w[2], w[3])):
        if file != previous_file:
            active = []
        active = [w for w in active if w[1] > start + EPS]
        for other_start, other_end, other_path, other_id in active:
            overlap = min(end, other_end) - max(start, other_start)
            if overlap > EPS:
                a.issue("source_window_reuse", path, f"Source window overlaps {other_path} ({other_id!r}) by {overlap:.6f}s; current shot {sid!r}.", warning=not forbid_reuse)
        active.append((start, end, path, sid))
        previous_file = file

    for i, lyric in enumerate(raw_lyrics):
        p = f"lyrics[{i}]"
        if not isinstance(lyric, dict):
            a.issue("object_type", p, "Expected a lyric/text event object.")
            continue
        a.identity(lyric.get("id"), p + ".id", lyric_ids)
        start = a.number(lyric.get("start"), p + ".start", minimum=0)
        end = a.number(lyric.get("end"), p + ".end", minimum=0)
        if start is not None and end is not None:
            if end <= start:
                a.issue("lyric_empty_or_reversed", p, "end must be greater than start.")
            if duration is not None and (start >= duration or end > duration + EPS):
                a.issue("lyric_time_range", p, "Text display extends outside the declared timeline.")
        if "shot_id" in lyric:
            sid = lyric["shot_id"]
            shot = shots.get(sid) if isinstance(sid, str) else None
            if shot is None:
                a.issue("unknown_shot", p + ".shot_id", "Expected the ID of a declared valid shot.")
            elif start is not None and end is not None and fps is not None and duration is not None and shot["start"] is not None and shot["end"] is not None and 0 <= shot["start"] < shot["end"] <= total:
                if start < shot["start"] / fps - EPS or end > shot["end"] / fps + EPS:
                    a.issue("lyric_shot_range", p, "Text display extends outside its bound shot; split a cross-shot phrase into layout segments or omit shot_id for intentionally unbound text.")
        if "beat_frames" in lyric:
            frames = a.beat_frames(lyric["beat_frames"], p + ".beat_frames", total)
            if fps is not None and duration is not None and start is not None and end is not None:
                for frame in frames:
                    if 0 <= frame < (total or 0) and not start - EPS <= frame / fps < end:
                        if start - 1 / fps - EPS <= frame / fps <= end + 1 / fps + EPS:
                            a.issue("lyric_beat_boundary", p + ".beat_frames", f"Global beat frame {frame} lies outside the active display interval but within one frame of its boundary. It may be carried by the adjacent layout; verify the rendered hand-off instead of counting it as an active displayed peak.", warning=True)
                        else:
                            a.issue("lyric_beat_time_range", p + ".beat_frames", f"Global beat frame {frame} is outside this text display interval by more than one frame. Do not pass shot-local frames here.")
        enter = None
        if "enter_duration" in lyric:
            enter = a.number(lyric["enter_duration"], p + ".enter_duration", positive=True)
        if lyric.get("entry_landing_frame") is not None:
            landing = a.integer(lyric["entry_landing_frame"], p + ".entry_landing_frame")
            if landing is not None:
                if total is not None and landing >= total:
                    a.issue("entry_landing_range", p + ".entry_landing_frame", "Entry landing must use a global frame inside the timeline.")
                elif fps is not None and duration is not None and start is not None and end is not None:
                    when = landing / fps
                    if not start - EPS <= when < end:
                        a.issue("entry_landing_outside_lyric", p + ".entry_landing_frame", "Entry lands outside this display segment; review whether a cross-shot continuation still makes the wording readable.", warning=True)
                    if enter is not None and abs(start + enter - when) > 1 / fps + EPS:
                        a.issue("entry_landing_duration_mismatch", p, "start + enter_duration differs from entry_landing_frame/fps by more than one frame.", warning=True)
        elif enter is not None and start is not None and end is not None and enter > end - start + EPS:
            a.issue("entry_longer_than_lyric", p + ".enter_duration", "Entrance lasts longer than the display interval; review the visible reading window.", warning=True)
        if "pos" in lyric:
            a.coordinates(lyric["pos"], p + ".pos", 2)
        if "quad" in lyric:
            quad = lyric["quad"]
            if not isinstance(quad, list) or len(quad) != 4:
                a.issue("quad_shape", p + ".quad", "Expected four ordered [x, y] points.")
            else:
                points = [a.coordinates(v, f"{p}.quad[{j}]", 2) for j, v in enumerate(quad)]
                if all(v is not None for v in points):
                    crosses = []
                    for j in range(4):
                        x, y, z = points[j], points[(j + 1) % 4], points[(j + 2) % 4]
                        crosses.append((y[0] - x[0]) * (z[1] - y[1]) - (y[1] - x[1]) * (z[0] - y[0]))
                    if not (all(c > 1e-12 for c in crosses) or all(c < -1e-12 for c in crosses)):
                        a.issue("quad_geometry", p + ".quad", "Quad must be convex, nondegenerate and ordered around its perimeter.")
    return _finish(a.report)


def _input_error(code, path, message):
    report = _report()
    report["errors"].append({"code": code, "path": str(path), "message": message})
    return _finish(report)


def _reject_constant(value):
    raise ValueError(f"Nonstandard JSON constant is not allowed: {value}")


def _parse_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"JSON number exceeds the finite numeric range: {value}")
    return result


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps(_input_error("cli_arguments", "$", message), ensure_ascii=False), file=sys.stderr)
        self.exit(2)


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument("timeline", help="Timeline JSON file")
    parser.add_argument("--project-root", help="Base directory for relative source paths; default: timeline file's parent")
    parser.add_argument("--check-files", action="store_true", help="Check sources[].path exists as a local file; does not decode media")
    parser.add_argument("--forbid-reuse", action="store_true", help="Treat overlapping source windows as errors instead of warnings")
    parser.add_argument("--output", help="Also save the JSON report to this file")
    args = parser.parse_args(argv)
    try:
        timeline = Path(args.timeline).expanduser()
        output = Path(args.output).expanduser() if args.output else None
        conflict = output is not None and output.resolve() == timeline.resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps(_input_error("cli_path", "$", str(exc)), ensure_ascii=False))
        return 2
    if conflict:
        print(json.dumps(_input_error("output_conflict", args.output, "Report output must not overwrite the input timeline."), ensure_ascii=False))
        return 2
    try:
        data = json.loads(timeline.read_text(encoding="utf-8"), parse_constant=_reject_constant, parse_float=_parse_float)
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        report = _input_error("input_read_or_json", args.timeline, str(exc))
        code = 2
    else:
        report = audit_timeline(data, args.project_root or timeline.absolute().parent, args.check_files, args.forbid_reuse)
        code = 0 if report["ok"] else 1
    if output is not None:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        except (OSError, ValueError) as exc:
            report["errors"].append({"code": "output_write", "path": str(output), "message": str(exc)})
            _finish(report)
            code = 2
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
