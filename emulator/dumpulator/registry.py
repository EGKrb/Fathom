# Fathom addition to dumpulator: the registry of the emulated program, read from the host's
# registry. Boost Software License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# A key the program opens is read from the host (read-only: advapi32 on the host, with the WoW64
# view of a 32-bit program); what the program writes, creates or deletes stays in the emulator (an
# overlay shared by the emulated processes of a run), so the host's registry never changes.
# \Registry\Machine is HKEY_LOCAL_MACHINE, \Registry\User\<the emulated user's SID> is
# HKEY_CURRENT_USER (and ..._Classes its Software\Classes), \Registry\User\.DEFAULT and the other
# SIDs are HKEY_USERS. The keys dumpulator sets up (dp.handles.create_key) come first.

import ctypes
import struct
from ctypes import wintypes
from typing import Annotated, Dict, List, Optional, Tuple

from .dumpulator import Dumpulator
from .handles import AbstractObject, RegistryKeyObject
from .native import *
from .ntsyscalls import syscall

STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
STATUS_BUFFER_OVERFLOW = 0x80000005
STATUS_BUFFER_TOO_SMALL = 0xC0000023
STATUS_NO_MORE_ENTRIES = 0x8000001A
STATUS_INVALID_INFO_CLASS = 0xC0000003
STATUS_KEY_DELETED = 0xC000017C
STATUS_INVALID_HANDLE = 0xC0000008
STATUS_PENDING = 0x103

HKEY_CLASSES_ROOT, HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, HKEY_USERS = (0x80000000, 0x80000001, 0x80000002, 0x80000003)
KEY_READ, KEY_WOW64_64KEY, KEY_WOW64_32KEY = 0x20019, 0x100, 0x200

