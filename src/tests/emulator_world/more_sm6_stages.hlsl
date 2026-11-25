// The dxilgs mode of emulator_world (compiled by dxc, compile_shaders.cmd): the hull, domain and geometry
// stages of shader model 6, their output captured by stream output.

struct VSOut
{
    float4 pos : POS;
};

VSOut vs(float4 pos : POSITION)
{
    VSOut o;
    o.pos = pos;
    return o;
}

struct HSPatch
{
    float edges[3] : SV_TessFactor;
    float inside : SV_InsideTessFactor;
    uint id : PATCHID;
};

HSPatch patch(InputPatch<VSOut, 3> points, uint id : SV_PrimitiveID)
{
    HSPatch o;
    o.edges[0] = 2;
    o.edges[1] = 3;
    o.edges[2] = 4;
    o.inside = 3;
    o.id = id + 100 + (uint)points[2].pos.x;
    return o;
}

[domain("tri")]
[partitioning("integer")]
[outputtopology("triangle_cw")]
[outputcontrolpoints(3)]
[patchconstantfunc("patch")]
VSOut hs(InputPatch<VSOut, 3> points, uint i : SV_OutputControlPointID)
{
    VSOut o;
    o.pos = points[i].pos * 2;
    return o;
}

struct DSOut
{
    float4 pos : SV_Position;
    float3 uvw : UVW;
    uint id : PATCHID;
};

[domain("tri")]
DSOut ds(HSPatch constants, float3 uvw : SV_DomainLocation, const OutputPatch<VSOut, 3> points)
{
    DSOut o;
    o.pos = points[0].pos * uvw.x + points[1].pos * uvw.y + points[2].pos * uvw.z;
    o.uvw = uvw;
    o.id = constants.id;
    return o;
}

struct GSOut
{
    float4 pos : SV_Position;
    float4 data : DATA;
};

[maxvertexcount(3)]
[instance(2)]
void gs(triangle DSOut t[3], uint prim : SV_PrimitiveID, uint instance : SV_GSInstanceID, inout PointStream<GSOut> stream)
{
    for(uint i = 0; i < 3; i++)
    {
        GSOut o;
        o.pos = t[i].pos;
        o.data = float4(t[i].uvw.x, t[i].uvw.y, (float)t[i].id, (float)(instance * 1000 + prim));
        stream.Append(o);
        stream.RestartStrip();
    }
}
