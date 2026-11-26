// Decompiler plugin of Fathom: Ghidra's decompiler (its native C++ engine, Apache-2.0) on the code of the
// debuggee. The code is read from the process memory (not from the file on disk: unpacked or patched
// code decompiles as it is now), read-only memory is treated as constant (calls through the import
// table resolve to their target), and functions are named after x64dbg's labels and symbols.
//
//   decompile [address[, file]]   decompiles the function at address (default: the selection in the
//                                 disassembly, else the instruction pointer); C in the log, and in
//                                 file if given. $result = the number of lines
//   decompile.raw [address]       the same with Ghidra's raw p-code-level listing of the function
// Ghidra first: the plugin SDK defines macros (MAX_STRING_SIZE, PAGE_SIZE) that are names in Ghidra
#include "libdecomp.hh"
#include "sleigh_arch.hh"
#include "loadimage.hh"
#include "database.hh"
#include "funcdata.hh"
#include "printlanguage.hh"

#include "_plugins.h"
#undef LoadImage // winuser.h's LoadImageA / LoadImageW, not Ghidra's class

#include <cinttypes>
#include <cstdarg>
#include <fstream>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

#define PLUGIN_NAME "Decompiler"
#define PLUGIN_VERSION 1
#define LOG_PREFIX "[Decompiler] "

using namespace ghidra;

namespace
{
    int gPluginHandle = 0;
    bool gLibraryReady = false;

    void Log(const char* format, ...)
    {
        char text[4096];
        va_list args;
        va_start(args, format);
        vsnprintf(text, sizeof(text), format, args);
        va_end(args);
        _plugin_logprintf(LOG_PREFIX "%s", text);
    }

    std::string HexAddress(uint64_t value)
    {
        char text[32];
        sprintf_s(text, "%" PRIX64, value);
        return text;
    }

    void SetResult(const char* name, uint64_t value)
    {
        char command[64];
        sprintf_s(command, "mov %s, 0x%llX", name, (unsigned long long)value);
        DbgCmdExecDirect(command);
    }

    // A C identifier made from an x64dbg label ("kernel32.CreateFileW", "&MessageBoxW", "sub_140001000")
    std::string Identifier(const std::string & label)
    {
        std::string name;
        for(char ch : label)
        {
            if(isalnum((unsigned char)ch) || ch == '_')
                name.push_back(ch);
            else if(!name.empty() && name.back() != '_')
                name.push_back('_');
        }
        while(!name.empty() && name.back() == '_')
            name.pop_back();
        if(name.empty() || isdigit((unsigned char)name[0]))
            name = "f_" + name;
        return name;
    }

    // The label x64dbg has for an address: a symbol, an export, a user label (without the module prefix)
    bool LabelAt(uint64_t address, std::string & name)
    {
        char label[MAX_LABEL_SIZE] = "";
        if(!DbgGetLabelAt((duint)address, SEG_DEFAULT, label) || !label[0])
            return false;
        name = Identifier(label);
        return !name.empty();
    }

    // ------------------------------------------------------------------------------ load image ---
    // The debuggee's memory, read when the decompiler needs it
    class DebuggeeLoadImage : public LoadImage
    {
    public:
        DebuggeeLoadImage() : LoadImage("debuggee") {}

        AddrSpace* space = nullptr;

        void loadFill(uint1* ptr, int4 size, const Address & addr) override
        {
            memset(ptr, 0, size);
            // page by page: a range that crosses into unreadable memory still gets its readable part
            uint64_t address = addr.getOffset();
            int4 done = 0;
            while(done < size)
            {
                const uint64_t pageEnd = (address + 0x1000) & ~0xFFFull;
                const int4 chunk = (int4)std::min<uint64_t>(size - done, pageEnd - address);
                DbgMemRead((duint)address, ptr + done, (duint)chunk);
                done += chunk;
                address += chunk;
            }
        }

