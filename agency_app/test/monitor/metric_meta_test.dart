import 'package:agencyapp/monitor/metric_meta.dart';
import 'package:agencyapp/monitor/system_metrics.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// T005 — per-metric identity (data-model.md §2, contracts/ui.md "Cards"):
/// card title, accent color, unit, value/sub-value formatting, trend-value
/// extractor, the alphabetical display order, and network unit scaling
/// (`>1024` ⇒ `MB/s`).
void main() {
  final sample = SystemMetrics(
    cpuUsage: 42.5,
    ramUsage: 60.2,
    ramTotal: 16.0,
    ramUsed: 9.6,
    networkIn: 2048.0,
    networkOut: 512.0,
    gpuUsage: 33.0,
    gpuMemoryHas: 24.0,
    gpuMemoryUsed: 3.0,
    gpuTemp: 55.0,
  );

  group('MetricKey identity', () {
    test('cpu: title, accent, unit, value, no sub-value, trend', () {
      expect(MetricKey.cpu.title, 'CPU Usage');
      expect(MetricKey.cpu.accent, Colors.blueAccent);
      expect(MetricKey.cpu.unitFor(sample), '%');
      expect(MetricKey.cpu.formatValue(sample), '42.5');
      expect(MetricKey.cpu.subValue(sample), isNull);
      expect(MetricKey.cpu.trendValue(sample), 42.5);
    });

    test('ram: title, accent, unit, value, sub-value, trend', () {
      expect(MetricKey.ram.title, 'RAM Usage');
      expect(MetricKey.ram.accent, Colors.purpleAccent);
      expect(MetricKey.ram.unitFor(sample), '%');
      expect(MetricKey.ram.formatValue(sample), '60.2');
      expect(MetricKey.ram.subValue(sample), '9.6GB / 16.0GB');
      expect(MetricKey.ram.trendValue(sample), 60.2);
    });

    test('gpu: title, accent, unit, value, sub-value, trend', () {
      expect(MetricKey.gpu.title, 'GPU Usage');
      expect(MetricKey.gpu.accent, Colors.greenAccent);
      expect(MetricKey.gpu.unitFor(sample), '%');
      expect(MetricKey.gpu.formatValue(sample), '33.0');
      expect(MetricKey.gpu.subValue(sample), '3.0GB / 24.0GB • 55°C');
      expect(MetricKey.gpu.trendValue(sample), 33.0);
    });

    test('netDown/netUp: titles, accents, no sub-value, trend extractors', () {
      expect(MetricKey.netDown.title, 'Network Down');
      expect(MetricKey.netDown.accent, Colors.orangeAccent);
      expect(MetricKey.netDown.subValue(sample), isNull);
      expect(MetricKey.netDown.trendValue(sample), 2048.0);

      expect(MetricKey.netUp.title, 'Network Up');
      expect(MetricKey.netUp.accent, Colors.redAccent);
      expect(MetricKey.netUp.subValue(sample), isNull);
      expect(MetricKey.netUp.trendValue(sample), 512.0);
    });

    test('network unit scaling: value > 1024 renders MB/s, else KB/s', () {
      // 2048 KB/s => 2.0 MB/s
      expect(MetricKey.netDown.unitFor(sample), 'MB/s');
      expect(MetricKey.netDown.formatValue(sample), '2.0');
      // 512 KB/s stays KB/s
      expect(MetricKey.netUp.unitFor(sample), 'KB/s');
      expect(MetricKey.netUp.formatValue(sample), '512.0');
    });

    test('network scaling boundary: 1024 stays KB/s, 1025 switches', () {
      final at = SystemMetrics(
        cpuUsage: 0,
        ramUsage: 0,
        ramTotal: 0,
        ramUsed: 0,
        networkIn: 1024.0,
        networkOut: 1025.0,
        gpuUsage: 0,
        gpuMemoryHas: 0,
        gpuMemoryUsed: 0,
        gpuTemp: 0,
      );

      expect(MetricKey.netDown.unitFor(at), 'KB/s');
      expect(MetricKey.netDown.formatValue(at), '1024.0');
      expect(MetricKey.netUp.unitFor(at), 'MB/s');
      expect(MetricKey.netUp.formatValue(at), '1.0');
    });
  });

  group('maxY rules', () {
    test('percent metrics use a fixed maxY of 100', () {
      expect(MetricKey.cpu.maxY(const [0.0, 99.5]), 100);
      expect(MetricKey.ram.maxY(const []), 100);
      expect(MetricKey.gpu.maxY(const [50.0]), 100);
    });

    test('network maxY = max(window) * 1.2 + 10 (ported)', () {
      expect(MetricKey.netDown.maxY(const [100.0, 500.0, 200.0]), 610.0);
      expect(MetricKey.netUp.maxY(const [75.0]), 100.0);
      expect(MetricKey.netDown.maxY(const []), 10.0);
    });
  });

  group('display order', () {
    test('alphabetical: CPU, GPU, Network Down, Network Up, RAM', () {
      expect(
        kMetricDisplayOrder,
        [
          MetricKey.cpu,
          MetricKey.gpu,
          MetricKey.netDown,
          MetricKey.netUp,
          MetricKey.ram,
        ],
      );
    });
  });
}
