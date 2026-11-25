// The gl4 mode of emulator_world (included by target.cpp, after graphics_more2.inc): OpenGL 4.3 with a
// compute shader (buffer block, shared memory, atomics, atomic counter, imageStore), a geometry shader, a
// tessellation with a level per edge (its points counted by a geometry shader in a buffer), and the
// derivatives and mip levels of GLSL. Checked natively and in the emulator.

GLFN(void, glDispatchCompute, GLuint, GLuint, GLuint)
GLFN(void, glBindBufferBase, GLenum, GLuint, GLuint)
GLFN(void, glGetBufferSubData, GLenum, ptrdiff_t, GLsizeiptr, void*)
GLFN(void, glMemoryBarrier, GLbitfield)
GLFN(void, glBindImageTexture, GLuint, GLuint, GLint, GLboolean, GLint, GLenum, GLenum)
GLFN(void, glPatchParameteri, GLenum, GLint)

static GLuint LinkGl(std::initializer_list<std::pair<GLenum, const char*>> stages)
{
    GLuint program = p_glCreateProgram();
    for(auto& stage : stages)
        p_glAttachShader(program, CompileGl(stage.first, stage.second));
    p_glLinkProgram(program);
    GLint linked = 0;
    p_glGetProgramiv(program, 0x8B82 /* GL_LINK_STATUS */, &linked);
    Check(linked != 0, "a GLSL program linked");
    return program;
}

static GLuint StorageBuffer(GLuint binding, GLsizeiptr size)
{
    std::vector<BYTE> zeros(size);
    GLuint buffer = 0;
    p_glGenBuffers(1, &buffer);
    p_glBindBuffer(0x90D2 /* GL_SHADER_STORAGE_BUFFER */, buffer);
    p_glBufferData(0x90D2, size, zeros.data(), 0x88E8 /* GL_DYNAMIC_READ */);
    p_glBindBufferBase(0x90D2, binding, buffer);
    return buffer;
}

static void ReadStorage(GLenum target, GLuint buffer, void* out, GLsizeiptr size)
{
    p_glBindBuffer(target, buffer);
    p_glGetBufferSubData(target, 0, size, out);
}

static const char* gl4Vertex =
    "#version 430 core\n"
    "layout(location = 0) in vec2 position;\n"
    "layout(location = 1) in vec3 color;\n"
    "out vec3 vColor;\n"
    "void main() { vColor = color; gl_Position = vec4(position, 0.5, 1.0); }\n";
static const char* gl4Color =
    "#version 430 core\n"
    "in vec3 fColor;\n"
    "out vec4 result;\n"
    "void main() { result = vec4(fColor, 1.0); }\n";

