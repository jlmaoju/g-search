from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import torch
except ImportError as exc:  # pragma: no cover - optional ASR dependency
    torch = None  # type: ignore[assignment]
    TORCH_IMPORT_ERROR: ImportError | None = exc
else:
    TORCH_IMPORT_ERROR = None


ROOT = Path(__file__).resolve().parents[1]
FIRERED_ROOT = ROOT / "vendor" / "FireRedASR2S"
if str(FIRERED_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRERED_ROOT))

try:
    from fireredasr2s import FireRedAsr2System, FireRedAsr2SystemConfig
    from fireredasr2s.fireredasr2 import FireRedAsr2Config
    from fireredasr2s.fireredlid import FireRedLidConfig
    from fireredasr2s.fireredpunc import FireRedPuncConfig
    from fireredasr2s.fireredvad import FireRedVadConfig
except ImportError as exc:  # pragma: no cover - optional ASR dependency
    FireRedAsr2System = None  # type: ignore[assignment]
    FireRedAsr2SystemConfig = None  # type: ignore[assignment]
    FireRedAsr2Config = None  # type: ignore[assignment]
    FireRedLidConfig = None  # type: ignore[assignment]
    FireRedPuncConfig = None  # type: ignore[assignment]
    FireRedVadConfig = None  # type: ignore[assignment]
    FIRERED_IMPORT_ERROR: ImportError | None = exc
else:
    FIRERED_IMPORT_ERROR = None


MODEL_ROOT = FIRERED_ROOT / "pretrained_models"


def ensure_firered_dependencies() -> None:
    if TORCH_IMPORT_ERROR is not None:
        raise RuntimeError("FireRed ASR requires the optional torch dependency") from TORCH_IMPORT_ERROR
    if FIRERED_IMPORT_ERROR is not None:
        raise RuntimeError(
            "FireRed ASR requires the optional FireRedASR2S vendor package. "
            "Install or place it under vendor/FireRedASR2S before using FireRed transcription."
        ) from FIRERED_IMPORT_ERROR


@dataclass
class FireRedRuntimeConfig:
    disable_lid: bool = False
    disable_punc: bool = False
    asr_batch_size: int = 4
    punc_batch_size: int = 8
    use_half: bool = False
    beam_size: int = 3
    return_timestamp: bool = True


class FireRedRunner:
    def __init__(self, config: FireRedRuntimeConfig | None = None) -> None:
        ensure_firered_dependencies()
        self.config = config or FireRedRuntimeConfig()
        self.use_gpu = torch.cuda.is_available()
        self.enable_lid = has_lid_model() and not self.config.disable_lid
        self.enable_punc = has_punc_model() and not self.config.disable_punc
        self.system = build_system(
            use_gpu=self.use_gpu,
            enable_lid=self.enable_lid,
            enable_punc=self.enable_punc,
            asr_batch_size=self.config.asr_batch_size,
            punc_batch_size=self.config.punc_batch_size,
            use_half=self.config.use_half,
            beam_size=self.config.beam_size,
            return_timestamp=self.config.return_timestamp,
        )

    def transcribe_path(self, input_path: Path, *, uttid: str | None = None) -> dict:
        input_path = input_path.resolve()
        with tempfile.TemporaryDirectory(prefix="firered_asr2_") as temp_dir:
            wav_path = Path(temp_dir) / f"{input_path.stem}.wav"
            convert_to_wav(input_path=input_path, wav_path=wav_path)
            return self.transcribe_wav(wav_path, uttid=uttid or input_path.stem, input_path=input_path)

    def transcribe_wav(
        self,
        wav_path: Path,
        *,
        uttid: str | None = None,
        input_path: Optional[Path] = None,
    ) -> dict:
        wav_path = wav_path.resolve()
        original_input = (input_path or wav_path).resolve()
        result = self.system.process(str(wav_path), uttid=uttid or original_input.stem)
        result["runtime_device"] = "cuda" if self.use_gpu else "cpu"
        return build_payload(
            result=result,
            input_path=original_input,
            wav_path=wav_path,
            enable_lid=self.enable_lid,
            enable_punc=self.enable_punc,
            config=self.config,
        )


