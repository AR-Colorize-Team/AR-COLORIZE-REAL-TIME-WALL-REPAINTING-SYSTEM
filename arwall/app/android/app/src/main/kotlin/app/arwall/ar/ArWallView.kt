package app.arwall.ar

import android.app.Activity
import android.content.Context
import android.graphics.Bitmap
import android.opengl.GLES11Ext
import android.opengl.GLES30
import android.opengl.GLSurfaceView
import android.opengl.Matrix
import android.os.Handler
import android.os.Looper
import android.view.View
import com.google.ar.core.*
import com.google.ar.core.exceptions.NotYetAvailableException
import com.google.ar.core.exceptions.SessionPausedException
import io.flutter.plugin.platform.PlatformView
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import javax.microedition.khronos.egl.EGLConfig
import javax.microedition.khronos.opengles.GL10
import kotlin.math.min

data class PaintSpec(
    val colorA: FloatArray = floatArrayOf(0.6f, 0.65f, 0.55f),   // linear RGB
    val colorB: FloatArray = colorA,
    val pattern: Int = 0,
    val params: FloatArray = FloatArray(4),
    val finish: String = "matte",
    val opacity: Float = 1f,
)

private val FINISHES = mapOf(
    "flat" to (0f to 0f), "matte" to (0.05f to 0f), "eggshell" to (0.15f to 0.03f),
    "satin" to (0.30f to 0.06f), "semi_gloss" to (0.50f to 0.10f), "gloss" to (0.70f to 0.15f),
)

/**
 * Live AR painting on Android: ARCore tracking + vertical planes + Depth API (on supported
 * devices, no ToF sensor needed) + on-device segmentation, rendered in one GLES 3 pass.
 */
