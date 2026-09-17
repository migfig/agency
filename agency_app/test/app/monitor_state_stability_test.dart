import 'dart:async';
import 'dart:convert';

import 'package:agencyapp/api/agency_client.dart';
import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/app/app_state.dart';
import 'package:agencyapp/app/home_page.dart';
import 'package:agencyapp/canvas/classic/classic_view.dart';
import 'package:agencyapp/canvas/canvas_view.dart';
import 'package:agencyapp/canvas/summary_bar.dart';
import 'package:agencyapp/monitor/monitor_card.dart';
import 'package:agencyapp/monitor/resource_monitor.dart';
import 'package:agencyapp/monitor/resource_monitor_panel.dart';
import 'package:agencyapp/monitor/system_metrics.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// T031 — the US5 state-stability test (edge cases "Run switching while
/// open" and "Single instance"; contracts/ui.md "Z-order"): switching the
/// selected run (including a run that fails to load) and switching canvas
/// modes (abyss ↔ classic) never close, reset, or corrupt the panel; there
/// is a single panel instance shared across modes and runs; and the overlay
/// geometry is unchanged — the panel sits at
/// `Positioned(top: 8, right: 8, width: 250)` in the canvas-area Stack
/// (content-height, no `bottom` pin), below the run SummaryBar when one is
/// rendered (the Inspector's reference frame; no overlap of the SummaryBar).
///
/// No filesystem, no real network, no wall-clock dependence.
void main() {
  const ts = '2026-09-14T10:00:00+00:00';
  const activeRun = 'run-2';
  const completedRun = 'run-1';
  const failingRun = 'run-3';

  SystemMetrics sample(double cpu) => SystemMetrics(
        cpuUsage: cpu,
        ramUsage: cpu / 2,
        ramTotal: 16.0,
        ramUsed: 8.0,
        networkIn: cpu * 10,
        networkOut: cpu * 5,
        gpuUsage: cpu / 4,
        gpuMemoryHas: 24.0,
        gpuMemoryUsed: 3.0,
        gpuTemp: 55.0,
      );

  Map<String, Object> snapshot(String runId, String state) => {
        'run_id': runId,
        'workflow_name': 'demo',
        'state': state,
        'started_at': ts,
        'nodes': state == 'running'
            ? <String, Object>{}
            : <String, Object>{
                'a': {
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 100,
                  'duration_seconds': 1.0,
                },
              },
        'pending_inputs': <Object>[],
      };

  /// run-1 loads as a completed run; run-2 loads as the active (running)
  /// run and is newest, so startup auto-selects it; run-3 is *not* in the
  /// history and its status fetch fails (500) — the "run that fails to
  /// load" case: the selection sticks, the canvas shows `loading run…`,
  /// and the panel must survive.
  MockClient fakeClient() {
    return MockClient((request) async {
      final path = request.url.path;
      final headers = <String, String>{'content-type': 'application/json'};
      if (request.method == 'GET' && path == '/runs') {
        return http.Response(
          jsonEncode([
            {
              'run_id': completedRun,
              'workflow_name': 'demo',
              'started_at': ts,
              'state': 'completed',
            },
            {
              'run_id': activeRun,
              'workflow_name': 'demo',
              'started_at': ts,
              'state': 'running',
            },
          ]),
          200,
          headers: headers,
        );
      }
      if (request.method == 'GET' && path == '/runs/$completedRun') {
        return http.Response(
            jsonEncode(snapshot(completedRun, 'completed')), 200,
            headers: headers);
      }
      if (request.method == 'GET' && path == '/runs/$completedRun/result') {
        return http.Response(
          jsonEncode({
            'run_id': completedRun,
            'status': 'completed',
            'total_tokens': 100,
            'duration_seconds': 1.0,
            'nodes': <Object>[],
          }),
          200,
          headers: headers,
        );
      }
      if (request.method == 'GET' && path == '/runs/$activeRun') {
        return http.Response(
            jsonEncode(snapshot(activeRun, 'running')), 200,
            headers: headers);
      }
      if (request.method == 'GET' && path == '/runs/$failingRun') {
        return http.Response(
          jsonEncode({'error': {'code': 'internal_error', 'message': 'boom'}}),
          500,
          headers: headers,
        );
      }
      if (request.method == 'GET' && path == '/health') {
        return http.Response(jsonEncode({'status': 'ok'}), 200,
            headers: headers);
      }
      return http.Response(
        jsonEncode({'error': {'code': 'unknown_run', 'message': 'nope'}}),
        404,
        headers: headers,
      );
    });
  }

  late AppState app;
  late ResourceMonitor monitor;
  late AgencyClient client;
  late StreamController<EventFrame> events;

  Future<void> pumpHome(WidgetTester tester) async {
    await tester.pumpWidget(
      ListenableBuilder(
        listenable: app,
        builder: (context, _) => MaterialApp(
          title: 'Agency',
          debugShowCheckedModeBanner: false,
          theme: themeDataFor(app.themeMode),
          home: HomePage(
            app: app,
            client: client,
            entries: const [],
            eventStream: events.stream,
            monitor: monitor,
          ),
        ),
      ),
    );
  }

  /// Fresh app + monitor over the current mock prefs, then pump.
  Future<void> bootAndPump(WidgetTester tester, http.Client httpClient) async {
    app = AppState();
    await app.init();
    monitor = ResourceMonitor(await SharedPreferences.getInstance());
    client = AgencyClient('http://fake', httpClient: httpClient);
    events = StreamController<EventFrame>.broadcast();
    await pumpHome(tester);
  }

  /// Pumps frames with elapsed time so the async startup settles and the
  /// default 200 ms MaterialApp theme crossfade completes.
  Future<void> settle(WidgetTester tester) async {
    for (var i = 0; i < 3; i++) {
      await tester.pump(const Duration(milliseconds: 250));
    }
  }

  /// The panel's own `Positioned` (its build root), so the contract's
  /// geometry can be asserted exactly.
  Positioned monitorPositioned(WidgetTester tester) {
    final panel = find.byType(ResourceMonitorPanel);
    return tester.widgetList<Positioned>(
      find.descendant(of: panel, matching: find.byType(Positioned)),
    ).first;
  }

  /// The canvas-area Stack that hosts the panel (its nearest Stack
  /// ancestor — the panel is a direct child of it).
  Rect areaRect(WidgetTester tester) {
    final stack = find.ancestor(
      of: find.byType(ResourceMonitorPanel),
      matching: find.byType(Stack),
    );
    return tester.getRect(stack.first);
  }

  /// The normative overlay geometry (contracts/ui.md "Z-order"): the panel
  /// is bounded by `Positioned(top: 8, right: 8, bottom: 8, width: 250)`
  /// in the canvas-area Stack, and the visible card is content-sized
  /// (exactly the header plus its cards) and top-aligned in that region —
  /// 8 px from the area's top/right edges, 250 wide, 8 px below the
  /// SummaryBar when one is rendered (the Inspector's reference frame),
  /// and never extending past the region's bottom.
  void expectPanelGeometry(WidgetTester tester) {
    final p = monitorPositioned(tester);
    expect(p.top, 8.0);
    expect(p.right, 8.0);
    expect(p.bottom, 8.0);
    expect(p.width, 250.0);

    final area = areaRect(tester);
    // The first Card descendant (preorder) is the panel's own card; the
    // metric cards (each a MonitorCard) are its descendants.
    final card = find.descendant(
      of: find.byType(ResourceMonitorPanel),
      matching: find.byType(Card),
    );
    expect(card, findsWidgets, reason: 'the visible panel is its card');
    final rect = tester.getRect(card.first);
    expect(rect.width, 250.0);
    expect(rect.top, area.top + 8.0);
    expect(rect.right, area.right - 8.0);
    expect(rect.bottom, lessThanOrEqualTo(area.bottom - 8.0),
        reason: 'the content-sized panel must stay within its region');
    if (tester.any(find.byType(SummaryBar))) {
      final bar = tester.getRect(find.byType(SummaryBar));
      expect(rect.top, bar.bottom + 8.0,
          reason: 'the panel sits below the SummaryBar (no overlap)');
    }
  }

  /// The single-instance guarantee: exactly one panel in the tree, reading
  /// the app-level [monitor] controller.
  void expectSinglePanel(WidgetTester tester) {
    expect(find.byType(ResourceMonitorPanel), findsOneWidget,
        reason: 'exactly one panel instance in the tree');
    expect(
        identical(
            tester
                .widget<ResourceMonitorPanel>(find.byType(ResourceMonitorPanel))
                .monitor,
            monitor),
        isTrue,
        reason: 'the panel renders from the app-level controller');
  }

  /// The no-reset / no-corruption guarantee after a switch: nothing threw,
  /// the controller's state is untouched (sample ordinal, trend window),
  /// and the latest recorded value is still rendered.
  void expectPanelStable(
      WidgetTester tester,
      {required int ordinal,
      required int spots,
      required double lastCpu}) {
    expect(tester.takeException(), isNull,
        reason: 'the switch must not throw or break the layout');
    expectSinglePanel(tester);
    expectPanelGeometry(tester);
    expect(monitor.sampleOrdinal, ordinal,
        reason: 'the controller was not reset by the switch');
    final window = tester
        .widget<LineChart>(find.descendant(
            of: find.byWidgetPredicate(
                (w) => w is MonitorCard && w.title == 'CPU Usage'),
            matching: find.byType(LineChart)))
        .data
        .lineBarsData
        .first
        .spots;
    expect(window.length, spots, reason: 'the trend window survived intact');
    expect(window.last.y, lastCpu,
        reason: 'the newest point is still the last recorded sample');
    expect(find.text(lastCpu.toStringAsFixed(1)), findsOneWidget,
        reason: 'the latest recorded value is still rendered');
  }

  tearDown(() {
    monitor.dispose();
    events.close();
    app.dispose();
  });

  group('T031 monitor state stability (US5)', () {
    testWidgets(
        'switching the selected run — including a run that fails to load — '
        'never closes, resets, or corrupts the panel', (tester) async {
      SharedPreferences.setMockInitialValues(const {});
      await bootAndPump(tester, fakeClient());
      await settle(tester);

      // (i) The active run (run-2, newest) is auto-selected on startup.
      expect(app.selectedRunId, activeRun);
      expect(app.selectedRunStore!.state!.runStatus, 'running');
      monitor.recordSample(sample(11.1));
      await tester.pump();
      expectPanelStable(tester, ordinal: 1, spots: 1, lastCpu: 11.1);

      // (ii) Switch to the historical, completed run.
      app.selectRun(completedRun);
      await settle(tester);
      expect(app.selectedRunId, completedRun);
      expect(app.selectedRunStore!.state!.runStatus, 'completed');
      expectPanelStable(tester, ordinal: 1, spots: 1, lastCpu: 11.1);

      // (iii) Switch to a run that fails to load: the selection sticks,
      // the canvas shows `loading run…`, and the panel is untouched.
      app.selectRun(failingRun);
      await settle(tester);
      expect(app.selectedRunId, failingRun,
          reason: 'the failed load keeps the selection');
      expect(find.text('loading run…'), findsOneWidget,
          reason: 'the canvas is still loading the failed run');
      expectPanelStable(tester, ordinal: 1, spots: 1, lastCpu: 11.1);

      // (iv) Switch back to the active run — and keep updating.
      app.selectRun(activeRun);
      await settle(tester);
      expect(app.selectedRunId, activeRun);
      expect(app.selectedRunStore!.state!.runStatus, 'running');
      expectPanelStable(tester, ordinal: 1, spots: 1, lastCpu: 11.1);

      monitor.recordSample(sample(22.2));
      await tester.pump();
      expectPanelStable(tester, ordinal: 2, spots: 2, lastCpu: 22.2);
    });

    testWidgets(
        'switching canvas modes (abyss ↔ classic) never closes, resets, or '
        'corrupts the panel; single instance; geometry unchanged',
        (tester) async {
      SharedPreferences.setMockInitialValues(const {});
      await bootAndPump(tester, fakeClient());
      await settle(tester);

      expect(app.canvasMode, CanvasMode.abyss);
      monitor.recordSample(sample(7.7));
      await tester.pump();

      // (i) Abyss mode: the panel coexists with the abyss canvas.
      expect(find.byType(CanvasView), findsOneWidget);
      expectPanelStable(tester, ordinal: 1, spots: 1, lastCpu: 7.7);

      // (ii) Switch to classic.
      app.setCanvasMode(CanvasMode.classic);
      await settle(tester);
      expect(app.canvasMode, CanvasMode.classic);
      expect(find.byType(ClassicCanvasView), findsOneWidget,
          reason: 'the classic canvas is rendered');
      expect(find.byType(CanvasView), findsNothing,
          reason: 'the abyss canvas is replaced, not stacked');
      expectPanelStable(tester, ordinal: 1, spots: 1, lastCpu: 7.7);

      // (iii) Back to abyss.
      app.setCanvasMode(CanvasMode.abyss);
      await settle(tester);
      expect(app.canvasMode, CanvasMode.abyss);
      expect(find.byType(CanvasView), findsOneWidget);
      expectPanelStable(tester, ordinal: 1, spots: 1, lastCpu: 7.7);

      // (iv) Deselecting to the no-run state (the panel widget unmounts
      // and remounts over the same controller) keeps the geometry.
      app.deselectRun();
      await settle(tester);
      expect(app.selectedRunId, isNull);
      expect(find.text('No runs yet — start a workflow from the sidebar.'),
          findsOneWidget);
      expectPanelStable(tester, ordinal: 1, spots: 1, lastCpu: 7.7);
    });
  });
}
