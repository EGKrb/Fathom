// The dxr2 mode of emulator_world (compiled by dxc, compile_shaders.cmd): ray tracing with procedural geometry
// (an AABB with a sphere: intersection shader, ReportHit, any hit shader), local root signatures (a root
// constant per shader record), subobjects declared in the library, and inline ray tracing (RayQuery).

RaytracingAccelerationStructure scene : register(t0);
RWStructuredBuffer<uint4> output : register(u0);
cbuffer Local : register(b0, space1)
{
    uint tag;       // the local root constant of the shader record
};

// the subobjects of the library: the hit groups and configurations (the local root signature is the API's)
TriangleHitGroup QuadGroup = { "", "QuadHit" };
ProceduralPrimitiveHitGroup SphereGroup = { "SphereAnyHit", "SphereHit", "SphereIntersect" };
RaytracingShaderConfig ShaderConfig = { 32, 16 };
RaytracingPipelineConfig PipelineConfig = { 1 };

struct Payload
{
    uint4 hit;          // tag, InstanceID, PrimitiveIndex, HitKind
    float4 surface;     // RayTCurrent, normal or barycentrics
};

struct Sphere
{
    float3 normal;
    float side;
};

static RayDesc GridRay(uint2 index, uint2 size)
{
    RayDesc ray;
    ray.Origin = float3((index.x + 0.5) / size.x * 2 - 1, 1 - (index.y + 0.5) / size.y * 2, -2);
    ray.Direction = float3(0, 0, 1);
    ray.TMin = 0.001;
    ray.TMax = 100;
    return ray;
}

[shader("raygeneration")]
void RayGen()
{
    uint2 index = DispatchRaysIndex().xy;
    uint2 size = DispatchRaysDimensions().xy;
    Payload payload;
    payload.hit = uint4(0, 0, 0, 0);
    payload.surface = float4(0, 0, 0, 0);
    TraceRay(scene, RAY_FLAG_NONE, 0xFF, 0, 1, 0, GridRay(index, size), payload);
    uint at = (index.y * size.x + index.x) * 4;
    output[at] = payload.hit;
    output[at + 1] = asuint(payload.surface);
}

[shader("closesthit")]
void QuadHit(inout Payload payload, in BuiltInTriangleIntersectionAttributes attributes)
{
    payload.hit = uint4(tag, InstanceID(), PrimitiveIndex(), HitKind());
    payload.surface = float4(RayTCurrent(), attributes.barycentrics, 0);
}

// a sphere of radius 0.5 at the center of the AABB, the two roots reported (the far one when the near one is ignored)
[shader("intersection")]
void SphereIntersect()
{
    float3 o = ObjectRayOrigin(), d = ObjectRayDirection();
    float b = dot(o, d), c = dot(o, o) - 0.25, a = dot(d, d);
    float disc = b * b - a * c;
    if(disc < 0)
        return;
    float root = sqrt(disc);
    Sphere s;
    float t0 = (-b - root) / a;
    s.normal = (o + t0 * d) * 2;
    s.side = 0;
    ReportHit(t0, 1, s);
    float t1 = (-b + root) / a;
    s.normal = (o + t1 * d) * 2;
    s.side = 1;
    ReportHit(t1, 2, s);
}

// the hits on the lower part of the sphere are ignored
[shader("anyhit")]
void SphereAnyHit(inout Payload payload, in Sphere s)
{
    if(s.normal.y < -0.5)
        IgnoreHit();
}

[shader("closesthit")]
void SphereHit(inout Payload payload, in Sphere s)
{
    payload.hit = uint4(tag, InstanceID(), PrimitiveIndex(), HitKind() + (uint)s.side * 16);
    payload.surface = float4(RayTCurrent(), s.normal);
}

[shader("miss")]
void Miss(inout Payload payload)
{
    payload.hit = uint4(tag, 0xFFFF, 0, 0);
    payload.surface = float4(RayTCurrent(), 0, 0, 0);
}

// ---------------------------------------------------------------- inline ray tracing, the same rays
[numthreads(16, 8, 1)]
void QueryRays(uint2 index : SV_DispatchThreadID)
{
    RayQuery<RAY_FLAG_NONE> query;
    query.TraceRayInline(scene, RAY_FLAG_NONE, 0xFF, GridRay(index, uint2(16, 8)));
    float3 normal = float3(0, 0, 0);
    while(query.Proceed())
    {
        if(query.CandidateType() == CANDIDATE_PROCEDURAL_PRIMITIVE)
        {
            float3 o = query.CandidateObjectRayOrigin(), d = query.CandidateObjectRayDirection();
            float b = dot(o, d), c = dot(o, o) - 0.25, a = dot(d, d);
            float disc = b * b - a * c;
            if(disc >= 0)
            {
                float root = sqrt(disc);
                for(int k = 0; k < 2; k++)      // the near root, else the far one, as the any hit shader decides
                {
                    float t = (-b + (k ? root : -root)) / a;
                    float3 n = (o + t * d) * 2;
                    if(n.y >= -0.5 && t >= query.RayTMin() && t <= query.CommittedRayT())
                    {
                        query.CommitProceduralPrimitiveHit(t);
                        normal = n;
                        break;
                    }
                }
            }
        }
        else
            query.CommitNonOpaqueTriangleHit();
    }
    uint at = 512 + (index.y * 16 + index.x) * 4;
    uint status = query.CommittedStatus();
    if(status == COMMITTED_TRIANGLE_HIT)
    {
        output[at] = uint4(1, query.CommittedInstanceID(), query.CommittedPrimitiveIndex(), query.CommittedTriangleFrontFace() ? 1 : 0);
        output[at + 1] = asuint(float4(query.CommittedRayT(), query.CommittedTriangleBarycentrics(), 0));
    }
    else if(status == COMMITTED_PROCEDURAL_PRIMITIVE_HIT)
    {
        output[at] = uint4(2, query.CommittedInstanceID(), query.CommittedPrimitiveIndex(), query.CommittedGeometryIndex());
        output[at + 1] = asuint(float4(query.CommittedRayT(), normal));
    }
    else
    {
        output[at] = uint4(0, 0xFFFF, 0, 0);
        output[at + 1] = uint4(0, 0, 0, 0);
    }
}
