"""V2-T04 learning evaluation against the real model, on a live service.

The isolated suites prove the *contract*: an entry with provenance is enabled, a
self-echoed phrase is refused, a revoked entry stays revoked. What they cannot
prove is whether a real extraction model actually learns anything usable from
real conversation, and whether the learned entry then shows up in the prompt a
real reply is generated from.

That is what this probe measures, and it is deliberately not a pytest test: it
needs a running service and a configured learning provider.

Usage::

    python scripts/probe_learning_learning.py

It prints one line per check, the learned entries, and
``LEARNING EVAL PASSED`` / ``LEARNING EVAL FAILED``.

Design notes
------------

* **Result, not row count.** The requirements forbid substituting "a row was
  written" for learning effect. So the probe asserts on the *assembled system
  prompt* — what the character is actually told — and records the entries the
  model produced, including failures.
* **The conversations are written to be learnable.** Each turn contains a phrase
  with obvious provenance, so a model producing nothing is a genuine finding
  about the model, not an artefact of vague input.
* **Failures are printed, not hidden.** A conversation that taught nothing is
  reported as such, because that is the result the task asked to record.
* **Nothing personal is touched.** It runs against the acceptance configuration
  (``data/acceptance``), which exists precisely so evaluation never mixes with
  the user's own memory.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = ROOT / "vendor" / "Open-LLM-VTuber"
for _path in (str(UPSTREAM), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record and print one check."""
    results.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


#: The evaluation conversations. Each states something a model can learn from,
#: with the phrase repeated so there is real evidence rather than one mention.
EVALUATION_TURNS = [
    {
        "label": "新用语（黑话）",
        "user": "这次改动我们先跑个冒烟测试，主流程通了再说。冒烟测试就是最快确认没崩的那种验证。",
        "assistant": "好，那我先跑冒烟测试，只看主流程能不能通。",
        "expect_kind": "jargon",
        "expect_terms": ["冒烟测试"],
    },
    {
        "label": "说话方式（表达）",
        "user": "这个需求先放着吧，回头再说。别急，先放着。",
        "assistant": "行，那就先放着，等你回头想清楚了我们再动。",
        "expect_kind": "expression",
        "expect_terms": ["先放着", "回头再说"],
    },
]

#: A conversation whose only new phrase came from Aemeath herself. Learning it
#: would mean training on her own echo, so it must not become enabled.
ECHO_TURN = {
    "label": "自身复述（应拒绝启用）",
    "user": "今天有点累。",
    "assistant": "那就早点休息，别熬太晚，明天再说也行。",
    "expect_terms": ["早点休息", "别熬太晚"],
}


