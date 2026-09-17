import 'dart:math' as math;

import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/canvas/canvas_view.dart';
import 'package:agencyapp/canvas/scene_painter.dart';
import 'package:agencyapp/canvas/zoom_controls.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:agencyapp/run/run_store.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:flutter/gestures.dart' show PointerDeviceKind, PointerScrollEvent;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

/// The Abyss canvas view's keyboard viewport gestures (arrows pan, +/− zoom,
/// 0 resets, F fits), cursor-anchored wheel zoom, and the shared
/// [ZoomControls] bar.
void main() {
  const ts = '2026-08-29T10:00:00+00:00';
  const runId = 'run-1';

  WorkflowDefinition def() => parseWorkflowDefinition('''
name: demo
nodes:
  a: {id: a, type: agent, model: draft}
  b: {id: b, type: agent, model: draft}
  c: {id: c, type: agent, model: draft}
  d: {id: d, type: agent, model: draft}
  e: {id: e, type: agent, model: draft}
edges:
  - {from_id: a, to_id: b}
  - {from_id: b, to_id: c}
  - {from_id: a, to_id: d}
  - {from_id: d, to_id: e}
''');

  Map<String, Object> settledSnapshot() => {
        'run_id': runId,
        'workflow_name': 'settled',
        'state': 'completed',
        'started_at': ts,
        'nodes': <String, Object>{
          'a': {'status': 'completed', 'attempt': 1, 'tokens': 12, 'duration_seconds': 1.5},
          'b': {'status': 'completed', 'attempt': 1, 'tokens': 30, 'duration_seconds': 2.0},
          'c': {'status': 'skipped', 'attempt': 1, 'reason': 'unresolved_binding'},
          'd': {'status': 'skipped', 'attempt': 1, 'reason': 'condition false'},
          'e': {'status': 'skipped', 'attempt': 1, 'reason': 'unresolved_binding'},
        },
        'pending_inputs': <Object>[],
      };

  Future<void> pumpCanvas(WidgetTester tester) async {
    final d = def();
    final store = RunStore(workflowDefinition: d)
      ..seed(RunStatusView.fromJson(settledSnapshot()));
    await tester.pumpWidget(
      MaterialApp(
        theme: themeDataFor(ThemeMode.dark),
        home: Scaffold(body: CanvasView(store: store, definition: d)),
      ),
    );
    await tester.pump(); // post-frame zoom-to-fit + canvas focus
  }

  CanvasViewState canvas(WidgetTester tester) =>
      tester.state<CanvasViewState>(find.byType(CanvasView));

  Offset worldAt(Offset focal, CanvasViewState st) =>
      Offset((focal.dx - st.pan.dx) / st.scale,
          (focal.dy - st.pan.dy) / st.scale);

  double fitScale(CanvasViewState st, Size size) {
    final b = layoutBounds(st.scene!.layout);
    return math.min(size.width / b.width, size.height / b.height)
        .clamp(0.25, 4.0);
  }

  Future<void> wheelZoom(
    WidgetTester tester,
    Offset location,
    Offset scrollDelta,
  ) async {
    final gesture = await tester.createGesture(kind: PointerDeviceKind.mouse);
    await gesture.addPointer(location: location);
    await tester.pump();
    await gesture.updateWithCustomEvent(PointerScrollEvent(
      position: location,
      scrollDelta: scrollDelta,
    ));
    await gesture.removePointer();
    await tester.pump();
  }

  group('abyss canvas keyboard gestures + zoom controls (FR-016)', () {
    testWidgets('arrow keys pan the scene by 48 px in the arrow direction',
        (tester) async {
      await pumpCanvas(tester);
      final p0 = canvas(tester).pan;
      final scale0 = canvas(tester).scale;

      expect(await tester.sendKeyEvent(LogicalKeyboardKey.arrowRight), isTrue,
          reason: 'the canvas handles arrow keys');
      await tester.pump();
      expect(canvas(tester).pan, p0 + const Offset(48, 0));
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pump();
      expect(canvas(tester).pan, p0 + const Offset(48, 48));
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
      await tester.pump();
      expect(canvas(tester).pan, p0, reason: 'the round trip cancels');
      expect(canvas(tester).scale, scale0,
          reason: 'panning never changes the zoom');
    });

    testWidgets('Shift + arrow doubles the pan step', (tester) async {
      await pumpCanvas(tester);
      final p0 = canvas(tester).pan;
      await tester.sendKeyDownEvent(LogicalKeyboardKey.shiftLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowRight);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.shiftLeft);
      await tester.pump();
      expect(canvas(tester).pan, p0 + const Offset(96, 0));
    });

    testWidgets('/ + / - zoom 1.25x at the center, 0 resets, F fits, '
        'clamped 0.25-4', (tester) async {
      await pumpCanvas(tester);
      const center = Offset(400, 300); // 800x600 test surface
      final st0 = canvas(tester);
      final scale0 = st0.scale;
      final worldBefore = worldAt(center, st0);

      await tester.sendKeyEvent(LogicalKeyboardKey.equal);
      await tester.pump();
      final st1 = canvas(tester);
      expect(st1.scale, closeTo(scale0 * 1.25, 1e-9),
          reason: '/= zooms in by 1.25');
      expect(worldAt(center, st1).dx, closeTo(worldBefore.dx, 1e-6),
          reason: 'the viewport-center world point stays fixed');

      await tester.sendKeyEvent(LogicalKeyboardKey.minus);
      await tester.pump();
      expect(canvas(tester).scale, closeTo(scale0, 1e-9),
          reason: '- zooms back out by 1/1.25');

      await tester.sendKeyEvent(LogicalKeyboardKey.digit0);
      await tester.pump();
      expect(canvas(tester).scale, closeTo(1.0, 1e-9),
          reason: 'digit0 resets the zoom to 100%');

      final st = canvas(tester);
      await tester.sendKeyEvent(LogicalKeyboardKey.keyF);
      await tester.pump();
      expect(canvas(tester).scale,
          closeTo(fitScale(st, const Size(800, 600)), 1e-9),
          reason: 'f zooms to fit');

      for (var i = 0; i < 30; i++) {
        await tester.sendKeyEvent(LogicalKeyboardKey.minus);
      }
      await tester.pump();
      expect(canvas(tester).scale, closeTo(0.25, 1e-9),
          reason: 'keyboard zoom-out clamps at 0.25');
      for (var i = 0; i < 30; i++) {
        await tester.sendKeyEvent(LogicalKeyboardKey.equal);
      }
      await tester.pump();
      expect(canvas(tester).scale, closeTo(4.0, 1e-9),
          reason: 'keyboard zoom-in clamps at 4');
    });

    testWidgets('wheel zoom holds the world point under the cursor fixed',
        (tester) async {
      await pumpCanvas(tester);
      final st0 = canvas(tester);
      // Capture the scale value (not the live State) before the gesture mutates it.
      final scale0 = st0.scale;
      const cursor = Offset(500, 80); // empty canvas, clear of the controls
      final worldBefore = worldAt(cursor, st0);

      await wheelZoom(tester, cursor, const Offset(0, -100));
      final st1 = canvas(tester);
      expect(st1.scale, closeTo(scale0 * math.pow(1.0015, 100).toDouble(), 1e-9),
          reason: 'scroll up zooms in by 1.0015^-dy');
      expect(worldAt(cursor, st1).dx, closeTo(worldBefore.dx, 1e-6),
          reason: 'zooming holds the world point under the cursor (x)');
      expect(worldAt(cursor, st1).dy, closeTo(worldBefore.dy, 1e-6),
          reason: 'zooming holds the world point under the cursor (y)');
    });

    testWidgets('zoom controls sit bottom-right and drive the view '
        'transform', (tester) async {
      await pumpCanvas(tester);
      final scale0 = canvas(tester).scale;

      expect(find.byType(ZoomControls), findsOneWidget);
      expect(tester.getBottomRight(find.byType(ZoomControls)),
          const Offset(792, 592),
          reason: 'pinned 8 px from the bottom-right of the 800x600 view');
      expect(find.text('${(scale0 * 100).round()}%'), findsOneWidget,
          reason: 'the label shows the current scale');

      await tester.tap(find.text('${(scale0 * 100).round()}%'));
      await tester.pump();
      expect(canvas(tester).scale, closeTo(1.0, 1e-9),
          reason: 'tapping the label resets to 100%');

      await tester.tap(find.byIcon(Icons.remove));
      await tester.pump();
      expect(canvas(tester).scale, closeTo(0.8, 1e-9));

      await tester.tap(find.byIcon(Icons.add));
      await tester.pump();
      expect(canvas(tester).scale, closeTo(1.0, 1e-9));

      await tester.tap(find.byIcon(Icons.fit_screen));
      await tester.pump();
      expect(canvas(tester).scale,
          closeTo(fitScale(canvas(tester), const Size(800, 600)), 1e-9));
    });

    testWidgets('tapping the canvas re-acquires keyboard focus',
        (tester) async {
      await pumpCanvas(tester);
      FocusManager.instance.primaryFocus?.unfocus();
      await tester.pump();

      final p0 = canvas(tester).pan;
      await tester.tapAt(const Offset(400, 80)); // empty canvas spot
      await tester.pump();
      expect(await tester.sendKeyEvent(LogicalKeyboardKey.arrowRight), isTrue,
          reason: 'after a canvas tap the keyboard works again');
      await tester.pump();
      expect(canvas(tester).pan, p0 + const Offset(48, 0));
    });
  });
}
