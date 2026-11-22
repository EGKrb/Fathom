# Fathom addition to dumpulator: what the emulated processes share. Boost Software License 1.0, like
# the rest of dumpulator (LICENSE.dumpulator).
#
# - Named objects (events, mutexes, semaphores, sections): a name refers to the same object in every
#   emulated process of a run (CreateEvent of an existing name opens it: STATUS_OBJECT_NAME_EXISTS).
# - Sections (CreateFileMapping, MapViewOfFile): a section is memory of the host; each view maps it
#   into the emulator (unicorn's mem_map_ptr), so the processes that map a section share its bytes,
#   as natively. A section of a file starts with the file's content (the file does not change).
# - Pipes (CreatePipe, CreateNamedPipe, ConnectNamedPipe, CreateFile of \\.\pipe\name, PeekNamedPipe,
#   TransactNamedPipe): the bytes (or messages) written to one end are read at the other, between
#   threads and between processes. A read of an empty pipe waits for data (or for the other end to
#   close: ERROR_BROKEN_PIPE, which ends the reads of a C runtime), and so does ConnectNamedPipe.
#
# The objects with handles in several processes count them (fathom_add_ref / fathom_release): a pipe
# is broken when every handle of its writing end is closed, in every process.

import ctypes
import struct
from typing import Annotated, Dict, List, Optional

from .dumpulator import Dumpulator, syscall_functions
from .handles import AbstractObject, EventObject
from .memory import MemoryProtect, MemoryType, PAGE_SIZE
from .native import *
from .ntsyscalls import syscall

STATUS_OBJECT_NAME_EXISTS = 0x40000000
STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
STATUS_OBJECT_TYPE_MISMATCH = 0xC0000024
STATUS_PIPE_BROKEN = 0xC000014B
STATUS_PIPE_CLOSING = 0xC00000B1
STATUS_PIPE_CONNECTED = 0xC00000B2
STATUS_PIPE_LISTENING = 0xC00000B3
STATUS_PIPE_DISCONNECTED = 0xC00000B0
STATUS_PIPE_NOT_AVAILABLE = 0xC00000AC
STATUS_PIPE_EMPTY = 0xC00000D9
STATUS_BUFFER_OVERFLOW = 0x80000005
STATUS_PENDING = 0x103
STATUS_INVALID_DEVICE_REQUEST = 0xC0000010
STATUS_INVALID_INFO_CLASS = 0xC0000003
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
STATUS_SECTION_TOO_BIG = 0xC0000040
STATUS_NOT_MAPPED_VIEW = 0xC0000019

FSCTL_PIPE_DISCONNECT, FSCTL_PIPE_LISTEN, FSCTL_PIPE_PEEK = 0x110004, 0x110008, 0x11400C
FSCTL_PIPE_WAIT, FSCTL_PIPE_TRANSCEIVE = 0x110018, 0x11C017

_previous = {}
for _name in ("ZwSetInformationObject", "ZwQueryObject", "ZwCreateEvent", "ZwOpenEvent", "ZwCreateMutant", "ZwOpenMutant", "ZwCreateSemaphore", "ZwOpenSemaphore",
              "ZwCreateSection", "ZwOpenSection", "ZwMapViewOfSection", "ZwUnmapViewOfSection", "ZwClose",
              "ZwDuplicateObject", "ZwCreateFile", "ZwOpenFile", "ZwReadFile", "ZwWriteFile", "ZwFsControlFile",
              "ZwQueryInformationFile", "ZwSetInformationFile", "ZwQueryVolumeInformationFile", "ZwQuerySection",
              "ZwCreateNamedPipeFile"):
    _previous[_name] = syscall_functions[_name]


# ---------------------------------------------------------------------------------- the shared state

class Shared:
    """What the processes of a run share: named objects and named pipes."""

    def __init__(self):
        self.named: Dict[str, AbstractObject] = {}
        self.pipes: Dict[str, "NamedPipe"] = {}
        self.events: List[dict] = []


def shared(dp: Dumpulator) -> Shared:
    world = getattr(dp, "world", None)
    holder = world if world is not None else dp
    if getattr(holder, "shared", None) is None:
        holder.shared = Shared()
    return holder.shared


def signal_all(dp: Dumpulator):
    """An object changed: the waiting threads of every emulated process check their waits."""
    world = getattr(dp, "world", None)
    if world is not None:
        world.signal_all()
    else:
        dp.scheduler.signal()


def _object_name(attributes) -> str:
    if attributes == 0 or attributes.ptr == 0:
        return ""
    attrs = attributes[0]
    if attrs.ObjectName == 0:
        return ""
    return attrs.ObjectName[0].read_str().split("\0", 1)[0]


def _name_key(name: str) -> str:
    """Global\\x, Local\\x, Session\\1\\x and \\BaseNamedObjects\\x are the same name here."""
    return name.replace("/", "\\").split("\\")[-1].lower()


def _object(dp: Dumpulator, handle):
    handle = int(handle)
    return dp.handles.get(handle, None) if handle and dp.handles.valid(handle) else None


def _new_handle(dp: Dumpulator, obj) -> int:
    add = getattr(obj, "fathom_add_ref", None)
    if add is not None:
        add()
    return dp.handles.new(obj)


def inheritable(dp: Dumpulator) -> set:
    """The handles of a process a child inherits (OBJ_INHERIT, SetHandleInformation)."""
    if not hasattr(dp, "_fathom_inheritable"):
        dp._fathom_inheritable = set()
    return dp._fathom_inheritable


def mark_inherit(dp: Dumpulator, handle_ptr, attributes):
    """A handle created with OBJECT_ATTRIBUTES.Attributes & OBJ_INHERIT is inheritable."""
    try:
        if attributes == 0 or not attributes.ptr or handle_ptr == 0 or not handle_ptr.ptr:
            return
        flags = dp.read_ulong(attributes.ptr + (0x18 if dp._x64 else 0xC))
        handle = dp.read_ptr(handle_ptr.ptr)
        if flags & 0x2 and handle:
            inheritable(dp).add(handle & ~3)
    except Exception:
        pass


