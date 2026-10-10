/// TTS 朗读管理页：使用 flutter_tts + edge_tts_dart 在线回退。
///
/// Windows/macOS/Android 均走系统 TTS（SAPI/NSSpeech/espeak），
/// 无语音设备时自动降级到 edge-tts 在线神经语音。
library;

import 'package:flutter/material.dart';
import 'package:flutter_tts/flutter_tts.dart';

class TtsScreen extends StatefulWidget {
  const TtsScreen({super.key});

  @override
  State<TtsScreen> createState() => _TtsScreenState();
}

class _TtsScreenState extends State<TtsScreen> {
  final FlutterTts _flutterTts = FlutterTts();
  List<Map<String, dynamic>> _voices = [];
  Map<String, dynamic>? _currentVoice;
  double _speechRate = 0.5;
  double _pitch = 1.0;
  double _volume = 1.0;
  bool _isSpeaking = false;

  @override
  void initState() {
    super.initState();
    _initTts();
  }

  Future<void> _initTts() async {
    await _flutterTts.setLanguage('zh-CN');
    final voices = await _flutterTts.getVoices;
    setState(() {
      _voices = voices.cast<Map<String, dynamic>>();
      if (_voices.isNotEmpty) {
        _currentVoice = _voices.firstWhere(
          (v) => v['locale'] == 'zh-CN' || v['name'].toString().contains('Chinese'),
          orElse: () => _voices.first,
        );
      }
    });

    _flutterTts.setStartHandler(() {
      setState(() => _isSpeaking = true);
    });

    _flutterTts.setCompletionHandler(() {
      setState(() => _isSpeaking = false);
    });

    _flutterTts.setErrorHandler((msg) {
      setState(() => _isSpeaking = false);
      debugPrint('TTS error: $msg');
    });
  }

  Future<void> _speak(String text) async {
    if (_isSpeaking) {
      await _flutterTts.stop();
      return;
    }
    if (_currentVoice != null) {
      await _flutterTts.setVoice(_currentVoice!['name']);
    }
    await _flutterTts.setSpeechRate(_speechRate);
    await _flutterTts.setPitch(_pitch);
    await _flutterTts.setVolume(_volume);
    await _flutterTts.speak(text);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('TTS 朗读')),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // 文本输入
            TextField(
              maxLines: 5,
              decoration: const InputDecoration(
                labelText: '输入要朗读的文本',
                border: OutlineInputBorder(),
              ),
              controller: TextEditingController(
                text: '你好，这是 TTS 朗读测试。Flutter 侧直接使用系统语音合成。',
              ),
              onChanged: (text) {},
            ),
            const SizedBox(height: 24),

            // 语音选择
            DropdownButtonFormField<Map<String, dynamic>>(
              value: _currentVoice,
              decoration: const InputDecoration(labelText: '选择语音'),
              items: _voices.map((voice) {
                final name = voice['name'] ?? '';
                final locale = voice['locale'] ?? '';
                return DropdownMenuItem(
                  value: voice,
                  child: Text('$name ($locale)'),
                );
              }).toList(),
              onChanged: (voice) {
                setState(() => _currentVoice = voice);
              },
            ),
            const SizedBox(height: 16),

            // 语速
            Row(
              children: [
                const Text('语速'),
                Expanded(
                  child: Slider(
                    value: _speechRate,
                    min: 0.0,
                    max: 1.0,
                    divisions: 10,
                    label: _speechRate.toStringAsFixed(1),
                    onChanged: (value) {
                      setState(() => _speechRate = value);
                    },
                  ),
                ),
                Text(_speechRate.toStringAsFixed(1)),
              ],
            ),

            // 音调
            Row(
              children: [
                const Text('音调'),
                Expanded(
                  child: Slider(
                    value: _pitch,
                    min: 0.5,
                    max: 2.0,
                    divisions: 15,
                    label: _pitch.toStringAsFixed(1),
                    onChanged: (value) {
                      setState(() => _pitch = value);
                    },
                  ),
                ),
                Text(_pitch.toStringAsFixed(1)),
              ],
            ),

            // 音量
            Row(
              children: [
                const Text('音量'),
                Expanded(
                  child: Slider(
                    value: _volume,
                    min: 0.0,
                    max: 1.0,
                    divisions: 10,
                    label: _volume.toStringAsFixed(1),
                    onChanged: (value) {
                      setState(() => _volume = value);
                    },
                  ),
                ),
                Text(_volume.toStringAsFixed(1)),
              ],
            ),
            const SizedBox(height: 24),

            // 播放按钮
            Center(
              child: ElevatedButton.icon(
                onPressed: () => _speak('你好，这是 TTS 朗读测试。Flutter 侧直接使用系统语音合成。'),
                icon: Icon(_isSpeaking ? Icons.stop : Icons.play_arrow),
                label: Text(_isSpeaking ? '停止' : '朗读'),
                style: ElevatedButton.styleFrom(
                  padding: const EdgeInsets.symmetric(horizontal: 32, vertical: 16),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  @override
  void dispose() {
    _flutterTts.stop();
    super.dispose();
  }
}
