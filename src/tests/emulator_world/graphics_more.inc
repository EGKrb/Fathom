// The modes clip, gl3, d3d9sh, d3d10, d3d11gs, d3d11cs, d3d12 and mesh of emulator_world (included by
// target.cpp): what they draw or compute is read back and checked, natively and in the emulator.

#include <d3d10.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include "stages_vs9.h"
#include "stages_ps9.h"
#include "stages_vs_point.h"
#include "stages_gs.h"
#include "stages_ps_color.h"
#include "stages_vs_patch.h"
#include "stages_hs.h"
#include "stages_ds.h"
#include "stages_cs.h"
#include "scene12_vs.h"
#include "scene12_ps.h"

static bool NearRgb(const BYTE* p, int r, int g, int b, int tolerance = 3)
{
    return abs(p[0] - r) <= tolerance && abs(p[1] - g) <= tolerance && abs(p[2] - b) <= tolerance;
}

static void CheckRgb(const char* mode, const char* what, const BYTE* p, int r, int g, int b, int tolerance = 3)
{
    bool ok = NearRgb(p, r, g, b, tolerance);
    if(!ok)
        printf("%s: %s: %u %u %u instead of %d %d %d\n", mode, what, p[0], p[1], p[2], r, g, b);
    Check(ok, what);
}

// ---------------------------------------------------------------- OpenGL: clipping against the near plane
static int GlClip()
{
    HWND window = TestWindow(L"OpenGL clipping");
    HDC dc = GetDC(window);
    PIXELFORMATDESCRIPTOR pfd = { sizeof(pfd), 1, PFD_DRAW_TO_WINDOW | PFD_SUPPORT_OPENGL | PFD_DOUBLEBUFFER, PFD_TYPE_RGBA, 32 };
    SetPixelFormat(dc, ChoosePixelFormat(dc, &pfd), &pfd);
    HGLRC context = wglCreateContext(dc);
    wglMakeCurrent(dc, context);
    glViewport(0, 0, 200, 200);
    glClearColor(0, 0, 0.5f, 1);
    glClear(GL_COLOR_BUFFER_BIT);
    glMatrixMode(GL_PROJECTION);
    glLoadIdentity();
    glFrustum(-1, 1, -1, 1, 1, 10);
    glMatrixMode(GL_MODELVIEW);
    glLoadIdentity();
    // two vertices in front of the camera, one behind it: only the part in front is drawn
    glBegin(GL_TRIANGLES);
    glColor3f(0, 1, 0);
    glVertex3f(-1, -1, -2);
    glVertex3f(1, -1, -2);
    glVertex3f(0, 2, 1);
    glEnd();
    glFinish();
    BYTE inside[4] = {}, above[4] = {}, below[4] = {};
    glReadPixels(100, 75, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, inside);
    glReadPixels(100, 150, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, above);
    glReadPixels(100, 20, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, below);
    printf("clip: inside %u %u %u, above %u %u %u, below %u %u %u\n", inside[0], inside[1], inside[2], above[0], above[1],
           above[2], below[0], below[1], below[2]);
    CheckRgb("clip", "the part of the triangle in front of the near plane", inside, 0, 255, 0);
    CheckRgb("clip", "beyond the near plane", above, 0, 0, 128);
    CheckRgb("clip", "below the triangle", below, 0, 0, 128);
    SwapBuffers(dc);
    wglMakeCurrent(NULL, NULL);
    wglDeleteContext(context);
    ReleaseDC(window, dc);
    DestroyWindow(window);
    return failures;
}

// ---------------------------------------------------------------- OpenGL 3.3 core: buffers, GLSL, framebuffer objects
typedef ptrdiff_t GLsizeiptr;
typedef char GLchar;
#define GLFN(ret, name, ...) typedef ret(APIENTRY* PFN_##name)(__VA_ARGS__); static PFN_##name p_##name;
GLFN(void, glGenBuffers, GLsizei, GLuint*)
GLFN(void, glBindBuffer, GLenum, GLuint)
GLFN(void, glBufferData, GLenum, GLsizeiptr, const void*, GLenum)
GLFN(void, glGenVertexArrays, GLsizei, GLuint*)
GLFN(void, glBindVertexArray, GLuint)
GLFN(void, glVertexAttribPointer, GLuint, GLint, GLenum, GLboolean, GLsizei, const void*)
GLFN(void, glEnableVertexAttribArray, GLuint)
GLFN(GLuint, glCreateShader, GLenum)
GLFN(void, glShaderSource, GLuint, GLsizei, const GLchar* const*, const GLint*)
GLFN(void, glCompileShader, GLuint)
GLFN(void, glGetShaderiv, GLuint, GLenum, GLint*)
GLFN(void, glGetShaderInfoLog, GLuint, GLsizei, GLsizei*, GLchar*)
GLFN(GLuint, glCreateProgram, void)
GLFN(void, glAttachShader, GLuint, GLuint)
GLFN(void, glLinkProgram, GLuint)
GLFN(void, glGetProgramiv, GLuint, GLenum, GLint*)
GLFN(void, glUseProgram, GLuint)
GLFN(GLint, glGetUniformLocation, GLuint, const GLchar*)
GLFN(void, glUniform4f, GLint, GLfloat, GLfloat, GLfloat, GLfloat)
GLFN(void, glUniform1i, GLint, GLint)
GLFN(void, glUniformMatrix4fv, GLint, GLsizei, GLboolean, const GLfloat*)
GLFN(void, glActiveTexture, GLenum)
GLFN(void, glGenFramebuffers, GLsizei, GLuint*)
GLFN(void, glBindFramebuffer, GLenum, GLuint)
GLFN(void, glFramebufferTexture2D, GLenum, GLenum, GLenum, GLuint, GLint)
GLFN(GLenum, glCheckFramebufferStatus, GLenum)
GLFN(void, glBlitFramebuffer, GLint, GLint, GLint, GLint, GLint, GLint, GLint, GLint, GLbitfield, GLenum)
typedef HGLRC(WINAPI* PFN_wglCreateContextAttribsARB)(HDC, HGLRC, const int*);

template <typename T> static bool Load(T& pointer, const char* name)
{
    pointer = (T)wglGetProcAddress(name);
    return pointer != NULL;
}

static GLuint CompileGl(GLenum kind, const char* source)
{
    GLuint shader = p_glCreateShader(kind);
    p_glShaderSource(shader, 1, &source, NULL);
    p_glCompileShader(shader);
    GLint ok = 0;
    p_glGetShaderiv(shader, 0x8B81 /* GL_COMPILE_STATUS */, &ok);
    if(!ok)
    {
        char log[512] = {};
        p_glGetShaderInfoLog(shader, sizeof(log), NULL, log);
        printf("gl3: shader: %s\n", log);
    }
    Check(ok != 0, "a GLSL shader compiled");
    return shader;
}

