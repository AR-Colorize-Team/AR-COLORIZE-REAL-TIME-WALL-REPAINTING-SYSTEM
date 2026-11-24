#version 300 es
// Full-screen quad. UVs are produced on the CPU each frame by
// Frame.transformCoordinates2d so that the camera texture, the CPU image (mask,
// depth) and the view all line up regardless of device rotation/aspect.
layout(location = 0) in vec2 aNdc;
layout(location = 1) in vec2 aCamUV;
layout(location = 2) in vec2 aImgUV;
out vec2 vViewUV;
out vec2 vCamUV;
out vec2 vImgUV;

void main() {
    vViewUV = vec2(aNdc.x * 0.5 + 0.5, 0.5 - aNdc.y * 0.5);
    vCamUV = aCamUV;
    vImgUV = aImgUV;
    gl_Position = vec4(aNdc, 0.0, 1.0);
}
