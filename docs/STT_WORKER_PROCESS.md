# STT Worker Process Architecture (Future Enhancement)

## Current Status

**Working:** GPU acceleration via import order control  
**Risk:** Future indirect torch imports could break GPU

## Recommended Architecture

### Isolated STT Worker Process
```
jarvis.service (main)
 ├─ Control logic
 ├─ TTS
 ├─ Skills
 └─ STT worker (subprocess)
      └─ CTranslate2 + ROCm 7.2 only
      └─ No torch imports
```

### Benefits

1. **Absolute isolation** - No possibility of ROCm library conflicts
2. **Fault tolerance** - STT crashes don't kill main process
3. **Independent restart** - Can reload model without restarting JARVIS
4. **Resource control** - GPU memory managed separately
5. **Future-proof** - Immune to dependency graph changes

### Implementation Plan

**Simple IPC via stdin/stdout:**
```python
# stt_worker.py
import json
import sys
from core.stt import SpeechToText

config = load_config()
stt = SpeechToText(config)

while True:
    line = sys.stdin.readline()
    if not line:
        break
    
    request = json.loads(line)
    audio_data = decode_audio(request['audio_b64'])
    
    result = stt.transcribe(audio_data)
    
    response = {'text': result}
    print(json.dumps(response), flush=True)
```

**Main process communication:**
```python
# In jarvis_continuous.py
import subprocess
import json

# Start worker once
stt_worker = subprocess.Popen(
    ['python3', 'stt_worker.py'],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    text=True,
    bufsize=1
)

# Use it
request = {'audio_b64': encode_audio(audio_data)}
stt_worker.stdin.write(json.dumps(request) + '\n')
stt_worker.stdin.flush()
result = json.loads(stt_worker.stdout.readline())
```

### Effort Estimate

- **Time:** 2-3 hours
- **Complexity:** Low-Medium
- **Risk:** Low (current system keeps working)
- **Priority:** Medium (nice-to-have, not urgent)

### When to Implement

Trigger conditions:
- Adding new GPU-heavy features
- Updating PyTorch version
- Adding other ROCm-dependent libraries
- If import order breaks again

## Current Mitigation

**Status:** Stable with current import order control  
**Monitor:** Watch for indirect torch imports  
**Document:** All GPU dependencies tracked

---

**Created:** February 13, 2026  
**Recommendation:** ChatGPT ROCm expertise  
**Status:** Future enhancement
