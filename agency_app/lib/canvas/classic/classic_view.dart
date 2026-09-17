import 'dart:math' as math;

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import '../../catalog/workflow_def.dart';
import '../../run/run_store.dart';
import '../canvas_keyboard.dart';
import '../zoom_controls.dart';
import 'classic_layout.dart';
import 'classic_legend.dart';
import 'classic_painter.dart';
import 'classic_palette.dart';
import 'classic_scene.dart';
import 'classic_topology.dart';

/// The classic canvas view (US1–US5): reads `RunStore.state` for the
/// selected run, rebuilds the scene on every store change, paints it with
/// `ClassicPainter` using the theme-selected `ClassicPalette`, performs
/// zoom-to-fit on the first scene build and on every run change, and hosts
/// the `ClassicLegend` overlay. The view owns a [Ticker] (mirroring
/// `CanvasView`): a ticker clock drives the running-state pulsing border
/// and its orbiting comet (FR-008), a per-node pulse map registered when a
/// node's (status, model,
/// fallback) identity changes drives the ~600 ms card highlight + traveling
/// edge markers (FR-013), and a ticker-anchored "now" feeds the `retry in
/// Ns` countdown (FR-010). Stale runs freeze the clock, desaturate the
/// palette, and show the shared stale badge (US5). Pan/zoom gestures (drag
/// to pan, wheel/pinch to zoom under the cursor, clamped 0.25–4) plus the
/// standard keyboard gestures (arrows pan, +/− zoom, 0 resets to 100%,
/// F zooms to fit) and the bottom-right [ZoomControls] bar; tap hit-testing
/// on the card rectangles (world space) opens the shared inspector (US5,
/// T035). The tapped node is reported through [onInspect] and, while
/// [selectedNodeId] names it, its card carries the accent selection
/// highlight (halo + tinted fill + thick border); a tap on empty canvas
/// reports null and clears the selection.
class ClassicCanvasView extends StatefulWidget {
  const ClassicCanvasView({
    super.key,
    required this.store,
    this.definition,
    this.onInspect,
    required this.topology,
    this.selectedNodeId,
  });

  final RunStore store;
  final WorkflowDefinition? definition;

  /// Called with the tapped node id, or null when the tap misses every card
  /// (deselects).
  final ValueChanged<String?>? onInspect;

  /// The inspected node's id, painted with the accent selection highlight on
  /// its card; null when no node is selected (the default).
  final String? selectedNodeId;

  /// The flow topology the scene is laid out under (persisted app setting).
  final CanvasTopology topology;

  @override
  State<ClassicCanvasView> createState() => ClassicCanvasViewState();
}

