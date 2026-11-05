// Fathom Time Travel trace (.ftt): writer and reader, shared by TraceExport (x64dbg) and the
// "Time Travel" tab (ExtraCmds plugin). Header-only, standard C++17 + LZ4 (lz4.dll of x64dbg).
//
// A .ftt file is binary, indexed and compressed. Each thread has its own timeline (chunks of its
// instructions) and a global sequence number orders the instructions of all threads, so the state
// of every thread is known at any instant. Context switches are explicit markers. Registers are
// stored as deltas from a full keyframe at the start of each chunk, memory as the strict deltas of
// each instruction (bytes already known with the same value are not stored again), and memory
// snapshots taken at regular intervals bound the work needed to rebuild the state at any instant.
// Specification: docs/developers/ftt.md
#pragma once

#include <cstdint>
#include <cstring>
#include <cstdio>
#include <string>
#include <vector>
#include <map>
#include <unordered_map>
#include <algorithm>
#include <memory>
#include <functional>

#include "../lz4/lz4.h"

namespace ftt
{
    // ---------------------------------------------------------------- constants and layout ---
    constexpr char Magic[4] = { 'F', 'T', 'T', '1' };
    constexpr char ChunkMagic[4] = { 'F', 'C', 'H', 'K' };
    constexpr char TrailerMagic[4] = { 'F', 'T', 'T', 'X' };
    constexpr uint16_t Version = 1;
    constexpr uint32_t PageSize = 0x1000;

    enum ChunkKind : uint8_t
    {
        ChunkThread = 1, // the instructions of one thread
        ChunkSnapshot = 2, // memory pages whose content changed since the previous snapshot
        ChunkIndex = 3, // the index, at the end of the file
        ChunkCpuSamples = 4, // CPU time samples of the recorder (readers skip the kinds they do not know)
    };

    enum EventTag : uint8_t
    {
        EventInstruction = 1,
        EventSwitch = 2, // the previous instruction (global order) ran on another thread
    };

    enum MemoryFlags : uint8_t
    {
        MemoryWritten = 1, // new content follows
        MemoryOldOmitted = 2, // the content before the instruction was already known
    };

    // Register sets, chosen when the trace is written ("verbosity")
    enum RegisterLevel : uint32_t
    {
        LevelGpr = 1, // general purpose registers, instruction pointer, flags (always present)
        LevelFpu = 2, // x87: st0-st7, control / status / tag words
        LevelSse = 4, // xmm registers, mxcsr
        LevelAvx = 8, // upper halves of the ymm registers
        LevelAll = 15,
    };

    struct RegisterInfo
    {
        const char* name;
        uint8_t size; // bytes, little-endian
        uint32_t level;
    };

    // The register catalog of an architecture: a register's id is its position in this list
    inline const std::vector<RegisterInfo> & Registers(unsigned pointerSize)
    {
        static const std::vector<RegisterInfo> regs64 = []
        {
            std::vector<RegisterInfo> r;
            const char* gpr[] = { "rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi", "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15", "rip", "rflags" };
            for(auto n : gpr)
                r.push_back({ n, 8, LevelGpr });
            static const char* st[] = { "st0", "st1", "st2", "st3", "st4", "st5", "st6", "st7" };
            for(auto n : st)
                r.push_back({ n, 10, LevelFpu });
            r.push_back({ "fpcw", 2, LevelFpu });
            r.push_back({ "fpsw", 2, LevelFpu });
            r.push_back({ "fptw", 2, LevelFpu });
            static const char* xmm[] = { "xmm0", "xmm1", "xmm2", "xmm3", "xmm4", "xmm5", "xmm6", "xmm7", "xmm8", "xmm9", "xmm10", "xmm11", "xmm12", "xmm13", "xmm14", "xmm15" };
            for(auto n : xmm)
                r.push_back({ n, 16, LevelSse });
            r.push_back({ "mxcsr", 4, LevelSse });
            static const char* ymm[] = { "ymm0h", "ymm1h", "ymm2h", "ymm3h", "ymm4h", "ymm5h", "ymm6h", "ymm7h", "ymm8h", "ymm9h", "ymm10h", "ymm11h", "ymm12h", "ymm13h", "ymm14h", "ymm15h" };
            for(auto n : ymm)
                r.push_back({ n, 16, LevelAvx });
            return r;
        }();
        static const std::vector<RegisterInfo> regs32 = []
        {
            std::vector<RegisterInfo> r;
            const char* gpr[] = { "eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi", "eip", "eflags" };
            for(auto n : gpr)
                r.push_back({ n, 4, LevelGpr });
            static const char* st[] = { "st0", "st1", "st2", "st3", "st4", "st5", "st6", "st7" };
            for(auto n : st)
                r.push_back({ n, 10, LevelFpu });
            r.push_back({ "fpcw", 2, LevelFpu });
            r.push_back({ "fpsw", 2, LevelFpu });
            r.push_back({ "fptw", 2, LevelFpu });
            static const char* xmm[] = { "xmm0", "xmm1", "xmm2", "xmm3", "xmm4", "xmm5", "xmm6", "xmm7" };
            for(auto n : xmm)
                r.push_back({ n, 16, LevelSse });
            r.push_back({ "mxcsr", 4, LevelSse });
            static const char* ymm[] = { "ymm0h", "ymm1h", "ymm2h", "ymm3h", "ymm4h", "ymm5h", "ymm6h", "ymm7h" };
            for(auto n : ymm)
                r.push_back({ n, 16, LevelAvx });
            return r;
        }();
        return pointerSize == 8 ? regs64 : regs32;
    }

    inline unsigned IpRegister(unsigned pointerSize)
    {
        return pointerSize == 8 ? 16 : 8;
    }

    inline unsigned GprCount(unsigned pointerSize)
    {
        return pointerSize == 8 ? 18 : 10; // the instruction pointer and the flags are the last two
    }

