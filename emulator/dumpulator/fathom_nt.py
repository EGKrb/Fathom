# Fathom additions to dumpulator: system calls it does not implement, so that whole programs run
# (the start-up and exit of the C runtime, DLLs loaded through \KnownDlls...). Boost Software
# License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# An implementation here replaces the one of ntsyscalls.py with the same name (the @syscall
# decorator registers by name); when it only adds cases, it calls the original for the others.

import os
import struct
from dataclasses import dataclass
from typing import Annotated

from .dumpulator import Dumpulator, syscall_functions
from .native import *
from .handles import *
from .memory import *
from .ntsyscalls import syscall

STATUS_NO_TOKEN = 0xC000007C
STATUS_IMAGE_NOT_AT_BASE = 0x40000003
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
STATUS_INVALID_INFO_CLASS = 0xC0000003
STATUS_OBJECT_TYPE_MISMATCH = 0xC0000024
STATUS_BUFFER_TOO_SMALL = 0xC0000023

_original = dict(syscall_functions)


def _object_name(attributes) -> str:
    """The ObjectName of an OBJECT_ATTRIBUTES pointer ("" without one)."""
    if attributes == 0:
        return ""
    attrs = attributes[0]
    if attrs.ObjectName == 0:
        return ""
    return attrs.ObjectName[0].read_str()


@dataclass
class ImageSectionObject(AbstractObject):
    """A section of an executable image (SEC_IMAGE): a DLL read from the host's disk."""
    path: str
    data: bytes

    def __str__(self):
        return self.pretty("path")


def _system_directory(dp: Dumpulator) -> str:
    windows = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.join(windows, "System32" if dp._x64 else "SysWOW64")


@syscall
def ZwOpenSection(dp: Dumpulator,
                  SectionHandle: Annotated[P[HANDLE], SAL("_Out_")],
                  DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                  ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")]
                  ):
    # The loader opens "\KnownDlls\name.dll" (or "\KnownDlls32\..." in a WoW64 process): the section
    # of a system DLL. It is the DLL of the host's system directory.
    name = _object_name(ObjectAttributes).replace("/", "\\").split("\\")[-1]
    path = os.path.join(_system_directory(dp), name)
    if not name.lower().endswith((".dll", ".drv")) or not os.path.isfile(path):
        return STATUS_OBJECT_NAME_NOT_FOUND
    with open(path, "rb") as f:
        data = f.read()
    SectionHandle.write_ptr(dp.handles.new(ImageSectionObject(path, data)))
    return STATUS_SUCCESS


@syscall
def ZwMapViewOfSection(dp: Dumpulator,
                       SectionHandle: Annotated[HANDLE, SAL("_In_")],
                       ProcessHandle: Annotated[HANDLE, SAL("_In_")],
                       BaseAddress: Annotated[P[PVOID], SAL("_Inout_ _At_(*BaseAddress, _Readable_bytes_(*ViewSize) _Writable_bytes_(*ViewSize) _Post_readable_byte_size_(*ViewSize))")],
                       ZeroBits: Annotated[ULONG_PTR, SAL("_In_")],
                       CommitSize: Annotated[SIZE_T, SAL("_In_")],
                       SectionOffset: Annotated[P[LARGE_INTEGER], SAL("_Inout_opt_")],
                       ViewSize: Annotated[P[SIZE_T], SAL("_Inout_")],
                       InheritDisposition: Annotated[SECTION_INHERIT, SAL("_In_")],
                       AllocationType: Annotated[ULONG, SAL("_In_")],
                       Win32Protect: Annotated[ULONG, SAL("_In_")]
                       ):
    section = dp.handles.get(SectionHandle, None)
    if isinstance(section, ImageSectionObject):
        assert ProcessHandle == dp.NtCurrentProcess()
        # mapped and relocated at a free address; the loader resolves the imports itself
        module = dp.map_module(section.data, section.path, BaseAddress.read_ptr(), False)
        BaseAddress.write_ptr(module.base)
        ViewSize.write_ptr(module.size)
        return STATUS_SUCCESS
    return _original["ZwMapViewOfSection"](dp, SectionHandle, ProcessHandle, BaseAddress, ZeroBits, CommitSize,
                                           SectionOffset, ViewSize, InheritDisposition, AllocationType, Win32Protect)


def _module_file(dp: Dumpulator, address: int):
    module = dp.modules.find(int(address) & dp.addr_mask)
    if module is None or not module.path:
        return None
    path = module.path
    for prefix in ("\\??\\", "\\\\?\\"):
        if path.startswith(prefix):
            path = path[len(prefix):]
    return os.path.normcase(os.path.normpath(path))


@syscall
def ZwAreMappedFilesTheSame(dp: Dumpulator,
                            File1MappedAsAnImage: Annotated[PVOID, SAL("_In_")],
                            File2MappedAsFile: Annotated[PVOID, SAL("_In_")]
                            ):
    # The loader maps a DLL it is asked for (LoadLibrary("ntdll.dll")) and asks whether a module already
    # loaded is the same file: it then uses that module
    first = _module_file(dp, File1MappedAsAnImage.ptr if hasattr(File1MappedAsAnImage, "ptr") else File1MappedAsAnImage)
    second = _module_file(dp, File2MappedAsFile.ptr if hasattr(File2MappedAsFile, "ptr") else File2MappedAsFile)
    if first is None or second is None:
        return STATUS_INVALID_ADDRESS
    return STATUS_SUCCESS if first == second else 0xC00000D4  # STATUS_NOT_SAME_DEVICE


@syscall
def ZwOpenThreadToken(dp: Dumpulator,
                      ThreadHandle: Annotated[HANDLE, SAL("_In_")],
                      DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                      OpenAsSelf: Annotated[BOOLEAN, SAL("_In_")],
                      TokenHandle: Annotated[P[HANDLE], SAL("_Out_")]
                      ):
    # The emulated threads never impersonate: they have no token of their own (what Windows answers
    # for such a thread; callers then open the process token)
    return STATUS_NO_TOKEN


@syscall
def ZwOpenThreadTokenEx(dp: Dumpulator,
                        ThreadHandle: Annotated[HANDLE, SAL("_In_")],
                        DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                        OpenAsSelf: Annotated[BOOLEAN, SAL("_In_")],
                        HandleAttributes: Annotated[ULONG, SAL("_In_")],
                        TokenHandle: Annotated[P[HANDLE], SAL("_Out_")]
                        ):
    return STATUS_NO_TOKEN


# ---------------------------------------------------------------------------------- threads, waits
# (the scheduler is dumpulator/threads.py)

from .threads import (Scheduler, ThreadObject2, MutantObject, SemaphoreObject, KeyedEventObject,
                      STATUS_TIMEOUT, STATUS_MUTANT_NOT_OWNED, STATUS_SEMAPHORE_LIMIT_EXCEEDED)


def _scheduler(dp: Dumpulator) -> Scheduler:
    return dp.scheduler


def _is_pseudo(dp: Dumpulator, handle: int, pseudo: int) -> bool:
    return handle in (pseudo, pseudo & dp.addr_mask)


def _object(dp: Dumpulator, handle):
    """The object of a handle (the current thread pseudo-handle included; None for the process)."""
    handle = int(handle)
    if _is_pseudo(dp, handle, dp.NtCurrentThread()):
        current = dp.scheduler.current
        if current.object is None:
            current.object = ThreadObject2(current)
        return current.object
    if _is_pseudo(dp, handle, dp.NtCurrentProcess()):
        return None
    try:
        return dp.handles.get(handle, None)
    except Exception:
        return None


def _write_long(pointer, value):
    if pointer != 0:
        pointer.write_ulong(value & 0xFFFFFFFF)


@syscall
def ZwQueryPerformanceCounter(dp: Dumpulator,
                              PerformanceCounter: Annotated[P[LARGE_INTEGER], SAL("_Out_")],
                              PerformanceFrequency: Annotated[P[LARGE_INTEGER], SAL("_Out_opt_")]
                              ):
    s = _scheduler(dp)
    dp.write(PerformanceCounter.ptr, struct.pack("<q", s.performance_counter()))
    if PerformanceFrequency != 0:
        dp.write(PerformanceFrequency.ptr, struct.pack("<q", s.qpc_frequency))
    return STATUS_SUCCESS


@syscall
def ZwQuerySystemTime(dp: Dumpulator,
                      SystemTime: Annotated[P[LARGE_INTEGER], SAL("_Out_")]
                      ):
    dp.write(SystemTime.ptr, struct.pack("<q", _scheduler(dp).system_time))
    return STATUS_SUCCESS


@syscall
def ZwDelayExecution(dp: Dumpulator,
                     Alertable: Annotated[BOOLEAN, SAL("_In_")],
                     DelayInterval: Annotated[P[LARGE_INTEGER], SAL("_In_opt_")]
                     ):
    return _scheduler(dp).delay(DelayInterval)


@syscall
def ZwYieldExecution(dp: Dumpulator
                     ):
    return _scheduler(dp).yield_execution()


@syscall
def ZwCreateEvent(dp: Dumpulator,
                  EventHandle: Annotated[P[HANDLE], SAL("_Out_")],
                  DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                  ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_opt_")],
                  EventType: Annotated[EVENT_TYPE, SAL("_In_")],
                  InitialState: Annotated[BOOLEAN, SAL("_In_")]
                  ):
    EventHandle.write_ptr(dp.handles.new(EventObject(EventType, InitialState != 0)))
    return STATUS_SUCCESS


def _signal_world(dp):
    # an object changed: the waiting threads of every emulated process check their waits (another
    # process may wait for it)
    world = getattr(dp, "world", None)
    if world is not None:
        world.signal_all()
    else:
        _scheduler(dp).signal()


