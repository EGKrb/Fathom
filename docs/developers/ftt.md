# Fathom Time Travel trace format (.ftt)

A `.ftt` file holds an execution trace that can be explored forward and backward: binary, indexed and
compressed. `TraceExport` writes it from a `.trace32`/`.trace64` recorded by x64dbg (`TraceExport
trace.trace64, trace.ftt`) and reads it back as a source for the other formats; the **Time Travel** tab
and the `tt.*` commands of the ExtraCmds plugin navigate it. The reference implementation is the
header-only [`src/dbg/ftt/ftt.h`](../../src/dbg/ftt/ftt.h) (writer and reader); an independent Python
reader is in [`src/tests/trace_export/check.py`](../../src/tests/trace_export/check.py).

Design:

- **One timeline per thread, synchronized.** The instructions of each thread are stored in that
  thread's chunks. Every instruction has a *global sequence number* (its position among the
  instructions of all threads), so the state of every thread is known at any instant, and a debugger
  can show another thread at the same instant.
- **Context switches are explicit.** When an instruction runs on another thread than the previous one
  in the global order, a switch marker precedes it (previous thread, time of the switch when known).
- **Strict deltas.** A chunk starts with a full register keyframe; each instruction then stores only
  the registers that changed. A memory access stores its old content only when it was not already
  known with the same value, and its new content only when it was written.
- **Memory snapshots.** At regular intervals (`snapshot=` option of `TraceExport`, 65536 instructions
  by default) the pages whose known content changed since the previous snapshot are written. The
  memory at any instant is the last snapshot before it plus the accesses replayed since: the work is
  bounded whatever the position, as with the snapshots of a `.run` file.
- **Registers at a chosen verbosity.** General purpose registers always; x87, SSE and AVX on request
  (`regs=` option), so a trace is not heavier than needed.
- **Compressed and indexed.** Chunks are LZ4 blocks; an index at the end of the file lists them, so a
  reader seeks directly to the chunk of an instruction.

All integers are little-endian. Structures are packed (no padding).

## Layout

```
FileHeader | metadata (JSON) | Chunk ... | index Chunk | Trailer
```

### FileHeader (24 bytes)

| Field | Type | Description |
|---|---|---|
| magic | char[4] | `FTT1` |
| version | uint16 | 1 |
| pointerSize | uint16 | 4 (x86) or 8 (x64) |
| levels | uint32 | register levels stored: 1 general purpose (always), 2 x87, 4 SSE, 8 AVX |
| snapshotInterval | uint32 | instructions between two memory snapshots |
| metaSize | uint32 | size of the JSON metadata that follows |
| reserved | uint32 | 0 |

The metadata is a JSON object: `ver`, `arch` (`x86`/`x64`), `path` (traced executable), `hash` (module
hash, as in a `.trace64` header), `source` (file it was converted from).

### Chunk (48-byte header, then the data)

| Field | Type | Description |
|---|---|---|
| magic | char[4] | `FCHK` |
| kind | uint8 | 1 thread, 2 memory snapshot, 3 index |
| compressed | uint8 | 1: the data is an LZ4 block, 0: stored as is |
| reserved | uint16 | 0 |
| thread | uint32 | thread id (thread chunk) |
| count | uint32 | instructions (thread chunk) or pages (snapshot) |
| firstSeq | uint64 | global sequence number of the first instruction; snapshot: the state *before* this instruction |
| lastSeq | uint64 | global sequence number of the last instruction |
| firstLocal | uint64 | position of the first instruction in its thread's timeline |
| rawSize | uint32 | size of the data once decompressed |
| storedSize | uint32 | size of the data in the file |

### Thread chunk data

A keyframe, then events.

```
uint16 n;                           // keyframe: the full register state before the first instruction
struct { uint8 id; uint8 value[size of register id]; } registers[n];
events...
```

Events:

- **Switch** (`tag 2`): `uint32 fromThread; uint64 timestamp;` — the next instruction follows, in the
  global order, an instruction of `fromThread`. `timestamp` is in 100 ns units (arbitrary origin), 0 when
  unknown.
- **Instruction** (`tag 1`):
  ```
  varint seqDelta;                  // global sequence number - previous one in this chunk (first: - firstSeq)
  uint8 opcodeSize; uint8 opcode[opcodeSize];
  uint8 nchanged;                   // registers, state *before* the instruction, changed since the previous
  struct { uint8 id; uint8 value[size]; } changed[nchanged];   // instruction of the same thread
  uint8 nmemory;
  struct {
      uint8 flags;                  // 1: written (new content follows), 2: old content omitted (already known)
      uint64 address;
      uint8 size;                   // up to 16
      uint8 old[size];              // content before the instruction, unless flags & 2
      uint8 new[size];              // content after the instruction, if flags & 1
  } memory[nmemory];
  ```

`varint` is unsigned LEB128 (7 bits per byte, the high bit set on all bytes but the last). The
instruction pointer is a register: an instruction's address is its value after applying the deltas.

### Memory snapshot data

`count` pages of 4616 bytes: `uint64 base; uint8 known[512]; uint8 data[4096];` — `known` has one bit per
byte (bit `i % 8` of `known[i / 8]`) set when that byte's content is known. A snapshot only holds the
pages whose known content changed since the previous snapshot: the most recent copy of a page is the
one of the last snapshot that contains it.

### Index chunk data (kind 3, not listed in itself)

```
uint32 n;
struct { uint64 offset; uint8 kind; uint32 thread; uint32 count; uint64 firstSeq; uint64 lastSeq; uint64 firstLocal; } chunks[n];   // 41 bytes
uint32 nthreads;
struct { uint32 thread; uint64 count; uint64 firstSeq; uint64 lastSeq; } threads[nthreads];   // 28 bytes
uint64 instructionCount;
```

### Trailer (16 bytes)

`uint64 indexOffset; char magic[4] = "FTTX"; uint32 reserved;` — at the very end of the file.

## Register catalog

A register's id is its position in this list (values little-endian, sizes in bytes).

| ids (x64) | ids (x86) | registers | size | level |
|---|---|---|---|---|
| 0-15 | 0-7 | rax rcx rdx rbx rsp rbp rsi rdi r8-r15 / eax ecx edx ebx esp ebp esi edi | 8 / 4 | 1 |
| 16, 17 | 8, 9 | rip, rflags / eip, eflags | 8 / 4 | 1 |
| 18-25 | 10-17 | st0-st7 (80-bit) | 10 | 2 |
| 26-28 | 18-20 | fpcw, fpsw, fptw | 2 | 2 |
| 29-44 | 21-28 | xmm0-xmm15 / xmm0-xmm7 | 16 | 4 |
| 45 | 29 | mxcsr | 4 | 4 |
| 46-61 | 30-37 | ymm0h-ymm15h / ymm0h-ymm7h (upper halves) | 16 | 8 |

## Rebuilding the state at an instant

Before instruction `S`:

- **Registers of thread T**: take T's first instruction at or after `S` (or its last one if it ended);
  decode its chunk from the keyframe, applying the deltas up to that instruction.
- **Memory**: for each page, the copy of the last snapshot taken at or before `S` that contains it;
  then, in global order, the accesses of all threads from that snapshot to `S - 1` (the old content of
  an access, then its new content if it was written). Bytes still unknown can take the content the
  next access reads (TraceExport and the Time Travel tab show them as inferred).
