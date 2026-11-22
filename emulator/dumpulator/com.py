# Fathom addition to dumpulator: COM, RPC and the service manager of the emulated program. Boost
# Software License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# - COM objects of an in-process server run in the emulator: CoCreateInstance(Ex) / CoGetClassObject
#   read the class's InprocServer32 in the registry (the host's, with what the program registered),
#   load the DLL with the emulated loader (LoadLibraryExW: DllMain runs), and call its
#   DllGetClassObject, then IClassFactory::CreateInstance: the object's own code runs, as natively.
#   CoInitialize(Ex) / OleInitialize succeed. A class served by another process (LocalServer32,
#   a service) gets CO_E_SERVER_EXEC_FAILURE, as when the server cannot start; it is recorded.
# - RPC to other processes (NdrClientCall2/3/4, the stubs of MIDL): RPC_S_SERVER_UNAVAILABLE, as when
#   the service does not answer; each call is recorded with its interface and procedure number.
# - The service manager (OpenSCManager, CreateService, OpenService, StartService, ControlService,
#   DeleteService, QueryServiceStatus, ChangeServiceConfig) is emulated: the services a program
#   creates are in the emulator's registry (HKLM\SYSTEM\CurrentControlSet\Services) and in the
#   events; starting one is recorded (its program does not run).

import os
import struct
import uuid
from typing import Dict, List, Optional

from .apihooks import Invoke, read_string, set_last_error, write_string

S_OK, S_FALSE = 0, 1
REGDB_E_CLASSNOTREG = 0x80040154
CO_E_SERVER_EXEC_FAILURE = 0x80080005
CO_E_DLLNOTFOUND = 0x800401F8
CO_E_ERRORINDLL = 0x800401F9
CLASS_E_CLASSNOTAVAILABLE = 0x80040111
RPC_S_SERVER_UNAVAILABLE = 1722
IID_ICLASSFACTORY = uuid.UUID("00000001-0000-0000-C000-000000000046")


def _guid(dp, address: int) -> str:
    return "{" + str(uuid.UUID(bytes_le=bytes(dp.read(address, 16)))).upper() + "}"