_advapi = ctypes.WinDLL("advapi32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


class HostRegistry:
    """Raw reads of the host's registry (bytes and types as stored)."""

    def __init__(self, x64: bool):
        self.view = KEY_WOW64_64KEY if x64 else KEY_WOW64_32KEY

    def _open(self, root: int, path: str) -> Optional[int]:
        if _advapi is None:
            return None
        key = wintypes.HKEY()
        result = _advapi.RegOpenKeyExW(wintypes.HKEY(root), path or None, 0, KEY_READ | self.view, ctypes.byref(key))
        return key.value if result == 0 else None

    def _close(self, key: int):
        _advapi.RegCloseKey(wintypes.HKEY(key))

    def exists(self, root: int, path: str) -> bool:
        key = self._open(root, path)
        if key is None:
            return False
        self._close(key)
        return True

    def read(self, root: int, path: str):
        """(subkey names, {value name lower: (name, type, data)}, last write time) or None."""
        key = self._open(root, path)
        if key is None:
            return None
        try:
            subkeys: List[str] = []
            name = ctypes.create_unicode_buffer(512)
            index = 0
            while True:
                size = wintypes.DWORD(512)
                if _advapi.RegEnumKeyExW(wintypes.HKEY(key), index, name, ctypes.byref(size), None, None, None, None) != 0:
                    break
                subkeys.append(name.value)
                index += 1
            values: Dict[str, Tuple[str, int, bytes]] = {}
            index = 0
            data = ctypes.create_string_buffer(0x10000)
            while True:
                size = wintypes.DWORD(16384)
                kind = wintypes.DWORD()
                length = wintypes.DWORD(len(data))
                result = _advapi.RegEnumValueW(wintypes.HKEY(key), index, name, ctypes.byref(size), None, ctypes.byref(kind),
                                               data, ctypes.byref(length))
                if result == 234:  # ERROR_MORE_DATA: a larger buffer
                    data = ctypes.create_string_buffer(length.value + 16)
                    continue
                if result != 0:
                    break
                values[name.value.lower()] = (name.value, kind.value, data.raw[:length.value])
                index += 1
            written = _FILETIME()
            _advapi.RegQueryInfoKeyW(wintypes.HKEY(key), None, None, None, None, None, None, None, None, None, None,
                                     ctypes.byref(written))
            return subkeys, values, (written.high << 32) | written.low
        finally:
            self._close(key)


class KeyState:
    """The emulator's changes to a key."""

    def __init__(self):
        self.created = False
        self.deleted = False
        self.values: Dict[str, Tuple[str, int, bytes]] = {}
        self.deleted_values: set = set()
        self.subkeys: Dict[str, str] = {}       # created subkeys: lower -> name
        self.deleted_subkeys: set = set()


class Registry:
    """The registry of a run: the host's, under the changes of the emulated processes."""

    def __init__(self, x64: bool):
        self.host = HostRegistry(x64)
        self.changes: Dict[str, KeyState] = {}
        self.events: List[dict] = []

    @staticmethod
    def normalize(path: str) -> str:
        parts = [p for p in path.replace("/", "\\").split("\\") if p]
        return "\\" + "\\".join(parts)

    def host_location(self, path: str) -> Optional[Tuple[int, str]]:
        parts = [p for p in path.split("\\") if p]
        if len(parts) < 2 or parts[0].lower() != "registry":
            return None
        hive = parts[1].lower()
        rest = parts[2:]
        if hive == "machine":
            return HKEY_LOCAL_MACHINE, "\\".join(rest)
        if hive == "user":
            if not rest:
                return HKEY_USERS, ""
            sid = rest[0]
            if sid.lower() == ".default" or sid in ("S-1-5-18", "S-1-5-19", "S-1-5-20"):
                return HKEY_USERS, "\\".join(rest)
            if sid.lower().endswith("_classes"):
                return HKEY_CURRENT_USER, "\\".join(["Software", "Classes"] + rest[1:])
            return HKEY_CURRENT_USER, "\\".join(rest[1:])
        return None

    def state(self, path: str, create: bool = False) -> Optional[KeyState]:
        key = path.lower()
        state = self.changes.get(key)
        if state is None and create:
            state = self.changes[key] = KeyState()
        return state

    def exists(self, path: str) -> bool:
        path = self.normalize(path)
        if path.lower() in ("\\registry", "\\registry\\machine", "\\registry\\user"):
            return True
        # a deleted ancestor hides the key
        parts = path.split("\\")
        for i in range(2, len(parts) + 1):
            ancestor = self.state("\\".join(parts[:i]))
            if ancestor is not None and ancestor.deleted and not ancestor.created:
                return False
        state = self.state(path)
        if state is not None:
            if state.deleted:
                return False
            if state.created:
                return True
        location = self.host_location(path)
        return location is not None and self.host.exists(*location)

    def create(self, path: str) -> bool:
        """Create the key (and its missing parents); True when it is new."""
        path = self.normalize(path)
        if self.exists(path):
            return False
        parts = path.split("\\")
        for i in range(3, len(parts) + 1):
            sub = "\\".join(parts[:i])
            if not self.exists(sub):
                state = self.state(sub, True)
                state.created, state.deleted = True, False
                parent = self.state("\\".join(parts[:i - 1]), True)
                parent.subkeys[parts[i - 1].lower()] = parts[i - 1]
                parent.deleted_subkeys.discard(parts[i - 1].lower())
        self.events.append(dict(kind="create_key", key=path))
        return True

    def contents(self, path: str):
        """(subkey names, values {lower: (name, type, data)}, last write time) with the changes."""
        path = self.normalize(path)
        state = self.state(path)
        subkeys: List[str] = []
        values: Dict[str, Tuple[str, int, bytes]] = {}
        written = 0
        if state is None or not state.created:
            location = self.host_location(path)
            host = self.host.read(*location) if location is not None else None
            if host is not None:
                subkeys, values, written = list(host[0]), dict(host[1]), host[2]
        if state is not None:
            subkeys = [s for s in subkeys if s.lower() not in state.deleted_subkeys]
            known = {s.lower() for s in subkeys}
            subkeys += [name for lower, name in state.subkeys.items() if lower not in known]
            for lower in state.deleted_values:
                values.pop(lower, None)
            values.update(state.values)
        return subkeys, values, written

    def set_value(self, path: str, name: str, kind: int, data: bytes):
        state = self.state(self.normalize(path), True)
        state.values[name.lower()] = (name, kind, data)
        state.deleted_values.discard(name.lower())
        self.events.append(dict(kind="set_value", key=self.normalize(path), name=name, type=kind,
                                data=_printable(kind, data)))

    def delete_value(self, path: str, name: str) -> bool:
        subkeys, values, _ = self.contents(path)
        if name.lower() not in values:
            return False
        state = self.state(self.normalize(path), True)
        state.values.pop(name.lower(), None)
        state.deleted_values.add(name.lower())
        self.events.append(dict(kind="delete_value", key=self.normalize(path), name=name))
        return True

    def delete_key(self, path: str):
        path = self.normalize(path)
        state = self.state(path, True)
        state.deleted, state.created = True, False
        state.values.clear()
        parts = path.split("\\")
        parent = self.state("\\".join(parts[:-1]), True)
        parent.subkeys.pop(parts[-1].lower(), None)
        parent.deleted_subkeys.add(parts[-1].lower())
        self.events.append(dict(kind="delete_key", key=path))


def _printable(kind: int, data: bytes):
    try:
        if kind in (1, 2):  # REG_SZ, REG_EXPAND_SZ
            return data.decode("utf-16-le").rstrip("\0")
        if kind == 7:  # REG_MULTI_SZ
            return [s for s in data.decode("utf-16-le").split("\0") if s]
        if kind == 4 and len(data) >= 4:
            return struct.unpack_from("<I", data)[0]
        if kind == 11 and len(data) >= 8:
            return struct.unpack_from("<Q", data)[0]
    except Exception:
        pass
    return data[:64].hex()


class KeyObject(RegistryKeyObject):
    """A key handle of the emulated program (its full NT name in `key`)."""

    def __init__(self, key: str):
        super().__init__(key, {})

    def __str__(self):
        return f"Key({self.key})"


def registry(dp: Dumpulator) -> Registry:
    """The registry of the run (shared with the child processes)."""
    world = getattr(dp, "world", None)
    holder = world if world is not None else dp
    if getattr(holder, "registry", None) is None:
        holder.registry = Registry(dp._x64)
        # the keys dumpulator set up
        for name, data in list(dp.handles._mapped_files.items()):
            if isinstance(data, RegistryKeyObject) and not isinstance(data, KeyObject):
                path = Registry.normalize(data.key)
                state = holder.registry.state(path, True)
                state.created = True
                for value_name, value in data.values.items():
                    if isinstance(value, str):
                        state.values[value_name.lower()] = (value_name, 1, (value + "\0").encode("utf-16-le"))
                    elif isinstance(value, int):
                        state.values[value_name.lower()] = (value_name, 4, struct.pack("<I", value & 0xFFFFFFFF))
                    else:
                        state.values[value_name.lower()] = (value_name, 3, bytes(value))
    return holder.registry


def _key_path(dp: Dumpulator, attributes) -> str:
    attrs = attributes[0]
    name = attrs.ObjectName[0].read_str() if attrs.ObjectName != 0 else ""
    root = int(attrs.RootDirectory)
    if root:
        parent = dp.handles.get(root, None) if dp.handles.valid(root) else None
        if isinstance(parent, RegistryKeyObject):
            name = parent.key.rstrip("\\") + ("\\" + name if name else "")
    return Registry.normalize(name)


def _key(dp: Dumpulator, handle) -> Optional[RegistryKeyObject]:
    handle = int(handle)
    obj = dp.handles.get(handle, None) if dp.handles.valid(handle) else None
    return obj if isinstance(obj, RegistryKeyObject) else None


def _unicode(pointer) -> str:
    if pointer == 0:
        return ""
    value = pointer[0]
    if value.Length == 0 or value.Buffer == 0:
        return ""
    return value.read_str()


def _write_result(dp, buffer: int, length: int, result_length, data: bytes, fixed: int) -> int:
    """Write an information structure: STATUS_BUFFER_TOO_SMALL when even its fixed part does not fit,
    STATUS_BUFFER_OVERFLOW (the fixed part written) when the rest does not."""
    if result_length != 0:
        result_length.write_ulong(len(data))
    if length < fixed:
        return STATUS_BUFFER_TOO_SMALL
    if length < len(data):
        dp.write(buffer, data[:length])
        return STATUS_BUFFER_OVERFLOW
    dp.write(buffer, data)
    return STATUS_SUCCESS


def _key_information(klass: int, name: str, full_name: str, subkeys, values, written) -> Optional[Tuple[bytes, int]]:
    encoded = name.encode("utf-16-le")
    if klass == 0:  # KeyBasicInformation
        return struct.pack("<QII", written, 0, len(encoded)) + encoded, 16
    if klass == 1:  # KeyNodeInformation
        return struct.pack("<QIIII", written, 0, 0xFFFFFFFF, 0, len(encoded)) + encoded, 24
    max_name = max([len(s) * 2 for s in subkeys] or [0])
    max_value_name = max([len(v[0]) * 2 for v in values.values()] or [0])
    max_value_data = max([len(v[2]) for v in values.values()] or [0])
    if klass == 2:  # KeyFullInformation
        return struct.pack("<QIIIIIIIIII", written, 0, 44, 0, len(subkeys), max_name, 0, len(values),
                           max_value_name, max_value_data, 0), 44
    if klass == 3:  # KeyNameInformation
        full = full_name.encode("utf-16-le")
        return struct.pack("<I", len(full)) + full, 4
    if klass == 4:  # KeyCachedInformation
        return struct.pack("<QIIIIIII", written, 0, len(subkeys), max_name, len(values), max_value_name,
                           max_value_data, len(encoded)), 40
    if klass == 5:  # KeyFlagsInformation
        return struct.pack("<III", 0, 0, 0), 12
    if klass == 6:  # KeyVirtualizationInformation
        return struct.pack("<I", 0), 4
    if klass == 7:  # KeyHandleTagsInformation
        return struct.pack("<I", 0), 4
    return None


def _value_information(klass: int, name: str, kind: int, data: bytes) -> Optional[Tuple[bytes, int]]:
    encoded = name.encode("utf-16-le")
    if klass == 0:  # KeyValueBasicInformation
        return struct.pack("<III", 0, kind, len(encoded)) + encoded, 12
    if klass in (1, 3):  # KeyValueFullInformation (Align64)
        offset = 20 + len(encoded)
        offset = (offset + (7 if klass == 3 else 3)) & ~(7 if klass == 3 else 3)
        head = struct.pack("<IIIII", 0, kind, offset, len(data), len(encoded)) + encoded
        return head + bytes(offset - len(head)) + data, 20
    if klass == 2:  # KeyValuePartialInformation
        return struct.pack("<III", 0, kind, len(data)) + data, 12
    if klass == 4:  # KeyValuePartialInformationAlign64
        return struct.pack("<II", kind, len(data)) + data, 8
    return None


def open_key(dp: Dumpulator, KeyHandle, ObjectAttributes, create: bool = False, Disposition=0) -> int:
    path = _key_path(dp, ObjectAttributes)
    reg = registry(dp)
    if create:
        new = reg.create(path)
        if Disposition != 0:
            Disposition.write_ulong(1 if new else 2)  # REG_CREATED_NEW_KEY / REG_OPENED_EXISTING_KEY
    elif not reg.exists(path):
        dp.info(f"registry key not found: {path}")
        return STATUS_OBJECT_NAME_NOT_FOUND
    KeyHandle.write_ptr(dp.handles.new(KeyObject(path)))
    return STATUS_SUCCESS


@syscall
def ZwOpenKey(dp: Dumpulator,
              KeyHandle: Annotated[P[HANDLE], SAL("_Out_")],
              DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
              ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")]
              ):
    return open_key(dp, KeyHandle, ObjectAttributes)


@syscall
def ZwOpenKeyEx(dp: Dumpulator,
                KeyHandle: Annotated[P[HANDLE], SAL("_Out_")],
                DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")],
                OpenOptions: Annotated[ULONG, SAL("_In_")]
                ):
    return open_key(dp, KeyHandle, ObjectAttributes)


@syscall
def ZwCreateKey(dp: Dumpulator,
                KeyHandle: Annotated[P[HANDLE], SAL("_Out_")],
                DesiredAccess: Annotated[ACCESS_MASK, SAL("_In_")],
                ObjectAttributes: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_")],
                TitleIndex: Annotated[ULONG, SAL("_Reserved_")],
                Class: Annotated[P[UNICODE_STRING], SAL("_In_opt_")],
                CreateOptions: Annotated[ULONG, SAL("_In_")],
                Disposition: Annotated[P[ULONG], SAL("_Out_opt_")]
                ):
    return open_key(dp, KeyHandle, ObjectAttributes, True, Disposition)


@syscall
def ZwQueryKey(dp: Dumpulator,
               KeyHandle: Annotated[HANDLE, SAL("_In_")],
               KeyInformationClass: Annotated[ULONG, SAL("_In_")],
               KeyInformation: Annotated[PVOID, SAL("_Out_writes_bytes_opt_(Length)")],
               Length: Annotated[ULONG, SAL("_In_")],
               ResultLength: Annotated[P[ULONG], SAL("_Out_")]
               ):
    key = _key(dp, KeyHandle)
    if key is None:
        return STATUS_INVALID_HANDLE
    reg = registry(dp)
    if not reg.exists(key.key):
        return STATUS_KEY_DELETED
    subkeys, values, written = reg.contents(key.key)
    info = _key_information(int(KeyInformationClass), key.key.split("\\")[-1], key.key, subkeys, values, written)
    if info is None:
        return STATUS_INVALID_INFO_CLASS
    return _write_result(dp, KeyInformation.ptr, int(Length), ResultLength, *info)


@syscall
def ZwEnumerateKey(dp: Dumpulator,
                   KeyHandle: Annotated[HANDLE, SAL("_In_")],
                   Index: Annotated[ULONG, SAL("_In_")],
                   KeyInformationClass: Annotated[ULONG, SAL("_In_")],
                   KeyInformation: Annotated[PVOID, SAL("_Out_writes_bytes_opt_(Length)")],
                   Length: Annotated[ULONG, SAL("_In_")],
                   ResultLength: Annotated[P[ULONG], SAL("_Out_")]
                   ):
    key = _key(dp, KeyHandle)
    if key is None:
        return STATUS_INVALID_HANDLE
    reg = registry(dp)
    subkeys, _, _ = reg.contents(key.key)
    index = int(Index)
    if index >= len(subkeys):
        return STATUS_NO_MORE_ENTRIES
    child = key.key + "\\" + subkeys[index]
    child_subkeys, child_values, written = reg.contents(child)
    info = _key_information(int(KeyInformationClass), subkeys[index], child, child_subkeys, child_values, written)
    if info is None:
        return STATUS_INVALID_INFO_CLASS
    return _write_result(dp, KeyInformation.ptr, int(Length), ResultLength, *info)


@syscall
def ZwQueryValueKey(dp: Dumpulator,
                    KeyHandle: Annotated[HANDLE, SAL("_In_")],
                    ValueName: Annotated[P[UNICODE_STRING], SAL("_In_")],
                    KeyValueInformationClass: Annotated[ULONG, SAL("_In_")],
                    KeyValueInformation: Annotated[PVOID, SAL("_Out_writes_bytes_opt_(Length)")],
                    Length: Annotated[ULONG, SAL("_In_")],
                    ResultLength: Annotated[P[ULONG], SAL("_Out_")]
                    ):
    key = _key(dp, KeyHandle)
    if key is None:
        return STATUS_INVALID_HANDLE
    name = _unicode(ValueName)
    _, values, _ = registry(dp).contents(key.key)
    value = values.get(name.lower())
    if value is None:
        return STATUS_OBJECT_NAME_NOT_FOUND
    info = _value_information(int(KeyValueInformationClass), value[0], value[1], value[2])
    if info is None:
        return STATUS_INVALID_INFO_CLASS
    return _write_result(dp, KeyValueInformation.ptr, int(Length), ResultLength, *info)


@syscall
def ZwEnumerateValueKey(dp: Dumpulator,
                        KeyHandle: Annotated[HANDLE, SAL("_In_")],
                        Index: Annotated[ULONG, SAL("_In_")],
                        KeyValueInformationClass: Annotated[ULONG, SAL("_In_")],
                        KeyValueInformation: Annotated[PVOID, SAL("_Out_writes_bytes_opt_(Length)")],
                        Length: Annotated[ULONG, SAL("_In_")],
                        ResultLength: Annotated[P[ULONG], SAL("_Out_")]
                        ):
    key = _key(dp, KeyHandle)
    if key is None:
        return STATUS_INVALID_HANDLE
    _, values, _ = registry(dp).contents(key.key)
    ordered = list(values.values())
    index = int(Index)
    if index >= len(ordered):
        return STATUS_NO_MORE_ENTRIES
    info = _value_information(int(KeyValueInformationClass), *ordered[index])
    if info is None:
        return STATUS_INVALID_INFO_CLASS
    return _write_result(dp, KeyValueInformation.ptr, int(Length), ResultLength, *info)


@syscall
def ZwSetValueKey(dp: Dumpulator,
                  KeyHandle: Annotated[HANDLE, SAL("_In_")],
                  ValueName: Annotated[P[UNICODE_STRING], SAL("_In_")],
                  TitleIndex: Annotated[ULONG, SAL("_In_opt_")],
                  Type: Annotated[ULONG, SAL("_In_")],
                  Data: Annotated[PVOID, SAL("_In_reads_bytes_opt_(DataSize)")],
                  DataSize: Annotated[ULONG, SAL("_In_")]
                  ):
    key = _key(dp, KeyHandle)
    if key is None:
        return STATUS_INVALID_HANDLE
    size = int(DataSize)
    data = bytes(dp.read(Data.ptr, size)) if size else b""
    registry(dp).set_value(key.key, _unicode(ValueName), int(Type), data)
    return STATUS_SUCCESS


@syscall
def ZwDeleteValueKey(dp: Dumpulator,
                     KeyHandle: Annotated[HANDLE, SAL("_In_")],
                     ValueName: Annotated[P[UNICODE_STRING], SAL("_In_")]
                     ):
    key = _key(dp, KeyHandle)
    if key is None:
        return STATUS_INVALID_HANDLE
    return STATUS_SUCCESS if registry(dp).delete_value(key.key, _unicode(ValueName)) else STATUS_OBJECT_NAME_NOT_FOUND


@syscall
def ZwDeleteKey(dp: Dumpulator,
                KeyHandle: Annotated[HANDLE, SAL("_In_")]
                ):
    key = _key(dp, KeyHandle)
    if key is None:
        return STATUS_INVALID_HANDLE
    registry(dp).delete_key(key.key)
    return STATUS_SUCCESS


@syscall
def ZwFlushKey(dp: Dumpulator,
               KeyHandle: Annotated[HANDLE, SAL("_In_")]
               ):
    return STATUS_SUCCESS if _key(dp, KeyHandle) is not None else STATUS_INVALID_HANDLE


@syscall
def ZwNotifyChangeKey(dp: Dumpulator,
                      KeyHandle: Annotated[HANDLE, SAL("_In_")],
                      Event: Annotated[HANDLE, SAL("_In_opt_")],
                      ApcRoutine: Annotated[P[IO_APC_ROUTINE], SAL("_In_opt_")],
                      ApcContext: Annotated[PVOID, SAL("_In_opt_")],
                      IoStatusBlock: Annotated[P[IO_STATUS_BLOCK], SAL("_Out_")],
                      CompletionFilter: Annotated[ULONG, SAL("_In_")],
                      WatchTree: Annotated[BOOLEAN, SAL("_In_")],
                      Buffer: Annotated[PVOID, SAL("_Out_writes_bytes_opt_(BufferSize)")],
                      BufferSize: Annotated[ULONG, SAL("_In_")],
                      Asynchronous: Annotated[BOOLEAN, SAL("_In_")]
                      ):
    # nothing else changes the registry of the emulator: the notification stays pending
    return STATUS_PENDING


@syscall
def ZwNotifyChangeMultipleKeys(dp: Dumpulator,
                               MasterKeyHandle: Annotated[HANDLE, SAL("_In_")],
                               Count: Annotated[ULONG, SAL("_In_opt_")],
                               SubordinateObjects: Annotated[P[OBJECT_ATTRIBUTES], SAL("_In_reads_opt_(Count)")],
                               Event: Annotated[HANDLE, SAL("_In_opt_")],
                               ApcRoutine: Annotated[P[IO_APC_ROUTINE], SAL("_In_opt_")],
                               ApcContext: Annotated[PVOID, SAL("_In_opt_")],
                               IoStatusBlock: Annotated[P[IO_STATUS_BLOCK], SAL("_Out_")],
                               CompletionFilter: Annotated[ULONG, SAL("_In_")],
                               WatchTree: Annotated[BOOLEAN, SAL("_In_")],
                               Buffer: Annotated[PVOID, SAL("_Out_writes_bytes_opt_(BufferSize)")],
                               BufferSize: Annotated[ULONG, SAL("_In_")],
                               Asynchronous: Annotated[BOOLEAN, SAL("_In_")]
                               ):
    return STATUS_PENDING


@syscall
def ZwQueryMultipleValueKey(dp: Dumpulator,
                            KeyHandle: Annotated[HANDLE, SAL("_In_")],
                            ValueEntries: Annotated[PVOID, SAL("_Inout_updates_(EntryCount)")],
                            EntryCount: Annotated[ULONG, SAL("_In_")],
                            ValueBuffer: Annotated[PVOID, SAL("_Out_writes_bytes_(*BufferLength)")],
                            BufferLength: Annotated[P[ULONG], SAL("_Inout_")],
                            RequiredBufferLength: Annotated[P[ULONG], SAL("_Out_opt_")]
                            ):
    # KEY_VALUE_ENTRY {PUNICODE_STRING ValueName, DataLength, DataOffset, Type}
    key = _key(dp, KeyHandle)
    if key is None:
        return STATUS_INVALID_HANDLE
    _, values, _ = registry(dp).contents(key.key)
    ptr = dp.ptr_size()
    size = ptr + 12 + (4 if ptr == 8 else 0)
    found = []
    for i in range(int(EntryCount)):
        entry = ValueEntries.ptr + i * size
        name_ptr = dp.read_ptr(entry)
        length = struct.unpack("<H", bytes(dp.read(name_ptr, 2)))[0]
        buffer = dp.read_ptr(name_ptr + ptr)
        name = bytes(dp.read(buffer, length)).decode("utf-16-le") if length else ""
        value = values.get(name.lower())
        if value is None:
            return STATUS_OBJECT_NAME_NOT_FOUND
        found.append((entry, value))
    available = dp.read_ulong(BufferLength.ptr)
    offset = 0
    for entry, value in found:
        offset = (offset + 3) & ~3
        dp.write(entry + ptr, struct.pack("<III", len(value[2]), offset, value[1]))
        if offset + len(value[2]) <= available:
            dp.write(ValueBuffer.ptr + offset, value[2])
        offset += len(value[2])
    if RequiredBufferLength != 0:
        RequiredBufferLength.write_ulong(offset)
    if offset > available:
        return STATUS_BUFFER_OVERFLOW
    BufferLength.write_ulong(offset)
    return STATUS_SUCCESS
