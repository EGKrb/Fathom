#include "session.h"

#include "_plugins.h"

#include <triton/x86Specifications.hpp>

#include <cinttypes>
#include <cstring>
#include <sstream>

namespace fathom_triton
{
    // Executed instructions kept for sym.list / sym.mark (the oldest are dropped beyond)
    static const size_t HistoryLimit = 200000;

    std::string Hex(uint64_t value)
    {
        char text[32];
        sprintf_s(text, "0x%" PRIX64, value);
        return text;
    }

    std::string Hex(const triton::uint512 & value)
    {
        if(value <= triton::uint512(UINT64_MAX))
            return Hex(static_cast<uint64_t>(value));
        // wider than 64 bits (xmm / ymm): hexadecimal digits, most significant first
        std::string digits;
        triton::uint512 v = value;
        while(!v.is_zero())
        {
            const unsigned digit = static_cast<unsigned>(static_cast<uint64_t>(v & triton::uint512(15)));
            digits.insert(digits.begin(), "0123456789ABCDEF"[digit]);
            v >>= 4;
        }
        return "0x" + digits;
    }

    Session & Session::Get()
    {
        static Session session;
        return session;
    }

    bool Session::Create(unsigned pointerSize, std::string & error)
    {
        Stop();
        try
        {
            mPointerSize = pointerSize;
            mContext = std::make_unique<triton::Context>(pointerSize == 8 ? triton::arch::ARCH_X86_64 : triton::arch::ARCH_X86);
            auto & ctx = *mContext;
            ctx.setMode(triton::modes::ALIGNED_MEMORY, true);
            ctx.setMode(triton::modes::CONSTANT_FOLDING, true);
            ctx.setMode(triton::modes::AST_OPTIMIZATIONS, true);
            // symbolic expressions and path constraints only where symbolic data flows: the rest of the
            // execution stays concrete (much faster, and the constraints are the ones that matter)
            ctx.setMode(triton::modes::ONLY_ON_SYMBOLIZED, true);
            ctx.setMode(triton::modes::PC_TRACKING_SYMBOLIC, true);
            ctx.setSolverTimeout(60000);
            ctx.addCallback(triton::callbacks::GET_CONCRETE_MEMORY_VALUE,
                            triton::ComparableFunctor<void(triton::Context &, const triton::arch::MemoryAccess &)>(
                                [this](triton::Context & c, const triton::arch::MemoryAccess & mem) { OnMemoryRead(c, mem); }, this));
        }
        catch(const std::exception & e)
        {
            mContext.reset();
            error = e.what();
            return false;
        }
        mExecuted = 0;
        mHistory.clear();
        mHistoryDropped = 0;
        lastModel.clear();
        desyncRegisters = desyncMemory = 0;
        return true;
    }

    void Session::Stop()
    {
        mContext.reset();
        mReader.reset();
        mTracePath.clear();
        mTraceRegs.clear();
        mSource = Source::None;
        mSeq = 0;
        mTraceEnded = false;
        mExecuted = 0;
        mHistory.clear();
        lastModel.clear();
    }

    uint64_t Session::Ip()
    {
        if(!mContext)
            return 0;
        return static_cast<uint64_t>(mContext->getConcreteRegisterValue(mContext->getRegister(mPointerSize == 8 ? "rip" : "eip"), false));
    }

    const triton::arch::Register* Session::Register(const std::string & name)
    {
        if(!mContext)
            return nullptr;
        std::string lower = name;
        for(auto & ch : lower)
            ch = (char)tolower((unsigned char)ch);
        // x64dbg's names of the flags register
        if(lower == "rflags" || lower == "cflags")
            lower = "eflags";
        try
        {
            auto & reg = mContext->getRegister(lower);
            if(reg.getId() == triton::arch::ID_REG_INVALID || !mContext->isRegisterValid(reg))
                return nullptr;
            return &reg;
        }
        catch(const std::exception &)
        {
            return nullptr;
        }
    }

