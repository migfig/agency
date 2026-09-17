import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../../api/models.dart';
import '../../catalog/workflow_def.dart';
import '../../run/run_state.dart';
import 'classic_encodings.dart';
import 'classic_layout.dart';
import 'classic_palette.dart';
import 'classic_scene.dart';

/// Classic card corner radius (world px).
const double kCardRadius = 8;

/// The step-change response window in ticker seconds (FR-013): a card
/// highlight plus a marker traveling every edge that touches the node.
const double kPulseDuration = 0.6;

/// The selection halo's offset from the card border (world px).
const double kSelectionGlow = 5;

/// Cycles per second of the running-state pulsing border (FR-008).
const double kRunningPulseHz = 0.5;

/// Ticker seconds per full comet lap around a running card's border.
const double kCometPeriod = 1.6;

/// The fraction of the card perimeter covered by the comet's fading tail.
const double kCometSweep = 0.35;

/// Stroked sub-segments of the comet tail (alpha-ramp granularity).
const int kCometSegments = 24;

/// In-flight progress of a pulse that started at [start] (ticker seconds),
/// or `null` once the ~600 ms window has passed.
double? pulseProgress(double clock, double start) {
  final p = (clock - start) / kPulseDuration;
  if (p < 0) return null;
  return p >= 1 ? null : p;
}

/// The control point of the edge's quadratic Bézier: the midpoint of the two
/// anchors along the flow axis, other axis from the source (research R5).
/// Stored on the edge at layout time so it matches the active topology.
Offset classicEdgeControl(ClassicEdge e) => e.control;

/// Point at [t] along the edge's quadratic Bézier, traveling
/// `sourceAnchor → targetAnchor` (markers travel with the flow, FR-013).
Offset classicEdgePointAt(ClassicEdge e, double t) {
  final c = classicEdgeControl(e);
  final s = e.sourceAnchor;
  final o = 1 - t;
  return Offset(
    o * o * s.dx + 2 * o * t * c.dx + t * t * e.targetAnchor.dx,
    o * o * s.dy + 2 * o * t * c.dy + t * t * e.targetAnchor.dy,
  );
}

/// The in-flight progress of the marker riding [e] given the nodes currently
/// pulsing [nodePulses] (node id → pulse start), or `null` when the edge
/// touches no pulsing node. A source-side pulse travels source → target; a
/// target-side pulse is mirrored so the marker also arrives at the target.
double? pulseForEdge(
    ClassicEdge e, Map<String, double> nodePulses, double clock) {
  final a = nodePulses[e.fromId];
  if (a != null) {
    final p = pulseProgress(clock, a);
    if (p != null) return p;
  }
  final b = nodePulses[e.toId];
  if (b != null) {
    final p = pulseProgress(clock, b);
    if (p != null) return 1 - p;
  }
  return null;
}

/// Deterministic pulsing-border alpha for a running card (FR-008), in
/// (0, 1]. The border is the comet's orbit track, so it stays quiet and
/// lets the orbiting comet read on top of it. No randomness, no wall
/// clock — the ticker clock is the only input.
double runningBorderAlpha(double clock) =>
    0.35 + 0.30 * (math.sin(clock * kRunningPulseHz * 2 * math.pi) + 1) / 2;

/// Normalized 0..1 head position of the running card's comet at [clock]
/// (ticker seconds): the comet orbits the card border clockwise, one lap
/// per [kCometPeriod]. No randomness, no wall clock.
double cometHeadPhase(double clock) => (clock / kCometPeriod) % 1.0;

/// Perimeter of the rounded-rect border of a [w]×[h] rect with corner
/// radius [r] (clamped to half the smaller side so degenerate rects stay
/// valid): four straight sides plus four quarter arcs.
double rrectPerimeter(double w, double h, double r) {
  final rr = math.min(r, math.min(w, h) / 2);
  return 2 * (w + h) - 8 * rr + 2 * math.pi * rr;
}

