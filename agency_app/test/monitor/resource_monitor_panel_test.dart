import 'package:agencyapp/monitor/metric_meta.dart';
import 'package:agencyapp/monitor/monitor_card.dart';
import 'package:agencyapp/monitor/resource_monitor.dart';
import 'package:agencyapp/monitor/resource_monitor_panel.dart';
import 'package:agencyapp/monitor/system_metrics.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// T011 — the [ResourceMonitorPanel] (contracts/ui.md "Panel"/"Cards"):
/// driven by a real [ResourceMonitor] fed a canned [SystemMetrics] sequence,
/// the body renders one card per **enabled** metric in display order and the
/// cards advance as new samples are recorded. An all-zero-GPU sample renders
/// the whole panel — GPU card neutral, all non-GPU cards normal, zero visible
/// errors (FR-007, SC-005).
void main() {
  late SharedPreferences prefs;
  late ResourceMonitor monitor;

  SystemMetrics sample(double cpu) => SystemMetrics(
        cpuUsage: cpu,
        ramUsage: 60.0,
        ramTotal: 16.0,
        ramUsed: 9.6,
        networkIn: 200.0,
        networkOut: 50.0,
        gpuUsage: 42.0,
        gpuMemoryHas: 24.0,
        gpuMemoryUsed: 3.0,
        gpuTemp: 55.0,
      );

  setUp(() async {
    SharedPreferences.setMockInitialValues(const {});
    prefs = await SharedPreferences.getInstance();
    monitor = ResourceMonitor(prefs);
  });

  tearDown(() => monitor.dispose());

  Future<void> pumpPanel(WidgetTester tester) async {
    await tester.pumpWidget(
      MaterialApp(
        theme: themeDataFor(ThemeMode.dark),
        home: Scaffold(
          body: Stack(children: [ResourceMonitorPanel(monitor: monitor)]),
        ),
      ),
    );
  }

  List<String> cardTitles(WidgetTester tester) => tester
      .widgetList<MonitorCard>(find.byType(MonitorCard))
      .map((c) => c.title)
      .toList();

  /// The panel's own [Card]: it is built before any monitor-card [Card],
  /// so it is the first [Card] in tree order.
  Card panelCard(WidgetTester tester) =>
      tester.widgetList<Card>(find.byType(Card)).first;

  Size panelSize(WidgetTester tester) {
    final panel = panelCard(tester);
    return tester.getSize(find.byWidgetPredicate((w) => identical(w, panel)));
  }

  /// The vertical margin the panel [Card] actually renders with, resolved
  /// the same way [Card] does: its own `margin`, else the theme's
  /// `cardTheme.margin`, else the 4.0 default. The panel sets none of the
  /// first two, so this is the framework default (8.0 total).
  double panelMarginVertical(WidgetTester tester) {
    final panel = panelCard(tester);
    final ctx = tester.element(find.byWidgetPredicate((w) => identical(w, panel)));
    final themeMargin = Theme.of(ctx).cardTheme.margin;
    final effective =
        panel.margin ?? themeMargin ?? const EdgeInsets.all(4.0);
    return effective.vertical;
  }

  LineChart chartOf(WidgetTester tester, String title) {
    final card = find.descendant(
      of: find.byWidgetPredicate((w) => w is MonitorCard && w.title == title),
      matching: find.byType(LineChart),
    );
    return tester.widget<LineChart>(card);
  }

  group('ResourceMonitorPanel', () {
    testWidgets('shows the header and no cards before the first sample',
        (tester) async {
      await pumpPanel(tester);
      expect(find.text('Resource Monitor'), findsOneWidget);
      expect(find.byType(MonitorCard), findsNothing);
      expect(find.text('waiting for first sample…'), findsOneWidget);
    });

    testWidgets('one card per enabled metric, in display order', (tester) async {
      await pumpPanel(tester);
      monitor.recordSample(sample(10));
      await tester.pump();

      // Display order is CPU, GPU, Net Down, Net Up, RAM — filtered to the
      // first-launch enabled set {cpu, ram, gpu}.
      expect(cardTitles(tester), ['CPU Usage', 'GPU Usage', 'RAM Usage']);
      expect(find.text('Network Down'), findsNothing);
      expect(find.text('Network Up'), findsNothing);
    });

    testWidgets('the cards advance as new samples are recorded', (tester) async {
      await pumpPanel(tester);
      monitor.recordSample(sample(10));
      await tester.pump();
      expect(find.text('10.0'), findsOneWidget);
      expect(chartOf(tester, 'CPU Usage').data.lineBarsData.first.spots.length, 1);

      monitor.recordSample(sample(35));
      await tester.pump();

      // The CPU value reflects the latest sample; the oldest dropped out of
      // the previous value.
      expect(find.text('35.0'), findsOneWidget);
      expect(find.text('10.0'), findsNothing);
      expect(monitor.histories[MetricKey.cpu]!.length, 2);

      final spots = chartOf(tester, 'CPU Usage').data.lineBarsData.first.spots;
      expect(spots.length, 2);
      expect(spots.last.y, 35.0);
      expect(spots.last.x, 1);
    });

    testWidgets(
        'toggling a metric off removes its card and the body reflows '
        'without layout breakage (US3-AS2)', (tester) async {
      await pumpPanel(tester);
      monitor.recordSample(sample(10));
      await tester.pump();
      expect(cardTitles(tester), ['CPU Usage', 'GPU Usage', 'RAM Usage']);

      monitor.setMetricVisible(MetricKey.ram, false);
      await tester.pump();
      expect(tester.takeException(), isNull,
          reason: 'the reflow must not break the layout');
      expect(cardTitles(tester), ['CPU Usage', 'GPU Usage']);
      expect(find.text('RAM Usage'), findsNothing);
      // The remaining cards keep rendering live values.
      expect(find.text('10.0'), findsOneWidget);

      monitor.setMetricVisible(MetricKey.ram, true);
      await tester.pump();
      expect(cardTitles(tester), ['CPU Usage', 'GPU Usage', 'RAM Usage']);
    });

    testWidgets(
        'all metrics off leaves an empty body with the header controls '
        'still reachable (US3-AS5)', (tester) async {
      await pumpPanel(tester);
      monitor.recordSample(sample(10));
      await tester.pump();

      for (final key in MetricKey.values) {
        monitor.setMetricVisible(key, false);
      }
      await tester.pump();

      expect(tester.takeException(), isNull);
      expect(find.byType(MonitorCard), findsNothing,
          reason: 'zero cards and no error when every metric is off');
      expect(find.text('waiting for first sample…'), findsNothing,
          reason: 'a sample exists; the body is simply empty');
      // The header and its controls stay reachable.
      expect(find.text('Resource Monitor'), findsOneWidget);
      expect(find.byTooltip('Configure metrics'), findsOneWidget);
    });

    testWidgets(
        'an all-zero-GPU sample renders the whole panel — GPU card neutral, '
        'non-GPU cards normal, zero visible errors (FR-007, SC-005)',
        (tester) async {
      await pumpPanel(tester);
      monitor.recordSample(SystemMetrics.empty());
      await tester.pump();

      expect(tester.takeException(), isNull);
      expect(find.text('Resource Monitor'), findsOneWidget);
      expect(cardTitles(tester), ['CPU Usage', 'GPU Usage', 'RAM Usage']);
      // The GPU card renders the neutral value with a zero/blank sub-value.
      expect(find.text('0.0GB / 0.0GB • 0°C'), findsOneWidget);
      // The non-GPU cards render normally (zero values, no errors).
      expect(find.text('0.0GB / 0.0GB'), findsOneWidget);
      expect(find.text('0.0'), findsWidgets);
      expect(tester.takeException(), isNull);
    });

    group('collapse/expand (US4)', () {
      testWidgets(
          'collapsing hides the card body, leaving the compact header '
          '(title + metrics control + collapse control) (FR-005, SC-003)',
          (tester) async {
        await pumpPanel(tester);
        monitor.recordSample(sample(10));
        await tester.pump();
        expect(cardTitles(tester), ['CPU Usage', 'GPU Usage', 'RAM Usage']);

        await tester.tap(find.byTooltip('Collapse resource monitor'));
        await tester.pump();

        expect(tester.takeException(), isNull);
        expect(find.byType(MonitorCard), findsNothing);
        expect(find.text('waiting for first sample…'), findsNothing);
        // The compact header keeps the title, the metrics control, and the
        // (now expand) collapse control.
        expect(find.text('Resource Monitor'), findsOneWidget);
        expect(find.byTooltip('Configure metrics'), findsOneWidget);
        expect(find.byTooltip('Expand resource monitor'), findsOneWidget);
      });

      testWidgets(
          'expanding restores the full cards with current values '
          '(FR-005, SC-003)', (tester) async {
        await pumpPanel(tester);
        monitor.recordSample(sample(10));
        await tester.pump();

        await tester.tap(find.byTooltip('Collapse resource monitor'));
        await tester.pump();
        expect(find.byType(MonitorCard), findsNothing);

        await tester.tap(find.byTooltip('Expand resource monitor'));
        await tester.pump();

        expect(cardTitles(tester), ['CPU Usage', 'GPU Usage', 'RAM Usage']);
        expect(find.text('10.0'), findsOneWidget);
        expect(find.byTooltip('Collapse resource monitor'), findsOneWidget);
      });

      testWidgets(
          'collapsed is per-session: default expanded and never persisted',
          (tester) async {
        expect(monitor.collapsed, isFalse,
            reason: 'a fresh controller is expanded by default');

        monitor.setCollapsed(true);
        expect(monitor.collapsed, isTrue);
        expect(prefs.getKeys().where((k) => k.contains('collapsed')), isEmpty,
            reason: 'the collapsed state must not write any pref key');

        final restored = ResourceMonitor(prefs);
        addTearDown(restored.dispose);
        expect(restored.collapsed, isFalse,
            reason: 'a fresh controller over the same prefs is expanded');
      });

      testWidgets(
          'the collapsed state survives a topbar hide/show cycle — the '
          'controller keeps it while the panel widget is unmounted (FR-005)',
          (tester) async {
        await pumpPanel(tester);
        monitor.recordSample(sample(10));
        await tester.pump();

        await tester.tap(find.byTooltip('Collapse resource monitor'));
        await tester.pump();
        expect(find.byType(MonitorCard), findsNothing);

        // Topbar off: the panel widget unmounts, the controller lives on.
        await tester.pumpWidget(
          MaterialApp(
            theme: themeDataFor(ThemeMode.dark),
            home: const Scaffold(),
          ),
        );
        expect(monitor.collapsed, isTrue);

        // Topbar on: a fresh widget over the same controller.
        await pumpPanel(tester);

        expect(find.byType(MonitorCard), findsNothing,
            reason: 'the panel re-shows in its collapsed state');
        expect(find.text('Resource Monitor'), findsOneWidget);
        expect(find.byTooltip('Expand resource monitor'), findsOneWidget);
      });
    });

    group('panel sizing (height fits its cards)', () {
      testWidgets(
          'the panel is 250 wide and as tall as its cards — not the '
          'whole canvas (SC-003)', (tester) async {
        await pumpPanel(tester);
        monitor.recordSample(sample(10));
        await tester.pump();

        final size = panelSize(tester);
        final header = tester.getSize(find.byType(Row).first);
        final cardRows =
            tester.widgetList<MonitorCard>(find.byType(MonitorCard)).length;

        expect(size.width, 250);
        // Content is the header row, the 12px gap, and one 150px row per
        // enabled metric (the 8px between-card gap is inside each row). The
        // panel pads it 12px all around, and the Card's rendered margin
        // (resolved from the widget/theme, not assumed) wraps the whole thing.
        final content = header.height + 12 + 150 * cardRows;
        expect(size.height, content + 2 * 12 + panelMarginVertical(tester));
      });

      testWidgets(
          'collapsing shrinks the panel to the header row only, like the '
          'classic legend (FR-005, SC-003)', (tester) async {
        await pumpPanel(tester);
        monitor.recordSample(sample(10));
        await tester.pump();

        await tester.tap(find.byTooltip('Collapse resource monitor'));
        await tester.pump();

        expect(find.byType(MonitorCard), findsNothing);
        final size = panelSize(tester);
        final header = tester.getSize(find.byType(Row).first);
        expect(size.width, 250);
        // Just the header row, its 12px panel padding, and the Card's rendered
        // margin — no cards, no canvas fill (the classic-legend pattern).
        expect(size.height, header.height + 2 * 12 + panelMarginVertical(tester));
      });
    });
  });
}
