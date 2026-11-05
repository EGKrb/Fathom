#include "TraceExport.h"
#include "_dbgfunctions.h"
#include "debugger.h"
#include "handle.h"
#include "module.h"
#include "stringutils.h"
#include "zydis_wrapper.h"
#include "jansson/jansson_x64dbg.h"
#include "ftt/ftt.h"
#include <set>

// File format references: docs/developers/tracefile.md (.trace32/.trace64), docs/developers/ftt.md (.ftt)

namespace
{
    // Number of pointer-sized words of REGDUMP that are stored in a trace file
    constexpr size_t TraceExportRegWordCount = (FIELD_OFFSET(REGDUMP, lastError) + sizeof(DWORD)) / sizeof(duint);
    // Matches the size of the memory operand buffers in TraceRecordManager
    constexpr size_t TraceExportMaxMemoryOperands = 32;

    // The general purpose registers are the first words of REGISTERCONTEXT, ending with the
    // instruction pointer and eflags, in the order of the .ftt register catalog
    const size_t TraceExportGprCount = ftt::GprCount(sizeof(duint));
    const size_t TraceExportIpIndex = ftt::IpRegister(sizeof(duint));
    static_assert(FIELD_OFFSET(REGISTERCONTEXT, eflags) == ArchValue(9, 17) * sizeof(duint), "Unexpected REGISTERCONTEXT layout");

    class TraceExportReader
    {
    public:
        explicit TraceExportReader(HANDLE hFile) : mFile(hFile), mBuffer(1024 * 1024) { }

        // Returns the number of bytes read, which is less than size at the end of the file
        size_t read(void* dest, size_t size)
        {
            auto out = (unsigned char*)dest;
            size_t total = 0;
            while(total < size)
            {
                if(mPos == mSize)
                {
                    DWORD bytesRead = 0;
                    if(!ReadFile(mFile, mBuffer.data(), (DWORD)mBuffer.size(), &bytesRead, nullptr) || bytesRead == 0)
                        break;
                    mPos = 0;
                    mSize = bytesRead;
                }
                auto chunk = std::min(size - total, mSize - mPos);
                memcpy(out + total, mBuffer.data() + mPos, chunk);
                mPos += chunk;
                total += chunk;
            }
            return total;
        }

        bool readExact(void* dest, size_t size)
        {
            return read(dest, size) == size;
        }

        bool skip(size_t size)
        {
            unsigned char temp[4096];
            while(size > 0)
            {
                auto chunk = std::min(size, sizeof(temp));
                if(!readExact(temp, chunk))
                    return false;
                size -= chunk;
            }
            return true;
        }

    private:
        HANDLE mFile;
        std::vector<unsigned char> mBuffer;
        size_t mPos = 0;
        size_t mSize = 0;
    };

    struct TraceExportEntry
    {
        DWORD threadId = 0;
        unsigned char opcode[16] = {};
        unsigned char opcodeSize = 0;
        union
        {
            REGDUMP registers;
            duint regwords[TraceExportRegWordCount];
        };
        unsigned char memoryCount = 0;
        unsigned char memoryFlags[TraceExportMaxMemoryOperands] = {};
        duint memoryAddress[TraceExportMaxMemoryOperands] = {};
        duint memoryOld[TraceExportMaxMemoryOperands] = {};
        duint memoryNew[TraceExportMaxMemoryOperands] = {};

        TraceExportEntry()
        {
            memset(&registers, 0, sizeof(registers));
        }
    };

    // Context switch marker written by TraceRecordManager (user block 0x81)
    struct TraceExportSwitchMarker
    {
        DWORD fromThread;
        DWORD toThread;
        uint64_t nextInstruction;
        uint64_t counter; // QueryPerformanceCounter
        uint64_t frequency; // QueryPerformanceFrequency
    };

    // Call that ran untraced (TraceSetSkip), written by TraceRecordManager before its block (user
    // block 0x83)
    struct TraceExportSkippedCall
    {
        uint64_t instruction; // index of the call
        uint64_t address; // of the call
        uint64_t destination;
        uint32_t thread;
        uint32_t reserved;
    };

    // CPU time sample written by TraceRecordManager (user block 0x82): 40-byte header, then the
    // threads (24 bytes each)
    struct TraceExportCpuThread
    {
        uint32_t thread = 0;
        uint32_t flags = 0; // bit 0: contextHash is valid
        uint64_t cycles = 0; // QueryThreadCycleTime
        uint64_t contextHash = 0; // of the user-mode integer and control registers
    };

    struct TraceExportCpuSample
    {
        uint64_t nextInstruction = 0;
        uint64_t counter = 0; // QueryPerformanceCounter
        uint64_t frequency = 0; // QueryPerformanceFrequency
        uint32_t nextThread = 0;
        uint32_t flags = 0; // bit 0: the recording (re)started, the previous sample does not apply
        std::vector<TraceExportCpuThread> threads;
    };

    enum class TraceExportBlockResult
    {
        Instruction,
        UserBlock,
        SwitchMarker,
        CpuSample,
        SkippedCall,
        EndOfFile,
        Truncated,
    };

