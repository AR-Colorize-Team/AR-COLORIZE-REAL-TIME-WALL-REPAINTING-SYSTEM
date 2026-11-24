#version 300 es
// =============================================================================
//  wall_paint.frag — realtime AR wall painting (Android / ARCore, GLES 3.0)
//  1:1 port of engine/reference/paint_math.py and ios/.../WallPaint.metal.
// =============================================================================
#extension GL_OES_EGL_image_external_essl3 : require
precision highp float;

#define MAX_WALLS 4
#define SOLID 0
#define TWO_TONE 1
#define STRIPES_V 2
#define STRIPES_H 3
#define OMBRE 4
#define MOUNTAIN 5
#define ARCH 6
#define COLOR_BLOCK 7

in vec2 vViewUV;     // 0..1 view space, y down
in vec2 vCamUV;      // camera OES texture coords (TEXTURE_NORMALIZED)
in vec2 vImgUV;      // CPU image coords (IMAGE_NORMALIZED) — mask & depth live here
out vec4 fragColor;

uniform samplerExternalOES uCamera;
uniform sampler2D uWallMask;      // R8 wall probability (WallSegmenter)
uniform sampler2D uDepth;         // RG8-packed DEPTH16 millimetres (ARCore Depth API) or 1×1 dummy

uniform mat4  uInvViewProj;
uniform vec3  uCameraPos;
uniform vec3  uLightTint;
uniform float uYRef;
uniform float uOldWallAlbedo;
uniform float uContrastK;
uniform float uRevealX;
uniform int   uWallCount;
uniform int   uHasDepth;
uniform float uDepthTolerance;

uniform mat4  uWorldToPlane[MAX_WALLS];
uniform vec4  uPlane[MAX_WALLS];        // xyz normal, w = d
uniform vec4  uExtent[MAX_WALLS];       // minX, maxX, minZ, maxZ (plane-local metres)
uniform vec3  uColorA[MAX_WALLS];       // linear RGB
uniform vec3  uColorB[MAX_WALLS];
uniform vec4  uParams[MAX_WALLS];
uniform int   uPattern[MAX_WALLS];
uniform vec4  uWallMisc[MAX_WALLS];     // x floorY, y opacity, z specularKeep, w sheen

const vec3 LUMA = vec3(0.2126, 0.7152, 0.0722);

vec3 srgbToLinear(vec3 c) {
    c = clamp(c, 0.0, 1.0);
    return mix(pow((c + 0.055) / 1.055, vec3(2.4)), c / 12.92, vec3(lessThanEqual(c, vec3(0.04045))));
}
vec3 linearToSrgb(vec3 c) {
    c = clamp(c, 0.0, 1.0);
    return mix(1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, c * 12.92, vec3(lessThanEqual(c, vec3(0.0031308))));
}
vec3 softClip(vec3 x) {
    const float knee = 0.8;
    vec3 over = max(x - knee, 0.0);
    return min(x, vec3(knee)) + (1.0 - knee) * (1.0 - exp(-over / (1.0 - knee)));
}
float aaStep(float edge, float x, float px) { return clamp((x - edge) / max(px, 1e-6) * 0.5 + 0.5, 0.0, 1.0); }
float pmod(float x, float m) { return x - m * floor(x / m); }

float patternWeight(int p, float u, float v, vec4 q, float px) {
    if (p == TWO_TONE) return 1.0 - aaStep(q.x > 0.0 ? q.x : 0.95, v, px);
    if (p == STRIPES_V) { float w = q.x > 0.0 ? q.x : 0.30; float t = pmod(u, 2.0 * w);
                          return aaStep(w, t, px) * (1.0 - aaStep(2.0 * w - px, t, px)); }
    if (p == STRIPES_H) { float w = q.x > 0.0 ? q.x : 0.25; float t = pmod(v, 2.0 * w);
                          return aaStep(w, t, px) * (1.0 - aaStep(2.0 * w - px, t, px)); }
    if (p == OMBRE)     { float lo = q.x > 0.0 ? q.x : 0.3, hi = q.y > 0.0 ? q.y : 2.2;
                          float t = clamp((v - lo) / max(hi - lo, 1e-3), 0.0, 1.0);
                          return 1.0 - t * t * (3.0 - 2.0 * t); }
    if (p == MOUNTAIN)  { float period = q.x > 0.0 ? q.x : 1.6, base = q.y > 0.0 ? q.y : 0.9, amp = q.z > 0.0 ? q.z : 0.7;
                          float tri = 1.0 - abs(2.0 * fract(u / period) - 1.0);
                          return 1.0 - aaStep(base + amp * tri, v, px); }
    if (p == ARCH)      { float cx = q.x, r = (q.y > 0.0 ? q.y : 1.4) * 0.5, h = q.z > 0.0 ? q.z : 2.1;
                          float spring = h - r;
                          float rect = (1.0 - aaStep(r, abs(u - cx), px)) * (1.0 - aaStep(spring, v, px));
                          float dome = (1.0 - aaStep(r, distance(vec2(u, v), vec2(cx, spring)), px)) * aaStep(spring, v, px);
                          return clamp(rect + dome, 0.0, 1.0); }
    if (p == COLOR_BLOCK) return aaStep(q.x, u, px) * (1.0 - aaStep(q.y, u, px))
                                 * aaStep(0.0, v, px) * (1.0 - aaStep(q.z > 0.0 ? q.z : 1.8, v, px));
    return 0.0;
}