static int Gl4()
{
    HWND window = TestWindow(L"OpenGL 4.3");
    HDC dc = GetDC(window);
    PIXELFORMATDESCRIPTOR pfd = { sizeof(pfd), 1, PFD_DRAW_TO_WINDOW | PFD_SUPPORT_OPENGL | PFD_DOUBLEBUFFER, PFD_TYPE_RGBA, 32 };
    SetPixelFormat(dc, ChoosePixelFormat(dc, &pfd), &pfd);
    HGLRC legacy = wglCreateContext(dc);
    wglMakeCurrent(dc, legacy);
    PFN_wglCreateContextAttribsARB createContext = NULL;
    Check(Load(createContext, "wglCreateContextAttribsARB"), "wglCreateContextAttribsARB");
    if(!createContext)
        return failures;
    const int attributes[] = { 0x2091, 4, 0x2092, 3, 0x9126, 1 /* core */, 0 };
    HGLRC context = createContext(dc, NULL, attributes);
    Check(context != NULL, "a 4.3 core context");
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
                  Load(p_glGetUniformLocation, "glGetUniformLocation") && Load(p_glUniform1i, "glUniform1i") &&
                  Load(p_glActiveTexture, "glActiveTexture") && Load(p_glGenFramebuffers, "glGenFramebuffers") &&
                  Load(p_glBindFramebuffer, "glBindFramebuffer") && Load(p_glFramebufferTexture2D, "glFramebufferTexture2D") &&
                  Load(p_glCheckFramebufferStatus, "glCheckFramebufferStatus") && Load(p_glDispatchCompute, "glDispatchCompute") &&
                  Load(p_glBindBufferBase, "glBindBufferBase") && Load(p_glGetBufferSubData, "glGetBufferSubData") &&
                  Load(p_glMemoryBarrier, "glMemoryBarrier") && Load(p_glBindImageTexture, "glBindImageTexture") &&
                  Load(p_glPatchParameteri, "glPatchParameteri");
    Check(loaded, "the OpenGL 4.3 functions");
    if(!loaded)
        return failures;
    printf("gl4: %s\n", (const char*)glGetString(GL_VERSION));

    // ------------------------------------------------ compute
    const char* computeSource =
        "#version 430 core\n"
        "layout(local_size_x = 8, local_size_y = 8) in;\n"
        "layout(std430, binding = 0) buffer Results { uint total; uint rows[4]; float values[]; };\n"
        "layout(binding = 0, rgba32f) uniform writeonly image2D picture;\n"
        "layout(binding = 0, offset = 0) uniform atomic_uint counter;\n"
        "shared uint rowSum[8];\n"
        "void main()\n"
        "{\n"
        "    uvec2 id = gl_GlobalInvocationID.xy;\n"
        "    imageStore(picture, ivec2(id), vec4(float(id.x) / 15.0, float(id.y) / 15.0, 0.5, 1.0));\n"
        "    if (gl_LocalInvocationID.x == 0u)\n"
        "        rowSum[gl_LocalInvocationID.y] = 0u;\n"
        "    barrier();\n"
        "    atomicAdd(rowSum[gl_LocalInvocationID.y], id.x);\n"
        "    barrier();\n"
        "    if (gl_LocalInvocationID.x == 0u)\n"
        "        atomicAdd(rows[id.y % 4u], rowSum[gl_LocalInvocationID.y]);\n"
        "    values[id.y * 16u + id.x] = float(id.x + id.y);\n"
        "    atomicAdd(total, 1u);\n"
        "    atomicCounterIncrement(counter);\n"
        "}\n";
    GLuint compute = LinkGl({ { 0x91B9 /* GL_COMPUTE_SHADER */, computeSource } });
    GLuint results = StorageBuffer(0, 4 * 5 + 4 * 256);
    GLuint counter = 0;
    const GLuint zero = 0;
    p_glGenBuffers(1, &counter);
    p_glBindBuffer(0x92C0 /* GL_ATOMIC_COUNTER_BUFFER */, counter);
    p_glBufferData(0x92C0, 4, &zero, 0x88E8);
    p_glBindBufferBase(0x92C0, 0, counter);
    GLuint picture = 0;
    glGenTextures(1, &picture);
    glBindTexture(GL_TEXTURE_2D, picture);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexImage2D(GL_TEXTURE_2D, 0, 0x8814 /* GL_RGBA32F */, 16, 16, 0, GL_RGBA, GL_FLOAT, NULL);
    p_glBindImageTexture(0, picture, 0, GL_FALSE, 0, 0x88B9 /* GL_WRITE_ONLY */, 0x8814);
    p_glUseProgram(compute);
    p_glDispatchCompute(2, 2, 1);
    p_glMemoryBarrier(0xFFFFFFFF);
    GLuint header[5] = {}, atomic = 0;
    std::vector<float> values(256);
    ReadStorage(0x90D2, results, header, sizeof(header));
    p_glBindBuffer(0x90D2, results);
    p_glGetBufferSubData(0x90D2, 20, 4 * 256, values.data());
    ReadStorage(0x92C0, counter, &atomic, 4);
    printf("gl4: compute total %u, counter %u, rows %u %u %u %u, values[37] %g\n", header[0], atomic, header[1], header[2], header[3],
           header[4], values[37]);
    Check(header[0] == 256 && atomic == 256, "the atomics of the compute shader (buffer and atomic counter)");
    Check(header[1] == 480 && header[2] == 480 && header[3] == 480 && header[4] == 480, "shared memory and barriers");
    Check(values[37] == 7 && values[255] == 30, "the runtime-sized array of the buffer block");
    std::vector<float> texels(16 * 16 * 4);
    glBindTexture(GL_TEXTURE_2D, picture);
    glGetTexImage(GL_TEXTURE_2D, 0, GL_RGBA, GL_FLOAT, texels.data());
    const float* texel = &texels[(10 * 16 + 5) * 4];
    Check(fabsf(texel[0] - 5 / 15.0f) < 1e-3f && fabsf(texel[1] - 10 / 15.0f) < 1e-3f && fabsf(texel[2] - 0.5f) < 1e-3f,
          "imageStore of the compute shader");

    // ------------------------------------------------ a framebuffer object for the draws
    GLuint target = 0, fbo = 0;
    glGenTextures(1, &target);
    glBindTexture(GL_TEXTURE_2D, target);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, 200, 200, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    p_glGenFramebuffers(1, &fbo);
    p_glBindFramebuffer(0x8D40, fbo);
    p_glFramebufferTexture2D(0x8D40, 0x8CE0, GL_TEXTURE_2D, target, 0);
    Check(p_glCheckFramebufferStatus(0x8D40) == 0x8CD5, "a complete framebuffer object");
    glViewport(0, 0, 200, 200);
    GLuint vao = 0, vbo = 0;
    p_glGenVertexArrays(1, &vao);
    p_glBindVertexArray(vao);
    p_glGenBuffers(1, &vbo);
    p_glBindBuffer(0x8892, vbo);
    p_glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 5 * sizeof(float), (void*)0);
    p_glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 5 * sizeof(float), (void*)(2 * sizeof(float)));
    p_glEnableVertexAttribArray(0);
    p_glEnableVertexAttribArray(1);

    // ------------------------------------------------ geometry: two points become two squares
    const char* squareSource =
        "#version 430 core\n"
        "layout(points) in;\n"
        "layout(triangle_strip, max_vertices = 4) out;\n"
        "in vec3 vColor[];\n"
        "out vec3 fColor;\n"
        "void main()\n"
        "{\n"
        "    for (int i = 0; i < 4; i++)\n"
        "    {\n"
        "        fColor = vColor[0];\n"
        "        gl_Position = gl_in[0].gl_Position + vec4((i & 1) != 0 ? 0.1 : -0.1, (i & 2) != 0 ? 0.1 : -0.1, 0.0, 0.0);\n"
        "        EmitVertex();\n"
        "    }\n"
        "    EndPrimitive();\n"
        "}\n";
    GLuint squares = LinkGl({ { 0x8B31, gl4Vertex }, { 0x8DD9 /* GL_GEOMETRY_SHADER */, squareSource }, { 0x8B30, gl4Color } });
    const float points[] = { -0.5f, 0, 1, 0, 0, 0.5f, 0, 0, 1, 0 };
    p_glBufferData(0x8892, sizeof(points), points, 0x88E4);
    glClearColor(0, 0, 0, 1);
    glClear(GL_COLOR_BUFFER_BIT);
    p_glUseProgram(squares);
    glDrawArrays(GL_POINTS, 0, 2);
    BYTE pixel[4][4] = {};
    glReadPixels(50, 100, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, pixel[0]);
    glReadPixels(150, 100, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, pixel[1]);
    glReadPixels(100, 100, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, pixel[2]);
    CheckRgb("gl4", "the square the geometry shader made of the first point (red)", pixel[0], 255, 0, 0);
    CheckRgb("gl4", "the square of the second point (green)", pixel[1], 0, 255, 0);
    CheckRgb("gl4", "between the squares (black)", pixel[2], 0, 0, 0);

    // ------------------------------------------------ tessellation: a level per edge
    const char* patchVertex =
        "#version 430 core\n"
        "layout(location = 0) in vec2 position;\n"
        "void main() { gl_Position = vec4(position, 0.5, 1.0); }\n";
    const char* control =
        "#version 430 core\n"
        "layout(vertices = 3) out;\n"
        "void main()\n"
        "{\n"
        "    gl_out[gl_InvocationID].gl_Position = gl_in[gl_InvocationID].gl_Position;\n"
        "    if (gl_InvocationID == 0)\n"
        "    {\n"
        "        gl_TessLevelOuter[0] = 2.0; gl_TessLevelOuter[1] = 3.0; gl_TessLevelOuter[2] = 4.0;\n"
        "        gl_TessLevelInner[0] = 3.0;\n"
        "    }\n"
        "}\n";
    const char* evaluationPoints =
        "#version 430 core\n"
        "layout(triangles, equal_spacing, ccw, point_mode) in;\n"
        "out vec3 coord;\n"
        "void main()\n"
        "{\n"
        "    coord = gl_TessCoord;\n"
        "    gl_Position = gl_TessCoord.x * gl_in[0].gl_Position + gl_TessCoord.y * gl_in[1].gl_Position + gl_TessCoord.z * gl_in[2].gl_Position;\n"
        "}\n";
    const char* countPoints =
        "#version 430 core\n"
        "layout(points) in;\n"
        "layout(points, max_vertices = 1) out;\n"
        "in vec3 coord[];\n"
        "layout(std430, binding = 1) buffer Points { uint count; uint edges[3]; };\n"
        "void main()\n"
        "{\n"
        "    atomicAdd(count, 1u);\n"
        "    if (coord[0].x < 1e-5) atomicAdd(edges[0], 1u);\n"
        "    if (coord[0].y < 1e-5) atomicAdd(edges[1], 1u);\n"
        "    if (coord[0].z < 1e-5) atomicAdd(edges[2], 1u);\n"
        "}\n";
    const char* evaluationColor =
        "#version 430 core\n"
        "layout(triangles, equal_spacing, ccw) in;\n"
        "out vec3 fColor;\n"
        "void main()\n"
        "{\n"
        "    fColor = gl_TessCoord;\n"
        "    gl_Position = gl_TessCoord.x * gl_in[0].gl_Position + gl_TessCoord.y * gl_in[1].gl_Position + gl_TessCoord.z * gl_in[2].gl_Position;\n"
        "}\n";
    GLuint counted = LinkGl({ { 0x8B31, patchVertex }, { 0x8E88 /* GL_TESS_CONTROL_SHADER */, control },
                              { 0x8E87 /* GL_TESS_EVALUATION_SHADER */, evaluationPoints }, { 0x8DD9, countPoints }, { 0x8B30, gl4Color } });
    GLuint colored = LinkGl({ { 0x8B31, patchVertex }, { 0x8E88, control }, { 0x8E87, evaluationColor }, { 0x8B30, gl4Color } });
    GLuint pointsBuffer = StorageBuffer(1, 16);
    const float corners[] = { -0.9f, -0.9f, 0, 0, 0, 0.9f, -0.9f, 0, 0, 0, 0, 0.9f, 0, 0, 0 };
    p_glBindBuffer(0x8892, vbo);
    p_glBufferData(0x8892, sizeof(corners), corners, 0x88E4);
    p_glPatchParameteri(0x8E72 /* GL_PATCH_VERTICES */, 3);
    p_glUseProgram(counted);
    glDrawArrays(0x000E /* GL_PATCHES */, 0, 3);
    p_glMemoryBarrier(0xFFFFFFFF);
    GLuint found[4] = {};
    ReadStorage(0x90D2, pointsBuffer, found, sizeof(found));
    printf("gl4: tessellation, %u points, on the edges %u %u %u\n", found[0], found[1], found[2], found[3]);
    Check(found[1] == 3 && found[2] == 4 && found[3] == 5, "the points of each edge (levels 2 3 4)");
    Check(found[0] == 12, "the points of the patch (inside level 3)");
    glClear(GL_COLOR_BUFFER_BIT);
    p_glUseProgram(colored);
    glDrawArrays(0x000E, 0, 3);
    BYTE middle[4] = {};
    glReadPixels(100, 70, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, middle);
    printf("gl4: the tessellated patch at its middle %u %u %u\n", middle[0], middle[1], middle[2]);
    // at (100, 70): the barycentric coordinates of the pixel's center in the patch
    double y = (70.5 / 100.0 - 1.0 + 0.9) / 1.8, x = (100.5 / 100.0 - 1.0 + 0.9) / 1.8;
    int c = (int)(y * 255 + 0.5), b = (int)((x - y / 2) * 255 + 0.5), a = 255 - b - c;
    CheckRgb("gl4", "the colors of the tessellated patch (its domain coordinates)", middle, a, b, c, 4);

    // ------------------------------------------------ derivatives and mip levels
    const char* screenVertex =
        "#version 430 core\n"
        "out vec2 uv;\n"
        "void main() { vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2); uv = p; gl_Position = vec4(p * 2.0 - 1.0, 0.5, 1.0); }\n";
    const char* derivedFragment =
        "#version 430 core\n"
        "in vec2 uv;\n"
        "uniform sampler2D levels;\n"
        "uniform int part;\n"
        "out vec4 result;\n"
        "void main()\n"
        "{\n"
        "    if (part == 0) result = vec4(dFdx(gl_FragCoord.x * 0.25), dFdy(gl_FragCoord.y * 0.5), fwidth(uv.x) * 64.0, 1.0);\n"
        "    else if (part == 1) result = texture(levels, uv);\n"
        "    else result = texture(levels, uv, -1.0);\n"
        "}\n";
    GLuint derived = LinkGl({ { 0x8B31, screenVertex }, { 0x8B30, derivedFragment } });
    GLuint levels = 0;
    glGenTextures(1, &levels);
    p_glActiveTexture(0x84C0);
    glBindTexture(GL_TEXTURE_2D, levels);
    const UINT colors[4] = { 0xFF0000FF, 0xFF00FF00, 0xFFFF0000, 0xFFFFFFFF };
    for(int level = 0; level < 4; level++)
    {
        std::vector<UINT> image((64 >> level) * (64 >> level), colors[level]);
        glTexImage2D(GL_TEXTURE_2D, level, GL_RGBA, 64 >> level, 64 >> level, 0, GL_RGBA, GL_UNSIGNED_BYTE, image.data());
    }
    glTexParameteri(GL_TEXTURE_2D, 0x813D /* GL_TEXTURE_MAX_LEVEL */, 3);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST_MIPMAP_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    GLuint tiny = 0, tinyFbo = 0;
    glGenTextures(1, &tiny);
    glBindTexture(GL_TEXTURE_2D, tiny);
    glTexImage2D(GL_TEXTURE_2D, 0, 0x8814, 16, 16, 0, GL_RGBA, GL_FLOAT, NULL);
    p_glGenFramebuffers(1, &tinyFbo);
    p_glBindFramebuffer(0x8D40, tinyFbo);
    p_glFramebufferTexture2D(0x8D40, 0x8CE0, GL_TEXTURE_2D, tiny, 0);
    glViewport(0, 0, 16, 16);
    glBindTexture(GL_TEXTURE_2D, levels);
    p_glUseProgram(derived);
    p_glUniform1i(p_glGetUniformLocation(derived, "levels"), 0);
    float read[3][4] = {};
    for(int part = 0; part < 3; part++)
    {
        p_glUniform1i(p_glGetUniformLocation(derived, "part"), part);
        glDrawArrays(GL_TRIANGLES, 0, 3);
        glReadPixels(5, 6, 1, 1, GL_RGBA, GL_FLOAT, read[part]);
    }
    printf("gl4: dFdx %g, dFdy %g, fwidth(u) x 64 %g\n", read[0][0], read[0][1], read[0][2]);
    Check(fabsf(read[0][0] - 0.25f) < 1e-4f && fabsf(read[0][1] - 0.5f) < 1e-4f && fabsf(read[0][2] - 4) < 1e-3f, "dFdx, dFdy and fwidth");
    Check(read[1][0] < 0.01f && read[1][1] < 0.01f && read[1][2] > 0.99f, "texture(): the level of the derivatives, 2 (blue)");
    Check(read[2][0] < 0.01f && read[2][1] > 0.99f && read[2][2] < 0.01f, "texture() with a bias of -1: level 1 (green)");

    p_glBindFramebuffer(0x8D40, 0);
    printf("gl4: %d check(s) failed\n", failures);
    SwapBuffers(dc);
    wglMakeCurrent(NULL, NULL);
    wglDeleteContext(context);
    ReleaseDC(window, dc);
    DestroyWindow(window);
    return failures;
}

