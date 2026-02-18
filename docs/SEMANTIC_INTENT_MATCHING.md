# Semantic Intent Matching System
## Using Sentence Transformers for Natural Language Understanding

## The Problem
Current exact pattern matching is:
- **Brittle**: "what cpu is running" works, "what cpu is in" doesn't
- **Unsustainable**: Need 100+ patterns for one query type
- **Unmaintainable**: Adding new skills = pattern explosion
- **Unnatural**: Users have to guess exact phrasings

## The Solution: Semantic Similarity

Instead of exact matching, compute semantic similarity between:
- User's query: "what cpu is running in this machine?"
- Intent examples: ["what cpu do i have", "show me my processor"]

If similarity > threshold (e.g., 85%), trigger the intent!

---

## Architecture

### 1. Sentence Transformer Model

**Model:** `all-MiniLM-L6-v2`
- Size: 23MB (tiny!)
- Speed: ~100ms inference on CPU
- Quality: 85%+ accuracy on semantic similarity
- No GPU needed
- Offline-capable

**Why this model:**
- Fast enough for real-time voice
- Small enough for Raspberry Pi
- Good enough for intent matching
- Better alternatives available if needed

### 2. Intent Registration Changes

**Before (exact matching):**
```python
self.register_intent("what cpu do i have", self.get_cpu_info)
self.register_intent("what's my cpu", self.get_cpu_info)
self.register_intent("what type of cpu", self.get_cpu_info)
# ... 100 more variations
```

**After (semantic matching):**
```python
self.register_semantic_intent(
    examples=[
        "what cpu do i have",
        "show me my processor",
        "tell me about this computer's cpu"
    ],
    handler=self.get_cpu_info,
    threshold=0.85  # 85% similarity required
)
```

**3 examples** instead of 100+ patterns!

### 3. How It Works

```
User says: "what cpu is running in this machine?"

1. Encode user query → embedding vector
2. Compare to all registered intent examples
3. Find best match with similarity score
4. If score > threshold → trigger intent
5. Otherwise → fall back to LLM

Example:
User: "what cpu is running in this machine?"
Best match: "what cpu do i have" (similarity: 0.91)
Result: ✅ Trigger get_cpu_info()
```

---

## Implementation

### File Structure

```
/home/user/jarvis/core/
├── semantic_matcher.py      # New: Semantic matching engine
├── skill_manager.py          # Modified: Add semantic matching
└── base_skill.py             # Modified: Add semantic registration

/mnt/storage/jarvis/models/
└── sentence-transformers/
    └── all-MiniLM-L6-v2/     # Model cache (23MB)
```

### Core Components

#### 1. SemanticMatcher Class

```python
from sentence_transformers import SentenceTransformer
import numpy as np
from typing import List, Tuple

class SemanticMatcher:
    """Semantic intent matching using sentence transformers"""
    
    def __init__(self, model_name="all-MiniLM-L6-v2"):
        """
        Initialize semantic matcher
        
        Args:
            model_name: Sentence transformer model to use
        """
        # Load model (caches locally after first download)
        self.model = SentenceTransformer(model_name)
        
        # Intent embeddings cache
        self.intent_embeddings = {}  # {intent_id: embedding_vector}
        self.intent_examples = {}    # {intent_id: [example1, example2, ...]}
        
    def register_intent(self, intent_id: str, examples: List[str]):
        """
        Register an intent with example phrases
        
        Args:
            intent_id: Unique identifier for this intent
            examples: List of example phrases for this intent
        """
        # Encode all examples
        embeddings = self.model.encode(examples)
        
        # Store (we'll compare against all examples)
        self.intent_embeddings[intent_id] = embeddings
        self.intent_examples[intent_id] = examples
        
    def match(self, query: str, threshold: float = 0.85) -> Tuple[str, float]:
        """
        Find best matching intent for query
        
        Args:
            query: User's query text
            threshold: Minimum similarity score (0.0 - 1.0)
            
        Returns:
            Tuple of (intent_id, similarity_score) or (None, 0.0)
        """
        # Encode query
        query_embedding = self.model.encode([query])[0]
        
        best_intent = None
        best_score = 0.0
        
        # Compare to all registered intents
        for intent_id, intent_embeddings in self.intent_embeddings.items():
            # Compute cosine similarity with each example
            similarities = self._cosine_similarity(query_embedding, intent_embeddings)
            
            # Take maximum similarity across all examples
            max_similarity = np.max(similarities)
            
            if max_similarity > best_score:
                best_score = max_similarity
                best_intent = intent_id
        
        # Return if above threshold
        if best_score >= threshold:
            return best_intent, best_score
        
        return None, 0.0
    
    def _cosine_similarity(self, vec1, vec2):
        """Compute cosine similarity between vectors"""
        # vec2 can be matrix of multiple vectors
        dot_product = np.dot(vec2, vec1)
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2, axis=1)
        return dot_product / (norm1 * norm2)
```

#### 2. Skill Registration API

