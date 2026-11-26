// The commands of the Triton plugin: symbolic execution and taint analysis with Triton.
//
//   sym.start                          a session on the debuggee's current thread (registers now, memory
//                                      read on demand); Triton emulates, the debuggee does not move
//   sym.trace file[, seq[, thread]]    a session on a recorded trace (.ftt, or a .trace32 / .trace64
//                                      converted once), from instruction seq (default 0) of its thread
//   sym.stop                           ends the session
//   sym.info                           the state of the session
//   sym.reg reg[, name]                makes a register symbolic (it keeps its concrete value)
//   sym.mem address, size[, name]      makes memory symbolic, one 8-bit variable per byte (name_0...)
//   sym.memvar address, size[, name]   makes memory symbolic as one variable of 1, 2, 4 or 8 bytes
//   sym.concrete reg | address[, size] drops the symbolic data of a register / memory
//   taint.reg reg / taint.mem address, size / untaint.reg reg / untaint.mem address, size
//   sym.step [count]                   runs count instructions (default 1)
//   sym.run [end[, count]]             runs to end; without end: debuggee source, until the current
//                                      function returns; trace source, to the end of the thread.
//                                      count: at most (default 10,000,000). $result = the address reached
//   sym.expr reg | address[, size]     the expression of a register / memory, simplified (Z3);
//                                      $result = its concrete value, $result1 = 1 if symbolic
//   sym.smt reg | address[, size]      the same in SMT-LIB
//   sym.synth reg | address[, size]    synthesizes a simpler equivalent expression (deobfuscation)
//   sym.slice reg | address[, size]    the instructions the expression comes from
//   sym.vars                           the symbolic variables
//   sym.branches                       the branches that depend on symbolic data
//   sym.flip n                         the input that takes the other side of branch n; $result = 1 if found
//   sym.reach address                  the input that reaches address through a recorded branch
//   sym.solve target, value[, size]    the input for which target (reg or address) equals value at
//                                      this point of the path
//   sym.apply                          writes the last solution into the debuggee
//   sym.list [count]                   the last instructions that used symbolic or tainted data
//   sym.mark / sym.unmark              comments them in the disassembly
#include "session.h"

#include "_plugins.h"

#include <triton/ast.hpp>
#include <triton/x86Specifications.hpp>

#include <algorithm>
#include <cinttypes>
#include <cstdarg>
#include <map>
#include <sstream>

#define LOG_PREFIX "[Triton] "

namespace fathom_triton
{
    extern int gPluginHandle;
    void RegisterVmCommands(int handle);

    static void Log(const char* format, ...)
    {
        char text[4096];
        va_list args;
        va_start(args, format);
        vsnprintf(text, sizeof(text), format, args);
        va_end(args);
        _plugin_logprintf(LOG_PREFIX "%s", text);
    }

    // Long text (expressions): the log prints at most a few thousand characters per call
    static void LogLong(const std::string & prefix, const std::string & text, size_t limit = 20000)
    {
        std::string shown = text.size() > limit ? text.substr(0, limit) + "... (" + std::to_string(text.size()) + " characters)" : text;
        _plugin_logprintf(LOG_PREFIX "%s", prefix.c_str());
        for(size_t i = 0; i < shown.size(); i += 2000)
            _plugin_logputs(shown.substr(i, 2000).c_str());
    }

    static void SetResult(const char* name, uint64_t value)
    {
        char command[64];
        sprintf_s(command, "mov %s, 0x%llX", name, (unsigned long long)value);
        DbgCmdExecDirect(command);
    }

    // An expression in the state of the session: its registers are those of Triton (after sym.run, or
    // at the position of a trace, not the debuggee's), and [address] reads the session's memory (the
    // trace's, for a trace). The rest (numbers, labels, operators, variables) goes to x64dbg's evaluator.
    static std::string SessionExpression(const std::string & expression, bool & ok)
    {
        auto & s = Session::Get();
        if(!s.Active())
            return expression;
        auto & ctx = s.Context();
        // registers, replaced by their value (not inside labels such as module.function or module!export)
        std::string text;
        for(size_t i = 0; i < expression.size();)
        {
            const char ch = expression[i];
            if(isalpha((unsigned char)ch) || ch == '_')
            {
                size_t j = i;
                while(j < expression.size() && (isalnum((unsigned char)expression[j]) || expression[j] == '_'))
                    j++;
                const std::string word = expression.substr(i, j - i);
                const bool label = (i > 0 && (expression[i - 1] == '.' || expression[i - 1] == '!' || expression[i - 1] == '$')) ||
                                   (j < expression.size() && (expression[j] == '.' || expression[j] == '!' || expression[j] == ':'));
                const triton::arch::Register* reg = label ? nullptr : s.Register(word);
                if(reg && reg->getBitSize() <= 64)
                    text += Hex(static_cast<uint64_t>(ctx.getConcreteRegisterValue(*reg)));
                else
                    text += word;
                i = j;
                continue;
            }
            text.push_back(ch);
            i++;
        }
        // [address]: the innermost first, a pointer-sized value read from the session
        for(;;)
        {
            const auto close = text.find(']');
            if(close == std::string::npos)
                break;
            const auto open = text.rfind('[', close);
            if(open == std::string::npos)
                break;
            bool inner = false;
            const uint64_t address = DbgEval(text.substr(open + 1, close - open - 1).c_str(), &inner);
            if(!inner)
            {
                ok = false;
                return expression;
            }
            const auto value = ctx.getConcreteMemoryValue(triton::arch::MemoryAccess(address, s.PointerSize()));
            text = text.substr(0, open) + Hex(static_cast<uint64_t>(value)) + text.substr(close + 1);
        }
        return text;
    }

