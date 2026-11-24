import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:share_plus/share_plus.dart';

import '../../core/theme.dart';
import '../ai/ai_report_screen.dart';
import '../designs/designs.dart';
import 'ar_camera_view.dart';
import 'ar_controller.dart';

/// The heart of the app. Designed for one thumb and zero learning:
///   1. point the phone  →  2. tap a wall  →  3. swipe through colours.
/// Everything else (designs, styles, finish, compare, AI) is one tap away in the dock,
/// and never covers the wall being painted.
class ArPaintScreen extends StatefulWidget {
  const ArPaintScreen({super.key, this.roomName = 'Living room', this.projectId, this.roomPhotoAssetId});
  final String roomName;
  final String? projectId;
  final String? roomPhotoAssetId;   // verified room photo used for AI analysis (not the painted frame)

  @override
  State<ArPaintScreen> createState() => _ArPaintScreenState();
}

enum _DockTab { colours, designs, styles, ai }

class _ArPaintScreenState extends State<ArPaintScreen> with WidgetsBindingObserver {
  late final ArController ar;
  _DockTab tab = _DockTab.colours;
  bool comparing = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    ar = ArController(devicePixelRatio: WidgetsBinding.instance.platformDispatcher.views.first.devicePixelRatio);
    SystemChrome.setPreferredOrientations([DeviceOrientation.portraitUp]); // renderer assumes portrait
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.paused) ar.pause();
    if (state == AppLifecycleState.resumed) ar.resume();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    SystemChrome.setPreferredOrientations(DeviceOrientation.values);
    ar.dispose();
    super.dispose();
  }

  Future<void> _capture() async {
    final png = await ar.capture();
    if (png == null || !mounted) return;
    await Share.shareXFiles([XFile.fromData(png, mimeType: 'image/png', name: 'my-new-wall.png')],
        text: 'What do you think of this colour? ${ar.look.nameA ?? ar.look.colorA}');
  }

  @override
  Widget build(BuildContext context) {
    return AnnotatedRegion<SystemUiOverlayStyle>(
      value: SystemUiOverlayStyle.light,
      child: Scaffold(
        backgroundColor: Colors.black,
        body: ListenableBuilder(
          listenable: ar,
          builder: (context, _) => Stack(children: [
            // Live camera + paint. Taps go to the wall picker.
            Positioned.fill(
              child: GestureDetector(
                behavior: HitTestBehavior.opaque,
                onTapUp: (d) => ar.tapAt(d.localPosition),
                onHorizontalDragUpdate: comparing
                    ? (d) => ar.setReveal(d.localPosition.dx / MediaQuery.sizeOf(context).width)
                    : null,
                child: const ArCameraView(),
              ),
            ),
            if (comparing) _CompareHandle(x: ar.reveal),
            SafeArea(child: _TopBar(
              room: widget.roomName,
              precision: ar.hasLiDAR,
              canUndo: ar.canUndo,
              onUndo: ar.undo,
              onCapture: _capture,
            )),
            _Coach(state: ar.coach, hint: ar.transientHint),
            Align(
              alignment: Alignment.bottomCenter,
              child: SafeArea(
                top: false,
                child: Padding(
                  padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
                  child: _Dock(
                    ar: ar,
                    tab: tab,
                    comparing: comparing,
                    onTab: (t) => setState(() => tab = t),
                    onCompare: () {
                      setState(() => comparing = !comparing);
                      ar.setReveal(comparing ? 0.5 : 1.0);
                    },
                    onOpenAi: () => Navigator.of(context).push(MaterialPageRoute(
                      builder: (_) => AiReportScreen(projectId: widget.projectId, assetId: widget.roomPhotoAssetId, onApply: (look, lrv) {
                        if (lrv != null) ar.setCurrentWallLuminance(lrv);
                        ar.applyLook(look);
                      }),
                    )),
                  ),
                ),
              ),
            ),
          ]),
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
class _TopBar extends StatelessWidget {
  const _TopBar({required this.room, required this.precision, required this.canUndo,
      required this.onUndo, required this.onCapture});
  final String room;
  final bool precision;
  final bool canUndo;
  final VoidCallback onUndo;
  final VoidCallback onCapture;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 0),
        child: Row(children: [
          _RoundGlassButton(icon: Icons.arrow_back_ios_new_rounded, label: 'Back',
              onTap: () => Navigator.of(context).maybePop()),
          const SizedBox(width: 8),
          Expanded(
            child: GlassPanel(
              radius: AppRadius.pill,
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
              child: Row(children: [
                Flexible(child: Text(room, overflow: TextOverflow.ellipsis,
                    style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w600))),
                if (precision) ...[
                  const SizedBox(width: 8),
                  const Icon(Icons.auto_awesome, size: 14, color: AppColors.brass),
                  const SizedBox(width: 4),
                  const Text('Precision', style: TextStyle(color: AppColors.brass, fontSize: 12)),
                ],
              ]),
            ),
          ),
          const SizedBox(width: 8),
          _RoundGlassButton(icon: Icons.undo_rounded, label: 'Undo', onTap: canUndo ? onUndo : null),
          const SizedBox(width: 8),
          _RoundGlassButton(icon: Icons.ios_share_rounded, label: 'Save & share', onTap: onCapture),
        ]),
      );
}

class _RoundGlassButton extends StatelessWidget {
  const _RoundGlassButton({required this.icon, required this.label, this.onTap});
  final IconData icon;
  final String label;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) => Semantics(
        button: true,
        label: label,
        child: GestureDetector(
          onTap: onTap,
          child: GlassPanel(
            radius: AppRadius.pill,
            padding: const EdgeInsets.all(11),
            child: Icon(icon, size: 20, color: onTap == null ? Colors.white38 : Colors.white),
          ),
        ),
      );
}