    // Parses "gpr", "fpu", "sse", "avx" or "all", or a "+"-separated combination (gpr is implied)
    inline bool ParseLevels(const std::string & text, uint32_t & levels)
    {
        levels = LevelGpr;
        size_t start = 0;
        while(start <= text.size())
        {
            auto end = text.find('+', start);
            if(end == std::string::npos)
                end = text.size();
            std::string part = text.substr(start, end - start);
            for(auto & ch : part)
                ch = (char)tolower((unsigned char)ch);
            if(part == "all")
                levels |= LevelAll;
            else if(part == "fpu" || part == "x87")
                levels |= LevelFpu;
            else if(part == "sse")
                levels |= LevelSse;
            else if(part == "avx")
                levels |= LevelSse | LevelAvx;
            else if(part != "gpr" && !part.empty())
                return false;
            start = end + 1;
        }
        return true;
    }

    // The values of all the registers of a thread, by register id
    struct RegisterFile
    {
        uint8_t value[64][16] = {};
        bool known[64] = {};

        uint64_t get(unsigned id) const
        {
            uint64_t v = 0;
            memcpy(&v, value[id], 8);
            return v;
        }
        void set(unsigned id, const void* data, unsigned size)
        {
            memset(value[id], 0, sizeof(value[id]));
            memcpy(value[id], data, size);
            known[id] = true;
        }
    };

    // Hexadecimal text of a register value (most significant digit first)
    inline std::string RegisterText(const RegisterFile & regs, unsigned id, unsigned size)
    {
        if(!regs.known[id])
            return "?";
        std::string digits;
        for(int i = (int)size - 1; i >= 0; i--)
        {
            char hex[3];
            snprintf(hex, sizeof(hex), "%02x", regs.value[id][i]);
            digits += hex;
        }
        const auto first = digits.find_first_not_of('0');
        return "0x" + (first == std::string::npos ? std::string("0") : digits.substr(first));
    }

    struct MemoryAccess
    {
        uint64_t address = 0;
        uint8_t size = 0;
        uint8_t flags = 0; // MemoryFlags
        uint8_t oldData[16] = {}; // content before the instruction (unless MemoryOldOmitted)
        uint8_t newData[16] = {}; // content after the instruction (when MemoryWritten)
    };

    // ------------------------------------------------------------------------------ encoding ---
    class ByteWriter
    {
    public:
        std::vector<uint8_t> data;

        void u8(uint8_t v) { data.push_back(v); }
        void bytes(const void* p, size_t n)
        {
            auto b = (const uint8_t*)p;
            data.insert(data.end(), b, b + n);
        }
        template<typename T> void raw(T v) { bytes(&v, sizeof(v)); }
        void varint(uint64_t v)
        {
            while(v >= 0x80)
            {
                data.push_back((uint8_t)(v | 0x80));
                v >>= 7;
            }
            data.push_back((uint8_t)v);
        }
    };

    class ByteReader
    {
    public:
        ByteReader(const uint8_t* p, size_t n) : mPtr(p), mEnd(p + n) { }

        bool done() const { return mPtr >= mEnd; }
        bool bytes(void* dest, size_t n)
        {
            if((size_t)(mEnd - mPtr) < n)
                return false;
            memcpy(dest, mPtr, n);
            mPtr += n;
            return true;
        }
        template<typename T> bool raw(T & v) { return bytes(&v, sizeof(v)); }
        bool varint(uint64_t & v)
        {
            v = 0;
            for(int shift = 0; shift < 64; shift += 7)
            {
                if(mPtr >= mEnd)
                    return false;
                uint8_t b = *mPtr++;
                v |= (uint64_t)(b & 0x7F) << shift;
                if(!(b & 0x80))
                    return true;
            }
            return false;
        }

    private:
        const uint8_t* mPtr;
        const uint8_t* mEnd;
    };

#pragma pack(push, 1)
    struct FileHeader
    {
        char magic[4];
        uint16_t version;
        uint16_t pointerSize; // 4 or 8
        uint32_t levels; // RegisterLevel bits
        uint32_t snapshotInterval; // instructions between two memory snapshots
        uint32_t metaSize; // JSON metadata that follows the header
        uint32_t reserved;
    };

    struct ChunkHeader
    {
        char magic[4];
        uint8_t kind;
        uint8_t compressed; // 1: LZ4 block, 0: stored
        uint16_t reserved;
        uint32_t thread;
        uint32_t count; // instructions (thread chunk) or pages (snapshot)
        uint64_t firstSeq; // global sequence number of the first instruction (snapshot: state before it)
        uint64_t lastSeq;
        uint64_t firstLocal; // index of the first instruction in its thread's timeline
        uint32_t rawSize;
        uint32_t storedSize;
    };

    struct IndexEntry
    {
        uint64_t offset;
        uint8_t kind;
        uint32_t thread;
        uint32_t count;
        uint64_t firstSeq;
        uint64_t lastSeq;
        uint64_t firstLocal;
    };

    struct ThreadEntry
    {
        uint32_t thread;
        uint64_t count;
        uint64_t firstSeq;
        uint64_t lastSeq;
    };

    struct Trailer
    {
        uint64_t indexOffset;
        char magic[4];
        uint32_t reserved;
    };
#pragma pack(pop)

    // The recorder samples every thread (when the recording starts, on each context switch and every
    // 4096 instructions): its CPU time and its user-mode registers. Between two samples only one
    // thread is recorded. Another thread whose user-mode registers changed in that interval executed
    // code outside the trace, concurrently with the recorded instructions: their relative order is
    // unknown. No such thread: the global order of the interval is exact. A thread that used CPU time
    // with unchanged registers only ran kernel code (the suspensions of the debugger wake waiting
    // threads, for example).
    struct CpuSampleThread
    {
        uint32_t thread = 0;
        uint64_t cycles = 0; // CPU cycles used in the interval (user and kernel mode)
        bool userCode = false; // its user-mode registers changed: it executed code outside the trace
    };

    struct CpuSample
    {
        uint64_t from = 0; // the interval covers the instructions [from, seq)
        uint64_t seq = 0; // taken right before this instruction
        uint64_t interval = 0; // duration since the previous sample, in 100 ns units
        std::vector<CpuSampleThread> threads; // the other threads that used CPU time in the interval

        bool exact() const
        {
            for(auto & thread : threads)
                if(thread.userCode)
                    return false;
            return true;
        }
    };

    // ---------------------------------------------------------------------------- the writer ---
    struct InstructionIn
    {
        uint32_t thread = 0;
        uint64_t ip = 0;
        uint8_t opcode[16] = {};
        uint8_t opcodeSize = 0;
        const RegisterFile* registers = nullptr; // state before the instruction
        std::vector<MemoryAccess> memory;
        uint64_t switchTimestamp = 0; // optional time of a context switch to this thread
    };

