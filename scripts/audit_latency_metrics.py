"""Audit and summarize latency sampling records.

Validates samples according to acceptance criteria:
- Text-to-display (client_first_text_ms): P95 <= 5000 ms, source: user_text_input
- Voice-to-playback (client_playback_start_ms): P95 <= 10000 ms, source: microphone_vad
- Click-to-silence (client_cancel_ms): P95 <= 500 ms, source: click_stop (actively playing audio)
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional


def nearest_rank_percentile(values: List[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = math.ceil(fraction * len(ordered))
    index = min(max(index, 1), len(ordered))
    return ordered[index - 1]


def analyze_turns_file(log_path: Path) -> Dict[str, Any]:
    if not log_path.is_file():
        return {"error": f"file not found: {log_path}"}

    raw_lines = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    total_turns = len(raw_lines)
    cancelled_turns = 0
    error_turns = 0

    valid_text_samples: List[float] = []
    valid_voice_samples: List[float] = []
    valid_stop_samples: List[float] = []

    unverified_samples: List[Dict[str, Any]] = []
    invalid_samples: List[Dict[str, Any]] = []

    for row in raw_lines:
        turn_id = row.get("turn_id", "")
        source = row.get("source", "")
        cancelled = bool(row.get("cancelled", False))
        err = row.get("error")

        if cancelled:
            cancelled_turns += 1
        if err:
            error_turns += 1

        # 1. Text latency
        c_text = row.get("client_first_text_ms")
        text_src = row.get("text_sample_source")
        if c_text is not None:
            if not (math.isfinite(c_text) and c_text >= 0):
                invalid_samples.append({"turn_id": turn_id, "type": "text", "value": c_text, "reason": "non-finite or negative"})
            elif text_src == "user_text_input" or (text_src is None and source == "user_text"):
                # If explicit source is given, or fallback for legacy
                valid_text_samples.append(c_text)
            else:
                unverified_samples.append({"turn_id": turn_id, "type": "text", "value": c_text, "source": text_src or source})

        # 2. Voice playback latency
        c_play = row.get("client_playback_start_ms")
        play_src = row.get("playback_sample_source")
        if c_play is not None:
            if not (math.isfinite(c_play) and c_play >= 0):
                invalid_samples.append({"turn_id": turn_id, "type": "voice", "value": c_play, "reason": "non-finite or negative"})
            elif play_src == "microphone_vad" and source == "user_voice":
                valid_voice_samples.append(c_play)
            else:
                # Legacy samples where source was 'user_text' or playback_sample_source missing
                unverified_samples.append({
                    "turn_id": turn_id,
                    "type": "voice",
                    "value": c_play,
                    "turn_source": source,
                    "sample_source": play_src,
                    "reason": "marked for verification (source is user_text or missing microphone_vad)",
                })

        # 3. Stop latency
        c_cancel = row.get("client_cancel_ms")
        cancel_src = row.get("cancel_sample_source")
        cancel_trigger = row.get("cancel_trigger")
        if c_cancel is not None:
            if not (math.isfinite(c_cancel) and c_cancel >= 0):
                invalid_samples.append({"turn_id": turn_id, "type": "stop", "value": c_cancel, "reason": "non-finite or negative"})
            elif cancel_src in ("click_stop", "button_click") or cancel_trigger == "click":
                valid_stop_samples.append(c_cancel)
            elif abs(c_cancel - 12.5) < 1e-3:
                # Constant 12.5 ms synthetic execution artifact
                unverified_samples.append({
                    "turn_id": turn_id,
                    "type": "stop",
                    "value": c_cancel,
                    "reason": "marked for verification (constant 12.5ms artifact without playing audio proof)",
                })
            else:
                unverified_samples.append({
                    "turn_id": turn_id,
                    "type": "stop",
                    "value": c_cancel,
                    "reason": "missing explicit click_stop source",
                })

    def calc_stats(samples: List[float], target_ms: float) -> Dict[str, Any]:
        count = len(samples)
        p50 = nearest_rank_percentile(samples, 0.50)
        p95 = nearest_rank_percentile(samples, 0.95)
        return {
            "count": count,
            "sufficient": count >= 20,
            "p50_ms": p50,
            "p95_ms": p95,
            "target_ms": target_ms,
            "pass": (p95 <= target_ms) if p95 is not None and count >= 20 else False,
        }

    return {
        "file": str(log_path),
        "total_turns": total_turns,
        "cancelled_turns": cancelled_turns,
        "error_turns": error_turns,
        "metrics": {
            "first_text": calc_stats(valid_text_samples, 5000.0),
            "voice_reply": calc_stats(valid_voice_samples, 10000.0),
            "click_stop": calc_stats(valid_stop_samples, 500.0),
        },
        "unverified_count": len(unverified_samples),
        "invalid_count": len(invalid_samples),
        "unverified_samples": unverified_samples,
        "invalid_samples": invalid_samples,
    }


def main():
    parser = argparse.ArgumentParser(description="Audit Aemeath latency logs.")
    parser.add_argument("log_path", nargs="?", default="logs/acceptance/turns.jsonl", help="Path to turns.jsonl")
    parser.add_argument("--json", action="store_true", help="Print json output")
    args = parser.parse_args()

    result = analyze_turns_file(Path(args.log_path))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Log file: {result.get('file')}")
        print(f"Total turns: {result.get('total_turns')}, Cancelled: {result.get('cancelled_turns')}, Errors: {result.get('error_turns')}")
        print(f"Unverified samples: {result.get('unverified_count')}, Invalid: {result.get('invalid_count')}")
        for name, m in result.get("metrics", {}).items():
            print(f"- {name:12}: count={m['count']} (sufficient={m['sufficient']}), P50={m['p50_ms']}ms, P95={m['p95_ms']}ms, target<={m['target_ms']}ms, pass={m['pass']}")


if __name__ == "__main__":
    main()
