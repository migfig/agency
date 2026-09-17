import 'dart:async';
import 'dart:convert';

import 'package:agencyapp/api/agency_client.dart';
import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/app/app_state.dart';
import 'package:agencyapp/app/home_page.dart';
import 'package:agencyapp/canvas/hil_prompt.dart';
import 'package:agencyapp/catalog/catalog_entry.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

/// T039 — HIL prompt (FR-017, research R12): the card is driven by
/// `pending_inputs`, submits to the exact node endpoint, and resyncs after
/// submit / 409.
void main() {
  const ts = '2026-08-29T10:00:00Z';
  const runId = 'run-hil';
  const nodeId = 'approve';
  const question = 'Approve the draft for publication?';

  WorkflowDefinition demoDef() => parseWorkflowDefinition('''
name: demo
nodes:
  a: {id: a, type: agent, model: draft}
  approve: {id: approve, type: human_in_loop}
''');

  final deadline =
      DateTime.now().toUtc().add(const Duration(minutes: 30)).toIso8601String();

  MockClient fakeClient({
    required bool Function() isPending,
    required int submitStatus,
    required String submitErrorCode,
    required void Function(String path, String body) recordPost,
    required void Function() recordStatus,
    void Function()? onSuccessfulSubmit,
  }) {
    const headers = <String, String>{'content-type': 'application/json'};
    return MockClient((request) async {
      final path = request.url.path;
      if (request.method == 'GET' && path == '/runs') {
        return http.Response(
          jsonEncode([
            {
              'run_id': runId,
              'workflow_name': 'demo',
              'started_at': ts,
              'state': 'running',
            },
          ]),
          200,
          headers: headers,
        );
      }
      if (request.method == 'GET' && path == '/runs/$runId') {
        recordStatus();
        final pending = isPending();
        final nodes = <String, Object>{
          'a': {
            'status': 'completed',
            'attempt': 1,
            'tokens': 10,
            'duration_seconds': 0.5,
          },
          'approve':
              {'status': pending ? 'running' : 'completed', 'attempt': 1},
        };
        final snapshot = <String, Object>{
          'run_id': runId,
          'workflow_name': 'demo',
          'state': 'running',
          'started_at': ts,
          'nodes': nodes,
          'pending_inputs': pending
              ? <Object>[
                  {
                    'node_id': nodeId,
                    'prompt': question,
                    'deadline': deadline,
                  },
                ]
              : <Object>[],
        };
        return http.Response(jsonEncode(snapshot), 200, headers: headers);
      }
      if (request.method == 'POST' &&
          path == '/runs/$runId/nodes/$nodeId/input') {
        recordPost(path, request.body);
        if (submitStatus == 201) {
          onSuccessfulSubmit?.call();
          return http.Response(
            jsonEncode(
                {'run_id': runId, 'state': 'running', 'workflow': 'demo'}),
            201,
            headers: headers,
          );
        }
        return http.Response(
          jsonEncode({'error': {'code': submitErrorCode, 'message': 'x'}}),
          submitStatus,
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

  Future<void> settle(WidgetTester tester) async {
    for (var i = 0; i < 8; i++) {
      await tester.pump();
    }
  }

  group('T039 HIL prompt', () {
    testWidgets(
        '(a) pending_inputs non-empty shows the card: step, question, deadline',
        (tester) async {
      var pending = true;
      final client = AgencyClient('http://fake',
          httpClient: fakeClient(
        isPending: () => pending,
        submitStatus: 201,
        submitErrorCode: 'input_not_awaiting',
        recordPost: (_, _) {},
        recordStatus: () {},
      ));
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      final entry =
          CatalogEntry(path: 'demo.yaml', name: 'demo', definition: demoDef());

      await pumpHome(tester,
          app: app, client: client, entry: entry, events: events);
      await settle(tester);

      final card = find.byType(HilPromptCard);
      expect(card, findsOneWidget,
          reason: 'a pending input renders the HIL prompt card');
      expect(find.descendant(of: card, matching: find.text(nodeId)),
          findsOneWidget,
          reason: 'the card shows the waiting node id');
      expect(find.descendant(of: card, matching: find.text(question)),
          findsOneWidget,
          reason: 'the card shows the question text');
      expect(find.descendant(of: card, matching: find.textContaining('deadline')),
          findsOneWidget,
          reason: 'the card shows the deadline countdown');
    });

    testWidgets('(b) submit posts the exact node id and text; card closes',
        (tester) async {
      var pending = true;
      String? postPath;
      String? postBody;
      final client = AgencyClient('http://fake',
          httpClient: fakeClient(
        isPending: () => pending,
        submitStatus: 201,
        submitErrorCode: 'input_not_awaiting',
        recordPost: (p, b) {
          postPath = p;
          postBody = b;
        },
        recordStatus: () {},
        onSuccessfulSubmit: () => pending = false,
      ));
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      final entry =
          CatalogEntry(path: 'demo.yaml', name: 'demo', definition: demoDef());

      await pumpHome(tester,
          app: app, client: client, entry: entry, events: events);
      await settle(tester);

      final card = find.byType(HilPromptCard);
      await tester.enterText(
          find.descendant(of: card, matching: find.byType(TextField)),
          'yes, proceed');
      await tester.pump();
      await tester
          .tap(find.descendant(of: card, matching: find.text('Submit')));
      await settle(tester);

      expect(postPath, '/runs/$runId/nodes/$nodeId/input',
          reason: 'submit targets the exact node endpoint');
      expect(jsonDecode(postBody!), {'value': 'yes, proceed'},
          reason: 'the submitted text is the request body');
      expect(pending, isFalse, reason: 'the fake resolved the input on submit');
      expect(find.byType(HilPromptCard), findsNothing,
          reason: 'the resync snapshot has no pending input, so the card closes');
    });

    testWidgets(
        '(c) 409 input_not_awaiting shows the already-resolved note + resyncs',
        (tester) async {
      var pending = true; // the snapshot keeps listing the input
      var statusCalls = 0;
      final client = AgencyClient('http://fake',
          httpClient: fakeClient(
        isPending: () => pending,
        submitStatus: 409,
        submitErrorCode: 'input_not_awaiting',
        recordPost: (_, _) {},
        recordStatus: () => statusCalls++,
      ));
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      final entry =
          CatalogEntry(path: 'demo.yaml', name: 'demo', definition: demoDef());

      await pumpHome(tester,
          app: app, client: client, entry: entry, events: events);
      await settle(tester);

      statusCalls = 0; // ignore the initial seed fetch
      final card = find.byType(HilPromptCard);
      await tester.enterText(
          find.descendant(of: card, matching: find.byType(TextField)), 'dup');
      await tester.pump();
      await tester
          .tap(find.descendant(of: card, matching: find.text('Submit')));
      await settle(tester);

      expect(find.text('input not needed — already resolved'), findsOneWidget,
          reason: 'a 409 shows the duplicate-submission note');
      expect(statusCalls, 1,
          reason: 'the 409 triggers exactly one resync');
      expect(find.byType(HilPromptCard), findsOneWidget,
          reason: 'the input is still listed, so the card stays with the note');
    });

    testWidgets(
        '(d) unknown_node from a stale card resyncs and closes the card',
        (tester) async {
      var pending = true;
      var statusCalls = 0;
      final client = AgencyClient('http://fake',
          httpClient: fakeClient(
        isPending: () => pending,
        submitStatus: 404,
        submitErrorCode: 'unknown_node',
        recordPost: (_, _) {},
        recordStatus: () {
          statusCalls++;
        },
        onSuccessfulSubmit: () {},
      ));
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      final entry =
          CatalogEntry(path: 'demo.yaml', name: 'demo', definition: demoDef());

      await pumpHome(tester,
          app: app, client: client, entry: entry, events: events);
      await settle(tester);

      // Simulate the server having moved on: the resync no longer lists the
      // input, so the card must close with no error note.
      pending = false;
      statusCalls = 0;
      final card = find.byType(HilPromptCard);
      await tester.enterText(
          find.descendant(of: card, matching: find.byType(TextField)), 'late');
      await tester.pump();
      await tester
          .tap(find.descendant(of: card, matching: find.text('Submit')));
      await settle(tester);

      expect(statusCalls, 1,
          reason: 'the unknown_node 404 triggers exactly one resync');
      expect(find.byType(HilPromptCard), findsNothing,
          reason: 'the resynced snapshot has no pending input, card closes');
    });
  });
}