static int Gl3()
{
    HWND window = TestWindow(L"OpenGL 3.3");
    HDC dc = GetDC(window);
    PIXELFORMATDESCRIPTOR pfd = { sizeof(pfd), 1, PFD_DRAW_TO_WINDOW | PFD_SUPPORT_OPENGL | PFD_DOUBLEBUFFER, PFD_TYPE_RGBA, 32 };
    SetPixelFormat(dc, ChoosePixelFormat(dc, &pfd), &pfd);
    HGLRC legacy = wglCreateContext(dc);
    wglMakeCurrent(dc, legacy);
    PFN_wglCreateContextAttribsARB createContext = NULL;
    Check(Load(createContext, "wglCreateContextAttribsARB"), "wglCreateContextAttribsARB");
    if(!createContext)
        return failures;
    const int attributes[] = { 0x2091, 3, 0x2092, 3, 0x9126, 1 /* core */, 0 };
    HGLRC context = createContext(dc, NULL, attributes);
    Check(context != NULL, "a 3.3 core context");
    wglMakeCurrent(dc, context);
    wglDeleteContext(legacy);
    bool loaded = Load(p_glGenBuffers, "glGenBuffers") && Load(p_glBindBuffer, "glBindBuffer") && Load(p_glBufferData, "glBufferData") &&
                  Load(p_glGenVertexArrays, "glGenVertexArrays") && Load(p_glBindVertexArray, "glBindVertexArray") &&
                  Load(p_glVertexAttribPointer, "glVertexAttribPointer") && Load(p_glEnableVertexAttribArray, "glEnableVertexAttribArray") &&
                  Load(p_glCreateShader, "glCreateShader") && Load(p_glShaderSource, "glShaderSource") &&
                  Load(p_glCompileShader, "glCompileShader") && Load(p_glGetShaderiv, "glGetShaderiv") &&
                  Load(p_glGetShaderInfoLog, "glGetShaderInfoLog") && Load(p_glCreateProgram, "glCreateProgram") &&
                  Load(p_glAttachShader, "glAttachShader") && Load(p_glLinkProgram, "glLinkProgram") &&
                  Load(p_glGetProgramiv, "glGetProgramiv") && Load(p_glUseProgram, "glUseProgram") &&
                  Load(p_glGetUniformLocation, "glGetUniformLocation") && Load(p_glUniform4f, "glUniform4f") &&
                  Load(p_glUniform1i, "glUniform1i") && Load(p_glUniformMatrix4fv, "glUniformMatrix4fv") &&
                  Load(p_glActiveTexture, "glActiveTexture") && Load(p_glGenFramebuffers, "glGenFramebuffers") &&
                  Load(p_glBindFramebuffer, "glBindFramebuffer") && Load(p_glFramebufferTexture2D, "glFramebufferTexture2D") &&
                  Load(p_glCheckFramebufferStatus, "glCheckFramebufferStatus") && Load(p_glBlitFramebuffer, "glBlitFramebuffer");
    Check(loaded, "the OpenGL 3.3 functions");
    if(!loaded)
        return failures;
    printf("gl3: %s\n", (const char*)glGetString(GL_VERSION));

    const char* vertexSource =
        "#version 330 core\n"
        "layout(location = 0) in vec2 position;\n"
        "layout(location = 1) in vec3 color;\n"
        "layout(location = 2) in vec2 uv;\n"
        "uniform mat4 transform;\n"
        "out vec3 vColor;\n"
        "out vec2 vUv;\n"
        "void main() { vColor = color; vUv = uv; gl_Position = transform * vec4(position, 0.0, 1.0); }\n";
    const char* fragmentSource =
        "#version 330 core\n"
        "in vec3 vColor;\n"
        "in vec2 vUv;\n"
        "uniform vec4 tint;\n"
        "uniform sampler2D picture;\n"
        "out vec4 fragColor;\n"
        "void main()\n"
        "{\n"
        "    vec4 texel = texture(picture, vUv);\n"
        "    if (texel.a < 0.5)\n"
        "        discard;\n"
        "    fragColor = vec4(vColor, 1.0) * texel * tint;\n"
        "}\n";
    GLuint program = p_glCreateProgram();
    p_glAttachShader(program, CompileGl(0x8B31, vertexSource));
    p_glAttachShader(program, CompileGl(0x8B30, fragmentSource));
    p_glLinkProgram(program);
    GLint linked = 0;
    p_glGetProgramiv(program, 0x8B82 /* GL_LINK_STATUS */, &linked);
    Check(linked != 0, "the GLSL program linked");

    float vertices[] = {
        -0.5f, -0.5f, 1, 0.5f, 0, 0.25f, 0.25f, 0.5f, -0.5f, 1, 0.5f, 0, 0.25f, 0.25f, 0.0f, 0.5f, 1, 0.5f, 0, 0.25f, 0.25f,
        0.6f, 0.6f, 1, 1, 1, 0, 0, 1.0f, 0.6f, 1, 1, 1, 1, 0, 1.0f, 1.0f, 1, 1, 1, 1, 1,
        0.6f, 0.6f, 1, 1, 1, 0, 0, 1.0f, 1.0f, 1, 1, 1, 1, 1, 0.6f, 1.0f, 1, 1, 1, 0, 1,
    };
    GLuint vao = 0, vbo = 0;
    p_glGenVertexArrays(1, &vao);
    p_glBindVertexArray(vao);
    p_glGenBuffers(1, &vbo);
    p_glBindBuffer(0x8892 /* GL_ARRAY_BUFFER */, vbo);
    p_glBufferData(0x8892, sizeof(vertices), vertices, 0x88E4 /* GL_STATIC_DRAW */);
    p_glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 7 * sizeof(float), (void*)0);
    p_glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 7 * sizeof(float), (void*)(2 * sizeof(float)));
    p_glVertexAttribPointer(2, 2, GL_FLOAT, GL_FALSE, 7 * sizeof(float), (void*)(5 * sizeof(float)));
    p_glEnableVertexAttribArray(0);
    p_glEnableVertexAttribArray(1);
    p_glEnableVertexAttribArray(2);

    // a 2x2 texture: white red / blue, and green fully transparent (discarded)
    const GLubyte texels[16] = { 255, 255, 255, 255, 255, 0, 0, 255, 0, 0, 255, 255, 0, 255, 0, 0 };
    GLuint picture = 0;
    glGenTextures(1, &picture);
    p_glActiveTexture(0x84C0);
    glBindTexture(GL_TEXTURE_2D, picture);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, 2, 2, 0, GL_RGBA, GL_UNSIGNED_BYTE, texels);

    // the scene into a texture of a framebuffer object
    GLuint target = 0, fbo = 0;
    glGenTextures(1, &target);
    glBindTexture(GL_TEXTURE_2D, target);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, 200, 200, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    p_glGenFramebuffers(1, &fbo);
    p_glBindFramebuffer(0x8D40 /* GL_FRAMEBUFFER */, fbo);
    p_glFramebufferTexture2D(0x8D40, 0x8CE0 /* GL_COLOR_ATTACHMENT0 */, GL_TEXTURE_2D, target, 0);
    Check(p_glCheckFramebufferStatus(0x8D40) == 0x8CD5, "a complete framebuffer object");
    glViewport(0, 0, 200, 200);
    glClearColor(0, 0, 0.5f, 1);
    glClear(GL_COLOR_BUFFER_BIT);
    glBindTexture(GL_TEXTURE_2D, picture);
    p_glUseProgram(program);
    const float identity[16] = { 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1 };
    p_glUniformMatrix4fv(p_glGetUniformLocation(program, "transform"), 1, GL_FALSE, identity);
    p_glUniform4f(p_glGetUniformLocation(program, "tint"), 1, 1, 1, 1);
    p_glUniform1i(p_glGetUniformLocation(program, "picture"), 0);
    glDrawArrays(GL_TRIANGLES, 0, 9);
    // copied to the window, read there
    p_glBindFramebuffer(0x8CA9 /* GL_DRAW_FRAMEBUFFER */, 0);
    p_glBlitFramebuffer(0, 0, 200, 200, 0, 0, 200, 200, GL_COLOR_BUFFER_BIT, GL_NEAREST);
    p_glBindFramebuffer(0x8D40, 0);
    glFinish();
    BYTE center[4] = {}, texel[4][4] = {};
    glReadPixels(100, 80, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, center);
    const int at[4][2] = { { 170, 170 }, { 190, 170 }, { 170, 190 }, { 190, 190 } };
    for(int i = 0; i < 4; i++)
        glReadPixels(at[i][0], at[i][1], 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, texel[i]);
    printf("gl3: triangle %u %u %u\n", center[0], center[1], center[2]);
    CheckRgb("gl3", "the triangle of the GLSL program", center, 255, 128, 0);
    CheckRgb("gl3", "a texel (white)", texel[0], 255, 255, 255);
    CheckRgb("gl3", "a texel (red)", texel[1], 255, 0, 0);
    CheckRgb("gl3", "a texel (blue)", texel[2], 0, 0, 255);
    CheckRgb("gl3", "a discarded texel (the background)", texel[3], 0, 0, 128);
    SwapBuffers(dc);
    wglMakeCurrent(NULL, NULL);
    wglDeleteContext(context);
    ReleaseDC(window, dc);
    DestroyWindow(window);
    return failures;
}

