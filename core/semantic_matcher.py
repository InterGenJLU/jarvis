"""
Semantic Intent Matcher
Uses sentence transformers for semantic similarity-based intent matching.
"""

import warnings
import numpy as np
from typing import List, Tuple, Optional, Dict
from pathlib import Path

# Suppress huggingface_hub deprecation warnings (resume_download etc.)
warnings.filterwarnings("ignore", category=FutureWarning, module="huggingface_hub")

try:
    from sentence_transformers import SentenceTransformer
    AVAILABLE = True
except ImportError:
    AVAILABLE = False

class SemanticMatcher:
    def __init__(self, model_name="all-MiniLM-L6-v2", cache_dir=None):
        if not AVAILABLE:
            raise ImportError("sentence-transformers not installed")
        if cache_dir:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
        print(f"Loading model: {model_name}...")
        self.model = SentenceTransformer(model_name, cache_folder=cache_dir)
        print("✓ Semantic matcher ready")
        self.intent_embeddings = {}
        self.intent_examples = {}
        self.intent_thresholds = {}
    
    def register_intent(self, intent_id, examples, threshold=0.85):
        if not examples:
            raise ValueError(f"Intent {intent_id} has no examples")
        embeddings = self.model.encode(examples, convert_to_numpy=True)
        self.intent_embeddings[intent_id] = embeddings
        self.intent_examples[intent_id] = examples
        self.intent_thresholds[intent_id] = threshold
        print(f"  Registered: {intent_id} ({len(examples)} examples, threshold: {threshold})")
    
    def match(self, query, default_threshold=0.85):
        """Returns (intent_id, score) - score is ALWAYS the best match, even if below threshold"""
        if not self.intent_embeddings:
            return None, 0.0
        
        query_embedding = self.model.encode([query], convert_to_numpy=True)[0]
        best_intent, best_score = None, 0.0
        
        for intent_id, embeddings in self.intent_embeddings.items():
            sims = self._cosine_similarity(query_embedding, embeddings)
            max_sim = float(np.max(sims))
            
            # Always track best score
            if max_sim > best_score:
                best_score = max_sim
                
                # But only set intent if above threshold
                thresh = self.intent_thresholds.get(intent_id, default_threshold)
                if max_sim >= thresh:
                    best_intent = intent_id
        
        return best_intent, best_score
    
    def _cosine_similarity(self, vec1, vec2):
        if vec2.ndim == 1:
            vec2 = vec2.reshape(1, -1)
        dot = np.dot(vec2, vec1)
        norm1, norm2 = np.linalg.norm(vec1), np.linalg.norm(vec2, axis=1)
        return dot / (norm1 * norm2 + 1e-8)
    
    def get_intent_count(self):
        """Get number of registered intents"""
        return len(self.intent_embeddings)

