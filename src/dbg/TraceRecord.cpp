#include "TraceRecord.h"
#include "debugger.h"
#include "module.h"
#include "memory.h"
#include "threading.h"
#include "thread.h"
#include "disasm_helper.h"
#include "disasm_fast.h"
#include "plugin_loader.h"
#include "stringformat.h"
#include "value.h"

#define MAX_INSTRUCTIONS_TRACED_FULL_REG_DUMP 512

TraceRecordManager TraceRecord;

TraceRecordManager::TraceRecordManager()
{
    ModuleNames.emplace_back("");
}

TraceRecordManager::~TraceRecordManager()
{
    clear();
}

void TraceRecordManager::clear()
{
    EXCLUSIVE_ACQUIRE(LockTraceRecord);
    for(auto i = TraceRecord.begin(); i != TraceRecord.end(); ++i)
        efree(i->second.rawPtr, "TraceRecordManager");
    TraceRecord.clear();
    ModuleNames.clear();
    ModuleNames.emplace_back("");
}

bool TraceRecordManager::setTraceRecordType(duint pageAddress, TraceRecordType type)
{
    EXCLUSIVE_ACQUIRE(LockTraceRecord);
    pageAddress &= ~((duint)4096 - 1);
    auto pageInfo = TraceRecord.find(ModHashFromAddr(pageAddress));
    if(pageInfo == TraceRecord.end())
    {
        if(type != TraceRecordType::TraceRecordNone)
        {
            TraceRecordPage newPage;
            char modName[MAX_MODULE_SIZE];
            switch(type)
            {
            case TraceRecordBitExec:
                newPage.rawPtr = emalloc(4096 / 8, "TraceRecordManager");
                memset(newPage.rawPtr, 0, 4096 / 8);
                break;
            case TraceRecordByteWithExecTypeAndCounter:
                newPage.rawPtr = emalloc(4096, "TraceRecordManager");
                memset(newPage.rawPtr, 0, 4096);
                break;
            case TraceRecordWordWithExecTypeAndCounter:
                newPage.rawPtr = emalloc(4096 * 2, "TraceRecordManager");
                memset(newPage.rawPtr, 0, 4096 * 2);
                break;
            default:
                return false;
            }
            newPage.dataType = type;
            if(ModNameFromAddr(pageAddress, modName, true))
            {
                newPage.rva = pageAddress - ModBaseFromAddr(pageAddress);
                newPage.moduleIndex = getModuleIndex(std::string(modName));
            }
            else
            {
                newPage.rva = pageAddress;
                newPage.moduleIndex = ~0;
            }

            auto inserted = TraceRecord.emplace(ModHashFromAddr(pageAddress), newPage);
            if(inserted.second == false) // we failed to insert new page into the map
            {
                efree(newPage.rawPtr);
                return false;
            }
            return true;
        }
        else
            return true;
    }
    else
    {
        if(type == TraceRecordType::TraceRecordNone)
        {
            if(pageInfo != TraceRecord.end())
            {
                efree(pageInfo->second.rawPtr, "TraceRecordManager");
                TraceRecord.erase(pageInfo);
            }
            return true;
        }
        else
            return pageInfo->second.dataType == type; //Can't covert between data types
    }
}

TraceRecordManager::TraceRecordType TraceRecordManager::getTraceRecordType(duint pageAddress)
{
    SHARED_ACQUIRE(LockTraceRecord);
    pageAddress &= ~((duint)4096 - 1);
    auto pageInfo = TraceRecord.find(ModHashFromAddr(pageAddress));
    if(pageInfo == TraceRecord.end())
        return TraceRecordNone;
    else
        return pageInfo->second.dataType;
}