def release_handle(dp: Dumpulator, obj):
    release = getattr(obj, "fathom_release", None)
    if release is not None:
        release()
        signal_all(dp)


# ---------------------------------------------------------------------------------- named objects

def _create_named(kind: str, types, create, dp, handle_ptr, attributes):
    """create() makes the object and its handle (the implementation underneath); a name that exists
    gives a handle to the object that has it."""
    name = _object_name(attributes)
    if not name:
        status = create()
        if status == STATUS_SUCCESS:
            mark_inherit(dp, handle_ptr, attributes)
        return status
    table = shared(dp).named
    key = _name_key(name)
    existing = table.get(key)
    if existing is not None:
        if not isinstance(existing, types):
            return STATUS_OBJECT_TYPE_MISMATCH
        handle_ptr.write_ptr(_new_handle(dp, existing))
        mark_inherit(dp, handle_ptr, attributes)
        return STATUS_OBJECT_NAME_EXISTS
    status = create()
    if status == STATUS_SUCCESS:
        mark_inherit(dp, handle_ptr, attributes)
        obj = _object(dp, dp.read_ptr(handle_ptr.ptr))
        if obj is not None:
            table[key] = obj
            shared(dp).events.append(dict(kind="create_" + kind, name=name, pid=dp.process_id))
    return status


def _open_named(types, dp, handle_ptr, attributes, fallback=None):
    name = _object_name(attributes)
    obj = shared(dp).named.get(_name_key(name)) if name else None
    if obj is None:
        return fallback() if fallback is not None else STATUS_OBJECT_NAME_NOT_FOUND
    if not isinstance(obj, types):
        return STATUS_OBJECT_TYPE_MISMATCH
    handle_ptr.write_ptr(_new_handle(dp, obj))
    mark_inherit(dp, handle_ptr, attributes)
    return STATUS_SUCCESS


def _sync_types():
    from .threads import MutantObject, SemaphoreObject
    return EventObject, MutantObject, SemaphoreObject


@syscall
def ZwCreateEvent(dp: Dumpulator,
                  EventHandle: Annotated[P[HANDLE], SAL("_Out_")],
                  DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                  ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_opt_")],
                  EventType: Annotated[EVENT_TYPE, SAL("_In_")],
                  InitialState: Annotated[BOOLEAN, SAL("_In_")]
                  ):
    return _create_named("event", EventObject, lambda: _previous["ZwCreateEvent"](
        dp, EventHandle, DesiredAccess, ObjectAttributes, EventType, InitialState), dp, EventHandle, ObjectAttributes)


@syscall
def ZwOpenEvent(dp: Dumpulator,
                EventHandle: Annotated[P[HANDLE], SAL("_Out_")],
                DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")]
                ):
    return _open_named(EventObject, dp, EventHandle, ObjectAttributes)


@syscall
def ZwCreateMutant(dp: Dumpulator,
                   MutantHandle: Annotated[P[HANDLE], SAL("_Out_")],
                   DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                   ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_opt_")],
                   InitialOwner: Annotated[BOOLEAN, SAL("_In_")]
                   ):
    return _create_named("mutex", _sync_types()[1], lambda: _previous["ZwCreateMutant"](
        dp, MutantHandle, DesiredAccess, ObjectAttributes, InitialOwner), dp, MutantHandle, ObjectAttributes)


@syscall
def ZwOpenMutant(dp: Dumpulator,
                 MutantHandle: Annotated[P[HANDLE], SAL("_Out_")],
                 DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                 ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")]
                 ):
    return _open_named(_sync_types()[1], dp, MutantHandle, ObjectAttributes)


@syscall
def ZwCreateSemaphore(dp: Dumpulator,
                      SemaphoreHandle: Annotated[P[HANDLE], SAL("_Out_")],
                      DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                      ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_opt_")],
                      InitialCount: Annotated[LONG, SAL("_In_")],
                      MaximumCount: Annotated[LONG, SAL("_In_")]
                      ):
    return _create_named("semaphore", _sync_types()[2], lambda: _previous["ZwCreateSemaphore"](
        dp, SemaphoreHandle, DesiredAccess, ObjectAttributes, InitialCount, MaximumCount), dp, SemaphoreHandle,
        ObjectAttributes)


@syscall
def ZwOpenSemaphore(dp: Dumpulator,
                    SemaphoreHandle: Annotated[P[HANDLE], SAL("_Out_")],
                    DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                    ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")]
                    ):
    return _open_named(_sync_types()[2], dp, SemaphoreHandle, ObjectAttributes)


# ---------------------------------------------------------------------------------- sections

class SharedSection(AbstractObject):
    """A section whose views share host memory."""

    def __init__(self, size: int, protect: int, name: str = "", path: str = ""):
        self.size = size
        self.protect = protect
        self.name = name
        self.path = path
        self.memory = ctypes.create_string_buffer((size + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1) or PAGE_SIZE)
        self.address = ctypes.addressof(self.memory)
        self.mappers = []  # the emulators that map a view of it

    def __str__(self):
        return f"Section({self.name or self.path or hex(self.size)})"


class SectionView:
    def __init__(self, section: SharedSection, offset: int):
        self.section = section
        self.offset = offset

    def __str__(self):
        return f"view of {self.section}"


