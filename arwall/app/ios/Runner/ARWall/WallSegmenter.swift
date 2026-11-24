//  WallSegmenter.swift
//  On-device semantic wall mask for every ARKit camera frame.
//
//  Model: SegFormer-B0 fine-tuned on ADE20K + in-house room photos, exported to
//  Core ML with a 1-channel head = P(wall) after suppressing door / window /
//  switch / artwork / furniture classes (see model export pipeline, Part 3).
//  Input 384×288 (4:3), FP16, runs on the Neural Engine in ~6–9 ms (A14+),
//  so it keeps up with 30 fps; older devices are throttled automatically.

import Accelerate
import ARKit
import CoreML
import Metal
import Vision

final class WallSegmenter {
    struct Output {
        let maskTexture: MTLTexture   // R8Unorm, captured-image space
        let yRef: Float               // smoothed wall reference luminance (linear)
    }

    private let device: MTLDevice
    private let request: VNCoreMLRequest
    private let queue = DispatchQueue(label: "arwall.segmenter", qos: .userInteractive)
    private var busy = false
    private var lastDuration: CFTimeInterval = 0
    private var maskTexture: MTLTexture?
    private var smoothedMask: [Float] = []
    private var yRefEMA: Float = 0.18
    private let lock = NSLock()
    private var latest: Output?

    /// 0 = only latest mask, 1 = frozen. 0.35 removes flicker without visible lag.
    var temporalSmoothing: Float = 0.35

    init?(device: MTLDevice) {
        self.device = device
        let config = MLModelConfiguration()
        config.computeUnits = .all                                     // prefer Neural Engine
        guard let url = Bundle.main.url(forResource: "WallSegmenter", withExtension: "mlmodelc"),
              let ml = try? MLModel(contentsOf: url, configuration: config),
              let vnModel = try? VNCoreMLModel(for: ml) else { return nil }
        request = VNCoreMLRequest(model: vnModel)
        request.imageCropAndScaleOption = .scaleFill                   // keep image-space alignment
    }

    var current: Output? { lock.lock(); defer { lock.unlock() }; return latest }

    /// Non-blocking: drops frames while the previous inference is still running.
    func submit(frame: ARFrame) {
        guard !busy else { return }
        busy = true
        let pixelBuffer = frame.capturedImage
        queue.async { [weak self] in
            guard let self else { return }
            defer { self.busy = false }
            let started = CACurrentMediaTime()
            let handler = VNImageRequestHandler(cvPixelBuffer: pixelBuffer, orientation: .up)
            guard (try? handler.perform([self.request])) != nil,
                  let obs = self.request.results?.first as? VNCoreMLFeatureValueObservation,
                  let prob = obs.featureValue.multiArrayValue else { return }
            self.consume(prob: prob, pixelBuffer: pixelBuffer)
            self.lastDuration = CACurrentMediaTime() - started
        }
    }

    private func consume(prob: MLMultiArray, pixelBuffer: CVPixelBuffer) {
        // Expect shape [1, H, W] (or [1, 1, H, W]); values 0..1.
        let shape = prob.shape.map { $0.intValue }
        let h = shape[shape.count - 2], w = shape[shape.count - 1]
        let n = h * w
        var values = [Float](repeating: 0, count: n)
        if prob.dataType == .float16 {
            var src = vImage_Buffer(data: prob.dataPointer, height: 1, width: vImagePixelCount(n), rowBytes: n * 2)
            values.withUnsafeMutableBytes { dst in
                var d = vImage_Buffer(data: dst.baseAddress, height: 1, width: vImagePixelCount(n), rowBytes: n * 4)
                vImageConvert_Planar16FtoPlanarF(&src, &d, 0)
            }
        } else {
            let p = prob.dataPointer.bindMemory(to: Float.self, capacity: n)
            values = Array(UnsafeBufferPointer(start: p, count: n))
        }

        // Temporal EMA (cheap stabiliser; edges stop "boiling" between frames).
        if smoothedMask.count == n {
            let a = temporalSmoothing
            vDSP.add(vDSP.multiply(a, smoothedMask), vDSP.multiply(1 - a, values), result: &smoothedMask)
        } else {
            smoothedMask = values
        }

        let yRef = wallLuminance(pixelBuffer: pixelBuffer, mask: smoothedMask, maskW: w, maskH: h)
        if let yRef { yRefEMA = 0.85 * yRefEMA + 0.15 * yRef }       // ignore auto-exposure jumps

        // Upload to an R8 texture (Metal samples it with bilinear filtering).
        if maskTexture == nil || maskTexture!.width != w || maskTexture!.height != h {
            let d = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .r8Unorm, width: w, height: h, mipmapped: false)
            d.usage = .shaderRead
            maskTexture = device.makeTexture(descriptor: d)
        }
        var bytes = [UInt8](repeating: 0, count: n)
        for i in 0..<n { bytes[i] = UInt8(max(0, min(255, smoothedMask[i] * 255))) }
        maskTexture!.replace(region: MTLRegionMake2D(0, 0, w, h), mipmapLevel: 0, withBytes: bytes, bytesPerRow: w)

        lock.lock(); latest = Output(maskTexture: maskTexture!, yRef: yRefEMA); lock.unlock()
    }

    /// Median-ish wall luminance from the Y plane, sampled on the mask grid (≈100k samples max).
    private func wallLuminance(pixelBuffer: CVPixelBuffer, mask: [Float], maskW: Int, maskH: Int) -> Float? {
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, 0) else { return nil }
        let pw = CVPixelBufferGetWidthOfPlane(pixelBuffer, 0), ph = CVPixelBufferGetHeightOfPlane(pixelBuffer, 0)
        let stride = CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, 0)
        let ptr = base.assumingMemoryBound(to: UInt8.self)
        var histogram = [Int](repeating: 0, count: 256)
        var count = 0
        for my in Swift.stride(from: 0, to: maskH, by: 2) {
            let py = my * ph / maskH
            for mx in Swift.stride(from: 0, to: maskW, by: 2) where mask[my * maskW + mx] > 0.6 {
                histogram[Int(ptr[py * stride + mx * pw / maskW])] += 1
                count += 1
            }
        }
        guard count > 200 else { return nil }
        var acc = 0, median = 0
        for (i, c) in histogram.enumerated() { acc += c; if acc >= count / 2 { median = i; break } }
        let y = Float(median) / 255
        return y <= 0.04045 ? y / 12.92 : powf((y + 0.055) / 1.055, 2.4)   // gamma-encoded luma → linear
    }

    var averageInferenceMs: Double { lastDuration * 1000 }
}
