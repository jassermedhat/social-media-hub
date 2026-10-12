import 'package:flutter/material.dart';

import '../theme/smh_theme.dart';
import '../widgets/smh_card.dart';

class HistoryPage extends StatelessWidget {
  const HistoryPage({super.key});

  @override
  Widget build(BuildContext context) {
    final palette = SmhTheme.paletteOf(context);
    return SmhPageFrame(
      eyebrow: 'Workspace / History',
      title: 'Publishing history',
      subtitle:
          'Review previous attempts and per-platform results when history is connected.',
      child: SmhCard(
        child: SizedBox(
          width: double.infinity,
          height: 310,
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Container(
                padding: const EdgeInsets.all(18),
                decoration: BoxDecoration(
                  color: palette.cream,
                  borderRadius: BorderRadius.circular(20),
                ),
                child: Icon(
                  Icons.history_rounded,
                  size: 34,
                  color: palette.text,
                ),
              ),
              const SizedBox(height: 22),
              Text(
                'History is not loaded yet',
                style: Theme.of(context).textTheme.titleLarge,
              ),
              const SizedBox(height: 8),
              Text(
                'Recorded publishing attempts will appear here in a later milestone.',
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.bodyMedium,
              ),
            ],
          ),
        ),
      ),
    );
  }
}