/// The point [s] (arc length) along the border of a [w]×[h] rounded rect
/// with corner radius [r], in local card coordinates (origin at the card's
/// top-left corner), walking clockwise from the top edge's first corner.
/// [s] wraps modulo the perimeter, so the walk is a closed loop.
Offset rrectPointAt(double s, double w, double h, double r) {
  final rr = math.min(r, math.min(w, h) / 2);
  final p = rrectPerimeter(w, h, rr);
  var t = s % p;
  if (t < 0) t += p;
  final top = w - 2 * rr;
  final side = h - 2 * rr;
  final corner = math.pi * rr / 2;
  if (t <= top) {
    // Top edge, from its first corner (r, 0) rightward.
    return Offset(rr + t, 0);
  }
  t -= top;
  if (t <= corner) {
    // Top-right corner: center (w-r, r), sweeping from up (-90°) to right.
    final a = -math.pi / 2 + t / rr;
    return Offset(w - rr + rr * math.cos(a), rr + rr * math.sin(a));
  }
  t -= corner;
  if (t <= side) {
    return Offset(w, rr + t);
  }
  t -= side;
  if (t <= corner) {
    // Bottom-right corner: center (w-r, h-r), sweeping from right to down.
    final a = t / rr;
    return Offset(w - rr + rr * math.cos(a), h - rr + rr * math.sin(a));
  }
  t -= corner;
  if (t <= top) {
    return Offset(w - rr - t, h);
  }
  t -= top;
  if (t <= corner) {
    // Bottom-left corner: center (r, h-r), sweeping from down to left.
    final a = math.pi / 2 + t / rr;
    return Offset(rr + rr * math.cos(a), h - rr + rr * math.sin(a));
  }
  t -= corner;
  if (t <= side) {
    return Offset(0, h - rr - t);
  }
  t -= side;
  // Top-left corner: center (r, r), sweeping from left to up.
  final a = math.pi + t / rr;
  return Offset(rr + rr * math.cos(a), rr + rr * math.sin(a));
}

/// Header label for an awaiting_retry card (FR-010): `retry in Ns` while the
/// countdown is positive, `retrying…` once [nextRetryAt] has passed (never a
/// negative countdown), `retry…` when the instant or [now] is unknown.
String retryCountdownLabel(DateTime? nextRetryAt, DateTime? now) {
  if (nextRetryAt == null || now == null) return 'retry…';
  final ms = nextRetryAt.difference(now).inMilliseconds;
  if (ms <= 0) return 'retrying…';
  return 'retry in ${ms ~/ 1000}s';
}

/// The classic canvas painter (US1 static channels + US2 animated channels):
/// flat background, quadratic Bézier edges with filled triangular arrowheads
/// at the target border, and rounded node cards (type icon + ellipsized id +
/// state label header, model body row, terminal metrics row, persistent `fb`
/// chip, accent entry border + tag, dimmed skipped cards).
///
/// US2 channels, all driven by the injected frame parameters (never by
/// wall clock or randomness — FR-020): a ~600 ms card highlight + traveling
/// edge markers on step changes (FR-013, [nodePulses]/[clock]), the
/// running-state pulsing border with its orbiting comet (FR-008, [clock]),
/// and the ticking `retry in Ns` countdown (FR-010, [now]).
class ClassicPainter extends CustomPainter {
  ClassicPainter({
    required this.scene,
    ClassicPalette? palette,
    this.transform,
    this._clock = 0,
    Map<String, double>? nodePulses,
    this._now,
    this._selectedNodeId,
  })  : _palette = palette ?? darkClassic(),
        _nodePulses = nodePulses ?? const {};

  final ClassicScene scene;
  final Matrix4? transform;
  final ClassicPalette _palette;
  final double _clock;
  final Map<String, double> _nodePulses;
  final DateTime? _now;

  /// The inspected node's id (accent halo + thick border on its card), or
  /// null when no node is selected.
  final String? _selectedNodeId;