@syscall
def ZwCreateSection(dp: Dumpulator,
                    SectionHandle: Annotated[P[HANDLE], SAL("_Out_")],
                    DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                    ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_opt_")],
                    MaximumSize: Annotated[P[LARGE_INTEGER], SAL("_In_opt_")],
                    SectionPageProtection: Annotated[ULONG, SAL("_In_")],
                    AllocationAttributes: Annotated[ULONG, SAL("_In_")],
                    FileHandle: Annotated[HANDLE, SAL("_In_opt_")]
                    ):
    from .filesystem import HostFile, filesystem
    attributes = int(AllocationAttributes)
    file = _object(dp, FileHandle)
    if attributes & 0x1000000 or (int(FileHandle) and not isinstance(file, HostFile)):
        # an image, or a file of dumpulator's: the implementations underneath
        return _previous["ZwCreateSection"](dp, SectionHandle, DesiredAccess, ObjectAttributes, MaximumSize,
                                            SectionPageProtection, AllocationAttributes, FileHandle)
    size = struct.unpack("<q", bytes(dp.read(MaximumSize.ptr, 8)))[0] if MaximumSize != 0 and MaximumSize.ptr else 0
    data = b""
    if isinstance(file, HostFile):
        data = filesystem(dp).read(file.path)
        size = size or len(data)
    if size <= 0 or size > 0x40000000:
        return STATUS_SECTION_TOO_BIG if size > 0 else STATUS_INVALID_PARAMETER

    def create():
        section = SharedSection(size, int(SectionPageProtection), _object_name(ObjectAttributes),
                                file.path if isinstance(file, HostFile) else "")
        if data:
            ctypes.memmove(section.address, data, min(len(data), size))
        SectionHandle.write_ptr(_new_handle(dp, section))
        return STATUS_SUCCESS
    return _create_named("section", SharedSection, create, dp, SectionHandle, ObjectAttributes)


@syscall
def ZwOpenSection(dp: Dumpulator,
                  SectionHandle: Annotated[P[HANDLE], SAL("_Out_")],
                  DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                  ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")]
                  ):
    name = _object_name(ObjectAttributes)
    if "knowndlls" in name.lower():
        return _previous["ZwOpenSection"](dp, SectionHandle, DesiredAccess, ObjectAttributes)
    return _open_named(SharedSection, dp, SectionHandle, ObjectAttributes,
                       lambda: _previous["ZwOpenSection"](dp, SectionHandle, DesiredAccess, ObjectAttributes))


def _target(dp: Dumpulator, process) -> Optional[Dumpulator]:
    """The emulator of a process handle: this one, or a child's."""
    value = int(process)
    if value in (dp.NtCurrentProcess(), 0xFFFFFFFFFFFFFFFF, 0xFFFFFFFF):
        return dp
    obj = _object(dp, value)
    child = getattr(obj, "child", None)
    if child is not None and getattr(child, "dp", None) is not None and child.state != "exited":
        return child.dp
    return None


def map_view(target: Dumpulator, section: SharedSection, base: int, offset: int, size: int, protect: int) -> int:
    """Map size bytes of the section from offset at base (0: anywhere) in target; the address."""
    size = (size + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1)
    if not base:
        base = target.memory.find_free(size)
    protection = MemoryProtect(protect) if protect else MemoryProtect.PAGE_READWRITE
    if protection.value & 0x100:  # no guard pages on a view
        protection = MemoryProtect(protection.value & ~0x100)
    target.memory.reserve(base, size, protection, MemoryType.MEM_MAPPED, info=SectionView(section, offset))
    target._pages.fathom_shared = section.address + offset
    try:
        target.memory.commit(base, size, protection)
    finally:
        target._pages.fathom_shared = None
    if target not in section.mappers:
        section.mappers.append(target)
    world = getattr(target, "world", None)
    if world is not None and section not in world.sections:
        world.sections.append(section)
    return base


@syscall
def ZwMapViewOfSection(dp: Dumpulator,
                       SectionHandle: Annotated[HANDLE, SAL("_In_")],
                       ProcessHandle: Annotated[HANDLE, SAL("_In_")],
                       BaseAddress: Annotated[P[PVOID], SAL("_Inout_")],
                       ZeroBits: Annotated[ULONG_PTR, SAL("_In_")],
                       CommitSize: Annotated[SIZE_T, SAL("_In_")],
                       SectionOffset: Annotated[P[LARGE_INTEGER], SAL("_Inout_opt_")],
                       ViewSize: Annotated[P[SIZE_T], SAL("_Inout_")],
                       InheritDisposition: Annotated[SECTION_INHERIT, SAL("_In_")],
                       AllocationType: Annotated[ULONG, SAL("_In_")],
                       Win32Protect: Annotated[ULONG, SAL("_In_")]
                       ):
    section = _object(dp, SectionHandle)
    if not isinstance(section, SharedSection):
        return _previous["ZwMapViewOfSection"](dp, SectionHandle, ProcessHandle, BaseAddress, ZeroBits, CommitSize,
                                               SectionOffset, ViewSize, InheritDisposition, AllocationType, Win32Protect)
    target = _target(dp, ProcessHandle)
    if target is None:
        return STATUS_INVALID_HANDLE
    offset = struct.unpack("<q", bytes(dp.read(SectionOffset.ptr, 8)))[0] if SectionOffset != 0 and SectionOffset.ptr else 0
    offset &= ~0xFFFF if offset > 0 else 0
    size = dp.read_ptr(ViewSize.ptr) or (section.size - offset)
    if offset + size > len(section.memory):
        return STATUS_INVALID_PARAMETER
    base = map_view(target, section, dp.read_ptr(BaseAddress.ptr), offset, size, int(Win32Protect))
    BaseAddress.write_ptr(base)
    ViewSize.write_ptr((size + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1))
    shared(dp).events.append(dict(kind="map_view", section=str(section), pid=target.process_id, address=hex(base),
                                  size=size))
    return STATUS_SUCCESS


@syscall
def ZwUnmapViewOfSection(dp: Dumpulator,
                         ProcessHandle: Annotated[HANDLE, SAL("_In_")],
                         BaseAddress: Annotated[PVOID, SAL("_In_opt_")]
                         ):
    target = _target(dp, ProcessHandle)
    address = int(BaseAddress.ptr if hasattr(BaseAddress, "ptr") else BaseAddress)
    if target is not None:
        region = target.memory.find_region(address)
        if region is not None and isinstance(getattr(region, "info", None), SectionView):
            target.memory.release(region.start)
            return STATUS_SUCCESS
    return _previous["ZwUnmapViewOfSection"](dp, ProcessHandle, BaseAddress)