    // ------------------------------------------------------------------------- debuggee source ---
    bool Session::StartDebuggee(std::string & error)
    {
        if(!DbgIsDebugging())
        {
            error = "not debugging";
            return false;
        }
#ifdef _WIN64
        const unsigned pointerSize = 8;
#else
        const unsigned pointerSize = 4;
#endif
        if(!Create(pointerSize, error))
            return false;
        mSource = Source::Debuggee;
        mThread = DbgGetThreadId();
        auto & ctx = *mContext;

        REGDUMP_AVX512 dump = {};
        if(!DbgGetRegDumpEx(&dump, sizeof(dump)))
        {
            error = "cannot read the registers of the debuggee";
            Stop();
            return false;
        }
        auto & r = dump.regcontext;
        auto set = [&](const char* name, uint64_t value) { ctx.setConcreteRegisterValue(ctx.getRegister(name), triton::uint512(value), false); };
#ifdef _WIN64
        set("rax", r.cax); set("rcx", r.ccx); set("rdx", r.cdx); set("rbx", r.cbx);
        set("rsp", r.csp); set("rbp", r.cbp); set("rsi", r.csi); set("rdi", r.cdi);
        set("r8", r.r8); set("r9", r.r9); set("r10", r.r10); set("r11", r.r11);
        set("r12", r.r12); set("r13", r.r13); set("r14", r.r14); set("r15", r.r15);
        set("rip", r.cip);
        set("gs", DbgGetTebAddress(mThread)); // Triton's segment registers hold their base
        const int vectors = 16;
#else
        set("eax", r.cax); set("ecx", r.ccx); set("edx", r.cdx); set("ebx", r.cbx);
        set("esp", r.csp); set("ebp", r.cbp); set("esi", r.csi); set("edi", r.cdi);
        set("eip", r.cip);
        set("fs", DbgGetTebAddress(mThread));
        const int vectors = 8;
#endif
        set("eflags", r.eflags);
        set("mxcsr", r.MxCsr);
        for(int i = 0; i < vectors; i++)
        {
            // ymm = High:Low of the 256-bit part of zmm i
            triton::uint512 value = 0;
            const auto & ymm = r.ZmmRegisters[i].Low;
            uint64_t parts[4] = { ymm.Low.Low, (uint64_t)ymm.Low.High, ymm.High.Low, (uint64_t)ymm.High.High };
            for(int p = 3; p >= 0; p--)
                value = (value << 64) | triton::uint512(parts[p]);
            char name[8];
            sprintf_s(name, "ymm%d", i);
            ctx.setConcreteRegisterValue(ctx.getRegister(name), value, false);
        }
        return true;
    }

    // ---------------------------------------------------------------------------- trace source ---
    bool Session::StartTrace(const std::wstring & path, uint64_t seq, uint32_t thread, std::string & error)
    {
        auto reader = std::make_unique<ftt::Reader>();
        if(!reader->open(path, error))
            return false;
        if(reader->count() == 0)
        {
            error = "the trace is empty";
            return false;
        }
        const unsigned pointerSize = reader->pointerSize();
#ifdef _WIN64
        if(pointerSize != 8)
#else
        if(pointerSize != 4)
#endif
        {
            error = pointerSize == 8 ? "64-bit trace: open it in x64dbg" : "32-bit trace: open it in x32dbg";
            return false;
        }
        if(seq >= reader->count())
        {
            error = "instruction " + std::to_string(seq) + " is not in the trace (" + std::to_string(reader->count()) + " instructions)";
            return false;
        }
        if(thread == 0)
        {
            if(!reader->threadAt(seq, thread))
            {
                error = "cannot read instruction " + std::to_string(seq);
                return false;
            }
        }
        else
        {
            // the first instruction of that thread at or after seq
            ftt::Instruction ins;
            if(!reader->instruction(seq, ins) || ins.thread != thread)
            {
                uint64_t next = 0;
                if(!reader->nextInThread(thread, seq, next, true))
                {
                    error = "thread " + std::to_string(thread) + " has no instruction from " + std::to_string(seq);
                    return false;
                }
                seq = next;
            }
        }
        if(!Create(pointerSize, error))
            return false;
        mReader = std::move(reader);
        mSource = Source::Trace;
        mTracePath = path;
        mThread = thread;
        mSeq = seq;
        mTraceEnded = false;

        // the trace's register catalog, by ftt id
        const auto & catalog = ftt::Registers(pointerSize);
        mTraceRegs.assign(catalog.size(), nullptr);
        for(size_t id = 0; id < catalog.size(); id++)
        {
            std::string name = catalog[id].name;
            if(name == "rflags")
                name = "eflags";
            if(name.size() > 3 && name.compare(0, 3, "ymm") == 0 && name.back() == 'h')
                continue; // upper halves: merged with the xmm register below
            if(name == "fpcw")
                name = "fcw";
            else if(name == "fpsw")
                name = "fsw";
            else if(name == "fptw")
                name = "ftw";
            mTraceRegs[id] = Register(name);
        }

        // the state before the first instruction
        ftt::Instruction ins;
        ftt::RegisterFile regs;
        if(!mReader->instruction(mSeq, ins) || !mReader->registers(mThread, mSeq, regs))
        {
            error = "cannot read instruction " + std::to_string(mSeq);
            Stop();
            return false;
        }
        SyncTrace(ins, regs);
        desyncRegisters = desyncMemory = 0;
        return true;
    }

