// The modes bc, deriv, tess, so and deferred of emulator_world (included by target.cpp, after
// graphics_more.inc): block-compressed textures, derivatives and mip levels, tessellation with a factor
// per edge, stream output, deferred contexts. Checked natively and in the emulator.

#include "more_bc_data.h"
#include "more_cs_load.h"
#include "more_vs_screen.h"
#include "more_ps_deriv.h"
#include "more_vs_patch_point.h"
#include "more_hs_integer.h"
#include "more_hs_odd.h"
#include "more_ds_edges.h"
#include "more_vs_seed.h"
#include "more_gs_triangle.h"
#include "more_vs_streamed.h"
#include "more_vs_plain.h"
#include "more_ps_paint.h"

static void Release(std::initializer_list<IUnknown*> objects)
{
    for(auto object : objects)
        if(object)
            object->Release();
}

static bool ReadBuffer11(ID3D11Device* device, ID3D11DeviceContext* context, ID3D11Buffer* buffer, UINT size, std::vector<BYTE>& data)
{
    D3D11_BUFFER_DESC sd = { size, D3D11_USAGE_STAGING, 0, D3D11_CPU_ACCESS_READ };
    ID3D11Buffer* staging = NULL;
    device->CreateBuffer(&sd, NULL, &staging);
    if(!staging)
        return false;
    context->CopyResource(staging, buffer);
    D3D11_MAPPED_SUBRESOURCE mapped;
    bool ok = SUCCEEDED(context->Map(staging, 0, D3D11_MAP_READ, 0, &mapped));
    if(ok)
    {
        data.assign((BYTE*)mapped.pData, (BYTE*)mapped.pData + size);
        context->Unmap(staging, 0);
    }
    staging->Release();
    return ok;
}

// ---------------------------------------------------------------- bc: BC7 and BC6H texels read by a compute shader
static bool LoadBlocks(ID3D11Device* device, ID3D11DeviceContext* context, DXGI_FORMAT format, const unsigned char* blocks,
                       std::vector<float>& texels)
{
    D3D11_TEXTURE2D_DESC td = { 8, 4, 1, 1, format, { 1, 0 }, D3D11_USAGE_DEFAULT, D3D11_BIND_SHADER_RESOURCE };
    D3D11_SUBRESOURCE_DATA init = { blocks, 32 };
    ID3D11Texture2D* texture = NULL;
    device->CreateTexture2D(&td, &init, &texture);
    ID3D11ShaderResourceView* srv = NULL;
    if(texture)
        device->CreateShaderResourceView(texture, NULL, &srv);
    D3D11_BUFFER_DESC bd = { 32 * 16, D3D11_USAGE_DEFAULT, D3D11_BIND_UNORDERED_ACCESS, 0, D3D11_RESOURCE_MISC_BUFFER_STRUCTURED, 16 };
    ID3D11Buffer* buffer = NULL;
    device->CreateBuffer(&bd, NULL, &buffer);
    ID3D11UnorderedAccessView* uav = NULL;
    if(buffer)
        device->CreateUnorderedAccessView(buffer, NULL, &uav);
    ID3D11ComputeShader* cs = NULL;
    device->CreateComputeShader(MoreLoadComputeShader, sizeof(MoreLoadComputeShader), NULL, &cs);
    bool ok = srv && uav && cs;
    std::vector<BYTE> data;
    if(ok)
    {
        context->CSSetShader(cs, NULL, 0);
        context->CSSetShaderResources(0, 1, &srv);
        context->CSSetUnorderedAccessViews(0, 1, &uav, NULL);
        context->Dispatch(1, 1, 1);
        ok = ReadBuffer11(device, context, buffer, 32 * 16, data);
    }
    if(ok)
        texels.assign((const float*)data.data(), (const float*)data.data() + 128);
    Release({ cs, uav, buffer, srv, texture });
    return ok;
}

