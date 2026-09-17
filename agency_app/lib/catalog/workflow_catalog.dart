import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';

import 'catalog_entry.dart';
import 'workflow_def.dart';

/// A live catalog of workflow files in a root directory.
///
/// Scans [root] recursively for `.yaml`/`.yml` files, parses each, and
/// notifies listeners when the catalog changes. Uses `Directory.watch`
/// (recursive) with a 500 ms debounce, plus a 5 s fallback poll so nothing
/// is silently missed on platforms with flaky fs events.
class WorkflowCatalog extends ChangeNotifier {
  WorkflowCatalog(this.root) {
    _scan();
    _startWatching();
  }

  final String root;
  final Map<String, WorkflowDefinition> _byPath = {};
  final List<String> _order = [];
  final Map<String, String> _errors = {};

  /// Paths in stable (sorted) order.
  List<String> get paths => List.unmodifiable(_order);

  /// Sidebar-ready entries: readable carry a definition, unreadable carry
  /// [CatalogEntry.parseError]. Ordering mirrors [_order] (filesystem sort).
  List<CatalogEntry> get entries {
    return List.of(_order.map((p) {
      final def = _byPath[p];
      if (def != null) {
        return CatalogEntry(path: p, name: def.name, definition: def);
      }
      return CatalogEntry(
        path: p,
        name: _fileStem(p),
        parseError: _errors[p] ?? 'unreadable',
      );
    }).toList());
  }

  WorkflowDefinition? definitionFor(String path) => _byPath[path];

  int get count => _order.length;

  /// Re-scan the root directory and refresh the in-memory catalog.
  void refresh() {
    _scan();
    notifyListeners();
  }

  bool _disposed = false;
  StreamSubscription? _watchSub;
  Timer? _debounce;
  Timer? _pollTimer;

  void _startWatching() {
    final dir = Directory(root);
    try {
      _watchSub = dir
          .watch(recursive: true)
          .listen(
            (_) => _scheduleRescan(),
            onError: (_) {},
            onDone: () => _watchSub = null,
          );
    } catch (_) {
      // Watching unavailable (e.g. read-only fs) — poll-only fallback still runs.
      _watchSub = null;
    }
    _pollTimer = Timer.periodic(const Duration(seconds: 5), (_) => _scan());
  }

  void _scheduleRescan() {
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 500), () {
      if (!_disposed) refresh();
    });
  }

  void _scan() {
    final dir = Directory(root);
    if (!dir.existsSync()) {
      _byPath.clear();
      _order.clear();
      _errors.clear();
      return;
    }
    final entities = dir.listSync(recursive: true, followLinks: false);
    final yamlPaths = entities
        .whereType<FileSystemEntity>()
        .map((e) => e.path)
        .where((p) => p.endsWith('.yaml') || p.endsWith('.yml'))
        .toList()
      ..sort();

    final next = <String, WorkflowDefinition>{};
    final errs = <String, String>{};
    for (final p in yamlPaths) {
      final file = File(p);
      if (!file.existsSync()) continue;
      try {
        next[p] = parseWorkflowDefinition(file.readAsStringSync(), sourcePath: p);
      } on WorkflowParseError catch (e) {
        errs[p] = e.message.split('\n').first.trim();
      } on FileSystemException catch (e) {
        errs[p] = e.message;
      }
    }
    _byPath
      ..clear()
      ..addAll(next);
    _order
      ..clear()
      ..addAll(yamlPaths);
    _errors
      ..clear()
      ..addAll(errs);
  }

  String _fileStem(String path) {
    final basename = path.split(Platform.pathSeparator).last;
    final dot = basename.lastIndexOf('.');
    return dot > 0 ? basename.substring(0, dot) : basename;
  }

  @override
  void dispose() {
    _disposed = true;
    _debounce?.cancel();
    _pollTimer?.cancel();
    _watchSub?.cancel();
    super.dispose();
  }
}
