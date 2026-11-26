// Virtualized code in a trace (the method of VMHunt, "VMHunt: A Verifiable Approach to Partially-
// Virtualized Binary Code Simplification", Xu et al., CCS 2018, reimplemented here on Fathom's traces):
//
//   vm.detect [file]     finds the virtualized snippets of a trace (.ftt / .trace64 / .trace32; default:
//                        the trace of the sym.trace session). A virtual machine enters by saving the whole
//                        context (a burst of pushes of nearly every register, or pushad) and leaves by
//                        restoring it (a burst of pops); in between, a dispatcher (an indirect jump, or a
//                        ret used as one) runs again and again to the handlers. $result = the snippets
//   vm.list              the snippets found
//   vm.simplify n        replays snippet n in Triton with the registers at its entry as symbolic inputs
//                        (rax_in...), and prints what the registers hold at its exit, simplified (Z3) and,
//                        when Triton finds one, synthesized: the semantics of the virtualized code without
//                        the virtual machine. The session stays open (sym.* on the result)
#include "session.h"

#include "_plugins.h"

#include <triton/ast.hpp>
#include <triton/x86Specifications.hpp>

#include <cinttypes>
#include <cstdarg>
#include <deque>
#include <map>
#include <sstream>
#include <unordered_map>

#define LOG_PREFIX "[Triton] "

namespace fathom_triton
{
    namespace
    {
        void Log(const char* format, ...)
        {
            char text[4096];
            va_list args;
            va_start(args, format);
            vsnprintf(text, sizeof(text), format, args);
            va_end(args);
            _plugin_logprintf(LOG_PREFIX "%s", text);
        }

        void SetResult(const char* name, uint64_t value)
        {
            char command[64];
            sprintf_s(command, "mov %s, 0x%llX", name, (unsigned long long)value);
            DbgCmdExecDirect(command);
        }

        struct Snippet
        {
            uint32_t thread = 0;
            uint64_t entry = 0; // sequence number of the first instruction of the context save
            uint64_t exit = 0; // sequence number of the last instruction of the context restore
            uint64_t entryAddress = 0;
            uint64_t exitAddress = 0;
            uint64_t instructions = 0;
            uint64_t dispatcher = 0; // address of the dispatching indirect jump
            uint64_t dispatches = 0;
            size_t handlers = 0;
            bool closed = true; // false: the trace ends inside the virtual machine
        };

        std::wstring gPath;
        std::vector<Snippet> gSnippets;

        // What vm.detect needs of an instruction
        struct Decoded
        {
            enum Kind { Other, Push, Pop, PushAll, PopAll, Indirect } kind = Other;
            int reg = -1; // the pushed / popped register (parent id), -2: the flags
        };

        std::string Narrow(const std::wstring & text)
        {
            if(text.empty())
                return {};
            int size = WideCharToMultiByte(CP_UTF8, 0, text.c_str(), (int)text.size(), nullptr, 0, nullptr, nullptr);
            std::string result(size, '\0');
            WideCharToMultiByte(CP_UTF8, 0, text.c_str(), (int)text.size(), &result[0], size, nullptr, nullptr);
            return result;
        }

        std::wstring Widen(const std::string & text)
        {
            if(text.empty())
                return {};
            int size = MultiByteToWideChar(CP_UTF8, 0, text.c_str(), (int)text.size(), nullptr, 0);
            std::wstring result(size, L'\0');
            MultiByteToWideChar(CP_UTF8, 0, text.c_str(), (int)text.size(), &result[0], size);
            return result;
        }