static int Bc()
{
    ID3D11Device* device = NULL;
    ID3D11DeviceContext* context = NULL;
    Check(Device11(&device, &context), "D3D11CreateDevice");
    if(!device)
        return failures;
    struct { const char* name; DXGI_FORMAT format; const unsigned char* blocks; const float* expected; float tolerance; } sets[] = {
        { "BC7 (modes 6 and 1)", DXGI_FORMAT_BC7_UNORM, Bc7Blocks, Bc7Texels, 0.6f / 255 },
        { "BC7 (modes 4 and 7)", DXGI_FORMAT_BC7_UNORM, Bc7bBlocks, Bc7bTexels, 0.6f / 255 },
        { "BC6H unsigned", DXGI_FORMAT_BC6H_UF16, Bc6hBlocks, Bc6hTexels, 0 },
        { "BC6H signed", DXGI_FORMAT_BC6H_SF16, Bc6hsBlocks, Bc6hsTexels, 0 },
    };
    for(auto& set : sets)
    {
        std::vector<float> texels;
        bool ok = LoadBlocks(device, context, set.format, set.blocks, texels);
        int wrong = 0;
        for(size_t i = 0; ok && i < 128; i++)
        {
            float allowed = set.tolerance ? set.tolerance : 1e-3f * fabsf(set.expected[i]) + 1e-6f;
            if(fabsf(texels[i] - set.expected[i]) > allowed)
            {
                if(wrong++ < 2)
                    printf("bc: %s texel %zu.%zu: %g instead of %g\n", set.name, i / 4, i % 4, texels[i], set.expected[i]);
            }
        }
        Check(ok && wrong == 0, set.name);
    }
    printf("bc: %d check(s) failed\n", failures);
    Release({ context, device });
    return failures;
}