// ---------------------------------------------------------------- Direct3D 9 shaders
static int D3D9Shaders()
{
    HWND window = TestWindow(L"Direct3D 9 shaders");
    IDirect3D9* d3d = Direct3DCreate9(D3D_SDK_VERSION);
    if(!d3d)
        return Check(false, "Direct3DCreate9"), failures;
    D3DPRESENT_PARAMETERS pp = {};
    pp.Windowed = TRUE;
    pp.SwapEffect = D3DSWAPEFFECT_DISCARD;
    pp.BackBufferFormat = D3DFMT_X8R8G8B8;
    pp.BackBufferWidth = pp.BackBufferHeight = 200;
    pp.hDeviceWindow = window;
    IDirect3DDevice9* device = NULL;
    d3d->CreateDevice(D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL, window, D3DCREATE_SOFTWARE_VERTEXPROCESSING, &pp, &device);
    Check(device != NULL, "CreateDevice");
    if(!device)
        return failures;
    IDirect3DVertexShader9* vs = NULL;
    IDirect3DPixelShader9* ps = NULL;
    Check(SUCCEEDED(device->CreateVertexShader((const DWORD*)Scene9VertexShader, &vs)), "CreateVertexShader (vs_2_0)");
    Check(SUCCEEDED(device->CreatePixelShader((const DWORD*)Scene9PixelShader, &ps)), "CreatePixelShader (ps_2_0)");
    const D3DVERTEXELEMENT9 elements[] = {
        { 0, 0, D3DDECLTYPE_FLOAT4, D3DDECLMETHOD_DEFAULT, D3DDECLUSAGE_POSITION, 0 },
        { 0, 16, D3DDECLTYPE_D3DCOLOR, D3DDECLMETHOD_DEFAULT, D3DDECLUSAGE_COLOR, 0 },
        { 0, 20, D3DDECLTYPE_FLOAT2, D3DDECLMETHOD_DEFAULT, D3DDECLUSAGE_TEXCOORD, 0 },
        D3DDECL_END(),
    };
    IDirect3DVertexDeclaration9* declaration = NULL;
    device->CreateVertexDeclaration(elements, &declaration);
    IDirect3DTexture9* texture = NULL;
    const DWORD texels[4] = { 0xFFFFFFFF, 0xFFFF0000, 0xFF0000FF, 0xFF00FF00 };
    device->CreateTexture(2, 2, 1, 0, D3DFMT_A8R8G8B8, D3DPOOL_MANAGED, &texture, NULL);
    D3DLOCKED_RECT locked;
    if(texture && SUCCEEDED(texture->LockRect(0, &locked, NULL, 0)))
    {
        for(int y = 0; y < 2; y++)
            memcpy((BYTE*)locked.pBits + y * locked.Pitch, texels + 2 * y, 8);
        texture->UnlockRect(0);
    }
    struct Vertex
    {
        float x, y, z, w;
        DWORD color;
        float u, v;
    } vertices[9] = {
        { -0.5f, -0.5f, 0.5f, 1, 0xFFFF8000, 0.25f, 0.25f }, { 0, 0.5f, 0.5f, 1, 0xFFFF8000, 0.25f, 0.25f }, { 0.5f, -0.5f, 0.5f, 1, 0xFFFF8000, 0.25f, 0.25f },
        { 0.6f, 1, 0.5f, 1, 0xFFFFFFFF, 0, 0 }, { 1, 1, 0.5f, 1, 0xFFFFFFFF, 1, 0 }, { 1, 0.6f, 0.5f, 1, 0xFFFFFFFF, 1, 1 },
        { 0.6f, 1, 0.5f, 1, 0xFFFFFFFF, 0, 0 }, { 1, 0.6f, 0.5f, 1, 0xFFFFFFFF, 1, 1 }, { 0.6f, 0.6f, 0.5f, 1, 0xFFFFFFFF, 0, 1 },
    };
    const float constants[20] = { 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 1, 1, 1, 1 };
    device->Clear(0, NULL, D3DCLEAR_TARGET, D3DCOLOR_XRGB(0, 0, 128), 1.0f, 0);
    device->BeginScene();
    device->SetVertexDeclaration(declaration);
    device->SetVertexShader(vs);
    device->SetPixelShader(ps);
    device->SetVertexShaderConstantF(0, constants, 5);
    device->SetTexture(0, texture);
    device->SetSamplerState(0, D3DSAMP_MAGFILTER, D3DTEXF_POINT);
    device->SetSamplerState(0, D3DSAMP_MINFILTER, D3DTEXF_POINT);
    Check(SUCCEEDED(device->DrawPrimitiveUP(D3DPT_TRIANGLELIST, 3, vertices, sizeof(Vertex))), "DrawPrimitiveUP with shaders");
    device->EndScene();
    IDirect3DSurface9* back = NULL;
    IDirect3DSurface9* copy = NULL;
    device->GetBackBuffer(0, 0, D3DBACKBUFFER_TYPE_MONO, &back);
    device->CreateOffscreenPlainSurface(200, 200, D3DFMT_X8R8G8B8, D3DPOOL_SYSTEMMEM, &copy, NULL);
    if(back && copy && SUCCEEDED(device->GetRenderTargetData(back, copy)) && SUCCEEDED(copy->LockRect(&locked, NULL, D3DLOCK_READONLY)))
    {
        auto pixel = [&](int x, int y) {
            DWORD value = ((DWORD*)((BYTE*)locked.pBits + y * locked.Pitch))[x];
            static BYTE rgb[4];
            rgb[0] = (value >> 16) & 0xFF;
            rgb[1] = (value >> 8) & 0xFF;
            rgb[2] = value & 0xFF;
            return (const BYTE*)rgb;
        };
        CheckRgb("d3d9sh", "the triangle of the shaders", pixel(100, 120), 255, 128, 0);
        CheckRgb("d3d9sh", "the background", pixel(10, 190), 0, 0, 128);
        CheckRgb("d3d9sh", "a texel (white)", pixel(170, 10), 255, 255, 255);
        CheckRgb("d3d9sh", "a texel (red)", pixel(190, 10), 255, 0, 0);
        CheckRgb("d3d9sh", "a texel (blue)", pixel(170, 30), 0, 0, 255);
        CheckRgb("d3d9sh", "a texel (green)", pixel(190, 30), 0, 255, 0);
        copy->UnlockRect();
    }
    else
        Check(false, "GetRenderTargetData");
    device->Present(NULL, NULL, NULL, NULL);
    printf("d3d9sh: %d check(s) failed\n", failures);
    if(copy)
        copy->Release();
    if(back)
        back->Release();
    if(texture)
        texture->Release();
    if(declaration)
        declaration->Release();
    if(vs)
        vs->Release();
    if(ps)
        ps->Release();
    device->Release();
    d3d->Release();
    DestroyWindow(window);
    return failures;
}

// ---------------------------------------------------------------- Direct3D 10: the scene of scene.hlsl
struct SceneVertex
{
    float x, y;
    float r, g, b, a;
    float u, v;
};