void TraceRecordManager::TraceExecute(duint address, duint size)
{
    SHARED_ACQUIRE(LockTraceRecord);
    if(size == 0)
        return;
    duint base = address & ~((duint)4096 - 1);
    auto pageInfoIterator = TraceRecord.find(ModHashFromAddr(base));
    if(pageInfoIterator == TraceRecord.end())
        return;
    TraceRecordPage pageInfo;
    pageInfo = pageInfoIterator->second;
    duint offset = address - base;
    bool isMixed;
    if((offset + size) > 4096) // execution crossed page boundary, splitting into 2 sub calls. Noting that byte type may be mislabelled.
    {
        SHARED_RELEASE();
        TraceExecute(address, 4096 - offset);
        TraceExecute(base + 4096, size + offset - 4096);
        return;
    }
    isMixed = false;
    switch(pageInfo.dataType)
    {
    case TraceRecordType::TraceRecordBitExec:
        for(unsigned char i = 0; i < size; i++)
            *((char*)pageInfo.rawPtr + (i + offset) / 8) |= 1 << ((i + offset) % 8);
        break;

    case TraceRecordType::TraceRecordByteWithExecTypeAndCounter:
        for(unsigned char i = 0; i < size; i++)
        {
            TraceRecordByteType_2bit currentByteType;
            if(isMixed)
                currentByteType = TraceRecordByteType_2bit::_InstructionOverlapped;
            else if(i == 0)
                currentByteType = TraceRecordByteType_2bit::_InstructionHeading;
            else if(i == size - 1)
                currentByteType = TraceRecordByteType_2bit::_InstructionTailing;
            else
                currentByteType = TraceRecordByteType_2bit::_InstructionBody;

            char* data = (char*)pageInfo.rawPtr + offset + i;
            if(*data == 0)
            {
                *data = (char)currentByteType << 6 | 1;
            }
            else
            {
                isMixed |= (*data & 0xC0) >> 6 == currentByteType;
                *data = ((char)currentByteType << 6) | ((*data & 0x3F) == 0x3F ? 0x3F : (*data & 0x3F) + 1);
            }
        }
        if(isMixed)
            for(unsigned char i = 0; i < size; i++)
                *((char*)pageInfo.rawPtr + i + offset) |= 0xC0;
        break;

    case TraceRecordType::TraceRecordWordWithExecTypeAndCounter:
        for(unsigned char i = 0; i < size; i++)
        {
            TraceRecordByteType_2bit currentByteType;
            if(isMixed)
                currentByteType = TraceRecordByteType_2bit::_InstructionOverlapped;
            else if(i == 0)
                currentByteType = TraceRecordByteType_2bit::_InstructionHeading;
            else if(i == size - 1)
                currentByteType = TraceRecordByteType_2bit::_InstructionTailing;
            else
                currentByteType = TraceRecordByteType_2bit::_InstructionBody;

            short* data = (short*)pageInfo.rawPtr + offset + i;
            if(*data == 0)
            {
                *data = (char)currentByteType << 14 | 1;
            }
            else
            {
                isMixed |= (*data & 0xC0) >> 6 == currentByteType;
                *data = ((char)currentByteType << 14) | ((*data & 0x3FFF) == 0x3FFF ? 0x3FFF : (*data & 0x3FFF) + 1);
            }
        }
        if(isMixed)
            for(unsigned char i = 0; i < size; i++)
                *((short*)pageInfo.rawPtr + i + offset) |= 0xC000;
        break;

    default:
        break;
    }
}

//See https://www.felixcloutier.com/x86/FXSAVE.html, max 512 bytes
#define memoryContentSize 512

static void HandleZydisOperand(const Zydis & zydis, int opindex, DISASM_ARGTYPE* argType, duint* value, unsigned char memoryContent[memoryContentSize], unsigned char* memorySize)
{
    *value = (duint)zydis.ResolveOpValue(opindex, [&zydis](ZydisRegister reg) -> uint64_t
    {
        auto regName = zydis.RegName(reg);
        return regName ? getregister(nullptr, regName) : 0; //TODO: temporary needs enums + caching
    });
    const auto & op = zydis[opindex];
    switch(op.type)
    {
    case ZYDIS_OPERAND_TYPE_REGISTER:
        *argType = arg_normal;
        break;

    case ZYDIS_OPERAND_TYPE_IMMEDIATE:
        *argType = arg_normal;
        break;

    case ZYDIS_OPERAND_TYPE_POINTER:
        *argType = arg_normal;
        break;

    case ZYDIS_OPERAND_TYPE_MEMORY:
    {
        *argType = arg_memory;
        const auto & mem = op.mem;
        if(mem.segment == ArchValue(ZYDIS_REGISTER_FS, ZYDIS_REGISTER_GS))
        {
            *value += ThreadGetLocalBase(GetDebugData()->dwThreadId);
        }
        *memorySize = op.size / 8;
        if(op.size / 8 <= memoryContentSize && DbgMemIsValidReadPtr(*value))
        {
            MemRead(*value, memoryContent, std::max(op.size / 8, (int)sizeof(duint)));
        }
    }
    break;

    default:
        __debugbreak();
    }
}

