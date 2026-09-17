import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../catalog/workflow_def.dart';
import '../../theme/abyss_palette.dart';
import 'canvas_scene.dart';
import 'scene_layout.dart';
import 'state_encodings.dart';

const double kNodeRadius = 16;
const double kColWidth = 170;
const double kRowHeight = 120;
const double kWorldPadding = 90;

/// The run_id-null environment event types (contracts/ui.md "Environment
/// strip"): vram/model lifecycle frames become decorative motes (T048).
const List<String> envFrameTypes = [
  'vram_threshold_exceeded',
  'vram_freed',
  'model_offloaded',
  'model_reloaded',
];

/// A decorative mote: one run_id-null vram/model lifecycle frame (T048).
/// Never state-bearing — all geometry derives from (seed, bornAt, clock, size).
class EnvMote {
  const EnvMote({required this.seed, required this.bornAt, required this.kind});

  /// Stable integer seed (the frame's `seq`); positions derive from it.
  final int seed;

  /// Ticker clock (s) at which the mote was observed.
  final double bornAt;

  /// The source frame's event type (one of [envFrameTypes]).
  final String kind;
}

const double kMoteLife = 14.0;
const double kMoteFadeIn = 1.5;
const double kMoteBandTop = 12.0;
const double kMoteBandHeight = 26.0;
const double kMoteBobAmp = 3.0;
const double kMoteDriftSpeed = 7.0;
const double kMoteMinRadius = 1.2;
const double kMoteRadiusSpread = 1.4;

/// Pure mote geometry: (seed, bornAt, clock, size) → offset/radius/fade.
/// Returns `null` when the mote is unborn or expired. No per-frame
/// randomness (FR-016) — drift/bob are animation (time-varying) inputs,
/// excluded from scene equality like the other pulse effects.
({Offset offset, double radius, double fade})? envMoteGeometry(
  EnvMote mote, {
  required double clock,
  required Size size,
}) {
  final age = clock - mote.bornAt;
  if (age < 0 || age >= kMoteLife) return null;
  final fade = math.min(age / kMoteFadeIn, 1.0) * (1.0 - age / kMoteLife);
  final span = size.width + 40;
  final x = (_moteFrac(mote.seed, 1) * size.width + clock * kMoteDriftSpeed) % span - 20;
  final y = kMoteBandTop +
      _moteFrac(mote.seed, 2) * kMoteBandHeight +
      kMoteBobAmp * math.sin(clock * 0.6 + _moteFrac(mote.seed, 3) * math.pi * 2);
  final radius = (kMoteMinRadius + _moteFrac(mote.seed, 4) * kMoteRadiusSpread) *
      (mote.kind.startsWith('vram_') ? 1.25 : 1.0);
  return (offset: Offset(x, y), radius: radius, fade: fade);
}

/// Deterministic pseudo-fraction in [0, 1) from (seed, salt) — no RNG.
double _moteFrac(int seed, int salt) {
  var h = (seed + salt * 0x9E3779B9) & 0xFFFFFFFF;
  h = (h ^ (h >> 13)) & 0xFFFFFFFF;
  h = (h * 0x5BF330F1) & 0xFFFFFFFF;
  h = (h ^ (h >> 15)) & 0xFFFFFFFF;
  return (h & 0xFFFFFF) / 0x1000000;
}

Offset worldPositionOf(NodePosition pos) {
  return Offset(
    kWorldPadding + pos.col * kColWidth,
    kWorldPadding + pos.row * kRowHeight,
  );
}

Rect layoutBounds(SceneLayout layout) {
  var maxX = 0.0;
  var maxY = 0.0;
  for (final pos in layout.positions.values) {
    final wp = worldPositionOf(pos);
    if (wp.dx > maxX) maxX = wp.dx;
    if (wp.dy > maxY) maxY = wp.dy;
  }
  return Rect.fromLTWH(0, 0, maxX + kNodeRadius * 2, maxY + kNodeRadius * 2);
}

Offset strandControl(Offset a, Offset b) {
  final dx = b.dx - a.dx;
  final dy = b.dy - a.dy;
  final len = math.sqrt(dx * dx + dy * dy);
  if (len < 1) return a;
  final bend = math.min(44, len * 0.18);
  final nx = -dy / len;
  final ny = dx / len;
  final mx = (a.dx + b.dx) / 2;
  final my = (a.dy + b.dy) / 2;
  return Offset(mx + nx * bend, my + ny * bend);
}

Offset strandPoint(Offset a, Offset b, double t) {
  final c = strandControl(a, b);
  final u = 1 - t;
  return Offset(
    u * u * a.dx + 2 * u * t * c.dx + t * t * b.dx,
    u * u * a.dy + 2 * u * t * c.dy + t * t * b.dy,
  );
}

class ScenePainter extends CustomPainter {
  ScenePainter({
    required this.scene,
    this._definition,
    AbyssPalette? palette,
    this._transform,
    this._clock = 0,
    Map<int, double>? pulses,
    List<EnvMote>? envMotes,
  })  : _palette = palette ?? darkAbyss(),
        _pulses = pulses ?? const {},
        _envMotes = envMotes ?? const [];

