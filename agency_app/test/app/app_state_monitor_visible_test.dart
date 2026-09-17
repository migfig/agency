import 'package:agencyapp/app/app_state.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// T018 — `AppState.monitorVisible` (US2, FR-004/SC-002,
/// contracts/ui.md "Preferences"): a fresh [AppState] over the same prefs
/// restores the stored bool, a missing key defaults to `true` (first launch
/// shows the panel), and [AppState.setMonitorVisible] persists the
/// `monitor_visible` key immediately.
void main() {
  group('AppState.monitorVisible (US2)', () {
    test('missing key defaults to true (visible on first launch)', () async {
      SharedPreferences.setMockInitialValues(const {});
      final app = AppState();
      await app.init();
      expect(app.monitorVisible, isTrue,
          reason: 'no stored monitor_visible keeps the visible default');
    });

    test('a fresh AppState over the same prefs restores the stored bool',
        () async {
      SharedPreferences.setMockInitialValues(const {});
      final first = AppState();
      await first.init();
      first.setMonitorVisible(false);

      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getBool('monitor_visible'), isFalse,
          reason: 'the choice is written to shared_preferences');

      final second = AppState();
      await second.init();
      expect(second.monitorVisible, isFalse,
          reason: 'a fresh state over the same prefs restores false');

      SharedPreferences.setMockInitialValues(const {'monitor_visible': true});
      final third = AppState();
      await third.init();
      expect(third.monitorVisible, isTrue,
          reason: 'a fresh state over the same prefs restores true');
    });

    test('setMonitorVisible persists the monitor_visible key and notifies',
        () async {
      SharedPreferences.setMockInitialValues(const {});
      final app = AppState();
      await app.init();
      var notifications = 0;
      app.addListener(() => notifications++);

      app.setMonitorVisible(false);
      expect(app.monitorVisible, isFalse);
      expect(notifications, 1);
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getBool('monitor_visible'), isFalse,
          reason: 'the choice persists immediately (SC-002)');

      app.setMonitorVisible(true);
      expect(app.monitorVisible, isTrue);
      expect(prefs.getBool('monitor_visible'), isTrue);

      app.setMonitorVisible(true);
      expect(notifications, 2,
          reason: 'setting the same value again does not re-notify');
    });
  });
}
