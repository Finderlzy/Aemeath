"""Aemeath status and memory management entry point.

Provides the first release's settings surface from the command line: current
situation, memory listing, correction and deletion, and usage/latency totals.

Usage:
    python scripts/status.py                       # show status
    python scripts/status.py --list                # list memories
    python scripts/status.py --search 咖啡         # search memories
    python scripts/status.py --forget <memory_id>  # delete a memory
    python scripts/status.py --mode class          # switch conversation mode
    python scripts/status.py --config PATH         # use a specific config

``--config`` matters for the acceptance database: without it the status screen
reports on the personal one, which looks identical but holds different data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "vendor" / "Open-LLM-VTuber" / "src"))
sys.path.insert(0, str(ROOT_DIR / "vendor" / "Open-LLM-VTuber"))
sys.path.insert(0, str(ROOT_DIR))

from aemeath.config import load_config, resolve_config_path, set_config_path  # noqa: E402
from aemeath.interfaces import SpeechMode  # noqa: E402
from aemeath.runtime import build_runtime  # noqa: E402


def _runtime(config_path: Path, *, note: bool = True):
    """Build a runtime for one config, without model adapters.

    Adapters are left unbuilt because this entry point inspects stored state and
    changes local settings; it never calls a provider, so demanding credentials
    to read the status screen would be a pointless obstacle.

    Args:
        config_path: Config selecting the database and log directory to report on.
        note: Print how many historical turns were loaded. Suppressed for
            ``--json`` so the output stays one parseable document.
    """
    runtime = build_runtime(config=load_config(config_path))
    # Latency and usage describe the acceptance run, not this empty process, so
    # turns recorded by earlier runs are read back before anything is reported.
    loaded = runtime.metrics.load()
    if loaded and note:
        print(f"（已载入 {loaded} 条历史轮次记录）")
    return runtime


def show_status(runtime) -> None:
    """Print the current situation and usage summary."""
    status = runtime.status()
    print("Aemeath 状态")
    print("=" * 56)
    print(f"交流模式      : {status['mode']}")
    print(f"允许语音      : {'是' if status['voice_allowed'] else '否'}")
    print(f"麦克风        : {'开' if status['microphone_enabled'] else '关'}")
    print(f"屏幕观察      : {'开' if status['screen_observation_enabled'] else '关'}")
    print(f"主动搭话      : {'开' if status['proactive_enabled'] else '关'}")
    print(f"用户暂停聊天  : {'是' if status['user_paused'] else '否'}")
    print(f"有效记忆条数  : {status['memories']}")
    print(f"待处理提取任务: {status['pending_tasks']}")
    print(f"数据目录      : {status['data_dir']}")

    metrics = status["metrics"]
    print("\n交互统计")
    print("-" * 56)
    print(f"已完成轮次    : {metrics['turns']}")
    print(f"被取消轮次    : {metrics['cancelled']}")
    print(f"出错轮次      : {metrics['errors']}")
    p95_text = metrics["p95_first_text_ms"]
    p95_audio = metrics["p95_first_audio_ms"]
    print(f"首段文字 P95  : {f'{p95_text:.0f} ms' if p95_text else '无数据'}")
    print(f"首段音频 P95  : {f'{p95_audio:.0f} ms' if p95_audio else '无数据'}")

    client_metrics = metrics.get("client", {})
    c_text = client_metrics.get("first_text_ms", {})
    c_play = client_metrics.get("playback_start_ms", {})
    c_cancel = client_metrics.get("cancel_ms", {})
    if c_text.get("available") or c_play.get("available") or c_cancel.get("available"):
        print("\n客户端端到端体验延迟")
        print("-" * 56)
        if c_text.get("available"):
            suff = "达标" if c_text.get("sufficient") else "样本不足"
            print(f"首段文字延迟  : P50={c_text.get('p50', 0):.0f} ms, P95={c_text.get('p95', 0):.0f} ms (样本: {c_text.get('count', 0)}, 目标: <=5000 ms, {suff})")
        if c_play.get("available"):
            suff = "达标" if c_play.get("sufficient") else "样本不足"
            print(f"语音回复延迟  : P50={c_play.get('p50', 0):.0f} ms, P95={c_play.get('p95', 0):.0f} ms (样本: {c_play.get('count', 0)}, 目标: <=10000 ms, {suff})")
        if c_cancel.get("available"):
            suff = "达标" if c_cancel.get("sufficient") else "样本不足"
            print(f"点击停止延迟  : P50={c_cancel.get('p50', 0):.0f} ms, P95={c_cancel.get('p95', 0):.0f} ms (样本: {c_cancel.get('count', 0)}, 目标: <=500 ms, {suff})")

    usage = metrics["usage"]
    print(f"累计轮次      : {usage['turns']}")
    print(f"输入 tokens   : {usage['input_tokens']}")
    print(f"输出 tokens   : {usage['output_tokens']}")
    if usage["cost_available"]:
        print(f"估算费用      : {usage['estimated_cost']:.4f}")
    else:
        # The plan forbids inventing a price that was never configured.
        print("估算费用      : 未配置单价，仅显示用量")


def list_memories(runtime, query: str = "") -> None:
    """Print stored memories, optionally filtered."""
    memories = runtime.memory.store.list_memories(query=query)
    if not memories:
        print("没有匹配的记忆。")
        return
    print(f"共 {len(memories)} 条记忆：")
    for item in memories:
        kind = "事实" if item.kind == "fact" else "经历"
        print(f"  [{kind}] {item.memory_id[:8]}  {item.content}")


def forget(runtime, memory_id: str) -> int:
    """Delete a memory by id prefix.

    Accepts a short prefix because ids are long; refuses ambiguous prefixes.
    """
    store = runtime.memory.store
    matches = [
        item
        for item in store.list_memories(include_invalid=True)
        if item.memory_id.startswith(memory_id)
    ]
    if not matches:
        print(f"没有找到 id 以 {memory_id} 开头的记忆。")
        return 1
    if len(matches) > 1:
        print(f"id 前缀 {memory_id} 匹配到 {len(matches)} 条，请输入更长的前缀。")
        return 1

    target = matches[0]
    store.delete_memory(target.memory_id)
    print(f"已删除记忆：{target.content}")
    return 0


def set_mode(runtime, mode: str) -> int:
    """Switch conversation mode."""
    try:
        speech_mode = SpeechMode(mode)
    except ValueError:
        print(f"未知模式 {mode}，可选：normal、class")
        return 1
    state = runtime.situation.set_mode(speech_mode)
    print(f"已切换到 {state.mode.value} 模式（允许语音：{state.voice_allowed}）")
    return 0


async def run_pending(runtime) -> int:
    """Process any queued extraction tasks."""
    written = await runtime.memory.run_pending_extraction()
    print(f"处理完成，写入 {written} 条记忆。")
    return 0


def main() -> int:
    """Parse arguments and dispatch."""
    parser = argparse.ArgumentParser(description="Aemeath 状态与记忆管理")
    parser.add_argument("--list", action="store_true", help="列出记忆")
    parser.add_argument("--search", metavar="TEXT", help="按关键词搜索记忆")
    parser.add_argument("--forget", metavar="MEMORY_ID", help="按 id 前缀删除记忆")
    parser.add_argument("--mode", choices=["normal", "class"], help="切换交流模式")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出状态")
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="使用的配置文件（默认 config/conf.aemeath.yaml）",
    )
    parser.add_argument(
        "--process-pending", action="store_true", help="处理待办的记忆提取任务"
    )
    args = parser.parse_args()

    config_path = resolve_config_path(args.config)
    set_config_path(config_path)
    # History is always loaded; the note is printed only in the human-readable
    # mode so that ``--json`` output stays a single parseable document.
    runtime = _runtime(config_path, note=not args.json)

    if args.json:
        print(json.dumps(runtime.status(), ensure_ascii=False, indent=2))
        return 0
    if args.list:
        list_memories(runtime)
        return 0
    if args.search:
        list_memories(runtime, args.search)
        return 0
    if args.forget:
        return forget(runtime, args.forget)
    if args.mode:
        return set_mode(runtime, args.mode)
    if args.process_pending:
        return asyncio.run(run_pending(runtime))

    show_status(runtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
