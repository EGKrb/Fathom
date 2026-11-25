"""Generate dumpulator/graphics_tables.py from the Windows SDK headers and Khronos' OpenGL headers: the
methods of the COM interfaces of Direct3D 9, 10, 11, 12 and DXGI in vtable order, and the functions of
GDI+ (flat API), OpenGL 1.1 (opengl32.dll), OpenGL 1.2+ and its extensions (glext.h) and the WGL
extensions (wglext.h), each with the kinds of its parameters (p: a pointer-sized value, i: a 32-bit
integer, h: 16 bits, b: 8 bits, f: float, d: double, q: a 64-bit value). The emulator needs them to
build vtables and, in 32-bit, to pop the right number of bytes when a function returns (stdcall).

    python emulator/tools/make_graphics_tables.py [SDK Include\\<version>] [folder of glext.h and wglext.h]

glext.h and wglext.h: https://registry.khronos.org/OpenGL/api/GL/ (MIT license).
"""
import glob
import os
import pprint
import re
import sys

INTERFACES = {
    "d3d9.h": ["IDirect3D9", "IDirect3DDevice9", "IDirect3DSurface9", "IDirect3DVertexBuffer9", "IDirect3DIndexBuffer9",
               "IDirect3DTexture9", "IDirect3DCubeTexture9", "IDirect3DVolumeTexture9", "IDirect3DSwapChain9",
               "IDirect3DVertexDeclaration9", "IDirect3DVertexShader9", "IDirect3DPixelShader9", "IDirect3DStateBlock9",
               "IDirect3DQuery9"],
    "d3dcommon.h": ["ID3D10Blob"],
    "d3d10.h": ["ID3D10Device", "ID3D10Buffer", "ID3D10Texture1D", "ID3D10Texture2D", "ID3D10Texture3D",
                "ID3D10RenderTargetView", "ID3D10DepthStencilView", "ID3D10ShaderResourceView", "ID3D10VertexShader",
                "ID3D10PixelShader", "ID3D10GeometryShader", "ID3D10InputLayout", "ID3D10SamplerState",
                "ID3D10RasterizerState", "ID3D10BlendState", "ID3D10DepthStencilState", "ID3D10Query",
                "ID3D10Predicate", "ID3D10Counter", "ID3D10Multithread"],
    "d3d10_1.h": ["ID3D10Device1", "ID3D10BlendState1", "ID3D10ShaderResourceView1"],
    "d3d10effect.h": ["ID3D10Effect", "ID3D10EffectPool", "ID3D10EffectTechnique", "ID3D10EffectPass", "ID3D10EffectType",
                      "ID3D10EffectVariable", "ID3D10EffectScalarVariable", "ID3D10EffectVectorVariable",
                      "ID3D10EffectMatrixVariable", "ID3D10EffectStringVariable", "ID3D10EffectShaderResourceVariable",
                      "ID3D10EffectRenderTargetViewVariable", "ID3D10EffectDepthStencilViewVariable",
                      "ID3D10EffectConstantBuffer", "ID3D10EffectShaderVariable", "ID3D10EffectBlendVariable",
                      "ID3D10EffectDepthStencilVariable", "ID3D10EffectRasterizerVariable", "ID3D10EffectSamplerVariable",
                      "ID3D10StateBlock"],
    "d3d11.h": ["ID3D11Device", "ID3D11DeviceContext", "ID3D11Texture2D", "ID3D11Buffer", "ID3D11RenderTargetView",
                "ID3D11DepthStencilView", "ID3D11ShaderResourceView", "ID3D11VertexShader", "ID3D11PixelShader",
                "ID3D11GeometryShader", "ID3D11InputLayout", "ID3D11SamplerState", "ID3D11RasterizerState",
                "ID3D11BlendState", "ID3D11DepthStencilState", "ID3D11Query", "ID3D11Texture1D", "ID3D11Texture3D",
                "ID3D11UnorderedAccessView", "ID3D11ComputeShader", "ID3D11HullShader", "ID3D11DomainShader",
                "ID3D11ClassLinkage", "ID3D11CommandList", "ID3D11Predicate", "ID3D11Counter"],
    "d3d11_1.h": ["ID3D11Device1", "ID3D11DeviceContext1", "ID3D11BlendState1", "ID3D11RasterizerState1"],
    "d3d12.h": ["ID3D12Device", "ID3D12Device1", "ID3D12Device2", "ID3D12Device3", "ID3D12Device4", "ID3D12Device5",
                "ID3D12CommandQueue", "ID3D12CommandAllocator", "ID3D12GraphicsCommandList", "ID3D12GraphicsCommandList1",
                "ID3D12GraphicsCommandList2", "ID3D12GraphicsCommandList3", "ID3D12GraphicsCommandList4", "ID3D12Fence",
                "ID3D12Fence1", "ID3D12Resource", "ID3D12Resource1", "ID3D12DescriptorHeap", "ID3D12RootSignature",
                "ID3D12PipelineState", "ID3D12Heap", "ID3D12QueryHeap", "ID3D12CommandSignature",
                "ID3D12RootSignatureDeserializer", "ID3D12VersionedRootSignatureDeserializer", "ID3D12StateObject",
                "ID3D12StateObjectProperties", "ID3D12StateObjectProperties1", "ID3D12WorkGraphProperties"]
               + [f"ID3D12Device{k}" for k in range(6, 15)] + [f"ID3D12GraphicsCommandList{k}" for k in range(5, 11)],
    "d3d12sdklayers.h": ["ID3D12Debug"],
    "dxgi.h": ["IDXGISwapChain", "IDXGIFactory", "IDXGIFactory1", "IDXGIAdapter", "IDXGIAdapter1", "IDXGIDevice",
               "IDXGIDevice1", "IDXGIOutput", "IDXGISurface", "IDXGISurface1", "IDXGIResource"],
    "dxgi1_2.h": ["IDXGIFactory2", "IDXGISwapChain1", "IDXGIAdapter2", "IDXGIDevice2", "IDXGIOutput1", "IDXGIResource1",
                  "IDXGISurface2"],
    "dxgi1_3.h": ["IDXGIFactory3", "IDXGISwapChain2", "IDXGIDevice3"],
    "dxgi1_4.h": ["IDXGIFactory4", "IDXGISwapChain3", "IDXGIAdapter3"],
    "dxgi1_5.h": ["IDXGIFactory5", "IDXGISwapChain4", "IDXGIDevice4"],
    "dxgi1_6.h": ["IDXGIFactory6", "IDXGIFactory7", "IDXGIAdapter4"],
}

