"""黄金样本**语料库**的守卫：样本必须是现契约的快照，不许悄悄过期（设计 §10）。

为什么单独一支持这套：Dart 侧那些 ``fromJson`` 读的键、文案里出现的字段名，全部以
``tests/gateway/golden/*.json`` 为靶子（见 ``flutter_client/test/golden_contract_test.dart``）。
靶子一旦与真进程脱钩，比对就变成"两侧各自自证"——两边都对着同一份旧样本绿，而现场
早就改了字段。记忆里 ``measurement-corpus-must-not-be-self-proving`` 与
``refactor-regression-orphan-methods`` 说的都是这一族：**样本必须被钉在现契约上**。

五笔账，每一笔都朝两个方向查：

1. **覆盖**：``BY_ACTION`` 里每条动作都得有样本，例外只有一格且写死原因。
2. **时效**：每份样本都盖着抓取那趟握手的 ``schema_digest``，与现场算出来的必须同值
   ——改表、改任一 schema 而不重抓，全库立刻红。
3. **合法性**：样本里那发 ``request.payload`` 用**今天的** schema 重新验一遍。
   参数改名后旧样本发的还是旧键名，这一笔当场抓住。
4. **自证**：非截断样本的 ``bytes``/``sha256`` 必须能从样本内的 ``data`` 重算出来，
   ``field_names`` 必须是样本内那几行的键并集——手改过一份样本就重算不出同一个串。
5. **形态**：``mode`` 为 job 的动作必须带 ``event_sample``（受理回包里没有结果），
   非 job 的不许带。这一笔钉的是"表里的 mode 与抓取时看到交付方式"是同一件事。

跑法：``PYTHONIOENCODING=utf-8 python -m pytest tests/test_gateway_golden_corpus.py -q``
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from app.gateway import jsonschema_min as schema
from app.gateway.methods import BY_ACTION
from app.gateway.protocol import PROTOCOL_VERSION
from app.services.gateway.service import GatewayService

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "gateway" / "golden"
ENVELOPE_KEYS = ["code", "data", "error", "id", "ok", "v"]

#: 没有样本的动作，逐条写明为什么没有。**这张表只许变短**：新登记的动作用例内没样本
#: 会红，除非作者在这里加一行并给出理由——加一行是显式决定，漏抓是事故。
NO_SAMPLE_REASON = {
    "engine.webContentReady":
        "反向 WebView 那一发要先有一次在途的 webview.content 请求才回得来，"
        "靶子在 §8 R1 那趟探针里（`.probe` 的 R1 脚本），不属于这一轮的动作面抓取",
}


def _samples() -> list[tuple[Path, dict]]:
    if not GOLDEN.is_dir():
        pytest.fail(f"没有 {GOLDEN}：跑 .probe/g2_golden_capture.py")
    out = [(path, json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(GOLDEN.glob("*.json"))]
    # 正对照：仪器接上了才谈得上"缺一条就是红"。空目录会让下面每一支都绿着。
    assert out, f"{GOLDEN} 里一份样本都没有"
    return out


def _action_of(path: Path, sample: dict) -> str:
    action = sample["action"]
    assert path.stem == action or path.stem.startswith(f"{action}--"), (
        f"{path.name} 的文件名与它记录的 action {action!r} 不符")
    return action


def _canon(value) -> bytes:
    """样本内 data 的规范字节：与抓取脚本同一个口径（紧凑分隔符 + 非转义中文）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _row_union(rows) -> list[str]:
    seen: dict[str, None] = {}
    for row in rows or []:
        if isinstance(row, dict):
            for key in row:
                seen.setdefault(key, None)
    return sorted(seen)


