import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "dark-amv-editing/scripts/review_frames.py"
spec = importlib.util.spec_from_file_location("review_frames", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReviewFramesTests(unittest.TestCase):
    def test_half_open_intervals_and_lyric_landing(self):
        data = {"fps": 30, "shots": [{"id": "a", "start_frame": 30, "end_frame": 60}],
                "lyrics": [{"id": "word", "start": 1.2, "end": 1.4, "entry_landing_frame": 39}]}
        fps, rows = module.build_samples(data, ["start", "mid", "end"], True)
        self.assertEqual(fps, 30)
        self.assertEqual({frame for frame, _ in rows}, {30, 45, 59, 36, 39, 41})

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_real_decode_gallery_and_preserve_existing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video, out = root / "synthetic.mp4", root / "review"
            subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                            "testsrc2=size=320x180:rate=30", "-t", "2", "-c:v", "mpeg4",
                            str(video)], check=True)
            command = [sys.executable, str(SCRIPT), str(video), "--at", "0.2", "--at", "1.7",
                       "--width", "320", "--out", str(out)]
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            report = json.loads((out / "frames.json").read_text())
            self.assertEqual(len(report["frames"]), 2)
            image = (out / "frame-0000.jpg").read_bytes()
            self.assertTrue(image.startswith(b"\xff\xd8"))
            self.assertIn("frame-0001.jpg", (out / "index.html").read_text())
            second = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual((out / "frame-0000.jpg").read_bytes(), image)


if __name__ == "__main__":
    unittest.main()
