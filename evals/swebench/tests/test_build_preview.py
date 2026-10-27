"""Offline build progress tests: actual subprocess output, no Docker or paid runtime."""
import codecs
import io
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from evals.swebench.build_preview import BuildPreview
from evals.swebench.runner import build_images
from evals.swebench.tests.test_runner import TASK, prepare_fixture


class Terminal(io.StringIO):
    def isatty(self):
        return True

    def fileno(self):
        return 2


class BuildPreviewTests(unittest.TestCase):
    def setUp(self):
        self.term = patch.dict(os.environ, {"TERM": "xterm"})
        self.term.start()
        self.addCleanup(self.term.stop)

    def invoke(self, run, script, stream, interrupt=False):
        processes = []
        timers = []

        class SyntheticDocker:
            def call(self, *args, **kwargs):
                # Keep the build call's real stdout/stderr file contract.
                if interrupt:
                    timer = threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGINT))
                    timers.append(timer)
                    timer.start()
                real_popen = subprocess.Popen
                def record(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    processes.append(process)
                    return process
                with patch("subprocess.Popen", side_effect=record):
                    return subprocess.run([sys.executable, "-c", script], check=True, **kwargs)

            def text(self, *args):
                return "sha256:synthetic"

        output = io.StringIO()
        try:
            with redirect_stdout(output), redirect_stderr(stream), patch(
                    "evals.swebench.runner.Docker", return_value=SyntheticDocker()):
                build_images(run, "linux/amd64")
        finally:
            for timer in timers:
                timer.cancel()
                timer.join()
            self.assertEqual(output.getvalue(), "")
            # subprocess.run kills on interruption; POSIX can report its death
            # asynchronously after the KeyboardInterrupt has unwound.
            for process in processes:
                process.wait(timeout=2)
                self.assertIsNotNone(process.returncode)
            self.assertFalse(any(t.name == "pepi-build-preview" for t in threading.enumerate()))

    def test_last_ten_lines_and_byte_complete_log_with_split_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            run = prepare_fixture(Path(directory))
            stream = Terminal()
            prefix = b"".join(f"old-{i}\n".encode() for i in range(3000))
            tail = "".join(f"last-{i}\n" for i in range(9)).encode() + "last-9: €".encode()
            script = (f"import os,time; os.write(1,{prefix!r}); os.write(2,{tail[:-1]!r}); "
                      f"time.sleep(.15); os.write(1,{tail[-1:]!r})")
            self.invoke(run, script, stream)
            self.assertEqual((run / f"build-{TASK['instance_id']}.log").read_bytes(), prefix + tail)
            frames = stream.getvalue().split("\x1b[13A")
            final = frames[-1]
            self.assertIn("[complete]", final)
            self.assertIn(run.name, final)
            self.assertIn(f"build-{TASK['instance_id']}.log", final)
            self.assertNotIn("old-", final)
            self.assertIn("last-9: €", final)
            log_rows = final.split("\r\x1b[2K")[4:]
            self.assertEqual(len(log_rows), 10)
            self.assertLessEqual(len(frames), 5)  # Timed updates, not per-log-line writes.

    def test_non_tty_has_only_escape_free_identity_and_no_log_spam(self):
        with tempfile.TemporaryDirectory() as directory:
            run = prepare_fixture(Path(directory))
            stream = io.StringIO()
            payload = b"\x1b[31mprivate-build-output\n" * 10000 + b"\xff"
            self.invoke(run, "import os; os.write(1,b'\\x1b[31mprivate-build-output\\n' * 10000 + b'\\xff')",
                        stream)
            text = stream.getvalue()
            self.assertEqual(text.count("\n"), 1)
            self.assertIn(TASK["instance_id"], text)
            self.assertIn(str(run), text)
            self.assertIn(".log", text)
            self.assertNotIn("\x1b", text)
            self.assertNotIn("private-build-output", text)
            self.assertEqual((run / f"build-{TASK['instance_id']}.log").read_bytes(), payload)

    def test_failed_build_finishes_preview_and_preserves_log(self):
        with tempfile.TemporaryDirectory() as directory:
            run = prepare_fixture(Path(directory))
            stream = Terminal()
            with self.assertRaisesRegex(RuntimeError, "exit 7.*build-owner__repo-1.log"):
                self.invoke(run, "import os; os.write(2,b'failed details\\n'); raise SystemExit(7)", stream)
            self.assertIn("[failed]", stream.getvalue())
            self.assertTrue(stream.getvalue().endswith("\n"))
            self.assertEqual((run / f"build-{TASK['instance_id']}.log").read_bytes(), b"failed details\n")
            self.assertFalse((run / "builds.json").exists())

    def test_interruption_reaps_subprocess_and_joins_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            run = prepare_fixture(Path(directory))
            stream = Terminal()
            with self.assertRaises(KeyboardInterrupt):
                self.invoke(run, "import os,time; os.write(1,b'before interrupt\\n'); time.sleep(30)",
                            stream, interrupt=True)
            self.assertIn("[interrupted]", stream.getvalue())
            self.assertTrue(stream.getvalue().endswith("\n"))
            self.assertEqual((run / f"build-{TASK['instance_id']}.log").read_bytes(), b"before interrupt\n")
            self.assertFalse((run / "builds.json").exists())

    def test_partial_lines_crlf_and_memory_bound(self):
        preview = BuildPreview(Path("run"), "task", Path("unused"))
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        for chunk in [b"zero\r", b"\n", b"\n", b"one\r", b"two\n", b"\xe2", b"\x82", b"\xac", b"\xff"]:
            preview._feed(decoder.decode(chunk))
        self.assertEqual(list(preview.lines), ["zero", "", "one", "two"])
        self.assertEqual(preview.partial, "€�")
        preview._feed("x" * 1000000)
        self.assertEqual(len(preview.partial), preview.max_columns)
        preview._feed("\n" + "line\n" * 10000)
        self.assertEqual(len(preview.lines), 10)

    def test_width_controls_and_small_terminal(self):
        stream = Terminal()
        with redirect_stderr(stream):
            preview = BuildPreview(Path("selected-run"), "task\x1b[2J", Path("build.log"))
        preview._feed("\x1b[31m" + "界" * 100 + "\n" + "x" * 100 + "\t\x00\n")
        with patch("os.get_terminal_size", return_value=os.terminal_size((20, 8))):
            preview._render("building")
        rows = stream.getvalue().split("\r\x1b[2K")[1:]
        self.assertEqual(len(rows), 7)  # Three context rows plus four available log rows.
        for row in rows:
            content = row.rstrip("\n")
            self.assertNotIn("\x1b", content)
            self.assertNotIn("\t", content)
            self.assertLessEqual(sum(2 if char == "界" else 1 for char in content), 19)
        self.assertEqual(BuildPreview._clip("e\u0301界x", 3), "e\u0301界")

    def test_rapid_output_rendering_is_throttled(self):
        with tempfile.TemporaryDirectory() as directory:
            run = prepare_fixture(Path(directory))
            stream = Terminal()
            script = "import os,time\nfor i in range(300):\n os.write(1, f'{i}\\n'.encode()); time.sleep(.001)"
            self.invoke(run, script, stream)
            self.assertLessEqual(stream.getvalue().count("Build "), 8)
            self.assertIn("299\n", stream.getvalue().split("\x1b[13A")[-1])

    def test_exit_drains_fixed_snapshot_even_if_a_writer_outlives_docker(self):
        class GrowingLog:
            position = 0
            reads = 0
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def fileno(self):
                return 99
            def tell(self):
                return self.position
            def read(self, count):
                self.reads += 1
                if self.reads > 3:
                    raise AssertionError("Reader followed a surviving writer indefinitely")
                self.position += count
                return b"x" * count

        preview = BuildPreview(Path("run"), "task", Path("unused"))
        preview.stop.set()
        log = GrowingLog()
        with patch.object(Path, "open", return_value=log), patch(
                "os.fstat", return_value=type("Stat", (), {"st_size": 65538})()):
            preview._follow()
        self.assertEqual(log.position, 65538)
        self.assertEqual(log.reads, 3)

    def test_exception_stops_reader_and_closed_stderr_does_not_mask_build(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "build.log"
            path.write_bytes(b"details")
            stream = Terminal()
            with redirect_stderr(stream):
                preview = BuildPreview(Path(directory), "task", path)
                with self.assertRaisesRegex(OSError, "synthetic"):
                    with preview:
                        stream.close()
                        raise OSError("synthetic")
            self.assertFalse(preview.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
