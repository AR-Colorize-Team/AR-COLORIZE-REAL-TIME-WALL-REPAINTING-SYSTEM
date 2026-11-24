//  ARWallPlugin.swift
//  Bridges Flutter ⇄ native AR painting.
//    Platform view : "arwall/ar_view"
//    MethodChannel : "arwall/ar"          (commands)
//    EventChannel  : "arwall/ar/events"   (tracking, walls found, hints, stats)
//
//  Register in AppDelegate:  ARWallPlugin.register(with: registrar(forPlugin: "ARWallPlugin")!)
//  Info.plist: NSCameraUsageDescription; UIRequiredDeviceCapabilities is NOT set to arkit,
//  so non-AR devices can still install and use photo mode.

import ARKit
import Flutter
import UIKit

protocol ARWallEventSink: AnyObject { func send(_ event: [String: Any]) }

final class ARWallPlugin: NSObject, FlutterPlugin, FlutterStreamHandler, ARWallEventSink {
    private var eventSink: FlutterEventSink?
    fileprivate weak var activeView: ARWallView?

    static func register(with registrar: FlutterPluginRegistrar) {
        let instance = ARWallPlugin()
        let methods = FlutterMethodChannel(name: "arwall/ar", binaryMessenger: registrar.messenger())
        registrar.addMethodCallDelegate(instance, channel: methods)
        FlutterEventChannel(name: "arwall/ar/events", binaryMessenger: registrar.messenger())
            .setStreamHandler(instance)
        registrar.register(ARWallViewFactory(plugin: instance), withId: "arwall/ar_view")
    }

    // MARK: Events
    func onListen(withArguments _: Any?, eventSink events: @escaping FlutterEventSink) -> FlutterError? {
        eventSink = events; return nil
    }
    func onCancel(withArguments _: Any?) -> FlutterError? { eventSink = nil; return nil }
    func send(_ event: [String: Any]) { DispatchQueue.main.async { self.eventSink?(event) } }

    // MARK: Commands
    func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
        let args = call.arguments as? [String: Any] ?? [:]
        if call.method == "isSupported" {
            result(["ar": ARWorldTrackingConfiguration.isSupported,
                    "lidar": ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)])
            return
        }
        guard let view = activeView else {
            result(FlutterError(code: "no_view", message: "AR view is not on screen", details: nil)); return
        }
        switch call.method {
        case "setPaint":
            view.setPaint(Self.paintSpec(from: args), wallIndex: args["wallIndex"] as? Int)
            result(nil)
        case "selectWallAt":
            let x = args["x"] as? Double ?? 0, y = args["y"] as? Double ?? 0   // logical points
            view.selectWall(at: CGPoint(x: x, y: y)); result(nil)
        case "selectAllWalls": view.selectAllVisibleWalls(); result(nil)
        case "removeWall": view.removeWall(at: args["index"] as? Int ?? -1); result(nil)
        case "clearWalls": view.clearWalls(); result(nil)
        case "setReveal": view.setReveal(Float(args["x"] as? Double ?? 1)); result(nil)
        case "setOldWallAlbedo": view.setOldWallAlbedo(Float(args["value"] as? Double ?? 0.65)); result(nil)
        case "capture":
            view.capture { data in
                result(data.map { FlutterStandardTypedData(bytes: $0) } ??
                       FlutterError(code: "capture_failed", message: nil, details: nil))
            }
        case "stats": result(view.stats)
        case "pause": view.pause(); result(nil)
        case "resume": view.start(); result(nil)
        default: result(FlutterMethodNotImplemented)
        }
    }

    /// Dart sends sRGB HEX strings; the GPU wants linear RGB.
    static func paintSpec(from a: [String: Any]) -> PaintSpec {
        var s = PaintSpec()
        if let h = a["colorA"] as? String { s.colorA = linear(hex: h) }
        s.colorB = (a["colorB"] as? String).map(linear(hex:)) ?? s.colorA
        s.pattern = Int32(a["pattern"] as? Int ?? 0)
        if let p = a["params"] as? [Double] {
            let v = (p + [0, 0, 0, 0]).prefix(4).map(Float.init)
            s.params = SIMD4(v[0], v[1], v[2], v[3])
        }
        s.finish = a["finish"] as? String ?? "matte"
        s.opacity = Float(a["opacity"] as? Double ?? 1)
        return s
    }

    static func linear(hex: String) -> SIMD3<Float> {
        let h = hex.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        var v: UInt64 = 0
        Scanner(string: h).scanHexInt64(&v)
        func lin(_ c: UInt64) -> Float {
            let x = Float(c) / 255
            return x <= 0.04045 ? x / 12.92 : powf((x + 0.055) / 1.055, 2.4)
        }
        return SIMD3(lin((v >> 16) & 0xFF), lin((v >> 8) & 0xFF), lin(v & 0xFF))
    }
}

final class ARWallViewFactory: NSObject, FlutterPlatformViewFactory {
    private let plugin: ARWallPlugin
    init(plugin: ARWallPlugin) { self.plugin = plugin }

    func create(withFrame frame: CGRect, viewIdentifier _: Int64, arguments _: Any?) -> FlutterPlatformView {
        let view = ARWallView(frame: frame, events: plugin)
        plugin.activeView = view
        return view
    }
}
