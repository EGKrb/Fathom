"""Protocol-level tests for wineye's MCP / DAP / gdbserver modes, with a fake Vmm.

No real target (no vmm.dll) is needed: a FakeVmm feeds canned data, so this runs
anywhere (e.g. GitHub Actions). Exits non-zero on failure.
"""
import base64
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import wineye  # noqa: E402


class FakeVmm:
    def pids(self):
        return [4, 1234]

    def process(self, pid):
        return {"pid": pid, "ppid": 0 if pid == 4 else 4,
                "name": "System" if pid == 4 else "notepad.exe", "name_long": "",
                "eprocess": 0xffffb00012340000 + pid,
                "peb": 0 if pid == 4 else 0x7ff000000000,
                "dtb": 0x1aa000, "wow64": False, "session": 0 if pid == 4 else 1,
                "integrity": "system", "sid": "S-1-5-18", "system": "windows-x64"}

    def modules(self, pid):
        return [{"base": 0xfffff80000000000, "size": 0x900000, "entry": 0, "wow64": False,
                 "name": "ntoskrnl.exe", "path": r"C:\Windows\System32\ntoskrnl.exe"}]

    def handles(self, pid):
        return [{"handle": 0x4, "object": 0xffffb00000001000, "access": 0x1f0003,
                 "type": "Process", "text": "System"}]

    def vads(self, pid):
        return [{"start": 0x7ff000000000, "end": 0x7ff000010000, "vad": 0, "protection": 6,
                 "image": True, "file": False, "stack": False, "text": "notepad.exe"}]

    def symbol(self, m, n):
        return 0xfffff80000123456

    def read(self, pid, addr, size):
        return bytes((addr + i) & 0xff for i in range(size))


ops = wineye.WineyeOps(FakeVmm(), None)
_failures = 0


def check(name, cond):
    global _failures
    print(("  OK  " if cond else "  FAIL") + "  " + name)
    if not cond:
        _failures += 1


# ----------------------------- MCP -----------------------------
print("== MCP ==")
reqs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "read_memory",
                "arguments": {"address": "0xfffff80000001000", "size": "16"}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "list_processes", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
     "params": {"name": "registers", "arguments": {}}},
]
_in, _out = io.StringIO("".join(json.dumps(r) + "\n" for r in reqs)), io.StringIO()
_oi, _oo = sys.stdin, sys.stdout
sys.stdin, sys.stdout = _in, _out
try:
    wineye.McpServer(ops).serve()
finally:
    sys.stdin, sys.stdout = _oi, _oo
responses = [json.loads(l) for l in _out.getvalue().splitlines() if l.strip()]
byid = {r.get("id"): r for r in responses}
check("initialize -> protocolVersion", byid[1]["result"]["protocolVersion"] == "2024-11-05")
check("initialized notification -> no response", None not in byid)
check("tools/list -> 8 tools", len(byid[2]["result"]["tools"]) == 8)
_rm = json.loads(byid[3]["result"]["content"][0]["text"])
check("tools/call read_memory -> 16 bytes", int(_rm["size"], 0) == 16 and len(bytes.fromhex(_rm["hex"])) == 16)
check("tools/call list_processes -> 2 procs", len(json.loads(byid[4]["result"]["content"][0]["text"])) == 2)
check("tools/call registers (no ctl) -> isError", byid[5]["result"].get("isError") is True)

# ----------------------------- gdb -----------------------------
print("== gdb (RSP) ==")
g = wineye.GdbServer(ops)
check("qSupported -> PacketSize", "PacketSize=" in g._handle("qSupported"))
check("? -> S05", g._handle("?") == "S05")
mem = g._handle("mfffff80000000000,10")
check("m read -> 32 hex chars", len(mem) == 32 and all(c in "0123456789abcdef" for c in mem))
check("m read -> correct first byte (LE)", mem[:2] == f"{0xfffff80000000000 & 0xff:02x}")
regs = g._handle("g")
check("g (no ctl) -> all unavailable, 328 chars", len(regs) == 328 and set(regs) == {"x"})
check("M (write) -> E01", g._handle("Mdead,1:90") == "E01")
check("c (continue) -> S05", g._handle("c") == "S05")

# ----------------------------- DAP -----------------------------
print("== DAP ==")


def frame(obj):
    b = json.dumps(obj).encode()
    return f"Content-Length: {len(b)}\r\n\r\n".encode() + b


dap = (frame({"seq": 1, "type": "request", "command": "initialize", "arguments": {}}) +
       frame({"seq": 2, "type": "request", "command": "attach", "arguments": {}}) +
       frame({"seq": 3, "type": "request", "command": "modules", "arguments": {}}) +
       frame({"seq": 4, "type": "request", "command": "readMemory",
              "arguments": {"memoryReference": "0xfffff80000002000", "offset": 0, "count": 8}}) +
       frame({"seq": 5, "type": "request", "command": "evaluate",
              "arguments": {"expression": "nt!PsInitialSystemProcess"}}) +
       frame({"seq": 6, "type": "request", "command": "disconnect", "arguments": {}}))
rf, wf = io.BytesIO(dap), io.BytesIO()
wineye.DapServer(ops).serve(rf, wf)
raw = wf.getvalue()
msgs = []
while raw:
    h = raw.find(b"\r\n\r\n")
    if h == -1:
        break
    n = int([x for x in raw[:h].decode().split("\r\n") if x.lower().startswith("content-length")][0].split(":")[1])
    msgs.append(json.loads(raw[h + 4:h + 4 + n].decode()))
    raw = raw[h + 4 + n:]
resp = {m["request_seq"]: m for m in msgs if m["type"] == "response"}
events = [m for m in msgs if m["type"] == "event"]
check("initialize -> supportsReadMemoryRequest", resp[1]["body"]["supportsReadMemoryRequest"] is True)
check("initialized event", any(e["event"] == "initialized" for e in events))
check("attach -> stopped event", any(e["event"] == "stopped" for e in events))
check("modules -> 1 module", resp[3]["body"]["totalModules"] == 1)
check("readMemory -> 8 bytes base64", len(base64.b64decode(resp[4]["body"]["data"])) == 8)
check("evaluate symbol -> address", resp[5]["body"]["result"] == hex(0xfffff80000123456))
check("disconnect -> success", resp[6]["success"] is True)

print()
print("RESULT:", "ALL PASS" if _failures == 0 else f"{_failures} FAILURE(S)")
sys.exit(1 if _failures else 0)