    class Writer
    {
    public:
        // Instructions per thread chunk (each chunk starts with a full register keyframe)
        uint32_t chunkInstructions = 4096;

        ~Writer()
        {
            if(mFile)
                fclose(mFile);
        }

        bool create(const std::wstring & path, unsigned pointerSize, uint32_t levels, uint32_t snapshotInterval, const std::string & metaJson, std::string & error)
        {
            mPointerSize = pointerSize;
            mLevels = levels | LevelGpr;
            mSnapshotInterval = snapshotInterval ? snapshotInterval : 65536;
            mFile = _wfopen(path.c_str(), L"wb");
            if(!mFile)
            {
                error = "cannot create the output file";
                return false;
            }
            setvbuf(mFile, nullptr, _IOFBF, 1 << 20);
            FileHeader header = {};
            memcpy(header.magic, Magic, 4);
            header.version = Version;
            header.pointerSize = (uint16_t)pointerSize;
            header.levels = mLevels;
            header.snapshotInterval = mSnapshotInterval;
            header.metaSize = (uint32_t)metaJson.size();
            fwrite(&header, sizeof(header), 1, mFile);
            fwrite(metaJson.data(), 1, metaJson.size(), mFile);
            return !ferror(mFile);
        }

        // A CPU time sample taken before the next instruction, for the interval that started before
        // instruction from
        void cpuSample(uint64_t from, uint64_t interval, const std::vector<CpuSampleThread> & threads)
        {
            mSamples.raw<uint64_t>(mNextSeq);
            mSamples.varint(mNextSeq - std::min(from, mNextSeq));
            mSamples.varint(interval);
            mSamples.varint(threads.size());
            for(auto & thread : threads)
            {
                mSamples.raw<uint32_t>(thread.thread);
                mSamples.varint(thread.cycles);
                mSamples.u8(thread.userCode ? 1 : 0);
            }
            if(mSampleCount++ == 0)
                mFirstSampleSeq = mNextSeq;
            mLastSampleSeq = mNextSeq;
        }

        void instruction(const InstructionIn & in)
        {
            const uint64_t seq = mNextSeq++;
            if(seq && seq % mSnapshotInterval == 0)
                writeSnapshot(seq);

            auto & t = mThreads[in.thread];
            if(t.count == 0 && t.chunkCount == 0)
            {
                t.firstSeq = seq;
                mThreadOrder.push_back(in.thread);
            }
            if(t.chunkCount == chunkInstructions)
                flushThread(in.thread, t);
            if(t.chunkCount == 0)
            {
                // keyframe: the full register state before the first instruction of the chunk
                t.buffer.data.clear();
                t.chunkFirstSeq = seq;
                t.chunkFirstLocal = t.count;
                t.previousSeq = seq;
                t.registers = *in.registers;
                const auto & catalog = Registers(mPointerSize);
                uint16_t n = 0;
                for(unsigned id = 0; id < catalog.size(); id++)
                    if((catalog[id].level & mLevels) && in.registers->known[id])
                        n++;
                t.buffer.raw(n);
                for(unsigned id = 0; id < catalog.size(); id++)
                    if((catalog[id].level & mLevels) && in.registers->known[id])
                    {
                        t.buffer.u8((uint8_t)id);
                        t.buffer.bytes(in.registers->value[id], catalog[id].size);
                    }
            }

            if(mHasLast && mLastThread != in.thread)
            {
                t.buffer.u8(EventSwitch);
                t.buffer.raw<uint32_t>(mLastThread);
                t.buffer.raw<uint64_t>(in.switchTimestamp);
            }
            mHasLast = true;
            mLastThread = in.thread;

            auto & b = t.buffer;
            b.u8(EventInstruction);
            b.varint(seq - t.previousSeq);
            t.previousSeq = seq;
            b.u8(in.opcodeSize);
            b.bytes(in.opcode, in.opcodeSize);

            // registers: strict deltas from the previous instruction of the same thread
            const auto & catalog = Registers(mPointerSize);
            uint8_t changed[64];
            uint8_t nchanged = 0;
            for(unsigned id = 0; id < catalog.size(); id++)
            {
                if(!(catalog[id].level & mLevels) || !in.registers->known[id])
                    continue;
                if(!t.registers.known[id] || memcmp(t.registers.value[id], in.registers->value[id], catalog[id].size) != 0)
                {
                    changed[nchanged++] = (uint8_t)id;
                    t.registers.set(id, in.registers->value[id], catalog[id].size);
                }
            }
            b.u8(nchanged);
            for(uint8_t i = 0; i < nchanged; i++)
            {
                b.u8(changed[i]);
                b.bytes(in.registers->value[changed[i]], catalog[changed[i]].size);
            }

            // memory: the old content is stored only when it is not already known
            b.u8((uint8_t)in.memory.size());
            for(auto access : in.memory)
            {
                if(knownEqual(access.address, access.oldData, access.size))
                    access.flags |= MemoryOldOmitted;
                b.u8(access.flags);
                b.raw<uint64_t>(access.address);
                b.u8(access.size);
                if(!(access.flags & MemoryOldOmitted))
                    b.bytes(access.oldData, access.size);
                if(access.flags & MemoryWritten)
                    b.bytes(access.newData, access.size);
                learn(access.address, access.oldData, access.size);
                if(access.flags & MemoryWritten)
                    learn(access.address, access.newData, access.size);
            }

            t.chunkCount++;
            t.count++;
            t.lastSeq = seq;
        }