    void Session::SyncTrace(const ftt::Instruction & ins, const ftt::RegisterFile & regs)
    {
        auto & ctx = *mContext;
        const auto & catalog = ftt::Registers(mPointerSize);
        for(size_t id = 0; id < mTraceRegs.size(); id++)
        {
            auto reg = mTraceRegs[id];
            if(!reg || !regs.known[id])
                continue;
            triton::uint512 value = 0;
            const unsigned size = catalog[id].size;
            for(int b = (int)size - 1; b >= 0; b--)
                value = (value << 8) | triton::uint512(regs.value[id][b]);
            // xmm i + the upper half recorded as ymm i h: the whole ymm register
            if(catalog[id].level == ftt::LevelSse && std::strncmp(catalog[id].name, "xmm", 3) == 0)
            {
                const std::string high = std::string("ymm") + (catalog[id].name + 3) + "h";
                for(size_t h = 0; h < catalog.size(); h++)
                {
                    if(high == catalog[h].name && regs.known[h])
                    {
                        triton::uint512 upper = 0;
                        for(int b = 15; b >= 0; b--)
                            upper = (upper << 8) | triton::uint512(regs.value[h][b]);
                        value = (upper << 128) | value;
                        reg = Register(std::string("ymm") + (catalog[id].name + 3));
                        break;
                    }
                }
                if(!reg)
                    continue;
            }
            if(reg->getId() == triton::arch::ID_REG_X86_EFLAGS)
            {
                // the flags: one by one, so that a symbolic flag whose value is right stays symbolic
                static const std::pair<const char*, int> flags[] = { { "cf", 0 }, { "pf", 2 }, { "af", 4 }, { "zf", 6 }, { "sf", 7 },
                    { "tf", 8 }, { "if", 9 }, { "df", 10 }, { "of", 11 } };
                const uint64_t bits = static_cast<uint64_t>(value);
                for(auto & flag : flags)
                {
                    auto & f = ctx.getRegister(flag.first);
                    const triton::uint512 want = (bits >> flag.second) & 1;
                    if(ctx.getConcreteRegisterValue(f, false) != want)
                    {
                        if(ctx.isRegisterSymbolized(f))
                        {
                            ctx.concretizeRegister(f);
                            desyncRegisters++;
                        }
                        ctx.setConcreteRegisterValue(f, want, false);
                    }
                }
                ctx.setConcreteRegisterValue(*reg, value, false);
                continue;
            }
            if(ctx.getConcreteRegisterValue(*reg, false) != value)
            {
                // the trace says otherwise: code outside the trace (a skipped call, the kernel) changed it
                if(ctx.isRegisterSymbolized(*reg))
                {
                    ctx.concretizeRegister(*reg);
                    desyncRegisters++;
                }
                ctx.setConcreteRegisterValue(*reg, value, false);
            }
        }
        // the memory the instruction reads, as recorded (another thread, or code outside the trace, may
        // have written it since Triton last saw it)
        for(auto & access : ins.memory)
        {
            if(access.flags & ftt::MemoryOldOmitted)
                continue; // the same as already known by the trace: the replay already has it
            for(uint8_t i = 0; i < access.size; i++)
            {
                const uint64_t address = access.address + i;
                if(ctx.isConcreteMemoryValueDefined(address, 1))
                {
                    const auto current = ctx.getConcreteMemoryAreaValue(address, 1, false);
                    if(current[0] == access.oldData[i])
                        continue;
                    if(ctx.isMemorySymbolized(address, 1))
                    {
                        ctx.concretizeMemory(address);
                        desyncMemory++;
                    }
                }
                ctx.setConcreteMemoryAreaValue(address, &access.oldData[i], 1, false);
            }
        }
    }

