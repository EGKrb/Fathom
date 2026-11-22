# Fathom addition to dumpulator: the files of the emulated program, read from the host's disk.
# Boost Software License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# A file the program opens is read from the host (read-only); what it creates, writes, renames or
# deletes stays in the emulator (an overlay shared by the emulated processes of a run), so the
# host's disk never changes. The files written are in the events of the run (fathom_emu.py can save
# them: --files-out). Directories are listed (FindFirstFile: NtQueryDirectoryFile[Ex]) with the
# overlay's changes; System32 is SysWOW64 for a 32-bit program, as the WoW64 layer does it. Names
# that are not files of a drive (the console, devices, the files a script maps with
# dp.handles.map_file) go to the implementations underneath.

import fnmatch
import os
import stat as _stat
import struct
import time
from typing import Annotated, Dict, List, Optional, Tuple

from .dumpulator import Dumpulator, syscall_functions
from .handles import AbstractFileObject, AbstractObject, EventObject
from .native import *
from .ntsyscalls import syscall

STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
STATUS_OBJECT_NAME_COLLISION = 0xC0000035
STATUS_FILE_IS_A_DIRECTORY = 0xC00000BA
STATUS_NOT_A_DIRECTORY = 0xC0000103
STATUS_END_OF_FILE = 0xC0000011
STATUS_NO_MORE_FILES = 0x80000006
STATUS_NO_SUCH_FILE = 0xC000000F
STATUS_BUFFER_OVERFLOW = 0x80000005
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
STATUS_INVALID_INFO_CLASS = 0xC0000003
STATUS_DIRECTORY_NOT_EMPTY = 0xC0000101
STATUS_ACCESS_DENIED = 0xC0000022
STATUS_INVALID_DEVICE_REQUEST = 0xC0000010

FILE_DIRECTORY_FILE, FILE_NON_DIRECTORY_FILE, FILE_DELETE_ON_CLOSE = 0x1, 0x40, 0x1000
FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_ARCHIVE, FILE_ATTRIBUTE_NORMAL = 0x10, 0x20, 0x80
EPOCH_AS_FILETIME = 116444736000000000

_previous = {}  # the implementations underneath (fathom_nt, ntsyscalls)


def _enum(kind, value):
    """An information class for the implementations underneath (they take dumpulator's enums); a class
    the enum lacks stops them as one they do not implement."""
    try:
        return kind(int(value))
    except ValueError:
        raise NotImplementedError(f"{kind.__name__} {int(value)}") from None


def _chain(name):
    _previous[name] = syscall_functions[name]


for _name in ("ZwCreateFile", "ZwOpenFile", "ZwReadFile", "ZwWriteFile", "ZwQueryInformationFile",
              "ZwSetInformationFile", "ZwQueryVolumeInformationFile", "ZwClose", "ZwCreateSection"):
    _chain(_name)


class FileState:
    """A file or directory of the overlay."""

    def __init__(self, path: str, directory: bool, data: Optional[bytearray] = None):
        self.path = path
        self.directory = directory
        self.data = data if data is not None else bytearray()
        self.deleted = False
        self.attributes = FILE_ATTRIBUTE_DIRECTORY if directory else FILE_ATTRIBUTE_ARCHIVE
        now = int(time.time() * 10_000_000) + EPOCH_AS_FILETIME
        self.times = [now, now, now, now]
        self.written = False


