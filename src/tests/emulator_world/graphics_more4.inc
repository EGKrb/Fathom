// The dxilgs, dxilops, dxr2, mesh6, graph and sm69 modes of emulator_world (included by target.cpp, after graphics_more3.inc).
// dxilgs: hull, domain and geometry shaders of shader model 6 in Direct3D 12, their points captured by
// stream output. dxilops: the rarer DXIL operations (waves, quads, packing, gathers, comparison sampling,
// counters, ResourceDescriptorHeap) in a compute shader and a pixel shader. dxr2: ray tracing with an intersection
// shader, local root signatures and RayQuery. mesh6: amplification and mesh shaders (WARP natively when the GPU
// has none). graph: a work graph (WARP natively when the GPU has none). sm69: shader execution reordering and long
// vectors (shader model 6.9: skipped natively here, checked in the emulator against computed values). Checked
// natively and in the emulator.

#include "more_sm6_vs.h"
#include "more_sm6_hs.h"
#include "more_sm6_ds.h"
#include "more_sm6_gs.h"
#include "more_sm6_ops_cs.h"
#include "more_sm6_quad_vs.h"
#include "more_sm6_ops_ps.h"

// closes the list, runs it and waits for the queue
static void Finish12(ID3D12Device* device, ID3D12CommandQueue* queue, ID3D12GraphicsCommandList* list)
{
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
        WaitForSingleObject(event, 10000);
    }
    CloseHandle(event);
    fence->Release();
}

static ID3D12RootSignature* RootSignature12(ID3D12Device* device, D3D12_ROOT_SIGNATURE_FLAGS flags)
{
    D3D12_VERSIONED_ROOT_SIGNATURE_DESC desc = {};
    desc.Version = D3D_ROOT_SIGNATURE_VERSION_1_1;
    desc.Desc_1_1.Flags = flags;
    ID3DBlob* blob = NULL;
    ID3D12RootSignature* root = NULL;
    if(SUCCEEDED(D3D12SerializeVersionedRootSignature(&desc, &blob, NULL)))
    {
        device->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), __uuidof(ID3D12RootSignature), (void**)&root);
        blob->Release();
    }
    return root;
}

// ---------------------------------------------------------------- dxilgs
static unsigned long long Fnv(unsigned long long hash, long long value)
{
    for(int k = 0; k < 8; k++)
    {
        hash ^= (unsigned long long)(value >> (8 * k)) & 0xFF;
        hash *= 1099511628211ull;
    }
    return hash;
}

static int DxilStages()
{
    ID3D12Device* device = NULL;
    if(FAILED(D3D12CreateDevice(NULL, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device), (void**)&device)))
        return Check(false, "D3D12CreateDevice"), failures;
    ID3D12RootSignature* root = RootSignature12(device, D3D12_ROOT_SIGNATURE_FLAG_ALLOW_INPUT_ASSEMBLER_INPUT_LAYOUT |
                                                        D3D12_ROOT_SIGNATURE_FLAG_ALLOW_STREAM_OUTPUT);
    D3D12_INPUT_ELEMENT_DESC layout[] = { { "POSITION", 0, DXGI_FORMAT_R32G32B32A32_FLOAT, 0, 0, D3D12_INPUT_CLASSIFICATION_PER_VERTEX_DATA, 0 } };
    D3D12_SO_DECLARATION_ENTRY entries[] = { { 0, "SV_Position", 0, 0, 4, 0 }, { 0, "DATA", 0, 0, 4, 0 } };
    UINT stride = 32;
    D3D12_GRAPHICS_PIPELINE_STATE_DESC pd = {};
    pd.pRootSignature = root;
    pd.VS = { MoreSm6StagesVertexShader, sizeof(MoreSm6StagesVertexShader) };
    pd.HS = { MoreSm6HullShader, sizeof(MoreSm6HullShader) };
    pd.DS = { MoreSm6DomainShader, sizeof(MoreSm6DomainShader) };
    pd.GS = { MoreSm6GeometryShader, sizeof(MoreSm6GeometryShader) };
    pd.StreamOutput = { entries, 2, &stride, 1, D3D12_SO_NO_RASTERIZED_STREAM };
    pd.BlendState.RenderTarget[0].RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
    pd.SampleMask = UINT_MAX;
    pd.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID;
    pd.RasterizerState.CullMode = D3D12_CULL_MODE_NONE;
    pd.RasterizerState.DepthClipEnable = TRUE;
    pd.InputLayout = { layout, 1 };
    pd.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_PATCH;
    pd.SampleDesc.Count = 1;
    ID3D12PipelineState* pso = NULL;
    if(root)
        device->CreateGraphicsPipelineState(&pd, __uuidof(ID3D12PipelineState), (void**)&pso);
    Check(root && pso, "the pipeline state (vs_6_0, hs_6_0, ds_6_0, gs_6_0, stream output)");
    const float points[12] = { 0, 0, 0, 1, 1, 0, 0, 1, 0, 1, 0, 1 };
    ID3D12Resource* vertices = Upload12(device, points, sizeof(points));
    const UINT size = 64 * 1024;
    D3D12_HEAP_PROPERTIES properties = { D3D12_HEAP_TYPE_DEFAULT };
    D3D12_RESOURCE_DESC desc = { D3D12_RESOURCE_DIMENSION_BUFFER, 0, size + 256, 1, 1, 1, DXGI_FORMAT_UNKNOWN, { 1, 0 },
                                 D3D12_TEXTURE_LAYOUT_ROW_MAJOR, D3D12_RESOURCE_FLAG_NONE };
    ID3D12Resource* so = NULL;
    device->CreateCommittedResource(&properties, D3D12_HEAP_FLAG_NONE, &desc, D3D12_RESOURCE_STATE_STREAM_OUT, NULL,
                                    __uuidof(ID3D12Resource), (void**)&so);
    ID3D12Resource* readback = Buffer12(device, D3D12_HEAP_TYPE_READBACK, size + 256, D3D12_RESOURCE_STATE_COPY_DEST);
    D3D12_COMMAND_QUEUE_DESC qd = {};
    ID3D12CommandQueue* queue = NULL;
    device->CreateCommandQueue(&qd, __uuidof(ID3D12CommandQueue), (void**)&queue);
    ID3D12CommandAllocator* allocator = NULL;
    ID3D12GraphicsCommandList* list = NULL;
    device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator), (void**)&allocator);
    device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator, pso, __uuidof(ID3D12GraphicsCommandList), (void**)&list);
    if(pso && vertices && so && readback && list)
    {
        list->SetGraphicsRootSignature(root);
        D3D12_VERTEX_BUFFER_VIEW vb = { vertices->GetGPUVirtualAddress(), sizeof(points), 16 };
        list->IASetVertexBuffers(0, 1, &vb);
        list->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_3_CONTROL_POINT_PATCHLIST);
        D3D12_VIEWPORT viewport = { 0, 0, 64, 64, 0, 1 };
        list->RSSetViewports(1, &viewport);
        D3D12_STREAM_OUTPUT_BUFFER_VIEW target = { so->GetGPUVirtualAddress(), size, so->GetGPUVirtualAddress() + size };
        list->SOSetTargets(0, 1, &target);
        list->DrawInstanced(3, 1, 0, 0);
        Barrier(list, so, D3D12_RESOURCE_STATE_STREAM_OUT, D3D12_RESOURCE_STATE_COPY_SOURCE);
        list->CopyBufferRegion(readback, 0, so, 0, size + 256);
        Finish12(device, queue, list);
        void* mapped = NULL;
        if(SUCCEEDED(readback->Map(0, NULL, &mapped)))
        {
            const float* f = (const float*)mapped;
            UINT count = (UINT)(*(const unsigned long long*)((const BYTE*)mapped + size) / stride);
            int wrongPositions = 0, wrongIds = 0, instance1 = 0;
            long long keys[4096];
            UINT n = count < 4096 ? count : 4096;
            for(UINT i = 0; i < n; i++)
            {
                const float* v = f + i * 8;
                float u = v[4], w = v[5];
                // the domain shader: 2 * (p0 u + p1 v + p2 (1 - u - v)) with p0 = 0, p1 = (1, 0), p2 = (0, 1)
                if(fabsf(v[0] - 2 * w) > 1e-4f || fabsf(v[1] - 2 * (1 - u - w)) > 1e-4f || v[3] != 2)
                    wrongPositions++;
                if(v[6] != 100 || (v[7] != 0 && v[7] != 1000))
                    wrongIds++;
                instance1 += v[7] == 1000;
                keys[i] = (long long)floorf(u * 4096 + 0.5f) * 65536 + (long long)floorf(w * 4096 + 0.5f) +
                          (v[7] == 1000 ? 1ll << 40 : 0);
            }
            for(UINT i = 1; i < n; i++)     // the points in a fixed order: the hash does not depend on the GPU's order
                for(UINT j = i; j > 0 && keys[j - 1] > keys[j]; j--)
                {
                    long long t = keys[j];
                    keys[j] = keys[j - 1];
                    keys[j - 1] = t;
                }
            unsigned long long hash = 14695981039346656037ull;
            for(UINT i = 0; i < n; i++)
                hash = Fnv(hash, keys[i]);
            if(GetEnvironmentVariableW(L"WORLD_DUMP", NULL, 0))     // the points, to compare two runs
                for(UINT i = 0; i < n; i++)
                    printf("dxilgs point %lld %lld %lld\n", keys[i] >> 40, (keys[i] >> 16) & 0xFFFFFF, keys[i] & 0xFFFF);
            printf("dxilgs: %u points (%d of instance 1), hash %016llx, %d wrong position(s), %d wrong id(s)\n", count,
                   instance1, hash, wrongPositions, wrongIds);
            // edges 2, 3, 4 and inside 3 (integer): 13 triangles, twice (two instances of the geometry shader)
            Check(count == 78 && instance1 == 39, "the tessellation (a factor per edge) and the instances of the geometry shader");
            Check(wrongPositions == 0, "the domain shader (control points of the hull shader, SV_DomainLocation)");
            Check(wrongIds == 0, "the patch constants (SV_PrimitiveID, a control point) and SV_GSInstanceID");
            Check(hash == 0xf13f68c2376a4ee2ull, "the points of the tessellation, as on the GPU");
            readback->Unmap(0, NULL);
        }
    }
    else
        Check(false, "the objects of the dxilgs mode");
    printf("dxilgs: %d check(s) failed\n", failures);
    IUnknown* objects[] = { list, allocator, queue, readback, so, vertices, pso, root, device };
    for(auto object : objects)
        if(object)
            object->Release();
    return failures;
}