// ---------------------------------------------------------------- fx10: a Direct3D 10 effect (fx_4_0), two passes
#include "more_fx10.h"

static int Fx10()
{
    ID3D10Device* device = NULL;
    Check(SUCCEEDED(D3D10CreateDevice(NULL, D3D10_DRIVER_TYPE_HARDWARE, NULL, 0, D3D10_SDK_VERSION, &device)) && device,
          "D3D10CreateDevice");
    if(!device)
        return failures;
    ID3D10Effect* effect = NULL;
    Check(SUCCEEDED(D3D10CreateEffectFromMemory((void*)MoreEffect, sizeof(MoreEffect), 0, device, NULL, &effect)) && effect,
          "D3D10CreateEffectFromMemory");
    if(!effect)
    {
        device->Release();
        return failures;
    }
    D3D10_EFFECT_DESC ed = {};
    effect->GetDesc(&ed);
    ID3D10EffectTechnique* technique = effect->GetTechniqueByName("Draw");
    D3D10_TECHNIQUE_DESC td = {};
    technique->GetDesc(&td);
    printf("fx10: %u constant buffers, %u variables, %u technique(s), %u passes\n", ed.ConstantBuffers, ed.GlobalVariables,
           ed.Techniques, td.Passes);
    Check(ed.ConstantBuffers == 2 && ed.Techniques == 1 && td.Passes == 2, "the description of the effect and its technique");
    Check(!effect->GetVariableByName("nothing")->IsValid(), "an unknown variable is not valid");
    ID3D10EffectPass* plain = technique->GetPassByName("Plain");
    ID3D10EffectPass* add = technique->GetPassByIndex(1);
    D3D10_PASS_DESC pd = {};
    plain->GetDesc(&pd);
    D3D10_INPUT_ELEMENT_DESC elements[] = { { "POSITION", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0, D3D10_INPUT_PER_VERTEX_DATA, 0 },
                                            { "TEXCOORD", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 8, D3D10_INPUT_PER_VERTEX_DATA, 0 } };
    ID3D10InputLayout* layout = NULL;
    device->CreateInputLayout(elements, 2, pd.pIAInputSignature, pd.IAInputSignatureSize, &layout);
    const float vertices[] = { -1, -1, 0, 1, -1, 3, 0, -1, 3, -1, 2, 1 };
    D3D10_BUFFER_DESC vd = { sizeof(vertices), D3D10_USAGE_DEFAULT, D3D10_BIND_VERTEX_BUFFER };
    D3D10_SUBRESOURCE_DATA vdata = { vertices };
    ID3D10Buffer* buffer = NULL;
    device->CreateBuffer(&vd, &vdata, &buffer);
    // a 2 x 2 picture: red, green / blue, white
    const UINT texels[4] = { 0xFF0000FF, 0xFF00FF00, 0xFFFF0000, 0xFFFFFFFF };
    D3D10_TEXTURE2D_DESC pdesc = { 2, 2, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, { 1, 0 }, D3D10_USAGE_DEFAULT, D3D10_BIND_SHADER_RESOURCE };
    D3D10_SUBRESOURCE_DATA pdata = { texels, 8 };
    ID3D10Texture2D* picture = NULL;
    device->CreateTexture2D(&pdesc, &pdata, &picture);
    ID3D10ShaderResourceView* srv = NULL;
    device->CreateShaderResourceView(picture, NULL, &srv);
    D3D10_TEXTURE2D_DESC rd = { 64, 64, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, { 1, 0 }, D3D10_USAGE_DEFAULT, D3D10_BIND_RENDER_TARGET };
    ID3D10Texture2D* target = NULL;
    device->CreateTexture2D(&rd, NULL, &target);
    ID3D10RenderTargetView* rtv = NULL;
    device->CreateRenderTargetView(target, NULL, &rtv);
    bool ready = layout && buffer && srv && rtv;
    Check(ready, "the objects of the effect's scene");
    if(ready)
    {
        const float black[4] = { 0, 0, 0, 1 };
        device->ClearRenderTargetView(rtv, black);
        device->OMSetRenderTargets(1, &rtv, NULL);
        D3D10_VIEWPORT viewport = { 0, 0, 64, 64, 0, 1 };
        device->RSSetViewports(1, &viewport);
        UINT stride = 16, offset = 0;
        device->IASetInputLayout(layout);
        device->IASetVertexBuffers(0, 1, &buffer, &stride, &offset);
        device->IASetPrimitiveTopology(D3D10_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        ID3D10EffectMatrixVariable* transform = effect->GetVariableByName("transform")->AsMatrix();
        ID3D10EffectVectorVariable* tint = effect->GetVariableByName("tint")->AsVector();
        Check(transform->IsValid() && tint->IsValid(), "the matrix and vector variables");
        effect->GetVariableByName("picture")->AsShaderResource()->SetResource(srv);
        // pass Plain: the picture over the target, tinted
        const float identity[16] = { 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1 };
        const float half[4] = { 1, 0.5f, 1, 1 };
        transform->SetMatrix((float*)identity);
        tint->SetFloatVector((float*)half);
        plain->Apply(0);
        device->Draw(3, 0);
        // pass Add: shifted right by half the target (a translation in the last row), added
        const float shift[16] = { 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 1 };
        const float dim[4] = { 0.5f, 0.5f, 0.5f, 1 };
        transform->SetMatrix((float*)shift);
        tint->SetFloatVector((float*)dim);
        add->Apply(0);
        device->Draw(3, 0);
        D3D10_TEXTURE2D_DESC sd = rd;
        sd.Usage = D3D10_USAGE_STAGING;
        sd.BindFlags = 0;
        sd.CPUAccessFlags = D3D10_CPU_ACCESS_READ;
        ID3D10Texture2D* staging = NULL;
        device->CreateTexture2D(&sd, NULL, &staging);
        D3D10_MAPPED_TEXTURE2D mapped = {};
        if(staging)
        {
            device->CopyResource(staging, target);
            if(SUCCEEDED(staging->Map(0, D3D10_MAP_READ, 0, &mapped)))
            {
                const BYTE* p = (const BYTE*)mapped.pData;
                CheckRgb("fx10", "pass Plain: the red texel", p + 16 * mapped.RowPitch + 16 * 4, 255, 0, 0);
                CheckRgb("fx10", "pass Plain: the blue texel", p + 48 * mapped.RowPitch + 16 * 4, 0, 0, 255);
                CheckRgb("fx10", "pass Add: half the red texel added to the green one (128, 128, 0)", p + 16 * mapped.RowPitch + 48 * 4,
                         128, 128, 0);
                CheckRgb("fx10", "pass Add: the white texel stays (255, 128, 255)", p + 48 * mapped.RowPitch + 48 * 4, 255, 128, 255);
                staging->Unmap(0);
            }
            staging->Release();
        }
    }
    printf("fx10: %d check(s) failed\n", failures);
    IUnknown* objects[] = { rtv, target, srv, picture, buffer, layout, effect, device };
    for(auto object : objects)
        if(object)
            object->Release();
    return failures;
}

// ---------------------------------------------------------------- dxil: shader model 6 (dxc) in Direct3D 12
#include "scene6_vs.h"
#include "scene6_ps.h"
#include "more_sm6_cs.h"

static ID3D12Resource* UavBuffer12(ID3D12Device* device, UINT64 size)
{
    D3D12_HEAP_PROPERTIES properties = { D3D12_HEAP_TYPE_DEFAULT };
    D3D12_RESOURCE_DESC desc = { D3D12_RESOURCE_DIMENSION_BUFFER, 0, size, 1, 1, 1, DXGI_FORMAT_UNKNOWN, { 1, 0 },
                                 D3D12_TEXTURE_LAYOUT_ROW_MAJOR, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS };
    ID3D12Resource* resource = NULL;
    device->CreateCommittedResource(&properties, D3D12_HEAP_FLAG_NONE, &desc, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, NULL,
                                    __uuidof(ID3D12Resource), (void**)&resource);
    return resource;
}

static int Dxil()
{
    D3D12Scene("dxil", Scene6VertexShader, sizeof(Scene6VertexShader), Scene6PixelShader, sizeof(Scene6PixelShader));
    ID3D12Device* device = NULL;
    if(FAILED(D3D12CreateDevice(NULL, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device), (void**)&device)))
        return Check(false, "D3D12CreateDevice"), failures;
    D3D12_FEATURE_DATA_SHADER_MODEL model = { D3D_SHADER_MODEL_6_0 };
    Check(SUCCEEDED(device->CheckFeatureSupport(D3D12_FEATURE_SHADER_MODEL, &model, sizeof(model))) &&
          model.HighestShaderModel >= D3D_SHADER_MODEL_6_0, "shader model 6");
    // the compute shader: three unordered access views as root descriptors
    D3D12_ROOT_PARAMETER parameters[3] = {};
    for(UINT i = 0; i < 3; i++)
    {
        parameters[i].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
        parameters[i].Descriptor.ShaderRegister = i;
        parameters[i].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    }
    D3D12_ROOT_SIGNATURE_DESC rootDesc = { 3, parameters, 0, NULL, D3D12_ROOT_SIGNATURE_FLAG_NONE };
    ID3DBlob* blob = NULL;
    ID3D12RootSignature* root = NULL;
    if(SUCCEEDED(D3D12SerializeRootSignature(&rootDesc, D3D_ROOT_SIGNATURE_VERSION_1, &blob, NULL)))
        device->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), __uuidof(ID3D12RootSignature), (void**)&root);
    D3D12_COMPUTE_PIPELINE_STATE_DESC cd = {};
    cd.pRootSignature = root;
    cd.CS = { MoreSm6ComputeShader, sizeof(MoreSm6ComputeShader) };
    ID3D12PipelineState* pso = NULL;
    if(root)
        device->CreateComputePipelineState(&cd, __uuidof(ID3D12PipelineState), (void**)&pso);
    Check(root && pso, "the compute pipeline state (cs_6_0)");
    D3D12_COMMAND_QUEUE_DESC qd = {};
    ID3D12CommandQueue* queue = NULL;
    device->CreateCommandQueue(&qd, __uuidof(ID3D12CommandQueue), (void**)&queue);
    ID3D12Resource* sums = UavBuffer12(device, 128 * 4);
    ID3D12Resource* waves = UavBuffer12(device, 128 * 4);
    ID3D12Resource* counter = UavBuffer12(device, 256);
    ID3D12Resource* readback = Buffer12(device, D3D12_HEAP_TYPE_READBACK, 128 * 4 * 2 + 256, D3D12_RESOURCE_STATE_COPY_DEST);
    ID3D12CommandAllocator* allocator = NULL;
    ID3D12GraphicsCommandList* list = NULL;
    device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator), (void**)&allocator);
    device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator, pso, __uuidof(ID3D12GraphicsCommandList), (void**)&list);
    bool ready = pso && queue && sums && waves && counter && readback && list;
    if(ready)
    {
        list->SetComputeRootSignature(root);
        list->SetComputeRootUnorderedAccessView(0, sums->GetGPUVirtualAddress());
        list->SetComputeRootUnorderedAccessView(1, waves->GetGPUVirtualAddress());
        list->SetComputeRootUnorderedAccessView(2, counter->GetGPUVirtualAddress());
        list->Dispatch(2, 1, 1);
        ID3D12Resource* sources[3] = { sums, waves, counter };
        for(auto source : sources)
            Barrier(list, source, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
        list->CopyBufferRegion(readback, 0, sums, 0, 128 * 4);
        list->CopyBufferRegion(readback, 128 * 4, waves, 0, 128 * 4);
        list->CopyBufferRegion(readback, 128 * 8, counter, 0, 4);
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
        void* mapped = NULL;
        if(SUCCEEDED(readback->Map(0, NULL, &mapped)))
        {
            const float* s = (const float*)mapped;
            const UINT* w = (const UINT*)((BYTE*)mapped + 128 * 4);
            UINT count = *(const UINT*)((BYTE*)mapped + 128 * 8);
            int wrong = 0, wavesOk = 0;
            for(UINT i = 0; i < 128; i++)
            {
                UINT group = i / 64, local = i % 64;
                float expected = 0;
                for(UINT k = 0; k < 4; k++)
                    expected += (float)(group * 64 + (local + k) % 64);
                wrong += s[i] != expected;
                wavesOk += w[i] == 1;
            }
            printf("dxil: compute %d wrong sums, %d lanes with right wave operations, counter %u\n", wrong, wavesOk, count);
            Check(wrong == 0, "the sums of the compute shader (groupshared memory, a loop)");
            Check(wavesOk == 128, "the wave operations (WaveActiveSum, WavePrefixSum, the lane index and count)");
            Check(count == 128, "InterlockedAdd on a byte address buffer");
            readback->Unmap(0, NULL);
        }
        CloseHandle(event);
        if(fence)
            fence->Release();
    }
    printf("dxil: %d check(s) failed\n", failures);
    IUnknown* objects[] = { list, allocator, readback, counter, waves, sums, queue, pso, root, blob, device };
    for(auto object : objects)
        if(object)
            object->Release();
    return failures;
}

