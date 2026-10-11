"""Observable timeline invariants; run with python -m unittest discover -s tests."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "dark-amv-editing/scripts/audit_timeline.py"
SPEC = importlib.util.spec_from_file_location("audit_timeline", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def example():
    return {
        "fps": 30,
        "frames": 90,
        "sources": [{"id": "video", "path": "media/clip.mp4", "duration": 20}],
        "shots": [
            {"id": "A", "source_id": "video", "source_in": 0, "source_span": 2,
             "start_frame": 0, "end_frame": 45, "beat_frames": [0, 30, 44],
             "ramp": [[0, 0], [0.4, 0.2], [0.6, 0.2], [1, 1]]},
            {"id": "B", "source_id": "video", "source_in": 2, "source_span": 3,
             "start_frame": 45, "end_frame": 90, "beat_frames": [0, 20, 44]},
        ],
        "lyrics": [
            {"id": "L1", "start": 0.2, "end": 1.4, "shot_id": "A", "beat_frames": [12, 30],
             "entry_landing_frame": 12, "enter_duration": 0.2, "pos": [0.2, 0.7]},
            {"id": "L2", "start": 1.75, "end": 2.75, "shot_id": "B", "beat_frames": [60, 80],
             "quad": [[0.1, 0.1], [0.8, 0.15], [0.7, 0.6], [0.1, 0.5]]},
        ],
    }


def codes(report, severity="errors"):
    return {item["code"] for item in report[severity]}


class TimelineAuditTests(unittest.TestCase):
    def test_valid_timeline_and_source_hold(self):
        report = MODULE.audit_timeline(example())
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["summary"]["duration_seconds"], 3)

    def test_unbound_title_can_cross_a_cut_and_lyrics_can_be_absent(self):
        data = example()
        data["lyrics"].append({"id": "title", "start": 1.2, "end": 2.0})
        self.assertTrue(MODULE.audit_timeline(data)["ok"])
        del data["lyrics"]
        self.assertTrue(MODULE.audit_timeline(data)["ok"])

    def test_gap_overlap_and_final_duration_are_errors(self):
        for start, code in [(46, "timeline_gap"), (44, "timeline_overlap")]:
            with self.subTest(start=start):
                data = example()
                data["shots"][1]["start_frame"] = start
                self.assertIn(code, codes(MODULE.audit_timeline(data)))
        data = example()
        data["shots"][-1]["end_frame"] = 89
        self.assertIn("timeline_total", codes(MODULE.audit_timeline(data)))

    def test_initial_gap_and_shot_outside_timeline(self):
        data = example()
        data["shots"][0]["start_frame"] = 1
        data["shots"][-1]["end_frame"] = 91
        result = codes(MODULE.audit_timeline(data))
        self.assertIn("timeline_gap", result)
        self.assertIn("shot_frame_range", result)

    def test_source_window_bounds_and_reference(self):
        data = example()
        data["shots"][1]["source_in"] = 19
        self.assertIn("source_window_range", codes(MODULE.audit_timeline(data)))
        data["shots"][1]["source_id"] = "missing"
        self.assertIn("unknown_source", codes(MODULE.audit_timeline(data)))

    def test_source_touch_is_allowed_but_overlap_warns_or_fails(self):
        data = example()
        self.assertNotIn("source_window_reuse", codes(MODULE.audit_timeline(data), "warnings"))
        data["shots"][1]["source_in"] = 1.5
        result = MODULE.audit_timeline(data)
        self.assertTrue(result["ok"])
        self.assertIn("source_window_reuse", codes(result, "warnings"))
        result = MODULE.audit_timeline(data, forbid_reuse=True)
        self.assertFalse(result["ok"])
        self.assertIn("source_window_reuse", codes(result))

    def test_source_alias_ids_do_not_hide_reuse(self):
        data = example()
        data["sources"].append({"id": "alias", "path": "media/../media/clip.mp4", "duration": 20})
        data["shots"][1].update(source_id="alias", source_in=1)
        self.assertIn("source_window_reuse", codes(MODULE.audit_timeline(data), "warnings"))

    def test_shot_beats_are_local_and_lyric_beats_global(self):
        data = example()
        data["shots"][1]["beat_frames"] = [60]
        self.assertIn("beat_frame_range", codes(MODULE.audit_timeline(data)))
        data = example()
        data["lyrics"][1]["beat_frames"] = [15]
        self.assertIn("lyric_beat_time_range", codes(MODULE.audit_timeline(data)))
        data["lyrics"][1]["beat_frames"] = [90]
        self.assertIn("beat_frame_range", codes(MODULE.audit_timeline(data)))

    def test_lyric_binding_and_global_time_bounds(self):
        data = example()
        data["lyrics"][0]["end"] = 1.6
        self.assertIn("lyric_shot_range", codes(MODULE.audit_timeline(data)))
        data["lyrics"][1]["end"] = 3.1
        self.assertIn("lyric_time_range", codes(MODULE.audit_timeline(data)))
        data["lyrics"][1]["shot_id"] = "unknown"
        self.assertIn("unknown_shot", codes(MODULE.audit_timeline(data)))

    def test_clipped_entry_landing_warns_without_invalidating_timeline(self):
        data = example()
        data["lyrics"][0].update(start=0.72, end=0.9, enter_duration=0.213333333333, entry_landing_frame=28, beat_frames=[])
        result = MODULE.audit_timeline(data)
        self.assertTrue(result["ok"])
        self.assertIn("entry_landing_outside_lyric", codes(result, "warnings"))

    def test_invalid_ramps_reject_reverse_duplicate_and_bad_endpoints(self):
        variants = [
            ([[0, 0], [0.5, 0.6], [0.7, 0.5], [1, 1]], "ramp_source_order"),
            ([[0, 0], [0.5, 0.4], [0.5, 0.6], [1, 1]], "ramp_output_order"),
            ([[0, 0.1], [1, 1]], "ramp_endpoints"),
            ([], "ramp_shape"),
            ([[0, 0], [0.5, 1.2], [1, 1]], "coordinate_range"),
        ]
        for ramp, code in variants:
            with self.subTest(ramp=ramp):
                data = example()
                data["shots"][0]["ramp"] = ramp
                self.assertIn(code, codes(MODULE.audit_timeline(data)))

    def test_known_ramp_presets_and_null_are_supported_but_unknown_is_error(self):
        for ramp in [None, "gather", "strike"]:
            with self.subTest(ramp=ramp):
                data = example()
                data["shots"][0]["ramp"] = ramp
                self.assertTrue(MODULE.audit_timeline(data)["ok"])
        data = example()
        data["shots"][0]["ramp"] = "unrecognized"
        self.assertIn("ramp_preset", codes(MODULE.audit_timeline(data)))

    def test_lyric_beat_at_exclusive_end_is_a_visible_boundary_warning(self):
        data = example()
        data["lyrics"][0]["beat_frames"] = [42]
        report = MODULE.audit_timeline(data)
        self.assertTrue(report["ok"])
        self.assertIn("lyric_beat_boundary", codes(report, "warnings"))
        data["lyrics"][0]["beat_frames"] = [44]
        self.assertIn("lyric_beat_time_range", codes(MODULE.audit_timeline(data)))

    def test_unique_ids_in_each_namespace(self):
        for key in ["sources", "shots", "lyrics"]:
            with self.subTest(key=key):
                data = example()
                data[key].append(copy.deepcopy(data[key][0]))
                self.assertIn("duplicate_id", codes(MODULE.audit_timeline(data)))

    def test_non_finite_numbers_and_boolean_frames_are_invalid(self):
        for value in [float("nan"), float("inf"), float("-inf"), True, "30"]:
            with self.subTest(value=value):
                data = example()
                data["fps"] = value
                self.assertFalse(MODULE.audit_timeline(data)["ok"])
        data = example()
        data["shots"][0]["end_frame"] = True
        self.assertIn("integer_type", codes(MODULE.audit_timeline(data)))

    def test_empty_shots_wrong_collections_and_malformed_items(self):
        for root in [None, [], 3, "timeline"]:
            self.assertIn("root_type", codes(MODULE.audit_timeline(root)))
        for key, value in [("shots", []), ("sources", {}), ("lyrics", "text"), ("shots", [None]), ("sources", [False])]:
            with self.subTest(key=key, value=value):
                data = example()
                data[key] = value
                self.assertFalse(MODULE.audit_timeline(data)["ok"])

    def test_quad_and_position_geometry(self):
        data = example()
        data["lyrics"][0]["pos"] = [1.2, 0.5]
        data["lyrics"][1]["quad"] = [[0, 0], [1, 1], [1, 0], [0, 1]]
        result = codes(MODULE.audit_timeline(data))
        self.assertIn("coordinate_range", result)
        self.assertIn("quad_geometry", result)

    def test_relative_file_check_uses_project_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "media/clip.mp4"
            path.parent.mkdir()
            path.write_bytes(b"not decoded by this audit")
            result = MODULE.audit_timeline(example(), project_root=root, check_files=True)
            self.assertTrue(result["ok"])
            self.assertEqual(result["checks"]["files_checked"], 1)
            path.unlink()
            self.assertIn("source_file_missing", codes(MODULE.audit_timeline(example(), project_root=root, check_files=True)))


class TimelineCliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)

    def test_valid_report_output_and_invalid_exit_status(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "timeline.json"
            out = Path(temp) / "reports/audit.json"
            path.write_text(json.dumps(example()), encoding="utf-8")
            result = self.run_cli(path, "--output", out)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout), json.loads(out.read_text()))
            data = example()
            data["shots"][1]["start_frame"] = 46
            path.write_text(json.dumps(data), encoding="utf-8")
            result = self.run_cli(path)
            self.assertEqual(result.returncode, 1)
            self.assertIn("timeline_gap", codes(json.loads(result.stdout)))

    def test_bad_json_nan_overflow_and_missing_file_have_no_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "timeline.json"
            for raw in ["{", '{"fps":NaN}', '{"fps":Infinity}', '{"fps":1e400}']:
                with self.subTest(raw=raw):
                    path.write_text(raw, encoding="utf-8")
                    result = self.run_cli(path)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("input_read_or_json", codes(json.loads(result.stdout)))
                    self.assertNotIn("Traceback", result.stderr + result.stdout)
            result = self.run_cli(Path(temp) / "missing.json")
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr + result.stdout)

    def test_report_cannot_overwrite_input_and_bad_cli_is_clear(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "timeline.json"
            content = json.dumps(example())
            path.write_text(content, encoding="utf-8")
            result = self.run_cli(path, "--output", path)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(path.read_text(), content)
            self.assertIn("output_conflict", codes(json.loads(result.stdout)))
        result = self.run_cli("--unknown")
        self.assertEqual(result.returncode, 2)
        self.assertIn("cli_arguments", codes(json.loads(result.stderr)))


if __name__ == "__main__":
    unittest.main()