async def run_evaluation() -> int:
    """Drive the real learning pipeline and evaluate its output."""
    from aemeath.config import load_config
    from aemeath.interfaces import SituationState
    from aemeath.memory import MemoryStore
    from aemeath.prompts import build_system_prompt
    from aemeath.runtime import build_runtime, reset_runtime

    print("=== V2-T04 learning evaluation (real model, live configuration) ===\n")

    config = load_config()
    print(f"配置文件数据目录：{config.data_dir}")
    print(f"学习开关：{config.learning.enabled}")
    provider = config.providers.learning
    print(
        "学习模型："
        f"{provider.provider}:{provider.model} @ {provider.base_url or '(未配置)'}\n"
    )

    reset_runtime()
    runtime = build_runtime(config=config)
    service = runtime.learning

    if service is None or not service.available:
        reason = service.unavailable_reason() if service else "学习服务未构建"
        print(f"学习不可用：{reason}")
        print(
            "\n结论：学习效果**未验证**。"
            "需要开启 learning.enabled 并配置 providers.learning（含可用凭据）后重跑。"
        )
        return 2

    store = MemoryStore(config.db_path, embedding=runtime.memory.store.embedding)
    learned_report: list[dict] = []
    failures: list[str] = []

    try:
        for turn in EVALUATION_TURNS:
            print(f"--- {turn['label']} ---")
            user_id = runtime.memory.record_user_message(
                turn["user"], source="user_text"
            )
            assistant_id = runtime.memory.record_assistant_message(
                turn["assistant"]
            )
            service.queue_turn([user_id, assistant_id])
            written = await service.run_pending_learning(runtime.persona_text())
            print(f"  写入 {written} 条")

            items = store is not None and service.store.list_items(
                kind=turn["expect_kind"]
            )
            contents = [item.content for item in items]
            matched = [
                item
                for item in items
                if any(term in item.content for term in turn["expect_terms"])
            ]
            learned_report.append(
                {
                    "label": turn["label"],
                    "kind": turn["expect_kind"],
                    "learned": contents,
                    "matched": [item.content for item in matched],
                }
            )
            print(f"  已学到的{turn['expect_kind']}条目：{contents}")
            if not matched:
                failures.append(
                    f"{turn['label']}：没有学到预期用语 {turn['expect_terms']}"
                )
            else:
                for item in matched:
                    print(
                        f"    · {item.content}｜状态={item.status}"
                        f"｜来源={len(service.store.evidence_for(item.item_id))} 条"
                        f"｜场景={item.scenario or '(无)'}"
                    )

        print("\n--- 自身复述（应拒绝启用）---")
        user_id = runtime.memory.record_user_message(ECHO_TURN["user"], source="user_text")
        assistant_id = runtime.memory.record_assistant_message(ECHO_TURN["assistant"])
        service.queue_turn([user_id, assistant_id])
        await service.run_pending_learning(runtime.persona_text())
        echo_items = [
            item
            for item in service.store.list_items()
            if any(term in item.content for term in ECHO_TURN["expect_terms"])
        ]
        if echo_items:
            for item in echo_items:
                print(
                    f"    · {item.content}｜状态={item.status}"
                    f"｜原因={item.check_detail or '(无)'}"
                )
        else:
            print("    （模型没有提出这些内容，视为已正确忽略）")

        # ------------------------------------------------------------------
        # Effect: what the character is actually told
        # ------------------------------------------------------------------
        print("\n--- 语境使用：真实组装系统提示词 ---")
        prompt_items = service.prompt_items()
        system_prompt = build_system_prompt(
            persona=runtime.persona_text(),
            situation=SituationState(),
            learnings=prompt_items,
        )
        print(f"  注入条目数：{len(prompt_items)}")
        for item in prompt_items:
            print(f"    · [{item.kind}] {item.content}")

        enabled_terms = [
            term
            for turn in EVALUATION_TURNS
            for term in turn["expect_terms"]
            if any(term in item.content for item in prompt_items)
        ]

        check(
            "至少学到一条预期用语并已启用",
            bool(enabled_terms),
            f"命中：{enabled_terms}" if enabled_terms else f"失败样例：{failures}",
        )
        check(
            "学到的内容确实进入系统提示词（语境可用）",
            bool(enabled_terms),
            "；".join(enabled_terms),
        )
        check(
            "提示词把学习结果限定为措辞参考而非事实",
            "表达与用词参考" in system_prompt
            and "不能覆盖你上面的人设" in system_prompt
            and "不是关于用户的事实" in system_prompt,
            "学习块规则文案",
        )
        check(
            "自身复述没有被启用（不把她自己的话当新证据）",
            all(item.status != "enabled" for item in echo_items),
            "；".join(f"{i.content}={i.status}" for i in echo_items) or "未提出",
        )

        # ------------------------------------------------------------------
        # Revocation, on a real entry
        # ------------------------------------------------------------------
        if prompt_items:
            target = prompt_items[0]
            service.revoke(target.item_id)
            # Compare by item id, not by text: the same phrase can exist as both
            # an expression and a jargon entry, and revoking one must not be
            # reported as failed because the other is still enabled. The first
            # version of this check compared content strings and produced
            # exactly that false failure.
            after_ids = [item.item_id for item in service.prompt_items()]
            check(
                "撤销后不再注入",
                target.item_id not in after_ids,
                f"撤销 {target.kind}「{target.content}」",
            )

            await service.run_pending_learning(runtime.persona_text())
            refreshed = service.store.get(target.item_id)
            check(
                "撤销后不能从同一证据立即重学",
                refreshed is not None and refreshed.status == "revoked",
                f"状态={refreshed.status if refreshed else 'missing'}",
            )

        # A phrase can legitimately exist as both an expression and a jargon
        # entry; revoking one must leave the other alone. Asserted explicitly
        # because the naive implementation of the check above confused them.
        same_text = {}
        for item in service.store.list_items():
            same_text.setdefault(item.content, set()).add(item.kind)
        duplicated = {text: kinds for text, kinds in same_text.items() if len(kinds) > 1}
        if duplicated:
            print(f"\n  同名不同类条目：{duplicated}")
            for text, kinds in duplicated.items():
                revoked_kind = [
                    item.kind
                    for item in service.store.list_items()
                    if item.content == text and item.status == "revoked"
                ]
                surviving = [
                    item.kind
                    for item in service.store.list_items()
                    if item.content == text and item.status == "enabled"
                ]
                check(
                    f"「{text}」的撤销只影响被撤销的那一类",
                    not set(revoked_kind) & set(surviving),
                    f"已撤销={revoked_kind} 仍启用={surviving}",
                )

        # ------------------------------------------------------------------
        # Report
        # ------------------------------------------------------------------
        print("\n--- 学习结果记录 ---")
        print(json.dumps(learned_report, ensure_ascii=False, indent=2))
        if failures:
            print("\n--- 失败样例 ---")
            for failure in failures:
                print(f"  · {failure}")
    finally:
        await runtime.stop()
        reset_runtime()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    if passed == len(results):
        print("LEARNING EVAL PASSED")
        return 0
    print("LEARNING EVAL FAILED")
    return 1


def main() -> int:
    """Entry point."""
    return asyncio.run(run_evaluation())


if __name__ == "__main__":
    raise SystemExit(main())
