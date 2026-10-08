/// 外观那一屏（对齐 flet `app/views/settings/appearance_view.py`）。
///
/// 三块：模式单选（跟随系统/浅色/深色）+ 墨水屏开关 + 五个命名皮肤，
/// 底下是"实时预览"那一小组真实控件，最末是「恢复默认 / 保存并应用」。
///
/// **没有"应用背景图"那一段**：flet 那一屏有，但协议面上没有对应的动作
/// （`source.*`/`settings.*`/`theme.*` 三域 31 发里没有一条读写背景图，
/// 选图还要 `file_picker`）。放一个点开什么都不发生的按钮，比不放更糟。
///
/// 预览与保存是两件事（`appearance_controller.py` 的 pending/saved，见
/// `domain/appearance_notifier.dart`）：这一屏改任何一格都立刻反映到整个窗口
/// —— MaterialApp 的主题就是 `ref.watch(appearanceProvider)` 的 pending
/// （`app/bootstrap.dart`），所以"预览"不需要自己再造一套局部主题。
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/widgets/failure_banner.dart';
import '../../../core/widgets/state_bar.dart';
import '../data/theme_preferences.dart';
import '../domain/appearance_notifier.dart';

class AppearanceScreen extends ConsumerWidget {
  const AppearanceScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final state = ref.watch(appearanceProvider);
    final notifier = ref.read(appearanceProvider.notifier);
    final pref = state.value?.pending ?? ThemePreferences.defaults;
    final saved = state.value?.saved;
    final skin = pref.skin;

    return Scaffold(
      appBar: AppBar(
        title: const Text('外观设置'),
        actions: <Widget>[
          IconButton(
            key: const Key('appearance.reload'),
            icon: const Icon(Icons.refresh),
            tooltip: '重新载入偏好',
            onPressed: () => ref.invalidate(appearanceProvider),
          ),
        ],
      ),
      body: Column(
        children: <Widget>[
          FailureBanner(stream: notifier.failures),
          Expanded(
            child: ListView(
              padding: const EdgeInsets.all(24),
              children: <Widget>[
                Text('外观设置',
                    style: Theme.of(context).textTheme.headlineMedium),
                const SizedBox(height: 4),
                const Text('应用皮肤与阅读主题相互独立；主题应用到整个应用窗口。'),
                const Divider(),
                const _Section('外观模式'),
                // `RadioGroup` 而不是三个 `Radio` 各带 groupValue/onChanged：
                // 后一种写法在 3.47 里已标废弃（组值由 RadioGroup 统一持）。
                RadioGroup<ThemeModeKind>(
                  groupValue: pref.mode,
                  // 墨水屏时模式那一格**照样可点**：它是待存的意图，
                  // 关掉墨水屏就按它生效（服务端只在 eink 落库时折成 light）。
                  onChanged: (mode) {
                    if (mode != null) notifier.changeMode(mode);
                  },
                  // wrap 而不是 Row：flet 那边就是 `Row(wrap=True)`，
                  // 窄窗口里三根标签挤成一排会溢出（同一条记忆里的窄窗契约）。
                  child: Wrap(
                    spacing: 12,
                    runSpacing: 4,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    children: <Widget>[
                      for (final mode in ThemeModeKind.values)
                        _LabeledRadio<ThemeModeKind>(
                          key: Key('appearance.mode.${mode.wireName}'),
                          value: mode,
                          label: mode.label,
                        ),
                    ],
                  ),
                ),
                // 开关始终可点（`appearance_view.py:208-210`：曾把 disabled 写成
                // `bool(eink)`，于是墨水屏一开就再也关不掉）。
                Row(
                  children: <Widget>[
                    Expanded(
                        child: Text('墨水屏（纯黑白高对比）',
                            style: Theme.of(context).textTheme.bodyLarge)),
                    Switch(
                      key: const Key('appearance.eink'),
                      value: pref.eink,
                      onChanged: notifier.changeEink,
                    ),
                  ],
                ),
                const _Section('应用皮肤'),
                DropdownButton<String>(
                  key: const Key('appearance.skin'),
                  isExpanded: true,
                  value: skin.id,
                  items: <DropdownMenuItem<String>>[
                    for (final item in kAppSkins)
                      DropdownMenuItem(value: item.id, child: Text(item.name)),
                  ],
                  onChanged: (id) {
                    if (id != null) notifier.changeSkin(id);
                  },
                ),
                Text(skin.description,
                    style: Theme.of(context)
                        .textTheme
                        .bodySmall
                        ?.copyWith(color: Theme.of(context).colorScheme.onSurfaceVariant)),
                const Divider(),
                const _Preview(),
                if (saved != null && pref != saved)
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Text('有未保存的改动',
                        style: Theme.of(context).textTheme.bodySmall),
                  ),
                Row(
                  mainAxisAlignment: MainAxisAlignment.end,
                  children: <Widget>[
                    TextButton(
                      key: const Key('appearance.reset'),
                      onPressed: notifier.reset,
                      child: const Text('恢复默认'),
                    ),
                    const SizedBox(width: 8),
                    FilledButton.icon(
                      key: const Key('appearance.save'),
                      onPressed: notifier.save,
                      icon: const Icon(Icons.save),
                      label: const Text('保存并应用'),
                    ),
                  ],
                ),
              ],
            ),
          ),
          StateBar(
            message: state.value?.status ?? '',
            state: state,
            isError: state.value?.statusIsError ?? false,
            failedText: '读取外观偏好失败',
            loadingText: '正在读取外观偏好…',
          ),
        ],
      ),
    );
  }
}