def test_corpus_is_not_empty_and_covers_every_action() -> None:
    """每条登记过的动作都得有靶子；例外表**恰好**是缺的那几格。

    写成"缺的集合 == 例外表"而不是"缺的集合 ⊆ 例外表"：后者会在样本被抓齐之后
    仍然绿着留着那行例外，于是下一轮真漏一条时没人知道例外表本来管不管用。
    """
    sampled = {_action_of(path, sample) for path, sample in _samples()}
    missing = set(BY_ACTION) - sampled
    assert missing == set(NO_SAMPLE_REASON), (
        f"没有样本的动作：{sorted(missing)}；例外表：{sorted(NO_SAMPLE_REASON)}。"
        "新登记动作请重跑 .probe/g2_golden_capture.py，别往例外表里塞")
    # 正对照：例外表里那条**确实**没样本，而不是它恰好也被抓到了。
    for action in NO_SAMPLE_REASON:
        assert action not in sampled, f"{action} 已经有样本，例外表该删这一行"
    assert len(sampled) >= 29, f"只覆盖 {len(sampled)} 条，语料库不像抓全过"


def test_every_sample_is_stamped_with_the_live_schema_digest() -> None:
    """样本属于**这张表**：摘要对不上就是过期语料，比对作废。

    抓取的样本是历史，action 表是现在。这一支不许"历史追上现在"，只许重抓。
    """
    live = GatewayService.schema_digest()
    assert re.fullmatch(r"[0-9a-f]{12}", live), f"现场摘要不像个摘要：{live!r}"
    for path, sample in _samples():
        assert sample["schema_digest"] == live, (
            f"{path.name} 盖的是 {sample['schema_digest']}，现场算出来是 {live}："
            "action 表或某个 schema 动过而样本没重抓。跑 .probe/g2_golden_capture.py")
    # 正对照：engine.hello 那份样本里**两处**摘要必须同源——顶层是探针盖的章，
    # data 里那句是真进程握手里报的。探针自己算不出 data 那一句。
    hello = next(s for p, s in _samples() if p.name == "engine.hello.json")
    assert hello["response"]["data"]["schema_digest"] == hello["schema_digest"]


def test_sample_requests_still_validate_under_todays_schemas() -> None:
    """样本里那发请求今天仍然合法——参数改名会在这儿红，而不是在客户端静默丢参。"""
    for path, sample in _samples():
        action = _action_of(path, sample)
        payload = sample["request"]["payload"]
        spec = schema.load(action)
        try:
            schema.validate(payload, spec)
        except schema.SchemaError as exc:
            pytest.fail(f"{path.name} 的请求今天过不了 schema：{exc}")
        assert set(payload) <= set(BY_ACTION[action].params), (
            f"{path.name} 发了表上没有的参数："
            f"{sorted(set(payload) - set(BY_ACTION[action].params))}")
        # 必填那几格必须**真的被样本发过**：否则这份样本压根没走那条分支。
        for key in spec.get("required") or []:
            assert key in payload, f"{path.name} 没发必填的 {key}"


def test_sample_envelopes_are_legal_and_pair_with_their_request() -> None:
    for path, sample in _samples():
        response = sample["response"]
        assert sorted(response) == ENVELOPE_KEYS, f"{path.name} 信封键不是冻结那六格"
        assert response["v"] == sample["protocol_version"] == PROTOCOL_VERSION
        assert sample["request"]["v"] == PROTOCOL_VERSION
        # 请求与响应必须成对：教客户端"发了就算"而不比对 id，正是断线重连时
        # 把上一轮的迟到结果当本轮渲染的那类事故。
        assert sample["request"]["id"] == response["id"], f"{path.name} 收发 id 不配对"
        if response["ok"] is False:
            # 失败信封也是契约的一部分（``reader.toc--no-session`` 钉的就是它）：
            # 客户端按 error 那句里的**异常名**决定要不要重开会话，所以判据是
            # "有名字、没 data"，而不是"也给我一份能解析的 data"。
            assert str(response.get("error") or ""), f"{path.name} 失败却不给原因"
            assert response["data"] is None, f"{path.name} 失败却带着 data"
            continue
        assert response["ok"] is True, (
            f"{path.name} 的 ok 既不是 true 也不是 false：{response['ok']!r}")