    // Reads the next block. The entry keeps the register state of the previous instruction, because
    // registers are delta-encoded. Throws a String on malformed data.
    TraceExportBlockResult TraceExportReadBlock(TraceExportReader & reader, TraceExportEntry & entry, TraceExportSwitchMarker & marker, TraceExportCpuSample & sample, TraceExportSkippedCall & skipped)
    {
        unsigned char blockType;
        if(!reader.readExact(&blockType, 1))
            return TraceExportBlockResult::EndOfFile;

        if(blockType >= 0x80)
        {
            uint32_t blockSize;
            if(!reader.readExact(&blockSize, sizeof(blockSize)))
                return TraceExportBlockResult::Truncated;
            if(blockType == 0x81 && blockSize == sizeof(TraceExportSwitchMarker))
                return reader.readExact(&marker, sizeof(marker)) ? TraceExportBlockResult::SwitchMarker : TraceExportBlockResult::Truncated;
            static_assert(sizeof(TraceExportSkippedCall) == 32, "TraceExportSkippedCall is the 32-byte block");
            if(blockType == 0x83 && blockSize == sizeof(TraceExportSkippedCall))
                return reader.readExact(&skipped, sizeof(skipped)) ? TraceExportBlockResult::SkippedCall : TraceExportBlockResult::Truncated;
            if(blockType == 0x82 && blockSize >= 40 && (blockSize - 40) % 24 == 0)
            {
                uint32_t header[10];
                if(!reader.readExact(header, sizeof(header)))
                    return TraceExportBlockResult::Truncated;
                memcpy(&sample.nextInstruction, &header[0], 8);
                memcpy(&sample.counter, &header[2], 8);
                memcpy(&sample.frequency, &header[4], 8);
                sample.nextThread = header[6];
                sample.flags = header[7];
                const uint32_t count = header[8];
                if(count != (blockSize - 40) / 24)
                    throw String("Bad CPU time sample");
                sample.threads.resize(count);
                static_assert(sizeof(TraceExportCpuThread) == 24, "TraceExportCpuThread is the 24-byte entry");
                for(auto & thread : sample.threads)
                {
                    if(!reader.readExact(&thread, sizeof(thread)))
                        return TraceExportBlockResult::Truncated;
                }
                return TraceExportBlockResult::CpuSample;
            }
            return reader.skip(blockSize) ? TraceExportBlockResult::UserBlock : TraceExportBlockResult::Truncated;
        }
        if(blockType != 0)
            throw StringUtils::sprintf("Unsupported block type %u", blockType);

        unsigned char changedCountFlags[3]; //reg changed count, mem accessed count, flags
        if(!reader.readExact(changedCountFlags, sizeof(changedCountFlags)))
            return TraceExportBlockResult::Truncated;
        if((changedCountFlags[2] & 0x80) && !reader.readExact(&entry.threadId, sizeof(entry.threadId)))
            return TraceExportBlockResult::Truncated;

        entry.opcodeSize = changedCountFlags[2] & 0x0F;
        if(entry.opcodeSize == 0)
            throw String("Instruction block without opcode");
        if(!reader.readExact(entry.opcode, entry.opcodeSize))
            return TraceExportBlockResult::Truncated;

        const unsigned char regCount = changedCountFlags[0];
        if(regCount > TraceExportRegWordCount)
            throw StringUtils::sprintf("Bad register count %u", regCount);
        unsigned char regPosition[TraceExportRegWordCount];
        duint regContent[TraceExportRegWordCount];
        if(!reader.readExact(regPosition, regCount) || !reader.readExact(regContent, regCount * sizeof(duint)))
            return TraceExportBlockResult::Truncated;
        int lastPosition = -1;
        for(unsigned char i = 0; i < regCount; i++)
        {
            lastPosition += regPosition[i] + 1;
            if(lastPosition >= (int)TraceExportRegWordCount)
                throw String("Register change out of bounds");
            entry.regwords[lastPosition] = regContent[i];
        }

        entry.memoryCount = changedCountFlags[1];
        if(entry.memoryCount > TraceExportMaxMemoryOperands)
            throw StringUtils::sprintf("Bad memory operand count %u", entry.memoryCount);
        const size_t memorySize = entry.memoryCount * sizeof(duint);
        if(!reader.readExact(entry.memoryFlags, entry.memoryCount)
                || !reader.readExact(entry.memoryAddress, memorySize)
                || !reader.readExact(entry.memoryOld, memorySize))
            return TraceExportBlockResult::Truncated;
        for(unsigned char i = 0; i < entry.memoryCount; i++)
        {
            // Bit 0 set: memory is unchanged, no new content is stored
            if(entry.memoryFlags[i] & 1)
                entry.memoryNew[i] = entry.memoryOld[i];
            else if(!reader.readExact(&entry.memoryNew[i], sizeof(duint)))
                return TraceExportBlockResult::Truncated;
        }
        return TraceExportBlockResult::Instruction;
    }

    // One executed instruction, whatever the source format
    struct TraceExportRecord
    {
        uint64_t seq = 0; // global sequence number (position in the timeline of all threads)
        uint32_t thread = 0;
        duint ip = 0;
        unsigned char opcode[16] = {};
        unsigned char opcodeSize = 0;
        ftt::RegisterFile regs; // state before the instruction
        std::vector<ftt::MemoryAccess> memory;
        bool switched = false; // the previous instruction ran on another thread
        uint32_t fromThread = 0;
        uint64_t timestamp = 0; // of the context switch, in 100 ns units (0 = unknown)
        bool skipped = false; // a call that ran untraced (TraceSetSkip)
        uint64_t skipDestination = 0;
        // A CPU time sample precedes the instruction: the threads that used CPU time outside the trace
        // during the interval [sampleFrom, seq) (see ftt::CpuSample)
        bool sampled = false;
        uint64_t sampleFrom = 0;
        uint64_t sampleInterval = 0; // 100 ns units
        std::vector<ftt::CpuSampleThread> outside;
    };

