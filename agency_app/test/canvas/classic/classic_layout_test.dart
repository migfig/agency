import 'package:agencyapp/canvas/classic/classic_layout.dart';
import 'package:agencyapp/canvas/classic/classic_painter.dart';
import 'package:agencyapp/canvas/classic/classic_topology.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// T009 — pure unit coverage of `ClassicLayout`: the `computeLayout`
/// (col, row) grid mapped to world-px card rectangles (200×72 at column gap
/// 90 / row gap 36 / padding 60), the entry set (in-degree 0 in the
/// definition edges; orphans/cycles never entries), and edge anchors
/// (source right-midpoint → target left-midpoint) with edges emitted only
/// when both endpoints are placed.
void main() {
  WorkflowDefinition def(String yaml) => parseWorkflowDefinition(yaml);

  void expectNoOverlap(ClassicLayout l) {
    final rects = l.cards.values.toList();
    for (var i = 0; i < rects.length; i++) {
      for (var j = i + 1; j < rects.length; j++) {
        final a = rects[i].rect;
        final b = rects[j].rect;
        final overlap = !(a.right <= b.left ||
            b.right <= a.left ||
            a.bottom <= b.top ||
            b.bottom <= a.top);
        expect(overlap, isFalse,
            reason: '${rects[i].nodeId} overlaps ${rects[j].nodeId}');
      }
    }
  }

  group('card placement', () {
    test('every run node id is placed (def nodes + orphans in trailing columns)',
        () {
      final d = def('''
name: chain
nodes:
  a: {id: a, type: agent, model: m}
  b: {id: b, type: agent, model: m}
edges:
  - {from_id: a, to_id: b}
''');
      final l = buildClassicLayout(d, runNodeIds: ['a', 'b', 'z'], topology: CanvasTopology.leftRight);
      expect(l.cards.keys.toSet(), {'a', 'b', 'z'});
      // a=0, b=1 → orphan z goes to the trailing column 2.
      expect(l.cards['z']!.col, 2);
      expect(l.cards['z']!.row, 0);
      // def nodes keep their grid positions.
      expect(l.cards['a']!.col, 0);
      expect(l.cards['b']!.col, 1);
    });

    test('cards are 200×72 world px at column gap 90 / row gap 36 / padding 60',
        () {
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
      final l = buildClassicLayout(d, topology: CanvasTopology.leftRight);
      // a=(0,0), c=(1,0), d=(1,1), e=(2,0).
      // Pitch X = 200+90 = 290, pitch Y = 72+36 = 108, padding 60.
      expect(l.cards['a']!.rect, const Rect.fromLTWH(60, 60, 200, 72));
      expect(l.cards['c']!.rect, const Rect.fromLTWH(350, 60, 200, 72));
      expect(l.cards['d']!.rect, const Rect.fromLTWH(350, 168, 200, 72));
      expect(l.cards['e']!.rect, const Rect.fromLTWH(640, 60, 200, 72));
      expectNoOverlap(l);
    });

    test('no pair of cards overlaps for a dense run', () {
      final d = def('''
name: dense
nodes:
  a: {id: a, type: agent, model: m}
  b: {id: b, type: agent, model: m}
  c: {id: c, type: agent, model: m}
  d: {id: d, type: agent, model: m}
  e: {id: e, type: agent, model: m}
  f: {id: f, type: agent, model: m}
edges:
  - {from_id: a, to_id: c}
  - {from_id: a, to_id: d}
  - {from_id: b, to_id: e}
  - {from_id: c, to_id: f}
  - {from_id: d, to_id: f}
  - {from_id: e, to_id: f}
''');
      final l = buildClassicLayout(
        d,
        runNodeIds: ['a', 'b', 'c', 'd', 'e', 'f'],
        topology: CanvasTopology.leftRight,
      );
      expect(l.cards.length, 6);
      expectNoOverlap(l);
    });
  });

  group('entry set', () {
    test('entry = in-degree 0 in the definition edges', () {
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
      final l = buildClassicLayout(d, topology: CanvasTopology.leftRight);
      expect(l.cards['a']!.isEntry, isTrue);
      for (final id in ['c', 'd', 'e']) {
        expect(l.cards[id]!.isEntry, isFalse, reason: '$id must not be an entry');
      }
    });

    test('multiple independent entries are all marked', () {
      final d = def('''
name: indep
nodes:
  a: {id: a, type: agent, model: m}
  b: {id: b, type: agent, model: m}
  c: {id: c, type: agent, model: m}
edges:
  - {from_id: a, to_id: c}
  - {from_id: b, to_id: c}
''');
      final l = buildClassicLayout(d, topology: CanvasTopology.leftRight);
      expect(l.cards['a']!.isEntry, isTrue);
      expect(l.cards['b']!.isEntry, isTrue);
      expect(l.cards['c']!.isEntry, isFalse);
    });

    test('cycle nodes are never entries (a→b→a)', () {
      final d = def('''
name: cycle
nodes:
  a: {id: a, type: tool_call}
  b: {id: b, type: tool_call}
edges:
  - {from_id: a, to_id: b}
  - {from_id: b, to_id: a}
''');
      final l = buildClassicLayout(d, topology: CanvasTopology.leftRight);
      expect(l.cards['a']!.isEntry, isFalse);
      expect(l.cards['b']!.isEntry, isFalse);
      // The cycle still places both nodes.
      expect(l.cards.length, 2);
    });

    test('orphan run nodes are never entries', () {
      final d = def('''
name: chain
nodes:
  a: {id: a, type: agent, model: m}
edges:
  - {from_id: a, to_id: a}
''');
      final l = buildClassicLayout(d, runNodeIds: ['a', 'z'], topology: CanvasTopology.leftRight);
      expect(l.cards['z']!.isEntry, isFalse);
    });

    test('single-step workflow = one entry card with no edges', () {
      final d = def('name: one\nnodes:\n  a: {id: a, type: agent, model: m}\n');
      final l = buildClassicLayout(d, topology: CanvasTopology.leftRight);
      expect(l.cards.keys, {'a'});
      expect(l.cards['a']!.isEntry, isTrue);
      expect(l.edges, isEmpty);
    });
  });

  group('edge anchors', () {
    test('source = source card right-edge midpoint, target = target card left-edge midpoint',
        () {
      final d = def('''
name: chain
nodes:
  a: {id: a, type: agent, model: m}
  b: {id: b, type: agent, model: m}
edges:
  - {from_id: a, to_id: b}
''');
      final l = buildClassicLayout(d, topology: CanvasTopology.leftRight);
      expect(l.edges, hasLength(1));
      final e = l.edges.single;
      expect(e.fromId, 'a');
      expect(e.toId, 'b');
      final sa = l.cards['a']!.rect;
      final tb = l.cards['b']!.rect;
      expect(e.sourceAnchor, Offset(sa.right, sa.center.dy));
      expect(e.targetAnchor, Offset(tb.left, tb.center.dy));
    });

    test('edges are emitted only when both endpoints are placed', () {
      final d = def('''
name: dang
nodes:
  a: {id: a, type: agent, model: m}
edges:
  - {from_id: a, to_id: x}
''');
      // x is not a definition node and not a run node → unplaced → no edge.
      final without = buildClassicLayout(d, runNodeIds: ['a'], topology: CanvasTopology.leftRight);
      expect(without.edges, isEmpty);
      // When x shows up as an orphan run node it is placed → edge emitted.
      final withX = buildClassicLayout(d, runNodeIds: ['a', 'x'], topology: CanvasTopology.leftRight);
      expect(withX.edges, hasLength(1));
      expect(withX.edges.single.fromId, 'a');
      expect(withX.edges.single.toId, 'x');
    });

    test('purity: same input twice → identical layout', () {
      final d = def('''
name: p
nodes:
  a: {id: a, type: agent, model: m}
  b: {id: b, type: agent, model: m}
edges:
  - {from_id: a, to_id: b}
''');
      expect(buildClassicLayout(d, runNodeIds: ['a', 'b'], topology: CanvasTopology.leftRight),
          buildClassicLayout(d, runNodeIds: ['a', 'b'], topology: CanvasTopology.leftRight));
    });
  });

  group('topologies', () {
    // The diamond grid: a=(col 0,row 0), c=(1,0), d=(1,1), e=(2,0); colMax 2.
    WorkflowDefinition diamond() => def('''
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

    // The two-node chain a→b: a=(col 0,row 0), b=(1,0); colMax 1.
    WorkflowDefinition chain() => def('''
name: chain
nodes:
  a: {id: a, type: agent, model: m}
  b: {id: b, type: agent, model: m}
edges:
  - {from_id: a, to_id: b}
''');

    test('every topology maps the same (col,row) grid, keeps the entry set, '
        'and never overlaps', () {
      final d = diamond();
      for (final t in CanvasTopology.values) {
        final l = buildClassicLayout(d, topology: t);
        expect(l.cards.length, 4, reason: '$t places every node');
        // The same computeLayout grid under every topology.
        expect(l.cards['a']!.col, 0);
        expect(l.cards['c']!.col, 1);
        expect(l.cards['d']!.col, 1);
        expect(l.cards['e']!.col, 2);
        expect(l.cards['d']!.row, 1);
        expect(l.cards['a']!.isEntry, isTrue, reason: '$t keeps the entry set');
        for (final id in ['c', 'd', 'e']) {
          expect(l.cards[id]!.isEntry, isFalse,
              reason: '$t: $id must not be an entry');
        }
        expectNoOverlap(l);
      }
    });

    test('leftRight: x = col·pitchX, y = row·pitchY (+ padding)', () {
      final l = buildClassicLayout(diamond(), topology: CanvasTopology.leftRight);
      expect(l.cards['a']!.rect, const Rect.fromLTWH(60, 60, 200, 72));
      expect(l.cards['c']!.rect, const Rect.fromLTWH(350, 60, 200, 72));
      expect(l.cards['d']!.rect, const Rect.fromLTWH(350, 168, 200, 72));
      expect(l.cards['e']!.rect, const Rect.fromLTWH(640, 60, 200, 72));
    });

    test('topDown: x = row·pitchX, y = col·pitchY (+ padding)', () {
      final l = buildClassicLayout(diamond(), topology: CanvasTopology.topDown);
      expect(l.cards['a']!.rect, const Rect.fromLTWH(60, 60, 200, 72));
      expect(l.cards['c']!.rect, const Rect.fromLTWH(60, 168, 200, 72));
      expect(l.cards['d']!.rect, const Rect.fromLTWH(350, 168, 200, 72));
      expect(l.cards['e']!.rect, const Rect.fromLTWH(60, 276, 200, 72));
    });

    test('rightLeft: x = (colMax−col)·pitchX, y = row·pitchY (+ padding)', () {
      final l = buildClassicLayout(diamond(), topology: CanvasTopology.rightLeft);
      expect(l.cards['a']!.rect, const Rect.fromLTWH(640, 60, 200, 72));
      expect(l.cards['c']!.rect, const Rect.fromLTWH(350, 60, 200, 72));
      expect(l.cards['d']!.rect, const Rect.fromLTWH(350, 168, 200, 72));
      expect(l.cards['e']!.rect, const Rect.fromLTWH(60, 60, 200, 72));
    });

    test('bottomUp: x = row·pitchX, y = (colMax−col)·pitchY (+ padding)', () {
      final l = buildClassicLayout(diamond(), topology: CanvasTopology.bottomUp);
      expect(l.cards['a']!.rect, const Rect.fromLTWH(60, 276, 200, 72));
      expect(l.cards['c']!.rect, const Rect.fromLTWH(60, 168, 200, 72));
      expect(l.cards['d']!.rect, const Rect.fromLTWH(350, 168, 200, 72));
      expect(l.cards['e']!.rect, const Rect.fromLTWH(60, 60, 200, 72));
    });

    test('mirrored topologies reflect about colMax: the max-col card sits at '
        'the padding edge', () {
      // rightLeft: flow ←, so the deepest node (e, col 2 = colMax) starts at
      // the left padding edge and the entry (a) sits farthest right.
      final rl = buildClassicLayout(diamond(), topology: CanvasTopology.rightLeft);
      expect(rl.cards['e']!.rect.left, 60,
          reason: 'max-col card at the left padding edge');
      expect(rl.cards['a']!.rect.right, greaterThan(rl.cards['e']!.rect.right));
      // bottomUp: flow ↑, so the max-col card sits at the top padding edge.
      final bu = buildClassicLayout(diamond(), topology: CanvasTopology.bottomUp);
      expect(bu.cards['e']!.rect.top, 60,
          reason: 'max-col card at the top padding edge');
      expect(bu.cards['a']!.rect.bottom, greaterThan(bu.cards['e']!.rect.bottom));
    });

    test('edge anchors + control per topology (the flow-side midpoints)', () {
      final lrs = buildClassicLayout(chain(), topology: CanvasTopology.leftRight);
      final lr = lrs.edges.single;
      expect(lr.sourceAnchor, const Offset(260, 96),
          reason: 'source right-midpoint (a spans x 60–260, y 60–132)');
      expect(lr.targetAnchor, const Offset(350, 96), reason: 'target left-midpoint');
      expect(lr.control, const Offset(305, 96),
          reason: 'control on the flow axis: (midX, srcY)');

      final td = buildClassicLayout(chain(), topology: CanvasTopology.topDown).edges.single;
      expect(td.sourceAnchor, const Offset(160, 132), reason: 'source bottom-midpoint');
      expect(td.targetAnchor, const Offset(160, 168), reason: 'target top-midpoint');
      expect(td.control, const Offset(160, 150),
          reason: 'control on the flow axis: (srcX, midY)');

      final rl = buildClassicLayout(chain(), topology: CanvasTopology.rightLeft).edges.single;
      expect(rl.sourceAnchor, const Offset(350, 96), reason: 'source left-midpoint');
      expect(rl.targetAnchor, const Offset(260, 96), reason: 'target right-midpoint');
      expect(rl.control, const Offset(305, 96),
          reason: 'control on the flow axis: (midX, srcY)');

      final bu = buildClassicLayout(chain(), topology: CanvasTopology.bottomUp).edges.single;
      expect(bu.sourceAnchor, const Offset(160, 168), reason: 'source top-midpoint');
      expect(bu.targetAnchor, const Offset(160, 132), reason: 'target bottom-midpoint');
      expect(bu.control, const Offset(160, 150),
          reason: 'control on the flow axis: (srcX, midY)');
    });

    test('classicEdgeControl returns the stored control and the Bézier '
        'endpoints land on the anchors', () {
      for (final t in CanvasTopology.values) {
        final e = buildClassicLayout(chain(), topology: t).edges.single;
        expect(classicEdgeControl(e), e.control,
            reason: '$t: the painter uses the layout-time control');
        expect(classicEdgePointAt(e, 0), e.sourceAnchor,
            reason: '$t: the curve starts on the source anchor');
        expect(classicEdgePointAt(e, 1), e.targetAnchor,
            reason: '$t: the curve ends on the target anchor');
      }
    });
  });
}