static const SceneVertex SceneVertices[9] = {
    { -0.5f, -0.5f, 1, 0.5f, 0, 1, 0.25f, 0.25f }, { 0.0f, 0.5f, 1, 0.5f, 0, 1, 0.25f, 0.25f }, { 0.5f, -0.5f, 1, 0.5f, 0, 1, 0.25f, 0.25f },
    { 0.6f, 1.0f, 1, 1, 1, 1, 0, 0 }, { 1.0f, 1.0f, 1, 1, 1, 1, 1, 0 }, { 1.0f, 0.6f, 1, 1, 1, 1, 1, 1 },
    { 0.6f, 1.0f, 1, 1, 1, 1, 0, 0 }, { 1.0f, 0.6f, 1, 1, 1, 1, 1, 1 }, { 0.6f, 0.6f, 1, 1, 1, 1, 0, 1 },
};
static const UINT SceneTexels[4] = { 0xFFFFFFFF, 0xFF0000FF, 0xFFFF0000, 0xFF00FF00 }; // R8G8B8A8: white red / blue green

static void CheckScene(const char* mode, const BYTE* data, UINT pitch)
{
    auto at = [&](int x, int y) { return data + y * pitch + x * 4; };
    CheckRgb(mode, "the background", at(10, 190), 0, 153, 0);
    CheckRgb(mode, "the triangle drawn by the shaders", at(100, 120), 255, 128, 0);
    CheckRgb(mode, "a texel (white)", at(170, 10), 255, 255, 255);
    CheckRgb(mode, "a texel (red)", at(190, 10), 255, 0, 0);
    CheckRgb(mode, "a texel (blue)", at(170, 30), 0, 0, 255);
    CheckRgb(mode, "a texel (green)", at(190, 30), 0, 255, 0);
}