class Com:
    def __init__(self, dp):
        self.dp = dp
        self.events: List[dict] = []
        self.initialized: Dict[int, int] = {}  # thread -> CoInitialize count
        self.services: Dict[int, dict] = {}    # handle -> service (or manager)
        self.next_handle = 0x7C000

    def _record(self, kind: str, **values):
        values["kind"] = kind
        self.events.append(values)
        self.dp.info(f"[com] {kind}: " + ", ".join(f"{k}={v!r}" for k, v in values.items() if k != "kind"))

    # ------------------------------------------------------------------ COM
    def co_initialize(self, dp, *args):
        count = self.initialized.get(dp.thread_id, 0)
        self.initialized[dp.thread_id] = count + 1
        return S_FALSE if count else S_OK

    def co_uninitialize(self, dp):
        if self.initialized.get(dp.thread_id):
            self.initialized[dp.thread_id] -= 1
        return 0

    def _registration(self, clsid: str):
        """("inproc", path) | ("local", command) | None, from the emulator's registry."""
        from .registry import registry
        reg = registry(self.dp)
        for root in ("\\Registry\\Machine\\Software\\Classes", "\\Registry\\User\\S-1-5-21-0_Classes"):
            base = f"{root}\\CLSID\\{clsid}"
            for kind, key in (("inproc", "InprocServer32"), ("local", "LocalServer32")):
                path = base + "\\" + key
                if not reg.exists(path):
                    continue
                _, values, _ = reg.contents(path)
                value = values.get("")
                if value is None:
                    if kind == "local":
                        return kind, ""
                    continue
                text = value[2].decode("utf-16-le", "replace").split("\0", 1)[0]
                if value[1] == 2 or "%" in text:  # REG_EXPAND_SZ
                    text = os.path.expandvars(text)
                return kind, text.strip('"')
            if reg.exists(base + "\\AppID") or reg.exists(base):
                _, values, _ = reg.contents(base)
                if "appid" in values:
                    return "local", ""
        return None

    def _create(self, dp, clsid_ptr, outer, iid_ptr, out_ptr, class_object: bool):
        clsid = _guid(dp, clsid_ptr)
        iid = _guid(dp, iid_ptr) if iid_ptr else ""
        dp.write_ptr(out_ptr, 0)
        registration = self._registration(clsid)
        if registration is None:
            self._record("com_class_not_registered", clsid=clsid, iid=iid)
            return REGDB_E_CLASSNOTREG
        kind, server = registration
        if kind != "inproc":
            self._record("com_out_of_process", clsid=clsid, iid=iid, server=server)
            return CO_E_SERVER_EXEC_FAILURE
        kernelbase = dp.modules["kernelbase.dll"]
        load = kernelbase.find_export("LoadLibraryExW").address
        get_proc = kernelbase.find_export("GetProcAddress").address
        ptr = dp.ptr_size()
        state = {}

        def after_load(module):
            module &= dp.addr_mask
            if not module:
                self._record("com_server_not_loaded", clsid=clsid, server=server)
                return CO_E_DLLNOTFOUND
            state["module"] = module
            return Invoke(get_proc, [module, write_string(dp, "DllGetClassObject", False)], after_proc)

        def after_proc(proc):
            proc &= dp.addr_mask
            if not proc:
                return CO_E_ERRORINDLL
            if class_object:
                return Invoke(proc, [clsid_ptr, iid_ptr, out_ptr], done)
            factory_out = dp.allocate(ptr)
            dp.write_ptr(factory_out, 0)
            factory_iid = dp.allocate(16)
            dp.write(factory_iid, IID_ICLASSFACTORY.bytes_le)
            state["factory_out"] = factory_out
            return Invoke(proc, [clsid_ptr, factory_iid, factory_out], after_factory)

        def after_factory(hr):
            hr &= 0xFFFFFFFF
            if hr & 0x80000000:
                return done(hr)
            factory = dp.read_ptr(state["factory_out"])
            table = dp.read_ptr(factory)
            state["factory"], state["release"] = factory, dp.read_ptr(table + 2 * ptr)
            return Invoke(dp.read_ptr(table + 3 * ptr), [factory, outer, iid_ptr, out_ptr], after_instance)

        def after_instance(hr):
            state["hr"] = hr & 0xFFFFFFFF
            return Invoke(state["release"], [state["factory"]], lambda _: done(state["hr"]))

        def done(hr):
            hr &= 0xFFFFFFFF
            self._record("com_create" if not class_object else "com_class_object", clsid=clsid, iid=iid,
                         server=server, hresult=hex(hr), object=hex(dp.read_ptr(out_ptr)))
            return hr

        return Invoke(load, [write_string(dp, server, True), 0, 0], after_load)

    def co_create_instance(self, dp, clsid, outer, context, iid, out):
        return self._create(dp, clsid, outer, iid, out, False)

    def co_create_instance_ex(self, dp, clsid, outer, context, server_info, count, results):
        # MULTI_QI {pIID, pItf, hr}: the object for the first interface (the others: E_NOINTERFACE)
        ptr = dp.ptr_size()
        size = 3 * ptr if dp._x64 else 12
        for i in range(1, int(count)):
            dp.write_ptr(results + i * size + ptr, 0)
            dp.write_ulong(results + i * size + 2 * ptr, 0x80004002)
        iid = dp.read_ptr(results)
        result = self._create(dp, clsid, outer, iid, results + ptr, False)

        def finish(hr):
            dp.write_ulong(results + 2 * ptr, hr & 0xFFFFFFFF)
            return hr
        if isinstance(result, Invoke):
            inner_then = None

            def chain(invoke):
                original = invoke.then

                def then(value):
                    out = original(value) if original is not None else value
                    if isinstance(out, Invoke):
                        return chain(out)
                    return finish(out)
                invoke.then = then
                return invoke
            return chain(result)
        return finish(result)

    def co_get_class_object(self, dp, clsid, context, server_info, iid, out):
        return self._create(dp, clsid, 0, iid, out, True)

    # ------------------------------------------------------------------ RPC
    def ndr_client_call(self, dp, stub_or_proxy, format_or_proc, *rest, version: int):
        try:
            if version == 2:
                descriptor = stub_or_proxy
                fmt = bytes(dp.read(format_or_proc, 8))
                offset = 2 + (4 if fmt[1] & 0x08 else 0)
                procedure = struct.unpack_from("<H", fmt, offset)[0]
            else:
                descriptor = dp.read_ptr(stub_or_proxy)  # MIDL_STUBLESS_PROXY_INFO.pStubDesc
                procedure = format_or_proc & 0xFFFFFFFF
            interface = dp.read_ptr(descriptor)  # MIDL_STUB_DESC.RpcInterfaceInformation
            uuid_text = _guid(dp, interface + 4)
        except Exception:
            uuid_text, procedure = "?", -1
        self._record("rpc_call", interface=uuid_text, procedure=procedure, result="RPC_S_SERVER_UNAVAILABLE")
        return RPC_S_SERVER_UNAVAILABLE

    # ------------------------------------------------------------------ the service manager
    def _handle(self, value: dict) -> int:
        self.next_handle += 4
        self.services[self.next_handle] = value
        return self.next_handle

    def _service_key(self, name: str) -> str:
        return "\\Registry\\Machine\\SYSTEM\\CurrentControlSet\\Services\\" + name

    def open_sc_manager(self, dp, machine, database, access, wide):
        self._record("open_sc_manager", machine=read_string(dp, machine, wide) if machine else None)
        return self._handle(dict(kind="manager"))

    def create_service(self, dp, manager, name, display, access, kind, start, error, binary, group, tag, dependencies,
                       user, password, wide):
        from .registry import registry
        service = read_string(dp, name, wide) or ""
        path = read_string(dp, binary, wide) if binary else ""
        reg = registry(dp)
        key = self._service_key(service)
        if reg.exists(key):
            set_last_error(dp, 1073)  # ERROR_SERVICE_EXISTS
            return 0
        reg.create(key)

        def sz(text):
            return ((text or "") + "\0").encode("utf-16-le")
        reg.set_value(key, "ImagePath", 2, sz(path))
        reg.set_value(key, "DisplayName", 1, sz(read_string(dp, display, wide) if display else service))
        reg.set_value(key, "Type", 4, struct.pack("<I", kind & 0xFFFFFFFF))
        reg.set_value(key, "Start", 4, struct.pack("<I", start & 0xFFFFFFFF))
        reg.set_value(key, "ErrorControl", 4, struct.pack("<I", error & 0xFFFFFFFF))
        if user:
            reg.set_value(key, "ObjectName", 1, sz(read_string(dp, user, wide)))
        self._record("create_service", name=service, binary=path, type=kind & 0xFFFFFFFF, start=start & 0xFFFFFFFF)
        return self._handle(dict(kind="service", name=service, state=1))

    def open_service(self, dp, manager, name, access, wide):
        from .registry import registry
        service = read_string(dp, name, wide) or ""
        if not registry(dp).exists(self._service_key(service)):
            set_last_error(dp, 1060)  # ERROR_SERVICE_DOES_NOT_EXIST
            return 0
        self._record("open_service", name=service)
        return self._handle(dict(kind="service", name=service, state=1))

    def start_service(self, dp, handle, argc, argv, wide):
        service = self.services.get(handle)
        if service is None or service["kind"] != "service":
            set_last_error(dp, 6)
            return 0
        service["state"] = 4  # SERVICE_RUNNING
        self._record("start_service", name=service["name"])
        return 1

    def _write_status(self, dp, status_ptr, service, extended=False):
        if not status_ptr:
            return
        state = service.get("state", 1)
        data = struct.pack("<IIIIIII", 0x10, state, 1 if state == 4 else 0, 0, 0, 0, 0)
        if extended:
            data += struct.pack("<II", 0, 0)
        dp.write(status_ptr, data)

    def control_service(self, dp, handle, control, status):
        service = self.services.get(handle)
        if service is None:
            set_last_error(dp, 6)
            return 0
        if control == 1:  # SERVICE_CONTROL_STOP
            service["state"] = 1
        self._record("control_service", name=service.get("name"), control=control)
        self._write_status(dp, status, service)
        return 1

    def query_service_status(self, dp, handle, status):
        service = self.services.get(handle)
        if service is None:
            set_last_error(dp, 6)
            return 0
        self._write_status(dp, status, service)
        return 1

    def query_service_status_ex(self, dp, handle, level, buffer, size, needed):
        service = self.services.get(handle)
        if service is None:
            set_last_error(dp, 6)
            return 0
        if needed:
            dp.write_ulong(needed, 36)
        if size < 36:
            set_last_error(dp, 122)
            return 0
        self._write_status(dp, buffer, service, True)
        return 1

    def delete_service(self, dp, handle):
        from .registry import registry
        service = self.services.get(handle)
        if service is None or service["kind"] != "service":
            set_last_error(dp, 6)
            return 0
        registry(dp).delete_key(self._service_key(service["name"]))
        self._record("delete_service", name=service["name"])
        return 1

    def change_service_config(self, dp, handle, kind, start, error, binary, *rest, wide):
        service = self.services.get(handle)
        if service is None:
            set_last_error(dp, 6)
            return 0
        self._record("change_service_config", name=service.get("name"),
                     binary=read_string(dp, binary, wide) if binary else None, start=start & 0xFFFFFFFF)
        return 1

    def close_service_handle(self, dp, handle):
        return 1 if self.services.pop(handle, None) is not None else 0