        bool finish(std::string & error)
        {
            for(auto tid : mThreadOrder)
            {
                auto & t = mThreads[tid];
                if(t.chunkCount)
                    flushThread(tid, t);
            }
            if(mSampleCount)
                writeChunk(ChunkCpuSamples, 0, mSampleCount, mFirstSampleSeq, mLastSampleSeq, 0, mSamples.data, true);
            // index
            ByteWriter index;
            index.raw<uint32_t>((uint32_t)mIndex.size());
            for(auto & e : mIndex)
                index.raw(e);
            index.raw<uint32_t>((uint32_t)mThreadOrder.size());
            for(auto tid : mThreadOrder)
            {
                auto & t = mThreads[tid];
                ThreadEntry te = { tid, t.count, t.firstSeq, t.lastSeq };
                index.raw(te);
            }
            index.raw<uint64_t>(mNextSeq);
            const uint64_t indexOffset = (uint64_t)_ftelli64(mFile);
            writeChunk(ChunkIndex, 0, 0, 0, 0, 0, index.data, false);
            Trailer trailer = {};
            trailer.indexOffset = indexOffset;
            memcpy(trailer.magic, TrailerMagic, 4);
            fwrite(&trailer, sizeof(trailer), 1, mFile);
            const bool failed = ferror(mFile) != 0;
            if(fclose(mFile) != 0 || failed)
            {
                mFile = nullptr;
                error = "failed to write the output file";
                return false;
            }
            mFile = nullptr;
            return true;
        }

        uint64_t instructionCount() const { return mNextSeq; }
        size_t snapshotCount() const { return mSnapshots; }

    private:
        struct ThreadState
        {
            ByteWriter buffer;
            RegisterFile registers;
            uint32_t chunkCount = 0;
            uint64_t chunkFirstSeq = 0, chunkFirstLocal = 0, previousSeq = 0;
            uint64_t count = 0, firstSeq = 0, lastSeq = 0;
        };

        struct Page
        {
            uint8_t known[PageSize / 8] = {};
            uint8_t data[PageSize] = {};
            bool dirty = false;
        };

        bool knownEqual(uint64_t address, const uint8_t* data, uint8_t size)
        {
            for(uint8_t i = 0; i < size; i++)
            {
                const uint64_t a = address + i;
                auto it = mPages.find(a & ~(uint64_t)(PageSize - 1));
                if(it == mPages.end())
                    return false;
                const uint32_t o = (uint32_t)(a & (PageSize - 1));
                if(!(it->second.known[o / 8] & (1 << (o % 8))) || it->second.data[o] != data[i])
                    return false;
            }
            return true;
        }

        void learn(uint64_t address, const uint8_t* data, uint8_t size)
        {
            for(uint8_t i = 0; i < size; i++)
            {
                const uint64_t a = address + i;
                auto & page = mPages[a & ~(uint64_t)(PageSize - 1)];
                const uint32_t o = (uint32_t)(a & (PageSize - 1));
                const bool known = (page.known[o / 8] & (1 << (o % 8))) != 0;
                if(!known || page.data[o] != data[i])
                {
                    page.known[o / 8] |= (uint8_t)(1 << (o % 8));
                    page.data[o] = data[i];
                    page.dirty = true;
                }
            }
        }

        // The pages whose content (or what is known of it) changed since the previous snapshot,
        // as they are before instruction seq
        void writeSnapshot(uint64_t seq)
        {
            ByteWriter b;
            uint32_t count = 0;
            for(auto & it : mPages)
            {
                if(!it.second.dirty)
                    continue;
                b.raw<uint64_t>(it.first);
                b.bytes(it.second.known, sizeof(it.second.known));
                b.bytes(it.second.data, sizeof(it.second.data));
                it.second.dirty = false;
                count++;
            }
            writeChunk(ChunkSnapshot, 0, count, seq, seq, 0, b.data, true);
            mSnapshots++;
        }

        void flushThread(uint32_t tid, ThreadState & t)
        {
            writeChunk(ChunkThread, tid, t.chunkCount, t.chunkFirstSeq, t.lastSeq, t.chunkFirstLocal, t.buffer.data, true);
            t.buffer.data.clear();
            t.chunkCount = 0;
        }

        void writeChunk(uint8_t kind, uint32_t thread, uint32_t count, uint64_t firstSeq, uint64_t lastSeq, uint64_t firstLocal, const std::vector<uint8_t> & raw, bool indexed)
        {
            std::vector<char> stored;
            uint8_t compressed = 0;
            if(!raw.empty())
            {
                stored.resize(LZ4_compressBound((int)raw.size()));
                int n = LZ4_compress_limitedOutput((const char*)raw.data(), stored.data(), (int)raw.size(), (int)stored.size());
                if(n > 0 && (size_t)n < raw.size())
                {
                    stored.resize(n);
                    compressed = 1;
                }
                else
                    stored.assign(raw.begin(), raw.end());
            }
            ChunkHeader h = {};
            memcpy(h.magic, ChunkMagic, 4);
            h.kind = kind;
            h.compressed = compressed;
            h.thread = thread;
            h.count = count;
            h.firstSeq = firstSeq;
            h.lastSeq = lastSeq;
            h.firstLocal = firstLocal;
            h.rawSize = (uint32_t)raw.size();
            h.storedSize = (uint32_t)stored.size();
            const uint64_t offset = (uint64_t)_ftelli64(mFile);
            fwrite(&h, sizeof(h), 1, mFile);
            if(!stored.empty())
                fwrite(stored.data(), 1, stored.size(), mFile);
            if(indexed)
                mIndex.push_back({ offset, kind, thread, count, firstSeq, lastSeq, firstLocal });
        }

        FILE* mFile = nullptr;
        unsigned mPointerSize = 8;
        uint32_t mLevels = LevelGpr;
        uint32_t mSnapshotInterval = 65536;
        uint64_t mNextSeq = 0;
        bool mHasLast = false;
        uint32_t mLastThread = 0;
        size_t mSnapshots = 0;
        std::map<uint32_t, ThreadState> mThreads;
        std::vector<uint32_t> mThreadOrder;
        std::unordered_map<uint64_t, Page> mPages;
        std::vector<IndexEntry> mIndex;
        ByteWriter mSamples;
        uint32_t mSampleCount = 0;
        uint64_t mFirstSampleSeq = 0;
        uint64_t mLastSampleSeq = 0;
    };

    // ---------------------------------------------------------------------------- the reader ---
    struct Instruction
    {
        uint64_t seq = 0;
        uint64_t local = 0; // position in its thread's timeline
        uint32_t thread = 0;
        uint64_t ip = 0;
        uint8_t opcode[16] = {};
        uint8_t opcodeSize = 0;
        bool switched = false; // context switch: the previous instruction ran on fromThread
        uint32_t fromThread = 0;
        uint64_t switchTimestamp = 0;
        std::vector<std::pair<uint8_t, std::vector<uint8_t>>> changes; // register deltas
        std::vector<MemoryAccess> memory;
    };

