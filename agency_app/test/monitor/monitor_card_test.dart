import 'package:agencyapp/monitor/metric_meta.dart';
import 'package:agencyapp/monitor/monitor_card.dart';
import 'package:agencyapp/monitor/system_metrics.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// T010 — the theme-aware [MonitorCard] (FR-002/FR-011, research R2/R8/R9):
/// it builds the title row, the large value + unit, the optional sub-value,
/// and a [LineChart] trend without animation, in both themes. An all-zero
/// sample (GPU-absent, FR-007) renders the neutral value without throwing.
void main() {
  Future<void> pumpCard(
    WidgetTester tester,
    ThemeMode mode, {
    required String title,
    required String value,
    String? subValue,
    required String unit,
    List<FlSpot> spots = const [],
    Color color = Colors.blueAccent,
    double maxY = 100,
  }) {
    return tester.pumpWidget(
      MaterialApp(
        theme: themeDataFor(mode),
        home: Scaffold(
          body: Center(
            child: SizedBox(
              height: 150,
              width: 280,
              child: MonitorCard(
                title: title,
                value: value,
                subValue: subValue,
                unit: unit,
                spots: spots,
                color: color,
                maxY: maxY,
              ),
            ),
          ),
        ),
      ),
    );
  }

  group('MonitorCard', () {
    testWidgets(
        'builds the title row, large value + unit, sub-value, and the LineChart',
        (tester) async {
      await pumpCard(
        tester,
        ThemeMode.dark,
        title: 'CPU Usage',
        value: '42.5',
        unit: '%',
        subValue: null,
        spots: const [FlSpot(1, 10), FlSpot(2, 30), FlSpot(3, 20)],
      );
      expect(find.text('CPU Usage'), findsOneWidget);
      expect(find.text('42.5'), findsOneWidget);
      expect(find.text('%'), findsOneWidget);
      expect(find.byType(LineChart), findsOneWidget);
    });

    testWidgets('the trend chart has no animation (duration zero, R8)',
        (tester) async {
      await pumpCard(
        tester,
        ThemeMode.dark,
        title: 'CPU Usage',
        value: '1.0',
        unit: '%',
        spots: const [FlSpot(0, 1), FlSpot(1, 2)],
      );
      final chart = tester.widget<LineChart>(find.byType(LineChart));
      expect(chart.duration, Duration.zero);
    });

    testWidgets('the trend line renders in the card accent color',
        (tester) async {
      await pumpCard(
        tester,
        ThemeMode.dark,
        title: 'GPU Usage',
        value: '33.0',
        unit: '%',
        color: Colors.greenAccent,
        spots: const [FlSpot(0, 5), FlSpot(1, 33)],
      );
      final chart = tester.widget<LineChart>(find.byType(LineChart));
      // The line must carry the same color as the card's accent dot —
      // without it fl_chart falls back to a shared default (cyan) and
      // every card's trend renders identically.
      expect(chart.data.lineBarsData.first.color, Colors.greenAccent);
    });

    testWidgets('renders the sub-value only when provided', (tester) async {
      await pumpCard(
        tester,
        ThemeMode.dark,
        title: 'RAM Usage',
        value: '50.0',
        unit: '%',
        subValue: '8.0GB / 16.0GB',
      );
      expect(find.text('8.0GB / 16.0GB'), findsOneWidget);

      await pumpCard(
        tester,
        ThemeMode.dark,
        title: 'CPU Usage',
        value: '50.0',
        unit: '%',
      );
      expect(find.text('8.0GB / 16.0GB'), findsNothing);
    });

    for (final mode in [ThemeMode.dark, ThemeMode.light]) {
      testWidgets(
          'renders in the ${mode.name} theme without errors (FR-011)',
          (tester) async {
        await pumpCard(
          tester,
          mode,
          title: 'GPU Usage',
          value: '33.0',
          unit: '%',
          subValue: '3.0GB / 24.0GB • 55°C',
          color: Colors.greenAccent,
          spots: const [FlSpot(0, 5), FlSpot(1, 33)],
        );
        expect(tester.takeException(), isNull);
        expect(find.text('GPU Usage'), findsOneWidget);
        expect(find.text('33.0'), findsOneWidget);
        expect(find.text('3.0GB / 24.0GB • 55°C'), findsOneWidget);
        expect(find.byType(LineChart), findsOneWidget);
      });
    }

    testWidgets(
        'an all-zero GPU sample renders the neutral value without throwing (FR-007)',
        (tester) async {
      final m = SystemMetrics.empty();
      await pumpCard(
        tester,
        ThemeMode.dark,
        title: MetricKey.gpu.title,
        value: MetricKey.gpu.formatValue(m),
        unit: MetricKey.gpu.unitFor(m),
        subValue: MetricKey.gpu.subValue(m),
        color: MetricKey.gpu.accent,
      );
      expect(tester.takeException(), isNull);
      expect(find.text('0.0'), findsOneWidget);
      expect(find.text('0.0GB / 0.0GB • 0°C'), findsOneWidget);
    });
  });
}