        bool Detect(const std::wstring & path, std::string & error)
        {
            ftt::Reader reader;
            if(!reader.open(path, error))
                return false;
            const unsigned pointerSize = reader.pointerSize();
            triton::Context ctx(pointerSize == 8 ? triton::arch::ARCH_X86_64 : triton::arch::ARCH_X86);
            // a context save covers nearly every general register: 12 of the 16 of x64 (a function
            // prologue saves at most the 8 non-volatile ones), 7 of the 8 of x86 (a prologue: 4)
            const size_t threshold = pointerSize == 8 ? 12 : 7;
            const size_t window = pointerSize == 8 ? 64 : 48;
            const int allRegisters = pointerSize == 8 ? 17 : 9;

            std::unordered_map<std::string, Decoded> cache;
            auto decode = [&](const ftt::Instruction & ins) -> Decoded
            {
                std::string key(reinterpret_cast<const char*>(ins.opcode), ins.opcodeSize);
                key.append(reinterpret_cast<const char*>(&ins.ip), sizeof(ins.ip));
                auto it = cache.find(key);
                if(it != cache.end())
                    return it->second;
                Decoded d;
                try
                {
                    triton::arch::Instruction inst(ins.ip, ins.opcode, ins.opcodeSize);
                    ctx.disassembly(inst);
                    namespace x86 = triton::arch::x86;
                    const auto type = inst.getType();
                    if(type == x86::ID_INS_PUSHAL || type == x86::ID_INS_PUSHAW)
                        d.kind = Decoded::PushAll;
                    else if(type == x86::ID_INS_POPAL || type == x86::ID_INS_POPAW)
                        d.kind = Decoded::PopAll;
                    else if(type == x86::ID_INS_PUSHF || type == x86::ID_INS_PUSHFD || type == x86::ID_INS_PUSHFQ)
                        d.kind = Decoded::Push, d.reg = -2;
                    else if(type == x86::ID_INS_POPF || type == x86::ID_INS_POPFD || type == x86::ID_INS_POPFQ)
                        d.kind = Decoded::Pop, d.reg = -2;
                    else if((type == x86::ID_INS_PUSH || type == x86::ID_INS_POP) && !inst.operands.empty() &&
                            inst.operands[0].getType() == triton::arch::OP_REG)
                    {
                        d.kind = type == x86::ID_INS_PUSH ? Decoded::Push : Decoded::Pop;
                        d.reg = (int)ctx.getParentRegister(inst.operands[0].getConstRegister()).getId();
                    }
                    else if(type == x86::ID_INS_RET ||
                            (type == x86::ID_INS_JMP && !inst.operands.empty() && inst.operands[0].getType() != triton::arch::OP_IMM))
                        d.kind = Decoded::Indirect;
                }
                catch(const std::exception &)
                {
                }
                cache.emplace(std::move(key), d);
                return d;
            };

            gSnippets.clear();
            for(auto & thread : reader.threads())
            {
                // pushes / pops of the last `window` instructions of the thread: (position, register)
                std::deque<std::pair<uint64_t, std::pair<int, uint64_t>>> pushes, pops;
                bool inside = false;
                Snippet current;
                std::map<uint64_t, std::pair<uint64_t, std::map<uint64_t, uint64_t>>> jumps; // address -> count, targets
                uint64_t position = 0, pendingIndirect = 0;
                bool hasPending = false;
                uint64_t lastSeq = 0, lastIp = 0;

                auto distinct = [](const std::deque<std::pair<uint64_t, std::pair<int, uint64_t>>> & list, bool & all)
                {
                    std::map<int, bool> seen;
                    all = false;
                    for(auto & e : list)
                    {
                        if(e.second.first == INT_MIN)
                            all = true;
                        seen[e.second.first] = true;
                    }
                    return seen.size();
                };
                auto close = [&](uint64_t exitSeq, uint64_t exitIp, bool closed)
                {
                    current.exit = exitSeq;
                    current.exitAddress = exitIp;
                    current.closed = closed;
                    for(auto & j : jumps)
                    {
                        if(j.second.first > current.dispatches)
                        {
                            current.dispatches = j.second.first;
                            current.dispatcher = j.first;
                            current.handlers = j.second.second.size();
                        }
                    }
                    // a virtual machine dispatches: a context save without it is not one
                    if(current.dispatches >= 3 && current.handlers >= 2)
                        gSnippets.push_back(current);
                    inside = false;
                    jumps.clear();
                };

                reader.forEach(thread.thread, [&](const ftt::Instruction & ins, const ftt::RegisterFile &)
                {
                    position++;
                    // the target of the previous indirect jump: this instruction
                    if(hasPending && inside)
                    {
                        auto & j = jumps[pendingIndirect];
                        j.first++;
                        j.second[ins.ip]++;
                    }
                    hasPending = false;
                    const Decoded d = decode(ins);
                    while(!pushes.empty() && position - pushes.front().first > window)
                        pushes.pop_front();
                    while(!pops.empty() && position - pops.front().first > window)
                        pops.pop_front();
                    if(inside)
                        current.instructions++;
                    switch(d.kind)
                    {
                    case Decoded::Push:
                    case Decoded::PushAll:
                        if(!inside)
                        {
                            pushes.push_back({ position, { d.kind == Decoded::PushAll ? INT_MIN : d.reg, ins.seq } });
                            bool all = false;
                            const size_t n = distinct(pushes, all);
                            if(all || n >= threshold)
                            {
                                inside = true;
                                current = Snippet();
                                current.thread = thread.thread;
                                // the entry: the start of the shortest run of the last pushes that saves
                                // the context (a push before it, of the caller, is not part of it)
                                {
                                    std::map<int, bool> seen;
                                    current.entry = pushes.back().second.second;
                                    for(auto e = pushes.rbegin(); e != pushes.rend(); ++e)
                                    {
                                        seen[e->second.first] = true;
                                        current.entry = e->second.second;
                                        if(e->second.first == INT_MIN || seen.size() >= threshold)
                                            break;
                                    }
                                }
                                uint64_t first = current.entry;
                                ftt::Instruction e;
                                current.entryAddress = reader.instruction(first, e) ? e.ip : ins.ip;
                                current.instructions = 0;
                                pushes.clear();
                                pops.clear();
                            }
                        }
                        break;
                    case Decoded::Pop:
                    case Decoded::PopAll:
                        if(inside)
                        {
                            pops.push_back({ position, { d.kind == Decoded::PopAll ? INT_MIN : d.reg, ins.seq } });
                            bool all = false;
                            const size_t n = distinct(pops, all);
                            if(all || n >= threshold)
                            {
                                close(ins.seq, ins.ip, true);
                                pops.clear();
                            }
                        }
                        break;
                    case Decoded::Indirect:
                        pendingIndirect = ins.ip;
                        hasPending = true;
                        break;
                    default:
                        break;
                    }
                    lastSeq = ins.seq;
                    lastIp = ins.ip;
                    return true;
                });
                (void)allRegisters;
                if(inside)
                    close(lastSeq, lastIp, false);
            }
            gPath = path;
            return true;
        }

