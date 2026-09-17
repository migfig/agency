import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';

import '../api/agency_client.dart';
import '../api/models.dart';
import '../app/app_state.dart';
import '../app/sidebar/history_list.dart';
import '../app/sidebar/workflow_list.dart';
import '../canvas/canvas_view.dart';
import '../canvas/classic/classic_topology.dart';
import '../canvas/classic/classic_view.dart';
import '../canvas/hil_prompt.dart';
import '../canvas/inspector.dart';
import '../canvas/summary_bar.dart';
import '../catalog/catalog_entry.dart';
import '../catalog/workflow_def.dart';
import '../monitor/resource_monitor.dart';
import '../monitor/resource_monitor_panel.dart';
import '../run/run_state.dart';
import '../run/run_store.dart';

/// Full width of the sidebar drawer (px). It is the expanded width; the
/// collapsed drawer animates this to 0 so the canvas can maximize.
const double kSidebarWidth = 280;

class HomePage extends StatefulWidget {
  const HomePage({
    super.key,
    required this.app,
    required this.client,
    required this.entries,
    required this.eventStream,
    this.monitor,
  });

  final AppState app;
  final AgencyClient client;
  final List<CatalogEntry> entries;
  final Stream<EventFrame> eventStream;

  /// App-level resource-monitor state (spec 007). When present, the monitor
  /// panel overlays the canvas area in every run state (the no-run
  /// placeholder included).
  final ResourceMonitor? monitor;

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> with WidgetsBindingObserver {
  final Set<String> _starting = {};
  final Map<String, String> _startErrors = {};
  String? _inspecting;
  String? _selectedRunId;
  final Set<String> _hilSubmitting = {};
  final Map<String, String> _hilNotes = {};

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    widget.app.addListener(_onAppChanged);
    _selectedRunId = widget.app.selectedRunId;
    _startup();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    widget.app.removeListener(_onAppChanged);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _startup();
  }

  /// Loads the server history and, when no run is selected, selects the
  /// newest one (server history is ascending, so the last entry).
  Future<void> _startup() async {
    List<RunHistoryEntry> history;
    try {
      history = await widget.client.listRuns();
    } catch (_) {
      return;
    }
    final app = widget.app;
    app.setHistory(history);
    if (app.selectedRunId == null && history.isNotEmpty) {
      final newest = history.last;
      app.selectRun(
        newest.runId,
        workflowDefinition: _definitionFor(newest.workflowName),
      );
    }
  }

  /// Refresh the sidebar history from the server (best effort: a failed poll
  /// keeps the last known list).
  Future<void> _refreshHistory() async {
    try {
      widget.app.setHistory(await widget.client.listRuns());
    } catch (_) {}
  }

  /// A selected run just finished: refresh the history so its state in the
  /// sidebar (running → completed/failed) and any newer runs are current.
  void _onRunFinalized() => unawaited(_refreshHistory());

  void _selectHistory(String runId) {
    String? workflowName;
    for (final e in widget.app.history) {
      if (e.runId == runId) {
        workflowName = e.workflowName;
        break;
      }
    }
    widget.app.selectRun(
      runId,
      workflowDefinition: _definitionFor(workflowName),
    );
  }

  WorkflowDefinition? _definitionFor(String? workflowName) {
    if (workflowName == null) return null;
    for (final e in widget.entries) {
      final def = e.definition;
      if (def != null && def.name == workflowName) return def;
    }
    return null;
  }

  void _onAppChanged() {
    final newRunId = widget.app.selectedRunId;
    if (newRunId != _selectedRunId) {
      setState(() {
        _selectedRunId = newRunId;
        _inspecting = null;
      });
    }
  }

  Future<void> _start(CatalogEntry entry) async {
    final def = entry.definition;
    if (def == null || _starting.contains(entry.path)) return;
    setState(() {
      _starting.add(entry.path);
      _startErrors.remove(entry.path);
    });
    try {
      final accepted = await widget.client.startRun(
        File(entry.path).absolute.path,
      );
      widget.app.selectRun(accepted.runId, workflowDefinition: def);
      // The server history now includes the new run; surface it in the
      // sidebar without waiting for the next startup/reconnect refresh.
      unawaited(_refreshHistory());
    } on ApiError catch (e) {
      setState(() => _startErrors[entry.path] = userFacingMessage(e.body.code));
    } on NetworkError catch (e) {
      setState(() => _startErrors[entry.path] = e.message);
    } finally {
      setState(() => _starting.remove(entry.path));
    }
  }

