import 'package:flutter/material.dart';

import '../theme/smh_theme.dart';
import '../widgets/smh_card.dart';

class AccountsPage extends StatelessWidget {
  const AccountsPage({super.key});

  @override
  Widget build(BuildContext context) {
    final palette = SmhTheme.paletteOf(context);
    const platforms = [
      ('Instagram', Icons.camera_alt_outlined),
      ('Facebook', Icons.facebook_rounded),
      ('TikTok', Icons.music_note_rounded),
      ('LinkedIn', Icons.business_center_outlined),
      ('YouTube', Icons.play_circle_outline_rounded),
    ];
    return SmhPageFrame(
      eyebrow: 'Workspace / Accounts',
      title: 'Connected accounts',
      subtitle:
          'A home for your publishing destinations. Account checks will be connected to the local backend in a later milestone.',
      child: LayoutBuilder(
        builder: (context, constraints) {
          final columns = constraints.maxWidth >= 1060
              ? 3
              : constraints.maxWidth >= 630
              ? 2
              : 1;
          final width = (constraints.maxWidth - (columns - 1) * 16) / columns;
          return Wrap(
            spacing: 16,
            runSpacing: 16,
            children: [
              for (final platform in platforms)
                SizedBox(
                  width: width,
                  child: SmhCard(
                    child: SizedBox(
                      height: 132,
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Icon(platform.$2, size: 28, color: palette.text),
                          const Spacer(),
                          Text(
                            platform.$1,
                            style: Theme.of(context).textTheme.titleLarge,
                          ),
                          const SizedBox(height: 4),
                          Text(
                            'Connection not checked',
                            style: Theme.of(context).textTheme.bodyMedium,
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
            ],
          );
        },
      ),
    );
  }
}
