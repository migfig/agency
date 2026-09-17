import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/canvas/classic/classic_encodings.dart';
import 'package:agencyapp/canvas/classic/classic_scene.dart';
import 'package:agencyapp/canvas/classic/classic_topology.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:agencyapp/run/run_state.dart';
import 'package:flutter_test/flutter_test.dart';

/// T010 — `buildClassicScene(state, definition)` is pure and deterministic
/// (same inputs → identical scene; no wall clock, no randomness — FR-020):
/// a state encoding exists for every node, edges are emitted only when both
/// endpoints are placed, and orphan/cycle/single-step definitions still
/// produce a full scene.
void main() {
  final d = parseWorkflowDefinition('''
name: settled
nodes:
  a: {id: a, type: agent, model: draft}
  b: {id: b, type: agent, model: draft, fallback: fb}
  c: {id: c, type: tool_call, tool: echo}
edges:
  - {from_id: a, to_id: b}
  - {from_id: b, to_id: c}
''');

  RunState settledState() => reduce(
        RunStatusView.fromJson({
          'run_id': 'run-1',
          'workflow_name': 'settled',
          'state': 'completed',
          'started_at': '2026-08-29T10:00:00+00:00',
          'nodes': <String, Object>{
            'a': {'status': 'completed', 'attempt': 1, 'tokens': 12, 'duration_seconds': 1.5},
            'b': {'status': 'completed_fallback', 'attempt': 1, 'fallback': 'fb', 'tokens': 30, 'duration_seconds': 2.0},
            'c': {'status': 'skipped', 'attempt': 1, 'reason': 'unresolved_binding'},
          },
          'pending_inputs': <Object>[],
        }),
        definition: d,
      );

  group('buildClassicScene', () {
    test('is pure and deterministic: same inputs → identical scene', () {
      final state = settledState();
      final s1 = buildClassicScene(state, definition: d, topology: CanvasTopology.leftRight);
      final s2 = buildClassicScene(state, definition: d, topology: CanvasTopology.leftRight);
      expect(s2, equals(s1), reason: 'FR-020: no wall clock, no randomness');
      // A value-identical (but distinct instance of the) definition yields
      // the same scene too.
      final d2 = parseWorkflowDefinition('''
name: settled
nodes:
  a: {id: a, type: agent, model: draft}
  b: {id: b, type: agent, model: draft, fallback: fb}
  c: {id: c, type: tool_call, tool: echo}
edges:
  - {from_id: a, to_id: b}
  - {from_id: b, to_id: c}
''');
      expect(
        buildClassicScene(state, definition: d2, topology: CanvasTopology.leftRight),
        equals(s1),
      );
    });

    test('is deterministic for a topology and differs only when the topology differs',
        () {
      final state = settledState();
      final lr =
          buildClassicScene(state, definition: d, topology: CanvasTopology.leftRight);
      final lrAgain =
          buildClassicScene(state, definition: d, topology: CanvasTopology.leftRight);
      expect(lr.topology, CanvasTopology.leftRight);
      expect(lrAgain, equals(lr),
          reason: 'FR-020: same inputs (incl. topology) → identical scene');

      for (final t in CanvasTopology.values) {
        if (t == CanvasTopology.leftRight) continue;
        final other =
            buildClassicScene(state, definition: d, topology: t);
        expect(other.topology, t);
        expect(other, isNot(equals(lr)),
            reason: '$t re-lays the same state out in a different orientation');
      }
    });

    test('a state encoding exists for every node (total)', () {
      final state = settledState();
      final s = buildClassicScene(state, definition: d, topology: CanvasTopology.leftRight);
      expect(s.encodings.keys.toSet(), state.nodes.keys.toSet());
      expect(s.encodings['a'], classicStateEncodingFor(NodeStatus.completed));
      expect(s.encodings['b'], classicStateEncodingFor(NodeStatus.completedFallback));
      expect(s.encodings['c'], classicStateEncodingFor(NodeStatus.skipped));
    });

    test('carries the run id, state, and definition', () {
      final state = settledState();
      final s = buildClassicScene(state, definition: d, topology: CanvasTopology.leftRight);
      expect(s.runId, 'run-1');
      expect(s.state, same(state));
      expect(s.definition, same(d));
    });

    test('edges emitted only when both endpoints are placed', () {
      final state = settledState();
      final s = buildClassicScene(state, definition: d, topology: CanvasTopology.leftRight);
      expect(s.layout.edges.map((e) => '${e.fromId}>${e.toId}').toSet(),
          {'a>b', 'b>c'});

      // Dangling edge: x is neither a definition node nor a run node.
      final dang = parseWorkflowDefinition('''
name: dang
nodes:
  a: {id: a, type: agent, model: draft}
edges:
  - {from_id: a, to_id: x}
''');
      final dangState = reduce(
        RunStatusView.fromJson({
          'run_id': 'run-2',
          'workflow_name': 'dang',
          'state': 'completed',
          'started_at': '2026-08-29T10:00:00+00:00',
          'nodes': <String, Object>{
            'a': {'status': 'completed', 'attempt': 1, 'tokens': 1, 'duration_seconds': 0.5},
          },
          'pending_inputs': <Object>[],
        }),
        definition: dang,
      );
      expect(
          buildClassicScene(
              dangState,
              definition: dang,
              topology: CanvasTopology.leftRight)
          .layout.edges,
          isEmpty);
    });

    test('orphan, cycle, and single-step definitions still produce a full scene',
        () {
      // Orphan: z is a run node absent from the definition.
      final orphanState = reduce(
        RunStatusView.fromJson({
          'run_id': 'run-3',
          'workflow_name': 'settled',
          'state': 'completed',
          'started_at': '2026-08-29T10:00:00+00:00',
          'nodes': <String, Object>{
            'a': {'status': 'completed', 'attempt': 1, 'tokens': 1, 'duration_seconds': 0.5},
            'b': {'status': 'completed', 'attempt': 1, 'tokens': 1, 'duration_seconds': 0.5},
            'c': {'status': 'completed', 'attempt': 1, 'tokens': 1, 'duration_seconds': 0.5},
            'z': {'status': 'completed', 'attempt': 1, 'tokens': 1, 'duration_seconds': 0.5},
          },
          'pending_inputs': <Object>[],
        }),
        definition: d,
      );
      final orphanScene = buildClassicScene(
        orphanState,
        definition: d,
        topology: CanvasTopology.leftRight,
      );
      expect(orphanScene.layout.cards.keys.toSet(), {'a', 'b', 'c', 'z'});
      expect(orphanScene.encodings.keys.toSet(), {'a', 'b', 'c', 'z'});

      // Cycle: a→b→a terminates and still yields a full scene.
      final cyc = parseWorkflowDefinition('''
name: cycle
nodes:
  a: {id: a, type: tool_call}
  b: {id: b, type: tool_call}
edges:
  - {from_id: a, to_id: b}
  - {from_id: b, to_id: a}
''');
      final cycState = reduce(
        RunStatusView.fromJson({
          'run_id': 'run-4',
          'workflow_name': 'cycle',
          'state': 'completed',
          'started_at': '2026-08-29T10:00:00+00:00',
          'nodes': <String, Object>{
            'a': {'status': 'completed', 'attempt': 1, 'tokens': 1, 'duration_seconds': 0.5},
            'b': {'status': 'completed', 'attempt': 1, 'tokens': 1, 'duration_seconds': 0.5},
          },
          'pending_inputs': <Object>[],
        }),
        definition: cyc,
      );
      final cycScene = buildClassicScene(
        cycState,
        definition: cyc,
        topology: CanvasTopology.leftRight,
      );
      expect(cycScene.layout.cards.keys.toSet(), {'a', 'b'});
      expect(cycScene.encodings.keys.toSet(), {'a', 'b'});

      // Single step.
      final one = parseWorkflowDefinition(
          'name: one\nnodes:\n  a: {id: a, type: agent, model: draft}\n');
      final oneState = reduce(
        RunStatusView.fromJson({
          'run_id': 'run-5',
          'workflow_name': 'one',
          'state': 'completed',
          'started_at': '2026-08-29T10:00:00+00:00',
          'nodes': <String, Object>{
            'a': {'status': 'completed', 'attempt': 1, 'tokens': 1, 'duration_seconds': 0.5},
          },
          'pending_inputs': <Object>[],
        }),
        definition: one,
      );
      final oneScene = buildClassicScene(
        oneState,
        definition: one,
        topology: CanvasTopology.leftRight,
      );
      expect(oneScene.layout.cards.keys, {'a'});
      expect(oneScene.encodings.keys, {'a'});
      expect(oneScene.layout.edges, isEmpty);
      expect(oneScene.layout.cards['a']!.isEntry, isTrue);
    });
  });
}