def _event_set(dp, handle, previous, state, pulse=False):
    event = _object(dp, handle)
    if not isinstance(event, EventObject):
        return STATUS_INVALID_HANDLE
    _write_long(previous, 1 if event.signalled else 0)
    event.signalled = state or pulse
    if event.signalled:
        _signal_world(dp)
    if pulse:
        event.signalled = False
    return STATUS_SUCCESS


@syscall
def ZwSetEvent(dp: Dumpulator,
               EventHandle: Annotated[HANDLE, SAL("_In_")],
               PreviousState: Annotated[P[LONG], SAL("_Out_opt_")]
               ):
    return _event_set(dp, EventHandle, PreviousState, True)


@syscall
def ZwResetEvent(dp: Dumpulator,
                 EventHandle: Annotated[HANDLE, SAL("_In_")],
                 PreviousState: Annotated[P[LONG], SAL("_Out_opt_")]
                 ):
    return _event_set(dp, EventHandle, PreviousState, False)


@syscall
def ZwClearEvent(dp: Dumpulator,
                 EventHandle: Annotated[HANDLE, SAL("_In_")]
                 ):
    return _event_set(dp, EventHandle, 0, False)


@syscall
def ZwPulseEvent(dp: Dumpulator,
                 EventHandle: Annotated[HANDLE, SAL("_In_")],
                 PreviousState: Annotated[P[LONG], SAL("_Out_opt_")]
                 ):
    return _event_set(dp, EventHandle, PreviousState, False, pulse=True)


@syscall
def ZwCreateMutant(dp: Dumpulator,
                   MutantHandle: Annotated[P[HANDLE], SAL("_Out_")],
                   DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                   ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_opt_")],
                   InitialOwner: Annotated[BOOLEAN, SAL("_In_")]
                   ):
    mutant = MutantObject()
    if InitialOwner:
        mutant.owner = dp.thread_id
        mutant.count = 1
    MutantHandle.write_ptr(dp.handles.new(mutant))
    return STATUS_SUCCESS


def _release_mutant(dp, handle, previous):
    mutant = _object(dp, handle)
    if not isinstance(mutant, MutantObject):
        return STATUS_INVALID_HANDLE
    if mutant.owner != dp.thread_id:
        return STATUS_MUTANT_NOT_OWNED
    _write_long(previous, 1 - mutant.count)  # 1 when free, 0 or less when owned
    mutant.count -= 1
    if mutant.count == 0:
        mutant.owner = None
        _signal_world(dp)
    return STATUS_SUCCESS


@syscall
def ZwReleaseMutant(dp: Dumpulator,
                    MutantHandle: Annotated[HANDLE, SAL("_In_")],
                    PreviousCount: Annotated[P[LONG], SAL("_Out_opt_")]
                    ):
    return _release_mutant(dp, MutantHandle, PreviousCount)


@syscall
def ZwCreateSemaphore(dp: Dumpulator,
                      SemaphoreHandle: Annotated[P[HANDLE], SAL("_Out_")],
                      DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                      ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_opt_")],
                      InitialCount: Annotated[LONG, SAL("_In_")],
                      MaximumCount: Annotated[LONG, SAL("_In_")]
                      ):
    initial, maximum = int(InitialCount), int(MaximumCount)
    if maximum <= 0 or initial < 0 or initial > maximum:
        return STATUS_INVALID_PARAMETER
    SemaphoreHandle.write_ptr(dp.handles.new(SemaphoreObject(initial, maximum)))
    return STATUS_SUCCESS


def _release_semaphore(dp, handle, count, previous):
    semaphore = _object(dp, handle)
    if not isinstance(semaphore, SemaphoreObject):
        return STATUS_INVALID_HANDLE
    if count <= 0:
        return STATUS_INVALID_PARAMETER
    if semaphore.count + count > semaphore.maximum:
        return STATUS_SEMAPHORE_LIMIT_EXCEEDED
    _write_long(previous, semaphore.count)
    semaphore.count += count
    _signal_world(dp)
    return STATUS_SUCCESS


@syscall
def ZwReleaseSemaphore(dp: Dumpulator,
                       SemaphoreHandle: Annotated[HANDLE, SAL("_In_")],
                       ReleaseCount: Annotated[LONG, SAL("_In_")],
                       PreviousCount: Annotated[P[LONG], SAL("_Out_opt_")]
                       ):
    return _release_semaphore(dp, SemaphoreHandle, int(ReleaseCount), PreviousCount)


def _wait_single(dp, handle, timeout):
    obj = _object(dp, handle)
    if obj is None and not _is_pseudo(dp, int(handle), dp.NtCurrentProcess()):
        return STATUS_INVALID_HANDLE
    return _scheduler(dp).wait([obj] if obj is not None else [], False, timeout)


@syscall
def ZwWaitForSingleObject(dp: Dumpulator,
                          Handle: Annotated[HANDLE, SAL("_In_")],
                          Alertable: Annotated[BOOLEAN, SAL("_In_")],
                          Timeout: Annotated[P[LARGE_INTEGER], SAL("_In_opt_")]
                          ):
    return _wait_single(dp, Handle, Timeout)


@syscall
def ZwWaitForMultipleObjects(dp: Dumpulator,
                             Count: Annotated[ULONG, SAL("_In_")],
                             Handles: Annotated[P[HANDLE], SAL("_In_reads_(Count)")],
                             WaitType: Annotated[WAIT_TYPE, SAL("_In_")],
                             Alertable: Annotated[BOOLEAN, SAL("_In_")],
                             Timeout: Annotated[P[LARGE_INTEGER], SAL("_In_opt_")]
                             ):
    objects = []
    for i in range(int(Count)):
        obj = _object(dp, dp.read_ptr(Handles.ptr + i * dp.ptr_size()))
        if obj is None:
            return STATUS_INVALID_HANDLE
        objects.append(obj)
    return _scheduler(dp).wait(objects, WaitType == WAIT_TYPE.WaitAll, Timeout)


@syscall
def ZwSignalAndWaitForSingleObject(dp: Dumpulator,
                                   SignalHandle: Annotated[HANDLE, SAL("_In_")],
                                   WaitHandle: Annotated[HANDLE, SAL("_In_")],
                                   Alertable: Annotated[BOOLEAN, SAL("_In_")],
                                   Timeout: Annotated[P[LARGE_INTEGER], SAL("_In_opt_")]
                                   ):
    signal = _object(dp, SignalHandle)
    if isinstance(signal, EventObject):
        status = _event_set(dp, SignalHandle, 0, True)
    elif isinstance(signal, MutantObject):
        status = _release_mutant(dp, SignalHandle, 0)
    elif isinstance(signal, SemaphoreObject):
        status = _release_semaphore(dp, SignalHandle, 1, 0)
    else:
        return STATUS_OBJECT_TYPE_MISMATCH
    if status != STATUS_SUCCESS:
        return status
    return _wait_single(dp, WaitHandle, Timeout)


@syscall
def ZwWaitForAlertByThreadId(dp: Dumpulator,
                             Address: Annotated[PVOID, SAL("_In_")],
                             Timeout: Annotated[P[LARGE_INTEGER], SAL("_In_opt_")]
                             ):
    return _scheduler(dp).wait_alert(Timeout)


@syscall
def ZwAlertThreadByThreadId(dp: Dumpulator,
                            ThreadId: Annotated[HANDLE, SAL("_In_")]
                            ):
    return _scheduler(dp).alert(int(ThreadId))


@syscall
def ZwCreateKeyedEvent(dp: Dumpulator,
                       KeyedEventHandle: Annotated[P[HANDLE], SAL("_Out_")],
                       DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                       ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_opt_")],
                       Flags: Annotated[ULONG, SAL("_In_")]
                       ):
    KeyedEventHandle.write_ptr(dp.handles.new(KeyedEventObject()))
    return STATUS_SUCCESS


_global_keyed_event = KeyedEventObject()


def _keyed(dp, handle):
    obj = _object(dp, handle)
    if isinstance(obj, KeyedEventObject):
        return obj
    if int(handle) == 0:  # NULL: the system's keyed event (critical sections)
        return _global_keyed_event
    return None


@syscall
def ZwWaitForKeyedEvent(dp: Dumpulator,
                        KeyedEventHandle: Annotated[HANDLE, SAL("_In_")],
                        KeyValue: Annotated[PVOID, SAL("_In_")],
                        Alertable: Annotated[BOOLEAN, SAL("_In_")],
                        Timeout: Annotated[P[LARGE_INTEGER], SAL("_In_opt_")]
                        ):
    keyed = _keyed(dp, KeyedEventHandle)
    if keyed is None:
        return STATUS_INVALID_HANDLE
    return _scheduler(dp).keyed(keyed, KeyValue.ptr, "wait", Timeout)


@syscall
def ZwReleaseKeyedEvent(dp: Dumpulator,
                        KeyedEventHandle: Annotated[HANDLE, SAL("_In_")],
                        KeyValue: Annotated[PVOID, SAL("_In_")],
                        Alertable: Annotated[BOOLEAN, SAL("_In_")],
                        Timeout: Annotated[P[LARGE_INTEGER], SAL("_In_opt_")]
                        ):
    keyed = _keyed(dp, KeyedEventHandle)
    if keyed is None:
        return STATUS_INVALID_HANDLE
    return _scheduler(dp).keyed(keyed, KeyValue.ptr, "release", Timeout)


