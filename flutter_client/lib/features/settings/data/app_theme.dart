/// 皮肤 → Flutter 主题（`app/themes/builtin_themes.py` 那一份颜色表在 Dart 侧的落点）。
///
/// flet 那边是服务端算好一张颜色角色表（`ThemeService.apply()` 的 light/dark 两面）
/// 再让视图落成 `ft.Theme`；协议面上**没有**"把这张表要过来"那一发
/// （`theme.preferences` 只回 `{skin_id, mode, eink}` 三格），所以这里按种子色求值。
/// 两边同源的是**判据**，不是色号：
///
/// - 皮肤只换种子，浅色档用 `ColorScheme.fromSeed`，深色档用同一颗种子的 `Brightness.dark`
///   （flet 的 `_skin(...)` 也是"一个主色生成两面"，见 `builtin_themes.py`）；
/// - **墨水屏赢过模式**：`apply_page_theme:66-70` 那条——两面同一套色、强制 LIGHT，
///   因为深色桌面上"深色底 + 纯黑字"会糊成一片，而那正是墨水屏要的效果；
/// - `mode == 'system'` 交给平台，不自己判亮暗。
library;

import 'package:flutter/material.dart';

import 'theme_preferences.dart';

/// 浅色档。[eink] 时走最大对比档（见文件头第三条）。
ThemeData lightThemeFor(AppSkin skin, {bool eink = false}) {
  final scheme = ColorScheme.fromSeed(
    seedColor: Color(skin.primary),
    brightness: Brightness.light,
    contrastLevel: eink ? 2.0 : 0.0,
  );
  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    // flet 那边这三项是 scaffold/card/divider 的底色（`build_theme:51-53`）：
    // 少了它们，"换皮肤"只改到按钮，看不出皮肤换了。
    scaffoldBackgroundColor: scheme.surface,
    dividerColor: scheme.outline,
  );
}

/// 深色档。墨水屏不分亮暗（两面同一套），所以 eink 时回的就是浅色那一份。
ThemeData darkThemeFor(AppSkin skin, {bool eink = false}) => eink
    ? lightThemeFor(skin, eink: true)
    : ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(
          seedColor: Color(skin.primary),
          brightness: Brightness.dark,
        ),
      );

/// `theme_mode` 那一格（`appearance_view.py:26-29` 的 `_THEME_MODES`：
/// 只认 light/dark，其余含未知值一律跟随系统）。
ThemeMode appThemeModeFor(ThemePreferences pref) {
  if (pref.eink) return ThemeMode.light;
  return switch (pref.mode) {
    ThemeModeKind.light => ThemeMode.light,
    ThemeModeKind.dark => ThemeMode.dark,
    ThemeModeKind.system => ThemeMode.system,
  };
}

/// 一套 MaterialApp 主题三件套（flet 的 `apply_page_theme` 同名规矩：
/// **必须三件套一起下发**，只设 theme 而把 theme_mode 留在 system，在深色桌面上等于什么都没改）。
({ThemeData theme, ThemeData darkTheme, ThemeMode themeMode}) appThemeFor(
  ThemePreferences pref,
) =>
    (
      theme: lightThemeFor(pref.skin, eink: pref.eink),
      darkTheme: darkThemeFor(pref.skin, eink: pref.eink),
      themeMode: appThemeModeFor(pref),
    );
