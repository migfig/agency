import 'dart:async';
import 'dart:convert';

import 'package:agencyapp/api/agency_client.dart';
import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/app/app_state.dart';
import 'package:agencyapp/app/home_page.dart';
import 'package:agencyapp/app/sidebar/history_list.dart';
import 'package:agencyapp/canvas/canvas_scene.dart';
import 'package:agencyapp/canvas/canvas_view.dart';
import 'package:agencyapp/canvas/state_encodings.dart';
import 'package:agencyapp/catalog/catalog_entry.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

/// T030 — startup-selection tests (research R14): the server's history is
/// ascending by `started_at`, so the newest run is the LAST element and must
/// be auto-selected and rendered with no user action; an empty history shows
/// the empty state with the sidebar start action still usable.
///
/// No filesystem, no real network, no wall-clock dependence.
void main() {
  const oldTs = '2026-08-29T08:00:00Z';
  const midTs = '2026-08-29T09:00:00Z';
  const newTs = '2026-08-29T10:00:00Z';

  WorkflowDefinition demoDef() => parseWorkflowDefinition('''
name: demo
nodes:
  a: {id: a, type: agent, model: draft}
  b: {id: b, type: agent, model: draft}
''');

  RunHistoryEntry historyEntry(String runId, String ts, String state) =>
      RunHistoryEntry(
        runId: runId,
        workflowName: 'demo',
        startedAt: DateTime.parse(ts).toUtc(),
        state: state,
      );

  Map<String, Object> completedSnapshot(String runId, String ts) => {
        'run_id': runId,
        'workflow_name': 'demo',
        'state': 'completed',
        'started_at': ts,
        'nodes': <String, Object>{
          'a': {'status': 'completed', 'attempt': 1, 'tokens': 100, 'duration_seconds': 1.0},
        },
        'pending_inputs': <Object>[],
      };

  Map<String, Object> failedSnapshot(String runId, String ts) => {
        'run_id': runId,
        'workflow_name': 'demo',
        'state': 'failed',
        'started_at': ts,
        'nodes': <String, Object>{
          'a': {'status': 'completed', 'attempt': 1, 'tokens': 50, 'duration_seconds': 1.0},
          'b': {'status': 'failed', 'attempt': 1, 'reason': 'b boom'},
        },
        'pending_inputs': <Object>[],
      };

  Map<String, Object> runningSnapshot(String runId, String ts) => {
        'run_id': runId,
        'workflow_name': 'demo',
        'state': 'running',
        'started_at': ts,
        'nodes': <String, Object>{
          'a': {'status': 'running', 'attempt': 1, 'model': 'draft'},
        },
        'pending_inputs': <Object>[],
      };

  MockClient fakeClient({
    required List<RunHistoryEntry> history,
    Map<String, Map<String, Object>> statusByRun = const {},
    Map<String, Map<String, Object>> resultByRun = const {},
  }) {
    const headers = <String, String>{'content-type': 'application/json'};
    return MockClient((request) async {
      final path = request.url.path;
      if (request.method == 'GET' && path == '/runs') {
        return http.Response(
          jsonEncode([
            for (final e in history)
              {
                'run_id': e.runId,
                'workflow_name': e.workflowName,
                'started_at': e.startedAt.toUtc().toIso8601String(),
                'state': e.state,
              },
          ]),
          200,
          headers: headers,
        );
      }
      final m = RegExp(r'^/runs/([^/]+)(/result)?$').firstMatch(path);
      if (request.method == 'GET' && m != null) {
        final id = m.group(1)!;
        final body = m.group(2) != null ? resultByRun[id] : statusByRun[id];
        if (body == null) {
          return http.Response(
            jsonEncode({'error': {'code': 'unknown_run', 'message': 'nope'}}),
            404,
            headers: headers,
          );
        }
        return http.Response(jsonEncode(body), 200, headers: headers);
      }
      if (request.method == 'POST' && path == '/runs') {
        return http.Response(
          jsonEncode({'run_id': 'run-new', 'state': 'running', 'workflow': 'demo'}),
          201,
          headers: headers,
        );
      }
      return http.Response(
        jsonEncode({'error': {'code': 'unknown_run', 'message': 'nope'}}),
        404,
        headers: headers,
      );
    });
  }

  Future<void> settle(WidgetTester tester) async {
    for (var i = 0; i < 6; i++) {
      await tester.pump();
    }
  }

  Future<void> pumpHome(WidgetTester tester,
      {required AppState app,
      required AgencyClient client,
      required CatalogEntry entry,
      required StreamController<EventFrame> events}) async {
    await tester.pumpWidget(MaterialApp(
      theme: themeDataFor(ThemeMode.dark),
      home: HomePage(
        app: app,
        client: client,
        entries: [entry],
        eventStream: events.stream,
      ),
    ));
  }

  CanvasScene sceneOf(WidgetTester tester) =>
      tester.state<CanvasViewState>(find.byType(CanvasView)).scene!;

  group('T030 startup selection', () {
    testWidgets('(a) the newest (LAST) run is auto-selected and rendered',
        (tester) async {
      final d = demoDef();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      final client = AgencyClient(
        'http://fake',
        httpClient: fakeClient(
          history: [
            historyEntry('r-1', oldTs, 'completed'),
            historyEntry('r-2', midTs, 'failed'),
            historyEntry('r-3', newTs, 'running'),
          ],
          statusByRun: {
            'r-1': completedSnapshot('r-1', oldTs),
            'r-2': failedSnapshot('r-2', midTs),
            'r-3': runningSnapshot('r-3', newTs),
          },
        ),
      );
      final entry = CatalogEntry(path: 'demo.yaml', name: 'demo', definition: d);

      await pumpHome(tester, app: app, client: client, entry: entry, events: events);
      await settle(tester);

      expect(app.selectedRunId, 'r-3',
          reason: 'the newest run (LAST element of the ascending list) is auto-selected');
      expect(find.byType(CanvasView), findsOneWidget,
          reason: 'the selected run renders without any user action');
      expect(sceneOf(tester).encodingsByNode['a'], kStateEncodings[NodeStatus.running],
          reason: 'the rendered canvas is the newest run, not an older one');

      expect(find.text('r-1'), findsOneWidget, reason: 'older run r-1 is listed');
      expect(find.text('r-2'), findsOneWidget, reason: 'older run r-2 is listed');
      expect(
        tester.getTopLeft(find.text('r-2')).dy,
        lessThan(tester.getTopLeft(find.text('r-1')).dy),
        reason: 'history is listed newest-first',
      );
    });

    testWidgets('(b) empty history shows the empty state; start still works',
        (tester) async {
      final d = demoDef();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      final client = AgencyClient(
        'http://fake',
        httpClient: fakeClient(
          history: const [],
          statusByRun: {
            'run-new': runningSnapshot('run-new', newTs),
          },
        ),
      );
      final entry = CatalogEntry(path: 'demo.yaml', name: 'demo', definition: d);

      await pumpHome(tester, app: app, client: client, entry: entry, events: events);
      await settle(tester);

      expect(
        find.text('No runs yet — start a workflow from the sidebar.'),
        findsOneWidget,
        reason: 'the empty state is shown for an empty history',
      );

      await tester.tap(find.text('Start').first);
      await settle(tester);

      expect(app.selectedRunId, 'run-new',
          reason: 'the sidebar start action is usable in the empty state');
      expect(find.byType(CanvasView), findsOneWidget);
    });
  });

  group('history list helpers', () {
    test('relativeTimeAgo buckets', () {
      final now = DateTime.utc(2026, 8, 31, 12, 0, 0);
      expect(relativeTimeAgo(now.subtract(const Duration(seconds: 30)), now), 'just now');
      expect(relativeTimeAgo(now.subtract(const Duration(minutes: 5)), now), '5m ago');
      expect(relativeTimeAgo(now.subtract(const Duration(hours: 3)), now), '3h ago');
      expect(relativeTimeAgo(now.subtract(const Duration(days: 2)), now), '2d ago');
    });

    test('shortRunId keeps short ids intact and truncates long ones', () {
      expect(shortRunId('run-1'), 'run-1');
      expect(shortRunId('r-0123456789abcdef'), 'r-0123456789');
    });
  });
}
