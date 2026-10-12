import 'package:flutter/material.dart';

import '../pages/accounts_page.dart';
import '../pages/create_post_page.dart';
import '../pages/dashboard_page.dart';
import '../pages/history_page.dart';
import '../pages/settings_page.dart';
import '../state/theme_controller.dart';
import '../theme/smh_theme.dart';

enum SmhDestination {
  dashboard('Dashboard', Icons.grid_view_rounded),
  createPost('Create Post', Icons.add_circle_outline_rounded),
  accounts('Accounts', Icons.people_alt_outlined),
  history('History', Icons.history_rounded),
  settings('Settings', Icons.tune_rounded);

  const SmhDestination(this.label, this.icon);
  final String label;
  final IconData icon;
}

class SmhShell extends StatefulWidget {
  const SmhShell({super.key, required this.themeController});

  final SmhThemeController themeController;

  @override
  State<SmhShell> createState() => _SmhShellState();
}

class _SmhShellState extends State<SmhShell> {
  SmhDestination _selected = SmhDestination.dashboard;
  bool _hovering = false;
  bool _focused = false;

  bool get _expanded => _hovering || _focused;

  void _select(SmhDestination destination) {
    setState(() => _selected = destination);
  }

  @override
  Widget build(BuildContext context) {
    final palette = SmhTheme.paletteOf(context);
    final pages = [
      DashboardPage(onCreatePost: () => _select(SmhDestination.createPost)),
      const CreatePostPage(),
      const AccountsPage(),
      const HistoryPage(),
      SettingsPage(themeController: widget.themeController),
    ];

    return Scaffold(
      body: Row(
        children: [
          Focus(
            onFocusChange: (focused) => setState(() => _focused = focused),
            child: MouseRegion(
              onEnter: (_) => setState(() => _hovering = true),
              onExit: (_) => setState(() => _hovering = false),
              child: AnimatedContainer(
                key: const Key('smh-sidebar'),
                duration: const Duration(milliseconds: 240),
                curve: Curves.easeInOutCubic,
                width: _expanded ? 244 : 84,
                color: palette.sidebar,
                child: LayoutBuilder(
                  builder: (context, constraints) {
                    final showLabels = constraints.maxWidth >= 180;
                    return Column(
                      children: [
                        const SizedBox(height: 28),
                        Padding(
                          padding: const EdgeInsets.symmetric(horizontal: 16),
                          child: Row(
                            children: [
                              Container(
                                width: 48,
                                height: 48,
                                alignment: Alignment.center,
                                decoration: BoxDecoration(
                                  color: palette.sidebarActive,
                                  borderRadius: BorderRadius.circular(16),
                                ),
                                child: Text(
                                  'S',
                                  style: TextStyle(
                                    color: palette.sidebarText,
                                    fontSize: 22,
                                    fontWeight: FontWeight.w800,
                                  ),
                                ),
                              ),
                              if (showLabels) ...[
                                const SizedBox(width: 12),
                                Expanded(
                                  child: Text(
                                    'SMH',
                                    overflow: TextOverflow.fade,
                                    softWrap: false,
                                    style: TextStyle(
                                      color: palette.sidebarText,
                                      fontSize: 21,
                                      fontWeight: FontWeight.w800,
                                      letterSpacing: -0.8,
                                    ),
                                  ),
                                ),
                              ],
                            ],
                          ),
                        ),
                        const SizedBox(height: 54),
                        for (final destination in SmhDestination.values)
                          _NavigationItem(
                            destination: destination,
                            selected: _selected == destination,
                            showLabel: showLabels,
                            onTap: () => _select(destination),
                          ),
                        const Spacer(),
                        Padding(
                          padding: const EdgeInsets.fromLTRB(20, 0, 20, 24),
                          child: showLabels
                              ? Text(
                                  'IPS AUTOMATION  •  V1',
                                  style: TextStyle(
                                    color: palette.sidebarMuted,
                                    fontSize: 10,
                                    fontWeight: FontWeight.w700,
                                    letterSpacing: 1.2,
                                  ),
                                )
                              : Icon(
                                  Icons.circle,
                                  size: 7,
                                  color: palette.sidebarMuted,
                                ),
                        ),
                      ],
                    );
                  },
                ),
              ),
            ),
          ),
          Expanded(
            child: Container(
              margin: const EdgeInsets.fromLTRB(0, 12, 12, 12),
              decoration: BoxDecoration(
                color: palette.workspace,
                borderRadius: BorderRadius.circular(30),
              ),
              clipBehavior: Clip.antiAlias,
              child: IndexedStack(index: _selected.index, children: pages),
            ),
          ),
        ],
      ),
    );
  }
}

class _NavigationItem extends StatelessWidget {
  const _NavigationItem({
    required this.destination,
    required this.selected,
    required this.showLabel,
    required this.onTap,
  });

  final SmhDestination destination;
  final bool selected;
  final bool showLabel;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final palette = SmhTheme.paletteOf(context);
    final foreground = selected ? palette.sidebarText : palette.sidebarMuted;
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 0, 12, 6),
      child: Tooltip(
        message: destination.label,
        child: Semantics(
          selected: selected,
          button: true,
          label: destination.label,
          child: Material(
            color: selected ? palette.sidebarActive : Colors.transparent,
            borderRadius: BorderRadius.circular(16),
            child: InkWell(
              key: Key('nav-${destination.name}'),
              onTap: onTap,
              borderRadius: BorderRadius.circular(16),
              hoverColor: palette.sidebarActive,
              focusColor: palette.sidebarActive,
              child: SizedBox(
                height: 52,
                child: Row(
                  children: [
                    const SizedBox(width: 16),
                    Icon(destination.icon, color: foreground, size: 22),
                    if (showLabel) ...[
                      const SizedBox(width: 18),
                      Expanded(
                        child: Text(
                          destination.label,
                          overflow: TextOverflow.fade,
                          softWrap: false,
                          style: TextStyle(
                            color: foreground,
                            fontSize: 14,
                            fontWeight: selected
                                ? FontWeight.w700
                                : FontWeight.w500,
                          ),
                        ),
                      ),
                    ],
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
