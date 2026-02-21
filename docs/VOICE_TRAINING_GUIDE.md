# Whisper Voice Training Guide - Custom Accent Model

> **Status (Feb 21):** Second training complete and deployed. 198 phrases recorded with FIFINE K669B USB condenser mic. GPU training (fp16, 89 seconds). Live voice testing: **17/18 phrases correct (94.4% phrase accuracy)**. Wake word 100%, "what's" contractions 100%, cybersecurity jargon 100%. Only miss: "how's" → "house" (contraction gap in training data). Model runs via CTranslate2 on GPU (0.1-0.2s latency).

## Overview
This guide documents the complete process for training a custom Whisper model on your Southern accent, achieving 94%+ accuracy.

---

## Prerequisites

### Hardware
- CPU: Ryzen 9 5900X or similar (12 cores recommended)
- RAM: 16GB minimum
- Storage: ~5GB for training data and models
- Microphone: FIFINE K669B USB condenser mic (or any quality USB mic)

### Software
- Ubuntu 24.04 LTS
- Python 3.12 (system Python — venv-tts is broken/unused)
- sox (audio processing)
- arecord/aplay (recording/playback)

---

## Training Process

### Phase 1: Dataset Creation (2-3 hours)

**1. Create Training Phrases File**

Location: `/mnt/models/voice_training/training_phrases.txt`

Include 150+ phrases covering:
- Wake word variations (Jarvis, Hey Jarvis, etc.)
- Problem words that Whisper mishears
- Domain-specific vocabulary:
  - Threat hunting terms (C2, lateral movement, TTPs)
  - Jeep Wrangler terminology (lift, 35s, death wobble)
  - Your dogs (Heinz 57s, Huskies, mutts)
- Common commands and queries
- Technical vocabulary

**2. Create Recording Script**

Critical features:
```bash
#!/bin/bash
# Key requirement: read </dev/tty to prevent stdin conflicts!

while IFS= read -r phrase; do
    clear
    echo "$phrase"
    echo "Press ENTER to record"
    read </dev/tty  # CRITICAL: Force keyboard input!
    
    arecord -f S16_LE -r 16000 -c 2 -d 5 audio.wav
    aplay audio.wav
    
    echo "Press ENTER to continue"
    read </dev/tty
done < training_phrases.txt
```

**Recording specs:**
- Format: S16_LE (16-bit signed integer)
- Sample rate: 16000 Hz
- Channels: 1 (mono — matches production STT pipeline)
- Duration: 5 seconds per phrase
- Trim first 0.1s (1600 samples) to remove mic activation click

**3. Record All Phrases**

Time required: 45-60 minutes for 200 phrases

Tips:
- Quiet environment
- Consistent distance from mic (~6 inches)
- Clear enunciation
- Natural speaking pace
- Re-record any mistakes immediately

---

### Phase 2: Dataset Preparation (5 minutes)

**1. Create Dataset Metadata**
```python
# prepare_dataset.py
import json
from pathlib import Path

audio_dir = Path("audio")
transcript_dir = Path("transcripts")
metadata = []

for audio_file in sorted(audio_dir.glob("*.wav")):
    transcript_file = transcript_dir / f"{audio_file.stem}.txt"
    
    if transcript_file.exists():
        with open(transcript_file, 'r') as f:
            text = f.read().strip()
        
        metadata.append({
            "file_name": audio_file.name,
            "transcription": text
        })

hf_dir = Path("hf_dataset")
hf_dir.mkdir(exist_ok=True)

with open(hf_dir / "metadata.json", 'w') as f:
    json.dump(metadata, f, indent=2)

print(f"Created metadata for {len(metadata)} files")
```

Run: `python3 prepare_dataset.py`

Expected output: `Created metadata for 150 files`

---

### Phase 3: Model Training (~90 seconds on GPU)

