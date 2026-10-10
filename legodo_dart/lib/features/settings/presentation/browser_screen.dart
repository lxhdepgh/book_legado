/// 浏览器工具页（占位）：通用内置浏览器（CDP 投屏架构）。
///
/// 注意：Flutter Windows 没有成熟的 WebView 插件，因此采用与 Flet 版本相同的
/// CDP 投屏架构：Python 侧用 Playwright 打开 Chromium，通过 WebSocket 发送
/// JPEG 帧给 Flutter，用户手势（点击/滚动/输入）发回 Python 执行。
///
/// 当前为占位实现，完整功能需要：
/// 1. 事件订阅机制（接收服务端推送的帧）
/// 2. 手势捕获与坐标换算
/// 3. 会话管理（open/navigate/close）
library;

import 'package:flutter/material.dart';

class BrowserScreen extends StatelessWidget {
  const BrowserScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('浏览器')),
      body: Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.public, size: 64, color: Colors.grey),
            const SizedBox(height: 16),
            Text('内置浏览器',
                style: theme.textTheme.headlineSmall?.copyWith(
                  fontWeight: FontWeight.bold,
                )),
            const SizedBox(height: 8),
            Text('CDP 投屏架构', style: theme.textTheme.bodyMedium),
            const SizedBox(height: 16),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 32),
              child: Text(
                '此功能需要完整的 CDP 投屏实现：\n'
                '- Python 侧 Playwright + Chromium\n'
                '- WebSocket 帧传输\n'
                '- 手势捕获与坐标换算\n\n'
                '当前为占位页面，后续完善。',
                textAlign: TextAlign.center,
                style: theme.textTheme.bodySmall?.copyWith(
                  color: Colors.grey,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
