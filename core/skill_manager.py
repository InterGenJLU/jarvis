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
                            self._embedding_model.encode(examples, convert_to_tensor=True)
            
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
        # Remove ALL punctuation (not just trailing)
        normalized = normalized.translate(str.maketrans('', '', '?.!,;:'))
        
        self.logger.debug(f"🔍 Normalized: '{normalized}'")
        
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

        user_embedding = self._embedding_model.encode(user_text, convert_to_tensor=True)

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
        words = set(normalized_text.split())
        
        # Check each skill's keywords
        best_match = None
        best_score = 0

        # Keywords with explicit handler aliases get a bonus so they win ties
        # (e.g. "search" → search_web should beat "graphics" → gpu_info)
        _keyword_aliases = {"google", "search"}

        for skill_name, metadata in self.skill_metadata.items():
            # Access keywords attribute directly
            keywords = getattr(metadata, 'keywords', [])
            if not keywords:
                continue

            # Count how many keywords match (whole-word only to avoid substring false positives)
            # Alias keywords get +1 bonus to win ties against regular keywords
            matches = sum(
                (2 if keyword.lower() in _keyword_aliases else 1)
                for keyword in keywords
                if re.search(r'\b' + re.escape(keyword.lower()) + r'\b', normalized_text)
            )

            if matches > best_score:
                best_score = matches
                best_match = skill_name
        
        if best_match and best_score > 0:
            entities = {}

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
    
    def execute_intent(self, user_text: str) -> Optional[str]:
        """
        Match and execute an intent
        
        Args:
            user_text: User's input text
            
        Returns:
            Response text or None if no match
        """
        match_result = self.match_intent(user_text)
        
        if not match_result:
            return None
        
        skill_name, pattern, entities = match_result
        
        # Get skill
        skill = self.skills.get(skill_name)
        if not skill:
            self.logger.error(f"Skill {skill_name} not found")
            return None
        
        try:
            # Store the original text on the skill so handlers can access it
            skill._last_user_text = user_text

            # Ensure entities always contains the original user text
            if entities is None:
                entities = {}
            if 'original_text' not in entities:
                entities['original_text'] = user_text

            # For semantic matches, call the handler directly
            if hasattr(skill, 'semantic_intents') and pattern in skill.semantic_intents:
                handler = skill.semantic_intents[pattern]['handler']
                if self._last_match_info:
                    self._last_match_info["handler_name"] = handler.__name__
                # Pass entities if handler accepts them
                sig = inspect.signature(handler)
                if 'entities' in sig.parameters:
                    response = handler(entities=entities or {})
                else:
                    response = handler()
                if isinstance(response, str):
                    response = resolve_honorific(response)
                return response

            # If keyword match landed us on a skill with semantic intents,
            # find the best semantic intent within this skill (relaxed threshold)
            if hasattr(skill, 'semantic_intents') and skill.semantic_intents:
                # LAYER 4a: Check if a matched keyword directly names an intent
                # e.g. keyword "amazon" → intent "search_amazon", "youtube" → "search_youtube"
                # Try all matched keywords, longest first (most specific wins)
                skill_keywords = getattr(self.skill_metadata.get(skill_name), 'keywords', [])
                matched_kws = sorted(
                    [kw for kw in skill_keywords if re.search(r'\b' + re.escape(kw.lower()) + r'\b', user_text.lower())],
                    key=len, reverse=True
                )

                # Explicit keyword→handler aliases (keywords that don't suffix-match naturally)
                _keyword_aliases = {
                    "google": "search_web",
                    "search": "search_web",
                }
                # Generic keywords too ambiguous for suffix matching
                _generic_keywords = {"search", "open", "find", "look", "browse", "navigate", "web",
                                         "file", "code", "directory", "count", "analyze"}

                for kw in matched_kws:
                    kw_lower = kw.lower()

                    # Check explicit alias first
                    if kw_lower in _keyword_aliases:
                        alias_target = _keyword_aliases[kw_lower]
                        for intent_id, intent_data in skill.semantic_intents.items():
                            if intent_data['handler'].__name__.lower() == alias_target:
                                self.logger.info(f"Keyword->intent alias match: {intent_id} (keyword={kw_lower} -> {alias_target})")
                                self._last_match_info = {"layer": "keyword_alias", "skill_name": skill_name, "intent_id": intent_id, "confidence": None, "handler_name": intent_data['handler'].__name__}
                                h = intent_data['handler']
                                response = h(entities=entities or {}) if 'entities' in inspect.signature(h).parameters else h()
                                return response

                    # Skip suffix matching for generic keywords
                    if kw_lower in _generic_keywords:
                        continue

                    for intent_id, intent_data in skill.semantic_intents.items():
                        handler_name = intent_data['handler'].__name__.lower()
                        # Match keyword as a suffix: "search_amazon" ends with "_amazon"
                        if handler_name.endswith(f"_{kw_lower}"):
                            self.logger.info(f"Keyword->intent direct match: {intent_id} (keyword={kw_lower})")
                            self._last_match_info = {"layer": "keyword_direct", "skill_name": skill_name, "intent_id": intent_id, "confidence": None, "handler_name": handler_name}
                            h = intent_data['handler']
                            response = h(entities=entities or {}) if 'entities' in inspect.signature(h).parameters else h()
                            return response

                # LAYER 4b: Semantic similarity within the matched skill
                from sentence_transformers import util
                if hasattr(self, '_embedding_model'):
                    user_emb = self._embedding_model.encode(user_text, convert_to_tensor=True)
                    best_handler = None
                    best_score = 0.0
                    best_intent = None
                    for intent_id, intent_data in skill.semantic_intents.items():
                        # Use cached embeddings (populated at skill load time)
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
                    # Use the intent's own threshold (not a fixed global one)
                    if best_handler and best_intent:
                        intent_threshold = skill.semantic_intents[best_intent].get('threshold', 0.55)
                        if best_score >= intent_threshold:
                            self.logger.info(f"Keyword->semantic fallback: {best_intent} (score={best_score:.2f})")
                            self._last_match_info = {"layer": "keyword_semantic", "skill_name": skill_name, "intent_id": best_intent, "confidence": best_score, "handler_name": best_handler.__name__}
                            response = best_handler(entities=entities or {}) if 'entities' in inspect.signature(best_handler).parameters else best_handler()
                            return response
                        else:
                            self.logger.info(f"Keyword->semantic fallback rejected: {best_intent} (score={best_score:.2f} < threshold={intent_threshold})")
                            # Keyword already confirmed the skill — try relaxed threshold
                            relaxed_threshold = max(intent_threshold * 0.7, 0.40)
                            if best_score >= relaxed_threshold:
                                self.logger.info(f"Keyword->semantic relaxed match: {best_intent} (score={best_score:.2f} >= relaxed {relaxed_threshold:.2f})")
                                self._last_match_info = {"layer": "keyword_semantic_relaxed", "skill_name": skill_name, "intent_id": best_intent, "confidence": best_score, "handler_name": best_handler.__name__}
                                response = best_handler(entities=entities or {}) if 'entities' in inspect.signature(best_handler).parameters else best_handler()
                                return response
                            else:
                                # Keyword matched but semantic rejected — try global semantic before LLM
                                self.logger.info(f"Keyword->semantic rejected for {skill_name} (best={best_score:.2f} < relaxed={relaxed_threshold:.2f}), trying global semantic fallback")
                                normalized_fallback = user_text.strip().lower().translate(str.maketrans('', '', '?.!,;:'))
                                semantic_match = self._match_semantic_intents(normalized_fallback)
                                if semantic_match:
                                    fb_skill_name, fb_intent_id, fb_entities = semantic_match
                                    fb_skill = self.skills.get(fb_skill_name)
                                    if fb_skill and hasattr(fb_skill, 'semantic_intents') and fb_intent_id in fb_skill.semantic_intents:
                                        fb_entities['original_text'] = user_text  # Use original, not normalized (preserves periods in filenames)
                                        handler = fb_skill.semantic_intents[fb_intent_id]['handler']
                                        self._last_match_info = {
                                            "layer": "keyword_global_semantic",
                                            "skill_name": fb_skill_name,
                                            "intent_id": fb_intent_id,
                                            "confidence": fb_entities.get("similarity"),
                                            "handler_name": handler.__name__
                                        }
                                        sig = inspect.signature(handler)
                                        response = handler(entities=fb_entities) if 'entities' in sig.parameters else handler()
                                        if isinstance(response, str):
                                            response = resolve_honorific(response)
                                        return response
                                self.logger.info(f"Global semantic fallback also failed, falling through to LLM")
                                return None

            # Execute skill handler for pattern-based matches
            response = skill.handle_intent(pattern, entities)
            if isinstance(response, str):
                response = resolve_honorific(response)
            return response

        except Exception as e:
            self.logger.error(f"Error executing skill {skill_name}: {e}")
            import traceback
            traceback.print_exc()
            return "I'm sorry, I encountered an error processing that request."
    
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