    static bool Eval(const char* expression, uint64_t & value)
    {
        bool ok = true;
        const std::string text = SessionExpression(expression, ok);
        if(ok)
            value = DbgEval(text.c_str(), &ok);
        if(!ok)
            Log("invalid expression: %s\n", expression);
        return ok;
    }

    static Session & Need()
    {
        return Session::Get();
    }

    static bool Active()
    {
        if(Session::Get().Active())
            return true;
        Log("no session: sym.start (the debuggee) or sym.trace (a recorded trace) first\n");
        return false;
    }

    static std::string Narrow(const std::wstring & text)
    {
        if(text.empty())
            return {};
        int size = WideCharToMultiByte(CP_UTF8, 0, text.c_str(), (int)text.size(), nullptr, 0, nullptr, nullptr);
        std::string result(size, '\0');
        WideCharToMultiByte(CP_UTF8, 0, text.c_str(), (int)text.size(), &result[0], size, nullptr, nullptr);
        return result;
    }

    static std::wstring Widen(const std::string & text)
    {
        if(text.empty())
            return {};
        int size = MultiByteToWideChar(CP_UTF8, 0, text.c_str(), (int)text.size(), nullptr, 0);
        std::wstring result(size, L'\0');
        MultiByteToWideChar(CP_UTF8, 0, text.c_str(), (int)text.size(), &result[0], size);
        return result;
    }

    static std::string Unquote(std::string text)
    {
        if(text.size() >= 2 && text.front() == '"' && text.back() == '"')
            text = text.substr(1, text.size() - 2);
        return text;
    }

    // A register, or memory (address, size): the target of sym.expr / sym.slice / sym.solve
    struct Target
    {
        const triton::arch::Register* reg = nullptr;
        uint64_t address = 0;
        uint32_t size = 0;
        std::string text;
    };

    static bool ParseTarget(int argc, char** argv, int index, Target & target, bool sizeAllowed = true)
    {
        auto & s = Session::Get();
        if(argc <= index)
        {
            Log("%s: a register or an address is needed\n", argv[0]);
            return false;
        }
        target.text = argv[index];
        target.reg = s.Register(argv[index]);
        if(target.reg)
            return true;
        if(!Eval(argv[index], target.address))
            return false;
        target.size = s.PointerSize();
        if(sizeAllowed && argc > index + 1)
        {
            uint64_t size = 0;
            if(!Eval(argv[index + 1], size))
                return false;
            if(size == 0 || size > 64)
            {
                Log("size %" PRIu64 ": 1 to 64 bytes\n", size);
                return false;
            }
            target.size = (uint32_t)size;
        }
        target.text = Hex(target.address) + " (" + std::to_string(target.size) + " bytes)";
        return true;
    }

    static triton::ast::SharedAbstractNode TargetAst(const Target & target)
    {
        auto & ctx = Session::Get().Context();
        if(target.reg)
            return ctx.getRegisterAst(*target.reg);
        return ctx.getMemoryAst(triton::arch::MemoryAccess(target.address, target.size));
    }

    static bool TargetSymbolic(const Target & target)
    {
        auto & ctx = Session::Get().Context();
        if(target.reg)
            return ctx.isRegisterSymbolized(*target.reg);
        return ctx.isMemorySymbolized(target.address, target.size);
    }

    static triton::uint512 TargetValue(const Target & target)
    {
        auto & ctx = Session::Get().Context();
        if(target.reg)
            return ctx.getConcreteRegisterValue(*target.reg);
        return ctx.getConcreteMemoryValue(triton::arch::MemoryAccess(target.address, target.size));
    }

