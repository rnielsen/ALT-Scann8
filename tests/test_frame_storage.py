import importlib.util
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from frame_paths import frame_files, frame_path, frame_number


class FrameStorageTests(unittest.TestCase):
    def test_boundaries_hdr_and_numeric_sort(self):
        with tempfile.TemporaryDirectory() as folder:
            for number in (100000, 27315, 1000, 999, 1):
                name = f'picture-{number:05d}.jpg'
                path = Path(frame_path(folder, name, create=True))
                self.assertEqual(path.parent.name, f'{number // 1000:04d}')
                path.touch()
                self.assertEqual(Path(frame_path(folder, f'picture-{number:05d}.3.jpg')).parent, path.parent)
            self.assertEqual([frame_number(p) for p in frame_files(folder, 'picture-?????.jpg')],
                             [1, 999, 1000, 27315, 100000])

    def test_flat_mixed_duplicate_and_unrelated_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            flat = Path(folder) / 'picture-01000.jpg'
            flat.touch()
            self.assertEqual(frame_path(folder, flat.name), str(flat))
            bucket = Path(folder) / '0001'
            bucket.mkdir()
            (bucket / flat.name).touch()
            (bucket / 'picture-01001.jpg').touch()
            for directory in ('output', 'templates'):
                p = Path(folder) / directory
                p.mkdir()
                (p / 'picture-00001.jpg').touch()
            (bucket / 'picture-01002.123.part.jpg').touch()
            self.assertEqual(frame_files(folder), [str(bucket / flat.name), str(bucket / 'picture-01001.jpg')])

    def test_base_frame_pattern_does_not_include_hdr_exposures(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ('picture-01000.jpg', 'picture-01000.2.jpg', 'picture-100000.jpg'):
                Path(frame_path(folder, name, create=True)).touch()
            self.assertEqual([Path(p).name for p in frame_files(folder, 'picture-?????.jpg')],
                             ['picture-01000.jpg', 'picture-100000.jpg'])

    def test_legacy_hdr_and_other_scanner_names(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ('hdrpic-01234.3.jpg', 'frame-01235.png'):
                p = Path(frame_path(folder, name, create=True))
                p.touch()
                self.assertIn(str(p), frame_files(folder))


spec = importlib.util.spec_from_file_location('scanner', ROOT / 'ALT-Scann8.py')
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class CaptureSaveTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.state = patch.multiple(scanner, CurrentDir=self.folder.name, save_failure=None,
                                    save_failure_notified=False, ScanStopRequested=False)
        self.state.start()
        self.addCleanup(self.state.stop)

    def test_success_publishes_frame_and_preserves_file_extension(self):
        def save(path):
            self.assertTrue(path.endswith('.jpg'))
            Path(path).write_bytes(b'complete')
        scanner.save_frame('picture-01000.jpg', save)
        path = Path(frame_path(self.folder.name, 'picture-01000.jpg'))
        self.assertEqual(path.read_bytes(), b'complete')
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_disk_failure_removes_partial_preserves_previous_and_latches_first_error(self):
        path = Path(frame_path(self.folder.name, 'picture-01000.jpg', create=True))
        path.write_bytes(b'previous')
        def fail(destination):
            Path(destination).write_bytes(b'partial')
            raise OSError(28, 'No space left on device')
        with self.assertLogs(level='ERROR'):
            with self.assertRaises(scanner.CaptureSaveError):
                scanner.save_frame(path.name, fail)
        self.assertTrue(scanner.ScanStopRequested)
        self.assertEqual(scanner.save_failure[:2], (1000, str(path)))
        self.assertEqual(path.read_bytes(), b'previous')
        self.assertEqual(list(path.parent.iterdir()), [path])
        save = Mock()
        with self.assertRaises(scanner.CaptureSaveError):
            scanner.save_frame('picture-01001.jpg', save)
        save.assert_not_called()
        self.assertEqual(scanner.save_failure[0], 1000)

    def test_directory_creation_failure_stops_scan(self):
        (Path(self.folder.name) / '0001').write_text('blocks directory')
        with self.assertLogs(level='ERROR'), self.assertRaises(scanner.CaptureSaveError):
            scanner.save_frame('picture-01000.jpg', Mock())
        self.assertTrue(scanner.ScanStopRequested)
        self.assertEqual(scanner.save_failure[0], 1000)

    def test_failed_request_is_released_and_worker_drains_remaining_queue(self):
        q = queue.Queue()
        request = Mock()
        request.save_dng.side_effect = OSError(28, 'No space left on device')
        skipped = Mock()
        for req, number in ((request, 1000), (skipped, 1001)):
            q.put((scanner.REQUEST_TOKEN, req, number, 0, self.folder.name))
        q.put(scanner.END_TOKEN)
        with patch.multiple(scanner, ExitingApp=False, active_threads=1, FileType='dng'), self.assertLogs(level='ERROR'):
            scanner.capture_save_thread(q, threading.Event(), 1)
            self.assertEqual(scanner.active_threads, 0)
        request.release.assert_called_once()
        skipped.release.assert_called_once()
        skipped.save_dng.assert_not_called()
        self.assertEqual(q.unfinished_tasks, 0)
        self.assertEqual(scanner.save_failure[0], 1000)

    def test_ui_stops_and_reports_once_on_main_thread(self):
        scanner.save_failure = (1000, '/scan/0001/picture-01000.jpg', 'disk full')
        with patch.multiple(scanner, ScanOngoing=True, ExitingApp=False, win=Mock(), stop_scan=Mock()) as state, patch.object(scanner.tk.messagebox, 'showerror') as dialog:
            scanner.check_save_failure()
            scanner.check_save_failure()
            scanner.stop_scan.assert_called_once()
            dialog.assert_called_once()
            self.assertIn('1000', dialog.call_args.args[1])
            self.assertIn('/scan/0001/picture-01000.jpg', dialog.call_args.args[1])

    def test_failed_capture_does_not_advance_film(self):
        state = dict(ScanOngoing=True, ScanStopRequested=False, FrameDetectMode='PFD',
                     NewFrameAvailable=True, CurrentFrame=999, session_frames=0,
                     frames_to_go_str=Mock(get=lambda: ''), register_frame=Mock(),
                     capture=Mock(return_value=False), win=Mock(), send_arduino_command=Mock())
        with patch.multiple(scanner, create=True, **state):
            scanner.capture_loop()
            scanner.send_arduino_command.assert_not_called()
            scanner.win.after.assert_called_once_with(5, scanner.capture_loop)

    def test_synchronous_failure_does_not_escape_capture_callback(self):
        with patch.object(scanner, '_capture', side_effect=scanner.CaptureSaveError('failed')):
            self.assertFalse(scanner.capture('normal'))


if __name__ == '__main__':
    unittest.main()
