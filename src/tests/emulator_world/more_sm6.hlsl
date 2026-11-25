// The compute shader of the dxil mode of emulator_world (graphics_more3.inc), compiled by compile_shaders.cmd with
// the dxc of the Windows SDK (shader model 6) into more_sm6_cs.h; the scene's shaders are scene.hlsl in 6.0.
RWStructuredBuffer<float> sums : register(u0);
RWStructuredBuffer<uint> waves : register(u1);
RWByteAddressBuffer counter : register(u2);
groupshared float tile[64];

[numthreads(64, 1, 1)]
void cs(uint3 id : SV_DispatchThreadID, uint local : SV_GroupIndex)
{
    tile[local] = (float)id.x;
    GroupMemoryBarrierWithGroupSync();
    float sum = 0;
    for (uint k = 0; k < 4; k++)
        sum += tile[(local + k) % 64];
    sums[id.x] = sum;
    uint lanes = WaveActiveSum(1u);
    uint before = WavePrefixSum(1u);
    waves[id.x] = (lanes == WaveGetLaneCount() && before == WaveGetLaneIndex()) ? 1u : 0u;
    counter.InterlockedAdd(0, 1);
}
