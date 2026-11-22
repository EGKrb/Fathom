# Fathom addition to dumpulator: the child processes of the emulated program, which run at the same
# time as it. Boost Software License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# CreateProcess (kernelbase!CreateProcessInternalW, where every variant ends, ShellExecute included)
# starts the child in an emulator of its own: a second Dumpulator loaded from the same dump (the
# same system DLLs, already initialized) or, for a child of the other architecture, from a dump of
# a small host program of that architecture (emulator_host, dumped once at its entry point by
# Fathom's headless of that architecture: only the system's loader runs natively). The child's
# executable is mapped from the disk, its imports bound (API sets through the ApiSetMap of the PEB;
# a DLL the dump lacks is loaded by the child's own loader, DllMain included), its command line,
# image path, process and thread ids set; its main thread starts at
# ntdll!RtlUserThreadStart(entry point, PEB), as natively.
#
# The emulated processes of a run (a World) run at the same time: in turn, by slices of 20,000
# instructions, and whenever one waits for something another process may do (a child's exit, a
# pipe, an event, a section), the others run until it can go on. Their virtual clocks move
# together; when every process waits, they jump to the first timeout. A child keeps running after
# its parent has exited. The handles of a child work from its parent: memory (NtAllocate / Free /
# Protect / Query / Read / WriteVirtualMemory), NtMapViewOfSection (an image or a section),
# NtUnmapViewOfSection, NtQueryInformationProcess / Thread, NtGet / SetContextThread, NtResume /
# SuspendThread, NtTerminateProcess, NtCreateThreadEx (a remote thread), NtDuplicateObject (ipc.py)
# and the waits. Handles are inherited (bInheritHandles) and the standard handles of STARTUPINFO
# (STARTF_USESTDHANDLES) become the child's: a pipe to its output works as natively.

import os
import random
import struct
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from typing import Dict, List, Optional

from .apihooks import read_string, read_wstring, set_last_error
from .handles import AbstractObject

STATUS_SUCCESS = 0
STATUS_PENDING = 0x103
STATUS_INVALID_INFO_CLASS = 0xC0000003
STATUS_INVALID_HANDLE = 0xC0000008
STATUS_INVALID_PARAMETER = 0xC000000D
STATUS_NOT_SUPPORTED = 0xC00000BB
STATUS_PARTIAL_COPY = 0x8000000D
STATUS_PROCESS_IS_TERMINATING = 0xC000010A
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
CREATE_SUSPENDED = 0x4
MAX_DEPTH = 6
SLICE = 20000


# ---------------------------------------------------------------------------------- objects

@dataclass
class ChildProcess(AbstractObject):
    """A process handle of a child."""
    child: "Child"

    def fathom_signaled(self) -> bool:
        return self.child.state == "exited"

    def __str__(self):
        return f"ChildProcess(pid={self.child.pid}, {self.child.state})"


@dataclass
class ChildThread(AbstractObject):
    """A thread handle of a child's thread."""
    child: "Child"
    tid: int

    def thread(self):
        dp = self.child.dp
        return dp.scheduler.threads.get(self.tid) if dp is not None else None

    def fathom_signaled(self) -> bool:
        if self.child.state == "exited":
            return True
        thread = self.thread()
        return thread is not None and thread.state == "terminated"

    def __str__(self):
        return f"ChildThread(pid={self.child.pid}, tid={self.tid})"


class Child:
    def __init__(self, parent_dp, pid: int, tid: int, path: str, command_line: str):
        self.parent_dp = parent_dp
        self.pid = pid
        self.tid = tid
        self.path = path
        self.command_line = command_line
        self.dp = None                  # its Dumpulator (None: simulated)
        self.state = "created"          # created, suspended, running, exited, stopped
        self.exit_code: Optional[int] = None
        self.note = ""
        self.suspend_count = 0
        self.peb = 0
        self.entry = 0
        self.executed = 0
        self.arch = ""

    def summary(self) -> dict:
        info = dict(pid=self.pid, tid=self.tid, path=self.path, command_line=self.command_line, arch=self.arch,
                    state=self.state, exit_code=self.exit_code, instructions=self.executed)
        if self.note:
            info["note"] = self.note
        dp = self.dp
        if dp is not None:
            missing = getattr(dp, "_fathom_unimplemented", None)
            if missing:
                info["not_implemented"] = dict(missing)
            network = getattr(dp, "network", None)
            if network is not None and network.events:
                info["network"] = list(network.events)
            gui = getattr(dp, "gui", None)
            if gui is not None and gui.events:
                info["gui"] = list(gui.events)
            processes = getattr(dp, "processes", None)
            if processes is not None and processes.children:
                info["children"] = [c.summary() for c in processes.children]
            if dp.scheduler.stop_reason:
                info["stop_reason"] = dp.scheduler.stop_reason
        return info


# ---------------------------------------------------------------------------------- the world

