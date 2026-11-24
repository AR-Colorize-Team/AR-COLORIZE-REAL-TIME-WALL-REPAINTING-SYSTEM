import 'dart:async';

import 'package:flutter/material.dart';

import '../../core/api.dart';
import '../../core/theme.dart';
import '../designs/designs.dart';

/// Premium AI room report. Reads like a consultant's note, not a data dump:
///   room in one sentence → what the light is doing → three palettes with
///   psychology & reasons → "Try in AR" puts a palette on the wall instantly.
class AiReportScreen extends StatefulWidget {
  const AiReportScreen({super.key, this.projectId, this.assetId, required this.onApply});
  final String? projectId;
  final String? assetId;
  final void Function(WallLook look, double? currentWallLrv) onApply;

  @override
  State<AiReportScreen> createState() => _AiReportScreenState();
}

class _AiReportScreenState extends State<AiReportScreen> {
  Map<String, dynamic>? report;
  String? error;
  int step = 0;
  Timer? _ticker;

  static const _steps = ['Reading the light', 'Finding undertones', 'Matching your furniture', 'Curating palettes'];

  @override
  void initState() {
    super.initState();
    _ticker = Timer.periodic(const Duration(milliseconds: 1800), (_) {
      if (mounted && report == null) setState(() => step = (step + 1) % _steps.length);
    });
    _load();
  }

  Future<void> _load() async {
    try {
      final r = (widget.projectId == null || widget.assetId == null)
          ? sampleReport                                    // design preview / offline demo
          : await Api.instance.recommendPalettes(widget.projectId!, widget.assetId!);
      if (mounted) setState(() => report = r);
    } on ApiException catch (e) {
      if (mounted) setState(() => error = e.code == 'quota_exceeded' ? 'quota' : e.message);
    }
  }

  @override
  void dispose() {
    _ticker?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    if (error == 'quota') return const _Paywall();
    if (error != null) return Scaffold(appBar: AppBar(), body: Center(child: Text(error!)));
    if (report == null) {
      return Scaffold(
        body: Center(
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            const SizedBox(width: 56, height: 56, child: CircularProgressIndicator(strokeWidth: 2, color: AppColors.brass)),
            const SizedBox(height: 24),
            AnimatedSwitcher(duration: const Duration(milliseconds: 300),
                child: Text(_steps[step], key: ValueKey(step), style: t.headlineSmall)),
            const SizedBox(height: 8),
            Text('Your AI colour consultant is studying your room', style: t.bodySmall),
          ]),
        ),
      );
    }
    final ra = report!['room_analysis'] as Map<String, dynamic>;
    final light = ra['lighting'] as Map<String, dynamic>;
    final wall = ra['current_wall'] as Map<String, dynamic>;
    final style = ra['style'] as Map<String, dynamic>;
    final palettes = (report!['palettes'] as List).cast<Map<String, dynamic>>();

    return Scaffold(
      body: CustomScrollView(slivers: [
        SliverAppBar.large(
          backgroundColor: AppColors.ivory,
          title: Text('Your room, read', style: t.headlineSmall),
        ),
        SliverPadding(
          padding: const EdgeInsets.fromLTRB(20, 0, 20, 40),
          sliver: SliverList.list(children: [
            Text(ra['summary'] as String? ?? '', style: t.bodyLarge?.copyWith(height: 1.5)),
            const SizedBox(height: 20),
            Row(children: [
              Expanded(child: _Fact(icon: Icons.wb_incandescent_outlined, title: '${light['color_temperature_k']} K',
                  subtitle: _cap(light['label'] as String))),
              const SizedBox(width: 10),
              Expanded(child: _Fact(icon: Icons.light_mode_outlined, title: _cap(light['brightness'] as String),
                  subtitle: 'Light from ${light['direction']}')),
              const SizedBox(width: 10),
              Expanded(child: _Fact(icon: Icons.chair_outlined, title: style['detected'] as String,
                  subtitle: '${((style['confidence'] as num) * 100).round()}% match')),
            ]),
            if (wall['hex'] != null) ...[
              const SizedBox(height: 10),
              _CurrentWall(hex: wall['hex'] as String, lrv: (wall['lrv'] as num).toDouble(), tone: wall['undertone'] as String),
            ],
            if ((ra['constraints'] as List).isNotEmpty) ...[
              const SizedBox(height: 24),
              Text('What your room is telling us', style: t.titleMedium),
              const SizedBox(height: 8),
              for (final c in (ra['constraints'] as List))
                Padding(
                  padding: const EdgeInsets.only(bottom: 6),
                  child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    const Padding(padding: EdgeInsets.only(top: 7), child: CircleAvatar(radius: 3, backgroundColor: AppColors.brass)),
                    const SizedBox(width: 10),
                    Expanded(child: Text(c as String, style: t.bodyMedium)),
                  ]),
                ),
            ],
            const SizedBox(height: 28),
            Text('Three directions for this room', style: t.headlineSmall),
            const SizedBox(height: 12),
            for (final (i, p) in palettes.indexed)
              _PaletteCard(index: i, p: p, onTry: () {
                widget.onApply(_lookFrom(p), (wall['lrv'] as num?)?.toDouble());
                Navigator.of(context).pop();
              }),
          ]),
        ),
      ]),
    );
  }

  static String _cap(String s) => s.isEmpty ? s : s[0].toUpperCase() + s.substring(1);

  static WallLook _lookFrom(Map<String, dynamic> p) {
    final colors = (p['colors'] as List).cast<Map<String, dynamic>>();
    Map<String, dynamic> byRole(String r) => colors.firstWhere((c) => c['role'] == r, orElse: () => colors.first);
    final primary = byRole('primary');
    final second = colors.firstWhere((c) => c['role'] == 'secondary' || c['role'] == 'accent', orElse: () => primary);
    final pattern = PaintPattern.values.firstWhere(
        (x) => _snake(x.name) == (p['suggested_design']?['pattern'] ?? 'solid'), orElse: () => PaintPattern.solid);
    final finish = Finish.values.firstWhere((f) => f.key == primary['finish'], orElse: () => Finish.matte);
    return WallLook(colorA: primary['hex'] as String, colorB: second['hex'] as String, pattern: pattern,
        finish: finish, nameA: primary['label'] as String?, nameB: second['label'] as String?);
  }

  static String _snake(String camel) => camel.replaceAllMapped(RegExp('[A-Z]'), (m) => '_${m[0]!.toLowerCase()}');
}