  /// The effective palette (defaults to the dark palette).
  ClassicPalette get palette => _palette;

  /// Ticker seconds this frame was painted with (FR-008/FR-013).
  double get clock => _clock;

  /// Node id → pulse start (ticker seconds) this frame (FR-013).
  Map<String, double> get nodePulses => _nodePulses;

  /// Wall-clock used for the retry countdown label (FR-010).
  DateTime? get now => _now;

  /// The inspected node's id this frame, or null (no selection).
  String? get selectedNodeId => _selectedNodeId;

  @override
  void paint(Canvas canvas, Size size) {
    canvas.drawRect(
      Rect.fromLTWH(0, 0, size.width, size.height),
      Paint()..color = _palette.background,
    );

    final xf = transform;
    if (xf != null) {
      canvas.save();
      canvas.transform(xf.storage);
    }
    _paintEdges(canvas);
    _paintCards(canvas);
    if (xf != null) canvas.restore();
  }

  void _paintEdges(Canvas canvas) {
    final stroke = Paint()
      ..color = _palette.edge
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.5
      ..strokeCap = StrokeCap.round;
    final inFlight = <MapEntry<ClassicEdge, double>>[];
    for (final e in scene.layout.edges) {
      final a = e.sourceAnchor;
      final b = e.targetAnchor;
      // Quadratic Bézier: leave the source horizontally, arrive at the
      // target's left border; control at the horizontal midpoint keeps the
      // curve on the source row (research R5).
      final control = classicEdgeControl(e);
      canvas.drawPath(
        Path()
          ..moveTo(a.dx, a.dy)
          ..quadraticBezierTo(control.dx, control.dy, b.dx, b.dy),
        stroke,
      );
      _paintArrowhead(canvas, b, b - control);
      final p = pulseForEdge(e, _nodePulses, _clock);
      if (p != null) inFlight.add(MapEntry(e, p));
    }
    // FR-013: edges touching a just-changed node re-stroke in accent with a
    // marker dot traveling source → target while the pulse is in flight.
    for (final entry in inFlight) {
      final e = entry.key;
      final control = classicEdgeControl(e);
      canvas.drawPath(
        Path()
          ..moveTo(e.sourceAnchor.dx, e.sourceAnchor.dy)
          ..quadraticBezierTo(
              control.dx, control.dy, e.targetAnchor.dx, e.targetAnchor.dy),
        Paint()
          ..color = _palette.accent
          ..style = PaintingStyle.stroke
          ..strokeWidth = 2.5
          ..strokeCap = StrokeCap.round,
      );
      canvas.drawCircle(classicEdgePointAt(e, entry.value), 3.0,
          Paint()..color = _palette.accent);
    }
  }

  /// Filled triangular arrowhead with its tip at [tip], oriented along the
  /// curve's end tangent [tangent].
  void _paintArrowhead(Canvas canvas, Offset tip, Offset tangent) {
    final len = math.sqrt(tangent.dx * tangent.dx + tangent.dy * tangent.dy);
    if (len < 1) return;
    final dir = Offset(tangent.dx / len, tangent.dy / len);
    const size = 9.0;
    const half = 4.5;
    final base = Offset(tip.dx - dir.dx * size, tip.dy - dir.dy * size);
    final perp = Offset(-dir.dy, dir.dx);
    canvas.drawPath(
      Path()
        ..moveTo(tip.dx, tip.dy)
        ..lineTo(base.dx + perp.dx * half, base.dy + perp.dy * half)
        ..lineTo(base.dx - perp.dx * half, base.dy - perp.dy * half)
        ..close(),
      Paint()..color = _palette.edge,
    );
  }

