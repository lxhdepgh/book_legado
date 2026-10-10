/// 底部那一行状态（四屏共用一份实现）。
///
/// flet 那边每一屏底部都只有一个槽：动作的回报（"已删除 3 条书源"）优先占位，
/// 没有回报时才回这屏自己的常态读数（"书架共 N 本"/"共 N 条 | 启用…"）。
/// 四屏各抄一份就会飘成四种"加载中/失败了说什么"——而"失败"那一格是这套迁移
/// 最不能写错的一处：**加载失败不许报成"共 0 条"**（那是把"没拿到数据"说成"没有数据"）。
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

/// 底部状态行。[message] 非空时就是它，否则按 [state] 的形态回落到默认文案。
class StateBar extends StatelessWidget {
  const StateBar({
    required this.message,
    required this.state,
    required this.failedText,
    required this.loadingText,
    this.dataText,
    this.isError = false,
    super.key,
  });

  /// 动作回报（'' = 这一拍没有刚做完的事要回声）。
  final String message;

  /// 这一句是**坏消息**吗：外观那一屏的 `set_status(..., error=True)` 要用红字
  /// （`appearance_view.py:233-235`），其余三屏的状态行不分色。
  final bool isError;

  /// 这一屏的数据形态：只用来分"还没到/失败了/到了"。
  final AsyncValue<Object?> state;

  /// 失败/加载中文案由调用方给：每一屏失败的说法是自己的（守卫测试从 flet 源码抽那三句）。
  final String failedText;
  final String loadingText;

  /// 数据到位时的那一句；null 表示这一屏没有常态读数。
  final String? dataText;

  String get text => message.isNotEmpty
      ? message
      : switch (state) {
          AsyncError() => failedText,
          _ when state.value == null => loadingText,
          _ => dataText ?? message,
        };

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        child: Text(text,
            style: Theme.of(context)
                .textTheme
                .bodySmall
                ?.copyWith(color: isError ? scheme.error : null)),
      ),
    );
  }
}