// ---------------------------------------------------------------------------
class _Coach extends StatelessWidget {
  const _Coach({required this.state, this.hint});
  final CoachState state;
  final String? hint;

  @override
  Widget build(BuildContext context) {
    final text = hint ?? state.message;
    return Align(
      alignment: const Alignment(0, -0.62),
      child: AnimatedSwitcher(
        duration: const Duration(milliseconds: 250),
        child: text.isEmpty
            ? const SizedBox.shrink()
            : GlassPanel(
                key: ValueKey(text),
                radius: AppRadius.pill,
                padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
                child: Row(mainAxisSize: MainAxisSize.min, children: [
                  Icon(state == CoachState.tapWall ? Icons.touch_app_rounded : Icons.phone_iphone_rounded,
                      color: Colors.white, size: 18),
                  const SizedBox(width: 8),
                  Text(text, style: const TextStyle(color: Colors.white, fontSize: 14)),
                ]),
              ),
      ),
    );
  }
}

class _CompareHandle extends StatelessWidget {
  const _CompareHandle({required this.x});
  final double x;

  @override
  Widget build(BuildContext context) {
    final w = MediaQuery.sizeOf(context).width;
    return Positioned(
      left: x * w - 20, top: 0, bottom: 0,
      child: IgnorePointer(
        child: Column(children: [
          const Spacer(),
          Container(width: 2, height: 160, color: Colors.white),
          Container(
            width: 40, height: 40,
            decoration: const BoxDecoration(color: Colors.white, shape: BoxShape.circle),
            child: const Icon(Icons.compare_arrows_rounded, color: AppColors.ink),
          ),
          Container(width: 2, height: 160, color: Colors.white),
          const Spacer(),
        ]),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
class _Dock extends StatelessWidget {
  const _Dock({required this.ar, required this.tab, required this.comparing, required this.onTab,
      required this.onCompare, required this.onOpenAi});
  final ArController ar;
  final _DockTab tab;
  final bool comparing;
  final ValueChanged<_DockTab> onTab;
  final VoidCallback onCompare;
  final VoidCallback onOpenAi;

  @override
  Widget build(BuildContext context) {
    return GlassPanel(
      padding: const EdgeInsets.fromLTRB(12, 12, 12, 10),
      child: Column(mainAxisSize: MainAxisSize.min, children: [
        // Current selection, always visible: what's on the wall right now.
        Row(children: [
          _Swatch(hex: ar.look.colorA, size: 28),
          if (ar.look.pattern.usesSecondColor) ...[
            const SizedBox(width: 4),
            _Swatch(hex: ar.look.colorB ?? ar.look.colorA, size: 28),
          ],
          const SizedBox(width: 10),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(ar.look.nameB ?? ar.look.nameA ?? ar.look.colorA,
                  style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w600)),
              Text(
                ar.walls.isEmpty
                    ? '${ar.look.pattern.label} · ${ar.look.finish.label}'
                    : '${ar.look.pattern.label} · ${ar.look.finish.label} · ≈ ${ar.litresNeeded.toStringAsFixed(1)} L',
                style: const TextStyle(color: Colors.white70, fontSize: 12),
              ),
            ]),
          ),
          _PillButton(label: comparing ? 'Done' : 'Compare', icon: Icons.compare_rounded, onTap: onCompare),
        ]),
        const SizedBox(height: 12),
        SizedBox(height: 96, child: _tabBody(context)),
        const SizedBox(height: 10),
        _Tabs(tab: tab, onTab: onTab),
      ]),
    );
  }