// ---------------------------------------------------------------- dxr: ray tracing in Direct3D 12 (DXR 1.0)
#include "more_dxr.h"

static ID3D12Resource* StructureBuffer12(ID3D12Device* device, UINT64 size, D3D12_RESOURCE_STATES state)
{
    D3D12_HEAP_PROPERTIES properties = { D3D12_HEAP_TYPE_DEFAULT };
    D3D12_RESOURCE_DESC desc = { D3D12_RESOURCE_DIMENSION_BUFFER, 0, size, 1, 1, 1, DXGI_FORMAT_UNKNOWN, { 1, 0 },
                                 D3D12_TEXTURE_LAYOUT_ROW_MAJOR, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS };
    ID3D12Resource* resource = NULL;
    device->CreateCommittedResource(&properties, D3D12_HEAP_FLAG_NONE, &desc, state, NULL, __uuidof(ID3D12Resource),
                                    (void**)&resource);
    return resource;
}

static ID3D12Resource* Upload12(ID3D12Device* device, const void* data, UINT64 size)
{
    ID3D12Resource* resource = Buffer12(device, D3D12_HEAP_TYPE_UPLOAD, size, D3D12_RESOURCE_STATE_GENERIC_READ);
    void* mapped = NULL;
    if(resource && SUCCEEDED(resource->Map(0, NULL, &mapped)))
    {
        memcpy(mapped, data, (size_t)size);
        resource->Unmap(0, NULL);
    }
    return resource;
}