// ---------------------------------------------------------------- deriv: ddx / ddy and the mip level of Sample
static int Deriv()
{
    ID3D11Device* device = NULL;
    ID3D11DeviceContext* context = NULL;
    Check(Device11(&device, &context), "D3D11CreateDevice");
    if(!device)
        return failures;
    ID3D11Texture2D* targets[4] = { Texture11(device, 16, 16, DXGI_FORMAT_R32G32B32A32_FLOAT, D3D11_BIND_RENDER_TARGET),
                                    Texture11(device, 16, 16, DXGI_FORMAT_R8G8B8A8_UNORM, D3D11_BIND_RENDER_TARGET),
                                    Texture11(device, 16, 16, DXGI_FORMAT_R8G8B8A8_UNORM, D3D11_BIND_RENDER_TARGET),
                                    Texture11(device, 16, 16, DXGI_FORMAT_R8G8B8A8_UNORM, D3D11_BIND_RENDER_TARGET) };
    ID3D11RenderTargetView* views[4] = {};
    for(int i = 0; i < 4; i++)
        if(targets[i])
            device->CreateRenderTargetView(targets[i], NULL, &views[i]);
    // a 64 x 64 texture with 4 levels, each of its own color: red, green, blue, white
    const UINT colors[4] = { 0xFF0000FF, 0xFF00FF00, 0xFFFF0000, 0xFFFFFFFF };
    std::vector<UINT> levels[4];
    D3D11_SUBRESOURCE_DATA init[4];
    for(int level = 0; level < 4; level++)
    {
        UINT size = 64 >> level;
        levels[level].assign(size * size, colors[level]);
        init[level] = { levels[level].data(), size * 4 };
    }
    D3D11_TEXTURE2D_DESC td = { 64, 64, 4, 1, DXGI_FORMAT_R8G8B8A8_UNORM, { 1, 0 }, D3D11_USAGE_DEFAULT, D3D11_BIND_SHADER_RESOURCE };
    ID3D11Texture2D* texture = NULL;
    device->CreateTexture2D(&td, init, &texture);
    ID3D11ShaderResourceView* srv = NULL;
    if(texture)
        device->CreateShaderResourceView(texture, NULL, &srv);
    D3D11_SAMPLER_DESC sd = { D3D11_FILTER_MIN_MAG_MIP_POINT, D3D11_TEXTURE_ADDRESS_WRAP, D3D11_TEXTURE_ADDRESS_WRAP,
                              D3D11_TEXTURE_ADDRESS_WRAP, 0, 1, D3D11_COMPARISON_NEVER, {}, 0, D3D11_FLOAT32_MAX };
    ID3D11SamplerState* sampler = NULL;
    device->CreateSamplerState(&sd, &sampler);
    ID3D11VertexShader* vs = NULL;
    ID3D11PixelShader* ps = NULL;
    device->CreateVertexShader(MoreScreenVertexShader, sizeof(MoreScreenVertexShader), NULL, &vs);
    device->CreatePixelShader(MoreDerivPixelShader, sizeof(MoreDerivPixelShader), NULL, &ps);
    Check(views[0] && views[3] && srv && sampler && vs && ps, "the objects of the derivatives' scene");
    if(views[0] && views[3] && srv && sampler && vs && ps)
    {
        context->OMSetRenderTargets(4, views, NULL);
        D3D11_VIEWPORT viewport = { 0, 0, 16, 16, 0, 1 };
        context->RSSetViewports(1, &viewport);
        context->IASetInputLayout(NULL);
        context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        context->VSSetShader(vs, NULL, 0);
        context->PSSetShader(ps, NULL, 0);
        context->PSSetShaderResources(0, 1, &srv);
        context->PSSetSamplers(0, 1, &sampler);
        context->Draw(3, 0);
        std::vector<BYTE> data;
        UINT pitch = 0;
        if(Read11(device, context, targets[0], data, pitch))
        {
            const float* v = (const float*)&data[7 * pitch + 5 * 16];
            bool ok = fabsf(v[0] - 0.25f) < 1e-4f && fabsf(v[1] - 0.5f) < 1e-4f && fabsf(v[2] - 4) < 1e-3f && fabsf(v[3] - 2) < 0.01f;
            printf("deriv: ddx %g, ddy %g, ddx(u) x 64 %g, level %g\n", v[0], v[1], v[2], v[3]);
            Check(ok, "ddx_fine, ddy_coarse, ddx and CalculateLevelOfDetail");
        }
        struct { int target; int r, g, b; const char* what; } expected[] = {
            { 1, 0, 0, 255, "Sample: level 2 (blue)" }, { 2, 0, 255, 0, "SampleBias -1: level 1 (green)" },
            { 3, 255, 255, 255, "SampleGrad: level 3, the last (white)" } };
        for(auto& e : expected)
            if(Read11(device, context, targets[e.target], data, pitch))
            {
                CheckRgb("deriv", e.what, &data[3 * pitch + 3 * 4], e.r, e.g, e.b);
                CheckRgb("deriv", e.what, &data[12 * pitch + 10 * 4], e.r, e.g, e.b);
            }
    }
    printf("deriv: %d check(s) failed\n", failures);
    Release({ ps, vs, sampler, srv, texture, views[0], views[1], views[2], views[3], targets[0], targets[1], targets[2], targets[3],
              context, device });
    return failures;
}

// ---------------------------------------------------------------- tess: the points of each edge, read by stream output
struct TessCase { const char* name; const BYTE* hull; SIZE_T size; float factors[4]; int points[3]; };

