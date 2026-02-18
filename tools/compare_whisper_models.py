"""
Quick diagnostic: Compare fine-tuned vs base Whisper model
on recent audio clips to identify if the fine-tuned model
has regressed on common words like "Jarvis".
"""

import numpy as np
import wave
import glob
import os

def load_wav(path):
    """Load WAV file as float32 numpy array"""
    with wave.open(path, 'r') as wf:
        frames = wf.readframes(wf.getnframes())
        sr = wf.getframerate()
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0
    return audio, sr

def transcribe_with_model(model, audio, language="en"):
    """Transcribe audio with a faster-whisper model"""
    segments, info = model.transcribe(
        audio,
        language=language,
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(
            threshold=0.3,
            min_speech_duration_ms=100,
            min_silence_duration_ms=200
        ),
        word_timestamps=True,
        condition_on_previous_text=False,
        temperature=0.0,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6
    )
    text = " ".join([s.text for s in segments]).strip()
    return text

def transcribe_no_vad(model, audio, language="en"):
    """Transcribe without VAD filter (in case VAD is rejecting speech)"""
    segments, info = model.transcribe(
        audio,
        language=language,
        beam_size=5,
        vad_filter=False,
        condition_on_previous_text=False,
        temperature=0.0,
    )
    text = " ".join([s.text for s in segments]).strip()
    return text

def main():
    from faster_whisper import WhisperModel

    clip_dir = "/tmp/jarvis_audio_debug"
    clips = sorted(glob.glob(f"{clip_dir}/clip_*.wav"))

    if not clips:
        print("No debug clips found!")
        return

    print(f"Found {len(clips)} debug clips\n")

    # Load models
    finetuned_path = "/mnt/models/voice_training/whisper_finetuned_ct2"
    print("Loading fine-tuned model...")
    finetuned = WhisperModel(finetuned_path, device="cuda", compute_type="float16")

    print("Loading base model...")
    base = WhisperModel("base", device="cuda", compute_type="float16")

    print("\n" + "=" * 80)
    print(f"{'CLIP':<20} {'FINE-TUNED (VAD)':<25} {'BASE (VAD)':<25} {'BASE (no VAD)':<25}")
    print("=" * 80)

    for clip_path in clips:
        fname = os.path.basename(clip_path)
        audio, sr = load_wav(clip_path)
        duration = len(audio) / sr

        ft_text = transcribe_with_model(finetuned, audio)
        base_text = transcribe_with_model(base, audio)
        base_novad = transcribe_no_vad(base, audio)

        ft_display = ft_text if ft_text else "(blank)"
        base_display = base_text if base_text else "(blank)"
        novad_display = base_novad if base_novad else "(blank)"

        print(f"{fname:<20} {ft_display:<25} {base_display:<25} {novad_display:<25}")

    print("=" * 80)
    print("\nDone. If base model consistently outperforms fine-tuned on 'Jarvis',")
    print("the fine-tuning may have overtrained away from common English words.")

if __name__ == "__main__":
    main()
