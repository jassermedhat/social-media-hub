import 'package:flutter/material.dart';

import '../theme/smh_theme.dart';
import '../widgets/smh_card.dart';

class DashboardPage extends StatelessWidget {
  const DashboardPage({super.key, required this.onCreatePost});

  final VoidCallback onCreatePost;

  @override
  Widget build(BuildContext context) {
    final palette = SmhTheme.paletteOf(context);
    const metrics = [
      (
        'Connected Accounts',
        'Connection status is not loaded',
        Icons.link_rounded,
      ),
      (
        'Ready to Publish',
        'Readiness has not been verified',
        Icons.check_circle_outline_rounded,
      ),
      ('Publishing Attempts', 'History is not loaded', Icons.send_outlined),
      (
        'Pending Approvals',
        'No approval data source yet',
        Icons.hourglass_empty_rounded,
      ),
    ];
    const platforms = [
      ('Instagram', Icons.camera_alt_outlined),
      ('Facebook', Icons.facebook_rounded),
      ('TikTok', Icons.music_note_rounded),
      ('LinkedIn', Icons.business_center_outlined),
      ('YouTube', Icons.play_circle_outline_rounded),
    ];
    final accents = [
      palette.blue,
      palette.lavender,
      palette.mint,
      palette.cream,
    ];

    return SmhPageFrame(
      eyebrow: 'Workspace / Overview',
      title: 'Overview',
      subtitle:
          'Your publishing workspace at a glance. Live account data will appear when the backend is connected.',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          LayoutBuilder(
            builder: (context, constraints) {
              final columns = constraints.maxWidth >= 1180
                  ? 4
                  : constraints.maxWidth >= 580
                  ? 2
                  : 1;
              final width =
                  (constraints.maxWidth - (columns - 1) * 16) / columns;
              return Wrap(
                spacing: 16,
                runSpacing: 16,
                children: [
                  for (var index = 0; index < metrics.length; index++)
                    SizedBox(
                      width: width,
                      child: SmhCard(
                        color: accents[index],
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Icon(
                              metrics[index].$3,
                              color: palette.text,
                              size: 23,
                            ),
                            const SizedBox(height: 20),
                            Text(
                              '\u2014',
                              style: TextStyle(
                                color: palette.text,
                                fontSize: 28,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                            const SizedBox(height: 3),
                            Text(
                              metrics[index].$1,
                              style: TextStyle(
                                color: palette.text,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                            Text(
                              metrics[index].$2,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(
                                color: palette.muted,
                                fontSize: 11,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                ],
              );
            },
          ),
          const SizedBox(height: 34),
          LayoutBuilder(
            builder: (context, constraints) {
              final narrow = constraints.maxWidth < 900;
              final accountCard = SmhCard(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const SmhSectionTitle('Platform connections'),
                    const SizedBox(height: 6),
                    Text(
                      'Connection state will be checked in a later milestone.',
                      style: Theme.of(context).textTheme.bodyMedium,
                    ),
                    const SizedBox(height: 20),
                    for (final platform in platforms)
                      Padding(
                        padding: const EdgeInsets.symmetric(vertical: 10),
                        child: Row(
                          children: [
                            Icon(platform.$2, size: 21, color: palette.text),
                            const SizedBox(width: 14),
                            Expanded(
                              child: Text(
                                platform.$1,
                                style: TextStyle(
                                  color: palette.text,
                                  fontWeight: FontWeight.w600,
                                ),
                              ),
                            ),
                            Text(
                              'Not checked',
                              style: TextStyle(
                                color: palette.muted,
                                fontSize: 12,
                              ),
                            ),
                          ],
                        ),
                      ),
                  ],
                ),
              );
              final actionCard = Column(
                children: [
                  SmhCard(
                    color: palette.mint,
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Icon(
                          Icons.add_circle_outline_rounded,
                          color: palette.text,
                          size: 28,
                        ),
                        const SizedBox(height: 24),
                        Text(
                          'Ready to create?',
                          style: Theme.of(context).textTheme.titleLarge,
                        ),
                        const SizedBox(height: 8),
                        Text(
                          'Open the post workspace. Media selection and publishing arrive in later milestones.',
                          style: Theme.of(context).textTheme.bodyMedium,
                        ),
                        const SizedBox(height: 20),
                        FilledButton.icon(
                          onPressed: onCreatePost,
                          icon: const Icon(Icons.arrow_forward_rounded),
                          label: const Text('Create Post'),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 16),
                  SmhCard(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const SmhSectionTitle('Recent activity'),
                        const SizedBox(height: 12),
                        Text(
                          'Publishing history has not been loaded.',
                          style: Theme.of(context).textTheme.bodyMedium,
                        ),
                      ],
                    ),
                  ),
                ],
              );
              if (narrow) {
                return Column(
                  children: [
                    accountCard,
                    const SizedBox(height: 16),
                    actionCard,
                  ],
                );
              }
              return Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Expanded(flex: 6, child: accountCard),
                  const SizedBox(width: 16),
                  Expanded(flex: 4, child: actionCard),
                ],
              );
            },
          ),
        ],
      ),
    );
  }
}
