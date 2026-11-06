"""Shared hard byte/code-point limits at the inference-sidecar boundary."""

MEBIBYTE = 1024 * 1024

# Mirrors MAX_PREPARATION_RAW_BYTES_PER_TURN.
MAX_ASR_AUDIO_BYTES = 1 * MEBIBYTE
# Mirrors the ordered browser's full-reply code-point bound.
MAX_TTS_TEXT_CODE_POINTS = 64 * 1024
# Smallest whole-MiB bound above every checked-in reference WAV.
MAX_TTS_REFERENCE_AUDIO_BYTES = 2 * MEBIBYTE
# Mirrors MAX_TURN_PCM_BYTES and also caps total streamed output.
MAX_TTS_OUTPUT_BYTES = 8 * MEBIBYTE
