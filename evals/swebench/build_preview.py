"""Bounded stderr preview of build logs only; never used for paid agent runtime."""
from __future__ import annotations

import codecs
import os
import sys
import threading
import time
import unicodedata
from collections import deque
from pathlib import Path


class BuildPreview:
    """Tail the existing byte-complete log without changing Docker.call's contract."""

    interval = 0.1
    # Bound memory even if a build emits a single enormous unterminated line.
    max_columns = 4096

    def __init__(self, run: Path, task: str, log_path: Path):
        self.run, self.task, self.log_path = run, task, log_path
        self.stream = sys.stderr
        self.interactive = self.stream.isatty() and os.environ.get("TERM") != "dumb"
        self.lines = deque(maxlen=10)
        self.partial = ""
        self.after_cr = False
        self.rows = 0
        self.stop = threading.Event()
        self.thread = None

    def _write(self, text: str) -> None:
        if self.stream is not None:
            try:
                self.stream.write(text)
                self.stream.flush()
            except (OSError, ValueError):
                # A closed progress sink must not stop the build or mask its failure.
                self.stream = None

    @staticmethod
    def _clip(text: str, width: int) -> str:
        result = []
        used = 0
        for char in text:
            if not char.isprintable():
                char = "?"
            columns = (0 if unicodedata.combining(char) else
                       2 if unicodedata.east_asian_width(char) in ("W", "F") else 1)
            if used + columns > width:
                break
            result.append(char)
            used += columns
        return "".join(result)

    def _feed(self, text: str) -> None:
        # Treat CR progress output as logical lines, and CRLF as a single boundary,
        # including when the CR and LF arrive in different reads.
        if self.after_cr and text.startswith("\n"):
            text = text[1:]
            self.after_cr = False
        if not text:
            return
        self.after_cr = text.endswith("\r")
        parts = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        for index, part in enumerate(parts):
            self.partial = (self.partial + part)[:self.max_columns]
            if index < len(parts) - 1:
                self.lines.append(self.partial)
                self.partial = ""

    def _render(self, status: str) -> None:
        if self.stream is None:
            return
        try:
            size = os.get_terminal_size(self.stream.fileno())
            columns, height = size.columns, size.lines
        except (OSError, ValueError, AttributeError):
            columns, height = 80, 24
        width = max(0, min(self.max_columns, columns - 1))  # Never trigger autowrap.
        count = min(10, max(0, height - 4))
        tail = list(self.lines) + ([self.partial] if self.partial else [])
        tail = tail[-count:] if count else []
        frame = [f"Build {self.task} [{status}]", f"Run: {self.run.name} ({self.run})",
                 f"Log: {self.log_path.name} ({self.log_path})", *tail,
                 *([""] * (count - len(tail)))]
        prefix = f"\x1b[{self.rows}A" if self.rows else ""
        self._write(prefix + "".join("\r\x1b[2K" + self._clip(line, width) + "\n"
                                    for line in frame))
        self.rows = len(frame)

    def _follow(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        last_render = time.monotonic()
        dirty = False
        with self.log_path.open("rb") as log:
            final_size = None
            while True:
                if self.stop.is_set() and final_size is None:
                    # Drain a fixed snapshot on exit, even if an inherited writer
                    # outlives a failed/interrupted Docker client.
                    final_size = os.fstat(log.fileno()).st_size
                remaining = 65536 if final_size is None else max(0, final_size - log.tell())
                chunk = log.read(min(65536, remaining))
                if chunk:
                    self._feed(decoder.decode(chunk))
                    dirty = True
                now = time.monotonic()
                if dirty and now - last_render >= self.interval:
                    self._render("building")
                    dirty = False
                    last_render = now
                if not chunk:
                    if self.stop.is_set():
                        self._feed(decoder.decode(b"", final=True))
                        return
                    self.stop.wait(self.interval)

    def __enter__(self):
        if self.interactive:
            self._render("building")
            self.thread = threading.Thread(target=self._follow, name="pepi-build-preview")
            self.thread.start()
        else:
            # Identity only in CI; no log spam or terminal escapes (even in paths).
            identity = f"Build {self.task}; run: {self.run}; log: {self.log_path}"
            self._write(self._clip(identity, len(identity) * 2) + "\n")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
            status = ("interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else
                      "failed" if exc_type is not None else "complete")
            self._render(status)
        return False
