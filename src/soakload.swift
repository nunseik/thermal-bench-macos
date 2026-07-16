// soakload — sustained CPU + GPU compute load with per-second throughput reporting.
//
// Emits one line per second to stdout:
//   SAMPLE epoch=<unix> elapsed=<s> cpu_gflops=<f> gpu_gflops=<f>
//
// Throughput (GFLOP/s) is the real performance signal: when the SoC thermal
// throttles, clocks drop and these numbers fall. Comparing the sustained
// (steady-state) value baseline-vs-modded is the headline result.
//
// Build:  swiftc -O -o soakload src/soakload.swift -framework Metal
// Usage:  ./soakload --mode both|cpu|gpu --duration <seconds>

import Foundation
import Metal

// ---- args ----------------------------------------------------------------
var mode = "both"
var duration = 1200.0
do {
    var it = CommandLine.arguments.dropFirst().makeIterator()
    while let a = it.next() {
        switch a {
        case "--mode":     if let v = it.next() { mode = v }
        case "--duration": if let v = it.next(), let d = Double(v) { duration = d }
        default: FileHandle.standardError.write("ignoring arg: \(a)\n".data(using: .utf8)!)
        }
    }
}
let runCPU = (mode == "both" || mode == "cpu")
let runGPU = (mode == "both" || mode == "gpu")

// ---- shared counters (flops completed) ------------------------------------
let ncpu = ProcessInfo.processInfo.activeProcessorCount
let cpuCounters = UnsafeMutablePointer<UInt64>.allocate(capacity: ncpu)
cpuCounters.initialize(repeating: 0, count: ncpu)
var gpuFlops: UInt64 = 0            // written by GPU thread, read by reporter
let gpuLock = NSLock()

var stop = false
signal(SIGINT)  { _ in stop = true }
signal(SIGTERM) { _ in stop = true }

// Optimization barrier: an @inline(never) sink the compiler must assume has
// side effects, so it cannot fold, hoist, or eliminate the FP recurrences.
var blackholeGlobal: Double = 0
@inline(never) func blackhole(_ x: Double) { blackholeGlobal = x }

// ---- CPU workers ----------------------------------------------------------
// Each worker hammers 4 independent FMA chains to fill the FP pipeline.
// FLOPS_PER_ITER = 4 chains * 2 flops (mul+add) = 8.
let cpuThreads: [Thread] = runCPU ? (0..<ncpu).map { idx in
    let t = Thread {
        // 8 independent FMA chains → fills the FP pipeline (ILP), each iter =
        // 8 chains * 2 flops = 16 flops. Rescale factors keep values bounded
        // without a data-dependent branch in the hot path.
        var a0 = 0.5 + Double(idx) * 1e-3, a1 = 0.25, a2 = 0.75, a3 = 0.125
        var a4 = 0.6, a5 = 0.35, a6 = 0.85, a7 = 0.15
        let c: [Double] = [1.0000000001, 0.9999999999, 1.0000000002, 0.9999999998,
                           1.0000000003, 0.9999999997, 1.0000000004, 0.9999999996]
        let BATCH: UInt64 = 1_000_000
        while !stop {
            for _ in 0..<BATCH {
                a0 = a0 * c[0] + 0.1; a1 = a1 * c[1] + 0.1
                a2 = a2 * c[2] + 0.1; a3 = a3 * c[3] + 0.1
                a4 = a4 * c[4] + 0.1; a5 = a5 * c[5] + 0.1
                a6 = a6 * c[6] + 0.1; a7 = a7 * c[7] + 0.1
            }
            // consume all chains through the barrier so none can be eliminated
            blackhole(a0 + a1 + a2 + a3 + a4 + a5 + a6 + a7)
            // keep values in a sane range (rare, out of hot loop)
            if a0 > 1e6 { a0*=1e-6; a1*=1e-6; a2*=1e-6; a3*=1e-6; a4*=1e-6; a5*=1e-6; a6*=1e-6; a7*=1e-6 }
            cpuCounters[idx] &+= BATCH &* 16
        }
    }
    t.stackSize = 1 << 20
    return t
} : []
cpuThreads.forEach { $0.start() }