  void _setInspecting(String? nodeId) {
    setState(() {
      _inspecting = _inspecting == nodeId ? null : nodeId;
    });
  }

  /// POST the answer for a human-in-the-loop node, then resync so the card
  /// closes when the server no longer lists the input (FR-017, research R12).
  /// A 409 `input_not_awaiting` keeps the card but notes the duplicate; a
  /// 404 `unknown_node` just resyncs; other failures show the message and
  /// leave the card open.
  Future<void> _submitHil(String nodeId, String text) async {
    final runId = widget.app.selectedRunId;
    if (runId == null) return;
    final store = widget.app.storeFor(runId);
    if (_hilSubmitting.contains(nodeId)) return;
    setState(() => _hilSubmitting.add(nodeId));
    var resync = true;
    try {
      await widget.client.submitInput(runId, nodeId, text);
    } on ApiError catch (e) {
      final code = e.body.code;
      if (code == ErrorCodes.inputNotAwaiting) {
        setState(
          () => _hilNotes[nodeId] = 'input not needed — already resolved',
        );
      } else if (code != ErrorCodes.unknownNode) {
        resync = false;
        setState(() => _hilNotes[nodeId] = userFacingMessage(code));
      }
    } on NetworkError catch (e) {
      resync = false;
      setState(() => _hilNotes[nodeId] = e.message);
    } finally {
      setState(() => _hilSubmitting.remove(nodeId));
    }
    if (resync) {
      await store.resync(
        runId,
        widget.client.runStatus,
        fetchResult: widget.client.runResult,
      );
    }
  }

  Widget _hilWidget(RunState state) {
    return Positioned(
      left: 8,
      right: 8,
      bottom: 8,
      child: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 520),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              for (final p in state.pendingInputs)
                HilPromptCard(
                  prompt: p,
                  note: _hilNotes[p.nodeId],
                  submitting: _hilSubmitting.contains(p.nodeId),
                  onSubmit: (text) => _submitHil(p.nodeId, text),
                ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _inspectorWidget(RunState state) {
    final nodeId = _inspecting!;
    final node = state.nodes[nodeId];
    if (node == null) return const SizedBox.shrink();
    return Inspector(node: node, onClose: () => _setInspecting(null));
  }

  /// T047: edit the configured server address; saving persists it and
  /// retargets client + events (AppState.setServerAddress).
  void _showAddressDialog() {
    final controller = TextEditingController(text: widget.app.serverAddress);
    showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Server address'),
        content: TextField(
          controller: controller,
          autofocus: true,
          decoration: const InputDecoration(
            hintText: 'http://127.0.0.1:8000',
            helperText: 'The Agency API base address (persisted).',
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () {
              final address = controller.text.trim();
              if (address.isNotEmpty) {
                widget.app.setServerAddress(address);
              }
              Navigator.pop(context);
            },
            child: const Text('Save'),
          ),
        ],
      ),
    );
  }

