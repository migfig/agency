import 'dart:async';
import 'dart:io';

import 'system_metrics.dart';

/// One point-in-time machine reading, produced once per second.
///
/// Ported from the system-monitor reference service with two seams (research
/// R7): the raw sources are injectable so tests never touch `/proc`,
/// `nvidia-smi`, or the wall clock, and a manual [tick] drives one sample.
/// A per-metric read failure collapses to that metric's neutral value only
/// (FR-007); the first network sample is 0 (FR-010).
class SystemService {
  SystemService({
    Future<List<String>> Function()? cpuStatLines,
    Future<List<String>> Function()? meminfoLines,
    Future<List<String>> Function()? netDevLines,
    Future<Map<String, double>> Function()? gpu,
  })  : _cpuStatLines = cpuStatLines ?? _readCpuStat,
        _meminfoLines = meminfoLines ?? _readMeminfo,
        _netDevLines = netDevLines ?? _readNetDev,
        _gpu = gpu ?? _readGpuProbe;

  final Future<List<String>> Function() _cpuStatLines;
  final Future<List<String>> Function() _meminfoLines;
  final Future<List<String>> Function() _netDevLines;
  final Future<Map<String, double>> Function() _gpu;

  Timer? _timer;
  final StreamController<SystemMetrics> _metricsController =
      StreamController.broadcast();
  Stream<SystemMetrics> get metricsStream => _metricsController.stream;

  bool _disposed = false;

  // CPU calculation history
  int _prevUser = 0;
  int _prevNice = 0;
  int _prevSystem = 0;
  int _prevIdle = 0;
  int _prevIowait = 0;
  int _prevIrq = 0;
  int _prevSoftirq = 0;
  int _prevSteal = 0;

  // Network calculation history
  int _prevRxBytes = 0;
  int _prevTxBytes = 0;
  DateTime _lastNetworkCheck = DateTime.now();

  /// Starts the 1 Hz production timer; tests call [tick] directly instead.
  void startMonitoring({Duration interval = const Duration(seconds: 1)}) {
    _timer?.cancel();
    _timer = Timer.periodic(interval, (_) => tick());
  }

  void stopMonitoring() {
    _timer?.cancel();
    _timer = null;
  }

  /// One sample: read every metric source, fold each failure to its neutral
  /// value, publish, and return the built [SystemMetrics].
  Future<SystemMetrics> tick() async {
    final cpu = await _readCpuUsage();
    final ram = await _readRamUsage();
    final net = await _readNetworkUsage();
    final gpu = await _readGpuUsage();

    final metrics = SystemMetrics(
      cpuUsage: cpu,
      ramUsage: ram['percent']!,
      ramTotal: ram['total']!,
      ramUsed: ram['used']!,
      networkIn: net['in']!,
      networkOut: net['out']!,
      gpuUsage: gpu['usage']!,
      gpuMemoryHas: gpu['memoryTotal']!,
      gpuMemoryUsed: gpu['memoryUsed']!,
      gpuTemp: gpu['temp']!,
    );
    _metricsController.add(metrics);
    return metrics;
  }

  Future<double> _readCpuUsage() async {
    try {
      final lines = await _cpuStatLines();
      if (lines.isEmpty) return 0.0;

      final parts = lines.first.split(RegExp(r'\s+'));
      // user, nice, system, idle, iowait, irq, softirq, steal
      if (parts.length < 9) return 0.0;

      final user = int.parse(parts[1]);
      final nice = int.parse(parts[2]);
      final system = int.parse(parts[3]);
      final idle = int.parse(parts[4]);
      final iowait = int.parse(parts[5]);
      final irq = int.parse(parts[6]);
      final softirq = int.parse(parts[7]);
      final steal = int.parse(parts[8]);

      // Calculate total time
      final total = user + nice + system + idle + iowait + irq + softirq + steal;

      // Calculate differences from last reading
      final totald = total - (_prevUser + _prevNice + _prevSystem + _prevIdle +
          _prevIowait + _prevIrq + _prevSoftirq + _prevSteal);
      final idled = idle + iowait - (_prevIdle + _prevIowait);

      // Update previous values
      _prevUser = user;
      _prevNice = nice;
      _prevSystem = system;
      _prevIdle = idle;
      _prevIowait = iowait;
      _prevIrq = irq;
      _prevSoftirq = softirq;
      _prevSteal = steal;

      // Calculate CPU usage
      if (totald == 0) return 0.0;
      return (totald - idled) / totald * 100;
    } catch (e) {
      return 0.0;
    }
  }