class FileSystem:
    """The files of a run: the host's disk under the changes of the emulated processes."""

    def __init__(self):
        self.overlay: Dict[str, FileState] = {}
        self.events: List[dict] = []

    @staticmethod
    def key(path: str) -> str:
        return os.path.normcase(os.path.normpath(path))

    def lookup(self, path: str) -> Tuple[Optional[FileState], Optional[os.stat_result]]:
        """(the overlay's state, the host's stat): one of them, or neither when it does not exist."""
        state = self.overlay.get(self.key(path))
        if state is not None:
            return (None, None) if state.deleted else (state, None)
        # a deleted or replaced ancestor hides the host's file
        parent = os.path.dirname(path)
        while parent and parent != os.path.dirname(parent):
            ancestor = self.overlay.get(self.key(parent))
            if ancestor is not None and (ancestor.deleted or not ancestor.directory):
                return None, None
            parent = os.path.dirname(parent)
        try:
            return None, os.stat(path)
        except (OSError, ValueError):
            return None, None

    def exists(self, path: str) -> bool:
        state, host = self.lookup(path)
        return state is not None or host is not None

    def is_directory(self, path: str) -> bool:
        state, host = self.lookup(path)
        if state is not None:
            return state.directory
        return host is not None and _stat.S_ISDIR(host.st_mode)

    def materialize(self, path: str) -> FileState:
        """The overlay's copy of a file (copied from the host the first time it changes)."""
        key = self.key(path)
        state = self.overlay.get(key)
        if state is not None and not state.deleted:
            return state
        _, host = self.lookup(path)
        if host is not None and _stat.S_ISDIR(host.st_mode):
            state = FileState(path, True)
        else:
            data = bytearray()
            if host is not None:
                with open(path, "rb") as f:
                    data = bytearray(f.read())
            state = FileState(path, False, data)
            if host is not None:
                state.times = [_filetime(host.st_ctime), _filetime(host.st_atime), _filetime(host.st_mtime), _filetime(host.st_mtime)]
        self.overlay[key] = state
        return state

    def create(self, path: str, directory: bool) -> FileState:
        state = FileState(path, directory)
        self.overlay[self.key(path)] = state
        self.events.append(dict(kind="create_directory" if directory else "create_file", path=path))
        return state

    def delete(self, path: str):
        state = self.materialize(path)
        state.deleted = True
        self.events.append(dict(kind="delete", path=path))

    def rename(self, old: str, new: str):
        state = self.materialize(old)
        moved = FileState(new, state.directory, bytearray(state.data))
        moved.times, moved.attributes, moved.written = list(state.times), state.attributes, state.written
        state.deleted = True
        self.overlay[self.key(new)] = moved
        self.events.append(dict(kind="rename", path=old, new_path=new))

    def read(self, path: str) -> bytes:
        state, host = self.lookup(path)
        if state is not None:
            return bytes(state.data)
        with open(path, "rb") as f:
            return f.read()

    def size(self, path: str) -> int:
        state, host = self.lookup(path)
        if state is not None:
            return len(state.data)
        return host.st_size if host is not None and not _stat.S_ISDIR(host.st_mode) else 0

    def listing(self, path: str) -> List[str]:
        names: Dict[str, str] = {}
        state, host = self.lookup(path)
        if host is not None:
            try:
                for name in os.listdir(path):
                    names[name.lower()] = name
            except OSError:
                pass
        prefix = self.key(path).rstrip("\\") + "\\"
        for key, entry in self.overlay.items():
            if key.startswith(prefix) and "\\" not in key[len(prefix):]:
                name = os.path.basename(entry.path)
                if entry.deleted:
                    names.pop(name.lower(), None)
                else:
                    names[name.lower()] = name
        return sorted(names.values(), key=str.lower)

    def written_files(self) -> List[FileState]:
        return [s for s in self.overlay.values() if s.written and not s.deleted and not s.directory]


def _filetime(seconds: float) -> int:
    return int(seconds * 10_000_000) + EPOCH_AS_FILETIME


def filesystem(dp: Dumpulator) -> FileSystem:
    world = getattr(dp, "world", None)
    holder = world if world is not None else dp
    if getattr(holder, "filesystem", None) is None:
        holder.filesystem = FileSystem()
    return holder.filesystem


class HostFile(AbstractFileObject):
    """A handle of a file or directory of a drive."""

    def __init__(self, path: str, directory: bool, access: int, delete_on_close: bool):
        super().__init__(path)
        self.directory = directory
        self.access = access
        self.delete_on_close = delete_on_close
        self.position = 0
        self.listing: Optional[List[str]] = None
        self.listing_index = 0
        self.pattern = "*"

    def __str__(self):
        return f"HostFile({self.path})"


# ---------------------------------------------------------------------------------- names

def _current_directory(dp: Dumpulator) -> Tuple[str, int]:
    """(DosPath of the current directory, its handle) from the process parameters."""
    params = dp.read_ptr(dp.peb + (0x20 if dp._x64 else 0x10))
    offset = 0x38 if dp._x64 else 0x24
    length = struct.unpack("<H", bytes(dp.read(params + offset, 2)))[0]
    buffer = dp.read_ptr(params + offset + (8 if dp._x64 else 4))
    handle = dp.read_ptr(params + offset + (16 if dp._x64 else 8))
    path = bytes(dp.read(buffer, length)).decode("utf-16-le") if length and buffer else ""
    return path, handle


def _redirect(dp: Dumpulator, path: str) -> str:
    """System32 of a 32-bit program is SysWOW64 (Sysnative: the real System32)."""
    windows = os.environ.get("SystemRoot", r"C:\Windows")
    lower = path.lower()
    system32 = (windows + "\\System32").lower()
    if not dp._x64 and (lower == system32 or lower.startswith(system32 + "\\")):
        rest = lower[len(system32):]
        if not rest.startswith(("\\drivers\\etc", "\\catroot", "\\logfiles", "\\spool")):
            return windows + "\\SysWOW64" + path[len(system32):]
    sysnative = (windows + "\\Sysnative").lower()
    if lower == sysnative or lower.startswith(sysnative + "\\"):
        return windows + "\\System32" + path[len(sysnative):]
    return path