  void _paintCards(Canvas canvas) {
    for (final e in scene.state.nodes.entries) {
      final node = e.value;
      final card = scene.layout.cardOf(e.key);
      if (card == null) continue;
      final encoding = scene.encodings[e.key]!;
      final dim = node.status == NodeStatus.skipped;
      var color = _palette.colorFor(node.status);
      // FR-008: running cards pulse their border on the ticker clock.
      if (node.status == NodeStatus.running) {
        color = color.withValues(
            alpha: (color.a * runningBorderAlpha(_clock)).clamp(0.0, 1.0));
      }
      final rect = card.rect;
      final rrect =
          RRect.fromRectAndRadius(rect, const Radius.circular(kCardRadius));
      final selected = node.nodeId == _selectedNodeId;
      if (selected) {
        // The inspected node: a soft accent halo ring around the card (the
        // palette accent keeps it legible in both themes and desaturates
        // with the scene while stale).
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            rect.deflate(-kSelectionGlow),
            Radius.circular(kCardRadius + kSelectionGlow),
          ),
          Paint()
            ..color = _tint(_palette.accent, dim, 0.3)
            ..style = PaintingStyle.stroke
            ..strokeWidth = 4.0,
        );
      }

      canvas.drawRRect(
        rrect,
        Paint()
          ..color = _tint(
            selected
                ? Color.lerp(_palette.cardFill, _palette.accent, 0.16)!
                : _palette.cardFill,
            dim,
          ),
      );
      // FR-013: a card whose (status, model, fallback) identity just changed
      // gets an accent highlight border while its pulse is in flight.
      final start = _nodePulses[node.nodeId];
      final inFlight =
          start != null && pulseProgress(_clock, start) != null;
      Color border = card.isEntry ? _palette.accent : color;
      var borderWidth = card.isEntry ? 2.0 : 1.5;
      if (inFlight) {
        border = _palette.accent;
        borderWidth = 2.5;
      }
      if (selected) {
        border = _palette.accent;
        borderWidth = 2.5;
      }
      canvas.drawRRect(
        rrect,
        Paint()
          ..color = _tint(border, dim)
          ..style = PaintingStyle.stroke
          ..strokeWidth = borderWidth,
      );
      // FR-008: a running card orbits a comet around its border (the
      // pulsing border above is the orbit track).
      if (node.status == NodeStatus.running) {
        _paintComet(canvas, rrect, _palette.colorFor(NodeStatus.running), _clock);
      }