  final CanvasScene scene;
  final WorkflowDefinition? _definition;
  final AbyssPalette _palette;
  final Matrix4? _transform;
  final double _clock;
  final Map<int, double> _pulses;
  final List<EnvMote> _envMotes;

  AbyssPalette get palette => _palette;

  @override
  void paint(Canvas canvas, Size size) {
    final Paint seaPaint = Paint();
    final gradient = LinearGradient(
      begin: Alignment.topCenter,
      end: Alignment.bottomCenter,
      colors: [
        Color.lerp(_palette.background, _palette.surface, 0.45)!,
        _palette.background,
      ],
    ).createShader(Rect.fromLTWH(0, 0, size.width, size.height));
    seaPaint.shader = gradient;
    canvas.drawRect(Rect.fromLTWH(0, 0, size.width, size.height), seaPaint);

    _paintEnvStrip(canvas, size);

    final Matrix4? xf = _transform;
    if (xf != null) canvas.save();
    if (xf != null) canvas.transform(xf.storage);

    _paintStrands(canvas);
    _paintNodes(canvas);
    _paintLabels(canvas);

    if (xf != null) canvas.restore();
  }

  void _paintStrands(Canvas canvas) {
    final edges = _definition?.edges ?? const <EdgeDef>[];
    for (var i = 0; i < edges.length; i++) {
      final e = edges[i];
      final fromPos = scene.layout.positionOf(e.fromId);
      final toPos = scene.layout.positionOf(e.toId);
      if (fromPos.col < 0 || toPos.col < 0) continue;
      final a = worldPositionOf(fromPos);
      final b = worldPositionOf(toPos);
      final c = strandControl(a, b);

      final glowPaint = Paint()
        ..color = _palette.accent.withValues(alpha: 0.05)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 7
        ..strokeCap = StrokeCap.round;
      canvas.drawPath(
        Path()..quadraticBezierTo(c.dx, c.dy, b.dx, b.dy),
        glowPaint,
      );

      final strokePaint = Paint()
        ..color = _palette.accent.withValues(alpha: 0.22)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.5
        ..strokeCap = StrokeCap.round;
      canvas.drawPath(
        Path()..quadraticBezierTo(c.dx, c.dy, b.dx, b.dy),
        strokePaint,
      );

      _paintPulse(canvas, a, b, i);
    }
  }

  void _paintPulse(Canvas canvas, Offset a, Offset b, int edgeIdx) {
    final start = _pulses[edgeIdx];
    if (start == null) return;
    final elapsed = _clock - start;
    if (elapsed < 0 || elapsed >= 0.6) return;
    final t = elapsed / 0.6;
    final p = strandPoint(a, b, t);
    canvas.drawCircle(p, 3, Paint()..color = _palette.accent);
  }

  void _paintNodes(Canvas canvas) {
    for (final e in scene.encodingsByNode.entries) {
      final pos = scene.layout.positionOf(e.key);
      if (pos.col < 0) continue;
      final center = worldPositionOf(pos);
      final encoding = e.value;
      final color = _palette.colorFor(scene.statusOf(e.key)!);
      final effect = _motionEffect(encoding.motion, _clock, e.key.hashCode);
      final radius = kNodeRadius * effect.scale;

      switch (encoding.shape) {
        case LightShape.hollowRing:
          canvas.drawCircle(
            center,
            radius,
            Paint()
              ..color = color.withValues(alpha: effect.alpha)
              ..style = PaintingStyle.stroke
              ..strokeWidth = 2,
          );
        case LightShape.thinHollowRing:
          canvas.drawCircle(
            center,
            radius,
            Paint()
              ..color = color.withValues(alpha: effect.alpha)
              ..style = PaintingStyle.stroke
              ..strokeWidth = 1,
          );
        case LightShape.filledDisc:
          _drawGlow(
            canvas,
            center,
            radius * 2.5,
            color.withValues(alpha: effect.alpha * 0.3),
          );
          canvas.drawCircle(
            center,
            radius,
            Paint()..color = color.withValues(alpha: effect.alpha),
          );
        case LightShape.filledDiscSmall:
          canvas.drawCircle(
            center,
            radius * 0.65,
            Paint()..color = color.withValues(alpha: effect.alpha),
          );
        case LightShape.dashedDisc:
          _drawDashedCircle(
            canvas,
            center,
            radius,
            color.withValues(alpha: effect.alpha),
            4,
            3,
          );
        case LightShape.haloDisc:
          canvas.drawCircle(
            center,
            radius * 1.5,
            Paint()
              ..color = color.withValues(alpha: effect.alpha * 0.25)
              ..style = PaintingStyle.stroke
              ..strokeWidth = 1,
          );
          canvas.drawCircle(
            center,
            radius * 0.7,
            Paint()..color = color.withValues(alpha: effect.alpha),
          );
        case LightShape.crossDisc:
          final cr = radius * 0.65;
          canvas.drawCircle(
            center,
            cr,
            Paint()..color = color.withValues(alpha: effect.alpha * 0.3),
          );
          final p1 = Offset(center.dx - cr * 0.7, center.dy - cr * 0.7);
          final p2 = Offset(center.dx + cr * 0.7, center.dy + cr * 0.7);
          final p3 = Offset(center.dx + cr * 0.7, center.dy - cr * 0.7);
          final p4 = Offset(center.dx - cr * 0.7, center.dy + cr * 0.7);
          canvas.drawLine(
            p1,
            p2,
            Paint()
              ..color = color.withValues(alpha: effect.alpha)
              ..strokeWidth = 2,
          );
          canvas.drawLine(
            p3,
            p4,
            Paint()
              ..color = color.withValues(alpha: effect.alpha)
              ..strokeWidth = 2,
          );
      }
    }
  }

