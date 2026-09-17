import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/app/sidebar/history_list.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// The HISTORY sidebar section (US2): past runs, newest first, each with a
/// state badge and a relative start age. The section is collapsible from its
/// header and filters by run id (full or displayed short form) / workflow
/// name plus a state menu that offers only the states present in the
/// history. The header count shows `total`, or `matched/total` while
/// filtering.
void main() {
  final t0 = DateTime.utc(2026, 8, 29, 8);
  final t1 = DateTime.utc(2026, 8, 29, 9);
  final t2 = DateTime.utc(2026, 8, 29, 10);
  final now = DateTime.utc(2026, 8, 29, 10, 30);

  RunHistoryEntry entry(
          String runId, String workflowName, DateTime at, String state) =>
      RunHistoryEntry(
        runId: runId,
        workflowName: workflowName,
        startedAt: at,
        state: state,
      );

  final eOld = entry('r-aaaaaaaaaaaaaaaa', 'demo', t0, 'completed');
  final eMid = entry('r-bbbbbbbbbbbbbbbb', 'demo2', t1, 'failed');
  final eNew = entry('r-0123456789abcdef', 'demo', t2, 'completed');
  final all = [eOld, eMid, eNew];

  Future<void> pump(
    WidgetTester tester,
    List<RunHistoryEntry> entries, {
    List<String>? selected,
  }) =>
      tester.pumpWidget(MaterialApp(
        theme: themeDataFor(ThemeMode.dark),
        home: Scaffold(
          body: HistoryList(
            entries: entries,
            selectedRunId: null,
            now: now,
            onSelect: (id) => selected?.add(id),
          ),
        ),
      ));

  /// A menu item while the state filter popup is open. Scoped to the
  /// [PopupMenuItem]s so it never matches the state badges on rows.
  Finder menuItem(String text) => find.descendant(
        of: find.byWidgetPredicate((w) => w is PopupMenuItem<String>),
        matching: find.text(text),
      );

  group('history list section', () {
    testWidgets('lists runs newest first with a total count', (tester) async {
      await pump(tester, all);

      expect(find.text('HISTORY'), findsOneWidget);
      expect(find.text('3'), findsOneWidget,
          reason: 'the header counts the entries');
      expect(find.text('r-0123456789'), findsOneWidget,
          reason: 'long run ids render in short form');
      expect(find.text('r-0123456789abcdef'), findsNothing,
          reason: 'the full long id is not shown');
      expect(find.text('r-aaaaaaaaaa'), findsOneWidget);

      final yNew = tester.getTopLeft(find.text('r-0123456789')).dy;
      final yMid = tester.getTopLeft(find.text('r-bbbbbbbbbb')).dy;
      final yOld = tester.getTopLeft(find.text('r-aaaaaaaaaa')).dy;
      expect(yNew, lessThan(yMid));
      expect(yMid, lessThan(yOld),
          reason: 'history is listed newest-first');
    });

    testWidgets('collapses on header tap and re-expands on the next tap',
        (tester) async {
      await pump(tester, all);

      await tester.tap(find.text('HISTORY'));
      await tester.pumpAndSettle();

      expect(find.text('r-aaaaaaaaaa'), findsNothing,
          reason: 'rows hide with the section body');
      expect(find.byType(TextField), findsNothing,
          reason: 'the filter field hides with the section body');
      expect(find.text('HISTORY'), findsOneWidget,
          reason: 'the header stays visible to re-expand');
      expect(find.byTooltip('Expand HISTORY'), findsOneWidget);

      await tester.tap(find.text('HISTORY'));
      await tester.pumpAndSettle();

      expect(find.text('r-aaaaaaaaaa'), findsOneWidget,
          reason: 're-expanding restores every row');
      expect(find.byType(TextField), findsOneWidget);
      expect(find.byTooltip('Collapse HISTORY'), findsOneWidget);
    });

    testWidgets('filters by workflow name, full run id, and short run id',
        (tester) async {
      await pump(tester, all);

      await tester.enterText(find.byType(TextField), 'demo2');
      await tester.pump();
      expect(find.text('r-bbbbbbbbbb'), findsOneWidget,
          reason: 'the query matches the workflow name');
      expect(find.text('r-aaaaaaaaaa'), findsNothing);
      expect(find.text('r-0123456789'), findsNothing);
      expect(find.text('1/3'), findsOneWidget);

      await tester.enterText(find.byType(TextField), 'AAAA');
      await tester.pump();
      expect(find.text('r-aaaaaaaaaa'), findsOneWidget,
          reason: 'the full run id matches, case-insensitively');
      expect(find.text('r-bbbbbbbbbb'), findsNothing);
      expect(find.text('1/3'), findsOneWidget);

      await tester.enterText(find.byType(TextField), '0123456789');
      await tester.pump();
      expect(find.text('r-0123456789'), findsOneWidget,
          reason: 'the displayed short run id is searchable');
      expect(find.text('r-aaaaaaaaaa'), findsNothing);
    });

    testWidgets('shows the no-match hint when the query matches nothing',
        (tester) async {
      await pump(tester, all);

      await tester.enterText(find.byType(TextField), 'zzz');
      await tester.pump();

      expect(find.text('No matches.'), findsOneWidget);
      expect(find.text('0/3'), findsOneWidget);
    });

    testWidgets('an empty history keeps its own hint', (tester) async {
      await pump(tester, const []);

      expect(find.text('No runs yet.'), findsOneWidget);
      expect(find.text('0'), findsOneWidget);
    });

    testWidgets('the state menu offers only the states present in the history',
        (tester) async {
      await pump(tester, all);

      await tester.tap(find.byIcon(Icons.filter_list));
      await tester.pumpAndSettle();

      expect(menuItem('All states'), findsOneWidget);
      expect(menuItem('completed'), findsOneWidget);
      expect(menuItem('failed'), findsOneWidget);
      expect(menuItem('running'), findsNothing,
          reason: 'absent states are not offered');
      expect(menuItem('interrupted'), findsNothing);
    });

    testWidgets('selecting a state narrows the list; All states restores it',
        (tester) async {
      await pump(tester, all);

      await tester.tap(find.byIcon(Icons.filter_list));
      await tester.pumpAndSettle();
      await tester.tap(menuItem('failed'));
      await tester.pumpAndSettle();

      expect(find.text('r-bbbbbbbbbb'), findsOneWidget);
      expect(find.text('r-aaaaaaaaaa'), findsNothing);
      expect(find.text('r-0123456789'), findsNothing);
      expect(find.text('1/3'), findsOneWidget);

      await tester.tap(find.byIcon(Icons.filter_list));
      await tester.pumpAndSettle();
      await tester.tap(menuItem('All states'));
      await tester.pumpAndSettle();

      expect(find.text('r-aaaaaaaaaa'), findsOneWidget,
          reason: 'clearing the state filter restores every row');
      expect(find.text('3'), findsOneWidget);
    });

    testWidgets('the text query and the state filter combine',
        (tester) async {
      await pump(tester, all);

      await tester.enterText(find.byType(TextField), 'r');
      await tester.pump();
      expect(find.text('3/3'), findsOneWidget,
          reason: 'every run id starts with "r-"');

      await tester.tap(find.byIcon(Icons.filter_list));
      await tester.pumpAndSettle();
      await tester.tap(menuItem('completed'));
      await tester.pumpAndSettle();

      expect(find.text('r-aaaaaaaaaa'), findsOneWidget);
      expect(find.text('r-0123456789'), findsOneWidget);
      expect(find.text('r-bbbbbbbbbb'), findsNothing,
          reason: 'its failed state is excluded');
      expect(find.text('2/3'), findsOneWidget);
    });

    testWidgets('the clear button restores the unfiltered list',
        (tester) async {
      await pump(tester, all);

      await tester.enterText(find.byType(TextField), 'abcdef');
      await tester.pump();
      expect(find.text('1/3'), findsOneWidget,
          reason: 'only the abcdef run matches the id');

      await tester.tap(find.byIcon(Icons.close));
      await tester.pump();

      expect(find.text('r-aaaaaaaaaa'), findsOneWidget);
      expect(find.text('r-bbbbbbbbbb'), findsOneWidget);
      expect(find.text('r-0123456789'), findsOneWidget,
          reason: 'clearing the query restores every row');
      expect(find.text('3'), findsOneWidget);
    });

    testWidgets('row taps still select the run while filtering',
        (tester) async {
      final selected = <String>[];
      await pump(tester, all, selected: selected);

      await tester.enterText(find.byType(TextField), 'demo2');
      await tester.pump();
      await tester.tap(find.text('r-bbbbbbbbbb'));

      expect(selected, ['r-bbbbbbbbbbbbbbbb'],
          reason: 'onSelect carries the full run id');
    });
  });
}