static int D3D10()
{
    HWND window = TestWindow(L"Direct3D 10");
    DXGI_SWAP_CHAIN_DESC sd = {};
    sd.BufferCount = 1;
    sd.BufferDesc.Width = sd.BufferDesc.Height = 200;
    sd.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    sd.OutputWindow = window;
    sd.SampleDesc.Count = 1;
    sd.Windowed = TRUE;
    IDXGISwapChain* chain = NULL;
    ID3D10Device* device = NULL;
    HRESULT hr = D3D10CreateDeviceAndSwapChain(NULL, D3D10_DRIVER_TYPE_HARDWARE, NULL, 0, D3D10_SDK_VERSION, &sd, &chain, &device);
    Check(SUCCEEDED(hr) && device && chain, "D3D10CreateDeviceAndSwapChain");
    if(!device || !chain)
        return failures;
    ID3D10Texture2D* back = NULL;
    chain->GetBuffer(0, __uuidof(ID3D10Texture2D), (void**)&back);
    ID3D10RenderTargetView* view = NULL;
    device->CreateRenderTargetView(back, NULL, &view);
    float green[4] = { 0, 0.6f, 0, 1 };
    device->ClearRenderTargetView(view, green);
    D3D10_BUFFER_DESC bd = { sizeof(SceneVertices), D3D10_USAGE_DEFAULT, D3D10_BIND_VERTEX_BUFFER };
    D3D10_SUBRESOURCE_DATA initial = { SceneVertices };
    ID3D10Buffer* vertices = NULL;
    device->CreateBuffer(&bd, &initial, &vertices);
    D3D10_BUFFER_DESC cd = { 16, D3D10_USAGE_DYNAMIC, D3D10_BIND_CONSTANT_BUFFER, D3D10_CPU_ACCESS_WRITE };
    ID3D10Buffer* constants = NULL;
    device->CreateBuffer(&cd, NULL, &constants);
    void* mapped = NULL;
    if(constants && SUCCEEDED(constants->Map(D3D10_MAP_WRITE_DISCARD, 0, &mapped)))
    {
        const float tint[4] = { 1, 1, 1, 1 };
        memcpy(mapped, tint, 16);
        constants->Unmap();
    }
    D3D10_TEXTURE2D_DESC td = { 2, 2, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, { 1, 0 }, D3D10_USAGE_DEFAULT, D3D10_BIND_SHADER_RESOURCE };
    D3D10_SUBRESOURCE_DATA pixels = { SceneTexels, 8 };
    ID3D10Texture2D* picture = NULL;
    ID3D10ShaderResourceView* pictureView = NULL;
    device->CreateTexture2D(&td, &pixels, &picture);
    if(picture)
        device->CreateShaderResourceView(picture, NULL, &pictureView);
    D3D10_SAMPLER_DESC sampler = {};
    sampler.Filter = D3D10_FILTER_MIN_MAG_MIP_POINT;
    sampler.AddressU = sampler.AddressV = sampler.AddressW = D3D10_TEXTURE_ADDRESS_CLAMP;
    sampler.MaxLOD = D3D10_FLOAT32_MAX;
    ID3D10SamplerState* nearest = NULL;
    device->CreateSamplerState(&sampler, &nearest);
    ID3D10VertexShader* vs = NULL;
    ID3D10PixelShader* ps = NULL;
    ID3D10InputLayout* layout = NULL;
    device->CreateVertexShader(SceneVertexShader, sizeof(SceneVertexShader), &vs);
    device->CreatePixelShader(ScenePixelShader, sizeof(ScenePixelShader), &ps);
    D3D10_INPUT_ELEMENT_DESC elements[3] = {
        { "POSITION", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0, D3D10_INPUT_PER_VERTEX_DATA, 0 },
        { "COLOR", 0, DXGI_FORMAT_R32G32B32A32_FLOAT, 0, D3D10_APPEND_ALIGNED_ELEMENT, D3D10_INPUT_PER_VERTEX_DATA, 0 },
        { "TEXCOORD", 0, DXGI_FORMAT_R32G32_FLOAT, 0, D3D10_APPEND_ALIGNED_ELEMENT, D3D10_INPUT_PER_VERTEX_DATA, 0 },
    };
    device->CreateInputLayout(elements, 3, SceneVertexShader, sizeof(SceneVertexShader), &layout);
    Check(vertices && constants && pictureView && nearest && vs && ps && layout, "the objects of the Direct3D 10 scene");
    if(vertices && constants && pictureView && nearest && vs && ps && layout)
    {
        UINT stride = sizeof(SceneVertex), offset = 0;
        D3D10_VIEWPORT viewport = { 0, 0, 200, 200, 0, 1 };
        device->OMSetRenderTargets(1, &view, NULL);
        device->RSSetViewports(1, &viewport);
        device->IASetInputLayout(layout);
        device->IASetPrimitiveTopology(D3D10_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        device->IASetVertexBuffers(0, 1, &vertices, &stride, &offset);
        device->VSSetShader(vs);
        device->VSSetConstantBuffers(0, 1, &constants);
        device->PSSetShader(ps);
        device->PSSetShaderResources(0, 1, &pictureView);
        device->PSSetSamplers(0, 1, &nearest);
        device->Draw(9, 0);
    }
    D3D10_TEXTURE2D_DESC desc;
    back->GetDesc(&desc);
    desc.Usage = D3D10_USAGE_STAGING;
    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D10_CPU_ACCESS_READ;
    ID3D10Texture2D* staging = NULL;
    device->CreateTexture2D(&desc, NULL, &staging);
    if(staging)
    {
        device->CopyResource(staging, back);
        D3D10_MAPPED_TEXTURE2D readback;
        if(SUCCEEDED(staging->Map(0, D3D10_MAP_READ, 0, &readback)))
        {
            CheckScene("d3d10", (const BYTE*)readback.pData, readback.RowPitch);
            staging->Unmap(0);
        }
        else
            Check(false, "Map of the staging texture");
        staging->Release();
    }
    chain->Present(0, 0);
    printf("d3d10: %d check(s) failed\n", failures);
    ID3D10DeviceChild* children[] = { layout, ps, vs, nearest, pictureView, picture, constants, vertices, view, back };
    for(auto child : children)
        if(child)
            child->Release();
    device->Release();
    chain->Release();
    DestroyWindow(window);
    return failures;
}

// ---------------------------------------------------------------- Direct3D 11: an offscreen device, its read back
static bool Device11(ID3D11Device** device, ID3D11DeviceContext** context)
{
    D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_11_0;
    return SUCCEEDED(D3D11CreateDevice(NULL, D3D_DRIVER_TYPE_HARDWARE, NULL, 0, &level, 1, D3D11_SDK_VERSION, device, NULL, context));
}

static ID3D11Texture2D* Texture11(ID3D11Device* device, UINT width, UINT height, DXGI_FORMAT format, UINT bind)
{
    D3D11_TEXTURE2D_DESC desc = { width, height, 1, 1, format, { 1, 0 }, D3D11_USAGE_DEFAULT, bind };
    ID3D11Texture2D* texture = NULL;
    device->CreateTexture2D(&desc, NULL, &texture);
    return texture;
}

// the texture's pixels copied through a staging texture (into data, rows of pitch bytes)
static bool Read11(ID3D11Device* device, ID3D11DeviceContext* context, ID3D11Resource* resource, std::vector<BYTE>& data, UINT& pitch)
{
    ID3D11Texture2D* texture = (ID3D11Texture2D*)resource;
    D3D11_TEXTURE2D_DESC desc;
    texture->GetDesc(&desc);
    desc.Usage = D3D11_USAGE_STAGING;
    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    desc.MiscFlags = 0;
    ID3D11Texture2D* staging = NULL;
    device->CreateTexture2D(&desc, NULL, &staging);
    if(!staging)
        return false;
    context->CopyResource(staging, texture);
    D3D11_MAPPED_SUBRESOURCE mapped;
    bool ok = SUCCEEDED(context->Map(staging, 0, D3D11_MAP_READ, 0, &mapped));
    if(ok)
    {
        pitch = mapped.RowPitch;
        data.assign((BYTE*)mapped.pData, (BYTE*)mapped.pData + mapped.RowPitch * desc.Height);
        context->Unmap(staging, 0);
    }
    staging->Release();
    return ok;
}

static int D3D11Stages()
{
    ID3D11Device* device = NULL;
    ID3D11DeviceContext* context = NULL;
    Check(Device11(&device, &context), "D3D11CreateDevice");
    if(!device)
        return failures;
    ID3D11Texture2D* target = Texture11(device, 200, 200, DXGI_FORMAT_R8G8B8A8_UNORM, D3D11_BIND_RENDER_TARGET);
    ID3D11RenderTargetView* view = NULL;
    device->CreateRenderTargetView(target, NULL, &view);
    float background[4] = { 0, 0, 0.5f, 1 };
    context->ClearRenderTargetView(view, background);
    D3D11_VIEWPORT viewport = { 0, 0, 200, 200, 0, 1 };
    context->OMSetRenderTargets(1, &view, NULL);
    context->RSSetViewports(1, &viewport);

    // points made squares by the geometry shader
    ID3D11VertexShader* pointVs = NULL;
    ID3D11GeometryShader* gs = NULL;
    ID3D11PixelShader* ps = NULL;
    ID3D11InputLayout* pointLayout = NULL;
    device->CreateVertexShader(PointVertexShader, sizeof(PointVertexShader), NULL, &pointVs);
    device->CreateGeometryShader(SquareGeometryShader, sizeof(SquareGeometryShader), NULL, &gs);
    device->CreatePixelShader(ColorPixelShader, sizeof(ColorPixelShader), NULL, &ps);
    D3D11_INPUT_ELEMENT_DESC pointElements[2] = {
        { "POSITION", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0, D3D11_INPUT_PER_VERTEX_DATA, 0 },
        { "COLOR", 0, DXGI_FORMAT_R32G32B32A32_FLOAT, 0, 8, D3D11_INPUT_PER_VERTEX_DATA, 0 },
    };
    device->CreateInputLayout(pointElements, 2, PointVertexShader, sizeof(PointVertexShader), &pointLayout);
    const float points[18] = { -0.5f, 0.5f, 1, 0, 0, 1, 0, 0.5f, 0, 1, 0, 1, 0.5f, 0.5f, 0, 0, 1, 1 };
    D3D11_BUFFER_DESC bd = { sizeof(points), D3D11_USAGE_DEFAULT, D3D11_BIND_VERTEX_BUFFER };
    D3D11_SUBRESOURCE_DATA initial = { points };
    ID3D11Buffer* pointBuffer = NULL;
    device->CreateBuffer(&bd, &initial, &pointBuffer);
    Check(pointVs && gs && ps && pointLayout && pointBuffer, "the geometry shader's objects");
    UINT stride = 24, offset = 0;
    context->IASetInputLayout(pointLayout);
    context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_POINTLIST);
    context->IASetVertexBuffers(0, 1, &pointBuffer, &stride, &offset);
    context->VSSetShader(pointVs, NULL, 0);
    context->GSSetShader(gs, NULL, 0);
    context->PSSetShader(ps, NULL, 0);
    context->Draw(3, 0);
    context->GSSetShader(NULL, NULL, 0);

    // a triangle patch tessellated, colored by its domain location
    ID3D11VertexShader* patchVs = NULL;
    ID3D11HullShader* hs = NULL;
    ID3D11DomainShader* ds = NULL;
    ID3D11InputLayout* patchLayout = NULL;
    device->CreateVertexShader(PatchVertexShader, sizeof(PatchVertexShader), NULL, &patchVs);
    device->CreateHullShader(PatchHullShader, sizeof(PatchHullShader), NULL, &hs);
    device->CreateDomainShader(PatchDomainShader, sizeof(PatchDomainShader), NULL, &ds);
    D3D11_INPUT_ELEMENT_DESC patchElements[1] = { { "POSITION", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0, D3D11_INPUT_PER_VERTEX_DATA, 0 } };
    device->CreateInputLayout(patchElements, 1, PatchVertexShader, sizeof(PatchVertexShader), &patchLayout);
    const float patch[6] = { -0.6f, -0.9f, 0, -0.2f, 0.6f, -0.9f };
    D3D11_BUFFER_DESC pd = { sizeof(patch), D3D11_USAGE_DEFAULT, D3D11_BIND_VERTEX_BUFFER };
    D3D11_SUBRESOURCE_DATA patchData = { patch };
    ID3D11Buffer* patchBuffer = NULL;
    device->CreateBuffer(&pd, &patchData, &patchBuffer);
    Check(patchVs && hs && ds && patchLayout && patchBuffer, "the tessellation's objects");
    stride = 8;
    context->IASetInputLayout(patchLayout);
    context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_3_CONTROL_POINT_PATCHLIST);
    context->IASetVertexBuffers(0, 1, &patchBuffer, &stride, &offset);
    context->VSSetShader(patchVs, NULL, 0);
    context->HSSetShader(hs, NULL, 0);
    context->DSSetShader(ds, NULL, 0);
    context->Draw(3, 0);

    std::vector<BYTE> data;
    UINT pitch = 0;
    if(Read11(device, context, target, data, pitch))
    {
        auto at = [&](int x, int y) { return &data[y * pitch + x * 4]; };
        CheckRgb("d3d11gs", "the square of the first point", at(50, 50), 255, 0, 0);
        CheckRgb("d3d11gs", "the square of the second point", at(100, 50), 0, 255, 0);
        CheckRgb("d3d11gs", "the square of the third point", at(150, 50), 0, 0, 255);
        CheckRgb("d3d11gs", "the tessellated patch (its middle)", at(100, 166), 85, 85, 85, 4);
        CheckRgb("d3d11gs", "the background", at(100, 110), 0, 0, 128);
        printf("d3d11gs: patch %u %u %u\n", at(100, 166)[0], at(100, 166)[1], at(100, 166)[2]);
    }
    else
        Check(false, "the read back");
    printf("d3d11gs: %d check(s) failed\n", failures);
    IUnknown* objects[] = { patchBuffer, patchLayout, ds, hs, patchVs, pointBuffer, pointLayout, ps, gs, pointVs, view, target, context, device };
    for(auto object : objects)
        if(object)
            object->Release();
    return failures;
}

