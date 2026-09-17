import 'package:agencyapp/canvas/zoom_controls.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// The shared on-canvas zoom controls: a compact `- 100% + fit` bar with one
/// tappable action per control, showing the current scale as a rounded
/// percentage.
void main() {
  Future<void> pumpControls(
    WidgetTester tester, {
    double scale = 1.0,
    VoidCallback? onZoomIn,
    VoidCallback? onZoomOut,
    VoidCallback? onReset,
    VoidCallback? onFit,
  }) =>
      tester.pumpWidget(
        MaterialApp(
          theme: themeDataFor(ThemeMode.dark),
          home: Scaffold(
            body: Center(
              child: ZoomControls(
                surface: const Color(0xFF10181E),
                text: const Color(0xFFD7E4EA),
                scale: scale,
                onZoomIn: onZoomIn ?? () {},
                onZoomOut: onZoomOut ?? () {},
                onReset: onReset ?? () {},
                onFit: onFit ?? () {},
              ),
            ),
          ),
        ),
      );

  group('zoom controls', () {
    testWidgets('shows the four actions and the rounded percentage',
        (tester) async {
      await pumpControls(tester, scale: 1.25);
      expect(find.byIcon(Icons.remove), findsOneWidget);
      expect(find.byIcon(Icons.add), findsOneWidget);
      expect(find.byIcon(Icons.fit_screen), findsOneWidget);
      expect(find.text('125%'), findsOneWidget,
          reason: 'scale 1.25 renders as 125%');
    });

    testWidgets('each control fires its callback', (tester) async {
      final fired = <String>[];
      await pumpControls(
        tester,
        onZoomIn: () => fired.add('in'),
        onZoomOut: () => fired.add('out'),
        onReset: () => fired.add('reset'),
        onFit: () => fired.add('fit'),
      );

      await tester.tap(find.byIcon(Icons.add));
      await tester.tap(find.byIcon(Icons.remove));
      await tester.tap(find.text('100%'));
      await tester.tap(find.byIcon(Icons.fit_screen));
      await tester.pump();

      expect(fired, ['in', 'out', 'reset', 'fit']);
    });
  });
}