// ---------------------------------------------------------------- dxilops
static ID3D12Resource* Texture12(ID3D12Device* device, ID3D12GraphicsCommandList* list, DXGI_FORMAT format, const void* texels,
                                 UINT width, UINT height, UINT texel, ID3D12Resource** upload)
{
    D3D12_HEAP_PROPERTIES properties = { D3D12_HEAP_TYPE_DEFAULT };
    D3D12_RESOURCE_DESC desc = { D3D12_RESOURCE_DIMENSION_TEXTURE2D, 0, width, height, 1, 1, format, { 1, 0 },
                                 D3D12_TEXTURE_LAYOUT_UNKNOWN, D3D12_RESOURCE_FLAG_NONE };
    ID3D12Resource* texture = NULL;
    device->CreateCommittedResource(&properties, D3D12_HEAP_FLAG_NONE, &desc, D3D12_RESOURCE_STATE_COPY_DEST, NULL,
                                    __uuidof(ID3D12Resource), (void**)&texture);
    BYTE staging[256 * 16] = {};
    for(UINT y = 0; y < height; y++)
        memcpy(staging + y * 256, (const BYTE*)texels + y * width * texel, width * texel);
    *upload = Upload12(device, staging, 256 * height);
    if(texture && *upload)
    {
        D3D12_TEXTURE_COPY_LOCATION dst = { texture, D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX };
        D3D12_TEXTURE_COPY_LOCATION src = { *upload, D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT };
        src.PlacedFootprint.Footprint = { format, width, height, 1, 256 };
        list->CopyTextureRegion(&dst, 0, 0, 0, &src, NULL);
        Barrier(list, texture, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_ALL_SHADER_RESOURCE);
    }
    return texture;
}

static void DxilOpsExpected(UINT lane, UINT out[16])
{
    UINT w = lane % 32;
    out[0] = 0;
    out[1] = 1;
    out[2] = 21;
    out[3] = 0xFF;
    out[4] = 0xF0;
    out[5] = 0;
    out[6] = 11;
    out[7] = (w + 2) / 3;
    out[8] = w / 4;
    out[9] = w / 4;
    out[10] = (lane ^ 1) + (lane ^ 2) * 256 + (lane ^ 3) * 65536;
    out[11] = ((lane & ~3u) | 2) + 1000;
    // msad4: the masked sums of absolute differences of the reference's bytes and four windows of the source
    UINT reference = 0x01020304u, sx = 0x05060708u, sy = 0x090A0B0Cu, sad[4];
    for(UINT i = 0; i < 4; i++)
    {
        UINT window = i ? (sx >> (8 * i)) | (sy << (32 - 8 * i)) : sx;
        sad[i] = i == 0 ? lane : 0;
        for(UINT j = 0; j < 4; j++)
        {
            int r = (reference >> (8 * j)) & 0xFF, s = (window >> (8 * j)) & 0xFF;
            sad[i] += r ? (UINT)abs(r - s) : 0;
        }
    }
    out[12] = sad[0] + sad[1] * 16 + sad[2] * 256 + sad[3] * 4096;
    out[13] = lane + 5;             // 3*4 - 2*2 + 2*-1 + -1*1
    out[14] = lane + 1285;          // 3*4 + 254*2 + 2*255 + 255*1
    out[15] = 0x80FD7F02u - 128;    // pack_clamp_s8(2, 127, -3, -300) + (-128)
}