**1. Use System Python 3.12** (NOT venv-tts — that's broken/unused)

**Key packages:** `transformers`, `datasets`, `torch` (2.10.0+rocm7.1), `accelerate`, `evaluate`

**2. Training Script Configuration**

Key settings in `train_whisper.py`:
```python
# Force English-only (prevents Welsh hallucination!)
processor = WhisperProcessor.from_pretrained(
    "openai/whisper-base",
    language="english",
    task="transcribe"
)

# Training arguments — GPU-accelerated
Seq2SeqTrainingArguments(
    output_dir="./whisper_finetuned",
    per_device_train_batch_size=8,   # 8 on GPU, 4 on CPU
    gradient_accumulation_steps=2,
    learning_rate=1e-5,
    warmup_steps=50,
    num_train_epochs=10,
    fp16=True,                       # GPU fp16 (auto-detected)
    dataloader_num_workers=12,
    load_best_model_at_end=True,
    metric_for_best_model="wer",
)
```

**3. Run Training (End-to-End Pipeline)**
```bash
cd /mnt/models/voice_training
./retrain.sh              # Full: stop services → train → convert → restart
./retrain.sh --skip-stop  # If services already stopped
```

**Expected timeline (GPU fp16):**
- Epoch 1: WER ~37%
- Epoch 4: WER ~15%
- Epoch 6-10: WER ~12% (plateaus)
- Total training time: ~90 seconds on RX 7900 XT

**Training output:**
```
Train: 178 samples (90%)
Test: 20 samples (10%)
Training time: 89 seconds (GPU fp16)
Final model: whisper_finetuned/final/
```

---

### Phase 4: Testing & Validation

**1. Test on Sample Phrases**
```python
from transformers import WhisperProcessor, WhisperForConditionalGeneration
import librosa

model_path = "whisper_finetuned/final"
processor = WhisperProcessor.from_pretrained(model_path)
model = WhisperForConditionalGeneration.from_pretrained(model_path)

# Test critical phrases
test_cases = [
    "audio/phrase_001.wav",  # Threat hunting
    "audio/phrase_050.wav",  # Dogs
    "audio/phrase_100.wav",  # Jeep
]

for audio_file in test_cases:
    audio, sr = librosa.load(audio_file, sr=16000)
    inputs = processor(audio, sampling_rate=16000, return_tensors="pt")
    predicted_ids = model.generate(inputs.input_features)
    transcription = processor.batch_decode(predicted_ids, skip_special_tokens=True)[0]
    print(f"Result: {transcription}")
```

**Success criteria:**
- ✅ Threat hunting terms recognized
- ✅ "Heinz 57" correct (not "Heinz fifty-seven")
- ✅ Wake word "Jarvis" perfect
- ✅ Technical vocabulary accurate

**2. Integration**
```bash
# Update config
cd ~/jarvis
vi config.yaml

# Enable fine-tuned model
stt_finetuned:
  enabled: true
  model_path: /mnt/models/voice_training/whisper_finetuned/final

# Restart JARVIS
restartjarvis
```

---

## Common Issues & Solutions

### Issue: Script Auto-Starts Recording

**Symptom:** Recording begins before you can read the phrase

**Cause:** `read` command consuming from file loop instead of keyboard

**Solution:** Use `read </dev/tty` to force keyboard input

---

### Issue: Model Hallucinates Welsh

**Symptom:** Transcriptions contain Welsh words like "hwnnwch ydy'r"

**Cause:** Multilingual tokenizer not constrained to English

**Solution:** Force English in processor initialization:
```python
processor = WhisperProcessor.from_pretrained(
    model_name,
    language="english",
    task="transcribe"
)
model.config.forced_decoder_ids = processor.get_decoder_prompt_ids(
    language="en",
    task="transcribe"
)
```

---

### Issue: High WER (>20%)

**Possible causes:**
1. Multilingual mode enabled (most common)
2. Poor audio quality or wrong channel count
3. Inconsistent recording environment
4. Too few training samples

**Solution:** Force English-only, use mono audio, increase dataset size

---

### Issue: Training Hangs at "Map (num_proc=4)"

**Cause:** Multiprocessing conflicts with file I/O

**Solution:** Change to `num_proc=1` or use venv-tts

---

### Issue: Numpy/Pandas Binary Incompatibility

**Symptom:** `ValueError: numpy.dtype size changed`

**Solution:** 
```bash
pip install --force-reinstall numpy==1.26.4 pandas
```

---

### Issue: Mic Activation Click/Pop

**Symptom:** Soft click at start of each recording from FIFINE K669B mic activation

**Solution:** Trim first 0.1s (1600 samples at 16kHz) from all recordings after the recording session:
```python
import wave
# Read WAV, skip first 1600 frames, write back
```

---

## Backup Strategy

**Critical files to backup:**

1. **Training data** (~1GB):
   - `/mnt/models/voice_training/audio/` (198 WAV files)
   - `/mnt/models/voice_training/transcripts/` (198 TXT files)
   - `/mnt/models/voice_training/training_phrases.txt`

2. **Old mic recordings** (historical):
   - `/mnt/models/voice_training/audio_backup_old_mic/` (150 WAV files from webcam mic)
   - `/mnt/models/voice_training/transcripts_backup_old_mic/`

3. **Trained model (HuggingFace source)** (~290MB):
   - `/mnt/models/voice_training/whisper_finetuned/final/`

4. **Production model (CTranslate2 GPU-optimized)** (~143MB):
   - `/mnt/models/voice_training/whisper_finetuned_ct2/`
   - This is what JARVIS actually loads at runtime (float16 quantization)

5. **Backup locations:**
   - Primary: Local system backup
   - Secondary: `/mnt/storage/whisper_finetuned_backup/`
   - Tertiary: `/mnt/models/whisper_finetuned_backup/`

---

## Performance Expectations

**Training metrics (GPU fp16):**
- Train time: ~90 seconds (RX 7900 XT), 15-20 minutes (CPU)
- Final WER: ~12% (held-out test set)
- Live accuracy: 94%+ (17/18 phrases correct in voice testing)
- Model size: ~290MB (HuggingFace), 143MB (CTranslate2 float16)

**Runtime performance:**
- Transcription: 0.1-0.2s per utterance (GPU CTranslate2)
- Memory: ~2GB additional VRAM
- Latency: Unchanged from base Whisper

---

## Maintenance

**When to retrain:**
- Significant drift in recognition accuracy
- Adding new domain vocabulary
- Changed microphone/recording setup
- Moved to different acoustic environment

**Quick retrain:**
1. Add new phrases to training_phrases.txt
2. Record new phrases (`./record_training_data.sh` — resumes where you left off)
3. Run `./retrain.sh` (stops services → train → convert → restart, ~2 min total)
4. Test with voice commands

---

## Success Criteria Checklist

- [x] 198 training phrases recorded (FIFINE K669B, mono, 16kHz)
- [x] All audio trimmed (0.1s mic activation click removed)
- [x] Dataset metadata created successfully
- [x] Training completes without errors (89s GPU fp16)
- [x] Final WER < 15% (12.3% on held-out test set)
- [x] Wake word "Jarvis" 100% accurate
- [x] Domain terms recognized correctly (cybersecurity, technical)
- [x] Model integrated and tested in JARVIS
- [x] Old recordings backed up to audio_backup_old_mic/

---

**Last successful training:** February 21, 2026
**Training time:** 89 seconds (GPU fp16)
**Final WER:** 12.3% (held-out test), 5.6% (live voice testing — 1/18 miss)
**Live Accuracy:** 94.4% (17/18 phrases correct)
**Status:** ✅ Production Ready — FIFINE K669B + GPU pipeline

---

## ADDENDUM: CTranslate2 Conversion

### Ultra-Fast Inference with faster-whisper

The trained HuggingFace model is converted to CTranslate2 format for GPU-accelerated inference via faster-whisper.

**Performance Comparison:**
- Python transformers: ~2.0s transcription
- faster-whisper (CTranslate2): 0.1-0.2s transcription (GPU)
- Accuracy: Identical

### Conversion Process

Conversion is now automated via `convert_to_ct2.py` (called by `retrain.sh`):
```python
from ctranslate2.converters import TransformersConverter
converter = TransformersConverter(
    "whisper_finetuned/final",
    copy_files=["tokenizer.json", "preprocessor_config.json"],
    load_as_float16=True,
)
converter.convert("whisper_finetuned_ct2", quantization="float16", force=True)
```

No more monkey-patching needed — the dtype bug was in older ctranslate2 versions.

### Result

- ✅ 10-20x faster inference (vs CPU transformers)
- ✅ Same accuracy as Python transformers
- ✅ GPU float16 quantization
- ✅ Production-ready performance

**Model Sizes:**
- HuggingFace format: ~290MB
- CTranslate2 float16: 143MB

---

**Updated:** February 21, 2026
**Status:** ✅ Production — faster-whisper (CTranslate2) on GPU, 94%+ accuracy
**Known gap:** "how's" contraction → add to next training round
