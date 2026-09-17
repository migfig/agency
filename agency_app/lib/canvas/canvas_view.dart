import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import '../../api/models.dart';
import '../../catalog/workflow_def.dart';
import '../../run/run_store.dart';
import '../../theme/abyss_palette.dart';
import 'canvas_keyboard.dart';
import 'canvas_scene.dart';
import 'scene_painter.dart';
import 'zoom_controls.dart';

/// The Abyss canvas view: the state encodings of the run's DAG, with
/// pan/zoom gestures (drag to pan, wheel/pinch to zoom under the cursor,
/// clamped 0.25–4), the standard keyboard gestures (arrows pan, +/− zoom,
/// 0 resets to 100%, F zooms to fit), the bottom-right [ZoomControls] bar,
/// and the optional environment strip (T048).
class CanvasView extends StatefulWidget {
  const CanvasView({
    super.key,
    required this.store,
    this.definition,
    this.onInspect,
    this.events,
  });

  final RunStore store;
  final WorkflowDefinition? definition;
  final ValueChanged<String>? onInspect;

  /// The broadcast event stream; run_id-null vram/model frames feed the
  /// optional environment strip (T048). Null ⇒ no motes (it may be absent).
  final Stream<EventFrame>? events;

  @override
  State<CanvasView> createState() => CanvasViewState();
}

class CanvasViewState extends State<CanvasView> with TickerProviderStateMixin {
  CanvasScene? scene;
  Offset _pan = Offset.zero;
  double _scale = 1;
  double _baseScale = 1;
  Offset? _baseWorld;
  bool _zoomToFitDone = false;
  bool _stale = false;
  Ticker? _ticker;
  double _clock = 0;
  Map<int, double> _pulses = {};
  CanvasScene? _prevScene;
  final List<EnvMote> _motes = [];
  StreamSubscription<EventFrame>? _frameSub;
  late final FocusNode _focusNode = FocusNode();

  /// The current view transform (exposed for tests).
  Offset get pan => _pan;
  double get scale => _scale;

  /// The motes currently drifting in the environment strip (T048).
  /// Decorative only — never state-bearing.
  List<EnvMote> get motes => List.unmodifiable(_motes);