BASES = {
    "d3d12.h": ["ID3D12Object", "ID3D12DeviceChild", "ID3D12Pageable", "ID3D12CommandList"],
    "d3d11.h": ["ID3D11DeviceChild", "ID3D11Resource", "ID3D11View", "ID3D11Asynchronous"],
    "d3d10.h": ["ID3D10DeviceChild", "ID3D10Resource", "ID3D10View", "ID3D10Asynchronous"],
    "dxgi.h": ["IDXGIObject", "IDXGIDeviceSubObject"],
}

POINTER = r"\b(H[A-Z]+|HANDLE|\w+_PTR|SIZE_T|SSIZE_T|LPARAM|WPARAM|LRESULT|REF\w+|LP\w+|P[A-Z]\w*|GLsizeiptr\w*|GLintptr\w*" \
          r"|GLsync|GLDEBUGPROC\w*|GLeglImageOES|GLeglClientBufferEXT|GLvdpauSurfaceNV|D3D12_CPU_DESCRIPTOR_HANDLE" \
          r"|HGPUNV|HPBUFFERARB|HPBUFFEREXT|HPVIDEODEV|HVIDEOOUTPUTDEVICENV|HVIDEOINPUTDEVICENV)\b"
QWORD = r"\b(UINT64|ULONGLONG|LONGLONG|INT64|LARGE_INTEGER|ULARGE_INTEGER|LUID|GLuint64\w*|GLint64\w*" \
        r"|D3D12_GPU_VIRTUAL_ADDRESS|D3D12_GPU_DESCRIPTOR_HANDLE" \
        r"|D3D12_NODE_ID)\b"    # a 16-byte struct by value: x64 passes a pointer to a copy, x86 its 8 bytes