    struct ThreadInfo
    {
        uint32_t thread = 0;
        uint64_t count = 0;
        uint64_t firstSeq = 0;
        uint64_t lastSeq = 0;
    };

    class Reader
    {
    public:
        ~Reader()
        {
            if(mFile)
                fclose(mFile);
        }

        static bool IsFtt(const std::wstring & path)
        {
            FILE* f = _wfopen(path.c_str(), L"rb");
            if(!f)
                return false;
            char magic[4] = {};
            const bool ok = fread(magic, 1, 4, f) == 4 && memcmp(magic, Magic, 4) == 0;
            fclose(f);
            return ok;
        }

        bool open(const std::wstring & path, std::string & error)
        {
            mFile = _wfopen(path.c_str(), L"rb");
            if(!mFile)
            {
                error = "cannot open the file";
                return false;
            }
            FileHeader header;
            if(fread(&header, sizeof(header), 1, mFile) != 1 || memcmp(header.magic, Magic, 4) != 0)
            {
                error = "not a Fathom Time Travel trace (missing FTT1 header)";
                return false;
            }
            if(header.version != Version || (header.pointerSize != 4 && header.pointerSize != 8))
            {
                error = "unsupported .ftt version";
                return false;
            }
            mHeader = header;
            mMeta.resize(header.metaSize);
            if(header.metaSize && fread(&mMeta[0], 1, header.metaSize, mFile) != header.metaSize)
            {
                error = "truncated metadata";
                return false;
            }
            Trailer trailer;
            if(_fseeki64(mFile, -(int64_t)sizeof(trailer), SEEK_END) != 0 || fread(&trailer, sizeof(trailer), 1, mFile) != 1
                    || memcmp(trailer.magic, TrailerMagic, 4) != 0)
            {
                error = "the index is missing (incomplete file?)";
                return false;
            }
            std::vector<uint8_t> raw;
            ChunkHeader ih;
            if(!readChunk(trailer.indexOffset, ih, raw) || ih.kind != ChunkIndex)
            {
                error = "unreadable index";
                return false;
            }
            ByteReader r(raw.data(), raw.size());
            uint32_t n = 0;
            if(!r.raw(n))
            {
                error = "unreadable index";
                return false;
            }
            for(uint32_t i = 0; i < n; i++)
            {
                IndexEntry e;
                if(!r.raw(e))
                {
                    error = "unreadable index";
                    return false;
                }
                if(e.kind == ChunkThread)
                    mThreadChunks[e.thread].push_back(e);
                else if(e.kind == ChunkSnapshot)
                    mSnapshots.push_back(e);
                else if(e.kind == ChunkCpuSamples && !readSamples(e))
                {
                    error = "unreadable CPU time samples";
                    return false;
                }
            }
            uint32_t nt = 0;
            r.raw(nt);
            for(uint32_t i = 0; i < nt; i++)
            {
                ThreadEntry te;
                if(!r.raw(te))
                    break;
                mThreads.push_back({ te.thread, te.count, te.firstSeq, te.lastSeq });
            }
            r.raw(mCount);
            for(auto & it : mThreadChunks)
                std::sort(it.second.begin(), it.second.end(), [](const IndexEntry & a, const IndexEntry & b) { return a.firstSeq < b.firstSeq; });
            std::sort(mSnapshots.begin(), mSnapshots.end(), [](const IndexEntry & a, const IndexEntry & b) { return a.firstSeq < b.firstSeq; });
            return true;
        }

        unsigned pointerSize() const { return mHeader.pointerSize; }
        uint32_t levels() const { return mHeader.levels; }
        uint32_t snapshotInterval() const { return mHeader.snapshotInterval; }
        const std::string & meta() const { return mMeta; }
        uint64_t count() const { return mCount; }
        const std::vector<ThreadInfo> & threads() const { return mThreads; }
        size_t snapshotCount() const { return mSnapshots.size(); }
        // CPU time samples of the recorder, by seq (empty for traces recorded before they existed)
        const std::vector<CpuSample> & cpuSamples() const { return mSamples; }

        // The sample whose interval contains instruction seq (the first one taken after it), or
        // nullptr when no sample covers it (no samples, before the first or after the last one).
        // For a context switch at seq, the order between seq - 1 and seq is that of sample(seq - 1).
        const CpuSample* cpuSample(uint64_t seq) const
        {
            auto it = std::upper_bound(mSamples.begin(), mSamples.end(), seq, [](uint64_t s, const CpuSample & sample) { return s < sample.seq; });
            if(it == mSamples.end() || it->from > seq)
                return nullptr;
            return &*it;
        }

        // The spans of global time covered by a thread's chunks (the thread runs inside them,
        // interleaved with the others)
        std::vector<std::pair<uint64_t, uint64_t>> threadRanges(uint32_t thread) const
        {
            std::vector<std::pair<uint64_t, uint64_t>> out;
            auto it = mThreadChunks.find(thread);
            if(it != mThreadChunks.end())
                for(auto & c : it->second)
                    out.push_back({ c.firstSeq, c.lastSeq });
            return out;
        }

        size_t chunkCount() const
        {
            size_t n = 0;
            for(auto & it : mThreadChunks)
                n += it.second.size();
            return n;
        }

        // The instruction with global sequence number seq
        bool instruction(uint64_t seq, Instruction & out)
        {
            for(auto & it : mThreadChunks)
            {
                auto chunk = findChunk(it.second, seq);
                if(!chunk)
                    continue;
                auto decoded = decode(*chunk);
                if(!decoded)
                    continue;
                auto pos = std::lower_bound(decoded->seqs.begin(), decoded->seqs.end(), seq);
                if(pos != decoded->seqs.end() && *pos == seq)
                {
                    out = decoded->instructions[pos - decoded->seqs.begin()];
                    return true;
                }
            }
            return false;
        }