void TraceRecordManager::TraceExecuteRecord(const Zydis & newInstruction, const unsigned char* bytes)
{
    if(!isTraceRecordingEnabled())
        return;
    unsigned char WriteBuffer[3072];
    unsigned char* WriteBufferPtr = WriteBuffer;
    //Get current data
    REGDUMPWORD newContext;
    //DISASM_INSTR newInstruction;
    DWORD newThreadId;
    const size_t memoryArrayCount = 32;
    duint newMemory[memoryArrayCount];
    duint newMemoryAddress[memoryArrayCount];
    duint oldMemory[memoryArrayCount];
    unsigned char newMemoryArrayCount = 0;
    DbgGetRegDumpEx((REGDUMP_AVX512*)&newContext.registers, sizeof(REGDUMP)); //TODO: Migrate
    newThreadId = GetDebugData()->dwThreadId;
    //The same thread reported again without executing anything (a breakpoint on the instruction it
    //stepped onto, see TraceSetThreads): keep its pending instruction. An empty instruction is the
    //flush of an exception, whose instruction may have changed nothing.
    //The flags (eflags and the FLAGS fields) may differ by the trap flag of the single step, and dr6
    //by the cause of the event.
    auto same = rtPending.find(newThreadId);
    if(newInstruction.Success() && same != rtPending.end())
    {
        const size_t eflagsWord = offsetof(REGDUMP, regcontext.eflags) / sizeof(duint);
        const size_t dr6Word = offsetof(REGDUMP, regcontext.dr6) / sizeof(duint);
        const size_t flagsFirst = offsetof(REGDUMP, flags) / sizeof(duint);
        const size_t flagsLast = (offsetof(REGDUMP, flags) + sizeof(FLAGS) - 1) / sizeof(duint);
        bool identical = true;
        for(size_t i = 0; i < _countof(newContext.regword) && identical; i++)
            identical = i == eflagsWord || i == dr6Word || (i >= flagsFirst && i <= flagsLast) || same->second.context.regword[i] == newContext.regword[i];
        if(identical)
            return;
    }
    // Don't try to resolve memory values for invalid/lea/nop instructions
    if(newInstruction.Success() && !newInstruction.IsNop() && newInstruction.GetId() != ZYDIS_MNEMONIC_LEA)
    {
        // This can happen when trace execute instruction is used, we need to fix that or something would go wrong with the GUI.
        newContext.registers.regcontext.cip = (duint)newInstruction.Address();
        DISASM_ARGTYPE argType;
        duint value;
        unsigned char memoryContent[memoryContentSize];
        unsigned char memorySize;
        for(int i = 0; i < newInstruction.TotalOpCount(); i++)
        {
            memset(memoryContent, 0, sizeof(memoryContent));
            HandleZydisOperand(newInstruction, i, &argType, &value, memoryContent, &memorySize);
            // check for overflow of the memory buffer
            if(newMemoryArrayCount * sizeof(duint) + memorySize > memoryArrayCount * sizeof(duint))
                continue;
            // TODO: Support memory value of ??? for invalid memory access
            if(argType == arg_memory)
            {
                if(memorySize <= sizeof(duint))
                {
                    memcpy(&newMemory[newMemoryArrayCount], memoryContent, sizeof(duint));
                    newMemoryAddress[newMemoryArrayCount] = value;
                    newMemoryArrayCount++;
                }
                else
                    for(unsigned char index = 0; index < memorySize / sizeof(duint) + ((memorySize % sizeof(duint)) > 0 ? 1 : 0); index++)
                    {
                        memcpy(&newMemory[newMemoryArrayCount], memoryContent + sizeof(duint) * index, sizeof(duint));
                        newMemoryAddress[newMemoryArrayCount] = value + sizeof(duint) * index;
                        newMemoryArrayCount++;
                    }
            }
        }
        if(newInstruction.GetId() == ZYDIS_MNEMONIC_PUSH || newInstruction.GetId() == ZYDIS_MNEMONIC_PUSHF || newInstruction.GetId() == ZYDIS_MNEMONIC_PUSHFD
                || newInstruction.GetId() == ZYDIS_MNEMONIC_PUSHFQ || newInstruction.GetId() == ZYDIS_MNEMONIC_CALL //TODO: far call accesses 2 stack entries
          )
        {
            // Discussion: https://github.com/zyantific/zydis/issues/510
            if(newMemoryArrayCount < memoryArrayCount)
            {
                auto stackAddress = newContext.registers.regcontext.csp - sizeof(duint);
                newMemory[newMemoryArrayCount] = 0;
                MemRead(stackAddress, &newMemory[newMemoryArrayCount], sizeof(duint));
                newMemoryAddress[newMemoryArrayCount] = stackAddress;
                newMemoryArrayCount++;
            }
        }
        //TODO: PUSHAD
        assert(newMemoryArrayCount <= memoryArrayCount);
    }
    //Each thread keeps its pending instruction: its block is written at the next event of the same
    //thread, when the instruction has executed (its memory is read after it). The blocks are therefore
    //in the order the instructions completed, whatever the other threads did in between.
    auto pending = rtPending.find(newThreadId);
    if(pending != rtPending.end())
    {
        const PendingInstruction & previous = pending->second;
        duint afterMemory[memoryArrayCount];
        for(unsigned char i = 0; i < previous.memoryCount; i++)
        {
            afterMemory[i] = previous.memory[i];
            MemRead(previous.memoryAddress[i], afterMemory + i, sizeof(duint));
        }
        //Skipped call (user block 0x83, see TraceSkippedCall): before the block of the call
        if(previous.skippedCall)
        {
            const uint32_t blockSize = 32;
            const uint64_t instruction = rtRecordedInstructions;
            const uint64_t callAddress = previous.context.registers.regcontext.cip;
            const uint64_t callDestination = previous.skippedDestination;
            const DWORD reserved = 0;
            WriteBufferPtr[0] = 0x83;
            memcpy(WriteBufferPtr + 1, &blockSize, sizeof(blockSize));
            WriteBufferPtr += 1 + sizeof(blockSize);
            memcpy(WriteBufferPtr, &instruction, sizeof(instruction));
            memcpy(WriteBufferPtr + 8, &callAddress, sizeof(callAddress));
            memcpy(WriteBufferPtr + 16, &callDestination, sizeof(callDestination));
            memcpy(WriteBufferPtr + 24, &newThreadId, sizeof(newThreadId));
            memcpy(WriteBufferPtr + 28, &reserved, sizeof(reserved));
            WriteBufferPtr += blockSize;
        }
        //Context switch marker (user block 0x81, skipped by readers that do not know it): this
        //instruction runs on another thread than the previous block. See docs/developers/tracefile.md
        if(rtWrittenThreadValid && newThreadId != rtWrittenThreadId)
        {
            LARGE_INTEGER counter, frequency;
            QueryPerformanceCounter(&counter);
            QueryPerformanceFrequency(&frequency);
            const uint32_t markerSize = 2 * sizeof(DWORD) + 3 * sizeof(uint64_t);
            const uint64_t instructionIndex = rtRecordedInstructions; // index of the next instruction
            WriteBufferPtr[0] = 0x81;
            memcpy(WriteBufferPtr + 1, &markerSize, sizeof(markerSize));
            WriteBufferPtr += 1 + sizeof(markerSize);
            memcpy(WriteBufferPtr, &rtWrittenThreadId, sizeof(DWORD));
            memcpy(WriteBufferPtr + 4, &newThreadId, sizeof(DWORD));
            memcpy(WriteBufferPtr + 8, &instructionIndex, sizeof(uint64_t));
            memcpy(WriteBufferPtr + 16, &counter.QuadPart, sizeof(uint64_t));
            memcpy(WriteBufferPtr + 24, &frequency.QuadPart, sizeof(uint64_t));
            WriteBufferPtr += markerSize;
        }
        //Delta compress registers
        //Data layout is Structure of Arrays to gather the same type of data in continuous memory to improve RLE compression performance.
        //1byte:block type,1byte:reg changed count,1byte:memory accessed count,1byte:flags,4byte/none:threadid,string:opcode,1byte[]:position,ptrbyte[]:regvalue,1byte[]:flags,ptrbyte[]:address,ptrbyte[]:oldmem,ptrbyte[]:newmem

        //A reader keeps the registers of the previous block: the block lists the registers that differ
        //from it (and those the instruction changes), all of them every 512 instructions
        const bool fullDump = rtFullDumpNext || rtRecordedInstructions % MAX_INSTRUCTIONS_TRACED_FULL_REG_DUMP == 0;
        auto regChanged = [&](size_t i)
        {
            return fullDump || previous.context.regword[i] != rtWrittenContext.regword[i] || previous.context.regword[i] != newContext.regword[i];
        };
        unsigned char changed = 0;
        for(size_t i = 0; i < _countof(previous.context.regword); i++)
        {
            if(regChanged(i))
                changed++;
        }
        const bool writeThreadId = fullDump || !rtWrittenThreadValid || newThreadId != rtWrittenThreadId;
        unsigned char blockFlags = writeThreadId ? 0x80 : 0;
        blockFlags |= previous.opcodeSize;

        unsigned char blockType = 0;
        WriteBufferPtr[0] = blockType; //1byte: block type
        WriteBufferPtr[1] = changed; //1byte: registers changed
        WriteBufferPtr[2] = previous.memoryCount; //1byte: memory accesses count
        WriteBufferPtr[3] = blockFlags; //1byte: flags and opcode size
        WriteBufferPtr += 4;
        if(writeThreadId)
        {
            memcpy(WriteBufferPtr, &newThreadId, sizeof(newThreadId));
            WriteBufferPtr += sizeof(newThreadId);
        }
        memcpy(WriteBufferPtr, previous.opcode, previous.opcodeSize);
        WriteBufferPtr += previous.opcodeSize;
        int lastChangedPosition = -1; //-1
        for(int i = 0; i < _countof(previous.context.regword); i++) //1byte: position
        {
            if(regChanged(i))
            {
                WriteBufferPtr[0] = (unsigned char)(i - lastChangedPosition - 1);
                WriteBufferPtr++;
                lastChangedPosition = i;
            }
        }
        for(size_t i = 0; i < _countof(previous.context.regword); i++) //ptrbyte: newvalue
        {
            if(regChanged(i))
            {
                memcpy(WriteBufferPtr, &previous.context.regword[i], sizeof(duint));
                WriteBufferPtr += sizeof(duint);
            }
        }
        for(unsigned char i = 0; i < previous.memoryCount; i++) //1byte: flags
        {
            unsigned char memoryOperandFlags = 0;
            if(previous.memory[i] == afterMemory[i]) //bit 0: memory is unchanged, no new memory is saved
                memoryOperandFlags |= 1;
            //proposed flags: is memory valid, is memory zero
            WriteBufferPtr[0] = memoryOperandFlags;
            WriteBufferPtr += 1;
        }
        for(unsigned char i = 0; i < previous.memoryCount; i++) //ptrbyte: address
        {
            memcpy(WriteBufferPtr, &previous.memoryAddress[i], sizeof(duint));
            WriteBufferPtr += sizeof(duint);
        }
        for(unsigned char i = 0; i < previous.memoryCount; i++) //ptrbyte: old content
        {
            memcpy(WriteBufferPtr, &previous.memory[i], sizeof(duint));
            WriteBufferPtr += sizeof(duint);
        }
        for(unsigned char i = 0; i < previous.memoryCount; i++) //ptrbyte: new content
        {
            if(previous.memory[i] != afterMemory[i])
            {
                memcpy(WriteBufferPtr, &afterMemory[i], sizeof(duint));
                WriteBufferPtr += sizeof(duint);
            }
        }
        rtWrittenContext = previous.context;
        rtWrittenThreadId = newThreadId;
        rtWrittenThreadValid = true;
        rtFullDumpNext = false;
        rtRecordedInstructions++;
    }
    //This thread's new pending instruction
    PendingInstruction & next = rtPending[newThreadId];
    next.context = newContext;
    next.memoryCount = newMemoryArrayCount;
    memcpy(next.memory, newMemory, sizeof(duint) * newMemoryArrayCount);
    memcpy(next.memoryAddress, newMemoryAddress, sizeof(duint) * newMemoryArrayCount);
    memset(next.opcode, 0, sizeof(next.opcode));
    next.opcodeSize = newInstruction.Size() & 0x0F;
    if(bytes && newInstruction.Success())
        memcpy(next.opcode, bytes, next.opcodeSize);
    else
        MemRead(newContext.registers.regcontext.cip, next.opcode, next.opcodeSize);
    next.skippedCall = false;
    //Write to file
    if(WriteBufferPtr - WriteBuffer <= sizeof(WriteBuffer))
    {
        rtWriteBuffer.insert(rtWriteBuffer.end(), WriteBuffer, WriteBufferPtr);
        // The trace browser reads the file while tracing: flush at least every 250 ms
        auto now = GetTickCount64();
        if(rtWriteBuffer.size() >= 256 * 1024 || now - rtLastFlush >= 250)
            FlushTraceFile();
    }
    else
        __debugbreak(); // Buffer overrun?
    //CPU time sample before the next instruction: when the recording (re)starts, when the traced
    //thread changes and every 4096 instructions. When every thread is traced (TraceSetThreads all),
    //the threads change all the time and the order is the one of their instructions: only the periodic
    //samples are written.
    const bool resync = rtResync;
    const bool threadChanged = rtEventThreadValid && newThreadId != rtEventThreadId && !dbggettracethreads();
    if(resync || threadChanged || rtRecordedInstructions - rtLastSample >= 4096)
        writeCpuSample(newThreadId, resync);
    rtResync = false;
    rtEventThreadId = newThreadId;
    rtEventThreadValid = true;

    dbgtracebrowserneedsupdate();
}

