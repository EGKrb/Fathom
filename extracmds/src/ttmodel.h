// Time Travel: navigation in a Fathom Time Travel trace (.ftt), shared by the tt.* commands and the
// "Time Travel" tab. Qt-free, so the commands also exist in the 32-bit plugin and headless.exe.
//
// The model holds a position (the global sequence number of the current instruction: the state
// shown is the state *before* it) and a viewed thread. Moving the position forward or backward,
// switching thread at the same instant or searching an execution / a memory write only reads the
// trace: nothing touches the debugged process.
#pragma once

#include "ftt.h"

#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

class TtModel
{
public:
    static TtModel & Get();

    // Open a .ftt, or convert a .trace32/.trace64 recorded by x64dbg first (TraceExport, every register)
    bool Open(const std::wstring & path, std::string & error);
    void Close();
    bool IsOpen();

    struct Info
    {
        std::wstring path;
        std::wstring source; // the .trace64 it was converted from, if any
        uint64_t count = 0;
        size_t threads = 0;
        size_t chunks = 0;
        size_t snapshots = 0;
        unsigned pointerSize = 0;
        uint32_t levels = 0;
        uint32_t snapshotInterval = 0;
        std::string hash; // module hash of the traced process (TraceExport metadata)
    };
    Info GetInfo();

    uint64_t Position();
    uint32_t ViewThread();

    // Go to an instruction; the viewed thread becomes the one that runs it
    bool Seek(uint64_t seq);
    // One instruction forward / backward, in the global timeline or in the viewed thread's
    bool Step(bool forward, bool sameThread);
    // Show another thread at the same instant (its state before its next instruction)
    bool SwitchThread(uint32_t thread);
    // Previous / next execution of an address (viewed thread only, or any thread)
    bool FindExecution(uint64_t address, bool forward, bool sameThread);
    // Previous / next write to a memory range
    bool FindWrite(uint64_t address, size_t size, bool forward);

    bool Instruction(uint64_t seq, ftt::Instruction & instruction);
    // The viewed thread's instruction after / before seq (to list its timeline)
    bool NextInViewThread(uint64_t seq, bool forward, uint64_t & result);
    // The viewed thread's instruction at the position (or its next / last one)
    bool CurrentInViewThread(uint64_t & seq);
    // The viewed thread's registers at the position, and the instruction they belong to
    bool Registers(ftt::RegisterFile & registers, uint64_t* at = nullptr);
    // The viewed thread's registers before its previous instruction (to highlight changes)
    bool PreviousRegisters(ftt::RegisterFile & registers);
    void Memory(uint64_t address, size_t size, std::vector<uint8_t> & data, std::vector<bool> & known, std::vector<bool> & inferred);
    std::vector<ftt::ThreadInfo> Threads();
    std::vector<std::pair<uint64_t, uint64_t>> ThreadRanges(uint32_t thread);

    // Disassembly of a trace instruction (empty when the trace architecture is not this debugger's)
    std::string Disassemble(const ftt::Instruction & instruction);
    // module!symbol of an address, only while debugging the process the trace was recorded from
    std::string Label(uint64_t address);
    bool SameProcess();

    // Called (on the thread that changed the model) after every change
    void AddListener(const std::function<void()> & listener);

private:
    TtModel() = default;
    void Notify();
    bool CurrentInThread(uint64_t & seq); // the viewed thread's instruction at or after the position

    std::recursive_mutex mLock;
    std::unique_ptr<ftt::Reader> mReader;
    Info mInfo;
    uint64_t mPosition = 0;
    uint32_t mThread = 0;
    std::vector<std::function<void()>> mListeners;
};
