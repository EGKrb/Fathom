// The dxr mode of emulator_world: a ray tracing library (lib_6_3, dxc) for DispatchRays. Rays along +z from a
// 16 x 8 grid against three instances of a quad (two triangles): a nearest hit, the facing of an instance
// whose triangles are counterclockwise, an any hit shader ignoring a primitive of a non opaque instance.
// Compiled by compile_shaders.cmd into more_dxr.h.

RaytracingAccelerationStructure scene : register(t0);
RWStructuredBuffer<uint4> output : register(u0);

struct Payload
{
    uint4 hit;
    uint4 object;
};

[shader("raygeneration")]
void RayGen()
{
    uint2 index = DispatchRaysIndex().xy;
    uint2 size = DispatchRaysDimensions().xy;
    RayDesc ray;
    ray.Origin = float3((index.x + 0.5) / size.x * 2 - 1, 1 - (index.y + 0.5) / size.y * 2, -2);
    ray.Direction = float3(0, 0, 1);
    ray.TMin = 0.001;
    ray.TMax = 100;
    Payload payload;
    payload.hit = uint4(0, 0, 0, 0);
    payload.object = uint4(0, 0, 0, 0);
    TraceRay(scene, RAY_FLAG_NONE, 0xFF, 0, 1, 0, ray, payload);
    uint at = (index.y * size.x + index.x) * 2;
    output[at] = payload.hit;
    output[at + 1] = payload.object;
}

[shader("closesthit")]
void Hit(inout Payload payload, in BuiltInTriangleIntersectionAttributes attributes)
{
    float2 b = attributes.barycentrics;
    payload.hit = uint4(InstanceID() + 1, PrimitiveIndex() | (HitKind() << 8) | (InstanceIndex() << 16),
                        (uint)(RayTCurrent() * 16 + 0.5), (uint)(b.x * 256 + 0.5) | ((uint)(b.y * 256 + 0.5) << 16));
    payload.object = uint4((uint)(int)floor(ObjectRayOrigin().x * 64 + 0.5) + 1000, (uint)WorldRayDirection().z,
                           (uint)(int)floor(ObjectToWorld3x4()[0][3] * 64 + 0.5) + 1000,
                           (uint)(WorldToObject3x4()[0][0] * 64 + 0.5));
}

[shader("anyhit")]
void AnyHit(inout Payload payload, in BuiltInTriangleIntersectionAttributes attributes)
{
    if(PrimitiveIndex() == 1)
        IgnoreHit();
}

[shader("miss")]
void Miss(inout Payload payload)
{
    payload.hit = uint4(0, 0xFFFF, (uint)(RayTCurrent() + 0.5), 0);
}
