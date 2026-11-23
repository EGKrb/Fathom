# Fathom addition to dumpulator: DXGI for the emulated Direct3D 10 / 11 / 12: factories (IDXGIFactory to
# IDXGIFactory7), the adapter (a software adapter), its output (the screen), swap chains (CreateSwapChain,
# CreateSwapChainForHwnd; Present draws the back buffer into the window's image, gui_draw.py), and what
# the Direct3D modules share (QueryInterface of the Python objects, the windows). Boost Software License
# 1.0, like the rest of dumpulator (LICENSE.dumpulator).

import struct
from typing import List, Optional

from .apihooks import com_runtime, read_guid, E_NOINTERFACE, E_NOTIMPL
from .graphics_tables import IIDS
from .raster import numpy

E_INVALIDARG, E_FAIL = 0x80070057, 0x80004005
DXGI_ERROR_NOT_FOUND, DXGI_ERROR_UNSUPPORTED = 0x887A0002, 0x887A0004
DXGI_ERROR_FRAME_STATISTICS_DISJOINT = 0x887A000B
S_FALSE = 1
IID_IUNKNOWN = "00000000-0000-0000-C000-000000000046"
FACTORIES = ["IDXGIFactory", "IDXGIFactory1", "IDXGIFactory2", "IDXGIFactory3", "IDXGIFactory4", "IDXGIFactory5",
             "IDXGIFactory6", "IDXGIFactory7"]
ADAPTERS = ["IDXGIAdapter", "IDXGIAdapter1", "IDXGIAdapter2", "IDXGIAdapter3", "IDXGIAdapter4"]
SWAPCHAINS = ["IDXGISwapChain", "IDXGISwapChain1", "IDXGISwapChain2", "IDXGISwapChain3", "IDXGISwapChain4"]
DEVICES = ["IDXGIDevice", "IDXGIDevice1", "IDXGIDevice2", "IDXGIDevice3", "IDXGIDevice4"]


def missing(dp, key: str):
    """Something a program asked that is not emulated: named once in the log and in the summary."""
    table = dp._fathom_unimplemented
    if key not in table:
        dp.info(f"{key} (not emulated)")
    table[key] = table.get(key, 0) + 1


class Dxgi:
    """The root of DXGI and of the Direct3D 10 / 11 / 12 objects of an emulated process."""

    def __init__(self, dp):
        self.dp = dp
        self.com = com_runtime(dp)
        self.factory = Factory(self)
        self.adapter = Adapter(self)
        self.output = Output(self)
        self.presents = 0
        self.events: List[dict] = []

    # COM ------------------------------------------------------------------
    def query(self, impl, riid, out, default=None) -> int:
        """QueryInterface on a Python object: the interface asked by riid among those it implements
        (impl.interface, impl.interfaces)."""
        iid = read_guid(self.dp, riid)
        names = [getattr(impl, "interface", None) or default] + list(getattr(impl, "interfaces", []))
        for name in names:
            if name and IIDS.get(name) == iid:
                self.dp.write_ptr(out, self.com.create(impl, name))
                impl.refs = getattr(impl, "refs", 0) + 1
                return 0
        from .graphics_tables import BASE_IIDS
        if (iid == IID_IUNKNOWN or iid in BASE_IIDS.values()) and names[0]:
            self.dp.write_ptr(out, self.com.create(impl, names[0]))
            impl.refs = getattr(impl, "refs", 0) + 1
            return 0
        if out:
            self.dp.write_ptr(out, 0)
        return E_NOINTERFACE

    def out(self, impl, interface: str, out) -> int:
        """A new object written to an out pointer (S_FALSE without one: a validation call)."""
        if not out:
            return S_FALSE
        self.dp.write_ptr(out, self.com.create(impl, interface))
        return 0

    def impl(self, address):
        return self.com.impl(address)

    # windows --------------------------------------------------------------
    def canvas(self, window):
        gui = getattr(self.dp, "gui", None)
        drawing = getattr(gui, "drawing", None) if gui is not None else None
        return drawing.canvas(window) if drawing is not None and window else None

    def window_size(self, window, default=(640, 480)):
        canvas = self.canvas(window)
        return canvas.size if canvas is not None else default

    def screen_size(self):
        gui = getattr(self.dp, "gui", None)
        return getattr(gui, "screen_size", (1920, 1080)) if gui is not None else (1920, 1080)

    def present(self, window, image):
        """The image (gpu.Image) in the window's image."""
        self.presents += 1
        canvas = self.canvas(window)
        if canvas is None or image is None:
            return
        frame = image.picture()
        if frame.size != canvas.size:
            frame = frame.resize(canvas.size)
        canvas.paste(frame)

    # exports --------------------------------------------------------------
    def create_factory(self, dp, riid, out):
        return self.query(self.factory, riid, out, "IDXGIFactory")