      _paintHeader(canvas, card, node, encoding, dim);
      _paintBody(canvas, card, node, dim);
      if (card.isEntry) _paintEntryTag(canvas, card, dim);
    }
  }

  /// The running card's orbiting comet (FR-008): a bright head with a soft
  /// glow and a fading tail sweeping the card's border clockwise, one lap
  /// per [kCometPeriod] of ticker time. Painted on top of the pulsing border
  /// (the orbit track). The head leads, the tail trails [kCometSweep] of the
  /// perimeter behind it; the tail's alpha ramps 0 → 1 toward the head so
  /// the comet dissolves into the track.
  void _paintComet(Canvas canvas, RRect rrect, Color color, double clock) {
    final w = rrect.width;
    final h = rrect.height;
    final radius = rrect.tlRadiusX;
    final p = rrectPerimeter(w, h, radius);
    final headS = cometHeadPhase(clock) * p;
    final tailS = kCometSweep * p;
    // pts[0] is the tail end, pts[kCometSegments] the head (tail → head).
    final pts = List<Offset>.generate(kCometSegments + 1, (i) {
      final s = headS - tailS * (kCometSegments - i) / kCometSegments;
      final pt = rrectPointAt(s, w, h, radius);
      return Offset(rrect.left + pt.dx, rrect.top + pt.dy);
    });
    for (var i = 0; i < kCometSegments; i++) {
      final t = (i + 1) / kCometSegments; // 0 at the tail, 1 at the head
      canvas.drawLine(
        pts[i],
        pts[i + 1],
        Paint()
          ..color = color.withValues(alpha: math.pow(t, 1.6).toDouble())
          ..strokeWidth = 2.5
          ..strokeCap = StrokeCap.round,
      );
    }
    final head = pts[kCometSegments];
    canvas.drawCircle(head, 6.0, Paint()..color = color.withValues(alpha: 0.35));
    canvas.drawCircle(head, 2.75, Paint()..color = color);
  }

  /// Header row: type icon + ellipsized node id + (fb chip) + state label.
  void _paintHeader(
    Canvas canvas,
    CardRect card,
    SNodeState node,
    ClassicStateEncoding encoding,
    bool dim,
  ) {
    final rect = card.rect;
    final text = _palette.text;
    final type = scene.definition.nodeById(node.nodeId)?.type ??
        NodeType.tryParse(node.type);

    // FR-010: awaiting_retry shows the live ticking countdown, degraded to
    // a static placeholder when the instant or clock is unknown.
    final label = node.status == NodeStatus.awaitingRetry
        ? retryCountdownLabel(node.nextRetryAt, now)
        : encoding.label;
    final labelTp = TextPainter(
      text: TextSpan(
        text: label,
        style: TextStyle(
            fontSize: 11, color: _tint(_palette.colorFor(node.status), dim)),
      ),
      textDirection: TextDirection.ltr,
    )..layout();
    final hasFbChip = node.status == NodeStatus.completedFallback;
    final chipW = hasFbChip ? 20.0 : 0.0;
    final labelX = rect.right - 10 - labelTp.width - chipW;

    if (type != null) {
      final icon = typeMarkerFor(type).icon;
      final iconTp = TextPainter(
        text: TextSpan(
          text: String.fromCharCode(icon.codePoint),
          style: TextStyle(
              fontFamily: 'MaterialIcons',
              fontSize: 14,
              color: _tint(text, dim, 0.85)),
        ),
        textDirection: TextDirection.ltr,
      )..layout();
      iconTp.paint(canvas, Offset(rect.left + 10, rect.top + 8));
      iconTp.dispose();
    }

    final idX = rect.left + 10 + 14 + 6;
    final idMaxW = labelX - 6 - idX;
    if (idMaxW > 20) {
      final idTp = TextPainter(
        text: TextSpan(
          text: node.nodeId,
          style: TextStyle(
              fontSize: 12,
              fontWeight: FontWeight.w600,
              color: _tint(text, dim)),
        ),
        textDirection: TextDirection.ltr,
        maxLines: 1,
        ellipsis: '…',
      );
      idTp.layout(maxWidth: idMaxW);
      idTp.paint(canvas, Offset(idX, rect.top + 7));
      idTp.dispose();
    }

    if (hasFbChip) {
      final chip =
          Rect.fromLTWH(labelX - chipW - 4, rect.top + 8, chipW, 13);
      final chipRRect = RRect.fromRectAndRadius(chip, const Radius.circular(6));
      canvas.drawRRect(
        chipRRect,
        Paint()..color = _tint(_palette.accent.withValues(alpha: 0.25), dim),
      );
      canvas.drawRRect(
        chipRRect,
        Paint()
          ..color = _tint(_palette.accent, dim)
          ..style = PaintingStyle.stroke,
      );
      final fbTp = TextPainter(
        text: TextSpan(
            text: 'fb', style: TextStyle(fontSize: 8, color: _tint(text, dim))),
        textDirection: TextDirection.ltr,
      )..layout();
      fbTp.paint(canvas,
          Offset(chip.center.dx - fbTp.width / 2, chip.center.dy - fbTp.height / 2));
      fbTp.dispose();
    }

    labelTp.paint(canvas, Offset(labelX, rect.top + 7));
    labelTp.dispose();
  }

  /// Body rows: model name (or dash) and, for terminal states, the metrics
  /// line (tokens · duration · attempt n/m, plus the truncated
  /// failure/skip reason — FR-008/FR-012).
  void _paintBody(Canvas canvas, CardRect card, SNodeState node, bool dim) {
    final rect = card.rect;
    final bodyTp = TextPainter(
      text: TextSpan(
        text: node.model ?? '—',
        style: TextStyle(fontSize: 11, color: _tint(_palette.text, dim, 0.75)),
      ),
      textDirection: TextDirection.ltr,
      maxLines: 1,
      ellipsis: '…',
    );
    bodyTp.layout(maxWidth: rect.width - 20);
    bodyTp.paint(canvas, Offset(rect.left + 10, rect.top + 30));
    bodyTp.dispose();

    final metrics = classicMetricsLine(node);
    if (metrics != null) {
      final mTp = TextPainter(
        text: TextSpan(
          text: metrics,
          style: TextStyle(fontSize: 10, color: _tint(_palette.text, dim, 0.6)),
        ),
        textDirection: TextDirection.ltr,
        maxLines: 1,
        ellipsis: '…',
      );
      mTp.layout(maxWidth: rect.width - 20);
      mTp.paint(canvas, Offset(rect.left + 10, rect.top + 48));
      mTp.dispose();
    }
  }

  /// The small "entry" tag above an entry card (FR-005).
  void _paintEntryTag(Canvas canvas, CardRect card, bool dim) {
    final tp = TextPainter(
      text: TextSpan(
        text: 'entry',
        style: TextStyle(
            fontSize: 10,
            fontWeight: FontWeight.w600,
            color: _tint(_palette.accent, dim)),
      ),
      textDirection: TextDirection.ltr,
    )..layout();
    tp.paint(canvas, Offset(card.rect.center.dx - tp.width / 2, card.rect.top - 16));
    tp.dispose();
  }

  /// Alpha-tint a color: `factor` for normal text hierarchy, times 0.55
  /// for dimmed (skipped) cards.
  Color _tint(Color c, bool dim, [double factor = 1.0]) => c.withValues(
      alpha: (c.a * factor * (dim ? 0.55 : 1.0)).clamp(0.0, 1.0));

  @override
  bool shouldRepaint(covariant ClassicPainter oldDelegate) =>
      oldDelegate.scene != scene ||
      oldDelegate.palette != palette ||
      oldDelegate.transform != transform ||
      oldDelegate._clock != _clock ||
      oldDelegate._now != _now ||
      oldDelegate._selectedNodeId != _selectedNodeId ||
      !mapEquals(oldDelegate._nodePulses, _nodePulses);
}