void TraceRecordManager::writeCpuSample(DWORD nextThreadId, bool resync)
{
    //User block 0x82, see docs/developers/tracefile.md. The debuggee is frozen: the cycle times of
    //its threads are those right before the next instruction.
    rtLastSample = rtRecordedInstructions;
    ThreadSampleCpu(rtCpuSamples);
    LARGE_INTEGER counter, frequency;
    QueryPerformanceCounter(&counter);
    QueryPerformanceFrequency(&frequency);
    const uint64_t nextInstruction = rtRecordedInstructions;
    const DWORD flags = resync ? 1 : 0; //bit 0: the recording (re)started, the previous sample does not apply
    const DWORD count = (DWORD)rtCpuSamples.size();
    const DWORD reserved = 0;
    const uint32_t blockSize = 40 + 24 * count;
    size_t position = rtWriteBuffer.size();
    rtWriteBuffer.resize(position + 1 + sizeof(blockSize) + blockSize);
    unsigned char* ptr = rtWriteBuffer.data() + position;
    auto put = [&ptr](const void* data, size_t size)
    {
        memcpy(ptr, data, size);
        ptr += size;
    };
    *ptr++ = 0x82;
    put(&blockSize, sizeof(blockSize));
    put(&nextInstruction, sizeof(nextInstruction));
    put(&counter.QuadPart, sizeof(counter.QuadPart));
    put(&frequency.QuadPart, sizeof(frequency.QuadPart));
    put(&nextThreadId, sizeof(nextThreadId));
    put(&flags, sizeof(flags));
    put(&count, sizeof(count));
    put(&reserved, sizeof(reserved));
    for(auto & thread : rtCpuSamples)
    {
        const DWORD threadFlags = thread.HasContext ? 1 : 0; //bit 0: ContextHash is valid
        put(&thread.ThreadId, sizeof(thread.ThreadId));
        put(&threadFlags, sizeof(threadFlags));
        put(&thread.CycleTime, sizeof(thread.CycleTime));
        put(&thread.ContextHash, sizeof(thread.ContextHash));
    }
}