class ClassicCanvasViewState extends State<ClassicCanvasView>
    with TickerProviderStateMixin {
  ClassicScene? scene;
  Offset _pan = Offset.zero;
  double _scale = 1;
  double _baseScale = 1;
  Offset? _baseWorld;
  bool _zoomToFitDone = false;
  bool _stale = false;
  Ticker? _ticker;
  double _clock = 0;
  final Map<String, double> _pulses = {};
  ClassicScene? _prevScene;
  final DateTime _origin = DateTime.now();
  late final FocusNode _focusNode = FocusNode();

  /// The current view transform (exposed for tests).
  Offset get pan => _pan;
  double get scale => _scale;

  /// Ticker seconds of the view's [Ticker] (FR-008/FR-013); frozen at the
  /// last value while stale.
  double get clock => _clock;

  /// Node id → pulse start (ticker seconds), exposed for tests.
  Map<String, double> get pulses => Map.unmodifiable(_pulses);

  /// Wall-clock of the current frame, anchored at the ticker origin so it
  /// advances exactly with the pumped ticker clock (FR-010, testable).
  DateTime get now =>
      _origin.add(Duration(milliseconds: (_clock * 1000).round()));

  @override
  void initState() {
    super.initState();
    widget.store.addListener(_onStoreChanged);
    _ticker = createTicker((value) {
      // Stale (disconnected) freezes the clock and halts repaints so
      // animations pause (US5).
      if (_stale) return;
      _clock = value.inMicroseconds / 1e6;
      setState(() {});
    })..start();
    // The canvas starts focused so the keyboard viewport gestures work out
    // of the box; a canvas tap re-requests focus (see onTapDown).
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) _focusNode.requestFocus();
    });
    _onStoreChanged();
  }

  @override
  void didUpdateWidget(covariant ClassicCanvasView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.store != widget.store) {
      oldWidget.store.removeListener(_onStoreChanged);
      widget.store.addListener(_onStoreChanged);
      _pan = Offset.zero;
      _scale = 1;
      _baseScale = 1;
      _baseWorld = null;
      _zoomToFitDone = false;
      _pulses.clear();
      _prevScene = null;
      _onStoreChanged();
    } else if (oldWidget.topology != widget.topology) {
      // Rebuild the scene in the new orientation and re-fit it into view
      // (brings the whole workflow back on screen). Node statuses are
      // unchanged, so no FR-013 pulse fires.
      _zoomToFitDone = false;
      _onStoreChanged();
    }
  }

  @override
  void dispose() {
    _ticker?.stop();
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
    if (def == null || widget.store.state == null) {
      if (scene != null) setState(() => scene = null);
      return;
    }
    final newScene = buildClassicScene(
      widget.store.state!,
      definition: def,
      topology: widget.topology,
    );
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

  /// FR-013: register a pulse at the current ticker clock for every node
  /// whose (status, model, fallback) identity changed since the previous
  /// scene. The painter lifts the card highlight and edge markers when the
  /// ~600 ms window passes; unmapped frames leave every identity untouched
  /// and therefore never animate.
  void _registerPulses(ClassicScene newScene) {
    final prev = _prevScene;
    if (prev == null) return;
    for (final e in newScene.state.nodes.entries) {
      final node = e.value;
      final old = prev.state.nodes[e.key];
      if (old == null ||
          old.status != node.status ||
          old.model != node.model ||
          old.fallbackModel != node.fallbackModel) {
        _pulses[node.nodeId] = _clock;
      }
    }
  }

  void _zoomToFit(Size size) {
    final s = scene;
    if (s == null) return;
    final bounds = s.layout.bounds;
    if (bounds.width <= 0 || bounds.height <= 0) return;
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

  /// FR-017: a tap selects the card whose world-space rectangle contains the
  /// tapped world point (opening the shared inspector); a tap that misses
  /// every card deselects (null). Cards never overlap, so the first
  /// containment hit is the answer.
  void _handleTap(TapDownDetails details) {
    final s = scene;
    if (s == null) return;
    final world = Offset(
      (details.localPosition.dx - _pan.dx) / _scale,
      (details.localPosition.dy - _pan.dy) / _scale,
    );
    CardRect? hit;
    for (final c in s.layout.cards.values) {
      if (c.rect.contains(world)) {
        hit = c;
        break;
      }
    }
    widget.onInspect?.call(hit?.nodeId);
  }

  @override
  Widget build(BuildContext context) {
    final base = paletteFor(
        Theme.of(context).brightness == Brightness.light
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
                  // card pixels outside these bounds — without this clip they
                  // would bleed over the sidebar.
                  child: ClipRect(
                    child: CustomPaint(
                      painter: ClassicPainter(
                        scene: scene!,
                        palette: palette,
                        transform: xf,
                        clock: _clock,
                        nodePulses: _pulses,
                        now: now,
                        selectedNodeId: widget.selectedNodeId,
                      ),
                    ),
                  ),
                ),
              ),
            ),
            // Screen-space overlay: pinned top-left, unaffected by pan/zoom.
            Positioned(
              top: 8,
              left: 8,
              child: ClassicLegend(palette: palette),
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
            // The shared stale badge (FR-019), matching the Abyss view: the
            // last scene stays on screen, desaturated, until the store
            // re-synchronizes.
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
