// =============================================================================
//  WallPaint.metal — realtime AR wall painting (iOS / ARKit)
//
//  1:1 port of engine/reference/paint_math.py (the tested golden model).
//  One full-screen pass per frame:
//    camera YCbCr ─► sRGB ─► which wall does this pixel belong to?
//                              • semantic wall mask (Core ML segmenter, doors/windows/switches = 0)
//                              • ray–plane hit inside the tapped wall's extent
//                              • LiDAR depth agrees with the plane (furniture in front is excluded)
//                              • not a person (ARKit person segmentation)
//                         ─► world-locked pattern colour at that wall point (metres)
//                         ─► lighting-preserving recolour ─► blend
// =============================================================================
#include <metal_stdlib>
using namespace metal;

constant int MAX_WALLS = 4;
constant float3 LUMA = float3(0.2126, 0.7152, 0.0722);

// Pattern ids — keep in sync with PaintPattern (Dart) and paint_math.PATTERNS
enum Pattern : int { SOLID = 0, TWO_TONE, STRIPES_V, STRIPES_H, OMBRE, MOUNTAIN, ARCH, COLOR_BLOCK };

struct WallUniform {
    float4x4 worldToPlane;   // inverse of the ARPlaneAnchor transform (plane local: x along, z across, y = normal)
    float4   planeNormal;    // xyz world normal, w = d   (n·p + d = 0)
    float4   extent;         // plane-local minX, maxX, minZ, maxZ (metres, from ARPlaneAnchor.planeExtent/geometry)
    float4   colorA;         // linear RGB, w unused
    float4   colorB;
    float4   params;         // pattern parameters (metres), meaning depends on pattern
    float    floorY;         // world Y of the floor (from the horizontal plane), for v = height above floor
    float    opacity;
    int      pattern;
    float    specularKeep;   // finish
    float    sheen;          // finish
    int      active;
    float2   _pad;
};

struct FrameUniforms {
    float4x4 invViewProj;    // NDC → world
    float4   cameraPos;      // world
    float3   lightTint;      // linear RGB, luminance-normalised (ARKit light estimate / reference estimator)
    float    yRef;           // wall reference luminance (EMA-smoothed, from WallSegmenter)
    float    oldWallAlbedo;  // 0.65 default; refined by AI analysis
    float    contrastK;      // 0.85
    float    revealX;        // before/after split in view space 0..1 (≥1 = all painted)
    int      wallCount;
    int      hasDepth;       // LiDAR sceneDepth available
    int      hasPersonMask;
    float    depthTolerance; // metres (0.08)
    float    _pad;
};

struct VertexOut {
    float4 position [[position]];
    float2 viewUV;   // 0..1 in view space
    float2 camUV;    // 0..1 in captured-image space (via ARFrame.displayTransform)
};

// Full-screen triangle strip. camUV is precomputed on the CPU per vertex from
// displayTransform so camera, mask and depth textures (all image-space) line up.
vertex VertexOut wallPaintVertex(uint vid [[vertex_id]],
                                 constant float4 *quad [[buffer(0)]])   // xy = NDC, zw = camUV
{
    VertexOut o;
    o.position = float4(quad[vid].xy, 0, 1);
    o.viewUV = float2(quad[vid].x * 0.5 + 0.5, 0.5 - quad[vid].y * 0.5);
    o.camUV = quad[vid].zw;
    return o;
}