class World:
    """The emulated processes of a run, which run at the same time."""

    def __init__(self, root):
        self.root = root
        self.children: List[Child] = []     # every child of every process
        self.active = [root]                # the processes whose emulation is in progress (nested)
        self.waiting = set()                # active processes with nothing to run (they wait for the others)
        self.since_others = 0
        self.root_finished = False
        self.instruction_limit = 50_000_000
        self.host_dumps: Dict[str, str] = {}
        self.ids = [((root.process_id // 4) + 0x100) * 4]
        self.sections = []                  # the shared sections mapped (ipc.py)
        self.rng = random.Random(1)         # the slices of processes that share memory

    def next_id(self) -> int:
        self.ids[0] += 4
        return self.ids[0]

    def live(self):
        processes = [] if self.root_finished else [self.root]
        return processes + [c.dp for c in self.children if c.dp is not None and c.state in ("running", "suspended")]

    def now(self) -> int:
        return max(p.scheduler.interrupt_time for p in self.live()) if self.live() else 0

    def signal_all(self):
        for p in self.live():
            p.scheduler.signal()

    def can_pause(self, dp) -> bool:
        return dp is not self.root

    def fine(self) -> bool:
        """Processes that run and share memory (a section mapped in two of them): their instructions
        interleave like those of threads on processors (short slices), so a race between them on
        that memory happens as natively; otherwise the processes run by slices of SLICE."""
        if not any(c.state == "running" for c in self.children):
            return False
        live = {id(p) for p in self.live()}
        for section in self.sections:
            if len({id(p) for p in section.mappers if id(p) in live}) >= 2:
                return True
        return False

    def _running(self, exclude) -> List[Child]:
        return [c for c in self.children if c.state == "running" and c.dp is not None and c.dp is not exclude
                and c.dp not in self.active]

    def slice_done(self, dp, executed: int):
        """A slice of dp ended: when dp leads (it is not run by another process), the others get
        their slices every SLICE instructions."""
        if not self.active or dp is not self.active[0]:
            return
        self.since_others += executed
        if (self.since_others >= SLICE or self.fine()) and self._running(dp):
            self.since_others = 0
            self.step_others(dp)

    def step_others(self, me) -> bool:
        """A slice for each other running process; True when one of them ran."""
        ran = False
        fine = self.fine()
        for child in self._running(me):
            if self.run_child(child, self.rng.randint(40, 400) if fine else SLICE):
                ran = True
        return ran

    def wait_for_others(self, me) -> bool:
        """me has nothing to run: the other processes run, or the clocks jump to the first timeout.
        False when nothing can go on (me then stops, or pauses for a child)."""
        self.waiting.add(me)
        try:
            if self.step_others(me):
                return True
            # what the others did may have ended a wait (here or in a process that runs this one)
            self.signal_all()
            if me.scheduler.runnable():
                return True
            if any(p not in self.waiting or p.scheduler.runnable() for p in self.active if p is not me):
                return False  # a process that runs this one can go on: the jump is not for now
            deadlines = [d for d in (p.scheduler.next_deadline() for p in self.live()) if d is not None]
            if not deadlines:
                return False
            when = min(deadlines)
            for p in self.live():
                p.scheduler.advance_to(when)
            return True
        finally:
            self.waiting.discard(me)

    def run_child(self, child: Child, count: int) -> bool:
        """A slice of count instructions of a child; True when it ran."""
        dp = child.dp
        scheduler = dp.scheduler
        if child.state != "running":
            return False
        scheduler.advance_to(self.now())
        if scheduler.paused or dp.stopped:
            if not scheduler.runnable():
                return False
            from .dumpulator import UnicornExceptionInfo
            nxt = scheduler.on_cpu[0]
            scheduler.paused = False
            dp.stopped = False
            dp._exception = UnicornExceptionInfo()
            scheduler._make_current(nxt)
            dp._uc.context_restore(nxt.context)
        self.active.append(dp)
        scheduler.budget = count
        try:
            dp.start(dp.regs.cip)
        except Exception as e:
            traceback.print_exc()
            child.note = f"the emulation of the child failed: {type(e).__name__}: {e}"
            child.state = "stopped"
        finally:
            self.active.pop()
            remaining = scheduler.budget if scheduler.budget is not None else 0
            scheduler.budget = None
            scheduler.yield_requested = False
        child.executed += max(0, count - max(0, remaining))
        if child.state == "running":
            ended = dp.exit_code is not None or all(t.state in ("terminated", "frozen") for t in scheduler.threads.values())
            if ended:
                self.exited(child, dp.exit_code if dp.exit_code is not None else 0)
            elif dp.stopped and not scheduler.paused:
                child.state = "stopped"
                child.note = child.note or scheduler.stop_reason or "the emulation of the child stopped"
            elif child.executed >= self.instruction_limit:
                child.state = "stopped"
                child.note = f"stopped after {child.executed} instructions (child limit)"
        if child.state == "stopped":
            child.parent_dp.processes._record("child_stopped", pid=child.pid, note=child.note)
        self.signal_all()
        return True

    def exited(self, child: Child, exit_code: int):
        child.state = "exited"
        child.exit_code = exit_code & 0xFFFFFFFF
        child.parent_dp.processes._record("child_exited", pid=child.pid, exit_code=child.exit_code)
        if child.dp is not None:
            close_all_handles(child.dp)
            gui = getattr(child.dp, "gui", None)
            if gui is not None:
                gui.process_exited()
        self.signal_all()

    def finish(self):
        """The root process ended: its children keep running until they end too (or wait forever)."""
        self.root_finished = True
        close_all_handles(self.root)
        self.active = []
        while True:
            if any(self.run_child(c, SLICE) for c in self._running(None)):
                continue
            deadlines = [d for d in (p.scheduler.next_deadline() for p in self.live()) if d is not None]
            if not deadlines:
                break
            when = min(deadlines)
            for p in self.live():
                p.scheduler.advance_to(when)
        for child in self.children:
            if child.state in ("running", "suspended"):
                child.note = child.note or ("never resumed" if child.state == "suspended" else
                                            "waits forever: " + (child.dp.scheduler.stop_reason or "every thread waits"))

    # ------------------------------------------------------------------ dumps of other architectures
    def host_dump(self, arch: str) -> Optional[str]:
        """A dump of emulator_host of that architecture at its entry point (made once, then kept in
        %TEMP%\\Fathom\\emu), for the children of that architecture."""
        if arch in self.host_dumps:
            return self.host_dumps[arch]
        self.host_dumps[arch] = None
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        root = os.path.dirname(here)
        folder = "x64" if arch == "x64" else "x32"
        hosts = [os.path.join(here, "hosts", f"emulator_host_{folder}.exe"),
                 os.path.join(root, "bin", folder, "tests", "emulator_host.exe")]
        headlesses = [os.path.join(root, folder, "headless.exe"), os.path.join(root, "bin", folder, "headless.exe")]
        host = next((h for h in hosts if os.path.isfile(h)), None)
        headless = next((h for h in headlesses if os.path.isfile(h)), None)
        if host is None or headless is None:
            return None
        work = os.path.join(tempfile.gettempdir(), "Fathom", "emu")
        os.makedirs(work, exist_ok=True)
        stamp = f"{sys.getwindowsversion().build}_{int(os.path.getmtime(host))}" if hasattr(sys, "getwindowsversion") else "0"
        dump = os.path.join(work, f"host_{folder}_{stamp}.dmp")
        if not os.path.isfile(dump):
            userdir = os.path.join(work, "headless_" + folder)
            os.makedirs(userdir, exist_ok=True)
            with open(os.path.join(userdir, "headless.ini"), "w") as f:
                f.write("[Engine]\nDebugEngine=0\nNoConsoleWindow=1\n")
            script = dump + ".txt"
            with open(script, "w") as f:
                f.write("settingset Events, TlsCallbacks, 0\nsettingset Events, TlsCallbacksSystem, 0\n"
                        "settingset Events, EntryBreakpoint, 1\n"
                        f'init "{host}"\nrun\nminidump "{dump}"\nstop\n')
            try:
                subprocess.run([headless, "-testing", "-userdir", userdir, "-cf", script], cwd=os.path.dirname(headless),
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=180)
            except Exception:
                return None
            if not os.path.isfile(dump):
                return None
        self.host_dumps[arch] = dump
        return dump


def close_all_handles(dp):
    """A process ended: its handles close (the pipes it wrote to break for their readers)."""
    from .ipc import release_handle
    for value, obj in list(dp.handles._handles.items()):
        if getattr(obj, "fathom_release", None) is not None:
            dp.handles._handles.pop(value, None)
            release_handle(dp, obj)


def world(dp) -> World:
    if getattr(dp, "world", None) is None:
        dp.world = World(dp)
    return dp.world


# ---------------------------------------------------------------------------------- per process

class Processes:
    def __init__(self, dp, depth: int = 0):
        self.dp = dp
        self.depth = depth
        self.children: List[Child] = []
        self.events: List[dict] = []

    @property
    def instruction_limit(self):
        return world(self.dp).instruction_limit

    @instruction_limit.setter
    def instruction_limit(self, value):
        world(self.dp).instruction_limit = value

    def _record(self, kind: str, **values):
        values["kind"] = kind
        self.events.append(values)
        self.dp.info(f"[process] {kind}: " + ", ".join(f"{k}={v!r}" for k, v in values.items() if k != "kind"))

    # ------------------------------------------------------------------ CreateProcess
    def create_process_internal(self, dp, token, application, command_line, process_attributes, thread_attributes,
                                inherit, flags, environment, directory, startup_info, information, new_token):
        app = read_wstring(dp, application) if application else None
        line = read_wstring(dp, command_line) if command_line else None
        cwd = read_wstring(dp, directory) if directory else None
        std = None
        if startup_info:
            dwflags = dp.read_ulong(startup_info + (0x3C if dp._x64 else 0x2C))
            if dwflags & 0x100:  # STARTF_USESTDHANDLES
                offset = 0x50 if dp._x64 else 0x38
                std = [dp.read_ptr(startup_info + offset + i * dp.ptr_size()) for i in range(3)]
        child = self.spawn(app, line, cwd, int(flags), bool(inherit & 0xFF), std)
        if child is None:
            set_last_error(dp, 2)  # ERROR_FILE_NOT_FOUND
            return 0
        process_handle = dp.handles.new(ChildProcess(child))
        thread_handle = dp.handles.new(ChildThread(child, child.tid))
        if dp._x64:
            dp.write(information, struct.pack("<QQII", process_handle, thread_handle, child.pid, child.tid))
        else:
            dp.write(information, struct.pack("<IIII", process_handle, thread_handle, child.pid, child.tid))
        return 1

    def spawn(self, app: Optional[str], line: Optional[str], cwd: Optional[str], flags: int = 0, inherit: bool = False,
              std: Optional[list] = None) -> Optional[Child]:
        """Start a child (CreateProcess, ShellExecute); None when its executable is not found."""
        path = self._find_image(app, line, cwd)
        if path is None:
            self._record("create_process_failed", application=app, command_line=line)
            return None
        line = line if line is not None else f'"{path}"'
        w = world(self.dp)
        child = Child(self.dp, w.next_id(), w.next_id(), path, line)
        self.children.append(child)
        w.children.append(child)
        try:
            self._start(child)
        except Exception as e:
            traceback.print_exc()
            child.dp = None
            child.note = f"simulated: the child could not be loaded in the emulator ({type(e).__name__}: {e})"
        if child.dp is None:
            child.state = "exited"
            child.exit_code = 0
        else:
            if inherit:
                self._inherit(child.dp)
            if std is not None:
                self._standard_handles(child.dp, std)
            child.dp.scheduler.advance_to(w.now())
            if flags & CREATE_SUSPENDED:
                child.state = "suspended"
                child.suspend_count = 1
            else:
                child.state = "running"
        self._record("create_process", pid=child.pid, path=path, command_line=line, arch=child.arch,
                     flags=hex(flags & 0xFFFFFFFF), emulated=child.dp is not None, note=child.note or None)
        return child

    def _inherit(self, cdp):
        """The parent's inheritable handles in the child, at the same values (when free there)."""
        from .ipc import inheritable
        allowed = inheritable(self.dp)
        for value, obj in list(self.dp.handles._handles.items()):
            if obj is None or value in cdp.handles._handles or value not in allowed:
                continue
            inheritable(cdp).add(value)
            add = getattr(obj, "fathom_add_ref", None)
            if add is not None:
                add()
            cdp.handles._handles[value] = obj

    def _standard_handles(self, cdp, handles: list):
        """STARTF_USESTDHANDLES: the child's standard input, output and error are these objects (in
        the slots of the child's own standard handles, which its C runtime already knows)."""
        slots = [cdp.stdin_handle, cdp.stdout_handle, cdp.stderr_handle]
        for slot, value in zip(slots, handles):
            if not slot or not value:
                continue
            obj = self.dp.handles.get(value, None) if self.dp.handles.valid(value) else None
            if obj is None:
                continue
            add = getattr(obj, "fathom_add_ref", None)
            if add is not None:
                add()
            old = cdp.handles._handles.get(slot & ~3)
            cdp.handles._handles[slot & ~3] = obj
            release = getattr(old, "fathom_release", None)
            if release is not None:
                release()

    def _find_image(self, application: Optional[str], command_line: Optional[str], directory: Optional[str]) -> Optional[str]:
        candidates = []
        if application:
            candidates.append(application)
        elif command_line:
            text = command_line.strip()
            if text.startswith('"'):
                candidates.append(text[1:].split('"', 1)[0])
            else:
                words = text.split(" ")
                for count in range(1, len(words) + 1):  # as Windows: "C:\Program Files\x.exe" without quotes
                    candidates.append(" ".join(words[:count]))
        main = self.dp.modules.find(self.dp.modules.main)
        folders = [os.path.dirname(main.path) if main is not None and main.path else None, directory, os.getcwd(),
                   self._system_directory(), os.environ.get("SystemRoot", r"C:\Windows")]
        folders += os.environ.get("PATH", "").split(os.pathsep)
        for candidate in candidates:
            for name in (candidate, candidate + ".exe") if not candidate.lower().endswith(".exe") else (candidate,):
                if os.path.isabs(name):
                    if os.path.isfile(name):
                        return name
                    continue
                for folder in folders:
                    if folder and os.path.isfile(os.path.join(folder, name)):
                        return os.path.join(folder, name)
        return None

    def _system_directory(self) -> str:
        windows = os.environ.get("SystemRoot", r"C:\Windows")
        return os.path.join(windows, "System32" if self.dp._x64 else "SysWOW64")

    # ------------------------------------------------------------------ the child's emulator
    def _start(self, child: Child):
        parent = self.dp
        with open(child.path, "rb") as f:
            data = f.read()
        import pefile
        pe = pefile.PE(data=data, fast_load=True)
        bits = 64 if pe.OPTIONAL_HEADER.Magic == 0x20B else 32
        child.arch = "x64" if bits == 64 else "x86"
        if self.depth + 1 > MAX_DEPTH:
            child.note = f"simulated: more than {MAX_DEPTH} nested child processes"
            return
        if bits == (64 if parent._x64 else 32):
            dump = getattr(parent, "_fathom_dump_path", None)
        else:
            dump = world(parent).host_dump(child.arch)
            if dump is None:
                child.note = (f"simulated: a {bits}-bit child needs a dump of emulator_host of that architecture "
                              "(emulator\\hosts and Fathom's headless of that architecture)")
                return
        if not dump:
            child.note = "simulated: the dump of the parent is not known"
            return
        from .dumpulator import Dumpulator
        dp = Dumpulator(dump, quiet=True, world=world(parent))
        dp._quiet = parent._quiet
        dp.strict = parent.strict
        dp._fathom_console = _console(parent)       # the console is shared
        dp.processes.depth = self.depth + 1
        if getattr(parent, "network", None) is not None and getattr(dp, "network", None) is not None:
            dp.network.mode, dp.network.body = parent.network.mode, parent.network.body
        if getattr(parent, "gui", None) is not None and getattr(dp, "gui", None) is not None:
            dp.gui.drawing.directory = parent.gui.drawing.directory
        child.dp = dp
        self._set_identity(child, dp)
        loader = ImageLoader(dp)
        module = loader.load(child.path, data, executable=True)
        if loader.missing:
            child.note = "imports not found: " + ", ".join(loader.missing[:10])
        self._set_parameters(child, dp, module)
        child.entry = module.base + pe.OPTIONAL_HEADER.AddressOfEntryPoint
        self._initial_context(child, dp)

    def _set_identity(self, child: Child, dp):
        # the process and thread ids: the TEB (and the 64-bit TEB of a WoW64 process), the scheduler
        offsets = [(dp.teb, 0x40, 8) if dp._x64 else (dp.teb, 0x20, 4)]
        if not dp._x64 and dp.wow64:
            offsets.append((dp.teb - 0x2000, 0x40, 8))
        for teb, offset, size in offsets:
            fmt = "<QQ" if size == 8 else "<II"
            dp.write(teb + offset, struct.pack(fmt, child.pid, child.tid))
        scheduler = dp.scheduler
        thread = scheduler.threads.pop(dp.thread_id, None)
        if thread is not None:
            thread.tid = child.tid
            scheduler.threads[child.tid] = thread
        dp.thread_id = child.tid
        dp._fathom_initial_tid = child.tid
        dp.parent_process_id = self.dp.process_id
        dp.process_id = child.pid

    def _set_parameters(self, child: Child, dp, module):
        peb = dp.peb
        child.peb = peb
        dp.write_ptr(peb + (0x10 if dp._x64 else 0x8), module.base)  # ImageBaseAddress
        dp.modules.main = module.base
        wide_line = child.command_line.encode("utf-16-le") + b"\0\0"
        ansi_line = child.command_line.encode("mbcs", "replace") + b"\0"
        wide_path = child.path.encode("utf-16-le") + b"\0\0"
        line_w, line_a, path_w = dp.allocate(len(wide_line)), dp.allocate(len(ansi_line)), dp.allocate(len(wide_path))
        dp.write(line_w, wide_line)
        dp.write(line_a, ansi_line)
        dp.write(path_w, wide_path)
        parameters = dp.read_ptr(peb + (0x20 if dp._x64 else 0x10))

        def unicode_string(address, buffer, size):
            if dp._x64:
                dp.write(address, struct.pack("<HHIQ", size - 2, size, 0, buffer))
            else:
                dp.write(address, struct.pack("<HHI", size - 2, size, buffer))
        unicode_string(parameters + (0x60 if dp._x64 else 0x38), path_w, len(wide_path))
        unicode_string(parameters + (0x70 if dp._x64 else 0x40), line_w, len(wide_line))
        # the command line kernelbase keeps (GetCommandLineW/A: mov rax, [global]; ret)
        kernelbase = dp.modules.find("kernelbase.dll")
        for name, buffer in (("GetCommandLineW", line_w), ("GetCommandLineA", line_a)):
            export = kernelbase.find_export(name) if kernelbase is not None else None
            if export is None:
                continue
            code = bytes(dp.read(export.address, 8))
            if dp._x64 and code[:3] == bytes([0x48, 0x8B, 0x05]) and code[7] == 0xC3:
                dp.write_ptr(export.address + 7 + struct.unpack_from("<i", code, 3)[0], buffer)
            elif not dp._x64 and code[0] == 0xA1 and code[5] == 0xC3:
                dp.write_ptr(struct.unpack_from("<I", code, 1)[0], buffer)
            else:
                dp.apihooks.register("kernelbase.dll", name, 0, lambda dp, b=buffer: b)
        # the C runtime's copies, made when it was initialized: _acmdln, _wcmdln (msvcrt exports the
        # variables, ucrtbase the functions __p__acmdln / __p__wcmdln that return their address)
        for runtime in ("ucrtbase.dll", "msvcrt.dll"):
            found = dp.modules.find(runtime)
            if found is None:
                continue
            for name, buffer in (("_acmdln", line_a), ("_wcmdln", line_w)):
                variable = None
                export = found.find_export(name)
                if export is not None:
                    variable = export.address
                export = found.find_export("__p_" + name)
                if variable is None and export is not None:
                    code = bytes(dp.read(export.address, 8))
                    if dp._x64 and code[:3] == bytes([0x48, 0x8D, 0x05]) and code[7] == 0xC3:     # lea rax, [rip+x]; ret
                        variable = export.address + 7 + struct.unpack_from("<i", code, 3)[0]
                    elif not dp._x64 and code[0] == 0xB8 and code[5] == 0xC3:          # mov eax, x; ret
                        variable = struct.unpack_from("<I", code, 1)[0]
                if variable is not None:
                    dp.write_ptr(variable, buffer)

    def _initial_context(self, child: Child, dp):
        """The main thread at ntdll!RtlUserThreadStart(entry point, PEB), on the top of its stack."""
        ntdll = dp.modules["ntdll.dll"]
        user_start = ntdll.find_export("RtlUserThreadStart").address
        regs = dp.regs
        ptr = dp.ptr_size()
        stack_base = dp.read_ptr(dp.teb + ptr)
        if dp._x64:
            for name in ("rax", "rbx", "rsi", "rdi", "rbp", "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"):
                regs[name] = 0
            regs.rsp = ((stack_base - 0x100) & ~0xF) - 8
            regs.rcx = child.entry
            regs.rdx = child.peb
            regs.rip = user_start
        else:
            for name in ("ecx", "edx", "esi", "edi", "ebp"):
                regs[name] = 0
            regs.esp = ((stack_base - 0x100) & ~0xF) - 0x10
            regs.eax = child.entry
            regs.ebx = child.peb
            regs.eip = user_start
        regs.eflags = 0x202

    # ------------------------------------------------------------------ ShellExecute
    def shell_execute(self, dp, verb: Optional[str], file: Optional[str], parameters: Optional[str],
                      directory: Optional[str]):
        """(child or None, error): an executable or a batch file runs in the emulator; a document or a
        URL is recorded (the program associated with it is not started)."""
        file = (file or "").strip().strip('"')
        lower = file.lower()
        params = f" {parameters}" if parameters else ""
        if "://" in lower or lower.startswith(("mailto:", "ms-settings:")):
            self._record("shell_open", verb=verb, target=file, parameters=parameters)
            return None, 0
        extension = os.path.splitext(lower)[1]
        if extension in (".bat", ".cmd"):
            system = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
            child = self.spawn(system, f'"{system}" /c ""{file}"{params}"', directory)
        elif extension in (".exe", ".com", ".scr", "") :
            child = self.spawn(None, f'"{file}"{params}', directory)
        else:
            self._record("shell_open", verb=verb, target=file, parameters=parameters,
                         program=_association(extension))
            return None, 0
        if child is None:
            return None, 2
        self._record("shell_execute", verb=verb, file=file, parameters=parameters, pid=child.pid)
        return child, 0

    def shell_execute_simple(self, dp, hwnd, verb, file, parameters, directory, show, wide):
        child, error = self.shell_execute(dp, read_string(dp, verb, wide) if verb else None,
                                          read_string(dp, file, wide) if file else None,
                                          read_string(dp, parameters, wide) if parameters else None,
                                          read_string(dp, directory, wide) if directory else None)
        return error if error else 42  # an HINSTANCE greater than 32: success

    def shell_execute_ex(self, dp, info, wide):
        ptr = dp.ptr_size()
        offsets = (dict(mask=4, verb=0x10, file=0x18, params=0x20, dir=0x28, inst=0x38, process=0x68) if dp._x64 else
                   dict(mask=4, verb=0xC, file=0x10, params=0x14, dir=0x18, inst=0x20, process=0x38))
        mask = dp.read_ulong(info + offsets["mask"])
        text = {k: read_string(dp, dp.read_ptr(info + offsets[k]), wide) if dp.read_ptr(info + offsets[k]) else None
                for k in ("verb", "file", "params", "dir")}
        child, error = self.shell_execute(dp, text["verb"], text["file"], text["params"], text["dir"])
        if error:
            dp.write_ptr(info + offsets["inst"], error)
            set_last_error(dp, error)
            return 0
        dp.write_ptr(info + offsets["inst"], 42)
        if mask & 0x40:  # SEE_MASK_NOCLOSEPROCESS
            dp.write_ptr(info + offsets["process"], dp.handles.new(ChildProcess(child)) if child is not None else 0)
        return 1

    # ------------------------------------------------------------------ system calls on a child's handles
    def _child(self, handle) -> Optional[Child]:
        obj = self.dp.handles.get(int(handle), None) if self.dp.handles.valid(int(handle) & ~3) else None
        if isinstance(obj, (ChildProcess, ChildThread)):
            return obj.child
        return None

    def _thread_object(self, handle) -> Optional[ChildThread]:
        obj = self.dp.handles.get(int(handle), None) if self.dp.handles.valid(int(handle) & ~3) else None
        return obj if isinstance(obj, ChildThread) else None

    def syscall(self, name: str, args: list):
        """A system call of the parent: handled here when its process or thread handle is a child's
        (the status), else None."""
        handler = _REMOTE.get(name[2:])
        if handler is None:
            return None
        try:
            return handler(self, *args)
        except Exception as e:
            traceback.print_exc()
            self.dp.error(f"{name} on a child process: {type(e).__name__}: {e}")
            return 0xC0000001  # STATUS_UNSUCCESSFUL

    def _live(self, child: Child):
        """The child's emulator, or None when the child has no memory any more (simulated, ended)."""
        return child.dp if child.dp is not None and child.state not in ("exited",) else None

    def allocate(self, process, base_ptr, zero_bits, size_ptr, allocation_type, protect):
        child = self._child(process)
        if child is None:
            return None
        cdp = self._live(child)
        if cdp is None:
            return STATUS_PROCESS_IS_TERMINATING
        from .memory import MemoryProtect, PAGE_SIZE
        dp = self.dp
        base = dp.read_ptr(base_ptr.ptr) & ~0xFFF
        size = (dp.read_ptr(size_ptr.ptr) + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1)
        kind = int(allocation_type) & 0x3000
        protect = MemoryProtect(int(protect))
        if base == 0:
            base = cdp.memory.find_free(size)
        if kind & 0x2000:  # MEM_RESERVE
            cdp.memory.reserve(base, size, protect)
        if kind & 0x1000:  # MEM_COMMIT
            cdp.memory.commit(base, size, protect)
        dp.write_ptr(base_ptr.ptr, base)
        dp.write_ptr(size_ptr.ptr, size)
        self._record("remote_allocate", pid=child.pid, address=hex(base), size=size, protect=str(protect))
        return STATUS_SUCCESS

    def free(self, process, base_ptr, size_ptr, free_type):
        child = self._child(process)
        if child is None:
            return None
        cdp = self._live(child)
        if cdp is None:
            return STATUS_PROCESS_IS_TERMINATING
        base = self.dp.read_ptr(base_ptr.ptr) & ~0xFFF
        if int(free_type) & 0x8000:  # MEM_RELEASE
            cdp.memory.release(base)
        else:
            size = (self.dp.read_ptr(size_ptr.ptr) + 0xFFF) & ~0xFFF
            cdp.memory.decommit(base, size)
        return STATUS_SUCCESS

    def protect(self, process, base_ptr, size_ptr, new_protect, old_ptr):
        child = self._child(process)
        if child is None:
            return None
        cdp = self._live(child)
        if cdp is None:
            return STATUS_PROCESS_IS_TERMINATING
        from .memory import MemoryProtect
        dp = self.dp
        address = dp.read_ptr(base_ptr.ptr)
        base = address & ~0xFFF
        size = ((address + dp.read_ptr(size_ptr.ptr) + 0xFFF) & ~0xFFF) - base
        old = cdp.memory.protect(base, size, MemoryProtect(int(new_protect)))
        dp.write_ptr(base_ptr.ptr, base)
        dp.write_ptr(size_ptr.ptr, size)
        if old_ptr.ptr:
            dp.write_ulong(old_ptr.ptr, _value(old))
        return STATUS_SUCCESS

    def query(self, process, address, information_class, information, length, return_length):
        child = self._child(process)
        if child is None:
            return None
        cdp = self._live(child)
        if cdp is None:
            return STATUS_PROCESS_IS_TERMINATING
        # MemoryBasicInformation, MemoryPrivilegedBasicInformation (WriteProcessMemory), MemoryBasicInformationCapped
        if int(information_class) not in (0, 8, 10):
            return STATUS_INVALID_INFO_CLASS
        dp = self.dp
        try:
            info = cdp.memory.query(int(address.ptr if hasattr(address, "ptr") else address) & ~0xFFF)
        except Exception:
            return STATUS_INVALID_PARAMETER
        protect = _value(info.protect)
        allocation_protect = _value(info.allocation_protect)
        kind = _value(info.type)
        if dp._x64:
            data = struct.pack("<QQIHHQIIII", info.base, info.allocation_base, allocation_protect, 0, 0, info.region_size,
                               _value(info.state), protect, kind, 0)
        else:
            data = struct.pack("<IIIIIII", info.base, info.allocation_base, allocation_protect, info.region_size,
                               _value(info.state), protect, kind)
        pointer = int(information.ptr if hasattr(information, "ptr") else information)
        if int(length) < len(data):
            return STATUS_INFO_LENGTH_MISMATCH
        dp.write(pointer, data)
        if return_length.ptr:
            dp.write_ptr(return_length.ptr, len(data))
        return STATUS_SUCCESS

    def _copy(self, process, address, buffer, size, done_ptr, write: bool):
        child = self._child(process)
        if child is None:
            return None
        cdp = self._live(child)
        if cdp is None:
            return STATUS_PROCESS_IS_TERMINATING
        dp = self.dp
        remote = int(address.ptr if hasattr(address, "ptr") else address)
        local = int(buffer.ptr if hasattr(buffer, "ptr") else buffer)
        size = int(size)
        try:
            if write:
                cdp.write(remote, bytes(dp.read(local, size)))
            else:
                dp.write(local, bytes(cdp.read(remote, size)))
            status = STATUS_SUCCESS
        except Exception:
            size, status = 0, STATUS_PARTIAL_COPY
        if done_ptr.ptr:
            dp.write_ptr(done_ptr.ptr, size)
        self._record("remote_write" if write else "remote_read", pid=child.pid, address=hex(remote), size=size)
        return status

    def write(self, process, address, buffer, size, done_ptr):
        return self._copy(process, address, buffer, size, done_ptr, True)

    def read(self, process, address, buffer, size, done_ptr):
        return self._copy(process, address, buffer, size, done_ptr, False)

    def query_process(self, process, information_class, information, length, return_length):
        child = self._child(process)
        if child is None or isinstance(self._thread_object(process), ChildThread):
            return None
        dp = self.dp
        klass = int(information_class)
        pointer = int(information.ptr if hasattr(information, "ptr") else information)
        if klass == 0:  # ProcessBasicInformation
            status = child.exit_code if child.state == "exited" else STATUS_PENDING
            if dp._x64:
                data = struct.pack("<IIQQIIQQ", status, 0, child.peb, 1, 8, 0, child.pid, self.dp.process_id)
            else:
                # a 64-bit child's PEB is out of reach of a 32-bit process: 0, as natively
                peb = child.peb if child.peb <= 0xFFFFFFFF else 0
                data = struct.pack("<IIIIII", status, peb, 1, 8, child.pid, self.dp.process_id)
        elif klass == 26:  # ProcessWow64Information: the PEB32 of a 32-bit child of a 64-bit process
            wow = child.peb if (dp._x64 and child.arch == "x86") else 0
            data = struct.pack("<Q" if dp._x64 else "<I", wow)
        elif klass == 27 or klass == 43:  # ProcessImageFileName (NT path) / ProcessImageFileNameWin32
            text = child.path.encode("utf-16-le")
            header = 16 if dp._x64 else 8
            data = (struct.pack("<HHIQ", len(text), len(text) + 2, 0, pointer + header) if dp._x64
                    else struct.pack("<HHI", len(text), len(text) + 2, pointer + header)) + text + b"\0\0"
        else:
            return STATUS_INVALID_INFO_CLASS
        if int(length) < len(data):
            if return_length.ptr:
                dp.write_ulong(return_length.ptr, len(data))
            return STATUS_INFO_LENGTH_MISMATCH
        dp.write(pointer, data)
        if return_length.ptr:
            dp.write_ulong(return_length.ptr, len(data))
        return STATUS_SUCCESS

    def query_thread(self, thread, information_class, information, length, return_length):
        obj = self._thread_object(thread)
        if obj is None:
            return None
        dp = self.dp
        child = obj.child
        if int(information_class) != 0:  # ThreadBasicInformation
            return STATUS_INVALID_INFO_CLASS
        emulated = obj.thread()
        if child.state == "exited":
            status = child.exit_code
        elif emulated is not None and emulated.state == "terminated":
            status = emulated.exit_code
        else:
            status = STATUS_PENDING
        teb = emulated.teb if emulated is not None else 0
        if dp._x64:
            data = struct.pack("<IIQQQQII", status, 0, teb, child.pid, obj.tid, 1, 8, 8)
        else:
            data = struct.pack("<IIIIIII", status, teb, child.pid, obj.tid, 1, 8, 8)
        if int(length) < len(data):
            return STATUS_INFO_LENGTH_MISMATCH
        dp.write(int(information.ptr if hasattr(information, "ptr") else information), data)
        if return_length.ptr:
            dp.write_ulong(return_length.ptr, len(data))
        return STATUS_SUCCESS

    def resume(self, thread, previous_ptr):
        obj = self._thread_object(thread)
        if obj is None:
            return None
        child = obj.child
        previous = 0
        if child.dp is not None and obj.tid != child.tid:
            emulated = obj.thread()
            if emulated is not None:
                previous = child.dp.scheduler.resume(emulated)
        else:
            previous = child.suspend_count
            if child.suspend_count:
                child.suspend_count -= 1
        if previous_ptr.ptr:
            self.dp.write_ulong(previous_ptr.ptr, previous)
        if child.state == "suspended" and child.suspend_count == 0:
            child.state = "running"
            self._record("child_resumed", pid=child.pid)
        return STATUS_SUCCESS

    def suspend(self, thread, previous_ptr):
        obj = self._thread_object(thread)
        if obj is None:
            return None
        child = obj.child
        previous = child.suspend_count
        if child.state in ("suspended", "running") and obj.tid == child.tid:
            child.suspend_count += 1
            child.state = "suspended"
        if previous_ptr.ptr:
            self.dp.write_ulong(previous_ptr.ptr, previous)
        return STATUS_SUCCESS

    _X64_REGISTERS = [("rax", 0x78), ("rcx", 0x80), ("rdx", 0x88), ("rbx", 0x90), ("rbp", 0xA0), ("rsi", 0xA8),
                      ("rdi", 0xB0)] + [(f"r{i}", 0xB8 + 8 * (i - 8)) for i in range(8, 16)]
    _X64_CONTROL = [("rsp", 0x98), ("rip", 0xF8)]
    _X86_REGISTERS = [("edi", 0x9C), ("esi", 0xA0), ("ebx", 0xA4), ("edx", 0xA8), ("ecx", 0xAC), ("eax", 0xB0)]
    _X86_CONTROL = [("ebp", 0xB4), ("eip", 0xB8), ("esp", 0xC4)]

    def _context(self, thread, context, setting: bool):
        obj = self._thread_object(thread)
        if obj is None:
            return None
        child = obj.child
        cdp = self._live(child)
        if cdp is None:
            return STATUS_PROCESS_IS_TERMINATING
        emulated = obj.thread()
        if emulated is None:
            return STATUS_INVALID_HANDLE
        dp = self.dp
        pointer = context.ptr
        x64 = cdp._x64
        flags = dp.read_ulong(pointer + (0x30 if x64 else 0))
        integer, control = (self._X64_REGISTERS, self._X64_CONTROL) if x64 else (self._X86_REGISTERS, self._X86_CONTROL)
        size = 8 if x64 else 4
        fmt = "<Q" if x64 else "<I"
        groups = []
        if flags & 2:
            groups += integer
        if flags & 1:
            groups += control + [("eflags", 0x44 if x64 else 0xC0)]
        live = emulated is cdp.scheduler.current  # its registers are in unicorn now, else in its saved context
        from unicorn import x86_const
        for name, offset in groups:
            width = 4 if name == "eflags" else size
            if live:
                if setting:
                    cdp.regs[name] = struct.unpack("<I" if width == 4 else fmt, bytes(dp.read(pointer + offset, width)))[0]
                else:
                    dp.write(pointer + offset, struct.pack("<I" if width == 4 else fmt, cdp.regs[name] & ((1 << (8 * width)) - 1)))
            else:
                register = getattr(x86_const, "UC_X86_REG_" + name.upper())
                if setting:
                    emulated.context.reg_write(register, struct.unpack("<I" if width == 4 else fmt, bytes(dp.read(pointer + offset, width)))[0])
                else:
                    value = emulated.context.reg_read(register)
                    dp.write(pointer + offset, struct.pack("<I" if width == 4 else fmt, value & ((1 << (8 * width)) - 1)))
        if setting:
            self._record("remote_set_context", pid=child.pid, tid=obj.tid)
        return STATUS_SUCCESS

    def get_context(self, thread, context):
        return self._context(thread, context, False)

    def set_context(self, thread, context):
        return self._context(thread, context, True)

    def terminate(self, process, exit_status):
        child = self._child(process)
        if child is None or isinstance(self._thread_object(process), ChildThread):
            return None
        if child.state != "exited":
            self._record("child_terminated", pid=child.pid, exit_code=int(exit_status) & 0xFFFFFFFF)
            world(self.dp).exited(child, int(exit_status))
        return STATUS_SUCCESS

    def unmap(self, process, address):
        child = self._child(process)
        if child is None:
            return None
        cdp = self._live(child)
        if cdp is None:
            return STATUS_PROCESS_IS_TERMINATING
        base = int(address.ptr if hasattr(address, "ptr") else address)
        self._record("remote_unmap", pid=child.pid, address=hex(base))
        from .ipc import SectionView
        region = cdp.memory.find_region(base)
        if region is not None and isinstance(getattr(region, "info", None), SectionView):
            cdp.memory.release(region.start)
            return STATUS_SUCCESS
        from .fathom_nt import _unmap
        return _unmap(cdp, base)

    def map_view(self, section_handle, process, base_ptr, zero_bits, commit, offset_ptr, size_ptr, inherit,
                 allocation_type, protect):
        """An image mapped into a child (process hollowing); a shared section is ipc.py's."""
        child = self._child(process)
        if child is None:
            return None
        cdp = self._live(child)
        if cdp is None:
            return STATUS_PROCESS_IS_TERMINATING
        from .fathom_nt import ImageSectionObject
        section = self.dp.handles.get(int(section_handle), None) if self.dp.handles.valid(int(section_handle)) else None
        if not isinstance(section, ImageSectionObject):
            return None
        module = cdp.map_module(section.data, section.path, self.dp.read_ptr(base_ptr.ptr), False)
        self.dp.write_ptr(base_ptr.ptr, module.base)
        self.dp.write_ptr(size_ptr.ptr, module.size)
        self._record("remote_map_image", pid=child.pid, path=section.path, address=hex(module.base))
        return STATUS_SUCCESS

    def create_thread(self, thread_ptr, access, attributes, process, start, argument, flags, zero_bits, stack_size,
                      maximum_stack_size, attribute_list):
        child = self._child(process)
        if child is None:
            return None
        cdp = self._live(child)
        if cdp is None or child.state not in ("suspended", "running"):
            return STATUS_PROCESS_IS_TERMINATING
        suspended = bool(int(flags) & 1)
        start_address = int(start.ptr if hasattr(start, "ptr") else start)
        emulated = cdp.scheduler.create_thread(start_address, int(argument.ptr if hasattr(argument, "ptr") else argument),
                                               suspended, int(maximum_stack_size) or int(stack_size))
        self.dp.write_ptr(thread_ptr.ptr, self.dp.handles.new(ChildThread(child, emulated.tid)))
        self._record("remote_thread", pid=child.pid, tid=emulated.tid, start=hex(start_address))
        world(self.dp).signal_all()
        return STATUS_SUCCESS

    def compare_objects(self, first, second):
        a, b = self._child(first), self._child(second)
        if a is None and b is None:
            return None
        same = a is b and type(self.dp.handles.get(int(first), None)) is type(self.dp.handles.get(int(second), None))
        return STATUS_SUCCESS if same else 0xC00001AC  # STATUS_NOT_SAME_OBJECT


_REMOTE = {
    "AllocateVirtualMemory": Processes.allocate,
    "FreeVirtualMemory": Processes.free,
    "ProtectVirtualMemory": Processes.protect,
    "QueryVirtualMemory": Processes.query,
    "WriteVirtualMemory": Processes.write,
    "ReadVirtualMemory": Processes.read,
    "QueryInformationProcess": Processes.query_process,
    "QueryInformationThread": Processes.query_thread,
    "ResumeThread": Processes.resume,
    "SuspendThread": Processes.suspend,
    "GetContextThread": Processes.get_context,
    "SetContextThread": Processes.set_context,
    "TerminateProcess": Processes.terminate,
    "UnmapViewOfSection": Processes.unmap,
    "MapViewOfSection": Processes.map_view,
    "CreateThreadEx": Processes.create_thread,
    "CompareObjects": Processes.compare_objects,
}


def _value(value) -> int:
    """The number of an enum of dumpulator's memory manager (MemoryProtect, MemoryState...)."""
    if value is None:
        return 0
    return int(value.value) if hasattr(value, "value") else int(value)


def _console(dp) -> list:
    from .fathom_nt import console_output
    return console_output(dp)


def _association(extension: str) -> Optional[str]:
    """The command the host associates with an extension (HKCR\\.ext -> ProgID -> shell\\open\\command)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, extension) as key:
            progid = winreg.QueryValue(key, None)
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\shell\open\command") as key:
            return winreg.QueryValue(key, None)
    except Exception:
        return None


class ImageLoader:
    """Maps an executable from the host's disk into an emulator, with its imports bound. A DLL the dump
    lacks is loaded by the emulated loader (LdrLoadDll: its dependencies and DllMain too); when that
    fails, it is mapped here without being initialized."""

    def __init__(self, dp):
        self.dp = dp
        self.missing: List[str] = []
        self._api_sets: Optional[Dict[str, str]] = None

    def api_set_host(self, name: str) -> Optional[str]:
        """The DLL an API set (api-ms-win-..., ext-ms-...) leads to, from the ApiSetMap of the PEB
        (schema version 6, Windows 10 and later)."""
        if self._api_sets is None:
            self._api_sets = {}
            dp = self.dp
            try:
                schema = dp.read_ptr(dp.peb + (0x68 if dp._x64 else 0x38))
                version, size, flags, count, entry_offset = struct.unpack("<IIIII", bytes(dp.read(schema, 20)))
                if version == 6:
                    for i in range(count):
                        _, name_offset, name_length, hashed, value_offset, value_count = struct.unpack(
                            "<IIIIII", bytes(dp.read(schema + entry_offset + 24 * i, 24)))
                        key = bytes(dp.read(schema + name_offset, name_length)).decode("utf-16-le").lower()
                        if not value_count:
                            continue
                        _, _, _, host_offset, host_length = struct.unpack("<IIIII", bytes(dp.read(schema + value_offset, 20)))
                        if host_length:
                            host = bytes(dp.read(schema + host_offset, host_length)).decode("utf-16-le")
                            self._api_sets[key[:hashed // 2]] = host
            except Exception:
                pass
        key = name.lower()
        if key.endswith(".dll"):
            key = key[:-4]
        if "-" in key:
            host = self._api_sets.get(key.rsplit("-", 1)[0])
            if host:
                return host
        if key.startswith("api-ms-win-crt-"):
            return "ucrtbase.dll"
        if key.startswith(("api-ms-win-", "ext-ms-win-")):
            return "kernelbase.dll"
        return None

    def module(self, name: str):
        """A module by its name (an API set resolved), loaded if needed."""
        dp = self.dp
        name = name.lower()
        if not name.endswith((".dll", ".exe", ".drv", ".sys", ".ocx", ".cpl")):
            name += ".dll"
        if name.startswith(("api-ms-", "ext-ms-")):
            host = self.api_set_host(name)
            if host is None:
                return None
            name = host.lower()
        found = dp.modules.find(name)
        if found is not None:
            return found
        loaded = self._load_library(name)
        if loaded is not None:
            return loaded
        windows = os.environ.get("SystemRoot", r"C:\Windows")
        path = os.path.join(windows, "System32" if dp._x64 else "SysWOW64", name)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            data = f.read()
        dp.info(f"[process] DLL {name} mapped from {path} (not initialized)")
        return self.load(path, data, executable=False)

    def _load_library(self, name: str):
        """The emulated loader loads the DLL (LdrLoadDll), as the child's own loader would."""
        dp = self.dp
        try:
            text = name.encode("utf-16-le")
            buffer = dp.allocate(len(text) + 2)
            dp.write(buffer, text + b"\0\0")
            string = dp.allocate(16)
            dp.write(string, struct.pack("<HHIQ", len(text), len(text) + 2, 0, buffer) if dp._x64
                     else struct.pack("<HHI", len(text), len(text) + 2, buffer))
            out = dp.allocate(8)
            saved = dp._uc.context_save()
            try:
                status = dp.call(dp.modules.resolve_export("ntdll.dll", "LdrLoadDll").address, [0, 0, string, out])
            finally:
                dp._uc.context_restore(saved)
                dp.stopped = False
                dp.exit_code = None
            if status & 0xFFFFFFFF:
                return None
            return dp.modules.find(dp.read_ptr(out)) or dp.modules.find(name)
        except Exception:
            return None

    def resolve(self, module_name: str, function, depth: int = 0) -> Optional[int]:
        module = self.module(module_name)
        if module is None or depth > 8:
            return None
        export = module.find_export(function)
        if export is None:
            return None
        if export.forward is not None:
            target, name = export.forward
            if isinstance(name, str) and name.startswith("#"):
                name = int(name[1:])
            return self.resolve(target, name, depth + 1)
        return export.address

    def load(self, path: str, data: bytes, executable: bool):
        dp = self.dp
        import pefile
        pe = pefile.PE(data=data)
        size = pe.OPTIONAL_HEADER.SizeOfImage
        relocatable = hasattr(pe, "DIRECTORY_ENTRY_BASERELOC") and not (pe.FILE_HEADER.Characteristics & 1)
        base = dp.memory.find_free(size) if relocatable else pe.OPTIONAL_HEADER.ImageBase
        if not relocatable:
            occupant = dp.modules.find(base)
            if occupant is not None:
                from .fathom_nt import _unmap
                _unmap(dp, occupant.base)
        original_base = pe.OPTIONAL_HEADER.ImageBase
        pe.relocate_image(base)
        pe.OPTIONAL_HEADER.ImageBase = base
        image = pe.write()
        module = dp.map_module(bytes(image), path, requested_base=base, resolve_imports=False)
        # the import address table, written in the mapped image (pefile's write() puts back the
        # thunks it parsed)
        for descriptor in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
            dll = descriptor.dll.decode("ascii", "replace")
            for entry in descriptor.imports:
                function = entry.name.decode("ascii", "replace") if entry.name else entry.ordinal
                address = self.resolve(dll, function)
                if address is None:
                    self.missing.append(f"{dll}!{function}")
                    address = 0
                # pefile moves the import addresses with relocate_image (some versions do not)
                slot = entry.address if base <= entry.address < base + size else base + entry.address - original_base
                dp.write_ptr(slot, address)
        return module


def install(dp, world_of=None):
    if world_of is not None:
        dp.world = world_of
    else:
        world(dp)
    processes = Processes(dp)
    dp.processes = processes
    # through dp.processes when called: a child's emulator gets a Processes of its own after its init
    dp.apihooks.register("kernelbase.dll", "CreateProcessInternalW", 12,
                         lambda dp, *args: dp.processes.create_process_internal(dp, *args))
    for wide, suffix in ((True, "W"), (False, "A")):
        dp.apihooks.register("shell32.dll", "ShellExecute" + suffix, 6,
                             lambda dp, *a, w=wide: dp.processes.shell_execute_simple(dp, *a, w))
        dp.apihooks.register("shell32.dll", "ShellExecuteEx" + suffix, 1,
                             lambda dp, *a, w=wide: dp.processes.shell_execute_ex(dp, *a, w))
    return processes
