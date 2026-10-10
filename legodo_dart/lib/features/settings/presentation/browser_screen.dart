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
  String _url = '';
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
              _url = url;
            });
          },
          onPageFinished: (url) {
            setState(() {
              _isLoading = false;
              _url = url;
            });
          },
        ),
      )
      ..loadRequest(Uri.parse('https://www.baidu.com'));
  }

  void _navigate(String url) {
    if (!url.startsWith('http://') && !url.startsWith('https://')) {
      url = 'https://$url';
    }
    _controller.loadRequest(Uri.parse(url));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: TextField(
          onSubmitted: _navigate,
          decoration: InputDecoration(
            hintText: '输入网址',
            border: InputBorder.none,
            suffixIcon: IconButton(
              icon: const Icon(Icons.search),
              onPressed: () {
                final text = (context.findRenderObject() as RenderBox?)
                    ?.parent
                    ?.debugGetNearestAncestorOfType<TextField>()
                    ?.value
                    .text;
                if (text != null && text.isNotEmpty) {
                  _navigate(text);
                }
              },
            ),
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