  /// The visible `Abyss | Classic` two-option canvas mode toggle (T018),
  /// next to the theme toggle. Drives `app.canvasMode` (persisted).
  Widget _canvasModeToggle(ColorScheme scheme) {
    return Container(
      decoration: BoxDecoration(
        border: Border.all(color: scheme.outlineVariant),
        borderRadius: BorderRadius.circular(6),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          _canvasModeOption('Abyss', CanvasMode.abyss, scheme),
          _canvasModeOption('Classic', CanvasMode.classic, scheme),
        ],
      ),
    );
  }

  Widget _canvasModeOption(String label, CanvasMode mode, ColorScheme scheme) {
    final selected = widget.app.canvasMode == mode;
    return Tooltip(
      message: 'Canvas mode: $label',
      child: TextButton(
        onPressed: () => widget.app.setCanvasMode(mode),
        style: TextButton.styleFrom(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 2),
          minimumSize: const Size(0, 26),
          textStyle: TextStyle(
            fontSize: 12,
            fontWeight: selected ? FontWeight.w600 : FontWeight.w400,
            color: selected ? scheme.onSurface : scheme.onSurfaceVariant,
          ),
          backgroundColor: selected
              ? scheme.secondaryContainer
              : Colors.transparent,
        ),
        child: Text(label),
      ),
    );
  }

  /// The classic-canvas flow-topology menu, shown only in classic mode
  /// (the topology only affects the classic layout). Selecting an option
  /// persists [AppState.canvasTopology] and re-lays out + re-fits the view.
  Widget _topologyMenu() {
    if (widget.app.canvasMode != CanvasMode.classic) {
      return const SizedBox.shrink();
    }
    return PopupMenuButton<CanvasTopology>(
      tooltip: 'Layout topology',
      icon: const Icon(Icons.fork_right, size: 18),
      onSelected: (t) => widget.app.setCanvasTopology(t),
      itemBuilder: (context) => [
        for (final t in CanvasTopology.values)
          PopupMenuItem<CanvasTopology>(
            value: t,
            child: Text(
              canvasTopologyLabel(t),
              style: TextStyle(
                fontSize: 12,
                fontWeight: widget.app.canvasTopology == t
                    ? FontWeight.w600
                    : null,
              ),
            ),
          ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Scaffold(
      body: Column(
        children: [
          SizedBox(
            height: 40,
            // Rebuilds on any app change so the sidebar toggle reflects
            // `sidebarVisible` regardless of how HomePage is mounted.
            child: ListenableBuilder(
              listenable: widget.app,
              builder: (context, _) => Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16),
                child: Row(
                  children: [
                    IconButton(
                      tooltip: widget.app.sidebarVisible
                          ? 'Hide sidebar'
                          : 'Show sidebar',
                      icon: Icon(
                        widget.app.sidebarVisible
                            ? Icons.vertical_split
                            : Icons.menu_open,
                        size: 18,
                      ),
                      onPressed: () => widget.app.setSidebarVisible(
                        !widget.app.sidebarVisible,
                      ),
                    ),
                    const Text(
                      'Agency',
                      style: TextStyle(fontWeight: FontWeight.w600),
                    ),
                    const Spacer(),
                    ConnectionBanner(app: widget.app),
                    IconButton(
                      tooltip: 'Server address',
                      icon: const Icon(Icons.dns, size: 18),
                      onPressed: _showAddressDialog,
                    ),
                    _canvasModeToggle(scheme),
                    _topologyMenu(),
                    IconButton(
                      tooltip: widget.app.monitorVisible
                          ? 'Hide resource monitor'
                          : 'Show resource monitor',
                      icon: Icon(
                        widget.app.monitorVisible
                            ? Icons.monitor
                            : Icons.monitor_outlined,
                        size: 18,
                      ),
                      onPressed: () => widget.app.setMonitorVisible(
                        !widget.app.monitorVisible,
                      ),
                    ),
                    IconButton(
                      tooltip: 'Toggle theme',
                      icon: Icon(
                        widget.app.themeMode == ThemeMode.dark
                            ? Icons.light_mode
                            : Icons.dark_mode,
                        size: 18,
                      ),
                      onPressed: () => widget.app.setThemeMode(
                        widget.app.themeMode == ThemeMode.dark
                            ? ThemeMode.light
                            : ThemeMode.dark,
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
          Expanded(
            child: Row(
              children: [
                // The collapsible sidebar drawer: a clipping window whose
                // width animates between [kSidebarWidth] (expanded) and 0
                // (collapsed). The inner content stays a fixed [kSidebarWidth]
                // so it slides/wipes rather than reflowing, and the canvas
                // (the Expanded sibling) maximizes as the window closes.
                ListenableBuilder(
                  listenable: widget.app,
                  builder: (context, _) => AnimatedContainer(
                    duration: const Duration(milliseconds: 200),
                    curve: Curves.easeInOutCubic,
                    width: widget.app.sidebarVisible ? kSidebarWidth : 0.0,
                    child: ClipRect(
                      child: OverflowBox(
                        minWidth: 0,
                        maxWidth: kSidebarWidth,
                        alignment: Alignment.topLeft,
                        child: SizedBox(
                          width: kSidebarWidth,
                          child: DecoratedBox(
                            decoration: BoxDecoration(
                              border: Border(
                                right: BorderSide(
                                  color: scheme.onSurfaceVariant.withValues(
                                    alpha: 0.35,
                                  ),
                                ),
                              ),
                            ),
                            child: SingleChildScrollView(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.stretch,
                                children: [
                                  WorkflowList(
                                    entries: widget.entries,
                                    startingPaths: _starting,
                                    startErrors: _startErrors,
                                    onStart: _start,
                                  ),
                                  HistoryList(
                                    entries: widget.app.history,
                                    selectedRunId: widget.app.selectedRunId,
                                    onSelect: _selectHistory,
                                  ),
                                ],
                              ),
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
                Expanded(
                  child: ListenableBuilder(
                    listenable: widget.app,
                    builder: (context, child) {
                      final runId = widget.app.selectedRunId;
                      if (runId == null) {
                        return Stack(
                          children: [
                            Positioned.fill(
                              child: Center(
                                child: Text(
                                  // While the start POST is in flight the
                                  // server is still provisioning (model
                                  // endpoints, sandbox image), so the run id
                                  // — and thus the canvas — is not ready yet.
                                  _starting.isNotEmpty
                                      ? 'Starting workflow — provisioning models and tools, this can take a while…'
                                      : 'No runs yet — start a workflow from the sidebar.',
                                ),
                              ),
                            ),
                            // The gate may unmount the panel widget; nothing
                            // is lost — all panel state lives in the
                            // ResourceMonitor (US2-AS5).
                            if (widget.monitor != null &&
                                widget.app.monitorVisible)
                              ResourceMonitorPanel(monitor: widget.monitor!),
                          ],
                        );
                      }
                      final store = widget.app.storeFor(runId);
                      return ListenableBuilder(
                        listenable: store,
                        builder: (context, child) {
                          final state = store.state;
                          return Column(
                            children: [
                              if (state != null) SummaryBar(state: state),
                              Expanded(
                                child: Stack(
                                  children: [
                                    Positioned.fill(
                                      child: _RunPanel(
                                        key: ValueKey('run-$runId'),
                                        client: widget.client,
                                        runId: runId,
                                        store: store,
                                        eventStream: widget.eventStream,
                                        canvasMode: widget.app.canvasMode,
                                        canvasTopology:
                                            widget.app.canvasTopology,
                                        inspecting: _inspecting,
                                        onInspect: _setInspecting,
                                        onUnknownRun: () {
                                          if (widget.app.selectedRunId ==
                                              runId) {
                                            widget.app.deselectRun();
                                          }
                                          _startup();
                                        },
                                        onRunFinalized: _onRunFinalized,
                                      ),
                                    ),
                                    if (widget.monitor != null &&
                                        widget.app.monitorVisible)
                                      ResourceMonitorPanel(
                                        monitor: widget.monitor!,
                                      ),
                                    if (state != null && _inspecting != null)
                                      _inspectorWidget(state),
                                    if (state != null &&
                                        state.pendingInputs.isNotEmpty)
                                      _hilWidget(state),
                                  ],
                                ),
                              ),
                            ],
                          );
                        },
                      );
                    },
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _RunPanel extends StatefulWidget {
  const _RunPanel({
    super.key,
    required this.client,
    required this.runId,
    required this.store,
    required this.eventStream,
    required this.canvasMode,
    required this.canvasTopology,
    required this.inspecting,
    required this.onInspect,
    this.onUnknownRun,
    this.onRunFinalized,
  });

  final AgencyClient client;
  final String runId;
  final RunStore store;
  final Stream<EventFrame> eventStream;
  final CanvasMode canvasMode;
  final CanvasTopology canvasTopology;
  final String? inspecting;
  final ValueChanged<String?>? onInspect;

  /// Invoked when the server reports this run no longer exists (US2).
  final VoidCallback? onUnknownRun;

  /// Invoked once after the run reaches all-terminal node status and the
  /// finalize pass (status + result fetch) has run, so callers can refresh
  /// dependent views (e.g. the sidebar history).
  final VoidCallback? onRunFinalized;

  @override
  State<_RunPanel> createState() => _RunPanelState();
}

class _RunPanelState extends State<_RunPanel> {
  bool _seeding = false;

  @override
  void initState() {
    super.initState();
    _connect();
    _seedIfEmpty();
  }

  @override
  void didUpdateWidget(covariant _RunPanel oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.store != widget.store ||
        oldWidget.eventStream != widget.eventStream) {
      _connect();
    }
  }

  /// (Re)subscribe the store to the frame stream, wiring the status/result
  /// fetchers so the store can finalize the run (fold in per-node output and
  /// total tokens) the moment every node has reached a terminal status.
  void _connect() {
    widget.store.connect(
      widget.eventStream,
      fetchStatus: widget.client.runStatus,
      fetchResult: widget.client.runResult,
      onFinalized: widget.onRunFinalized,
    );
  }

  Future<void> _seedIfEmpty() async {
    if (_seeding || widget.store.state != null) return;
    _seeding = true;
    try {
      final snapshot = await widget.client.runStatus(widget.runId);
      widget.store.seed(snapshot);
      if (snapshot.state != 'running') {
        try {
          final result = await widget.client.runResult(widget.runId);
          widget.store.applyResult(result);
        } catch (_) {}
      }
    } on ApiError catch (e) {
      if (e.body.code == ErrorCodes.unknownRun) {
        widget.onUnknownRun?.call();
      }
    } catch (_) {}
  }

  /// The active canvas renderer for `app.canvasMode` (T018): the Abyss
  /// `CanvasView` or the classic `ClassicCanvasView`. Both read the same
  /// store/selection, so switching re-renders the selected run immediately
  /// with no loss of run state or inspected step.
  Widget _canvasWidget(RunState state) {
    final definition =
        widget.store.workflowDefinition ?? _emptyDef(state.workflow);
    if (widget.canvasMode == CanvasMode.classic) {
      return ClassicCanvasView(
        store: widget.store,
        definition: definition,
        onInspect: widget.onInspect,
        selectedNodeId: widget.inspecting,
        topology: widget.canvasTopology,
      );
    }
    return CanvasView(
      store: widget.store,
      definition: definition,
      onInspect: widget.onInspect,
      events: widget.eventStream,
    );
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: widget.store,
      builder: (context, child) {
        final state = widget.store.state;
        if (state == null) {
          return const Center(child: Text('loading run…'));
        }
        return _canvasWidget(state);
      },
    );
  }

  /// A name-only definition, so the run's nodes still render (orphan column)
  /// when no catalog definition is available for the selected run (US2).
  WorkflowDefinition _emptyDef(String name) => WorkflowDefinition(
    name: name,
    sourcePath: '',
    lastModified: 0,
    nodes: const {},
    edges: const [],
    phases: const [],
  );
}

/// The tri-state connection banner (US5, contracts/ui.md :28-30):
/// connected = quiet dot, reconnecting = amber pulsing label,
/// disconnected = red label.
class ConnectionBanner extends StatelessWidget {
  const ConnectionBanner({super.key, required this.app});

  final AppState app;

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: app,
      builder: (context, _) {
        switch (app.link) {
          case ServerLink.connected:
            return const Icon(Icons.circle, size: 8, color: Colors.green);
          case ServerLink.reconnecting:
            return const _PulsingLabel(text: 'reconnecting…');
          case ServerLink.disconnected:
            return const Text(
              'disconnected — retrying',
              style: TextStyle(color: Colors.red, fontSize: 12),
            );
        }
      },
    );
  }
}

class _PulsingLabel extends StatefulWidget {
  const _PulsingLabel({required this.text});

  final String text;

  @override
  State<_PulsingLabel> createState() => _PulsingLabelState();
}

class _PulsingLabelState extends State<_PulsingLabel>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 900),
  )..repeat(reverse: true);

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return FadeTransition(
      opacity: Tween<double>(begin: 0.35, end: 1).animate(_controller),
      child: Text(
        widget.text,
        style: const TextStyle(color: Colors.amber, fontSize: 12),
      ),
    );
  }
}
