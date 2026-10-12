import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

class SmhThemeController extends ChangeNotifier {
  SmhThemeController({
    SharedPreferences? preferences,
    ThemeMode initialMode = ThemeMode.light,
  }) : _preferences = preferences,
       _mode = initialMode;

  static const preferenceKey = 'smh_theme_mode';

  final SharedPreferences? _preferences;
  ThemeMode _mode;

  ThemeMode get mode => _mode;

  static Future<SmhThemeController> load() async {
    final preferences = await SharedPreferences.getInstance();
    final saved = preferences.getString(preferenceKey);
    return SmhThemeController(
      preferences: preferences,
      initialMode: saved == 'dark' ? ThemeMode.dark : ThemeMode.light,
    );
  }

  Future<void> setMode(ThemeMode mode) async {
    if (mode == _mode || mode == ThemeMode.system) return;
    _mode = mode;
    notifyListeners();
    await _preferences?.setString(preferenceKey, mode.name);
  }
}