@syscall
def ZwQuerySection(dp: Dumpulator,
                   SectionHandle: Annotated[HANDLE, SAL("_In_")],
                   SectionInformationClass: Annotated[ULONG, SAL("_In_")],
                   SectionInformation: Annotated[PVOID, SAL("_Out_writes_bytes_(SectionInformationLength)")],
                   SectionInformationLength: Annotated[SIZE_T, SAL("_In_")],
                   ReturnLength: Annotated[P[SIZE_T], SAL("_Out_opt_")]
                   ):
    section = _object(dp, SectionHandle)
    if not isinstance(section, SharedSection):
        raise NotImplementedError("NtQuerySection of another section")
    if int(SectionInformationClass) != 0:  # SectionBasicInformation
        return STATUS_INVALID_INFO_CLASS
    data = (struct.pack("<QIIQ", 0, 0x8000000, 0, section.size) if dp._x64
            else struct.pack("<IIQ", 0, 0x8000000, section.size))
    if int(SectionInformationLength) < len(data):
        return STATUS_INFO_LENGTH_MISMATCH
    dp.write(SectionInformation.ptr, data)
    if ReturnLength != 0:
        ReturnLength.write_ptr(len(data))
    return STATUS_SUCCESS


# ---------------------------------------------------------------------------------- pipes

class PipeBuffer:
    """One direction of a pipe."""

    def __init__(self, message: bool):
        self.message = message
        self.data = bytearray()
        self.messages: List[bytes] = []
        self.readers = 0
        self.writers = 0

    def available(self) -> int:
        return sum(len(m) for m in self.messages) if self.message else len(self.data)

    def write(self, data: bytes):
        if self.message:
            self.messages.append(bytes(data))
        else:
            self.data += data

    def read(self, size: int):
        """(data, more): more when a message is longer than size (it stays for the next read)."""
        if self.message:
            if not self.messages:
                return b"", False
            message = self.messages[0]
            if len(message) > size:
                self.messages[0] = message[size:]
                return message[:size], True
            self.messages.pop(0)
            return message, False
        data = bytes(self.data[:size])
        del self.data[:size]
        return data, False

    def peek(self, size: int) -> bytes:
        if self.message:
            return self.messages[0][:size] if self.messages else b""
        return bytes(self.data[:size])


class NamedPipe:
    """The instances of a pipe name."""

    def __init__(self, name: str, maximum: int, message: bool):
        self.name = name
        self.maximum = maximum
        self.message = message
        self.instances: List["PipeEnd"] = []  # the server ends


class PipeEnd(AbstractObject):
    """A handle of one end of a pipe: it reads `inbound` and writes `outbound`."""

    def __init__(self, name: str, server: bool, inbound: Optional[PipeBuffer], outbound: Optional[PipeBuffer]):
        self.name = name
        self.server = server
        self.inbound = inbound
        self.outbound = outbound
        self.refs = 0
        self.connected = not server
        self.peer: Optional["PipeEnd"] = None
        self.pipe: Optional[NamedPipe] = None
        self.read_mode_message = False
        self.nonblocking = False
        self.transceive_written = False

    def __str__(self):
        return f"Pipe({self.name or 'anonymous'}, {'server' if self.server else 'client'})"

    def fathom_add_ref(self):
        if self.refs == 0:
            if self.inbound is not None:
                self.inbound.readers += 1
            if self.outbound is not None:
                self.outbound.writers += 1
        self.refs += 1

    def fathom_release(self):
        self.refs -= 1
        if self.refs == 0:
            if self.inbound is not None:
                self.inbound.readers -= 1
            if self.outbound is not None:
                self.outbound.writers -= 1
            if self.pipe is not None and self in self.pipe.instances:
                self.pipe.instances.remove(self)

    def broken(self) -> bool:
        if self.server and not self.connected:
            return False
        return self.inbound is None or self.inbound.writers == 0

    def fathom_signaled(self) -> bool:
        """For a thread waiting on this end: data to read, the other end gone, or a client."""
        if self.server and not self.connected:
            return False
        return (self.inbound is not None and self.inbound.available() > 0) or self.broken()


class PipeDirectory(AbstractObject):
    """\\Device\\NamedPipe\\ (CreatePipe creates its pipes relative to it)."""

    def __str__(self):
        return "NamedPipeDirectory"


def _pipe_name(dp: Dumpulator, attributes) -> Optional[tuple]:
    """("directory" | "pipe" | "relative", name, root object) for a name of the pipe file system."""
    if attributes == 0 or attributes.ptr == 0:
        return None
    attrs = attributes[0]
    name = attrs.ObjectName[0].read_str().split("\0", 1)[0] if attrs.ObjectName != 0 else ""
    root = _object(dp, attrs.RootDirectory)
    if isinstance(root, PipeDirectory):
        return ("pipe", name, root) if name else ("directory", "", root)
    if isinstance(root, PipeEnd):
        return "relative", name, root
    lower = name.lower()
    for prefix in ("\\device\\namedpipe\\", "\\??\\pipe\\", "\\dosdevices\\pipe\\", "\\global??\\pipe\\"):
        if lower.startswith(prefix):
            rest = name[len(prefix):]
            return ("pipe", rest, None) if rest else ("directory", "", None)
    if lower in ("\\device\\namedpipe", "\\device\\namedpipe\\"):
        return "directory", "", None
    return None


def _iosb(dp: Dumpulator, iosb, status: int, information: int):
    if iosb != 0 and iosb.ptr:
        dp.write_ptr(iosb.ptr, status & 0xFFFFFFFF)
        dp.write_ptr(iosb.ptr + dp.ptr_size(), information)


