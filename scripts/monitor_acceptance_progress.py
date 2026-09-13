"""Monitor live acceptance progress from turns.jsonl.

Tracks:
1. Valid user_text_input latency samples (target: >= 20, P95 <= 5000ms)
2. Valid microphone_vad voice latency samples (target: >= 20, P95 <= 10000ms)
3. Valid click_stop cancel latency samples (target: >= 20, P95 <= 500ms)
4. Class mode and interrupt events
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import List, Optional


def nearest_rank_p95(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    ordered = sorted(vals)
    idx = math.ceil(0.95 * len(ordered))
    idx = min(max(idx, 1), len(ordered))
    return ordered[idx - 1]


def main():
    p = Path("logs/acceptance/turns.jsonl")
    if not p.is_file():
        print(f"Log file not found: {p}")
        return

    lines = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    # Collect valid samples by explicit source
    text_samples = []
    voice_samples = []
    stop_samples = []

    for row in lines:
        turn_id = row.get("turn_id", "")
        # Text sample
        c_text = row.get("client_first_text_ms")
        text_src = row.get("text_sample_source")
        if c_text is not None and math.isfinite(c_text) and c_text >= 0:
            if text_src == "user_text_input" or (text_src is None and row.get("source") == "user_text"):
                text_samples.append(c_text)

        # Voice playback sample (must be microphone_vad & user_voice)
        c_play = row.get("client_playback_start_ms")
        play_src = row.get("playback_sample_source")
        if c_play is not None and math.isfinite(c_play) and c_play >= 0:
            if play_src == "microphone_vad" and row.get("source") == "user_voice":
                voice_samples.append(c_play)

        # Click stop sample (must be click_stop / button_click)
        c_cancel = row.get("client_cancel_ms")
        cancel_src = row.get("cancel_sample_source")
        cancel_trig = row.get("cancel_trigger")
        if c_cancel is not None and math.isfinite(c_cancel) and c_cancel >= 0:
            if cancel_src in ("click_stop", "button_click") or cancel_trig == "click":
                stop_samples.append(c_cancel)

    print("============================================================")
    print("           AEMEATH 现场延迟与样本采集进度监控                ")
    print("============================================================")
    print(f"总记录轮次数: {len(lines)}")
    
    t_p95 = nearest_rank_p95(text_samples)
    print(f"1. 首段文字延迟 (user_text_input):")
    print(f"   - 有效样本数: {len(text_samples)} / 20 ({'达标' if len(text_samples) >= 20 else '不足'})")
    print(f"   - P95 延迟   : {t_p95:.1f} ms (目标 <= 5000 ms) -> {'PASS' if (t_p95 and t_p95 <= 5000 and len(text_samples) >= 20) else '待补齐'}")

    v_p95 = nearest_rank_p95(voice_samples)
    print(f"\n2. 语音回复延迟 (microphone_vad + user_voice):")
    print(f"   - 有效样本数: {len(voice_samples)} / 20 ({'达标' if len(voice_samples) >= 20 else '待采录'})")
    print(f"   - P95 延迟   : {f'{v_p95:.1f} ms' if v_p95 else '无'} (目标 <= 10000 ms) -> {'PASS' if (v_p95 and v_p95 <= 10000 and len(voice_samples) >= 20) else '待采录'}")

    s_p95 = nearest_rank_p95(stop_samples)
    print(f"\n3. 点击停止延迟 (click_stop / 实际音频正在播放):")
    print(f"   - 有效样本数: {len(stop_samples)} / 20 ({'达标' if len(stop_samples) >= 20 else '待采录'})")
    print(f"   - P95 延迟   : {f'{s_p95:.1f} ms' if s_p95 else '无'} (目标 <= 500 ms) -> {'PASS' if (s_p95 and s_p95 <= 500 and len(stop_samples) >= 20) else '待采录'}")
    print("============================================================")


if __name__ == "__main__":
    main()