def test_counters_and_field_names_recompute_from_the_sample_itself() -> None:
    """样本里的账目必须自洽——手改一格 data 就算不出同一个 sha，也就改不掉守卫。"""
    truncated = 0
    for path, sample in _samples():
        data = sample["response"]["data"]
        expected_shape = ("list" if isinstance(data, list) else
                          "object" if isinstance(data, dict) else
                          "string" if isinstance(data, str) else
                          "null" if data is None else "scalar")
        assert sample["data_shape"] == expected_shape, (
            f"{path.name} 记的形状 {sample['data_shape']!r}，"
            f"样本内 data 却是 {type(data).__name__}")
        size = len(_canon(data))
        if sample["data_truncated"]:
            truncated += 1
            # 截断的意思是"这里少留了东西"，不是"随手标个记号"。
            assert size < sample["bytes"], (
                f"{path.name} 说截断，可样本内 data 的字节已经等于全量 {sample['bytes']}")
            rows = data if isinstance(data, list) else None
            if rows is not None:
                assert sample["inline_row_count"] == len(rows) < sample["row_count"], (
                    path.name)
            # 全量 field_names 允许比样本内那几行宽（有些行才有某一格），
            # 但不许窄——窄了就是 Dart 会去读一个样本里看得见、现场没有的键。
            assert set(_row_union(rows)) <= set(sample["field_names"]), path.name
            continue
        assert sample["bytes"] == size, f"{path.name} 的 bytes 与样本内 data 重算不符"
        assert (hashlib.sha256(_canon(data)).hexdigest() == sample["sha256"]
                ), f"{path.name} 的 sha256 重算不出来——这份样本被手改过"
        if isinstance(data, list):
            assert sample["row_count"] == len(data), path.name
            assert _row_union(data) == sample["field_names"], path.name
        elif isinstance(data, dict):
            assert sample["row_count"] == 1 and sorted(data) == sample["field_names"], (
                path.name)
        else:
            assert sample["field_names"] == [], path.name
    # 正对照：两种分支都得真的走过。一份都没截断，等于"截断那一支没人验过"。
    assert truncated >= 1, "没有一份样本是截断的：截断那支逻辑没人走过，口径没法验"
    assert len(_samples()) - truncated >= 1


def test_mutation_controls_show_the_guards_are_not_deaf() -> None:
    """前三支的"能红"证据：把样本改坏一处，重算与校验都必须炸。

    没有这一支，上面那些断言可能只是在比一个恒等式（记忆 ``guard-tests-assert-absence``）。
    """
    sample = next(s for p, s in _samples() if not s["data_truncated"]
                  and isinstance(s["response"]["data"], list) and s["response"]["data"])
    original = json.loads(json.dumps(sample))
    data = original["response"]["data"]
    # ① 多一个键：field_names 的并集不再等于行里的键。
    data[0] = {**data[0], "守卫不该放过这一格": 1}
    assert _row_union(data) != original["field_names"], "加了键却没被查出来"
    # ② 改一个值：sha 必须跟着变。
    tampered = json.loads(json.dumps(sample))
    tampered["response"]["data"][0] = dict(tampered["response"]["data"][0])
    first = tampered["response"]["data"][0]
    flip = next((k for k, v in first.items() if isinstance(v, (int, float))
                 and not isinstance(v, bool)), None)
    assert flip is not None, "样本行里没有一个数值，这支正对照退化了"
    first[flip] = (first[flip] or 0) + 1
    assert hashlib.sha256(_canon(tampered["response"]["data"])).hexdigest() \
        != sample["sha256"], "改了值 sha 却没变"
    # ③ 把必填参数删掉：今天的 schema 必须拒。
    action = sample["action"]
    required = schema.load(action).get("required") or []
    if required:
        broken = {k: v for k, v in sample["request"]["payload"].items()
                  if k != required[0]}
        with pytest.raises(schema.SchemaError):
            schema.validate(broken, schema.load(action))
    else:
        # 这一份本身没有必填，换一份有的：不许因为"抽到的样本恰好没必填"就跳过。
        with_required = next(s for p, s in _samples()
                             if schema.load(s["action"]).get("required"))
        key = (schema.load(with_required["action"]).get("required") or [None])[0]
        payload = {k: v for k, v in with_required["request"]["payload"].items()
                   if k != key}
        with pytest.raises(schema.SchemaError):
            schema.validate(payload, schema.load(with_required["action"]))


