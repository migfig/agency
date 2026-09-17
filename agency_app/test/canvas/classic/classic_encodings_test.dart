import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/canvas/classic/classic_encodings.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:flutter_test/flutter_test.dart';

/// T005 — pure unit coverage of the classic encoding tables (data-model.md
/// Validation Rules 2–4): the 7-state and 6-type tables are total over the
/// existing vocabularies, all six type icons are distinct, names match the
/// `NodeType.wire` vocabulary, and all seven states are pairwise
/// distinguishable by label + marker alone (never color).
void main() {
  final allStates = NodeStatus.values;
  final allTypes = NodeType.values;

  group('state-encoding table', () {
    test('table is total over the 7-value NodeStatus vocabulary', () {
      expect(allStates.length, 7);
      expect(kClassicStateEncodings.keys.toSet(), allStates.toSet());
      for (final s in allStates) {
        final e = classicStateEncodingFor(s);
        expect(e.label, isNotEmpty, reason: '$s has an empty label');
        expect(e.marker, isNotNull, reason: '$s has no marker');
      }
    });

    test('label + marker alone are pairwise distinct across the 7 states', () {
      final pairs = allStates
          .map((s) {
            final e = classicStateEncodingFor(s);
            return '${e.label}|${e.marker.name}';
          })
          .toSet();
      expect(pairs.length, 7, reason: 'label+marker pairs must be unique: $pairs');
    });

    test('markers alone are pairwise distinct across the 7 states', () {
      expect(allStates.map((s) => classicStateEncodingFor(s).marker).toSet().length, 7);
    });

    test('animated flag matches the marker (motion only for pulse/countdown)', () {
      for (final s in allStates) {
        final e = classicStateEncodingFor(s);
        final hasMotion =
            e.marker == ClassicMarker.pulse || e.marker == ClassicMarker.countdown;
        expect(e.animated, hasMotion, reason: '$s animated flag disagrees with marker');
      }
    });
  });

  group('type-marker table', () {
    test('table is total over the 6-value NodeType vocabulary', () {
      expect(allTypes.length, 6);
      expect(kTypeMarkers.keys.toSet(), allTypes.toSet());
      for (final t in allTypes) {
        expect(typeMarkerFor(t).icon, isNotNull, reason: '$t has no icon');
      }
    });

    test('all six type icons are distinct', () {
      final codes = allTypes.map((t) => typeMarkerFor(t).icon.codePoint).toSet();
      expect(codes.length, 6, reason: 'icons must be unique: $codes');
    });

    test('names match the NodeType.wire vocabulary', () {
      for (final t in allTypes) {
        expect(typeMarkerFor(t).name, t.wire, reason: '$t name must be its wire value');
      }
    });
  });
}