  Future<Map<String, double>> _readRamUsage() async {
    try {
      final lines = await _meminfoLines();

      double? total, available;
      for (final line in lines) {
        if (line.startsWith('MemTotal:')) {
          final parts = line.split(RegExp(r'\s+'));
          if (parts.length >= 2) total = double.parse(parts[1]) / 1024.0; // MB
        } else if (line.startsWith('MemAvailable:')) {
          final parts = line.split(RegExp(r'\s+'));
          if (parts.length >= 2) available = double.parse(parts[1]) / 1024.0;
        }
      }

      if (total == null || available == null) {
        return {'percent': 0, 'total': 0, 'used': 0};
      }

      final used = total - available;
      final percent = (used / total) * 100;

      // Convert to GB
      return {
        'percent': percent,
        'total': total / 1024.0,
        'used': used / 1024.0,
      };
    } catch (e) {
      return {'percent': 0, 'total': 0, 'used': 0};
    }
  }

  Future<Map<String, double>> _readNetworkUsage() async {
    try {
      final lines = await _netDevLines();

      int totalRxBytes = 0;
      int totalTxBytes = 0;

      for (final line in lines) {
        // Skip header lines
        if (line.contains('Inter-|') || line.contains('face |')) continue;

        // Parse network interface data
        final parts = line.trim().split(':');
        if (parts.length < 2) continue;

        final iface = parts[0].trim();
        if (iface.isEmpty) continue;

        final values = parts[1].split(RegExp(r'\s+'));
        if (values.length >= 10) {
          try {
            // Receive: bytes is first value
            totalRxBytes += int.parse(values[0]);
            // Transmit: bytes is first value after receive data (10 values in
            // receive section, then transmit starts)
            totalTxBytes += int.parse(values[9]);
          } catch (e) {
            // Skip malformed lines
          }
        }
      }

      final now = DateTime.now();
      final elapsed = now.difference(_lastNetworkCheck).inMilliseconds / 1000;
      _lastNetworkCheck = now;

      double inRate = 0;
      double outRate = 0;

      // Only calculate rates if we have previous data
      if (_prevRxBytes > 0 || _prevTxBytes > 0) {
        inRate = (totalRxBytes - _prevRxBytes) / 1024.0 / elapsed; // KB/s
        outRate = (totalTxBytes - _prevTxBytes) / 1024.0 / elapsed; // KB/s
      }

      // Update previous values
      _prevRxBytes = totalRxBytes;
      _prevTxBytes = totalTxBytes;

      // Ensure rates don't go negative
      if (inRate < 0) inRate = 0;
      if (outRate < 0) outRate = 0;

      return {'in': inRate, 'out': outRate};
    } catch (e) {
      return {'in': 0, 'out': 0};
    }
  }

  Future<Map<String, double>> _readGpuUsage() async {
    try {
      return await _gpu();
    } catch (e) {
      return {'usage': 0, 'memoryUsed': 0, 'memoryTotal': 0, 'temp': 0};
    }
  }

  static Future<List<String>> _readCpuStat() => File('/proc/stat').readAsLines();

  static Future<List<String>> _readMeminfo() =>
      File('/proc/meminfo').readAsLines();

  static Future<List<String>> _readNetDev() =>
      File('/proc/net/dev').readAsLines();

  static Future<Map<String, double>> _readGpuProbe() async {
    try {
      final result = await Process.run('nvidia-smi', [
        '--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu',
        '--format=csv,noheader,nounits',
      ]);
      if (result.exitCode == 0) {
        final parts = result.stdout.toString().trim().split(',');
        if (parts.length >= 4) {
          return {
            'usage': double.parse(parts[0].trim()),
            'memoryUsed': double.parse(parts[1].trim()) / 1024.0,
            'memoryTotal': double.parse(parts[2].trim()) / 1024.0,
            'temp': double.parse(parts[3].trim()),
          };
        }
      }
      return {'usage': 0, 'memoryUsed': 0, 'memoryTotal': 0, 'temp': 0};
    } catch (e) {
      return {'usage': 0, 'memoryUsed': 0, 'memoryTotal': 0, 'temp': 0};
    }
  }

  void dispose() {
    if (_disposed) return;
    _disposed = true;
    stopMonitoring();
    _metricsController.close();
  }
}