// the instances of the dxr mode: x offset, z, scale, InstanceID, flags, hit group (1: with the any hit shader)
struct DxrInstance
{
    float x, z, scale;
    UINT id, flags, group;
};

static const DxrInstance dxrInstances[3] = {
    { -0.5f, 0.0f, 1.0f, 7, 0, 0 },
    { 0.25f, -1.0f, 0.5f, 9, D3D12_RAYTRACING_INSTANCE_FLAG_TRIANGLE_FRONT_COUNTERCLOCKWISE, 0 },
    { 0.5f, 0.5f, 1.0f, 11, D3D12_RAYTRACING_INSTANCE_FLAG_FORCE_NON_OPAQUE, 1 },
};

// what the shaders of more_dxr.hlsl write for the ray (i, j) of the 16 x 8 grid
static void DxrExpected(UINT i, UINT j, UINT out[8])
{
    float ox = (i + 0.5f) / 16 * 2 - 1, oy = 1 - (j + 0.5f) / 8 * 2;
    memset(out, 0, 8 * sizeof(UINT));
    out[1] = 0xFFFF;
    out[2] = 100;
    float best = 1e30f;
    for(UINT k = 0; k < 3; k++)
    {
        const DxrInstance& d = dxrInstances[k];
        float x = (ox - d.x) / d.scale, y = oy / d.scale;
        if(x < -0.5f || x > 0.5f || y < -0.5f || y > 0.5f)
            continue;
        UINT primitive = y > x ? 0 : 1;
        if(k == 2 && primitive == 1)    // ignored by the any hit shader
            continue;
        float t = 2 + d.z;
        if(t >= best)
            continue;
        best = t;
        // the barycentrics (u: the second vertex, v: the third) of (-.5,-.5) (-.5,.5) (.5,.5) or (-.5,-.5) (.5,.5) (.5,-.5)
        float u, v;
        if(primitive == 0)
        {
            v = x + 0.5f;
            u = y + 0.5f - v;
        }
        else
        {
            u = y + 0.5f;
            v = x + 0.5f - u;
        }
        UINT kind = (d.flags & D3D12_RAYTRACING_INSTANCE_FLAG_TRIANGLE_FRONT_COUNTERCLOCKWISE) ? 0xFF : 0xFE;
        out[0] = d.id + 1;
        out[1] = primitive | (kind << 8) | (k << 16);
        out[2] = (UINT)(t * 16 + 0.5f);
        out[3] = (UINT)(u * 256 + 0.5f) | ((UINT)(v * 256 + 0.5f) << 16);
        out[4] = (UINT)(int)floorf(x * 64 + 0.5f) + 1000;
        out[5] = 1;
        out[6] = (UINT)(int)floorf(d.x * 64 + 0.5f) + 1000;
        out[7] = (UINT)(64 / d.scale + 0.5f);
    }
}

