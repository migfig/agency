import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:agencyapp/run/run_state.dart';
import 'package:flutter_test/flutter_test.dart';

/// T008 — pure unit coverage of the run-state reducer: full reduce(snapshot),
/// the T1 node state machine for every frame type, optimistic retry/fallback
/// classification, foreign-run_id drop, terminal-never-retransitions,
/// run_cancel badge, and invariants 1–5.
void main() {
  // a: retry max_attempts=2, no fallback.
  // b: no retry (single attempt), fallback=fb.
  WorkflowDefinition def() => parseWorkflowDefinition('''
name: run
nodes:
  a: {id: a, type: agent, model: draft, retry: {max_attempts: 2, backoff: exponential, base_delay_seconds: 1}}
  b: {id: b, type: agent, model: draft, fallback: fb}
''');

  const ts = '2026-08-29T10:00:00+00:00';

  EventFrame frame(String type, String runId, int seq, Map<String, Object?> payload,
      [String timestamp = ts]) {
    return decodeEventFrame({
      'event_type': type,
      'seq': seq,
      'timestamp': timestamp,
      'run_id': runId,
      'payload': payload,
    })!;
  }

  RunState initial({WorkflowDefinition? d}) => reduce(
        RunStatusView.fromJson({
          'run_id': 'r',
          'workflow_name': 'run',
          'state': 'running',
          'started_at': ts,
          'replay_source': null,
          'nodes': <String, Object>{},
          'pending_inputs': <Object>[],
        }),
        definition: d ?? def(),
      );

  group('reduce(snapshot) — full coverage', () {
    test('covers all def nodes + snapshot orphans (invariant 1)', () {
      final s = RunStatusView.fromJson({
        'run_id': 'r',
        'workflow_name': 'run',
        'state': 'running',
        'started_at': ts,
        'replay_source': null,
        'nodes': {
          'a': {'status': 'running', 'model': 'draft', 'attempt': 1},
          'zz': {'status': 'completed', 'attempt': 1},
        },
        'pending_inputs': <Object>[],
      });
      final st = reduce(s, definition: def());
      expect(st.nodes.keys, containsAll(['a', 'b', 'zz']));
      expect(st.nodes['a']!.status, NodeStatus.running);
      expect(st.nodes['b']!.status, NodeStatus.pending, reason: 'def-only node defaults to pending');
      expect(st.nodes['zz']!.status, NodeStatus.completed, reason: 'orphan from snapshot included');
      expect(st.runId, 'r');
      expect(st.workflow, 'run');
      expect(st.runStatus, 'running');
      expect(st.startedAt, DateTime.parse(ts).toUtc());
    });

    test('maps every wire status 1:1 (invariant 2)', () {
      const map = {
        'pending': NodeStatus.pending,
        'running': NodeStatus.running,
        'awaiting_retry': NodeStatus.awaitingRetry,
        'completed': NodeStatus.completed,
        'completed_fallback': NodeStatus.completedFallback,
        'failed': NodeStatus.failed,
        'skipped': NodeStatus.skipped,
      };
      for (final e in map.entries) {
        final st = reduce(
          RunStatusView.fromJson({
            'run_id': 'r',
            'workflow_name': 'run',
            'state': 'running',
            'started_at': ts,
            'replay_source': null,
            'nodes': {'a': {'status': e.key, 'attempt': 1}},
            'pending_inputs': <Object>[],
          }),
          definition: def(),
        );
        expect(st.nodes['a']!.status, e.value);
      }
    });

    test('invariant 4: pending_input for a non-running node → wantsResync', () {
      final s = RunStatusView.fromJson({
        'run_id': 'r',
        'workflow_name': 'run',
        'state': 'running',
        'started_at': ts,
        'replay_source': null,
        'nodes': {'a': {'status': 'completed', 'attempt': 1}},
        'pending_inputs': [
          {'node_id': 'a', 'prompt': 'p', 'deadline': null},
        ],
      });
      expect(reduce(s, definition: def()).wantsResync, isTrue);
    });

    test('invariant 4: pending_input for a running node → not resync', () {
      final s = RunStatusView.fromJson({
        'run_id': 'r',
        'workflow_name': 'run',
        'state': 'running',
        'started_at': ts,
        'replay_source': null,
        'nodes': {'a': {'status': 'running', 'attempt': 1}},
        'pending_inputs': [
          {'node_id': 'a', 'prompt': 'p', 'deadline': null},
        ],
      });
      expect(reduce(s, definition: def()).wantsResync, isFalse);
    });
  });

  group('apply(frame) — T1 state machine', () {
    test('node_queued → pending, sets queuedAt', () {
      final st = apply(initial(), frame('node_queued', 'r', 1, {'node_id': 'a', 'run_id': 'r'}),
          definition: def());
      expect(st.nodes['a']!.status, NodeStatus.pending);
      expect(st.nodes['a']!.queuedAt, isNotNull);
    });

    test('node_started → running, sets startedAt (invariant 3)', () {
      final st = apply(initial(), frame('node_started', 'r', 1,
          {'node_id': 'a', 'run_id': 'r', 'model': 'draft', 'attempt': 1}),
          definition: def());
      expect(st.nodes['a']!.status, NodeStatus.running);
      expect(st.nodes['a']!.startedAt, isNotNull);
      expect(st.nodes['a']!.attempt, 1);
    });

    test('node_retrying → awaiting_retry + nextRetryAt = ts + delay', () {
      final st = apply(initial(), frame('node_retrying', 'r', 1, {
            'node_id': 'a',
            'run_id': 'r',
            'model': 'draft',
            'error': 'boom',
            'attempt': 1,
            'next_attempt': 2,
            'delay_seconds': 1.5,
          }),
          definition: def());
      expect(st.nodes['a']!.status, NodeStatus.awaitingRetry);
      final ts0 = DateTime.parse(ts).toUtc();
      expect(st.nodes['a']!.nextRetryAt, ts0.add(const Duration(milliseconds: 1500)));
    });

    test('node_completed (no fallback) → completed + finishedAt + metrics (invariant 3)', () {
      final st = apply(initial(), frame('node_completed', 'r', 1, {
            'node_id': 'a',
            'run_id': 'r',
            'model': 'draft',
            'tokens_used': 50,
            'duration_seconds': 2.0,
            'attempt': 1,
          }),
          definition: def());
      expect(st.nodes['a']!.status, NodeStatus.completed);
      expect(st.nodes['a']!.finishedAt, isNotNull);
      expect(st.nodes['a']!.tokens, 50);
      expect(st.nodes['a']!.durationSeconds, 2.0);
    });

    test('node_completed (fallback set) → completed_fallback + onFallbackPath', () {
      final st = apply(initial(), frame('node_completed', 'r', 1, {
            'node_id': 'b',
            'run_id': 'r',
            'model': 'fb',
            'tokens_used': 10,
            'duration_seconds': 1.0,
            'attempt': 1,
            'fallback': 'fb',
          }),
          definition: def());
      expect(st.nodes['b']!.status, NodeStatus.completedFallback);
      expect(st.nodes['b']!.onFallbackPath, isTrue);
    });

    test('node_skipped → skipped + skipReason + finishedAt', () {
      final st = apply(initial(), frame('node_skipped', 'r', 1,
          {'node_id': 'b', 'run_id': 'r', 'reason': 'unresolved_binding', 'skipped_bindings': <Object>[]}),
          definition: def());
      expect(st.nodes['b']!.status, NodeStatus.skipped);
      expect(st.nodes['b']!.skipReason, 'unresolved_binding');
      expect(st.nodes['b']!.finishedAt, isNotNull);
    });

    test('run_cancel sets the run-level badge', () {
      final st = apply(initial(), frame('run_cancel', 'r', 1, {'run_id': 'r'}), definition: def());
      expect(st.runCancelled, isTrue);
    });
  });

  group('apply(frame) — optimistic node_failed classification', () {
    test('a@1 (retry remaining) → awaiting_retry, not on fallback path', () {
      final st = apply(initial(), frame('node_failed', 'r', 1,
          {'node_id': 'a', 'run_id': 'r', 'model': 'draft', 'error': 'boom', 'attempt': 1}),
          definition: def());
      expect(st.nodes['a']!.status, NodeStatus.awaitingRetry);
      expect(st.nodes['a']!.onFallbackPath, isFalse);
    });

    test('a@2 (retries exhausted, no fallback) → failed', () {
      final st = apply(initial(), frame('node_failed', 'r', 1,
          {'node_id': 'a', 'run_id': 'r', 'model': 'draft', 'error': 'boom', 'attempt': 2}),
          definition: def());
      expect(st.nodes['a']!.status, NodeStatus.failed);
    });

    test('b@1 (terminal, fallback declared) → awaiting_retry + onFallbackPath', () {
      final st = apply(initial(), frame('node_failed', 'r', 1,
          {'node_id': 'b', 'run_id': 'r', 'model': 'draft', 'error': 'boom', 'attempt': 1}),
          definition: def());
      expect(st.nodes['b']!.status, NodeStatus.awaitingRetry);
      expect(st.nodes['b']!.onFallbackPath, isTrue);
    });

    test('b fallback attempt (payload.fallback set) → failed', () {
      final st = apply(initial(), frame('node_failed', 'r', 1,
          {'node_id': 'b', 'run_id': 'r', 'model': 'fb', 'error': 'boom', 'attempt': 1, 'fallback': 'fb'}),
          definition: def());
      expect(st.nodes['b']!.status, NodeStatus.failed);
    });

    test('no definition → conservative failed', () {
      final base = reduce(
        RunStatusView.fromJson({
          'run_id': 'r',
          'workflow_name': 'run',
          'state': 'running',
          'started_at': ts,
          'replay_source': null,
          'nodes': <String, Object>{},
          'pending_inputs': <Object>[],
        }),
      );
      final st = apply(
        base,
        frame('node_failed', 'r', 1,
            {'node_id': 'a', 'run_id': 'r', 'model': 'draft', 'error': 'x', 'attempt': 1}),
      );
      expect(st.nodes['a']!.status, NodeStatus.failed);
    });
  });

  group('fallbackModel population (FR-011)', () {
    test('reduce: snapshot NodeStateView.fallback populates fallbackModel', () {
      final st = reduce(
        RunStatusView.fromJson({
          'run_id': 'r',
          'workflow_name': 'run',
          'state': 'running',
          'started_at': ts,
          'replay_source': null,
          'nodes': {
            'b': {'status': 'completed_fallback', 'model': 'fb', 'attempt': 1, 'fallback': 'fb'},
          },
          'pending_inputs': <Object>[],
        }),
        definition: def(),
      );
      expect(st.nodes['b']!.fallbackModel, 'fb');
      expect(st.nodes['b']!.onFallbackPath, isTrue);
      expect(st.nodes['a']!.fallbackModel, isNull, reason: 'def-only node has no fallback');
    });

    test('reduce: snapshot without fallback leaves fallbackModel null', () {
      final st = reduce(
        RunStatusView.fromJson({
          'run_id': 'r',
          'workflow_name': 'run',
          'state': 'running',
          'started_at': ts,
          'replay_source': null,
          'nodes': {'a': {'status': 'completed', 'model': 'draft', 'attempt': 1}},
          'pending_inputs': <Object>[],
        }),
        definition: def(),
      );
      expect(st.nodes['a']!.fallbackModel, isNull);
    });

    test('apply: node_completed with payload fallback sets fallbackModel', () {
      final st = apply(initial(), frame('node_completed', 'r', 1, {
            'node_id': 'b',
            'run_id': 'r',
            'model': 'fb',
            'tokens_used': 10,
            'duration_seconds': 1.0,
            'attempt': 1,
            'fallback': 'fb',
          }),
          definition: def());
      expect(st.nodes['b']!.fallbackModel, 'fb');
      expect(st.nodes['b']!.status, NodeStatus.completedFallback);
    });

    test('apply: node_completed without payload fallback leaves fallbackModel null', () {
      final st = apply(initial(), frame('node_completed', 'r', 1, {
            'node_id': 'a',
            'run_id': 'r',
            'model': 'draft',
            'tokens_used': 50,
            'duration_seconds': 2.0,
            'attempt': 1,
          }),
          definition: def());
      expect(st.nodes['a']!.fallbackModel, isNull);
    });

    test('apply: node_failed with payload fallback sets fallbackModel', () {
      final st = apply(initial(), frame('node_failed', 'r', 1,
          {'node_id': 'b', 'run_id': 'r', 'model': 'fb', 'error': 'boom', 'attempt': 1, 'fallback': 'fb'}),
          definition: def());
      expect(st.nodes['b']!.fallbackModel, 'fb');
      expect(st.nodes['b']!.status, NodeStatus.failed);
    });

    test('apply: fallback_activated sets fallbackModel and switches model to it', () {
      var st = apply(initial(), frame('node_failed', 'r', 1,
          {'node_id': 'b', 'run_id': 'r', 'model': 'draft', 'error': 'boom', 'attempt': 1}),
          definition: def());
      expect(st.nodes['b']!.status, NodeStatus.awaitingRetry);
      expect(st.nodes['b']!.onFallbackPath, isTrue);
      st = apply(st, frame('fallback_activated', 'r', 2, {
            'node_id': 'b',
            'run_id': 'r',
            'model': 'draft',
            'fallback': 'fb',
            'error': 'boom',
            'attempt': 1,
          }),
          definition: def());
      expect(st.nodes['b']!.fallbackModel, 'fb');
      expect(st.nodes['b']!.model, 'fb', reason: 'fallback model is now executing');
      expect(st.nodes['b']!.onFallbackPath, isTrue);
      expect(st.nodes['b']!.status, NodeStatus.awaitingRetry,
          reason: 'status unchanged by fallback_activated');
    });
  });

  group('frame guards', () {
    test('foreign run_id is ignored (FR-015)', () {
      final st = apply(initial(), frame('node_started', 'OTHER', 1,
          {'node_id': 'a', 'run_id': 'OTHER', 'model': 'draft', 'attempt': 1}),
          definition: def());
      expect(st.nodes['a']!.status, NodeStatus.pending, reason: 'foreign frame must not mutate');
      expect(st.lastEventSeq, isNull, reason: 'foreign frame must not advance seq');
    });

    test('null run_id frame is ignored', () {
      // A run-independent frame (run_id null) cannot target this run, so the
      // node stays in its pre-frame state (pending).
      final f = decodeEventFrame({
        'event_type': 'node_started',
        'seq': 1,
        'timestamp': ts,
        'run_id': null,
        'payload': {'node_id': 'a', 'model': 'draft', 'attempt': 1},
      })!;
      final st = apply(initial(), f, definition: def());
      expect(st.nodes['a']!.status, NodeStatus.pending);
    });

    test('terminal node never re-transitions; sets wantsResync', () {
      var st = apply(initial(), frame('node_completed', 'r', 1, {
            'node_id': 'a',
            'run_id': 'r',
            'model': 'draft',
            'tokens_used': 1,
            'duration_seconds': 1.0,
            'attempt': 1,
          }),
          definition: def());
      expect(st.nodes['a']!.status, NodeStatus.completed);
      st = apply(st, frame('node_started', 'r', 2, {'node_id': 'a', 'run_id': 'r', 'model': 'draft', 'attempt': 2}),
          definition: def());
      expect(st.nodes['a']!.status, NodeStatus.completed, reason: 'no re-transition');
      expect(st.wantsResync, isTrue);
    });
  });

  group('apply(frame) — human input events', () {
    test('node_input_requested adds a pending input (prompt + deadline)', () {
      final st = apply(initial(), frame('node_input_requested', 'r', 1, {
            'node_id': 'gate',
            'run_id': 'r',
            'prompt': 'Approve release? (y/n)',
            'deadline': '2026-08-29T10:05:00+00:00',
          }),
          definition: def());
      expect(st.pendingInputs, hasLength(1));
      expect(st.pendingInputs.single.nodeId, 'gate');
      expect(st.pendingInputs.single.prompt, 'Approve release? (y/n)');
      expect(
          st.pendingInputs.single.deadline,
          DateTime.parse('2026-08-29T10:05:00+00:00').toUtc());
    });

    test('node_input_requested with null deadline leaves it null', () {
      final st = apply(initial(), frame('node_input_requested', 'r', 1, {
            'node_id': 'gate',
            'run_id': 'r',
            'prompt': 'ok?',
            'deadline': null,
          }),
          definition: def());
      expect(st.pendingInputs.single.deadline, isNull);
    });

    test('node_input_resolved removes the pending input', () {
      var st = apply(initial(), frame('node_input_requested', 'r', 1,
          {'node_id': 'gate', 'run_id': 'r', 'prompt': 'ok?', 'deadline': null}),
          definition: def());
      expect(st.pendingInputs, hasLength(1));
      st = apply(st, frame('node_input_resolved', 'r', 2,
          {'node_id': 'gate', 'run_id': 'r'}),
          definition: def());
      expect(st.pendingInputs, isEmpty);
    });

    test('repeated node_input_requested for the same node does not duplicate', () {
      var st = apply(initial(), frame('node_input_requested', 'r', 1,
          {'node_id': 'gate', 'run_id': 'r', 'prompt': 'a?', 'deadline': null}),
          definition: def());
      st = apply(st, frame('node_input_requested', 'r', 2,
          {'node_id': 'gate', 'run_id': 'r', 'prompt': 'b?', 'deadline': null}),
          definition: def());
      expect(st.pendingInputs, hasLength(1));
      expect(st.pendingInputs.single.prompt, 'b?');
    });

    test('full HIL cycle keeps node running and toggles the card', () {
      var st = apply(initial(),
          frame('node_started', 'r', 1, {'node_id': 'gate', 'run_id': 'r'}),
          definition: def());
      expect(st.nodes['gate']!.status, NodeStatus.running);
      st = apply(st, frame('node_input_requested', 'r', 2,
          {'node_id': 'gate', 'run_id': 'r', 'prompt': 'ok?', 'deadline': null}),
          definition: def());
      expect(st.pendingInputs, hasLength(1));
      expect(st.nodes['gate']!.status, NodeStatus.running,
          reason: 'card does not change node status');
      st = apply(st, frame('node_input_resolved', 'r', 3,
          {'node_id': 'gate', 'run_id': 'r'}),
          definition: def());
      expect(st.pendingInputs, isEmpty);
      st = apply(st, frame('node_completed', 'r', 4, {
            'node_id': 'gate',
            'run_id': 'r',
            'tokens_used': 0,
            'duration_seconds': 1.0,
            'attempt': 1,
          }),
          definition: def());
      expect(st.nodes['gate']!.status, NodeStatus.completed);
      expect(st.pendingInputs, isEmpty);
    });
  });

  group('invariant 5 — isStale', () {
    test('markStale sets stale; a frame applied since clears it', () {
      var st = initial();
      expect(st.isStale, isFalse);
      st = markStale(st);
      expect(st.isStale, isTrue);
      st = apply(st, frame('node_started', 'r', 1,
          {'node_id': 'a', 'run_id': 'r', 'model': 'draft', 'attempt': 1}),
          definition: def());
      expect(st.isStale, isFalse);
    });
  });
}