class _Fact extends StatelessWidget {
  const _Fact({required this.icon, required this.title, required this.subtitle});
  final IconData icon;
  final String title, subtitle;

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(color: AppColors.stone, borderRadius: BorderRadius.circular(AppRadius.md)),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Icon(icon, size: 18, color: AppColors.brass),
          const SizedBox(height: 8),
          Text(title, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 15)),
          Text(subtitle, style: const TextStyle(color: AppColors.muted, fontSize: 12)),
        ]),
      );
}

class _CurrentWall extends StatelessWidget {
  const _CurrentWall({required this.hex, required this.lrv, required this.tone});
  final String hex, tone;
  final double lrv;

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(border: Border.all(color: AppColors.line), borderRadius: BorderRadius.circular(AppRadius.md)),
        child: Row(children: [
          Container(width: 36, height: 36, decoration: BoxDecoration(color: hexColor(hex), shape: BoxShape.circle,
              border: Border.all(color: AppColors.line))),
          const SizedBox(width: 12),
          Expanded(child: Text('Your walls today: $tone undertone, LRV ${lrv.round()}',
              style: const TextStyle(fontWeight: FontWeight.w500))),
        ]),
      );
}

class _PaletteCard extends StatelessWidget {
  const _PaletteCard({required this.index, required this.p, required this.onTry});
  final int index;
  final Map<String, dynamic> p;
  final VoidCallback onTry;

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    final colors = (p['colors'] as List).cast<Map<String, dynamic>>();
    final warnings = (p['warnings'] as List? ?? const []).cast<String>();
    return Container(
      margin: const EdgeInsets.only(bottom: 16),
      decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(AppRadius.lg),
          boxShadow: const [BoxShadow(color: Color(0x14000000), blurRadius: 24, offset: Offset(0, 8))]),
      clipBehavior: Clip.antiAlias,
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        SizedBox(
          height: 96,
          child: Row(children: [
            for (final c in colors)
              Expanded(flex: c['role'] == 'primary' ? 3 : 1, child: Container(color: hexColor(c['hex'] as String))),
          ]),
        ),
        Padding(
          padding: const EdgeInsets.all(18),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text('0${index + 1}', style: const TextStyle(color: AppColors.brass, fontWeight: FontWeight.w700, letterSpacing: 1)),
            const SizedBox(height: 4),
            Text(p['name'] as String, style: t.headlineSmall),
            Text(p['tagline'] as String? ?? '', style: t.bodySmall),
            const SizedBox(height: 12),
            Text('How it feels', style: t.labelLarge),
            Text(p['psychology'] as String? ?? '', style: t.bodyMedium),
            const SizedBox(height: 10),
            Text('Why it works here', style: t.labelLarge),
            Text(p['why_it_works'] as String? ?? '', style: t.bodyMedium),
            const SizedBox(height: 12),
            Wrap(spacing: 8, runSpacing: 8, children: [
              for (final c in colors)
                Chip(
                  avatar: CircleAvatar(backgroundColor: hexColor(c['hex'] as String)),
                  label: Text('${c['label']} · ${c['role']}${c['code'] != null ? ' · ${c['code']}' : ''}'),
                  backgroundColor: AppColors.ivory,
                  side: const BorderSide(color: AppColors.line),
                ),
            ]),
            for (final w in warnings)
              Padding(
                padding: const EdgeInsets.only(top: 10),
                child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  const Icon(Icons.info_outline, size: 16, color: AppColors.brass),
                  const SizedBox(width: 6),
                  Expanded(child: Text(w, style: t.bodySmall)),
                ]),
              ),
            const SizedBox(height: 16),
            FilledButton.icon(onPressed: onTry, icon: const Icon(Icons.view_in_ar_rounded), label: const Text('Try on my wall')),
          ]),
        ),
      ]),
    );
  }
}