    bool Session::ReadSource(uint64_t address, size_t size, std::vector<uint8_t> & data)
    {
        data.assign(size, 0);
        if(mSource == Source::Debuggee)
            return DbgMemRead((duint)address, data.data(), (duint)size);
        if(mSource == Source::Trace && mReader)
        {
            std::vector<bool> known, inferred;
            mReader->memory(mSeq, address, size, data, known, &inferred);
            // known, or inferred from what the trace reads next
            for(size_t i = 0; i < size; i++)
                if(!known[i] && !inferred[i])
                    return false;
            return true;
        }
        return false;
    }

    void Session::OnMemoryRead(triton::Context & ctx, const triton::arch::MemoryAccess & mem)
    {
        const uint64_t address = mem.getAddress();
        const uint32_t size = mem.getSize();
        if(ctx.isConcreteMemoryValueDefined(address, size))
            return;
        // bytes Triton does not know yet: from the source
        for(uint32_t i = 0; i < size; i++)
        {
            const uint64_t at = address + i;
            if(ctx.isConcreteMemoryValueDefined(at, 1))
                continue;
            // a page at a time from the debuggee (most reads are followed by others nearby)
            const uint64_t page = at & ~0xFFFull;
            std::vector<uint8_t> data;
            if(mSource == Source::Debuggee)
            {
                data.resize(0x1000);
                if(!DbgMemRead((duint)page, data.data(), data.size()))
                {
                    uint8_t byte = 0;
                    if(!DbgMemRead((duint)at, &byte, 1))
                    {
                        if(!mReadFailed)
                        {
                            mReadFailed = true;
                            mReadFailAddress = at;
                        }
                        ctx.setConcreteMemoryAreaValue(at, &byte, 1, false);
                        continue;
                    }
                    ctx.setConcreteMemoryAreaValue(at, &byte, 1, false);
                    continue;
                }
                // only the bytes not defined yet (others may hold values Triton wrote, or symbolic data)
                for(uint64_t o = 0; o < data.size(); o++)
                    if(!ctx.isConcreteMemoryValueDefined(page + o, 1))
                        ctx.setConcreteMemoryAreaValue(page + o, &data[(size_t)o], 1, false);
            }
            else if(mSource == Source::Trace && mReader)
            {
                std::vector<bool> known, inferred;
                mReader->memory(mSeq, at, 1, data, known, &inferred);
                ctx.setConcreteMemoryAreaValue(at, data.data(), 1, false);
            }
        }
    }

    void Session::Record(const triton::arch::Instruction & inst)
    {
        const bool symbolic = inst.isSymbolized();
        const bool tainted = inst.isTainted();
        if(!symbolic && !tainted)
            return;
        if(mHistory.size() >= HistoryLimit)
        {
            mHistory.erase(mHistory.begin(), mHistory.begin() + HistoryLimit / 10);
            mHistoryDropped += HistoryLimit / 10;
        }
        fathom_triton::Executed e;
        e.index = mExecuted;
        e.seq = mSource == Source::Trace ? mSeq : 0;
        e.address = inst.getAddress();
        e.disassembly = inst.getDisassembly();
        e.symbolic = symbolic;
        e.tainted = tainted;
        mHistory.push_back(std::move(e));
    }

    RunResult Session::Run(uint64_t end, uint64_t count, bool untilReturn, uint64_t lastSeq)
    {
        RunResult result;
        if(!mContext)
        {
            result.reason = "no Triton session (sym.start or sym.trace)";
            result.error = true;
            return result;
        }
        try
        {
            if(mSource == Source::Trace)
                result = RunTrace(end, count, lastSeq);
            else
                result = RunDebuggee(end, count, untilReturn);
        }
        catch(const std::exception & e)
        {
            result.reason = std::string("Triton: ") + e.what();
            result.error = true;
        }
        result.ip = Ip();
        return result;
    }

