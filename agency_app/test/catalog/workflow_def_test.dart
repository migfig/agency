import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:flutter_test/flutter_test.dart';

/// T005 — pure unit coverage of the lenient YAML workflow parser
/// (contracts/workflow-catalog.md §4): parse the real YAML subset, surface
/// the minimal required set, stay readable on malformed input.
void main() {
  group('valid parsing', () {
    test('parses a real workflow (wf.yaml) with nodes, retry, fallback, phases, edges', () {
      const yaml = '''
name: test_workflow
entry_point: start
nodes:
  start:
    id: start
    type: agent
    model: draft
    retry:
      max_attempts: 3
      backoff: exponential
      base_delay_seconds: 2
      max_delay_seconds: 30
      timeout_seconds: 300
  review:
    id: review
    type: tool_call
    tool: echo
    arguments_template: '{"text": "hi"}'
  merge:
    id: merge
    type: merge
edges:
  - from_id: start
    to_id: review
  - from_id: review
    to_id: merge
phases:
  - id: p1
    name: Drafting
    node_ids:
      - start
      - review
''';
      final def = parseWorkflowDefinition(yaml, sourcePath: 'wf.yaml', lastModified: 0);
      expect(def.name, 'test_workflow');
      expect(def.sourcePath, 'wf.yaml');
      expect(def.nodeById('start')!.type, NodeType.agent);
      expect(def.nodeById('start')!.retry, isNotNull);
      expect(def.nodeById('start')!.retry!.maxAttempts, 3);
      expect(def.nodeById('review')!.type, NodeType.toolCall);
      expect(def.nodeById('merge')!.type, NodeType.merge);
      expect(def.edges, hasLength(2));
      expect(def.edges.first.fromId, 'start');
      expect(def.phases.single.name, 'Drafting');
      expect(def.phases.single.nodeIds, ['start', 'review']);
      expect(def.nodes, hasLength(3));
    });

    test('parses conditional / broadcast / human_in_loop node types', () {
      const yaml = '''
name: t2
nodes:
  c:
    id: c
    type: conditional
  b:
    id: b
    type: broadcast
  h:
    id: h
    type: human_in_loop
''';
      final def = parseWorkflowDefinition(yaml);
      expect(def.nodeById('c')!.type, NodeType.conditional);
      expect(def.nodeById('b')!.type, NodeType.broadcast);
      expect(def.nodeById('h')!.type, NodeType.humanInLoop);
    });

    test('node id falls back to the map key when id is omitted', () {
      const yaml = '''
name: t3
nodes:
  auto_key:
    type: agent
    model: draft
''';
      final def = parseWorkflowDefinition(yaml);
      expect(def.nodeById('auto_key'), isNotNull);
      expect(def.nodeById('auto_key')!.id, 'auto_key');
    });

    test('supports YAML anchors/aliases and multi-line scalars', () {
      const yaml = '''
name: t4
models:
  &base
    path: /tmp/m.gguf
nodes:
  a:
    id: a
    type: agent
    model: *base
    prompt: >
      line one
      line two
''';
      final def = parseWorkflowDefinition(yaml);
      expect(def.nodeById('a')!.model, '/tmp/m.gguf');
      expect(def.nodeById('a')!.prompt, contains('line one'));
    });

    test('tolerates dangling edge ids (FR-022)', () {
      const yaml = '''
name: t5
nodes:
  a:
    id: a
    type: agent
    model: draft
edges:
  - from_id: a
    to_id: ghost
''';
      final def = parseWorkflowDefinition(yaml);
      expect(def.edges.single.toId, 'ghost');
      // ghost is not a node; parsing must not throw.
      expect(def.nodeById('ghost'), isNull);
    });
  });

  group('lenient / malformed input (FR-022)', () {
    test('unknown top-level keys are ignored', () {
      const yaml = '''
name: t6
totally_unknown_key: [1, 2, 3]
vram_limit: 8g
nodes:
  a:
    id: a
    type: agent
''';
      final def = parseWorkflowDefinition(yaml);
      expect(def.name, 't6');
      expect(def.nodes, hasLength(1));
    });

    test('unknown node keys are ignored', () {
      const yaml = '''
name: t7
nodes:
  a:
    id: a
    type: agent
    mystery_field: 42
''';
      final def = parseWorkflowDefinition(yaml);
      expect(def.nodeById('a')!.model, isNull);
      expect(def.nodeById('a')!.type, NodeType.agent);
    });

    test('missing name → WorkflowParseError with "missing name"', () {
      const yaml = '''
nodes:
  a:
    id: a
    type: agent
''';
      final err = expectThrowsWorkflowParse(yaml, reason: 'missing name');
      expect(err, contains('missing name'));
    });

    test('invalid node type → unreadable naming the node', () {
      const yaml = '''
name: t8
nodes:
  bad_node:
    id: bad_node
    type: not_a_real_type
''';
      final err = expectThrowsWorkflowParse(yaml, reason: 'invalid node type');
      expect(err, contains('bad_node'));
    });

    test('a node map with a non-string id is treated as invalid', () {
      const yaml = '''
name: t9
nodes:
  bad_node:
    id: [1, 2, 3]
    type: agent
''';
      expectThrowsWorkflowParse(yaml, reason: 'invalid node id');
    });
  });
}

Object expectThrowsWorkflowParse(String yaml, {required String reason}) {
  try {
    parseWorkflowDefinition(yaml);
  } on WorkflowParseError catch (e) {
    // ignore: avoid_print
    print('  (expected $reason): ${e.message}');
    return e.message;
  }
  fail('expected WorkflowParseError for: $reason');
}
