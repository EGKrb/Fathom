"""Test of the emulation commands of ExtraCmds (emu.*), in headless.exe with the real emulator.

The debuggee (tests/trace_export.exe, stopped at its entry point) is saved to a minidump and its code
runs in dumpulator: Windows functions called with integer, string and buffer arguments, a system
call (VirtualAlloc), the thread run on, a Python script, a recorded trace opened in Time Travel, a
minidump emulated after the debugging session has ended, and a whole program with worker threads
(src/tests/emulator_threads: critical section, events, mutex, semaphore, condition variable, TLS,
sleeps) run from its entry point to its exit, then recorded. Then the program
src/tests/emulator_world, whole, in each of its modes: threads racing on several emulated
processors, an instruction the emulator lacks (ud2 in the program, an exception it handles; with
emu.strict on, the emulation stops), windows and timers, the simulated network (Winsock, WinINet)
and child processes (one run to its end, one created suspended, written to, then resumed), then
processes that run at the same time and talk (anonymous pipes on the standard input and output of a
child, a section and named events, a named pipe), a child of the other architecture, ShellExecuteEx,
an in-process COM object (msxml6) with the service manager and an RPC, files and registry, two
processes racing on shared memory, window messages between processes (WM_COPYDATA, a registered
message), bitmaps, regions and paths drawn offscreen, GDI+ shapes, and frames of OpenGL, Direct3D 9
and Direct3D 11 (shaders run by the emulator, textures), then triangles clipped by the near plane,
OpenGL 3.3 with GLSL, Direct3D 9 shaders, Direct3D 10, the geometry, tessellation and compute
shaders of Direct3D 11, a mesh of 20000 triangles and Direct3D 12. The images of the windows
(emu.screenshots) are checked pixel by pixel. The debuggee itself must not move.

Needs Python with the emulator's packages (emulator/requirements.txt) on the PATH.

Usage: py extracmds/tests/test_emulator.py --bin bin/x64 --plugin extracmds/build-x64-noqt/ExtraCmds.dp64
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

SCRIPT = '''# emu.script test: dumpulator's API from a script, with the argument of the command
text = dp.allocate(32)
dp.write(text, b"dumpulator\\0")
lstrlen = dp.modules.resolve_export("kernel32.dll", "lstrlenA").address
result["return"] = dp.call(lstrlen, [text]) + int(args[0], 0)
print(f"lstrlenA in the script: {result['return'] - int(args[0], 0)}")
'''


def run_headless(bindir: Path, script: str, plugin: Path) -> tuple[str, int, str]:
    folder = bindir / "tests" / "emulator"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.iterdir():
        if old.is_file():
            old.unlink()
    (folder / "script.py").write_text(SCRIPT, encoding="utf-8")
    (folder / "test.txt").write_text(script, encoding="utf-8")
    shutil.copyfile(plugin, folder / plugin.name)
    command = [str(bindir / "headless.exe"), "-testing", "-plugin", f"tests/emulator/{plugin.name}",
               "-c", 'RedirectLog "tests/emulator/test.log"', "-cf", "tests/emulator/test.txt"]
    subprocess.run(command, cwd=bindir, timeout=3600, capture_output=True)
    log = folder / "test.log"
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    final = [line for line in text.splitlines() if "[x64dbg-test] FINAL" in line]
    return (final[-1] if final else "no FINAL line"), script.count("testassert"), text


def check_images(log: str) -> bool:
    """The images of the gui mode (the window with its blue band and green ellipse, the message box), of
    the paint mode and of the frames of OpenGL, Direct3D 9 and 11."""
    import re
    paths = re.findall(r"image of a window: (.+?\.png)", log)
    print(f"images of windows: {len(paths)}")
    window = [p for p in paths if "Emulated window" in p]
    if not window or not any("message box" in p for p in paths):
        print("   missing images:", paths)
        return False
    try:
        from PIL import Image
    except ImportError:
        print("   Pillow is not installed: the images are not checked")
        return True
    def colors(path):
        image = Image.open(path).convert("RGB")
        return image.size, set(image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata())

    size, found = colors(window[-1])
    blue, green = (0, 90, 200) in found, (0, 170, 80) in found
    print(f"   {window[-1]}: {size[0]}x{size[1]}, blue band {blue}, green ellipse {green}")
    paint = [p for p in paths if "Bitmaps regions paths" in p]
    if not paint:
        print("   missing the image of the paint mode")
        return False
    size, found = colors(paint[-1])
    wanted = {"bitmap": (255, 0, 0), "DIB section": (255, 255, 0), "StretchDIBits": (0, 255, 255),
              "region": (0, 0, 255), "path": (0, 160, 0), "clipping": (255, 0, 255)}
    present = {name: color in found for name, color in wanted.items()}
    print(f"   {paint[-1]}: " + ", ".join(f"{name} {ok}" for name, ok in present.items()))
    # the frames of OpenGL, Direct3D 9 and 11 shown in their windows: the orange triangle and the texels
    shown = True
    for title, wanted in (("OpenGL", [(255, 128, 0), (0, 0, 128), (255, 0, 0), (0, 255, 0)]),
                          ("Direct3D 9", [(255, 128, 0), (0, 0, 128), (255, 0, 0), (0, 255, 0)]),
                          ("Direct3D 11", [(255, 128, 0), (0, 153, 0), (255, 0, 0), (0, 0, 255)])):
        frames = [p for p in paths if p.endswith(f"_{title}.png")]
        if not frames:
            print(f"   missing the image of {title}")
            shown = False
            continue
        size, found = colors(frames[-1])
        ok = all(any(max(abs(a - b) for a, b in zip(color, c)) <= 2 for c in found) for color in wanted)
        print(f"   {frames[-1]}: {size[0]}x{size[1]}, triangle and texels {ok}")
        shown = shown and ok
    return blue and green and all(present.values()) and shown


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin", required=True, help="bin/x64 or bin/x32 of the Fathom build")
    parser.add_argument("--plugin", required=True, help="ExtraCmds.dp64/.dp32 built without Qt")
    args = parser.parse_args()
    bindir = Path(args.bin).resolve()
    plugin = Path(args.plugin).resolve()

    script = r'''settingset Events, TlsCallbacks, 0
settingset Events, TlsCallbacksSystem, 0
settingset Events, EntryBreakpoint, 1
init tests/trace_export.exe
run
testassert $pid != 0, "stopped at entry"
var entry
mov entry, cip
var stacktop
mov stacktop, [csp]

emu.call ntdll.RtlComputeCrc32, 0, str:hello, 5
testassert $result == 0x3610A686, "RtlComputeCrc32 of a string argument"
emu.call kernel32.lstrlenW, wstr:Fathom
testassert $result == 6, "lstrlenW of a UTF-16 argument"
emu.call kernel32.GetCurrentProcessId
testassert $result == $pid, "the process id in the emulator"
emu.call kernel32.VirtualAlloc, 0, 0x10000, 0x3000, 4
testassert $result != 0, "VirtualAlloc (NtAllocateVirtualMemory emulated)"
emu.call kernel32.lstrcpyA, buf:32, str:copied
testassert $result != 0, "lstrcpyA into a buffer argument"

emu.trace on
emu.call ntdll.RtlComputeCrc32, 0, str:hello, 5
testassert $result == 0x3610A686, "the same result with the trace"
var traced
mov traced, $result1
testassert traced > 0x10, "instructions recorded"
tt.seek 0
tt.regs
testassert $result == ntdll.RtlComputeCrc32, "the trace starts at the called function"
emu.trace off

emu.syscalls on
emu.run 0, .500
testassert $result != entry, "the thread ran on from the entry point"
testassert $result2 == -1, "500 instructions: the process has not exited"
emu.syscalls off

emu.script "tests/emulator/script.py", 7
testassert $result == 0x11, "a script: lstrlenA(dumpulator) + its argument"

testassert cip == entry, "the debuggee did not move"
testassert [csp] == stacktop, "the memory of the debuggee did not change"

emu.dump "tests/emulator/saved.dmp"
stop
emu.load "tests/emulator/saved.dmp"
emu.call ntdll!RtlComputeCrc32, 0, str:hello, 5
testassert $result == 0x3610A686, "a minidump emulated without debugging"
emu.unload

init tests/emulator_threads.exe
run
testassert $pid != 0, "emulator_threads stopped at entry"
mov entry, cip
emu.run
testassert $result2 == 0, "a whole program with worker threads, from its entry point to its exit (code 0: every check passed)"
emu.trace on
emu.run 0, .300000
testassert $result1 > .100000, "the instructions of several threads recorded"
emu.trace off
testassert cip == entry, "the debuggee did not move"
stop

emu.cpus
testassert $result == 0, "emu.cpus: the processors of the dump by default"
init tests/emulator_world.exe, race
run
emu.run
testassert $result2 == 0, "race: threads on several emulated processors (a racy counter loses updates, an atomic one does not)"
emu.cpus 1
emu.seed 5
emu.run
testassert $result2 == 0, "race on one emulated processor, another interleaving"
emu.cpus 0
emu.seed 1
stop
init tests/emulator_world.exe, illegal
run
emu.run
testassert $result2 == 0, "illegal: ud2 raises STATUS_ILLEGAL_INSTRUCTION in the program, which handles it"
emu.strict on
emu.run
testassert $result2 == -1, "illegal with emu.strict on: the emulation stops at ud2"
emu.strict off
stop
init tests/emulator_world.exe, gui
run
emu.screenshots on
emu.run
testassert $result2 == 0, "gui: a window that paints itself, its messages and timers, closed by the simulated user"
emu.screenshots off
stop
init tests/emulator_world.exe, net
run
emu.network
testassert $result == 0, "emu.network: simulated by default"
emu.run
testassert $result2 == 0, "net: DNS, TCP and HTTP (Winsock), then WinINet, on the simulated network"
stop
init tests/emulator_world.exe, process
run
mov entry, cip
emu.run
testassert $result2 == 0, "process: a child run to its end (exit code 7), a suspended one written to then resumed (9)"
testassert cip == entry, "the debuggee did not move"
stop
init tests/emulator_world.exe, pipe
run
emu.run
testassert $result2 == 0, "pipe: a child reads its input from a pipe and writes its output to another, at the same time as its parent"
stop
init tests/emulator_world.exe, shm
run
emu.run
testassert $result2 == 0, "shm: five round trips with a child through a shared section and named events"
stop
init tests/emulator_world.exe, namedpipe
run
emu.run
testassert $result2 == 0, "namedpipe: a named pipe server and its client in a child"
stop
init tests/emulator_world.exe, cross
run
emu.run
testassert $result2 == 0, "cross: a child of the other architecture (exit code 5)"
stop
init tests/emulator_world.exe, shell
run
emu.run
testassert $result2 == 0, "shell: ShellExecuteEx runs a program in the emulator (exit code 6)"
stop
init tests/emulator_world.exe, com
run
emu.run
testassert $result2 == 0, "com: an in-process COM object (msxml6) parses XML; a service's status"
stop
init tests/emulator_world.exe, system
run
emu.run
testassert $result2 == 0, "system: files and registry in the emulator"
stop
init tests/emulator_world.exe, racep
run
emu.run
testassert $result2 == 0, "racep: two processes on shared memory (interlocked increments exact, plain ones in range)"
stop
init tests/emulator_world.exe, winmsg
run
emu.run
testassert $result2 == 0, "winmsg: WM_COPYDATA sent and a registered message posted by another process"
stop
init tests/emulator_world.exe, paint
run
emu.screenshots on
emu.run
testassert $result2 == 0, "paint: bitmaps, regions and paths, checked with GetPixel"
init tests/emulator_world.exe, gdiplus
run
emu.run
testassert $result2 == 0, "gdiplus: GDI+ shapes and alpha blending on a memory DC"
stop
init tests/emulator_world.exe, opengl
run
emu.run
testassert $result2 == 0, "opengl: a triangle, a translated square and a texture, read with glReadPixels"
stop
init tests/emulator_world.exe, d3d9
run
emu.run
testassert $result2 == 0, "d3d9: a triangle and a textured square, read with GetRenderTargetData"
stop
init tests/emulator_world.exe, d3d11
run
emu.run
testassert $result2 == 0, "d3d11: a scene drawn by vertex and pixel shaders, read through a staging texture"
stop
init tests/emulator_world.exe, clip
run
emu.run
testassert $result2 == 0, "clip: OpenGL triangles crossing the near plane, clipped"
stop
init tests/emulator_world.exe, gl3
run
emu.run
testassert $result2 == 0, "gl3: an OpenGL 3.3 core context, GLSL shaders, buffers, a vertex array and a framebuffer object"
stop
init tests/emulator_world.exe, d3d9sh
run
emu.run
testassert $result2 == 0, "d3d9sh: Direct3D 9 vertex and pixel shaders (vs_2_0, ps_2_0)"
stop
init tests/emulator_world.exe, d3d10
run
emu.run
testassert $result2 == 0, "d3d10: a Direct3D 10 scene with shaders, read through a staging texture"
stop
init tests/emulator_world.exe, d3d11gs
run
emu.run
testassert $result2 == 0, "d3d11gs: Direct3D 11 geometry shader and tessellation (hull and domain shaders)"
stop
init tests/emulator_world.exe, d3d11cs
run
emu.run
testassert $result2 == 0, "d3d11cs: a Direct3D 11 compute shader writing a texture and atomics"
stop
init tests/emulator_world.exe, mesh
run
emu.run
testassert $result2 == 0, "mesh: a Direct3D 11 grid of 20000 triangles, drawn in one call"
stop
init tests/emulator_world.exe, d3d12
run
emu.run
testassert $result2 == 0, "d3d12: Direct3D 12 command lists, root signature, fence and flip swap chain"
stop
init tests/emulator_world.exe, bc
run
emu.run
testassert $result2 == 0, "bc: BC6H (unsigned and signed) and BC7 textures, decoded as the GPU does"
stop
init tests/emulator_world.exe, deriv
run
emu.run
testassert $result2 == 0, "deriv: ddx / ddy coarse and fine, mip levels chosen from the derivatives"
stop
init tests/emulator_world.exe, tess
run
emu.run
testassert $result2 == 0, "tess: the Direct3D 11 tessellator, a level per edge, integer and fractional partitionings"
stop
init tests/emulator_world.exe, so
run
emu.run
testassert $result2 == 0, "so: Direct3D 11 stream output, its statistics and DrawAuto"
stop
init tests/emulator_world.exe, deferred
run
emu.run
testassert $result2 == 0, "deferred: Direct3D 11 deferred contexts and command lists"
stop
init tests/emulator_world.exe, gl4
run
emu.run
testassert $result2 == 0, "gl4: OpenGL 4.3 compute, geometry and tessellation shaders, GLSL derivatives"
stop
init tests/emulator_world.exe, fx10
run
emu.run
testassert $result2 == 0, "fx10: a Direct3D 10 effect (fx_4_0), its variables and two passes"
stop
init tests/emulator_world.exe, dxil
run
emu.run
testassert $result2 == 0, "dxil: shader model 6 (DXIL) vertex, pixel and compute shaders in Direct3D 12"
stop
init tests/emulator_world.exe, dxr
run
emu.run
testassert $result2 == 0, "dxr: Direct3D 12 ray tracing (state object, acceleration structures, DispatchRays)"
stop
init tests/emulator_world.exe, dxilgs
run
emu.run
testassert $result2 == 0, "dxilgs: hull, domain and geometry shaders of shader model 6, captured by stream output"
stop
init tests/emulator_world.exe, dxilops
run
emu.run
testassert $result2 == 0, "dxilops: the rarer DXIL operations (waves, quads, packing, gathers, counters, ResourceDescriptorHeap)"
stop
init tests/emulator_world.exe, dxr2
run
emu.run
testassert $result2 == 0, "dxr2: ray tracing with an intersection shader, local root signatures and RayQuery"
stop
init tests/emulator_world.exe, mesh6
run
emu.run
testassert $result2 == 0, "mesh6: amplification and mesh shaders (DispatchMesh, a pipeline state stream)"
stop
init tests/emulator_world.exe, graph
run
emu.run
testassert $result2 == 0, "graph: a work graph (broadcasting, thread and coalescing nodes)"
stop
init tests/emulator_world.exe, sm69
run
emu.run
testassert $result2 == 0, "sm69: shader model 6.9 (HitObject, long vectors)"
emu.screenshots off
stop
'''
    # the multithreaded program is right natively: its exit code is its number of failed checks
    native = subprocess.run([str(bindir / "tests" / "emulator_threads.exe")], capture_output=True, text=True, timeout=120)
    print("emulator_threads natively:", native.stdout.strip(), f"(exit code {native.returncode})")
    if native.returncode != 0:
        return 1
    # the modes of emulator_world that do not need the network are right natively too
    for mode in ("race", "illegal", "process", "pipe", "shm", "namedpipe", "cross", "shell", "system", "racep", "winmsg", "paint",
                 "gdiplus", "opengl", "d3d9", "d3d11", "clip", "gl3", "d3d9sh", "d3d10", "d3d11gs", "d3d11cs", "mesh", "d3d12",
                 "bc", "deriv", "tess", "so", "deferred", "gl4", "fx10", "dxil", "dxr", "dxilgs", "dxilops", "dxr2", "mesh6",
                 "graph", "sm69"):
        native = subprocess.run([str(bindir / "tests" / "emulator_world.exe"), mode], capture_output=True, text=True, timeout=120)
        print(f"emulator_world {mode} natively:", " / ".join(native.stdout.splitlines()[:3]), f"(exit code {native.returncode})")
        if native.returncode != 0:
            return 1
    final, asserts, log = run_headless(bindir, script, plugin)
    print(final, f"({asserts} assertions in the script)")
    if not check_images(log):
        return 1
    if "status=pass" not in final or f"asserts={asserts}" not in final:
        for line in log.splitlines():
            if "testassert" in line.lower() or "ExtraCmds" in line or "Error" in line or "error" in line:
                print("  ", line)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
