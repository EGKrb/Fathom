#ifndef TRACERECORD_H
#define TRACERECORD_H
#include "_global.h"
#include "_dbgfunctions.h"
#include "debugger.h"
#include "thread.h"
#include "jansson/jansson_x64dbg.h"
#include <zydis_wrapper.h>

class TraceRecordManager
{
public:
    enum TraceRecordByteType
    {
        InstructionBody = 0,
        InstructionHeading = 1,
        InstructionTailing = 2,
        InstructionOverlapped = 3, // The byte was executed with differing instruction base addresses
        Unknown = 4,
        DataByte,  // This and the following is not implemented yet.
        DataWord,
        DataDWord,
        DataQWord,
        DataFloat,
        DataDouble,
        DataLongDouble,
        DataXMM,
        DataYMM,
        DataMMX,
        DataMixed, //the byte is accessed in multiple ways
        InstructionDataMixed //the byte is both executed and written
    };

    /***************************************************************
     * Trace record data layout
     * TraceRecordNonoe: disable trace record
     * TraceRecordBitExec: single-bit, executed.
     * TraceRecordByteWithExecTypeAndCounter: 8-bit, YYXXXXXX YY:=TraceRecordByteType_2bit, XXXXXX:=Hit count(6bit)
     * TraceRecordWordWithExecTypeAndCounter: 16-bit, YYXXXXXX XXXXXXXX YY:=TraceRecordByteType_2bit, XX:=Hit count(14bit)
     * Other: reserved for future expanding
     **************************************************************/
    enum TraceRecordType
    {
        TraceRecordNone,
        TraceRecordBitExec,
        TraceRecordByteWithExecTypeAndCounter,
        TraceRecordWordWithExecTypeAndCounter
    };

    TraceRecordManager();
    ~TraceRecordManager();
    void clear();

    bool setTraceRecordType(duint pageAddress, TraceRecordType type);
    TraceRecordType getTraceRecordType(duint pageAddress);

    void TraceExecute(duint address, duint size);
    //void TraceAccess(duint address, unsigned char size, TraceRecordByteType accessType);
    // bytes: the instruction bytes when the caller already read them (saves a memory read)
    void TraceExecuteRecord(const Zydis & newInstruction, const unsigned char* bytes = nullptr);
    void FlushTraceExecuteRecord();
    // User block 0x83: the instruction just recorded is a call that runs untraced (TraceSetSkip)
    void TraceSkippedCall(duint address, duint destination);
    // Writes the buffered records to the trace file (called when the debugger pauses)
    void FlushTraceFile();

    unsigned int getHitCount(duint address);
    TraceRecordByteType getByteType(duint address);
    void increaseInstructionCounter();

    bool isTraceRecordingEnabled();
    bool enableTraceRecording(bool enabled, const char* fileName);

    void saveToDb(JSON root);
    void loadFromDb(JSON root);
private:
    // An enumerated value indicating this byte is either start of instruction, middle of instruction, end of instruction, or part of overlapped instruction.
    enum TraceRecordByteType_2bit
    {
        _InstructionBody = 0,
        _InstructionHeading = 1,
        _InstructionTailing = 2,
        _InstructionOverlapped = 3,
        _Unknown = 4 // Never recorded, unknown
    };

    struct TraceRecordPage
    {
        void* rawPtr;
        duint rva;
        TraceRecordType dataType;
        unsigned int moduleIndex;
    };

    typedef union _REGDUMPWORD
    {
        REGDUMP registers;
        // 172 qwords on x64, 216 dwords on x86. Almost no space left for AVX512
        // strip off lastStatus and 128 bytes of lastError.name member.
        duint regword[(FIELD_OFFSET(REGDUMP, lastError) + sizeof(DWORD)) / sizeof(duint)];
    } REGDUMPWORD;

    //Key := page base, value := trace record raw data
    std::unordered_map<duint, TraceRecordPage> TraceRecord;
    std::vector<std::string> ModuleNames;
    unsigned int getModuleIndex(const String & moduleName);
    unsigned int instructionCounter = 0;

    bool rtEnabled = false;
    HANDLE rtFile = nullptr;
    // Records are buffered: one WriteFile per instruction costs as much as the recording itself
    std::vector<unsigned char> rtWriteBuffer;
    ULONGLONG rtLastFlush = 0;
    // CPU time sample (user block 0x82): the cycles used by every thread, so that a reader knows
    // whether other threads ran outside the trace between two samples
    void writeCpuSample(DWORD nextThreadId, bool resync);
    unsigned int rtLastSample = 0;
    std::vector<THREADCPUSAMPLE> rtCpuSamples;

    //The instruction a thread is about to execute: its block is written at the next event of the
    //same thread, once it executed
    struct PendingInstruction
    {
        REGDUMPWORD context; //registers before the instruction
        duint memory[32]; //memory operands before the instruction
        duint memoryAddress[32];
        unsigned char memoryCount = 0;
        char opcode[16];
        unsigned char opcodeSize = 0;
        bool skippedCall = false; //TraceSkippedCall: the call runs untraced
        duint skippedDestination = 0;
    };
    std::unordered_map<DWORD, PendingInstruction> rtPending;
    REGDUMPWORD rtWrittenContext; //registers of the last written block: a reader's register state
    DWORD rtWrittenThreadId = 0;
    bool rtWrittenThreadValid = false;
    bool rtFullDumpNext = true; //the next block lists every register
    bool rtResync = true; //the recording (re)starts: the next CPU sample has the resync flag
    DWORD rtEventThreadId = 0; //thread of the previous event
    bool rtEventThreadValid = false;
    unsigned int rtRecordedInstructions; //written blocks: index of the next block
};

extern TraceRecordManager TraceRecord;
void dbgtraceexecute(duint CIP);

#endif // TRACERECORD_H