def _open_pipe(dp: Dumpulator, FileHandle, IoStatusBlock, attributes) -> Optional[int]:
    """NtCreateFile / NtOpenFile of the pipe file system; None for another name."""
    target = _pipe_name(dp, attributes)
    if target is None:
        return None
    kind, name, root = target
    if kind == "directory":
        FileHandle.write_ptr(dp.handles.new(PipeDirectory()))
        _iosb(dp, IoStatusBlock, STATUS_SUCCESS, 1)
        return STATUS_SUCCESS
    if kind == "relative":
        # the other end of an anonymous pipe (CreatePipe opens its writing end relative to the reading one)
        server = root
        client = PipeEnd(server.name, False, server.outbound, server.inbound)
        server.connected, server.peer, client.peer = True, client, server
        FileHandle.write_ptr(_new_handle(dp, client))
        _iosb(dp, IoStatusBlock, STATUS_SUCCESS, 1)
        signal_all(dp)
        return STATUS_SUCCESS
    pipe = shared(dp).pipes.get(name.lower())
    if pipe is None:
        return STATUS_OBJECT_NAME_NOT_FOUND
    listening = [end for end in pipe.instances if not end.connected]
    if not listening:
        return STATUS_PIPE_NOT_AVAILABLE
    server = listening[0]
    client = PipeEnd(name, False, server.outbound, server.inbound)
    server.connected, server.peer, client.peer = True, client, server
    FileHandle.write_ptr(_new_handle(dp, client))
    _iosb(dp, IoStatusBlock, STATUS_SUCCESS, 1)
    shared(dp).events.append(dict(kind="pipe_connect", name=name, pid=dp.process_id))
    signal_all(dp)
    return STATUS_SUCCESS


@syscall
def ZwCreateFile(dp: Dumpulator,
                 FileHandle: Annotated[P[HANDLE], SAL("_Out_")],
                 DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                 ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")],
                 IoStatusBlock: Annotated[P[IO_STATUS_BLOCK], SAL("_Out_")],
                 AllocationSize: Annotated[P[LARGE_INTEGER], SAL("_In_opt_")],
                 FileAttributes: Annotated[ULONG, SAL("_In_")],
                 ShareAccess: Annotated[ULONG, SAL("_In_")],
                 CreateDisposition: Annotated[ULONG, SAL("_In_")],
                 CreateOptions: Annotated[ULONG, SAL("_In_")],
                 EaBuffer: Annotated[PVOID, SAL("_In_reads_bytes_opt_(EaLength)")],
                 EaLength: Annotated[ULONG, SAL("_In_")]
                 ):
    status = _open_pipe(dp, FileHandle, IoStatusBlock, ObjectAttributes)
    if status is None:
        status = _previous["ZwCreateFile"](dp, FileHandle, DesiredAccess, ObjectAttributes, IoStatusBlock, AllocationSize,
                                           FileAttributes, ShareAccess, CreateDisposition, CreateOptions, EaBuffer, EaLength)
    if status == STATUS_SUCCESS:
        mark_inherit(dp, FileHandle, ObjectAttributes)
    return status


@syscall
def ZwOpenFile(dp: Dumpulator,
               FileHandle: Annotated[P[HANDLE], SAL("_Out_")],
               DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
               ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")],
               IoStatusBlock: Annotated[P[IO_STATUS_BLOCK], SAL("_Out_")],
               ShareAccess: Annotated[ULONG, SAL("_In_")],
               OpenOptions: Annotated[ULONG, SAL("_In_")]
               ):
    status = _open_pipe(dp, FileHandle, IoStatusBlock, ObjectAttributes)
    if status is None:
        status = _previous["ZwOpenFile"](dp, FileHandle, DesiredAccess, ObjectAttributes, IoStatusBlock, ShareAccess,
                                         OpenOptions)
    if status == STATUS_SUCCESS:
        mark_inherit(dp, FileHandle, ObjectAttributes)
    return status


@syscall
def ZwCreateNamedPipeFile(dp: Dumpulator,
                          FileHandle: Annotated[P[HANDLE], SAL("_Out_")],
                          DesiredAccess: Annotated[ULONG, SAL("_In_")],
                          ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")],
                          IoStatusBlock: Annotated[P[IO_STATUS_BLOCK], SAL("_Out_")],
                          ShareAccess: Annotated[ULONG, SAL("_In_")],
                          CreateDisposition: Annotated[ULONG, SAL("_In_")],
                          CreateOptions: Annotated[ULONG, SAL("_In_")],
                          NamedPipeType: Annotated[ULONG, SAL("_In_")],
                          ReadMode: Annotated[ULONG, SAL("_In_")],
                          CompletionMode: Annotated[ULONG, SAL("_In_")],
                          MaximumInstances: Annotated[ULONG, SAL("_In_")],
                          InboundQuota: Annotated[ULONG, SAL("_In_")],
                          OutboundQuota: Annotated[ULONG, SAL("_In_")],
                          DefaultTimeout: Annotated[P[LARGE_INTEGER], SAL("_In_opt_")]
                          ):
    target = _pipe_name(dp, ObjectAttributes)
    name = target[1] if target is not None else ""
    message = bool(int(NamedPipeType) & 1)
    access = int(DesiredAccess)
    # the server reads what the client writes (inbound) when it has read access, and the other way
    inbound = PipeBuffer(message) if access & 0x80000001 or not access & 0x40000002 else None
    outbound = PipeBuffer(message) if access & 0x40000002 or not access & 0x80000001 else None
    server = PipeEnd(name, True, inbound, outbound)
    server.read_mode_message = bool(int(ReadMode) & 1)
    server.nonblocking = bool(int(CompletionMode) & 1)
    if name:
        pipes = shared(dp).pipes
        pipe = pipes.get(name.lower())
        if pipe is None:
            pipe = pipes[name.lower()] = NamedPipe(name, int(MaximumInstances), message)
            shared(dp).events.append(dict(kind="create_pipe", name=name, pid=dp.process_id))
        elif pipe.maximum != 0xFFFFFFFF and len(pipe.instances) >= pipe.maximum:
            return 0xC00000AB  # STATUS_INSTANCE_NOT_AVAILABLE
        server.pipe = pipe
        pipe.instances.append(server)
    FileHandle.write_ptr(_new_handle(dp, server))
    mark_inherit(dp, FileHandle, ObjectAttributes)
    _iosb(dp, IoStatusBlock, STATUS_SUCCESS, 2)
    return STATUS_SUCCESS