        // read-only pages of the process: their content is constant for the decompiler (string
        // literals, the import address table, jump tables)
        void getReadonly(RangeList & list) const override
        {
            if(!space)
                return;
            MEMMAP map = {};
            if(!DbgMemMap(&map))
                return;
            for(int i = 0; i < map.count; i++)
            {
                const auto & mbi = map.page[i].mbi;
                if(mbi.State != MEM_COMMIT)
                    continue;
                const DWORD protect = mbi.Protect & 0xFF;
                const bool readOnly = protect == PAGE_READONLY || protect == PAGE_EXECUTE_READ || protect == PAGE_EXECUTE;
                if(!readOnly || (mbi.Protect & PAGE_GUARD))
                    continue;
                const uint64_t start = (uint64_t)mbi.BaseAddress;
                list.insertRange(space, start, start + mbi.RegionSize - 1);
            }
            if(map.page)
                BridgeFree(map.page);
        }

        string getArchType() const override
        {
#ifdef _WIN64
            return "x86:LE:64:default:windows";
#else
            return "x86:LE:32:default:windows";
#endif
        }

        void adjustVma(long) override {}
    };

    // ---------------------------------------------------------------------------- global scope ---
    // The global scope: functions called by the decompiled code get the name x64dbg gives them
    class DebuggeeScope : public ScopeInternal
    {
    public:
        explicit DebuggeeScope(Architecture* glb) : ScopeInternal(0, "", glb) {}

        Funcdata* findFunction(const Address & addr) const override
        {
            Funcdata* fd = ScopeInternal::findFunction(addr);
            if(fd || addr.getSpace() != glb->getDefaultCodeSpace())
                return fd;
            std::string name;
            if(!LabelAt(addr.getOffset(), name))
                return nullptr;
            auto self = const_cast<DebuggeeScope*>(this);
            return self->addFunction(addr, makeNameUnique(name))->getFunction();
        }
    };

    // ---------------------------------------------------------------------------- architecture ---
    class DebuggeeArchitecture : public SleighArchitecture
    {
    public:
        DebuggeeArchitecture(const std::string & target, std::ostream* errors) : SleighArchitecture("", target, errors) {}

    protected:
        void buildLoader(DocumentStorage &) override
        {
            collectSpecFiles(*errorstream);
            loader = new DebuggeeLoadImage();
        }

        void resolveArchitecture() override
        {
            archid = getTarget();
            SleighArchitecture::resolveArchitecture();
        }

        void postSpecFile() override
        {
            Architecture::postSpecFile();
            static_cast<DebuggeeLoadImage*>(loader)->space = getDefaultCodeSpace();
        }

        Scope* buildDatabase(DocumentStorage &) override
        {
            symboltab = new Database(this, true);
            Scope* global = new DebuggeeScope(this);
            symboltab->attachScope(global, nullptr);
            return global;
        }
    };

    // The folder that holds the sleigh specifications (x86.ldefs, x86-64.sla...): plugins\decompiler
    std::string SpecFolder()
    {
        HMODULE self = nullptr;
        GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT, (LPCWSTR)&SpecFolder, &self);
        char path[MAX_PATH] = "";
        GetModuleFileNameA(self, path, _countof(path));
        std::string folder = path;
        folder = folder.substr(0, folder.find_last_of('\\') + 1) + "decompiler";
        return folder;
    }

    // The function that contains address: x64dbg's analysis (functions, symbols, exception directory)
    uint64_t FunctionStart(uint64_t address)
    {
        duint start = 0, end = 0;
        if(DbgFunctionGet((duint)address, &start, &end))
            return start;
#ifdef _WIN64
        // the exception directory of the module (every non-leaf x64 function has an entry there)
        bool ok = false;
        const duint base = DbgEval(("mod.base(" + HexAddress(address) + ")").c_str(), &ok);
        if(ok && base)
        {
            IMAGE_DOS_HEADER dos = {};
            IMAGE_NT_HEADERS64 nt = {};
            if(DbgMemRead(base, &dos, sizeof(dos)) && DbgMemRead(base + dos.e_lfanew, &nt, sizeof(nt)))
            {
                const auto & dir = nt.OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_EXCEPTION];
                const size_t count = dir.Size / sizeof(IMAGE_RUNTIME_FUNCTION_ENTRY);
                if(dir.VirtualAddress && count && count < 0x1000000)
                {
                    std::vector<IMAGE_RUNTIME_FUNCTION_ENTRY> entries(count);
                    if(DbgMemRead(base + dir.VirtualAddress, entries.data(), count * sizeof(entries[0])))
                    {
                        const uint64_t rva = address - base;
                        for(auto & e : entries)
                            if(rva >= e.BeginAddress && rva < e.EndAddress)
                                return base + e.BeginAddress;
                    }
                }
            }
        }