def test_projected_actions_deliver_exactly_their_declared_keys() -> None:
    """表里声明了出口投影的动作，样本里的键必须**正好**是那几格。

    这一支钉的是"投影真的在现场生效"，不是"投影函数写得对"（那半在
    ``tests/test_gateway_contract.py`` 的 ``ExitProjectionTests``）：样本来自真进程，
    键集与表不符只有两种可能——表动了没重抓，或者出口那格根本没接上。
    两边都不许绿着过去，因为 Dart 的 ``SourceRecord`` 就照这一发建模型。
    """
    projected = {a: m for a, m in BY_ACTION.items() if m.fields}
    assert projected, "action 表里一条投影都没有：这一支在空转"
    for path, sample in _samples():
        method = projected.get(_action_of(path, sample))
        if method is None:
            continue
        if sample["response"]["ok"] is False:
            # 失败那一发没有行，投影没得上场；它的契约在信封那一条里钉着。
            continue
        assert sorted(sample["field_names"]) == sorted(method.fields), (
            f"{path.name} 的键集 {sample['field_names']} 与表里声明的投影 "
            f"{sorted(method.fields)} 不符")
        rows = sample["response"]["data"]
        if isinstance(rows, list):
            for row in rows:
                assert set(row) <= set(method.fields), (
                    f"{path.name} 里有一行带着投影之外的键：{sorted(set(row) - set(method.fields))}")


def test_an_unprojected_action_still_delivers_the_heavy_fields() -> None:
    """正对照：同一份语料上，没投影的那一发**必须**还带着规则原文。

    少了这一支，上一条就可能是"两边都裁了所以一样"——真裁剪失效时它反而自洽。
    编辑器那条线（``source.get``）靠的就是这些格子，它们在列表那一发里没有、
    在这一发里必须有。
    """
    by_action = {_action_of(path, sample): sample for path, sample in _samples()}
    lean = set(BY_ACTION["source.list"].fields or ())
    sample = by_action["source.get"]
    extra = set(sample["field_names"]) - lean
    assert extra, ("source.get 的键集与列表那一发一模一样 = 出口那格把两条动作裁成"
                   "了同一发，编辑器再没有别的来源")
    assert any(name.startswith("rule") for name in extra), (
        f"source.get 只多带了 {sorted(extra)}：一条规则字段都没有，"
        "编辑器的底本就不完整（Gson 丢 null，这条源得至少设一条规则）")
    assert {"bookSourceUrl", "bookSourceName", "enabled"} <= set(sample["field_names"])


def test_job_actions_carry_their_event_delivery_and_others_do_not() -> None:
    """``mode=job`` 的样本必须带 ``event_sample``，非 job 的不许带。

    job 的 RPC 回包里只有一个受理凭据（``{started, action}``），真结果从 /ws 走。
    少了事件那一段，Dart 只比回包就能绿着把"结果从哪来"这条契约整个漏掉
    （记忆 ``ui-progress-must-travel-as-events``：这条契约本来就不是回包）。
    """
    jobs = {a for a, m in BY_ACTION.items() if m.mode == "job"}
    assert jobs, "action 表里没有一条 job——那这条守卫就是空转"
    by_action: dict[str, dict] = {}
    for path, sample in _samples():
        by_action.setdefault(_action_of(path, sample), sample)
    for action in sorted(jobs):
        sample = by_action[action]
        event = sample.get("event_sample")
        assert event, f"{action} 是 job，样本里却没有事件那一段交付"
        assert event["name"], f"{action} 的 event_sample 没名字"
        assert event["payload_field_names"], f"{action} 的事件载荷一个键都没有"
        assert isinstance(event["payload"], dict)
    for action, sample in sorted(by_action.items()):
        if action in jobs:
            continue
        assert "event_sample" not in sample, (
            f"{action} 不是 job 却带着事件样本：表里的 mode 与抓取时看到的交付方式"
            "不是同一件事，重跑探针或改表，别留着这份含糊样本")