// ---------------------------------------------------------------- colour helpers
static float3 srgbToLinear(float3 c) {
    c = saturate(c);
    return select(pow((c + 0.055) / 1.055, 2.4), c / 12.92, c <= 0.04045);
}
static float3 linearToSrgb(float3 c) {
    c = saturate(c);
    return select(1.055 * pow(c, 1.0 / 2.4) - 0.055, c * 12.92, c <= 0.0031308);
}
static float3 softClip(float3 x) {               // knee 0.8, matches paint_math.soft_clip
    const float knee = 0.8;
    float3 over = max(x - knee, 0.0);
    return min(x, knee) + (1.0 - knee) * (1.0 - exp(-over / (1.0 - knee)));
}
static float3 ycbcrToSrgb(float y, float2 cbcr) {  // BT.601 full range (ARKit capturedImage)
    const float4x4 m = float4x4(float4(+1.0000f, +1.0000f, +1.0000f, +0.0000f),
                                float4(+0.0000f, -0.3441f, +1.7720f, +0.0000f),
                                float4(+1.4020f, -0.7141f, +0.0000f, +0.0000f),
                                float4(-0.7010f, +0.5291f, -0.8860f, +1.0000f));
    return saturate((m * float4(y, cbcr, 1.0)).rgb);
}

// ---------------------------------------------------------------- patterns
static float aaStep(float edge, float x, float px) {
    return saturate((x - edge) / max(px, 1e-6) * 0.5 + 0.5);
}

static float patternWeight(int p, float u, float v, float4 q, float px) {
    switch (p) {
        case TWO_TONE:   return 1.0 - aaStep(q.x > 0 ? q.x : 0.95, v, px);
        case STRIPES_V: { float w = q.x > 0 ? q.x : 0.30; float t = fmod(fmod(u, 2*w) + 2*w, 2*w);
                          return aaStep(w, t, px) * (1.0 - aaStep(2*w - px, t, px)); }
        case STRIPES_H: { float w = q.x > 0 ? q.x : 0.25; float t = fmod(fmod(v, 2*w) + 2*w, 2*w);
                          return aaStep(w, t, px) * (1.0 - aaStep(2*w - px, t, px)); }
        case OMBRE:     { float lo = q.x > 0 ? q.x : 0.3, hi = q.y > 0 ? q.y : 2.2;
                          float t = saturate((v - lo) / max(hi - lo, 1e-3));
                          return 1.0 - t * t * (3.0 - 2.0 * t); }
        case MOUNTAIN:  { float period = q.x > 0 ? q.x : 1.6, base = q.y > 0 ? q.y : 0.9, amp = q.z > 0 ? q.z : 0.7;
                          float f = fract(u / period); float tri = 1.0 - abs(2.0 * f - 1.0);
                          return 1.0 - aaStep(base + amp * tri, v, px); }
        case ARCH:      { float cx = q.x, r = (q.y > 0 ? q.y : 1.4) * 0.5, h = q.z > 0 ? q.z : 2.1;
                          float spring = h - r;
                          float rect = (1.0 - aaStep(r, abs(u - cx), px)) * (1.0 - aaStep(spring, v, px));
                          float dome = (1.0 - aaStep(r, distance(float2(u, v), float2(cx, spring)), px)) * aaStep(spring, v, px);
                          return saturate(rect + dome); }
        case COLOR_BLOCK: return aaStep(q.x, u, px) * (1.0 - aaStep(q.y, u, px))
                                 * aaStep(0.0, v, px) * (1.0 - aaStep(q.z > 0 ? q.z : 1.8, v, px));
        default:         return 0.0;
    }
}