  Widget _tabBody(BuildContext context) => switch (tab) {
        _DockTab.colours => ListView.separated(
            scrollDirection: Axis.horizontal,
            itemCount: quickColors.length,
            separatorBuilder: (_, __) => const SizedBox(width: 10),
            itemBuilder: (_, i) {
              final (hex, name) = quickColors[i];
              final active = hex == (ar.look.pattern.usesSecondColor ? ar.look.colorB : ar.look.colorA);
              return GestureDetector(
                onTap: () => ar.pickColor(hex, name: name),
                child: Column(children: [
                  AnimatedContainer(
                    duration: const Duration(milliseconds: 180),
                    padding: EdgeInsets.all(active ? 3 : 0),
                    decoration: BoxDecoration(shape: BoxShape.circle,
                        border: Border.all(color: active ? Colors.white : Colors.transparent, width: 2)),
                    child: _Swatch(hex: hex, size: active ? 52 : 58),
                  ),
                  const SizedBox(height: 6),
                  Text(name, style: TextStyle(color: active ? Colors.white : Colors.white70, fontSize: 11)),
                ]),
              );
            },
          ),
        _DockTab.designs => ListView.separated(
            scrollDirection: Axis.horizontal,
            itemCount: PaintPattern.values.length,
            separatorBuilder: (_, __) => const SizedBox(width: 10),
            itemBuilder: (_, i) {
              final p = PaintPattern.values[i];
              final active = p == ar.look.pattern;
              return GestureDetector(
                onTap: () => ar.pickPattern(p),
                child: Column(children: [
                  Container(
                    width: 72, height: 60,
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(AppRadius.sm),
                      border: Border.all(color: active ? Colors.white : Colors.white24, width: active ? 2 : 1),
                    ),
                    clipBehavior: Clip.antiAlias,
                    child: CustomPaint(painter: PatternThumbPainter(p,
                        hexColor(ar.look.colorA), hexColor(ar.look.colorB ?? '#C46A4A'))),
                  ),
                  const SizedBox(height: 6),
                  Text(p.label, style: TextStyle(color: active ? Colors.white : Colors.white70, fontSize: 11)),
                ]),
              );
            },
          ),
        _DockTab.styles => ListView.separated(
            scrollDirection: Axis.horizontal,
            itemCount: stylePresets.length,
            separatorBuilder: (_, __) => const SizedBox(width: 10),
            itemBuilder: (_, i) {
              final s = stylePresets[i];
              return GestureDetector(
                onTap: () => ar.applyPreset(s),
                child: Container(
                  width: 150,
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: Colors.white.withValues(alpha: 0.08),
                    borderRadius: BorderRadius.circular(AppRadius.md),
                  ),
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Row(children: [for (final h in s.swatches) Expanded(child: Container(height: 26, color: hexColor(h)))]),
                    const Spacer(),
                    Text(s.name, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w600, fontSize: 13)),
                    Text(s.mood, style: const TextStyle(color: Colors.white60, fontSize: 11)),
                  ]),
                ),
              );
            },
          ),
        _DockTab.ai => GestureDetector(
            onTap: onOpenAi,
            child: Container(
              padding: const EdgeInsets.all(14),
              decoration: BoxDecoration(
                gradient: const LinearGradient(colors: [Color(0xFF3A3128), Color(0xFF1C1A17)]),
                borderRadius: BorderRadius.circular(AppRadius.md),
                border: Border.all(color: AppColors.brass.withValues(alpha: 0.6)),
              ),
              child: const Row(children: [
                Icon(Icons.auto_awesome, color: AppColors.brass, size: 28),
                SizedBox(width: 12),
                Expanded(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisAlignment: MainAxisAlignment.center, children: [
                    Text('Analyse my room', style: TextStyle(color: Colors.white, fontWeight: FontWeight.w600, fontSize: 15)),
                    SizedBox(height: 2),
                    Text('Lighting, undertones & 3 designer palettes in ~10 s',
                        style: TextStyle(color: Colors.white70, fontSize: 12)),
                  ]),
                ),
                Icon(Icons.chevron_right_rounded, color: Colors.white70),
              ]),
            ),
          ),
      };
}

class _Tabs extends StatelessWidget {
  const _Tabs({required this.tab, required this.onTab});
  final _DockTab tab;
  final ValueChanged<_DockTab> onTab;

  static const _labels = {
    _DockTab.colours: ('Colours', Icons.palette_outlined),
    _DockTab.designs: ('Designs', Icons.dashboard_customize_outlined),
    _DockTab.styles: ('Styles', Icons.weekend_outlined),
    _DockTab.ai: ('AI', Icons.auto_awesome_outlined),
  };

