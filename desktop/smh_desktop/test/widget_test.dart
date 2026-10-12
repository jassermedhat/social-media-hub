import 'dart:ui' show PointerDeviceKind;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:smh_desktop/app.dart';
import 'package:smh_desktop/state/theme_controller.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('dashboard starts with honest unavailable states and navigates', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(1200, 800));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    await tester.pumpWidget(SmhApp(themeController: SmhThemeController()));

    expect(find.text('Overview'), findsOneWidget);
    expect(find.text('Not checked'), findsNWidgets(5));
    expect(
      find.text('Publishing history has not been loaded.'),
      findsOneWidget,
    );

    await tester.tap(find.byKey(const Key('nav-createPost')));
    await tester.pumpAndSettle();
    expect(find.text('Your video will appear here'), findsOneWidget);

    await tester.tap(find.byKey(const Key('nav-accounts')));
    await tester.pumpAndSettle();
    expect(find.text('Connected accounts'), findsOneWidget);
  });

  testWidgets('sidebar expands on hover and keeps the selected page', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(1200, 800));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    await tester.pumpWidget(SmhApp(themeController: SmhThemeController()));
    final sidebar = find.byKey(const Key('smh-sidebar'));
    expect(tester.getSize(sidebar).width, 84);

    await tester.tap(find.byKey(const Key('nav-history')));
    await tester.pumpAndSettle();
    expect(find.text('Publishing history'), findsOneWidget);

    final mouse = await tester.createGesture(kind: PointerDeviceKind.mouse);
    addTearDown(mouse.removePointer);
    await mouse.addPointer(location: const Offset(400, 400));
    await mouse.moveTo(const Offset(40, 120));
    await tester.pumpAndSettle();
    expect(tester.getSize(sidebar).width, 244);
    expect(find.text('Publishing history'), findsOneWidget);

    await mouse.moveTo(const Offset(400, 400));
    await tester.pumpAndSettle();
    expect(tester.getSize(sidebar).width, 84);
    expect(find.text('Publishing history'), findsOneWidget);
  });

  testWidgets('Settings switches between light and dark themes', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(1200, 800));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    final controller = SmhThemeController();
    await tester.pumpWidget(SmhApp(themeController: controller));
    await tester.tap(find.byKey(const Key('nav-settings')));
    await tester.pumpAndSettle();
    expect(controller.mode, ThemeMode.light);

    await tester.tap(find.text('Dark'));
    await tester.pumpAndSettle();
    expect(controller.mode, ThemeMode.dark);

    await tester.tap(find.text('Light'));
    await tester.pumpAndSettle();
    expect(controller.mode, ThemeMode.light);
  });

  testWidgets('compact desktop size remains usable with larger text', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(900, 640));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    await tester.pumpWidget(
      MediaQuery(
        data: const MediaQueryData(textScaler: TextScaler.linear(1.5)),
        child: SmhApp(themeController: SmhThemeController()),
      ),
    );
    await tester.tap(find.byKey(const Key('nav-createPost')));
    await tester.pumpAndSettle();
    expect(find.text('Your video will appear here'), findsOneWidget);
    expect(tester.takeException(), isNull);

    await tester.tap(find.byKey(const Key('nav-settings')));
    await tester.pumpAndSettle();
    expect(find.text('Appearance'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
  test('saved theme loads on the next controller', () async {
    SharedPreferences.setMockInitialValues({});
    final controller = await SmhThemeController.load();
    expect(controller.mode, ThemeMode.light);

    await controller.setMode(ThemeMode.dark);
    final reloaded = await SmhThemeController.load();
    expect(reloaded.mode, ThemeMode.dark);
  });
}