def dos_path(dp: Dumpulator, attributes) -> Optional[str]:
    """The path of a drive's file an OBJECT_ATTRIBUTES names, or None for another object."""
    attrs = attributes[0]
    name = attrs.ObjectName[0].read_str() if attrs.ObjectName != 0 else ""
    name = name.split("\0", 1)[0]
    root = int(attrs.RootDirectory)
    if root:
        parent = dp.handles.get(root, None) if dp.handles.valid(root) else None
        if isinstance(parent, HostFile):
            base = parent.path
        else:
            current, handle = _current_directory(dp)
            if root != handle or not current:
                return None
            base = current
        return _redirect(dp, os.path.normpath(os.path.join(base, name))) if name else base.rstrip("\\") or base
    for prefix in ("\\??\\", "\\DosDevices\\", "\\GLOBAL??\\"):
        if name[:len(prefix)].lower() == prefix.lower():
            rest = name[len(prefix):]
            if len(rest) >= 2 and rest[1] == ":" and rest[0].isalpha():
                if len(rest) == 2:
                    rest += "\\"
                return _redirect(dp, os.path.normpath(rest) if len(rest) > 3 else rest)
            if rest[:4].upper() == "UNC\\":
                return "\\\\" + rest[4:]
            return None
    if name[:12].lower() == "\\systemroot\\":
        return _redirect(dp, os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), name[12:]))
    return None


def _mapped(dp: Dumpulator, attributes) -> bool:
    """A file a script mapped with dp.handles.map_file (dumpulator's own files come first)."""
    try:
        name = attributes[0].ObjectName[0].read_str()
    except Exception:
        return False
    return name.lower() in dp.handles._mapped_files


def _iosb(dp: Dumpulator, iosb, status: int, information: int):
    if iosb != 0 and iosb.ptr:
        dp.write_ptr(iosb.ptr, status & 0xFFFFFFFF)
        dp.write_ptr(iosb.ptr + dp.ptr_size(), information)


def _host_file(dp: Dumpulator, handle) -> Optional[HostFile]:
    handle = int(handle)
    obj = dp.handles.get(handle, None) if dp.handles.valid(handle) else None
    return obj if isinstance(obj, HostFile) else None


def _signal_event(dp: Dumpulator, event):
    event = int(event)
    if event and dp.handles.valid(event):
        obj = dp.handles.get(event, None)
        if isinstance(obj, EventObject):
            obj.signalled = True
            dp.scheduler.signal()


# ---------------------------------------------------------------------------------- open

def open_file(dp: Dumpulator, FileHandle, DesiredAccess, path: str, IoStatusBlock, disposition: int, options: int,
              attributes: int = 0) -> int:
    fs = filesystem(dp)
    exists = fs.exists(path)
    directory = fs.is_directory(path) if exists else bool(options & FILE_DIRECTORY_FILE)
    information = 1  # FILE_OPENED
    parent = os.path.dirname(path)
    if not exists:
        if disposition in (1, 4):  # FILE_OPEN, FILE_OVERWRITE
            return STATUS_OBJECT_NAME_NOT_FOUND if (not parent or fs.is_directory(parent)) else STATUS_OBJECT_PATH_NOT_FOUND
        if parent and parent != path and not fs.is_directory(parent):
            return STATUS_OBJECT_PATH_NOT_FOUND
        state = fs.create(path, directory)
        if attributes and not directory:
            state.attributes = attributes
        state.written = not directory
        information = 2  # FILE_CREATED
    else:
        if disposition == 2:  # FILE_CREATE
            return STATUS_OBJECT_NAME_COLLISION
        if directory and options & FILE_NON_DIRECTORY_FILE:
            return STATUS_FILE_IS_A_DIRECTORY
        if not directory and options & FILE_DIRECTORY_FILE:
            return STATUS_NOT_A_DIRECTORY
        if disposition in (0, 4, 5) and not directory:  # supersede, overwrite
            state = fs.materialize(path)
            state.data = bytearray()
            state.written = True
            fs.events.append(dict(kind="overwrite", path=path))
            information = 0 if disposition == 0 else 3
    handle = HostFile(path, directory, int(DesiredAccess), bool(options & FILE_DELETE_ON_CLOSE))
    FileHandle.write_ptr(dp.handles.new(handle))
    _iosb(dp, IoStatusBlock, STATUS_SUCCESS, information)
    dp.info(f"file {path}: {['superseded', 'opened', 'created', 'overwritten'][information]}")
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
    path = None if _mapped(dp, ObjectAttributes) else dos_path(dp, ObjectAttributes)
    if path is None:
        return _previous["ZwCreateFile"](dp, FileHandle, DesiredAccess, ObjectAttributes, IoStatusBlock, AllocationSize,
                                         FileAttributes, ShareAccess, CreateDisposition, CreateOptions, EaBuffer, EaLength)
    return open_file(dp, FileHandle, DesiredAccess, path, IoStatusBlock, int(CreateDisposition), int(CreateOptions),
                     int(FileAttributes))


