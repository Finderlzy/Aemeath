"""Verify SenseVoice ASR model and local microphone transcription.

Usage:
    python scripts/verify_mic_sensevoice.py                 # test reference wav + record 3s from mic
    python scripts/verify_mic_sensevoice.py --seconds 5     # record 5s
    python scripts/verify_mic_sensevoice.py --file PATH     # transcribe an existing wav file
    python scripts/verify_mic_sensevoice.py --devices       # list audio devices only
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "vendor" / "Open-LLM-VTuber" / "src"))
sys.path.insert(0, str(ROOT_DIR / "vendor" / "Open-LLM-VTuber"))
sys.path.insert(0, str(ROOT_DIR))

from aemeath.adapters import SenseVoiceAdapter, AdapterFactory  # noqa: E402
from aemeath.config import load_config, resolve_config_path  # noqa: E402


def list_devices() -> None:
    """Print available audio input devices."""
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        print("\n=== 可用音频输入设备 ===")
        for idx, dev in enumerate(devices):
            if dev.get("max_input_channels", 0) > 0:
                is_default = idx == sd.default.device[0]
                marker = " [默认输入]" if is_default else ""
                print(f"[{idx}] {dev['name']} (输入通道: {dev['max_input_channels']}, 采样率: {dev['default_samplerate']}Hz){marker}")
    except Exception as exc:
        print(f"获取音频设备列表失败: {exc}")


def record_from_mic(duration: float = 3.0, sample_rate: int = 16000) -> np.ndarray:
    """Record audio from default microphone."""
    import sounddevice as sd

    print(f"\n[麦克风录音] 正在录制 {duration} 秒音频 (采样率 {sample_rate}Hz)...")
    print(">>> 请对麦克风说话...")
    recording = sd.rec(int(duration * sample_rate), samplerate=sample_rate, channels=1, dtype="float32")
    sd.wait()
    print(">>> 录音结束。")
    return recording.flatten()


async def main() -> int:
    parser = argparse.ArgumentParser(description="SenseVoice ASR & 麦克风转写验证")
    parser.add_argument("--seconds", type=float, default=3.0, help="麦克风录音时长（秒，默认 3.0）")
    parser.add_argument("--file", type=str, default="", help="指定现有的 WAV 音频文件路径进行转写")
    parser.add_argument("--devices", action="store_true", help="仅列出音频设备并退出")
    parser.add_argument("--config", type=str, default="", help="配置文件路径")
    parser.add_argument("--skip-mic", action="store_true", help="跳过麦克风录制，仅做基准音频转写")
    args = parser.parse_args()

    print("=" * 60)
    print("Aemeath SenseVoice ASR 验证工具")
    print("=" * 60)

    if args.devices:
        list_devices()
        return 0

    # 1. 加载配置并初始化 SenseVoiceAdapter
    cfg_path = resolve_config_path(args.config if args.config else None)
    config = load_config(cfg_path)
    factory = AdapterFactory.from_config(config)
    adapter, status = factory.build_asr()

    if adapter is None or status.state != "ready":
        print(f"[FAIL] SenseVoice 适配器构建失败: {status.error or status.detail}")
        return 1
    print(f"[OK] SenseVoice 适配器已就绪: {status.detail}")

    # 2. 基准音频转写验证
    ref_wav = (
        ROOT_DIR
        / "vendor"
        / "Open-LLM-VTuber"
        / "models"
        / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
        / "test_wavs"
        / "zh.wav"
    )
    if ref_wav.is_file():
        t0 = time.monotonic()
        ref_text = await adapter.transcribe(ref_wav.read_bytes())
        t_ref = (time.monotonic() - t0) * 1000
        print(f"[OK] 基准测试音频转写 ({t_ref:.1f}ms): {ref_text!r}")
    else:
        print("[WARN] 基准测试音频文件未找到，跳过基准校验。")

    # 3. 如果指定了文件，则转写文件
    if args.file:
        file_path = Path(args.file)
        if not file_path.is_file():
            print(f"[FAIL] 指定的音频文件不存在: {args.file}")
            return 1
        t0 = time.monotonic()
        text = await adapter.transcribe(file_path.read_bytes())
        t_cost = (time.monotonic() - t0) * 1000
        print(f"[OK] 文件转写结果 ({t_cost:.1f}ms): {text!r}")
        return 0

    # 4. 验证麦克风转写
    if args.skip_mic:
        print("[INFO] --skip-mic 已设置，跳过麦克风录制。")
        return 0

    list_devices()

    try:
        audio = record_from_mic(duration=args.seconds, sample_rate=16000)
    except Exception as exc:
        print(f"[FAIL] 麦克风录音失败: {exc}")
        return 1

    rms = float(np.sqrt(np.mean(audio**2)))
    peak = float(np.max(np.abs(audio)))
    print(f"\n[录音信号分析] 采样点数: {len(audio)}, RMS 电平: {rms:.6f}, 峰值: {peak:.6f}")
    if peak < 0.001:
        print("[提示] 录音电平极低（接近绝对静音），可能未对麦克风说话或麦克风被静音。")

    t0 = time.monotonic()
    mic_text = adapter.transcribe_np(audio, 16000)
    t_cost = (time.monotonic() - t0) * 1000

    disp_text = repr(mic_text) if mic_text else "(no clear speech / silence detected)"
    print(f"\n[转写结果] 耗时: {t_cost:.1f}ms")
    print(f"转写文本: {disp_text}")
    print("=" * 60)
    print("SenseVoice 本地麦克风转写链路验证通过！")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