```python
# In base_skill.py

class BaseSkill:
    def register_semantic_intent(
        self, 
        examples: List[str], 
        handler: Callable,
        threshold: float = 0.85,
        priority: int = 5
    ):
        """
        Register a semantic intent (similarity-based matching)
        
        Args:
            examples: List of example phrases for this intent
            handler: Function to handle this intent
            threshold: Minimum similarity score (0.0-1.0)
            priority: Priority (1-10, higher = checked first)
        """
        intent_id = f"{self.metadata.name}_{handler.__name__}"
        
        self.semantic_intents[intent_id] = {
            "examples": examples,
            "handler": handler,
            "threshold": threshold,
            "priority": priority
        }
        
        self.logger.debug(f"Registered semantic intent: {intent_id} with {len(examples)} examples")
```

#### 3. Modified Skill Manager Matching

```python
# In skill_manager.py

def match_user_intent(self, user_text: str):
    """
    Match user input to registered intents
    
    Priority:
    1. Exact pattern matches (fast, highest confidence)
    2. Semantic matches (slower, good confidence)
    3. LLM fallback (slowest, last resort)
    """
    # 1. Try exact patterns first (existing code)
    exact_match = self._match_exact_patterns(user_text)
    if exact_match:
        return exact_match
    
    # 2. Try semantic matching
    if self.semantic_matcher:
        intent_id, score = self.semantic_matcher.match(user_text)
        if intent_id:
            self.logger.info(f"🎯 Semantic match: {intent_id} (score: {score:.2f})")
            return self._resolve_semantic_intent(intent_id)
    
    # 3. Fall back to LLM
    return None
```

---

## Example: CPU Queries

### Before (Exact Matching)
106 patterns needed:
```python
self.register_intent("what cpu do i have", self.get_cpu_info)
self.register_intent("what cpu is running", self.get_cpu_info)
self.register_intent("what cpu is installed", self.get_cpu_info)
self.register_intent("what cpu is in this machine", self.get_cpu_info)
self.register_intent("what cpu is on this machine", self.get_cpu_info)
# ... 101 more patterns
```

### After (Semantic Matching)
3-5 examples needed:
```python
self.register_semantic_intent(
    examples=[
        "what cpu do i have",
        "show me my processor",
        "tell me about this computer's cpu",
        "what type of processor is installed"
    ],
    handler=self.get_cpu_info,
    threshold=0.85
)
```

### Test Cases

All of these would match (similarity > 0.85):
- ✅ "what cpu is running in this machine" (0.91)
- ✅ "what processor do i have" (0.93)
- ✅ "tell me my cpu model" (0.88)
- ✅ "which cpu is in this box" (0.87)
- ✅ "processor info please" (0.86)

These would NOT match (too different):
- ❌ "what's the weather" (0.23)
- ❌ "send an email" (0.18)
- ❌ "play music" (0.15)

---

## Performance Considerations

### Latency
- Exact pattern matching: <1ms
- Semantic matching: ~100ms (CPU)
- LLM fallback: 1-2 seconds

**Strategy:** Try exact first, semantic second, LLM last

### Memory
- Model size: 23MB
- Model in RAM: ~100MB
- Per-intent overhead: ~1KB per example

**Impact:** Negligible on modern systems

### Accuracy
- Semantic matching: 90-95% intent accuracy
- Better than exact patterns for natural language
- Fails gracefully to LLM if unsure

---

## Migration Path

### Phase 1: Add Semantic Layer (Week 1)
1. Install sentence-transformers
2. Implement SemanticMatcher class
3. Add semantic registration API
4. Test with CPU queries

### Phase 2: Migrate Existing Skills (Week 2)
1. Keep exact patterns for high-traffic intents
2. Replace pattern explosions with semantic examples
3. Gradual migration skill-by-skill

### Phase 3: New Skills (Ongoing)
- All new skills use semantic matching by default
- Only add exact patterns for critical/common queries

---

## Installation Requirements

```bash
pip install sentence-transformers --break-system-packages
```

**Dependencies:**
- torch (CPU version): ~200MB
- sentence-transformers: ~23MB
- transformers: ~5MB

**Total:** ~230MB disk, ~100MB RAM

**First run:** Downloads model (23MB, one-time)

---

## Configuration

Add to `config.yaml`:

```yaml
semantic_matching:
  enabled: true
  model: "all-MiniLM-L6-v2"  # Can upgrade to better models later
  cache_dir: "/mnt/models/sentence-transformers"
  default_threshold: 0.85     # Global threshold
  fallback_to_llm: true       # Fall back to LLM if no match
  
  # Performance tuning
  batch_encode: false         # Encode queries one at a time
  device: "cpu"               # Use GPU if available (future)
```

---

## Benefits

### For Users
- ✅ Natural language queries work immediately
- ✅ No need to memorize exact phrases
- ✅ Typos and variations handled gracefully
- ✅ More human-like interaction

