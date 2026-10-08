/// `displayIntro` 的跨语言对照（G2-D：简介标记的渲染契约，记忆 `intro-render-contract`）。
///
/// 下面那张表的**期望值不是手搓的**：由本仓库 Python 侧
/// `app/services/engine.rule_engine.display_intro` 现跑出来贴进去
/// （`.probe/gen_intro_cases.py`，输入同表）。所以这一发比的是
/// "Dart 那一份与真引擎那一份读出来一样"，不是"Dart 那一份与我此刻想的一样"
/// （记忆 `measurement-corpus-must-not-be-self-proving`）。
///
/// 表里三类各钉一种错：
/// - 标记类（`<usehtml>`/`<md>`/`<useweb>`）判 kind —— 画了原文就是把 CSS 上了屏；
/// - 块级/实体/注释的剥离顺序 —— 少一步就是一片 `<br>` 或一整行空白；
/// - `<span/>` 与 `<p/>` 两条**方向相反** —— 只写一条的话，实现反了也照样绿。
library;

import 'package:flutter_test/flutter_test.dart';

import 'package:legodo_client/features/find_books/data/book_intro.dart';

void main() {
  group('displayIntro', () {
    for (final case_ in _cases) {
      test(case_.label, () {
        final parsed = displayIntro(case_.input);
        expect(parsed.kind, case_.kind);
        expect(parsed.text, case_.expected);
      });
    }
  });

  test('introForDisplay：非 text 的载荷不画原文，画一句实话', () {
    expect(introForDisplay("<usehtml><div>正文</div></usehtml>"),
        '(该书简介需网页/富文本渲染，开始阅读查看)');
    expect(introForDisplay("<useweb><style>.a{color:red}</style>正文</useweb>"),
        '(该书简介需网页/富文本渲染，开始阅读查看)');
    // text 那一类原样上屏（含剥完标签的结果）；空串留给视图那句"暂无简介"。
    expect(introForDisplay("<p>第一章</p><p>正文</p>"), "\u3000\u3000第一章\n\u3000\u3000正文");
    expect(introForDisplay(""), "");
  });

  test('formatBookIntro：带原始前缀的载荷不剥标签', () {
    expect(formatBookIntro("<usehtml><p>原样</p></usehtml>"),
        "<usehtml><p>原样</p></usehtml>");
    expect(formatBookIntro("<p>要剥</p>"), "\u3000\u3000要剥");
    // formatBookIntro 也能单独调用（display_intro 之外那条线，比如网页正文里的简介）。
    // 现测：同一串带前导空白输入，两个入口**同解** —— _strip_html_tags 里
    // `^[\n\s]+ → 　　` 那一支会把块级标签折出来的换行一起吃掉，lstrip 与否落不到差别上。
    expect(displayIntro("  \u3000  <p>甲</p>").text, "\u3000\u3000甲");
    expect(formatBookIntro("  \u3000  <p>甲</p>"), "\u3000\u3000甲");
  });
}

class _Case {
  const _Case(this.input, this.kind, this.expected);

  final String input;
  final String kind;
  final String expected;

  String get label {
    final head = input.length > 28 ? '${input.substring(0, 28)}…' : input;
    return '$head → $kind';
  }
}

const List<_Case> _cases = <_Case>[
  _Case("", 'text', ""),
  _Case("  一本普通的书。", 'text', "一本普通的书。"),
  _Case("  <p>前导空白里的块</p>", 'text', "\u3000\u3000前导空白里的块"),
  _Case("<p>第一章</p><p>正文</p>", 'text', "\u3000\u3000第一章\n\u3000\u3000正文"),
  _Case("<div>甲</div><br/>乙<hr></hr>丙<h2>丁</h2>", 'text', "\u3000\u3000甲\n\u3000\u3000乙\n\u3000\u3000丙\n\u3000\u3000丁"),
  _Case("A&nbsp;&nbsp;B&ensp;C&emsp;D&thinsp;E&zwnj;F&zwj;G", 'text', "A B C DEFG"),
  _Case("<!-- 注释 -->留下", 'text', "留下"),
  _Case("<span>x</span><b>y</b>", 'text', "xy"),
  _Case("<span/>甲</span>", 'text', "<span/>甲"),
  _Case("<p/>自闭合", 'text', "\u3000\u3000自闭合"),
  _Case("<usehtml><div>正文</div></usehtml>", 'html', "<div>正文</div>"),
  _Case("<md># 标题\n正文</md>", 'md', "# 标题\n正文"),
  _Case("<useweb><style>.a{color:red}</style>正文</useweb>", 'web', "<style>.a{color:red}</style>正文"),
  _Case("<usehtml>没有闭合标记", 'text', "<usehtml>没有闭合标记"),
  _Case("<usehtml></usehtml>", 'html', ""),
  _Case("&lt;div&gt;转义的分隔", 'text', "&lt;div&gt;转义的分隔"),
  _Case("行1\n\n行2\n    行3", 'text', "行1\n\u3000\u3000行2\n\u3000\u3000行3"),
  _Case("尾部空白   \n\n", 'text', "尾部空白"),
  _Case("\n\n开头的空白", 'text', "开头的空白"),
  _Case("<pre>等宽</pre>", 'text', "\u3000\u3000等宽"),
  _Case("中文\u3000全角空格\xa0不换行", 'text', "中文\u3000全角空格\xa0不换行"),
  _Case("<style>.x{}</style>裸样式", 'text', ".x{}裸样式"),
];