// ---- GPU worker -----------------------------------------------------------
var gpuThread: Thread? = nil
if runGPU {
    guard let dev = MTLCreateSystemDefaultDevice() else {
        FileHandle.standardError.write("no Metal device; cannot run GPU load\n".data(using: .utf8)!)
        exit(2)
    }
    let src = """
    #include <metal_stdlib>
    using namespace metal;
    kernel void soak(device float* buf [[buffer(0)]],
                     constant uint& loops [[buffer(1)]],
                     uint gid [[thread_position_in_grid]]) {
        // 8 independent FMA chains per thread → throughput-bound, no branches.
        float x0 = buf[gid],        x1 = buf[gid] + 0.1f;
        float x2 = fract(x0*3.1f),  x3 = fract(x0*7.3f);
        float x4 = fract(x0*1.7f),  x5 = fract(x0*5.9f);
        float x6 = fract(x0*2.3f),  x7 = fract(x0*4.1f);
        for (uint i = 0; i < loops; i++) {
            x0 = fma(x0, 0.9999999f, 0.1f); x1 = fma(x1, 0.9999998f, 0.1f);
            x2 = fma(x2, 0.9999997f, 0.1f); x3 = fma(x3, 0.9999996f, 0.1f);
            x4 = fma(x4, 0.9999995f, 0.1f); x5 = fma(x5, 0.9999994f, 0.1f);
            x6 = fma(x6, 0.9999993f, 0.1f); x7 = fma(x7, 0.9999992f, 0.1f);
        }
        buf[gid] = x0+x1+x2+x3+x4+x5+x6+x7;
    }
    """
    let lib = try! dev.makeLibrary(source: src, options: nil)
    let fn = lib.makeFunction(name: "soak")!
    let pipe = try! dev.makeComputePipelineState(function: fn)
    let queue = dev.makeCommandQueue()!

    let gridN = 1 << 20                 // 1,048,576 threads
    var loops: UInt32 = 8192            // inner iterations per thread
    let flopsPerThread = UInt64(loops) * 16   // 8 fma chains * 2 flops
    let flopsPerDispatch = UInt64(gridN) * flopsPerThread

    let buf = dev.makeBuffer(length: gridN * MemoryLayout<Float>.size,
                             options: .storageModeShared)!
    let loopBuf = dev.makeBuffer(bytes: &loops, length: MemoryLayout<UInt32>.size,
                                 options: .storageModeShared)!

    let tw = min(pipe.maxTotalThreadsPerThreadgroup, 256)
    let tg = MTLSize(width: tw, height: 1, depth: 1)
    let grid = MTLSize(width: gridN, height: 1, depth: 1)

    gpuThread = Thread {
        while !stop {
            guard let cb = queue.makeCommandBuffer(),
                  let enc = cb.makeComputeCommandEncoder() else { break }
            enc.setComputePipelineState(pipe)
            enc.setBuffer(buf, offset: 0, index: 0)
            enc.setBuffer(loopBuf, offset: 0, index: 1)
            enc.dispatchThreads(grid, threadsPerThreadgroup: tg)
            enc.endEncoding()
            cb.commit()
            cb.waitUntilCompleted()
            gpuLock.lock(); gpuFlops &+= flopsPerDispatch; gpuLock.unlock()
        }
    }
    gpuThread!.start()
}

// ---- reporter -------------------------------------------------------------
func nowEpoch() -> Double { Date().timeIntervalSince1970 }
let start = nowEpoch()
var lastCPU: UInt64 = 0
var lastGPU: UInt64 = 0
var lastT = start

// warm-up print so the collector sees the stream immediately
FileHandle.standardError.write("soakload: mode=\(mode) duration=\(Int(duration))s cores=\(ncpu)\n".data(using: .utf8)!)

while !stop {
    Thread.sleep(forTimeInterval: 1.0)
    let t = nowEpoch()
    let dt = t - lastT
    lastT = t

    var cpuTotal: UInt64 = 0
    for i in 0..<ncpu { cpuTotal &+= cpuCounters[i] }
    gpuLock.lock(); let gpuTotal = gpuFlops; gpuLock.unlock()

    let cpuGflops = Double(cpuTotal &- lastCPU) / dt / 1e9
    let gpuGflops = Double(gpuTotal &- lastGPU) / dt / 1e9
    lastCPU = cpuTotal
    lastGPU = gpuTotal

    let line = String(format: "SAMPLE epoch=%.3f elapsed=%.1f cpu_gflops=%.2f gpu_gflops=%.2f\n",
                      t, t - start, cpuGflops, gpuGflops)
    FileHandle.standardOutput.write(line.data(using: .utf8)!)

    if t - start >= duration { stop = true }
}
FileHandle.standardError.write("soakload: done\n".data(using: .utf8)!)
