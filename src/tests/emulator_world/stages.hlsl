// The shaders of the d3d9sh, d3d11gs and d3d11cs modes of emulator_world (target.cpp), compiled by
// compile_shaders.cmd into stages_*.h with the fxc of the Windows SDK.

// ---------------------------------------------------------------- Direct3D 9 (vs_2_0 / ps_2_0)
float4x4 transform : register(c0);
float4 tint9 : register(c4);
sampler2D picture9 : register(s0);

struct Vertex9 { float4 position : POSITION; float4 color : COLOR0; float2 uv : TEXCOORD0; };

Vertex9 vs9(Vertex9 input)
{
    Vertex9 output;
    output.position = mul(input.position, transform);
    output.color = input.color * tint9;
    output.uv = input.uv;
    return output;
}

float4 ps9(float4 color : COLOR0, float2 uv : TEXCOORD0) : COLOR
{
    return color * tex2D(picture9, uv);
}

// ---------------------------------------------------------------- geometry shader: points -> squares
struct PointIn { float2 position : POSITION; float4 color : COLOR; };
struct PointOut { float4 position : SV_Position; float4 color : COLOR; };

PointOut vs_point(PointIn input)
{
    PointOut output;
    output.position = float4(input.position, 0.5, 1);
    output.color = input.color;
    return output;
}

[maxvertexcount(4)]
void gs_square(point PointOut input[1], inout TriangleStream<PointOut> stream)
{
    for (int i = 0; i < 4; i++)
    {
        PointOut output;
        output.position = input[0].position + float4((i & 1) ? 0.1 : -0.1, (i & 2) ? -0.1 : 0.1, 0, 0);
        output.color = input[0].color;
        stream.Append(output);
    }
    stream.RestartStrip();
}

float4 ps_color(PointOut input) : SV_Target
{
    return input.color;
}

// ---------------------------------------------------------------- tessellation: a triangle patch
struct Patch { float4 position : POSITION; };
struct Factors { float edges[3] : SV_TessFactor; float inside : SV_InsideTessFactor; };

Patch vs_patch(float2 position : POSITION)
{
    Patch output;
    output.position = float4(position, 0.5, 1);
    return output;
}

Factors hs_factors(InputPatch<Patch, 3> patch)
{
    Factors factors;
    factors.edges[0] = factors.edges[1] = factors.edges[2] = 3;
    factors.inside = 3;
    return factors;
}

[domain("tri")]
[partitioning("integer")]
[outputtopology("triangle_cw")]
[outputcontrolpoints(3)]
[patchconstantfunc("hs_factors")]
Patch hs_patch(InputPatch<Patch, 3> patch, uint id : SV_OutputControlPointID)
{
    return patch[id];
}

[domain("tri")]
PointOut ds_patch(Factors factors, float3 location : SV_DomainLocation, const OutputPatch<Patch, 3> patch)
{
    PointOut output;
    output.position = location.x * patch[0].position + location.y * patch[1].position + location.z * patch[2].position;
    output.color = float4(location, 1);
    return output;
}

// ---------------------------------------------------------------- compute: a gradient and a histogram
RWTexture2D<float4> gradient : register(u0);
RWByteAddressBuffer histogram : register(u1);
groupshared uint row_total[8];

[numthreads(8, 8, 1)]
void cs_fill(uint3 id : SV_DispatchThreadID, uint3 local : SV_GroupThreadID)
{
    gradient[id.xy] = float4(id.x / 15.0, id.y / 15.0, 0.5, 1);
    if (local.x == 0)
        row_total[local.y] = 0;
    GroupMemoryBarrierWithGroupSync();
    InterlockedAdd(row_total[local.y], id.x);
    GroupMemoryBarrierWithGroupSync();
    if (local.x == 0)
        histogram.InterlockedAdd(4 * (id.y % 4), row_total[local.y]);
}
