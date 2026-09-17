import 'dart:io';

import 'package:agencyapp/monitor/system_metrics.dart';
import 'package:agencyapp/monitor/system_service.dart';
import 'package:flutter_test/flutter_test.dart';

/// T008 — deterministic [SystemService] sampler (research R7): the raw
/// sources are injectable so the tests never touch `/proc`, `nvidia-smi`, or
/// the wall clock, and a manual [SystemService.tick] drives one sample.
void main() {
  const cpuLine = 'cpu  100 0 50 800 0 0 0 0';
  const List<String> meminfoLines = [
    'MemTotal:        16777216 kB',
    'MemAvailable:     8388608 kB',
  ];
  const List<String> netDevLines = [
    'Inter-|   Receive                                                |  Transmit',
    ' face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed',
    '    lo: 1000000    100    0    0    0     0          0         0 1000000    100    0    0    0     0       0          0',
    '  eth0: 2000000    200    0    0    0     0          0         0 3000000    300    0    0    0     0       0          0',
  ];
  const Map<String, double> gpuSample = {
    'usage': 42.0,
    'memoryUsed': 3.5,
    'memoryTotal': 24.0,
    'temp': 61.0,
  };

  SystemService serviceWith({
    Future<List<String>> Function()? cpu,
    Future<List<String>> Function()? mem,
    Future<List<String>> Function()? net,
    Future<Map<String, double>> Function()? gpu,
  }) {
    return SystemService(
      cpuStatLines: cpu ?? () async => const [cpuLine],
      meminfoLines: mem ?? () async => meminfoLines,
      netDevLines: net ?? () async => netDevLines,
      gpu: gpu ?? () async => gpuSample,
    );
  }

  void expectMetricsMatch(SystemMetrics actual, SystemMetrics expected) {
    expect(actual.cpuUsage, expected.cpuUsage);
    expect(actual.ramUsage, expected.ramUsage);
    expect(actual.ramTotal, expected.ramTotal);
    expect(actual.ramUsed, expected.ramUsed);
    expect(actual.networkIn, expected.networkIn);
    expect(actual.networkOut, expected.networkOut);
    expect(actual.gpuUsage, expected.gpuUsage);
    expect(actual.gpuMemoryHas, expected.gpuMemoryHas);
    expect(actual.gpuMemoryUsed, expected.gpuMemoryUsed);
    expect(actual.gpuTemp, expected.gpuTemp);
  }

  group('SystemService (R7 seams)', () {
    test('a manual tick produces a SystemMetrics', () async {
      final service = serviceWith();
      final emitted = service.metricsStream.first;
      final m = await service.tick();
      // CPU: totald = 950, idled = 800 → (950 - 800) / 950 * 100.
      expect(m.cpuUsage, (150 / 950) * 100);
      // RAM: 16 GiB total, 8 GiB available → 50 %, 16.0 GB used-of-16.
      expect(m.ramUsage, 50.0);
      expect(m.ramTotal, 16.0);
      expect(m.ramUsed, 8.0);
      // Network: the first sample has no baseline → 0 (FR-010).
      expect(m.networkIn, 0.0);
      expect(m.networkOut, 0.0);
      // GPU: the injected probe.
      expect(m.gpuUsage, 42.0);
      expect(m.gpuMemoryHas, 24.0);
      expect(m.gpuMemoryUsed, 3.5);
      expect(m.gpuTemp, 61.0);
      final streamed = await emitted;
      expectMetricsMatch(streamed, m);
      service.dispose();
    });

    test('per-metric read failure collapses to that metric neutral value only (FR-007)',
        () async {
      final cpuFail = serviceWith(
        cpu: () async => throw const FileSystemException('no /proc/stat'),
      );
      final m1 = await cpuFail.tick();
      expect(m1.cpuUsage, 0.0);
      expect(m1.ramUsage, 50.0, reason: 'the other metrics are unaffected');
      expect(m1.gpuUsage, 42.0);
      cpuFail.dispose();

      final memFail = serviceWith(
        mem: () async => throw StateError('no meminfo'),
      );
      final m2 = await memFail.tick();
      expect(m2.ramUsage, 0.0);
      expect(m2.ramTotal, 0.0);
      expect(m2.ramUsed, 0.0);
      expect(m2.cpuUsage, (150 / 950) * 100, reason: 'cpu is unaffected');
      memFail.dispose();

      final netFail = serviceWith(
        net: () async => throw StateError('no net dev'),
      );
      final m3 = await netFail.tick();
      expect(m3.networkIn, 0.0);
      expect(m3.networkOut, 0.0);
      expect(m3.ramUsage, 50.0, reason: 'ram is unaffected');
      netFail.dispose();
    });

    test('the first network sample is 0 (FR-010)', () async {
      final service = serviceWith();
      final m = await service.tick();
      expect(m.networkIn, 0.0);
      expect(m.networkOut, 0.0);
      service.dispose();
    });

    test('a GPU-absent sample yields all-zero GPU fields', () async {
      final service = serviceWith(
        gpu: () async => throw ProcessException('nvidia-smi', const []),
      );
      final m = await service.tick();
      expect(m.gpuUsage, 0.0);
      expect(m.gpuMemoryHas, 0.0);
      expect(m.gpuMemoryUsed, 0.0);
      expect(m.gpuTemp, 0.0);
      expect(m.ramUsage, 50.0, reason: 'the other metrics are unaffected');
      service.dispose();
    });
  });
}