@syscall
def ZwOpenFile(dp: Dumpulator,
               FileHandle: Annotated[P[HANDLE], SAL("_Out_")],
               DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
               ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")],
               IoStatusBlock: Annotated[P[IO_STATUS_BLOCK], SAL("_Out_")],
               ShareAccess: Annotated[ULONG, SAL("_In_")],
               OpenOptions: Annotated[ULONG, SAL("_In_")]
               ):
    path = None if _mapped(dp, ObjectAttributes) else dos_path(dp, ObjectAttributes)
    if path is None:
        return _previous["ZwOpenFile"](dp, FileHandle, DesiredAccess, ObjectAttributes, IoStatusBlock, ShareAccess,
                                       OpenOptions)
    return open_file(dp, FileHandle, DesiredAccess, path, IoStatusBlock, 1, int(OpenOptions))


@syscall
def ZwClose(dp: Dumpulator,
            Handle: Annotated[HANDLE, SAL("_In_ _Post_ptr_invalid_")]
            ):
    file = _host_file(dp, Handle)
    if file is not None and file.delete_on_close:
        others = [h for h, o in dp.handles._handles.items() if o is file and h != (int(Handle) & ~3)]
        if not others:
            filesystem(dp).delete(file.path)
    return _previous["ZwClose"](dp, Handle)


# ---------------------------------------------------------------------------------- read, write

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
    file = _host_file(dp, FileHandle)
    if file is None:
        return _previous["ZwReadFile"](dp, FileHandle, Event, ApcRoutine, ApcContext, IoStatusBlock, Buffer, Length,
                                       ByteOffset, Key)
    if file.directory:
        return STATUS_INVALID_DEVICE_REQUEST
    offset = file.position
    if ByteOffset != 0 and ByteOffset.ptr:
        value = struct.unpack("<q", bytes(dp.read(ByteOffset.ptr, 8)))[0]
        if value >= 0:
            offset = value
    data = filesystem(dp).read(file.path)[offset:offset + int(Length)]
    if not data and int(Length):
        _iosb(dp, IoStatusBlock, STATUS_END_OF_FILE, 0)
        _signal_event(dp, Event)
        return STATUS_END_OF_FILE
    dp.write(Buffer.ptr, data)
    file.position = offset + len(data)
    _iosb(dp, IoStatusBlock, STATUS_SUCCESS, len(data))
    _signal_event(dp, Event)
    return STATUS_SUCCESS


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
    file = _host_file(dp, FileHandle)
    if file is None:
        return _previous["ZwWriteFile"](dp, FileHandle, Event, ApcRoutine, ApcContext, IoStatusBlock, Buffer, Length,
                                        ByteOffset, Key)
    if file.directory:
        return STATUS_INVALID_DEVICE_REQUEST
    fs = filesystem(dp)
    state = fs.materialize(file.path)
    offset = file.position
    if ByteOffset != 0 and ByteOffset.ptr:
        value = struct.unpack("<q", bytes(dp.read(ByteOffset.ptr, 8)))[0]
        if value == -1:  # FILE_WRITE_TO_END_OF_FILE
            offset = len(state.data)
        elif value >= 0:
            offset = value
    if file.access & 0x4 and not file.access & 0x2:  # FILE_APPEND_DATA only
        offset = len(state.data)
    data = bytes(dp.read(Buffer.ptr, int(Length))) if int(Length) else b""
    if offset > len(state.data):
        state.data.extend(bytes(offset - len(state.data)))
    state.data[offset:offset + len(data)] = data
    state.written = True
    state.times[2] = state.times[3] = _filetime(time.time())
    file.position = offset + len(data)
    fs.events.append(dict(kind="write", path=file.path, offset=offset, size=len(data)))
    _iosb(dp, IoStatusBlock, STATUS_SUCCESS, len(data))
    _signal_event(dp, Event)
    return STATUS_SUCCESS


@syscall
def ZwFlushBuffersFile(dp: Dumpulator,
                       FileHandle: Annotated[HANDLE, SAL("_In_")],
                       IoStatusBlock: Annotated[P[IO_STATUS_BLOCK], SAL("_Out_")]
                       ):
    _iosb(dp, IoStatusBlock, STATUS_SUCCESS, 0)
    return STATUS_SUCCESS