static void TessCaseRun(ID3D11Device* device, ID3D11DeviceContext* context, const TessCase& t)
{
    ID3D11VertexShader* vs = NULL;
    ID3D11HullShader* hs = NULL;
    ID3D11DomainShader* ds = NULL;
    ID3D11GeometryShader* so = NULL;
    device->CreateVertexShader(MorePatchVertexShader, sizeof(MorePatchVertexShader), NULL, &vs);
    device->CreateHullShader(t.hull, t.size, NULL, &hs);
    device->CreateDomainShader(MoreEdgesDomainShader, sizeof(MoreEdgesDomainShader), NULL, &ds);
    D3D11_SO_DECLARATION_ENTRY decl[] = { { 0, "SV_POSITION", 0, 0, 4, 0 }, { 0, "TEXCOORD", 0, 0, 3, 0 } };
    UINT stride = 28;
    device->CreateGeometryShaderWithStreamOutput(MoreEdgesDomainShader, sizeof(MoreEdgesDomainShader), decl, 2, &stride, 1,
                                                 D3D11_SO_NO_RASTERIZED_STREAM, NULL, &so);
    D3D11_INPUT_ELEMENT_DESC element = { "POSITION", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0, D3D11_INPUT_PER_VERTEX_DATA, 0 };
    ID3D11InputLayout* layout = NULL;
    device->CreateInputLayout(&element, 1, MorePatchVertexShader, sizeof(MorePatchVertexShader), &layout);
    const float corners[] = { -0.8f, -0.8f, 0, 0.8f, 0.8f, -0.8f };
    D3D11_BUFFER_DESC vd = { sizeof(corners), D3D11_USAGE_DEFAULT, D3D11_BIND_VERTEX_BUFFER };
    D3D11_SUBRESOURCE_DATA vdata = { corners };
    ID3D11Buffer* vertices = NULL;
    device->CreateBuffer(&vd, &vdata, &vertices);
    D3D11_BUFFER_DESC cd = { 16, D3D11_USAGE_DEFAULT, D3D11_BIND_CONSTANT_BUFFER };
    D3D11_SUBRESOURCE_DATA cdata = { t.factors };
    ID3D11Buffer* constants = NULL;
    device->CreateBuffer(&cd, &cdata, &constants);
    D3D11_BUFFER_DESC od = { 28 * 3 * 256, D3D11_USAGE_DEFAULT, D3D11_BIND_STREAM_OUTPUT };
    ID3D11Buffer* output = NULL;
    device->CreateBuffer(&od, NULL, &output);
    D3D11_QUERY_DESC qd = { D3D11_QUERY_SO_STATISTICS, 0 };
    ID3D11Query* query = NULL;
    device->CreateQuery(&qd, &query);
    bool ready = vs && hs && ds && so && layout && vertices && constants && output && query;
    Check(ready, "the objects of the tessellation (a stream output after the domain shader)");
    if(ready)
    {
        UINT vstride = 8, offset = 0;
        context->IASetInputLayout(layout);
        context->IASetVertexBuffers(0, 1, &vertices, &vstride, &offset);
        context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_3_CONTROL_POINT_PATCHLIST);
        context->VSSetShader(vs, NULL, 0);
        context->HSSetShader(hs, NULL, 0);
        context->HSSetConstantBuffers(0, 1, &constants);
        context->DSSetShader(ds, NULL, 0);
        context->GSSetShader(so, NULL, 0);
        context->PSSetShader(NULL, NULL, 0);
        context->SOSetTargets(1, &output, &offset);
        context->Begin(query);
        context->Draw(3, 0);
        context->End(query);
        ID3D11Buffer* none = NULL;
        context->SOSetTargets(1, &none, &offset);
        D3D11_QUERY_DATA_SO_STATISTICS stats = {};
        while(context->GetData(query, &stats, sizeof(stats), 0) == S_FALSE)
            ;
        std::vector<BYTE> data;
        if(ReadBuffer11(device, context, output, 28 * 3 * 256, data))
        {
            const float* v = (const float*)data.data();
            double area = 0;
            std::vector<std::pair<float, float>> edges[3];
            for(UINT p = 0; p < stats.NumPrimitivesWritten && p < 256; p++)
            {
                const float* a = v + (p * 3 + 0) * 7 + 4;
                const float* b = v + (p * 3 + 1) * 7 + 4;
                const float* c = v + (p * 3 + 2) * 7 + 4;
                area += fabs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) / 2;
                for(const float* q : { a, b, c })
                {
                    float coords[3] = { q[0], q[1], 1 - q[0] - q[1] };
                    for(int e = 0; e < 3; e++)
                        if(fabsf(coords[e]) < 1e-5f)
                        {
                            auto point = std::make_pair(roundf(q[0] * 10000) / 10000, roundf(q[1] * 10000) / 10000);
                            if(std::find(edges[e].begin(), edges[e].end(), point) == edges[e].end())
                                edges[e].push_back(point);
                        }
                }
            }
            printf("tess: %s: %llu triangles, area %.4f, points on the edges %zu %zu %zu\n", t.name, stats.NumPrimitivesWritten, area,
                   edges[0].size(), edges[1].size(), edges[2].size());
            Check(stats.NumPrimitivesWritten > 0 && stats.NumPrimitivesWritten == stats.PrimitivesStorageNeeded,
                  "the stream output statistics of the tessellation");
            Check(fabs(area - 0.5) < 1e-3, "the triangles of the tessellation cover the domain");
            Check((int)edges[0].size() == t.points[0] && (int)edges[1].size() == t.points[1] && (int)edges[2].size() == t.points[2],
                  t.name);
        }
        context->HSSetShader(NULL, NULL, 0);
        context->DSSetShader(NULL, NULL, 0);
        context->GSSetShader(NULL, NULL, 0);
    }
    Release({ query, output, constants, vertices, layout, so, ds, hs, vs });
}

