// The mesh6 mode of emulator_world (compiled by dxc, compile_shaders.cmd): an amplification shader keeps the
// tiles whose number is not 2 modulo 3 (compacted in its payload with wave operations), a mesh shader draws each
// as a square of two triangles with per-primitive attributes (SV_PrimitiveID, an id, SV_CullPrimitive), and
// the pixel shader colors them.

struct Payload
{
    uint tiles[4];
};

groupshared Payload payload;

[numthreads(4, 1, 1)]
void as(uint group : SV_GroupID, uint thread : SV_GroupIndex)
{
    uint tile = group * 4 + thread;
    bool visible = tile % 3 != 2;
    if(visible)
        payload.tiles[WavePrefixCountBits(visible)] = tile;
    DispatchMesh(WaveActiveCountBits(visible), 1, 1, payload);
}

struct Vertex
{
    float4 position : SV_Position;
    float2 uv : UV;
};

struct Primitive
{
    uint primitive : SV_PrimitiveID;
    uint id : ID;
    bool cull : SV_CullPrimitive;
};

[numthreads(4, 1, 1)]
[outputtopology("triangle")]
void ms(uint group : SV_GroupID, uint thread : SV_GroupIndex, in payload Payload input,
        out vertices Vertex corners[4], out indices uint3 triangles[2], out primitives Primitive faces[2])
{
    uint tile = input.tiles[group];
    SetMeshOutputCounts(4, 2);
    float2 corner = float2((tile % 4) * 8 + (thread & 1) * 8, (tile / 4) * 8 + (thread >> 1) * 8);   // pixels
    corners[thread].position = float4(corner.x / 16 - 1, 1 - corner.y / 16, 0.5, 1);
    corners[thread].uv = float2(thread & 1, thread >> 1);
    if(thread < 2)
    {
        triangles[thread] = thread == 0 ? uint3(0, 1, 2) : uint3(1, 3, 2);
        faces[thread].primitive = thread;
        faces[thread].id = tile * 2 + thread;
        faces[thread].cull = tile == 4 && thread == 1;
    }
}

float4 ps(Vertex v, uint id : ID, uint primitive : SV_PrimitiveID) : SV_Target
{
    return float4(id / 16.0, primitive, v.uv.x, 1);
}
