import 'package:flutter/material.dart';

/// Wall paint patterns. `id` order MUST match the shader enums
/// (WallPaint.metal / wall_paint.frag) and engine/reference/paint_math.PATTERNS.
enum PaintPattern {
  solid(0, 'Solid', 'One colour, wall to wall'),
  twoTone(1, 'Two-tone', 'Classic lower band, chair-rail height'),
  stripesVertical(2, 'Vertical stripes', 'Adds height to low ceilings'),
  stripesHorizontal(3, 'Horizontal stripes', 'Makes narrow rooms feel wider'),
  ombre(4, 'Ombré', 'Soft fade from ceiling to floor'),
  mountain(5, 'Mountain', 'Geometric peaks — great for kids’ rooms'),
  arch(6, 'Arch', 'Painted arch behind a bed or desk'),
  colorBlock(7, 'Colour block', 'Bold rectangle to zone a space');

  const PaintPattern(this.id, this.label, this.hint);
  final int id;
  final String label;
  final String hint;

  bool get usesSecondColor => this != PaintPattern.solid;

  /// Default geometry in metres (shader params x, y, z, w). Users drag to adjust.
  List<double> get defaultParams => switch (this) {
        PaintPattern.twoTone => [0.95, 0, 0, 0],
        PaintPattern.stripesVertical => [0.30, 0, 0, 0],
        PaintPattern.stripesHorizontal => [0.25, 0, 0, 0],
        PaintPattern.ombre => [0.3, 2.2, 0, 0],
        PaintPattern.mountain => [1.6, 0.9, 0.7, 0],
        PaintPattern.arch => [1.5, 1.4, 2.1, 0],      // centre, width, height
        PaintPattern.colorBlock => [0.4, 1.6, 1.8, 0], // x0, x1, height
        PaintPattern.solid => [0, 0, 0, 0],
      };
}

enum Finish {
  matte('matte', 'Matte', 'Hides imperfections · living & bedrooms'),
  eggshell('eggshell', 'Eggshell', 'Soft glow · easy to wipe'),
  satin('satin', 'Satin', 'Durable · hallways & kids’ rooms'),
  semiGloss('semi_gloss', 'Semi-gloss', 'Moisture-proof · kitchens & baths');

  const Finish(this.key, this.label, this.hint);
  final String key;
  final String label;
  final String hint;
}

/// What gets sent to the AR engine for one wall.
@immutable
class WallLook {
  const WallLook({
    required this.colorA,
    this.colorB,
    this.pattern = PaintPattern.solid,
    this.finish = Finish.matte,
    this.params,
    this.opacity = 1,
    this.nameA,
    this.nameB,
  });

  final String colorA;           // '#RRGGBB'
  final String? colorB;
  final PaintPattern pattern;
  final Finish finish;
  final List<double>? params;
  final double opacity;
  final String? nameA;           // e.g. "Sage Whisper · AP 7042"
  final String? nameB;

  WallLook copyWith({String? colorA, String? colorB, PaintPattern? pattern, Finish? finish,
          List<double>? params, double? opacity, String? nameA, String? nameB}) =>
      WallLook(
        colorA: colorA ?? this.colorA,
        colorB: colorB ?? this.colorB,
        pattern: pattern ?? this.pattern,
        finish: finish ?? this.finish,
        params: params ?? (pattern != null ? pattern.defaultParams : this.params),
        opacity: opacity ?? this.opacity,
        nameA: nameA ?? this.nameA,
        nameB: nameB ?? this.nameB,
      );

  Map<String, Object?> toChannel() => {
        'colorA': colorA,
        'colorB': colorB ?? colorA,
        'pattern': pattern.id,
        'params': params ?? pattern.defaultParams,
        'finish': finish.key,
        'opacity': opacity,
      };
}

/// Whole-room style presets: one tap paints every selected wall with a curated look.
@immutable
class StylePreset {
  const StylePreset(this.name, this.mood, this.walls, this.trim, this.ceiling, this.look);
  final String name;
  final String mood;
  final String walls;
  final String trim;
  final String ceiling;
  final WallLook look;

  List<String> get swatches => [walls, look.colorB ?? walls, trim, ceiling];
}

const stylePresets = <StylePreset>[
  StylePreset('Scandinavian Calm', 'Airy · restful', '#E9E4DA', '#F7F5F0', '#FBFAF7',
      WallLook(colorA: '#E9E4DA')),
  StylePreset('Modern Luxe', 'Moody · dramatic', '#2B2F36', '#1F2226', '#F2EFEA',
      WallLook(colorA: '#2B2F36', finish: Finish.eggshell)),
  StylePreset('Japandi', 'Grounded · minimal', '#D9CFC1', '#5B4E44', '#F4F0EA',
      WallLook(colorA: '#D9CFC1', colorB: '#8C7B6B', pattern: PaintPattern.twoTone)),
  StylePreset('Warm Terracotta', 'Sunny · sociable', '#C46A4A', '#EFE6D8', '#F7F2EA',
      WallLook(colorA: '#EFE6D8', colorB: '#C46A4A', pattern: PaintPattern.arch)),
  StylePreset('Coastal', 'Fresh · light', '#C9D8DE', '#F6F6F2', '#FFFFFF',
      WallLook(colorA: '#F6F6F2', colorB: '#9FB7C3', pattern: PaintPattern.stripesVertical)),
  StylePreset('Forest Retreat', 'Natural · cocooning', '#3F5E4B', '#E8DFD0', '#F3EFE7',
      WallLook(colorA: '#E8DFD0', colorB: '#3F5E4B', pattern: PaintPattern.mountain)),
  StylePreset('Dusk Ombré', 'Dreamy · soft', '#7A6C9B', '#F4E9DC', '#F8F3EC',
      WallLook(colorA: '#F4E9DC', colorB: '#7A6C9B', pattern: PaintPattern.ombre)),
];

/// Curated quick palette for the colour dock (replace with catalog API results).
const quickColors = <(String, String)>[
  ('#F2EEE6', 'Linen'), ('#E9E4DA', 'Oat'), ('#D9CFC1', 'Sandstone'), ('#9CAF88', 'Sage'),
  ('#6E7F64', 'Olive'), ('#3F5E4B', 'Forest'), ('#C9D8DE', 'Mist'), ('#7FA1B3', 'Harbour'),
  ('#1F2A44', 'Midnight'), ('#D8A7A0', 'Blush'), ('#C46A4A', 'Terracotta'), ('#E0B65C', 'Ochre'),
  ('#7A6C9B', 'Heather'), ('#2B2F36', 'Graphite'),
];

Color hexColor(String hex) => Color(int.parse('FF${hex.substring(1)}', radix: 16));
