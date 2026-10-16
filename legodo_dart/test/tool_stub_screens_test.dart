/// 三张「能力没实现」的工具页：网络桥 / 备份 / 导出。
///
/// Go 侧 `pkg/ffi/engine.go` 把这一族桩换成了显式的「未实现：…」错误，界面这一侧要接住
/// 的是**那句话本身**，而不是把错误折成一个看起来像正常状态的读数。这三屏原先各有自己的
/// 折法，且折完的方向都朝"没事"：
///
/// 1. **网络桥页从来不读状态**（没有 `initState`，`_loadStatus` 只在启动成功之后被叫一次），
///    还把读状态的错误吞了（`catch (e) { /* 忽略错误 */ }`）。于是状态行只在
///    「运行中 / 已停止」两格里选——一发从没成功过的动作，报的是「已停止」；
///    第一帧连那一发都还没发出去，报的也是「已停止」。
///    「已停止」是一句关于服务的断言，没读、读不到，都没有资格说。
/// 2. **备份页在加载失败后渲染「暂无备份」**：那条 snackbar 四秒就没了，留在屏幕上的
///    是一句关于远端的断言。Go 那句明的就是「不会回一份空列表装成远端没有备份」。
/// 3. **导出页**这一支原本是对的（只在 await 成功之后才写「导出成功」），这里把它钉住，
///    免得改别处时顺手把它也"优化"成先报成功。
///
/// 替身那侧的改动是这一支能测的前提：`FakeEngineRepository` 文件头写着「[failure] 非空时
/// 每个方法都抛那条」，而这六格原先是裸的 `async {}`，既接不到错误也不记账。
///
/// 不判的东西也说清楚：错误的具体措辞由 Go 侧 `unimplemented_test.go` 钉（那儿还有
/// 「两种拼法同一句话」那一条），这里只判**屏幕有没有把它显示出来、有没有同时演成功**。
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/core/di/providers.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/fake_engine_repository.dart';
import 'package:legodo_client/features/settings/presentation/export_screen.dart';
import 'package:legodo_client/features/settings/presentation/web_bridge_screen.dart';
import 'package:legodo_client/features/settings/presentation/webdav_screen.dart';

/// 替身注入的那一句。措辞故意不带界面上会出现的任何一个词，免得断言读成"屏幕上有个词"。
const String _lie = '工具面替身注入的那一句（不该被折成任何成功读数）';

EngineFailure get _failure =>
    const EngineFailure(EngineFailureKind.engine, _lie);

Future<void> _pump(WidgetTester tester, Widget screen,
    FakeEngineRepository fake) async {
  await tester.pumpWidget(ProviderScope(
    overrides: [engineRepositoryProvider.overrideWithValue(fake)],
    child: MaterialApp(home: screen),
  ));
  await tester.pumpAndSettle();
}

/// 状态行那一格的文本（按键取，不按措辞取：措辞是三格拼出来的，改一个字就红）。
String _statusLineText(WidgetTester tester) =>
    tester.widget<Text>(find.byKey(const Key('webbridge.statusLine'))).data ?? '';

