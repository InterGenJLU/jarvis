"""
Skill Manager

Discovers, loads, and manages skills.
Routes user intents to appropriate skills.
"""

import os
import sys
import inspect
import yaml
import importlib.util
import re
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

from core.logger import get_logger
from core.base_skill import BaseSkill, SkillMetadata
from core.honorific import resolve_honorific

# Generic keywords too ambiguous for suffix matching or bare-word routing
_generic_keywords = {"search", "open", "find", "look", "browse", "navigate", "web",
                     "file", "code", "directory", "count", "analyze", "amazon"}

# Keywords with explicit handler aliases get a bonus so they win ties
_keyword_aliases = {"google", "codebase"}


class SkillManager:
    """Manages skill discovery, loading, and execution"""
    
    def __init__(self, config, conversation, tts, responses, llm):
        """
        Initialize skill manager
        
        Args:
            config: Configuration object
            conversation: Conversation manager
            tts: Text-to-speech engine
            responses: Response library
            llm: LLM router
        """
        self.config = config
        self.conversation = conversation
        self.tts = tts
        self.responses = responses
        self.llm = llm
        self.logger = get_logger(__name__, config)
        
        # Get skills path from config
        self.skills_path = Path(config.get("skills.skills_path"))
        
        # Loaded skills
        self.skills: Dict[str, BaseSkill] = {}
        self.skill_metadata: Dict[str, SkillMetadata] = {}
        
        # Intent patterns (compiled for performance)
        self.intent_patterns: List[Tuple[re.Pattern, str, str, int]] = []
        
        # Pre-load the sentence-transformer model so first command isn't slow
        # (avoids audio input overflow from blocking during lazy load)
        try:
            from sentence_transformers import SentenceTransformer
            self._embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
            self.logger.info("Semantic embedding model pre-loaded")
        except Exception as e:
            self.logger.warning(f"Failed to pre-load embedding model: {e}")

        # Pre-computed embeddings for semantic intent examples.
        # Populated during load_skill() so match-time encoding is query-only.
        # Key: (skill_name, intent_id) → tensor of example embeddings.
        self._semantic_embedding_cache: dict = {}

        # Match metadata for console stats panel
        self._last_match_info = None

        self.logger.info(f"Skill Manager initialized, skills path: {self.skills_path}")
    
    def discover_skills(self) -> List[Path]:
        """
        Discover all skills in skills directory
        
        Returns:
            List of skill directory paths
        """
        if not self.skills_path.exists():
            self.logger.warning(f"Skills path does not exist: {self.skills_path}")
            return []
        
        skills = []
        
        # Walk through category directories
        for category_dir in self.skills_path.iterdir():
            if not category_dir.is_dir():
                continue
            
            # Each subdirectory is a skill
            for skill_dir in category_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                
                # Check if it has required files
                skill_file = skill_dir / "skill.py"
                metadata_file = skill_dir / "metadata.yaml"
                
                if skill_file.exists() and metadata_file.exists():
                    skills.append(skill_dir)
                    self.logger.debug(f"Discovered skill: {skill_dir.name}")
        
        return skills
    
    def load_skill(self, skill_path: Path) -> bool:
        """
        Load a single skill
        
        Args:
            skill_path: Path to skill directory
            
        Returns:
            True if loaded successfully
        """
        try:
            # Load metadata
            metadata_file = skill_path / "metadata.yaml"
            with open(metadata_file, 'r') as f:
                metadata_dict = yaml.safe_load(f)
            
            metadata = SkillMetadata(metadata_dict)
            
            # Check if enabled
            if not metadata.enabled:
                self.logger.info(f"Skill {metadata.name} is disabled, skipping")
                return False
            
            # Load skill module
            skill_file = skill_path / "skill.py"
            spec = importlib.util.spec_from_file_location(
                f"skills.{skill_path.parent.name}.{skill_path.name}",
                skill_file
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            
            # Find skill class (should inherit from BaseSkill)
            skill_class = None
            for item_name in dir(module):
                item = getattr(module, item_name)
                if (isinstance(item, type) and 
                    issubclass(item, BaseSkill) and 
                    item != BaseSkill):
                    skill_class = item
                    break
            
            if not skill_class:
                self.logger.error(f"No skill class found in {skill_file}")
                return False
            
            # Instantiate skill
            skill = skill_class(self.config, self.conversation, self.tts, self.responses)
            skill.category = metadata.category
            skill.description = metadata.description
            
            # Initialize skill
            if not skill.initialize():
                self.logger.error(f"Skill {metadata.name} failed to initialize")
                return False
            
            # Store skill
            self.skills[metadata.name] = skill
            self.skill_metadata[metadata.name] = metadata
            
            # Register intent patterns
            self._register_skill_intents(metadata.name, skill)
            
            # Log semantic intents and pre-compute embeddings
            if hasattr(skill, 'semantic_intents') and skill.semantic_intents:
                for intent_id in skill.semantic_intents:
                    data = skill.semantic_intents[intent_id]
                    examples = data.get('examples', [])
                    thresh = data.get('threshold', 0.75)
                    self.logger.info(f"  📋 {intent_id}: {len(examples)} examples, threshold={thresh}")
                    # Cache embeddings at load time — eliminates per-query re-encoding
                    if examples and hasattr(self, '_embedding_model'):
                        self._semantic_embedding_cache[(metadata.name, intent_id)] = \
                            self._embedding_model.encode(examples, convert_to_tensor=True, show_progress_bar=False)
            
            self.logger.info(f"✅ Loaded skill: {metadata.name} ({metadata.category})")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to load skill from {skill_path}: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _register_skill_intents(self, skill_name: str, skill: BaseSkill):
        """
        Register skill's intent patterns
        
        Args:
            skill_name: Skill name
            skill: Skill instance
        """
        for pattern, intent_data in skill.intents.items():
            # Convert pattern to regex
            # {variable} -> named capture group
            regex_pattern = re.escape(pattern)
            regex_pattern = regex_pattern.replace(r"\{", "(?P<").replace(r"\}", ">[^}]+)")
            regex_pattern = f"^{regex_pattern}$"
            
            compiled = re.compile(regex_pattern, re.IGNORECASE)
            priority = intent_data.get("priority", 5)
            
            self.intent_patterns.append((compiled, pattern, skill_name, priority))
            self.logger.debug(f"Registered pattern: {pattern} -> {skill_name}")
        
        # Sort by priority (highest first)
        self.intent_patterns.sort(key=lambda x: x[3], reverse=True)
    
    def load_all_skills(self) -> int:
        """
        Discover and load all skills
        
        Returns:
            Number of skills loaded
        """
        self.logger.info("Discovering skills...")
        skill_paths = self.discover_skills()
        
        self.logger.info(f"Found {len(skill_paths)} skills")
        
        loaded = 0
        for skill_path in skill_paths:
            if self.load_skill(skill_path):
                loaded += 1
        
        self.logger.info(f"Loaded {loaded}/{len(skill_paths)} skills")
        return loaded
    
    def match_intent(self, user_text: str) -> Optional[Tuple[str, str, Dict[str, Any]]]:
        """
        Match user text to a skill intent using 3-layer hybrid approach:
        Layer 1: Exact pattern match (fast)
        Layer 2: Fuzzy pattern match (contains)
        Layer 3: Keyword-based semantic match (fallback)
        
        Args:
            user_text: User's input text
            
        Returns:
            Tuple of (skill_name, pattern, entities) or None
        """
        # Normalize user text: lowercase, strip punctuation
        normalized = user_text.strip().lower()
        # Remove punctuation but preserve dots within file extensions
        # (e.g. "report.pptx" stays "report.pptx" so \bpptx\b matches)
        normalized = normalized.translate(str.maketrans('', '', '?!,;:'))
        normalized = re.sub(r'\.(?=\s|$)', '', normalized)  # only sentence-ending dots

        self.logger.debug(f"🔍 Normalized: '{normalized}'")

        # Bare generic word guard — block ALL layers, not just keywords
        _norm_words = normalized.split()
        if len(_norm_words) == 1 and _norm_words[0] in _generic_keywords:
            self.logger.info(f"Bare generic word '{_norm_words[0]}' — skipping to LLM")
            self._last_match_info = None
            return None
        
        # Debug: show first few patterns for system_info skill
        system_info_patterns = [
            (pattern, skill) for (_, pattern, skill, _) in self.intent_patterns 
            if skill == 'system_info'
        ]
        if system_info_patterns:
            self.logger.debug(f"📋 System_info has {len(system_info_patterns)} patterns")
            for pattern, _ in system_info_patterns[:3]:
                self.logger.debug(f"  - '{pattern}'")
        
        # LAYER 1: Try exact matches first (higher priority)
        self.logger.debug("Layer 1: testing %d exact patterns", len(self.intent_patterns))
        for compiled, pattern, skill_name, priority in self.intent_patterns:
            match = compiled.match(normalized)
            if match:
                entities = match.groupdict()
                self.logger.info(f"Matched intent (exact): {pattern} -> {skill_name}")
                self._last_match_info = {"layer": "exact", "skill_name": skill_name, "intent_id": pattern, "confidence": None, "handler_name": pattern}
                return (skill_name, pattern, entities)

        # LAYER 2: Try fuzzy matches (pattern contained anywhere in text)
        for compiled, pattern, skill_name, priority in self.intent_patterns:
            match = compiled.search(normalized)
            if match:
                entities = match.groupdict()
                self.logger.info(f"Matched intent (fuzzy): {pattern} -> {skill_name}")
                self._last_match_info = {"layer": "fuzzy", "skill_name": skill_name, "intent_id": pattern, "confidence": None, "handler_name": pattern}
                return (skill_name, pattern, entities)

        # LAYER 3: Try keyword-based matching (explicit signals beat fuzzy similarity)
        keyword_match = self._match_by_keywords(normalized)
        if keyword_match:
            skill_name, pattern, entities = keyword_match
            self.logger.info(f"Matched intent (keywords): {pattern} -> {skill_name}")
            self._last_match_info = {"layer": "keyword", "skill_name": skill_name, "intent_id": pattern, "confidence": None, "handler_name": pattern}
            return keyword_match

        # LAYER 4: Try semantic intent matching (embedding similarity fallback)
        semantic_match = self._match_semantic_intents(normalized)
        if semantic_match:
            skill_name, intent_id, entities = semantic_match
            self.logger.info(f"🎯 Matched semantic intent: {intent_id} -> {skill_name}")
            self._last_match_info = {"layer": "semantic", "skill_name": skill_name, "intent_id": intent_id, "confidence": entities.get("similarity"), "handler_name": intent_id}
            return (skill_name, intent_id, entities)

        self._last_match_info = None
        return None
    
    def _match_semantic_intents(self, user_text: str) -> Optional[Tuple[str, str, Dict]]:
        """Match using semantic intents (embedding similarity)"""
        from sentence_transformers import util

        if not hasattr(self, '_embedding_model'):
            return None

        user_embedding = self._embedding_model.encode(user_text, convert_to_tensor=True, show_progress_bar=False)

        best_match = None
        best_score = 0.0

        # Check all skills' semantic intents using pre-computed embeddings
        for skill_name, skill in self.skills.items():
            if not hasattr(skill, 'semantic_intents') or not skill.semantic_intents:
                continue

            for intent_id, data in skill.semantic_intents.items():
                threshold = data.get('threshold', 0.75)

                # Use cached embeddings (populated at skill load time)
                cache_key = (skill_name, intent_id)
                example_embeddings = self._semantic_embedding_cache.get(cache_key)
                if example_embeddings is None:
                    continue

                similarities = util.cos_sim(user_embedding, example_embeddings)
                max_sim = float(similarities.max())

                if max_sim > best_score and max_sim >= threshold:
                    best_score = max_sim
                    best_match = (skill_name, intent_id, {'original_text': user_text, 'similarity': max_sim})

        if best_match:
            self.logger.info(f"🎯 Semantic score={best_score:.2f}")
            self.logger.debug("Semantic match: skill=%s intent=%s score=%.3f",
                              best_match[0], best_match[1], best_score)
        else:
            self.logger.debug("Semantic match: no match (best=%.3f)", best_score)

        return best_match
    
    def _match_by_keywords(self, normalized_text: str) -> Optional[Tuple[str, str, Dict[str, Any]]]:
        """
        Match by keywords defined in skill metadata
        
        Args:
            normalized_text: Normalized user input
            
        Returns:
            Tuple of (skill_name, pattern, entities) or None
        """
        # Split text into words
        input_words = normalized_text.split()

        # Single generic word should not route — too ambiguous (e.g. bare "search", "open", "file")
        if len(input_words) == 1 and input_words[0] in _generic_keywords:
            return None

        # Check each skill's keywords
        best_score = 0
        tied_skills = []

        for skill_name, metadata in self.skill_metadata.items():
            keywords = getattr(metadata, 'keywords', [])
            if not keywords:
                continue

            # Count keyword matches. Alias keywords get +1 bonus to win ties.
            # Generic keywords score normally — the single-word guard above
            # handles the "bare generic" case.
            score = 0
            matched_kws = []
            for keyword in keywords:
                if re.search(r'\b' + re.escape(keyword.lower()) + r'\b', normalized_text):
                    if keyword.lower() in _keyword_aliases:
                        score += 2
                        matched_kws.append(f"{keyword}(+2)")
                    else:
                        score += 1
                        matched_kws.append(f"{keyword}(+1)")

            if matched_kws:
                self.logger.debug(
                    "Keyword routing: %s → score=%d [%s]",
                    skill_name, score, ", ".join(matched_kws),
                )

            if score > best_score:
                best_score = score
                tied_skills = [skill_name]
            elif score == best_score and score > 0:
                tied_skills.append(skill_name)

        # Ties fall through to semantic matching (Layer 4) for disambiguation
        if len(tied_skills) != 1:
            if tied_skills:
                self.logger.debug(
                    "Keyword tie (%d skills at score %d), falling to semantic",
                    len(tied_skills), best_score,
                )
            return None

        best_match = tied_skills[0]
        if best_score > 0:
            entities = {'_keyword_count': best_score}

            if best_match == 'weather':
                # Check for specific weather queries
                if "tomorrow" in normalized_text:
                    pattern = "weather tomorrow"
                elif "forecast" in normalized_text:
                    pattern = "what's the forecast"
                elif "rain" in normalized_text and "tomorrow" in normalized_text:
                    pattern = "will it rain tomorrow"
                elif entities.get('location'):
                    pattern = "weather in {location}"
                else:
                    # Check for location
                    entities = self._extract_location(normalized_text)
                    if entities.get('location'):
                        pattern = "weather in {location}"
                    else:
                        pattern = "what's the weather"
            else:
                # For non-weather skills, use the first matched keyword as pattern
                keywords = getattr(self.skill_metadata.get(best_match), 'keywords', [])
                matched_keywords = [kw for kw in keywords
                                    if re.search(r'\b' + re.escape(kw.lower()) + r'\b', normalized_text)]
                pattern = matched_keywords[0] if matched_keywords else best_match

            return (best_match, pattern, entities)
        
        return None
    
    def _extract_location(self, text: str) -> Dict[str, str]:
        """
        Extract location from text for weather queries
        
        Args:
            text: User input text
            
        Returns:
            Dict with 'location' key if found
        """
        # Common prepositions before location
        patterns = [
            r'in\s+([a-z\s]+?)(?:\s+\?|$)',
            r'for\s+([a-z\s]+?)(?:\s+\?|$)',
            r'at\s+([a-z\s]+?)(?:\s+\?|$)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                location = match.group(1).strip()
                # Remove common words that aren't part of location
                location = re.sub(r'\b(the|a|an|today|tomorrow|now)\b', '', location).strip()
                if location:
                    return {'location': location}
        
        return {}
    
    # ------------------------------------------------------------------
    # execute_intent helpers (extracted from monolithic method)
    # ------------------------------------------------------------------

    def _invoke_handler(self, handler, entities: dict):
        """Call a handler, passing entities if accepted by its signature."""
        sig = inspect.signature(handler)
        if 'entities' in sig.parameters:
            return handler(entities=entities or {})
        return handler()

    def _check_pending_confirmations(self, user_text: str) -> Optional[str]:
        """Check for pending 3-tuple confirmations in any skill.

        Must run before routing — "yes, please delete it" would otherwise
        match delete_file.
        """
        for skill_name, skill in self.skills.items():
            pending = getattr(skill, '_pending_confirmation', None)
            if pending and isinstance(pending, tuple) and len(pending) == 3:
                action, detail, expiry = pending
                import time as _time
                if _time.time() <= expiry:
                    handler = getattr(skill, 'confirm_action', None)
                    if handler:
                        self.logger.info(
                            "Pending confirmation for %s.%s, routing to confirm_action",
                            skill_name, action,
                        )
                        self._last_match_info = {
                            "layer": "pending_confirmation",
                            "skill_name": skill_name,
                            "intent_id": f"confirm_{action}",
                            "confidence": 1.0,
                            "handler_name": "confirm_action",
                        }
                        response = handler(entities={'original_text': user_text})
                        if isinstance(response, str):
                            response = resolve_honorific(response)
                        return response
        return None

    def _try_keyword_direct_match(self, skill, skill_name: str,
                                   user_text: str, entities: dict) -> Optional[str]:
        """LAYER 4a: keyword → handler name suffix match or alias."""
        skill_keywords = getattr(
            self.skill_metadata.get(skill_name), 'keywords', [],
        )
        matched_kws = sorted(
            [kw for kw in skill_keywords
             if re.search(r'\b' + re.escape(kw.lower()) + r'\b', user_text.lower())],
            key=len, reverse=True,
        )

        _handler_aliases = {"google": "search_web"}

        for kw in matched_kws:
            kw_lower = kw.lower()

            # Explicit alias
            if kw_lower in _handler_aliases:
                alias_target = _handler_aliases[kw_lower]
                for intent_id, intent_data in skill.semantic_intents.items():
                    if intent_data['handler'].__name__.lower() == alias_target:
                        self.logger.info(
                            "Keyword->intent alias match: %s (keyword=%s -> %s)",
                            intent_id, kw_lower, alias_target,
                        )
                        self._last_match_info = {
                            "layer": "keyword_alias",
                            "skill_name": skill_name,
                            "intent_id": intent_id,
                            "confidence": None,
                            "handler_name": intent_data['handler'].__name__,
                        }
                        return self._invoke_handler(intent_data['handler'], entities)

            # Skip generic keywords
            if kw_lower in _generic_keywords:
                continue

            # Suffix match
            suffix_matches = [
                (iid, idata)
                for iid, idata in skill.semantic_intents.items()
                if idata['handler'].__name__.lower().endswith(f"_{kw_lower}")
            ]
            if len(suffix_matches) == 1:
                intent_id, intent_data = suffix_matches[0]
                self.logger.info(
                    "Keyword->intent direct match: %s (keyword=%s)",
                    intent_id, kw_lower,
                )
                self._last_match_info = {
                    "layer": "keyword_direct",
                    "skill_name": skill_name,
                    "intent_id": intent_id,
                    "confidence": None,
                    "handler_name": intent_data['handler'].__name__.lower(),
                }
                return self._invoke_handler(intent_data['handler'], entities)

            if len(suffix_matches) > 1:
                # Ambiguous — disambiguate via semantic similarity
                result = self._disambiguate_suffix(
                    skill_name, suffix_matches, user_text, entities, kw_lower,
                )
                if result is not None:
                    return result

        return None

    def _disambiguate_suffix(self, skill_name: str, suffix_matches: list,
                              user_text: str, entities: dict,
                              kw_lower: str) -> Optional[str]:
        """Resolve ambiguous suffix matches via embedding similarity."""
        from sentence_transformers import util as _st_util
        if not hasattr(self, '_embedding_model'):
            return None
        _user_emb = self._embedding_model.encode(
            user_text, convert_to_tensor=True, show_progress_bar=False,
        )
        _best_sim, _best_pair = -1.0, None
        for _sid, _sdata in suffix_matches:
            _ex = self._semantic_embedding_cache.get((skill_name, _sid))
            if _ex is not None:
                _sim = float(_st_util.cos_sim(_user_emb, _ex).max())
                if _sim > _best_sim:
                    _best_sim, _best_pair = _sim, (_sid, _sdata)
        if _best_pair:
            intent_id, intent_data = _best_pair
            self.logger.info(
                "Keyword->intent disambiguated: %s (keyword=%s, score=%.2f)",
                intent_id, kw_lower, _best_sim,
            )
            self._last_match_info = {
                "layer": "keyword_direct",
                "skill_name": skill_name,
                "intent_id": intent_id,
                "confidence": _best_sim,
                "handler_name": intent_data['handler'].__name__.lower(),
            }
            return self._invoke_handler(intent_data['handler'], entities)
        return None

    def _try_keyword_semantic_fallback(self, skill, skill_name: str,
                                        user_text: str,
                                        entities: dict) -> Optional[str]:
        """LAYER 4b: Semantic similarity within the keyword-matched skill."""
        from sentence_transformers import util
        if not hasattr(self, '_embedding_model'):
            return None

        user_emb = self._embedding_model.encode(
            user_text, convert_to_tensor=True, show_progress_bar=False,
        )
        best_handler = None
        best_score = 0.0
        best_intent = None

        for intent_id, intent_data in skill.semantic_intents.items():
            cache_key = (skill_name, intent_id)
            ex_embs = self._semantic_embedding_cache.get(cache_key)
            if ex_embs is None:
                continue
            sims = util.cos_sim(user_emb, ex_embs)
            max_sim = float(sims.max())
            if max_sim > best_score:
                best_score = max_sim
                best_handler = intent_data['handler']
                best_intent = intent_id

        if not (best_handler and best_intent):
            return None

        intent_threshold = skill.semantic_intents[best_intent].get('threshold', 0.55)

        # Direct threshold match
        if best_score >= intent_threshold:
            self.logger.info(
                "Keyword->semantic fallback: %s (score=%.2f)",
                best_intent, best_score,
            )
            self._last_match_info = {
                "layer": "keyword_semantic",
                "skill_name": skill_name,
                "intent_id": best_intent,
                "confidence": best_score,
                "handler_name": best_handler.__name__,
            }
            return self._invoke_handler(best_handler, entities)

        # Relaxed threshold based on keyword count
        self.logger.info(
            "Keyword->semantic fallback rejected: %s (score=%.2f < threshold=%.2f)",
            best_intent, best_score, intent_threshold,
        )
        kw_count = (entities or {}).get('_keyword_count', 1)
        if kw_count >= 3:
            relaxed_threshold = max(intent_threshold * 0.4, 0.20)
        elif kw_count >= 2:
            relaxed_threshold = max(intent_threshold * 0.6, 0.30)
        else:
            relaxed_threshold = max(intent_threshold * 0.7, 0.40)

        if best_score >= relaxed_threshold:
            self.logger.info(
                "Keyword->semantic relaxed match: %s (score=%.2f >= relaxed %.2f)",
                best_intent, best_score, relaxed_threshold,
            )
            self._last_match_info = {
                "layer": "keyword_semantic_relaxed",
                "skill_name": skill_name,
                "intent_id": best_intent,
                "confidence": best_score,
                "handler_name": best_handler.__name__,
            }
            return self._invoke_handler(best_handler, entities)

        # Last resort: global semantic fallback
        return self._try_global_semantic_fallback(
            skill_name, user_text, best_score,
        )

    def _try_global_semantic_fallback(self, original_skill: str,
                                       user_text: str,
                                       rejected_score: float) -> Optional[str]:
        """Cross-skill semantic search when within-skill semantic fails."""
        self.logger.info(
            "Keyword->semantic rejected for %s (best=%.2f), trying global semantic",
            original_skill, rejected_score,
        )
        normalized = user_text.strip().lower().translate(
            str.maketrans('', '', '?.!,;:'),
        )
        semantic_match = self._match_semantic_intents(normalized)
        if semantic_match:
            fb_skill_name, fb_intent_id, fb_entities = semantic_match
            fb_skill = self.skills.get(fb_skill_name)
            if (fb_skill and hasattr(fb_skill, 'semantic_intents')
                    and fb_intent_id in fb_skill.semantic_intents):
                fb_entities['original_text'] = user_text
                fb_skill._last_user_text = user_text
                handler = fb_skill.semantic_intents[fb_intent_id]['handler']
                self._last_match_info = {
                    "layer": "keyword_global_semantic",
                    "skill_name": fb_skill_name,
                    "intent_id": fb_intent_id,
                    "confidence": fb_entities.get("similarity"),
                    "handler_name": handler.__name__,
                }
                response = self._invoke_handler(handler, fb_entities)
                if isinstance(response, str):
                    response = resolve_honorific(response)
                return response

        self.logger.info("Global semantic fallback also failed, falling through to LLM")
        self._last_match_info = {
            "layer": "keyword_rejected",
            "skill_name": original_skill,
            "intent_id": "rejected",
            "confidence": rejected_score,
            "handler_name": "→ LLM fallback",
        }
        return None

    # ------------------------------------------------------------------
    # execute_intent — main entry point
    # ------------------------------------------------------------------

    def execute_intent(self, user_text: str) -> Optional[str]:
        """Match and execute an intent.

        Decision chain:
        1. Pending confirmation check
        2. Pattern/keyword match via match_intent()
        3. Semantic intent (direct hit)
        4a. Keyword → handler name match
        4b. Keyword → within-skill semantic fallback
        5. Global semantic fallback
        6. Pattern-based skill handler
        Falls through to LLM if nothing matches.
        """
        # 1. Pending confirmations
        result = self._check_pending_confirmations(user_text)
        if result is not None:
            return result

        match_result = self.match_intent(user_text)
        if not match_result:
            return None

        skill_name, pattern, entities = match_result
        skill = self.skills.get(skill_name)
        if not skill:
            self.logger.error("Skill %s not found", skill_name)
            return None

        try:
            skill._last_user_text = user_text
            if entities is None:
                entities = {}
            if 'original_text' not in entities:
                entities['original_text'] = user_text

            # 3. Direct semantic intent match
            if hasattr(skill, 'semantic_intents') and pattern in skill.semantic_intents:
                handler = skill.semantic_intents[pattern]['handler']
                self.logger.debug("Handler dispatch: skill=%s intent=%s handler=%s entities=%d",
                                  skill_name, pattern, handler.__name__, len(entities))
                if self._last_match_info:
                    self._last_match_info["handler_name"] = handler.__name__
                response = self._invoke_handler(handler, entities)
                if isinstance(response, str):
                    response = resolve_honorific(response)
                return response

            # 4a + 4b. Keyword-based routing with semantic fallbacks.
            # If the skill has semantic intents, this block is authoritative:
            # either a semantic handler matches or we return None (→ LLM).
            # We do NOT fall through to handle_intent because the keyword
            # pattern (e.g. "open") is not a real intent for handle_intent.
            if hasattr(skill, 'semantic_intents') and skill.semantic_intents:
                result = self._try_keyword_direct_match(
                    skill, skill_name, user_text, entities,
                )
                if result is not None:
                    return result

                result = self._try_keyword_semantic_fallback(
                    skill, skill_name, user_text, entities,
                )
                # Whether matched or not, this block is terminal for
                # keyword-routed skills with semantic intents.
                return result

            # 6. Pattern-based skill handler (skills without semantic intents)
            response = skill.handle_intent(pattern, entities)
            if isinstance(response, str):
                response = resolve_honorific(response)
            return response

        except Exception as e:
            self.logger.error("Error executing skill %s: %s", skill_name, e)
            import traceback
            traceback.print_exc()
            return "I'm sorry, I encountered an error processing that request."
    
    def register_virtual_skill(self, name: str, intent_examples: list):
        """Register a lightweight skill for MCP tool semantic pruning.

        Creates a minimal skill-like object with semantic_intents so the
        semantic pruner in conversation_router can score it alongside real
        skills.  No handler — execution goes through tool_registry handlers.

        Args:
            name: Virtual skill name (e.g. "mcp_email").
            intent_examples: List of example phrases for semantic matching.
        """
        import types

        virtual = types.SimpleNamespace(
            semantic_intents={
                "default": {
                    "examples": intent_examples,
                    "threshold": 0.40,
                },
            },
        )
        self.skills[name] = virtual

        # Pre-compute embeddings for the pruner cache
        if hasattr(self, '_embedding_model') and self._embedding_model:
            embeddings = self._embedding_model.encode(
                intent_examples, convert_to_tensor=True, show_progress_bar=False,
            )
            self._semantic_embedding_cache[(name, "default")] = embeddings
            self.logger.info(
                f"Registered virtual skill '{name}' with "
                f"{len(intent_examples)} intent examples"
            )
        else:
            self.logger.warning(
                f"Registered virtual skill '{name}' without embeddings "
                f"(no embedding model loaded)"
            )

    def get_skill(self, name: str) -> Optional[BaseSkill]:
        """Get skill by name"""
        return self.skills.get(name)
    
    def list_skills(self) -> List[str]:
        """Get list of loaded skill names"""
        return list(self.skills.keys())
    
    def get_skills_by_category(self, category: str) -> List[str]:
        """Get skills in a category"""
        return [
            name for name, skill in self.skills.items()
            if skill.category == category
        ]


def get_skill_manager(config, conversation, tts, responses, llm) -> SkillManager:
    """Get skill manager instance"""
    return SkillManager(config, conversation, tts, responses, llm)
