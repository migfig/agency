import 'package:agencyapp/api/models.dart';
import 'package:flutter_test/flutter_test.dart';

/// T004 — pure unit coverage of the wire-faithful DTOs (A1–A9) and the
/// 9-code user-facing error mapping. No HTTP, no server: every case decodes
/// fixture JSON matching contracts/agency-api.md (the real 001/002 wire).
void main() {
  group('A1 EventFrame', () {
    const ts = '2026-08-29T10:38:00.000000+00:00';

    test('decodes a known node frame 1:1 (snake_case payload hoisted run_id)', () {
      final frame = decodeEventFrame({
        'event_type': 'node_started',
        'seq': 42,
        'timestamp': ts,
        'run_id': 'run-1',
        'payload': {
          'node_id': 'a',
          'run_id': 'run-1',
          'model': 'draft',
          'attempt': 1,
        },
      })!;
      expect(frame.eventType, 'node_started');
      expect(frame.seq, 42);
      expect(frame.runId, 'run-1');
      expect(frame.timestamp, DateTime.utc(2026, 8, 29, 10, 38, 0));
      expect(frame.payload['node_id'], 'a');
      expect(frame.payload['model'], 'draft');
    });

    test('run_id is null for run-independent frames', () {
      final frame = decodeEventFrame({
        'event_type': 'agent_queued',
        'seq': 1,
        'timestamp': ts,
        'run_id': null,
        'payload': {
          'node_id': 'a',
          'model_name': 'draft',
          'queue_position': 2,
          'estimated_bytes': 100,
        },
      })!;
      expect(frame.runId, isNull);
      expect(frame.payload['queue_position'], 2);
    });

    test('drops unknown event_type (FR-020) instead of throwing', () {
      final raw = decodeEventFrame({
        'event_type': 'mystery_event',
        'seq': 7,
        'timestamp': ts,
        'run_id': 'run-1',
        'payload': {'x': 1},
      });
      expect(raw, isNull);
    });

    test('drops a missing event_type', () {
      expect(
        decodeEventFrame({'seq': 1, 'timestamp': ts, 'run_id': 'r', 'payload': <String, Object>{}}),
        isNull,
      );
    });

    test('all known event types decode; unknowns do not', () {
      expect(EventFrame.knownEventTypes.length, 19);
      for (final t in EventFrame.knownEventTypes) {
        expect(
          decodeEventFrame({'event_type': t, 'seq': 0, 'timestamp': ts, 'run_id': null, 'payload': <String, Object>{}}),
          isNotNull,
          reason: 'expected $t to be a known type',
        );
      }
    });

    test('parses a raw JSON string frame end-to-end', () {
      const wire =
          '{"event_type":"node_completed","seq":9,"timestamp":"2026-08-29T10:40:00+00:00",'
          '"run_id":"run-2","payload":{"node_id":"b","run_id":"run-2","model":"draft",'
          '"tokens_used":120,"duration_seconds":3.5,"attempt":1}}';
      final frame = decodeEventFrameString(wire)!;
      expect(frame.eventType, 'node_completed');
      expect(frame.seq, 9);
      expect(frame.runId, 'run-2');
      expect(frame.payload['tokens_used'], 120);
      expect(frame.payload['duration_seconds'], 3.5);
    });
  });

  group('A2 RunAccepted / A3 RunHistoryEntry', () {
    test('RunAccepted decodes', () {
      final a = RunAccepted.fromJson({
        'run_id': 'r-1',
        'state': 'running',
        'workflow': 'wf',
      });
      expect(a.runId, 'r-1');
      expect(a.state, 'running');
      expect(a.workflow, 'wf');
    });

    test('RunHistoryEntry decodes (no finished_at in wire)', () {
      final e = RunHistoryEntry.fromJson({
        'run_id': 'r-2',
        'workflow_name': 'support_ticket',
        'started_at': '2026-08-29T09:00:00+00:00',
        'state': 'completed',
      });
      expect(e.runId, 'r-2');
      expect(e.workflowName, 'support_ticket');
      expect(e.startedAt, DateTime.utc(2026, 8, 29, 9, 0, 0));
      expect(e.state, 'completed');
    });
  });

  group('A4 RunStatusView', () {
    test('decodes with nodes map + pending_inputs', () {
      final v = RunStatusView.fromJson({
        'run_id': 'r-3',
        'workflow_name': 'wf',
        'state': 'running',
        'started_at': '2026-08-29T10:00:00+00:00',
        'replay_source': null,
        'nodes': {
          'a': {'status': 'running', 'model': 'draft', 'attempt': 1},
          'b': {
            'status': 'completed_fallback',
            'model': 'fb',
            'tokens': 10,
            'duration_seconds': 1.2,
            'attempt': 2,
            'fallback': 'fb',
          },
        },
        'pending_inputs': [
          {'node_id': 'c', 'prompt': 'approve?', 'deadline': null},
        ],
      });
      expect(v.runId, 'r-3');
      expect(v.state, 'running');
      expect(v.startedAt, DateTime.utc(2026, 8, 29, 10, 0, 0));
      expect(v.replaySource, isNull);
      expect(v.nodes.keys, containsAll(['a', 'b']));
      final b = v.nodes['b']!;
      expect(b.status, 'completed_fallback');
      expect(b.tokens, 10);
      expect(b.durationSeconds, 1.2);
      expect(b.fallback, 'fb');
      expect(v.pendingInputs.single.nodeId, 'c');
      expect(v.pendingInputs.single.prompt, 'approve?');
      expect(v.pendingInputs.single.deadline, isNull);
    });

    test('replay_source is preserved when set', () {
      final v = RunStatusView.fromJson({
        'run_id': 'r-4',
        'workflow_name': 'wf',
        'state': 'running',
        'started_at': '2026-08-29T10:00:00+00:00',
        'replay_source': 'r-3',
        'nodes': <String, Object>{},
        'pending_inputs': <Object>[],
      });
      expect(v.replaySource, 'r-3');
    });
  });

  group('A5 NodeStateView', () {
    test('decodes a failed node with reason', () {
      final n = NodeStateView.fromJson({
        'status': 'failed',
        'model': 'draft',
        'tokens': null,
        'duration_seconds': null,
        'attempt': 3,
        'fallback': null,
        'reason': 'boom',
      });
      expect(n.status, 'failed');
      expect(n.attempt, 3);
      expect(n.reason, 'boom');
      expect(n.tokens, isNull);
    });
  });

  group('A6 PendingInputView', () {
    test('decodes with a non-null deadline', () {
      final p = PendingInputView.fromJson({
        'node_id': 'hil',
        'prompt': 'confirm',
        'deadline': '2026-08-29T10:05:00+00:00',
      });
      expect(p.nodeId, 'hil');
      expect(p.deadline, DateTime.utc(2026, 8, 29, 10, 5, 0));
    });
  });

  group('A7 RunResultView / RunResultNodeView', () {
    test('decodes run result with node outputs', () {
      final r = RunResultView.fromJson({
        'run_id': 'r-5',
        'status': 'completed',
        'total_tokens': 500,
        'duration_seconds': 12.5,
        'nodes': [
          {
            'node_id': 'a',
            'status': 'completed',
            'model': 'draft',
            'tokens': 300,
            'duration_seconds': 6.0,
            'attempt': 1,
            'fallback': null,
            'reason': null,
            'output': 'hello',
          },
        ],
      });
      expect(r.runId, 'r-5');
      expect(r.status, 'completed');
      expect(r.totalTokens, 500);
      expect(r.durationSeconds, 12.5);
      expect(r.nodes.single.nodeId, 'a');
      expect(r.nodes.single.output, 'hello');
    });
  });

  group('A8 HealthResponse', () {
    test('decodes {status, log_dir} (no version)', () {
      final h = HealthResponse.fromJson({
        'status': 'ok',
        'log_dir': 'runs',
      });
      expect(h.status, 'ok');
      expect(h.logDir, 'runs');
    });
  });

  group('A9 ErrorBody + 9-code mapping', () {
    final all = {
      ErrorCodes.unknownWorkflow: 'Unknown workflow',
      ErrorCodes.invalidWorkflow: 'Invalid workflow',
      ErrorCodes.modelValidationFailed: 'Model validation failed',
      ErrorCodes.noReplaySource: 'No replay source',
      ErrorCodes.unknownRun: 'Unknown run',
      ErrorCodes.unknownNode: 'Unknown node',
      ErrorCodes.inputNotAwaiting: 'Input not awaiting',
      ErrorCodes.runNotFinished: 'Run not finished',
      ErrorCodes.invalidRequest: 'Invalid request',
    };

    test('ErrorBody decodes the wire envelope', () {
      final e = ErrorBody.fromJson({
        'code': ErrorCodes.unknownWorkflow,
        'message': 'nope',
        'details': {'workflow': 'x'},
      });
      expect(e.code, ErrorCodes.unknownWorkflow);
      expect(e.message, 'nope');
      expect(e.details, {'workflow': 'x'});
    });

    test('each of the 9 codes maps to a non-empty user-facing string', () {
      expect(all.length, 9);
      for (final entry in all.entries) {
        expect(userFacingMessage(entry.key), isNotEmpty);
      }
    });

    test('all codes are distinct wire values', () {
      final values = all.keys.toSet();
      expect(values.length, 9);
    });
  });
}