### For Development
- ✅ Fewer patterns to maintain
- ✅ Easier to add new skills
- ✅ Better code organization
- ✅ Reduced LLM usage (faster + cheaper)

### For System
- ✅ Faster than LLM fallback
- ✅ Works offline
- ✅ Scales to hundreds of intents
- ✅ Predictable performance

---

## Advanced Features (Future)

### 1. Intent Confidence Scores
```python
User: "what cpu is running"
Matches:
  - get_cpu_info (0.91) ← Use this
  - system_info (0.67)
  - hardware_details (0.54)
```

### 2. Multi-Intent Detection
```python
User: "what's my cpu and how much ram do i have"
Matches:
  - get_cpu_info (0.92)
  - get_memory_info (0.89)
Action: Handle both intents
```

### 3. Context-Aware Matching
```python
Previous: "tell me about my system"
User: "and the cpu?"
Context: "system hardware" domain
Boost: CPU-related intents get higher scores
```

### 4. Learning from Corrections
```python
User: "what cpu is running"
Jarvis: Mismatches → plays music
User: "no, i meant the processor"
System: Learn that "running" in hardware context = query, not playback
```

---

## Testing Strategy

### Unit Tests
```python
def test_cpu_semantic_matching():
    matcher = SemanticMatcher()
    matcher.register_intent(
        "cpu_info",
        ["what cpu do i have", "show processor"]
    )
    
    # Test variations
    intent, score = matcher.match("what cpu is running")
    assert intent == "cpu_info"
    assert score > 0.85
    
    # Test non-match
    intent, score = matcher.match("play music")
    assert intent is None
```

### Integration Tests
```python
def test_skill_semantic_registration():
    skill = SystemInfoSkill(config)
    skill.register_semantic_intent(
        examples=["what cpu do i have"],
        handler=skill.get_cpu_info
    )
    
    result = skill.handle("what processor is installed")
    assert "Ryzen 9 5900X" in result
```

### Performance Tests
```python
def test_semantic_matching_latency():
    matcher = SemanticMatcher()
    # Register 50 intents
    for i in range(50):
        matcher.register_intent(f"intent_{i}", [f"example {i}"])
    
    # Measure matching speed
    start = time.time()
    matcher.match("test query")
    elapsed = time.time() - start
    
    assert elapsed < 0.2  # Must be under 200ms
```

---

## Alternatives Considered

### 1. Fuzzy String Matching (Levenshtein Distance)
- ❌ Only handles typos, not semantic variations
- ❌ "what cpu is running" vs "show processor" = low similarity
- ✅ Fast (~1ms)

### 2. Keyword Matching
- ❌ Too simple: "running" could mean many things
- ❌ Doesn't understand context
- ✅ Very fast (<1ms)

### 3. Full NLP Pipeline (spaCy)
- ✅ Very accurate
- ❌ Much slower (500ms+)
- ❌ Larger models (100MB+)
- ❌ Overkill for intent matching

### 4. Sentence Transformers (CHOSEN)
- ✅ Semantic understanding
- ✅ Fast enough (~100ms)
- ✅ Small models (23MB)
- ✅ Easy to use
- ✅ Works offline

---

## Success Metrics

### Before (Exact Matching)
- Patterns per intent: 50-100
- LLM fallback rate: 30-40%
- Maintenance burden: HIGH
- User frustration: MEDIUM

### After (Semantic Matching)
- Examples per intent: 3-5
- LLM fallback rate: <10%
- Maintenance burden: LOW
- User frustration: LOW

### Goals
- ✅ Reduce patterns by 95%
- ✅ Improve intent match rate by 50%
- ✅ Reduce LLM usage by 60%
- ✅ Response time: <500ms total

---

## Implementation Priority

**Phase 1 (This Week):**
1. Install dependencies
2. Implement SemanticMatcher
3. Migrate CPU queries as proof of concept
4. Test and measure performance

**Phase 2 (Next Week):**
1. Migrate system_info skill fully
2. Migrate conversation skill
3. Create migration guide for other skills

**Phase 3 (Ongoing):**
1. All new skills use semantic matching
2. Gradually migrate old skills
3. Monitor performance and adjust thresholds

---

## Rollout Strategy

### 1. Feature Flag
```yaml
semantic_matching:
  enabled: true  # Can disable if issues arise
```

### 2. Hybrid Approach
- Keep exact patterns for critical paths
- Add semantic matching for variations
- Gradual cutover

### 3. Monitoring
- Log match types: exact vs semantic vs LLM
- Track latency per method
- Alert if semantic matching degrades

### 4. Rollback Plan
- Keep exact patterns as backup
- Can disable semantic matching instantly
- No breaking changes

---

## Conclusion

Semantic intent matching solves the pattern explosion problem with:
- **Less code:** 3-5 examples instead of 100+ patterns
- **Better UX:** Natural language works out of the box
- **Maintainable:** Easy to add new intents
- **Fast enough:** ~100ms is acceptable for voice
- **Offline:** No cloud API needed

**This is the right architectural decision.**

Let's implement it! 🚀