static int DxilOps()
{
    ID3D12Device* device = NULL;
    if(FAILED(D3D12CreateDevice(NULL, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device), (void**)&device)))
        return Check(false, "D3D12CreateDevice"), failures;
    D3D12_FEATURE_DATA_SHADER_MODEL model = { D3D_SHADER_MODEL_6_8 };
    Check(SUCCEEDED(device->CheckFeatureSupport(D3D12_FEATURE_SHADER_MODEL, &model, sizeof(model))) &&
          model.HighestShaderModel >= D3D_SHADER_MODEL_6_8, "shader model 6.8");
    ID3D12RootSignature* root = RootSignature12(device, D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED |
                                                        D3D12_ROOT_SIGNATURE_FLAG_SAMPLER_HEAP_DIRECTLY_INDEXED);
    D3D12_COMPUTE_PIPELINE_STATE_DESC cd = {};
    cd.pRootSignature = root;
    cd.CS = { MoreSm6OpsComputeShader, sizeof(MoreSm6OpsComputeShader) };
    ID3D12PipelineState* compute = NULL;
    if(root)
        device->CreateComputePipelineState(&cd, __uuidof(ID3D12PipelineState), (void**)&compute);
    D3D12_GRAPHICS_PIPELINE_STATE_DESC pd = {};
    pd.pRootSignature = root;
    pd.VS = { MoreSm6QuadVertexShader, sizeof(MoreSm6QuadVertexShader) };
    pd.PS = { MoreSm6OpsPixelShader, sizeof(MoreSm6OpsPixelShader) };
    pd.BlendState.RenderTarget[0].RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
    pd.SampleMask = UINT_MAX;
    pd.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID;
    pd.RasterizerState.CullMode = D3D12_CULL_MODE_NONE;
    pd.RasterizerState.DepthClipEnable = TRUE;
    pd.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE;
    pd.NumRenderTargets = 1;
    pd.RTVFormats[0] = DXGI_FORMAT_R8G8B8A8_UNORM;
    pd.SampleDesc.Count = 1;
    ID3D12PipelineState* graphics = NULL;
    if(root)
        device->CreateGraphicsPipelineState(&pd, __uuidof(ID3D12PipelineState), (void**)&graphics);
    Check(compute && graphics, "the pipeline states (cs_6_8, vs_6_6, ps_6_6, heaps directly indexed)");
    D3D12_COMMAND_QUEUE_DESC qd = {};
    ID3D12CommandQueue* queue = NULL;
    device->CreateCommandQueue(&qd, __uuidof(ID3D12CommandQueue), (void**)&queue);
    ID3D12CommandAllocator* allocator = NULL;
    ID3D12GraphicsCommandList* list = NULL;
    device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator), (void**)&allocator);
    device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator, NULL, __uuidof(ID3D12GraphicsCommandList), (void**)&list);
    // the textures, the results, the append buffer and its counter, the render target
    float floats[16];
    UINT uints[16];
    for(UINT i = 0; i < 16; i++)
        floats[i] = (float)(uints[i] = i);
    ID3D12Resource *uploadFloats = NULL, *uploadUints = NULL;
    ID3D12Resource* values = list ? Texture12(device, list, DXGI_FORMAT_R32_FLOAT, floats, 4, 4, 4, &uploadFloats) : NULL;
    ID3D12Resource* raws = list ? Texture12(device, list, DXGI_FORMAT_R32_UINT, uints, 4, 4, 4, &uploadUints) : NULL;
    const UINT resultSize = 16384;
    ID3D12Resource* results = UavBuffer12(device, resultSize);
    ID3D12Resource* odd = UavBuffer12(device, 64 * 4);
    ID3D12Resource* counter = UavBuffer12(device, 256);
    ID3D12Resource* readback = Buffer12(device, D3D12_HEAP_TYPE_READBACK, resultSize + 512, D3D12_RESOURCE_STATE_COPY_DEST);
    D3D12_HEAP_PROPERTIES properties = { D3D12_HEAP_TYPE_DEFAULT };
    D3D12_RESOURCE_DESC rtDesc = { D3D12_RESOURCE_DIMENSION_TEXTURE2D, 0, 8, 8, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, { 1, 0 },
                                   D3D12_TEXTURE_LAYOUT_UNKNOWN, D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET };
    ID3D12Resource* target = NULL;
    device->CreateCommittedResource(&properties, D3D12_HEAP_FLAG_NONE, &rtDesc, D3D12_RESOURCE_STATE_RENDER_TARGET, NULL,
                                    __uuidof(ID3D12Resource), (void**)&target);
    // the heaps: 0 Texture2D<float>, 1 Texture2D<uint>, 2 the results (raw), 3 the append buffer; samplers 0 point, 1 comparison
    D3D12_DESCRIPTOR_HEAP_DESC hd = { D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 4, D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE };
    D3D12_DESCRIPTOR_HEAP_DESC sd = { D3D12_DESCRIPTOR_HEAP_TYPE_SAMPLER, 2, D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE };
    D3D12_DESCRIPTOR_HEAP_DESC rd = { D3D12_DESCRIPTOR_HEAP_TYPE_RTV, 1, D3D12_DESCRIPTOR_HEAP_FLAG_NONE };
    ID3D12DescriptorHeap *heap = NULL, *samplers = NULL, *rtvs = NULL;
    device->CreateDescriptorHeap(&hd, __uuidof(ID3D12DescriptorHeap), (void**)&heap);
    device->CreateDescriptorHeap(&sd, __uuidof(ID3D12DescriptorHeap), (void**)&samplers);
    device->CreateDescriptorHeap(&rd, __uuidof(ID3D12DescriptorHeap), (void**)&rtvs);
    bool ready = root && compute && graphics && queue && list && values && raws && results && odd && counter && readback && target &&
                 heap && samplers && rtvs;
    if(ready)
    {
        UINT step = device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
        D3D12_CPU_DESCRIPTOR_HANDLE slot = heap->GetCPUDescriptorHandleForHeapStart();
        device->CreateShaderResourceView(values, NULL, slot);
        slot.ptr += step;
        device->CreateShaderResourceView(raws, NULL, slot);
        slot.ptr += step;
        D3D12_UNORDERED_ACCESS_VIEW_DESC raw = {};
        raw.Format = DXGI_FORMAT_R32_TYPELESS;
        raw.ViewDimension = D3D12_UAV_DIMENSION_BUFFER;
        raw.Buffer.NumElements = resultSize / 4;
        raw.Buffer.Flags = D3D12_BUFFER_UAV_FLAG_RAW;
        device->CreateUnorderedAccessView(results, NULL, &raw, slot);
        slot.ptr += step;
        D3D12_UNORDERED_ACCESS_VIEW_DESC append = {};
        append.Format = DXGI_FORMAT_UNKNOWN;
        append.ViewDimension = D3D12_UAV_DIMENSION_BUFFER;
        append.Buffer.NumElements = 64;
        append.Buffer.StructureByteStride = 4;
        append.Buffer.CounterOffsetInBytes = 0;
        device->CreateUnorderedAccessView(odd, counter, &append, slot);
        D3D12_SAMPLER_DESC point = {};
        point.Filter = D3D12_FILTER_MIN_MAG_MIP_POINT;
        point.AddressU = point.AddressV = point.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
        point.MaxLOD = D3D12_FLOAT32_MAX;
        D3D12_CPU_DESCRIPTOR_HANDLE sampler = samplers->GetCPUDescriptorHandleForHeapStart();
        device->CreateSampler(&point, sampler);
        point.Filter = D3D12_FILTER_COMPARISON_MIN_MAG_MIP_POINT;
        point.ComparisonFunc = D3D12_COMPARISON_FUNC_LESS_EQUAL;
        sampler.ptr += device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_SAMPLER);
        device->CreateSampler(&point, sampler);
        device->CreateRenderTargetView(target, NULL, rtvs->GetCPUDescriptorHandleForHeapStart());
        ID3D12DescriptorHeap* bound[] = { heap, samplers };
        list->SetDescriptorHeaps(2, bound);
        list->SetComputeRootSignature(root);
        list->SetPipelineState(compute);
        list->Dispatch(1, 1, 1);
        D3D12_RESOURCE_BARRIER uav = {};
        uav.Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
        uav.UAV.pResource = results;
        list->ResourceBarrier(1, &uav);
        list->SetGraphicsRootSignature(root);
        list->SetPipelineState(graphics);
        D3D12_CPU_DESCRIPTOR_HANDLE rtv = rtvs->GetCPUDescriptorHandleForHeapStart();
        list->OMSetRenderTargets(1, &rtv, FALSE, NULL);
        D3D12_VIEWPORT viewport = { 0, 0, 8, 8, 0, 1 };
        D3D12_RECT scissor = { 0, 0, 8, 8 };
        list->RSSetViewports(1, &viewport);
        list->RSSetScissorRects(1, &scissor);
        list->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        list->DrawInstanced(3, 1, 0, 0);
        ID3D12Resource* sources[3] = { results, odd, counter };
        for(auto source : sources)
            Barrier(list, source, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
        list->CopyBufferRegion(readback, 0, results, 0, resultSize);
        list->CopyBufferRegion(readback, resultSize, odd, 0, 256);
        list->CopyBufferRegion(readback, resultSize + 256, counter, 0, 4);
        Finish12(device, queue, list);
        void* mapped = NULL;
        if(SUCCEEDED(readback->Map(0, NULL, &mapped)))
        {
            const UINT* r = (const UINT*)mapped;
            int wrong = 0;
            for(UINT lane = 0; lane < 64; lane++)
            {
                UINT expected[16];
                DxilOpsExpected(lane, expected);
                for(UINT k = 0; k < 16; k++)
                    if(r[lane * 16 + k] != expected[k])
                    {
                        if(wrong < 6)
                            printf("dxilops: lane %u value %u: %08x, expected %08x\n", lane, k, r[lane * 16 + k], expected[k]);
                        wrong++;
                    }
            }
            printf("dxilops: %d wrong value(s) of the waves, quads, msad, dot4add, pack / unpack\n", wrong);
            Check(wrong == 0, "WaveActiveAllEqual / ReadLaneAt / Bit* / CountBits, WaveMatch, WaveMultiPrefix*, quad operations, "
                              "msad4, dot4add, pack / unpack");
            const UINT* t = r + 1024;
            double twice = 0;
            unsigned long long bits = 0x40091EB812345678ull;
            memcpy(&twice, &bits, 8);
            twice *= 2;
            memcpy(&bits, &twice, 8);
            printf("dxilops: gather %08x, gather cmp %u, raw %08x, cmp level / grad %u, double %08x%08x, dimensions %u\n", t[0], t[1],
                   t[2], t[3], t[5], t[4], t[6]);
            Check(t[0] == (9u | 10u << 8 | 6u << 16 | 5u << 24) && t[2] == t[0], "GatherRed and GatherRaw");
            Check(t[1] == 3, "GatherCmpRed (LESS_EQUAL)");
            Check(t[3] == 9, "SampleCmpLevel and SampleCmpGrad");
            Check(t[4] == (UINT)bits && t[5] == (UINT)(bits >> 32), "asdouble / asuint of a double");
            Check(t[6] == 441, "GetDimensions with the mip levels");
            const UINT* appended = r + resultSize / 4;
            UINT count = r[(resultSize + 256) / 4], sum = 0;
            for(UINT i = 0; i < count && i < 64; i++)
                sum += appended[i];
            printf("dxilops: %u values appended (sum %u)\n", count, sum);
            Check(count == 32 && sum == 1024, "Append on an AppendStructuredBuffer of the heap (its counter resource)");
            int wrongPixels = 0;
            for(UINT y = 0; y < 8; y++)
                for(UINT x = 0; x < 8; x++)
                {
                    const UINT* p = r + (8192 + (y * 8 + x) * 16) / 4;
                    UINT eval = (4 * x + 2) | (4 * y + 2) << 8 | (4 * x + 2) << 16 | (4 * y + 2) << 24;
                    UINT across = (x ^ 1) + (y ^ 1) * 16;
                    if(p[0] != eval || p[1] != 101 || p[2] != across || p[3] != 1010)
                    {
                        if(wrongPixels < 4)
                            printf("dxilops: pixel (%u, %u): %08x %u %u %u\n", x, y, p[0], p[1], p[2], p[3]);
                        wrongPixels++;
                    }
                }
            printf("dxilops: %d wrong pixel(s)\n", wrongPixels);
            Check(wrongPixels == 0, "EvaluateAttributeSnapped / Centroid, GetRenderTargetSampleCount, SV_Coverage, "
                                    "QuadReadAcrossX / Y and derivatives in a pixel shader");
            readback->Unmap(0, NULL);
        }
    }
    else
        Check(false, "the objects of the dxilops mode");
    printf("dxilops: %d check(s) failed\n", failures);
    IUnknown* objects[] = { rtvs, samplers, heap, target, readback, counter, odd, results, raws, values, uploadUints, uploadFloats,
                            list, allocator, queue, graphics, compute, root, device };
    for(auto object : objects)
        if(object)
            object->Release();
    return failures;
}

// ---------------------------------------------------------------- dxr2: procedural geometry, local root signatures, RayQuery
#include "more_dxr2.h"
#include "more_dxr2_cs.h"

// what the shaders of more_dxr2.hlsl find for the ray (i, j) of the 16 x 8 grid: a quad (instance 5, x in
// [-1, 0]) and a sphere of radius 0.5 at (0.5, 0, 0) (instance 6) whose lower hits (normal y < -0.5) are ignored
struct Dxr2Hit
{
    UINT kind;          // 0 miss, 1 quad, 2 sphere
    UINT primitive;
    float t, a, b, c;   // quad: barycentrics; sphere: normal
};

static Dxr2Hit Dxr2Expected(UINT i, UINT j)
{
    float ox = (i + 0.5f) / 16 * 2 - 1, oy = 1 - (j + 0.5f) / 8 * 2;
    Dxr2Hit hit = {};
    float x = ox + 0.5f, y = oy;
    if(x >= -0.5f && x <= 0.5f && y >= -0.5f && y <= 0.5f)
    {
        hit.kind = 1;
        hit.primitive = y > x ? 0 : 1;
        hit.t = 2;
        if(hit.primitive == 0)
        {
            hit.b = x + 0.5f;
            hit.a = y + 0.5f - hit.b;
        }
        else
        {
            hit.a = y + 0.5f;
            hit.b = x + 0.5f - hit.a;
        }
        return hit;
    }
    float dx = ox - 0.5f, dy = oy, d2 = dx * dx + dy * dy;
    if(d2 < 0.25f && 2 * dy >= -0.5f)
    {
        float s = sqrtf(0.25f - d2);
        hit.kind = 2;
        hit.t = 2 - s;
        hit.a = dx * 2;
        hit.b = dy * 2;
        hit.c = -s * 2;
    }
    return hit;
}

static bool Near(float a, float b)
{
    return fabsf(a - b) < 1e-3f;
}

