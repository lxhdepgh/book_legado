/// 外观那一屏的状态机（§6.2：`AsyncNotifier`；对照 `appearance_controller.py`）。
///
/// 三条是从 flet 那边照搬的语义，不是这里选的：
///
/// 1. **pending 与 saved 是两个对象**（`:70-81` 的 `self._pending`）。
///    改皮肤/切深色/开墨水屏都只动 pending 并立刻预览（`change_*` 三发都是
///    `pending → reflect → preview`，一次落库都没有）；点「保存并应用」才 `theme.save`。
///    "改了就是生效，但没保存"与"没保存就别改"是两种产品，这里是前一种。
/// 2. **恢复默认不动背景图**（`:145-150` 明写：把用户刚选的图连带清掉是最容易被当成丢数据的意外）。
///    G2 这一屏根本没有背景图那一行（协议面上没有对应 action），所以这里连"跳过"都不用写。
/// 3. **保存的回话就是新的已存态**（`theme.save` 返回的是服务端归一后的三格：
///    墨水屏会把 mode 折成 light，`theme_service.py:157-160`）。界面上拿自己那份
///    pending 冒充"已保存"，就会出现"开关写着 dark、库里存的是 light"。
library;

import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';
import '../../../core/rpc/envelope.dart';
import '../data/theme_preferences.dart';

final AsyncNotifierProvider<AppearanceNotifier, AppearanceState>
    appearanceProvider =
    AsyncNotifierProvider<AppearanceNotifier, AppearanceState>(
  AppearanceNotifier.new,
  retry: (_, _) => null,
);

class AppearanceNotifier extends AsyncNotifier<AppearanceState> {
  final StreamController<EngineFailure> _failures =
      StreamController<EngineFailure>.broadcast();

  Stream<EngineFailure> get failures => _failures.stream;

  @override
  Future<AppearanceState> build() async {
    ref.onDispose(() => unawaited(_failures.close()));
    try {
      final saved = await ref.read(engineRepositoryProvider).themePreferences();
      return AppearanceState(saved: saved, pending: saved);
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      // 读不回偏好不等于界面不能开：回落默认值，但**这句要写在状态行上**
      // （flet `_preferences:82-89` 捕获后回 `{}`，也就是静默回落——那一处是它的弱点，
      // 不是要照搬的行为：用户会以为"我存的皮肤没存上"）。
      return AppearanceState(
        saved: ThemePreferences.defaults,
        pending: ThemePreferences.defaults,
        status: '读取外观偏好失败，已按默认显示：${exc.message}',
        statusIsError: true,
      );
    }
  }

  AppearanceState get _current =>
      state.value ??
      AppearanceState(
          saved: ThemePreferences.defaults,
          pending: ThemePreferences.defaults);

  void _emit(AppearanceState next) => state = AsyncValue<AppearanceState>.data(next);

  /// 驱动 MaterialApp 的那一份：**待存的赢过已存的**（预览即所见）。
  /// provider 还没落值时用默认档，不拿 null 去把主题清空。
  ThemePreferences get effective => _current.pending;

  // ------------------------------------------------------------------ #
  // 改动（只动 pending，立刻预览，不落库）
  // ------------------------------------------------------------------ #
  void changeMode(ThemeModeKind mode) => _touch((p) => p.copy(mode: mode));

  void changeSkin(String skinId) => _touch((p) => p.copy(skinId: skinId));

  /// 墨水屏开关**始终可点**（`appearance_view.py:208-210` 那条：曾经把 disabled 写成
  /// `bool(eink)`，于是墨水屏一开就再也关不掉，只能连其它自定义一起"恢复默认"）。
  void changeEink(bool eink) => _touch((p) => p.copy(eink: eink));

  void _touch(ThemePreferences Function(ThemePreferences) edit) {
    final pending = edit(_current.pending);
    // 状态行清空：改一次控件就把"已保存/保存失败"那一句留着，是在说一件已经过期的事。
    _emit(_current.copyWith(pending: pending, status: ''));
  }

  // ------------------------------------------------------------------ #
  // 保存 / 恢复默认
  // ------------------------------------------------------------------ #
  /// 落库：三格一起发（`theme.save` 的 payload 就是这个形状，黄金样本
  /// `tests/gateway/golden/theme.save.json` 的 request 三格都在）。
  Future<void> save() async {
    final pending = _current.pending;
    try {
      final saved = await ref.read(engineRepositoryProvider).saveTheme(
            skinId: pending.skinId,
            mode: pending.mode.wireName,
            eink: pending.eink,
          );
      _emit(AppearanceState(
          saved: saved, pending: saved, status: '外观设置已保存'));
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      _emit(_current.copyWith(status: '保存失败：${exc.message}',
          statusIsError: true));
    }
  }

  /// 恢复默认：只重置 pending 并预览，**不落库**（`:145-150`）。
  /// 文案带着"保存后生效"——不然用户看到"已恢复默认"会以为库里已经写了。
  void reset() => _emit(_current.copyWith(
        pending: ThemePreferences.defaults,
        status: '已恢复默认外观（保存后生效；背景图不受影响）',
      ));
}

/// 已存的与待存的那两份，加状态行。
class AppearanceState {
  const AppearanceState({
    required this.saved,
    required this.pending,
    this.status = '',
    this.statusIsError = false,
  });

  /// 库里那一套。
  final ThemePreferences saved;

  /// 界面上那一套（未保存的改动在这里）。
  final ThemePreferences pending;

  final String status;
  final bool statusIsError;

  bool get dirty => pending != saved;

  AppearanceState copyWith({
    ThemePreferences? saved,
    ThemePreferences? pending,
    String? status,
    bool? statusIsError,
  }) =>
      AppearanceState(
        saved: saved ?? this.saved,
        pending: pending ?? this.pending,
        status: status ?? this.status,
        statusIsError: statusIsError ?? this.statusIsError,
      );
}