class ArWallView(
    private val activity: Activity,
    private val events: (Map<String, Any?>) -> Unit,
) : PlatformView, GLSurfaceView.Renderer {

    companion object { const val MAX_WALLS = 4 }

    private val glView = GLSurfaceView(activity).apply {
        preserveEGLContextOnPause = true
        setEGLContextClientVersion(3)
        setEGLConfigChooser(8, 8, 8, 8, 16, 0)
        setRenderer(this@ArWallView)
        renderMode = GLSurfaceView.RENDERMODE_CONTINUOUSLY
    }
    private val main = Handler(Looper.getMainLooper())
    private var session: Session? = null
    private val segmenter = WallSegmenter(activity)

    // GL objects
    private var program = 0
    private var cameraTex = 0
    private var maskTex = 0
    private var depthTex = 0
    private var dummyTex = 0
    private val quadNdc = floatArrayOf(-1f, -1f, 1f, -1f, -1f, 1f, 1f, 1f)
    private val camUV = FloatArray(8)
    private val imgUV = FloatArray(8)
    private val vbuf: FloatBuffer = ByteBuffer.allocateDirect(4 * 8 * 3).order(ByteOrder.nativeOrder()).asFloatBuffer()
    private var viewW = 1; private var viewH = 1
    private var hasDepth = false

    // Scene state (guarded by `this`)
    private data class Wall(val plane: Plane, var paint: PaintSpec)
    private val walls = mutableListOf<Wall>()
    private var current = PaintSpec()
    private var revealX = 1f
    private var oldWallAlbedo = 0.65f
    private var floorY: Float? = null
    private var pendingTap: Pair<Float, Float>? = null
    private var pendingCapture: ((ByteArray?) -> Unit)? = null
    private var lastTracking = ""
    private var fps = 0.0; private var lastFrameNs = 0L
    private var knownWalls = 0

    override fun getView(): View = glView

    // ------------------------------------------------------------------ lifecycle
    fun resume() {
        if (session == null) {
            session = Session(activity).also { s ->
                val cfg = Config(s).apply {
                    planeFindingMode = Config.PlaneFindingMode.HORIZONTAL_AND_VERTICAL
                    lightEstimationMode = Config.LightEstimationMode.AMBIENT_INTENSITY
                    updateMode = Config.UpdateMode.LATEST_CAMERA_IMAGE
                    focusMode = Config.FocusMode.AUTO
                    depthMode = if (s.isDepthModeSupported(Config.DepthMode.AUTOMATIC)) Config.DepthMode.AUTOMATIC
                                else Config.DepthMode.DISABLED
                }
                hasDepth = cfg.depthMode == Config.DepthMode.AUTOMATIC
                s.configure(cfg)
            }
            events(mapOf("type" to "capabilities", "depth" to hasDepth, "lidar" to false, "segmenter" to true))
        }
        session?.resume()
        glView.onResume()
    }

    fun pause() { glView.onPause(); session?.pause() }

    override fun dispose() {
        pause()
        session?.close(); session = null
        segmenter.close()
    }

    // ------------------------------------------------------------------ commands (UI thread)
    @Synchronized fun setPaint(spec: PaintSpec, wallIndex: Int?) {
        current = spec
        when {
            wallIndex != null && wallIndex in walls.indices -> walls[wallIndex].paint = spec
            walls.isNotEmpty() -> walls.last().paint = spec
        }
        emitWalls()
    }
    @Synchronized fun selectWallAt(xPx: Float, yPx: Float) { pendingTap = xPx to yPx }   // resolved on GL thread
    @Synchronized fun clearWalls() { walls.clear(); emitWalls() }
    @Synchronized fun removeWall(i: Int) { if (i in walls.indices) { walls.removeAt(i); emitWalls() } }
    @Synchronized fun setReveal(x: Float) { revealX = x }
    @Synchronized fun setOldWallAlbedo(v: Float) { oldWallAlbedo = v.coerceIn(0.05f, 0.95f) }
    @Synchronized fun capture(cb: (ByteArray?) -> Unit) { pendingCapture = cb }
    @Synchronized fun selectAllWalls() {
        val s = session ?: return
        s.getAllTrackables(Plane::class.java)
            .filter { it.type == Plane.Type.VERTICAL && it.trackingState == TrackingState.TRACKING && it.subsumedBy == null }
            .forEach { p -> if (walls.size < MAX_WALLS && walls.none { it.plane == p }) walls.add(Wall(p, current)) }
        emitWalls()
    }
    fun stats(): Map<String, Any> = mapOf("fps" to fps.toInt(), "segmenter_ms" to segmenter.lastInferenceMs,
                                          "walls" to walls.size, "depth" to hasDepth)

    private fun emitWalls() {
        val list = walls.mapIndexed { i, w ->
            mapOf("index" to i, "id" to w.plane.hashCode().toString(), "pattern" to w.paint.pattern,
                  "area_m2" to (w.plane.extentX * w.plane.extentZ).toDouble())
        }
        main.post { events(mapOf("type" to "walls", "count" to walls.size, "walls" to list)) }
    }
    private fun emit(e: Map<String, Any?>) = main.post { events(e) }

    // ------------------------------------------------------------------ GL
    override fun onSurfaceCreated(gl: GL10?, config: EGLConfig?) {
        program = GlUtil.program(activity.assets, "shaders/wall_paint.vert", "shaders/wall_paint.frag")
        val t = IntArray(4); GLES30.glGenTextures(4, t, 0)
        cameraTex = t[0]; maskTex = t[1]; depthTex = t[2]; dummyTex = t[3]
        GLES30.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, cameraTex)
        GlUtil.linearClamp(GLES11Ext.GL_TEXTURE_EXTERNAL_OES)
        for (tex in intArrayOf(maskTex, depthTex, dummyTex)) {
            GLES30.glBindTexture(GLES30.GL_TEXTURE_2D, tex); GlUtil.linearClamp(GLES30.GL_TEXTURE_2D)
        }
        GLES30.glBindTexture(GLES30.GL_TEXTURE_2D, dummyTex)
        GLES30.glPixelStorei(GLES30.GL_UNPACK_ALIGNMENT, 1)
        GLES30.glTexImage2D(GLES30.GL_TEXTURE_2D, 0, GLES30.GL_RG8, 1, 1, 0, GLES30.GL_RG,
                            GLES30.GL_UNSIGNED_BYTE, ByteBuffer.allocateDirect(2))
        session?.setCameraTextureName(cameraTex)
    }

    override fun onSurfaceChanged(gl: GL10?, width: Int, height: Int) {
        viewW = width; viewH = height
        GLES30.glViewport(0, 0, width, height)
        val rotation = activity.windowManager.defaultDisplay.rotation
        session?.setDisplayGeometry(rotation, width, height)
    }

    override fun onDrawFrame(gl: GL10?) {
        GLES30.glClear(GLES30.GL_COLOR_BUFFER_BIT)
        val s = session ?: return
        s.setCameraTextureName(cameraTex)
        val frame = try { s.update() } catch (e: SessionPausedException) { return }
        val camera = frame.camera

        if (frame.hasDisplayGeometryChanged() || camUV[0] == 0f && camUV[1] == 0f) {
            frame.transformCoordinates2d(Coordinates2d.OPENGL_NORMALIZED_DEVICE_COORDINATES, quadNdc,
                                         Coordinates2d.TEXTURE_NORMALIZED, camUV)
            frame.transformCoordinates2d(Coordinates2d.OPENGL_NORMALIZED_DEVICE_COORDINATES, quadNdc,
                                         Coordinates2d.IMAGE_NORMALIZED, imgUV)
        }
        reportTracking(camera)
        trackPlanes(s)
        handleTap(frame)

        // Feed the segmenter (CPU image; async).
        try { frame.acquireCameraImage().use { segmenter.submit(it) } } catch (_: NotYetAvailableException) {}
        segmenter.latest?.let { out ->
            GLES30.glBindTexture(GLES30.GL_TEXTURE_2D, maskTex)
            GLES30.glPixelStorei(GLES30.GL_UNPACK_ALIGNMENT, 1)
            GLES30.glTexImage2D(GLES30.GL_TEXTURE_2D, 0, GLES30.GL_R8, out.width, out.height, 0,
                                GLES30.GL_RED, GLES30.GL_UNSIGNED_BYTE, ByteBuffer.wrap(out.mask))
        }
        var depthOk = false
        if (hasDepth) {
            try {
                frame.acquireDepthImage16Bits().use { d ->
                    // DEPTH16 uploaded as RG8; shader reconstructs millimetres (ARCore sample technique).
                    GLES30.glBindTexture(GLES30.GL_TEXTURE_2D, depthTex)
                    GLES30.glPixelStorei(GLES30.GL_UNPACK_ALIGNMENT, 2)
                    GLES30.glTexImage2D(GLES30.GL_TEXTURE_2D, 0, GLES30.GL_RG8, d.width, d.height, 0,
                                        GLES30.GL_RG, GLES30.GL_UNSIGNED_BYTE, d.planes[0].buffer)
                    depthOk = true
                }
            } catch (_: NotYetAvailableException) {}
        }

        if (camera.trackingState == TrackingState.TRACKING) draw(frame, camera, depthOk)
        else drawCameraOnly(frame, camera)

        pendingCapture?.let { cb -> pendingCapture = null; val png = readPixelsPng(); main.post { cb(png) } }

        val now = System.nanoTime()
        if (lastFrameNs > 0) fps = fps * 0.9 + 0.1 * 1e9 / (now - lastFrameNs)
        lastFrameNs = now
    }

    private fun drawCameraOnly(frame: Frame, camera: Camera) = draw(frame, camera, false, wallsOverride = 0)

    @Synchronized
    private fun draw(frame: Frame, camera: Camera, depthOk: Boolean, wallsOverride: Int? = null) {
        val proj = FloatArray(16); val view = FloatArray(16); val vp = FloatArray(16); val inv = FloatArray(16)
        camera.getProjectionMatrix(proj, 0, 0.01f, 100f)
        camera.getViewMatrix(view, 0)
        Matrix.multiplyMM(vp, 0, proj, 0, view, 0)
        Matrix.invertM(inv, 0, vp, 0)
        val pose = camera.pose

        GLES30.glUseProgram(program)
        // Interleave NDC, camUV, imgUV per vertex.
        vbuf.clear()
        for (i in 0 until 4) {
            vbuf.put(quadNdc[i * 2]).put(quadNdc[i * 2 + 1])
            vbuf.put(camUV[i * 2]).put(camUV[i * 2 + 1])
            vbuf.put(imgUV[i * 2]).put(imgUV[i * 2 + 1])
        }
        vbuf.position(0)
        for ((loc, off) in listOf(0 to 0, 1 to 2, 2 to 4)) {
            vbuf.position(off)
            GLES30.glVertexAttribPointer(loc, 2, GLES30.GL_FLOAT, false, 24, vbuf)
            GLES30.glEnableVertexAttribArray(loc)
        }

        GlUtil.bindTex(program, "uCamera", 0, GLES11Ext.GL_TEXTURE_EXTERNAL_OES, cameraTex)
        GlUtil.bindTex(program, "uWallMask", 1, GLES30.GL_TEXTURE_2D, if (segmenter.latest != null) maskTex else dummyTex)
        GlUtil.bindTex(program, "uDepth", 2, GLES30.GL_TEXTURE_2D, if (depthOk) depthTex else dummyTex)

        GlUtil.mat4(program, "uInvViewProj", inv)
        GlUtil.vec3(program, "uCameraPos", pose.tx(), pose.ty(), pose.tz())
        GlUtil.vec3(program, "uLightTint", *lightTint(frame))
        GlUtil.float(program, "uYRef", segmenter.latest?.yRef ?: 0.18f)
        GlUtil.float(program, "uOldWallAlbedo", oldWallAlbedo)
        GlUtil.float(program, "uContrastK", 0.85f)
        GlUtil.float(program, "uRevealX", revealX)
        GlUtil.int(program, "uHasDepth", if (depthOk) 1 else 0)
        GlUtil.float(program, "uDepthTolerance", 0.10f)                 // ARCore depth is noisier than LiDAR

        val fy = floorY ?: (pose.ty() - 1.4f)
        val active = walls.filter { it.plane.trackingState == TrackingState.TRACKING }.take(MAX_WALLS)
        val count = wallsOverride ?: active.size
        GlUtil.int(program, "uWallCount", count)
        active.forEachIndexed { i, w ->
            val center = w.plane.centerPose
            val m = FloatArray(16); center.toMatrix(m, 0)
            val toPlane = FloatArray(16); Matrix.invertM(toPlane, 0, m, 0)
            val n = center.yAxis                                              // plane normal
            val d = -(n[0] * center.tx() + n[1] * center.ty() + n[2] * center.tz())
            val (spec, sheen) = FINISHES[w.paint.finish] ?: (0.05f to 0f)
            GlUtil.mat4(program, "uWorldToPlane[$i]", toPlane)
            GlUtil.vec4(program, "uPlane[$i]", n[0], n[1], n[2], d)
            GlUtil.vec4(program, "uExtent[$i]", -w.plane.extentX / 2, w.plane.extentX / 2,
                        -w.plane.extentZ / 2, w.plane.extentZ / 2)
            GlUtil.vec3(program, "uColorA[$i]", *w.paint.colorA)
            GlUtil.vec3(program, "uColorB[$i]", *w.paint.colorB)
            GlUtil.vec4(program, "uParams[$i]", *w.paint.params)
            GlUtil.int(program, "uPattern[$i]", w.paint.pattern)
            GlUtil.vec4(program, "uWallMisc[$i]", fy, w.paint.opacity, spec, sheen)
        }
        GLES30.glDrawArrays(GLES30.GL_TRIANGLE_STRIP, 0, 4)
    }

    /** ARCore colour correction → luminance-normalised tint (same convention as iOS). */
    private fun lightTint(frame: Frame): FloatArray {
        val le = frame.lightEstimate
        if (le.state != LightEstimate.State.VALID) return floatArrayOf(1f, 1f, 1f)
        val cc = FloatArray(4); le.getColorCorrection(cc, 0)                 // rgb scale (gamma), a = intensity
        val lin = FloatArray(3) { i -> cc[i].coerceAtLeast(0f).let { if (it <= 0.04045f) it / 12.92f else Math.pow(((it + 0.055) / 1.055), 2.4).toFloat() } }
        val luma = 0.2126f * lin[0] + 0.7152f * lin[1] + 0.0722f * lin[2]
        return FloatArray(3) { i -> 0.2f + 0.8f * lin[i] / luma.coerceAtLeast(1e-4f) }
    }

    @Synchronized private fun handleTap(frame: Frame) {
        val (x, y) = pendingTap ?: return
        pendingTap = null
        val hit = frame.hitTest(x, y).firstOrNull { h ->
            val t = h.trackable
            t is Plane && t.type == Plane.Type.VERTICAL && t.isPoseInPolygon(h.hitPose)
        }
        val plane = (hit?.trackable as? Plane)?.let { it.subsumedBy ?: it }
        if (plane == null) { emit(mapOf("type" to "hint", "code" to "no_wall_here")); return }
        val idx = walls.indexOfFirst { it.plane == plane }
        if (idx >= 0) walls[idx].paint = current
        else { if (walls.size >= MAX_WALLS) walls.removeAt(0); walls.add(Wall(plane, current)) }
        emit(mapOf("type" to "haptic"))
        emitWalls()
    }

    private fun trackPlanes(s: Session) {
        var verticals = 0
        for (p in s.getAllTrackables(Plane::class.java)) {
            if (p.trackingState != TrackingState.TRACKING || p.subsumedBy != null) continue
            if (p.type == Plane.Type.HORIZONTAL_UPWARD_FACING) floorY = min(floorY ?: p.centerPose.ty(), p.centerPose.ty())
            if (p.type == Plane.Type.VERTICAL) verticals++
        }
        // Merged planes: re-point selected walls at the surviving plane.
        synchronized(this) { walls.replaceAll { w -> w.plane.subsumedBy?.let { Wall(it, w.paint) } ?: w } }
        if (verticals > knownWalls) emit(mapOf("type" to "wall_found", "total" to verticals))
        knownWalls = verticals
    }

    private fun reportTracking(camera: Camera) {
        val state = when (camera.trackingState) {
            TrackingState.TRACKING -> "normal"
            TrackingState.STOPPED -> "not_available"
            else -> when (camera.trackingFailureReason) {
                TrackingFailureReason.EXCESSIVE_MOTION -> "move_slower"
                TrackingFailureReason.INSUFFICIENT_FEATURES, TrackingFailureReason.INSUFFICIENT_LIGHT -> "more_light_or_texture"
                else -> "initializing"
            }
        }
        if (state != lastTracking) { lastTracking = state; emit(mapOf("type" to "tracking", "state" to state)) }
    }

    private fun readPixelsPng(): ByteArray? {
        val buf = ByteBuffer.allocateDirect(viewW * viewH * 4).order(ByteOrder.nativeOrder())
        GLES30.glReadPixels(0, 0, viewW, viewH, GLES30.GL_RGBA, GLES30.GL_UNSIGNED_BYTE, buf)
        val bmp = Bitmap.createBitmap(viewW, viewH, Bitmap.Config.ARGB_8888)
        buf.rewind(); bmp.copyPixelsFromBuffer(buf)
        val flipped = Bitmap.createBitmap(bmp, 0, 0, viewW, viewH,
            android.graphics.Matrix().apply { preScale(1f, -1f) }, false)       // GL origin is bottom-left
        return ByteArrayOutputStream().use { flipped.compress(Bitmap.CompressFormat.PNG, 100, it); it.toByteArray() }
    }
}
