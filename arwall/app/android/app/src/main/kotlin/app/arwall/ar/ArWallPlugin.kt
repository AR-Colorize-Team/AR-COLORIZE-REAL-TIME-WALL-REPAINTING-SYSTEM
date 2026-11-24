package app.arwall.ar

import android.app.Activity
import android.content.Context
import com.google.ar.core.ArCoreApk
import io.flutter.embedding.engine.plugins.FlutterPlugin
import io.flutter.embedding.engine.plugins.activity.ActivityAware
import io.flutter.embedding.engine.plugins.activity.ActivityPluginBinding
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import io.flutter.plugin.common.StandardMessageCodec
import io.flutter.plugin.platform.PlatformView
import io.flutter.plugin.platform.PlatformViewFactory

/**
 * Same channel contract as iOS (ARWallPlugin.swift):
 *   view "arwall/ar_view" · methods "arwall/ar" · events "arwall/ar/events"
 * Register in MainActivity.configureFlutterEngine: flutterEngine.plugins.add(ArWallPlugin())
 * Manifest: CAMERA permission, <meta-data android:name="com.google.ar.core" android:value="optional"/>
 * (optional → non-ARCore phones still install and use photo mode).
 */
class ArWallPlugin : FlutterPlugin, ActivityAware, MethodChannel.MethodCallHandler, EventChannel.StreamHandler {
    private var activity: Activity? = null
    private var sink: EventChannel.EventSink? = null
    private var view: ArWallView? = null
    private var channel: MethodChannel? = null
    private var binding: FlutterPlugin.FlutterPluginBinding? = null

    override fun onAttachedToEngine(b: FlutterPlugin.FlutterPluginBinding) {
        binding = b
        channel = MethodChannel(b.binaryMessenger, "arwall/ar").also { it.setMethodCallHandler(this) }
        EventChannel(b.binaryMessenger, "arwall/ar/events").setStreamHandler(this)
    }
    override fun onDetachedFromEngine(b: FlutterPlugin.FlutterPluginBinding) { channel?.setMethodCallHandler(null) }

    override fun onAttachedToActivity(b: ActivityPluginBinding) {
        activity = b.activity
        binding?.platformViewRegistry?.registerViewFactory("arwall/ar_view", Factory())
    }
    override fun onDetachedFromActivity() { activity = null }
    override fun onReattachedToActivityForConfigChanges(b: ActivityPluginBinding) { activity = b.activity }
    override fun onDetachedFromActivityForConfigChanges() { activity = null }

    override fun onListen(args: Any?, events: EventChannel.EventSink) { sink = events }
    override fun onCancel(args: Any?) { sink = null }

    private inner class Factory : PlatformViewFactory(StandardMessageCodec.INSTANCE) {
        override fun create(context: Context, viewId: Int, args: Any?): PlatformView {
            val v = ArWallView(activity!!) { e -> sink?.success(e) }
            view = v
            v.resume()
            return v
        }
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        if (call.method == "isSupported") {
            val a = ArCoreApk.getInstance().checkAvailability(activity)
            result.success(mapOf("ar" to a.isSupported, "lidar" to false)); return
        }
        if (call.method == "ensureInstalled") {
            val status = ArCoreApk.getInstance().requestInstall(activity, true)
            result.success(status == ArCoreApk.InstallStatus.INSTALLED); return
        }
        val v = view ?: return result.error("no_view", "AR view is not on screen", null)
        val a = call.arguments as? Map<*, *> ?: emptyMap<String, Any>()
        when (call.method) {
            "setPaint" -> { v.setPaint(paintSpec(a), a["wallIndex"] as? Int); result.success(null) }
            "selectWallAt" -> {
                val dpr = (a["dpr"] as? Double ?: 1.0)                       // Flutter logical px → physical px
                v.selectWallAt(((a["x"] as Double) * dpr).toFloat(), ((a["y"] as Double) * dpr).toFloat())
                result.success(null)
            }
            "selectAllWalls" -> { v.selectAllWalls(); result.success(null) }
            "removeWall" -> { v.removeWall(a["index"] as? Int ?: -1); result.success(null) }
            "clearWalls" -> { v.clearWalls(); result.success(null) }
            "setReveal" -> { v.setReveal((a["x"] as? Double ?: 1.0).toFloat()); result.success(null) }
            "setOldWallAlbedo" -> { v.setOldWallAlbedo((a["value"] as? Double ?: 0.65).toFloat()); result.success(null) }
            "capture" -> v.capture { png -> if (png != null) result.success(png) else result.error("capture_failed", null, null) }
            "stats" -> result.success(v.stats())
            "pause" -> { v.pause(); result.success(null) }
            "resume" -> { v.resume(); result.success(null) }
            else -> result.notImplemented()
        }
    }

    private fun paintSpec(a: Map<*, *>): PaintSpec {
        val colorA = linear(a["colorA"] as? String ?: "#9CAF88")
        val p = (a["params"] as? List<*>)?.map { (it as Number).toFloat() } ?: emptyList()
        return PaintSpec(
            colorA = colorA,
            colorB = (a["colorB"] as? String)?.let(::linear) ?: colorA,
            pattern = a["pattern"] as? Int ?: 0,
            params = FloatArray(4) { i -> p.getOrElse(i) { 0f } },
            finish = a["finish"] as? String ?: "matte",
            opacity = ((a["opacity"] as? Double) ?: 1.0).toFloat(),
        )
    }

    private fun linear(hex: String): FloatArray {
        val v = hex.removePrefix("#").toLong(16)
        fun lin(c: Long): Float { val x = c / 255f; return if (x <= 0.04045f) x / 12.92f else Math.pow((x + 0.055) / 1.055, 2.4).toFloat() }
        return floatArrayOf(lin((v shr 16) and 0xFF), lin((v shr 8) and 0xFF), lin(v and 0xFF))
    }
}
