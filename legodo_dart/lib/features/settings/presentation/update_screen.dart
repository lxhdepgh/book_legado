/// 更新检查屏幕（M5，契约 §1.17）：显示当前版本、检查更新按钮、更新信息展示。
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';
import '../data/update_models.dart';

class UpdateScreen extends ConsumerStatefulWidget {
  const UpdateScreen({super.key});

  @override
  ConsumerState<UpdateScreen> createState() => _UpdateScreenState();
}

class _UpdateScreenState extends ConsumerState<UpdateScreen> {
  String _currentVersion = '未知';
  UpdateInfo? _updateInfo;
  bool _checking = false;
  String _status = '';

  @override
  void initState() {
    super.initState();
    _loadCurrentVersion();
  }

  Future<void> _loadCurrentVersion() async {
    try {
      final repo = ref.read(engineRepositoryProvider);
      final info = await repo.hello();
      setState(() {
        _currentVersion = info.build.isEmpty ? '未知' : info.build;
      });
    } catch (_) {
      // 握手失败不影响界面显示
    }
  }

  Future<void> _checkUpdate() async {
    if (_checking) return;
    setState(() {
      _checking = true;
      _status = '正在检查更新...';
      _updateInfo = null;
    });

    try {
      final repo = ref.read(engineRepositoryProvider);
      final info = await repo.updateCheck();
      setState(() {
        _checking = false;
        if (info != null) {
          _updateInfo = info;
          _status = '';
        } else {
          _status = '已是最新版本';
        }
      });
    } catch (e) {
      setState(() {
        _checking = false;
        _status = '检查失败：$e';
      });
    }
  }

  Future<void> _ignoreVersion() async {
    final info = _updateInfo;
    if (info == null) return;
    try {
      final repo = ref.read(engineRepositoryProvider);
      await repo.updateIgnore(info.version);
      setState(() {
        _updateInfo = null;
        // 版本号先抓下来再用：这一格刚置 null，`_updateInfo!.version` 会把
        // "忽略成功"这句本身炸成 Null check operator 红屏。
        _status = '已忽略版本 ${info.version}';
      });
    } catch (e) {
      setState(() {
        _status = '忽略失败：$e';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('检查更新',
              style: theme.textTheme.titleLarge?.copyWith(
                fontWeight: FontWeight.bold,
              )),
          const SizedBox(height: 8),
          Text('当前版本：$_currentVersion',
              style: theme.textTheme.bodySmall?.copyWith(
                color: theme.colorScheme.onSurfaceVariant,
              )),
          const SizedBox(height: 16),
          ElevatedButton.icon(
            onPressed: _checking ? null : _checkUpdate,
            icon: _checking
                ? const SizedBox(
                    width: 16,
                    height: 16,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.refresh),
            label: const Text('检查更新'),
          ),
          const SizedBox(height: 16),
          if (_updateInfo != null) _buildUpdateCard(theme),
          if (_status.isNotEmpty)
            Text(_status,
                style: theme.textTheme.bodySmall?.copyWith(
                  color: theme.colorScheme.onSurfaceVariant,
                )),
        ],
      ),
    );
  }

  Widget _buildUpdateCard(ThemeData theme) {
    final info = _updateInfo!;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('发现新版本：${info.version}',
                style: theme.textTheme.titleMedium?.copyWith(
                  fontWeight: FontWeight.w600,
                )),
            const SizedBox(height: 8),
            Text(info.notes.isEmpty ? '（暂无更新说明）' : info.notes,
                style: theme.textTheme.bodyMedium),
            const SizedBox(height: 12),
            Row(
              children: [
                ElevatedButton.icon(
                  onPressed: () {
                    // TODO: 打开下载链接（需要 url_launcher 插件）
                    _status = '下载功能待实现';
                    setState(() {});
                  },
                  icon: const Icon(Icons.download),
                  label: const Text('下载到下载目录'),
                ),
                const SizedBox(width: 8),
                TextButton(
                  onPressed: _ignoreVersion,
                  child: const Text('忽略此版本'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
