"""Generate dumpulator/graphics_tables.py from the Windows SDK headers: the methods of the COM
interfaces of Direct3D 9, Direct3D 11 and DXGI in vtable order, and the functions of GDI+ (flat API)
and OpenGL 1.1, each with the kinds of its parameters (p: a pointer-sized value, i: a 32-bit
integer, h: 16 bits, b: 8 bits, f: float, d: double, q: a 64-bit value). The emulator needs them to build vtables and, in 32-bit, to pop the right number
of bytes when a function returns (stdcall).

    python emulator/tools/make_graphics_tables.py [path of the SDK's Include\\<version>]
"""
import glob
import os
import pprint
import re
import sys

INTERFACES = {
    "d3d9.h": ["IDirect3D9", "IDirect3DDevice9", "IDirect3DSurface9", "IDirect3DVertexBuffer9", "IDirect3DIndexBuffer9",
               "IDirect3DTexture9", "IDirect3DSwapChain9", "IDirect3DVertexDeclaration9", "IDirect3DVertexShader9",
               "IDirect3DPixelShader9", "IDirect3DStateBlock9"],
    "d3d11.h": ["ID3D11Device", "ID3D11DeviceContext", "ID3D11Texture2D", "ID3D11Buffer", "ID3D11RenderTargetView",
                "ID3D11DepthStencilView", "ID3D11ShaderResourceView", "ID3D11VertexShader", "ID3D11PixelShader",
                "ID3D11GeometryShader", "ID3D11InputLayout", "ID3D11SamplerState", "ID3D11RasterizerState",
                "ID3D11BlendState", "ID3D11DepthStencilState", "ID3D11Query", "ID3D11Texture1D", "ID3D11Texture3D",
                "ID3D11UnorderedAccessView", "ID3D11ComputeShader", "ID3D11HullShader", "ID3D11DomainShader",
                "ID3D11ClassLinkage", "ID3D11CommandList", "ID3D11Predicate", "ID3D11Counter"],
    "dxgi.h": ["IDXGISwapChain", "IDXGIFactory", "IDXGIFactory1", "IDXGIAdapter", "IDXGIAdapter1", "IDXGIDevice",
               "IDXGIOutput", "IDXGISurface"],
}


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
    if re.search(r"\b(UINT64|ULONGLONG|LONGLONG|INT64|LARGE_INTEGER|ULARGE_INTEGER|LUID)\b", text):
        return "q"
    if re.search(r"\b(H[A-Z]+|HANDLE|\w+_PTR|SIZE_T|SSIZE_T|LPARAM|WPARAM|LRESULT|REF\w+|LP\w+|P[A-Z]\w*|GLsizeiptr|GLintptr)\b", text):
        return "p"
    if re.search(r"\b(GLubyte|GLbyte|GLboolean|BYTE|UCHAR|BOOLEAN|CHAR|char)\b", text):
        return "b"
    if re.search(r"\b(GLshort|GLushort|WORD|SHORT|USHORT|INT16|UINT16|WCHAR|short)\b", text):
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


def vtbl_methods(source: str, interface: str):
    """The methods of a MIDL C vtable (typedef struct IxVtbl { ... } IxVtbl;)."""
    match = re.search(r"typedef struct %sVtbl\s*\{(.*?)\}\s*%sVtbl;" % (interface, interface), source, re.S)
    if not match:
        return None
    methods = []
    for name, params in re.findall(r"\(\s*STDMETHODCALLTYPE\s*\*\s*(\w+)\s*\)\s*\((.*?)\)\s*;", match.group(1), re.S):
        parameters = split_parameters(params)[1:]  # without This
        methods.append((name, "".join(kind(p) for p in parameters)))
    return methods


def declare_methods(source: str, interface: str):
    """The methods of DECLARE_INTERFACE_(I, Base) { STDMETHOD(Name)(THIS_ ...) PURE; ... } (d3d9.h)."""
    match = re.search(r"DECLARE_INTERFACE_\(\s*%s\s*,\s*\w+\s*\)\s*\{(.*?)\n\};" % interface, source, re.S)
    if not match:
        return None
    methods = []
    for name, params in re.findall(r"STDMETHOD_?\((?:[^,()]+,\s*)?(\w+)\)\s*\((.*?)\)\s*PURE\s*;", match.group(1), re.S):
        params = params.replace("THIS_", "").replace("THIS", "")
        methods.append((name, "".join(kind(p) for p in split_parameters(params))))
    return methods


def flat_functions(source: str, pattern: str):
    out = {}
    for name, params in re.findall(pattern, source, re.S):
        out[name] = "".join(kind(p) for p in split_parameters(params))
    return out


def main():
    include = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob(r"C:\Program Files (x86)\Windows Kits\10\Include\10.*"))[-1]
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
            methods = vtbl_methods(source, name) or declare_methods(source, name)
            if not methods:
                raise SystemExit(f"{name} not found in {header}")
            interfaces[name] = methods
    gdiplus = flat_functions(read("gdiplusflat.h"), r"GpStatus\s+WINGDIPAPI\s+(Gdip\w+)\s*\((.*?)\)\s*;")
    gdiplus.update(flat_functions(read("gdiplusinit.h"), r"Status\s+WINAPI\s+(Gdiplus\w+)\s*\((.*?)\)\s*;"))
    gl = flat_functions(read(os.path.join("gl", "gl.h")) if os.path.isfile(os.path.join(um, "gl", "gl.h")) else read("gl.h"),
                        r"WINGDIAPI\s+[\w\s\*]+?\s*APIENTRY\s+(gl\w+)\s*\((.*?)\)\s*;")
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dumpulator", "graphics_tables.py")
    with open(out, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("# Generated by emulator/tools/make_graphics_tables.py from the Windows SDK headers (d3d9.h, d3d11.h,\n"
                "# dxgi.h, gdiplusflat.h, gdiplusinit.h, gl/gl.h): the methods of the COM interfaces in vtable order\n"
                "# and the parameters of the functions. p: a pointer-sized value, i / h / b: an integer of 32 / 16 / 8\n"
                "# bits, f: float, d: double, q: 64 bits.\n\n")
        f.write("INTERFACES = " + pprint.pformat(interfaces, width=110, compact=True) + "\n\n")
        f.write("IIDS = " + pprint.pformat(iids, width=110) + "\n\n")
        f.write("GDIPLUS = " + pprint.pformat(gdiplus, width=110, compact=True) + "\n\n")
        f.write("OPENGL = " + pprint.pformat(gl, width=110, compact=True) + "\n")
    missing = [n for names in INTERFACES.values() for n in names if n not in iids]
    print(out, len(interfaces), "interfaces,", len(iids), "iids (missing:", missing, "),", len(gdiplus), "gdiplus,", len(gl), "gl")


if __name__ == "__main__":
    main()