@syscall
def ZwCreateThreadEx(dp: Dumpulator,
                     ThreadHandle: Annotated[P[HANDLE], SAL("_Out_")],
                     DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                     ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_opt_")],
                     ProcessHandle: Annotated[HANDLE, SAL("_In_")],
                     StartRoutine: Annotated[PVOID, SAL("_In_", "PUSER_THREAD_START_ROUTINE")],
                     Argument: Annotated[PVOID, SAL("_In_opt_")],
                     CreateFlags: Annotated[ULONG, SAL("_In_", "THREAD_CREATE_FLAGS_*")],
                     ZeroBits: Annotated[SIZE_T, SAL("_In_")],
                     StackSize: Annotated[SIZE_T, SAL("_In_")],
                     MaximumStackSize: Annotated[SIZE_T, SAL("_In_")],
                     AttributeList: Annotated[P[PS_ATTRIBUTE_LIST], SAL("_In_opt_")]
                     ):
    if not _is_pseudo(dp, int(ProcessHandle), dp.NtCurrentProcess()):
        return STATUS_NOT_IMPLEMENTED  # a thread in another process
    s = _scheduler(dp)
    thread = s.create_thread(StartRoutine.ptr, Argument.ptr, bool(int(CreateFlags) & 1),
                             max(int(StackSize), int(MaximumStackSize)))
    handle = dp.handles.new(thread.object)
    ThreadHandle.write_ptr(handle)
    dp.info(f"thread {thread.tid} created: {hex(StartRoutine.ptr)}({hex(Argument.ptr)}), handle {hex(handle)}")
    # PS_ATTRIBUTE_LIST: TotalLength, then {Attribute, Size, Value / ValuePtr, ReturnLength} entries
    if AttributeList != 0:
        ptr = dp.ptr_size()
        total = dp.read_ptr(AttributeList.ptr)
        for i in range(max(0, (total - ptr) // (4 * ptr))):
            entry = AttributeList.ptr + ptr + i * 4 * ptr
            attribute = dp.read_ptr(entry) & 0xFFFFFFFF
            value = dp.read_ptr(entry + 2 * ptr)
            if attribute == 0x10003 and value:    # PsAttributeClientId
                dp.write_ptr(value, dp.process_id)
                dp.write_ptr(value + ptr, thread.tid)
            elif attribute == 0x10004 and value:  # PsAttributeTebAddress
                dp.write_ptr(value, thread.teb)
    return STATUS_SUCCESS


@syscall
def ZwResumeThread(dp: Dumpulator,
                   ThreadHandle: Annotated[HANDLE, SAL("_In_")],
                   PreviousSuspendCount: Annotated[P[ULONG], SAL("_Out_opt_")]
                   ):
    s = _scheduler(dp)
    thread = s.thread_of_handle(int(ThreadHandle))
    if thread is None:
        return STATUS_INVALID_HANDLE
    _write_long(PreviousSuspendCount, s.resume(thread))
    return STATUS_SUCCESS


@syscall
def ZwSuspendThread(dp: Dumpulator,
                    ThreadHandle: Annotated[HANDLE, SAL("_In_")],
                    PreviousSuspendCount: Annotated[P[ULONG], SAL("_Out_opt_")]
                    ):
    s = _scheduler(dp)
    thread = s.thread_of_handle(int(ThreadHandle))
    if thread is None:
        return STATUS_INVALID_HANDLE
    previous = thread.suspend_count
    _write_long(PreviousSuspendCount, previous)
    if thread is s.current:
        thread.suspend_count += 1
        return s.switch_from_syscall(lambda t: setattr(t, "state", "suspended"), STATUS_SUCCESS)
    s.suspend(thread)
    return STATUS_SUCCESS


@syscall
def ZwTerminateThread(dp: Dumpulator,
                      ThreadHandle: Annotated[HANDLE, SAL("_In_opt_")],
                      ExitStatus: Annotated[NTSTATUS, SAL("_In_")]
                      ):
    s = _scheduler(dp)
    thread = s.thread_of_handle(int(ThreadHandle))
    if thread is None:
        return STATUS_INVALID_HANDLE
    dp.info(f"thread {thread.tid} exits with code {hex(int(ExitStatus) & 0xFFFFFFFF)}")
    if thread is s.current:
        return s.terminate_current(int(ExitStatus))
    s.terminate_thread(thread, int(ExitStatus))
    return STATUS_SUCCESS


@syscall
def ZwTestAlert(dp: Dumpulator
                ):
    return STATUS_SUCCESS  # no user APC is ever queued in the emulator


@syscall
def ZwContinue(dp: Dumpulator,
               ContextRecord: Annotated[P[CONTEXT], SAL("_In_")],
               TestAlert: Annotated[BOOLEAN, SAL("_In_")]
               ):
    # dumpulator's, without refusing TestAlert (a new thread continues with it; no APC is queued)
    return _original["ZwContinue"](dp, ContextRecord, 0)


@syscall
def ZwTerminateProcess(dp: Dumpulator,
                       ProcessHandle: Annotated[HANDLE, SAL("_In_opt_")],
                       ExitStatus: Annotated[NTSTATUS, SAL("_In_")]
                       ):
    s = _scheduler(dp)
    if int(ProcessHandle) == 0:
        # ExitProcess first ends the other threads, then unloads the DLLs and ends the process
        for thread in list(s.threads.values()):
            if thread is not s.current and thread.state not in ("terminated", "frozen"):
                s.terminate_thread(thread, int(ExitStatus))
        return STATUS_SUCCESS
    return _original["ZwTerminateProcess"](dp, ProcessHandle, ExitStatus)


@syscall
def ZwQueryInformationThread(dp: Dumpulator,
                             ThreadHandle: Annotated[HANDLE, SAL("_In_")],
                             ThreadInformationClass: Annotated[ULONG, SAL("_In_")],
                             ThreadInformation: Annotated[PVOID, SAL("_Out_writes_bytes_(ThreadInformationLength)")],
                             ThreadInformationLength: Annotated[ULONG, SAL("_In_")],
                             ReturnLength: Annotated[P[ULONG], SAL("_Out_opt_")]
                             ):
    s = _scheduler(dp)
    thread = s.thread_of_handle(int(ThreadHandle))
    ptr = dp.ptr_size()

    def answer(data: bytes):
        if int(ThreadInformationLength) < len(data):
            return STATUS_INFO_LENGTH_MISMATCH
        dp.write(ThreadInformation.ptr, data)
        _write_long(ReturnLength, len(data))
        return STATUS_SUCCESS

    klass = int(ThreadInformationClass)
    if thread is not None and klass == THREADINFOCLASS.ThreadBasicInformation.value:
        exit_status = thread.exit_code if thread.state == "terminated" else 0x103  # STILL_ACTIVE
        if ptr == 8:
            data = struct.pack("<IIQQQQii", exit_status, 0, thread.teb, dp.process_id, thread.tid, 1, 8, 8)
        else:
            data = struct.pack("<IIIIIii", exit_status, thread.teb, dp.process_id, thread.tid, 1, 8, 8)
        return answer(data)
    if klass == THREADINFOCLASS.ThreadAmILastThread.value:
        others = [t for t in s.threads.values() if t is not s.current and t.state != "terminated"]
        return answer(struct.pack("<I", 0 if others else 1))
    if thread is not None and klass == THREADINFOCLASS.ThreadQuerySetWin32StartAddress.value:
        return answer(struct.pack("<Q" if ptr == 8 else "<I", thread.start))
    if thread is not None and klass == THREADINFOCLASS.ThreadIsTerminated.value:
        return answer(struct.pack("<I", 1 if thread.state == "terminated" else 0))
    try:
        klass = THREADINFOCLASS(klass)
    except ValueError:
        return STATUS_INVALID_INFO_CLASS
    return _original["ZwQueryInformationThread"](dp, ThreadHandle, klass, ThreadInformation,
                                                  ThreadInformationLength, ReturnLength)


@syscall
def ZwSetInformationThread(dp: Dumpulator,
                           ThreadHandle: Annotated[HANDLE, SAL("_In_")],
                           ThreadInformationClass: Annotated[ULONG, SAL("_In_")],
                           ThreadInformation: Annotated[PVOID, SAL("_In_reads_bytes_(ThreadInformationLength)")],
                           ThreadInformationLength: Annotated[ULONG, SAL("_In_")]
                           ):
    # hide from debugger, priority, affinity, name, ideal processor...: nothing to do with one
    # emulated processor and no debugger
    dp.info(f"NtSetInformationThread(class {int(ThreadInformationClass)}): no effect in the emulator")
    return STATUS_SUCCESS


# ---------------------------------------------------------------------------------- memory, images

STATUS_NOT_SUPPORTED = 0xC00000BB


def _module_at(dp: Dumpulator, address: int):
    for module in dp.modules:
        if module.base <= address < module.base + module.size:
            return module
    return None


@syscall
def ZwQueryVirtualMemory(dp: Dumpulator,
                         ProcessHandle: Annotated[HANDLE, SAL("_In_")],
                         BaseAddress: Annotated[PVOID, SAL("_In_opt_")],
                         MemoryInformationClass: Annotated[ULONG, SAL("_In_")],
                         MemoryInformation: Annotated[PVOID, SAL("_Out_writes_bytes_(MemoryInformationLength)")],
                         MemoryInformationLength: Annotated[SIZE_T, SAL("_In_")],
                         ReturnLength: Annotated[P[SIZE_T], SAL("_Out_opt_")]
                         ):
    klass = int(MemoryInformationClass)
    if klass == MEMORY_INFORMATION_CLASS.MemoryImageInformation.value:
        # MEMORY_IMAGE_INFORMATION: ImageBase, SizeOfImage, ImageFlags
        module = _module_at(dp, BaseAddress.ptr)
        if module is None:
            return STATUS_INVALID_ADDRESS
        ptr = dp.ptr_size()
        data = struct.pack("<QQII" if ptr == 8 else "<III", module.base, module.size, 0, *([0] if ptr == 8 else []))
        if int(MemoryInformationLength) < len(data):
            return STATUS_INFO_LENGTH_MISMATCH
        dp.write(MemoryInformation.ptr, data)
        if ReturnLength != 0:
            ReturnLength.write_ptr(len(data))
        return STATUS_SUCCESS
    if klass >= 14:
        # classes of recent Windows versions (14: image extensions, hot patches): none here, which
        # is what Windows answers for an ordinary image
        return STATUS_NOT_SUPPORTED
    return _original["ZwQueryVirtualMemory"](dp, ProcessHandle, BaseAddress, MEMORY_INFORMATION_CLASS(klass),
                                             MemoryInformation, MemoryInformationLength, ReturnLength)


def _unmap(dp: Dumpulator, address: int):
    module = _module_at(dp, address)
    if module is None:
        return STATUS_NOT_MAPPED_VIEW
    dp.memory.release(module.base)
    manager = dp.modules
    manager._modules.pop(module.base, None)
    for key in [k for k, v in manager._name_lookup.items() if v == module.base]:
        del manager._name_lookup[key]
    dp.info(f"unmapped {module.path}")
    return STATUS_SUCCESS


STATUS_NOT_MAPPED_VIEW = 0xC0000019
STATUS_INVALID_ADDRESS = 0xC0000141


@syscall
def ZwUnmapViewOfSection(dp: Dumpulator,
                         ProcessHandle: Annotated[HANDLE, SAL("_In_")],
                         BaseAddress: Annotated[PVOID, SAL("_In_opt_")]
                         ):
    return _unmap(dp, BaseAddress.ptr)


@syscall
def ZwUnmapViewOfSectionEx(dp: Dumpulator,
                           ProcessHandle: Annotated[HANDLE, SAL("_In_")],
                           BaseAddress: Annotated[PVOID, SAL("_In_opt_")],
                           Flags: Annotated[ULONG, SAL("_In_")]
                           ):
    return _unmap(dp, BaseAddress.ptr)


# ---------------------------------------------------------------------------------- recent system calls

@syscall
def ZwManageHotPatch(dp: Dumpulator,
                     HotPatchInformationClass: Annotated[ULONG, SAL("_In_")],
                     HotPatchInformation: Annotated[PVOID, SAL("_Out_writes_bytes_opt_(HotPatchInformationLength)")],
                     HotPatchInformationLength: Annotated[ULONG, SAL("_In_")],
                     ReturnLength: Annotated[P[ULONG], SAL("_Out_opt_")]
                     ):
    # no hot patches. The loader asks for the patches of each image it maps (class 8): an answer of
    # zeros (none) lets it go on, an error makes LoadLibrary fail.
    if HotPatchInformation.ptr and int(HotPatchInformationLength):
        dp.write(HotPatchInformation.ptr, bytes(int(HotPatchInformationLength)))
        if ReturnLength != 0:
            ReturnLength.write_ulong(int(HotPatchInformationLength))
        return STATUS_SUCCESS
    return STATUS_NOT_SUPPORTED


def _wnf_states(dp: Dumpulator) -> dict:
    """The states of the Windows notification facility the emulated code published: name -> (stamp, data)."""
    if not hasattr(dp, "_fathom_wnf"):
        dp._fathom_wnf = {}
    return dp._fathom_wnf


STATUS_UNSUCCESSFUL = 0xC0000001


@syscall
def ZwQueryWnfStateData(dp: Dumpulator,
                        StateName: Annotated[P[CWNF_STATE_NAME], SAL("_In_")],
                        TypeId: Annotated[P[CWNF_TYPE_ID], SAL("_In_opt_")],
                        ExplicitScope: Annotated[PVOID, SAL("_In_opt_")],
                        ChangeStamp: Annotated[P[WNF_CHANGE_STAMP], SAL("_Out_")],
                        Buffer: Annotated[PVOID, SAL("_Out_writes_bytes_to_opt_(*BufferSize, *BufferSize)")],
                        BufferSize: Annotated[P[ULONG], SAL("_Inout_")]
                        ):
    # a state nobody published has no data and the change stamp 0
    name = bytes(dp.read(StateName.ptr, 8))
    stamp, data = _wnf_states(dp).get(name, (0, b""))
    if ChangeStamp != 0:
        ChangeStamp.write_ulong(stamp)
    size = BufferSize.read_ulong()
    BufferSize.write_ulong(len(data))
    if len(data) > size:
        return STATUS_BUFFER_TOO_SMALL
    if data:
        dp.write(Buffer.ptr, data)
    return STATUS_SUCCESS


@syscall
def ZwUpdateWnfStateData(dp: Dumpulator,
                         StateName: Annotated[P[CWNF_STATE_NAME], SAL("_In_")],
                         Buffer: Annotated[PVOID, SAL("_In_reads_bytes_opt_(Length)")],
                         Length: Annotated[ULONG, SAL("_In_opt_")],
                         TypeId: Annotated[P[CWNF_TYPE_ID], SAL("_In_opt_")],
                         ExplicitScope: Annotated[PVOID, SAL("_In_opt_")],
                         MatchingChangeStamp: Annotated[WNF_CHANGE_STAMP, SAL("_In_")],
                         CheckStamp: Annotated[LOGICAL, SAL("_In_")]
                         ):
    states = _wnf_states(dp)
    name = bytes(dp.read(StateName.ptr, 8))
    stamp, _ = states.get(name, (0, b""))
    if CheckStamp and int(MatchingChangeStamp) != stamp:
        return STATUS_UNSUCCESSFUL
    data = bytes(dp.read(Buffer.ptr, int(Length))) if int(Length) and Buffer != 0 else b""
    states[name] = (stamp + 1, data)
    return STATUS_SUCCESS


# ---------------------------------------------------------------------------------- console

from . import ntdevices as _ntdevices

_console_io_control = _ntdevices.ConsoleDeviceObject.io_control


def console_output(dp: Dumpulator) -> list:
    """What the emulated program wrote to its console (WriteConsole / WriteFile)."""
    if not hasattr(dp, "_fathom_console"):
        dp._fathom_console = []
    return dp._fathom_console


def _write_console_text(dp: Dumpulator, text: str):
    text = text.replace("\0", "")  # a console shows nothing for NUL characters
    console_output(dp).append(text)
    import sys
    sys.stdout.write(text)
    sys.stdout.flush()


def _console_call(self, dp: Dumpulator, control):
    """ConsoleCallServerGeneric (IOCTL 0x500016 of \\Device\\ConDrv): CD_USER_DEFINED_IO, the console
    object handle, InputCount, OutputCount, then {Size, Buffer} pairs. The first input buffer is the
    message header (API number, descriptor size) followed by the API's message; the first output
    buffer receives the API's message back."""
    if control.code != 0x500016:
        return _console_io_control(self, dp, control)
    data = control.data
    handle, input_count, output_count = struct.unpack_from("<QII", data, 0)
    # {ULONG Size; (4 bytes not set by a 32-bit caller); PVOID64 Buffer}
    buffers = [(struct.unpack_from("<I", data, 16 + 16 * i)[0], struct.unpack_from("<Q", data, 16 + 16 * i + 8)[0])
               for i in range(input_count + output_count) if 16 + 16 * i + 16 <= len(data)]
    inputs, outputs = buffers[:input_count], buffers[input_count:]
    if not inputs:
        control.io_status = STATUS_INVALID_PARAMETER
        return None
    header = bytes(dp.read(inputs[0][1], min(inputs[0][0], 0x200)))
    api = struct.unpack_from("<I", header, 0)[0]
    message = header[8:]
    console = dp.handles.get(handle, None) if dp.handles.valid(handle) else None

    def reply(payload: bytes):
        if outputs:
            size, pointer = outputs[0]
            dp.write(pointer, payload[:size] + bytes(max(0, size - len(payload))))

    if api == 0x1000000:    # GetConsoleCP: CodePage, Output
        reply(struct.pack("<I", 65001))
    elif api == 0x1000001:  # GetConsoleMode
        reply(struct.pack("<I", getattr(console, "mode", 0x7)))
    elif api == 0x1000002:  # SetConsoleMode
        if console is not None and len(message) >= 4:
            console.mode = struct.unpack_from("<I", message, 0)[0]
        reply(message)
    elif api == 0x1000006:  # WriteConsole: NumBytes, Unicode; the text is the second input buffer
        unicode = len(message) > 4 and message[4] != 0
        size, pointer = inputs[1] if len(inputs) > 1 else (0, 0)
        text = bytes(dp.read(pointer, size)) if size else b""
        _write_console_text(dp, text.decode("utf-16-le", "replace") if unicode else text.decode("mbcs" if os.name == "nt" else "latin-1", "replace"))
        reply(struct.pack("<I", size) + message[4:])
    elif api == 0x1000005:  # ReadConsole: nothing to read
        reply(struct.pack("<I", 0))
    elif api == 0x2000007:  # GetConsoleScreenBufferInfoEx: 120 x 30, cursor at the origin
        info = struct.pack("<hhhhhhHhhhhH?", 120, 9001, 0, 0, 0, 0, 0x7, 120, 30, 120, 30, 0x7, False)
        reply(info)
    else:                   # titles, cursor, attributes, language id...: accepted, nothing to report
        dp.info(f"console API {hex(api)}: no effect in the emulator")
        reply(b"")
    control.io_status = STATUS_SUCCESS
    return None


_ntdevices.ConsoleDeviceObject.io_control = _console_call


# ---------------------------------------------------------------------------------- tokens

def _sid(*parts) -> bytes:
    """A SID: S-1-<authority>-<subauthorities...>."""
    authority, subs = parts[0], parts[1:]
    return struct.pack("<BB", 1, len(subs)) + authority.to_bytes(6, "big") + b"".join(struct.pack("<I", s) for s in subs)


_USER_SID = _sid(5, 21, 1000000001, 1000000002, 1000000003, 1001)  # a local user (placeholder)
_MEDIUM_INTEGRITY_SID = _sid(16, 0x2000)
_SE_GROUP_INTEGRITY = 0x20


@syscall
def ZwQueryInformationToken(dp: Dumpulator,
                            TokenHandle: Annotated[HANDLE, SAL("_In_")],
                            TokenInformationClass: Annotated[ULONG, SAL("_In_")],
                            TokenInformation: Annotated[PVOID, SAL("_Out_writes_bytes_to_opt_(TokenInformationLength, *ReturnLength)")],
                            TokenInformationLength: Annotated[ULONG, SAL("_In_")],
                            ReturnLength: Annotated[P[ULONG], SAL("_Out_")]
                            ):
    # The process token of an ordinary user, not elevated, medium integrity. The threads have none
    # of their own (NtOpenThreadToken).
    klass = int(TokenInformationClass)
    ptr = dp.ptr_size()
    address = TokenInformation.ptr
    pointer = "<Q" if ptr == 8 else "<I"

    def sid_and_attributes(sid: bytes, attributes: int) -> bytes:
        header = 2 * ptr
        return struct.pack(pointer, address + header) + struct.pack(pointer, attributes) + sid

    if klass == 1:      # TokenUser
        data = sid_and_attributes(_USER_SID, 0)
    elif klass in (4, 5):  # TokenOwner, TokenPrimaryGroup: {PSID}, the user
        data = struct.pack(pointer, address + ptr) + _USER_SID
    elif klass == 6:    # TokenDefaultDacl: {PACL}, full access for the user
        ace_size = 8 + len(_USER_SID)
        acl = struct.pack("<BBHHH", 2, 0, 8 + ace_size, 1, 0) + struct.pack("<BBHI", 0, 0, ace_size, 0x10000000) + _USER_SID
        data = struct.pack(pointer, address + ptr) + acl
    elif klass == 2:    # TokenGroups: Everyone, Users
        sids = [bytes.fromhex("010100000000000100000000"), bytes.fromhex("01020000000000052000000021020000")]
        header = 8 if ptr == 8 else 4
        entry = 2 * ptr
        offset = address + header + entry * len(sids)
        entries, blobs = b"", b""
        for sid in sids:
            entries += struct.pack(pointer, offset + len(blobs)) + struct.pack(pointer, 0x7)  # mandatory, enabled
            blobs += sid
        data = struct.pack("<I", len(sids)) + bytes(header - 4) + entries + blobs
    elif klass == 8:    # TokenType: TokenPrimary
        data = struct.pack("<I", 1)
    elif klass == 10:   # TokenStatistics
        data = struct.pack("<IiIiqIIIIIIIi", 0x1000, 0, 0x2000, 0, 0x7FFFFFFFFFFFFFFF, 1, 0, 4096, 4096, 15, 5, 0x1001, 0)
    elif klass == 12:   # TokenSessionId
        data = struct.pack("<I", 1)
    elif klass == 18:   # TokenElevationType: TokenElevationTypeDefault
        data = struct.pack("<I", 1)
    elif klass == 20:   # TokenElevation: not elevated
        data = struct.pack("<I", 0)
    elif klass in (24, 26, 29, 28):  # virtualization allowed / enabled, UI access, app container
        data = struct.pack("<I", 0)
    elif klass == 25:   # TokenIntegrityLevel: TOKEN_MANDATORY_LABEL, medium
        data = sid_and_attributes(_MEDIUM_INTEGRITY_SID, _SE_GROUP_INTEGRITY)
    elif klass == 31:   # TokenAppContainerSid: none
        data = struct.pack(pointer, 0)
    else:
        dp.error(f"NtQueryInformationToken: class {klass} is not emulated")
        return STATUS_INVALID_INFO_CLASS
    if ReturnLength != 0:
        ReturnLength.write_ulong(len(data))
    if int(TokenInformationLength) < len(data):
        return STATUS_BUFFER_TOO_SMALL
    dp.write(address, data)
    return STATUS_SUCCESS


# ---------------------------------------------------------------------------------- registry

def _key_path(dp: Dumpulator, attributes) -> str:
    """The full name of a key: ObjectName, after the name of RootDirectory when it is relative."""
    attrs = attributes[0]
    name = attrs.ObjectName[0].read_str() if attrs.ObjectName != 0 else ""
    root = int(attrs.RootDirectory)
    if root:
        parent = dp.handles.get(root, None) if dp.handles.valid(root) else None
        if isinstance(parent, RegistryKeyObject):
            name = parent.key.rstrip("\\") + ("\\" + name if name else "")
    return name


def _open_key(dp: Dumpulator, KeyHandle, ObjectAttributes):
    # The registry of the emulator holds the keys dumpulator sets up (and those a script adds with
    # dp.handles.create_key); the others do not exist, so the code takes its defaults
    path = _key_path(dp, ObjectAttributes)
    handle = dp.handles.open_file(path)
    if handle is None:
        dp.info(f"registry key not in the emulator: {path}")
        return STATUS_OBJECT_NAME_NOT_FOUND
    KeyHandle.write_ptr(handle)
    return STATUS_SUCCESS


@syscall
def ZwOpenKey(dp: Dumpulator,
              KeyHandle: Annotated[P[HANDLE], SAL("_Out_")],
              DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
              ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")]
              ):
    return _open_key(dp, KeyHandle, ObjectAttributes)


@syscall
def ZwOpenKeyEx(dp: Dumpulator,
                KeyHandle: Annotated[P[HANDLE], SAL("_Out_")],
                DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")],
                OpenOptions: Annotated[ULONG, SAL("_In_")]
                ):
    return _open_key(dp, KeyHandle, ObjectAttributes)


@syscall
def ZwAlertThreadByThreadIdEx(dp: Dumpulator,
                              ThreadId: Annotated[HANDLE, SAL("_In_")],
                              Lock: Annotated[PVOID, SAL("_In_opt_")]
                              ):
    # Windows 11: wakes the thread (Lock is an SRW lock the caller releases with the wake)
    return _scheduler(dp).alert(int(ThreadId))


@syscall
def ZwAlertMultipleThreadByThreadId(dp: Dumpulator,
                                    ThreadIds: Annotated[PVOID, SAL("_In_reads_(ThreadCount)")],
                                    ThreadCount: Annotated[ULONG, SAL("_In_")],
                                    Boost: Annotated[PVOID, SAL("_In_opt_")],
                                    BoostCount: Annotated[ULONG, SAL("_In_")]
                                    ):
    s = _scheduler(dp)
    for i in range(int(ThreadCount)):
        s.alert(dp.read_ptr(ThreadIds.ptr + i * dp.ptr_size()))
    return STATUS_SUCCESS


# ---------------------------------------------------------------------------------- process information

_PROCESS_COOKIE = 0x5BD1E995  # the secret of RtlEncodePointer: any value, constant in a process


@syscall
def ZwQueryInformationProcess(dp: Dumpulator,
                              ProcessHandle: Annotated[HANDLE, SAL("_In_")],
                              ProcessInformationClass: Annotated[ULONG, SAL("_In_")],
                              ProcessInformation: Annotated[PVOID, SAL("_Out_writes_bytes_(ProcessInformationLength)")],
                              ProcessInformationLength: Annotated[ULONG, SAL("_In_")],
                              ReturnLength: Annotated[P[ULONG], SAL("_Out_opt_")]
                              ):
    klass = int(ProcessInformationClass)
    if klass == PROCESSINFOCLASS.ProcessCookie.value:
        if int(ProcessInformationLength) != 4:
            return STATUS_INFO_LENGTH_MISMATCH
        dp.write_ulong(ProcessInformation.ptr, _PROCESS_COOKIE)
        if ReturnLength != 0:
            ReturnLength.write_ulong(4)
        return STATUS_SUCCESS
    try:
        klass = PROCESSINFOCLASS(klass)
    except ValueError:
        return STATUS_INVALID_INFO_CLASS
    return _original["ZwQueryInformationProcess"](dp, ProcessHandle, klass, ProcessInformation,
                                                   ProcessInformationLength, ReturnLength)


# ---------------------------------------------------------------------------------- exceptions

def _dispatch_exception(dp: Dumpulator, record_ptr: int, context_ptr: int):
    """NtRaiseException, first chance: the exception goes to KiUserExceptionDispatcher with the
    caller's CONTEXT and EXCEPTION_RECORD, on the stack layout dumpulator uses for its own exceptions
    (Dumpulator.handle_exception). Returns the context switch for the system call hook."""
    import ctypes
    x64 = dp._x64
    context_type = CONTEXT if x64 else WOW64_CONTEXT
    record_type = EXCEPTION_RECORD64 if x64 else EXCEPTION_RECORD32
    context_size = ctypes.sizeof(context_type)
    # the registers of the raise
    context = context_type.from_buffer(bytearray(dp.read(context_ptr, context_size)))
    context.to_regs(dp.regs)
    if x64:
        dp.regs.gs_base = dp.teb
    else:
        dp.regs.fs_base = dp.teb
    record = bytes(dp.read(record_ptr, ctypes.sizeof(record_type)))
    return _dispatch_from_registers(dp, record)


def _dispatch_from_registers(dp: Dumpulator, record: bytes):
    """The exception record goes to KiUserExceptionDispatcher with the current registers as its
    context. Returns the context switch to give dumpulator."""
    import ctypes
    from .dumpulator import UnicornExceptionInfo, ExceptionType
    x64 = dp._x64
    context_type = CONTEXT if x64 else WOW64_CONTEXT
    context_size = ctypes.sizeof(context_type)
    if x64:
        allocation_size, context_flags = 0x720, 0x10005F
    else:
        allocation_size, context_flags = 0x4B8, 0x1007F
    csp = dp.regs.csp - allocation_size
    if x64:
        csp &= ~0xF
    dp.write(csp, bytes(allocation_size))
    frame = context_type.from_buffer(bytearray(dp.read(csp, context_size)))
    frame.ContextFlags = context_flags
    frame.from_regs(dp.regs)
    context_ex = CONTEXT_EX()
    context_ex.All.Offset = -context_size & 0xFFFFFFFF
    context_ex.All.Length = allocation_size if x64 else 0x42C
    context_ex.Legacy.Offset = -context_size & 0xFFFFFFFF
    context_ex.Legacy.Length = context_size
    context_ex.XState.Offset = 0xF0 if x64 else 0x20
    context_ex.XState.Length = 0x160 if x64 else 0x140
    ptr = csp
    if x64:
        exception_address = struct.unpack_from("<Q", record, 0x10)[0]
        for data in (bytes(frame), bytes(context_ex)):
            dp.write(ptr, data)
            ptr += len(data)
        ptr += 8
        dp.write(ptr, record)
        ptr += len(record) + 8
        dp.write(ptr, struct.pack("<Q", exception_address))
        ptr += 8 + 16
        dp.write(ptr, struct.pack("<Q", frame.Rsp))
        ptr += 8 + 16
        dp.write(ptr, struct.pack("<QIIQQQQQQ", 0, 4, 8, 0, 0, 0, 0, 0, 0))
    else:
        ptr += 8
        dp.write_ulong(csp, ptr)
        dp.write(ptr, record)
        ptr += len(record)
        dp.write_ulong(csp + 4, ptr)
        dp.write(ptr, bytes(frame))
        ptr += context_size
        dp.write(ptr, bytes(context_ex))
    dp.regs.csp = csp
    dp.regs.cip = dp.KiUserExceptionDispatcher
    exception = UnicornExceptionInfo()
    exception.type = ExceptionType.ContextSwitch
    exception.final = True
    exception.context = dp._uc.context_save()
    return exception


@syscall
def ZwRaiseException(dp: Dumpulator,
                     ExceptionRecord: Annotated[P[EXCEPTION_RECORD], SAL("_In_")],
                     ContextRecord: Annotated[P[CONTEXT], SAL("_In_")],
                     FirstChance: Annotated[BOOLEAN, SAL("_In_")]
                     ):
    if not FirstChance:
        # second chance: nobody handled it, the process ends (as dumpulator does)
        return _original["ZwRaiseException"](dp, ExceptionRecord, ContextRecord, FirstChance)
    code = ExceptionRecord.read_ulong()
    if code in (0x40010006, 0x4001000A):  # DBG_PRINTEXCEPTION_C / _WIDE_C: OutputDebugString
        try:
            length = dp.read_ptr(ExceptionRecord.ptr + (0x20 if dp._x64 else 0x14))
            text_ptr = dp.read_ptr(ExceptionRecord.ptr + (0x28 if dp._x64 else 0x18))
            raw = bytes(dp.read(text_ptr, min(length, 4096)))
            text = raw.decode("utf-16-le" if code == 0x4001000A else "latin-1", "replace").rstrip("\0")
            dp.info(f"OutputDebugString: {text.rstrip()}")
        except Exception:
            pass
    return _dispatch_exception(dp, ExceptionRecord.ptr, ContextRecord.ptr)


@syscall
def ZwWriteFile(dp: Dumpulator,
                FileHandle: Annotated[HANDLE, SAL("_In_")],
                Event: Annotated[HANDLE, SAL("_In_opt_")],
                ApcRoutine: Annotated[P[IO_APC_ROUTINE], SAL("_In_opt_")],
                ApcContext: Annotated[PVOID, SAL("_In_opt_")],
                IoStatusBlock: Annotated[P[IO_STATUS_BLOCK], SAL("_Out_")],
                Buffer: Annotated[PVOID, SAL("_In_reads_bytes_(Length)")],
                Length: Annotated[ULONG, SAL("_In_")],
                ByteOffset: Annotated[P[LARGE_INTEGER], SAL("_In_opt_")],
                Key: Annotated[P[ULONG], SAL("_In_opt_")]
                ):
    # the console (stdout, stderr): the text goes to the output and to the result (console)
    file = dp.handles.get(int(FileHandle), None) if dp.handles.valid(int(FileHandle)) else None
    if isinstance(file, ConsoleFileObject) and file.type != ConsoleType.In:
        data = bytes(dp.read(Buffer.ptr, int(Length))) if int(Length) else b""
        _write_console_text(dp, data.decode("mbcs" if os.name == "nt" else "latin-1", "replace"))
        dp.write_ptr(IoStatusBlock.ptr, STATUS_SUCCESS)
        dp.write_ptr(IoStatusBlock.ptr + dp.ptr_size(), len(data))
        return STATUS_SUCCESS
    return _original["ZwWriteFile"](dp, FileHandle, Event, ApcRoutine, ApcContext, IoStatusBlock, Buffer, Length,
                                    ByteOffset, Key)


STATUS_ILLEGAL_INSTRUCTION = 0xC000001D


def raise_illegal_instruction(dp: Dumpulator, address: int) -> bool:
    """An instruction the emulator does not have: STATUS_ILLEGAL_INSTRUCTION in the program, as a
    processor without it raises (#UD). Called from dumpulator's invalid instruction hook."""
    try:
        code = bytes(dp.read(address, 15))
        instruction = next(dp.cs.disasm(code, address, 1))
        name = f"instruction {instruction.mnemonic}"
    except Exception:
        name = "instruction ?"
    first = name not in dp._fathom_unimplemented
    dp._fathom_unimplemented[name] = dp._fathom_unimplemented.get(name, 0) + 1
    if first:
        dp.error(f"{name} at {hex(address)} is not emulated: STATUS_ILLEGAL_INSTRUCTION in the program")
    dp.regs.cip = address
    if dp._x64:
        record = struct.pack("<IIQQII", STATUS_ILLEGAL_INSTRUCTION, 0, 0, address, 0, 0) + bytes(15 * 8)
    else:
        record = struct.pack("<IIIII", STATUS_ILLEGAL_INSTRUCTION, 0, 0, address, 0) + bytes(15 * 4)
    dp._exception = _dispatch_from_registers(dp, record)
    return True


# ---------------------------------------------------------------------------------- guard pages

def commit_stack(dp: Dumpulator):
    """The stack of the dumped thread, committed down to its last page before the emulation starts.
    Natively it grows through its guard page; changing the protection of a page inside unicorn's
    protection fault hook crashes unicorn (32-bit), so the whole reserve is committed here, outside
    of the emulation. The last page of the reserve stays reserved: the hard limit."""
    ptr = dp.ptr_size()
    teb = dp.teb
    try:
        stack_base = dp.read_ptr(teb + ptr)
        limit = dp.read_ptr(teb + 2 * ptr)
        deallocation = dp.read_ptr(teb + (0x1478 if dp._x64 else 0xE0C))
    except Exception:
        return
    if not (deallocation and deallocation < limit <= stack_base):
        return
    page = deallocation + PAGE_SIZE
    lowest = None
    while page < stack_base:
        try:
            info = dp.memory.query(page)
        except Exception:
            break
        size = min(info.base + info.region_size, stack_base) - page
        if size <= 0:
            break
        if info.state == MemoryState.MEM_RESERVE:
            dp.memory.commit(page, size, MemoryProtect.PAGE_READWRITE)
        elif info.state == MemoryState.MEM_COMMIT and info.protect is not None and info.protect & MemoryProtect.PAGE_GUARD:
            dp.memory.protect(page, size, MemoryProtect(info.protect & ~MemoryProtect.PAGE_GUARD))
        elif info.state != MemoryState.MEM_COMMIT:
            break
        if lowest is None:
            lowest = page
        page += size
    if lowest is not None:
        dp.write_ptr(teb + 2 * ptr, lowest)  # NtTib.StackLimit
        dp.debug(f"stack of thread {dp.thread_id} committed down to {hex(lowest)}")


def handle_guard_page(dp: Dumpulator, access: int, address: int) -> bool:
    """An access to a PAGE_GUARD page. The guard page of a thread's stack: the stack grows, as the
    kernel does it (the guard goes to the page below, StackLimit of the TEB follows) and the access
    goes on. Another guard page loses its guard (one-shot) and the access goes on. Returns True when
    handled (called from dumpulator's invalid memory hook)."""
    from unicorn import UC_MEM_READ_PROT, UC_MEM_WRITE_PROT, UC_MEM_FETCH_PROT
    if access not in (UC_MEM_READ_PROT, UC_MEM_WRITE_PROT, UC_MEM_FETCH_PROT):
        return False
    page = address & ~0xFFF
    try:
        info = dp.memory.query(page)
    except Exception:
        return False
    if info.protect is None or not (info.protect & MemoryProtect.PAGE_GUARD) or info.state != MemoryState.MEM_COMMIT:
        return False
    plain = MemoryProtect(info.protect & ~MemoryProtect.PAGE_GUARD)
    dp.memory.protect(page, PAGE_SIZE, plain)
    ptr = dp.ptr_size()
    teb = dp.teb
    stack_base = dp.read_ptr(teb + ptr)
    deallocation = dp.read_ptr(teb + (0x1478 if dp._x64 else 0xE0C))
    if deallocation <= page < stack_base:
        below = page - PAGE_SIZE
        if below >= deallocation + PAGE_SIZE:  # the last page of the reserve stays the hard limit
            try:
                below_info = dp.memory.query(below)
                if below_info.state == MemoryState.MEM_RESERVE:
                    dp.memory.commit(below, PAGE_SIZE, MemoryProtect(plain | MemoryProtect.PAGE_GUARD))
            except Exception:
                pass
        dp.write_ptr(teb + 2 * ptr, page)  # NtTib.StackLimit
        dp.debug(f"stack of thread {dp.thread_id} grown to {hex(page)}")
    else:
        dp.info(f"guard page {hex(page)} touched at {hex(dp.regs.cip)}: guard removed")
    return True


@syscall
def ZwCompareObjects(dp: Dumpulator,
                     FirstObjectHandle: Annotated[HANDLE, SAL("_In_")],
                     SecondObjectHandle: Annotated[HANDLE, SAL("_In_")]
                     ):
    # STATUS_SUCCESS when both handles lead to the same object (WriteProcessMemory compares its
    # process handle with the current process)
    def identity(handle):
        handle = int(handle)
        if _is_pseudo(dp, handle, dp.NtCurrentProcess()):
            return "process"
        obj = _object(dp, handle)
        return id(obj) if obj is not None else ("handle", handle)
    return STATUS_SUCCESS if identity(FirstObjectHandle) == identity(SecondObjectHandle) else 0xC00001AC


@syscall
def ZwFlushInstructionCache(dp: Dumpulator,
                            ProcessHandle: Annotated[HANDLE, SAL("_In_")],
                            BaseAddress: Annotated[PVOID, SAL("_In_opt_")],
                            Length: Annotated[SIZE_T, SAL("_In_")]
                            ):
    # nothing to flush: unicorn retranslates the code that is written
    return STATUS_SUCCESS


@syscall
def ZwProtectVirtualMemory(dp: Dumpulator,
                           ProcessHandle: Annotated[HANDLE, SAL("_In_")],
                           BaseAddress: Annotated[P[PVOID], SAL("_Inout_")],
                           RegionSize: Annotated[P[SIZE_T], SAL("_Inout_")],
                           NewProtect: Annotated[ULONG, SAL("_In_")],
                           OldProtect: Annotated[P[ULONG], SAL("_Out_")]
                           ):
    # the pages from the address to the address + size (dumpulator rounded the size alone: an
    # import address table that crosses a page boundary stayed read-only on its second page)
    address = BaseAddress.read_ptr()
    base = address & ~(PAGE_SIZE - 1)
    end = (address + max(RegionSize.read_ptr(), 1) + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1)
    protect = MemoryProtect(int(NewProtect))
    dp.info(f"protect {hex(base)}[{hex(end - base)}] = {protect}")
    try:
        old_protect = dp.memory.protect(base, end - base, protect)
    except (KeyError, AssertionError):
        return STATUS_CONFLICTING_ADDRESSES
    BaseAddress.write_ptr(base)
    RegionSize.write_ptr(end - base)
    OldProtect.write_ulong(old_protect.value)
    return STATUS_SUCCESS


STATUS_CONFLICTING_ADDRESSES = 0xC0000018


@syscall
def ZwReleaseWorkerFactoryWorker(dp: Dumpulator,
                                 WorkerFactoryHandle: Annotated[HANDLE, SAL("_In_")]
                                 ):
    # the thread pool asks the kernel for a worker: none is started, the threads that queued the
    # work do it themselves (the loader drains its queue on the loading thread)
    return STATUS_SUCCESS


@syscall
def ZwSetInformationProcess(dp: Dumpulator,
                            ProcessHandle: Annotated[HANDLE, SAL("_In_")],
                            ProcessInformationClass: Annotated[ULONG, SAL("_In_")],
                            ProcessInformation: Annotated[PVOID, SAL("_In_reads_bytes_(ProcessInformationLength)")],
                            ProcessInformationLength: Annotated[ULONG, SAL("_In_")]
                            ):
    klass = int(ProcessInformationClass)
    if klass == 35:  # ProcessTlsInformation: the TLS of a DLL loaded after the threads started
        return _set_process_tls(dp, ProcessInformation.ptr, int(ProcessInformationLength))
    return _original["ZwSetInformationProcess"](dp, ProcessHandle, PROCESSINFOCLASS(klass), ProcessInformation,
                                                ProcessInformationLength)


def _set_process_tls(dp: Dumpulator, pointer: int, length: int) -> int:
    """PROCESS_TLS_INFORMATION {Flags, OperationType, ThreadDataCount, TlsIndex / PreviousCount,
    THREAD_TLS_INFORMATION {Flags, NewTlsData / OldTlsData, ThreadId}[]}: the loader gives a TLS
    block per thread (operation 0, ProcessTlsReplaceIndex: it goes in the slot TlsIndex of the
    thread's vector) or a new vector (operation 1, ProcessTlsReplaceVector: the first PreviousCount
    slots are copied from the old one). The old value comes back in the entry, whose Flags get 2
    (used): the loader frees the old values of the used entries and its own allocations of the
    others. The current thread first, then the others that have a TLS vector, as many as there are
    entries (the loader counts the threads it initialized)."""
    ptr = dp.ptr_size()
    entry_size = 3 * ptr if dp._x64 else 12
    _, operation, count, index = struct.unpack("<IIII", bytes(dp.read(pointer, 16)))
    if length < 16 + count * entry_size or operation not in (0, 1):
        return STATUS_INFO_LENGTH_MISMATCH if length < 16 + count * entry_size else STATUS_INVALID_PARAMETER
    vector_offset = 0x58 if dp._x64 else 0x2C
    scheduler = dp.scheduler
    current = scheduler.current
    threads = [current] + [t for t in scheduler.threads.values() if t is not current and t.state != "terminated"]
    slot = 0
    for thread in threads:
        if slot >= count:
            break
        vector = dp.read_ptr(thread.teb + vector_offset)
        if not vector and thread is not current:
            continue
        entry = pointer + 16 + slot * entry_size
        new = dp.read_ptr(entry + ptr)
        if operation == 0:  # ProcessTlsReplaceIndex
            if not vector:
                continue
            old = dp.read_ptr(vector + index * ptr)
            dp.write_ptr(vector + index * ptr, new)
        else:  # ProcessTlsReplaceVector
            for i in range(index if vector else 0):
                dp.write_ptr(new + i * ptr, dp.read_ptr(vector + i * ptr))
            dp.write_ptr(thread.teb + vector_offset, new)
            old = vector
        dp.write_ptr(entry + ptr, old)
        dp.write_ptr(entry + 2 * ptr, thread.tid)
        # Flags: 2 = used (the loader frees the old value; an entry without it is freed as unused)
        dp.write_ulong(entry, dp.read_ulong(entry) | 2)
        slot += 1
    return STATUS_SUCCESS


@syscall
def ZwApphelpCacheControl(dp: Dumpulator,
                          Type: Annotated[ULONG, SAL("_In_")],
                          ServiceData: Annotated[PVOID, SAL("_Inout_opt_")]
                          ):
    # the application compatibility cache is empty: the image is looked up nowhere else
    return 0xC0000225  # STATUS_NOT_FOUND


@syscall
def ZwQuerySecurityObject(dp: Dumpulator,
                          Handle: Annotated[HANDLE, SAL("_In_")],
                          SecurityInformation: Annotated[ULONG, SAL("_In_")],
                          SecurityDescriptor: Annotated[PVOID, SAL("_Out_writes_bytes_opt_(Length)")],
                          Length: Annotated[ULONG, SAL("_In_")],
                          LengthNeeded: Annotated[P[ULONG], SAL("_Out_")]
                          ):
    # a self-relative descriptor without owner, group or ACL (a NULL DACL: everyone has access)
    data = struct.pack("<BBHIIII", 1, 0, 0x8004, 0, 0, 0, 0)
    if LengthNeeded != 0:
        LengthNeeded.write_ulong(len(data))
    if int(Length) < len(data):
        return STATUS_BUFFER_TOO_SMALL
    dp.write(SecurityDescriptor.ptr, data)
    return STATUS_SUCCESS


@syscall
def ZwQuerySecurityAttributesToken(dp: Dumpulator,
                                   TokenHandle: Annotated[HANDLE, SAL("_In_")],
                                   Attributes: Annotated[PVOID, SAL("_In_reads_opt_(NumberOfAttributes)")],
                                   NumberOfAttributes: Annotated[ULONG, SAL("_In_")],
                                   Buffer: Annotated[PVOID, SAL("_Out_writes_bytes_(Length)")],
                                   Length: Annotated[ULONG, SAL("_In_")],
                                   ReturnLength: Annotated[P[ULONG], SAL("_Out_")]
                                   ):
    # TOKEN_SECURITY_ATTRIBUTES_INFORMATION {Version 1, Reserved, AttributeCount 0, pAttributeV1 NULL}
    data = struct.pack("<HHI", 1, 0, 0) + bytes(dp.ptr_size())
    if ReturnLength != 0:
        ReturnLength.write_ulong(len(data))
    if int(Length) < len(data):
        return STATUS_BUFFER_TOO_SMALL
    dp.write(Buffer.ptr, data)
    return STATUS_SUCCESS


@syscall
def ZwQueryEvent(dp: Dumpulator,
                 EventHandle: Annotated[HANDLE, SAL("_In_")],
                 EventInformationClass: Annotated[ULONG, SAL("_In_")],
                 EventInformation: Annotated[PVOID, SAL("_Out_writes_bytes_(EventInformationLength)")],
                 EventInformationLength: Annotated[ULONG, SAL("_In_")],
                 ReturnLength: Annotated[P[ULONG], SAL("_Out_opt_")]
                 ):
    event = _object(dp, EventHandle)
    if not isinstance(event, EventObject):
        return STATUS_OBJECT_TYPE_MISMATCH
    if int(EventInformationLength) < 8:
        return STATUS_INFO_LENGTH_MISMATCH
    kind = event.event_type.value if hasattr(event.event_type, "value") else int(event.event_type)
    dp.write(EventInformation.ptr, struct.pack("<Ii", kind, 1 if event.signalled else 0))
    if ReturnLength != 0:
        ReturnLength.write_ulong(8)
    return STATUS_SUCCESS


@syscall
def ZwAllocateVirtualMemory(dp: Dumpulator,
                            ProcessHandle: Annotated[HANDLE, SAL("_In_")],
                            BaseAddress: Annotated[P[PVOID], SAL("_Inout_")],
                            ZeroBits: Annotated[ULONG_PTR, SAL("_In_")],
                            RegionSize: Annotated[P[SIZE_T], SAL("_Inout_")],
                            AllocationType: Annotated[ULONG, SAL("_In_")],
                            Protect: Annotated[ULONG, SAL("_In_")]
                            ):
    # as Windows: an address inside a page (the pages it touches), a commit of pages already
    # committed, ZeroBits, MEM_TOP_DOWN / MEM_WRITE_WATCH (ignored), MEM_RESET (nothing to do)
    if not _is_pseudo(dp, int(ProcessHandle), dp.NtCurrentProcess()):
        return STATUS_INVALID_HANDLE
    kind = int(AllocationType)
    address = dp.read_ptr(BaseAddress.ptr)
    size = dp.read_ptr(RegionSize.ptr)
    if size == 0:
        return STATUS_INVALID_PARAMETER
    if kind & (0x80000 | 0x1000000):  # MEM_RESET, MEM_RESET_UNDO
        return STATUS_SUCCESS
    try:
        protect = MemoryProtect(int(Protect))
    except ValueError:
        return 0xC0000045  # STATUS_INVALID_PAGE_PROTECTION
    if kind & MEM_RESERVE or address == 0:
        start = address & ~0xFFFF if address else 0
        end = (address + size + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1) if address else 0
        length = (end - start) if address else (size + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1)
        if not start:
            start = dp.memory.find_free(length)
            if start is None:
                return 0xC0000017  # STATUS_NO_MEMORY
        try:
            dp.memory.reserve(start, length, protect)
        except (KeyError, AssertionError):
            return STATUS_CONFLICTING_ADDRESSES
        if kind & MEM_COMMIT:
            dp.memory.commit(start, length, protect)
    else:
        start = address & ~(PAGE_SIZE - 1)
        length = ((address + size + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1)) - start
        try:
            dp.memory.commit(start, length, protect)
        except (KeyError, AssertionError):
            return 0xC0000018 if not dp.memory.query(start).state else STATUS_CONFLICTING_ADDRESSES
    BaseAddress.write_ptr(start)
    RegionSize.write_ptr(length)
    return STATUS_SUCCESS


# ---------------------------------------------------------------------------------- system information

def _processors(dp: Dumpulator) -> int:
    scheduler = getattr(dp, "scheduler", None)
    return max(1, scheduler.cpus if scheduler is not None else 1)


def _system_information(dp: Dumpulator, klass: int, native: bool) -> Optional[bytes]:
    """SystemBasicInformation, SystemProcessorInformation; native: the 64-bit machine seen by a
    32-bit process (NtWow64GetNativeSystemInformation, GetNativeSystemInfo)."""
    cpus = _processors(dp)
    if klass == 0:
        common = struct.pack("<IIIIIII", 0, 156250, PAGE_SIZE, 0x400000, 1, 0x47FFFF, 0x10000)
        if dp._x64:
            return common + bytes(4) + struct.pack("<QQQB", 0x10000, 0x7FFFFFFEFFFF, (1 << cpus) - 1, cpus) + bytes(7)
        maximum = 0xFFFEFFFF if native else 0x7FFEFFFF
        return common + struct.pack("<IIIB", 0x10000, maximum, (1 << min(cpus, 32)) - 1, min(cpus, 32)) + bytes(3)
    if klass == 1:
        architecture = 9 if (dp._x64 or native) else 0  # AMD64 / INTEL
        return struct.pack("<HHHHI", architecture, 6, 0x5507, cpus, 0x2FFFE)
    return None


def _write_information(dp: Dumpulator, data: bytes, buffer, length, return_length) -> int:
    if return_length != 0:
        return_length.write_ulong(len(data))
    if int(length) < len(data):
        return STATUS_INFO_LENGTH_MISMATCH
    dp.write(buffer.ptr, data)
    return STATUS_SUCCESS


_previous_system = _original["ZwQuerySystemInformation"]


@syscall
def ZwQuerySystemInformation(dp: Dumpulator,
                             SystemInformationClass: Annotated[ULONG, SAL("_In_")],
                             SystemInformation: Annotated[PVOID, SAL("_Out_writes_bytes_opt_(SystemInformationLength)")],
                             SystemInformationLength: Annotated[ULONG, SAL("_In_")],
                             ReturnLength: Annotated[P[ULONG], SAL("_Out_opt_")]
                             ):
    data = _system_information(dp, int(SystemInformationClass), False)
    if data is None:
        raise NotImplementedError(f"NtQuerySystemInformation class {int(SystemInformationClass)}")
    return _write_information(dp, data, SystemInformation, SystemInformationLength, ReturnLength)


@syscall
def ZwWow64GetNativeSystemInformation(dp: Dumpulator,
                                      SystemInformationClass: Annotated[ULONG, SAL("_In_")],
                                      NativeSystemInformation: Annotated[PVOID, SAL("_Out_writes_bytes_(InformationLength)")],
                                      InformationLength: Annotated[ULONG, SAL("_In_")],
                                      ReturnLength: Annotated[P[ULONG], SAL("_Out_opt_")]
                                      ):
    data = _system_information(dp, int(SystemInformationClass), True)
    if data is None:
        raise NotImplementedError(f"NtWow64GetNativeSystemInformation class {int(SystemInformationClass)}")
    return _write_information(dp, data, NativeSystemInformation, InformationLength, ReturnLength)


@syscall
def ZwOpenProcessToken(dp: Dumpulator,
                       ProcessHandle: Annotated[HANDLE, SAL("_In_")],
                       DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                       TokenHandle: Annotated[P[HANDLE], SAL("_Out_")]
                       ):
    # the token of the process (NtQueryInformationToken answers for any token handle)
    TokenHandle.write_ptr(dp.handles.new(ProcessTokenObject(ProcessHandle)))
    return STATUS_SUCCESS


@syscall
def ZwOpenProcessTokenEx(dp: Dumpulator,
                         ProcessHandle: Annotated[HANDLE, SAL("_In_")],
                         DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                         HandleAttributes: Annotated[ULONG, SAL("_In_")],
                         TokenHandle: Annotated[P[HANDLE], SAL("_Out_")]
                         ):
    TokenHandle.write_ptr(dp.handles.new(ProcessTokenObject(ProcessHandle)))
    return STATUS_SUCCESS


@syscall
def ZwTraceEvent(dp: Dumpulator,
                 TraceHandle: Annotated[HANDLE, SAL("_In_")],
                 Flags: Annotated[ULONG, SAL("_In_")],
                 FieldSize: Annotated[ULONG, SAL("_In_")],
                 Fields: Annotated[PVOID, SAL("_In_")]
                 ):
    return STATUS_SUCCESS  # event tracing (ETW): nobody listens in the emulator


@syscall
def ZwTraceControl(dp: Dumpulator,
                   TraceInformationClass: Annotated[ULONG, SAL("_In_")],
                   InputBuffer: Annotated[PVOID, SAL("_In_reads_bytes_opt_(InputBufferLength)")],
                   InputBufferLength: Annotated[ULONG, SAL("_In_")],
                   TraceInformation: Annotated[PVOID, SAL("_Out_writes_bytes_opt_(TraceInformationLength)")],
                   TraceInformationLength: Annotated[ULONG, SAL("_In_")],
                   ReturnLength: Annotated[P[ULONG], SAL("_Out_")]
                   ):
    if ReturnLength != 0:
        ReturnLength.write_ulong(0)
    return STATUS_NOT_SUPPORTED  # no event tracing sessions in the emulator


@syscall
def ZwQueryLicenseValue(dp: Dumpulator,
                        ValueName: Annotated[P[UNICODE_STRING], SAL("_In_")],
                        Type: Annotated[P[ULONG], SAL("_Out_opt_")],
                        Data: Annotated[PVOID, SAL("_Out_writes_bytes_to_opt_(DataSize, *ResultDataSize)")],
                        DataSize: Annotated[ULONG, SAL("_In_")],
                        ResultDataSize: Annotated[P[ULONG], SAL("_Out_")]
                        ):
    return STATUS_OBJECT_NAME_NOT_FOUND  # no license values: the callers take their defaults


@syscall
def ZwIsUILanguageComitted(dp: Dumpulator
                           ):
    return STATUS_SUCCESS


@syscall
def ZwQueryInstallUILanguage(dp: Dumpulator,
                             InstallUILanguageId: Annotated[P[USHORT], SAL("_Out_")]
                             ):
    # the language of the host's Windows
    try:
        import ctypes
        language = ctypes.windll.kernel32.GetSystemDefaultUILanguage() & 0xFFFF
    except Exception:
        language = 0x409
    dp.write(InstallUILanguageId.ptr, struct.pack("<H", language))
    return STATUS_SUCCESS


@syscall
def ZwGetMUIRegistryInfo(dp: Dumpulator,
                         Flags: Annotated[ULONG, SAL("_In_")],
                         DataSize: Annotated[P[ULONG], SAL("_Inout_")],
                         Data: Annotated[PVOID, SAL("_Out_")]
                         ):
    return 0xC0000225  # STATUS_NOT_FOUND: no MUI registry data, the default language is used


@dataclass
class DirectoryObject(AbstractObject):
    """An object directory (the session's BaseNamedObjects...): the names opened relative to it are
    those of the run (ipc.py)."""
    path: str

    def __str__(self):
        return self.pretty("path")


@syscall
def ZwOpenDirectoryObject(dp: Dumpulator,
                          DirectoryHandle: Annotated[P[HANDLE], SAL("_Out_")],
                          DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                          ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")]
                          ):
    DirectoryHandle.write_ptr(dp.handles.new(DirectoryObject(_object_name(ObjectAttributes))))
    return STATUS_SUCCESS
