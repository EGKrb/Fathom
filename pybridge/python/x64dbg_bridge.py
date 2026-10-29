"""Python client for the x64dbg pybridge plugin.

Only the standard library is used. Typical use:

    from x64dbg_bridge import Debugger

    with Debugger.launch(r"C:\\path\\to\\x64dbg\\release") as dbg:
        dbg.init(r"C:\\target.exe")
        dbg.set_breakpoint("kernel32.CreateFileW")
        dbg.run(wait=True)
        print(dbg.read_string(dbg.reg("rcx"), wide=True))

Addresses and values can be given as integers or as x64dbg expressions
("kernel32.CreateFileW", "rsp+8", "[rsp]"). In expressions, numbers are
hexadecimal by default, like everywhere in x64dbg.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

DEFAULT_PORT = 27041

__all__ = ["BridgeError", "Debugger", "escape_argument", "DEFAULT_PORT"]


class BridgeError(Exception):
    """Raised when the plugin reports an error or the connection fails."""


def escape_argument(text: str) -> str:
    """Quote a string argument for an x64dbg command (port of Command::Escape)."""
    result = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            if ch in '"{':
                result.append("\\")
            result.append(ch)
            i += 1
            continue
        start = i
        while i < len(text) and text[i] == "\\":
            i += 1
        count = i - start
        if i == len(text):
            result.append("\\" * (count * 2))  # protect trailing slashes from the closing quote
        elif text[i] in '"{':
            result.append("\\" * (count * 2 + 1))
            result.append(text[i])
            i += 1
        else:
            result.append("\\" * count)
    return '"' + "".join(result) + '"'


def _value(value: int | str) -> str | int:
    """Convert an address/value for the protocol (int or expression string)."""
    if isinstance(value, bool):
        raise TypeError("expected an integer or an expression string")
    if isinstance(value, int):
        return f"0x{value & 0xFFFFFFFFFFFFFFFF:X}"
    return value


def _terminate(process: subprocess.Popen, grace: float = 0.0) -> None:
    """Wait up to grace seconds for the process to exit, then kill it and wait until it is really gone
    (so that its port is free for the next session)."""
    try:
        process.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    process.kill()
    process.wait()


def _command_value(value: int | str) -> str:
    """Format an address/value as an unquoted command argument."""
    return _value(value) if isinstance(value, int) else str(value)


class Debugger:
    """Connection to a running x64dbg (GUI or headless) with the pybridge plugin."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout: float = 60.0):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._file = self._sock.makefile("rb")
        self._next_id = 1
        self._process: subprocess.Popen | None = None
        info = self.call("ping")
        self.arch = info["arch"]
        self.pid: int | None = info.get("pid")  # process id of x64dbg (not of the debuggee)

    # ----------------------------------------------------------------- lifetime

    @classmethod
    def launch(
        cls,
        x64dbg_dir: str | os.PathLike,
        arch: str = "x64",
        gui: bool = False,
        port: int = DEFAULT_PORT,
        extra_args: Iterable[str] = (),
        startup_timeout: float = 30.0,
        log_file: str | os.PathLike | None = None,
    ) -> "Debugger":
        """Start headless.exe (or x64dbg.exe with gui=True) and connect to it.

        x64dbg_dir is the "release" folder that contains x64 and x32.
        The plugin must be installed in <x64dbg_dir>/<arch>/plugins.
        """
        folder = Path(x64dbg_dir) / ("x64" if arch == "x64" else "x32")
        exe = folder / ("headless.exe" if not gui else f"{'x64' if arch == 'x64' else 'x32'}dbg.exe")
        if not exe.is_file():
            raise BridgeError(f"{exe} not found")
        env = dict(os.environ, X64DBG_PYBRIDGE_PORT=str(port))
        output = open(log_file, "wb") if log_file else subprocess.DEVNULL
        # headless.exe exits when its stdin is closed, keep a pipe open for the whole session
        process = subprocess.Popen(
            [str(exe), *extra_args],
            cwd=folder,
            env=env,
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        if log_file:
            output.close()
        deadline = time.monotonic() + startup_timeout
        other_pid = None
        while True:
            if process.poll() is not None:
                raise BridgeError(f"{exe.name} exited with code {process.returncode}")
            try:
                dbg = cls(port=port)
                # The port can still belong to another (or a closing) x64dbg: only accept our process
                if dbg.pid in (None, process.pid):
                    break
                other_pid = dbg.pid
                dbg._file.close()
                dbg._sock.close()
            except (OSError, BridgeError):
                pass
            if time.monotonic() > deadline:
                _terminate(process)
                if other_pid is not None:
                    raise BridgeError(f"port {port} is used by another x64dbg (pid {other_pid}), close it or use another port")
                raise BridgeError(f"cannot connect to pybridge on port {port} (is the plugin installed in {folder / 'plugins'}?)")
            time.sleep(0.2)
        dbg._process = process
        return dbg

    def close(self) -> None:
        """Close the connection. A process started by launch() is stopped too."""
        process, self._process = self._process, None
        if process is not None:
            try:
                if self.state()["debugging"]:
                    self.stop()
                self.call("quit")  # closes x64dbg (GUI or headless) cleanly
            except (BridgeError, OSError):
                pass
        try:
            self._file.close()
            self._sock.close()
        except OSError:
            pass
        if process is not None:
            if process.stdin:
                try:
                    process.stdin.write(b"exit\n")  # headless.exe also exits on this line
                    process.stdin.close()
                except OSError:
                    pass
            _terminate(process, grace=15)

    def __enter__(self) -> "Debugger":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ----------------------------------------------------------------- protocol

    def call(self, method: str, **params: Any) -> Any:
        """Send a raw request to the plugin and return its result."""
        request = {"id": self._next_id, "method": method, "params": params}
        self._next_id += 1
        try:
            self._sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
            line = self._file.readline()
        except OSError as exc:
            raise BridgeError(f"connection error: {exc}") from exc
        if not line:
            raise BridgeError("connection closed by x64dbg")
        response = json.loads(line)
        if "error" in response:
            raise BridgeError(f"{method}: {response['error']}")
        return response["result"]

    # ----------------------------------------------------------------- commands

    def cmd(self, command: str, check: bool = True) -> bool:
        """Execute an x64dbg command (same syntax as the command bar or a script)."""
        ok = self.call("cmd", command=command)
        if check and not ok:
            raise BridgeError(f"command failed: {command}")
        return ok

    def command(self, name: str, *args: int | str | None, check: bool = True) -> bool:
        """Execute a command with arguments: ints become hex, str are quoted (use expr() for expressions)."""
        parts = []
        for arg in args:
            if arg is None:
                continue
            if isinstance(arg, Expr):
                parts.append(arg.text)
            elif isinstance(arg, int):
                parts.append(_command_value(arg))
            else:
                parts.append(escape_argument(str(arg)))
        return self.cmd(name + (" " + ", ".join(parts) if parts else ""), check=check)

    def eval(self, expression: str) -> int:
        """Evaluate an x64dbg expression, e.g. eval("[rsp+8]") or eval("mod.base(kernel32)")."""
        return int(self.call("eval", expr=expression), 16)

    @property
    def result(self) -> int:
        """Value of $result, set by many commands."""
        return self.eval("$result")

    # ----------------------------------------------------------------- execution

    def state(self) -> dict:
        state = self.call("state")
        if "cip" in state:
            state["cip"] = int(state["cip"], 16)
        return state

    def wait(self, timeout: float | None = 10.0) -> bool:
        """Wait until the debuggee is paused. Returns False on timeout."""
        timeout_ms = -1 if timeout is None else int(timeout * 1000)
        return self.call("wait", timeout_ms=timeout_ms)["paused"]

    def init(self, path: str | os.PathLike, arguments: str | None = None, cwd: str | os.PathLike | None = None, timeout: float = 30.0) -> None:
        """Start debugging an executable and wait for the first pause (system/entry breakpoint)."""
        args = [str(path)]
        if arguments is not None or cwd is not None:
            args.append(arguments or "")
        if cwd is not None:
            args.append(str(cwd))
        self.command("init", *args)
        if not self.wait(timeout):
            raise BridgeError("the debuggee did not pause after init")

    def attach(self, pid: int, timeout: float = 30.0) -> None:
        self.cmd(f"attach {pid:X}")
        if not self.wait(timeout):
            raise BridgeError("the debuggee did not pause after attach")

    def run(self, wait: bool = False, timeout: float | None = 10.0, pass_exceptions: bool = False) -> bool:
        """Resume execution. With wait=True, returns True if the debuggee paused again (breakpoint...)."""
        timeout_ms = -1 if timeout is None else int(timeout * 1000)
        return self.call("run", wait=wait, timeout_ms=timeout_ms, pass_exceptions=pass_exceptions)["paused"]

    def pause(self, timeout: float = 10.0) -> bool:
        return self.call("pause", timeout_ms=int(timeout * 1000))["paused"]

    def step_into(self, timeout: float = 10.0) -> bool:
        return self.call("step_into", timeout_ms=int(timeout * 1000))["paused"]

    def step_over(self, timeout: float = 10.0) -> bool:
        return self.call("step_over", timeout_ms=int(timeout * 1000))["paused"]

    def step_out(self, timeout: float = 30.0) -> bool:
        return self.call("step_out", timeout_ms=int(timeout * 1000))["paused"]

    def stop(self, timeout: float = 15.0) -> None:
        """Stop debugging (terminates the debuggee). Raises BridgeError if it takes longer than timeout."""
        if not self.call("stop", timeout_ms=int(timeout * 1000))["stopped"]:
            raise BridgeError(f"debugging did not stop within {timeout:g} s")

    # ----------------------------------------------------------------- registers

    def regs(self) -> dict[str, int]:
        """General purpose registers, instruction pointer and flags."""
        return {name: int(value, 16) for name, value in self.call("regs").items()}

    def reg(self, name: str) -> int:
        return int(self.call("reg_get", name=name), 16)

    def set_reg(self, name: str, value: int | str) -> None:
        self.call("reg_set", name=name, value=_value(value))

    # ----------------------------------------------------------------- memory

    def read(self, address: int | str, size: int) -> bytes:
        return bytes.fromhex(self.call("mem_read", addr=_value(address), size=size))

    def write(self, address: int | str, data: bytes) -> None:
        self.call("mem_write", addr=_value(address), data=data.hex())

    def is_valid(self, address: int | str) -> bool:
        return self.call("mem_valid", addr=_value(address))

    def read_ptr(self, address: int | str) -> int:
        size = 8 if self.arch == "x64" else 4
        return int.from_bytes(self.read(address, size), "little")

    def read_string(self, address: int | str, wide: bool = False, max_length: int = 4096) -> str:
        """Read a NUL-terminated string (stops at the first unreadable page)."""
        unit = 2 if wide else 1
        address = self.eval(address) if isinstance(address, str) else address
        data = bytearray()
        while len(data) < max_length * unit:
            chunk_size = min(256, 0x1000 - (address + len(data)) % 0x1000)
            try:
                chunk = self.read(address + len(data), chunk_size)
            except BridgeError:
                break
            data += chunk
            for i in range(0, len(data) - unit + 1, unit):
                if data[i:i + unit] == b"\0" * unit:
                    return data[:i].decode("utf-16-le" if wide else "utf-8", errors="replace")
        return data[:max_length * unit].decode("utf-16-le" if wide else "utf-8", errors="replace")

    def disasm(self, address: int | str, count: int = 1) -> list[dict]:
        """Disassemble count instructions: [{"address", "size", "text", "bytes"}]."""
        result = self.call("disasm", addr=_value(address), count=count)
        for entry in result:
            entry["address"] = int(entry["address"], 16)
        return result

    # ----------------------------------------------------------------- breakpoints

    def set_breakpoint(
        self,
        address: int | str,
        condition: str | None = None,
        log: str | None = None,
        command: str | None = None,
        break_: bool = True,
        singleshot: bool = False,
    ) -> None:
        """Software breakpoint with optional condition, log text and command.

        break_=False only logs/executes the command and never pauses (bpcond = 0).
        Log text uses the x64dbg format syntax, e.g. "CreateFileW({utf16@rcx})".
        """
        target = Expr(_command_value(address))
        self.command("bp", target, Expr("ss") if singleshot else None)
        if not break_:
            self.command("SetBreakpointCondition", target, "0")
        elif condition:
            self.command("SetBreakpointCondition", target, condition)
        if log:
            self.command("SetBreakpointLog", target, log)
        if command:
            self.command("SetBreakpointCommand", target, command)

    def delete_breakpoint(self, address: int | str | None = None) -> None:
        """Delete one breakpoint, or all of them when address is None."""
        self.command("bc", Expr(_command_value(address)) if address is not None else None)

    def set_hardware_breakpoint(self, address: int | str, kind: str = "x", size: int = 1) -> None:
        """kind: "x" (execute), "w" (write) or "r" (read/write)."""
        self.command("bphws", Expr(_command_value(address)), Expr(kind), Expr(str(size)))

    # ----------------------------------------------------------------- anti-anti-debug

    def hide_debugger(self) -> None:
        """Patch the debuggee's PEB to hide the debugger (x64dbg built-in `hide` command).

        Clears `PEB.BeingDebugged`, `PEB.NtGlobalFlag` debug bits (0x70) and the
        process heap debug flags. Covers x64 and WoW64. Call it after `init()` /
        `attach()` and before the first `run()`. Idempotent.
        """
        self.cmd("hide")

    @staticmethod
    def scyllahide_profiles(ini_path: str | os.PathLike) -> list[str]:
        """Return the profile names available in a `scylla_hide.ini`."""
        profiles = []
        with open(ini_path, "r", encoding="utf-8-sig", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith("[") and line.endswith("]"):
                    name = line[1:-1]
                    if name != "SETTINGS":
                        profiles.append(name)
        return profiles

    @staticmethod
    def scyllahide_set_profile(ini_path: str | os.PathLike, profile: str) -> None:
        """Set `CurrentProfile=<profile>` in a `scylla_hide.ini` (writes the file in place).

        The ScyllaHide plugin reads this value when the next debuggee is launched,
        so change the profile *before* calling `init()` / `attach()`. The INI lives
        next to the ScyllaHide plugin, e.g. `<x64dbg>\\x64\\plugins\\scylla_hide.ini`.
        """
        path = Path(ini_path)
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        if profile not in Debugger.scyllahide_profiles(path):
            raise BridgeError(f"profile {profile!r} not found in {path} "
                              f"(available: {Debugger.scyllahide_profiles(path)})")
        lines = text.splitlines()
        section = None
        replaced = False
        for i, raw in enumerate(lines):
            stripped = raw.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1]
                continue
            if section == "SETTINGS" and stripped.lower().startswith("currentprofile"):
                lines[i] = f"CurrentProfile={profile}"
                replaced = True
                break
        if not replaced:
            # No [SETTINGS] block or no CurrentProfile line: prepend it
            lines = ["[SETTINGS]", f"CurrentProfile={profile}", ""] + lines
        # ScyllaHide expects CRLF; write bytes directly so Windows text mode
        # does not translate \n into \r\n (which would turn \r\n into \r\r\n).
        path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))

    # ----------------------------------------------------------------- API hooking

    def hook_api(
        self,
        api: str,
        log: str | None = None,
        condition: str | None = None,
        command: str | None = None,
        break_: bool = False,
    ) -> None:
        """Breakpoint on an API with log + optional action, without pausing by default.

        A convenience wrapper around `set_breakpoint`: `break_=False` (the default)
        turns it into a logging/counting hook, so the target keeps running at full speed
        while every call is recorded. Use `log` to format arguments with x64dbg's
        formatting syntax, e.g.:

            dbg.hook_api("kernelbase.VirtualProtect",
                         log="VirtualProtect({p:arg.get(0)}, size={x:arg.get(1)}, "
                             "protect={x:arg.get(2)}) from {a:[csp]}")

        `condition` filters which calls fire the log/command (x64dbg expression,
        e.g. `strstr(utf16(arg.get(0)), \".txt\")`). `command` runs an x64dbg
        script fragment on each hit (e.g. `savedata ".\\stack.bin", csp, 400`).
        `break_=True` makes it a real pausing breakpoint.
        """
        self.set_breakpoint(
            api,
            condition=condition,
            log=log,
            command=command,
            break_=break_,
        )

    # ----------------------------------------------------------------- memory dumping

    def dump_memory(self, address: int | str, size: int, path: str | os.PathLike) -> int:
        """Save `size` bytes from `address` to a binary file. Returns the bytes written."""
        data = self.read(address, size)
        Path(path).write_bytes(data)
        return len(data)

    # ----------------------------------------------------------------- tracing

    def trace(self, record_file: str | os.PathLike | None = None, condition: str = "0", max_steps: int = 50000, step_over: bool = False) -> int:
        """Trace until condition is true or max_steps instructions were executed.

        With record_file, the trace is recorded to a .trace32/.trace64 file.
        Returns the number of traced instructions.
        """
        if record_file is not None:
            self.command("StartTraceRecording", str(record_file))
        try:
            self.command("TraceOverConditional" if step_over else "TraceIntoConditional", condition, Expr(f"0x{max_steps:X}"))
            self.wait(None)
        finally:
            if record_file is not None:
                self.cmd("StopTraceRecording", check=False)
        return self.eval("$tracecounter")

    def export_trace(self, trace_file: str | os.PathLike, output_file: str | os.PathLike, fmt: str | None = None,
                     slide: int | str | None = None) -> int:
        """Convert a trace file with the TraceExport command. Returns the instruction count.

        fmt: "json", "csv", "tenet" or "frinet" (default: from the output file extension).
        slide: frinet only, the ASLR slide (an integer or an expression such as "mod.main()").
        """
        if slide is not None and not fmt:
            fmt = "frinet"
        self.command("TraceExport", str(trace_file), str(output_file), Expr(fmt) if fmt else None,
                     Expr(slide) if isinstance(slide, str) else slide)
        return self.result


class Expr:
    """Marks a command argument as an unquoted expression: dbg.command("bp", Expr("kernel32.Sleep"))."""

    def __init__(self, text: str):
        self.text = text

    def __repr__(self) -> str:
        return f"Expr({self.text!r})"


__all__.append("Expr")