# ---------------------------------------------------------------------------------- information

def _times_and_attributes(dp: Dumpulator, path: str) -> Tuple[List[int], int, int, bool]:
    """([creation, access, write, change], attributes, size, directory)."""
    fs = filesystem(dp)
    state, host = fs.lookup(path)
    if state is not None:
        return list(state.times), state.attributes if not state.directory else FILE_ATTRIBUTE_DIRECTORY, len(state.data), state.directory
    if host is None:
        return [0, 0, 0, 0], 0, 0, False
    directory = _stat.S_ISDIR(host.st_mode)
    attributes = getattr(host, "st_file_attributes", 0) or (FILE_ATTRIBUTE_DIRECTORY if directory else FILE_ATTRIBUTE_NORMAL)
    times = [_filetime(host.st_ctime), _filetime(host.st_atime), _filetime(host.st_mtime), _filetime(host.st_mtime)]
    return times, attributes, 0 if directory else host.st_size, directory


def _basic(times, attributes) -> bytes:
    return struct.pack("<QQQQII", *times, attributes, 0)


def _standard(size, directory, delete_pending=False) -> bytes:
    allocation = (size + 0xFFF) & ~0xFFF
    return struct.pack("<QQIBBH", allocation, size, 1, int(delete_pending), int(directory), 0)


def _network(times, attributes, size) -> bytes:
    return struct.pack("<QQQQQQII", *times, (size + 0xFFF) & ~0xFFF, size, attributes, 0)


def _file_information(dp: Dumpulator, file: HostFile, klass: int) -> Optional[bytes]:
    times, attributes, size, directory = _times_and_attributes(dp, file.path)
    name = file.path[2:] if len(file.path) > 1 and file.path[1] == ":" else file.path
    encoded = name.encode("utf-16-le")
    file_id = hash(FileSystem.key(file.path)) & 0x7FFFFFFFFFFFFFFF
    if klass == 4:      # FileBasicInformation
        return _basic(times, attributes)
    if klass == 5:      # FileStandardInformation
        return _standard(size, directory, file.delete_on_close)
    if klass == 6:      # FileInternalInformation
        return struct.pack("<Q", file_id)
    if klass == 7:      # FileEaInformation
        return struct.pack("<I", 0)
    if klass == 8:      # FileAccessInformation
        return struct.pack("<I", file.access)
    if klass == 9:      # FileNameInformation
        return struct.pack("<I", len(encoded)) + encoded
    if klass == 14:     # FilePositionInformation
        return struct.pack("<Q", file.position)
    if klass == 16:     # FileModeInformation
        return struct.pack("<I", 0x20)  # FILE_SYNCHRONOUS_IO_NONALERT
    if klass == 17:     # FileAlignmentInformation
        return struct.pack("<I", 0)
    if klass == 18:     # FileAllInformation
        return (_basic(times, attributes) + _standard(size, directory, file.delete_on_close) + struct.pack("<Q", file_id)
                + struct.pack("<IIQII", 0, file.access, file.position, 0x20, 0) + struct.pack("<I", len(encoded)) + encoded)
    if klass == 34:     # FileNetworkOpenInformation
        return _network(times, attributes, size)
    if klass == 35:     # FileAttributeTagInformation
        return struct.pack("<II", attributes, 0)
    if klass == 51:     # FileIsRemoteDeviceInformation
        return b"\0"
    if klass == 59:     # FileIdInformation: VolumeSerialNumber, FileId (128 bits)
        return struct.pack("<QQQ", 0x46415448, file_id, 0)
    if klass == 68:     # FileStatInformation
        return struct.pack("<QQQQQQQIIII", file_id, *times, (size + 0xFFF) & ~0xFFF, size, attributes, 0, 1, file.access)
    return None