        void List()
        {
            Log("%zu virtualized snippets in %s\n", gSnippets.size(), Narrow(gPath).c_str());
            for(size_t i = 0; i < gSnippets.size(); i++)
            {
                auto & s = gSnippets[i];
                Log("  %zu: thread %u, instructions %" PRIu64 " to %" PRIu64 " (%" PRIu64 " executed), entry %s, exit %s%s; dispatcher %s: %" PRIu64
                    " dispatches to %zu handlers\n", i, s.thread, s.entry, s.exit, s.instructions, Hex(s.entryAddress).c_str(), Hex(s.exitAddress).c_str(),
                    s.closed ? "" : " (the trace ends inside)", Hex(s.dispatcher).c_str(), s.dispatches, s.handlers);
            }
        }

        bool CbDetect(int argc, char** argv)
        {
            std::wstring path;
            if(argc > 1)
            {
                std::string text = argv[1];
                if(text.size() >= 2 && text.front() == '"' && text.back() == '"')
                    text = text.substr(1, text.size() - 2);
                path = Widen(text);
                if(!ftt::Reader::IsFtt(path))
                {
                    // an x64dbg trace: converted as sym.trace does (and kept: sym.trace reuses it)
                    if(!DbgCmdExecDirect(("sym.trace \"" + text + "\"").c_str()))
                        return false;
                    path = Session::Get().TracePath();
                    Session::Get().Stop();
                }
            }
            else
            {
                if(Session::Get().GetSource() != Source::Trace)
                {
                    Log("vm.detect file (or a sym.trace session first)\n");
                    return false;
                }
                path = Session::Get().TracePath();
            }
            std::string error;
            const DWORD began = GetTickCount();
            if(!Detect(path, error))
            {
                Log("vm.detect: %s\n", error.c_str());
                return false;
            }
            List();
            Log("(%lu ms)\n", GetTickCount() - began);
            SetResult("$result", gSnippets.size());
            return true;
        }

