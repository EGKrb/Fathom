// The shaders of the bc, deriv, tess, so and deferred modes of emulator_world (graphics_more2.inc), compiled
// by compile_shaders.cmd into more_*.h with the fxc of the Windows SDK.

// ---------------------------------------------------------------- bc: block-compressed texels, read by Load
Texture2D<float4> blocks : register(t0);
RWStructuredBuffer<float4> texels : register(u0);

[numthreads(8, 4, 1)]
void cs_load(uint3 id : SV_DispatchThreadID)
{
    texels[id.y * 8 + id.x] = blocks.Load(int3(id.xy, 0));
}

// ---------------------------------------------------------------- deriv: derivatives and mip levels
Texture2D<float4> mipmapped : register(t0);
SamplerState point_mips : register(s0);

struct Screen { float4 position : SV_Position; float2 uv : TEXCOORD; };

Screen vs_screen(uint id : SV_VertexID)
{
    // a triangle covering the target, uv 0..1 over it
    Screen output;
    float2 uv = float2((id << 1) & 2, id & 2);
    output.position = float4(uv * float2(2, -2) + float2(-1, 1), 0.5, 1);
    output.uv = uv;
    return output;
}

struct Derived
{
    float4 values : SV_Target0;     // ddx_fine, ddy_coarse of a linear function, ddx(uv.x) * 64, the mip level
    float4 sampled : SV_Target1;    // Sample: the level of the derivatives
    float4 biased : SV_Target2;     // SampleBias -1
    float4 graded : SV_Target3;     // SampleGrad, 4 times the derivatives
};

Derived ps_deriv(Screen input)
{
    Derived output;
    float f = input.position.x * 0.25 + input.position.y * 0.5;
    output.values = float4(ddx_fine(f), ddy_coarse(f), ddx(input.uv.x) * 64, mipmapped.CalculateLevelOfDetail(point_mips, input.uv));
    output.sampled = mipmapped.Sample(point_mips, input.uv);
    output.biased = mipmapped.SampleBias(point_mips, input.uv, -1);
    output.graded = mipmapped.SampleGrad(point_mips, input.uv, ddx(input.uv) * 4, ddy(input.uv) * 4);
    return output;
}

// ---------------------------------------------------------------- tess: a triangle patch with a factor per edge
cbuffer TessFactors : register(b0)
{
    float4 edge_factors;    // edges u == 0, v == 0, w == 0, inside
};

struct PatchPoint { float4 position : POSITION; };
struct TriFactors { float edges[3] : SV_TessFactor; float inside : SV_InsideTessFactor; };
struct Domain { float4 position : SV_Position; float3 location : TEXCOORD; };

PatchPoint vs_patch_point(float2 position : POSITION)
{
    PatchPoint output;
    output.position = float4(position, 0.5, 1);
    return output;
}

TriFactors hs_edges(InputPatch<PatchPoint, 3> patch)
{
    TriFactors factors;
    factors.edges[0] = edge_factors.x;
    factors.edges[1] = edge_factors.y;
    factors.edges[2] = edge_factors.z;
    factors.inside = edge_factors.w;
    return factors;
}

[domain("tri")]
[partitioning("integer")]
[outputtopology("triangle_cw")]
[outputcontrolpoints(3)]
[patchconstantfunc("hs_edges")]
PatchPoint hs_integer(InputPatch<PatchPoint, 3> patch, uint id : SV_OutputControlPointID)
{
    return patch[id];
}

[domain("tri")]
[partitioning("fractional_odd")]
[outputtopology("triangle_cw")]
[outputcontrolpoints(3)]
[patchconstantfunc("hs_edges")]
PatchPoint hs_odd(InputPatch<PatchPoint, 3> patch, uint id : SV_OutputControlPointID)
{
    return patch[id];
}

[domain("tri")]
Domain ds_edges(TriFactors factors, float3 location : SV_DomainLocation, const OutputPatch<PatchPoint, 3> patch)
{
    Domain output;
    output.position = location.x * patch[0].position + location.y * patch[1].position + location.z * patch[2].position;
    output.location = location;
    return output;
}

// ---------------------------------------------------------------- so: a geometry shader writing triangles out
struct Seed { float4 position : POSITION; float4 color : COLOR; };

Seed vs_seed(float2 position : POSITION, float4 color : COLOR)
{
    Seed output;
    output.position = float4(position, 0.5, 1);
    output.color = color;
    return output;
}

[maxvertexcount(3)]
void gs_triangle(point Seed input[1], inout TriangleStream<Seed> stream)
{
    // a triangle around each point, its corners clockwise on the screen
    const float2 corners[3] = { float2(-0.2, -0.2), float2(0, 0.2), float2(0.2, -0.2) };
    for (int i = 0; i < 3; i++)
    {
        Seed output;
        output.position = input[0].position + float4(corners[i], 0, 0);
        output.color = input[0].color;
        stream.Append(output);
    }
}

struct Colored { float4 position : SV_Position; float4 color : COLOR; };

Colored vs_streamed(float4 position : POSITION, float4 color : COLOR)
{
    Colored output;
    output.position = position;
    output.color = color;
    return output;
}

// ---------------------------------------------------------------- deferred: a triangle in a constant color
cbuffer Paint : register(b0)
{
    float4 paint;
};

float4 vs_plain(float2 position : POSITION) : SV_Position
{
    return float4(position, 0.5, 1);
}

float4 ps_paint(float4 position : SV_Position) : SV_Target
{
    return paint;
}