static int Tess()
{
    ID3D11Device* device = NULL;
    ID3D11DeviceContext* context = NULL;
    Check(Device11(&device, &context), "D3D11CreateDevice");
    if(!device)
        return failures;
    // the points of an edge: factor + 1 (integer), the odd number of segments above the factor + 1 (fractional_odd)
    TessCase cases[] = {
        { "integer partitioning, edges 2 3 4, inside 3", MoreIntegerHullShader, sizeof(MoreIntegerHullShader), { 2, 3, 4, 3 }, { 3, 4, 5 } },
        { "fractional_odd partitioning, edges 2.5 1 5.2, inside 3", MoreOddHullShader, sizeof(MoreOddHullShader), { 2.5f, 1, 5.2f, 3 },
          { 4, 2, 8 } } };
    for(auto& t : cases)
        TessCaseRun(device, context, t);
    printf("tess: %d check(s) failed\n", failures);
    Release({ context, device });
    return failures;
}

// ---------------------------------------------------------------- so: a geometry shader's triangles, then DrawAuto
static int StreamOut()
{
    ID3D11Device* device = NULL;
    ID3D11DeviceContext* context = NULL;
    Check(Device11(&device, &context), "D3D11CreateDevice");
    if(!device)
        return failures;
    ID3D11VertexShader* seedVs = NULL;
    ID3D11GeometryShader* gs = NULL;
    ID3D11VertexShader* drawVs = NULL;
    ID3D11PixelShader* ps = NULL;
    device->CreateVertexShader(MoreSeedVertexShader, sizeof(MoreSeedVertexShader), NULL, &seedVs);
    D3D11_SO_DECLARATION_ENTRY decl[] = { { 0, "POSITION", 0, 0, 4, 0 }, { 0, "COLOR", 0, 0, 4, 0 } };
    UINT stride = 32;
    device->CreateGeometryShaderWithStreamOutput(MoreTriangleGeometryShader, sizeof(MoreTriangleGeometryShader), decl, 2, &stride, 1,
                                                 D3D11_SO_NO_RASTERIZED_STREAM, NULL, &gs);
    device->CreateVertexShader(MoreStreamedVertexShader, sizeof(MoreStreamedVertexShader), NULL, &drawVs);
    device->CreatePixelShader(ColorPixelShader, sizeof(ColorPixelShader), NULL, &ps);
    D3D11_INPUT_ELEMENT_DESC seedElements[] = {
        { "POSITION", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0, D3D11_INPUT_PER_VERTEX_DATA, 0 },
        { "COLOR", 0, DXGI_FORMAT_R32G32B32A32_FLOAT, 0, 8, D3D11_INPUT_PER_VERTEX_DATA, 0 } };
    D3D11_INPUT_ELEMENT_DESC drawElements[] = {
        { "POSITION", 0, DXGI_FORMAT_R32G32B32A32_FLOAT, 0, 0, D3D11_INPUT_PER_VERTEX_DATA, 0 },
        { "COLOR", 0, DXGI_FORMAT_R32G32B32A32_FLOAT, 0, 16, D3D11_INPUT_PER_VERTEX_DATA, 0 } };
    ID3D11InputLayout* seedLayout = NULL;
    ID3D11InputLayout* drawLayout = NULL;
    device->CreateInputLayout(seedElements, 2, MoreSeedVertexShader, sizeof(MoreSeedVertexShader), &seedLayout);
    device->CreateInputLayout(drawElements, 2, MoreStreamedVertexShader, sizeof(MoreStreamedVertexShader), &drawLayout);
    const float seeds[] = { -0.5f, 0, 1, 0, 0, 1, 0.5f, 0, 0, 1, 0, 1 };
    D3D11_BUFFER_DESC vd = { sizeof(seeds), D3D11_USAGE_DEFAULT, D3D11_BIND_VERTEX_BUFFER };
    D3D11_SUBRESOURCE_DATA vdata = { seeds };
    ID3D11Buffer* seedBuffer = NULL;
    device->CreateBuffer(&vd, &vdata, &seedBuffer);
    D3D11_BUFFER_DESC od = { 32 * 6, D3D11_USAGE_DEFAULT, D3D11_BIND_STREAM_OUTPUT | D3D11_BIND_VERTEX_BUFFER };
    ID3D11Buffer* streamed = NULL;
    device->CreateBuffer(&od, NULL, &streamed);
    ID3D11Texture2D* target = Texture11(device, 200, 200, DXGI_FORMAT_R8G8B8A8_UNORM, D3D11_BIND_RENDER_TARGET);
    ID3D11RenderTargetView* view = NULL;
    if(target)
        device->CreateRenderTargetView(target, NULL, &view);
    D3D11_QUERY_DESC qd = { D3D11_QUERY_SO_STATISTICS, 0 };
    ID3D11Query* query = NULL;
    device->CreateQuery(&qd, &query);
    bool ready = seedVs && gs && drawVs && ps && seedLayout && drawLayout && seedBuffer && streamed && view && query;
    Check(ready, "the objects of the stream output");
    if(ready)
    {
        // pass 1: two points become two triangles in the buffer (nothing rasterized)
        UINT seedStride = 24, offset = 0;
        context->IASetInputLayout(seedLayout);
        context->IASetVertexBuffers(0, 1, &seedBuffer, &seedStride, &offset);
        context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_POINTLIST);
        context->VSSetShader(seedVs, NULL, 0);
        context->GSSetShader(gs, NULL, 0);
        context->PSSetShader(NULL, NULL, 0);
        context->SOSetTargets(1, &streamed, &offset);
        context->Begin(query);
        context->Draw(2, 0);
        context->End(query);
        ID3D11Buffer* none = NULL;
        context->SOSetTargets(1, &none, &offset);
        D3D11_QUERY_DATA_SO_STATISTICS stats = {};
        while(context->GetData(query, &stats, sizeof(stats), 0) == S_FALSE)
            ;
        printf("so: %llu primitives written, %llu needed\n", stats.NumPrimitivesWritten, stats.PrimitivesStorageNeeded);
        Check(stats.NumPrimitivesWritten == 2 && stats.PrimitivesStorageNeeded == 2, "the stream output statistics");
        // pass 2: the buffer drawn with DrawAuto
        const float black[4] = { 0, 0, 0, 1 };
        context->ClearRenderTargetView(view, black);
        context->OMSetRenderTargets(1, &view, NULL);
        D3D11_VIEWPORT viewport = { 0, 0, 200, 200, 0, 1 };
        context->RSSetViewports(1, &viewport);
        UINT drawStride = 32;
        context->IASetInputLayout(drawLayout);
        context->IASetVertexBuffers(0, 1, &streamed, &drawStride, &offset);
        context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        context->VSSetShader(drawVs, NULL, 0);
        context->GSSetShader(NULL, NULL, 0);
        context->PSSetShader(ps, NULL, 0);
        context->DrawAuto();
        std::vector<BYTE> data;
        UINT pitch = 0;
        if(Read11(device, context, target, data, pitch))
        {
            CheckRgb("so", "the first streamed triangle, drawn by DrawAuto (red)", &data[100 * pitch + 50 * 4], 255, 0, 0);
            CheckRgb("so", "the second streamed triangle (green)", &data[100 * pitch + 150 * 4], 0, 255, 0);
            CheckRgb("so", "between the triangles (black)", &data[100 * pitch + 100 * 4], 0, 0, 0);
        }
    }
    printf("so: %d check(s) failed\n", failures);
    Release({ query, view, target, streamed, seedBuffer, drawLayout, seedLayout, ps, drawVs, gs, seedVs, context, device });
    return failures;
}