void TraceRecordManager::FlushTraceFile()
{
    rtLastFlush = GetTickCount64();
    if(!rtEnabled || rtWriteBuffer.empty())
        return;
    DWORD written = 0;
    WriteFile(rtFile, rtWriteBuffer.data(), (DWORD)rtWriteBuffer.size(), &written, NULL);
    if(written < DWORD(rtWriteBuffer.size())) //Disk full?
    {
        CloseHandle(rtFile);
        String error = stringformatinline(StringUtils::sprintf("{winerror@%x}", GetLastError()));
        dprintf(QT_TRANSLATE_NOOP("DBG", "Trace recording has stopped unexpectedly because WriteFile() failed. GetLastError() = %s.\n"), error.c_str());
        rtEnabled = false;
    }
    rtWriteBuffer.clear();
}

void TraceRecordManager::TraceSkippedCall(duint address, duint destination)
{
    //User block 0x83 (see docs/developers/tracefile.md), written before the block of the call
    if(!isTraceRecordingEnabled())
        return;
    auto pending = rtPending.find(GetDebugData()->dwThreadId);
    if(pending != rtPending.end() && pending->second.context.registers.regcontext.cip == address)
    {
        pending->second.skippedCall = true;
        pending->second.skippedDestination = destination;
    }
}