def build_system(
    *,
    use_gpu: bool,
    enable_lid: bool,
    enable_punc: bool,
    asr_batch_size: int,
    punc_batch_size: int,
    use_half: bool,
    beam_size: int,
    return_timestamp: bool,
) -> FireRedAsr2System:
    ensure_firered_dependencies()
    vad_config = FireRedVadConfig(
        use_gpu=use_gpu,
        smooth_window_size=5,
        speech_threshold=0.2,
        min_speech_frame=20,
        max_speech_frame=1000,
        min_silence_frame=10,
        merge_silence_frame=50,
        extend_speech_frame=10,
        chunk_max_frame=30000,
    )
    lid_config = FireRedLidConfig(use_gpu=enable_lid and use_gpu, use_half=use_half)
    asr_config = FireRedAsr2Config(
        use_gpu=use_gpu,
        use_half=use_half,
        beam_size=beam_size,
        nbest=1,
        decode_max_len=0,
        softmax_smoothing=1.25,
        aed_length_penalty=0.6,
        eos_penalty=1.0,
        return_timestamp=return_timestamp,
    )
    punc_config = FireRedPuncConfig(use_gpu=enable_punc and use_gpu)

    return FireRedAsr2System(
        FireRedAsr2SystemConfig(
            vad_model_dir=str(MODEL_ROOT / "FireRedVAD" / "VAD"),
            lid_model_dir=str(MODEL_ROOT / "FireRedLID"),
            asr_type="aed",
            asr_model_dir=str(MODEL_ROOT / "FireRedASR2-AED"),
            punc_model_dir=str(MODEL_ROOT / "FireRedPunc"),
            vad_config=vad_config,
            lid_config=lid_config,
            asr_config=asr_config,
            punc_config=punc_config,
            asr_batch_size=max(1, int(asr_batch_size)),
            punc_batch_size=max(1, int(punc_batch_size)),
            enable_vad=True,
            enable_lid=enable_lid,
            enable_punc=enable_punc,
        )
    )


def convert_to_wav(*, input_path: Path, wav_path: Path) -> None:
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-acodec",
        "pcm_s16le",
        str(wav_path),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"ffmpeg conversion failed: {stderr}")


def build_payload(
    *,
    result: dict,
    input_path: Path,
    wav_path: Path,
    enable_lid: bool,
    enable_punc: bool,
    config: FireRedRuntimeConfig,
) -> dict:
    return {
        "text": result.get("text", ""),
        "segments": [
            {
                "start_ms": sentence.get("start_ms"),
                "end_ms": sentence.get("end_ms"),
                "text": sentence.get("text"),
                "lang": sentence.get("lang"),
                "lang_confidence": sentence.get("lang_confidence"),
                "asr_confidence": sentence.get("asr_confidence"),
            }
            for sentence in result.get("sentences", [])
        ],
        "words": result.get("words", []),
        "model_name": "FireRedASR2-AED",
        "language": pick_language(result),
        "metadata": {
            "enable_lid": enable_lid,
            "enable_punc": enable_punc,
            "runtime_device": result.get("runtime_device"),
            "vad_segments_ms": result.get("vad_segments_ms", []),
            "duration_seconds": result.get("dur_s"),
            "input_path": str(input_path),
            "wav_path": str(wav_path),
            "asr_batch_size": config.asr_batch_size,
            "punc_batch_size": config.punc_batch_size,
            "use_half": config.use_half,
            "beam_size": config.beam_size,
            "return_timestamp": config.return_timestamp,
        },
    }


def has_lid_model() -> bool:
    model_path = MODEL_ROOT / "FireRedLID" / "model.pth.tar"
    return model_path.exists() and model_path.stat().st_size > 0


def has_punc_model() -> bool:
    model_path = MODEL_ROOT / "FireRedPunc" / "model.pth.tar"
    return model_path.exists() and model_path.stat().st_size > 0


def pick_language(result: dict) -> str | None:
    for sentence in result.get("sentences", []):
        language = sentence.get("lang")
        if language:
            return language
    return "zh"


def save_payload(output_path: Path, payload: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