def _pipe(dp: Dumpulator, handle) -> Optional[PipeEnd]:
    obj = _object(dp, handle)
    return obj if isinstance(obj, PipeEnd) else None


def _signal_event(dp: Dumpulator, event):
    obj = _object(dp, event)
    if isinstance(obj, EventObject):
        obj.signalled = True
        dp.scheduler.signal()


@syscall
def ZwReadFile(dp: Dumpulator,
               FileHandle: Annotated[HANDLE, SAL("_In_")],
               Event: Annotated[HANDLE, SAL("_In_opt_")],
               ApcRoutine: Annotated[P[IO_APC_ROUTINE], SAL("_In_opt_")],
               ApcContext: Annotated[PVOID, SAL("_In_opt_")],
               IoStatusBlock: Annotated[P[IO_STATUS_BLOCK], SAL("_Out_")],
               Buffer: Annotated[PVOID, SAL("_Out_writes_bytes_(Length)")],
               Length: Annotated[ULONG, SAL("_In_")],
               ByteOffset: Annotated[P[LARGE_INTEGER], SAL("_In_opt_")],
               Key: Annotated[P[ULONG], SAL("_In_opt_")]
               ):
    end = _pipe(dp, FileHandle)
    if end is None:
        return _previous["ZwReadFile"](dp, FileHandle, Event, ApcRoutine, ApcContext, IoStatusBlock, Buffer, Length,
                                       ByteOffset, Key)
    if end.inbound is None:
        return STATUS_INVALID_DEVICE_REQUEST
    if end.server and not end.connected:
        return STATUS_PIPE_LISTENING
    if end.inbound.available() == 0:
        if end.broken():
            _iosb(dp, IoStatusBlock, STATUS_PIPE_BROKEN, 0)
            return STATUS_PIPE_BROKEN
        if end.nonblocking:
            return STATUS_PIPE_EMPTY
        return dp.scheduler.block_and_retry([end])
    data, more = end.inbound.read(int(Length))
    if data:
        dp.write(Buffer.ptr, data)
    status = STATUS_BUFFER_OVERFLOW if more else STATUS_SUCCESS
    _iosb(dp, IoStatusBlock, status, len(data))
    _signal_event(dp, Event)
    signal_all(dp)
    return status


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
    end = _pipe(dp, FileHandle)
    if end is None:
        return _previous["ZwWriteFile"](dp, FileHandle, Event, ApcRoutine, ApcContext, IoStatusBlock, Buffer, Length,
                                        ByteOffset, Key)
    if end.outbound is None:
        return STATUS_INVALID_DEVICE_REQUEST
    if end.server and not end.connected:
        return STATUS_PIPE_LISTENING
    if end.outbound.readers == 0:
        return STATUS_PIPE_CLOSING  # the reading end is closed: ERROR_NO_DATA
    data = bytes(dp.read(Buffer.ptr, int(Length))) if int(Length) else b""
    end.outbound.write(data)
    _iosb(dp, IoStatusBlock, STATUS_SUCCESS, len(data))
    _signal_event(dp, Event)
    signal_all(dp)
    return STATUS_SUCCESS


@syscall
def ZwFsControlFile(dp: Dumpulator,
                    FileHandle: Annotated[HANDLE, SAL("_In_")],
                    Event: Annotated[HANDLE, SAL("_In_opt_")],
                    ApcRoutine: Annotated[P[IO_APC_ROUTINE], SAL("_In_opt_")],
                    ApcContext: Annotated[PVOID, SAL("_In_opt_")],
                    IoStatusBlock: Annotated[P[IO_STATUS_BLOCK], SAL("_Out_")],
                    FsControlCode: Annotated[ULONG, SAL("_In_")],
                    InputBuffer: Annotated[PVOID, SAL("_In_reads_bytes_opt_(InputBufferLength)")],
                    InputBufferLength: Annotated[ULONG, SAL("_In_")],
                    OutputBuffer: Annotated[PVOID, SAL("_Out_writes_bytes_opt_(OutputBufferLength)")],
                    OutputBufferLength: Annotated[ULONG, SAL("_In_")]
                    ):
    code = int(FsControlCode)
    obj = _object(dp, FileHandle)
    if isinstance(obj, PipeDirectory) and code == FSCTL_PIPE_WAIT:
        # WaitNamedPipe: FILE_PIPE_WAIT_FOR_BUFFER {Timeout, NameLength, TimeoutSpecified, Name}
        length = dp.read_ulong(InputBuffer.ptr + 8)
        name = bytes(dp.read(InputBuffer.ptr + 14, length)).decode("utf-16-le")
        pipe = shared(dp).pipes.get(name.lower())
        if pipe is None:
            return STATUS_OBJECT_NAME_NOT_FOUND
        if any(not end.connected for end in pipe.instances):
            return STATUS_SUCCESS
        return 0xC00000B5  # STATUS_IO_TIMEOUT: every instance is busy
    if not isinstance(obj, PipeEnd):
        return _previous["ZwFsControlFile"](dp, FileHandle, Event, ApcRoutine, ApcContext, IoStatusBlock, FsControlCode,
                                            InputBuffer, InputBufferLength, OutputBuffer, OutputBufferLength)
    end = obj
    if code == FSCTL_PIPE_LISTEN:  # ConnectNamedPipe
        if end.connected:
            return STATUS_PIPE_CONNECTED
        if int(Event):
            # overlapped: the event is signaled when a client connects
            def connected():
                _iosb(dp, IoStatusBlock, STATUS_SUCCESS, 0)
                _signal_event(dp, Event)
            end.on_connect = connected
            return STATUS_PENDING
        return dp.scheduler.block_and_retry([_Connected(end)])
    if code == FSCTL_PIPE_DISCONNECT:
        if end.peer is not None:
            end.peer.peer = None
        end.connected = False
        end.peer = None
        if end.pipe is not None:
            # a fresh pair of buffers for the next client
            message = end.pipe.message
            end.inbound = PipeBuffer(message) if end.inbound is not None else None
            end.outbound = PipeBuffer(message) if end.outbound is not None else None
            if end.inbound is not None:
                end.inbound.readers = 1
            if end.outbound is not None:
                end.outbound.writers = 1
        signal_all(dp)
        return STATUS_SUCCESS
    if code == FSCTL_PIPE_PEEK:
        if end.server and not end.connected:
            return STATUS_PIPE_LISTENING
        available = end.inbound.available() if end.inbound is not None else 0
        if available == 0 and end.broken():
            return STATUS_PIPE_BROKEN
        size = int(OutputBufferLength)
        if size < 16:
            return STATUS_BUFFER_TOO_SMALL
        data = end.inbound.peek(size - 16) if end.inbound is not None else b""
        messages = len(end.inbound.messages) if end.inbound is not None and end.inbound.message else 0
        message_length = len(end.inbound.messages[0]) if messages else 0
        dp.write(OutputBuffer.ptr, struct.pack("<IIII", 3, available, messages, message_length) + data)
        _iosb(dp, IoStatusBlock, STATUS_SUCCESS, 16 + len(data))
        return STATUS_BUFFER_OVERFLOW if len(data) < (message_length or available) else STATUS_SUCCESS
    if code == FSCTL_PIPE_TRANSCEIVE:  # TransactNamedPipe: write, then read the answer
        if not end.transceive_written:
            data = bytes(dp.read(InputBuffer.ptr, int(InputBufferLength))) if int(InputBufferLength) else b""
            end.outbound.write(data)
            end.transceive_written = True
            signal_all(dp)
        if end.inbound.available() == 0:
            if end.broken():
                end.transceive_written = False
                return STATUS_PIPE_BROKEN
            return dp.scheduler.block_and_retry([end])
        end.transceive_written = False
        data, more = end.inbound.read(int(OutputBufferLength))
        dp.write(OutputBuffer.ptr, data)
        _iosb(dp, IoStatusBlock, STATUS_BUFFER_OVERFLOW if more else STATUS_SUCCESS, len(data))
        return STATUS_BUFFER_OVERFLOW if more else STATUS_SUCCESS
    return STATUS_INVALID_DEVICE_REQUEST