    RunResult Session::RunDebuggee(uint64_t end, uint64_t count, bool untilReturn)
    {
        RunResult result;
        auto & ctx = *mContext;
        auto & pc = ctx.getRegister(mPointerSize == 8 ? "rip" : "eip");
        auto & sp = ctx.getRegister(mPointerSize == 8 ? "rsp" : "esp");
        const uint64_t startSp = static_cast<uint64_t>(ctx.getConcreteRegisterValue(sp, false));
        for(;;)
        {
            const uint64_t ip = static_cast<uint64_t>(ctx.getConcreteRegisterValue(pc, false));
            if(result.executed && end && ip == end)
            {
                result.reason = "reached " + Hex(end);
                break;
            }
            if(count && result.executed >= count)
            {
                result.reason = std::to_string(count) + " instructions";
                break;
            }
            mReadFailed = false;
            auto opcode = ctx.getConcreteMemoryAreaValue(ip, 16, true);
            if(mReadFailed && mReadFailAddress < ip + 1)
            {
                result.reason = "cannot read the code at " + Hex(ip);
                result.error = true;
                break;
            }
            triton::arch::Instruction inst(ip, opcode.data(), (triton::uint32)opcode.size());
            inst.setThreadId(mThread);
            ctx.disassembly(inst);
            const auto type = inst.getType();
            if(type == triton::arch::x86::ID_INS_SYSCALL || type == triton::arch::x86::ID_INS_SYSENTER || type == triton::arch::x86::ID_INS_INT ||
                    type == triton::arch::x86::ID_INS_INT1 || type == triton::arch::x86::ID_INS_INTO)
            {
                result.reason = "system call at " + Hex(ip) + " (" + inst.getDisassembly() + "): Triton does not run the kernel";
                break;
            }
            mReadFailed = false;
            const auto fault = ctx.processing(inst);
            if(fault != triton::arch::NO_FAULT)
            {
                static const char* names[] = { "", "divide by zero", "breakpoint", "instruction not supported by Triton", "general protection fault" };
                result.reason = std::string(names[fault]) + " at " + Hex(ip) + " (" + inst.getDisassembly() + ")";
                result.error = fault != triton::arch::FAULT_BP;
                // the instruction did not execute
                ctx.setConcreteRegisterValue(pc, triton::uint512(ip), false);
                break;
            }
            result.executed++;
            mExecuted++;
            Record(inst);
            if(mReadFailed)
            {
                result.reason = "access to unreadable memory at " + Hex(mReadFailAddress) + " by " + Hex(ip) + " (" + inst.getDisassembly() + ")";
                result.error = true;
                break;
            }
            if(untilReturn && type == triton::arch::x86::ID_INS_RET &&
                    static_cast<uint64_t>(ctx.getConcreteRegisterValue(sp, false)) > startSp)
            {
                result.reason = "returned to " + Hex(static_cast<uint64_t>(ctx.getConcreteRegisterValue(pc, false)));
                break;
            }
            if(type == triton::arch::x86::ID_INS_HLT)
            {
                result.reason = "hlt at " + Hex(ip);
                break;
            }
        }
        return result;
    }

    RunResult Session::RunTrace(uint64_t end, uint64_t count, uint64_t lastSeq)
    {
        RunResult result;
        auto & ctx = *mContext;
        auto & pc = ctx.getRegister(mPointerSize == 8 ? "rip" : "eip");
        if(mTraceEnded)
        {
            result.reason = "end of the thread's trace";
            return result;
        }
        for(;;)
        {
            if(count && result.executed >= count)
            {
                result.reason = std::to_string(count) + " instructions";
                break;
            }
            if(mSeq > lastSeq)
            {
                result.reason = "instruction " + std::to_string(lastSeq) + " replayed";
                break;
            }
            ftt::Instruction ins;
            ftt::RegisterFile regs;
            if(!mReader->instruction(mSeq, ins) || !mReader->registers(mThread, mSeq, regs))
            {
                result.reason = "cannot read instruction " + std::to_string(mSeq) + " of the trace";
                result.error = true;
                break;
            }
            if(result.executed && end && ins.ip == end)
            {
                result.reason = "reached " + Hex(end) + " (instruction " + std::to_string(mSeq) + ")";
                break;
            }
            SyncTrace(ins, regs);
            triton::arch::Instruction inst(ins.ip, ins.opcode, ins.opcodeSize);
            inst.setThreadId(ins.thread);
            const auto fault = ctx.processing(inst);
            if(fault != triton::arch::NO_FAULT && fault != triton::arch::FAULT_BP)
            {
                // an instruction Triton cannot execute: its effects come from the trace (the next
                // synchronization), only the symbolic data it touched is lost
                result.reason.clear();
            }
            result.executed++;
            mExecuted++;
            Record(inst);
            uint64_t next = 0;
            if(!mReader->nextInThread(mThread, mSeq, next, true))
            {
                mTraceEnded = true;
                result.reason = "end of the thread's trace (instruction " + std::to_string(mSeq) + ")";
                break;
            }
            mSeq = next;
            // the trace decides where the thread goes (calls skipped while tracing, exceptions)
            ftt::Instruction following;
            if(mReader->instruction(mSeq, following))
                ctx.setConcreteRegisterValue(pc, triton::uint512(following.ip), false);
        }
        return result;
    }
}