static int D3D11Compute()
{
    ID3D11Device* device = NULL;
    ID3D11DeviceContext* context = NULL;
    Check(Device11(&device, &context), "D3D11CreateDevice");
    if(!device)
        return failures;
    ID3D11Texture2D* gradient = Texture11(device, 16, 16, DXGI_FORMAT_R32G32B32A32_FLOAT, D3D11_BIND_UNORDERED_ACCESS);
    ID3D11UnorderedAccessView* gradientView = NULL;
    device->CreateUnorderedAccessView(gradient, NULL, &gradientView);
    D3D11_BUFFER_DESC bd = { 16, D3D11_USAGE_DEFAULT, D3D11_BIND_UNORDERED_ACCESS, 0, D3D11_RESOURCE_MISC_BUFFER_ALLOW_RAW_VIEWS };
    const UINT zeros[4] = {};
    D3D11_SUBRESOURCE_DATA initial = { zeros };
    ID3D11Buffer* histogram = NULL;
    device->CreateBuffer(&bd, &initial, &histogram);
    D3D11_UNORDERED_ACCESS_VIEW_DESC raw = {};
    raw.Format = DXGI_FORMAT_R32_TYPELESS;
    raw.ViewDimension = D3D11_UAV_DIMENSION_BUFFER;
    raw.Buffer.NumElements = 4;
    raw.Buffer.Flags = D3D11_BUFFER_UAV_FLAG_RAW;
    ID3D11UnorderedAccessView* histogramView = NULL;
    device->CreateUnorderedAccessView(histogram, &raw, &histogramView);
    ID3D11ComputeShader* cs = NULL;
    device->CreateComputeShader(FillComputeShader, sizeof(FillComputeShader), NULL, &cs);
    Check(gradientView && histogramView && cs, "the compute shader's objects");
    ID3D11UnorderedAccessView* views[2] = { gradientView, histogramView };
    context->CSSetShader(cs, NULL, 0);
    context->CSSetUnorderedAccessViews(0, 2, views, NULL);
    context->Dispatch(2, 2, 1);
    std::vector<BYTE> data;
    UINT pitch = 0;
    if(Read11(device, context, gradient, data, pitch))
    {
        const float* texel = (const float*)&data[10 * pitch + 5 * 16];
        bool ok = fabs(texel[0] - 5 / 15.0f) < 0.01f && fabs(texel[1] - 10 / 15.0f) < 0.01f && fabs(texel[2] - 0.5f) < 0.01f;
        if(!ok)
            printf("d3d11cs: gradient %f %f %f\n", texel[0], texel[1], texel[2]);
        Check(ok, "the texture written by the compute shader");
    }
    D3D11_BUFFER_DESC sd = { 16, D3D11_USAGE_STAGING, 0, D3D11_CPU_ACCESS_READ };
    ID3D11Buffer* staging = NULL;
    device->CreateBuffer(&sd, NULL, &staging);
    if(staging)
    {
        context->CopyResource(staging, histogram);
        D3D11_MAPPED_SUBRESOURCE mapped;
        if(SUCCEEDED(context->Map(staging, 0, D3D11_MAP_READ, 0, &mapped)))
        {
            const UINT* bins = (const UINT*)mapped.pData;
            printf("d3d11cs: histogram %u %u %u %u\n", bins[0], bins[1], bins[2], bins[3]);
            Check(bins[0] == 480 && bins[1] == 480 && bins[2] == 480 && bins[3] == 480, "the atomics of the compute shader");
            context->Unmap(staging, 0);
        }
        staging->Release();
    }
    printf("d3d11cs: %d check(s) failed\n", failures);
    IUnknown* objects[] = { cs, histogramView, histogram, gradientView, gradient, context, device };
    for(auto object : objects)
        if(object)
            object->Release();
    return failures;
}

static int Mesh()
{
    ID3D11Device* device = NULL;
    ID3D11DeviceContext* context = NULL;
    Check(Device11(&device, &context), "D3D11CreateDevice");
    if(!device)
        return failures;
    const int grid = 100;
    std::vector<SceneVertex> vertices;
    std::vector<UINT> indices;
    for(int y = 0; y <= grid; y++)
        for(int x = 0; x <= grid; x++)
            vertices.push_back({ -1 + 2.0f * x / grid, 1 - 2.0f * y / grid, (float)x / grid, (float)y / grid, 0.5f, 1, 0.25f, 0.25f });
    for(int y = 0; y < grid; y++)
        for(int x = 0; x < grid; x++)
        {
            UINT a = y * (grid + 1) + x;
            UINT quad[6] = { a, a + 1, a + grid + 2, a, a + grid + 2, a + grid + 1 };
            indices.insert(indices.end(), quad, quad + 6);
        }
    ID3D11Texture2D* target = Texture11(device, 200, 200, DXGI_FORMAT_R8G8B8A8_UNORM, D3D11_BIND_RENDER_TARGET);
    ID3D11RenderTargetView* view = NULL;
    device->CreateRenderTargetView(target, NULL, &view);
    D3D11_BUFFER_DESC vd = { (UINT)(vertices.size() * sizeof(SceneVertex)), D3D11_USAGE_DEFAULT, D3D11_BIND_VERTEX_BUFFER };
    D3D11_SUBRESOURCE_DATA vdata = { vertices.data() };
    D3D11_BUFFER_DESC id = { (UINT)(indices.size() * 4), D3D11_USAGE_DEFAULT, D3D11_BIND_INDEX_BUFFER };
    D3D11_SUBRESOURCE_DATA idata = { indices.data() };
    const float tint[4] = { 1, 1, 1, 1 };
    D3D11_BUFFER_DESC cd = { 16, D3D11_USAGE_DEFAULT, D3D11_BIND_CONSTANT_BUFFER };
    D3D11_SUBRESOURCE_DATA cdata = { tint };
    const UINT white = 0xFFFFFFFF;
    D3D11_TEXTURE2D_DESC td = { 1, 1, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, { 1, 0 }, D3D11_USAGE_DEFAULT, D3D11_BIND_SHADER_RESOURCE };
    D3D11_SUBRESOURCE_DATA tdata = { &white, 4 };
    ID3D11Buffer *vb = NULL, *ib = NULL, *cb = NULL;
    ID3D11Texture2D* picture = NULL;
    ID3D11ShaderResourceView* pictureView = NULL;
    device->CreateBuffer(&vd, &vdata, &vb);
    device->CreateBuffer(&id, &idata, &ib);
    device->CreateBuffer(&cd, &cdata, &cb);
    device->CreateTexture2D(&td, &tdata, &picture);
    device->CreateShaderResourceView(picture, NULL, &pictureView);
    ID3D11VertexShader* vs = NULL;
    ID3D11PixelShader* ps = NULL;
    ID3D11InputLayout* layout = NULL;
    device->CreateVertexShader(SceneVertexShader, sizeof(SceneVertexShader), NULL, &vs);
    device->CreatePixelShader(ScenePixelShader, sizeof(ScenePixelShader), NULL, &ps);
    D3D11_INPUT_ELEMENT_DESC elements[3] = {
        { "POSITION", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0, D3D11_INPUT_PER_VERTEX_DATA, 0 },
        { "COLOR", 0, DXGI_FORMAT_R32G32B32A32_FLOAT, 0, 8, D3D11_INPUT_PER_VERTEX_DATA, 0 },
        { "TEXCOORD", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 24, D3D11_INPUT_PER_VERTEX_DATA, 0 },
    };
    device->CreateInputLayout(elements, 3, SceneVertexShader, sizeof(SceneVertexShader), &layout);
    UINT stride = sizeof(SceneVertex), offset = 0;
    D3D11_VIEWPORT viewport = { 0, 0, 200, 200, 0, 1 };
    context->OMSetRenderTargets(1, &view, NULL);
    context->RSSetViewports(1, &viewport);
    context->IASetInputLayout(layout);
    context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    context->IASetVertexBuffers(0, 1, &vb, &stride, &offset);
    context->IASetIndexBuffer(ib, DXGI_FORMAT_R32_UINT, 0);
    context->VSSetShader(vs, NULL, 0);
    context->VSSetConstantBuffers(0, 1, &cb);
    context->PSSetShader(ps, NULL, 0);
    context->PSSetShaderResources(0, 1, &pictureView);
    LARGE_INTEGER frequency, start, end;
    QueryPerformanceFrequency(&frequency);
    QueryPerformanceCounter(&start);
    context->DrawIndexed((UINT)indices.size(), 0, 0);
    std::vector<BYTE> data;
    UINT pitch = 0;
    bool read = Read11(device, context, target, data, pitch);
    QueryPerformanceCounter(&end);
    printf("mesh: %u triangles in %.0f ms\n", (UINT)(indices.size() / 3), 1000.0 * (end.QuadPart - start.QuadPart) / frequency.QuadPart);
    if(read)
    {
        CheckRgb("mesh", "the middle of the mesh", &data[100 * pitch + 100 * 4], 128, 128, 128, 4);
        CheckRgb("mesh", "a corner of the mesh", &data[2 * pitch + 197 * 4], 252, 3, 128, 5);
    }
    else
        Check(false, "the read back");
    IUnknown* objects[] = { layout, ps, vs, pictureView, picture, cb, ib, vb, view, target, context, device };
    for(auto object : objects)
        if(object)
            object->Release();
    return failures;
}

