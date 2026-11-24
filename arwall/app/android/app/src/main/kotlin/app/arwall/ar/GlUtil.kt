package app.arwall.ar

import android.content.res.AssetManager
import android.opengl.GLES30
import android.util.Log

/** Small GLES 3 helpers: shader compile/link with readable errors, cached uniform setters. */
object GlUtil {
    private val locations = HashMap<Pair<Int, String>, Int>()

    fun program(assets: AssetManager, vertPath: String, fragPath: String): Int {
        val vs = compile(GLES30.GL_VERTEX_SHADER, assets.open(vertPath).bufferedReader().readText())
        val fs = compile(GLES30.GL_FRAGMENT_SHADER, assets.open(fragPath).bufferedReader().readText())
        val p = GLES30.glCreateProgram()
        GLES30.glAttachShader(p, vs); GLES30.glAttachShader(p, fs)
        GLES30.glBindAttribLocation(p, 0, "aNdc")
        GLES30.glBindAttribLocation(p, 1, "aCamUV")
        GLES30.glBindAttribLocation(p, 2, "aImgUV")
        GLES30.glLinkProgram(p)
        val ok = IntArray(1); GLES30.glGetProgramiv(p, GLES30.GL_LINK_STATUS, ok, 0)
        check(ok[0] != 0) { "link failed: ${GLES30.glGetProgramInfoLog(p)}" }
        return p
    }

    private fun compile(type: Int, src: String): Int {
        val s = GLES30.glCreateShader(type)
        GLES30.glShaderSource(s, src); GLES30.glCompileShader(s)
        val ok = IntArray(1); GLES30.glGetShaderiv(s, GLES30.GL_COMPILE_STATUS, ok, 0)
        if (ok[0] == 0) {
            val log = GLES30.glGetShaderInfoLog(s)
            Log.e("ArWall", "shader compile failed: $log")
            error("shader compile failed: $log")
        }
        return s
    }

    fun linearClamp(target: Int) {
        GLES30.glTexParameteri(target, GLES30.GL_TEXTURE_MIN_FILTER, GLES30.GL_LINEAR)
        GLES30.glTexParameteri(target, GLES30.GL_TEXTURE_MAG_FILTER, GLES30.GL_LINEAR)
        GLES30.glTexParameteri(target, GLES30.GL_TEXTURE_WRAP_S, GLES30.GL_CLAMP_TO_EDGE)
        GLES30.glTexParameteri(target, GLES30.GL_TEXTURE_WRAP_T, GLES30.GL_CLAMP_TO_EDGE)
    }

    private fun loc(p: Int, name: String) = locations.getOrPut(p to name) { GLES30.glGetUniformLocation(p, name) }

    fun bindTex(p: Int, name: String, unit: Int, target: Int, tex: Int) {
        GLES30.glActiveTexture(GLES30.GL_TEXTURE0 + unit)
        GLES30.glBindTexture(target, tex)
        GLES30.glUniform1i(loc(p, name), unit)
    }
    fun mat4(p: Int, name: String, m: FloatArray) = GLES30.glUniformMatrix4fv(loc(p, name), 1, false, m, 0)
    fun vec3(p: Int, name: String, vararg v: Float) = GLES30.glUniform3f(loc(p, name), v[0], v[1], v[2])
    fun vec4(p: Int, name: String, vararg v: Float) = GLES30.glUniform4f(loc(p, name), v[0], v[1], v[2], v[3])
    fun float(p: Int, name: String, v: Float) = GLES30.glUniform1f(loc(p, name), v)
    fun int(p: Int, name: String, v: Int) = GLES30.glUniform1i(loc(p, name), v)
}
