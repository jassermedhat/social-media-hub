import 'package:flutter/material.dart';

import 'shell/smh_shell.dart';
import 'state/theme_controller.dart';
import 'theme/smh_theme.dart';

class SmhApp extends StatelessWidget {
  const SmhApp({super.key, required this.themeController});

  final SmhThemeController themeController;

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: themeController,
      builder: (context, _) => MaterialApp(
        title: 'SMH — Social Media Hub',
        debugShowCheckedModeBanner: false,
        theme: SmhTheme.light,
        darkTheme: SmhTheme.dark,
        themeMode: themeController.mode,
        themeAnimationDuration: const Duration(milliseconds: 250),
        home: SmhShell(themeController: themeController),
      ),
    );
  }
}