void TraceRecordManager::FlushTraceExecuteRecord()
{
    if(!rtEnabled || rtPending.find(GetDebugData()->dwThreadId) == rtPending.end())
        return;

    // Trace records are written one instruction late, because the writer needs
    // the next debug context to compute register and memory changes. When an
    // exception stops execution there may be no next instruction, so finalize
    // the pending instruction with the current exception context.
    Zydis nextInstruction;
    TraceExecuteRecord(nextInstruction);
    //The pending instructions of the other threads are not known to complete
    rtPending.clear();
    rtResync = true;
    rtFullDumpNext = true;
}

unsigned int TraceRecordManager::getHitCount(duint address)
{
    SHARED_ACQUIRE(LockTraceRecord);
    duint base = address & ~((duint)4096 - 1);
    auto pageInfoIterator = TraceRecord.find(ModHashFromAddr(base));
    if(pageInfoIterator == TraceRecord.end())
        return 0;
    else
    {
        TraceRecordPage pageInfo = pageInfoIterator->second;
        duint offset = address - base;
        switch(pageInfo.dataType)
        {
        case TraceRecordType::TraceRecordBitExec:
            return ((char*)pageInfo.rawPtr)[offset / 8] & (1 << (offset % 8)) ? 1 : 0;
        case TraceRecordType::TraceRecordByteWithExecTypeAndCounter:
            return ((char*)pageInfo.rawPtr)[offset] & 0x3F;
        case TraceRecordType::TraceRecordWordWithExecTypeAndCounter:
            return ((short*)pageInfo.rawPtr)[offset] & 0x3FFF;
        default:
            return 0;
        }
    }
}

TraceRecordManager::TraceRecordByteType TraceRecordManager::getByteType(duint address)
{
    SHARED_ACQUIRE(LockTraceRecord);
    duint base = address & ~((duint)4096 - 1);
    auto pageInfoIterator = TraceRecord.find(ModHashFromAddr(base));
    if(pageInfoIterator == TraceRecord.end())
        return TraceRecordByteType::Unknown;
    else
    {
        TraceRecordPage pageInfo = pageInfoIterator->second;
        duint offset = address - base;
        switch(pageInfo.dataType)
        {
        case TraceRecordType::TraceRecordBitExec:
        default:
            // bit type don't store byte type
            return TraceRecordByteType::Unknown;
        case TraceRecordType::TraceRecordByteWithExecTypeAndCounter:
            if((((char*)pageInfo.rawPtr)[offset] & 0x3F) != 0)
                return (TraceRecordByteType)((((char*)pageInfo.rawPtr)[offset] & 0xC0) >> 6);
            else
                return TraceRecordByteType::Unknown;
        case TraceRecordType::TraceRecordWordWithExecTypeAndCounter:
            if((((short*)pageInfo.rawPtr)[offset] & 0x3FFF) != 0)
                return (TraceRecordByteType)((((short*)pageInfo.rawPtr)[offset] & 0xC000) >> 14);
            else
                return TraceRecordByteType::Unknown;
        }
    }
}

void TraceRecordManager::increaseInstructionCounter()
{
    InterlockedIncrement((volatile long*)&instructionCounter);
}