        // Registers of a thread before instruction seq (global order): the state before its next
        // instruction, or after its last one if it already ended. False if the thread has none yet.
        bool registers(uint32_t thread, uint64_t seq, RegisterFile & regs, uint64_t* atSeq = nullptr)
        {
            auto it = mThreadChunks.find(thread);
            if(it == mThreadChunks.end() || it->second.empty())
                return false;
            auto & chunks = it->second;
            // the chunk holding the first instruction of the thread at or after seq
            const IndexEntry* chunk = nullptr;
            for(auto & c : chunks)
                if(c.lastSeq >= seq)
                {
                    chunk = &c;
                    break;
                }
            bool after = false;
            if(!chunk)
            {
                chunk = &chunks.back();
                after = true;
            }
            auto decoded = decode(*chunk);
            if(!decoded)
                return false;
            regs = decoded->keyframe;
            const auto & catalog = Registers(pointerSize());
            for(size_t i = 0; i < decoded->instructions.size(); i++)
            {
                auto & ins = decoded->instructions[i];
                if(!after && ins.seq >= seq)
                {
                    applyChanges(regs, ins, catalog);
                    if(atSeq)
                        *atSeq = ins.seq;
                    return true;
                }
                applyChanges(regs, ins, catalog);
            }
            if(atSeq)
                *atSeq = decoded->instructions.empty() ? 0 : decoded->instructions.back().seq;
            return true; // registers before the last instruction (the state after it is unknown)
        }

        // Next / previous instruction of the same thread (global sequence numbers)
        bool nextInThread(uint32_t thread, uint64_t seq, uint64_t & result, bool forward)
        {
            auto it = mThreadChunks.find(thread);
            if(it == mThreadChunks.end())
                return false;
            auto & chunks = it->second;
            if(forward)
            {
                for(auto & c : chunks)
                {
                    if(c.lastSeq <= seq)
                        continue;
                    auto d = decode(c);
                    if(!d)
                        return false;
                    auto pos = std::upper_bound(d->seqs.begin(), d->seqs.end(), seq);
                    if(pos != d->seqs.end())
                    {
                        result = *pos;
                        return true;
                    }
                }
            }
            else
            {
                for(auto c = chunks.rbegin(); c != chunks.rend(); ++c)
                {
                    if(c->firstSeq >= seq)
                        continue;
                    auto d = decode(*c);
                    if(!d)
                        return false;
                    auto pos = std::lower_bound(d->seqs.begin(), d->seqs.end(), seq);
                    if(pos != d->seqs.begin())
                    {
                        result = *(pos - 1);
                        return true;
                    }
                }
            }
            return false;
        }

        // The thread running instruction seq
        bool threadAt(uint64_t seq, uint32_t & thread)
        {
            Instruction ins;
            if(!instruction(seq, ins))
                return false;
            thread = ins.thread;
            return true;
        }

        // Memory before instruction seq: known[i] tells whether data[i] is known. Content comes from
        // the last snapshot before seq, then the accesses of all threads between it and seq. Bytes
        // still unknown take the value the next access reads (inferred[i] set), if any.
        void memory(uint64_t seq, uint64_t address, size_t size, std::vector<uint8_t> & data, std::vector<bool> & known, std::vector<bool>* inferred = nullptr)
        {
            data.assign(size, 0);
            known.assign(size, false);
            if(inferred)
                inferred->assign(size, false);
            // base: the pages of the snapshots up to seq (the most recent copy of each page)
            uint64_t replayFrom = 0;
            auto last = std::upper_bound(mSnapshots.begin(), mSnapshots.end(), seq, [](uint64_t s, const IndexEntry & e) { return s < e.firstSeq; });
            if(last != mSnapshots.begin())
                replayFrom = (last - 1)->firstSeq;
            const uint64_t firstPage = address & ~(uint64_t)(PageSize - 1);
            for(uint64_t page = firstPage; page < address + size; page += PageSize)
            {
                for(auto s = last; s != mSnapshots.begin();)
                {
                    --s;
                    auto pages = snapshotPages(*s);
                    if(!pages)
                        continue;
                    auto p = pages->find(page);
                    if(p == pages->end())
                        continue;
                    for(uint32_t o = 0; o < PageSize; o++)
                    {
                        const uint64_t a = page + o;
                        if(a < address || a >= address + size)
                            continue;
                        if(p->second.known[o / 8] & (1 << (o % 8)))
                        {
                            data[(size_t)(a - address)] = p->second.data[o];
                            known[(size_t)(a - address)] = true;
                        }
                    }
                    break;
                }
            }
            // then the accesses between the snapshot and seq, in global order
            auto events = accesses(replayFrom, seq, address, size);
            for(auto & e : events)
                applyAccess(e.second, address, size, data, known, true);
            // bytes still unknown: what the next accesses observe (until the next snapshot)
            if(inferred)
            {
                const uint64_t until = seq + (uint64_t)mHeader.snapshotInterval;
                auto future = accesses(seq, until, address, size);
                for(auto & e : future)
                {
                    auto & a = e.second;
                    for(uint8_t i = 0; i < a.size; i++)
                    {
                        const uint64_t at = a.address + i;
                        if(at < address || at >= address + size)
                            continue;
                        const size_t k = (size_t)(at - address);
                        if(known[k])
                            continue;
                        if(!(a.flags & MemoryOldOmitted))
                        {
                            data[k] = a.oldData[i];
                            known[k] = true;
                            (*inferred)[k] = true;
                        }
                    }
                }
            }
        }

        // Search an instruction that executes address, from seq (exclusive) forward or backward;
        // thread 0 = any thread
        bool findExecution(uint64_t address, uint64_t seq, bool forward, uint32_t thread, uint64_t & result)
        {
            return search(seq, forward, thread, [address](const Instruction & ins) { return ins.ip == address; }, result);
        }

        // Search an instruction that writes a byte of [address, address+size)
        bool findWrite(uint64_t address, size_t size, uint64_t seq, bool forward, uint64_t & result)
        {
            return search(seq, forward, 0, [address, size](const Instruction & ins)
            {
                for(auto & m : ins.memory)
                    if((m.flags & MemoryWritten) && m.address < address + size && m.address + m.size > address)
                        return true;
                return false;
            }, result);
        }