  @override
  Widget build(BuildContext context) => Row(
        children: [
          for (final t in _DockTab.values)
            Expanded(
              child: GestureDetector(
                onTap: () {
                  HapticFeedback.selectionClick();
                  onTab(t);
                },
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 180),
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  decoration: BoxDecoration(
                    color: t == tab ? Colors.white : Colors.transparent,
                    borderRadius: BorderRadius.circular(AppRadius.pill),
                  ),
                  child: Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                    Icon(_labels[t]!.$2, size: 16, color: t == tab ? AppColors.ink : Colors.white70),
                    const SizedBox(width: 4),
                    Text(_labels[t]!.$1, style: TextStyle(
                        fontSize: 12, fontWeight: FontWeight.w600,
                        color: t == tab ? AppColors.ink : Colors.white70)),
                  ]),
                ),
              ),
            ),
        ],
      );
}

class _PillButton extends StatelessWidget {
  const _PillButton({required this.label, required this.icon, required this.onTap});
  final String label;
  final IconData icon;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => GestureDetector(
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          decoration: BoxDecoration(
            color: Colors.white.withValues(alpha: 0.12),
            borderRadius: BorderRadius.circular(AppRadius.pill),
          ),
          child: Row(mainAxisSize: MainAxisSize.min, children: [
            Icon(icon, size: 16, color: Colors.white),
            const SizedBox(width: 6),
            Text(label, style: const TextStyle(color: Colors.white, fontSize: 12, fontWeight: FontWeight.w600)),
          ]),
        ),
      );
}

class _Swatch extends StatelessWidget {
  const _Swatch({required this.hex, required this.size});
  final String hex;
  final double size;

  @override
  Widget build(BuildContext context) => Container(
        width: size, height: size,
        decoration: BoxDecoration(
          color: hexColor(hex),
          shape: BoxShape.circle,
          border: Border.all(color: Colors.white24, width: 1),
          boxShadow: const [BoxShadow(color: Colors.black26, blurRadius: 6, offset: Offset(0, 2))],
        ),
      );
}

/// Mini preview of each pattern, drawn with the same geometry as the shader
/// (scaled: thumbnail width ≈ 3 m of wall, height ≈ 2.5 m).
class PatternThumbPainter extends CustomPainter {
  PatternThumbPainter(this.pattern, this.a, this.b);
  final PaintPattern pattern;
  final Color a, b;

  @override
  void paint(Canvas canvas, Size s) {
    final pa = Paint()..color = a;
    final pb = Paint()..color = b;
    canvas.drawRect(Offset.zero & s, pa);
    double mx(double m) => m / 3.0 * s.width;           // metres → px
    double my(double m) => s.height - m / 2.5 * s.height; // height above floor → px
    switch (pattern) {
      case PaintPattern.solid:
        break;
      case PaintPattern.twoTone:
        canvas.drawRect(Rect.fromLTRB(0, my(0.95), s.width, s.height), pb);
      case PaintPattern.stripesVertical:
        for (double x = 0.3; x < 3; x += 0.6) { canvas.drawRect(Rect.fromLTWH(mx(x), 0, mx(0.3), s.height), pb); }
      case PaintPattern.stripesHorizontal:
        for (double y = 0.25; y < 2.5; y += 0.5) { canvas.drawRect(Rect.fromLTRB(0, my(y + 0.25), s.width, my(y)), pb); }
      case PaintPattern.ombre:
        canvas.drawRect(Offset.zero & s, Paint()
          ..shader = LinearGradient(begin: Alignment.topCenter, end: Alignment.bottomCenter, colors: [a, b])
              .createShader(Offset.zero & s));
      case PaintPattern.mountain:
        final path = Path()..moveTo(0, s.height);
        for (double x = 0; x <= 3.0; x += 0.05) {
          final f = (x / 1.6) % 1.0;
          final tri = 1 - (2 * f - 1).abs();            // same triangle wave as the shader
          path.lineTo(mx(x), my(0.9 + 0.7 * tri));
        }
        path..lineTo(s.width, s.height)..close();
        canvas.drawPath(path, pb);
      case PaintPattern.arch:
        final r = mx(0.7);
        final rect = Rect.fromLTRB(mx(1.5) - r, my(2.1), mx(1.5) + r, s.height);
        canvas.drawRRect(RRect.fromRectAndCorners(rect, topLeft: Radius.circular(r), topRight: Radius.circular(r)), pb);
      case PaintPattern.colorBlock:
        canvas.drawRect(Rect.fromLTRB(mx(0.4), my(1.8), mx(1.6), s.height), pb);
    }
  }

  @override
  bool shouldRepaint(PatternThumbPainter old) => old.pattern != pattern || old.a != a || old.b != b;
}