void main() {
    vec3 cam = texture(uCamera, vCamUV).rgb;
    if (vViewUV.x > uRevealX) { fragColor = vec4(cam, 1.0); return; }   // live before/after split

    float semantic = smoothstep(0.35, 0.65, texture(uWallMask, vImgUV).r);
    if (semantic < 1.0 / 512.0) { fragColor = vec4(cam, 1.0); return; }

    vec2 ndc = vec2(vViewUV.x * 2.0 - 1.0, 1.0 - vViewUV.y * 2.0);
    vec4 farW = uInvViewProj * vec4(ndc, 1.0, 1.0);
    vec3 rayDir = normalize(farW.xyz / farW.w - uCameraPos);

    int best = -1; float bestT = 1e9; vec3 hit = vec3(0); vec3 local = vec3(0);
    for (int i = 0; i < MAX_WALLS; i++) {
        if (i >= uWallCount) break;
        float denom = dot(uPlane[i].xyz, rayDir);
        if (abs(denom) < 1e-4) continue;
        float t = -(dot(uPlane[i].xyz, uCameraPos) + uPlane[i].w) / denom;
        if (t <= 0.0 || t >= bestT) continue;
        vec3 p = uCameraPos + rayDir * t;
        vec3 l = (uWorldToPlane[i] * vec4(p, 1.0)).xyz;
        vec4 e = uExtent[i];
        const float grow = 0.25;
        if (l.x < e.x - grow || l.x > e.y + grow || l.z < e.z - grow || l.z > e.w + grow) continue;
        best = i; bestT = t; hit = p; local = l;
    }
    if (best < 0) { fragColor = vec4(cam, 1.0); return; }

    float coverage = semantic;
    if (uHasDepth != 0) {
        vec2 packed = texture(uDepth, vImgUV).rg;
        float measured = dot(packed, vec2(255.0, 256.0 * 255.0)) / 1000.0;     // mm → m
        if (measured > 0.0) {                                                   // 0 = no estimate
            vec4 fw = uInvViewProj * vec4(0.0, 0.0, 1.0, 1.0);
            vec3 fwd = normalize(fw.xyz / fw.w - uCameraPos);
            float planeDepth = bestT * dot(rayDir, fwd);
            coverage *= 1.0 - smoothstep(uDepthTolerance, uDepthTolerance * 2.0, planeDepth - measured);
        }
    }
    vec4 misc = uWallMisc[best];
    coverage *= misc.y;
    if (coverage < 1.0 / 512.0) { fragColor = vec4(cam, 1.0); return; }

    float u = local.x - uExtent[best].x;
    float v = hit.y - misc.x;
    float px = max(fwidth(u), fwidth(v));
    float wB = uPattern[best] == SOLID ? 0.0 : patternWeight(uPattern[best], u, v, uParams[best], px);
    vec3 paint = mix(uColorA[best], uColorB[best], wB);

    vec3 lin = srgbToLinear(cam);
    float yRef = max(uYRef, 1e-4);
    float shading = dot(lin, LUMA) / yRef;
    float paintY = dot(paint, LUMA);
    vec3 direction = paintY > 1e-5 ? paint / paintY : vec3(0.0);
    float level = yRef * pow(max(paintY, 0.0) / uOldWallAlbedo, uContrastK);
    vec3 outLin = direction * level * uLightTint * shading;
    outLin += (misc.z + misc.w) * max(shading - 1.3, 0.0) * yRef;

    fragColor = vec4(mix(cam, linearToSrgb(softClip(outLin)), coverage), 1.0);
}