    // The registers of a REGDUMP in the .ftt catalog, for the selected levels
    void TraceExportRegisters(const TraceExportEntry & entry, uint32_t levels, ftt::RegisterFile & regs)
    {
        const auto & catalog = ftt::Registers(sizeof(duint));
        const auto & context = entry.registers.regcontext;
        size_t id = 0;
        for(; id < TraceExportGprCount; id++)
            regs.set((unsigned)id, &entry.regwords[id], sizeof(duint));
        for(int i = 0; i < 8; i++, id++)
            if(levels & ftt::LevelFpu)
                regs.set((unsigned)id, context.RegisterArea + i * 10, 10);
        if(levels & ftt::LevelFpu)
        {
            regs.set((unsigned)id, &context.x87fpu.ControlWord, 2);
            regs.set((unsigned)id + 1, &context.x87fpu.StatusWord, 2);
            regs.set((unsigned)id + 2, &context.x87fpu.TagWord, 2);
        }
        id += 3;
        for(size_t i = 0; i < _countof(context.XmmRegisters); i++, id++)
            if(levels & ftt::LevelSse)
                regs.set((unsigned)id, &context.XmmRegisters[i], 16);
        if(levels & ftt::LevelSse)
            regs.set((unsigned)id, &context.MxCsr, 4);
        id++;
        for(size_t i = 0; i < _countof(context.YmmRegisters); i++, id++)
            if(levels & ftt::LevelAvx)
                regs.set((unsigned)id, &context.YmmRegisters[i].High, 16);
        if(id != catalog.size())
            throw String("Register catalog mismatch");
    }

    // Memory known so far while exporting a .ftt (it omits the old content of an access when it is
    // already known): fills those values back
    class TraceExportShadow
    {
    public:
        void complete(ftt::MemoryAccess & access)
        {
            if(access.flags & ftt::MemoryOldOmitted)
            {
                for(uint8_t i = 0; i < access.size; i++)
                {
                    auto it = mBytes.find(access.address + i);
                    access.oldData[i] = it == mBytes.end() ? 0 : it->second;
                }
                access.flags &= ~ftt::MemoryOldOmitted;
            }
            for(uint8_t i = 0; i < access.size; i++)
                mBytes[access.address + i] = (access.flags & ftt::MemoryWritten) ? access.newData[i] : access.oldData[i];
        }

    private:
        std::unordered_map<uint64_t, uint8_t> mBytes;
    };

    struct TraceExportHeader
    {
        String path;
        String hash;
        duint hashValue = 0;
    };

    TraceExportHeader TraceExportParseMeta(const char* data, size_t size, bool checkVersion)
    {
        json_error_t error;
        auto root = json_loadb(data, size, 0, &error);
        if(!root)
            throw String("Trace file header is not valid JSON");
        TraceExportHeader header;
        String failure;
        auto ver = json_integer_value(json_object_get(root, "ver"));
        auto arch = json_string_value(json_object_get(root, "arch"));
        auto path = json_string_value(json_object_get(root, "path"));
        auto hash = json_object_get(root, "hash");
        if(checkVersion && ver != 1)
            failure = StringUtils::sprintf("Unsupported trace file version %lld", (long long)ver);
        else if(!arch || strcmp(arch, ArchValue("x86", "x64")) != 0)
            failure = StringUtils::sprintf("This trace was recorded on %s, export it with %s", arch ? arch : "an unknown architecture", ArchValue("x64dbg", "x32dbg"));
        if(path)
            header.path = path;
        if(json_is_string(hash))
        {
            header.hash = json_string_value(hash);
            header.hashValue = (duint)json_hex_value(hash);
        }
        json_decref(root);
        if(!failure.empty())
            throw failure;
        return header;
    }

    TraceExportHeader TraceExportReadHeader(TraceExportReader & reader)
    {
        uint8_t magic[4];
        uint32_t headerSize;
        if(!reader.readExact(magic, sizeof(magic)) || memcmp(magic, "TRAC", sizeof(magic)) != 0)
            throw String("Not a trace file (missing TRAC header)");
        if(!reader.readExact(&headerSize, sizeof(headerSize)) || headerSize > 100 * 1024)
            throw String("Invalid trace file header size");
        std::vector<char> headerData(headerSize);
        if(!reader.readExact(headerData.data(), headerData.size()))
            throw String("Trace file header is truncated");
        return TraceExportParseMeta(headerData.data(), headerData.size(), true);
    }

    using TraceExportCallback = std::function<void(TraceExportRecord &)>;

    // A CPU time sample after the last instruction (taken when the recording stopped)
    struct TraceExportTrailingSample
    {
        bool present = false;
        uint64_t from = 0;
        uint64_t interval = 0;
        std::vector<ftt::CpuSampleThread> threads;
    };

    // Thrown by a callback to stop reading (its error is already in the result)
    struct TraceExportAbort { };

