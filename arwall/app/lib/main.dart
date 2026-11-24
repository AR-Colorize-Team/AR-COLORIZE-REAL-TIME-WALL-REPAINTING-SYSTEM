import 'package:flutter/material.dart';
import 'package:permission_handler/permission_handler.dart';

import 'core/theme.dart';
import 'features/ai/ai_report_screen.dart';
import 'features/ar/ar_controller.dart';
import 'features/ar/ar_paint_screen.dart';
import 'features/designs/designs.dart';

void main() => runApp(const ArWallApp());

class ArWallApp extends StatelessWidget {
  const ArWallApp({super.key});

  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'AR Wall',
        debugShowCheckedModeBanner: false,
        theme: buildTheme(),
        home: const HomeScreen(),
      );
}

/// Home: one obvious primary action ("Paint a wall"), everything else secondary.
class HomeScreen extends StatelessWidget {
  const HomeScreen({super.key});

  Future<void> _startAr(BuildContext context) async {
    // Friendly pre-prompt before the OS dialog: users who understand *why* say yes.
    final status = await Permission.camera.status;
    if (!status.isGranted && context.mounted) {
      final go = await showModalBottomSheet<bool>(
        context: context,
        backgroundColor: AppColors.ivory,
        shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(AppRadius.lg))),
        builder: (c) => Padding(
          padding: const EdgeInsets.fromLTRB(24, 28, 24, 24),
          child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text('See colours on your real walls', style: Theme.of(c).textTheme.headlineSmall),
            const SizedBox(height: 8),
            const Text('We use the camera live, on your phone. Nothing is recorded or uploaded unless you save a photo.'),
            const SizedBox(height: 24),
            FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Allow camera')),
          ]),
        ),
      );
      if (go != true) return;
      if (!(await Permission.camera.request()).isGranted) {
        if (context.mounted) _snack(context, 'Camera access is off. You can turn it on in Settings.');
        return;
      }
    }
    if (!await ArController.isSupported() || !await ArController.ensureInstalled()) {
      if (context.mounted) _snack(context, 'Live AR isn’t supported on this phone — use photo mode instead.');
      return;
    }
    if (context.mounted) {
      Navigator.of(context).push(MaterialPageRoute(builder: (_) => const ArPaintScreen()));
    }
  }

  static void _snack(BuildContext c, String m) => ScaffoldMessenger.of(c).showSnackBar(SnackBar(content: Text(m)));

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    return Scaffold(
      body: SafeArea(
        child: ListView(padding: const EdgeInsets.fromLTRB(20, 16, 20, 32), children: [
          Row(children: [
            Text('AR Wall', style: t.titleMedium?.copyWith(letterSpacing: 0.4)),
            const Spacer(),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
              decoration: BoxDecoration(border: Border.all(color: AppColors.brass), borderRadius: BorderRadius.circular(AppRadius.pill)),
              child: const Text('PRO', style: TextStyle(color: AppColors.brass, fontWeight: FontWeight.w700, fontSize: 11, letterSpacing: 1)),
            ),
          ]),
          const SizedBox(height: 28),
          Text('What will your\nwalls become?', style: t.displaySmall),
          const SizedBox(height: 20),
          _HeroCard(onTap: () => _startAr(context)),
          const SizedBox(height: 14),
          _ActionRow(
            icon: Icons.auto_awesome_outlined,
            title: 'AI colour consultant',
            subtitle: 'Reads your light & furniture, suggests 3 palettes',
            onTap: () => Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => AiReportScreen(onApply: (_, __) => _startAr(context)))),
          ),
          _ActionRow(icon: Icons.photo_outlined, title: 'Paint a photo', subtitle: 'No AR? Use any room photo', onTap: () {}),
          const SizedBox(height: 28),
          Text('Styles to try', style: t.titleMedium),
          const SizedBox(height: 12),
          SizedBox(
            height: 150,
            child: ListView.separated(
              scrollDirection: Axis.horizontal,
              itemCount: stylePresets.length,
              separatorBuilder: (_, __) => const SizedBox(width: 12),
              itemBuilder: (_, i) {
                final s = stylePresets[i];
                return Container(
                  width: 130,
                  decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(AppRadius.md)),
                  clipBehavior: Clip.antiAlias,
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Expanded(child: Row(children: [for (final h in s.swatches) Expanded(child: ColoredBox(color: hexColor(h)))])),
                    Padding(
                      padding: const EdgeInsets.all(10),
                      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                        Text(s.name, style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13)),
                        Text(s.mood, style: t.bodySmall),
                      ]),
                    ),
                  ]),
                );
              },
            ),
          ),
        ]),
      ),
    );
  }
}

class _HeroCard extends StatelessWidget {
  const _HeroCard({required this.onTap});
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => GestureDetector(
        onTap: onTap,
        child: Container(
          height: 220,
          padding: const EdgeInsets.all(20),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(AppRadius.lg),
            gradient: const LinearGradient(begin: Alignment.topLeft, end: Alignment.bottomRight,
                colors: [Color(0xFF9CAF88), Color(0xFF3F5E4B)]),
          ),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            const Icon(Icons.view_in_ar_rounded, color: Colors.white, size: 30),
            const Spacer(),
            Text('Paint a wall, live', style: Theme.of(context).textTheme.headlineSmall?.copyWith(color: Colors.white)),
            const SizedBox(height: 4),
            const Text('Point your camera · tap a wall · swipe colours',
                style: TextStyle(color: Colors.white70)),
          ]),
        ),
      );
}

class _ActionRow extends StatelessWidget {
  const _ActionRow({required this.icon, required this.title, required this.subtitle, required this.onTap});
  final IconData icon;
  final String title, subtitle;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => ListTile(
        onTap: onTap,
        contentPadding: const EdgeInsets.symmetric(vertical: 4),
        leading: Container(
          width: 44, height: 44,
          decoration: BoxDecoration(color: AppColors.stone, borderRadius: BorderRadius.circular(AppRadius.sm)),
          child: Icon(icon, color: AppColors.ink),
        ),
        title: Text(title, style: const TextStyle(fontWeight: FontWeight.w600)),
        subtitle: Text(subtitle),
        trailing: const Icon(Icons.chevron_right_rounded),
      );
}
