/// 书源导入对话框：本地 JSON 文件（先预览后确认）+ 在线地址。
///
/// 形状对齐 flet 那屏（`app/views/source/import_view.py` +
/// `app/controllers/source/source_import_controller.py`），三条不是风格的选择：
///
/// 1. **选完文件先出预览，不直接落库**：flet 分层重构把预览整块删过一回，于是
///    「替换」这个不可撤销的动作只需要一次文件点击就生效了。
/// 2. **替换要二次确认**（控制器 `_replace_dialog`），且那句要报"现有多少条"——
///    人要看见自己被清掉的是什么才按下去。
/// 3. **`placeholder` 不许演成"成功 0 条"**（见 [ImportOutcome] 的注释）：这一类资源
///    今天没有存储落点，和"文件坏了所以一条没进"是两件事。
///
/// 类型（`kind`）不进界面——本屏只有书源这一条路有落点，服务侧仍收 kind 参数。
library;

import 'dart:convert';
import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';
import '../../../core/rpc/envelope.dart';
import '../../../core/rpc/engine_repository.dart';
import '../data/book_source_preview.dart';
import '../data/import_outcome.dart';
import '../domain/sources_notifier.dart';

/// 服务侧 kind 的中文名（`import_view.py` 的 `_KIND_LABELS`；英文标识不能直接给用户看）。
const Map<String, String> _kindLabels = <String, String>{'book_source': '书源'};

/// 结果卡上那一行汇总。占位单独一句，不并进"成功 N 条"那一句式里。
String importOutcomeLine(ImportOutcome outcome) {
  final label = _kindLabels[outcome.kind] ?? outcome.kind;
  if (outcome.placeholder) {
    return '$label未落地（占位）：这一类资源目前没有存储落点，本次一行都没写';
  }
  return '$label导入完成：成功 ${outcome.count} 条';
}

Future<void> showSourceImportDialog(BuildContext context, {required int existingCount}) =>
    showDialog<void>(
      context: context,
      builder: (context) => SourceImportDialog(existingCount: existingCount),
    );

class SourceImportDialog extends ConsumerStatefulWidget {
  const SourceImportDialog({required this.existingCount, super.key});

  /// 替换确认框里那句"完全替换现有 N 条"的 N（取不到当 0，不拦确认）。
  final int existingCount;

  @override
  ConsumerState<SourceImportDialog> createState() => _SourceImportDialogState();
}

class _SourceImportDialogState extends ConsumerState<SourceImportDialog> {
  static const String _kind = 'book_source';

  final TextEditingController _url = TextEditingController();

  /// 待确认的文件：预览只解析过它，落库要等用户点「确认导入」。
  ({String name, String text})? _pending;
  BookSourcePreview? _preview;
  String _mode = 'append';
  bool _busy = false;
  String _stage = '';
  ImportOutcome? _outcome;
  String? _failure;

  @override
  void dispose() {
    _url.dispose();
    super.dispose();
  }

  void _say(String message) => setState(() => _failure = message);

  // ------------------------------------------------------------------ #
  // 本地文件：选 → 读 → 预览（不落库）
  // ------------------------------------------------------------------ #
  Future<void> _pickFile() async {
    FilePickerResult? picked;
    try {
      picked = await FilePicker.platform.pickFiles(
        type: FileType.custom,
        allowedExtensions: const <String>['json'],
        withData: true,
      );
    } catch (exc) {
      _say('打开文件选择器失败：$exc');
      return;
    }
    final file = picked?.files.isEmpty ?? true ? null : picked!.files.single;
    if (file == null) return; // 用户取消：不写"导入完成 0 条"，也不清掉上一次的结果

    final name = file.path ?? file.name;
    setState(() {
      _busy = true;
      _stage = '正在读取文件…';
      _failure = null;
    });
    String text;
    try {
      final bytes = file.bytes;
      text = bytes != null
          ? utf8.decode(bytes, allowMalformed: true)
          : await File(file.path!).readAsString();
    } catch (exc) {
      setState(() {
        _busy = false;
        _stage = '';
      });
      _say('读取文件失败：$exc');
      return;
    }
    setState(() {
      _busy = false;
      _stage = '';
      _preview = BookSourcePreview.parse(text);
      // 读不出东西就没有可确认的对象：`_pending` 与预览同生同灭（flet 控制器同一条），
      // 留着它，「确认导入」那条路就有了一份没有预览可对证的文本。
      _pending = _preview!.canImport ? (name: name, text: text) : null;
      _outcome = null;
    });
  }

