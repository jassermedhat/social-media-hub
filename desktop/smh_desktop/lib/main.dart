import 'package:flutter/material.dart';
import 'package:window_manager/window_manager.dart';

import 'app.dart';
import 'state/theme_controller.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  final themeController = await SmhThemeController.load();
  await windowManager.ensureInitialized();
  await windowManager.waitUntilReadyToShow(
    const WindowOptions(
      size: Size(1280, 800),
      minimumSize: Size(900, 640),
      center: true,
      title: 'SMH — Social Media Hub',
    ),
    () async {
      await windowManager.show();
      await windowManager.focus();
    },
  );

  runApp(SmhApp(themeController: themeController));
}