class _Connected(AbstractObject):
    """Waits for a client of a server end."""

    def __init__(self, end: PipeEnd):
        self.end = end

    def fathom_signaled(self) -> bool:
        return self.end.connected


@syscall
def ZwQueryInformationFile(dp: Dumpulator,
                           FileHandle: Annotated[HANDLE, SAL("_In_")],
                           IoStatusBlock: Annotated[P[IO_STATUS_BLOCK], SAL("_Out_")],
                           FileInformation: Annotated[PVOID, SAL("_Out_writes_bytes_(Length)")],
                           Length: Annotated[ULONG, SAL("_In_")],
                           FileInformationClass: Annotated[ULONG, SAL("_In_")]
                           ):
    end = _pipe(dp, FileHandle)
    if end is None:
        return _previous["ZwQueryInformationFile"](dp, FileHandle, IoStatusBlock, FileInformation, Length,
                                                   FileInformationClass)
    klass = int(FileInformationClass)
    available = end.inbound.available() if end.inbound is not None else 0
    if klass == 5:      # FileStandardInformation
        data = struct.pack("<QQIBBH", 0, available, 1, 0, 0, 0)
    elif klass == 16:   # FileModeInformation
        data = struct.pack("<I", 0x20)
    elif klass == 23:   # FilePipeInformation
        data = struct.pack("<II", int(end.read_mode_message), int(end.nonblocking))
    elif klass == 24:   # FilePipeLocalInformation
        state = 3 if end.connected else 2  # connected / listening
        if end.connected and end.broken():
            state = 4  # closing
        write_quota = 0x10000
        data = struct.pack("<IIIIIIIIII", int(bool(end.inbound and end.inbound.message)), 2, 0xFFFFFFFF, 1, 0x10000,
                           available, 0x10000, write_quota, state, int(not end.server))
    elif klass == 9:    # FileNameInformation
        name = ("\\" + end.name).encode("utf-16-le")
        data = struct.pack("<I", len(name)) + name
    elif klass == 4:    # FileBasicInformation
        data = struct.pack("<QQQQII", 0, 0, 0, 0, 0x80, 0)
    else:
        return STATUS_INVALID_INFO_CLASS
    if int(Length) < len(data):
        return STATUS_INFO_LENGTH_MISMATCH
    dp.write(FileInformation.ptr, data)
    _iosb(dp, IoStatusBlock, STATUS_SUCCESS, len(data))
    return STATUS_SUCCESS


@syscall
def ZwSetInformationFile(dp: Dumpulator,
                         FileHandle: Annotated[HANDLE, SAL("_In_")],
                         IoStatusBlock: Annotated[P[IO_STATUS_BLOCK], SAL("_Out_")],
                         FileInformation: Annotated[PVOID, SAL("_In_reads_bytes_(Length)")],
                         Length: Annotated[ULONG, SAL("_In_")],
                         FileInformationClass: Annotated[ULONG, SAL("_In_")]
                         ):
    end = _pipe(dp, FileHandle)
    if end is None:
        return _previous["ZwSetInformationFile"](dp, FileHandle, IoStatusBlock, FileInformation, Length,
                                                 FileInformationClass)
    if int(FileInformationClass) == 23:  # FilePipeInformation (SetNamedPipeHandleState)
        read_mode, completion = struct.unpack("<II", bytes(dp.read(FileInformation.ptr, 8)))
        end.read_mode_message, end.nonblocking = bool(read_mode), bool(completion)
    _iosb(dp, IoStatusBlock, STATUS_SUCCESS, 0)
    return STATUS_SUCCESS


