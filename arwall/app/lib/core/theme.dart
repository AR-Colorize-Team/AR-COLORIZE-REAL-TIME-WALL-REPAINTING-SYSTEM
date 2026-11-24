import 'dart:ui';

import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

/// Design tokens — the same values drive the screen designs on the Design canvas.
/// Premium = restraint: warm neutrals, one brass accent, generous space, a serif for
/// moments of delight (room names, palette names), Inter for everything functional.
abstract final class AppColors {
  static const ink = Color(0xFF161514);       // primary text, primary buttons
  static const ivory = Color(0xFFF7F3ED);     // app background
  static const stone = Color(0xFFE9E2D8);     // cards, chips
  static const line = Color(0xFFDCD3C6);      // hairlines
  static const muted = Color(0xFF6B655D);     // secondary text
  static const brass = Color(0xFFA9834C);     // single accent: Pro, highlights, focus
  static const sage = Color(0xFF6E7F64);      // success / "good match"
  static const glass = Color(0x99161514);     // dark glass over the live camera
  static const glassLight = Color(0x33FFFFFF);
}

abstract final class AppRadius {
  static const sm = 10.0;
  static const md = 16.0;
  static const lg = 24.0;
  static const pill = 999.0;
}

ThemeData buildTheme() {
  final base = ThemeData(useMaterial3: true, brightness: Brightness.light);
  final ui = GoogleFonts.interTextTheme(base.textTheme);
  return base.copyWith(
    scaffoldBackgroundColor: AppColors.ivory,
    colorScheme: ColorScheme.fromSeed(
      seedColor: AppColors.brass,
      surface: AppColors.ivory,
      primary: AppColors.ink,
      secondary: AppColors.brass,
    ),
    textTheme: ui.copyWith(
      displaySmall: GoogleFonts.fraunces(fontSize: 34, height: 1.1, fontWeight: FontWeight.w500, color: AppColors.ink),
      headlineSmall: GoogleFonts.fraunces(fontSize: 24, height: 1.2, fontWeight: FontWeight.w500, color: AppColors.ink),
      titleMedium: ui.titleMedium?.copyWith(fontWeight: FontWeight.w600, color: AppColors.ink),
      bodyMedium: ui.bodyMedium?.copyWith(color: AppColors.ink, height: 1.45),
      bodySmall: ui.bodySmall?.copyWith(color: AppColors.muted),
      labelLarge: ui.labelLarge?.copyWith(fontWeight: FontWeight.w600, letterSpacing: 0.2),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: AppColors.ink,
        foregroundColor: AppColors.ivory,
        minimumSize: const Size.fromHeight(54),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(AppRadius.pill)),
      ),
    ),
    pageTransitionsTheme: const PageTransitionsTheme(builders: {
      TargetPlatform.android: CupertinoPageTransitionsBuilder(),
      TargetPlatform.iOS: CupertinoPageTransitionsBuilder(),
    }),
  );
}

/// Frosted dark panel used for every control floating over the live camera.
class GlassPanel extends StatelessWidget {
  const GlassPanel({super.key, required this.child, this.padding = const EdgeInsets.all(12), this.radius = AppRadius.lg});
  final Widget child;
  final EdgeInsets padding;
  final double radius;

  @override
  Widget build(BuildContext context) => ClipRRect(
        borderRadius: BorderRadius.circular(radius),
        child: BackdropFilter(
          filter: ImageFilter.blur(sigmaX: 18, sigmaY: 18),
          child: Container(
            padding: padding,
            decoration: BoxDecoration(
              color: AppColors.glass,
              borderRadius: BorderRadius.circular(radius),
              border: Border.all(color: AppColors.glassLight, width: 0.5),
            ),
            child: child,
          ),
        ),
      );
}
