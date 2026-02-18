# Whisper Voice Training Guide - Custom Accent Model

> **Status (Feb 17):** Initial training complete and deployed (Feb 12). Model runs via CTranslate2 on GPU (0.1-0.2s). Retraining scheduled Feb 21 using log analysis from Feb 14-21 to expand the dataset. The `venv-tts` environment referenced below is broken (CUDA build, won't work on AMD GPU) — use system Python 3.12 for any future training work.

## Overview
This guide documents the complete process for training a custom Whisper model on your Southern accent, achieving 88%+ accuracy (2-12% WER).

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
- Channels: 2 (stereo)
- Duration: 5 seconds per phrase
- **DO NOT trim after recording** - 5 seconds is correct length

**3. Record All Phrases**

Time required: 45-60 minutes for 150 phrases

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

### Phase 3: Model Training (15-20 minutes)

**1. Activate Virtual Environment**
```bash
cd ~/jarvis
source venv-tts/bin/activate
```

**Critical versions:**
- transformers==4.36.0 (NOT 4.41.0!)
- torch==2.10.0
- datasets==4.5.0
- numpy==1.26.4 (NOT 2.x!)
- fsspec==2024.6.1

**2. Training Script Configuration**

Key settings:
```python
# Force English-only (prevents Welsh hallucination!)
processor = WhisperProcessor.from_pretrained(
    "openai/whisper-base",
    language="english",
    task="transcribe"
)
model.config.forced_decoder_ids = processor.get_decoder_prompt_ids(
    language="en",
    task="transcribe"
)

# Training arguments
Seq2SeqTrainingArguments(
    output_dir="./whisper_finetuned",
    per_device_train_batch_size=4,
    gradient_accumulation_steps=2,
    learning_rate=1e-5,
    warmup_steps=50,
    num_train_epochs=10,
    fp16=False,  # CPU training (AMD GPU not CUDA)
    dataloader_num_workers=12,  # Use all cores
)

# Dataset split
train_test_split(test_size=0.1)  # 90% train, 10% test
```

**3. Run Training**
```bash
cd /mnt/models/voice_training
python3 train_whisper.py
```

**Expected timeline:**
- Epoch 1: WER ~50%
- Epoch 5: WER ~7%
- Epoch 10: WER 2-12% (88-98% accuracy)

**Training output:**
```
Train: 135 samples
Test: 15 samples
Training time: ~15-20 minutes
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
1. Wrong library versions (especially transformers 4.41 vs 4.36)
2. Not using venv-tts
3. Multilingual mode enabled
4. Poor audio quality
5. Inconsistent recording environment

**Solution:** Verify all versions, use venv-tts, force English-only

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

### Issue: Audio Too Short After Trimming

**Symptom:** Model performs poorly with 4.7s clips

**Reality:** 4.7s clips work fine! The issue was elsewhere (likely multilingual mode)

**Conclusion:** DO NOT trim keypress pops. Record directly at 5 seconds.

---

## Backup Strategy

**Critical files to backup:**

1. **Training data** (~750MB):
   - `/mnt/models/voice_training/audio/` (150 WAV files)
   - `/mnt/models/voice_training/transcripts/` (150 TXT files)
   - `/mnt/models/voice_training/training_phrases.txt`

2. **Trained model (HuggingFace source)** (~290MB):
   - `/mnt/models/voice_training/whisper_finetuned/final/`

3. **Production model (CTranslate2 GPU-optimized)** (~73MB):
   - `/mnt/models/voice_training/whisper_finetuned_ct2/`
   - This is what JARVIS actually loads at runtime (converted from final/ for GPU inference)

4. **Backup locations:**
   - Primary: Local system backup
   - Secondary: `/mnt/storage/whisper_finetuned_backup/`
   - Tertiary: `/mnt/models/whisper_finetuned_backup/`

**Backup command:**
```bash
rsync -av /mnt/models/voice_training/whisper_finetuned/ \
  /mnt/storage/whisper_finetuned_backup/
```

---

## Performance Expectations

**Training metrics:**
- Train time: 15-20 minutes (CPU)
- Final WER: 2-12%
- Accuracy: 88-98%
- Model size: ~290MB

**Runtime performance:**
- Transcription: ~2 seconds per 5s clip
- Memory: ~2GB additional RAM
- CPU: Normal usage during inference

---

## Maintenance

**When to retrain:**
- Significant drift in recognition accuracy
- Adding new domain vocabulary
- Changed microphone/recording setup
- Moved to different acoustic environment

**Quick retrain:**
1. Add new phrases to training_phrases.txt
2. Record only new phrases (script resumes)
3. Retrain (15-20 min)
4. Test and deploy

---

## Success Criteria Checklist

- [ ] 150+ training phrases recorded
- [ ] All audio files 5 seconds, 16kHz, stereo
- [ ] Dataset metadata created successfully
- [ ] Training completes without errors
- [ ] Final WER < 15%
- [ ] Wake word "Jarvis" 100% accurate
- [ ] Domain terms recognized correctly
- [ ] Model integrated and tested in JARVIS
- [ ] Backups created in 3 locations

---

**Last successful training:** February 12, 2026
**Training time:** 20 minutes  
**Final WER:** 11.67%
**Accuracy:** 88.3%
**Status:** ✅ Production Ready

---

## ADDENDUM: CTranslate2 Conversion (February 12, 2026)

### Ultra-Fast Inference with faster-whisper

After initial training, we converted the model to CTranslate2 format for 6x faster inference.

**Performance Comparison:**
- Python transformers: ~2.0s transcription
- faster-whisper (CTranslate2): ~0.3-0.5s transcription
- Accuracy: Identical (88-98%)

### Conversion Process

**1. Install faster-whisper**
```bash
pip install --break-system-packages faster-whisper ctranslate2
```

**2. Convert Model (with dtype bug workaround)**
```python
# Activate training venv
# NOTE: venv-tts is broken (CUDA build). Use system Python 3.12 instead.
# source ~/jarvis/venv-tts/bin/activate  # DO NOT USE

# Monkey-patch the converter to fix dtype bug
import ctranslate2.converters.transformers as ct2_trans

original_load_model = ct2_trans.TransformersConverter.load_model

def patched_load_model(self, model_class, model_name_or_path, **kwargs):
    kwargs.pop('dtype', None)
    kwargs.pop('torch_dtype', None)
    return original_load_model(self, model_class, model_name_or_path, **kwargs)

ct2_trans.TransformersConverter.load_model = patched_load_model

# Convert
from ctranslate2.converters import TransformersConverter
converter = TransformersConverter("whisper_finetuned/final")
converter.convert("whisper_finetuned_ct2", quantization="int8", force=True)
```

**3. Update STT to use faster-whisper**

Modified `core/stt.py` to use `faster_whisper.WhisperModel` instead of transformers library.

**Key Configuration:**
```python
self.model = WhisperModel(
    model_path,
    device="cpu",
    compute_type="int8",
    cpu_threads=4
)
```

### Result

- ✅ 6x faster inference
- ✅ Same accuracy as Python transformers
- ✅ Lower memory usage (INT8 quantization)
- ✅ Production-ready performance

**Model Sizes:**
- HuggingFace format: 279MB
- CTranslate2 INT8: 73MB

---

**Updated:** February 17, 2026
**Status:** ✅ Production — faster-whisper (CTranslate2) on GPU active
**Next retraining:** Scheduled Feb 21 (log analysis from Feb 14-21)