    static std::string AstText(const triton::ast::SharedAbstractNode & node, triton::ast::representations::mode_e mode)
    {
        auto & ctx = Session::Get().Context();
        ctx.setAstRepresentationMode(mode);
        std::ostringstream text;
        text << node;
        ctx.setAstRepresentationMode(triton::ast::representations::PYTHON_REPRESENTATION);
        return text.str();
    }

    static std::string VariableText(const triton::engines::symbolic::SharedSymbolicVariable & var)
    {
        auto & ctx = Session::Get().Context();
        std::string where;
        if(var->getType() == triton::engines::symbolic::MEMORY_VARIABLE)
            where = "[" + Hex(var->getOrigin()) + "]";
        else if(var->getType() == triton::engines::symbolic::REGISTER_VARIABLE)
            where = ctx.getRegister((triton::arch::register_e)var->getOrigin()).getName();
        std::string name = var->getAlias().empty() ? var->getName() : var->getAlias() + " (" + var->getName() + ")";
        return name + (where.empty() ? "" : " " + where) + ", " + std::to_string(var->getSize()) + " bits";
    }

    // Prints a solver model; memory bytes that follow each other are also shown as text
    static void PrintModel(const std::unordered_map<triton::usize, triton::engines::solver::SolverModel> & model)
    {
        std::map<triton::usize, const triton::engines::solver::SolverModel*> sorted;
        for(auto & it : model)
            sorted[it.first] = &it.second;
        std::map<uint64_t, uint8_t> bytes;
        for(auto & it : sorted)
        {
            auto & var = it.second->getVariable();
            Log("  %s = %s\n", VariableText(var).c_str(), Hex(it.second->getValue()).c_str());
            if(var->getType() == triton::engines::symbolic::MEMORY_VARIABLE && var->getSize() == 8)
                bytes[var->getOrigin()] = static_cast<uint8_t>(static_cast<uint64_t>(it.second->getValue()));
        }
        // runs of bytes as text
        std::string run;
        uint64_t start = 0, last = 0;
        auto flush = [&]()
        {
            if(run.size() >= 2)
                Log("  bytes at %s: \"%s\"\n", Hex(start).c_str(), run.c_str());
            run.clear();
        };
        for(auto & b : bytes)
        {
            if(!run.empty() && b.first != last + 1)
                flush();
            if(run.empty())
                start = b.first;
            const uint8_t c = b.second;
            if(c >= 0x20 && c < 0x7F && c != '"' && c != '\\')
                run.push_back((char)c);
            else
            {
                char esc[8];
                sprintf_s(esc, "\\x%02X", c);
                run += esc;
            }
            last = b.first;
        }
        flush();
    }

    static bool Solve(const triton::ast::SharedAbstractNode & constraint, const char* what)
    {
        auto & s = Session::Get();
        auto & ctx = s.Context();
        triton::engines::solver::status_e status = triton::engines::solver::UNKNOWN;
        triton::uint32 time = 0;
        auto model = ctx.getModel(constraint, &status, 0, &time);
        static const char* names[] = { "unsatisfiable", "satisfiable", "timeout", "out of memory", "unknown" };
        if(status != triton::engines::solver::SAT)
        {
            Log("%s: %s (%u ms)\n", what, names[status], time);
            SetResult("$result", 0);
            return false;
        }
        Log("%s: solution found (%u ms)%s\n", what, time, model.empty() ? ", any input" : ":");
        PrintModel(model);
        s.lastModel = model;
        SetResult("$result", 1);
        return true;
    }

    // ---------------------------------------------------------------------------------- session ---
    static bool CbStart(int, char**)
    {
        std::string error;
        if(!Session::Get().StartDebuggee(error))
        {
            Log("sym.start: %s\n", error.c_str());
            return false;
        }
        auto & s = Session::Get();
        Log("session on thread %u of the debuggee at %s (%s): Triton emulates from here, the debuggee does not move\n",
            s.Thread(), Hex(s.Ip()).c_str(), s.PointerSize() == 8 ? "x86-64" : "x86");
        return true;
    }