// ---------------------------------------------------------------- Direct3D 12: the scene through a flip swap chain
static void Barrier(ID3D12GraphicsCommandList* list, ID3D12Resource* resource, D3D12_RESOURCE_STATES before, D3D12_RESOURCE_STATES after)
{
    D3D12_RESOURCE_BARRIER barrier = {};
    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier.Transition.pResource = resource;
    barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    barrier.Transition.StateBefore = before;
    barrier.Transition.StateAfter = after;
    list->ResourceBarrier(1, &barrier);
}

static ID3D12Resource* Buffer12(ID3D12Device* device, D3D12_HEAP_TYPE heap, UINT64 size, D3D12_RESOURCE_STATES state)
{
    D3D12_HEAP_PROPERTIES properties = { heap };
    D3D12_RESOURCE_DESC desc = { D3D12_RESOURCE_DIMENSION_BUFFER, 0, size, 1, 1, 1, DXGI_FORMAT_UNKNOWN, { 1, 0 },
                                 D3D12_TEXTURE_LAYOUT_ROW_MAJOR, D3D12_RESOURCE_FLAG_NONE };
    ID3D12Resource* resource = NULL;
    device->CreateCommittedResource(&properties, D3D12_HEAP_FLAG_NONE, &desc, state, NULL, __uuidof(ID3D12Resource), (void**)&resource);
    return resource;
}