@syscall
def ZwQueryInformationFile(dp: Dumpulator,
                           FileHandle: Annotated[HANDLE, SAL("_In_")],
                           IoStatusBlock: Annotated[P[IO_STATUS_BLOCK], SAL("_Out_")],
                           FileInformation: Annotated[PVOID, SAL("_Out_writes_bytes_(Length)")],
                           Length: Annotated[ULONG, SAL("_In_")],
                           FileInformationClass: Annotated[ULONG, SAL("_In_")]
                           ):
    file = _host_file(dp, FileHandle)
    if file is None:
        return _previous["ZwQueryInformationFile"](dp, FileHandle, IoStatusBlock, FileInformation, Length,
                                                   _enum(FILE_INFORMATION_CLASS, FileInformationClass))
    data = _file_information(dp, file, int(FileInformationClass))
    if data is None:
        return STATUS_INVALID_INFO_CLASS
    length = int(Length)
    if length < len(data):
        if int(FileInformationClass) in (9, 18) and length >= len(data) - len(file.path) * 2:
            dp.write(FileInformation.ptr, data[:length])
            _iosb(dp, IoStatusBlock, STATUS_BUFFER_OVERFLOW, length)
            return STATUS_BUFFER_OVERFLOW
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
    file = _host_file(dp, FileHandle)
    if file is None:
        return _previous["ZwSetInformationFile"](dp, FileHandle, IoStatusBlock, FileInformation, Length,
                                                 _enum(FILE_INFORMATION_CLASS, FileInformationClass))
    fs = filesystem(dp)
    klass = int(FileInformationClass)
    pointer = FileInformation.ptr
    if klass == 14:     # FilePositionInformation
        file.position = struct.unpack("<q", bytes(dp.read(pointer, 8)))[0]
    elif klass in (19, 20):  # FileAllocationInformation, FileEndOfFileInformation
        size = struct.unpack("<q", bytes(dp.read(pointer, 8)))[0]
        state = fs.materialize(file.path)
        if klass == 20 or size < len(state.data):
            del state.data[size:]
            state.data.extend(bytes(max(0, size - len(state.data))))
            state.written = True
    elif klass == 13:   # FileDispositionInformation
        file.delete_on_close = bool(dp.read(pointer, 1)[0])
    elif klass == 64:   # FileDispositionInformationEx
        file.delete_on_close = bool(dp.read_ulong(pointer) & 1)
    elif klass == 4:    # FileBasicInformation
        creation, access, write, change, attributes = struct.unpack("<QQQQI", bytes(dp.read(pointer, 36)))
        state = fs.materialize(file.path)
        for i, value in enumerate((creation, access, write, change)):
            if value not in (0, 0xFFFFFFFFFFFFFFFF):
                state.times[i] = value
        if attributes:
            state.attributes = attributes
    elif klass in (10, 65):  # FileRenameInformation (Ex)
        ptr = dp.ptr_size()
        name_length = dp.read_ulong(pointer + 2 * ptr)
        name = bytes(dp.read(pointer + 2 * ptr + 4, name_length)).decode("utf-16-le")
        target = dos_path_from_name(dp, name, os.path.dirname(file.path))
        if target is None:
            return STATUS_INVALID_PARAMETER
        replace = dp.read(pointer, 1)[0] if klass == 10 else dp.read_ulong(pointer) & 1
        if fs.exists(target) and not replace:
            return STATUS_OBJECT_NAME_COLLISION
        fs.rename(file.path, target)
        file.path = target
    else:
        _iosb(dp, IoStatusBlock, STATUS_SUCCESS, 0)
        return STATUS_SUCCESS  # other information (completion ports, short names...): ignored
    _iosb(dp, IoStatusBlock, STATUS_SUCCESS, 0)
    return STATUS_SUCCESS


def dos_path_from_name(dp: Dumpulator, name: str, directory: str) -> Optional[str]:
    if name.startswith("\\??\\") and len(name) > 6 and name[5] == ":":
        return _redirect(dp, os.path.normpath(name[4:]))
    if "\\" not in name:
        return os.path.join(directory, name)
    return None


@syscall
def ZwQueryAttributesFile(dp: Dumpulator,
                          ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")],
                          FileInformation: Annotated[P[FILE_BASIC_INFORMATION], SAL("_Out_")]
                          ):
    path = dos_path(dp, ObjectAttributes)
    if path is None:
        return STATUS_OBJECT_NAME_NOT_FOUND
    if not filesystem(dp).exists(path):
        return STATUS_OBJECT_NAME_NOT_FOUND
    times, attributes, _, _ = _times_and_attributes(dp, path)
    dp.write(FileInformation.ptr, _basic(times, attributes))
    return STATUS_SUCCESS


@syscall
def ZwQueryFullAttributesFile(dp: Dumpulator,
                              ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")],
                              FileInformation: Annotated[P[FILE_NETWORK_OPEN_INFORMATION], SAL("_Out_")]
                              ):
    path = dos_path(dp, ObjectAttributes)
    if path is None or not filesystem(dp).exists(path):
        return STATUS_OBJECT_NAME_NOT_FOUND
    times, attributes, size, _ = _times_and_attributes(dp, path)
    dp.write(FileInformation.ptr, _network(times, attributes, size))
    return STATUS_SUCCESS


