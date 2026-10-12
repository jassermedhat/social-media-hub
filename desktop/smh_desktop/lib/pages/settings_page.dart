import 'package:flutter/material.dart';

import '../state/theme_controller.dart';
import '../theme/smh_theme.dart';
import '../widgets/smh_card.dart';

class SettingsPage extends StatelessWidget {
  const SettingsPage({super.key, required this.themeController});

  final SmhThemeController themeController;

  @override
  Widget build(BuildContext context) {
    final palette = SmhTheme.paletteOf(context);
    return SmhPageFrame(
      eyebrow: 'Workspace / Settings',
      title: 'Settings',
      subtitle: 'Make SMH comfortable for your desktop.',
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 760),
        child: Column(
          children: [
            SmhCard(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const SmhSectionTitle('Appearance'),
                  const SizedBox(height: 8),
                  Text(
                    'Choose the appearance you prefer. Your choice is saved on this computer.',
                    style: Theme.of(context).textTheme.bodyMedium,
                  ),
                  const SizedBox(height: 22),
                  SegmentedButton<ThemeMode>(
                    key: const Key('theme-control'),
                    showSelectedIcon: false,
                    segments: const [
                      ButtonSegment(
                        value: ThemeMode.light,
                        label: Text('Light'),
                        icon: Icon(Icons.light_mode_outlined),
                      ),
                      ButtonSegment(
                        value: ThemeMode.dark,
                        label: Text('Dark'),
                        icon: Icon(Icons.dark_mode_outlined),
                      ),
                    ],
                    selected: {themeController.mode},
                    onSelectionChanged: (selection) {
                      themeController.setMode(selection.first);
                    },
                  ),
                ],
              ),
            ),
            const SizedBox(height: 16),
            SmhCard(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const SmhSectionTitle('Local service'),
                  const SizedBox(height: 8),
                  Text(
                    'Connection health has not been checked. A live status will be added when backend integration begins.',
                    style: Theme.of(context).textTheme.bodyMedium,
                  ),
                  const SizedBox(height: 18),
                  Row(
                    children: [
                      Icon(
                        Icons.info_outline_rounded,
                        size: 18,
                        color: palette.muted,
                      ),
                      const SizedBox(width: 10),
                      Text(
                        'Status unavailable',
                        style: TextStyle(color: palette.muted),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
