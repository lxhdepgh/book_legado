/// 更多工具入口页（M7 后导航精简：工具组折叠为单一入口）。
///
/// 工具域十个页面不在侧栏直接展示，统一收进本页卡片；点击经 GroupTabs 切换到同组的对应页签。
library;

import 'package:flutter/material.dart';

import '../../../core/widgets/tabbed_group.dart';

/// 工具卡片清单：(标签, 图标, 说明)。
const _tools = <({String label, IconData icon, String desc})>[
  (label: '浏览器', icon: Icons.public, desc: '通用内置浏览器（交互 / 无头 / 渲染）'),
  (label: '词典', icon: Icons.language, desc: '划词取词，查词结果弹窗'),
  (label: '替换规则', icon: Icons.find_replace, desc: '全局正文替换规则管理'),
  (label: '字体', icon: Icons.text_fields, desc: '系统字体枚举 / 用户字体'),
  (label: '二维码', icon: Icons.qr_code_2, desc: '二维码生成 / 识别导入'),
  (label: '导出', icon: Icons.download_for_offline, desc: '书籍导出 epub / txt'),
  (label: '备份', icon: Icons.upload_file, desc: 'WebDAV 备份 / 恢复'),
  (label: '更新', icon: Icons.system_update, desc: '检查更新 / 下载安装包'),
  (label: '网络桥', icon: Icons.lan, desc: '本机 HTTP/WebSocket 书源管理 / 调试'),
  (label: '朗读', icon: Icons.settings_voice, desc: '听书语音档（系统 / edge / HTTP-TTS）'),
];

class ToolsOverviewScreen extends StatelessWidget {
  const ToolsOverviewScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final tabs = GroupTabsScope.maybeOf(context);

    return SingleChildScrollView(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('更多工具',
              style: theme.textTheme.headlineMedium?.copyWith(
                fontWeight: FontWeight.bold,
              )),
          const SizedBox(height: 8),
          Text('浏览器、词典、替换规则等工具集中在这里，点卡片进入。',
              style: theme.textTheme.bodyMedium),
          const Divider(height: 32),
          Wrap(
            spacing: 12,
            runSpacing: 12,
            children: _tools.map((tool) => _buildCard(context, tabs, tool)).toList(),
          ),
        ],
      ),
    );
  }

  Widget _buildCard(
    BuildContext context,
    GroupTabs? tabs,
    ({String label, IconData icon, String desc}) tool,
  ) {
    final theme = Theme.of(context);
    return InkWell(
      onTap: () => tabs?.select(tool.label),
      borderRadius: BorderRadius.circular(12),
      child: Container(
        width: 280,
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: theme.colorScheme.surfaceContainerHighest,
          borderRadius: BorderRadius.circular(12),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(tool.icon, size: 26, color: theme.colorScheme.primary),
            const SizedBox(height: 8),
            Text(tool.label,
                style: theme.textTheme.titleMedium?.copyWith(
                  fontWeight: FontWeight.w600,
                )),
            const SizedBox(height: 4),
            Text(tool.desc,
                style: theme.textTheme.bodySmall?.copyWith(
                  color: theme.colorScheme.onSurfaceVariant,
                )),
          ],
        ),
      ),
    );
  }
}
