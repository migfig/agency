import 'package:agencyapp/monitor/system_metrics.dart';
import 'package:flutter_test/flutter_test.dart';

/// T004 — the ported usage-sample model (data-model.md §1): the full field
/// set and the all-zero `SystemMetrics.empty()` neutral sample (FR-007/FR-010
/// baseline).
void main() {
  group('SystemMetrics', () {
    test('exposes the full ported field set', () {
      final m = SystemMetrics(
        cpuUsage: 12.5,
        ramUsage: 60.0,
        ramTotal: 16.0,
        ramUsed: 9.6,
        networkIn: 2048.0,
        networkOut: 512.0,
        gpuUsage: 33.0,
        gpuMemoryHas: 24.0,
        gpuMemoryUsed: 3.0,
        gpuTemp: 55.0,
      );

      expect(m.cpuUsage, 12.5);
      expect(m.ramUsage, 60.0);
      expect(m.ramTotal, 16.0);
      expect(m.ramUsed, 9.6);
      expect(m.networkIn, 2048.0);
      expect(m.networkOut, 512.0);
      expect(m.gpuUsage, 33.0);
      expect(m.gpuMemoryHas, 24.0);
      expect(m.gpuMemoryUsed, 3.0);
      expect(m.gpuTemp, 55.0);
    });

    test('empty() yields an all-zero neutral sample', () {
      final m = SystemMetrics.empty();

      expect(m.cpuUsage, 0);
      expect(m.ramUsage, 0);
      expect(m.ramTotal, 0);
      expect(m.ramUsed, 0);
      expect(m.networkIn, 0);
      expect(m.networkOut, 0);
      expect(m.gpuUsage, 0);
      expect(m.gpuMemoryHas, 0);
      expect(m.gpuMemoryUsed, 0);
      expect(m.gpuTemp, 0);
    });
  });
}
