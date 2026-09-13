import soundfile as sf
import numpy as np
import scipy.signal
import sherpa_onnx
import sys
import json
import os
import re

sys.path.append('E:/WorkSpace/Tools/GPT-SoVITS')
from tools.slicer2 import Slicer

recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model='./vendor/Open-LLM-VTuber/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/model.int8.onnx',
    tokens='./vendor/Open-LLM-VTuber/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/tokens.txt',
    num_threads=4,
    use_itn=True
)

slicer = Slicer(
    sr=44100,
    threshold=-35,
    min_length=3000,
    min_interval=300,
    hop_size=10,
    max_sil_kept=400
)

y, sr = sf.read('E:/WorkSpace/Aemeath/data/raw_audio/aemeath_raw.wav')
y_mono = np.mean(y, axis=1)
chunks = list(slicer.slice(y_mono))

clean_candidates = []
for i, (chunk, start, end) in enumerate(chunks):
    dur = (end - start) / sr
    if not (3.0 <= dur <= 8.5):
        continue
        
    pre_start = max(0, start - int(0.2 * sr))
    pre = y_mono[pre_start:start]
    pre_rms = np.sqrt(np.mean(pre**2)) if len(pre) > 0 else 0
    
    post_end = min(len(y_mono), end + int(0.2 * sr))
    post = y_mono[end:post_end]
    post_rms = np.sqrt(np.mean(post**2)) if len(post) > 0 else 0
    
    chunk_rms = np.sqrt(np.mean(chunk**2))
    chunk_max = np.max(np.abs(chunk))
    
    if pre_rms < 0.001 and post_rms < 0.001 and chunk_rms > 0.008:
        # Transcribe
        audio16k = scipy.signal.resample_poly(chunk, 160, 441)
        stream = recognizer.create_stream()
        stream.accept_waveform(16000, audio16k)
        recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        
        zh_count = len(re.findall(r'[\u4e00-\u9fa5]', text))
        if zh_count >= 6:
            clean_candidates.append({
                'id': len(clean_candidates) + 1,
                'chunk_idx': i,
                'start_sec': round(start / sr, 3),
                'end_sec': round(end / sr, 3),
                'duration': round(dur, 3),
                'rms': round(float(chunk_rms), 4),
                'max_abs': round(float(chunk_max), 4),
                'text': text,
                'raw_chunk': chunk
            })

print('Selected clean lines:', len(clean_candidates))

output_dir = 'E:/WorkSpace/Aemeath/data/extracted_clean_voices'
os.makedirs(output_dir, exist_ok=True)

manifest = []
for item in clean_candidates:
    clip_id = item['id']
    fname = f"aemeath_clean_{clip_id:03d}.wav"
    fpath = os.path.join(output_dir, fname)
    chunk = item['raw_chunk']
    norm_chunk = chunk / np.max(np.abs(chunk)) * 0.89
    sf.write(fpath, norm_chunk, 44100, subtype='PCM_16')
    manifest.append({
        'filename': fname,
        'path': fpath,
        'start_sec': item['start_sec'],
        'end_sec': item['end_sec'],
        'duration': item['duration'],
        'rms': round(float(np.sqrt(np.mean(norm_chunk**2))), 4),
        'text': item['text']
    })

with open(os.path.join(output_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)

with open(os.path.join(output_dir, 'transcripts.list'), 'w', encoding='utf-8') as f:
    for m in manifest:
        f.write(f"{m['path']}|Aemeath|ZH|{m['text']}\n")

print(f"Done! Saved {len(manifest)} clips and manifest to {output_dir}")
