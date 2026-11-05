#include "_plugins.h"
#include "_dbgfunctions.h"

#include "ttmodel.h"

namespace
{
    std::string Narrow(const std::wstring & text)
    {
        if(text.empty())
            return {};
        int size = WideCharToMultiByte(CP_UTF8, 0, text.c_str(), (int)text.size(), nullptr, 0, nullptr, nullptr);
        std::string result(size, '\0');
        WideCharToMultiByte(CP_UTF8, 0, text.c_str(), (int)text.size(), &result[0], size, nullptr, nullptr);
        return result;
    }

    // Quote an argument for an x64dbg command (same rules as Command::Escape)
    std::string QuoteArgument(const std::string & argument)
    {
        std::string result = "\"";
        size_t i = 0;
        while(i < argument.size())
        {
            char ch = argument[i];
            if(ch != '\\')
            {
                if(ch == '"' || ch == '{')
                    result.push_back('\\');
                result.push_back(ch);
                i++;
                continue;
            }
            size_t start = i;
            while(i < argument.size() && argument[i] == '\\')
                i++;
            size_t count = i - start;
            if(i == argument.size())
                result.append(count * 2, '\\');
            else if(argument[i] == '"' || argument[i] == '{')
            {
                result.append(count * 2 + 1, '\\');
                result.push_back(argument[i++]);
            }
            else
                result.append(count, '\\');
        }
        return result + "\"";
    }

    // The value of "key": "..." in the .ftt metadata (a flat JSON object written by TraceExport)
    std::string MetaString(const std::string & json, const char* key)
    {
        const std::string pattern = std::string("\"") + key + "\":\"";
        auto start = json.find(pattern);
        if(start == std::string::npos)
            return {};
        start += pattern.size();
        auto end = json.find('"', start);
        return end == std::string::npos ? std::string() : json.substr(start, end - start);
    }
}

TtModel & TtModel::Get()
{
    static TtModel model;
    return model;
}

bool TtModel::Open(const std::wstring & path, std::string & error)
{
    std::wstring fttPath = path;
    std::wstring source;
    if(!ftt::Reader::IsFtt(path))
    {
        // A trace recorded by x64dbg: convert it once, next to the other converted traces in %TEMP%
        wchar_t temp[MAX_PATH] = L"";
        GetTempPathW(_countof(temp), temp);
        std::wstring folder = std::wstring(temp) + L"FathomTimeTravel";
        CreateDirectoryW(folder.c_str(), nullptr);
        auto slash = path.find_last_of(L"\\/");
        fttPath = folder + L"\\" + (slash == std::wstring::npos ? path : path.substr(slash + 1)) + L".ftt";
        const std::string command = "TraceExport " + QuoteArgument(Narrow(path)) + ", " + QuoteArgument(Narrow(fttPath)) + ", ftt, regs=all";
        if(!DbgCmdExecDirect(command.c_str()) || !ftt::Reader::IsFtt(fttPath))
        {
            error = "the conversion to .ftt failed (see the log): is it a .trace32/.trace64 file?";
            return false;
        }
        source = path;
    }
    auto reader = std::make_unique<ftt::Reader>();
    if(!reader->open(fttPath, error))
        return false;
    {
        std::lock_guard<std::recursive_mutex> lock(mLock);
        mReader = std::move(reader);
        mInfo = Info();
        mInfo.path = fttPath;
        mInfo.source = source;
        mInfo.count = mReader->count();
        mInfo.threads = mReader->threads().size();
        mInfo.chunks = mReader->chunkCount();
        mInfo.snapshots = mReader->snapshotCount();
        mInfo.cpuSamples = mReader->cpuSamples().size();
        for(auto & sample : mReader->cpuSamples())
            if(!sample.exact())
                mInfo.concurrentSamples++;
        mInfo.pointerSize = mReader->pointerSize();
        mInfo.levels = mReader->levels();
        mInfo.snapshotInterval = mReader->snapshotInterval();
        mInfo.hash = MetaString(mReader->meta(), "hash");
        mPosition = 0;
        mThread = 0;
        mReader->threadAt(0, mThread);
    }
    Notify();
    return true;
}