        // All instructions in global order (thread 0 = all threads), with the register state of the
        // thread before each instruction
        bool forEach(uint32_t thread, const std::function<bool(const Instruction &, const RegisterFile &)> & callback)
        {
            struct Cursor
            {
                const std::vector<IndexEntry>* chunks;
                size_t chunk = 0, pos = 0;
                std::shared_ptr<Decoded> decoded;
                RegisterFile regs;
            };
            std::vector<Cursor> cursors;
            for(auto & it : mThreadChunks)
                if(thread == 0 || it.first == thread)
                    cursors.push_back({ &it.second });
            const auto & catalog = Registers(pointerSize());
            auto load = [&](Cursor & c) -> bool
            {
                while(c.chunk < c.chunks->size())
                {
                    if(!c.decoded)
                    {
                        c.decoded = decodeUncached((*c.chunks)[c.chunk]);
                        if(!c.decoded)
                            return false;
                        c.pos = 0;
                        c.regs = c.decoded->keyframe;
                    }
                    if(c.pos < c.decoded->instructions.size())
                        return true;
                    c.decoded.reset();
                    c.chunk++;
                }
                return false;
            };
            while(true)
            {
                Cursor* best = nullptr;
                for(auto & c : cursors)
                    if(load(c) && (!best || c.decoded->seqs[c.pos] < best->decoded->seqs[best->pos]))
                        best = &c;
                if(!best)
                    return true;
                auto & ins = best->decoded->instructions[best->pos++];
                applyChanges(best->regs, ins, catalog);
                if(!callback(ins, best->regs))
                    return false;
            }
        }

    private:
        struct Decoded
        {
            RegisterFile keyframe;
            std::vector<Instruction> instructions;
            std::vector<uint64_t> seqs;
        };

        struct SnapshotPage
        {
            uint8_t known[PageSize / 8];
            uint8_t data[PageSize];
        };
        using PageMap = std::map<uint64_t, SnapshotPage>;

        static void applyChanges(RegisterFile & regs, const Instruction & ins, const std::vector<RegisterInfo> & catalog)
        {
            for(auto & c : ins.changes)
                if(c.first < catalog.size())
                    regs.set(c.first, c.second.data(), (unsigned)c.second.size());
        }

        static void applyAccess(const MemoryAccess & a, uint64_t address, size_t size, std::vector<uint8_t> & data, std::vector<bool> & known, bool after)
        {
            for(uint8_t i = 0; i < a.size; i++)
            {
                const uint64_t at = a.address + i;
                if(at < address || at >= address + size)
                    continue;
                const size_t k = (size_t)(at - address);
                if(!(a.flags & MemoryOldOmitted))
                {
                    data[k] = a.oldData[i];
                    known[k] = true;
                }
                if(after && (a.flags & MemoryWritten))
                {
                    data[k] = a.newData[i];
                    known[k] = true;
                }
            }
        }

        // The accesses of [from, to) touching [address, address+size), sorted by sequence number
        std::vector<std::pair<uint64_t, MemoryAccess>> accesses(uint64_t from, uint64_t to, uint64_t address, size_t size)
        {
            std::vector<std::pair<uint64_t, MemoryAccess>> out;
            for(auto & it : mThreadChunks)
                for(auto & c : it.second)
                {
                    if(c.lastSeq < from || c.firstSeq >= to)
                        continue;
                    auto d = decode(c);
                    if(!d)
                        continue;
                    for(auto & ins : d->instructions)
                    {
                        if(ins.seq < from || ins.seq >= to)
                            continue;
                        for(auto & m : ins.memory)
                            if(m.address < address + size && m.address + m.size > address)
                                out.push_back({ ins.seq, m });
                    }
                }
            std::stable_sort(out.begin(), out.end(), [](const std::pair<uint64_t, MemoryAccess> & a, const std::pair<uint64_t, MemoryAccess> & b) { return a.first < b.first; });
            return out;
        }

        template<typename Predicate>
        bool search(uint64_t seq, bool forward, uint32_t thread, Predicate predicate, uint64_t & result)
        {
            bool found = false;
            uint64_t best = 0;
            for(auto & it : mThreadChunks)
            {
                if(thread && it.first != thread)
                    continue;
                auto & chunks = it.second;
                if(forward)
                {
                    for(auto & c : chunks)
                    {
                        if(c.lastSeq <= seq || (found && c.firstSeq > best))
                            continue;
                        auto d = decode(c);
                        if(!d)
                            continue;
                        for(auto & ins : d->instructions)
                            if(ins.seq > seq && (!found || ins.seq < best) && predicate(ins))
                            {
                                found = true;
                                best = ins.seq;
                                break;
                            }
                    }
                }
                else
                {
                    for(auto c = chunks.rbegin(); c != chunks.rend(); ++c)
                    {
                        if(c->firstSeq >= seq || (found && c->lastSeq < best))
                            continue;
                        auto d = decode(*c);
                        if(!d)
                            continue;
                        for(auto ins = d->instructions.rbegin(); ins != d->instructions.rend(); ++ins)
                            if(ins->seq < seq && (!found || ins->seq > best) && predicate(*ins))
                            {
                                found = true;
                                best = ins->seq;
                                break;
                            }
                    }
                }
            }
            result = best;
            return found;
        }

        static const IndexEntry* findChunk(const std::vector<IndexEntry> & chunks, uint64_t seq)
        {
            auto pos = std::upper_bound(chunks.begin(), chunks.end(), seq, [](uint64_t s, const IndexEntry & e) { return s < e.firstSeq; });
            if(pos == chunks.begin())
                return nullptr;
            --pos;
            return seq <= pos->lastSeq ? &*pos : nullptr;
        }

        bool readSamples(const IndexEntry & e)
        {
            ChunkHeader h;
            std::vector<uint8_t> raw;
            if(!readChunk(e.offset, h, raw) || h.kind != ChunkCpuSamples)
                return false;
            ByteReader r(raw.data(), raw.size());
            mSamples.clear();
            for(uint32_t i = 0; i < h.count; i++)
            {
                CpuSample sample;
                uint64_t n;
                uint64_t length;
                if(!r.raw(sample.seq) || !r.varint(length) || length > sample.seq || !r.varint(sample.interval) || !r.varint(n) || n > 0x100000)
                    return false;
                sample.from = sample.seq - length;
                for(uint64_t j = 0; j < n; j++)
                {
                    CpuSampleThread thread;
                    uint8_t flags;
                    if(!r.raw(thread.thread) || !r.varint(thread.cycles) || !r.raw(flags))
                        return false;
                    thread.userCode = (flags & 1) != 0;
                    sample.threads.push_back(thread);
                }
                mSamples.push_back(std::move(sample));
            }
            return true;
        }