  void _cancelPreview() => setState(() {
        _pending = null;
        _preview = null;
        _mode = 'append';
      });

  // ------------------------------------------------------------------ #
  // 确认导入（预览卡上的动作）
  // ------------------------------------------------------------------ #
  Future<void> _confirmImport() async {
    final pending = _pending;
    if (pending == null || _busy) return;
    if (_mode == 'replace') {
      final confirmed = await _confirmReplace(pending);
      if (!confirmed) return;
    }
    await _run((repository) => repository.importText(
        text: pending.text, kind: _kind, mode: _mode),
        stage: _mode == 'replace' ? '正在替换书源列表…' : '正在解析并写入书源…');
  }

  Future<bool> _confirmReplace(({String name, String text}) pending) async {
    final preview = _preview ?? BookSourcePreview.parse(pending.text);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('替换全部现有书源？'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Text('${pending.name} 里的 ${preview.valid} 条书源将完全替换'
                '现有 ${widget.existingCount} 条。'),
            const SizedBox(height: 6),
            Text('此操作不可撤销。',
                style: TextStyle(color: Theme.of(context).colorScheme.error)),
          ],
        ),
        actions: <Widget>[
          TextButton(
              onPressed: () => Navigator.pop(context, false), child: const Text('取消')),
          FilledButton.tonal(
            key: const Key('sources.import.replaceConfirm'),
            onPressed: () => Navigator.pop(context, true),
            child: const Text('确认替换'),
          ),
        ],
      ),
    );
    return confirmed == true;
  }

  // ------------------------------------------------------------------ #
  // 在线地址（没有预览一步：文本还没到手）
  // ------------------------------------------------------------------ #
  Future<void> _importUrl() async {
    final url = _url.text.trim();
    if (url.isEmpty || _busy) return;
    setState(() {
      _busy = true;
      _stage = '正在下载 ${url.length > 60 ? '${url.substring(0, 60)}…' : url}…';
    });
    await _run((repository) => repository.importUrl(url: url, kind: _kind));
  }

  Future<void> _run(
      Future<ImportOutcome> Function(EngineRepository repository) send,
      {String stage = '正在解析并写入书源…'}) async {
    setState(() {
      _busy = true;
      _failure = null;
      _outcome = null;
      // 「正在下载 …」已经在 `_importUrl` 里写过一次，这里不覆盖它；文件那一路
      // 卡上是空的，由 `stage` 给——替换那一支的措辞不同（flet 的 `_start_import`）。
      if (_stage.isEmpty) _stage = stage;
    });
    try {
      final outcome = await send(ref.read(engineRepositoryProvider));
      if (!mounted) return;
      setState(() {
        _outcome = outcome;
        _pending = null;
        _preview = null;
      });
      if (outcome.count > 0) {
        // 收尾重取一次：书源屏也听 SourceChangedEvent，但那一发经轮询上来有延迟，
        // 用户点了导入就该立刻看见新列表。
        await ref.read(sourcesProvider.notifier).reload();
      }
    } on EngineFailure catch (exc) {
      if (!mounted) return;
      setState(() => _failure = '导入失败：${exc.message}');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final preview = _preview;
    final outcome = _outcome;
    final showPreviewCard = preview != null && preview.canImport && _pending != null;
    return AlertDialog(
      title: const Text('导入书源'),
      content: SizedBox(
        width: 520,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              const Text('从 Legado 格式的书源 JSON 文件或在线地址导入。'),
              const SizedBox(height: 10),
              Row(
                children: <Widget>[
                  FilledButton.tonalIcon(
                    key: const Key('sources.import.pickFile'),
                    onPressed: _busy ? null : _pickFile,
                    icon: const Icon(Icons.file_open_outlined),
                    label: const Text('选择本地 JSON 文件'),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(_pending?.name ?? '',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: Theme.of(context).textTheme.bodySmall),
                  ),
                ],
              ),
              if (preview != null && !preview.canImport)
                Padding(
                  padding: const EdgeInsets.only(top: 6),
                  child: Text(preview.summaryLine),
                ),
              if (showPreviewCard) _PreviewCard(
                preview: preview,
                mode: _mode,
                busy: _busy,
                onModeChanged: (mode) => setState(() => _mode = mode),
                onConfirm: _confirmImport,
                onCancel: _cancelPreview,
              ),
              const Divider(height: 24),
              Row(
                children: <Widget>[
                  Expanded(
                    child: TextField(
                      key: const Key('sources.import.urlField'),
                      controller: _url,
                      enabled: !_busy,
                      decoration: const InputDecoration(
                        isDense: true,
                        border: OutlineInputBorder(),
                        hintText: '书源 JSON 的地址，例如 https://…/bookSource.json',
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  FilledButton.icon(
                    key: const Key('sources.import.urlButton'),
                    onPressed: _busy ? null : _importUrl,
                    icon: const Icon(Icons.cloud_download_outlined),
                    label: const Text('导入'),
                  ),
                ],
              ),
              if (_busy || _outcome != null || _failure != null) const Divider(height: 24),
              if (_busy) ...<Widget>[
                const LinearProgressIndicator(minHeight: 4),
                const SizedBox(height: 6),
                Text(_stage, style: Theme.of(context).textTheme.bodySmall),
              ],
              if (!_busy && _failure != null) ...<Widget>[
                Text(_failure!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
              ],
              if (!_busy && outcome != null) ...<Widget>[
                Text(importOutcomeLine(outcome)),
                for (final line in <String>[
                  ...outcome.errors,
                  ...outcome.warnings,
                ])
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text('· $line',
                        style: const TextStyle(fontSize: 12, color: Colors.orange)),
                  ),
              ],
            ],
          ),
        ),
      ),
      actions: <Widget>[
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('关闭')),
      ],
    );
  }
}

