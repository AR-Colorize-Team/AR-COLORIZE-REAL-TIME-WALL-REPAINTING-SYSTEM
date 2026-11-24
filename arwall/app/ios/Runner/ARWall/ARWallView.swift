//  ARWallView.swift
//  Flutter platform view hosting the live AR painting session (ARKit + Metal).
//
//  Device strategy ("best on each device"):
//   • LiDAR iPhones/iPads: sceneDepth → pixel-accurate occlusion (sofa/lamp in front of
//     the wall are never painted) + mesh classification to confirm a tapped surface is
//     a wall, not a door/window/cabinet.
//   • All other devices: vertical plane detection + on-device semantic segmentation.
//   • Every device: person segmentation so people walking past aren't painted.

import ARKit
import Flutter
import MetalKit
import UIKit

// MARK: - Wall state shared with the renderer

struct PaintSpec {
    var colorA = SIMD3<Float>(0.6, 0.65, 0.55)   // linear RGB
    var colorB = SIMD3<Float>(0.6, 0.65, 0.55)
    var pattern: Int32 = 0
    var params = SIMD4<Float>(0, 0, 0, 0)
    var finish = "matte"
    var opacity: Float = 1
}

struct SelectedWall {
    let anchorID: UUID
    var paint: PaintSpec
}

// Must match `WallUniform` / `FrameUniforms` in WallPaint.metal byte-for-byte.
struct WallUniform {
    var worldToPlane = matrix_identity_float4x4
    var planeNormal = SIMD4<Float>(0, 0, 0, 0)
    var extent = SIMD4<Float>(0, 0, 0, 0)
    var colorA = SIMD4<Float>(0, 0, 0, 0)
    var colorB = SIMD4<Float>(0, 0, 0, 0)
    var params = SIMD4<Float>(0, 0, 0, 0)
    var floorY: Float = 0
    var opacity: Float = 1
    var pattern: Int32 = 0
    var specularKeep: Float = 0
    var sheen: Float = 0
    var active: Int32 = 0
    var _pad = SIMD2<Float>(0, 0)
}

struct FrameUniforms {
    var invViewProj = matrix_identity_float4x4
    var cameraPos = SIMD4<Float>(0, 0, 0, 1)
    var lightTint = SIMD3<Float>(1, 1, 1)
    var yRef: Float = 0.18
    var oldWallAlbedo: Float = 0.65
    var contrastK: Float = 0.85
    var revealX: Float = 1
    var wallCount: Int32 = 0
    var hasDepth: Int32 = 0
    var hasPersonMask: Int32 = 0
    var depthTolerance: Float = 0.08
    var _pad: Float = 0
}

let finishTable: [String: (Float, Float)] = [
    "flat": (0, 0), "matte": (0.05, 0), "eggshell": (0.15, 0.03),
    "satin": (0.30, 0.06), "semi_gloss": (0.50, 0.10), "gloss": (0.70, 0.15),
]

// MARK: - Platform view

final class ARWallView: NSObject, FlutterPlatformView, ARSessionDelegate, MTKViewDelegate {
    static let maxWalls = 4

    private let mtkView: MTKView
    private let session = ARSession()
    private let device: MTLDevice
    private let commandQueue: MTLCommandQueue
    private var pipeline: MTLRenderPipelineState!
    private var textureCache: CVMetalTextureCache!
    private let segmenter: WallSegmenter?
    private let events: ARWallEventSink

    private var walls: [SelectedWall] = []
    private var planes: [UUID: ARPlaneAnchor] = [:]
    private var floorY: Float?
    private var revealX: Float = 1
    private var oldWallAlbedo: Float = 0.65
    private var currentPaint = PaintSpec()
    private var lastFrameTime: CFTimeInterval = 0
    private var fps: Double = 0
    private let hasLiDAR = ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
    private var dummyTexture: MTLTexture!
    private var pendingCapture: ((Data?) -> Void)?

    init(frame: CGRect, events: ARWallEventSink) {
        guard let device = MTLCreateSystemDefaultDevice(), let queue = device.makeCommandQueue() else {
            fatalError("Metal unavailable")
        }
        self.device = device
        self.commandQueue = queue
        self.events = events
        self.segmenter = WallSegmenter(device: device)
        self.mtkView = MTKView(frame: frame, device: device)
        super.init()

        mtkView.colorPixelFormat = .bgra8Unorm
        mtkView.framebufferOnly = false               // allow readback for "Save photo"
        mtkView.preferredFramesPerSecond = 60
        mtkView.delegate = self
        // Taps are handled in Flutter (so overlays can intercept them) and forwarded via selectWall(at:).

        CVMetalTextureCacheCreate(nil, nil, device, nil, &textureCache)
        buildPipeline()
        dummyTexture = makeDummy()
        session.delegate = self
        start()
    }