// ---------------------------------------------------------------- main pass
fragment float4 wallPaintFragment(VertexOut in [[stage_in]],
                                  texture2d<float> camY        [[texture(0)]],
                                  texture2d<float> camCbCr     [[texture(1)]],
                                  texture2d<float> wallMask    [[texture(2)]],   // R8, 0..1 wall probability
                                  texture2d<float> sceneDepth  [[texture(3)]],   // R32F metres (LiDAR) or 1×1 dummy
                                  texture2d<float> personMask  [[texture(4)]],   // R8 or 1×1 dummy
                                  constant FrameUniforms &F    [[buffer(0)]],
                                  constant WallUniform *walls  [[buffer(1)]])
{
    constexpr sampler s(filter::linear, address::clamp_to_edge);
    float3 cam = ycbcrToSrgb(camY.sample(s, in.camUV).r, camCbCr.sample(s, in.camUV).rg);

    // Live before/after comparison: left of the split shows the real wall.
    if (in.viewUV.x > F.revealX) return float4(cam, 1);

    float semantic = smoothstep(0.35, 0.65, wallMask.sample(s, in.camUV).r);
    if (F.hasPersonMask != 0) semantic *= 1.0 - personMask.sample(s, in.camUV).r;
    if (semantic < 1.0 / 512.0) return float4(cam, 1);           // early-out: off-wall pixels are bit-exact

    // World-space ray through this pixel.
    float2 ndc = float2(in.viewUV.x * 2 - 1, 1 - in.viewUV.y * 2);
    float4 farW = F.invViewProj * float4(ndc, 1, 1);
    float3 rayDir = normalize(farW.xyz / farW.w - F.cameraPos.xyz);

    // Nearest selected wall whose plane this ray hits inside its extent.
    int best = -1; float bestT = 1e9; float3 hit = 0; float3 local = 0;
    for (int i = 0; i < MAX_WALLS; i++) {
        if (i >= F.wallCount || walls[i].active == 0) continue;
        float denom = dot(walls[i].planeNormal.xyz, rayDir);
        if (abs(denom) < 1e-4) continue;
        float t = -(dot(walls[i].planeNormal.xyz, F.cameraPos.xyz) + walls[i].planeNormal.w) / denom;
        if (t <= 0 || t >= bestT) continue;
        float3 p = F.cameraPos.xyz + rayDir * t;
        float3 l = (walls[i].worldToPlane * float4(p, 1)).xyz;
        float4 e = walls[i].extent;
        const float grow = 0.25;  // ARKit extents lag the real wall; semantic mask trims the excess
        if (l.x < e.x - grow || l.x > e.y + grow || l.z < e.z - grow || l.z > e.w + grow) continue;
        best = i; bestT = t; hit = p; local = l;
    }
    if (best < 0) return float4(cam, 1);
    WallUniform w = walls[best];

    // LiDAR: anything measurably in front of the wall (sofa, lamp, plant) keeps its pixels.
    float coverage = semantic;
    if (F.hasDepth != 0) {
        float measured = sceneDepth.sample(s, in.camUV).r;          // metres along camera forward
        float3 fwd = normalize((F.invViewProj * float4(0, 0, 1, 1)).xyz - F.cameraPos.xyz);
        float planeDepth = bestT * dot(rayDir, fwd);
        coverage *= 1.0 - smoothstep(F.depthTolerance, F.depthTolerance * 2.0, planeDepth - measured);
    }
    coverage *= w.opacity;
    if (coverage < 1.0 / 512.0) return float4(cam, 1);

    // Wall coordinates in metres: u along the wall, v = height above the floor.
    float u = local.x - w.extent.x;
    float v = hit.y - w.floorY;
    float px = max(fwidth(u), fwidth(v));
    float wB = (w.pattern == SOLID) ? 0.0 : patternWeight(w.pattern, u, v, w.params, px);
    float3 paint = mix(w.colorA.rgb, w.colorB.rgb, wB);

    // ---- lighting-preserving recolour (paint_math.recolor) ----
    float3 lin = srgbToLinear(cam);
    float yRef = max(F.yRef, 1e-4);
    float shading = dot(lin, LUMA) / yRef;
    float paintY = dot(paint, LUMA);
    float3 direction = paintY > 1e-5 ? paint / paintY : float3(0);
    float level = yRef * pow(max(paintY, 0.0) / F.oldWallAlbedo, F.contrastK);
    float3 outLin = direction * level * F.lightTint * shading;
    outLin += (w.specularKeep + w.sheen) * max(shading - 1.3, 0.0) * yRef;

    float3 painted = linearToSrgb(softClip(outLin));
    return float4(mix(cam, painted, coverage), 1);
}
