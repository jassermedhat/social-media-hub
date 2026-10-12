import 'package:flutter/material.dart';

@immutable
class SmhPalette {
  const SmhPalette({
    required this.workspace,
    required this.sidebar,
    required this.sidebarText,
    required this.sidebarMuted,
    required this.sidebarActive,
    required this.surface,
    required this.text,
    required this.muted,
    required this.border,
    required this.blue,
    required this.lavender,
    required this.mint,
    required this.cream,
  });

  final Color workspace;
  final Color sidebar;
  final Color sidebarText;
  final Color sidebarMuted;
  final Color sidebarActive;
  final Color surface;
  final Color text;
  final Color muted;
  final Color border;
  final Color blue;
  final Color lavender;
  final Color mint;
  final Color cream;
}

abstract final class SmhTheme {
  static const lightPalette = SmhPalette(
    workspace: Color(0xFFFAFAF8),
    sidebar: Color(0xFF1C1D20),
    sidebarText: Color(0xFFF8F8F6),
    sidebarMuted: Color(0xFFACADB1),
    sidebarActive: Color(0xFF383B42),
    surface: Color(0xFFFFFFFF),
    text: Color(0xFF202126),
    muted: Color(0xFF666A73),
    border: Color(0xFFE7E7E2),
    blue: Color(0xFFDFEDF9),
    lavender: Color(0xFFEBE3F5),
    mint: Color(0xFFE0F0E4),
    cream: Color(0xFFF6EBD5),
  );

  static const darkPalette = SmhPalette(
    workspace: Color(0xFF25272C),
    sidebar: Color(0xFF141518),
    sidebarText: Color(0xFFF7F7F5),
    sidebarMuted: Color(0xFFA7A9B0),
    sidebarActive: Color(0xFF353941),
    surface: Color(0xFF303238),
    text: Color(0xFFF6F6F3),
    muted: Color(0xFFB2B4BA),
    border: Color(0xFF42454D),
    blue: Color(0xFF34495A),
    lavender: Color(0xFF4A405B),
    mint: Color(0xFF344D42),
    cream: Color(0xFF584D38),
  );

  static SmhPalette paletteOf(BuildContext context) =>
      Theme.of(context).brightness == Brightness.dark
      ? darkPalette
      : lightPalette;

  static final light = _build(lightPalette, Brightness.light);
  static final dark = _build(darkPalette, Brightness.dark);

  static ThemeData _build(SmhPalette palette, Brightness brightness) {
    final scheme = ColorScheme.fromSeed(
      seedColor: const Color(0xFF4166B2),
      brightness: brightness,
      surface: palette.workspace,
    );
    final base = ThemeData(
      useMaterial3: true,
      brightness: brightness,
      colorScheme: scheme,
      fontFamily: 'Segoe UI',
    );
    return base.copyWith(
      scaffoldBackgroundColor: palette.sidebar,
      textTheme: base.textTheme
          .apply(bodyColor: palette.text, displayColor: palette.text)
          .copyWith(
            headlineLarge: TextStyle(
              color: palette.text,
              fontSize: 34,
              height: 1.12,
              fontWeight: FontWeight.w700,
              letterSpacing: -1.1,
            ),
            titleLarge: TextStyle(
              color: palette.text,
              fontSize: 20,
              fontWeight: FontWeight.w700,
              letterSpacing: -0.4,
            ),
            bodyMedium: TextStyle(
              color: palette.muted,
              fontSize: 14,
              height: 1.45,
            ),
          ),
      dividerColor: palette.border,
      tooltipTheme: TooltipThemeData(
        decoration: BoxDecoration(
          color: palette.sidebarActive,
          borderRadius: BorderRadius.circular(10),
        ),
        textStyle: const TextStyle(color: Colors.white, fontSize: 13),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          padding: const EdgeInsets.symmetric(horizontal: 22, vertical: 17),
          shape: const StadiumBorder(),
        ),
      ),
      focusColor: scheme.primary.withValues(alpha: 0.18),
    );
  }
}

abstract final class SmhSpace {
  static const double xs = 8;
  static const double sm = 12;
  static const double md = 16;
  static const double lg = 24;
  static const double xl = 32;
  static const double xxl = 48;
  static const double radius = 24;
}
