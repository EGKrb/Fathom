// The sm69 mode of emulator_world (compiled by dxc, compile_shaders.cmd): shader model 6.9. Shader execution
// reordering (HitObject: a ray traced without its shaders, its hit read, then Invoke; a miss made by hand; a
// nop) on the quad of the dxr mode, and long vectors (loads and stores of 8 floats, dot, all / any).

RaytracingAccelerationStructure scene : register(t0);
RWStructuredBuffer<uint4> output : register(u0);
RWByteAddressBuffer vectors : register(u1);
cbuffer Local : register(b0, space1)
{
    uint tag;
};

struct [raypayload] Payload
{
    uint value : read(caller) : write(closesthit, miss, caller);
};

[shader("raygeneration")]
void RayGen()
{
    uint2 index = DispatchRaysIndex().xy;
    RayDesc ray;
    ray.Origin = float3((index.x + 0.5) / 16 * 2 - 1, 1 - (index.y + 0.5) / 8 * 2, -2);
    ray.Direction = float3(0, 0, 1);
    ray.TMin = 0.001;
    ray.TMax = 100;
    Payload payload;
    payload.value = 7;
    dx::HitObject hit = dx::HitObject::TraceRay(scene, RAY_FLAG_NONE, 0xFF, 0, 1, 0, ray, payload);
    uint4 info = uint4(hit.IsHit() ? 1 : (hit.IsMiss() ? 2 : 0), 0, 0, 0);
    if(hit.IsHit())
    {
        info.y = hit.GetInstanceID() * 100 + hit.GetPrimitiveIndex() * 10 + hit.GetGeometryIndex();
        info.z = hit.GetHitKind() + hit.GetShaderTableIndex() * 1000 + hit.LoadLocalRootTableConstant(0) * 10000;
        info.w = (uint)(hit.GetRayTCurrent() * 16 + 0.5);
    }
    dx::MaybeReorderThread(hit, index.x & 1, 1);
    dx::HitObject::Invoke(hit, payload);         // the closest hit or miss shader, as TraceRay would have
    uint at = (index.y * 16 + index.x) * 4;
    output[at] = info;
    output[at + 1] = uint4(payload.value, 0, 0, 0);
    // a miss made by hand, and a nop (Invoke does nothing)
    dx::HitObject made = index.x < 8 ? dx::HitObject::MakeMiss(RAY_FLAG_NONE, 0, ray) : dx::HitObject::MakeNop();
    payload.value = 5;
    dx::HitObject::Invoke(made, payload);
    output[at + 2] = uint4(payload.value, made.IsMiss() ? 1 : 0, made.IsNop() ? 1 : 0, 0);
}

[shader("closesthit")]
void Hit(inout Payload payload, in BuiltInTriangleIntersectionAttributes attributes)
{
    payload.value = 1000 + tag + InstanceID();
}

[shader("miss")]
void Miss(inout Payload payload)
{
    payload.value = 2000 + tag;
}

// ---------------------------------------------------------------- long vectors
[numthreads(1, 1, 1)]
void Vectors()
{
    vector<float, 8> a = vectors.Load< vector<float, 8> >(0);
    vector<float, 8> b = vectors.Load< vector<float, 8> >(32);
    vectors.Store(64, dot(a, b));
    vectors.Store< vector<float, 8> >(96, a * 2 + b);
    vector<uint, 8> u = vectors.Load< vector<uint, 8> >(160);
    vectors.Store(68, all(u != 0) ? 1u : 0u);
    vectors.Store(72, any(u == 5) ? 1u : 0u);
}