@syscall
def ZwQueryVolumeInformationFile(dp: Dumpulator,
                                 FileHandle: Annotated[HANDLE, SAL("_In_")],
                                 IoStatusBlock: Annotated[P[IO_STATUS_BLOCK], SAL("_Out_")],
                                 FsInformation: Annotated[PVOID, SAL("_Out_writes_bytes_(Length)")],
                                 Length: Annotated[ULONG, SAL("_In_")],
                                 FsInformationClass: Annotated[ULONG, SAL("_In_")]
                                 ):
    obj = _object(dp, FileHandle)
    if isinstance(obj, (PipeEnd, PipeDirectory)):
        if int(FsInformationClass) != 4:  # FileFsDeviceInformation (GetFileType)
            return STATUS_INVALID_INFO_CLASS
        dp.write(FsInformation.ptr, struct.pack("<II", 0x11, 0))  # FILE_DEVICE_NAMED_PIPE
        _iosb(dp, IoStatusBlock, STATUS_SUCCESS, 8)
        return STATUS_SUCCESS
    return _previous["ZwQueryVolumeInformationFile"](dp, FileHandle, IoStatusBlock, FsInformation, Length,
                                                     FsInformationClass)


# ---------------------------------------------------------------------------------- handles

@syscall
def ZwClose(dp: Dumpulator,
            Handle: Annotated[HANDLE, SAL("_In_ _Post_ptr_invalid_")]
            ):
    obj = _object(dp, Handle)
    status = _previous["ZwClose"](dp, Handle)
    if obj is not None and status == STATUS_SUCCESS:
        release_handle(dp, obj)
    return status


@syscall
def ZwDuplicateObject(dp: Dumpulator,
                      SourceProcessHandle: Annotated[HANDLE, SAL("_In_")],
                      SourceHandle: Annotated[HANDLE, SAL("_In_")],
                      TargetProcessHandle: Annotated[HANDLE, SAL("_In_opt_")],
                      TargetHandle: Annotated[P[HANDLE], SAL("_Out_opt_")],
                      DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                      HandleAttributes: Annotated[ULONG, SAL("_In_")],
                      Options: Annotated[ULONG, SAL("_In_")]
                      ):
    # between this process and its children too (DuplicateHandle into a child, out of a child)
    source = _target(dp, SourceProcessHandle)
    target = _target(dp, TargetProcessHandle) if int(TargetProcessHandle) else None
    if source is None:
        return STATUS_INVALID_HANDLE
    value = int(SourceHandle)
    if value in (source.NtCurrentProcess(), source.NtCurrentThread()) or value in (0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFE):
        if source is dp and (target is None or target is dp):
            return _previous["ZwDuplicateObject"](dp, SourceProcessHandle, SourceHandle, TargetProcessHandle, TargetHandle,
                                                  DesiredAccess, HandleAttributes, Options)
        return STATUS_NOT_SUPPORTED
    obj = source.handles.get(value, None) if source.handles.valid(value) else None
    if obj is None:
        if source is dp and target in (None, dp):
            return _previous["ZwDuplicateObject"](dp, SourceProcessHandle, SourceHandle, TargetProcessHandle, TargetHandle,
                                                  DesiredAccess, HandleAttributes, Options)
        return STATUS_INVALID_HANDLE
    if target is not None and TargetHandle != 0 and TargetHandle.ptr:
        new = _new_handle(target, obj)
        TargetHandle.write_ptr(new)
        if int(HandleAttributes) & 0x2:
            inheritable(target).add(new)
    if int(Options) & 1:  # DUPLICATE_CLOSE_SOURCE
        source.handles.close(value)
        release_handle(source, obj)
    return STATUS_SUCCESS


@syscall
def ZwSetInformationObject(dp: Dumpulator,
                           Handle: Annotated[HANDLE, SAL("_In_")],
                           ObjectInformationClass: Annotated[ULONG, SAL("_In_")],
                           ObjectInformation: Annotated[PVOID, SAL("_In_reads_bytes_(ObjectInformationLength)")],
                           ObjectInformationLength: Annotated[ULONG, SAL("_In_")]
                           ):
    if int(ObjectInformationClass) == 4:  # ObjectHandleFlagInformation {Inherit, ProtectFromClose}
        handle = int(Handle) & ~3
        if not dp.handles.valid(handle):
            return STATUS_INVALID_HANDLE
        if dp.read(ObjectInformation.ptr, 1)[0]:
            inheritable(dp).add(handle)
        else:
            inheritable(dp).discard(handle)
        return STATUS_SUCCESS
    raise NotImplementedError(f"NtSetInformationObject class {int(ObjectInformationClass)}")


@syscall
def ZwQueryObject(dp: Dumpulator,
                  Handle: Annotated[HANDLE, SAL("_In_opt_")],
                  ObjectInformationClass: Annotated[ULONG, SAL("_In_")],
                  ObjectInformation: Annotated[PVOID, SAL("_Out_writes_bytes_opt_(ObjectInformationLength)")],
                  ObjectInformationLength: Annotated[ULONG, SAL("_In_")],
                  ReturnLength: Annotated[P[ULONG], SAL("_Out_opt_")]
                  ):
    if int(ObjectInformationClass) == 4:  # ObjectHandleFlagInformation
        handle = int(Handle) & ~3
        if int(ObjectInformationLength) < 2:
            return STATUS_INFO_LENGTH_MISMATCH
        dp.write(ObjectInformation.ptr, bytes([1 if handle in inheritable(dp) else 0, 0]))
        if ReturnLength != 0:
            ReturnLength.write_ulong(2)
        return STATUS_SUCCESS
    from .ntenums import OBJECT_INFORMATION_CLASS
    try:
        klass = OBJECT_INFORMATION_CLASS(int(ObjectInformationClass))
    except ValueError:
        raise NotImplementedError(f"NtQueryObject class {int(ObjectInformationClass)}") from None
    return _previous["ZwQueryObject"](dp, Handle, klass, ObjectInformation, ObjectInformationLength, ReturnLength)
