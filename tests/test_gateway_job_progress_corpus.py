"""``mode="job"`` 那一族的**进度面**守卫：进度是一等契约，样本必须带上且与现场同字段。

为什么单独一支持这一支：job 的交付是**两段**（设计 §4.5）——结果一份
（``*.FinishedEvent``，``tests/test_gateway_golden_corpus.py`` 的 ``event_sample`` 那一支
已经钉住了）、进度一份（``*.ProgressEvent``，带"跑到第几"的分母）。
少了后者，Dart 只比回包就能绿着把"这一屏该不该转圈、转到哪儿"整段漏掉——那正是记忆里
``ui-progress-must-travel-as-events`` 那一族：进度断链不报错，界面永远停在 0。

"哪一条 job 有进度面"**不写在手抄表里**，从现场推（记忆
``instrument-floors-read-the-live-contract``）：动作是 ``mode="job"``、且它派发到的那个
服务方法**函数体里**发布了一条 ``*ProgressEvent``。手抄表会漂——把发布点搬进另一个方法、
或给某条 job 新加一条进度事件，表都照样绿。推出来是零条也算仪器没接上，所以
:func:`test_progress_faces_are_derived_from_the_live_method_bodies` 里
**点名**要求 ``switch.scan`` 那一支必须在推导结果里（它管的就是 G3-D3 这一门）。

四笔账：

1. **谁该有**：推导出的每一支，其事件类必须真在 ``app.core.events`` 上、且是
   **只带计数**那一类（在 ``COALESCED_EVENTS`` 里）——结果类事件塞进进度面就是丢数据。
2. **样本在不在、长什么样**：每一支都得有 ``progress_sample``，事件名、字段清单、
   帧形状、帧数与"帧里出现过的键并集"必须一次过得了同一个口
   （:func:`_check_progress_sample`）；字段清单那个口读的是**今天的** dataclass，
   改字段不重抓就红。
3. **同一轮、走得最远**：各帧的关联格（``switch_id``/``search_id``）与 ``total`` 必须各自
   唯一，且与同一条样本 ``event_sample`` 里那一轮同号；``done`` 不许倒退，末帧就是最大那一发，
   且不超过分母——收尾那帧丢了，进度条停在半路。
4. **变异对照**：把样本逐处改坏喂回**同一个口**，每一处都必须被拒（少这一支，
   上面那些判据可能只是在比一个恒等式）。

跑法：``PYTHONIOENCODING=utf-8 python -m pytest tests/test_gateway_job_progress_corpus.py -q``
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import re
from pathlib import Path

from app.core import events as event_module
from app.gateway.methods import BY_ACTION, resolve_service_class
from app.services.gateway.service import COALESCED_EVENTS

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "gateway" / "golden"

#: 关联那一格在本库里的两个名字：换源用 ``switch_id``（G3-D3），搜索用 ``search_id``。
#: 为什么由客户端生成：``_start_job`` 的受理回包只有 ``{started, action}``，没有任务号
#: ——事件是异步到达的，晚到的那一份只能靠这个号认本轮。
CORRELATION_KEYS = ("switch_id", "search_id")

#: 这一门（§9 G3-D3）交付的那一条：**点名**要求它有自己的进度面与进度样本。
#: 少了这一行，上面的推导会因为"函数体里那条发布被搬走了"而退化成空表，
#: 于是四笔账全部绿着比空气（记忆 ``guard-tests-assert-absence``）。
NAMED_FACE = ("switch.scan", "SourceSwitchProgressEvent")


def _progress_event_names() -> set[str]:
    """现场所有 ``*ProgressEvent`` 的类名（反射，不抄清单）。"""
    return {value.__name__ for value in vars(event_module).values()
            if isinstance(value, type) and dataclasses.is_dataclass(value)
            and value.__name__.endswith("ProgressEvent")}


def _samples() -> dict[str, dict]:
    """每条动作的**主**样本（变体名 ``--xxx`` 那一类不算）。

    为什么排除变体：``switch.scan`` 只有一发正常收口，而进度帧只在正常收口那一轮收
    （被叫停/起扫前出错那两臂的帧形状由 ``tests/test_reading_services.py`` 在服务面钉）。
    按文件名取会把别人的样本当这一族的靶子。
    """
    out: dict[str, dict] = {}
    if not GOLDEN.is_dir():
        raise AssertionError(f"没有 {GOLDEN}：跑 .probe/g2_golden_capture.py")
    for path in sorted(GOLDEN.glob("*.json")):
        sample = json.loads(path.read_text(encoding="utf-8"))
        if path.stem != sample["action"]:
            continue
        out[sample["action"]] = sample
    assert out, f"{GOLDEN} 里一份主样本都没有：仪器没接上"
    return out


def _check_progress_sample(progress: dict, *, name: str, live: list[str]) -> str:
    """把一份 progress_sample 过一遍这一支持有的判据，回**第一条**不服的理由（空 = 全过）。

    收成"一个口"而不是散在五条 assert 里，是为了下面那支持有的变异对照能直接把
    改坏的样本喂回来读它拒不拒——散着的 assert 会让变异对照只测到它自己抄的那一份，
    与守卫本体无关（记忆 ``comparison-closure-must-reread-live``）。
    """
    if progress.get("name") != name:
        return f"事件名不是 {name}：{progress.get('name')!r}"
    if progress.get("payload_field_names") != live:
        return f"字段名与现场不同：{progress.get('payload_field_names')}"
    frames = progress.get("frames") or []
    if not frames:
        return "一帧都没有"
    if len(frames) != progress.get("frame_count"):
        return "frame_count 与帧列表不同数"
    for frame in frames:
        if set(frame) != {"name", "payload"}:
            return f"帧形状不是 name/payload：{sorted(frame)}"
        if frame["name"] != name:
            return f"这一帧记的名字是 {frame['name']!r}"
    if sorted({key for frame in frames for key in frame["payload"]}) != live:
        return "帧里出现过的键并集与现场字段不符"
    return ""


def _derived_faces() -> dict[str, str]:
    """每条 job → 它函数体里发布的那条进度事件名（没发布就不进这张表）。"""
    names = _progress_event_names()
    out: dict[str, str] = {}
    for action, method in BY_ACTION.items():
        if method.mode != "job":
            continue
        target = getattr(resolve_service_class(method.service), method.method)
        body = inspect.getsource(target)
        found = sorted(name for name in names
                       if re.search(r"\b%s\b" % name, body))
        if len(found) > 1:
            raise AssertionError(
                f"{action} 的函数体里发了两条进度事件：{found}——一屏只能有一把尺，"
                "请先确定哪一条是本轮的分母")
        if found:
            out[action] = found[0]
    return out


def test_progress_faces_are_derived_from_the_live_method_bodies() -> None:
    """推导本身必须是**接上的**：这一门那一条点名在里面，且推出来的都是计数类事件。"""
    faces = _derived_faces()
    assert NAMED_FACE[0] in faces, (
        f"{NAMED_FACE[0]} 不再发布进度事件（推导为空）：要么它的函数体把发布搬走了，"
        f"要么换源扫描退回界面回调那一版——两种都让 G3-D3 的判据落空。现场推出来的是 "
        f"{faces}")
    assert faces[NAMED_FACE[0]] == NAMED_FACE[1], (
        f"{NAMED_FACE[0]} 的进度事件不再是 {NAMED_FACE[1]}：换了名字，"
        "Dart 的订阅与黄金样本都还对着旧名字")
    for action, name in faces.items():
        cls = getattr(event_module, name, None)
        assert isinstance(cls, type) and dataclasses.is_dataclass(cls), (
            f"{action} 发的 {name} 不是 app.core.events 上的 dataclass")
        assert name in COALESCED_EVENTS, (
            f"{name} 是进度事件却没进合流表——一轮扫描几百条全直发？"
            "（合流的判据就是'这条只带计数'）")
        assert not name.endswith("FinishedEvent"), (
            f"{action} 把结果类事件当成了进度面：{name}")
    # 正对照：这张表里不许有非 job 的动作（推导按 mode 走，写歪了要当场说）。
    for action in faces:
        assert BY_ACTION[action].mode == "job", action


def test_every_job_with_a_progress_face_has_a_sample_carrying_it() -> None:
    samples = _samples()
    for action, name in sorted(_derived_faces().items()):
        sample = samples.get(action)
        assert sample, f"{action} 有进度面却没有样本"
        progress = sample.get("progress_sample")
        assert progress, (
            f"{action} 的函数体在发 {name}，样本里却没有 progress_sample："
            "进度那一面没有靶子，Dart 的进度模型就只是它自己的想象。"
            "重跑 .probe/g3_golden_capture.py")
        live = sorted(f.name for f in dataclasses.fields(getattr(event_module, name)))
        reason = _check_progress_sample(progress, name=name, live=live)
        assert not reason, f"{action} 的进度样本不服：{reason}"


def test_progress_frames_belong_to_one_round_and_end_at_the_furthest() -> None:
    """帧必须是**同一轮**的、且末帧是走得最远那一发。"""
    samples = _samples()
    for action in sorted(_derived_faces()):
        progress = samples[action]["progress_sample"]
        frames = progress["frames"]
        keys = [key for key in CORRELATION_KEYS
                if key in progress["payload_field_names"]]
        assert keys, (
            f"{action} 的进度事件没有关联格（{CORRELATION_KEYS} 都不在）："
            "上一轮的迟到进度会被当本轮渲染")
        key = keys[0]
        round_ids = {frame["payload"].get(key) for frame in frames}
        assert len(round_ids) == 1, (
            f"{action} 的进度样本混了 {len(round_ids)} 轮：{sorted(map(str, round_ids))}")
        event = samples[action].get("event_sample") or {}
        event_payload = event.get("payload") or {}
        if key in event_payload:
            assert event_payload[key] == frames[0]["payload"][key], (
                f"{action} 的进度帧与结果事件不是同一轮："
                f"{frames[0]['payload'][key]!r} vs {event_payload[key]!r}")
        totals = {frame["payload"].get("total") for frame in frames}
        assert len(totals) == 1, (
            f"{action} 的进度帧分母不唯一：{sorted(map(str, totals))}——"
            "一条进度里的 total 是'本轮计划源数'，中途变就是混了轮")
        dones = [frame["payload"].get("done") for frame in frames]
        assert all(isinstance(value, int) and not isinstance(value, bool)
                   for value in dones), action
        assert dones == sorted(dones), f"{action} 的 done 倒退了：{dones}"
        assert dones[-1] == max(dones), (
            f"{action} 的末帧不是最远那一发：{dones}——收尾那帧没进样本")
        assert dones[-1] <= next(iter(totals)), (
            f"{action} 扫完了 {dones[-1]} 却只计划 {next(iter(totals))} 条："
            "分母与活不是同一件事")


def test_only_a_job_with_a_progress_face_may_carry_a_progress_sample() -> None:
    """另一头：没推出进度面的动作**不许**带 ``progress_sample``。

    与 ``test_gateway_golden_corpus`` 里 ``event_sample`` 那一条同一个方向：带着就红，
    否则"表里的 mode 与抓取时看到的交付方式"是同一件事这句话没人查。
    """
    faces = set(_derived_faces())
    carried = {action for action, sample in _samples().items()
               if "progress_sample" in sample}
    assert carried, ("全库没有一份进度样本：这一支在空转。先确认 G3-D3 的抓取跑过"
                     "（.probe/g3_golden_capture.py 的 attach_progress_sample）")
    stray = sorted(carried - faces)
    assert not stray, (
        f"这些动作没有进度面却带着进度样本：{stray}——表里的 mode、函数体里发的事件、"
        "与样本记的交付方式必须同出一处")


def test_mutation_controls_show_the_progress_guards_are_not_deaf() -> None:
    """把这一族的样本逐处改坏一处，每一处都必须被 :func:`_check_progress_sample` 拒掉。

    没有这一支，上面那些判据可能只是在比一个恒等式（记忆 ``guard-tests-assert-absence``）。
    不写盘、不动真样本：改的是内存里的一份副本，喂回**同一个口**。
    """
    action, name = NAMED_FACE
    progress = _samples()[action]["progress_sample"]
    live = sorted(f.name for f in dataclasses.fields(getattr(event_module, name)))
    assert not _check_progress_sample(progress, name=name, live=live), (
        "现成的样本过不了自己的判据：判据写错了，后面每一发变异都测不出东西")
    assert len(live) >= 4, f"{name} 的字段少到不像有进度面：{live}"

    def reject(mutate, label: str) -> None:
        tampered = json.loads(json.dumps(progress))
        mutate(tampered)
        reason = _check_progress_sample(tampered, name=name, live=live)
        assert reason, f"{label}：改了却没被查出来"

    def add_key(sample: dict) -> None:
        sample["frames"][0]["payload"]["守卫不该放过这一格"] = 1

    def rename_event(sample: dict) -> None:
        sample["name"] = "SearchFinishedEvent"

    def rename_field(sample: dict) -> None:
        sample["payload_field_names"] = sorted(
            [*live[:1], "chapter_index", *live[1:]])

    def drop_frame(sample: dict) -> None:
        sample["frames"] = sample["frames"][:1]
        sample["frame_count"] = len(sample["frames"]) + 1

    def wrong_frame_shape(sample: dict) -> None:
        sample["frames"][0]["seq"] = 1

    def frame_named_otherwise(sample: dict) -> None:
        sample["frames"][0]["name"] = "SourceSwitchFinishedEvent"

    for mutate, label in ((add_key, "帧里多一个键"),
                          (rename_event, "整段换了事件名"),
                          (rename_field, "字段清单与现场不同"),
                          (drop_frame, "frame_count 与列表不同数"),
                          (wrong_frame_shape, "帧多带一格"),
                          (frame_named_otherwise, "某一帧挂着别的名字")):
        reject(mutate, label)
    # 正对照：空帧列表那一支也得真的会红（不然"至少一帧"是句空话）。
    empty = json.loads(json.dumps(progress))
    empty["frames"] = []
    empty["frame_count"] = 0
    assert _check_progress_sample(empty, name=name, live=live), "一帧都没有却没被查出来"
    # 这一支管的是进度，**不许**顺手把结果类事件当成进度面。
    assert name.endswith("ProgressEvent") and name in COALESCED_EVENTS, name
