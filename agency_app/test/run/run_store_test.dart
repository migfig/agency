import 'dart:async';

import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:agencyapp/run/run_state.dart';
import 'package:agencyapp/run/run_store.dart';
import 'package:flutter_test/flutter_test.dart';

/// RunStore finalize coverage (US2 / research R2): broadcast frames carry no
/// per-node output and there is no run-level terminal event, so once every
/// node has reached a terminal status the store must poll the status snapshot
/// until the server settles the run, then seed from it and fold in the result
/// view (per-node output, total tokens). The resync paths must fold the result
/// in too, so a re-seed never drops accumulated output.
void main() {
  const runId = 'r';
  const ts = '2026-08-29T10:00:00+00:00';

  WorkflowDefinition def() => parseWorkflowDefinition('''
name: run
nodes:
  a: {id: a, type: agent, model: draft}
  b: {id: b, type: agent, model: draft}
''');

  EventFrame frame(String type, int seq, Map<String, Object?> payload,
      [String timestamp = ts]) {
    return decodeEventFrame({
      'event_type': type,
      'seq': seq,
      'timestamp': timestamp,
      'run_id': runId,
      'payload': payload,
    })!;
  }

  Map<String, Object> runningSnapshot() => {
        'run_id': runId,
        'workflow_name': 'run',
        'state': 'running',
        'started_at': ts,
        'nodes': <String, Object>{},
        'pending_inputs': <Object>[],
      };

  Map<String, Object> settledSnapshot() => {
        'run_id': runId,
        'workflow_name': 'run',
        'state': 'completed',
        'started_at': ts,
        'nodes': <String, Object>{
          'a': {
            'status': 'completed',
            'attempt': 1,
            'tokens': 100,
            'duration_seconds': 1.0,
          },
          'b': {
            'status': 'completed',
            'attempt': 1,
            'tokens': 50,
            'duration_seconds': 0.5,
          },
        },
        'pending_inputs': <Object>[],
      };

  RunResultView resultView() => RunResultView.fromJson({
        'run_id': runId,
        'status': 'completed',
        'total_tokens': 150,
        'duration_seconds': 1.5,
        'nodes': <Object>[
          {
            'node_id': 'a',
            'status': 'completed',
            'attempt': 1,
            'tokens': 100,
            'duration_seconds': 1.0,
            'output': 'output-a',
          },
          {
            'node_id': 'b',
            'status': 'completed',
            'attempt': 1,
            'tokens': 50,
            'duration_seconds': 0.5,
            'output': 'output-b',
          },
        ],
      });

  /// Let microtasks (and, with [delayed], one 500 ms finalize poll) run.
  Future<void> pump([Duration d = const Duration(milliseconds: 20)]) async {
    await Future<void>.delayed(d);
  }

  group('allNodesTerminal', () {
    SNodeState node(String id, NodeStatus s) =>
        SNodeState(nodeId: id, type: 'agent', status: s);

    RunState state(Map<String, SNodeState> nodes) => RunState(
          runId: runId,
          workflow: 'run',
          runStatus: 'running',
          nodes: nodes,
          pendingInputs: const [],
        );

    test('empty node map is not terminal', () {
      expect(allNodesTerminal(state(const {})), isFalse);
    });

    test('true only when every node is terminal', () {
      expect(
        allNodesTerminal(state({'a': node('a', NodeStatus.completed),
          'b': node('b', NodeStatus.running)})),
        isFalse,
      );
      expect(
        allNodesTerminal(state({
          'a': node('a', NodeStatus.completed),
          'b': node('b', NodeStatus.failed),
          'c': node('c', NodeStatus.skipped),
          'd': node('d', NodeStatus.completedFallback),
        })),
        isTrue,
      );
    });
  });

  group('finalize (all-terminal nodes)', () {
    test('(a) the settled status + result are folded in; a duplicate '
        'terminal frame does not re-trigger the pass', () async {
      var statusCalls = 0;
      var resultCalls = 0;
      var finalizedCalls = 0;
      final store = RunStore(workflowDefinition: def());
      store.seed(RunStatusView.fromJson(runningSnapshot()));
      final frames = StreamController<EventFrame>();
      store.connect(
        frames.stream,
        fetchStatus: (id) async {
          statusCalls++;
          return RunStatusView.fromJson(settledSnapshot());
        },
        fetchResult: (id) async {
          resultCalls++;
          return resultView();
        },
        onFinalized: () => finalizedCalls++,
      );

      frames.add(frame('node_completed', 1, {'node_id': 'a'}));
      frames.add(frame('node_completed', 2, {'node_id': 'b'}));
      await pump();

      expect(statusCalls, 1, reason: 'one status poll once all nodes terminal');
      expect(resultCalls, 1, reason: 'the result view is fetched');
      expect(finalizedCalls, 1, reason: 'onFinalized fires once');
      final st = store.state!;
      expect(st.runStatus, 'completed', reason: 'authoritative run status');
      expect(st.totalTokens, 150, reason: 'API-level total tokens');
      expect(st.nodes['a']!.output, 'output-a',
          reason: 'per-node output from the result view');
      expect(st.nodes['b']!.output, 'output-b');

      // A duplicate terminal frame is dropped by the terminal lock and must
      // not re-run the finalize pass.
      frames.add(frame('node_completed', 3, {'node_id': 'a'}));
      await pump();
      expect(statusCalls, 1);
      expect(resultCalls, 1);
      frames.close();
      store.dispose();
    });

    test('(b) the pass polls until the server settles the run', () async {
      var statusCalls = 0;
      final store = RunStore(workflowDefinition: def());
      store.seed(RunStatusView.fromJson(runningSnapshot()));
      final frames = StreamController<EventFrame>();
      store.connect(
        frames.stream,
        fetchStatus: (id) async {
          statusCalls++;
          return RunStatusView.fromJson(
            statusCalls >= 2 ? settledSnapshot() : runningSnapshot(),
          );
        },
        fetchResult: (id) async => resultView(),
      );

      frames.add(frame('node_completed', 1, {'node_id': 'a'}));
      frames.add(frame('node_completed', 2, {'node_id': 'b'}));
      // First poll sees 'running', the settle delay elapses, the second
      // poll sees 'completed' and the result is folded in.
      await pump(const Duration(milliseconds: 700));

      expect(statusCalls, 2, reason: 'polled again after the settle delay');
      final st = store.state!;
      expect(st.runStatus, 'completed');
      expect(st.totalTokens, 150);
      expect(st.nodes['a']!.output, 'output-a');
      frames.close();
      store.dispose();
    });

    test('(c) reset stops an in-flight poll loop and suppresses onFinalized',
        () async {
      var statusCalls = 0;
      var finalizedCalls = 0;
      final store = RunStore(workflowDefinition: def());
      store.seed(RunStatusView.fromJson(runningSnapshot()));
      final frames = StreamController<EventFrame>();
      store.connect(
        frames.stream,
        fetchStatus: (id) async {
          statusCalls++;
          return RunStatusView.fromJson(runningSnapshot()); // never settles
        },
        fetchResult: (id) async => throw StateError('no result'),
        onFinalized: () => finalizedCalls++,
      );

      frames.add(frame('node_completed', 1, {'node_id': 'a'}));
      frames.add(frame('node_completed', 2, {'node_id': 'b'}));
      await pump();
      expect(statusCalls, 1, reason: 'the first poll fired before the wait');

      store.reset();
      await pump();
      await Future<void>.delayed(const Duration(milliseconds: 700));
      expect(statusCalls, 1,
          reason: 'reset cancelled the pending poll wait');
      expect(finalizedCalls, 0,
          reason: 'a reset store does not report finalization');
      frames.close();
      store.dispose();
    });
  });

  group('resync result folding', () {
    test('(a) a settled snapshot folds the result in', () async {
      var resultCalls = 0;
      final store = RunStore(workflowDefinition: def());
      store.seed(RunStatusView.fromJson(runningSnapshot()));
      await store.resync(
        runId,
        (id) async => RunStatusView.fromJson(settledSnapshot()),
        fetchResult: (id) async {
          resultCalls++;
          return resultView();
        },
      );
      expect(resultCalls, 1);
      final st = store.state!;
      expect(st.runStatus, 'completed');
      expect(st.totalTokens, 150);
      expect(st.nodes['a']!.output, 'output-a',
          reason: 'the re-seed does not drop the output');
      store.dispose();
    });

    test('(b) a still-running snapshot does not fetch the result', () async {
      var resultCalls = 0;
      final store = RunStore(workflowDefinition: def());
      store.seed(RunStatusView.fromJson(runningSnapshot()));
      await store.resync(
        runId,
        (id) async => RunStatusView.fromJson(runningSnapshot()),
        fetchResult: (id) async {
          resultCalls++;
          return resultView();
        },
      );
      expect(resultCalls, 0);
      expect(store.state!.runStatus, 'running');
      store.dispose();
    });

    test('(c) a missing fetchResult keeps the legacy snapshot-only resync',
        () async {
      final store = RunStore(workflowDefinition: def());
      store.seed(RunStatusView.fromJson(runningSnapshot()));
      await store.resync(
          runId, (id) async => RunStatusView.fromJson(settledSnapshot()));
      final st = store.state!;
      expect(st.runStatus, 'completed');
      expect(st.totalTokens, isNull);
      expect(st.nodes['a']!.output, isNull);
      store.dispose();
    });
  });
}