    // Reads a .trace32/.trace64 recorded by x64dbg and calls back for each instruction
    bool TraceExportReadTrace(const char* traceFileName, uint32_t levels, TraceExportHeader & header, TraceExportResult & result, TraceExportTrailingSample & trailing, const TraceExportCallback & callback)
    {
        // FILE_SHARE_WRITE allows exporting a trace that is still being recorded
        Handle hTrace = CreateFileW(StringUtils::Utf8ToUtf16(traceFileName).c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
        if(!hTrace)
        {
            result.error = StringUtils::sprintf("Cannot open trace file \"%s\" (error %u)", traceFileName, GetLastError());
            return false;
        }
        TraceExportReader reader(hTrace);
        header = TraceExportReadHeader(reader);

        TraceExportEntry entry;
        TraceExportSwitchMarker marker = {};
        bool hasMarker = false;
        TraceExportSkippedCall skipped = {};
        bool hasSkipped = false;
        // CPU time samples: the previous one, and the threads recorded since it
        TraceExportCpuSample sample, base;
        bool hasBase = false;
        uint64_t baseSeq = 0;
        std::set<uint32_t> recordedSinceBase;
        bool pendingSample = false;
        uint64_t pendingFrom = 0;
        uint64_t pendingInterval = 0;
        std::vector<ftt::CpuSampleThread> pendingOutside;
        bool hasPrevious = false;
        uint32_t previousThread = 0;
        TraceExportRecord record;
        uint64_t seq = 0;
        try
        {
            while(true)
            {
                auto blockResult = TraceExportReadBlock(reader, entry, marker, sample, skipped);
                if(blockResult == TraceExportBlockResult::EndOfFile)
                    break;
                if(blockResult == TraceExportBlockResult::Truncated)
                {
                    // The last block can be incomplete when the trace is still being recorded
                    result.truncated = true;
                    break;
                }
                if(blockResult == TraceExportBlockResult::SwitchMarker)
                {
                    hasMarker = true;
                    continue;
                }
                if(blockResult == TraceExportBlockResult::SkippedCall)
                {
                    hasSkipped = true;
                    continue;
                }
                if(blockResult == TraceExportBlockResult::CpuSample)
                {
                    // an interval without instruction (two samples in a row) tells nothing
                    pendingSample = hasBase && !(sample.flags & 1) && sample.frequency != 0 && seq > baseSeq;
                    if(pendingSample)
                    {
                        pendingFrom = baseSeq;
                        pendingInterval = (uint64_t)((double)(sample.counter - base.counter) * 10000000.0 / (double)sample.frequency);
                        pendingOutside.clear();
                        // The recorded threads used CPU time for their recorded instructions (and the
                        // single steps): only the others ran outside the trace
                        for(auto & thread : sample.threads)
                        {
                            if(recordedSinceBase.count(thread.thread))
                                continue;
                            const TraceExportCpuThread* before = nullptr;
                            for(auto & previousThread : base.threads)
                                if(previousThread.thread == thread.thread)
                                    before = &previousThread;
                            const uint64_t cycles = thread.cycles - (before ? std::min(before->cycles, thread.cycles) : 0);
                            if(cycles == 0)
                                continue;
                            ftt::CpuSampleThread outside;
                            outside.thread = thread.thread;
                            outside.cycles = cycles;
                            // a new thread, or one without a register hash, may have executed anything
                            outside.userCode = !before || !(before->flags & 1) || !(thread.flags & 1) || before->contextHash != thread.contextHash;
                            pendingOutside.push_back(outside);
                        }
                    }
                    base = sample;
                    hasBase = true;
                    baseSeq = seq;
                    recordedSinceBase.clear();
                    continue;
                }
                if(blockResult == TraceExportBlockResult::UserBlock)
                    continue;

                record.seq = seq++;
                record.thread = entry.threadId;
                record.ip = entry.registers.regcontext.cip;
                memcpy(record.opcode, entry.opcode, sizeof(record.opcode));
                record.opcodeSize = entry.opcodeSize;
                record.regs = ftt::RegisterFile();
                TraceExportRegisters(entry, levels, record.regs);
                record.switched = hasPrevious && previousThread != entry.threadId;
                record.fromThread = record.switched ? previousThread : 0;
                record.timestamp = 0;
                if(record.switched && hasMarker && marker.frequency)
                    record.timestamp = (uint64_t)((double)marker.counter * 10000000.0 / (double)marker.frequency);
                hasMarker = false;
                record.skipped = hasSkipped && (uint64_t)record.ip == skipped.address; // the block that follows (an appended recording restarts the indexes)
                record.skipDestination = record.skipped ? skipped.destination : 0;
                hasSkipped = false;
                record.sampled = pendingSample;
                record.sampleFrom = pendingFrom;
                record.sampleInterval = pendingInterval;
                record.outside.clear();
                if(pendingSample)
                    record.outside.swap(pendingOutside);
                pendingSample = false;
                recordedSinceBase.insert(entry.threadId);
                record.memory.clear();
                for(unsigned char i = 0; i < entry.memoryCount; i++)
                {
                    ftt::MemoryAccess access;
                    access.address = entry.memoryAddress[i];
                    access.size = sizeof(duint);
                    memcpy(access.oldData, &entry.memoryOld[i], sizeof(duint));
                    // Bit 0 set: the memory is unchanged (read, or rewritten with the same value)
                    if(!(entry.memoryFlags[i] & 1))
                    {
                        access.flags = ftt::MemoryWritten;
                        memcpy(access.newData, &entry.memoryNew[i], sizeof(duint));
                    }
                    record.memory.push_back(access);
                }
                hasPrevious = true;
                previousThread = entry.threadId;
                callback(record);
            }
        }
        catch(const String & error)
        {
            result.error = StringUtils::sprintf("Malformed trace file after %llu instructions: %s", (unsigned long long)seq, error.c_str());
        }
        trailing.present = pendingSample;
        trailing.from = pendingFrom;
        trailing.interval = pendingInterval;
        trailing.threads = pendingOutside;
        return true;
    }

    // Reads a .ftt trace and calls back for each instruction of the selected thread (0 = all), in
    // the global order of the instructions. levels is masked with the registers the .ftt holds,
    // before the first callback
    bool TraceExportReadFtt(const char* traceFileName, uint32_t thread, TraceExportHeader & header, TraceExportResult & result, uint32_t & levels, TraceExportTrailingSample & trailing, const TraceExportCallback & callback)
    {
        ftt::Reader reader;
        std::string error;
        if(!reader.open(StringUtils::Utf8ToUtf16(traceFileName), error))
        {
            result.error = StringUtils::sprintf("Cannot read \"%s\": %s", traceFileName, error.c_str());
            return false;
        }
        if(reader.pointerSize() != sizeof(duint))
        {
            result.error = StringUtils::sprintf("This trace was recorded on %s, export it with %s", reader.pointerSize() == 8 ? "x64" : "x86", ArchValue("x64dbg", "x32dbg"));
            return false;
        }
        if(!reader.meta().empty())
            header = TraceExportParseMeta(reader.meta().data(), reader.meta().size(), false);
        levels &= reader.levels() | ftt::LevelGpr;
        TraceExportShadow shadow;
        TraceExportRecord record;
        const auto & samples = reader.cpuSamples();
        size_t nextSample = 0;
        // every thread is read, so that the memory known so far includes their accesses
        reader.forEach(0, [&](const ftt::Instruction & ins, const ftt::RegisterFile & regs)
        {
            record.memory = ins.memory;
            for(auto & access : record.memory)
                shadow.complete(access);
            while(nextSample < samples.size() && samples[nextSample].seq < ins.seq)
                nextSample++;
            record.sampled = nextSample < samples.size() && samples[nextSample].seq == ins.seq;
            record.sampleFrom = record.sampled ? samples[nextSample].from : 0;
            record.sampleInterval = record.sampled ? samples[nextSample].interval : 0;
            record.outside.clear();
            if(record.sampled)
                record.outside = samples[nextSample].threads;
            if(thread && ins.thread != thread)
                return true;
            record.seq = ins.seq;
            record.thread = ins.thread;
            record.ip = (duint)ins.ip;
            memcpy(record.opcode, ins.opcode, sizeof(record.opcode));
            record.opcodeSize = ins.opcodeSize;
            record.regs = regs;
            record.switched = ins.switched;
            record.fromThread = ins.fromThread;
            record.timestamp = ins.switchTimestamp;
            callback(record);
            return true;
        });
        if(!samples.empty() && samples.back().seq >= reader.count())
        {
            trailing.present = true;
            trailing.from = samples.back().from;
            trailing.interval = samples.back().interval;
            trailing.threads = samples.back().threads;
        }
        return true;
    }

    String TraceExportHex(duint value)
    {
        return StringUtils::sprintf("0x%llX", (unsigned long long)value);
    }

    // Little-endian bytes as a hexadecimal number (most significant digit first)
    String TraceExportHexBytes(const unsigned char* data, size_t size)
    {
        String digits;
        for(size_t i = size; i-- > 0;)
            digits += StringUtils::sprintf("%02X", data[i]);
        auto first = digits.find_first_not_of('0');
        return "0x" + (first == String::npos ? String("0") : digits.substr(first));
    }

    String TraceExportBytes(const TraceExportRecord & record)
    {
        String result;
        for(unsigned char i = 0; i < record.opcodeSize; i++)
        {
            if(i)
                result.push_back(' ');
            result += StringUtils::sprintf("%02X", record.opcode[i]);
        }
        return result;
    }

    void TraceExportJsonString(String & out, const String & s)
    {
        out.push_back('"');
        for(unsigned char ch : s)
        {
            switch(ch)
            {
            case '"':
                out += "\\\"";
                break;
            case '\\':
                out += "\\\\";
                break;
            case '\n':
                out += "\\n";
                break;
            case '\r':
                out += "\\r";
                break;
            case '\t':
                out += "\\t";
                break;
            default:
                if(ch < 0x20)
                    out += StringUtils::sprintf("\\u%04X", ch);
                else
                    out.push_back(ch);
                break;
            }
        }
        out.push_back('"');
    }

    void TraceExportCsvString(String & out, const String & s)
    {
        out.push_back('"');
        for(char ch : s)
        {
            if(ch == '"')
                out.push_back('"');
            out.push_back(ch);
        }
        out.push_back('"');
    }

    // Tenet text trace: one line per instruction, "name=0xvalue" entries separated by commas.
    // Reference: https://github.com/gaasedelen/tenet/tree/master/tracers
    String TraceExportTenetHex(duint value)
    {
        return StringUtils::sprintf("0x%llx", (unsigned long long)value);
    }

    // Appends the general purpose registers that differ from state (all of them when full is set) and updates state
    void TraceExportTenetRegisters(String & line, const ftt::RegisterFile & regs, duint* state, bool full)
    {
        const auto & catalog = ftt::Registers(sizeof(duint));
        for(size_t i = 0; i < TraceExportIpIndex; i++)
        {
            const duint value = (duint)regs.get((unsigned)i);
            if(!full && value == state[i])
                continue;
            if(!line.empty())
                line.push_back(',');
            line += catalog[i].name;
            line.push_back('=');
            line += TraceExportTenetHex(value);
            state[i] = value;
        }
    }

    // Appends the memory accessed by an instruction with its content after execution: "mr" for an unchanged
    // value, "mw" for a modified one, the bytes in memory order
    void TraceExportTenetMemory(String & line, const TraceExportRecord & record)
    {
        for(auto & access : record.memory)
        {
            const bool written = (access.flags & ftt::MemoryWritten) != 0;
            line += written ? ",mw=" : ",mr=";
            line += TraceExportTenetHex((duint)access.address);
            line.push_back(':');
            const unsigned char* value = written ? access.newData : access.oldData;
            for(uint8_t b = 0; b < access.size; b++)
                line += StringUtils::sprintf("%02x", value[b]);
        }
    }

    bool TraceExportSamePath(const char* a, const char* b)
    {
        wchar_t fullA[MAX_PATH] = L"", fullB[MAX_PATH] = L"";
        if(!GetFullPathNameW(StringUtils::Utf8ToUtf16(a).c_str(), _countof(fullA), fullA, nullptr)
                || !GetFullPathNameW(StringUtils::Utf8ToUtf16(b).c_str(), _countof(fullB), fullB, nullptr))
            return false;
        return _wcsicmp(fullA, fullB) == 0;
    }
}

bool TraceExportFile(const char* traceFileName, const char* outputFileName, TraceExportFormat format, TraceExportResult & result, const TraceExportOptions & options)
{
    result = TraceExportResult();
#ifndef _WIN64
    if(format == TraceExportFormat::Frinet)
    {
        // The Frinet IDA plugin looks for the instruction pointer as rip (x64) or pc (ARM64)
        result.error = "The Frinet format only supports 64-bit traces, use the tenet format for 32-bit traces";
        return false;
    }
#endif //_WIN64
    if(TraceExportSamePath(traceFileName, outputFileName))
    {
        result.error = "The output file must be different from the trace file";
        return false;
    }
    const bool fromFtt = ftt::Reader::IsFtt(StringUtils::Utf8ToUtf16(traceFileName));
    if(fromFtt && format == TraceExportFormat::Ftt)
    {
        result.error = "The trace is already in the .ftt format";
        return false;
    }

    // The levels written by json, csv and ftt; a .ftt source has the levels it was written with
    uint32_t levels = options.registers | ftt::LevelGpr;
    const auto & catalog = ftt::Registers(sizeof(duint));

    // The source is read first into the output: open the output only once the source header is valid
    FILE* output = nullptr;
    ftt::Writer writer;
    bool started = false;
    TraceExportHeader header;
    bool resolveModules = false;
    String line;

    // Tenet and Frinet store the registers as differences from the previous line
    duint tenetState[32] = {};
    bool tenetFull = true;
    TraceExportRecord previous;
    bool hasPrevious = false;
    std::set<uint32_t> threads;
    Zydis zydis;

    // Frinet line: the executed instruction, then its effects (registers and memory after execution)
    auto writeFrinetLine = [&](const TraceExportRecord & executed, const TraceExportRecord * next)
    {
        line = catalog[TraceExportIpIndex].name;
        line.push_back('=');
        line += TraceExportTenetHex(executed.ip);
        // The registers after execution are only known when the next instruction runs on the same thread
        if(next && next->thread == executed.thread)
        {
            TraceExportTenetRegisters(line, next->regs, tenetState, tenetFull);
            tenetFull = false;
        }
        TraceExportTenetMemory(line, executed);
        line.push_back('\n');
        fwrite(line.data(), 1, line.size(), output);
        result.count++;
    };

    auto start = [&]() -> bool
    {
        if(format == TraceExportFormat::Ftt)
        {
            json_t* meta = json_object();
            json_object_set_new(meta, "ver", json_integer(1));
            json_object_set_new(meta, "arch", json_string(ArchValue("x86", "x64")));
            json_object_set_new(meta, "path", json_string(header.path.c_str()));
            json_object_set_new(meta, "hash", json_string(header.hash.c_str()));
            json_object_set_new(meta, "source", json_string(traceFileName));
            auto text = json_dumps(meta, JSON_COMPACT);
            std::string metaText = text ? text : "{}";
            json_free(text);
            json_decref(meta);
            std::string error;
            if(!writer.create(StringUtils::Utf8ToUtf16(outputFileName), sizeof(duint), levels, options.snapshotInterval, metaText, error))
            {
                result.error = StringUtils::sprintf("Cannot create output file \"%s\"", outputFileName);
                return false;
            }
            return true;
        }
        output = _wfopen(StringUtils::Utf8ToUtf16(outputFileName).c_str(), L"wb");
        if(!output)
        {
            result.error = StringUtils::sprintf("Cannot create output file \"%s\"", outputFileName);
            return false;
        }
        setvbuf(output, nullptr, _IOFBF, 1024 * 1024);

        // Module names are only meaningful while debugging the process that recorded the trace
        resolveModules = dbgisdebugging() && header.hashValue != 0 && header.hashValue == dbgfunctionsget()->DbGetHash();
        // Frinet rebases the trace on the IDA database with the ASLR slide, the runtime base of the traced module
        if(format == TraceExportFormat::Frinet)
            result.slide = options.slide ? options.slide : (resolveModules ? dbgdebuggedbase() : 0);

        line.clear();
        if(format == TraceExportFormat::Json)
        {
            line = "{\n\"version\": 1,\n\"arch\": ";
            TraceExportJsonString(line, ArchValue("x86", "x64"));
            line += ",\n\"path\": ";
            TraceExportJsonString(line, header.path);
            line += ",\n\"hash\": ";
            TraceExportJsonString(line, header.hash);
            if(options.thread)
                line += ",\n\"thread\": " + std::to_string(options.thread);
            line += ",\n\"instructions\": [\n";
        }
        else if(format == TraceExportFormat::Frinet)
        {
            if(result.slide)
                line = "slide=" + TraceExportTenetHex(result.slide) + "\n";
        }
        else if(format == TraceExportFormat::Csv)
        {
            line = "index,thread,address,module,bytes,disasm";
            for(auto & reg : catalog)
                if(reg.level & levels)
                {
                    line.push_back(',');
                    line += reg.name;
                }
            line += ",memory,seq\r\n";
        }
        fwrite(line.data(), 1, line.size(), output);
        return true;
    };

    auto onRecord = [&](TraceExportRecord & record)
    {
        if(!started)
        {
            if(!start())
                throw TraceExportAbort();
            started = true;
        }
        threads.insert(record.thread);
        if(record.switched)
            result.switches++;
        if(record.skipped)
            result.skippedCalls++;
        if(options.thread)
            record.sampled = false;
        if(record.sampled)
        {
            result.samples++;
            for(auto & thread : record.outside)
                if(thread.userCode)
                {
                    result.concurrentSamples++;
                    break;
                }
        }

        if(format == TraceExportFormat::Ftt)
        {
            ftt::InstructionIn in;
            in.thread = record.thread;
            in.ip = record.ip;
            memcpy(in.opcode, record.opcode, sizeof(in.opcode));
            in.opcodeSize = record.opcodeSize;
            in.registers = &record.regs;
            in.memory = record.memory;
            in.switchTimestamp = record.timestamp;
            if(record.sampled)
                writer.cpuSample(record.sampleFrom, record.sampleInterval, record.outside);
            writer.instruction(in);
            result.count++;
            return;
        }
        if(format == TraceExportFormat::Tenet)
        {
            // Tenet line: the state before the instruction, with the memory accessed by the previous one
            line.clear();
            TraceExportTenetRegisters(line, record.regs, tenetState, tenetFull);
            tenetFull = false;
            if(!line.empty())
                line.push_back(',');
            line += catalog[TraceExportIpIndex].name;
            line.push_back('=');
            line += TraceExportTenetHex(record.ip);
            if(hasPrevious)
                TraceExportTenetMemory(line, previous);
            line.push_back('\n');
            fwrite(line.data(), 1, line.size(), output);
            result.count++;
        }
        else if(format == TraceExportFormat::Frinet && hasPrevious)
            writeFrinetLine(previous, &record);
        if(format == TraceExportFormat::Tenet || format == TraceExportFormat::Frinet)
        {
            previous = record;
            hasPrevious = true;
            return;
        }

        const duint cip = record.ip;
        const String disasm = zydis.Disassemble(cip, record.opcode, record.opcodeSize) ? zydis.InstructionText() : "???";
        char moduleName[MAX_MODULE_SIZE] = "";
        if(resolveModules)
            ModNameFromAddr(cip, moduleName, true);

        line.clear();
        if(format == TraceExportFormat::Json)
        {
            line += result.count ? ",\n{\"index\": " : "{\"index\": ";
            line += std::to_string(result.count);
            line += ", \"seq\": ";
            line += std::to_string(record.seq);
            line += ", \"thread\": ";
            line += std::to_string(record.thread);
            if(record.switched)
            {
                line += ", \"switch\": {\"from\": " + std::to_string(record.fromThread);
                if(record.timestamp)
                    line += ", \"timestamp\": " + std::to_string(record.timestamp);
                line += "}";
            }
            if(record.skipped)
            {
                // the call ran untraced: the next instruction of this thread is after its return
                line += ", \"skip\": ";
                TraceExportJsonString(line, TraceExportHex((duint)record.skipDestination));
            }
            if(record.sampled)
            {
                // outside: threads that executed code outside the trace; kernel: CPU time in the kernel only
                line += ", \"cpu\": {\"from\": " + std::to_string(record.sampleFrom) + ", \"interval\": " + std::to_string(record.sampleInterval);
                for(bool userCode : { true, false })
                {
                    line += userCode ? ", \"outside\": {" : ", \"kernel\": {";
                    bool first = true;
                    for(auto & thread : record.outside)
                    {
                        if(thread.userCode != userCode)
                            continue;
                        if(!first)
                            line += ", ";
                        first = false;
                        line += "\"" + std::to_string(thread.thread) + "\": " + std::to_string(thread.cycles);
                    }
                    line += "}";
                }
                line += "}";
            }
            line += ", \"address\": ";
            TraceExportJsonString(line, TraceExportHex(cip));
            if(*moduleName)
            {
                line += ", \"module\": ";
                TraceExportJsonString(line, moduleName);
            }
            line += ", \"bytes\": ";
            TraceExportJsonString(line, TraceExportBytes(record));
            line += ", \"disasm\": ";
            TraceExportJsonString(line, disasm);
            line += ", \"regs\": {";
            bool first = true;
            for(size_t i = 0; i < catalog.size(); i++)
            {
                if(!(catalog[i].level & levels))
                    continue;
                if(!first)
                    line += ", ";
                first = false;
                TraceExportJsonString(line, catalog[i].name);
                line += ": ";
                TraceExportJsonString(line, TraceExportHexBytes(record.regs.value[i], catalog[i].size));
            }
            line += "}, \"mem\": [";
            for(size_t i = 0; i < record.memory.size(); i++)
            {
                auto & access = record.memory[i];
                if(i)
                    line += ", ";
                line += "{\"address\": ";
                TraceExportJsonString(line, TraceExportHex((duint)access.address));
                line += ", \"size\": " + std::to_string(access.size);
                line += ", \"old\": ";
                TraceExportJsonString(line, TraceExportHexBytes(access.oldData, access.size));
                line += ", \"new\": ";
                TraceExportJsonString(line, TraceExportHexBytes((access.flags & ftt::MemoryWritten) ? access.newData : access.oldData, access.size));
                line += "}";
            }
            line += "]}";
        }
        else
        {
            line += std::to_string(result.count);
            line.push_back(',');
            line += std::to_string(record.thread);
            line.push_back(',');
            line += TraceExportHex(cip);
            line.push_back(',');
            TraceExportCsvString(line, moduleName);
            line.push_back(',');
            TraceExportCsvString(line, TraceExportBytes(record));
            line.push_back(',');
            TraceExportCsvString(line, disasm);
            for(size_t i = 0; i < catalog.size(); i++)
            {
                if(!(catalog[i].level & levels))
                    continue;
                line.push_back(',');
                line += TraceExportHexBytes(record.regs.value[i], catalog[i].size);
            }
            String memory;
            for(size_t i = 0; i < record.memory.size(); i++)
            {
                auto & access = record.memory[i];
                if(i)
                    memory.push_back(';');
                memory += TraceExportHex((duint)access.address);
                memory.push_back(':');
                memory += TraceExportHexBytes(access.oldData, access.size);
                if(access.flags & ftt::MemoryWritten)
                {
                    memory += "->";
                    memory += TraceExportHexBytes(access.newData, access.size);
                }
            }
            line.push_back(',');
            TraceExportCsvString(line, memory);
            line.push_back(',');
            line += std::to_string(record.seq);
            line += "\r\n";
        }
        fwrite(line.data(), 1, line.size(), output);
        result.count++;
    };

    // json / csv / ftt export the registers of the levels chosen; from a .ftt, of the levels it has
    bool ok;
    TraceExportTrailingSample trailing;
    try
    {
        if(fromFtt)
        {
            ok = TraceExportReadFtt(traceFileName, options.thread, header, result, levels, trailing, [&](TraceExportRecord & record)
            {
                onRecord(record);
            });
        }
        else
        {
            ok = TraceExportReadTrace(traceFileName, ftt::LevelAll, header, result, trailing, [&](TraceExportRecord & record)
            {
                if(options.thread && record.thread != options.thread)
                    return;
                onRecord(record);
            });
        }
    }
    catch(const String & error)
    {
        result.error = error;
        ok = false;
    }
    catch(const TraceExportAbort &)
    {
        ok = false;
    }
    if(!ok && !started)
    {
        if(output)
            fclose(output);
        return false;
    }
    // An empty selection still produces a (valid, empty) output
    if(!started)
    {
        if(!start())
            return false;
        started = true;
    }

    result.threads = threads.size();
    if(trailing.present && !options.thread)
    {
        result.samples++;
        for(auto & thread : trailing.threads)
            if(thread.userCode)
            {
                result.concurrentSamples++;
                break;
            }
        if(format == TraceExportFormat::Ftt)
            writer.cpuSample(trailing.from, trailing.interval, trailing.threads);
    }
    if(format == TraceExportFormat::Ftt)
    {
        std::string error;
        result.snapshots = writer.snapshotCount();
        if(!writer.finish(error))
        {
            result.error = StringUtils::sprintf("Failed to write output file \"%s\"", outputFileName);
            return false;
        }
        return result.error.empty();
    }

    // The last Frinet instruction has no next instruction to read its registers from
    if(format == TraceExportFormat::Frinet && hasPrevious)
        writeFrinetLine(previous, nullptr);
    if(format == TraceExportFormat::Json)
    {
        line = result.count ? "\n]\n}\n" : "]\n}\n";
        fwrite(line.data(), 1, line.size(), output);
    }
    const bool writeFailed = ferror(output) != 0;
    if(fclose(output) != 0 || writeFailed)
    {
        result.error = StringUtils::sprintf("Failed to write output file \"%s\"", outputFileName);
        return false;
    }
    return result.error.empty();
}