static int Dxr()
{
    ID3D12Device5* device = NULL;
    if(FAILED(D3D12CreateDevice(NULL, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device5), (void**)&device)))
        return Check(false, "D3D12CreateDevice (ID3D12Device5)"), failures;
    D3D12_FEATURE_DATA_D3D12_OPTIONS5 options = {};
    device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS5, &options, sizeof(options));
    Check(options.RaytracingTier >= D3D12_RAYTRACING_TIER_1_0, "ray tracing tier 1.0");
    // the global root signature: the acceleration structure in a descriptor table, the output a root UAV
    D3D12_DESCRIPTOR_RANGE range = { D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, 0, 0, 0 };
    D3D12_ROOT_PARAMETER parameters[2] = {};
    parameters[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    parameters[0].DescriptorTable.NumDescriptorRanges = 1;
    parameters[0].DescriptorTable.pDescriptorRanges = &range;
    parameters[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
    parameters[1].Descriptor.ShaderRegister = 0;
    D3D12_ROOT_SIGNATURE_DESC rootDesc = { 2, parameters, 0, NULL, D3D12_ROOT_SIGNATURE_FLAG_NONE };
    ID3DBlob* blob = NULL;
    ID3D12RootSignature* root = NULL;
    if(SUCCEEDED(D3D12SerializeRootSignature(&rootDesc, D3D_ROOT_SIGNATURE_VERSION_1, &blob, NULL)))
        device->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), __uuidof(ID3D12RootSignature), (void**)&root);
    // the state object: the library, two hit groups, the configurations
    D3D12_DXIL_LIBRARY_DESC library = { { MoreDxrLibrary, sizeof(MoreDxrLibrary) }, 0, NULL };
    D3D12_HIT_GROUP_DESC groups[2] = {
        { L"HitGroup", D3D12_HIT_GROUP_TYPE_TRIANGLES, NULL, L"Hit", NULL },
        { L"HitGroupAny", D3D12_HIT_GROUP_TYPE_TRIANGLES, L"AnyHit", L"Hit", NULL },
    };
    D3D12_RAYTRACING_SHADER_CONFIG shaderConfig = { 32, 8 };
    D3D12_GLOBAL_ROOT_SIGNATURE global = { root };
    D3D12_RAYTRACING_PIPELINE_CONFIG pipelineConfig = { 1 };
    D3D12_STATE_SUBOBJECT subobjects[6] = {
        { D3D12_STATE_SUBOBJECT_TYPE_DXIL_LIBRARY, &library },
        { D3D12_STATE_SUBOBJECT_TYPE_HIT_GROUP, &groups[0] },
        { D3D12_STATE_SUBOBJECT_TYPE_HIT_GROUP, &groups[1] },
        { D3D12_STATE_SUBOBJECT_TYPE_RAYTRACING_SHADER_CONFIG, &shaderConfig },
        { D3D12_STATE_SUBOBJECT_TYPE_GLOBAL_ROOT_SIGNATURE, &global },
        { D3D12_STATE_SUBOBJECT_TYPE_RAYTRACING_PIPELINE_CONFIG, &pipelineConfig },
    };
    D3D12_STATE_OBJECT_DESC stateDesc = { D3D12_STATE_OBJECT_TYPE_RAYTRACING_PIPELINE, 6, subobjects };
    ID3D12StateObject* state = NULL;
    ID3D12StateObjectProperties* properties = NULL;
    if(root)
        device->CreateStateObject(&stateDesc, __uuidof(ID3D12StateObject), (void**)&state);
    if(state)
        state->QueryInterface(__uuidof(ID3D12StateObjectProperties), (void**)&properties);
    Check(state && properties, "the ray tracing state object (lib_6_3)");
    // the shader tables: ray generation at 0, miss at 64, the two hit groups at 128 (records of 32 bytes)
    BYTE tables[256] = {};
    const wchar_t* names[4] = { L"RayGen", L"Miss", L"HitGroup", L"HitGroupAny" };
    const UINT offsets[4] = { 0, 64, 128, 160 };
    bool identified = properties != NULL;
    for(UINT k = 0; k < 4 && properties; k++)
    {
        void* identifier = properties->GetShaderIdentifier(names[k]);
        identified = identified && identifier;
        if(identifier)
            memcpy(tables + offsets[k], identifier, D3D12_SHADER_IDENTIFIER_SIZE_IN_BYTES);
    }
    Check(identified, "GetShaderIdentifier");
    ID3D12Resource* table = Upload12(device, tables, sizeof(tables));
    // the quad (two triangles, clockwise) and its three instances
    const float quad[18] = { -0.5f, -0.5f, 0, -0.5f, 0.5f, 0, 0.5f, 0.5f, 0, -0.5f, -0.5f, 0, 0.5f, 0.5f, 0, 0.5f, -0.5f, 0 };
    ID3D12Resource* vertices = Upload12(device, quad, sizeof(quad));
    D3D12_RAYTRACING_GEOMETRY_DESC geometry = {};
    geometry.Type = D3D12_RAYTRACING_GEOMETRY_TYPE_TRIANGLES;
    geometry.Flags = D3D12_RAYTRACING_GEOMETRY_FLAG_OPAQUE;
    geometry.Triangles.VertexFormat = DXGI_FORMAT_R32G32B32_FLOAT;
    geometry.Triangles.VertexCount = 6;
    geometry.Triangles.VertexBuffer.StartAddress = vertices ? vertices->GetGPUVirtualAddress() : 0;
    geometry.Triangles.VertexBuffer.StrideInBytes = 12;
    D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_INPUTS bottomInputs = {};
    bottomInputs.Type = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL;
    bottomInputs.Flags = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_BUILD_FLAG_PREFER_FAST_TRACE;
    bottomInputs.NumDescs = 1;
    bottomInputs.DescsLayout = D3D12_ELEMENTS_LAYOUT_ARRAY;
    bottomInputs.pGeometryDescs = &geometry;
    D3D12_RAYTRACING_ACCELERATION_STRUCTURE_PREBUILD_INFO bottomInfo = {}, topInfo = {};
    device->GetRaytracingAccelerationStructurePrebuildInfo(&bottomInputs, &bottomInfo);
    ID3D12Resource* bottom = StructureBuffer12(device, bottomInfo.ResultDataMaxSizeInBytes,
                                               D3D12_RESOURCE_STATE_RAYTRACING_ACCELERATION_STRUCTURE);
    ID3D12Resource* bottomScratch = StructureBuffer12(device, bottomInfo.ScratchDataSizeInBytes, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    D3D12_RAYTRACING_INSTANCE_DESC instances[3] = {};
    for(UINT k = 0; k < 3; k++)
    {
        const DxrInstance& d = dxrInstances[k];
        instances[k].Transform[0][0] = instances[k].Transform[1][1] = d.scale;
        instances[k].Transform[2][2] = 1;
        instances[k].Transform[0][3] = d.x;
        instances[k].Transform[2][3] = d.z;
        instances[k].InstanceID = d.id;
        instances[k].InstanceMask = 0xFF;
        instances[k].InstanceContributionToHitGroupIndex = d.group;
        instances[k].Flags = d.flags;
        instances[k].AccelerationStructure = bottom ? bottom->GetGPUVirtualAddress() : 0;
    }
    ID3D12Resource* instanceBuffer = Upload12(device, instances, sizeof(instances));
    D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_INPUTS topInputs = {};
    topInputs.Type = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_TOP_LEVEL;
    topInputs.NumDescs = 3;
    topInputs.DescsLayout = D3D12_ELEMENTS_LAYOUT_ARRAY;
    topInputs.InstanceDescs = instanceBuffer ? instanceBuffer->GetGPUVirtualAddress() : 0;
    device->GetRaytracingAccelerationStructurePrebuildInfo(&topInputs, &topInfo);
    ID3D12Resource* top = StructureBuffer12(device, topInfo.ResultDataMaxSizeInBytes, D3D12_RESOURCE_STATE_RAYTRACING_ACCELERATION_STRUCTURE);
    ID3D12Resource* topScratch = StructureBuffer12(device, topInfo.ScratchDataSizeInBytes, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    Check(bottomInfo.ResultDataMaxSizeInBytes && topInfo.ResultDataMaxSizeInBytes, "GetRaytracingAccelerationStructurePrebuildInfo");
    const UINT width = 16, height = 8, size = width * height * 32;
    ID3D12Resource* output = UavBuffer12(device, size);
    ID3D12Resource* readback = Buffer12(device, D3D12_HEAP_TYPE_READBACK, size, D3D12_RESOURCE_STATE_COPY_DEST);
    // the acceleration structure's view in a shader visible heap
    D3D12_DESCRIPTOR_HEAP_DESC heapDesc = { D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 1, D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE };
    ID3D12DescriptorHeap* heap = NULL;
    device->CreateDescriptorHeap(&heapDesc, __uuidof(ID3D12DescriptorHeap), (void**)&heap);
    if(heap && top)
    {
        D3D12_SHADER_RESOURCE_VIEW_DESC srv = {};
        srv.ViewDimension = D3D12_SRV_DIMENSION_RAYTRACING_ACCELERATION_STRUCTURE;
        srv.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
        srv.RaytracingAccelerationStructure.Location = top->GetGPUVirtualAddress();
        device->CreateShaderResourceView(NULL, &srv, heap->GetCPUDescriptorHandleForHeapStart());
    }
    D3D12_COMMAND_QUEUE_DESC qd = {};
    ID3D12CommandQueue* queue = NULL;
    device->CreateCommandQueue(&qd, __uuidof(ID3D12CommandQueue), (void**)&queue);
    ID3D12CommandAllocator* allocator = NULL;
    ID3D12GraphicsCommandList4* list = NULL;
    device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator), (void**)&allocator);
    if(allocator)
        device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator, NULL, __uuidof(ID3D12GraphicsCommandList4),
                                  (void**)&list);
    bool ready = state && properties && table && vertices && bottom && bottomScratch && instanceBuffer && top && topScratch &&
                 output && readback && heap && queue && list;
    if(ready)
    {
        D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC build = {};
        build.DestAccelerationStructureData = bottom->GetGPUVirtualAddress();
        build.Inputs = bottomInputs;
        build.ScratchAccelerationStructureData = bottomScratch->GetGPUVirtualAddress();
        list->BuildRaytracingAccelerationStructure(&build, 0, NULL);
        D3D12_RESOURCE_BARRIER barrier = {};
        barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
        barrier.UAV.pResource = bottom;
        list->ResourceBarrier(1, &barrier);
        build.DestAccelerationStructureData = top->GetGPUVirtualAddress();
        build.Inputs = topInputs;
        build.ScratchAccelerationStructureData = topScratch->GetGPUVirtualAddress();
        list->BuildRaytracingAccelerationStructure(&build, 0, NULL);
        barrier.UAV.pResource = top;
        list->ResourceBarrier(1, &barrier);
        list->SetDescriptorHeaps(1, &heap);
        list->SetComputeRootSignature(root);
        list->SetComputeRootDescriptorTable(0, heap->GetGPUDescriptorHandleForHeapStart());
        list->SetComputeRootUnorderedAccessView(1, output->GetGPUVirtualAddress());
        list->SetPipelineState1(state);
        D3D12_DISPATCH_RAYS_DESC rays = {};
        D3D12_GPU_VIRTUAL_ADDRESS base = table->GetGPUVirtualAddress();
        rays.RayGenerationShaderRecord = { base, 32 };
        rays.MissShaderTable = { base + 64, 32, 32 };
        rays.HitGroupTable = { base + 128, 64, 32 };
        rays.Width = width;
        rays.Height = height;
        rays.Depth = 1;
        list->DispatchRays(&rays);
        Barrier(list, output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
        list->CopyBufferRegion(readback, 0, output, 0, size);
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
        void* mapped = NULL;
        if(SUCCEEDED(readback->Map(0, NULL, &mapped)))
        {
            const UINT* got = (const UINT*)mapped;
            int wrong = 0, hits = 0, misses = 0, ignored = 0;
            for(UINT j = 0; j < height; j++)
                for(UINT i = 0; i < width; i++)
                {
                    UINT expected[8];
                    DxrExpected(i, j, expected);
                    const UINT* g = got + (j * width + i) * 8;
                    bool same = memcmp(g, expected, sizeof(expected)) == 0;
                    if(!same && wrong < 4)
                        printf("dxr: ray (%u, %u): %x %x %x %x %x %x %x %x, expected %x %x %x %x %x %x %x %x\n", i, j, g[0], g[1],
                               g[2], g[3], g[4], g[5], g[6], g[7], expected[0], expected[1], expected[2], expected[3],
                               expected[4], expected[5], expected[6], expected[7]);
                    wrong += !same;
                    hits += expected[0] != 0;
                    misses += expected[0] == 0;
                    ignored += i >= 8 && expected[0] == 0 && fabsf(1 - (j + 0.5f) / 4) < 0.5f;
                }
            printf("dxr: %d hits, %d misses (%d through the any hit shader), %d wrong ray(s)\n", hits, misses, ignored, wrong);
            Check(wrong == 0, "the rays: nearest hits, InstanceID / InstanceIndex / PrimitiveIndex / HitKind, RayTCurrent, "
                              "barycentrics, object space, any hit (IgnoreHit), miss");
            readback->Unmap(0, NULL);
        }
        CloseHandle(event);
        if(fence)
            fence->Release();
    }
    else
        Check(false, "the ray tracing objects");
    printf("dxr: %d check(s) failed\n", failures);
    IUnknown* objects[] = { list, allocator, queue, heap, readback, output, topScratch, top, instanceBuffer, bottomScratch, bottom,
                            vertices, table, properties, state, root, blob, device };
    for(auto object : objects)
        if(object)
            object->Release();
    return failures;
}
