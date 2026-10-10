/// 浏览器工具页：使用 webview_flutter 加载网页。
///
/// Windows 需要 WebView2 Runtime（Win10/11 通常已预装），
/// Android 使用系统 WebView，iOS 使用 WKWebView。
library;

import 'package:flutter/material.dart';
import 'package:webview_flutter/webview_flutter.dart';

class BrowserScreen extends StatefulWidget {
  const BrowserScreen({super.key});

  @override
  State<BrowserScreen> createState() => _BrowserScreenState();
}

class _BrowserScreenState extends State<BrowserScreen> {
  late WebViewController _controller;
  final TextEditingController _urlController = TextEditingController(text: 'https://www.baidu.com');
  String _currentUrl = '';
  bool _isLoading = false;

  @override
  void initState() {
    super.initState();
    _controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setNavigationDelegate(
        NavigationDelegate(
          onPageStarted: (url) {
            setState(() {
              _isLoading = true;
            });
          },
          onPageFinished: (url) {
            setState(() {
              _isLoading = false;
              _currentUrl = url;
              _urlController.text = url;
            });
          },
        ),
      )
      ..loadRequest(Uri.parse('https://www.baidu.com'));
  }

  void _navigate(String url) {
    if (url.isEmpty) return;
    if (!url.startsWith('http://') && !url.startsWith('https://')) {
      url = 'https://$url';
    }
    _urlController.text = url;
    _controller.loadRequest(Uri.parse(url));
  }

  @override
  void dispose() {
    _urlController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: TextField(
          controller: _urlController,
          onSubmitted: _navigate,
          decoration: const InputDecoration(
            hintText: '输入网址',
            border: InputBorder.none,
            suffixIcon: Icon(Icons.search),
          ),
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.arrow_back),
            onPressed: () => _controller.goBack(),
          ),
          IconButton(
            icon: const Icon(Icons.arrow_forward),
            onPressed: () => _controller.goForward(),
          ),
          IconButton(
            icon: const Icon(Icons.refresh),
            onPressed: () => _controller.reload(),
          ),
        ],
      ),
      body: Stack(
        children: [
          WebViewWidget(controller: _controller),
          if (_isLoading)
            Container(
              alignment: Alignment.topCenter,
              child: const LinearProgressIndicator(),
            ),
        ],
      ),
    );
  }
}
