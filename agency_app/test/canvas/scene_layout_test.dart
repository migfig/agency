import 'package:agencyapp/canvas/scene_layout.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:flutter_test/flutter_test.dart';

/// T006 — pure unit coverage of the deterministic scene layout:
/// longest-path column, id-sorted row, orphan trailing column (FR-012),
/// cycle-safety, and purity/memoization by (sourcePath, lastModified).
void main() {
  WorkflowDefinition def(String yaml, {String path = 'wf.yaml', int m = 0}) =>
      parseWorkflowDefinition(yaml, sourcePath: path, lastModified: m);

  group('column = longest-path depth', () {
    test('linear chain a→b→c yields cols 0,1,2', () {
      final d = def('''
name: chain
nodes:
  a: {id: a, type: agent, model: m}
  b: {id: b, type: agent, model: m}
  c: {id: c, type: agent, model: m}
edges:
  - {from_id: a, to_id: b}
  - {from_id: b, to_id: c}
''');
      final l = computeLayout(d);
      expect(l.positionOf('a'), const NodePosition(col: 0, row: 0));
      expect(l.positionOf('b'), const NodePosition(col: 1, row: 0));
      expect(l.positionOf('c'), const NodePosition(col: 2, row: 0));
      expect(l.columnCount, 3);
    });

    test('diamond: shared successor sits one column past its preds', () {
      final d = def('''
name: diamond
nodes:
  a: {id: a, type: agent, model: m}
  c: {id: c, type: agent, model: m}
  d: {id: d, type: agent, model: m}
  e: {id: e, type: agent, model: m}
edges:
  - {from_id: a, to_id: c}
  - {from_id: a, to_id: d}
  - {from_id: c, to_id: e}
  - {from_id: d, to_id: e}
''');
      final l = computeLayout(d);
      expect(l.positionOf('a').col, 0);
      expect(l.positionOf('c').col, 1);
      expect(l.positionOf('d').col, 1);
      expect(l.positionOf('e').col, 2);
    });
  });

  group('row = id-sorted within a column', () {
    test('independent entries sort by id into rows 0,1', () {
      final d = def('''
name: indep
nodes:
  b: {id: b, type: agent, model: m}
  a: {id: a, type: agent, model: m}
''');
      final l = computeLayout(d);
      expect(l.positionOf('a').col, 0);
      expect(l.positionOf('b').col, 0);
      expect(l.positionOf('a').row, 0);
      expect(l.positionOf('b').row, 1);
      expect(l.rowCount, 2);
    });
  });

  group('orphan run node ids (FR-012)', () {
    test('orphans go to a trailing column after the definition max col', () {
      final d = def('''
name: chain
nodes:
  a: {id: a, type: agent, model: m}
  b: {id: b, type: agent, model: m}
edges:
  - {from_id: a, to_id: b}
''');
      final l = computeLayout(d, runNodeIds: ['a', 'b', 'z']);
      // a=0, b=1 → max def col 1 → orphan column 2.
      expect(l.positionOf('z').col, 2);
      expect(l.positionOf('z').row, 0);
      // def nodes unchanged.
      expect(l.positionOf('a').col, 0);
      expect(l.positionOf('b').col, 1);
    });

    test('multiple orphans sort by id within the trailing column', () {
      final d = def('name: only\nnodes:\n  a: {id: a, type: agent, model: m}\n');
      final l = computeLayout(d, runNodeIds: ['a', 'z', 'y']);
      expect(l.positionOf('z').col, 1);
      expect(l.positionOf('y').col, 1);
      expect(l.positionOf('y').row, 0); // y < z
      expect(l.positionOf('z').row, 1);
    });
  });

  group('cycle-safe (workflows/cycle.yaml)', () {
    test('a→b→a terminates and assigns every node a position', () {
      final d = def('''
name: cycle
nodes:
  a: {id: a, type: tool_call}
  b: {id: b, type: tool_call}
edges:
  - {from_id: a, to_id: b}
  - {from_id: b, to_id: a}
''');
      final l = computeLayout(d);
      expect(l.positionOf('a'), isNotNull);
      expect(l.positionOf('b'), isNotNull);
      // Deterministic: both present, distinct positions.
      expect(l.positionOf('a'), isNot(l.positionOf('b')));
    });

    test('mixed acyclic + cyclic graph still places the acyclic head correctly', () {
      final d = def('''
name: mixed
nodes:
  head: {id: head, type: agent, model: m}
  a: {id: a, type: agent, model: m}
  b: {id: b, type: agent, model: m}
edges:
  - {from_id: head, to_id: a}
  - {from_id: a, to_id: b}
  - {from_id: b, to_id: a}
''');
      final l = computeLayout(d);
      expect(l.positionOf('head').col, 0);
      expect(l.positions.keys, containsAll(['head', 'a', 'b']));
    });
  });

  group('purity + memoization', () {
    test('same input twice → identical layout (pure)', () {
      final d = def('''
name: p
nodes:
  a: {id: a, type: agent, model: m}
  b: {id: b, type: agent, model: m}
edges:
  - {from_id: a, to_id: b}
''');
      final l1 = computeLayout(d);
      final l2 = computeLayout(d);
      expect(l1.positions, equals(l2.positions));
      expect(l1.columnCount, l2.columnCount);
    });

    test('a different (sourcePath,lastModified) identity recomputes, not a stale hit', () {
      final base = '''
name: p
nodes:
  a: {id: a, type: agent, model: m}
  b: {id: b, type: agent, model: m}
edges:
  - {from_id: a, to_id: b}
''';
      final before = computeLayout(parseWorkflowDefinition(base, sourcePath: 'x.yaml', lastModified: 1));
      final after = computeLayout(parseWorkflowDefinition(base, sourcePath: 'x.yaml', lastModified: 2));
      expect(before.positions, equals(after.positions));
      expect(after.columnCount, 2);
    });
  });
}