void TtModel::Close()
{
    {
        std::lock_guard<std::recursive_mutex> lock(mLock);
        mReader.reset();
        mInfo = Info();
        mPosition = 0;
        mThread = 0;
    }
    Notify();
}

bool TtModel::IsOpen()
{
    std::lock_guard<std::recursive_mutex> lock(mLock);
    return mReader != nullptr;
}

TtModel::Info TtModel::GetInfo()
{
    std::lock_guard<std::recursive_mutex> lock(mLock);
    return mInfo;
}

uint64_t TtModel::Position()
{
    std::lock_guard<std::recursive_mutex> lock(mLock);
    return mPosition;
}

uint32_t TtModel::ViewThread()
{
    std::lock_guard<std::recursive_mutex> lock(mLock);
    return mThread;
}

bool TtModel::Seek(uint64_t seq)
{
    {
        std::lock_guard<std::recursive_mutex> lock(mLock);
        if(!mReader || seq >= mInfo.count)
            return false;
        uint32_t thread;
        if(!mReader->threadAt(seq, thread))
            return false;
        mPosition = seq;
        mThread = thread;
    }
    Notify();
    return true;
}

bool TtModel::CurrentInThread(uint64_t & seq)
{
    uint32_t running = 0;
    if(mReader->threadAt(mPosition, running) && running == mThread)
    {
        seq = mPosition;
        return true;
    }
    // the position belongs to another thread: the viewed thread's next instruction, or its last one
    // if it already ended
    if(mReader->nextInThread(mThread, mPosition, seq, true))
        return true;
    return mReader->nextInThread(mThread, mPosition, seq, false);
}

bool TtModel::Step(bool forward, bool sameThread)
{
    uint64_t target = 0;
    {
        std::lock_guard<std::recursive_mutex> lock(mLock);
        if(!mReader)
            return false;
        if(!sameThread)
        {
            if(forward ? mPosition + 1 >= mInfo.count : mPosition == 0)
                return false;
            target = forward ? mPosition + 1 : mPosition - 1;
        }
        else
        {
            uint64_t current;
            if(!CurrentInThread(current) || !mReader->nextInThread(mThread, current, target, forward))
                return false;
        }
    }
    const uint32_t thread = ViewThread();
    if(!Seek(target))
        return false;
    if(sameThread)
    {
        std::lock_guard<std::recursive_mutex> lock(mLock);
        mThread = thread;
    }
    return true;
}

bool TtModel::SwitchThread(uint32_t thread)
{
    {
        std::lock_guard<std::recursive_mutex> lock(mLock);
        if(!mReader)
            return false;
        bool found = false;
        for(auto & t : mReader->threads())
            found |= t.thread == thread;
        if(!found)
            return false;
        mThread = thread;
    }
    Notify();
    return true;
}

bool TtModel::FindExecution(uint64_t address, bool forward, bool sameThread)
{
    uint64_t target;
    uint32_t thread;
    {
        std::lock_guard<std::recursive_mutex> lock(mLock);
        if(!mReader || !mReader->findExecution(address, mPosition, forward, sameThread ? mThread : 0, target))
            return false;
        thread = mThread;
    }
    if(!Seek(target))
        return false;
    if(sameThread)
    {
        std::lock_guard<std::recursive_mutex> lock(mLock);
        mThread = thread;
    }
    return true;
}

bool TtModel::FindWrite(uint64_t address, size_t size, bool forward)
{
    uint64_t target;
    {
        std::lock_guard<std::recursive_mutex> lock(mLock);
        if(!mReader || !mReader->findWrite(address, size, mPosition, forward, target))
            return false;
    }
    return Seek(target);
}

bool TtModel::Instruction(uint64_t seq, ftt::Instruction & instruction)
{
    std::lock_guard<std::recursive_mutex> lock(mLock);
    return mReader && mReader->instruction(seq, instruction);
}

bool TtModel::OrderSample(uint64_t seq, ftt::CpuSample & sample)
{
    std::lock_guard<std::recursive_mutex> lock(mLock);
    auto found = mReader ? mReader->cpuSample(seq) : nullptr;
    if(!found)
        return false;
    sample = *found;
    return true;
}