static int Dxr2()
{
    ID3D12Device5* device = NULL;
    if(FAILED(D3D12CreateDevice(NULL, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device5), (void**)&device)))
        return Check(false, "D3D12CreateDevice (ID3D12Device5)"), failures;
    D3D12_FEATURE_DATA_D3D12_OPTIONS5 options = {};
    device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS5, &options, sizeof(options));
    Check(options.RaytracingTier >= D3D12_RAYTRACING_TIER_1_1, "ray tracing tier 1.1 (RayQuery)");
    // the global root signature (the acceleration structure in a table, the output a root UAV) and the local one
    D3D12_DESCRIPTOR_RANGE range = { D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, 0, 0, 0 };
    D3D12_ROOT_PARAMETER parameters[2] = {};
    parameters[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    parameters[0].DescriptorTable.NumDescriptorRanges = 1;
    parameters[0].DescriptorTable.pDescriptorRanges = &range;
    parameters[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
    D3D12_ROOT_SIGNATURE_DESC globalDesc = { 2, parameters, 0, NULL, D3D12_ROOT_SIGNATURE_FLAG_NONE };
    D3D12_ROOT_PARAMETER constant = {};
    constant.ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    constant.Constants = { 0, 1, 1 };       // b0, space1, one value
    D3D12_ROOT_SIGNATURE_DESC localDesc = { 1, &constant, 0, NULL, D3D12_ROOT_SIGNATURE_FLAG_LOCAL_ROOT_SIGNATURE };
    ID3D12RootSignature *global = NULL, *local = NULL;
    ID3DBlob* blob = NULL;
    if(SUCCEEDED(D3D12SerializeRootSignature(&globalDesc, D3D_ROOT_SIGNATURE_VERSION_1, &blob, NULL)))
    {
        device->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), __uuidof(ID3D12RootSignature), (void**)&global);
        blob->Release();
    }
    if(SUCCEEDED(D3D12SerializeRootSignature(&localDesc, D3D_ROOT_SIGNATURE_VERSION_1, &blob, NULL)))
    {
        device->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), __uuidof(ID3D12RootSignature), (void**)&local);
        blob->Release();
    }
    // the state object: the library (its hit groups and configurations are subobjects of the library), the
    // local root signature associated with the hit groups and the miss shader, the global root signature
    D3D12_DXIL_LIBRARY_DESC library = { { MoreDxr2Library, sizeof(MoreDxr2Library) }, 0, NULL };
    D3D12_LOCAL_ROOT_SIGNATURE localSubobject = { local };
    D3D12_GLOBAL_ROOT_SIGNATURE globalSubobject = { global };
    D3D12_STATE_SUBOBJECT subobjects[4] = {
        { D3D12_STATE_SUBOBJECT_TYPE_DXIL_LIBRARY, &library },
        { D3D12_STATE_SUBOBJECT_TYPE_LOCAL_ROOT_SIGNATURE, &localSubobject },
        { D3D12_STATE_SUBOBJECT_TYPE_SUBOBJECT_TO_EXPORTS_ASSOCIATION, NULL },
        { D3D12_STATE_SUBOBJECT_TYPE_GLOBAL_ROOT_SIGNATURE, &globalSubobject },
    };
    LPCWSTR associated[3] = { L"QuadGroup", L"SphereGroup", L"Miss" };
    D3D12_SUBOBJECT_TO_EXPORTS_ASSOCIATION association = { &subobjects[1], 3, associated };
    subobjects[2].pDesc = &association;
    D3D12_STATE_OBJECT_DESC stateDesc = { D3D12_STATE_OBJECT_TYPE_RAYTRACING_PIPELINE, 4, subobjects };
    ID3D12StateObject* state = NULL;
    ID3D12StateObjectProperties* properties = NULL;
    if(global && local)
        device->CreateStateObject(&stateDesc, __uuidof(ID3D12StateObject), (void**)&state);
    if(state)
        state->QueryInterface(__uuidof(ID3D12StateObjectProperties), (void**)&properties);
    Check(state && properties, "the state object (subobjects of the library, local root signature)");
    D3D12_COMPUTE_PIPELINE_STATE_DESC cd = {};
    cd.pRootSignature = global;
    cd.CS = { MoreDxr2QueryShader, sizeof(MoreDxr2QueryShader) };
    ID3D12PipelineState* query = NULL;
    if(global)
        device->CreateComputePipelineState(&cd, __uuidof(ID3D12PipelineState), (void**)&query);
    Check(query != NULL, "the compute pipeline state with RayQuery (cs_6_5)");
    // the shader tables: ray generation at 0, miss at 64, the hit groups at 128 (records of 64 bytes: the
    // identifier and the local root constant, the tag the shaders write)
    BYTE tables[256] = {};
    struct
    {
        const wchar_t* name;
        UINT offset, tag;
    } records[4] = { { L"RayGen", 0, 0 }, { L"Miss", 64, 33 }, { L"QuadGroup", 128, 11 }, { L"SphereGroup", 192, 22 } };
    bool identified = properties != NULL;
    for(UINT k = 0; k < 4 && properties; k++)
    {
        void* identifier = properties->GetShaderIdentifier(records[k].name);
        identified = identified && identifier;
        if(identifier)
            memcpy(tables + records[k].offset, identifier, D3D12_SHADER_IDENTIFIER_SIZE_IN_BYTES);
        memcpy(tables + records[k].offset + D3D12_SHADER_IDENTIFIER_SIZE_IN_BYTES, &records[k].tag, 4);
    }
    Check(identified, "GetShaderIdentifier (hit groups of the library)");
    ID3D12Resource* table = Upload12(device, tables, sizeof(tables));
    // the quad (triangles, opaque) and the box of the sphere (an AABB, not opaque)
    const float quad[18] = { -0.5f, -0.5f, 0, -0.5f, 0.5f, 0, 0.5f, 0.5f, 0, -0.5f, -0.5f, 0, 0.5f, 0.5f, 0, 0.5f, -0.5f, 0 };
    const float box[6] = { -0.5f, -0.5f, -0.5f, 0.5f, 0.5f, 0.5f };
    ID3D12Resource* vertices = Upload12(device, quad, sizeof(quad));
    ID3D12Resource* boxes = Upload12(device, box, sizeof(box));
    D3D12_RAYTRACING_GEOMETRY_DESC geometries[2] = {};
    geometries[0].Type = D3D12_RAYTRACING_GEOMETRY_TYPE_TRIANGLES;
    geometries[0].Flags = D3D12_RAYTRACING_GEOMETRY_FLAG_OPAQUE;
    geometries[0].Triangles.VertexFormat = DXGI_FORMAT_R32G32B32_FLOAT;
    geometries[0].Triangles.VertexCount = 6;
    geometries[0].Triangles.VertexBuffer.StartAddress = vertices ? vertices->GetGPUVirtualAddress() : 0;
    geometries[0].Triangles.VertexBuffer.StrideInBytes = 12;
    geometries[1].Type = D3D12_RAYTRACING_GEOMETRY_TYPE_PROCEDURAL_PRIMITIVE_AABBS;
    geometries[1].Flags = D3D12_RAYTRACING_GEOMETRY_FLAG_NONE;
    geometries[1].AABBs.AABBCount = 1;
    geometries[1].AABBs.AABBs.StartAddress = boxes ? boxes->GetGPUVirtualAddress() : 0;
    geometries[1].AABBs.AABBs.StrideInBytes = sizeof(box);
    ID3D12Resource *bottoms[2] = {}, *scratches[2] = {};
    D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_INPUTS bottomInputs[2] = {};
    for(UINT k = 0; k < 2; k++)
    {
        bottomInputs[k].Type = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL;
        bottomInputs[k].NumDescs = 1;
        bottomInputs[k].DescsLayout = D3D12_ELEMENTS_LAYOUT_ARRAY;
        bottomInputs[k].pGeometryDescs = &geometries[k];
        D3D12_RAYTRACING_ACCELERATION_STRUCTURE_PREBUILD_INFO info = {};
        device->GetRaytracingAccelerationStructurePrebuildInfo(&bottomInputs[k], &info);
        bottoms[k] = StructureBuffer12(device, info.ResultDataMaxSizeInBytes, D3D12_RESOURCE_STATE_RAYTRACING_ACCELERATION_STRUCTURE);
        scratches[k] = StructureBuffer12(device, info.ScratchDataSizeInBytes, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    }
    D3D12_RAYTRACING_INSTANCE_DESC instances[2] = {};
    for(UINT k = 0; k < 2; k++)
    {
        instances[k].Transform[0][0] = instances[k].Transform[1][1] = instances[k].Transform[2][2] = 1;
        instances[k].Transform[0][3] = k ? 0.5f : -0.5f;
        instances[k].InstanceID = 5 + k;
        instances[k].InstanceMask = 0xFF;
        instances[k].InstanceContributionToHitGroupIndex = k;
        instances[k].AccelerationStructure = bottoms[k] ? bottoms[k]->GetGPUVirtualAddress() : 0;
    }
    ID3D12Resource* instanceBuffer = Upload12(device, instances, sizeof(instances));
    D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_INPUTS topInputs = {};
    topInputs.Type = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_TOP_LEVEL;
    topInputs.NumDescs = 2;
    topInputs.DescsLayout = D3D12_ELEMENTS_LAYOUT_ARRAY;
    topInputs.InstanceDescs = instanceBuffer ? instanceBuffer->GetGPUVirtualAddress() : 0;
    D3D12_RAYTRACING_ACCELERATION_STRUCTURE_PREBUILD_INFO topInfo = {};
    device->GetRaytracingAccelerationStructurePrebuildInfo(&topInputs, &topInfo);
    ID3D12Resource* top = StructureBuffer12(device, topInfo.ResultDataMaxSizeInBytes, D3D12_RESOURCE_STATE_RAYTRACING_ACCELERATION_STRUCTURE);
    ID3D12Resource* topScratch = StructureBuffer12(device, topInfo.ScratchDataSizeInBytes, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    const UINT size = 1024 * 16;
    ID3D12Resource* output = UavBuffer12(device, size);
    ID3D12Resource* readback = Buffer12(device, D3D12_HEAP_TYPE_READBACK, size, D3D12_RESOURCE_STATE_COPY_DEST);
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
    bool ready = state && properties && query && table && vertices && boxes && bottoms[0] && bottoms[1] && scratches[0] &&
                 scratches[1] && instanceBuffer && top && topScratch && output && readback && heap && queue && list;
    if(ready)
    {
        D3D12_RESOURCE_BARRIER barrier = {};
        barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
        D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC build = {};
        for(UINT k = 0; k < 2; k++)
        {
            build.DestAccelerationStructureData = bottoms[k]->GetGPUVirtualAddress();
            build.Inputs = bottomInputs[k];
            build.ScratchAccelerationStructureData = scratches[k]->GetGPUVirtualAddress();
            list->BuildRaytracingAccelerationStructure(&build, 0, NULL);
            barrier.UAV.pResource = bottoms[k];
            list->ResourceBarrier(1, &barrier);
        }
        build.DestAccelerationStructureData = top->GetGPUVirtualAddress();
        build.Inputs = topInputs;
        build.ScratchAccelerationStructureData = topScratch->GetGPUVirtualAddress();
        list->BuildRaytracingAccelerationStructure(&build, 0, NULL);
        barrier.UAV.pResource = top;
        list->ResourceBarrier(1, &barrier);
        list->SetDescriptorHeaps(1, &heap);
        list->SetComputeRootSignature(global);
        list->SetComputeRootDescriptorTable(0, heap->GetGPUDescriptorHandleForHeapStart());
        list->SetComputeRootUnorderedAccessView(1, output->GetGPUVirtualAddress());
        list->SetPipelineState1(state);
        D3D12_DISPATCH_RAYS_DESC rays = {};
        D3D12_GPU_VIRTUAL_ADDRESS base = table->GetGPUVirtualAddress();
        rays.RayGenerationShaderRecord = { base, 64 };
        rays.MissShaderTable = { base + 64, 64, 64 };
        rays.HitGroupTable = { base + 128, 128, 64 };
        rays.Width = 16;
        rays.Height = 8;
        rays.Depth = 1;
        list->DispatchRays(&rays);
        barrier.UAV.pResource = output;
        list->ResourceBarrier(1, &barrier);
        list->SetPipelineState(query);
        list->Dispatch(1, 1, 1);
        Barrier(list, output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
        list->CopyBufferRegion(readback, 0, output, 0, size);
        Finish12(device, queue, list);
        void* mapped = NULL;
        if(SUCCEEDED(readback->Map(0, NULL, &mapped)))
        {
            const UINT* got = (const UINT*)mapped;
            int wrongRays = 0, wrongQueries = 0, quads = 0, spheres = 0, ignored = 0;
            for(UINT j = 0; j < 8; j++)
                for(UINT i = 0; i < 16; i++)
                {
                    Dxr2Hit e = Dxr2Expected(i, j);
                    const UINT* r = got + (j * 16 + i) * 16;
                    const float* rf = (const float*)r + 4;
                    bool ok;
                    if(e.kind == 1)
                        ok = r[0] == 11 && r[1] == 5 && r[2] == e.primitive && r[3] == 254 && Near(rf[0], e.t) && Near(rf[1], e.a) &&
                             Near(rf[2], e.b);
                    else if(e.kind == 2)
                        ok = r[0] == 22 && r[1] == 6 && r[2] == 0 && r[3] == 1 && Near(rf[0], e.t) && Near(rf[1], e.a) &&
                             Near(rf[2], e.b) && Near(rf[3], e.c);
                    else
                        ok = r[0] == 33 && r[1] == 0xFFFF && Near(rf[0], 100);
                    if(!ok && wrongRays < 4)
                        printf("dxr2: ray (%u, %u): %u %x %u %u %g %g %g %g (expected kind %u t %g %g %g %g)\n", i, j, r[0], r[1], r[2],
                               r[3], rf[0], rf[1], rf[2], rf[3], e.kind, e.t, e.a, e.b, e.c);
                    wrongRays += !ok;
                    const UINT* q = got + (512 + (j * 16 + i) * 4) * 4;
                    const float* qf = (const float*)q + 4;
                    if(e.kind == 1)
                        ok = q[0] == 1 && q[1] == 5 && q[2] == e.primitive && q[3] == 1 && Near(qf[0], 2) && Near(qf[1], e.a) &&
                             Near(qf[2], e.b);
                    else if(e.kind == 2)
                        ok = q[0] == 2 && q[1] == 6 && q[2] == 0 && q[3] == 0 && Near(qf[0], e.t) && Near(qf[1], e.a) &&
                             Near(qf[2], e.b) && Near(qf[3], e.c);
                    else
                        ok = q[0] == 0 && q[1] == 0xFFFF;
                    if(!ok && wrongQueries < 4)
                        printf("dxr2: query (%u, %u): %u %x %u %u %g %g %g %g\n", i, j, q[0], q[1], q[2], q[3], qf[0], qf[1], qf[2], qf[3]);
                    wrongQueries += !ok;
                    quads += e.kind == 1;
                    spheres += e.kind == 2;
                    float dx = (i + 0.5f) / 16 * 2 - 1.5f, dy = 1 - (j + 0.5f) / 8 * 2;
                    ignored += e.kind == 0 && dx * dx + dy * dy < 0.25f;
                }
            printf("dxr2: %d quad and %d sphere hits (%d ignored by the any hit shader), %d wrong ray(s), %d wrong quer(ies)\n",
                   quads, spheres, ignored, wrongRays, wrongQueries);
            Check(wrongRays == 0, "TraceRay: intersection shader, ReportHit, any hit on a procedural primitive, local root "
                                  "constants of the hit groups and of the miss shader");
            Check(wrongQueries == 0, "RayQuery: Proceed, CommitProceduralPrimitiveHit, the committed values");
            readback->Unmap(0, NULL);
        }
    }
    else
        Check(false, "the objects of the dxr2 mode");
    printf("dxr2: %d check(s) failed\n", failures);
    IUnknown* objects[] = { list, allocator, queue, heap, readback, output, topScratch, top, instanceBuffer, scratches[1], scratches[0],
                            bottoms[1], bottoms[0], boxes, vertices, table, query, properties, state, local, global, device };
    for(auto object : objects)
        if(object)
            object->Release();
    return failures;
}

// ---------------------------------------------------------------- mesh6: amplification and mesh shaders (DispatchMesh)
#include "more_mesh6_as.h"
#include "more_mesh6_ms.h"
#include "more_mesh6_ps.h"

// a subobject of a pipeline state stream (as CD3DX12_PIPELINE_STATE_STREAM_SUBOBJECT)
template <D3D12_PIPELINE_STATE_SUBOBJECT_TYPE Type, typename Value> struct alignas(void*) StreamSubobject
{
    D3D12_PIPELINE_STATE_SUBOBJECT_TYPE type = Type;
    Value value;
};

struct MeshStream
{
    StreamSubobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_ROOT_SIGNATURE, ID3D12RootSignature*> root;
    StreamSubobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_AS, D3D12_SHADER_BYTECODE> as;
    StreamSubobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_MS, D3D12_SHADER_BYTECODE> ms;
    StreamSubobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_PS, D3D12_SHADER_BYTECODE> ps;
    StreamSubobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_RASTERIZER, D3D12_RASTERIZER_DESC> rasterizer;
    StreamSubobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_DEPTH_STENCIL, D3D12_DEPTH_STENCIL_DESC> depth;
    StreamSubobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_RENDER_TARGET_FORMATS, D3D12_RT_FORMAT_ARRAY> formats;
    StreamSubobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_SAMPLE_DESC, DXGI_SAMPLE_DESC> samples;
    StreamSubobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_SAMPLE_MASK, UINT> mask;
};

static int Mesh6()
{
    // the GPU, or WARP when the GPU has no mesh shaders
    ID3D12Device2* device = NULL;
    if(FAILED(D3D12CreateDevice(NULL, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device2), (void**)&device)))
        return Check(false, "D3D12CreateDevice"), failures;
    D3D12_FEATURE_DATA_D3D12_OPTIONS7 options = {};
    device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS7, &options, sizeof(options));
    if(options.MeshShaderTier == D3D12_MESH_SHADER_TIER_NOT_SUPPORTED)
    {
        device->Release();
        device = NULL;
        IDXGIFactory4* factory = NULL;
        IDXGIAdapter* warp = NULL;
        if(SUCCEEDED(CreateDXGIFactory1(__uuidof(IDXGIFactory4), (void**)&factory)) &&
           SUCCEEDED(factory->EnumWarpAdapter(__uuidof(IDXGIAdapter), (void**)&warp)))
            D3D12CreateDevice(warp, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device2), (void**)&device);
        if(warp)
            warp->Release();
        if(factory)
            factory->Release();
        if(!device)
            return Check(false, "a device with mesh shaders (WARP)"), failures;
        device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS7, &options, sizeof(options));
        printf("mesh6: the GPU has no mesh shaders: WARP\n");
    }
    Check(options.MeshShaderTier >= D3D12_MESH_SHADER_TIER_1, "mesh shaders tier 1");
    D3D12_ROOT_SIGNATURE_DESC rootDesc = { 0, NULL, 0, NULL, D3D12_ROOT_SIGNATURE_FLAG_NONE };
    ID3DBlob* blob = NULL;
    ID3D12RootSignature* root = NULL;
    if(SUCCEEDED(D3D12SerializeRootSignature(&rootDesc, D3D_ROOT_SIGNATURE_VERSION_1, &blob, NULL)))
    {
        device->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), __uuidof(ID3D12RootSignature), (void**)&root);
        blob->Release();
    }
    MeshStream stream = {};
    stream.root.value = root;
    stream.as.value = { MoreMeshAmplificationShader, sizeof(MoreMeshAmplificationShader) };
    stream.ms.value = { MoreMeshShader, sizeof(MoreMeshShader) };
    stream.ps.value = { MoreMeshPixelShader, sizeof(MoreMeshPixelShader) };
    stream.rasterizer.value.FillMode = D3D12_FILL_MODE_SOLID;
    stream.rasterizer.value.CullMode = D3D12_CULL_MODE_NONE;
    stream.rasterizer.value.DepthClipEnable = TRUE;
    stream.depth.value.DepthEnable = FALSE;
    stream.formats.value.NumRenderTargets = 1;
    stream.formats.value.RTFormats[0] = DXGI_FORMAT_R8G8B8A8_UNORM;
    stream.samples.value = { 1, 0 };
    stream.mask.value = UINT_MAX;
    D3D12_PIPELINE_STATE_STREAM_DESC streamDesc = { sizeof(stream), &stream };
    ID3D12PipelineState* pso = NULL;
    if(root)
        device->CreatePipelineState(&streamDesc, __uuidof(ID3D12PipelineState), (void**)&pso);
    Check(pso != NULL, "CreatePipelineState (a stream: amplification, mesh and pixel shaders)");
    D3D12_HEAP_PROPERTIES properties = { D3D12_HEAP_TYPE_DEFAULT };
    D3D12_RESOURCE_DESC rtDesc = { D3D12_RESOURCE_DIMENSION_TEXTURE2D, 0, 32, 32, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, { 1, 0 },
                                   D3D12_TEXTURE_LAYOUT_UNKNOWN, D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET };
    ID3D12Resource* target = NULL;
    device->CreateCommittedResource(&properties, D3D12_HEAP_FLAG_NONE, &rtDesc, D3D12_RESOURCE_STATE_RENDER_TARGET, NULL,
                                    __uuidof(ID3D12Resource), (void**)&target);
    ID3D12Resource* readback = Buffer12(device, D3D12_HEAP_TYPE_READBACK, 256 * 32, D3D12_RESOURCE_STATE_COPY_DEST);
    D3D12_DESCRIPTOR_HEAP_DESC rd = { D3D12_DESCRIPTOR_HEAP_TYPE_RTV, 1, D3D12_DESCRIPTOR_HEAP_FLAG_NONE };
    ID3D12DescriptorHeap* rtvs = NULL;
    device->CreateDescriptorHeap(&rd, __uuidof(ID3D12DescriptorHeap), (void**)&rtvs);
    D3D12_COMMAND_QUEUE_DESC qd = {};
    ID3D12CommandQueue* queue = NULL;
    device->CreateCommandQueue(&qd, __uuidof(ID3D12CommandQueue), (void**)&queue);
    ID3D12CommandAllocator* allocator = NULL;
    ID3D12GraphicsCommandList6* list = NULL;
    device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator), (void**)&allocator);
    if(allocator)
        device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator, pso, __uuidof(ID3D12GraphicsCommandList6),
                                  (void**)&list);
    if(pso && target && readback && rtvs && queue && list)
    {
        D3D12_CPU_DESCRIPTOR_HANDLE rtv = rtvs->GetCPUDescriptorHandleForHeapStart();
        device->CreateRenderTargetView(target, NULL, rtv);
        const float clear[4] = { 0, 0, 0, 0 };
        list->ClearRenderTargetView(rtv, clear, 0, NULL);
        list->OMSetRenderTargets(1, &rtv, FALSE, NULL);
        list->SetGraphicsRootSignature(root);
        D3D12_VIEWPORT viewport = { 0, 0, 32, 32, 0, 1 };
        D3D12_RECT scissor = { 0, 0, 32, 32 };
        list->RSSetViewports(1, &viewport);
        list->RSSetScissorRects(1, &scissor);
        list->DispatchMesh(2, 1, 1);
        Barrier(list, target, D3D12_RESOURCE_STATE_RENDER_TARGET, D3D12_RESOURCE_STATE_COPY_SOURCE);
        D3D12_TEXTURE_COPY_LOCATION src = { target, D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX };
        D3D12_TEXTURE_COPY_LOCATION dst = { readback, D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT };
        dst.PlacedFootprint.Footprint = { DXGI_FORMAT_R8G8B8A8_UNORM, 32, 32, 1, 256 };
        list->CopyTextureRegion(&dst, 0, 0, 0, &src, NULL);
        Finish12(device, queue, list);
        void* mapped = NULL;
        if(SUCCEEDED(readback->Map(0, NULL, &mapped)))
        {
            const BYTE* image = (const BYTE*)mapped;
            int drawn = 0, wrong = 0;
            for(UINT y = 0; y < 32; y++)
                for(UINT x = 0; x < 32; x++)
                    drawn += image[y * 256 + x * 4 + 3] == 255;
            for(UINT tile = 0; tile < 8; tile++)
                for(UINT face = 0; face < 2; face++)
                {
                    UINT x = (tile % 4) * 8 + (face ? 6 : 1), y = (tile / 4) * 8 + (face ? 6 : 1);
                    const BYTE* p = image + y * 256 + x * 4;
                    bool shown = tile % 3 != 2 && !(tile == 4 && face == 1);
                    int red = (int)((tile * 2 + face) / 16.0 * 255 + 0.5);
                    bool ok = shown ? (abs(p[0] - red) <= 1 && p[1] == (face ? 255 : 0) && p[3] == 255) : p[3] == 0;
                    if(!ok)
                        printf("mesh6: tile %u triangle %u: %02x %02x %02x %02x\n", tile, face, p[0], p[1], p[2], p[3]);
                    wrong += !ok;
                }
            // five tiles of 8 x 8 pixels, and the first triangle of tile 4 (the diagonal belongs to the second one)
            printf("mesh6: %d pixels drawn, %d wrong triangle(s)\n", drawn, wrong);
            Check(wrong == 0, "the tiles the amplification shader kept, the attributes of the primitives (SV_PrimitiveID, "
                              "an id) and SV_CullPrimitive");
            Check(drawn == 5 * 64 + 28, "the pixels of the mesh shader's triangles");
            readback->Unmap(0, NULL);
        }
    }
    else
        Check(false, "the objects of the mesh6 mode");
    printf("mesh6: %d check(s) failed\n", failures);
    IUnknown* objects[] = { list, allocator, queue, rtvs, readback, target, pso, root, device };
    for(auto object : objects)
        if(object)
            object->Release();
    return failures;
}