// ---------------------------------------------------------------- deferred: a command list recorded, then executed
static int Deferred()
{
    ID3D11Device* device = NULL;
    ID3D11DeviceContext* immediate = NULL;
    Check(Device11(&device, &immediate), "D3D11CreateDevice");
    if(!device)
        return failures;
    ID3D11DeviceContext* deferred = NULL;
    Check(SUCCEEDED(device->CreateDeferredContext(0, &deferred)), "CreateDeferredContext");
    if(!deferred)
    {
        Release({ immediate, device });
        return failures;
    }
    Check(deferred->GetType() == D3D11_DEVICE_CONTEXT_DEFERRED, "the type of the deferred context");
    ID3D11Texture2D* target = Texture11(device, 64, 64, DXGI_FORMAT_R8G8B8A8_UNORM, D3D11_BIND_RENDER_TARGET);
    ID3D11RenderTargetView* view = NULL;
    if(target)
        device->CreateRenderTargetView(target, NULL, &view);
    ID3D11VertexShader* vs = NULL;
    ID3D11PixelShader* ps = NULL;
    device->CreateVertexShader(MorePlainVertexShader, sizeof(MorePlainVertexShader), NULL, &vs);
    device->CreatePixelShader(MorePaintPixelShader, sizeof(MorePaintPixelShader), NULL, &ps);
    D3D11_INPUT_ELEMENT_DESC element = { "POSITION", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0, D3D11_INPUT_PER_VERTEX_DATA, 0 };
    ID3D11InputLayout* layout = NULL;
    device->CreateInputLayout(&element, 1, MorePlainVertexShader, sizeof(MorePlainVertexShader), &layout);
    // two triangles: the top left half, and a small one at the bottom right
    const float corners[] = { -1, 1, 1, 1, -1, -1, 1, 0, 1, -1, 0, -1 };
    D3D11_BUFFER_DESC vd = { sizeof(corners), D3D11_USAGE_DEFAULT, D3D11_BIND_VERTEX_BUFFER };
    D3D11_SUBRESOURCE_DATA vdata = { corners };
    ID3D11Buffer* vertices = NULL;
    device->CreateBuffer(&vd, &vdata, &vertices);
    D3D11_BUFFER_DESC dyn = { 16, D3D11_USAGE_DYNAMIC, D3D11_BIND_CONSTANT_BUFFER, D3D11_CPU_ACCESS_WRITE };
    ID3D11Buffer* mapped = NULL;
    device->CreateBuffer(&dyn, NULL, &mapped);
    D3D11_BUFFER_DESC def = { 16, D3D11_USAGE_DEFAULT, D3D11_BIND_CONSTANT_BUFFER };
    ID3D11Buffer* updated = NULL;
    device->CreateBuffer(&def, NULL, &updated);
    D3D11_RASTERIZER_DESC rd = { D3D11_FILL_SOLID, D3D11_CULL_NONE, FALSE, 0, 0, 0, TRUE };
    ID3D11RasterizerState* rasterizer = NULL;
    device->CreateRasterizerState(&rd, &rasterizer);
    bool ready = view && vs && ps && layout && vertices && mapped && updated && rasterizer;
    Check(ready, "the objects of the deferred context's scene");
    if(ready)
    {
        // recorded: a clear, a constant buffer written by Map, a draw, one by UpdateSubresource, a draw
        const float blue[4] = { 0, 0, 1, 1 };
        deferred->ClearRenderTargetView(view, blue);
        deferred->OMSetRenderTargets(1, &view, NULL);
        D3D11_VIEWPORT viewport = { 0, 0, 64, 64, 0, 1 };
        deferred->RSSetViewports(1, &viewport);
        deferred->RSSetState(rasterizer);
        UINT stride = 8, offset = 0;
        deferred->IASetInputLayout(layout);
        deferred->IASetVertexBuffers(0, 1, &vertices, &stride, &offset);
        deferred->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        deferred->VSSetShader(vs, NULL, 0);
        deferred->PSSetShader(ps, NULL, 0);
        D3D11_MAPPED_SUBRESOURCE m;
        if(SUCCEEDED(deferred->Map(mapped, 0, D3D11_MAP_WRITE_DISCARD, 0, &m)))
        {
            const float orange[4] = { 1, 0.5f, 0, 1 };
            memcpy(m.pData, orange, 16);
            deferred->Unmap(mapped, 0);
        }
        deferred->PSSetConstantBuffers(0, 1, &mapped);
        deferred->Draw(3, 0);
        float green[4] = { 0, 1, 0, 1 };
        deferred->UpdateSubresource(updated, 0, NULL, green, 0, 0);
        green[0] = 1;   // changed after the call: the command list keeps what it was given
        deferred->PSSetConstantBuffers(0, 1, &updated);
        deferred->Draw(3, 3);
        ID3D11CommandList* list = NULL;
        Check(SUCCEEDED(deferred->FinishCommandList(FALSE, &list)) && list, "FinishCommandList");
        // the immediate context: nothing drawn until the list runs
        const float black[4] = { 0, 0, 0, 1 };
        immediate->ClearRenderTargetView(view, black);
        std::vector<BYTE> data;
        UINT pitch = 0;
        if(Read11(device, immediate, target, data, pitch))
            CheckRgb("deferred", "nothing drawn before ExecuteCommandList", &data[10 * pitch + 10 * 4], 0, 0, 0);
        if(list)
            immediate->ExecuteCommandList(list, FALSE);
        if(Read11(device, immediate, target, data, pitch))
        {
            CheckRgb("deferred", "the triangle of the mapped constant buffer (orange)", &data[10 * pitch + 10 * 4], 255, 128, 0);
            CheckRgb("deferred", "the triangle of the updated constant buffer (green)", &data[58 * pitch + 58 * 4], 0, 255, 0);
            CheckRgb("deferred", "the clear of the command list (blue)", &data[40 * pitch + 40 * 4], 0, 0, 255);
        }
        if(list)
            list->Release();
    }
    printf("deferred: %d check(s) failed\n", failures);
    Release({ rasterizer, updated, mapped, vertices, layout, ps, vs, view, target, deferred, immediate, device });
    return failures;
}
