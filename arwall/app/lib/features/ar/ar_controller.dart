import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import '../designs/designs.dart';

/// Guidance shown to the user, derived from native tracking events. Each maps to one
/// short, friendly line — the app never shows raw AR jargon.
enum CoachState {
  starting('Getting ready…'),
  scan('Slowly move your phone to scan the room'),
  moveSlower('A little slower'),
  needLight('Turn on a light or point at a wall with more detail'),
  tapWall('Tap a wall to paint it'),
  painting(''),
  interrupted('Paused — come back to continue');

  const CoachState(this.message);
  final String message;
}

class PaintedWall {
  PaintedWall(this.index, this.areaM2, this.look);
  final int index;
  final double areaM2;
  WallLook look;
}

/// Single source of truth for the live AR screen. Talks to ARWallPlugin (iOS) /
/// ArWallPlugin (Android) over identical channels.
class ArController extends ChangeNotifier {
  ArController({double devicePixelRatio = 1}) : _dpr = devicePixelRatio {
    _sub = _events.receiveBroadcastStream().listen(_onEvent, onError: (_) {});
  }

  static const _methods = MethodChannel('arwall/ar');
  static const _events = EventChannel('arwall/ar/events');

  final double _dpr;
  late final StreamSubscription _sub;

  CoachState coach = CoachState.starting;
  bool hasLiDAR = false;
  bool hasDepth = false;
  int wallsFound = 0;
  final List<PaintedWall> walls = [];
  int? selectedWall;
  WallLook look = const WallLook(colorA: '#9CAF88', nameA: 'Sage');
  double reveal = 1.0;                       // 1 = fully painted; < 1 = before/after split
  String? transientHint;                     // short toast ("That's a door")
  final List<WallLook> _undo = [];

  static Future<bool> isSupported() async {
    try {
      final r = await _methods.invokeMapMethod<String, Object?>('isSupported');
      return r?['ar'] == true;
    } on PlatformException {
      return false;
    } on MissingPluginException {
      return false;
    }
  }

  static Future<bool> ensureInstalled() async {
    if (defaultTargetPlatform != TargetPlatform.android) return true;
    return await _methods.invokeMethod<bool>('ensureInstalled') ?? false;
  }

  // ------------------------------------------------------------------ user actions
  Future<void> tapAt(Offset localPosition) async {
    HapticFeedback.selectionClick();
    await _methods.invokeMethod('selectWallAt', {'x': localPosition.dx, 'y': localPosition.dy, 'dpr': _dpr});
  }

  Future<void> applyLook(WallLook next, {bool recordUndo = true}) async {
    if (recordUndo) _undo.add(look);
    if (_undo.length > 30) _undo.removeAt(0);
    look = next;
    notifyListeners();
    await _methods.invokeMethod('setPaint', {...next.toChannel(), 'wallIndex': selectedWall});
  }

  Future<void> pickColor(String hex, {String? name}) => look.pattern.usesSecondColor
      ? applyLook(look.copyWith(colorB: hex, nameB: name))      // patterns: dock edits the accent colour
      : applyLook(look.copyWith(colorA: hex, nameA: name));

  Future<void> pickPattern(PaintPattern p) => applyLook(look.copyWith(
        pattern: p,
        colorB: p.usesSecondColor ? (look.colorB ?? '#D9CFC1') : null,
      ));

  Future<void> pickFinish(Finish f) => applyLook(look.copyWith(finish: f));

  Future<void> applyPreset(StylePreset preset) async {
    if (walls.isEmpty) await _methods.invokeMethod('selectAllWalls');
    await applyLook(preset.look);
  }

  Future<void> undo() async {
    if (_undo.isEmpty) return;
    HapticFeedback.lightImpact();
    await applyLook(_undo.removeLast(), recordUndo: false);
  }

  bool get canUndo => _undo.isNotEmpty;

  Future<void> paintAllWalls() => _methods.invokeMethod('selectAllWalls');
  Future<void> clearWalls() => _methods.invokeMethod('clearWalls');

  void setReveal(double x) {
    reveal = x.clamp(0.0, 1.0);
    notifyListeners();
    _methods.invokeMethod('setReveal', {'x': reveal});
  }

  /// AI analysis estimates the current wall colour; feeding its reflectance in
  /// makes dark/light paint brightness physically accurate for this room.
  Future<void> setCurrentWallLuminance(double lrv) =>
      _methods.invokeMethod('setOldWallAlbedo', {'value': (lrv / 100).clamp(0.05, 0.95)});

  Future<Uint8List?> capture() async {
    HapticFeedback.mediumImpact();
    return _methods.invokeMethod<Uint8List>('capture');
  }

  Future<void> pause() => _methods.invokeMethod('pause');
  Future<void> resume() => _methods.invokeMethod('resume');

  /// Estimated paint needed: area × 2 coats ÷ ~10 m²/L coverage, +10% wastage.
  double get litresNeeded =>
      walls.fold<double>(0, (sum, w) => sum + w.areaM2) * 2 / 10 * 1.1;

  // ------------------------------------------------------------------ native events
  void _onEvent(dynamic raw) {
    final e = Map<String, Object?>.from(raw as Map);
    switch (e['type']) {
      case 'capabilities':
        hasLiDAR = e['lidar'] == true;
        hasDepth = e['depth'] == true || hasLiDAR;
      case 'tracking':
        coach = switch (e['state']) {
          'normal' => walls.isEmpty ? (wallsFound > 0 ? CoachState.tapWall : CoachState.scan) : CoachState.painting,
          'move_slower' => CoachState.moveSlower,
          'more_light_or_texture' => CoachState.needLight,
          'interrupted' || 'relocalizing' => CoachState.interrupted,
          _ => CoachState.scan,
        };
      case 'wall_found':
        wallsFound = (e['total'] as num).toInt();
        if (walls.isEmpty) {
          coach = CoachState.tapWall;
          HapticFeedback.lightImpact();
        }
      case 'walls':
        final list = (e['walls'] as List).cast<Map>();
        walls
          ..clear()
          ..addAll(list.map((w) => PaintedWall(
                (w['index'] as num).toInt(), (w['area_m2'] as num).toDouble(), look)));
        coach = walls.isEmpty ? CoachState.tapWall : CoachState.painting;
      case 'hint':
        transientHint = switch (e['code']) {
          'not_a_wall' => 'That looks like a door or window',
          'no_wall_here' => 'Point at a wall and tap again',
          _ => null,
        };
        Future.delayed(const Duration(seconds: 2), () {
          transientHint = null;
          notifyListeners();
        });
      case 'haptic':
        HapticFeedback.mediumImpact();
    }
    notifyListeners();
  }

  @override
  void dispose() {
    _sub.cancel();
    super.dispose();
  }
}
