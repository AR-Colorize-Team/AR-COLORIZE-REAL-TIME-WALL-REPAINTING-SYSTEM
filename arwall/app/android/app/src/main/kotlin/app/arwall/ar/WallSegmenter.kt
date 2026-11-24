package app.arwall.ar

import android.content.Context
import android.media.Image
import org.tensorflow.lite.Interpreter
import org.tensorflow.lite.gpu.CompatibilityList
import org.tensorflow.lite.gpu.GpuDelegate
import java.io.FileInputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.MappedByteBuffer
import java.nio.channels.FileChannel
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.pow

/**
 * On-device semantic wall mask (same model family as iOS, exported to TFLite FP16:
 * model export pipeline, Part 3). Input 384×288 RGB, output 1×288×384 P(wall) with
 * doors, windows, switches, furniture, artwork and people suppressed.
 *
 * Runs on the GPU delegate (~12–20 ms on Snapdragon 7-series), off the GL thread.
 * The GL thread only uploads the latest finished mask.
 */
class WallSegmenter(context: Context) {
    companion object {
        const val IN_W = 384
        const val IN_H = 288
    }

    class Output(val mask: ByteArray, val width: Int, val height: Int, val yRef: Float)

    private val executor = Executors.newSingleThreadExecutor()
    private val busy = AtomicBoolean(false)
    private val gpu: GpuDelegate?
    private val interpreter: Interpreter
    private val input = ByteBuffer.allocateDirect(IN_W * IN_H * 3 * 4).order(ByteOrder.nativeOrder())
    private val output = ByteBuffer.allocateDirect(IN_W * IN_H * 4).order(ByteOrder.nativeOrder())
    private val luma = FloatArray(IN_W * IN_H)
    private var smoothed = FloatArray(IN_W * IN_H)
    private var hasPrevious = false
    private var yRefEma = 0.18f

    @Volatile var latest: Output? = null; private set
    @Volatile var lastInferenceMs = 0L; private set

    init {
        val options = Interpreter.Options()
        val compat = CompatibilityList()
        gpu = if (compat.isDelegateSupportedOnThisDevice) GpuDelegate(compat.bestOptionsForThisDevice) else null
        if (gpu != null) options.addDelegate(gpu) else options.setNumThreads(4)
        interpreter = Interpreter(loadModel(context, "wall_segmenter_fp16.tflite"), options)
    }

    private fun loadModel(context: Context, name: String): MappedByteBuffer {
        val fd = context.assets.openFd(name)
        FileInputStream(fd.fileDescriptor).channel.use { ch ->
            return ch.map(FileChannel.MapMode.READ_ONLY, fd.startOffset, fd.declaredLength)
        }
    }

    /**
     * Called on the GL thread with frame.acquireCameraImage(). Converts YUV_420_888 to the
     * model input synchronously (cheap, nearest-neighbour downscale) so the Image can be
     * closed immediately, then runs inference asynchronously. Drops frames while busy.
     */
    fun submit(image: Image) {
        if (!busy.compareAndSet(false, true)) return
        fillInput(image)
        executor.execute {
            val start = System.nanoTime()
            try {
                output.rewind()
                interpreter.run(input, output)
                consume()
            } finally {
                lastInferenceMs = (System.nanoTime() - start) / 1_000_000
                busy.set(false)
            }
        }
    }

    private fun fillInput(image: Image) {
        val yP = image.planes[0]; val uP = image.planes[1]; val vP = image.planes[2]
        val w = image.width; val h = image.height
        input.rewind()
        for (oy in 0 until IN_H) {
            val sy = oy * h / IN_H
            for (ox in 0 until IN_W) {
                val sx = ox * w / IN_W
                val y = (yP.buffer.get(sy * yP.rowStride + sx * yP.pixelStride).toInt() and 0xFF).toFloat()
                val ci = (sy / 2) * uP.rowStride + (sx / 2) * uP.pixelStride
                val u = (uP.buffer.get(ci).toInt() and 0xFF) - 128f
                val v = (vP.buffer.get((sy / 2) * vP.rowStride + (sx / 2) * vP.pixelStride).toInt() and 0xFF) - 128f
                // BT.601 full range → RGB, then ImageNet normalisation expected by the model.
                val r = ((y + 1.402f * v) / 255f).coerceIn(0f, 1f)
                val g = ((y - 0.344136f * u - 0.714136f * v) / 255f).coerceIn(0f, 1f)
                val b = ((y + 1.772f * u) / 255f).coerceIn(0f, 1f)
                input.putFloat((r - 0.485f) / 0.229f)
                input.putFloat((g - 0.456f) / 0.224f)
                input.putFloat((b - 0.406f) / 0.225f)
                luma[oy * IN_W + ox] = y / 255f
            }
        }
        input.rewind()
    }

    private fun consume() {
        output.rewind()
        val n = IN_W * IN_H
        val a = 0.35f                                    // temporal smoothing (matches iOS)
        val hist = IntArray(256)
        var count = 0
        val bytes = ByteArray(n)
        for (i in 0 until n) {
            val p = output.float
            smoothed[i] = if (hasPrevious) a * smoothed[i] + (1 - a) * p else p
            bytes[i] = (smoothed[i].coerceIn(0f, 1f) * 255f).toInt().toByte()
            if (smoothed[i] > 0.6f) { hist[(luma[i] * 255).toInt().coerceIn(0, 255)]++; count++ }
        }
        hasPrevious = true
        if (count > 200) {
            var acc = 0; var median = 0
            for (i in 0 until 256) { acc += hist[i]; if (acc >= count / 2) { median = i; break } }
            val yv = median / 255f
            val lin = if (yv <= 0.04045f) yv / 12.92f else ((yv + 0.055f) / 1.055f).pow(2.4f)
            yRefEma = 0.85f * yRefEma + 0.15f * lin
        }
        latest = Output(bytes, IN_W, IN_H, yRefEma)
    }

    fun close() {
        executor.shutdown()
        interpreter.close()
        gpu?.close()
    }
}
