/// One point-in-time reading of the machine (spec "Usage sample").
///
/// Ported from the system-monitor reference model
/// (`system-monitor/lib/services/system_service.dart`); the reference field
/// name `gpuMemoryHas` is kept.
class SystemMetrics {
  final double cpuUsage;
  final double ramUsage;
  final double ramTotal;
  final double ramUsed;
  final double networkIn; // KB/s
  final double networkOut; // KB/s
  final double gpuUsage;
  final double gpuMemoryHas;
  final double gpuMemoryUsed;
  final double gpuTemp;

  const SystemMetrics({
    required this.cpuUsage,
    required this.ramUsage,
    required this.ramTotal,
    required this.ramUsed,
    required this.networkIn,
    required this.networkOut,
    required this.gpuUsage,
    required this.gpuMemoryHas,
    required this.gpuMemoryUsed,
    required this.gpuTemp,
  });

  /// The all-zero neutral sample (FR-007/FR-010 baseline).
  factory SystemMetrics.empty() {
    return SystemMetrics(
      cpuUsage: 0,
      ramUsage: 0,
      ramTotal: 0,
      ramUsed: 0,
      networkIn: 0,
      networkOut: 0,
      gpuUsage: 0,
      gpuMemoryHas: 0,
      gpuMemoryUsed: 0,
      gpuTemp: 0,
    );
  }
}