class Factory:
    interface = "IDXGIFactory"
    interfaces = FACTORIES

    def __init__(self, dxgi: Dxgi):
        self.dxgi = dxgi
        self.window = 0

    def _adapter(self, index, out, interface):
        if index:
            if out:
                self.dxgi.dp.write_ptr(out, 0)
            return DXGI_ERROR_NOT_FOUND
        self.dxgi.dp.write_ptr(out, self.dxgi.com.create(self.dxgi.adapter, interface))
        self.dxgi.adapter.refs = getattr(self.dxgi.adapter, "refs", 0) + 1
        return 0

    def EnumAdapters(self, index, out):
        return self._adapter(index, out, "IDXGIAdapter")

    def EnumAdapters1(self, index, out):
        return self._adapter(index, out, "IDXGIAdapter1")

    def EnumAdapterByLuid(self, luid, riid, out):
        return self.dxgi.query(self.dxgi.adapter, riid, out)

    def EnumWarpAdapter(self, riid, out):
        return self.dxgi.query(self.dxgi.adapter, riid, out)

    def EnumAdapterByGpuPreference(self, index, preference, riid, out):
        if index:
            self.dxgi.dp.write_ptr(out, 0)
            return DXGI_ERROR_NOT_FOUND
        return self.dxgi.query(self.dxgi.adapter, riid, out)

    def MakeWindowAssociation(self, window, flags):
        self.window = window
        return 0

    def GetWindowAssociation(self, out):
        self.dxgi.dp.write_ptr(out, self.window)

    def CreateSwapChain(self, device, desc, out):
        dp = self.dxgi.dp
        data = bytes(dp.read(desc, 72 if dp._x64 else 60))
        width, height, _, _, fmt = struct.unpack_from("<5I", data, 0)
        count = struct.unpack_from("<I", data, 40)[0]
        window = struct.unpack_from("<Q" if dp._x64 else "<I", data, 48 if dp._x64 else 44)[0]
        chain = SwapChain(self.dxgi, self.dxgi.impl(device), window, width, height, fmt, count, data)
        if chain.owner is None:
            return E_INVALIDARG
        dp.write_ptr(out, self.dxgi.com.create(chain, "IDXGISwapChain"))
        return 0

    def CreateSwapChainForHwnd(self, device, window, desc, fullscreen, output, out):
        dp = self.dxgi.dp
        data = bytes(dp.read(desc, 48))
        width, height, fmt = struct.unpack_from("<3I", data, 0)
        count = struct.unpack_from("<I", data, 24)[0]
        chain = SwapChain(self.dxgi, self.dxgi.impl(device), window, width, height, fmt, count, None, data)
        if chain.owner is None:
            return E_INVALIDARG
        dp.write_ptr(out, self.dxgi.com.create(chain, "IDXGISwapChain1"))
        return 0

    def CreateSwapChainForCoreWindow(self, device, window, desc, output, out):
        missing(self.dxgi.dp, "DXGI swap chains of a CoreWindow (UWP)")
        return E_NOTIMPL

    def CreateSwapChainForComposition(self, device, desc, output, out):
        missing(self.dxgi.dp, "DXGI swap chains for composition")
        return E_NOTIMPL

    def CreateSoftwareAdapter(self, module, out):
        return self._adapter(0, out, "IDXGIAdapter")

    def IsCurrent(self):
        return 1

    def IsWindowedStereoEnabled(self):
        return 0

    def GetCreationFlags(self):
        return 0

    def CheckFeatureSupport(self, feature, data, size):
        self.dxgi.dp.write(data, b"\0" * size)

    def GetSharedResourceAdapterLuid(self, handle, luid):
        return E_NOTIMPL

    def RegisterStereoStatusWindow(self, window, message, cookie):
        self.dxgi.dp.write(cookie, struct.pack("<I", 1))

    RegisterOcclusionStatusWindow = RegisterStereoStatusWindow

    def RegisterStereoStatusEvent(self, event, cookie):
        self.dxgi.dp.write(cookie, struct.pack("<I", 1))

    RegisterOcclusionStatusEvent = RegisterStereoStatusEvent
    RegisterAdaptersChangedEvent = RegisterStereoStatusEvent

    def UnregisterStereoStatus(self, cookie):
        return None

    UnregisterOcclusionStatus = UnregisterStereoStatus

    def UnregisterAdaptersChangedEvent(self, cookie):
        return 0

    def GetParent(self, riid, out):
        return E_NOINTERFACE

    def SetPrivateData(self, guid, size, data):
        return 0

    def SetPrivateDataInterface(self, guid, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND


class Adapter:
    interface = "IDXGIAdapter"
    interfaces = ADAPTERS
    NAME = "Fathom software rasterizer"

    def __init__(self, dxgi: Dxgi):
        self.dxgi = dxgi

    def _desc(self, out, version):
        dp = self.dxgi.dp
        ptr = dp.ptr_size()
        data = self.NAME.encode("utf-16-le").ljust(256, b"\0") + struct.pack("<4I", 0x1414, 0x8C, 0, 0)
        data += struct.pack("<3Q" if ptr == 8 else "<3I", 0, 256 << 20, 512 << 20)
        data += struct.pack("<Ii", 0x46544D, 0)   # LUID
        if version >= 1:
            data += struct.pack("<I", 2)            # DXGI_ADAPTER_FLAG_SOFTWARE
        if version >= 2:
            data += struct.pack("<2I", 0, 0)
        dp.write(out, data)

    def GetDesc(self, out):
        self._desc(out, 0)

    def GetDesc1(self, out):
        self._desc(out, 1)

    def GetDesc2(self, out):
        self._desc(out, 2)

    def GetDesc3(self, out):
        self._desc(out, 2)

    def EnumOutputs(self, index, out):
        if index:
            self.dxgi.dp.write_ptr(out, 0)
            return DXGI_ERROR_NOT_FOUND
        self.dxgi.dp.write_ptr(out, self.dxgi.com.create(self.dxgi.output, "IDXGIOutput"))
        return 0

    def CheckInterfaceSupport(self, guid, version):
        iid = read_guid(self.dxgi.dp, guid)
        if iid in (IIDS.get("ID3D10Device"), IIDS.get("ID3D11Device")):
            if version:
                self.dxgi.dp.write(version, struct.pack("<Q", 0x0001000000000000))
            return 0
        return DXGI_ERROR_UNSUPPORTED

    def QueryVideoMemoryInfo(self, node, group, info):
        self.dxgi.dp.write(info, struct.pack("<4Q", 512 << 20, 0, 512 << 20, 0))

    def SetVideoMemoryReservation(self, node, group, size):
        return 0

    def RegisterHardwareContentProtectionTeardownStatusEvent(self, event, cookie):
        self.dxgi.dp.write(cookie, struct.pack("<I", 1))

    RegisterVideoMemoryBudgetChangeNotificationEvent = RegisterHardwareContentProtectionTeardownStatusEvent

    def UnregisterHardwareContentProtectionTeardownStatus(self, cookie):
        return None

    UnregisterVideoMemoryBudgetChangeNotification = UnregisterHardwareContentProtectionTeardownStatus

    def GetParent(self, riid, out):
        return self.dxgi.query(self.dxgi.factory, riid, out)

    def SetPrivateData(self, guid, size, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND


class Output:
    interface = "IDXGIOutput"
    interfaces = ["IDXGIOutput", "IDXGIOutput1"]

    def __init__(self, dxgi: Dxgi):
        self.dxgi = dxgi

    def GetDesc(self, out):
        dp = self.dxgi.dp
        width, height = self.dxgi.screen_size()
        data = "\\\\.\\DISPLAY1".encode("utf-16-le").ljust(64, b"\0") + struct.pack("<4iIi", 0, 0, width, height, 1, 1)
        if dp._x64:
            data += b"\0" * (-len(data) % 8)
            data += struct.pack("<Q", 0x10001)
        else:
            data += struct.pack("<I", 0x10001)
        dp.write(out, data)

    def _mode(self, fmt, stereo=False):
        width, height = self.dxgi.screen_size()
        return struct.pack("<7I", width, height, 60, 1, fmt or 87, 0, 0) + (struct.pack("<I", 0) if stereo else b"")

    def GetDisplayModeList(self, fmt, flags, count, desc, stereo=False):
        dp = self.dxgi.dp
        if desc:
            dp.write(desc, self._mode(fmt, stereo))
        dp.write(count, struct.pack("<I", 1))

    def GetDisplayModeList1(self, fmt, flags, count, desc):
        return self.GetDisplayModeList(fmt, flags, count, desc, True)

    def FindClosestMatchingMode(self, mode, closest, device):
        fmt = struct.unpack("<I", bytes(self.dxgi.dp.read(mode + 16, 4)))[0] if mode else 0
        self.dxgi.dp.write(closest, self._mode(fmt))

    def FindClosestMatchingMode1(self, mode, closest, device):
        fmt = struct.unpack("<I", bytes(self.dxgi.dp.read(mode + 16, 4)))[0] if mode else 0
        self.dxgi.dp.write(closest, self._mode(fmt, True))

    def WaitForVBlank(self):
        return 0

    def TakeOwnership(self, device, exclusive):
        return 0

    def ReleaseOwnership(self):
        return None

    def GetGammaControlCapabilities(self, out):
        self.dxgi.dp.write(out, b"\0" * 16)

    def GetFrameStatistics(self, out):
        return DXGI_ERROR_FRAME_STATISTICS_DISJOINT

    def GetParent(self, riid, out):
        return self.dxgi.query(self.dxgi.adapter, riid, out)


class SwapChain:
    """A swap chain of a Direct3D 10 / 11 device or a Direct3D 12 command queue (owner): its buffers are
    resources of that API, made by owner.swapchain_buffer(width, height, format)."""

    interface = "IDXGISwapChain"
    interfaces = SWAPCHAINS

    def __init__(self, dxgi: Dxgi, owner, window, width, height, fmt, count, desc=None, desc1=None):
        self.dxgi, self.window = dxgi, window
        self.owner = owner if hasattr(owner, "swapchain_buffer") else None
        self.flip = hasattr(owner, "flip_model") and owner.flip_model
        if not width or not height:
            width, height = dxgi.window_size(window)
        self.width, self.height, self.format = width, height, fmt or 87
        self.count = max(1, count) if self.flip else 1
        self.desc = bytearray(desc) if desc is not None else None
        self.desc1 = bytearray(desc1) if desc1 is not None else None
        self.current = 0
        self.latency = 3
        self.background = (0.0, 0.0, 0.0, 1.0)
        self.buffers = []
        self._make_buffers()
        dxgi.events.append(dict(kind="swap_chain", window=hex(window or 0), width=self.width, height=self.height,
                                buffers=self.count))

    def _make_buffers(self):
        if self.owner is None:
            return
        self.buffers = [self.owner.swapchain_buffer(self.width, self.height, self.format) for _ in range(self.count)]
        self.current = 0

    def GetBuffer(self, index, riid, out):
        if index >= len(self.buffers):
            return E_INVALIDARG
        buffer = self.buffers[index if self.flip else 0]
        return self.dxgi.query(buffer, riid, out)

    def Present(self, interval, flags):
        if flags & 1:  # DXGI_PRESENT_TEST
            return 0
        buffer = self.buffers[self.current] if self.buffers else None
        image = buffer.images[0][0] if buffer is not None and getattr(buffer, "images", None) else None
        self.dxgi.present(self.window, image)
        if self.flip:
            self.current = (self.current + 1) % self.count
        return 0

    def Present1(self, interval, flags, parameters):
        return self.Present(interval, flags)

    def GetCurrentBackBufferIndex(self):
        return self.current

    def GetDesc(self, out):
        dp = self.dxgi.dp
        data = bytearray(72 if dp._x64 else 60)
        struct.pack_into("<2I2I3I", data, 0, self.width, self.height, 60, 1, self.format, 0, 0)
        struct.pack_into("<2I2I", data, 28, 1, 0, 0x20, self.count)
        if dp._x64:
            struct.pack_into("<QIII", data, 48, self.window, 1, 3 if self.flip else 0, 0)
        else:
            struct.pack_into("<IIII", data, 44, self.window, 1, 3 if self.flip else 0, 0)
        dp.write(out, bytes(data))

    def GetDesc1(self, out):
        self.dxgi.dp.write(out, struct.pack("<12I", self.width, self.height, self.format, 0, 1, 0, 0x20, self.count, 0,
                                            3 if self.flip else 0, 0, 0))

    def GetFullscreenDesc(self, out):
        self.dxgi.dp.write(out, struct.pack("<5I", 60, 1, 0, 0, 1))

    def GetHwnd(self, out):
        self.dxgi.dp.write_ptr(out, self.window)

    def ResizeBuffers(self, count, width, height, fmt, flags):
        if not width or not height:
            width, height = self.dxgi.window_size(self.window, (self.width, self.height))
        self.width, self.height = width, height
        if fmt:
            self.format = fmt
        if count and self.flip:
            self.count = count
        self._make_buffers()

    def ResizeBuffers1(self, count, width, height, fmt, flags, masks, queues):
        return self.ResizeBuffers(count, width, height, fmt, flags)

    def ResizeTarget(self, desc):
        return 0

    def SetFullscreenState(self, fullscreen, output):
        return 0

    def GetFullscreenState(self, fullscreen, output):
        dp = self.dxgi.dp
        if fullscreen:
            dp.write(fullscreen, struct.pack("<I", 0))
        if output:
            dp.write_ptr(output, 0)

    def GetContainingOutput(self, out):
        self.dxgi.dp.write_ptr(out, self.dxgi.com.create(self.dxgi.output, "IDXGIOutput"))

    def GetFrameStatistics(self, out):
        return DXGI_ERROR_FRAME_STATISTICS_DISJOINT

    def GetLastPresentCount(self, out):
        self.dxgi.dp.write(out, struct.pack("<I", self.dxgi.presents))

    def IsTemporaryMonoSupported(self):
        return 0

    def GetRestrictToOutput(self, out):
        self.dxgi.dp.write_ptr(out, 0)

    def SetBackgroundColor(self, color):
        self.background = struct.unpack("<4f", bytes(self.dxgi.dp.read(color, 16)))

    def GetBackgroundColor(self, color):
        self.dxgi.dp.write(color, struct.pack("<4f", *self.background))

    def SetRotation(self, rotation):
        return 0

    def GetRotation(self, out):
        self.dxgi.dp.write(out, struct.pack("<I", 1))

    def SetSourceSize(self, width, height):
        return 0

    def GetSourceSize(self, width, height):
        self.dxgi.dp.write(width, struct.pack("<I", self.width))
        self.dxgi.dp.write(height, struct.pack("<I", self.height))

    def SetMaximumFrameLatency(self, latency):
        self.latency = latency

    def GetMaximumFrameLatency(self, out):
        self.dxgi.dp.write(out, struct.pack("<I", self.latency))

    def GetFrameLatencyWaitableObject(self):
        return 0

    def SetMatrixTransform(self, matrix):
        return 0

    def GetMatrixTransform(self, matrix):
        self.dxgi.dp.write(matrix, struct.pack("<6f", 1, 0, 0, 1, 0, 0))

    def CheckColorSpaceSupport(self, space, support):
        self.dxgi.dp.write(support, struct.pack("<I", 1 if space == 0 else 0))

    def SetColorSpace1(self, space):
        return 0

    def SetHDRMetaData(self, kind, size, data):
        return 0

    def GetDevice(self, riid, out):
        device = getattr(self.owner, "device", self.owner)
        return self.dxgi.query(device, riid, out)

    def GetParent(self, riid, out):
        return self.dxgi.query(self.dxgi.factory, riid, out)

    def SetPrivateData(self, guid, size, data):
        return 0

    def SetPrivateDataInterface(self, guid, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND


class DxgiDevice:
    """The IDXGIDevice methods of a Direct3D 10 / 11 device (mixed into its class)."""

    def GetAdapter(self, out):
        dxgi = self.dxgi
        dxgi.dp.write_ptr(out, dxgi.com.create(dxgi.adapter, "IDXGIAdapter"))
        dxgi.adapter.refs = getattr(dxgi.adapter, "refs", 0) + 1

    def GetParent(self, riid, out):
        return self.dxgi.query(self.dxgi.adapter, riid, out)

    def CreateSurface(self, desc, count, usage, shared, out):
        return E_NOTIMPL

    def QueryResourceResidency(self, resources, status, count):
        self.dxgi.dp.write(status, struct.pack(f"<{count}I", *([1] * count)))

    def SetGPUThreadPriority(self, priority):
        return 0

    def GetGPUThreadPriority(self, out):
        self.dxgi.dp.write(out, struct.pack("<i", 0))

    def SetMaximumFrameLatency(self, latency):
        return 0

    def GetMaximumFrameLatency(self, out):
        self.dxgi.dp.write(out, struct.pack("<I", 3))

    def OfferResources(self, count, resources, priority):
        return 0

    def ReclaimResources(self, count, resources, discarded):
        if discarded:
            self.dxgi.dp.write(discarded, b"\0" * 4 * count)

    def EnqueueSetEvent(self, event):
        return 0

    def Trim(self):
        return None


def dxgi_of(dp) -> Dxgi:
    if getattr(dp, "dxgi", None) is None:
        dp.dxgi = Dxgi(dp)
    return dp.dxgi


def install(dp):
    if numpy is None:
        return None
    dxgi = dxgi_of(dp)
    hooks = dp.apihooks
    hooks.register_signature("dxgi.dll", "CreateDXGIFactory", "pp", dxgi.create_factory)
    hooks.register_signature("dxgi.dll", "CreateDXGIFactory1", "pp", dxgi.create_factory)
    hooks.register_signature("dxgi.dll", "CreateDXGIFactory2", "ipp", lambda dp, flags, riid, out: dxgi.create_factory(dp, riid, out))
    hooks.register_signature("dxgi.dll", "DXGIGetDebugInterface1", "ipp", lambda dp, flags, riid, out: E_NOINTERFACE)
    hooks.register_signature("dxgi.dll", "DXGIDeclareAdapterRemovalSupport", "", lambda dp: 0)
    return dxgi
