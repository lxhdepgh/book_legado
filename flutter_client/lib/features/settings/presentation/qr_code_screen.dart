/// 二维码工具页：生成二维码 / 识别图片中的二维码。
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';

class QrCodeScreen extends ConsumerStatefulWidget {
  const QrCodeScreen({super.key});

  @override
  ConsumerState<QrCodeScreen> createState() => _QrCodeScreenState();
}

class _QrCodeScreenState extends ConsumerState<QrCodeScreen> {
  final _textController = TextEditingController();
  String? _qrImageBase64;
  String? _decodeResult;
  bool _generating = false;
  bool _decoding = false;

  @override
  void dispose() {
    _textController.dispose();
    super.dispose();
  }

  Future<void> _generateQr() async {
    final text = _textController.text.trim();
    if (text.isEmpty) return;

    setState(() {
      _generating = true;
      _qrImageBase64 = null;
    });

    try {
      final repo = ref.read(engineRepositoryProvider);
      final base64 = await repo.qrGenerate(text);
      setState(() {
        _generating = false;
        _qrImageBase64 = base64;
      });
    } catch (e) {
      setState(() {
        _generating = false;
      });
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('生成失败：$e')),
        );
      }
    }
  }

  Future<void> _decodeQr() async {
    final text = _textController.text.trim();
    if (text.isEmpty) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('请在上方输入框粘贴要解码的图片 Base64 数据')),
        );
      }
      return;
    }

    setState(() => _decoding = true);
    try {
      final repo = ref.read(engineRepositoryProvider);
      final result = await repo.qrDecode(text);
      setState(() {
        _decoding = false;
        _decodeResult = result ?? '未识别到二维码';
      });
    } catch (e) {
      setState(() => _decoding = false);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('识别失败：$e')),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('二维码')),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('生成二维码', style: theme.textTheme.titleLarge),
            const SizedBox(height: 8),
            TextField(
              controller: _textController,
              decoration: const InputDecoration(
                labelText: '要生成二维码的文本',
                hintText: '书源 JSON / 替换规则 JSON / 任意文本',
                border: OutlineInputBorder(),
              ),
              maxLines: 3,
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                ElevatedButton.icon(
                  onPressed: _generating ? null : _generateQr,
                  icon: _generating
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.qr_code_2),
                  label: const Text('生成'),
                ),
                const SizedBox(width: 8),
                OutlinedButton.icon(
                  onPressed: _decoding ? null : _decodeQr,
                  icon: const Icon(Icons.image_search),
                  label: const Text('识别（Base64）'),
                ),
              ],
            ),
            if (_qrImageBase64 != null) ...[
              const SizedBox(height: 16),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    children: [
                      Image.memory(
                        base64Decode(_qrImageBase64!),
                        width: 200,
                        height: 200,
                        fit: BoxFit.contain,
                      ),
                      const SizedBox(height: 8),
                      Text(
                        '二维码已生成',
                        style: theme.textTheme.bodySmall,
                      ),
                    ],
                  ),
                ),
              ),
            ],
            if (_decodeResult != null) ...[
              const SizedBox(height: 16),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text('识别结果', style: theme.textTheme.titleMedium),
                      const SizedBox(height: 8),
                      SelectableText(_decodeResult!),
                    ],
                  ),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