class _PreviewCard extends StatelessWidget {
  const _PreviewCard({
    required this.preview,
    required this.mode,
    required this.busy,
    required this.onModeChanged,
    required this.onConfirm,
    required this.onCancel,
  });

  final BookSourcePreview preview;
  final String mode;
  final bool busy;
  final ValueChanged<String> onModeChanged;
  final Future<void> Function() onConfirm;
  final VoidCallback onCancel;

  static const Map<String, (String, String)> _modeLabels = <String, (String, String)>{
    'append': ('追加', '把文件里的书源并进现有列表（按书源地址去重）'),
    'replace': ('替换', '用文件里的书源整体覆盖现有列表，落库前会再确认一次'),
  };

  @override
  Widget build(BuildContext context) {
    final groupsLine = preview.groupsLine;
    return Container(
      width: double.infinity,
      margin: const EdgeInsets.only(top: 10),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        border: Border.all(color: Theme.of(context).colorScheme.outlineVariant),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Text(preview.summaryLine),
          if (groupsLine.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(groupsLine, style: Theme.of(context).textTheme.bodySmall),
            ),
          // `RadioGroup` 而不是每根 Radio 各带 groupValue/onChanged：后者在 3.47 已标废弃
          // （同外观那一屏的选择）。
          RadioGroup<String>(
            groupValue: mode,
            // `onChanged` 是必填非空，所以"导入在途就别再改模式"只能挡在回调里
            // ——用 IgnorePointer 只挡得住鼠标，方向键那条路照样能改。
            onChanged: (value) {
              if (busy || value == null) return;
              onModeChanged(value);
            },
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                for (final entry in _modeLabels.entries)
                  _ModeRadio(value: entry.key, label: entry.value),
              ],
            ),
          ),
          Row(
            children: <Widget>[
              FilledButton.tonalIcon(
                key: const Key('sources.import.confirm'),
                onPressed: busy ? null : onConfirm,
                icon: const Icon(Icons.import_export),
                label: const Text('确认导入'),
              ),
              const SizedBox(width: 8),
              TextButton(onPressed: busy ? null : onCancel, child: const Text('取消')),
            ],
          ),
        ],
      ),
    );
  }
}

/// 模式那一格：整行都是命中区（flet 的 `ft.Radio(label=…)` 就是整行可点）。
class _ModeRadio extends StatelessWidget {
  const _ModeRadio({required this.value, required this.label});

  final String value;
  final (String, String) label;

  @override
  Widget build(BuildContext context) {
    final group = RadioGroup.maybeOf<String>(context);
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: <Widget>[
        Radio<String>(value: value),
        Expanded(
          child: GestureDetector(
            onTap: () => group?.onChanged(value),
            child: Text('${label.$1} —— ${label.$2}'),
          ),
        ),
      ],
    );
  }
}