def kind(declaration: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", declaration)
    text = re.sub(r"_[A-Z][A-Za-z_]*\([^()]*\)|_[A-Z][A-Za-z_]*_\b|\b_In_\w*|\b_Out_\w*|\b_Inout_\w*", "", text)
    text = text.replace("const", "").replace("CONST", "").strip()
    if "*" in text or "[" in text:
        return "p"
    if re.search(r"\b(double|DOUBLE|GLdouble|GLclampd)\b", text):
        return "d"
    if re.search(r"\b(float|FLOAT|GLfloat|GLclampf|REAL)\b", text):
        return "f"
    if re.search(QWORD, text):
        return "q"
    if re.search(POINTER, text):
        return "p"
    if re.search(r"\b(GLubyte|GLbyte|GLboolean|BYTE|UCHAR|BOOLEAN|CHAR|char|GLcharARB)\b", text):
        return "b"
    if re.search(r"\b(GLshort|GLushort|GLhalfNV|GLhalfARB|GLhalf|WORD|SHORT|USHORT|INT16|UINT16|WCHAR|short)\b", text):
        return "h"
    return "i"  # the other integers and the enumerations: 32 bits


def split_parameters(text: str):
    depth, current, out = 0, "", []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            out.append(current)
            current = ""
        else:
            current += char
    if current.strip():
        out.append(current)
    return [p.strip() for p in out if p.strip() and p.strip() not in ("void", "VOID", "THIS")]


def windows_branch(text: str) -> str:
    """The lines of a header as a Windows compiler sees them, for the conditions on _WIN32 (d3d12.h
    declares the methods returning a structure twice: the C++ ABI on Windows has a hidden return pointer)."""
    out, stack = [], []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#if"):
            condition = stripped
            if "_WIN32" in condition and "!defined(_WIN32)" in condition.replace(" ", ""):
                stack.append(False)
            elif "_WIN32" in condition:
                stack.append(True)
            else:
                stack.append(None)
            continue
        if stripped.startswith("#else") and stack:
            if stack[-1] is not None:
                stack[-1] = not stack[-1]
            continue
        if stripped.startswith("#endif") and stack:
            stack.pop()
            continue
        if all(s is not False for s in stack):
            out.append(line)
    return "\n".join(out)


def vtbl_methods(source: str, interface: str):
    """The methods of a MIDL C vtable (typedef struct IxVtbl { ... } IxVtbl;)."""
    match = re.search(r"typedef struct %sVtbl\s*\{(.*?)\}\s*%sVtbl;" % (interface, interface), source, re.S)
    if not match:
        return None
    body = windows_branch(match.group(1))
    methods = []
    for name, params in re.findall(r"\(\s*STDMETHODCALLTYPE\s*\*\s*(\w+)\s*\)\s*\((.*?)\)\s*;", body, re.S):
        parameters = split_parameters(params)[1:]  # without This
        methods.append((name, "".join(kind(p) for p in parameters)))
    return methods


def declare_methods(source: str, interface: str, parsed=None):
    """The methods of DECLARE_INTERFACE_(I, Base) / DECLARE_INTERFACE(I) { STDMETHOD(Name)(THIS_ ...) PURE; ... }
    (d3d9.h, d3d10effect.h, whose interfaces without a base have no IUnknown methods)."""
    match = re.search(r"DECLARE_INTERFACE_?\(\s*%s\s*(?:,\s*(\w+)\s*)?\)\s*\{(.*?)\n\};" % interface, source, re.S)
    if not match:
        return None
    base = match.group(1)
    methods = []
    for name, params in re.findall(r"STDMETHOD_?\((?:[^,()]+,\s*)?(\w+)\)\s*\((.*?)\)\s*PURE\s*;", match.group(2), re.S):
        params = params.replace("THIS_", "").replace("THIS", "")
        methods.append((name, "".join(kind(p) for p in split_parameters(params))))
    if parsed and base in parsed:
        # C++: the vtable of the base (whose methods the body may repeat, or leave out, as d3d10effect.h
        # does with IsValid), then the new methods
        known = {name for name, _ in parsed[base]}
        methods = list(parsed[base]) + [m for m in methods if m[0] not in known]
    return methods


def flat_functions(source: str, pattern: str):
    out = {}
    for name, params in re.findall(pattern, source, re.S):
        out[name] = "".join(kind(p) for p in split_parameters(params))
    return out


def main():
    include = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob(r"C:\Program Files (x86)\Windows Kits\10\Include\10.*"))[-1]
    khronos = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "khronos")
    um, shared = os.path.join(include, "um"), os.path.join(include, "shared")

    def read(name):
        for folder in (um, shared):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                return open(path, encoding="latin-1").read()
        raise FileNotFoundError(name)

    interfaces, iids = {}, {}
    for header, names in INTERFACES.items():
        source = read(header)
        for guid, name in re.findall(r'MIDL_INTERFACE\("([0-9A-Fa-f-]+)"\)\s*(\w+)\s*:', source):
            if name in names:
                iids[name] = guid.upper()
        for match in re.finditer(r"DEFINE_GUID\(\s*IID_(\w+)\s*,([^)]*)\)", source):
            name = match.group(1)
            parts = [int(x.strip(), 16) for x in match.group(2).split(",")]
            if name in names and len(parts) == 11:
                iids[name] = ("%08X-%04X-%04X-%02X%02X-" % tuple(parts[:5]) + "".join("%02X" % b for b in parts[5:]))
        for name in names:
            methods = vtbl_methods(source, name) or declare_methods(source, name, interfaces)
            if not methods:
                raise SystemExit(f"{name} not found in {header}")
            interfaces[name] = methods
    iids.setdefault("ID3DBlob", iids.get("ID3D10Blob"))
    # the base interfaces (a QueryInterface for them gives the object's main interface)
    bases = {}
    for header, names in BASES.items():
        source = read(header)
        for guid, name in re.findall(r'MIDL_INTERFACE\("([0-9A-Fa-f-]+)"\)\s*(\w+)\s*:', source):
            if name in names:
                bases[name] = guid.upper()
    gdiplus = flat_functions(read("gdiplusflat.h"), r"GpStatus\s+WINGDIPAPI\s+(Gdip\w+)\s*\((.*?)\)\s*;")
    gdiplus.update(flat_functions(read("gdiplusinit.h"), r"Status\s+WINAPI\s+(Gdiplus\w+)\s*\((.*?)\)\s*;"))
    gl = flat_functions(read(os.path.join("gl", "gl.h")) if os.path.isfile(os.path.join(um, "gl", "gl.h")) else read("gl.h"),
                        r"WINGDIAPI\s+[\w\s\*]+?\s*APIENTRY\s+(gl\w+)\s*\((.*?)\)\s*;")
    glext = flat_functions(open(os.path.join(khronos, "glext.h"), encoding="latin-1").read(),
                           r"GLAPI\s+[\w\s\*]+?\s*APIENTRY\s+(gl\w+)\s*\((.*?)\)\s*;")
    wglext = flat_functions(open(os.path.join(khronos, "wglext.h"), encoding="latin-1").read(),
                            r"\n[\w\s\*]+?\s+WINAPI\s+(wgl\w+)\s*\((.*?)\)\s*;")
    for name in gl:
        glext.pop(name, None)
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dumpulator", "graphics_tables.py")
    with open(out, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("# Generated by emulator/tools/make_graphics_tables.py from the Windows SDK headers (d3d9.h, d3d10*.h,\n"
                "# d3d11*.h, d3d12.h, dxgi*.h, d3dcommon.h, gdiplusflat.h, gdiplusinit.h, gl/gl.h) and Khronos' glext.h /\n"
                "# wglext.h (MIT): the methods of the COM interfaces in vtable order and the parameters of the functions.\n"
                "# p: a pointer-sized value, i / h / b: an integer of 32 / 16 / 8 bits, f: float, d: double, q: 64 bits.\n\n")
        f.write("INTERFACES = " + pprint.pformat(interfaces, width=110, compact=True) + "\n\n")
        f.write("IIDS = " + pprint.pformat(iids, width=110) + "\n\n")
        f.write("GDIPLUS = " + pprint.pformat(gdiplus, width=110, compact=True) + "\n\n")
        f.write("OPENGL = " + pprint.pformat(gl, width=110, compact=True) + "\n\n")
        f.write("OPENGL_EXT = " + pprint.pformat(glext, width=110, compact=True) + "\n\n")
        f.write("WGL_EXT = " + pprint.pformat(wglext, width=110, compact=True) + "\n\n")
        f.write("BASE_IIDS = " + pprint.pformat(bases, width=110) + "\n")
    missing = [n for names in INTERFACES.values() for n in names if n not in iids]
    print(out, len(interfaces), "interfaces,", len(iids), "iids (missing:", missing, "),", len(gdiplus), "gdiplus,", len(gl),
          "gl,", len(glext), "gl ext,", len(wglext), "wgl ext")


if __name__ == "__main__":
    main()