#endif
        return address;
    }

    bool Decompile(uint64_t address, bool raw, std::string & text, std::string & name, std::string & error)
    {
        std::ostringstream errors;
        try
        {
            std::unique_ptr<DebuggeeArchitecture> arch(new DebuggeeArchitecture(
#ifdef _WIN64
                                                           "x86:LE:64:default:windows",
#else
                                                           "x86:LE:32:default:windows",
#endif
                                                           &errors));
            DocumentStorage store;
            arch->init(store);
            // read-only memory is constant (Ghidra's "readonly" option, on in Ghidra itself): calls through
            // the import table become calls to their target, jump tables resolve
            arch->readonlypropagate = true;
            Address entry(arch->getDefaultCodeSpace(), address);
            if(!LabelAt(address, name))
                name = "sub_" + HexAddress(address);
            Scope* global = arch->symboltab->getGlobalScope();
            Funcdata* fd = global->findFunction(entry);
            if(fd)
                name = fd->getName();
            else
                fd = global->addFunction(entry, name)->getFunction();
            // the whole address space: the flow follows the function wherever it goes
            fd->followFlow(Address(entry.getSpace(), 0), Address(entry.getSpace(), entry.getSpace()->getHighest()));
            if(fd->hasNoCode())
            {
                error = "no code at " + HexAddress(address);
                return false;
            }
            auto action = arch->allacts.getCurrent();
            action->reset(*fd);
            const int4 result = action->perform(*fd);
            if(result < 0)
            {
                std::ostringstream state;
                action->printState(state);
                error = "the decompiler stopped: " + state.str();
                return false;
            }
            std::ostringstream out;
            if(raw)
                fd->printRaw(out);
            else
            {
                arch->print->setOutputStream(&out);
                arch->print->docFunction(fd);
            }
            text = out.str();
            return true;
        }
        catch(const LowlevelError & e)
        {
            error = e.explain;
        }
        catch(const std::exception & e)
        {
            error = e.what();
        }
        if(!errors.str().empty())
            error += " (" + errors.str() + ")";
        return false;
    }

    // A string of the debuggee at address (ASCII or UTF-16, at least 2 printable characters, terminated)
    bool StringAt(uint64_t address, std::string & text)
    {
        unsigned char data[256] = {};
        if(!DbgMemIsValidReadPtr((duint)address) || !DbgMemRead((duint)address, data, 255) && !DbgMemRead((duint)address, data, 16))
            return false;
        auto printable = [](unsigned c) { return (c >= 0x20 && c < 0x7F) || c == '\n' || c == '\r' || c == '\t'; };
        auto escape = [](unsigned c) -> std::string
        {
            if(c == '\n') return "\\n";
            if(c == '\r') return "\\r";
            if(c == '\t') return "\\t";
            if(c == '"' || c == '\\') return std::string("\\") + (char)c;
            return std::string(1, (char)c);
        };
        size_t n = 0;
        while(n < 255 && printable(data[n]))
            n++;
        if(n >= 2 && data[n] == 0)
        {
            text = "\"";
            for(size_t i = 0; i < n && i < 80; i++)
                text += escape(data[i]);
            text += n > 80 ? "...\"" : "\"";
            return true;
        }
        size_t w = 0;
        while(w * 2 + 1 < 255 && data[w * 2 + 1] == 0 && printable(data[w * 2]))
            w++;
        if(w >= 2 && w * 2 + 1 < 255 && data[w * 2] == 0 && data[w * 2 + 1] == 0)
        {
            text = "L\"";
            for(size_t i = 0; i < w && i < 80; i++)
                text += escape(data[i * 2]);
            text += w > 80 ? "...\"" : "\"";
            return true;
        }
        return false;
    }

    // Addresses in the C: a comment with the string or the symbol they point to
    std::string Annotate(const std::string & text)
    {
        std::string out;
        out.reserve(text.size());
        std::istringstream in(text);
        std::string line;
        while(std::getline(in, line))
        {
            std::string notes;
            for(size_t i = 0; i + 2 < line.size(); i++)
            {
                if(line[i] != '0' || line[i + 1] != 'x' || (i > 0 && (isalnum((unsigned char)line[i - 1]) || line[i - 1] == '_')))
                    continue;
                size_t j = i + 2;
                while(j < line.size() && isxdigit((unsigned char)line[j]))
                    j++;
                if(j - i - 2 < 5 || j - i - 2 > 16)
                    continue;
                const uint64_t value = std::stoull(line.substr(i + 2, j - i - 2), nullptr, 16);
                std::string note, label;
                if(StringAt(value, note) || LabelAt(value, note))
                    if(notes.find(note) == std::string::npos)
                        notes += (notes.empty() ? "" : ", ") + note;
                i = j;
            }
            out += line;
            if(!notes.empty())
                out += "  /* " + notes + " */";
            out += "\n";
        }
        return out;
    }

    bool Run(int argc, char** argv, bool raw)
    {
        if(!gLibraryReady)
        {
            Log("the sleigh specifications were not found in %s\n", SpecFolder().c_str());
            return false;
        }
        uint64_t address = 0;
        bool ok = false;
        if(argc > 1)
        {
            address = DbgEval(argv[1], &ok);
            if(!ok)
            {
                Log("invalid expression: %s\n", argv[1]);
                return false;
            }
        }
        else
        {
            address = DbgEval("dis.sel()", &ok);
            if(!ok || !address)
                address = DbgValFromString("cip");
        }
        const uint64_t start = FunctionStart(address);
        std::string text, name, error;
        const DWORD began = GetTickCount();
        if(!Decompile(start, raw, text, name, error))
        {
            Log("%s at %s: %s\n", argv[0], HexAddress(start).c_str(), error.c_str());
            SetResult("$result", 0);
            return false;
        }
        if(!raw)
            text = Annotate(text);
        size_t lines = 0;
        std::istringstream in(text);
        std::string line;
        Log("%s (%s), %lu ms:\n", name.c_str(), HexAddress(start).c_str(), GetTickCount() - began);
        while(std::getline(in, line))
        {
            if(!line.empty() && line.back() == '\r')
                line.pop_back();
            _plugin_logputs(line.c_str());
            lines++;
        }
        if(argc > 2)
        {
            std::string file = argv[2];
            if(file.size() >= 2 && file.front() == '"' && file.back() == '"')
                file = file.substr(1, file.size() - 2);
            std::ofstream f(file, std::ios::binary);
            f << text;
            if(f)
                Log("written to %s\n", file.c_str());
            else
                Log("cannot write %s\n", file.c_str());
        }
        SetResult("$result", lines);
        return true;
    }

    bool CbDecompile(int argc, char** argv) { return Run(argc, argv, false); }
    bool CbDecompileRaw(int argc, char** argv) { return Run(argc, argv, true); }
}

extern "C" __declspec(dllexport) bool pluginit(PLUG_INITSTRUCT* initStruct)
{
    initStruct->pluginVersion = PLUGIN_VERSION;
    initStruct->sdkVersion = PLUG_SDKVERSION;
    strncpy_s(initStruct->pluginName, PLUGIN_NAME, _TRUNCATE);
    gPluginHandle = initStruct->pluginHandle;
    try
    {
        std::vector<std::string> paths = { SpecFolder() };
        startDecompilerLibrary(paths);
        gLibraryReady = GetFileAttributesA((SpecFolder() + "\\x86.ldefs").c_str()) != INVALID_FILE_ATTRIBUTES;
    }
    catch(const LowlevelError & e)
    {
        Log("cannot start Ghidra's decompiler: %s\n", e.explain.c_str());
    }
    _plugin_registercommand(gPluginHandle, "decompile", CbDecompile, true);
    _plugin_registercommand(gPluginHandle, "decompile.raw", CbDecompileRaw, true);
    return true;
}

extern "C" __declspec(dllexport) void plugstop()
{
}

extern "C" __declspec(dllexport) void plugsetup(PLUG_SETUPSTRUCT*)
{
}