// the scene of the d3d12 mode (shader model 5.1) and of the dxil mode (the same shaders in shader model 6)
static int D3D12Scene(const char* mode, const void* vs, SIZE_T vsSize, const void* ps, SIZE_T psSize)
{
    HWND window = TestWindow(L"Direct3D 12");
    ID3D12Device* device = NULL;
    if(FAILED(D3D12CreateDevice(NULL, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device), (void**)&device)))
        return Check(false, "D3D12CreateDevice"), failures;
    D3D12_COMMAND_QUEUE_DESC qd = {};
    ID3D12CommandQueue* queue = NULL;
    device->CreateCommandQueue(&qd, __uuidof(ID3D12CommandQueue), (void**)&queue);
    IDXGIFactory4* factory = NULL;
    CreateDXGIFactory2(0, __uuidof(IDXGIFactory4), (void**)&factory);
    DXGI_SWAP_CHAIN_DESC1 sd = {};
    sd.Width = sd.Height = 200;
    sd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    sd.SampleDesc.Count = 1;
    sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    sd.BufferCount = 2;
    sd.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    IDXGISwapChain1* chain1 = NULL;
    IDXGISwapChain3* chain = NULL;
    if(factory)
        factory->CreateSwapChainForHwnd(queue, window, &sd, NULL, NULL, &chain1);
    if(chain1)
        chain1->QueryInterface(__uuidof(IDXGISwapChain3), (void**)&chain);
    Check(queue && chain, "a command queue and a flip swap chain");
    if(!queue || !chain)
        return failures;
    UINT frame = chain->GetCurrentBackBufferIndex();
    D3D12_DESCRIPTOR_HEAP_DESC rd = { D3D12_DESCRIPTOR_HEAP_TYPE_RTV, 2 };
    ID3D12DescriptorHeap* rtvHeap = NULL;
    device->CreateDescriptorHeap(&rd, __uuidof(ID3D12DescriptorHeap), (void**)&rtvHeap);
    UINT rtvSize = device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
    ID3D12Resource* buffers[2] = {};
    for(UINT i = 0; i < 2; i++)
    {
        chain->GetBuffer(i, __uuidof(ID3D12Resource), (void**)&buffers[i]);
        D3D12_CPU_DESCRIPTOR_HANDLE handle = rtvHeap->GetCPUDescriptorHandleForHeapStart();
        handle.ptr += i * rtvSize;
        device->CreateRenderTargetView(buffers[i], NULL, handle);
    }
    D3D12_DESCRIPTOR_HEAP_DESC hd = { D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 1, D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE };
    ID3D12DescriptorHeap* srvHeap = NULL;
    device->CreateDescriptorHeap(&hd, __uuidof(ID3D12DescriptorHeap), (void**)&srvHeap);

    // the root signature: the tint in root constants (b0), the texture in a table (t0), a static sampler (s0)
    D3D12_DESCRIPTOR_RANGE range = { D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, 0, 0, 0 };
    D3D12_ROOT_PARAMETER parameters[2] = {};
    parameters[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    parameters[0].Constants.Num32BitValues = 4;
    parameters[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    parameters[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    parameters[1].DescriptorTable.NumDescriptorRanges = 1;
    parameters[1].DescriptorTable.pDescriptorRanges = &range;
    parameters[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
    D3D12_STATIC_SAMPLER_DESC sampler = {};
    sampler.Filter = D3D12_FILTER_MIN_MAG_MIP_POINT;
    sampler.AddressU = sampler.AddressV = sampler.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    sampler.MaxLOD = D3D12_FLOAT32_MAX;
    sampler.ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
    D3D12_ROOT_SIGNATURE_DESC rootDesc = { 2, parameters, 1, &sampler, D3D12_ROOT_SIGNATURE_FLAG_ALLOW_INPUT_ASSEMBLER_INPUT_LAYOUT };
    ID3DBlob* blob = NULL;
    ID3D12RootSignature* root = NULL;
    if(SUCCEEDED(D3D12SerializeRootSignature(&rootDesc, D3D_ROOT_SIGNATURE_VERSION_1, &blob, NULL)))
        device->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), __uuidof(ID3D12RootSignature), (void**)&root);
    D3D12_INPUT_ELEMENT_DESC elements[3] = {
        { "POSITION", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0, D3D12_INPUT_CLASSIFICATION_PER_VERTEX_DATA, 0 },
        { "COLOR", 0, DXGI_FORMAT_R32G32B32A32_FLOAT, 0, D3D12_APPEND_ALIGNED_ELEMENT, D3D12_INPUT_CLASSIFICATION_PER_VERTEX_DATA, 0 },
        { "TEXCOORD", 0, DXGI_FORMAT_R32G32_FLOAT, 0, D3D12_APPEND_ALIGNED_ELEMENT, D3D12_INPUT_CLASSIFICATION_PER_VERTEX_DATA, 0 },
    };
    D3D12_GRAPHICS_PIPELINE_STATE_DESC pd = {};
    pd.pRootSignature = root;
    pd.VS = { vs, vsSize };
    pd.PS = { ps, psSize };
    pd.BlendState.RenderTarget[0].RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
    pd.SampleMask = UINT_MAX;
    pd.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID;
    pd.RasterizerState.CullMode = D3D12_CULL_MODE_BACK;
    pd.RasterizerState.DepthClipEnable = TRUE;
    pd.InputLayout = { elements, 3 };
    pd.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE;
    pd.NumRenderTargets = 1;
    pd.RTVFormats[0] = DXGI_FORMAT_R8G8B8A8_UNORM;
    pd.SampleDesc.Count = 1;
    ID3D12PipelineState* pso = NULL;
    if(root)
        device->CreateGraphicsPipelineState(&pd, __uuidof(ID3D12PipelineState), (void**)&pso);
    Check(root && pso, "a root signature and a pipeline state");
    if(!root || !pso)
        return failures;

    // the vertices in an upload heap, the texture through an upload buffer
    ID3D12Resource* vb = Buffer12(device, D3D12_HEAP_TYPE_UPLOAD, sizeof(SceneVertices), D3D12_RESOURCE_STATE_GENERIC_READ);
    void* mapped = NULL;
    vb->Map(0, NULL, &mapped);
    memcpy(mapped, SceneVertices, sizeof(SceneVertices));
    vb->Unmap(0, NULL);
    D3D12_VERTEX_BUFFER_VIEW vbView = { vb->GetGPUVirtualAddress(), sizeof(SceneVertices), sizeof(SceneVertex) };
    D3D12_HEAP_PROPERTIES defaultHeap = { D3D12_HEAP_TYPE_DEFAULT };
    D3D12_RESOURCE_DESC td = { D3D12_RESOURCE_DIMENSION_TEXTURE2D, 0, 2, 2, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, { 1, 0 },
                               D3D12_TEXTURE_LAYOUT_UNKNOWN, D3D12_RESOURCE_FLAG_NONE };
    ID3D12Resource* picture = NULL;
    device->CreateCommittedResource(&defaultHeap, D3D12_HEAP_FLAG_NONE, &td, D3D12_RESOURCE_STATE_COPY_DEST, NULL,
                                    __uuidof(ID3D12Resource), (void**)&picture);
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint;
    UINT rows = 0;
    UINT64 rowSize = 0, total = 0;
    device->GetCopyableFootprints(&td, 0, 1, 0, &footprint, &rows, &rowSize, &total);
    ID3D12Resource* upload = Buffer12(device, D3D12_HEAP_TYPE_UPLOAD, total, D3D12_RESOURCE_STATE_GENERIC_READ);
    upload->Map(0, NULL, &mapped);
    for(UINT y = 0; y < rows; y++)
        memcpy((BYTE*)mapped + footprint.Offset + y * footprint.Footprint.RowPitch, SceneTexels + 2 * y, 8);
    upload->Unmap(0, NULL);
    device->CreateShaderResourceView(picture, NULL, srvHeap->GetCPUDescriptorHandleForHeapStart());
    D3D12_RESOURCE_DESC backDesc = buffers[frame]->GetDesc();
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT backFootprint;
    UINT64 backTotal = 0;
    device->GetCopyableFootprints(&backDesc, 0, 1, 0, &backFootprint, &rows, &rowSize, &backTotal);
    ID3D12Resource* readback = Buffer12(device, D3D12_HEAP_TYPE_READBACK, backTotal, D3D12_RESOURCE_STATE_COPY_DEST);

    ID3D12CommandAllocator* allocator = NULL;
    ID3D12GraphicsCommandList* list = NULL;
    device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator), (void**)&allocator);
    device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator, NULL, __uuidof(ID3D12GraphicsCommandList), (void**)&list);
    D3D12_TEXTURE_COPY_LOCATION destination = { picture, D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX };
    destination.SubresourceIndex = 0;
    D3D12_TEXTURE_COPY_LOCATION source = { upload, D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT };
    source.PlacedFootprint = footprint;
    list->CopyTextureRegion(&destination, 0, 0, 0, &source, NULL);
    Barrier(list, picture, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE);
    Barrier(list, buffers[frame], D3D12_RESOURCE_STATE_PRESENT, D3D12_RESOURCE_STATE_RENDER_TARGET);
    D3D12_CPU_DESCRIPTOR_HANDLE rtv = rtvHeap->GetCPUDescriptorHandleForHeapStart();
    rtv.ptr += frame * rtvSize;
    const float green[4] = { 0, 0.6f, 0, 1 };
    list->ClearRenderTargetView(rtv, green, 0, NULL);
    list->OMSetRenderTargets(1, &rtv, FALSE, NULL);
    list->SetGraphicsRootSignature(root);
    list->SetDescriptorHeaps(1, &srvHeap);
    const float tint[4] = { 1, 1, 1, 1 };
    list->SetGraphicsRoot32BitConstants(0, 4, tint, 0);
    list->SetGraphicsRootDescriptorTable(1, srvHeap->GetGPUDescriptorHandleForHeapStart());
    D3D12_VIEWPORT viewport = { 0, 0, 200, 200, 0, 1 };
    D3D12_RECT scissor = { 0, 0, 200, 200 };
    list->RSSetViewports(1, &viewport);
    list->RSSetScissorRects(1, &scissor);
    list->SetPipelineState(pso);
    list->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    list->IASetVertexBuffers(0, 1, &vbView);
    list->DrawInstanced(9, 1, 0, 0);
    Barrier(list, buffers[frame], D3D12_RESOURCE_STATE_RENDER_TARGET, D3D12_RESOURCE_STATE_COPY_SOURCE);
    D3D12_TEXTURE_COPY_LOCATION copyFrom = { buffers[frame], D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX };
    copyFrom.SubresourceIndex = 0;
    D3D12_TEXTURE_COPY_LOCATION copyTo = { readback, D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT };
    copyTo.PlacedFootprint = backFootprint;
    list->CopyTextureRegion(&copyTo, 0, 0, 0, &copyFrom, NULL);
    Barrier(list, buffers[frame], D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_PRESENT);
    list->Close();
    ID3D12CommandList* lists[] = { list };
    queue->ExecuteCommandLists(1, lists);
    ID3D12Fence* fence = NULL;
    device->CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence), (void**)&fence);
    HANDLE event = CreateEventW(NULL, FALSE, FALSE, NULL);
    queue->Signal(fence, 1);
    if(fence->GetCompletedValue() < 1)
    {
        fence->SetEventOnCompletion(1, event);
        WaitForSingleObject(event, 5000);
    }
    Check(fence->GetCompletedValue() == 1, "the fence");
    if(SUCCEEDED(readback->Map(0, NULL, &mapped)))
    {
        CheckScene(mode, (const BYTE*)mapped + backFootprint.Offset, backFootprint.Footprint.RowPitch);
        readback->Unmap(0, NULL);
    }
    else
        Check(false, "Map of the read back buffer");
    chain->Present(0, 0);
    Check(chain->GetCurrentBackBufferIndex() == (frame + 1) % 2, "the next back buffer after Present");
    printf("%s: %d check(s) failed\n", mode, failures);
    CloseHandle(event);
    IUnknown* objects[] = { fence, list, allocator, readback, upload, picture, vb, pso, root, blob, srvHeap, buffers[0], buffers[1],
                            rtvHeap, chain, chain1, factory, queue, device };
    for(auto object : objects)
        if(object)
            object->Release();
    DestroyWindow(window);
    return failures;
}

static int D3D12()
{
    return D3D12Scene("d3d12", Scene12VertexShader, sizeof(Scene12VertexShader), Scene12PixelShader,
                      sizeof(Scene12PixelShader));
}