// ---------------------------------------------------------------- graph: a work graph (DispatchGraph)
#include "more_graph.h"

static int Graph()
{
    // the GPU, or WARP when the GPU has no work graphs
    ID3D12Device5* device = NULL;
    if(FAILED(D3D12CreateDevice(NULL, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device5), (void**)&device)))
        return Check(false, "D3D12CreateDevice"), failures;
    D3D12_FEATURE_DATA_D3D12_OPTIONS21 options = {};
    device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS21, &options, sizeof(options));
    if(options.WorkGraphsTier == D3D12_WORK_GRAPHS_TIER_NOT_SUPPORTED)
    {
        device->Release();
        device = NULL;
        IDXGIFactory4* factory = NULL;
        IDXGIAdapter* warp = NULL;
        if(SUCCEEDED(CreateDXGIFactory1(__uuidof(IDXGIFactory4), (void**)&factory)) &&
           SUCCEEDED(factory->EnumWarpAdapter(__uuidof(IDXGIAdapter), (void**)&warp)))
            D3D12CreateDevice(warp, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device5), (void**)&device);
        if(warp)
            warp->Release();
        if(factory)
            factory->Release();
        if(!device)
            return Check(false, "a device with work graphs (WARP)"), failures;
        device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS21, &options, sizeof(options));
        printf("graph: the GPU has no work graphs: WARP\n");
    }
    Check(options.WorkGraphsTier >= D3D12_WORK_GRAPHS_TIER_1_0, "work graphs tier 1.0");
    D3D12_ROOT_PARAMETER parameter = {};
    parameter.ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
    D3D12_ROOT_SIGNATURE_DESC rootDesc = { 1, &parameter, 0, NULL, D3D12_ROOT_SIGNATURE_FLAG_NONE };
    ID3DBlob* blob = NULL;
    ID3D12RootSignature* root = NULL;
    if(SUCCEEDED(D3D12SerializeRootSignature(&rootDesc, D3D_ROOT_SIGNATURE_VERSION_1, &blob, NULL)))
    {
        device->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), __uuidof(ID3D12RootSignature), (void**)&root);
        blob->Release();
    }
    // the state object: the library, the work graph (all its nodes), the global root signature
    D3D12_DXIL_LIBRARY_DESC library = { { MoreGraphLibrary, sizeof(MoreGraphLibrary) }, 0, NULL };
    D3D12_WORK_GRAPH_DESC graphDesc = {};
    graphDesc.ProgramName = L"Graph";
    graphDesc.Flags = D3D12_WORK_GRAPH_FLAG_INCLUDE_ALL_AVAILABLE_NODES;
    D3D12_GLOBAL_ROOT_SIGNATURE global = { root };
    D3D12_STATE_SUBOBJECT subobjects[3] = {
        { D3D12_STATE_SUBOBJECT_TYPE_DXIL_LIBRARY, &library },
        { D3D12_STATE_SUBOBJECT_TYPE_WORK_GRAPH, &graphDesc },
        { D3D12_STATE_SUBOBJECT_TYPE_GLOBAL_ROOT_SIGNATURE, &global },
    };
    D3D12_STATE_OBJECT_DESC stateDesc = { D3D12_STATE_OBJECT_TYPE_EXECUTABLE, 3, subobjects };
    ID3D12StateObject* state = NULL;
    ID3D12StateObjectProperties1* properties = NULL;
    ID3D12WorkGraphProperties* graph = NULL;
    if(root)
        device->CreateStateObject(&stateDesc, __uuidof(ID3D12StateObject), (void**)&state);
    if(state)
    {
        state->QueryInterface(__uuidof(ID3D12StateObjectProperties1), (void**)&properties);
        state->QueryInterface(__uuidof(ID3D12WorkGraphProperties), (void**)&graph);
    }
    Check(state && properties && graph, "the work graph state object (lib_6_8)");
    UINT index = 0, entry = 0, recordSize = 0;
    D3D12_WORK_GRAPH_MEMORY_REQUIREMENTS memory = {};
    D3D12_PROGRAM_IDENTIFIER identifier = {};
    if(graph && properties)
    {
        index = graph->GetWorkGraphIndex(L"Graph");
        graph->GetWorkGraphMemoryRequirements(index, &memory);
        entry = graph->GetEntrypointIndex(index, { L"Entry", 0 });
        recordSize = graph->GetEntrypointRecordSizeInBytes(index, entry);
        identifier = properties->GetProgramIdentifier(L"Graph");
        printf("graph: %u node(s), %u entry point(s), record of %u bytes, memory %llu bytes\n", graph->GetNumNodes(index),
               graph->GetNumEntrypoints(index), recordSize, memory.MaxSizeInBytes);
        Check(graph->GetNumNodes(index) == 4 && graph->GetNumEntrypoints(index) == 1 && entry == 0 && recordSize == 4,
              "ID3D12WorkGraphProperties (nodes, entry point, record size)");
    }
    ID3D12Resource* output = UavBuffer12(device, 256);
    ID3D12Resource* backing = memory.MaxSizeInBytes ? UavBuffer12(device, memory.MaxSizeInBytes) : NULL;
    ID3D12Resource* readback = Buffer12(device, D3D12_HEAP_TYPE_READBACK, 256, D3D12_RESOURCE_STATE_COPY_DEST);
    D3D12_COMMAND_QUEUE_DESC qd = {};
    ID3D12CommandQueue* queue = NULL;
    device->CreateCommandQueue(&qd, __uuidof(ID3D12CommandQueue), (void**)&queue);
    ID3D12CommandAllocator* allocator = NULL;
    ID3D12GraphicsCommandList10* list = NULL;
    device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator), (void**)&allocator);
    if(allocator)
        device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator, NULL, __uuidof(ID3D12GraphicsCommandList10),
                                  (void**)&list);
    if(graph && output && backing && readback && queue && list)
    {
        list->SetComputeRootSignature(root);
        list->SetComputeRootUnorderedAccessView(0, output->GetGPUVirtualAddress());
        D3D12_SET_PROGRAM_DESC program = {};
        program.Type = D3D12_PROGRAM_TYPE_WORK_GRAPH;
        program.WorkGraph.ProgramIdentifier = identifier;
        program.WorkGraph.Flags = D3D12_SET_WORK_GRAPH_FLAG_INITIALIZE;
        program.WorkGraph.BackingMemory = { backing->GetGPUVirtualAddress(), memory.MaxSizeInBytes };
        list->SetProgram(&program);
        UINT base = 100;
        D3D12_DISPATCH_GRAPH_DESC dispatch = {};
        dispatch.Mode = D3D12_DISPATCH_MODE_NODE_CPU_INPUT;
        dispatch.NodeCPUInput = { entry, 1, &base, sizeof(base) };
        list->DispatchGraph(&dispatch);
        Barrier(list, output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
        list->CopyBufferRegion(readback, 0, output, 0, 256);
        Finish12(device, queue, list);
        void* mapped = NULL;
        if(SUCCEEDED(readback->Map(0, NULL, &mapped)))
        {
            const UINT* words = (const UINT*)mapped;
            int wrong = 0;
            for(UINT i = 0; i < 8; i++)        // the Item records: value 100 + thread of the grid, its thread
                wrong += words[i] != (100 + i) * 10 + i % 4;
            printf("graph: items %u %u ... %u, count %u, buckets %u %u, %d wrong\n", words[0], words[1], words[7], words[16], words[20],
                   words[21], wrong);
            Check(wrong == 0 && words[16] == 8, "the thread launch node: a record per thread of the entry node");
            Check(words[20] == 1106 && words[21] == 2106, "the coalescing nodes of the node array: a record per group");
            readback->Unmap(0, NULL);
        }
    }
    else
        Check(false, "the objects of the graph mode");
    printf("graph: %d check(s) failed\n", failures);
    IUnknown* objects[] = { list, allocator, queue, readback, backing, output, graph, properties, state, root, device };
    for(auto object : objects)
        if(object)
            object->Release();
    return failures;
}

