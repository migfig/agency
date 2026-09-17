import 'workflow_def.dart';

/// One entry in the workflow catalog — the pure, filesystem-free input to
/// [WorkflowList] and [HomePage].
///
/// A readable entry carries a parsed [definition]; an unreadable one carries a
/// [parseError] (the first-line message) and no definition. `path` is the
/// catalog-relative path (the identity used to key per-row UI state), `name` is
/// the display name (the workflow's `name` when readable, else the file's stem).
class CatalogEntry {
  const CatalogEntry({
    required this.path,
    required this.name,
    this.definition,
    this.parseError,
  });

  /// Catalog-relative path of the `.yaml` file (unique within the catalog).
  final String path;

  /// Display name of the workflow.
  final String name;

  /// The parsed definition, when the file is readable.
  final WorkflowDefinition? definition;

  /// Human-readable first-line error, when the file failed to parse.
  final String? parseError;

  /// True when the file could not be parsed (a workflow cannot be started).
  bool get isUnreadable => definition == null && parseError != null;
}