bool TtModel::NextInViewThread(uint64_t seq, bool forward, uint64_t & result)
{
    std::lock_guard<std::recursive_mutex> lock(mLock);
    return mReader && mReader->nextInThread(mThread, seq, result, forward);
}

bool TtModel::CurrentInViewThread(uint64_t & seq)
{
    std::lock_guard<std::recursive_mutex> lock(mLock);
    return mReader && CurrentInThread(seq);
}

bool TtModel::Registers(ftt::RegisterFile & registers, uint64_t* at)
{
    std::lock_guard<std::recursive_mutex> lock(mLock);
    return mReader && mReader->registers(mThread, mPosition, registers, at);
}

bool TtModel::PreviousRegisters(ftt::RegisterFile & registers)
{
    std::lock_guard<std::recursive_mutex> lock(mLock);
    uint64_t current, previous;
    if(!mReader || !CurrentInThread(current) || !mReader->nextInThread(mThread, current, previous, false))
        return false;
    return mReader->registers(mThread, previous, registers);
}

void TtModel::Memory(uint64_t address, size_t size, std::vector<uint8_t> & data, std::vector<bool> & known, std::vector<bool> & inferred)
{
    std::lock_guard<std::recursive_mutex> lock(mLock);
    if(!mReader)
    {
        data.assign(size, 0);
        known.assign(size, false);
        inferred.assign(size, false);
        return;
    }
    mReader->memory(mPosition, address, size, data, known, &inferred);
}

std::vector<ftt::ThreadInfo> TtModel::Threads()
{
    std::lock_guard<std::recursive_mutex> lock(mLock);
    return mReader ? mReader->threads() : std::vector<ftt::ThreadInfo>();
}

std::vector<std::pair<uint64_t, uint64_t>> TtModel::ThreadRanges(uint32_t thread)
{
    std::lock_guard<std::recursive_mutex> lock(mLock);
    return mReader ? mReader->threadRanges(thread) : std::vector<std::pair<uint64_t, uint64_t>>();
}

std::string TtModel::Disassemble(const ftt::Instruction & instruction)
{
    if(GetInfo().pointerSize != sizeof(duint) || !instruction.opcodeSize)
        return {};
    unsigned char code[16] = {};
    memcpy(code, instruction.opcode, instruction.opcodeSize);
    BASIC_INSTRUCTION_INFO info = {};
    if(!DbgFunctions()->DisasmFast(code, (duint)instruction.ip, &info))
        return "???";
    return info.instruction;
}

bool TtModel::SameProcess()
{
    const std::string hash = GetInfo().hash;
    if(hash.empty() || !DbgIsDebugging())
        return false;
    return strtoull(hash.c_str(), nullptr, 16) == (unsigned long long)DbgFunctions()->DbGetHash();
}

std::string TtModel::Label(uint64_t address)
{
    if(!SameProcess())
        return {};
    char module[MAX_MODULE_SIZE] = "";
    char label[MAX_LABEL_SIZE] = "";
    if(DbgGetLabelAt((duint)address, SEG_DEFAULT, label))
    {
        if(DbgGetModuleAt((duint)address, module) && !strchr(label, '.'))
            return std::string(module) + "." + label;
        return label;
    }
    if(DbgGetModuleAt((duint)address, module))
    {
        const duint base = DbgFunctions()->ModBaseFromAddr((duint)address);
        char text[MAX_MODULE_SIZE + 32];
        sprintf_s(text, "%s+%llX", module, (unsigned long long)(address - base));
        return text;
    }
    return {};
}

void TtModel::AddListener(const std::function<void()> & listener)
{
    std::lock_guard<std::recursive_mutex> lock(mLock);
    mListeners.push_back(listener);
}

void TtModel::Notify()
{
    std::vector<std::function<void()>> listeners;
    {
        std::lock_guard<std::recursive_mutex> lock(mLock);
        listeners = mListeners;
    }
    for(auto & listener : listeners)
        listener();
}