@syscall
def ZwDeleteFile(dp: Dumpulator,
                 ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")]
                 ):
    path = dos_path(dp, ObjectAttributes)
    fs = filesystem(dp)
    if path is None or not fs.exists(path):
        return STATUS_OBJECT_NAME_NOT_FOUND
    fs.delete(path)
    return STATUS_SUCCESS


@syscall
def ZwQueryVolumeInformationFile(dp: Dumpulator,
                                 FileHandle: Annotated[HANDLE, SAL("_In_")],
                                 IoStatusBlock: Annotated[P[IO_STATUS_BLOCK], SAL("_Out_")],
                                 FsInformation: Annotated[PVOID, SAL("_Out_writes_bytes_(Length)")],
                                 Length: Annotated[ULONG, SAL("_In_")],
                                 FsInformationClass: Annotated[ULONG, SAL("_In_")]
                                 ):
    file = _host_file(dp, FileHandle)
    if file is None:
        return _previous["ZwQueryVolumeInformationFile"](dp, FileHandle, IoStatusBlock, FsInformation, Length,
                                                         _enum(FSINFOCLASS, FsInformationClass))
    klass = int(FsInformationClass)
    if klass == 1:      # FileFsVolumeInformation
        label = "Windows".encode("utf-16-le")
        data = struct.pack("<QIIBB", 0, 0x46415448, len(label), 0, 0) + label
    elif klass == 3:    # FileFsSizeInformation
        data = struct.pack("<QQII", 0x4000000, 0x2000000, 8, 512)
    elif klass == 4:    # FileFsDeviceInformation
        data = struct.pack("<II", 7, 0)  # FILE_DEVICE_DISK
    elif klass == 5:    # FileFsAttributeInformation
        name = "NTFS".encode("utf-16-le")
        data = struct.pack("<IiI", 0x00E700FF, 255, len(name)) + name
    elif klass == 7:    # FileFsFullSizeInformation
        data = struct.pack("<QQQII", 0x4000000, 0x2000000, 0x2000000, 8, 512)
    elif klass == 11:   # FileFsSectorSizeInformation
        data = struct.pack("<IIIIIII", 512, 4096, 4096, 4096, 0, 0, 0)
    else:
        return STATUS_INVALID_INFO_CLASS
    length = int(Length)
    if length < len(data):
        if klass in (1, 5) and length >= len(data) - 16:
            dp.write(FsInformation.ptr, data[:length])
            _iosb(dp, IoStatusBlock, STATUS_BUFFER_OVERFLOW, length)
            return STATUS_BUFFER_OVERFLOW
        return STATUS_INFO_LENGTH_MISMATCH
    dp.write(FsInformation.ptr, data)
    _iosb(dp, IoStatusBlock, STATUS_SUCCESS, len(data))
    return STATUS_SUCCESS


# ---------------------------------------------------------------------------------- directories

def _matches(name: str, pattern: str) -> bool:
    pattern = pattern.replace("<", "*").replace(">", "?").replace('"', ".")
    if pattern in ("*", "*.*"):
        return True
    return fnmatch.fnmatch(name.lower(), pattern.lower())


def _directory_entry(dp: Dumpulator, klass: int, directory: str, name: str, index: int) -> Optional[bytes]:
    path = directory if name in (".", "..") else os.path.join(directory, name)
    times, attributes, size, is_dir = _times_and_attributes(dp, path)
    if name in (".", ".."):
        attributes, size = FILE_ATTRIBUTE_DIRECTORY, 0
    encoded = name.encode("utf-16-le")
    common = struct.pack("<IIQQQQQQII", 0, index, *times, size, (size + 0xFFF) & ~0xFFF, attributes, len(encoded))
    file_id = hash(FileSystem.key(path)) & 0x7FFFFFFFFFFFFFFF
    if klass == 1:      # FileDirectoryInformation
        return common + encoded
    if klass == 2:      # FileFullDirectoryInformation
        return common + struct.pack("<I", 0) + encoded
    if klass == 3:      # FileBothDirectoryInformation
        return common + struct.pack("<IBB", 0, 0, 0) + bytes(24) + encoded
    if klass == 37:     # FileIdBothDirectoryInformation
        return common + struct.pack("<IBB", 0, 0, 0) + bytes(24) + struct.pack("<HQ", 0, file_id) + encoded
    if klass == 38:     # FileIdFullDirectoryInformation
        return common + struct.pack("<IIQ", 0, 0, file_id) + encoded
    if klass == 12:     # FileNamesInformation
        return struct.pack("<III", 0, index, len(encoded)) + encoded
    return None