bool TraceRecordManager::enableTraceRecording(bool enabled, const char* fileName)
{
    if(enabled)
    {
        if(rtEnabled)
            enableTraceRecording(false, NULL); //re-enable run trace
        if(!DbgIsDebugging())
            return false;
        rtFile = CreateFileW(StringUtils::Utf8ToUtf16(fileName).c_str(), FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_DELETE, NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        if(rtFile != INVALID_HANDLE_VALUE)
        {
            LARGE_INTEGER size;
            if(GetFileSizeEx(rtFile, &size))
            {
                if(size.QuadPart != 0)
                {
                    SetFilePointer(rtFile, 0, 0, FILE_END);
                }
                else //file is empty, write some file header
                {
                    //TRAC, SIZE, JSON header
                    json_t* root = json_object();
                    json_object_set_new(root, "ver", json_integer(1));
                    json_object_set_new(root, "arch", json_string(ArchValue("x86", "x64")));
                    json_object_set_new(root, "hashAlgorithm", json_string("murmurhash"));
                    json_object_set_new(root, "hash", json_hex(dbgfunctionsget()->DbGetHash()));
                    json_object_set_new(root, "compression", json_string(""));
                    char path[MAX_PATH];
                    ModPathFromAddr(dbgdebuggedbase(), path, MAX_PATH);
                    json_object_set_new(root, "path", json_string(path));
                    char* headerinfo;
                    headerinfo = json_dumps(root, JSON_COMPACT);
                    size_t headerinfosize = strlen(headerinfo);
                    LARGE_INTEGER header;
                    DWORD written;
                    uint8_t TRAC[4] = { 'T', 'R', 'A', 'C' };
                    memcpy(&header.LowPart, TRAC, sizeof(TRAC));
                    header.HighPart = (LONG)headerinfosize;
                    WriteFile(rtFile, &header, 8, &written, nullptr);
                    if(written < 8) //read-only?
                    {
                        CloseHandle(rtFile);
                        json_free(headerinfo);
                        json_decref(root);
                        dputs(QT_TRANSLATE_NOOP("DBG", "Trace recording failed to start because the file header cannot be written."));
                        return false;
                    }
                    WriteFile(rtFile, headerinfo, (DWORD)headerinfosize, &written, nullptr);
                    json_free(headerinfo);
                    json_decref(root);
                    if(written < headerinfosize) //disk-full?
                    {
                        CloseHandle(rtFile);
                        dputs(QT_TRANSLATE_NOOP("DBG", "Trace recording failed to start because the file header cannot be written."));
                        return false;
                    }
                }
            }
            rtPending.clear();
            rtWrittenThreadValid = false;
            rtEventThreadValid = false;
            rtFullDumpNext = true;
            rtResync = true;
            rtEnabled = true;
            //Every record reads the AVX registers: one system call per instruction for the whole context
            SetEngineVariable(UE_ENGINE_CONTEXT_CACHE_AVX, true);
            rtRecordedInstructions = 0;
            rtLastSample = 0;
            dprintf(QT_TRANSLATE_NOOP("DBG", "Started trace recording to file: %s\n"), fileName);
            PLUG_CB_STARTTRACE startTraceInfo{};
            startTraceInfo.traceFilePath = fileName;
            plugincbcall(CB_STARTTRACE, &startTraceInfo);
            Zydis zydis;
            unsigned char instr[MAX_DISASM_BUFFER];
            auto cip = GetContextDataEx(hActiveThread, UE_CIP);
            if(MemRead(cip, instr, MAX_DISASM_BUFFER))
            {
                zydis.DisassembleSafe(cip, instr, MAX_DISASM_BUFFER);
                TraceExecuteRecord(zydis);
            }
            GuiOpenTraceFile(fileName);
            return true;
        }
        else
        {
            String error = stringformatinline(StringUtils::sprintf("{winerror@%x}", GetLastError()));
            dprintf(QT_TRANSLATE_NOOP("DBG", "Cannot create trace recording file. GetLastError() = %s.\n"), error.c_str());
            return false;
        }
    }
    else
    {
        if(rtEnabled)
        {
            //The last interval ends here
            if(rtEventThreadValid)
                writeCpuSample(rtEventThreadId, false);
            FlushTraceFile();
            CloseHandle(rtFile);
            rtPending.clear();
            rtEnabled = false;
            SetEngineVariable(UE_ENGINE_CONTEXT_CACHE_AVX, false);
            dputs(QT_TRANSLATE_NOOP("DBG", "Trace recording stopped."));
            PLUG_CB_STOPTRACE stopTraceInfo{};
            stopTraceInfo.reserved = nullptr;
            plugincbcall(CB_STOPTRACE, &stopTraceInfo);
        }
        return true;
    }
}

void TraceRecordManager::saveToDb(JSON root)
{
    EXCLUSIVE_ACQUIRE(LockTraceRecord);
    const JSON jsonTraceRecords = json_array();
    const char* byteToHex = "0123456789ABCDEF";
    for(auto i : TraceRecord)
    {
        JSON jsonObj = json_object();
        if(i.second.moduleIndex != ~0)
        {
            json_object_set_new(jsonObj, "module", json_string(ModuleNames[i.second.moduleIndex].c_str()));
            json_object_set_new(jsonObj, "rva", json_hex(i.second.rva));
        }
        else
        {
            json_object_set_new(jsonObj, "module", json_string(""));
            json_object_set_new(jsonObj, "rva", json_hex(i.first));
        }
        json_object_set_new(jsonObj, "type", json_hex((duint)i.second.dataType));
        auto ptr = (unsigned char*)i.second.rawPtr;
        duint size = 0;
        switch(i.second.dataType)
        {
        case TraceRecordType::TraceRecordBitExec:
            size = 4096 / 8;
            break;
        case TraceRecordType::TraceRecordByteWithExecTypeAndCounter:
            size = 4096;
            break;
        case TraceRecordType::TraceRecordWordWithExecTypeAndCounter:
            size = 4096 * 2;
            break;
        default:
            __debugbreak(); // We have encountered an error condition.
        }
        auto hex = StringUtils::ToCompressedHex(ptr, size);
        json_object_set_new(jsonObj, "data", json_string(hex.c_str()));
        json_array_append_new(jsonTraceRecords, jsonObj);
    }
    if(json_array_size(jsonTraceRecords))
        json_object_set(root, "tracerecord", jsonTraceRecords);

    // Notify garbage collector
    json_decref(jsonTraceRecords);
}

void TraceRecordManager::loadFromDb(JSON root)
{
    EXCLUSIVE_ACQUIRE(LockTraceRecord);
    // get the root object
    const JSON tracerecord = json_object_get(root, "tracerecord");

    // return if nothing found
    if(!tracerecord)
        return;

    size_t i;
    JSON value;
    json_array_foreach(tracerecord, i, value)
    {
        TraceRecordPage currentPage;
        size_t size;
        currentPage.dataType = (TraceRecordType)json_hex_value(json_object_get(value, "type"));
        currentPage.rva = (duint)json_hex_value(json_object_get(value, "rva"));
        switch(currentPage.dataType)
        {
        case TraceRecordType::TraceRecordBitExec:
            size = 4096 / 8;
            break;
        case TraceRecordType::TraceRecordByteWithExecTypeAndCounter:
            size = 4096;
            break;
        case TraceRecordType::TraceRecordWordWithExecTypeAndCounter:
            size = 4096 * 2;
            break;
        default:
            size = 0;
            break;
        }
        if(size != 0)
        {
            currentPage.rawPtr = emalloc(size, "TraceRecordManager");
            const char* p = json_string_value(json_object_get(value, "data"));
            std::vector<unsigned char> data;
            if(StringUtils::FromCompressedHex(p, data) && data.size() == size)
            {
                memcpy(currentPage.rawPtr, data.data(), size);
                const char* moduleName = json_string_value(json_object_get(value, "module"));
                duint key;
                if(*moduleName)
                {
                    currentPage.moduleIndex = getModuleIndex(std::string(moduleName));
                    key = currentPage.rva + ModHashFromName(moduleName);
                }
                else
                {
                    currentPage.moduleIndex = ~0;
                    key = currentPage.rva;
                }
                TraceRecord.emplace(key, currentPage);
            }
            else
                efree(currentPage.rawPtr, "TraceRecordManager");
        }
    }
}

unsigned int TraceRecordManager::getModuleIndex(const String & moduleName)
{
    auto iterator = std::find(ModuleNames.begin(), ModuleNames.end(), moduleName);
    if(iterator != ModuleNames.end())
        return (unsigned int)(iterator - ModuleNames.begin());
    else
    {
        ModuleNames.push_back(moduleName);
        return (unsigned int)(ModuleNames.size() - 1);
    }
}

bool TraceRecordManager::isTraceRecordingEnabled()
{
    return rtEnabled;
}

void dbgtraceexecute(duint CIP)
{
    if(TraceRecord.getTraceRecordType(CIP) != TraceRecordManager::TraceRecordType::TraceRecordNone)
    {
        Zydis instruction;
        unsigned char data[MAX_DISASM_BUFFER];
        if(MemRead(CIP, data, MAX_DISASM_BUFFER))
        {
            instruction.DisassembleSafe(CIP, data, MAX_DISASM_BUFFER);
            if(TraceRecord.isTraceRecordingEnabled())
            {
                TraceRecord.TraceExecute(CIP, instruction.Size());
                TraceRecord.TraceExecuteRecord(instruction, data);
            }
            else
            {
                TraceRecord.TraceExecute(CIP, instruction.Size());
            }
        }
    }
    else
    {
        if(TraceRecord.isTraceRecordingEnabled())
        {
            Zydis instruction;
            unsigned char data[MAX_DISASM_BUFFER];
            if(MemRead(CIP, data, MAX_DISASM_BUFFER))
            {
                instruction.DisassembleSafe(CIP, data, MAX_DISASM_BUFFER);
                TraceRecord.TraceExecuteRecord(instruction, data);
            }
        }
    }
    TraceRecord.increaseInstructionCounter();
}