  @override
  void initState() {
    super.initState();
    widget.store.addListener(_onStoreChanged);
    _frameSub = widget.events?.listen(_onEnvFrame);
    _ticker = createTicker((value) {
      // Stale (disconnected) freezes the clock and halts repaints so
      // animations pause (US5).
      if (_stale) return;
      _clock = value.inMicroseconds / 1e6;
      _motes.removeWhere((m) => _clock - m.bornAt >= kMoteLife);
      setState(() {});
    })..start();
    // The canvas starts focused so the keyboard viewport gestures work out
    // of the box; a canvas tap re-requests focus (see onTapDown).
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) _focusNode.requestFocus();
    });
    _onStoreChanged();
  }

  /// T048: collect run_id-null vram/model lifecycle frames as motes.
  void _onEnvFrame(EventFrame frame) {
    if (frame.runId != null) return;
    if (!envFrameTypes.contains(frame.eventType)) return;
    _motes.add(EnvMote(seed: frame.seq, bornAt: _clock, kind: frame.eventType));
    if (_motes.length > 32) _motes.removeAt(0);
  }

  @override
  void didUpdateWidget(covariant CanvasView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.store != widget.store) {
      oldWidget.store.removeListener(_onStoreChanged);
      widget.store.addListener(_onStoreChanged);
      _pan = Offset.zero;
      _scale = 1;
      _baseScale = 1;
      _baseWorld = null;
      _zoomToFitDone = false;
      _pulses = {};
      _prevScene = null;
      _onStoreChanged();
    }
    if (oldWidget.events != widget.events) {
      _frameSub?.cancel();
      _frameSub = widget.events?.listen(_onEnvFrame);
    }
  }

  @override
  void dispose() {
    _ticker?.stop();
    _frameSub?.cancel();
    widget.store.removeListener(_onStoreChanged);
    _focusNode.dispose();
    super.dispose();
  }

  /// The viewport center in screen space (the keyboard zoom anchor).
  Offset _viewportCenter() {
    final sz = context.size;
    return sz == null ? Offset.zero : Offset(sz.width / 2, sz.height / 2);
  }

  /// The view's keyboard viewport gestures: arrows pan, +/− zoom, 0 resets,
  /// F zooms to fit (see `handleViewportKeys` for the full map).
  KeyEventResult _onKey(FocusNode node, KeyEvent event) =>
      handleViewportKeys(
        node,
        event,
        panBy: (d) {
          setState(() => _pan = _pan + d);
        },
        zoomBy: (f) => _zoomAt(_viewportCenter(), f),
        resetZoom: () => _setScaleAt(_viewportCenter(), 1.0),
        zoomToFit: () {
          final sz = context.size;
          if (sz != null) _zoomToFit(sz);
        },
      );

  void _onStoreChanged() {
    _stale = widget.store.state?.isStale ?? false;
    final def = widget.definition;
    if (def == null || widget.store.state == null) return;
    final newScene = buildScene(widget.store.state!, definition: def);
    _registerPulses(newScene);
    setState(() {
      scene = newScene;
      _prevScene = newScene;
    });
    if (!_zoomToFitDone) {
      _zoomToFitDone = true;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted) return;
        final sz = context.size;
        if (sz == null) return;
        _zoomToFit(sz);
      });
    }
  }

  void _registerPulses(CanvasScene newScene) {
    final prev = _prevScene;
    if (prev == null) return;
    final def = widget.definition;
    if (def == null) return;
    for (var ei = 0; ei < def.edges.length; ei++) {
      final edge = def.edges[ei];
      final prevFrom = prev.encodingsByNode[edge.fromId];
      final prevTo = prev.encodingsByNode[edge.toId];
      final newFrom = newScene.encodingsByNode[edge.fromId];
      final newTo = newScene.encodingsByNode[edge.toId];
      if ((newFrom != prevFrom) || (newTo != prevTo)) {
        _pulses[ei] = _clock;
      }
    }
  }

  void _zoomToFit(Size size) {
    final def = widget.definition;
    if (def == null || scene == null) return;
    final bounds = layoutBounds(scene!.layout);
    final scaleX = size.width / bounds.width;
    final scaleY = size.height / bounds.height;
    _scale = math.min(scaleX, scaleY).clamp(0.25, 4.0);
    final cx = (bounds.width * _scale) / 2;
    final cy = (bounds.height * _scale) / 2;
    _pan = Offset(size.width / 2 - cx, size.height / 2 - cy);
    setState(() {});
  }

  void _zoomAt(Offset focal, double factor) {
    _setScaleAt(focal, _scale * factor);
  }

  void _setScaleAt(Offset focal, double target) {
    final worldPt = Offset(
      (focal.dx - _pan.dx) / _scale,
      (focal.dy - _pan.dy) / _scale,
    );
    _scale = target.clamp(0.25, 4.0);
    _pan = Offset(
      focal.dx - worldPt.dx * _scale,
      focal.dy - worldPt.dy * _scale,
    );
    setState(() {});
  }

  void _handleTap(TapDownDetails details) {
    if (scene == null || widget.definition == null) return;
    final sz = context.size;
    if (sz == null) return;
    final local = details.localPosition;
    final world = Offset(
      (local.dx - _pan.dx) / _scale,
      (local.dy - _pan.dy) / _scale,
    );
    double bestDist = kNodeRadius * 1.6;
    String? bestId;
    for (final id in scene!.layout.positions.keys) {
      final pos = scene!.layout.positionOf(id);
      if (pos.col < 0) continue;
      final wp = worldPositionOf(pos);
      final d = (world - wp).distance;
      if (d < bestDist) {
        bestDist = d;
        bestId = id;
      }
    }
    if (bestId != null) widget.onInspect?.call(bestId);
  }

  @override
  Widget build(BuildContext context) {
    final base = paletteFor(Theme.of(context).brightness == Brightness.light
        ? ThemeMode.light
        : ThemeMode.dark);
    final palette = _stale ? base.desaturated() : base;

    if (scene == null) return const SizedBox.expand();

    final xf = Matrix4.identity()
      ..translateByDouble(_pan.dx, _pan.dy, 0, 1)
      ..scaleByDouble(_scale, _scale, 1, 1);

    return Focus(
      focusNode: _focusNode,
      autofocus: true,
      onKeyEvent: _onKey,
      child: SizedBox.expand(
        child: Stack(
          clipBehavior: Clip.hardEdge,
          children: [
            Positioned.fill(
              child: Listener(
                onPointerSignal: (event) {
                  if (event is PointerScrollEvent) {
                    // Anchor the zoom under the cursor so the world point
                    // under the pointer stays fixed while zooming.
                    _zoomAt(
                      event.position,
                      math.pow(1.0015, -event.scrollDelta.dy).toDouble(),
                    );
                  }
                },
                child: GestureDetector(
                  onTapDown: (d) {
                    _focusNode.requestFocus();
                    _handleTap(d);
                  },
                  onScaleStart: (d) {
                    _baseScale = _scale;
                    _baseWorld = Offset(
                      (d.focalPoint.dx - _pan.dx) / _scale,
                      (d.focalPoint.dy - _pan.dy) / _scale,
                    );
                  },
                  onScaleUpdate: (d) {
                    final base = _baseWorld;
                    if (base == null || d.scale <= 0) return;
                    _scale = (_baseScale * d.scale).clamp(0.25, 4.0);
                    _pan = Offset(
                      d.focalPoint.dx - base.dx * _scale,
                      d.focalPoint.dy - base.dy * _scale,
                    );
                    setState(() {});
                  },
                  onScaleEnd: (d) => _baseWorld = null,
                  // Clip the painter to the canvas's own area: the CustomPaint
                  // is a positioned child of the Stack (so Stack.clipBehavior
                  // never clips it), and the world transform lets panning draw
                  // node pixels outside these bounds — without this clip they
                  // would bleed over the sidebar.
                  child: ClipRect(
                    child: CustomPaint(
                      painter: ScenePainter(
                        scene: scene!,
                        definition: widget.definition,
                        palette: palette,
                        transform: xf,
                        clock: _clock,
                        pulses: _pulses,
                        envMotes: _motes,
                      ),
                    ),
                  ),
                ),
              ),
            ),
            // Bottom-right zoom controls (screen space).
            Positioned(
              bottom: 8,
              right: 8,
              child: ZoomControls(
                surface: palette.surface,
                text: palette.text,
                scale: _scale,
                onZoomIn: () => _zoomAt(_viewportCenter(), 1.25),
                onZoomOut: () => _zoomAt(_viewportCenter(), 0.8),
                onReset: () => _setScaleAt(_viewportCenter(), 1.0),
                onFit: () {
                  final sz = context.size;
                  if (sz != null) _zoomToFit(sz);
                },
              ),
            ),
            if (_stale)
              Positioned(
                top: 8,
                left: 0,
                right: 0,
                child: Center(
                  child: Container(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
                    decoration: BoxDecoration(
                      color: base.surface.withValues(alpha: 0.85),
                      borderRadius: BorderRadius.circular(6),
                      border: Border.all(color: base.text.withValues(alpha: 0.4)),
                    ),
                    child: Text(
                      'stale — waiting for server',
                      style: TextStyle(fontSize: 12, color: base.text),
                    ),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