class _Paywall extends StatelessWidget {
  const _Paywall();

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(backgroundColor: AppColors.ivory),
      body: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Icon(Icons.auto_awesome, color: AppColors.brass, size: 36),
          const SizedBox(height: 16),
          Text('You’ve used this month’s free analyses', style: t.displaySmall),
          const SizedBox(height: 12),
          Text('Pro gives you 100 AI room reads a month, HD exports and every design pattern.', style: t.bodyMedium),
          const Spacer(),
          FilledButton(onPressed: () => Api.instance.openUpgrade(), child: const Text('Upgrade to Pro')),
          const SizedBox(height: 8),
          Center(child: TextButton(onPressed: () => Navigator.pop(context), child: const Text('Maybe later'))),
        ]),
      ),
    );
  }
}

/// Representative output of engine/ai/room_analysis.py — used for previews and offline demo.
const Map<String, dynamic> sampleReport = {
  'room_analysis': {
    'summary': 'A softly lit living room with warm oak floors and a charcoal sofa. Warm evening bulbs and a '
        'left-hand window give it a gentle, golden cast.',
    'lighting': {'color_temperature_k': 2900, 'label': 'warm white', 'brightness': 'medium', 'direction': 'left'},
    'current_wall': {'hex': '#DBD6CC', 'lrv': 67, 'undertone': 'warm'},
    'style': {'detected': 'Japandi', 'confidence': 0.78},
    'constraints': [
      'Warm 2,900 K bulbs will make cool blue-greys look muddy at night.',
      'The charcoal sofa is the anchor — palettes should give it something to sit against.',
      'Light falls off to the right; keep that wall at LRV 55+ or it will feel heavy.',
    ],
  },
  'palettes': [
    {
      'name': 'Morning Linen', 'tagline': 'Soft, sunlit calm',
      'psychology': 'Pale warm neutrals quiet the eye and help you unwind — the room will feel bigger and slower.',
      'why_it_works': 'Echoes your oak floor, flatters warm bulbs, and makes the charcoal sofa look intentional.',
      'colors': [
        {'role': 'primary', 'hex': '#ECE4D6', 'label': 'Linen', 'finish': 'matte'},
        {'role': 'accent', 'hex': '#9CAF88', 'label': 'Sage', 'finish': 'matte'},
        {'role': 'trim', 'hex': '#F7F4EE', 'label': 'Chalk', 'finish': 'satin'},
        {'role': 'ceiling', 'hex': '#FBF9F5', 'label': 'Paper', 'finish': 'matte'},
      ],
      'suggested_design': {'pattern': 'solid'}, 'warnings': [],
    },
    {
      'name': 'Terracotta Supper', 'tagline': 'Warm, sociable, golden-hour',
      'psychology': 'Clay tones raise perceived warmth and draw people together — great where you host.',
      'why_it_works': 'An arch behind the sofa frames it like a gallery piece; terracotta glows under 2,900 K light.',
      'colors': [
        {'role': 'primary', 'hex': '#EFE6D8', 'label': 'Plaster', 'finish': 'matte'},
        {'role': 'secondary', 'hex': '#C46A4A', 'label': 'Terracotta', 'finish': 'matte'},
        {'role': 'trim', 'hex': '#F6F0E6', 'label': 'Bone', 'finish': 'satin'},
      ],
      'suggested_design': {'pattern': 'arch'}, 'warnings': [],
    },
    {
      'name': 'Forest Study', 'tagline': 'Grounded and cocooning',
      'psychology': 'Deep green reads as nature and safety; it softens screens and makes evenings feel cosy.',
      'why_it_works': 'Rich enough for the bright left wall; the parchment trim keeps the darker right side lifted.',
      'colors': [
        {'role': 'primary', 'hex': '#3F5E4B', 'label': 'Forest', 'finish': 'matte'},
        {'role': 'secondary', 'hex': '#E8DFD0', 'label': 'Parchment', 'finish': 'matte'},
        {'role': 'trim', 'hex': '#E8DFD0', 'label': 'Parchment', 'finish': 'satin'},
      ],
      'suggested_design': {'pattern': 'mountain'},
      'warnings': ['Deep colour: lovely and cosy, but the room will feel smaller. Balance with a pale ceiling.'],
    },
  ],
};
