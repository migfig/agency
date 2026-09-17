import 'package:agencyapp/monitor/metric_meta.dart';
import 'package:agencyapp/monitor/metrics_dialog.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// T022 — the per-metric visibility dialog (US3, FR-012,
/// contracts/ui.md "Per-metric visibility"): lists all five metrics as
/// checkboxes in display order, reflects the current `visible` set, and
/// invoking a checkbox calls the toggle callback.
void main() {
  const Map<MetricKey, bool> visible = {
    MetricKey.cpu: true,
    MetricKey.ram: true,
    MetricKey.gpu: true,
    MetricKey.netDown: false,
    MetricKey.netUp: false,
  };

  Future<void> pumpDialog(
    WidgetTester tester,
    ThemeMode mode,
    Map<MetricKey, bool> visible,
    void Function(MetricKey, bool) onToggle,
  ) async {
    await tester.pumpWidget(
      MaterialApp(
        theme: themeDataFor(mode),
        home: Scaffold(
          body: Center(
            child: MetricsDialog(visible: visible, onToggle: onToggle),
          ),
        ),
      ),
    );
  }

  List<CheckboxListTile> tiles(WidgetTester tester) => tester
      .widgetList<CheckboxListTile>(find.byType(CheckboxListTile))
      .toList();

  String titleOf(CheckboxListTile tile) => (tile.title! as Text).data!;

  Finder checkboxFor(String title) => find.descendant(
        of: find.byWidgetPredicate(
            (w) => w is CheckboxListTile && titleOf(w) == title),
        matching: find.byType(Checkbox),
      );

  group('MetricsDialog (US3)', () {
    testWidgets(
        'lists all five metrics as checkboxes in display order, reflecting '
        'the current visible set (FR-012)', (tester) async {
      await pumpDialog(tester, ThemeMode.dark, Map.of(visible), (k, v) {});

      final list = tiles(tester);
      expect(list, hasLength(5));
      expect(
        list.map(titleOf),
        ['CPU Usage', 'GPU Usage', 'Network Down', 'Network Up', 'RAM Usage'],
        reason: 'the display order (the reference app title order)',
      );
      expect(
        list.map((t) => t.value),
        [true, true, false, false, true],
        reason: 'the checkboxes reflect the current visible set '
            '(cpu, gpu, netDown, netUp, ram in display order)',
      );
      expect(tester.takeException(), isNull);
    });

    testWidgets('renders in both themes without errors (FR-011)',
        (tester) async {
      await pumpDialog(tester, ThemeMode.light, Map.of(visible), (k, v) {});
      expect(tester.takeException(), isNull);
      expect(find.byType(CheckboxListTile), findsNWidgets(5));

      await pumpDialog(tester, ThemeMode.dark, Map.of(visible), (k, v) {});
      expect(tester.takeException(), isNull);
      expect(find.byType(CheckboxListTile), findsNWidgets(5));
    });

    testWidgets('invoking a checkbox calls the toggle callback (FR-012)',
        (tester) async {
      final calls = <(MetricKey, bool)>[];
      await pumpDialog(
          tester, ThemeMode.dark, Map.of(visible), (k, v) => calls.add((k, v)));

      // Toggle the default-off Network Down metric on.
      await tester.tap(checkboxFor('Network Down'));
      await tester.pump();
      expect(calls, [(MetricKey.netDown, true)]);
      expect(
        tiles(tester).singleWhere((t) => titleOf(t) == 'Network Down').value,
        isTrue,
        reason: 'the checkbox reflects the toggle',
      );

      // Toggle the default-on CPU metric off.
      await tester.tap(checkboxFor('CPU Usage'));
      await tester.pump();
      expect(calls, [(MetricKey.netDown, true), (MetricKey.cpu, false)]);
      expect(
        tiles(tester).singleWhere((t) => titleOf(t) == 'CPU Usage').value,
        isFalse,
      );
    });
  });
}