/// The terminal card's metrics line (FR-012/FR-014): `tokens · duration ·
/// attempt n/m`, prefixed with the state's terminal glyph (✓ for
/// completed/completed_fallback, ✕ for failed — the non-colour marker
/// channel, FR-009) and a truncated failure/skip reason where relevant.
/// Returns null for non-terminal states (no metrics line).
String? classicMetricsLine(SNodeState node) {
  final s = node.status;
  if (s != NodeStatus.completed &&
      s != NodeStatus.completedFallback &&
      s != NodeStatus.failed &&
      s != NodeStatus.skipped) {
    return null;
  }
  final parts = <String>[];
  final tokens = node.tokens;
  final duration = node.durationSeconds;
  if (tokens != null) parts.add('$tokens tok');
  if (duration != null) {
    parts.add('${duration.toStringAsFixed(1)} s');
  }
  parts.add('attempt ${node.attempt}/${node.maxAttempts}');
  var line = parts.join(' · ');
  final reason = s == NodeStatus.failed
      ? node.error
      : (s == NodeStatus.skipped ? node.skipReason : null);
  if (reason != null && reason.trim().isNotEmpty) {
    final r = reason.trim();
    line = '$line · ${r.length > 28 ? '${r.substring(0, 28)}…' : r}';
  }
  switch (s) {
    case NodeStatus.completed:
    case NodeStatus.completedFallback:
      line = '✓ $line';
    case NodeStatus.failed:
      line = '✕ $line';
    default:
      break;
  }
  return line;
}
