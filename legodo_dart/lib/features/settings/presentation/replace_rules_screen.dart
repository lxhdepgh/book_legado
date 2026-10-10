/// 替换规则管理页：全局正文替换规则 CRUD + 全局开关。
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';

class ReplaceRulesScreen extends ConsumerStatefulWidget {
  const ReplaceRulesScreen({super.key});

  @override
  ConsumerState<ReplaceRulesScreen> createState() => _ReplaceRulesScreenState();
}

class _ReplaceRulesScreenState extends ConsumerState<ReplaceRulesScreen> {
  List<Map<String, Object?>> _rules = [];
  bool _globalEnabled = true;
  bool _loading = false;

  @override
  void initState() {
    super.initState();
    _loadRules();
  }

  Future<void> _loadRules() async {
    setState(() => _loading = true);
    try {
      final repo = ref.read(engineRepositoryProvider);
      final rules = await repo.replaceList();
      setState(() {
        _rules = rules;
        _loading = false;
      });
    } catch (e) {
      setState(() => _loading = false);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('加载失败：$e')),
        );
      }
    }
  }

  Future<void> _addRule() async {
    final nameController = TextEditingController();
    final patternController = TextEditingController();
    final replacementController = TextEditingController();
    var enabled = true;

    final result = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('新建替换规则'),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                controller: nameController,
                decoration: const InputDecoration(labelText: '规则名称'),
              ),
              TextField(
                controller: patternController,
                decoration: const InputDecoration(labelText: '查找内容（正则）'),
              ),
              TextField(
                controller: replacementController,
                decoration: const InputDecoration(labelText: '替换为'),
              ),
              SwitchListTile(
                title: const Text('启用'),
                value: enabled,
                onChanged: (v) => enabled = v,
              ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('取消'),
          ),
          ElevatedButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('保存'),
          ),
        ],
      ),
    );

    nameController.dispose();
    patternController.dispose();
    replacementController.dispose();

    if (result == true) {
      try {
        final repo = ref.read(engineRepositoryProvider);
        await repo.replaceAdd(
          name: nameController.text,
          pattern: patternController.text,
          replacement: replacementController.text,
          enabled: enabled,
        );
        await _loadRules();
      } catch (e) {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text('添加失败：$e')),
          );
        }
      }
    }
  }

  Future<void> _deleteRule(String ruleId) async {
    try {
      final repo = ref.read(engineRepositoryProvider);
      await repo.replaceDelete(ruleId);
      await _loadRules();
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('删除失败：$e')),
        );
      }
    }
  }

  Future<void> _toggleGlobal(bool value) async {
    try {
      final repo = ref.read(engineRepositoryProvider);
      await repo.replaceToggleGlobal(value);
      setState(() => _globalEnabled = value);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('切换失败：$e')),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(
        title: const Text('替换规则'),
        actions: [
          IconButton(
            icon: const Icon(Icons.add),
            tooltip: '新建规则',
            onPressed: _addRule,
          ),
        ],
      ),
      body: Column(
        children: [
          SwitchListTile(
            title: const Text('全局替换生效'),
            subtitle: const Text('关闭后不再对正文应用替换规则'),
            value: _globalEnabled,
            onChanged: _toggleGlobal,
          ),
          const Divider(height: 1),
          Expanded(
            child: _loading
                ? const Center(child: CircularProgressIndicator())
                : _rules.isEmpty
                    ? const Center(child: Text('暂无规则'))
                    : ListView.builder(
                        itemCount: _rules.length,
                        itemBuilder: (ctx, i) {
                          final rule = _rules[i];
                          final ruleId = rule['id']?.toString() ?? '';
                          final name = rule['name']?.toString() ?? '(未命名)';
                          final pattern = rule['pattern']?.toString() ?? '';
                          final replacement =
                              rule['replacement']?.toString() ?? '';
                          final enabled = rule['enabled'] as bool? ?? true;

                          return ListTile(
                            leading: Switch(
                              value: enabled,
                              onChanged: (v) async {
                                // TODO: 更新单个规则的启用状态
                              },
                            ),
                            title: Text(name),
                            subtitle: Text('$pattern → $replacement'),
                            trailing: IconButton(
                              icon: const Icon(Icons.delete_outline),
                              tooltip: '删除',
                              onPressed: () => _deleteRule(ruleId),
                            ),
                          );
                        },
                      ),
          ),
        ],
      ),
    );
  }
}
