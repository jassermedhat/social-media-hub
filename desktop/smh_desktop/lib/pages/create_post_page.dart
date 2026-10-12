import 'package:flutter/material.dart';

import '../theme/smh_theme.dart';
import '../widgets/smh_card.dart';

class CreatePostPage extends StatelessWidget {
  const CreatePostPage({super.key});

  @override
  Widget build(BuildContext context) {
    final palette = SmhTheme.paletteOf(context);
    final media = SmhCard(
      child: ConstrainedBox(
        constraints: const BoxConstraints(minHeight: 360),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const SmhSectionTitle('Media workspace'),
            const SizedBox(height: 20),
            ExpandedPlaceholder(
              icon: Icons.video_file_outlined,
              title: 'Your video will appear here',
              description:
                  'Video selection and playback are planned for Milestone 3.',
              color: palette.blue,
            ),
          ],
        ),
      ),
    );
    final details = SmhCard(
      child: ConstrainedBox(
        constraints: const BoxConstraints(minHeight: 360),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const SmhSectionTitle('Publishing details'),
            const SizedBox(height: 20),
            ExpandedPlaceholder(
              icon: Icons.edit_note_rounded,
              title: 'Shape your post',
              description:
                  'Title, shared caption, platform choices, and review will be added here.',
              color: palette.lavender,
            ),
          ],
        ),
      ),
    );
    return SmhPageFrame(
      eyebrow: 'Workspace / Create Post',
      title: 'Create Post',
      subtitle:
          'A focused place to prepare one video for the destinations you choose.',
      child: LayoutBuilder(
        builder: (context, constraints) {
          if (constraints.maxWidth < 880) {
            return Column(
              children: [media, const SizedBox(height: 16), details],
            );
          }
          return Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(flex: 5, child: media),
              const SizedBox(width: 16),
              Expanded(flex: 4, child: details),
            ],
          );
        },
      ),
    );
  }
}

class ExpandedPlaceholder extends StatelessWidget {
  const ExpandedPlaceholder({
    super.key,
    required this.icon,
    required this.title,
    required this.description,
    required this.color,
  });

  final IconData icon;
  final String title;
  final String description;
  final Color color;

  @override
  Widget build(BuildContext context) {
    final palette = SmhTheme.paletteOf(context);
    return Container(
      width: double.infinity,
      constraints: const BoxConstraints(minHeight: 280),
      padding: const EdgeInsets.all(32),
      decoration: BoxDecoration(
        color: color,
        borderRadius: BorderRadius.circular(20),
      ),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(icon, size: 42, color: palette.text),
          const SizedBox(height: 20),
          Text(
            title,
            textAlign: TextAlign.center,
            style: Theme.of(context).textTheme.titleLarge,
          ),
          const SizedBox(height: 8),
          Text(
            description,
            textAlign: TextAlign.center,
            style: Theme.of(context).textTheme.bodyMedium,
          ),
        ],
      ),
    );
  }
}
