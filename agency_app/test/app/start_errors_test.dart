import 'dart:async';
import 'dart:convert';

import 'package:agencyapp/api/agency_client.dart';
import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/app/app_state.dart';
import 'package:agencyapp/app/home_page.dart';
import 'package:agencyapp/canvas/canvas_view.dart';
import 'package:agencyapp/catalog/catalog_entry.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

/// T022 — surfacing start failures (SC-004, SC-006).
///
/// Pumps the real [HomePage] against a scripted [AgencyClient] whose `POST /runs`
/// is rejected with one of the API error codes, then proves:
///   - the inline, human, code-derived message is shown on the workflow row;
///   - no run is selected (`AppState.selectedRunId` stays null);
///   - no canvas is opened;
///   - the Start affordance is restored (no stuck loading state).
///
/// The expected copy is the spec's user-facing message for each code — this
/// locks the UI to `userFacingMessage(code)`, not the raw server body.
void main() {
  WorkflowDefinition demoDef() => parseWorkflowDefinition('''
name: demo
nodes:
  a: {id: a, type: agent, model: draft, retry: {max_attempts: 3, backoff: exponential, base_delay_seconds: 1}}
  b: {id: b, type: agent, model: draft, fallback: fb}
  c: {id: c, type: tool_call, tool: echo}
  d: {id: d, type: conditional}
''');

  MockClient failingClient(int status, String code) => MockClient((request) async {
        final headers = <String, String>{'content-type': 'application/json'};
        if (request.method == 'POST' && request.url.path == '/runs') {
          return http.Response(
            jsonEncode({'error': {'code': code, 'message': 'server said no'}}),
            status,
            headers: headers,
          );
        }
        // Non-start endpoints are irrelevant to this scenario; satisfy them.
        return http.Response('[]', 200, headers: headers);
      });

  Future<void> scenario(
    WidgetTester tester, {
    required int status,
    required String code,
    required String message,
  }) async {
    final d = demoDef();
    final events = StreamController<EventFrame>.broadcast();
    final app = AppState();
    final client = AgencyClient('http://fake', httpClient: failingClient(status, code));
    final entry = CatalogEntry(path: 'demo.yaml', name: 'demo', definition: d);

    await tester.pumpWidget(MaterialApp(
      theme: themeDataFor(ThemeMode.dark),
      home: HomePage(
        app: app,
        client: client,
        entries: [entry],
        eventStream: events.stream,
      ),
    ));

    await tester.tap(find.text('Start').first);
    for (var i = 0; i < 3; i++) {
      await tester.pump();
    }

    expect(find.text(message), findsOneWidget,
        reason: 'the inline message for $code is shown on the row');
    expect(app.selectedRunId, isNull,
        reason: 'a rejected start selects no run');
    expect(find.byType(CanvasView), findsNothing,
        reason: 'no canvas is opened on a rejected start');
    expect(find.text('Start'), findsWidgets,
        reason: 'the Start affordance is restored (no stuck loading state)');
  }

  group('T022 start failures', () {
    testWidgets('404 unknown_workflow', (tester) =>
        scenario(tester,
            status: 404,
            code: 'unknown_workflow',
            message: 'That workflow is not in the catalog.'));

    testWidgets('409 model_validation_failed', (tester) =>
        scenario(tester,
            status: 409,
            code: 'model_validation_failed',
            message: 'A required model could not be resolved before the run.'));

    testWidgets('422 invalid_workflow', (tester) =>
        scenario(tester,
            status: 422,
            code: 'invalid_workflow',
            message: 'The workflow file could not be validated.'));
  });
}
