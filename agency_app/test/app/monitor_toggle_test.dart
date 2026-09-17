import 'dart:async';
import 'dart:convert';

import 'package:agencyapp/api/agency_client.dart';
import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/app/app_state.dart';
import 'package:agencyapp/app/home_page.dart';
import 'package:agencyapp/monitor/monitor_card.dart';
import 'package:agencyapp/monitor/resource_monitor.dart';
import 'package:agencyapp/monitor/resource_monitor_panel.dart';
import 'package:agencyapp/monitor/system_metrics.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// T019 — the topbar monitor toggle (US2, FR-004/SC-002,
/// contracts/ui.md "Topbar toggle"): the `IconButton` sits next to the theme
/// toggle, toggling it hides/shows the panel, the icon reflects the state,
/// and re-showing restores the full ~60 s history (US2-AS5) — all panel
/// state lives in the [ResourceMonitor], not the (unmounted) panel widget.
void main() {
  SystemMetrics sample(double cpu) => SystemMetrics(
        cpuUsage: cpu,
        ramUsage: cpu / 2,
        ramTotal: 16.0,
        ramUsed: 8.0,
        networkIn: cpu * 10,
        networkOut: cpu * 5,
        gpuUsage: cpu / 4,
        gpuMemoryHas: 24.0,
        gpuMemoryUsed: 3.0,
        gpuTemp: 55.0,
      );

  MockClient fakeClient() => MockClient((request) async {
        final headers = <String, String>{'content-type': 'application/json'};
        if (request.method == 'GET' && request.url.path == '/runs') {
          return http.Response(jsonEncode(<Object>[]), 200,
              headers: headers);
        }
        if (request.method == 'GET' && request.url.path == '/health') {
          return http.Response(jsonEncode({'status': 'ok'}), 200,
              headers: headers);
        }
        return http.Response(
          jsonEncode({'error': {'code': 'not_found', 'message': 'nope'}}),
          404,
          headers: headers,
        );
      });

  late AppState app;
  late ResourceMonitor monitor;
  late AgencyClient client;
  late StreamController<EventFrame> events;

  Future<void> pumpAgency(WidgetTester tester) async {
    await tester.pumpWidget(
      ListenableBuilder(
        listenable: app,
        builder: (context, _) => MaterialApp(
          title: 'Agency',
          debugShowCheckedModeBanner: false,
          theme: themeDataFor(app.themeMode),
          home: HomePage(
            app: app,
            client: client,
            entries: const [],
            eventStream: events.stream,
            monitor: monitor,
          ),
        ),
      ),
    );
  }

  /// Fresh app + monitor over the current mock prefs, then pump (the
  /// "restart" step of the persistence scenario).
  Future<void> bootAndPump(WidgetTester tester) async {
    app = AppState();
    await app.init();
    monitor = ResourceMonitor(await SharedPreferences.getInstance());
    client = AgencyClient('http://fake', httpClient: fakeClient());
    events = StreamController<EventFrame>.broadcast();
    await pumpAgency(tester);
  }

  /// Pumps frames with elapsed time so the default 200ms MaterialApp theme
  /// crossfade completes (and fake-client microtasks flush).
  Future<void> settle(WidgetTester tester) async {
    for (var i = 0; i < 3; i++) {
      await tester.pump(const Duration(milliseconds: 250));
    }
  }

  IconData toggleIcon(WidgetTester tester) {
    final tooltip = app.monitorVisible
        ? 'Hide resource monitor'
        : 'Show resource monitor';
    final button = tester.widget<IconButton>(
      find.ancestor(
        of: find.byTooltip(tooltip),
        matching: find.byType(IconButton),
      ).first,
    );
    return (button.icon as Icon).icon!;
  }

  int cpuSpots(WidgetTester tester) {
    final card = find.descendant(
      of: find.byWidgetPredicate(
          (w) => w is MonitorCard && w.title == 'CPU Usage'),
      matching: find.byType(LineChart),
    );
    return tester.widget<LineChart>(card).data.lineBarsData.first.spots.length;
  }

  tearDown(() {
    monitor.dispose();
    events.close();
    app.dispose();
  });

  group('T019 topbar monitor toggle (US2)', () {
    testWidgets(
        '(a) first launch: the toggle sits next to the theme toggle and the '
        'panel is visible', (tester) async {
      SharedPreferences.setMockInitialValues(const {});
      await bootAndPump(tester);
      await settle(tester);

      expect(app.monitorVisible, isTrue,
          reason: 'no stored pref keeps the visible default');
      expect(find.byType(ResourceMonitorPanel), findsOneWidget,
          reason: 'the panel is on by default (FR-004)');

      // Next to the theme toggle in the topbar Row.
      final row = tester.widget<Row>(
          find.ancestor(
            of: find.byTooltip('Hide resource monitor'),
            matching: find.byType(Row),
          ).first);
      final children = row.children.toList();
      int indexOf(String tooltip) => children
          .indexWhere((w) => w is IconButton && w.tooltip == tooltip);
      expect(indexOf('Hide resource monitor'), indexOf('Toggle theme') - 1,
          reason: 'the monitor toggle is next to the theme toggle');
    });

    testWidgets(
        '(b) the toggle hides and re-shows the panel; the icon reflects the '
        'state; re-show restores the full ~60 s history (US2-AS5)',
        (tester) async {
      SharedPreferences.setMockInitialValues(const {});
      await bootAndPump(tester);
      await settle(tester);

      // Build a full ~60 s window at one sample per second.
      for (var i = 0; i < 60; i++) {
        monitor.recordSample(sample(i.toDouble()));
        await tester.pump();
      }
      expect(cpuSpots(tester), 60);

      final iconVisible = toggleIcon(tester);
      await tester.tap(find.byTooltip('Hide resource monitor'));
      await settle(tester);

      expect(app.monitorVisible, isFalse);
      expect(find.byType(ResourceMonitorPanel), findsNothing,
          reason: 'the panel unmounts when hidden');
      expect(
          (await SharedPreferences.getInstance()).getBool('monitor_visible'),
          isFalse,
          reason: 'the choice persists immediately (SC-002)');
      expect(toggleIcon(tester), isNot(iconVisible),
          reason: 'the icon reflects the hidden state');

      // Sampling continues while hidden (FR-003): five more seconds pass.
      for (var i = 60; i < 65; i++) {
        monitor.recordSample(sample(i.toDouble()));
        await tester.pump();
      }

      await tester.tap(find.byTooltip('Show resource monitor'));
      await settle(tester);

      expect(app.monitorVisible, isTrue);
      expect(find.byType(ResourceMonitorPanel), findsOneWidget,
          reason: 're-showing remounts the panel');
      expect(cpuSpots(tester), 60,
          reason: 'the full capped 60 s window comes back from the controller');
      expect(find.text('64.0'), findsOneWidget,
          reason: 'the re-shown CPU card shows the live latest value');
      expect(toggleIcon(tester), iconVisible,
          reason: 'the icon is back to the visible state');
    });

    testWidgets('(c) the choice survives a restart (SC-002)', (tester) async {
      SharedPreferences.setMockInitialValues(const {'monitor_visible': false});
      await bootAndPump(tester);
      await settle(tester);
      expect(app.monitorVisible, isFalse);
      expect(find.byType(ResourceMonitorPanel), findsNothing,
          reason: 'the stored hidden choice restores on restart');

      // Restart with the stored visible choice.
      SharedPreferences.setMockInitialValues(const {'monitor_visible': true});
      app.dispose();
      monitor.dispose();
      events.close();
      await bootAndPump(tester);
      await settle(tester);
      expect(app.monitorVisible, isTrue);
      expect(find.byType(ResourceMonitorPanel), findsOneWidget,
          reason: 'the stored visible choice restores on restart');
    });
  });
}
