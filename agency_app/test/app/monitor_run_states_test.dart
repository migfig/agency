import 'dart:async';
import 'dart:convert';

import 'package:agencyapp/api/agency_client.dart';
import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/app/app_state.dart';
import 'package:agencyapp/app/home_page.dart';
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

/// T030 — the US5 run-state integration test (FR-006, SC-004,
/// US5-AS1/AS2/AS3): pump the real [HomePage] with a driven
/// [ResourceMonitor] and assert the panel is present and updating while
/// (a) a run is active, (b) a completed run is selected, and (c) no run is
/// selected. In every state the cards reflect the machine's *current*
/// usage: the trend gains a point and the value advances as new samples
/// are recorded.
///
/// No filesystem, no real network, no wall-clock dependence.
void main() {
  const ts = '2026-09-14T10:00:00+00:00';
  const runId = 'run-1';

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

  Map<String, Object> snapshot(String state) => {
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

  /// A scripted fake client: [history] drives `GET /runs` (startup
  /// auto-selects the newest entry) and [state] is the run's state for
  /// `GET /runs/{id}`; a completed run also answers `GET /runs/{id}/result`.
  MockClient fakeClient(
    List<Map<String, Object>> history, {
    String state = 'running',
  }) {
    return MockClient((request) async {
      final path = request.url.path;
      final headers = <String, String>{'content-type': 'application/json'};
      if (request.method == 'GET' && path == '/runs') {
        return http.Response(jsonEncode(history), 200, headers: headers);
      }
      if (request.method == 'GET' && path == '/runs/$runId') {
        return http.Response(jsonEncode(snapshot(state)), 200, headers: headers);
      }
      if (request.method == 'GET' && path == '/runs/$runId/result') {
        return http.Response(
          jsonEncode({
            'run_id': runId,
            'status': 'completed',
            'total_tokens': 100,
            'duration_seconds': 1.0,
            'nodes': <Object>[],
          }),
          200,
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

  Map<String, Object> historyEntry(String state) => {
        'run_id': runId,
        'workflow_name': 'demo',
        'started_at': ts,
        'state': state,
      };

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

  /// Pumps frames with elapsed time so the async startup
  /// (`listRuns` → `selectRun` → seed) settles and the default 200 ms
  /// MaterialApp theme crossfade completes.
  Future<void> settle(WidgetTester tester) async {
    for (var i = 0; i < 3; i++) {
      await tester.pump(const Duration(milliseconds: 250));
    }
  }

  LineChart cpuChart(WidgetTester tester) {
    final card = find.descendant(
      of: find.byWidgetPredicate(
          (w) => w is MonitorCard && w.title == 'CPU Usage'),
      matching: find.byType(LineChart),
    );
    return tester.widget<LineChart>(card);
  }

  /// The controller the on-screen panel renders from.
  ResourceMonitor panelMonitor(WidgetTester tester) => tester
      .widget<ResourceMonitorPanel>(find.byType(ResourceMonitorPanel))
      .monitor;

  /// Records one sample and proves the panel picked up the machine's
  /// *current* usage: the CPU trend gained exactly one point (the new
  /// sample) and the CPU card shows the new value (FR-006, SC-004).
  Future<void> expectUpdating(
      WidgetTester tester, double cpu, int ordinal) async {
    monitor.recordSample(sample(cpu));
    await tester.pump();
    expect(tester.takeException(), isNull,
        reason: 'the panel must render the new sample without error');
    expect(cpuChart(tester).data.lineBarsData.first.spots.length, ordinal,
        reason: 'the CPU trend gained exactly one point');
    expect(cpuChart(tester).data.lineBarsData.first.spots.last.y, cpu,
        reason: 'the newest point is the just-recorded sample');
    expect(find.text(cpu.toStringAsFixed(1)), findsOneWidget,
        reason: 'the CPU card shows the current value');
  }

  tearDown(() {
    monitor.dispose();
    events.close();
    app.dispose();
  });

  group('T030 monitor across run states (US5)', () {
    testWidgets('(a) active run: the panel is present and updating',
        (tester) async {
      SharedPreferences.setMockInitialValues(const {});
      await bootAndPump(tester,
          fakeClient([historyEntry('running')], state: 'running'));
      await settle(tester);

      expect(app.selectedRunId, runId);
      expect(app.selectedRunStore!.state!.runStatus, 'running',
          reason: 'the selected run is actively executing');
      expect(find.byType(SummaryBar), findsOneWidget);

      expect(find.byType(ResourceMonitorPanel), findsOneWidget,
          reason: 'the panel is present while a run is active (US5-AS1)');
      expect(identical(panelMonitor(tester), monitor), isTrue,
          reason: 'the panel renders from the app-level controller');

      await expectUpdating(tester, 12.34, 1);
      await expectUpdating(tester, 45.67, 2);
    });

    testWidgets(
        '(b) completed run selected: the panel still shows current '
        'machine usage', (tester) async {
      SharedPreferences.setMockInitialValues(const {});
      await bootAndPump(tester,
          fakeClient([historyEntry('completed')], state: 'completed'));
      await settle(tester);

      expect(app.selectedRunId, runId);
      expect(app.selectedRunStore!.state!.runStatus, 'completed',
          reason: 'a historical, completed run is selected');
      expect(find.byType(SummaryBar), findsOneWidget);

      expect(find.byType(ResourceMonitorPanel), findsOneWidget,
          reason: 'the panel is present on a historical run (US5-AS2)');
      expect(identical(panelMonitor(tester), monitor), isTrue);

      await expectUpdating(tester, 33.21, 1);
      await expectUpdating(tester, 55.55, 2);
    });

    testWidgets('(c) no run selected: the panel is still visible and updating',
        (tester) async {
      SharedPreferences.setMockInitialValues(const {});
      await bootAndPump(tester, fakeClient(<Map<String, Object>>[]));
      await settle(tester);

      expect(app.selectedRunId, isNull, reason: 'no run is selected');
      expect(find.text('No runs yet — start a workflow from the sidebar.'),
          findsOneWidget,
          reason: 'the no-run placeholder is shown');

      expect(find.byType(ResourceMonitorPanel), findsOneWidget,
          reason: 'the panel is present with no run selected (US5-AS3)');
      expect(identical(panelMonitor(tester), monitor), isTrue);

      await expectUpdating(tester, 77.77, 1);
      await expectUpdating(tester, 88.88, 2);
    });
  });
}