def install(dp):
    com = Com(dp)
    dp.com = com
    hooks = dp.apihooks
    for module in ("combase.dll", "ole32.dll"):
        hooks.register(module, "CoInitialize", 1, com.co_initialize)
        hooks.register(module, "CoInitializeEx", 2, com.co_initialize)
        hooks.register(module, "OleInitialize", 1, com.co_initialize)
        hooks.register(module, "CoUninitialize", 0, com.co_uninitialize)
        hooks.register(module, "OleUninitialize", 0, com.co_uninitialize)
        hooks.register(module, "CoInitializeSecurity", 9, lambda dp, *a: S_OK)
        hooks.register(module, "CoCreateInstance", 5, com.co_create_instance)
        hooks.register(module, "CoCreateInstanceEx", 6, com.co_create_instance_ex)
        hooks.register(module, "CoGetClassObject", 5, com.co_get_class_object)
    # NdrClientCall* take their arguments as varargs (cdecl in 32-bit: the caller pops them)
    hooks.register("rpcrt4.dll", "NdrClientCall2", 0 if not dp._x64 else 2,
                   lambda dp, *a: com.ndr_client_call(dp, dp.args[0], dp.args[1], version=2))
    hooks.register("rpcrt4.dll", "NdrClientCall3", 0 if not dp._x64 else 2,
                   lambda dp, *a: com.ndr_client_call(dp, dp.args[0], dp.args[1], version=3))
    # NdrClientCall4 (stub descriptor, format string, ...) like NdrClientCall2
    hooks.register("rpcrt4.dll", "NdrClientCall4", 0 if not dp._x64 else 2,
                   lambda dp, *a: com.ndr_client_call(dp, dp.args[0], dp.args[1], version=2))
    for module in ("sechost.dll", "advapi32.dll"):
        for wide, suffix in ((True, "W"), (False, "A")):
            hooks.register(module, "OpenSCManager" + suffix, 3, lambda dp, *a, w=wide: com.open_sc_manager(dp, *a, w))
            hooks.register(module, "CreateService" + suffix, 13, lambda dp, *a, w=wide: com.create_service(dp, *a, w))
            hooks.register(module, "OpenService" + suffix, 3, lambda dp, *a, w=wide: com.open_service(dp, *a, w))
            hooks.register(module, "StartService" + suffix, 3, lambda dp, *a, w=wide: com.start_service(dp, *a, w))
            hooks.register(module, "ChangeServiceConfig" + suffix, 11,
                           lambda dp, *a, w=wide: com.change_service_config(dp, *a, wide=w))
        hooks.register(module, "ControlService", 3, com.control_service)
        hooks.register(module, "QueryServiceStatus", 2, com.query_service_status)
        hooks.register(module, "QueryServiceStatusEx", 5, com.query_service_status_ex)
        hooks.register(module, "DeleteService", 1, com.delete_service)
        hooks.register(module, "CloseServiceHandle", 1, com.close_service_handle)
    return com
