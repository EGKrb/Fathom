// The dxilops mode of emulator_world (compiled by dxc, compile_shaders.cmd): the rarer DXIL operations in a
// compute shader (resources from ResourceDescriptorHeap) and a pixel shader.

// ---------------------------------------------------------------- compute
// heap: 0 Texture2D<float> 4x4 (x + 4y), 1 Texture2D<uint> (same), 2 RWByteAddressBuffer results,
// 3 AppendStructuredBuffer<uint> with a counter; samplers: 0 point clamp, 1 point clamp comparison LESS_EQUAL

[WaveSize(32)]
[numthreads(64, 1, 1)]
void cs(uint lane : SV_GroupIndex)
{
    RWByteAddressBuffer results = ResourceDescriptorHeap[2];
    Texture2D<float> values = ResourceDescriptorHeap[0];
    Texture2D<uint> raws = ResourceDescriptorHeap[1];
    AppendStructuredBuffer<uint> odd = ResourceDescriptorHeap[3];
    SamplerState pointSampler = SamplerDescriptorHeap[0];
    SamplerComparisonState compare = SamplerDescriptorHeap[1];
    uint w = WaveGetLaneIndex();
    uint base = lane * 64;
    // waves
    results.Store(base + 0, WaveActiveAllEqual(w / 8) ? 1 : 0);
    results.Store(base + 4, WaveActiveAllEqual(5u) ? 1 : 0);
    results.Store(base + 8, WaveReadLaneAt(w * 3, 7));
    results.Store(base + 12, WaveActiveBitOr(1u << (w % 8)));
    results.Store(base + 16, WaveActiveBitAnd(0xF0u | w));
    results.Store(base + 20, WaveActiveBitXor(w));
    results.Store(base + 24, WaveActiveCountBits(w % 3 == 0));
    results.Store(base + 28, WavePrefixCountBits(w % 3 == 0));
    uint4 peers = WaveMatch(w % 4);
    results.Store(base + 32, WaveMultiPrefixSum(1u, peers));
    results.Store(base + 36, WaveMultiPrefixCountBits(true, peers));
    results.Store(base + 40, QuadReadAcrossX(lane) + QuadReadAcrossY(lane) * 256 + QuadReadAcrossDiagonal(lane) * 65536);
    results.Store(base + 44, QuadReadLaneAt(lane, 2) + (QuadAny(lane % 4 == 3) ? 1000 : 0) + (QuadAll(lane % 4 == 3) ? 2000 : 0));
    // integers, packing, doubles
    uint4 sad = msad4(0x01020304u, uint2(0x05060708u, 0x090A0B0Cu), uint4(lane, 0, 0, 0));
    results.Store(base + 48, sad.x + sad.y * 16 + sad.z * 256 + sad.w * 4096);
    results.Store(base + 52, dot4add_i8packed(0xFF02FE03u, 0x01FF0204u, (int)lane));
    results.Store(base + 56, dot4add_u8packed(0xFF02FE03u, 0x01FF0204u, lane));
    int4 unpacked = unpack_s8s32((int8_t4_packed)0x80FF7F01u);
    results.Store(base + 60, pack_clamp_s8(int4(unpacked.x * 2, unpacked.y, unpacked.z * 3, -300)) + (uint)unpacked.w);
    // textures (the same answers from every thread), counters, doubles
    if(lane == 0)
    {
        float4 g = values.GatherRed(pointSampler, float2(0.5, 0.5));
        results.Store(4096 + 0, (uint)g.x | ((uint)g.y << 8) | ((uint)g.z << 16) | ((uint)g.w << 24));
        float4 c = values.GatherCmpRed(compare, float2(0.5, 0.5), 7.0);
        results.Store(4096 + 4, (uint)(c.x + c.y * 2 + c.z * 4 + c.w * 8));
        uint4 r = raws.GatherRaw(pointSampler, float2(0.5, 0.5));
        results.Store(4096 + 8, r.x | (r.y << 8) | (r.z << 16) | (r.w << 24));
        results.Store(4096 + 12, (uint)(values.SampleCmpLevel(compare, float2(0.375, 0.375), 5.0, 0) * 1 +
                                        values.SampleCmpLevel(compare, float2(0.375, 0.375), 6.0, 0) * 2 +
                                        values.SampleCmpGrad(compare, float2(0.625, 0.625), 10.5, float2(0, 0), float2(0, 0)) * 4 +
                                        values.SampleCmpGrad(compare, float2(0.625, 0.625), 9.5, float2(0, 0), float2(0, 0)) * 8));
        uint lo, hi;
        asuint(asdouble(0x12345678u, 0x40091EB8u) * 2.0, lo, hi);
        results.Store(4096 + 16, lo);
        results.Store(4096 + 20, hi);
        uint width, height, levels;
        values.GetDimensions(0, width, height, levels);
        results.Store(4096 + 24, width * 100 + height * 10 + levels);
    }
    if(lane % 2 == 1)
        odd.Append(lane);
}

// ---------------------------------------------------------------- dxilops: pixel
struct PSIn
{
    float4 pos : SV_Position;
    float2 uv : UV;
};

PSIn vs_quad(uint id : SV_VertexID)
{
    PSIn o;
    float2 corner = float2((id & 1) * 2, (id >> 1) * 2);       // 0..2: a triangle covering the 8 x 8 target
    o.pos = float4(corner.x * 2 - 1, 1 - corner.y * 2, 0.5, 1);
    o.uv = corner * 8;                                          // the pixel coordinates (centers at .5)
    return o;
}

float4 ps(PSIn i, uint coverage : SV_Coverage) : SV_Target
{
    RWByteAddressBuffer results = ResourceDescriptorHeap[2];
    uint2 p = (uint2)i.pos.xy;
    uint at = 8192 + (p.y * 8 + p.x) * 16;
    float2 snapped = EvaluateAttributeSnapped(i.uv, int2(0, 0));
    float2 atCentroid = EvaluateAttributeCentroid(i.uv);
    results.Store(at + 0, (uint)(snapped.x * 4) | ((uint)(snapped.y * 4) << 8) | ((uint)(atCentroid.x * 4) << 16) |
                          ((uint)(atCentroid.y * 4) << 24));
    results.Store(at + 4, GetRenderTargetSampleCount() * 100 + coverage);
    uint across = (uint)QuadReadAcrossX(i.pos.x) + (uint)QuadReadAcrossY(i.pos.y) * 16;
    results.Store(at + 8, across);
    results.Store(at + 12, (uint)(ddx_fine(i.uv.x) * 10) + (uint)(ddy_coarse(i.uv.y) * 10) * 100);
    return float4(i.uv / 8, 0, 1);
}
