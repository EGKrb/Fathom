"""Example script for emu.script (Fathom) or `fathom_emu.py script`: call a function of the
dumped process on several inputs.

    emu.script "C:\\...\\emulator\\examples\\crc_strings.py", ntdll!RtlComputeCrc32
    python fathom_emu.py script app.dmp examples\\crc_strings.py ntdll!RtlComputeCrc32

In a script: `dp` is the Dumpulator of the dump, `args` the script's arguments (as typed),
`result` the result dictionary (result["return"] = value sets $result in Fathom).
"""
from fathom_emu import resolve_address

function = resolve_address(dp, args[0] if args else "ntdll!RtlComputeCrc32")
crc = 0
for word in ("hello", "Fathom", "dumpulator"):
    data = word.encode()
    buffer = dp.allocate(len(data))
    dp.write(buffer, data)
    crc = dp.call(function, [0, buffer, len(data)]) & 0xFFFFFFFF
    print(f"{word:12} {crc:#010x}")
result["return"] = crc