// ---------------------------------------------------------------- sm69: shader execution reordering, long vectors
#include "more_sm69.h"
#include "more_sm69_cs.h"

static int Sm69()
{
    ID3D12Device5* device = NULL;
    if(FAILED(D3D12CreateDevice(NULL, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device5), (void**)&device)))
        return Check(false, "D3D12CreateDevice"), failures;
    D3D12_FEATURE_DATA_SHADER_MODEL model = { (D3D_SHADER_MODEL)0x69 };
    if(FAILED(device->CheckFeatureSupport(D3D12_FEATURE_SHADER_MODEL, &model, sizeof(model))) || model.HighestShaderModel < 0x69)
    {
        // neither this GPU nor WARP runs shader model 6.9: only the emulator is checked (against the values below)
        printf("sm69: shader model 6.9 not supported here (highest %x): skipped\n", model.HighestShaderModel);
        device->Release();
        return 0;
    }
    D3D12_DESCRIPTOR_RANGE range = { D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, 0, 0, 0 };
    D3D12_ROOT_PARAMETER parameters[3] = {};
    parameters[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    parameters[0].DescriptorTable = { 1, &range };
    parameters[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
    parameters[2].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
    parameters[2].Descriptor.ShaderRegister = 1;
    D3D12_ROOT_SIGNATURE_DESC globalDesc = { 3, parameters, 0, NULL, D3D12_ROOT_SIGNATURE_FLAG_NONE };
    D3D12_ROOT_PARAMETER constant = {};
    constant.ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    constant.Constants = { 0, 1, 1 };
    D3D12_ROOT_SIGNATURE_DESC localDesc = { 1, &constant, 0, NULL, D3D12_ROOT_SIGNATURE_FLAG_LOCAL_ROOT_SIGNATURE };
    ID3D12RootSignature *global = NULL, *local = NULL;
    ID3DBlob* blob = NULL;
    if(SUCCEEDED(D3D12SerializeRootSignature(&globalDesc, D3D_ROOT_SIGNATURE_VERSION_1, &blob, NULL)))
    {
        device->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), __uuidof(ID3D12RootSignature), (void**)&global);
        blob->Release();
    }
    if(SUCCEEDED(D3D12SerializeRootSignature(&localDesc, D3D_ROOT_SIGNATURE_VERSION_1, &blob, NULL)))
    {
        device->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), __uuidof(ID3D12RootSignature), (void**)&local);
        blob->Release();
    }
    D3D12_DXIL_LIBRARY_DESC library = { { MoreSm69Library, sizeof(MoreSm69Library) }, 0, NULL };
    D3D12_HIT_GROUP_DESC group = { L"HitGroup", D3D12_HIT_GROUP_TYPE_TRIANGLES, NULL, L"Hit", NULL };
    D3D12_RAYTRACING_SHADER_CONFIG shaderConfig = { 4, 8 };
    D3D12_RAYTRACING_PIPELINE_CONFIG pipelineConfig = { 1 };
    D3D12_LOCAL_ROOT_SIGNATURE localSubobject = { local };
    D3D12_GLOBAL_ROOT_SIGNATURE globalSubobject = { global };
    D3D12_STATE_SUBOBJECT subobjects[7] = {
        { D3D12_STATE_SUBOBJECT_TYPE_DXIL_LIBRARY, &library },
        { D3D12_STATE_SUBOBJECT_TYPE_HIT_GROUP, &group },
        { D3D12_STATE_SUBOBJECT_TYPE_RAYTRACING_SHADER_CONFIG, &shaderConfig },
        { D3D12_STATE_SUBOBJECT_TYPE_RAYTRACING_PIPELINE_CONFIG, &pipelineConfig },
        { D3D12_STATE_SUBOBJECT_TYPE_LOCAL_ROOT_SIGNATURE, &localSubobject },
        { D3D12_STATE_SUBOBJECT_TYPE_SUBOBJECT_TO_EXPORTS_ASSOCIATION, NULL },
        { D3D12_STATE_SUBOBJECT_TYPE_GLOBAL_ROOT_SIGNATURE, &globalSubobject },
    };
    LPCWSTR associated[2] = { L"HitGroup", L"Miss" };
    D3D12_SUBOBJECT_TO_EXPORTS_ASSOCIATION association = { &subobjects[4], 2, associated };
    subobjects[5].pDesc = &association;
    D3D12_STATE_OBJECT_DESC stateDesc = { D3D12_STATE_OBJECT_TYPE_RAYTRACING_PIPELINE, 7, subobjects };
    ID3D12StateObject* state = NULL;
    ID3D12StateObjectProperties* properties = NULL;
    if(global && local)
        device->CreateStateObject(&stateDesc, __uuidof(ID3D12StateObject), (void**)&state);
    if(state)
        state->QueryInterface(__uuidof(ID3D12StateObjectProperties), (void**)&properties);
    D3D12_COMPUTE_PIPELINE_STATE_DESC cd = {};
    cd.pRootSignature = global;
    cd.CS = { MoreSm69VectorShader, sizeof(MoreSm69VectorShader) };
    ID3D12PipelineState* vectorPso = NULL;
    if(global)
        device->CreateComputePipelineState(&cd, __uuidof(ID3D12PipelineState), (void**)&vectorPso);
    Check(state && properties && vectorPso, "the state object (lib_6_9) and the compute pipeline state (cs_6_9)");
    BYTE tables[256] = {};
    struct
    {
        const wchar_t* name;
        UINT offset, tag;
    } records[3] = { { L"RayGen", 0, 0 }, { L"Miss", 64, 33 }, { L"HitGroup", 128, 11 } };
    for(UINT k = 0; k < 3 && properties; k++)
    {
        void* identifier = properties->GetShaderIdentifier(records[k].name);
        if(identifier)
            memcpy(tables + records[k].offset, identifier, D3D12_SHADER_IDENTIFIER_SIZE_IN_BYTES);
        memcpy(tables + records[k].offset + D3D12_SHADER_IDENTIFIER_SIZE_IN_BYTES, &records[k].tag, 4);
    }
    ID3D12Resource* table = Upload12(device, tables, sizeof(tables));
    const float quad[18] = { -0.5f, -0.5f, 0, -0.5f, 0.5f, 0, 0.5f, 0.5f, 0, -0.5f, -0.5f, 0, 0.5f, 0.5f, 0, 0.5f, -0.5f, 0 };
    ID3D12Resource* vertices = Upload12(device, quad, sizeof(quad));
    D3D12_RAYTRACING_GEOMETRY_DESC geometry = {};
    geometry.Type = D3D12_RAYTRACING_GEOMETRY_TYPE_TRIANGLES;
    geometry.Flags = D3D12_RAYTRACING_GEOMETRY_FLAG_OPAQUE;
    geometry.Triangles.VertexFormat = DXGI_FORMAT_R32G32B32_FLOAT;
    geometry.Triangles.VertexCount = 6;
    geometry.Triangles.VertexBuffer = { vertices ? vertices->GetGPUVirtualAddress() : 0, 12 };
    D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_INPUTS bottomInputs = {};
    bottomInputs.Type = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL;
    bottomInputs.NumDescs = 1;
    bottomInputs.pGeometryDescs = &geometry;
    D3D12_RAYTRACING_ACCELERATION_STRUCTURE_PREBUILD_INFO info = {};
    device->GetRaytracingAccelerationStructurePrebuildInfo(&bottomInputs, &info);
    ID3D12Resource* bottom = StructureBuffer12(device, info.ResultDataMaxSizeInBytes, D3D12_RESOURCE_STATE_RAYTRACING_ACCELERATION_STRUCTURE);
    ID3D12Resource* scratch = StructureBuffer12(device, 64 * 1024, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    D3D12_RAYTRACING_INSTANCE_DESC instance = {};
    instance.Transform[0][0] = instance.Transform[1][1] = instance.Transform[2][2] = 1;
    instance.Transform[0][3] = -0.5f;
    instance.InstanceID = 5;
    instance.InstanceMask = 0xFF;
    instance.AccelerationStructure = bottom ? bottom->GetGPUVirtualAddress() : 0;
    ID3D12Resource* instances = Upload12(device, &instance, sizeof(instance));
    D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_INPUTS topInputs = {};
    topInputs.Type = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_TOP_LEVEL;
    topInputs.NumDescs = 1;
    topInputs.InstanceDescs = instances ? instances->GetGPUVirtualAddress() : 0;
    device->GetRaytracingAccelerationStructurePrebuildInfo(&topInputs, &info);
    ID3D12Resource* top = StructureBuffer12(device, info.ResultDataMaxSizeInBytes, D3D12_RESOURCE_STATE_RAYTRACING_ACCELERATION_STRUCTURE);
    ID3D12Resource* topScratch = StructureBuffer12(device, 64 * 1024, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    // the long vectors: a = 1..8, b = 8..1, u = 1..8
    float numbers[16];
    UINT words[8];
    for(UINT i = 0; i < 8; i++)
    {
        numbers[i] = (float)(i + 1);
        numbers[8 + i] = (float)(8 - i);
        words[i] = i + 1;
    }
    ID3D12Resource* output = UavBuffer12(device, 128 * 64);
    ID3D12Resource* vectors = UavBuffer12(device, 256);
    ID3D12Resource* uploadNumbers = Upload12(device, numbers, sizeof(numbers));
    ID3D12Resource* uploadWords = Upload12(device, words, sizeof(words));
    ID3D12Resource* readback = Buffer12(device, D3D12_HEAP_TYPE_READBACK, 128 * 64 + 256, D3D12_RESOURCE_STATE_COPY_DEST);
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
    if(state && properties && vectorPso && table && bottom && scratch && instances && top && topScratch && output && vectors &&
       uploadNumbers && uploadWords && readback && heap && queue && list)
    {
        D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC build = {};
        build.DestAccelerationStructureData = bottom->GetGPUVirtualAddress();
        build.Inputs = bottomInputs;
        build.ScratchAccelerationStructureData = scratch->GetGPUVirtualAddress();
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
        Barrier(list, vectors, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST);
        list->CopyBufferRegion(vectors, 0, uploadNumbers, 0, sizeof(numbers));
        list->CopyBufferRegion(vectors, 160, uploadWords, 0, sizeof(words));
        Barrier(list, vectors, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        list->SetDescriptorHeaps(1, &heap);
        list->SetComputeRootSignature(global);
        list->SetComputeRootDescriptorTable(0, heap->GetGPUDescriptorHandleForHeapStart());
        list->SetComputeRootUnorderedAccessView(1, output->GetGPUVirtualAddress());
        list->SetComputeRootUnorderedAccessView(2, vectors->GetGPUVirtualAddress());
        list->SetPipelineState1(state);
        D3D12_DISPATCH_RAYS_DESC rays = {};
        D3D12_GPU_VIRTUAL_ADDRESS base = table->GetGPUVirtualAddress();
        rays.RayGenerationShaderRecord = { base, 64 };
        rays.MissShaderTable = { base + 64, 64, 64 };
        rays.HitGroupTable = { base + 128, 64, 64 };
        rays.Width = 16;
        rays.Height = 8;
        rays.Depth = 1;
        list->DispatchRays(&rays);
        list->SetPipelineState(vectorPso);
        list->Dispatch(1, 1, 1);
        Barrier(list, output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
        Barrier(list, vectors, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
        list->CopyBufferRegion(readback, 0, output, 0, 128 * 64);
        list->CopyBufferRegion(readback, 128 * 64, vectors, 0, 256);
        Finish12(device, queue, list);
        void* mapped = NULL;
        if(SUCCEEDED(readback->Map(0, NULL, &mapped)))
        {
            const UINT* r = (const UINT*)mapped;
            int wrong = 0, hits = 0;
            for(UINT j = 0; j < 8; j++)
                for(UINT i = 0; i < 16; i++)
                {
                    const UINT* p = r + (j * 16 + i) * 16;
                    float x = (i + 0.5f) / 16 * 2 - 1 + 0.5f, y = 1 - (j + 0.5f) / 8 * 2;
                    bool hit = x >= -0.5f && x <= 0.5f && y >= -0.5f && y <= 0.5f;
                    UINT primitive = y > x ? 0 : 1;
                    bool ok = hit ? p[0] == 1 && p[1] == 500 + primitive * 10 && p[2] == 254 + 110000 && p[3] == 32 && p[4] == 1016
                                  : p[0] == 2 && p[1] == 0 && p[4] == 2033;
                    ok = ok && (i < 8 ? p[8] == 2033 && p[9] == 1 && p[10] == 0 : p[8] == 5 && p[9] == 0 && p[10] == 1);
                    if(!ok && wrong < 4)
                        printf("sm69: ray (%u, %u): %u %u %u %u, payload %u, made %u %u %u\n", i, j, p[0], p[1], p[2], p[3], p[4], p[8],
                               p[9], p[10]);
                    wrong += !ok;
                    hits += hit;
                }
            const UINT* v = r + 128 * 16;
            const float* vf = (const float*)v;
            int wrongVectors = !(vf[16] == 120 && v[17] == 1 && v[18] == 1);
            for(UINT i = 0; i < 8; i++)
                wrongVectors += vf[24 + i] != (float)(i + 10);
            printf("sm69: %d hits, %d wrong ray(s); dot %g, %d wrong vector value(s)\n", hits, wrong, vf[16], wrongVectors);
            Check(wrong == 0, "HitObject: TraceRay without shaders, its hit, LoadLocalRootTableConstant, MaybeReorderThread, "
                              "Invoke, MakeMiss, MakeNop");
            Check(wrongVectors == 0, "long vectors: loads and stores of 8 values, dot, all, any");
            readback->Unmap(0, NULL);
        }
    }
    else
        Check(false, "the objects of the sm69 mode");
    printf("sm69: %d check(s) failed\n", failures);
    IUnknown* objects[] = { list, allocator, queue, heap, readback, uploadWords, uploadNumbers, vectors, output, topScratch, top,
                            instances, scratch, bottom, vertices, table, vectorPso, properties, state, local, global, device };
    for(auto object : objects)
        if(object)
            object->Release();
    return failures;
}
