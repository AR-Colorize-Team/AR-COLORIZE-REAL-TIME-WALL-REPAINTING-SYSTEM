import 'package:flutter/foundation.dart';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';

/// Embeds the native AR renderer.
/// Android uses Hybrid Composition (`initExpensiveAndroidView`) because the renderer is a
/// GLSurfaceView — texture-layer composition can show black frames with SurfaceViews.
class ArCameraView extends StatelessWidget {
  const ArCameraView({super.key});

  static const viewType = 'arwall/ar_view';

  @override
  Widget build(BuildContext context) {
    switch (defaultTargetPlatform) {
      case TargetPlatform.iOS:
        return const UiKitView(viewType: viewType, creationParamsCodec: StandardMessageCodec());
      case TargetPlatform.android:
        return PlatformViewLink(
          viewType: viewType,
          surfaceFactory: (context, controller) => AndroidViewSurface(
            controller: controller as AndroidViewController,
            gestureRecognizers: const <Factory<OneSequenceGestureRecognizer>>{},
            hitTestBehavior: PlatformViewHitTestBehavior.transparent, // taps handled in Flutter
          ),
          onCreatePlatformView: (params) => PlatformViewsService.initExpensiveAndroidView(
            id: params.id,
            viewType: viewType,
            layoutDirection: TextDirection.ltr,
            creationParamsCodec: const StandardMessageCodec(),
            onFocus: () => params.onFocusChanged(true),
          )
            ..addOnPlatformViewCreatedListener(params.onPlatformViewCreated)
            ..create(),
        );
      default:
        return const ColoredBox(color: Colors.black);
    }
  }
}
