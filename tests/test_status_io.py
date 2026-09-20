"""Real filesystem and process boundaries. No Docker calls."""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import status_io as io


class StatusIOTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_deadline_stops_descendant_after_direct_child_exits(self):
        marker = self.root / 'descendant-heartbeat'
        identity = self.root / 'descendant-identity'
        child = ("import os,pathlib,time; p=pathlib.Path(" + repr(str(marker)) + "); "
                 + "pathlib.Path(" + repr(str(identity)) + ").write_text(str(os.getpid())+' '+pathlib.Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]); "
                 + "\nfor i in range(200): p.write_text(str(i)); time.sleep(.02)")
        parent = "import subprocess,sys; subprocess.Popen([sys.executable, '-c', " + repr(child) + "])"
        with self.assertRaises(io.Unavailable):
            io.run([sys.executable, '-c', parent], timeout=.3)
        self.assertTrue(marker.exists(), 'descendant must execute before the deadline')
        before = marker.read_text()
        time.sleep(.15)
        self.assertEqual(marker.read_text(), before, 'descendant continued after probe cleanup')
        pid, started = identity.read_text().split()
        def same_process_running():
            try:
                fields = Path('/proc', pid, 'stat').read_text().rsplit(')', 1)[1].split()
            except FileNotFoundError:
                return False
            return fields[19] == started and fields[0] not in ('Z', 'X')
        deadline = time.monotonic() + 1
        while same_process_running() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(same_process_running(), 'captured descendant survived cleanup')

    def test_public_atomic_mode_and_old_file_survives_replace_failure(self):
        state = self.root / 'state'
        mask = os.umask(0o077)
        try:
            with io.directory(state / 'status', 0o700):
                pass
            with io.directory(state / 'console') as fd:
                io.publish(fd, 'status.json', {'old': True})
                path = state / 'console/status.json'
                self.assertEqual(path.stat().st_mode & 0o777, 0o644)
                before = path.read_bytes()
                with patch.object(io.os, 'replace', side_effect=OSError('SECRET')):
                    with self.assertRaises(OSError):
                        io.publish(fd, 'status.json', {'new': True})
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(path.parent.iterdir()), [path])
        finally:
            os.umask(mask)
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)
        self.assertEqual((state / 'status').stat().st_mode & 0o777, 0o700)
        self.assertEqual((state / 'console').stat().st_mode & 0o777, 0o755)
        with self.assertRaises(io.Unavailable):
            with io.directory(state / 'writable', 0o775):
                pass
        self.assertFalse((state / 'writable').exists())

    def test_failed_flush_and_oversize_preserve_previous_document(self):
        with io.directory(self.root) as fd:
            io.publish(fd, 'status.json', {'old': True})
            with patch.object(io.os, 'fsync', side_effect=OSError):
                with self.assertRaises(OSError):
                    io.publish(fd, 'status.json', {'new': True})
            with self.assertRaises(io.Unavailable):
                io.publish(fd, 'status.json', {'huge': 'x' * 65536})
            self.assertEqual(json.loads((self.root / 'status.json').read_text()), {'old': True})

    def test_publication_reclaims_fixed_temporary_file(self):
        with io.directory(self.root / 'console') as fd:
            temporary = self.root / 'console/.status.json.tmp'
            temporary.write_text('partial')
            io.publish(fd, 'status.json', {'complete': True}, serialized=True)
        self.assertFalse(temporary.exists())
        self.assertEqual(json.loads((self.root / 'console/status.json').read_text()),
                         {'complete': True})

    def test_symlink_directory_destination_and_hardlink_refused(self):
        (self.root / 'real').mkdir(mode=0o755)
        (self.root / 'link').symlink_to(self.root / 'real')
        with self.assertRaises(OSError), io.directory(self.root / 'link'):
            pass
        with io.directory(self.root / 'real') as fd:
            (self.root / 'real/status.json').symlink_to(self.root / 'secret')
            with self.assertRaises(io.Unavailable):
                io.publish(fd, 'status.json', {})
            (self.root / 'real/status.json').unlink()
            (self.root / 'secret').write_text('SECRET')
            os.link(self.root / 'secret', self.root / 'real/status.json')
            with self.assertRaises(io.Unavailable):
                io.publish(fd, 'status.json', {})
        self.assertEqual((self.root / 'secret').read_text(), 'SECRET')

    def test_private_record_permissions_and_installation_binding(self):
        io.task_record(self.root, self.root, self.root / '.env', '2026-09-20T12:00:00Z', 'healthy')
        self.assertEqual((self.root / 'status').stat().st_mode & 0o777, 0o700)
        path = self.root / 'status/bootstrap.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(io.read_task(self.root, self.root, self.root / '.env')['state'], 'healthy')
        with self.assertRaises(io.Unavailable):
            io.read_task(self.root, self.root, self.root / 'other.env')
        path.chmod(0o644)
        with self.assertRaises(io.Unavailable):
            io.read_task(self.root, self.root, self.root / '.env')

    def test_existing_private_directory_mode_is_preserved(self):
        private = self.root / 'status'
        private.mkdir(mode=0o750)
        io.task_record(self.root, self.root, self.root / '.env',
                       '2026-09-20T12:00:00Z', 'healthy')
        self.assertEqual(private.stat().st_mode & 0o777, 0o750)
        self.assertEqual((private / 'bootstrap.json').stat().st_mode & 0o777, 0o600)

    def test_process_deadline_includes_silent_child(self):
        start = time.monotonic()
        with self.assertRaises(io.Unavailable):
            io.run([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.1)
        self.assertLess(time.monotonic() - start, 2)

    def test_both_pipes_share_output_budget_and_errors_are_opaque(self):
        for code in ('print("x" * 300)', 'import sys; sys.stderr.write("SECRET" * 100)'):
            with self.subTest(code=code), self.assertRaises(io.Unavailable) as caught:
                io.run([sys.executable, '-c', code], limit=100)
            self.assertNotIn('SECRET', str(caught.exception))
        self.assertEqual(io.run([sys.executable, '-c', 'print("ok")']), 'ok\n')

    def test_process_exit_and_spawn_failures_are_classified(self):
        for code in (125, 126, 127):
            with self.subTest(code=code), self.assertRaises(io.Unsupported):
                io.run([sys.executable, '-c', f'raise SystemExit({code})'])
        with self.assertRaises(io.Unavailable) as caught:
            io.run([sys.executable, '-c', 'raise SystemExit(1)'])
        self.assertNotIsInstance(caught.exception, io.Unsupported)
        with self.assertRaises(io.Unsupported):
            io.run([str(self.root / 'missing-binary')])
        with self.assertRaises(io.Unsupported):
            io.run([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(bytes([255]))'])

    def test_private_json_rejects_duplicate_nonfinite_and_oversize(self):
        for text in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '1e999', '-1e999', 'x' * 65537):
            with self.subTest(text=text[:40]), self.assertRaises(io.Unavailable):
                io.read_json(text)

    def test_nonregular_and_oversize_file_reads_are_bounded(self):
        os.mkfifo(self.root / 'fifo')
        with self.assertRaises(io.Unavailable):
            io.read_file(self.root / 'fifo')
        (self.root / 'large').write_bytes(b'x' * 100)
        with self.assertRaises(io.Unavailable):
            io.read_file(self.root / 'large', 99)

    def test_missing_record_read_does_not_create_or_chmod_private_directory(self):
        with self.assertRaises(FileNotFoundError):
            io.read_task(self.root, self.root, self.root / '.env')
        self.assertFalse((self.root / 'status').exists())
        (self.root / 'status').mkdir(mode=0o750)
        with self.assertRaises(FileNotFoundError):
            io.read_task(self.root, self.root, self.root / '.env')
        self.assertEqual((self.root / 'status').stat().st_mode & 0o777, 0o750)

    def test_malformed_private_utf8_is_unavailable(self):
        io.task_record(self.root, self.root, self.root / '.env', '2026-09-20T12:00:00Z', 'healthy')
        (self.root / 'status/bootstrap.json').write_bytes(b'\xff')
        with self.assertRaises(io.Unavailable):
            io.read_task(self.root, self.root, self.root / '.env')
