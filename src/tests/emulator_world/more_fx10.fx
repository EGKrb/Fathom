// The effect of the fx10 mode of emulator_world (graphics_more3.inc), compiled by compile_shaders.cmd into
// more_fx10.h (fxc /T fx_4_0).
cbuffer Frame
{
    float4x4 transform;
    float4 tint;
};

cbuffer Extra
{
    float scale = 1.0;
    float4 offsets[2] = { float4(0, 0, 0, 0), float4(0, 0, 0, 0) };
};

Texture2D picture;

SamplerState pointWrap
{
    Filter = MIN_MAG_MIP_POINT;
    AddressU = Wrap;
    AddressV = Wrap;
};

BlendState additive
{
    BlendEnable[0] = TRUE;
    SrcBlend = ONE;
    DestBlend = ONE;
    BlendOp = ADD;
};

RasterizerState noCull
{
    CullMode = NONE;
};

struct VIn { float2 position : POSITION; float2 uv : TEXCOORD; };
struct VOut { float4 position : SV_Position; float2 uv : TEXCOORD; };

VOut vs(VIn input)
{
    VOut output;
    output.position = mul(float4(input.position * scale, 0.5, 1), transform) + offsets[1];
    output.uv = input.uv;
    return output;
}

float4 ps(VOut input) : SV_Target
{
    return picture.Sample(pointWrap, input.uv) * tint;
}

VertexShader sharedVs = CompileShader(vs_4_0, vs());

technique10 Draw
{
    pass Plain
    {
        SetVertexShader(CompileShader(vs_4_0, vs()));
        SetGeometryShader(NULL);
        SetPixelShader(CompileShader(ps_4_0, ps()));
        SetRasterizerState(noCull);
    }
    pass Add
    {
        SetVertexShader(sharedVs);
        SetGeometryShader(NULL);
        SetPixelShader(CompileShader(ps_4_0, ps()));
        SetBlendState(additive, float4(0, 0, 0, 0), 0xFFFFFFFF);
        SetRasterizerState(noCull);
    }
}
