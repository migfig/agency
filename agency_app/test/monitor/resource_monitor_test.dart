import 'package:agencyapp/monitor/metric_meta.dart';
import 'package:agencyapp/monitor/resource_monitor.dart';
import 'package:agencyapp/monitor/system_metrics.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// T009 — the [ResourceMonitor] controller (data-model.md §3): `current`
/// goes null → SystemMetrics, each `histories[key]` caps at 60 (SC-006),
/// `sampleOrdinal` advances, the first-launch `visible` set is exactly
/// {cpu, ram, gpu} (FR-014), the `visible` set round-trips through
/// SharedPreferences (FR-013), and `collapsed` defaults to false and writes
/// no pref key on toggle (per-session only).
void main() {
  late SharedPreferences prefs;
  late ResourceMonitor monitor;

  SystemMetrics sample(double cpu) => SystemMetrics(
        cpuUsage: cpu,
        ramUsage: cpu / 2,
        ramTotal: 16.0,
        ramUsed: 8.0,
        networkIn: cpu * 10,
        networkOut: cpu * 5,
        gpuUsage: cpu / 4,
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

  test('current is null before the first sample, a SystemMetrics after recordSample',
      () {
    expect(monitor.current, isNull);
    monitor.recordSample(sample(10));
    expect(monitor.current, isA<SystemMetrics>());
    expect(monitor.current!.cpuUsage, 10.0);
  });

  test('each histories[key] caps at 60 (SC-006)', () {
    for (var i = 0; i < 70; i++) {
      monitor.recordSample(sample(i.toDouble()));
    }
    for (final key in MetricKey.values) {
      expect(monitor.histories[key]!.length, 60, reason: '$key caps at 60');
    }
    expect(monitor.histories[MetricKey.cpu]!.first, 10.0,
        reason: 'the oldest 10 samples were dropped');
    expect(monitor.histories[MetricKey.cpu]!.last, 69.0);
  });

  test('sampleOrdinal advances on every recorded sample', () {
    expect(monitor.sampleOrdinal, 0);
    monitor.recordSample(sample(1));
    expect(monitor.sampleOrdinal, 1);
    monitor.recordSample(sample(2));
    expect(monitor.sampleOrdinal, 2);
  });

  test('the first-launch visible set is exactly {cpu, ram, gpu} (FR-014)', () {
    expect(monitor.visible, {
      MetricKey.cpu: true,
      MetricKey.ram: true,
      MetricKey.gpu: true,
      MetricKey.netDown: false,
      MetricKey.netUp: false,
    });
  });

  test('the visible set round-trips through SharedPreferences (FR-013)', () {
    monitor.setMetricVisible(MetricKey.netDown, true);
    monitor.setMetricVisible(MetricKey.gpu, false);
    expect(prefs.getBool('monitor_metric_net_down'), true);
    expect(prefs.getBool('monitor_metric_gpu'), false);

    final restored = ResourceMonitor(prefs);
    addTearDown(restored.dispose);
    expect(restored.visible, {
      MetricKey.cpu: true,
      MetricKey.ram: true,
      MetricKey.gpu: false,
      MetricKey.netDown: true,
      MetricKey.netUp: false,
    });
  });

  test(
      'setMetricVisible updates visible, persists the pref, notifies, and a '
      'fresh controller restores the exact set (FR-012/FR-013, SC-008)', () {
    var notified = 0;
    monitor.addListener(() => notified++);

    monitor.setMetricVisible(MetricKey.netDown, true);
    expect(monitor.visible[MetricKey.netDown], isTrue);
    expect(prefs.getBool('monitor_metric_net_down'), isTrue);
    expect(notified, 1, reason: 'the panel is notified so the body reflows');

    monitor.setMetricVisible(MetricKey.cpu, false);
    expect(monitor.visible[MetricKey.cpu], isFalse);
    expect(prefs.getBool('monitor_metric_cpu'), isFalse);
    expect(notified, 2);

    final restored = ResourceMonitor(prefs);
    addTearDown(restored.dispose);
    expect(restored.visible, {
      MetricKey.cpu: false,
      MetricKey.ram: true,
      MetricKey.gpu: true,
      MetricKey.netDown: true,
      MetricKey.netUp: false,
    });
  });

  test('collapsed defaults to false and writes no pref key on toggle', () {
    expect(monitor.collapsed, isFalse);
    monitor.setCollapsed(true);
    expect(monitor.collapsed, isTrue);
    expect(prefs.getKeys(), isEmpty, reason: 'collapsed is per-session only');
    monitor.setCollapsed(false);
    expect(monitor.collapsed, isFalse);
    expect(prefs.getKeys(), isEmpty);
  });
}