        bool readChunk(uint64_t offset, ChunkHeader & h, std::vector<uint8_t> & raw)
        {
            if(_fseeki64(mFile, (int64_t)offset, SEEK_SET) != 0 || fread(&h, sizeof(h), 1, mFile) != 1 || memcmp(h.magic, ChunkMagic, 4) != 0)
                return false;
            std::vector<char> stored(h.storedSize);
            if(h.storedSize && fread(stored.data(), 1, h.storedSize, mFile) != h.storedSize)
                return false;
            raw.resize(h.rawSize);
            if(!h.compressed)
            {
                if(h.storedSize != h.rawSize)
                    return false;
                if(h.rawSize)
                    memcpy(raw.data(), stored.data(), h.rawSize);
                return true;
            }
            return LZ4_decompress_safe(stored.data(), (char*)raw.data(), (int)h.storedSize, (int)h.rawSize) == (int)h.rawSize;
        }

        std::shared_ptr<Decoded> decode(const IndexEntry & e)
        {
            auto it = mCache.find(e.offset);
            if(it != mCache.end())
                return it->second;
            auto d = decodeUncached(e);
            if(!d)
                return nullptr;
            if(mCache.size() >= 64)
                mCache.erase(mCache.begin());
            mCache[e.offset] = d;
            return d;
        }

        std::shared_ptr<Decoded> decodeUncached(const IndexEntry & e)
        {
            ChunkHeader h;
            std::vector<uint8_t> raw;
            if(!readChunk(e.offset, h, raw))
                return nullptr;
            const auto & catalog = Registers(pointerSize());
            auto d = std::make_shared<Decoded>();
            ByteReader r(raw.data(), raw.size());
            uint16_t nkey = 0;
            if(!r.raw(nkey))
                return nullptr;
            for(uint16_t i = 0; i < nkey; i++)
            {
                uint8_t id;
                uint8_t value[16] = {};
                if(!r.raw(id) || id >= catalog.size() || !r.bytes(value, catalog[id].size))
                    return nullptr;
                d->keyframe.set(id, value, catalog[id].size);
            }
            uint64_t seq = h.firstSeq;
            uint64_t local = h.firstLocal;
            bool pendingSwitch = false;
            uint32_t from = 0;
            uint64_t timestamp = 0;
            while(!r.done())
            {
                uint8_t tag;
                if(!r.raw(tag))
                    return nullptr;
                if(tag == EventSwitch)
                {
                    if(!r.raw(from) || !r.raw(timestamp))
                        return nullptr;
                    pendingSwitch = true;
                    continue;
                }
                if(tag != EventInstruction)
                    return nullptr;
                Instruction ins;
                uint64_t delta;
                if(!r.varint(delta) || !r.raw(ins.opcodeSize) || ins.opcodeSize > 16 || !r.bytes(ins.opcode, ins.opcodeSize))
                    return nullptr;
                seq += delta;
                ins.seq = seq;
                ins.local = local++;
                ins.thread = h.thread;
                ins.switched = pendingSwitch;
                ins.fromThread = from;
                ins.switchTimestamp = timestamp;
                pendingSwitch = false;
                uint8_t nchanged;
                if(!r.raw(nchanged))
                    return nullptr;
                for(uint8_t i = 0; i < nchanged; i++)
                {
                    uint8_t id;
                    if(!r.raw(id) || id >= catalog.size())
                        return nullptr;
                    std::vector<uint8_t> value(catalog[id].size);
                    if(!r.bytes(value.data(), value.size()))
                        return nullptr;
                    ins.changes.push_back({ id, std::move(value) });
                }
                uint8_t nmem;
                if(!r.raw(nmem))
                    return nullptr;
                for(uint8_t i = 0; i < nmem; i++)
                {
                    MemoryAccess m;
                    if(!r.raw(m.flags) || !r.raw(m.address) || !r.raw(m.size) || m.size > 16)
                        return nullptr;
                    if(!(m.flags & MemoryOldOmitted) && !r.bytes(m.oldData, m.size))
                        return nullptr;
                    if((m.flags & MemoryWritten) && !r.bytes(m.newData, m.size))
                        return nullptr;
                    ins.memory.push_back(m);
                }
                d->seqs.push_back(ins.seq);
                d->instructions.push_back(std::move(ins));
            }
            // the instruction pointer is a register: take it from the deltas
            RegisterFile regs = d->keyframe;
            const unsigned ip = IpRegister(pointerSize());
            for(auto & ins : d->instructions)
            {
                applyChanges(regs, ins, catalog);
                ins.ip = regs.get(ip) & (pointerSize() == 8 ? ~0ull : 0xFFFFFFFFull);
            }
            return d;
        }

        std::shared_ptr<PageMap> snapshotPages(const IndexEntry & e)
        {
            auto it = mSnapshotCache.find(e.offset);
            if(it != mSnapshotCache.end())
                return it->second;
            ChunkHeader h;
            std::vector<uint8_t> raw;
            if(!readChunk(e.offset, h, raw))
                return nullptr;
            auto pages = std::make_shared<PageMap>();
            ByteReader r(raw.data(), raw.size());
            for(uint32_t i = 0; i < h.count; i++)
            {
                uint64_t base;
                SnapshotPage page;
                if(!r.raw(base) || !r.bytes(page.known, sizeof(page.known)) || !r.bytes(page.data, sizeof(page.data)))
                    break;
                (*pages)[base] = page;
            }
            if(mSnapshotCache.size() >= 16)
                mSnapshotCache.erase(mSnapshotCache.begin());
            mSnapshotCache[e.offset] = pages;
            return pages;
        }

        FILE* mFile = nullptr;
        FileHeader mHeader = {};
        std::string mMeta;
        uint64_t mCount = 0;
        std::map<uint32_t, std::vector<IndexEntry>> mThreadChunks;
        std::vector<IndexEntry> mSnapshots;
        std::vector<CpuSample> mSamples;
        std::vector<ThreadInfo> mThreads;
        std::map<uint64_t, std::shared_ptr<Decoded>> mCache;
        std::map<uint64_t, std::shared_ptr<PageMap>> mSnapshotCache;
    };
}
