// The graph mode of emulator_world (compiled by dxc, compile_shaders.cmd): a work graph. The entry node
// (broadcasting, a grid of 2 groups of 4 threads, a record from the CPU) gives each thread a record for the Item
// node (thread launch) and each group a record for one node of the Bucket array (coalescing, up to 4 records per
// group); the nodes write what they get in a buffer.

RWByteAddressBuffer output : register(u0);

struct EntryRecord
{
    uint base;
};

struct Item
{
    uint value;
    uint thread;
};

struct Bucket
{
    uint group;
    uint sum;
};

[Shader("node")]
[NodeIsProgramEntry]
[NodeLaunch("broadcasting")]
[NodeDispatchGrid(2, 1, 1)]
[NumThreads(4, 1, 1)]
void Entry(DispatchNodeInputRecord<EntryRecord> input, uint3 group : SV_GroupID, uint thread : SV_GroupIndex,
           [MaxRecords(4)] NodeOutput<Item> Items, [MaxRecords(1)] [NodeArraySize(2)] NodeOutputArray<Bucket> Buckets)
{
    ThreadNodeOutputRecords<Item> item = Items.GetThreadNodeOutputRecords(1);
    item.Get().value = input.Get().base + group.x * 4 + thread;
    item.Get().thread = thread;
    item.OutputComplete();
    GroupNodeOutputRecords<Bucket> bucket = Buckets[group.x % 2].GetGroupNodeOutputRecords(1);
    bucket.Get().group = group.x;
    bucket.Get().sum = WaveActiveSum(thread) + input.Get().base;
    bucket.OutputComplete();
}

[Shader("node")]
[NodeLaunch("thread")]
void Items(ThreadNodeInputRecord<Item> item)
{
    output.Store(item.Get().value * 4 - 400, item.Get().value * 10 + item.Get().thread);
    output.InterlockedAdd(64, 1);
}

[Shader("node")]
[NodeLaunch("coalescing")]
[NodeID("Buckets", 0)]
[NumThreads(2, 1, 1)]
void Bucket0([MaxRecords(4)] GroupNodeInputRecords<Bucket> buckets, uint thread : SV_GroupIndex)
{
    if(thread < buckets.Count())
        output.Store(80 + buckets[thread].group * 4, buckets[thread].sum + 1000);
}

[Shader("node")]
[NodeLaunch("coalescing")]
[NodeID("Buckets", 1)]
[NumThreads(2, 1, 1)]
void Bucket1([MaxRecords(4)] GroupNodeInputRecords<Bucket> buckets, uint thread : SV_GroupIndex)
{
    if(thread < buckets.Count())
        output.Store(80 + buckets[thread].group * 4, buckets[thread].sum + 2000);
}