def query_directory(dp: Dumpulator, FileHandle, Event, IoStatusBlock, FileInformation, Length, klass: int,
                    single: bool, FileName, restart: bool) -> Optional[int]:
    file = _host_file(dp, FileHandle)
    if file is None:
        return None
    if not file.directory:
        return STATUS_INVALID_PARAMETER
    first = file.listing is None
    if first or restart:
        if FileName != 0 and FileName.ptr and FileName[0].Length:
            file.pattern = FileName[0].read_str()
        names = filesystem(dp).listing(file.path)
        if len(file.path) > 3:
            names = [".", ".."] + names
        file.listing = [n for n in names if _matches(n, file.pattern)]
        file.listing_index = 0
    pointer, length = FileInformation.ptr, int(Length)
    written, previous = 0, None
    while file.listing_index < len(file.listing):
        entry = _directory_entry(dp, klass, file.path, file.listing[file.listing_index], file.listing_index)
        if entry is None:
            return STATUS_INVALID_INFO_CLASS
        offset = (written + 7) & ~7
        if offset + len(entry) > length:
            if written == 0:
                return STATUS_BUFFER_OVERFLOW
            break
        dp.write(pointer + offset, entry)
        if previous is not None:
            dp.write_ulong(pointer + previous, offset - previous)
        previous, written = offset, offset + len(entry)
        file.listing_index += 1
        if single:
            break
    if written == 0:
        status = STATUS_NO_SUCH_FILE if (first or restart) else STATUS_NO_MORE_FILES
        _iosb(dp, IoStatusBlock, status, 0)
        return status
    _iosb(dp, IoStatusBlock, STATUS_SUCCESS, written)
    _signal_event(dp, Event)
    return STATUS_SUCCESS


@syscall
def ZwQueryDirectoryFile(dp: Dumpulator,
                         FileHandle: Annotated[HANDLE, SAL("_In_")],
                         Event: Annotated[HANDLE, SAL("_In_opt_")],
                         ApcRoutine: Annotated[P[IO_APC_ROUTINE], SAL("_In_opt_")],
                         ApcContext: Annotated[PVOID, SAL("_In_opt_")],
                         IoStatusBlock: Annotated[P[IO_STATUS_BLOCK], SAL("_Out_")],
                         FileInformation: Annotated[PVOID, SAL("_Out_writes_bytes_(Length)")],
                         Length: Annotated[ULONG, SAL("_In_")],
                         FileInformationClass: Annotated[ULONG, SAL("_In_")],
                         ReturnSingleEntry: Annotated[BOOLEAN, SAL("_In_")],
                         FileName: Annotated[P[UNICODE_STRING], SAL("_In_opt_")],
                         RestartScan: Annotated[BOOLEAN, SAL("_In_")]
                         ):
    status = query_directory(dp, FileHandle, Event, IoStatusBlock, FileInformation, Length, int(FileInformationClass),
                             bool(ReturnSingleEntry), FileName, bool(RestartScan))
    return STATUS_INVALID_HANDLE if status is None else status


@syscall
def ZwQueryDirectoryFileEx(dp: Dumpulator,
                           FileHandle: Annotated[HANDLE, SAL("_In_")],
                           Event: Annotated[HANDLE, SAL("_In_opt_")],
                           ApcRoutine: Annotated[P[IO_APC_ROUTINE], SAL("_In_opt_")],
                           ApcContext: Annotated[PVOID, SAL("_In_opt_")],
                           IoStatusBlock: Annotated[P[IO_STATUS_BLOCK], SAL("_Out_")],
                           FileInformation: Annotated[PVOID, SAL("_Out_writes_bytes_(Length)")],
                           Length: Annotated[ULONG, SAL("_In_")],
                           FileInformationClass: Annotated[ULONG, SAL("_In_")],
                           QueryFlags: Annotated[ULONG, SAL("_In_")],
                           FileName: Annotated[P[UNICODE_STRING], SAL("_In_opt_")]
                           ):
    flags = int(QueryFlags)
    status = query_directory(dp, FileHandle, Event, IoStatusBlock, FileInformation, Length, int(FileInformationClass),
                             bool(flags & 2), FileName, bool(flags & 1))
    return STATUS_INVALID_HANDLE if status is None else status


# ---------------------------------------------------------------------------------- sections of files

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
    file = _host_file(dp, FileHandle)
    if file is not None and int(AllocationAttributes) & 0x1000000:  # SEC_IMAGE: a DLL or an executable
        from .fathom_nt import ImageSectionObject
        SectionHandle.write_ptr(dp.handles.new(ImageSectionObject(file.path, filesystem(dp).read(file.path))))
        return STATUS_SUCCESS
    return _previous["ZwCreateSection"](dp, SectionHandle, DesiredAccess, ObjectAttributes, MaximumSize,
                                        SectionPageProtection, AllocationAttributes, FileHandle)