  void _paintLabels(Canvas canvas) {
    for (final e in scene.encodingsByNode.entries) {
      final pos = scene.layout.positionOf(e.key);
      if (pos.col < 0) continue;
      final center = worldPositionOf(pos);
      final tp = TextPainter(
        text: TextSpan(
          text: e.key,
          style: TextStyle(
            color: _palette.text.withValues(alpha: 0.7),
            fontSize: 12,
          ),
        ),
        textDirection: TextDirection.ltr,
      )..layout();
      tp.paint(
        canvas,
        Offset(center.dx - tp.width / 2, center.dy + kNodeRadius + 6),
      );
      tp.dispose();
    }
  }

  /// The optional environment strip (T048): faint drifting motes for the
  /// run_id-null vram/model frames. Decorative, never state-bearing; painted
  /// in screen space behind the world (it does not pan/zoom with the scene).
  void _paintEnvStrip(Canvas canvas, Size size) {
    final base = _palette.mode == ThemeMode.light ? 0.32 : 0.22;
    for (final m in _envMotes) {
      final g = envMoteGeometry(m, clock: _clock, size: size);
      if (g == null) continue;
      canvas.drawCircle(
        g.offset,
        g.radius,
        Paint()..color = _palette.accent.withValues(alpha: base * g.fade),
      );
    }
  }

  void _drawGlow(Canvas canvas, Offset c, double r, Color color) {
    final paint = Paint()
      ..shader = RadialGradient(
        colors: [color.withValues(alpha: 0.5), color.withValues(alpha: 0)],
        stops: const [0.3, 1],
      ).createShader(Rect.fromCircle(center: c, radius: r));
    canvas.drawCircle(c, r, paint);
  }

  void _drawDashedCircle(
    Canvas canvas,
    Offset c,
    double r,
    Color color,
    double dashLen,
    double gapLen,
  ) {
    final path = Path();
    const totalAngle = math.pi * 2;
    var angle = 0.0;
    while (angle < totalAngle) {
      final a1 = angle;
      angle += dashLen / r;
      final a2 = math.min(angle, totalAngle);
      angle += gapLen / r;
      path.addArc(
        Rect.fromCircle(center: c, radius: r),
        a1 * 180 / math.pi,
        (a2 - a1) * 180 / math.pi,
      );
    }
    canvas.drawPath(
      path,
      Paint()
        ..color = color
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2,
    );
  }

  _MotionEffect _motionEffect(PulseMotion motion, double clock, int seed) {
    final s = clock;
    switch (motion) {
      case PulseMotion.breathing:
        return _MotionEffect(scale: 0.7 + 0.3 * math.sin(s * 2), alpha: 1);
      case PulseMotion.pulsingGlow:
        return _MotionEffect(
          scale: 1 + 0.15 * math.sin(s * 3),
          alpha: 0.7 + 0.3 * math.sin(s * 3),
        );
      case PulseMotion.jitterFlicker:
        final jx = 2 * math.sin(s * 11 + seed);
        final jy = 2 * math.cos(s * 13 + seed);
        return _MotionEffect(
          scale: 1,
          alpha: 0.5 + 0.5 * math.sin(s * 7).abs(),
          jitterX: jx,
          jitterY: jy,
        );
      case PulseMotion.steadyGlow:
        return const _MotionEffect(scale: 1, alpha: 1);
      case PulseMotion.haloShimmer:
        return _MotionEffect(
          scale: 1 + 0.05 * math.sin(s * 2),
          alpha: 0.6 + 0.4 * math.sin(s * 1.5).abs(),
        );
      case PulseMotion.hardFlicker:
        final v = math.sin(s * 9) * math.cos(s * 13);
        return _MotionEffect(scale: 1, alpha: v > 0 ? 1 : 0.2);
      case PulseMotion.still:
        return const _MotionEffect(scale: 1, alpha: 1);
    }
  }

  @override
  bool shouldRepaint(covariant ScenePainter oldDelegate) => true;
}

class _MotionEffect {
  const _MotionEffect({
    required this.scale,
    required this.alpha,
    this.jitterX = 0,
    this.jitterY = 0,
  });
  final double scale;
  final double alpha;
  final double jitterX;
  final double jitterY;
}
