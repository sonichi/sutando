#!/usr/bin/env python3
"""Platform resize preserves originals on failure and bounds Windows images."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import sutando_platform as platform


class ResizeImage(unittest.TestCase):
    def test_macos_arguments_preserve_existing_behavior(self):
        with patch.object(platform, 'is_windows', return_value=False), patch.object(platform.subprocess, 'run') as run:
            self.assertTrue(platform.resize_image('frame.jpg', 1280, 60))
        self.assertEqual(run.call_args.args[0], ['sips', '--resampleHeightWidthMax', '1280', '-s', 'format', 'jpeg', '-s', 'formatOptions', '60', 'frame.jpg'])

    def test_windows_failure_keeps_original_and_removes_scratch(self):
        with tempfile.TemporaryDirectory() as td:
            frame = Path(td) / "frame's shot.jpg"
            frame.write_bytes(b'original')
            with patch.object(platform, 'is_windows', return_value=True), patch.object(platform.subprocess, 'run', side_effect=subprocess.TimeoutExpired('powershell', 10)) as run:
                self.assertFalse(platform.resize_image(str(frame), 1280, 60))
            self.assertEqual(frame.read_bytes(), b'original')
            self.assertEqual(list(Path(td).iterdir()), [frame])
            self.assertEqual(run.call_args.args[0][0], 'powershell.exe')
            # The path must never reach the script source; PowerShell treats
            # U+2018-U+201B as quote delimiters too, so escaping cannot be local.
            self.assertNotIn("frame's shot.jpg", run.call_args.args[0][-1])
            self.assertEqual(run.call_args.kwargs['env']['SUTANDO_RESIZE_INPUT'], str(frame.resolve()))

    @unittest.skipUnless(sys.platform == 'win32', 'requires native Windows System.Drawing')
    def test_windows_resizes_real_frame_to_jpeg(self):
        with tempfile.TemporaryDirectory() as td:
            frame = Path(td) / "frame's shot.png"
            safe = str(frame).replace("'", "''")
            subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                            'Add-Type -AssemblyName System.Drawing; '
                            '$b = [System.Drawing.Bitmap]::new(2400, 1200); '
                            f"$b.Save('{safe}', [System.Drawing.Imaging.ImageFormat]::Png); $b.Dispose()"], check=True)
            self.assertTrue(platform.resize_image(str(frame), 1280, 60))
            self.assertEqual(frame.read_bytes()[:2], b'\xff\xd8')
            result = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                                     'Add-Type -AssemblyName System.Drawing; '
                                     f"$b = [System.Drawing.Image]::FromFile('{safe}'); "
                                     'Write-Output "$($b.Width)x$($b.Height)"; $b.Dispose()'],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), '1280x640')


    @unittest.skipUnless(sys.platform == 'win32', 'requires native Windows System.Drawing')
    def test_windows_resizes_frame_under_typographic_apostrophe(self):
        # A profile such as O’Brien broke resize while only the ASCII
        # apostrophe was doubled, so frames over the size cap returned HTTP 500.
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / 'O’Brien'
            folder.mkdir()
            frame = folder / 'frame.png'
            env = {**os.environ, 'SUTANDO_TEST_FRAME': str(frame)}
            subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                            'Add-Type -AssemblyName System.Drawing; '
                            '$b = [System.Drawing.Bitmap]::new(2400, 1200); '
                            '$b.Save($env:SUTANDO_TEST_FRAME, '
                            '[System.Drawing.Imaging.ImageFormat]::Png); $b.Dispose()'],
                           check=True, env=env)
            self.assertTrue(platform.resize_image(str(frame), 1280, 60))
            self.assertEqual(frame.read_bytes()[:2], b'\xff\xd8')


if __name__ == '__main__':
    unittest.main()
