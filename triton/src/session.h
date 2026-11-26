// The Triton session of the plugin: one Triton context (symbolic execution, taint analysis) whose
// concrete state comes either from the debuggee (its current thread's registers, its memory read on
// demand: the debuggee itself never moves) or from a recorded Time Travel trace (.ftt: each
// instruction is replayed with the registers and memory values the trace recorded).
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <set>
#include <string>
#include <vector>

#include <triton/context.hpp>

#include "ftt.h"

namespace fathom_triton
{
    enum class Source
    {
        None,
        Debuggee, // the debuggee's state, emulated by Triton
        Trace, // a recorded .ftt, replayed
    };

    // An instruction Triton executed (kept when it read symbolic or tainted data)
    struct Executed
    {
        uint64_t index = 0; // position in the run (0 = first instruction executed in the session)
        uint64_t seq = 0; // trace sequence number (trace source)
        uint64_t address = 0;
        std::string disassembly;
        bool symbolic = false;
        bool tainted = false;
    };

    struct RunResult
    {
        uint64_t executed = 0;
        uint64_t ip = 0;
        std::string reason;
        bool error = false;
    };

    class Session
    {
    public:
        static Session & Get();

        // Start from the debuggee's current thread (registers, segment bases, memory on demand)
        bool StartDebuggee(std::string & error);
        // Start at instruction `seq` of a .ftt (its thread; `thread` 0: the thread that runs seq)
        bool StartTrace(const std::wstring & path, uint64_t seq, uint32_t thread, std::string & error);
        void Stop();

        bool Active() const { return mContext != nullptr; }
        Source GetSource() const { return mSource; }
        triton::Context & Context() { return *mContext; }
        unsigned PointerSize() const { return mPointerSize; }
        uint64_t Ip();
        uint32_t Thread() const { return mThread; }
        uint64_t Seq() const { return mSeq; }
        uint64_t Executed() const { return mExecuted; }
        const std::wstring & TracePath() const { return mTracePath; }

        // Run: debuggee source, until `end` (0: none), `count` instructions, the return of the current
        // function (untilReturn), a system call or an instruction Triton cannot execute. Trace source:
        // the recorded instructions of the thread, until `end`, `count` or the end of the thread.
        RunResult Run(uint64_t end, uint64_t count, bool untilReturn, uint64_t lastSeq = UINT64_MAX);

        // A register by name (rax, eax, al, xmm0...), nullptr if unknown
        const triton::arch::Register* Register(const std::string & name);
        // Read concrete memory of the source (debuggee or trace at the current position)
        bool ReadSource(uint64_t address, size_t size, std::vector<uint8_t> & data);

        // Executed instructions that read symbolic or tainted data, in order
        const std::vector<fathom_triton::Executed> & History() const { return mHistory; }
        size_t HistoryDropped() const { return mHistoryDropped; }

        // Last solver model (sym.solve), for sym.apply
        std::unordered_map<triton::usize, triton::engines::solver::SolverModel> lastModel;

        // Addresses commented in the disassembly by sym.mark
        std::set<uint64_t> marked;

        // Trace source: registers / memory synchronizations that dropped symbolic data (the trace ran
        // code that is not in it, or another thread wrote the memory)
        uint64_t desyncRegisters = 0;
        uint64_t desyncMemory = 0;

    private:
        Session() = default;
        bool Create(unsigned pointerSize, std::string & error);
        void OnMemoryRead(triton::Context & ctx, const triton::arch::MemoryAccess & mem);
        void Record(const triton::arch::Instruction & inst);
        // Trace: make the context agree with the recorded state before instruction ins
        void SyncTrace(const ftt::Instruction & ins, const ftt::RegisterFile & regs);
        RunResult RunDebuggee(uint64_t end, uint64_t count, bool untilReturn);
        RunResult RunTrace(uint64_t end, uint64_t count, uint64_t lastSeq);

        std::unique_ptr<triton::Context> mContext;
        Source mSource = Source::None;
        unsigned mPointerSize = 8;
        uint32_t mThread = 0;
        uint64_t mExecuted = 0;
        std::vector<fathom_triton::Executed> mHistory;
        size_t mHistoryDropped = 0;
        bool mReadFailed = false;
        uint64_t mReadFailAddress = 0;

        // trace source
        std::unique_ptr<ftt::Reader> mReader;
        std::wstring mTracePath;
        uint64_t mSeq = 0; // the next instruction to replay (global sequence number)
        bool mTraceEnded = false;
        std::vector<const triton::arch::Register*> mTraceRegs; // ftt register id -> Triton register
    };

    std::string Hex(uint64_t value);
    std::string Hex(const triton::uint512 & value);
}