void main() {
  group('网络桥页（webbridge.* 这一族没有后端）', () {
    testWidgets('读状态失败时那一行是「未知 + 那一句」，不是「已停止」', (t) async {
      final fake = FakeEngineRepository(failure: _failure);
      await _pump(t, const WebBridgeScreen(), fake);

      final line = _statusLineText(t);
      expect(line, contains('未知'), reason: '读不到却报「已停止」= 演一个没发生过的状态');
      expect(line, contains(_lie), reason: '那一句要原样落到界面上，不是只闪一条 snackbar');
      expect(find.text('状态：已停止'), findsNothing);
      expect(find.text('状态：运行中'), findsNothing);
      expect(fake.callsFor('webbridge.status'), 1);
    });

    testWidgets('正对照：读得到时那一行照常报「已停止」，「未知」不是常亮的', (t) async {
      await _pump(t, const WebBridgeScreen(), FakeEngineRepository());

      expect(_statusLineText(t), '状态：已停止');
      expect(find.textContaining('未知'), findsNothing);
    });

    testWidgets('第一帧还不许报「已停止」：那一发没回来之前页面没有资格断言服务状态', (t) async {
      final fake = FakeEngineRepository(
          failure: _failure, delay: const Duration(milliseconds: 50));
      await t.pumpWidget(ProviderScope(
        overrides: [engineRepositoryProvider.overrideWithValue(fake)],
        child: const MaterialApp(home: WebBridgeScreen()),
      ));
      await t.pump(); // 只走一帧，让那一发还挂在替身的延迟上

      expect(_statusLineText(t), contains('读取中'));
      expect(find.text('状态：已停止'), findsNothing);
      expect(fake.callsFor('webbridge.status'), 1, reason: '页面确实发了那一发');

      // 收摊前把那枚延迟的定时器走完：留着 pending timer 退出，测试框架会先报
      // "A Timer is still pending"，那条红与这一屏无关。
      await t.pump(const Duration(milliseconds: 60));
      await t.pumpAndSettle();
    });

    testWidgets('点「启动」发了那一发，失败之后不演成运行中', (t) async {
      final fake = FakeEngineRepository(failure: _failure);
      await _pump(t, const WebBridgeScreen(), fake);

      await t.tap(find.text('启动'));
      await t.pumpAndSettle();

      expect(fake.callsFor('webbridge.start'), 1);
      expect(_statusLineText(t), isNot(contains('运行中')));
      // 启动失败之后「停止」该是-disabled 的：没有跑着的东西可停。
      expect(t.widget<OutlinedButton>(find.byType(OutlinedButton)).onPressed,
          isNull);
    });
  });

  group('备份页（webdav.* 这一族没有后端）', () {
    testWidgets('加载失败时列表区摆的是那一句，不是「暂无备份」', (t) async {
      final fake = FakeEngineRepository(failure: _failure);
      await _pump(t, const WebDavScreen(), fake);

      expect(
        t.widget<Text>(find.byKey(const Key('webdav.loadError'))).data,
        contains(_lie),
      );
      expect(find.text('暂无备份'), findsNothing,
          reason: '「暂无备份」是关于远端的断言，读失败时没资格说');
      expect(fake.callsFor('webdav.list_backups'), greaterThanOrEqualTo(1));
    });

    testWidgets('正对照：读得到一份空列表时才说「暂无备份」', (t) async {
      await _pump(t, const WebDavScreen(), FakeEngineRepository());

      expect(find.text('暂无备份'), findsOneWidget);
      expect(find.byKey(const Key('webdav.loadError')), findsNothing);
    });

    testWidgets('「立即备份」失败写的是失败，不是「备份成功」', (t) async {
      final fake = FakeEngineRepository(failure: _failure);
      await _pump(t, const WebDavScreen(), fake);

      await t.tap(find.text('立即备份'));
      await t.pumpAndSettle();

      expect(fake.callsFor('webdav.backup'), 1);
      expect(find.text('备份成功'), findsNothing);
      expect(find.textContaining('备份失败'), findsOneWidget);
      expect(find.textContaining(_lie), findsWidgets);
    });
  });

  group('导出页（export.* 这一族没有后端）', () {
    testWidgets('导出失败时不出现「导出成功」，那一句落在状态行', (t) async {
      final fake = FakeEngineRepository(failure: _failure);
      await _pump(t, const ExportScreen(), fake);

      // 两个输入框按出现顺序取：[0] 书籍标识、[1] 输出路径（界面上没有别的 TextField）。
      await t.enterText(find.byType(TextField).at(0), 'http://x.test/book');
      await t.enterText(find.byType(TextField).at(1), r'D:\out\book.epub');
      await t.pump();

      await t.tap(find.text('导出 epub'));
      await t.pumpAndSettle();

      expect(fake.callsFor('export.epub'), 1);
      expect(find.textContaining('导出成功'), findsNothing);
      expect(find.textContaining(_lie), findsOneWidget);
    });
  });
}