    func view() -> UIView { mtkView }

    // MARK: Session

    func start() {
        let config = ARWorldTrackingConfiguration()
        config.planeDetection = [.vertical, .horizontal]
        config.isLightEstimationEnabled = true
        config.environmentTexturing = .none
        if hasLiDAR {
            config.frameSemantics.insert(.sceneDepth)
            if ARWorldTrackingConfiguration.supportsSceneReconstruction(.meshWithClassification) {
                config.sceneReconstruction = .meshWithClassification
            }
        }
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.personSegmentation) {
            config.frameSemantics.insert(.personSegmentation)
        }
        // Highest-resolution 4:3 format ≤ 1920 wide: sharp edges without thermal throttling.
        if let fmt = ARWorldTrackingConfiguration.supportedVideoFormats
            .filter({ $0.imageResolution.width <= 1920 && $0.framesPerSecond >= 30 })
            .max(by: { $0.imageResolution.width < $1.imageResolution.width }) {
            config.videoFormat = fmt
        }
        session.run(config, options: [.resetTracking, .removeExistingAnchors])
        events.send(["type": "capabilities", "lidar": hasLiDAR, "segmenter": segmenter != nil])
    }

    func pause() { session.pause() }

    // MARK: Commands from Flutter

    func setPaint(_ spec: PaintSpec, wallIndex: Int?) {
        currentPaint = spec
        if let i = wallIndex, walls.indices.contains(i) { walls[i].paint = spec }
        else if !walls.isEmpty { walls[walls.count - 1].paint = spec }   // default: last selected wall
        emitWalls()
    }

    func setReveal(_ x: Float) { revealX = x }
    func setOldWallAlbedo(_ v: Float) { oldWallAlbedo = max(0.05, min(0.95, v)) }
    func clearWalls() { walls.removeAll(); emitWalls() }
    func removeWall(at i: Int) { if walls.indices.contains(i) { walls.remove(at: i); emitWalls() } }

    /// "Paint all walls": select every tracked vertical plane in view.
    func selectAllVisibleWalls() {
        for (id, _) in planes where planes[id]?.alignment == .vertical {
            if walls.count >= Self.maxWalls { break }
            if !walls.contains(where: { $0.anchorID == id }) { walls.append(SelectedWall(anchorID: id, paint: currentPaint)) }
        }
        emitWalls()
    }

    func capture(_ completion: @escaping (Data?) -> Void) { pendingCapture = completion }

    /// Tap a wall → raycast to a vertical plane → paint it with the current colour.
    func selectWall(at point: CGPoint) {
        guard let frame = session.currentFrame else { return }
        let query = frame.raycastQuery(from: point, allowing: .existingPlaneGeometry, alignment: .vertical)
        guard let hit = session.raycast(query).first, let anchor = hit.anchor as? ARPlaneAnchor else {
            events.send(["type": "hint", "code": "no_wall_here"])
            UINotificationFeedbackGenerator().notificationOccurred(.warning)
            return
        }
        if isNonWallSurface(at: hit.worldTransform.columns.3) {
            events.send(["type": "hint", "code": "not_a_wall"])                 // e.g. wardrobe door
            return
        }
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
        if let i = walls.firstIndex(where: { $0.anchorID == anchor.identifier }) {
            walls[i].paint = currentPaint                                        // re-tap = repaint
        } else {
            if walls.count >= Self.maxWalls { walls.removeFirst() }
            walls.append(SelectedWall(anchorID: anchor.identifier, paint: currentPaint))
        }
        emitWalls()
    }

    /// LiDAR mesh classification: reject taps landing on doors/windows.
    private func isNonWallSurface(at p: SIMD4<Float>) -> Bool {
        guard hasLiDAR, let anchors = session.currentFrame?.anchors.compactMap({ $0 as? ARMeshAnchor }) else { return false }
        let point = SIMD3<Float>(p.x, p.y, p.z)
        for mesh in anchors {
            let local = mesh.transform.inverse * SIMD4<Float>(point, 1)
            let g = mesh.geometry
            // Cheap check on a subsample of faces near the hit (production: spatial hash per anchor).
            let faces = g.faces
            let stride = max(1, faces.count / 4000)
            for f in Swift.stride(from: 0, to: faces.count, by: stride) {
                let v = g.vertex(at: g.vertexIndicesOf(faceWithIndex: f)[0])
                if simd_distance(SIMD3(v.0, v.1, v.2), SIMD3(local.x, local.y, local.z)) < 0.06 {
                    let cls = g.classificationOf(faceWithIndex: f)
                    return cls == .door || cls == .window
                }
            }
        }
        return false
    }

    private func emitWalls() {
        events.send(["type": "walls", "count": walls.count,
                     "walls": walls.enumerated().map { i, w in
                         ["index": i, "id": w.anchorID.uuidString, "pattern": Int(w.paint.pattern),
                          "area_m2": planes[w.anchorID].map { Double($0.planeExtent.width * $0.planeExtent.height) } ?? 0]
                     }])
    }

    // MARK: ARSessionDelegate

    func session(_ session: ARSession, didAdd anchors: [ARAnchor]) { ingest(anchors) }
    func session(_ session: ARSession, didUpdate anchors: [ARAnchor]) { ingest(anchors) }
    func session(_ session: ARSession, didRemove anchors: [ARAnchor]) {
        for a in anchors { planes.removeValue(forKey: a.identifier) }
        walls.removeAll { w in anchors.contains { $0.identifier == w.anchorID } }
    }

    private func ingest(_ anchors: [ARAnchor]) {
        var newWall = false
        for case let p as ARPlaneAnchor in anchors {
            if planes[p.identifier] == nil && p.alignment == .vertical { newWall = true }
            planes[p.identifier] = p
            if p.alignment == .horizontal, p.classification == .floor || floorY == nil {
                let y = p.transform.columns.3.y
                floorY = min(floorY ?? y, y)
            }
        }
        if newWall { events.send(["type": "wall_found", "total": planes.values.filter { $0.alignment == .vertical }.count]) }
    }

    func session(_ session: ARSession, cameraDidChangeTrackingState camera: ARCamera) {
        let state: String
        switch camera.trackingState {
        case .normal: state = "normal"
        case .notAvailable: state = "not_available"
        case .limited(let r):
            switch r {
            case .excessiveMotion: state = "move_slower"
            case .insufficientFeatures: state = "more_light_or_texture"
            case .initializing, .relocalizing: state = "initializing"
            @unknown default: state = "limited"
            }
        }
        events.send(["type": "tracking", "state": state])
    }

    func sessionWasInterrupted(_ session: ARSession) { events.send(["type": "tracking", "state": "interrupted"]) }
    func sessionInterruptionEnded(_ session: ARSession) {
        // Keep anchors; ARKit relocalises automatically after a phone call / app switch.
        events.send(["type": "tracking", "state": "relocalizing"])
    }

    // MARK: Rendering

    private func buildPipeline() {
        let lib = device.makeDefaultLibrary()!
        let d = MTLRenderPipelineDescriptor()
        d.vertexFunction = lib.makeFunction(name: "wallPaintVertex")
        d.fragmentFunction = lib.makeFunction(name: "wallPaintFragment")
        d.colorAttachments[0].pixelFormat = mtkView.colorPixelFormat
        pipeline = try! device.makeRenderPipelineState(descriptor: d)
    }

    private func makeDummy() -> MTLTexture {
        let d = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .r8Unorm, width: 1, height: 1, mipmapped: false)
        let t = device.makeTexture(descriptor: d)!
        var zero: UInt8 = 0
        t.replace(region: MTLRegionMake2D(0, 0, 1, 1), mipmapLevel: 0, withBytes: &zero, bytesPerRow: 1)
        return t
    }

    private func texture(from pb: CVPixelBuffer, plane: Int, format: MTLPixelFormat) -> MTLTexture? {
        let planar = CVPixelBufferIsPlanar(pb)
        let w = planar ? CVPixelBufferGetWidthOfPlane(pb, plane) : CVPixelBufferGetWidth(pb)
        let h = planar ? CVPixelBufferGetHeightOfPlane(pb, plane) : CVPixelBufferGetHeight(pb)
        var cv: CVMetalTexture?
        CVMetalTextureCacheCreateTextureFromImage(nil, textureCache, pb, nil, format, w, h, plane, &cv)
        return cv.flatMap(CVMetalTextureGetTexture)
    }

    func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) {}

    func draw(in view: MTKView) {
        guard let frame = session.currentFrame,
              let drawable = view.currentDrawable,
              let pass = view.currentRenderPassDescriptor,
              let camY = texture(from: frame.capturedImage, plane: 0, format: .r8Unorm),
              let camCbCr = texture(from: frame.capturedImage, plane: 1, format: .rg8Unorm),
              let cmd = commandQueue.makeCommandBuffer(),
              let enc = cmd.makeRenderCommandEncoder(descriptor: pass) else { return }

        segmenter?.submit(frame: frame)
        let seg = segmenter?.current
        let orientation = UIInterfaceOrientation.portrait
        let viewport = view.bounds.size

        // Quad with per-vertex camera UVs (displayTransform maps image → view; we need its inverse).
        let toImage = frame.displayTransform(for: orientation, viewportSize: viewport).inverted()
        let corners: [(Float, Float, Float, Float)] = [(-1, -1, 0, 1), (1, -1, 1, 1), (-1, 1, 0, 0), (1, 1, 1, 0)]
        var quad: [SIMD4<Float>] = corners.map { c in
            let p = CGPoint(x: CGFloat(c.2), y: CGFloat(c.3)).applying(toImage)   // view UV → image UV
            return SIMD4<Float>(c.0, c.1, Float(p.x), Float(p.y))
        }

        let cam = frame.camera
        let proj = cam.projectionMatrix(for: orientation, viewportSize: viewport, zNear: 0.01, zFar: 100)
        let viewM = cam.viewMatrix(for: orientation)
        var F = FrameUniforms()
        F.invViewProj = (proj * viewM).inverse
        F.cameraPos = cam.transform.columns.3
        F.lightTint = Self.tint(fromKelvin: Float(frame.lightEstimate?.ambientColorTemperature ?? 6500))
        F.yRef = seg?.yRef ?? 0.18
        F.oldWallAlbedo = oldWallAlbedo
        F.revealX = revealX
        F.wallCount = Int32(min(walls.count, Self.maxWalls))
        F.hasDepth = frame.sceneDepth != nil ? 1 : 0
        F.hasPersonMask = frame.segmentationBuffer != nil ? 1 : 0

        let fy = floorY ?? (cam.transform.columns.3.y - 1.4)            // fallback: phone held ~1.4 m up
        var U = [WallUniform](repeating: WallUniform(), count: Self.maxWalls)
        for (i, w) in walls.prefix(Self.maxWalls).enumerated() {
            guard let a = planes[w.anchorID] else { continue }
            let n = simd_normalize(SIMD3(a.transform.columns.1.x, a.transform.columns.1.y, a.transform.columns.1.z))
            let o = SIMD3(a.transform.columns.3.x, a.transform.columns.3.y, a.transform.columns.3.z)
            let c = a.center, ext = a.planeExtent
            let (spec, sheen) = finishTable[w.paint.finish] ?? (0.05, 0)
            U[i] = WallUniform(worldToPlane: a.transform.inverse,
                               planeNormal: SIMD4(n, -simd_dot(n, o)),
                               extent: SIMD4(c.x - ext.width / 2, c.x + ext.width / 2, c.z - ext.height / 2, c.z + ext.height / 2),
                               colorA: SIMD4(w.paint.colorA, 0), colorB: SIMD4(w.paint.colorB, 0),
                               params: w.paint.params, floorY: fy, opacity: w.paint.opacity,
                               pattern: w.paint.pattern, specularKeep: spec, sheen: sheen, active: 1)
        }

        var depthTex: MTLTexture = dummyTexture
        if let d = frame.sceneDepth?.depthMap, let t = texture(from: d, plane: 0, format: .r32Float) { depthTex = t }
        var personTex: MTLTexture = dummyTexture
        if let s = frame.segmentationBuffer, let t = texture(from: s, plane: 0, format: .r8Unorm) { personTex = t }

        enc.setRenderPipelineState(pipeline)
        enc.setVertexBytes(&quad, length: MemoryLayout<SIMD4<Float>>.stride * 4, index: 0)
        enc.setFragmentTexture(camY, index: 0)
        enc.setFragmentTexture(camCbCr, index: 1)
        enc.setFragmentTexture(seg?.maskTexture ?? dummyTexture, index: 2)
        enc.setFragmentTexture(depthTex, index: 3)
        enc.setFragmentTexture(personTex, index: 4)
        enc.setFragmentBytes(&F, length: MemoryLayout<FrameUniforms>.stride, index: 0)
        enc.setFragmentBytes(&U, length: MemoryLayout<WallUniform>.stride * Self.maxWalls, index: 1)
        enc.drawPrimitives(type: .triangleStrip, vertexStart: 0, vertexCount: 4)
        enc.endEncoding()

        if let done = pendingCapture, let blit = cmd.makeBlitCommandEncoder() {
            // Drawables live in private GPU memory: blit into a CPU-readable texture first.
            pendingCapture = nil
            let src = drawable.texture
            let d = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: src.pixelFormat, width: src.width,
                                                             height: src.height, mipmapped: false)
            d.storageMode = .shared
            if let copy = device.makeTexture(descriptor: d) {
                blit.copy(from: src, to: copy)
                cmd.addCompletedHandler { _ in
                    let data = Self.png(from: copy)
                    DispatchQueue.main.async { done(data) }
                }
            } else { DispatchQueue.main.async { done(nil) } }
            blit.endEncoding()
        }
        cmd.present(drawable)
        cmd.commit()

        let now = CACurrentMediaTime()
        if lastFrameTime > 0 { fps = fps * 0.9 + 0.1 / (now - lastFrameTime) }
        lastFrameTime = now
    }

    // MARK: Helpers

    /// Colour temperature → luminance-normalised linear RGB tint relative to D65.
    static func tint(fromKelvin k: Float, strength: Float = 0.8) -> SIMD3<Float> {
        func rgb(_ kelvin: Float) -> SIMD3<Float> {            // Tanner Helland approximation
            let t = kelvin / 100
            let r: Float = t <= 66 ? 255 : 329.698727446 * powf(t - 60, -0.1332047592)
            let g: Float = t <= 66 ? 99.4708025861 * logf(t) - 161.1195681661 : 288.1221695283 * powf(t - 60, -0.0755148492)
            let b: Float = t >= 66 ? 255 : (t <= 19 ? 0 : 138.5177312231 * logf(t - 10) - 305.0447927307)
            let c = simd_clamp(SIMD3(r, g, b) / 255, SIMD3(repeating: 0), SIMD3(repeating: 1))
            return SIMD3(c.x <= 0.04045 ? c.x / 12.92 : powf((c.x + 0.055) / 1.055, 2.4),
                         c.y <= 0.04045 ? c.y / 12.92 : powf((c.y + 0.055) / 1.055, 2.4),
                         c.z <= 0.04045 ? c.z / 12.92 : powf((c.z + 0.055) / 1.055, 2.4))
        }
        let ratio = rgb(max(1500, min(k, 12000))) / rgb(6500)
        let luma = simd_dot(ratio, SIMD3(0.2126, 0.7152, 0.0722))
        let t = ratio / max(luma, 1e-4)
        return SIMD3(repeating: 1 - strength) + strength * t
    }

    private static func png(from tex: MTLTexture) -> Data? {
        let w = tex.width, h = tex.height, bpr = w * 4
        var bytes = [UInt8](repeating: 0, count: bpr * h)
        tex.getBytes(&bytes, bytesPerRow: bpr, from: MTLRegionMake2D(0, 0, w, h), mipmapLevel: 0)
        let info = CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)
        guard let ctx = CGContext(data: &bytes, width: w, height: h, bitsPerComponent: 8, bytesPerRow: bpr,
                                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: info.rawValue),
              let img = ctx.makeImage() else { return nil }
        return UIImage(cgImage: img).pngData()
    }

    var stats: [String: Any] {
        ["fps": fps.rounded(), "segmenter_ms": segmenter?.averageInferenceMs ?? 0,
         "walls": walls.count, "lidar": hasLiDAR]
    }
}

// MARK: - ARMeshGeometry convenience (Apple sample pattern)

extension ARMeshGeometry {
    func vertex(at index: UInt32) -> (Float, Float, Float) {
        let p = vertices.buffer.contents().advanced(by: vertices.offset + vertices.stride * Int(index))
        return p.assumingMemoryBound(to: (Float, Float, Float).self).pointee
    }
    func vertexIndicesOf(faceWithIndex i: Int) -> [UInt32] {
        let per = faces.indexCountPerPrimitive
        let base = faces.buffer.contents()
        return (0..<per).map { base.advanced(by: (i * per + $0) * faces.bytesPerIndex).assumingMemoryBound(to: UInt32.self).pointee }
    }
    func classificationOf(faceWithIndex i: Int) -> ARMeshClassification {
        guard let c = classification else { return .none }
        let v = c.buffer.contents().advanced(by: c.offset + c.stride * i).assumingMemoryBound(to: UInt8.self).pointee
        return ARMeshClassification(rawValue: Int(v)) ?? .none
    }
}