    static bool CbTrace(int argc, char** argv)
    {
        if(argc < 2)
        {
            Log("sym.trace file[, seq[, thread]]\n");
            return false;
        }
        std::wstring path = Widen(Unquote(argv[1]));
        uint64_t seq = 0, thread = 0;
        if(argc > 2 && !Eval(argv[2], seq))
            return false;
        if(argc > 3 && !Eval(argv[3], thread))
            return false;
        if(!ftt::Reader::IsFtt(path))
        {
            // an x64dbg trace: converted to .ftt once (as the Time Travel tab does), in %TEMP%
            wchar_t temp[MAX_PATH] = L"";
            GetTempPathW(_countof(temp), temp);
            std::wstring folder = std::wstring(temp) + L"FathomTimeTravel";
            CreateDirectoryW(folder.c_str(), nullptr);
            auto slash = path.find_last_of(L"\\/");
            std::wstring ftt = folder + L"\\" + (slash == std::wstring::npos ? path : path.substr(slash + 1)) + L".ftt";
            // converted again when the trace is newer than its conversion (it may have been recorded again)
            WIN32_FILE_ATTRIBUTE_DATA source = {}, converted = {};
            const bool haveSource = GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &source) != 0;
            const bool haveConverted = GetFileAttributesExW(ftt.c_str(), GetFileExInfoStandard, &converted) != 0;
            if(!haveSource)
            {
                Log("sym.trace: cannot open %s\n", Narrow(path).c_str());
                return false;
            }
            if(!haveConverted || CompareFileTime(&source.ftLastWriteTime, &converted.ftLastWriteTime) > 0)
            {
                const std::string command = "TraceExport \"" + Narrow(path) + "\", \"" + Narrow(ftt) + "\", ftt, regs=all";
                if(!DbgCmdExecDirect(command.c_str()) || !ftt::Reader::IsFtt(ftt))
                {
                    Log("sym.trace: the conversion to .ftt failed (see the log): is it a .trace32 / .trace64 file?\n");
                    return false;
                }
            }
            path = ftt;
        }
        std::string error;
        auto & s = Session::Get();
        if(!s.StartTrace(path, seq, (uint32_t)thread, error))
        {
            Log("sym.trace: %s\n", error.c_str());
            return false;
        }
        Log("session on the trace %s, thread %u, from instruction %" PRIu64 " (%s)\n", Narrow(path).c_str(), s.Thread(), s.Seq(), Hex(s.Ip()).c_str());
        return true;
    }

    static bool CbStop(int, char**)
    {
        Session::Get().Stop();
        Log("session ended\n");
        return true;
    }

    static bool CbInfo(int, char**)
    {
        if(!Active())
            return false;
        auto & s = Need();
        auto & ctx = s.Context();
        if(s.GetSource() == Source::Trace)
            Log("source: trace %s, thread %u, next instruction %" PRIu64 "\n", Narrow(s.TracePath()).c_str(), s.Thread(), s.Seq());
        else
            Log("source: the debuggee, thread %u\n", s.Thread());
        Log("ip %s, %" PRIu64 " instructions executed, %zu symbolic variables, %zu symbolic branches, %zu tainted bytes, %zu tainted registers\n",
            Hex(s.Ip()).c_str(), s.Executed(), ctx.getSymbolicVariables().size(), ctx.getPathConstraints().size(),
            ctx.getTaintedMemory().size(), ctx.getTaintedRegisters().size());
        if(s.desyncRegisters || s.desyncMemory)
            Log("symbolic data dropped where the trace disagreed (code outside the trace): %" PRIu64 " registers, %" PRIu64 " bytes\n",
                s.desyncRegisters, s.desyncMemory);
        return true;
    }

    // ------------------------------------------------------------------------- symbolize, taint ---
    static bool CbReg(int argc, char** argv)
    {
        if(!Active())
            return false;
        if(argc < 2)
        {
            Log("sym.reg reg[, name]\n");
            return false;
        }
        auto & s = Need();
        auto reg = s.Register(argv[1]);
        if(!reg)
        {
            Log("unknown register: %s\n", argv[1]);
            return false;
        }
        auto var = s.Context().symbolizeRegister(*reg, argc > 2 ? Unquote(argv[2]) : reg->getName());
        Log("%s is symbolic: %s, concrete value %s\n", reg->getName().c_str(), VariableText(var).c_str(),
            Hex(s.Context().getConcreteRegisterValue(*reg)).c_str());
        return true;
    }

    static bool CbMem(int argc, char** argv)
    {
        if(!Active())
            return false;
        uint64_t address = 0, size = 0;
        if(argc < 3)
        {
            Log("sym.mem address, size[, name]\n");
            return false;
        }
        if(!Eval(argv[1], address) || !Eval(argv[2], size))
            return false;
        if(size == 0 || size > 0x100000)
        {
            Log("size %" PRIu64 ": 1 to 0x100000 bytes\n", size);
            return false;
        }
        auto & s = Need();
        auto & ctx = s.Context();
        const std::string name = argc > 3 ? Unquote(argv[3]) : "mem";
        // the bytes keep their current value (read from the source now)
        std::vector<uint8_t> data;
        const bool readable = s.ReadSource(address, (size_t)size, data);
        for(uint64_t i = 0; i < size; i++)
        {
            if(!ctx.isConcreteMemoryValueDefined(address + i, 1) && readable)
                ctx.setConcreteMemoryAreaValue(address + i, &data[(size_t)i], 1, false);
            ctx.symbolizeMemory(triton::arch::MemoryAccess(address + i, 1), name + "_" + std::to_string(i));
        }
        Log("%" PRIu64 " bytes at %s are symbolic (%s_0 to %s_%" PRIu64 ")%s\n", size, Hex(address).c_str(), name.c_str(), name.c_str(),
            size - 1, readable ? "" : ", not all readable: their value is 0");
        return true;
    }

    static bool CbMemVar(int argc, char** argv)
    {
        if(!Active())
            return false;
        uint64_t address = 0, size = 0;
        if(argc < 3)
        {
            Log("sym.memvar address, size[, name] (size 1, 2, 4 or 8)\n");
            return false;
        }
        if(!Eval(argv[1], address) || !Eval(argv[2], size))
            return false;
        if(size != 1 && size != 2 && size != 4 && size != 8)
        {
            Log("size %" PRIu64 ": 1, 2, 4 or 8 bytes\n", size);
            return false;
        }
        auto & s = Need();
        auto & ctx = s.Context();
        std::vector<uint8_t> data;
        if(s.ReadSource(address, (size_t)size, data))
            for(uint64_t i = 0; i < size; i++)
                if(!ctx.isConcreteMemoryValueDefined(address + i, 1))
                    ctx.setConcreteMemoryAreaValue(address + i, &data[(size_t)i], 1, false);
        auto var = ctx.symbolizeMemory(triton::arch::MemoryAccess(address, (triton::uint32)size), argc > 3 ? Unquote(argv[3]) : "var");
        Log("%s is symbolic, concrete value %s\n", VariableText(var).c_str(),
            Hex(ctx.getConcreteMemoryValue(triton::arch::MemoryAccess(address, (triton::uint32)size))).c_str());
        return true;
    }

    static bool CbConcrete(int argc, char** argv)
    {
        if(!Active())
            return false;
        Target target;
        if(!ParseTarget(argc, argv, 1, target))
            return false;
        auto & ctx = Need().Context();
        if(target.reg)
            ctx.concretizeRegister(*target.reg);
        else
            for(uint32_t i = 0; i < target.size; i++)
                ctx.concretizeMemory(target.address + i);
        Log("%s is concrete\n", target.text.c_str());
        return true;
    }

    static bool Taint(int argc, char** argv, bool taint)
    {
        if(!Active())
            return false;
        Target target;
        if(!ParseTarget(argc, argv, 1, target))
            return false;
        auto & ctx = Need().Context();
        if(target.reg)
            taint ? ctx.taintRegister(*target.reg) : ctx.untaintRegister(*target.reg);
        else
            for(uint32_t i = 0; i < target.size; i++)
                taint ? ctx.taintMemory(target.address + i) : ctx.untaintMemory(target.address + i);
        Log("%s is %s\n", target.text.c_str(), taint ? "tainted" : "not tainted");
        return true;
    }

    static bool CbTaintReg(int argc, char** argv) { return Taint(argc, argv, true); }
    static bool CbUntaint(int argc, char** argv) { return Taint(argc, argv, false); }

    static bool CbTaintMem(int argc, char** argv)
    {
        if(argc < 3)
        {
            Log("%s address, size\n", argv[0]);
            return false;
        }
        return Taint(argc, argv, strstr(argv[0], "untaint") == nullptr);
    }

    // -------------------------------------------------------------------------------------- run ---
    static void Report(const RunResult & r)
    {
        auto & s = Need();
        Log("%" PRIu64 " instructions executed, stopped: %s; ip %s, %zu symbolic branches\n", r.executed, r.reason.c_str(),
            Hex(r.ip).c_str(), s.Context().getPathConstraints().size());
        SetResult("$result", r.ip);
        SetResult("$result1", r.executed);
    }

    static bool CbStep(int argc, char** argv)
    {
        if(!Active())
            return false;
        uint64_t count = 1;
        if(argc > 1 && !Eval(argv[1], count))
            return false;
        auto r = Need().Run(0, count ? count : 1, false);
        Report(r);
        return !r.error;
    }

    static bool CbRun(int argc, char** argv)
    {
        if(!Active())
            return false;
        uint64_t end = 0, count = 10000000;
        if(argc > 1 && !Eval(argv[1], end))
            return false;
        if(argc > 2 && !Eval(argv[2], count))
            return false;
        auto & s = Need();
        auto r = s.Run(end, count, end == 0 && s.GetSource() == Source::Debuggee);
        Report(r);
        return !r.error;
    }

    // ------------------------------------------------------------------------------ expressions ---
    static bool Expression(int argc, char** argv, triton::ast::representations::mode_e mode)
    {
        if(!Active())
            return false;
        Target target;
        if(!ParseTarget(argc, argv, 1, target))
            return false;
        auto & ctx = Need().Context();
        const bool symbolic = TargetSymbolic(target);
        const auto value = TargetValue(target);
        SetResult("$result", static_cast<uint64_t>(value));
        SetResult("$result1", symbolic ? 1 : 0);
        if(!symbolic)
        {
            Log("%s = %s (concrete)\n", target.text.c_str(), Hex(value).c_str());
            return true;
        }
        auto node = triton::ast::unroll(TargetAst(target));
        // Z3's simplifier, then Triton's own passes
        try
        {
            node = ctx.simplify(node, true);
        }
        catch(const std::exception &)
        {
        }
        LogLong(target.text + " = " + Hex(value) + ", symbolic:\n", AstText(node, mode));
        return true;
    }

    static bool CbExpr(int argc, char** argv) { return Expression(argc, argv, triton::ast::representations::PYTHON_REPRESENTATION); }
    static bool CbSmt(int argc, char** argv) { return Expression(argc, argv, triton::ast::representations::SMT_REPRESENTATION); }

    static bool CbSynth(int argc, char** argv)
    {
        if(!Active())
            return false;
        Target target;
        if(!ParseTarget(argc, argv, 1, target))
            return false;
        if(!TargetSymbolic(target))
        {
            Log("%s is concrete: %s\n", target.text.c_str(), Hex(TargetValue(target)).c_str());
            return true;
        }
        auto & ctx = Need().Context();
        auto node = triton::ast::unroll(TargetAst(target));
        auto result = ctx.synthesize(node, true, true, true);
        if(!result.successful())
        {
            Log("no simpler expression found for %s\n", target.text.c_str());
            SetResult("$result", 0);
            return true;
        }
        LogLong(target.text + " is equivalent to:\n", AstText(result.getOutput(), triton::ast::representations::PYTHON_REPRESENTATION));
        SetResult("$result", 1);
        return true;
    }

    static bool CbSlice(int argc, char** argv)
    {
        if(!Active())
            return false;
        Target target;
        if(!ParseTarget(argc, argv, 1, target))
            return false;
        auto & ctx = Need().Context();
        std::map<triton::usize, triton::engines::symbolic::SharedSymbolicExpression> slice;
        auto add = [&](const triton::engines::symbolic::SharedSymbolicExpression & e)
        {
            if(!e)
                return;
            for(auto & it : ctx.sliceExpressions(e))
                slice[it.first] = it.second;
        };
        if(target.reg)
            add(ctx.getSymbolicRegister(*target.reg));
        else
            for(uint32_t i = 0; i < target.size; i++)
                add(ctx.getSymbolicMemory(target.address + i));
        if(slice.empty())
        {
            Log("%s does not depend on symbolic data\n", target.text.c_str());
            return true;
        }
        Log("%s comes from:\n", target.text.c_str());
        uint64_t previous = 0;
        size_t shown = 0;
        for(auto & it : slice)
        {
            auto & e = it.second;
            const uint64_t address = e->getAddress();
            if(address == 0 || address == previous)
                continue;
            previous = address;
            Log("  %s  %s\n", Hex(address).c_str(), e->getDisassembly().c_str());
            shown++;
        }
        SetResult("$result", shown);
        return true;
    }

    static bool CbVars(int, char**)
    {
        if(!Active())
            return false;
        auto & ctx = Need().Context();
        auto vars = ctx.getSymbolicVariables();
        Log("%zu symbolic variables\n", vars.size());
        size_t n = 0;
        for(auto & it : vars)
        {
            if(++n > 256)
            {
                Log("  ... %zu more\n", vars.size() - 256);
                break;
            }
            Log("  %s\n", VariableText(it.second).c_str());
        }
        return true;
    }

    // ------------------------------------------------------------------------- branches, solving ---
    static bool CbBranches(int, char**)
    {
        if(!Active())
            return false;
        auto & ctx = Need().Context();
        auto & pcs = ctx.getPathConstraints();
        Log("%zu branches depend on symbolic data\n", pcs.size());
        for(size_t i = 0; i < pcs.size(); i++)
        {
            auto & pc = pcs[i];
            if(!pc.isMultipleBranches())
            {
                Log("  %zu: %s -> %s (symbolic target)\n", i, Hex(pc.getSourceAddress()).c_str(), Hex(pc.getTakenAddress()).c_str());
                continue;
            }
            std::string other;
            for(auto & b : pc.getBranchConstraints())
                if(!std::get<0>(b))
                    other += (other.empty() ? "" : ", ") + Hex(std::get<2>(b));
            Log("  %zu: %s took %s, not %s\n", i, Hex(pc.getSourceAddress()).c_str(), Hex(pc.getTakenAddress()).c_str(), other.c_str());
        }
        SetResult("$result", pcs.size());
        return true;
    }

    static bool CbFlip(int argc, char** argv)
    {
        if(!Active())
            return false;
        uint64_t index = 0;
        if(argc < 2 || !Eval(argv[1], index))
        {
            Log("sym.flip n (n: a branch of sym.branches)\n");
            return false;
        }
        auto & ctx = Need().Context();
        auto actx = ctx.getAstContext();
        auto & pcs = ctx.getPathConstraints();
        if(index >= pcs.size())
        {
            Log("branch %" PRIu64 ": there are %zu symbolic branches\n", index, pcs.size());
            return false;
        }
        // the path up to the branch, then its other side
        triton::ast::SharedAbstractNode prefix = actx->equal(actx->bvtrue(), actx->bvtrue());
        for(size_t i = 0; i < index; i++)
            prefix = actx->land(prefix, pcs[i].getTakenPredicate());
        for(auto & b : pcs[index].getBranchConstraints())
        {
            if(std::get<0>(b))
                continue;
            const std::string what = "branch " + std::to_string(index) + " at " + Hex(std::get<1>(b)) + " to " + Hex(std::get<2>(b));
            if(Solve(actx->land(prefix, std::get<3>(b)), what.c_str()))
                return true;
        }
        if(!pcs[index].isMultipleBranches())
            Log("branch %" PRIu64 " has a symbolic target: sym.solve on its register instead\n", index);
        SetResult("$result", 0);
        return true;
    }

    static bool CbReach(int argc, char** argv)
    {
        if(!Active())
            return false;
        uint64_t address = 0;
        if(argc < 2 || !Eval(argv[1], address))
        {
            Log("sym.reach address\n");
            return false;
        }
        auto & ctx = Need().Context();
        auto predicates = ctx.getPredicatesToReachAddress(address);
        if(predicates.empty())
        {
            Log("no recorded symbolic branch leads to %s\n", Hex(address).c_str());
            SetResult("$result", 0);
            return true;
        }
        for(auto & p : predicates)
            if(Solve(p, ("reach " + Hex(address)).c_str()))
                return true;
        return true;
    }

    static bool CbSolve(int argc, char** argv)
    {
        if(!Active())
            return false;
        if(argc < 3)
        {
            Log("sym.solve reg, value or sym.solve address, value[, size]\n");
            return false;
        }
        Target target;
        target.text = argv[1];
        auto & s = Need();
        target.reg = s.Register(argv[1]);
        if(!target.reg)
        {
            if(!Eval(argv[1], target.address))
                return false;
            target.size = s.PointerSize();
            uint64_t size = 0;
            if(argc > 3)
            {
                if(!Eval(argv[3], size))
                    return false;
                if(size == 0 || size > 64)
                {
                    Log("size %" PRIu64 ": 1 to 64 bytes\n", size);
                    return false;
                }
                target.size = (uint32_t)size;
            }
            target.text = Hex(target.address) + " (" + std::to_string(target.size) + " bytes)";
        }
        uint64_t value = 0;
        if(!Eval(argv[2], value))
            return false;
        if(!TargetSymbolic(target))
        {
            Log("%s does not depend on symbolic data (its value is %s)\n", target.text.c_str(), Hex(TargetValue(target)).c_str());
            SetResult("$result", TargetValue(target) == triton::uint512(value) ? 1 : 0);
            return true;
        }
        auto & ctx = s.Context();
        auto actx = ctx.getAstContext();
        auto node = TargetAst(target);
        auto constraint = actx->land(ctx.getPathPredicate(), actx->equal(node, actx->bv(triton::uint512(value), node->getBitvectorSize())));
        Solve(constraint, (target.text + " == " + Hex(value)).c_str());
        return true;
    }

    static bool CbApply(int, char**)
    {
        if(!Active())
            return false;
        auto & s = Need();
        if(s.lastModel.empty())
        {
            Log("no solution to apply: sym.flip, sym.reach or sym.solve first\n");
            return false;
        }
        if(!DbgIsDebugging())
        {
            Log("sym.apply writes into the debuggee: not debugging\n");
            return false;
        }
        auto & ctx = s.Context();
        size_t written = 0;
        for(auto & it : s.lastModel)
        {
            auto & var = it.second.getVariable();
            const triton::uint512 value = it.second.getValue();
            if(var->getType() == triton::engines::symbolic::MEMORY_VARIABLE)
            {
                const uint32_t bytes = var->getSize() / 8;
                uint8_t data[64] = {};
                triton::uint512 v = value;
                for(uint32_t i = 0; i < bytes && i < sizeof(data); i++)
                {
                    data[i] = static_cast<uint8_t>(static_cast<uint64_t>(v & triton::uint512(0xFF)));
                    v >>= 8;
                }
                if(DbgMemWrite((duint)var->getOrigin(), data, bytes))
                    written++;
                else
                    Log("cannot write %u bytes at %s\n", bytes, Hex(var->getOrigin()).c_str());
            }
            else if(var->getType() == triton::engines::symbolic::REGISTER_VARIABLE)
            {
                const auto & reg = ctx.getRegister((triton::arch::register_e)var->getOrigin());
                if(DbgValSetScalar(reg.getName().c_str(), (duint)static_cast<uint64_t>(value)))
                    written++;
                else
                    Log("cannot set %s\n", reg.getName().c_str());
            }
        }
        GuiUpdateAllViews();
        Log("%zu values of the solution written into the debuggee\n", written);
        SetResult("$result", written);
        return true;
    }

    // ------------------------------------------------------------------------------ history ---
    static bool CbList(int argc, char** argv)
    {
        if(!Active())
            return false;
        uint64_t count = 50;
        if(argc > 1 && !Eval(argv[1], count))
            return false;
        auto & s = Need();
        auto & history = s.History();
        Log("%zu instructions used symbolic or tainted data%s\n", history.size() + s.HistoryDropped(),
            history.size() > count ? (", the last " + std::to_string(count) + ":").c_str() : ":");
        const size_t first = history.size() > count ? history.size() - (size_t)count : 0;
        for(size_t i = first; i < history.size(); i++)
        {
            auto & e = history[i];
            const std::string where = s.GetSource() == Source::Trace ? " #" + std::to_string(e.seq) : "";
            Log("  %s%s  %-40s %s%s\n", Hex(e.address).c_str(), where.c_str(), e.disassembly.c_str(), e.symbolic ? " symbolic" : "",
                e.tainted ? " tainted" : "");
        }
        SetResult("$result", history.size());
        return true;
    }

    static bool CbMark(int, char**)
    {
        if(!Active())
            return false;
        auto & s = Need();
        std::map<uint64_t, std::pair<bool, bool>> marks;
        for(auto & e : s.History())
        {
            auto & m = marks[e.address];
            m.first |= e.symbolic;
            m.second |= e.tainted;
        }
        for(auto & it : marks)
        {
            const char* text = it.second.first && it.second.second ? "Triton: symbolic, tainted" : it.second.first ? "Triton: symbolic" : "Triton: tainted";
            if(DbgSetAutoCommentAt((duint)it.first, text))
                s.marked.insert(it.first);
        }
        GuiUpdateDisassemblyView();
        Log("%zu instructions commented (sym.unmark removes the comments)\n", marks.size());
        return true;
    }

    static bool CbUnmark(int, char**)
    {
        auto & s = Session::Get();
        for(auto address : s.marked)
            DbgClearAutoCommentRange((duint)address, (duint)address + 1);
        Log("%zu comments removed\n", s.marked.size());
        s.marked.clear();
        GuiUpdateDisassemblyView();
        return true;
    }

    void RegisterCommands(int handle)
    {
        struct Entry
        {
            const char* name;
            CBPLUGINCOMMAND cb;
            bool debugging;
        };
        static const Entry entries[] =
        {
            { "sym.start", CbStart, true },
            { "sym.trace", CbTrace, false },
            { "sym.stop", CbStop, false },
            { "sym.info", CbInfo, false },
            { "sym.reg", CbReg, false },
            { "sym.mem", CbMem, false },
            { "sym.memvar", CbMemVar, false },
            { "sym.concrete", CbConcrete, false },
            { "taint.reg", CbTaintReg, false },
            { "taint.mem", CbTaintMem, false },
            { "untaint.reg", CbUntaint, false },
            { "untaint.mem", CbTaintMem, false },
            { "sym.step", CbStep, false },
            { "sym.run", CbRun, false },
            { "sym.expr", CbExpr, false },
            { "sym.smt", CbSmt, false },
            { "sym.synth", CbSynth, false },
            { "sym.slice", CbSlice, false },
            { "sym.vars", CbVars, false },
            { "sym.branches", CbBranches, false },
            { "sym.flip", CbFlip, false },
            { "sym.reach", CbReach, false },
            { "sym.solve", CbSolve, false },
            { "sym.apply", CbApply, true },
            { "sym.list", CbList, false },
            { "sym.mark", CbMark, false },
            { "sym.unmark", CbUnmark, false },
        };
        for(auto & e : entries)
            _plugin_registercommand(handle, e.name, e.cb, e.debugging);
        RegisterVmCommands(handle);
    }

}
