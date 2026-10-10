/// 外观偏好：`theme.preferences` / `theme.save` 的那三格 `{skin_id, mode, eink}`。
///
/// 键名取自真响应（`tests/gateway/golden/theme.preferences.json` 的 `field_names`）。
/// 皮肤表照 `app/themes/builtin_themes.py:40-53` 的 `BUILTIN_SKINS`——**顺序也算内容**：
/// flet 那个下拉就是按声明序建的（`appearance_view.py:116-117`），换序等于换默认观感。
/// 跨语言守卫在 `tests/test_gateway_contract_single_source.py` 逐 id/逐名比对，
/// 所以这张表不是"抄一遍好看"，是抄错了会被拽红的那一份。
library;

/// 皮肤：id + 给人看的名字 + 一句说明 + 浅色档的主色。
class AppSkin {
  const AppSkin({
    required this.id,
    required this.name,
    required this.description,
    required this.primary,
  });

  final String id;
  final String name;
  final String description;

  /// 浅色档 primary（`builtin_themes.py` 里 `_skin(...)` 的第四参）。
  /// Flutter 侧它是 `ColorScheme.fromSeed` 的种子；flet 侧它是 `Palette.primary`。
  final int primary;
}

const List<AppSkin> kAppSkins = <AppSkin>[
  AppSkin(
      id: 'classic_blue',
      name: '经典蓝',
      description: '稳定、中性的默认皮肤',
      primary: 0xFF3F51B5),
  AppSkin(
      id: 'emerald_green',
      name: '翡翠绿',
      description: '柔和，适合长时间使用',
      primary: 0xFF2E7D6B),
  AppSkin(
      id: 'warm_amber',
      name: '暖琥珀',
      description: '温暖、低刺激',
      primary: 0xFF9A6700),
  AppSkin(
      id: 'elegant_purple',
      name: '雅致紫',
      description: '轻松而有辨识度',
      primary: 0xFF6D5BD0),
  AppSkin(
      id: 'ink_gray',
      name: '水墨灰',
      description: '低饱和的管理界面',
      primary: 0xFF4F5B62),
];

/// 表外的 id 退回默认皮肤（服务端 `theme_service.py:168-169` 也是这么归一的）：
/// 界面遇到没见过的皮肤不许白屏，也不许把 id 当名字显示。
AppSkin skinById(String id) =>
    kAppSkins.firstWhere((skin) => skin.id == id, orElse: () => kAppSkins.first);

/// 默认皮肤 id（`theme_service.py:35` 的 `DEFAULT_SKIN`）。
const String kDefaultSkinId = 'classic_blue';

enum ThemeModeKind {
  system,
  light,
  dark;

  /// 线上值（`VALID_MODES`）。表外的值归一到 system（`theme_service.py:37`）。
  String get wireName => name;

  static ThemeModeKind fromWire(String? value) => ThemeModeKind.values.firstWhere(
        (mode) => mode.wireName == value,
        orElse: () => ThemeModeKind.system,
      );

  /// 单选按钮那三个标签（`appearance_view.py:104-106`）。
  String get label => switch (this) {
        ThemeModeKind.system => '跟随系统',
        ThemeModeKind.light => '浅色',
        ThemeModeKind.dark => '深色',
      };
}

/// 一份外观偏好。不可变——"改一格"走 [copy]，屏幕上的待存态与已存态才是两个对象。
class ThemePreferences {
  const ThemePreferences({
    required this.skinId,
    required this.mode,
    required this.eink,
  });

  final String skinId;
  final ThemeModeKind mode;

  /// 墨水屏：纯黑白高对比。服务端在 eink 时会把 mode 归一成 light
  /// （`theme_service.py:157-160`），所以这一格**赢过** mode，界面上不用解释为什么。
  final bool eink;

  AppSkin get skin => skinById(skinId);

  ThemePreferences copy({String? skinId, ThemeModeKind? mode, bool? eink}) =>
      ThemePreferences(
        skinId: skinId ?? this.skinId,
        mode: mode ?? this.mode,
        eink: eink ?? this.eink,
      );

  factory ThemePreferences.fromJson(Map<String, Object?> json) => ThemePreferences(
        skinId: '${json['skin_id'] ?? kDefaultSkinId}',
        mode: ThemeModeKind.fromWire('${json['mode'] ?? 'system'}'),
        eink: json['eink'] == true,
      );

  Map<String, Object?> toJson() =>
      <String, Object?>{'skin_id': skinId, 'mode': mode.wireName, 'eink': eink};

  static const ThemePreferences defaults = ThemePreferences(
      skinId: kDefaultSkinId, mode: ThemeModeKind.system, eink: false);

  @override
  bool operator ==(Object other) =>
      other is ThemePreferences &&
      other.skinId == skinId &&
      other.mode == mode &&
      other.eink == eink;

  @override
  int get hashCode => Object.hash(skinId, mode, eink);

  @override
  String toString() => 'ThemePreferences($skinId, ${mode.wireName}, eink=$eink)';
}
