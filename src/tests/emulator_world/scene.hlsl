// The shaders of the d3d11 mode of emulator_world (target.cpp): a colored and textured scene. scene_vs.h
// and scene_ps.h are compiled from it with the fxc of the Windows SDK:
//   fxc /nologo /T vs_4_0 /E vs /Vn SceneVertexShader /Fh scene_vs.h scene.hlsl
//   fxc /nologo /T ps_4_0 /E ps /Vn ScenePixelShader /Fh scene_ps.h scene.hlsl
cbuffer Tint : register(b0)
{
    float4 tint;
};
Texture2D picture : register(t0);
SamplerState nearest : register(s0);

struct VertexIn
{
    float2 position : POSITION;
    float4 color : COLOR;
    float2 uv : TEXCOORD;
};

struct VertexOut
{
    float4 position : SV_Position;
    float4 color : COLOR;
    float2 uv : TEXCOORD;
};

VertexOut vs(VertexIn input)
{
    VertexOut output;
    output.position = float4(input.position, 0.5, 1);
    output.color = input.color * tint;
    output.uv = input.uv;
    return output;
}

float4 ps(VertexOut input) : SV_Target
{
    return input.color * picture.Sample(nearest, input.uv);
}