class _Section extends StatelessWidget {
  const _Section(this.title);

  final String title;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(top: 12, bottom: 6),
        child: Text(title, style: Theme.of(context).textTheme.titleMedium),
      );
}

/// 一根带标签的单选。
///
/// 标签自己也要点得动：flet 那边是 `ft.Radio(label=…)`，整行都是命中区；
/// Flutter 的 `Radio` 只有那颗圆点，所以名字挂上去得补一发 `onTap`
/// ——不然"点'深色'两个字没反应"就是界面上最不像 bug 的 bug。
class _LabeledRadio<T> extends StatelessWidget {
  const _LabeledRadio({required this.value, required this.label, super.key});

  final T value;
  final String label;

  @override
  Widget build(BuildContext context) {
    final group = RadioGroup.maybeOf<T>(context);
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: <Widget>[
        Radio<T>(value: value),
        GestureDetector(onTap: () => group?.onChanged(value), child: Text(label)),
      ],
    );
  }
}

/// 实时预览那一小组：一屏的缩样，控件全是真的（配色来自整页主题）。
///
/// flet 那边这一组原先是三块手写 `bgcolor` 的色板，换成真实控件后
/// "换了主题这里跟着变"才是**看得见的**（`appearance_view.py:136-139` 那条注释）。
class _Preview extends StatelessWidget {
  const _Preview();

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Card(
      color: scheme.surfaceContainerLow,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Text('主题预览', style: Theme.of(context).textTheme.titleLarge),
            Text('正文、按钮、输入框与分隔线随选择即时变化。',
                style: Theme.of(context)
                    .textTheme
                    .bodyMedium
                    ?.copyWith(color: scheme.onSurfaceVariant)),
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: <Widget>[
                FilledButton.icon(
                  onPressed: null,
                  icon: const Icon(Icons.check),
                  label: const Text('主要操作'),
                ),
                const OutlinedButton(onPressed: null, child: Text('次要操作')),
                const Chip(label: Text('状态标签')),
              ],
            ),
            const SizedBox(height: 12),
            const TextField(
              enabled: false,
              decoration: InputDecoration(
                  labelText: '输入框预览', hintText: '搜索书名或作者'),
            ),
            const Divider(),
          ],
        ),
      ),
    );
  }
}
