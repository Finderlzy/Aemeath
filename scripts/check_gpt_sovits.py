"""Check local GPT-SoVITS api_v2 service status and connectivity.

Usage:
    python scripts/check_gpt_sovits.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "vendor" / "Open-LLM-VTuber" / "src"))
sys.path.insert(0, str(ROOT_DIR / "vendor" / "Open-LLM-VTuber"))
sys.path.insert(0, str(ROOT_DIR))

from aemeath.adapters import http_client  # noqa: E402
from aemeath.config import load_config, resolve_config_path  # noqa: E402


async def check_service(api_url: str) -> bool:
    print(f"检查 GPT-SoVITS 端点: {api_url}")
    base_url = api_url.rsplit("/tts", 1)[0]
    openapi_url = f"{base_url}/openapi.json"

    try:
        async with http_client(5.0) as client:
            resp = await client.get(openapi_url)
            if resp.status_code == 200:
                print(f"[OK] GPT-SoVITS 服务正常响应: HTTP 200 ({openapi_url})")
            else:
                print(f"[FAIL] 服务返回异常状态码: HTTP {resp.status_code}")
                return False

            # 测试 GET /tts 契约 (缺参考音频时预期 400 Bad Request)
            tts_test_url = f"{base_url}/tts"
            test_params = {
                "text": "测试",
                "text_lang": "zh",
                "prompt_lang": "zh",
                "ref_audio_path": "__probe_not_exist.wav",
                "streaming_mode": "false",
            }
            tts_resp = await client.get(tts_test_url, params=test_params)
            if tts_resp.status_code == 400:
                print(f"[OK] GET /tts 接口契约校验正常 (预期的 400 缺少参考音频): {tts_resp.status_code}")
                print("[INFO] 参考音频暂缺，未进行角色音色与听感验收。")
                return True
            else:
                print(f"[WARN] /tts 端点响应状态: HTTP {tts_resp.status_code}")
                return True
    except Exception as exc:
        print(f"[FAIL] 无法连接到 GPT-SoVITS 服务 ({api_url}): {exc}")
        print("请确认是否已运行: powershell scripts/start_gpt_sovits.ps1")
        return False


async def main() -> int:
    parser = argparse.ArgumentParser(description="检查 GPT-SoVITS 服务")
    parser.add_argument("--config", type=str, default="", help="配置文件路径")
    args = parser.parse_args()

    cfg_path = resolve_config_path(args.config if args.config else None)
    config = load_config(cfg_path)
    gpt_sovits_cfg = config.speech.gpt_sovits or {}
    api_url = str(gpt_sovits_cfg.get("api_url") or "http://127.0.0.1:9880/tts")

    ok = await check_service(api_url)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
