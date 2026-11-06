#!/usr/bin/env python3
"""Generate Sphinx's synthetic reference with local Qwen VoiceDesign weights, offline."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import time


REFERENCE_TEXT = (
    "Hallo, ich bin Sphinx. Ich höre dir zu, ordne deine Anfrage ein und antworte "
    "klar und verständlich. Du kannst ganz normal mit mir sprechen."
)
DESCRIPTION = (
    "Eine eigenständige, eindeutig männliche deutsche Stimme eines erwachsenen Mannes "
    "mit warmer, mitteltiefer Baritonlage. Sachlich, gelassen, klar und aufmerksam. "
    "Zurückhaltende Sprechenergie, weiche Satzanfänge und dezente Betonung. "
    "Leicht technische Klangfarbe durch klare Konsonanten, kleine kontrollierte "
    "Tonhöhenbewegungen und gleichmäßige, angenehme Lautstärke. Natürlich und fließend "
    "sprechen, in entspanntem normalem Gesprächstempo ohne Drängen, mit kurzen "
    "natürlichen Pausen. Wach und präsent bleiben, nicht müde, flüsternd oder schleppend. "
    "Akzentfreies Standardhochdeutsch. Natürlich resonant, ohne künstlich übertiefe "
    "oder gepresste Stimme. Keine Werbe- oder Radiostimme, keine übertriebene Begeisterung "
    "und kein metallischer Robotereffekt. Sphinx wird als ein Wort ausgesprochen."
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True,
                        help="Existing local VoiceDesign snapshot; no download is allowed")
    parser.add_argument("--output", type=Path, default=Path("models/voice/sphinx-male-calm.wav"))
    parser.add_argument("--seed", type=int, default=20261030)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--threads", type=int, default=8)
    args = parser.parse_args()

    model_dir = args.model_dir.resolve(strict=True)
    for name in ("config.json", "model.safetensors", "speech_tokenizer/model.safetensors"):
        if not (model_dir / name).is_file():
            raise SystemExit(f"Required cached file is missing: {name}")
    paths = [args.output, args.output.with_suffix(".txt"), args.output.with_suffix(".json")]
    if args.output.suffix != ".wav" or any(path.exists() for path in paths):
        raise SystemExit("Choose a new .wav output path; existing voice files are never overwritten.")

    # Set before importing Hugging Face. Both model and tokenizer use local files only.
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
    import importlib.metadata
    import numpy as np
    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel
    from transformers import set_seed

    torch.set_num_threads(args.threads)
    set_seed(args.seed)
    started = time.monotonic()
    print(f"Loading cached VoiceDesign on {args.device}; downloads disabled.", flush=True)
    model = Qwen3TTSModel.from_pretrained(
        str(model_dir), device_map=args.device, local_files_only=True,
        dtype=torch.float32 if args.device == "cpu" else torch.bfloat16,
        attn_implementation="sdpa",
    )
    print("Generating calm male German reference with restrained emphasis.", flush=True)
    with torch.inference_mode():
        wavs, sample_rate = model.generate_voice_design(
            text=REFERENCE_TEXT, language="German", instruct=DESCRIPTION,
            max_new_tokens=360,
        )
    audio = np.asarray(wavs[0], dtype=np.float32).reshape(-1)
    duration = len(audio) / sample_rate
    if not np.isfinite(audio).all() or not 5 <= duration <= 29:
        raise SystemExit(f"Generated reference failed basic audio checks ({duration:.2f}s).")
    peak = float(np.abs(audio).max())
    if peak < 0.001:
        raise SystemExit("Generated reference is silent.")
    # Preserve prosody; only prevent PCM clipping, without compression or tempo changes.
    gain = min(1.0, 0.98 / peak)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(args.output, audio * gain, sample_rate, subtype="PCM_16")
    paths[1].write_text(REFERENCE_TEXT + "\n", encoding="utf-8")
    metadata = {
        "model": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        "revision": model_dir.name,
        "method": "Qwen3TTSModel.generate_voice_design",
        "description": DESCRIPTION,
        "text": REFERENCE_TEXT,
        "seed": args.seed,
        "device": args.device,
        "dtype": "float32" if args.device == "cpu" else "bfloat16",
        "attention": "sdpa",
        "max_new_tokens": 360,
        "sample_rate": sample_rate,
        "duration_seconds": round(duration, 3),
        "pcm_gain": gain,
        "generation_seconds": round(time.monotonic() - started, 2),
        "offline": True,
        "packages": {name: importlib.metadata.version(name) for name in ("qwen-tts", "torch", "transformers")},
        "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
    }
    paths[2].write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {args.output}: {duration:.2f}s, {sample_rate}Hz, mono PCM16.", flush=True)


if __name__ == "__main__":
    main()