        bool CbList(int, char**)
        {
            List();
            SetResult("$result", gSnippets.size());
            return true;
        }

        bool CbSimplify(int argc, char** argv)
        {
            uint64_t index = 0;
            if(argc > 1)
            {
                bool ok = false;
                index = DbgEval(argv[1], &ok);
                if(!ok)
                {
                    Log("invalid expression: %s\n", argv[1]);
                    return false;
                }
            }
            if(index >= gSnippets.size())
            {
                Log("vm.simplify n: %zu snippets (vm.detect first)\n", gSnippets.size());
                return false;
            }
            const Snippet s = gSnippets[(size_t)index];
            auto & session = Session::Get();
            std::string error;
            if(!session.StartTrace(gPath, s.entry, s.thread, error))
            {
                Log("vm.simplify: %s\n", error.c_str());
                return false;
            }
            auto & ctx = session.Context();
            static const char* regs64[] = { "rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15" };
            static const char* regs32[] = { "eax", "ebx", "ecx", "edx", "esi", "edi", "ebp" };
            std::vector<const char*> names;
            if(session.PointerSize() == 8)
                names.assign(std::begin(regs64), std::end(regs64));
            else
                names.assign(std::begin(regs32), std::end(regs32));
            for(auto name : names)
                ctx.symbolizeRegister(ctx.getRegister(name), std::string(name) + "_in");
            auto result = session.Run(0, 0, false, s.exit);
            Log("snippet %" PRIu64 " replayed: %" PRIu64 " instructions (%s); its effect on the registers:\n", index, result.executed, result.reason.c_str());
            size_t outputs = 0;
            for(auto name : names)
            {
                auto & reg = ctx.getRegister(name);
                if(!ctx.isRegisterSymbolized(reg))
                {
                    const auto value = ctx.getConcreteRegisterValue(reg);
                    Log("  %s = %s (constant)\n", name, Hex(value).c_str());
                    continue;
                }
                auto node = triton::ast::unroll(ctx.getRegisterAst(reg));
                try
                {
                    node = ctx.simplify(node, true);
                }
                catch(const std::exception &)
                {
                }
                std::ostringstream text;
                ctx.setAstRepresentationMode(triton::ast::representations::PYTHON_REPRESENTATION);
                text << node;
                std::string expression = text.str();
                if(expression == std::string(name) + "_in")
                    continue; // unchanged
                outputs++;
                if(expression.size() > 1500)
                    expression = expression.substr(0, 1500) + "... (" + std::to_string(expression.size()) + " characters)";
                Log("  %s = %s\n", name, expression.c_str());
                try
                {
                    auto synth = ctx.synthesize(node, true, true, true);
                    if(synth.successful())
                    {
                        std::ostringstream simpler;
                        simpler << synth.getOutput();
                        if(simpler.str() != text.str())
                            Log("      equivalent to %s\n", simpler.str().c_str());
                    }
                }
                catch(const std::exception &)
                {
                }
            }
            Log("%zu registers computed from the inputs (the session stays open: sym.expr, sym.slice...)\n", outputs);
            SetResult("$result", outputs);
            return true;
        }
    }

    void RegisterVmCommands(int handle)
    {
        _plugin_registercommand(handle, "vm.detect", CbDetect, false);
        _plugin_registercommand(handle, "vm.list", CbList, false);
        _plugin_registercommand(handle, "vm.simplify", CbSimplify, false);
    }
}
