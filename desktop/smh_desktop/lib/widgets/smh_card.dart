import 'package:flutter/material.dart';

import '../theme/smh_theme.dart';

class SmhCard extends StatelessWidget {
  const SmhCard({
    super.key,
    required this.child,
    this.color,
    this.padding = const EdgeInsets.all(SmhSpace.lg),
  });

  final Widget child;
  final Color? color;
  final EdgeInsetsGeometry padding;

  @override
  Widget build(BuildContext context) {
    final palette = SmhTheme.paletteOf(context);
    return Container(
      padding: padding,
      decoration: BoxDecoration(
        color: color ?? palette.surface,
        borderRadius: BorderRadius.circular(SmhSpace.radius),
        border: Border.all(color: palette.border.withValues(alpha: 0.7)),
      ),
      child: child,
    );
  }
}

class SmhPageFrame extends StatelessWidget {
  const SmhPageFrame({
    super.key,
    required this.eyebrow,
    required this.title,
    required this.subtitle,
    required this.child,
  });

  final String eyebrow;
  final String title;
  final String subtitle;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    final palette = SmhTheme.paletteOf(context);
    return LayoutBuilder(
      builder: (context, constraints) {
        final horizontal = constraints.maxWidth < 760 ? 24.0 : 44.0;
        return SingleChildScrollView(
          padding: EdgeInsets.fromLTRB(horizontal, 40, horizontal, 48),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 1480),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  eyebrow.toUpperCase(),
                  style: TextStyle(
                    color: palette.muted,
                    fontSize: 11,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 2.2,
                  ),
                ),
                const SizedBox(height: SmhSpace.sm),
                Text(title, style: Theme.of(context).textTheme.headlineLarge),
                const SizedBox(height: SmhSpace.sm),
                Text(subtitle, style: Theme.of(context).textTheme.bodyMedium),
                const SizedBox(height: SmhSpace.xl),
                child,
              ],
            ),
          ),
        );
      },
    );
  }
}

class SmhSectionTitle extends StatelessWidget {
  const SmhSectionTitle(this.title, {super.key, this.trailing});

  final String title;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Expanded(
          child: Text(title, style: Theme.of(context).textTheme.titleLarge),
        ),
        if (trailing != null) trailing!,
      ],
    );
  }
}
