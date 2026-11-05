# x64dbg trace file format specification
x64dbg trace file is a binary file that contains all information about program execution. Each trace file is composed of 3 parts: **Magic word**, **JSON header** and **binary trace blocks**. x64dbg trace file is little-endian.
## Magic word
Every trace file will begin with 4 bytes, "TRAC" (encoded in ASCII).
## Header
Header is located after header at offset 4. It is composed of a 4-byte length field, followed by a JSON blob. The JSON blob might not be null-terminated and might not be aligned to 4-byte boundary.
## Binary trace blocks
Binary trace data is immediately after header without any padding and might not be aligned to 4-byte boundary. It is defined as a sequence of blocks. Every block is started with a 1-byte type number.

If the type number is `0`, then the block will contain the instruction data:

```c++
struct {
    uint8_t BlockType; //BlockType is 0, indicating it describes an instruction execution.
    uint8_t RegisterChanges;
    uint8_t MemoryAccesses;
    uint8_t BlockFlagsAndOpcodeSize; //Bitfield

    DWORD ThreadId;
    uint8_t Opcode[];

    uint8_t RegisterChangePosition[];
    duint RegisterChangeNewData[];

    uint8_t MemoryAccessFlags[];
    duint MemoryAccessAddress[];
    duint MemoryAccessOldData[];
    duint MemoryAccessNewData[];
};
```

If the type number is `0x80` or higher, the block will contain the following user-defined data:

```c++
struct {
    uint8_t BlockType;
    uint32_t BlockSize;
    uint8_t BlockData[];
};
```

The debugger will skip these user-defined blocks, and you can use them for whatever you like.

Block type `0x81` is written by the recorder itself, between two instructions that run on different threads (a context switch). Its 32-byte data is:

```c++
struct {
    DWORD FromThreadId;      // thread of the previous instruction
    DWORD ToThreadId;        // thread of the next instruction
    uint64_t NextInstruction; // index of the next instruction in the trace
    uint64_t Counter;         // QueryPerformanceCounter when the switch was recorded
    uint64_t Frequency;       // QueryPerformanceFrequency
};
```

Readers that do not know it skip it like any other user-defined block. `TraceExport` uses it to timestamp the context switches of the per-thread timelines of the `.ftt` format ([ftt.md](ftt.md)).

Block type `0x82` is a CPU time sample of every thread of the debuggee, written by the recorder before the next instruction when the recording starts, on a context switch, every 4096 instructions and when the recording stops. The debuggee is frozen while it is taken. Its data is a 40-byte header followed by 24 bytes per thread:

```c++
struct {
    uint64_t NextInstruction; // index of the next instruction in the trace
    uint64_t Counter;         // QueryPerformanceCounter
    uint64_t Frequency;       // QueryPerformanceFrequency
    DWORD NextThreadId;       // thread of the next instruction
    DWORD Flags;              // bit 0: the recording (re)started, the previous sample does not apply
    DWORD Count;              // number of threads
    DWORD Reserved;
    struct {
        DWORD ThreadId;
        DWORD Flags;          // bit 0: ContextHash is valid
        uint64_t CycleTime;   // QueryThreadCycleTime (user and kernel mode)
        uint64_t ContextHash; // FNV-1a of the user-mode integer registers, stack pointer, instruction pointer and flags
    } Threads[Count];
};
```

Between two samples a single thread is recorded (a context switch takes a sample). Another thread whose `CycleTime` grew in that interval used the processor while the recorded instructions ran; if its `ContextHash` changed too, it executed user-mode code outside the trace, so the order of its instructions relative to the recorded ones is unknown. A thread that used CPU time with an unchanged hash only ran kernel code: the suspensions of the debugger wake waiting threads at every debug event. `TraceExport` turns the samples into the intervals of the `.ftt` format.

`RegisterChanges` is a unsigned byte that counts the number of elements in the array `RegisterChangePosition` and `RegisterChangeNewData`.

`MemoryAccesses` is a unsigned byte that counts the number of elements in the array `MemoryAccessFlags`.

`BlockFlagsAndOpcodeSize` is a bitfield. The most significant bit is ThreadId bit. When this bit is set, `ThreadId` field is available and indicates the thread id which executed the instruction. When this bit is clear, the thread id that executed the instruction is the same as last instruction, so it is not stored in file. The least 4 significant bits specify the length of `Opcode` field, in number of bytes. Other bits are reserved and set to 0. `Opcode` field contains the opcode of current instruction.

`RegisterChangePosition` is an array of unsigned bytes. Each element indicates a pointer-sized integer in struct `REGDUMP` that is updated after execution of current instruction, as an offset to previous location. The absolute index is computed by adding the absolute index of previous element +1 (or 0 if it is first element) with this relative index. `RegisterChangeNewData` is an array of pointer-sized integers that contains the new value of register, that is recorded before the instruction is executed. `REGDUMP` structure is given below.

```c++
typedef struct
{
    REGISTERCONTEXT regcontext;
    FLAGS flags;
    X87FPUREGISTER x87FPURegisters[8];
    unsigned long long mmx[8];
    MXCSRFIELDS MxCsrFields;
    X87STATUSWORDFIELDS x87StatusWordFields;
    X87CONTROLWORDFIELDS x87ControlWordFields;
    LASTERROR lastError;
    //LASTSTATUS lastStatus; //This field is not supported and not included in trace file.
} REGDUMP;
```

For example, `ccx` is the second member of `regcontext`. On x64 architecture, it is at byte offset 8 and on x86 architecture it is at byte offset 4. On both architectures, it is at index 1 and `cax` is at index 0. Therefore, when `RegisterChangePosition[0]` = 0, `RegisterChangeNewData[0]` contains the new value of `cax`. If `RegisterChangePosition[1]` = 0, `RegisterChangeNewData[1]` contains the new value of `ccx`, since the absolute index is computed by 0+0+1=1. The use of relative indexing helps achieve better data compression if a lossless compression is then applied to trace file, and also allow future expansion of `REGDUMP` structure without increasing size of `RegisterChanges` and `RegisterChangePosition` beyond a byte. Note: the file reader can locate the address of the instruction using `cip` register in this structure.

x64dbg will save all registers at the start of trace, and every 512 instructions (this number might be changed in future versions to have different tradeoff between speed and space). A block with all registers saved will have `RegisterChanges`=172 on 64-bit platform and 216 on 32-bit platform. This allows x64dbg trace file to be randomly accessed. x64dbg relies on random accessibility of the trace file to support arbitrary sized files within bounded memory footprint. x64dbg might be unable to open a trace file that has a sequence of instruction longer than an implementation-defined limit without all registers saved.

`MemoryAccessFlags` is an array of bytes that indicates properties of memory access. Currently, only bit 0 is defined and all other bits are reserved and set to 0. When bit 0 is set, it indicates the memory is not changed (This could mean it is read, or it is overwritten with identical value), so `MemoryAccessNewData` will not have an entry for this memory access. The file reader may use a disassembler to determine the true type of memory access.

`MemoryAccessAddress` is an array of pointers that indicates the address of memory access.

`MemoryAccessOldData` is an array of pointer-sized integers that stores the old content of memory.

`MemoryAccessNewData` is an array of pointer-sized integers that stores the new content of memory.

When recording the trace, new instructions are appended to the file end. The reader sees real-time update as the file is appended.
