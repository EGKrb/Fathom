"""The link between Binary Ninja and Fathom, without Binary Ninja: what the plugin's commands do, written so that
it can be tested against a real Fathom (tests/test_binja.py).

Fathom is reached through pybridge (its Python client, x64dbg_bridge.py): `pybridge.start` in Fathom, then
Link(host, port). Addresses are mapped between the view (Binary Ninja: the file, at its image base) and the
debuggee (the module, where it is loaded): the same when the view was opened with Fathom's `binja` command
(the module read from memory, at its load address), shifted by the difference of the bases otherwise.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import Counter
from typing import Callable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
# pybridge's client: next to this file in a release, in pybridge/python in the repository, or FATHOM_PYBRIDGE
for candidate in (HERE, os.path.join(HERE, "..", "..", "pybridge", "python"), os.environ.get("FATHOM_PYBRIDGE", "")):
    if candidate and os.path.isfile(os.path.join(candidate, "x64dbg_bridge.py")):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
        break

DEFAULT_PORT = 27041


def module_name(path: str) -> str:
    """The module a view comes from: the file name, without the folder Fathom's binja command adds."""
    return os.path.basename(path)


class Link:
    """A connection to Fathom (pybridge) and the address mapping of one module."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, debugger=None):
        if debugger is None:
            from x64dbg_bridge import Debugger
            debugger = Debugger(host, port)
        self.debugger = debugger
        self.delta = 0  # debuggee address - view address
        self.module = ""

    # ------------------------------------------------------------------- mapping
    def attach_view(self, path: str, view_start: int) -> int:
        """Maps a view (its file and its start address) to the module of the debuggee with the same name."""
        self.module = module_name(path)
        stem = os.path.splitext(self.module)[0]
        base = 0
        for name in (self.module, stem):
            try:
                base = self.debugger.eval(f"mod.base({name})")
            except Exception:
                base = 0
            if base:
                break
        if not base:
            raise LookupError(f"the debuggee has no module {self.module}")
        self.delta = base - view_start
        return base

    def to_debuggee(self, address: int) -> int:
        return address + self.delta

    def to_view(self, address: int) -> int:
        return address - self.delta

    # ------------------------------------------------------------------ actions
    def cip(self) -> int:
        return self.debugger.reg("cip")

    def goto(self, view_address: int) -> None:
        """Shows the address in Fathom's disassembly."""
        self.debugger.cmd(f"disasm {self.to_debuggee(view_address):#x}")

    def breakpoint(self, view_address: int, enable: bool = True) -> None:
        address = self.to_debuggee(view_address)
        self.debugger.cmd(f"bp {address:#x}" if enable else f"bc {address:#x}")

    def run_to(self, view_address: int) -> None:
        """Runs the debuggee to the address (a single-shot breakpoint)."""
        self.debugger.cmd(f"run {self.to_debuggee(view_address):#x}")

    def comment(self, view_address: int, text: str) -> None:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        self.debugger.cmd(f'cmt {self.to_debuggee(view_address):#x}, "{escaped}"')


class Follower:
    """Follows the debuggee: calls navigate(view address) each time the instruction pointer moves inside the
    mapped module (a background thread polling Fathom)."""

    def __init__(self, link: Link, view_start: int, size: int, navigate: Callable[[int], None], interval: float = 0.3):
        self.link = link
        self.view_start = view_start
        self.size = size
        self.navigate = navigate
        self.interval = interval
        self.moves = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fathom-follow", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def step(self, last: Optional[int]) -> Optional[int]:
        """One poll: navigates if cip moved inside the module; returns cip (None if Fathom did not answer)."""
        try:
            cip = self.link.cip()
        except Exception:
            return last
        if cip != last:
            view = self.link.to_view(cip)
            if 0 <= view - self.view_start < self.size:
                self.moves += 1
                self.navigate(view)
        return cip

    def _run(self) -> None:
        last = None
        while not self._stop.is_set():
            last = self.step(last)
            time.sleep(self.interval)


def read_trace(path: str) -> Counter:
    """The executions per address of a trace exported by Fathom (TraceExport ..., json)."""
    with open(path, encoding="utf-8") as f:
        trace = json.load(f)
    counts: Counter = Counter()
    for ins in trace.get("instructions", []):
        address = ins.get("address")
        if isinstance(address, str):
            address = int(address, 16)
        if isinstance(address, int):
            counts[address] += 1
    return counts
