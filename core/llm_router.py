"""
LLM Router

Routes requests to appropriate LLM (local Qwen or Claude API fallback).
Handles prompt formatting, response quality gating, and smart fallback.
Supports tool calling (Qwen3) for web research integration.

Fallback strategy (local-first):
  1. Qwen generates response
  2. Quality gate checks for bad output (empty, gibberish, echoes)
  3. If bad, retry local once with a nudge prompt
  4. If still bad, fall back to Claude API as last resort
"""

import subprocess
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional, Iterator, Union
from pathlib import Path

from datetime import date, datetime
from core.logger import get_logger
from core.honorific import get_honorific, get_formal_address
import requests
import json


@dataclass
class ToolCallRequest:
    """Sentinel yielded by stream_with_tools() when the LLM requests a tool call."""
    name: str
    arguments: dict
    call_id: str = ""


# ---------------------------------------------------------------------------
# Tool schemas — auto-assembled from core/tools/*.py via tool_registry
# ---------------------------------------------------------------------------
from core.tool_registry import (  # noqa: E402
    ALL_TOOLS, SKILL_TOOLS,
    WEB_SEARCH_TOOL, GET_SYSTEM_INFO_TOOL,
    FIND_FILES_TOOL, GET_WEATHER_TOOL, MANAGE_REMINDERS_TOOL,
    DEVELOPER_TOOLS_TOOL, GET_NEWS_TOOL,
    TAKE_SCREENSHOT_TOOL, CAPTURE_WEBCAM_TOOL,
    build_tool_prompt_rules,
)


class LLMRouter:
    """Routes LLM requests to local or API models with smart fallback"""

    def __init__(self, config):
        """
        Initialize LLM router

        Args:
            config: Configuration object
        """
        self.config = config
        self.logger = get_logger(__name__, config)

        # User location (injected into system prompt)
        self.home_location = config.get("location.home_address")

        # Local LLM configuration (llama.cpp)
        self.local_model_path = config.get("llm.local.model_path")
        self.llama_completion = os.path.expanduser(config.get("llm.local.llama_completion"))
        self.context_size = config.get("llm.local.context_size", 8192)
        self.gpu_layers = config.get("llm.local.gpu_layers", 999)
        self.batch_size = config.get("llm.local.batch_size", 512)
        self.ubatch_size = config.get("llm.local.ubatch_size", 128)
        self.temperature = config.get("llm.local.temperature", 0.6)
        self.top_p = config.get("llm.local.top_p", 0.8)
        self.top_k = config.get("llm.local.top_k", 20)
        self.tool_calling = config.get("llm.local.tool_calling", False)

        # Verify local model exists
        if self.local_model_path:
            model_path = Path(self.local_model_path).expanduser()
            if not model_path.exists():
                self.logger.warning(f"Local LLM model not found: {model_path}")
                self.local_model_path = None

        # API configuration (Claude)
        # Call metadata for console stats panel
        self.last_call_info = None

        self.api_provider = config.get("llm.api.provider", "anthropic")
        self.api_model = config.get("llm.api.model", "claude-sonnet-4-20250514")
        self.api_key_env = config.get("llm.api.api_key_env")

        # Fallback configuration
        self.fallback_enabled = config.get("semantic_matching.fallback_to_llm", True)
        self.api_call_count = 0

        self.logger.info(f"LLM Router initialized (fallback={'enabled' if self.fallback_enabled else 'disabled'})")
        if self.local_model_path:
            self.logger.info(f"Local model: {Path(self.local_model_path).name}")
    
    @staticmethod
    def strip_filler(text: str) -> str:
        """Strip trailing 'feel free to ask' filler from LLM responses."""
        import re
        # Match common Qwen filler patterns at the end of responses
        filler_patterns = [
            r"\s*If you have any (?:specific |more )?questions.*$",
            r"\s*(?:Please )?[Ff]eel free to ask.*$",
            r"\s*(?:Please )?[Ll]et me know if (?:you )?(?:need|have|want).*$",
            r"\s*Don't hesitate to (?:ask|reach out).*$",
            r"\s*I'm here (?:to help|if you need).*$",
            r"\s*How (?:can|may) I (?:assist|help) you (?:further|today).*$",
            r"\s*Is there anything else (?:you )?(?:need|want|would like|I can).*$",
            r"\s*Would you like (?:to know|anything else|more details|further).*$",
            r"\s*(?:Do you )?[Nn]eed (?:anything|something) else.*$",
            r"\s*What else (?:can|may|would) (?:I|you).*$",
        ]
        result = text.rstrip()
        for pattern in filler_patterns:
            result = re.sub(pattern, "", result, flags=re.IGNORECASE).rstrip()
        # Clean trailing punctuation artifacts (e.g. lone period after stripped sentence)
        result = result.rstrip()
        if result and result[-1] not in ".!?)]\"'":
            result += "."
        return result

    @staticmethod
    def strip_metric(text: str, command: str = "") -> str:
        """Strip parenthetical metric conversions from LLM responses.

        Qwen tends to include '(X,XXX kilometers)' etc. even when told not to.
        Skipped if the user explicitly asked for metric units.
        """
        import re
        metric_words = ("metric", "kilometers", "km", "celsius", "kilograms", "kg", "in metric")
        if any(w in command.lower() for w in metric_words):
            return text
        # Remove patterns like (1,207 kilometers), (2,575 km), (25°C), (90 kg)
        text = re.sub(r'\s*\([\d,.]+ (?:kilometers?|km|kilograms?|kg|°?C|celsius)\)', '', text, flags=re.IGNORECASE)
        return text

    def _check_response_quality(self, response: str, user_message: str) -> str:
        """
        Check if a local LLM response is usable.

        Returns:
            Empty string if quality is acceptable, otherwise a reason string
        """
        if not response or not response.strip():
            return "empty"

        text = response.strip()

        # Too short to be meaningful (but allow short acknowledgments)
        if len(text) < 3:
            return "too_short"

        # Repeated token gibberish (e.g. "the the the the")
        # Exempt short responses (< 10 words) — tool summaries and brief
        # answers legitimately reuse words (e.g. "The temperature is 72°F")
        words = text.lower().split()
        if len(words) >= 10:
            unique_ratio = len(set(words)) / len(words)
            if unique_ratio < 0.25:
                return "repetitive"

        # Response is just the user's question echoed back
        if user_message and text.lower().strip('?.! ') == user_message.lower().strip('?.! '):
            return "echo"

        # Contains raw prompt artifacts that cleaning missed
        bad_markers = ["<|im_start|>", "<|im_end|>", "[INST]", "[/INST]", "<<SYS>>", "<think>", "</think>"]
        for marker in bad_markers:
            if marker in text:
                self.logger.debug("Quality gate: artifacts (%s) in response: %.80s", marker, text)
                return "artifacts"

        return ""

    @staticmethod
    def _build_user_message(text: str, image_data: str | None = None) -> dict:
        """Build a user message dict, optionally with multimodal image content.

        Args:
            text: The text content of the user message
            image_data: Optional base64-encoded image data

        Returns:
            OpenAI-compatible message dict with string or array content
        """
        _log = get_logger(__name__)
        _log.debug("_build_user_message: text_len=%d image=%s%s",
                    len(text) if text else 0,
                    "yes" if image_data else "no",
                    f" base64_len={len(image_data)}" if image_data else "")
        if image_data:
            return {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{image_data}"
                    }},
                ],
            }
        return {"role": "user", "content": text}

    def generate(self, prompt: str, use_api: bool = False, max_tokens: int = 512,
                 temperature: float | None = None, timeout: int = 30) -> str:
        """
        Generate response from LLM.

        When use_api is False and fallback is enabled, uses smart fallback:
        local → quality check → retry local → quality check → Claude API

        Args:
            prompt: Input prompt
            use_api: Whether to force API (Claude) instead of local
            max_tokens: Maximum tokens to generate
            timeout: HTTP request timeout in seconds (local only)

        Returns:
            Generated text response
        """
        if use_api:
            return self._generate_api(prompt, max_tokens)
        else:
            return self._generate_local(prompt, max_tokens, temperature=temperature,
                                        timeout=timeout)
    
    def _generate_local(self, user_message: str, max_tokens: int = 512,
                        temperature: float | None = None,
                        timeout: int = 30) -> str:
        """Generate using llama-server REST API"""
        from core import persona
        system_prompt = persona.system_prompt_brief()
        model_name = Path(self.local_model_path).stem if self.local_model_path else "unknown"

        start = time.time()
        try:
            response = requests.post(
                "http://127.0.0.1:8080/v1/chat/completions",
                json={
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message}
                    ],
                    "temperature": temperature if temperature is not None else self.temperature,
                    "top_p": self.top_p,
                    "top_k": self.top_k,
                    "max_tokens": max_tokens
                },
                timeout=timeout
            )
            # Log context overflow clearly instead of generic error
            if response.status_code == 400:
                try:
                    err = response.json().get("error", {})
                except Exception:
                    err = {}
                error_msg = str(err) if err else "bad_request"
                if err.get("type") == "exceed_context_size_error":
                    error_msg = "context_overflow"
                    self.logger.error(
                        f"Context overflow: {err.get('n_prompt_tokens', '?')}/"
                        f"{err.get('n_ctx', '?')} tokens"
                    )
                else:
                    self.logger.error(f"LLM server rejected request: {err}")
                self.last_call_info = {
                    "provider": "qwen", "method": "generate",
                    "input_tokens": None, "output_tokens": None,
                    "estimated_tokens": None, "model": model_name,
                    "latency_ms": (time.time() - start) * 1000,
                    "ttft_ms": None, "quality_gate": False,
                    "is_fallback": False, "error": error_msg,
                }
                return ""
            response.raise_for_status()
            data = response.json()
            usage = data.get("usage", {})
            self.last_call_info = {
                "provider": "qwen", "method": "generate",
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "estimated_tokens": None, "model": model_name,
                "latency_ms": (time.time() - start) * 1000,
                "ttft_ms": None, "quality_gate": False,
                "is_fallback": False, "error": None,
            }
            return self.strip_filler(data["choices"][0]["message"]["content"].strip())
        except Exception as e:
            self.logger.error(f"LLM server error: {e}")
            self.last_call_info = {
                "provider": "qwen", "method": "generate",
                "input_tokens": None, "output_tokens": None,
                "estimated_tokens": None, "model": model_name,
                "latency_ms": (time.time() - start) * 1000,
                "ttft_ms": None, "quality_gate": False,
                "is_fallback": False, "error": str(e),
            }
            return ""
    
    def _generate_api(self, prompt: str, max_tokens: int = 512) -> str:
        """
        Generate response using Claude API

        Args:
            prompt: Input prompt
            max_tokens: Maximum tokens to generate

        Returns:
            Generated text
        """
        start = time.time()
        try:
            # Import anthropic SDK
            import anthropic

            # Get API key
            api_key = self.config.get_env(self.api_key_env)
            if not api_key or api_key == "your_key_here":
                self.logger.error("Claude API key not configured")
                self.last_call_info = {
                    "provider": "claude", "method": "generate",
                    "input_tokens": None, "output_tokens": None,
                    "estimated_tokens": None, "model": self.api_model,
                    "latency_ms": (time.time() - start) * 1000,
                    "ttft_ms": None, "quality_gate": False,
                    "is_fallback": True, "error": "api_key_not_configured",
                }
                return "I'm sorry, I don't have access to the Claude API at the moment."

            # Create client
            client = anthropic.Anthropic(api_key=api_key)

            self.logger.debug("Calling Claude API...")

            # Generate response
            message = client.messages.create(
                model=self.api_model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )

            response = message.content[0].text
            self.last_call_info = {
                "provider": "claude", "method": "generate",
                "input_tokens": message.usage.input_tokens,
                "output_tokens": message.usage.output_tokens,
                "estimated_tokens": None, "model": self.api_model,
                "latency_ms": (time.time() - start) * 1000,
                "ttft_ms": None, "quality_gate": False,
                "is_fallback": True, "error": None,
            }

            return response

        except ImportError:
            self.logger.error("anthropic package not installed")
            self.last_call_info = {
                "provider": "claude", "method": "generate",
                "input_tokens": None, "output_tokens": None,
                "estimated_tokens": None, "model": self.api_model,
                "latency_ms": (time.time() - start) * 1000,
                "ttft_ms": None, "quality_gate": False,
                "is_fallback": True, "error": "anthropic_not_installed",
            }
            return "I'm sorry, the Claude API is not available."
        except Exception as e:
            self.logger.error(f"Claude API call failed: {e}")
            self.last_call_info = {
                "provider": "claude", "method": "generate",
                "input_tokens": None, "output_tokens": None,
                "estimated_tokens": None, "model": self.api_model,
                "latency_ms": (time.time() - start) * 1000,
                "ttft_ms": None, "quality_gate": False,
                "is_fallback": True, "error": str(e),
            }
            return ""
    
    def _clean_llm_output(self, output: str) -> str:
        """
        Clean up LLM output (remove prompt echoes, artifacts)
        
        Args:
            output: Raw LLM output
            
        Returns:
            Cleaned text
        """
        text = output.strip()
        
        # First pass: Remove question echoes like "what is 2+2? Four, sir."
        import re
        # Match common question words followed by answer
        # Pattern: [question words] [2+ spaces or newline] [Answer starting with capital]
        question_echo = r'^.*(what|how|where|when|who|why|is|are|can|do|does).*?[\s]{2,}([A-Z][^.]*\.).*$'
        match = re.match(question_echo, text, re.IGNORECASE | re.DOTALL)
        if match:
            text = match.group(2).strip()
        
        # Mistral-specific: Remove entire [INST] block if echoed
        if "[INST]" in text and "[/INST]" in text:
            # Extract everything after [/INST]
            parts = text.split("[/INST]")
            if len(parts) > 1:
                text = parts[-1].strip()
        
        # Remove system prompt if it leaked through
        if "You are JARVIS" in text:
            # Try to find where actual response starts
            # Look for common response patterns after the prompt
            h = get_honorific()
            patterns = [
                f"Good morning, {h}",
                f"Good afternoon, {h}",
                f"Good evening, {h}",
                f"Hello, {h}",
                f"Yes, {h}",
                f"Of course, {h}",
                f"Certainly, {h}"
            ]
            
            for pattern in patterns:
                if pattern in text:
                    # Extract from this point forward
                    text = text[text.index(pattern):]
                    break
            else:
                # If no pattern found, try splitting on "User:" or similar
                if "User:" in text:
                    parts = text.split("User:")
                    if len(parts) > 1:
                        # Get the text after "User: [their message]"
                        remaining = parts[-1]
                        # Find the response (after their message)
                        if "\n" in remaining:
                            lines = remaining.split("\n")
                            # First non-empty line after user message is response
                            for line in lines[1:]:
                                if line.strip() and not any(x in line for x in ["You are", "USER:", "ASSISTANT:"]):
                                    text = line.strip()
                                    break
        
        # Remove "USER:" echoes (sometimes LLM echoes the prompt)
        if "USER:" in text or "User:" in text:
            # Remove everything up to and including "User: [message]"
            for marker in ["USER:", "User:"]:
                if marker in text:
                    parts = text.split(marker)
                    if len(parts) > 1:
                        # Get everything after the user message
                        remaining = parts[-1]
                        # Find first sentence that's a response
                        sentences = remaining.split(".")
                        for sent in sentences:
                            if sent.strip() and "You are" not in sent and len(sent.strip()) > 5:
                                text = sent.strip()
                                if not text.endswith("."):
                                    text += "."
                                break
        
        # Remove end markers
        end_markers = ["[end of text]", "</s>", "[INST]", "[/INST]", "<|im_end|>", "<|eot_id|>"]
        for marker in end_markers:
            text = text.replace(marker, "")
        
        # Remove common artifacts
        artifacts = [
            "USER:", "ASSISTANT:",
            "Human:", "AI:",
        ]
        
        for artifact in artifacts:
            text = text.replace(artifact, "")
        
        # Remove leading/trailing whitespace
        text = text.strip()
        
        # Remove question echo if LLM repeated the user's question
        # Pattern: "user question? Answer here"
        if "?" in text:
            parts = text.split("?", 1)
            if len(parts) == 2:
                question_part = parts[0].strip()
                answer_part = parts[1].strip()
                
                # If the "question" part looks like it was just echoing user input
                # and the answer part is substantive, use just the answer
                question_words = question_part.lower().split()
                if (len(question_words) > 3 and 
                    any(word in question_words for word in ["what", "how", "when", "where", "who", "why"]) and
                    len(answer_part) > 10):
                    text = answer_part
        
        # If response is suspiciously short (< 5 chars), it's probably a fragment - return empty
        if len(text) < 5:
            self.logger.warning(f"LLM output too short after cleaning: '{text}'")
            return f"I apologize, {get_honorific()}, but I'm having trouble formulating a response."
        
        # Final check: if still contains "You are JARVIS", something went wrong
        if "You are JARVIS" in text:
            self.logger.error("Failed to clean LLM output - prompt still present")
            return f"Good morning, {get_honorific()}."  # Safe fallback
        
        return text
    
    @staticmethod
    def _parse_history_string(history: str) -> list:
        """Parse format_history_for_llm() output into message dicts.

        Handles both plain format (``USER: text``) and timestamped format
        (``[today 2:30 PM] USER: text``) produced by ConversationManager.
        """
        import re
        messages = []
        for line in history.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            # Strip optional leading timestamp bracket: [today 2:30 PM]
            line = re.sub(r'^\[.*?\]\s*', '', line)
            if line.startswith("USER:"):
                messages.append({"role": "user", "content": line[5:].strip()})
            elif line.startswith("ASSISTANT:"):
                messages.append({"role": "assistant", "content": line[10:].strip()})
        return messages

    def _build_system_prompt(self) -> str:
        """Build the JARVIS system prompt (delegated to persona module)."""
        from core import persona
        return persona.system_prompt(home_location=self.home_location)

    @staticmethod
    def _estimate_max_tokens(query: str) -> int:
        """Estimate appropriate max_tokens based on query complexity.

        Short (150):  Simple factual, greetings, yes/no
        Medium (250): General questions, opinions, conversational
        Long (400):   Explanations, comparisons, deep knowledge, multi-part
        """
        q = query.strip().lower()

        # Short — quick exchanges
        short_signals = [
            "what time", "what's the time", "what day", "what's the date",
            "how are you", "thank you", "thanks", "goodbye", "good morning",
            "good night", "never mind", "cancel", "stop", "yes", "no",
        ]
        for signal in short_signals:
            if signal in q:
                return 150

        # Long — explanation / deep knowledge queries
        long_signals = [
            "why ", "why?", "how does", "how do ", "how is ", "how are ",
            "explain", "describe", "compare", "difference between",
            "tell me about", "what causes", "what happens when",
            "what would happen", "going back to", "elaborate",
            "more about", "in detail", "walk me through",
            "what's the history", "pros and cons",
            "rank ", "rank the", "list ", "list the", "top ",
            "best ", "worst ", "all the",
        ]
        for signal in long_signals:
            if signal in q:
                return 400

        # Long — question length itself suggests complexity
        if len(q.split()) > 15:
            return 400

        # Medium — default for everything else
        return 250

    def _build_chat_prompt(self, user_message: str, conversation_history: str = "",
                           memory_context: str = None) -> str:
        """Build ChatML-formatted prompt for Qwen"""
        system_prompt = self._build_system_prompt()
        if memory_context:
            system_prompt += f"\n\n{memory_context}"
        if conversation_history:
            return f"<|im_start|>system\n{system_prompt}<|im_end|>\n{conversation_history}<|im_start|>assistant\n"
        else:
            return f"<|im_start|>system\n{system_prompt}<|im_end|>\n<|im_start|>user\n{user_message}<|im_end|>\n<|im_start|>assistant\n"

    def _generate_api_chat(self, user_message: str, conversation_history: str = "",
                           max_tokens: int = None,
                           conversation_messages: list = None) -> str:
        """Generate chat response via Claude API with proper message format"""
        if max_tokens is None:
            max_tokens = self._estimate_max_tokens(user_message)
        start = time.time()
        try:
            import anthropic

            api_key = self.config.get_env(self.api_key_env)
            if not api_key or api_key == "your_key_here":
                self.logger.error("Claude API key not configured")
                self.last_call_info = {
                    "provider": "claude", "method": "chat",
                    "input_tokens": None, "output_tokens": None,
                    "estimated_tokens": None, "model": self.api_model,
                    "latency_ms": (time.time() - start) * 1000,
                    "ttft_ms": None, "quality_gate": False,
                    "is_fallback": True, "error": "api_key_not_configured",
                }
                return ""

            client = anthropic.Anthropic(api_key=api_key)
            system_prompt = self._build_system_prompt()

            # Build messages — prefer pre-built list over string parsing
            messages = []
            if conversation_messages:
                messages = list(conversation_messages)
            elif conversation_history:
                messages = self._parse_history_string(conversation_history)

            if not messages or messages[-1]["role"] != "user":
                messages.append({"role": "user", "content": user_message})

            self.logger.info(f"🔄 Claude API fallback (call #{self.api_call_count + 1})")

            message = client.messages.create(
                model=self.api_model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=messages
            )

            response = message.content[0].text
            elapsed_ms = (time.time() - start) * 1000
            self.api_call_count += 1
            self.last_call_info = {
                "provider": "claude", "method": "chat",
                "input_tokens": message.usage.input_tokens,
                "output_tokens": message.usage.output_tokens,
                "estimated_tokens": None, "model": self.api_model,
                "latency_ms": elapsed_ms,
                "ttft_ms": None, "quality_gate": False,
                "is_fallback": True, "error": None,
            }
            self.logger.info(f"✅ Claude API responded in {elapsed_ms / 1000:.1f}s "
                           f"(tokens: {message.usage.input_tokens}+{message.usage.output_tokens}, "
                           f"total API calls this session: {self.api_call_count})")
            return response

        except ImportError:
            self.logger.error("anthropic package not installed")
            self.last_call_info = {
                "provider": "claude", "method": "chat",
                "input_tokens": None, "output_tokens": None,
                "estimated_tokens": None, "model": self.api_model,
                "latency_ms": (time.time() - start) * 1000,
                "ttft_ms": None, "quality_gate": False,
                "is_fallback": True, "error": "anthropic_not_installed",
            }
            return ""
        except Exception as e:
            self.logger.error(f"Claude API call failed: {e}")
            self.last_call_info = {
                "provider": "claude", "method": "chat",
                "input_tokens": None, "output_tokens": None,
                "estimated_tokens": None, "model": self.api_model,
                "latency_ms": (time.time() - start) * 1000,
                "ttft_ms": None, "quality_gate": False,
                "is_fallback": True, "error": str(e),
            }
            return ""

    def chat(self, user_message: str, conversation_history: str = "",
             use_api: bool = False, max_tokens: int = None,
             memory_context: str = None,
             conversation_messages: list = None,
             image_data: str = None) -> str:
        """
        Generate chat response with smart local-first fallback.

        Flow: local Qwen → quality gate → retry local → quality gate → Claude API

        Args:
            user_message: Current user message
            conversation_history: Previous conversation (formatted)
            use_api: Whether to force Claude API
            max_tokens: Maximum tokens to generate (auto-estimated from query if None)
            memory_context: Optional proactive memory context to inject into system prompt
            conversation_messages: Pre-built message list (bypasses string parsing)
            image_data: Optional base64-encoded image for multimodal queries

        Returns:
            Assistant response
        """
        if max_tokens is None:
            max_tokens = self._estimate_max_tokens(user_message)
        # If explicitly requesting API, go straight there
        if use_api:
            return self._generate_api_chat(user_message, conversation_history,
                                           max_tokens, conversation_messages)

        # --- Attempt 1: Local Qwen ---
        # When image_data is present, use streaming path (supports multimodal
        # messages natively) and collect the full response for quality gating.
        if image_data:
            tokens = []
            for token in self.stream(user_message, conversation_history,
                                     max_tokens, memory_context,
                                     conversation_messages,
                                     image_data=image_data):
                tokens.append(token)
            response = "".join(tokens)
            if response:
                return self.strip_filler(response)
            # Fall through to API fallback below if empty
            if self.fallback_enabled:
                return self._generate_api_chat(user_message, conversation_history,
                                               max_tokens, conversation_messages)
            return ""

        prompt = self._build_chat_prompt(user_message, conversation_history,
                                         memory_context=memory_context)
        start = time.time()
        response = self._generate_local(prompt, max_tokens)
        elapsed_ms = (time.time() - start) * 1000

        quality_issue = self._check_response_quality(response, user_message)
        self.logger.debug("Quality gate: issue=%s response=%.80s",
                          quality_issue or "ok", response or "(empty)")
        if not quality_issue:
            # Overlay chat-level metadata onto _generate_local's last_call_info
            if self.last_call_info:
                self.last_call_info["method"] = "chat"
            self.logger.debug(f"Local LLM responded in {elapsed_ms:.0f}ms")
            return response

        self.logger.warning(f"Local LLM quality issue ({quality_issue}): '{response[:80]}' — retrying")

        # --- Attempt 2: Retry local with a nudge ---
        retry_system = self._build_system_prompt()
        if memory_context:
            retry_system += f"\n\n{memory_context}"
        nudge = (
            f"<|im_start|>system\n{retry_system}<|im_end|>\n"
            f"<|im_start|>user\n{user_message}\n\n"
            f"Please provide a direct, helpful answer.<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
        start = time.time()
        response = self._generate_local(nudge, max_tokens)
        elapsed_ms = (time.time() - start) * 1000

        quality_issue = self._check_response_quality(response, user_message)
        if not quality_issue:
            # Overlay: retry succeeded, mark quality_gate
            if self.last_call_info:
                self.last_call_info["method"] = "chat"
                self.last_call_info["quality_gate"] = True
            self.logger.info(f"Local LLM succeeded on retry in {elapsed_ms:.0f}ms")
            return response

        self.logger.warning(f"Local LLM failed twice ({quality_issue}) — falling back to Claude API")

        # --- Attempt 3: Claude API (last resort) ---
        if not self.fallback_enabled:
            self.logger.warning("API fallback disabled, returning best local attempt")
            return response if response else ""

        api_response = self._generate_api_chat(user_message, conversation_history,
                                                max_tokens, conversation_messages)
        if api_response:
            # _generate_api_chat already sets is_fallback=True; overlay quality_gate
            if self.last_call_info:
                self.last_call_info["quality_gate"] = True
            return api_response

        # Everything failed — return whatever local gave us
        self.logger.error("All LLM attempts failed")
        return response if response else ""

    def stream(self, user_message: str, conversation_history: str = "",
               max_tokens: int = None, memory_context: str = None,
               conversation_messages: list = None,
               image_data: str = None) -> Iterator[str]:
        """Stream tokens from the local LLM as they're generated.

        Uses the llama.cpp /v1/chat/completions endpoint with SSE streaming.
        Yields individual tokens as they arrive.

        Args:
            user_message: Current user message
            conversation_history: Previous conversation (ChatML-formatted)
            max_tokens: Maximum tokens to generate (auto-estimated from query if None)
            memory_context: Optional proactive memory context to inject into system prompt
            conversation_messages: Pre-built message list (bypasses string parsing)
            image_data: Optional base64-encoded image for multimodal queries

        Yields:
            Individual tokens as strings
        """
        if max_tokens is None:
            max_tokens = self._estimate_max_tokens(user_message)
        system_prompt = self._build_system_prompt()
        if memory_context:
            system_prompt += f"\n\n{memory_context}"
        messages = [{"role": "system", "content": system_prompt}]

        # Build conversation messages — prefer pre-built list over string parsing
        if conversation_messages:
            messages.extend(conversation_messages)
        elif conversation_history:
            messages.extend(self._parse_history_string(conversation_history))

        # Ensure current message is included (with optional image)
        if not messages or messages[-1].get("content") != user_message:
            messages.append(self._build_user_message(user_message, image_data))

        model_name = Path(self.local_model_path).stem if self.local_model_path else "unknown"
        start = time.time()
        first_token_time = None
        total_chars = 0
        stream_error = None
        try:
            response = requests.post(
                "http://127.0.0.1:8080/v1/chat/completions",
                json={
                    "messages": messages,
                    "temperature": self.temperature,
                    "top_p": self.top_p,
                    "top_k": self.top_k,
                    "max_tokens": max_tokens,
                    "stream": True,
                },
                timeout=30,
                stream=True,
            )

            # Handle context overflow — trim oldest context and retry once
            if response.status_code == 400:
                try:
                    err = response.json().get("error", {})
                except Exception:
                    err = {}
                if err.get("type") == "exceed_context_size_error":
                    n_ctx = err.get("n_ctx", "?")
                    n_prompt = err.get("n_prompt_tokens", "?")
                    self.logger.warning(
                        f"Context overflow ({n_prompt}/{n_ctx} tokens, "
                        f"{len(messages)} msgs) — trimming and retrying"
                    )
                    # Keep system prompt (idx 0) + last 6 conversation messages
                    if len(messages) > 7:
                        messages = [messages[0]] + messages[-6:]
                    response = requests.post(
                        "http://127.0.0.1:8080/v1/chat/completions",
                        json={
                            "messages": messages,
                            "temperature": self.temperature,
                            "top_p": self.top_p,
                            "top_k": self.top_k,
                            "max_tokens": max_tokens,
                            "stream": True,
                        },
                        timeout=30,
                        stream=True,
                    )
                else:
                    self.logger.error(f"LLM server rejected request: {err}")
                    stream_error = "context_overflow"
                    return

            response.raise_for_status()

            for line in response.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8")
                if line.startswith("data: "):
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                        token = delta.get("content", "")
                        if token:
                            if first_token_time is None:
                                first_token_time = time.time()
                            total_chars += len(token)
                            yield token
                    except (json.JSONDecodeError, KeyError, IndexError) as e:
                        self.logger.debug(f"Skipping malformed SSE chunk: {e}")
                        continue

        except Exception as e:
            stream_error = str(e)
            self.logger.error(f"LLM streaming error: {e}")
        finally:
            self.last_call_info = {
                "provider": "qwen", "method": "stream",
                "input_tokens": None, "output_tokens": None,
                "estimated_tokens": total_chars // 4 if total_chars else None,
                "model": model_name,
                "latency_ms": (time.time() - start) * 1000,
                "ttft_ms": ((first_token_time - start) * 1000) if first_token_time else None,
                "quality_gate": False, "is_fallback": False,
                "error": stream_error,
            }

    def stream_with_tools(self, user_message: str, conversation_history: str = "",
                          max_tokens: int = None, memory_context: str = None,
                          conversation_messages: list = None,
                          raw_command: str = None,
                          tools: list = None,
                          tool_temperature: float = None,
                          tool_presence_penalty: float = None,
                          image_data: str = None,
                          force_web_search: bool = False,
                          ) -> Iterator[Union[str, ToolCallRequest]]:
        """Stream tokens from the local LLM with tool calling support.

        Like stream(), but passes tool definitions to the server. If the LLM
        decides to call a tool, yields a ToolCallRequest instead of text tokens.
        The caller should then execute the tool and call continue_after_tool_call().

        Args:
            tools: List of OpenAI-compatible tool dicts. Defaults to [WEB_SEARCH_TOOL].
                   When skill tools are included, system prompt adapts automatically.
            tool_temperature: Override temperature for tool selection phase.
                              Use lower values (0.0-0.3) for more deterministic
                              tool selection. Defaults to self.temperature.
            tool_presence_penalty: Presence penalty for tool-calling requests.
                                   Qwen3.5 recommends 1.5 for tool calling.
            raw_command: Reserved (unused since tool_choice=auto).

        Yields:
            str tokens for regular text, or a single ToolCallRequest.
        """
        if not self.tool_calling:
            yield from self.stream(user_message, conversation_history,
                                   max_tokens, memory_context, conversation_messages)
            return

        # Default to web search only (backward compatible)
        if tools is None:
            tools = [WEB_SEARCH_TOOL]

        # Sampling parameters for tool selection phase
        temp = tool_temperature if tool_temperature is not None else self.temperature
        pp = tool_presence_penalty  # None means omit from payload

        if max_tokens is None:
            max_tokens = self._estimate_max_tokens(user_message)
        system_prompt = self._build_system_prompt()

        # Determine which tool names are present to customize the prompt
        tool_names = {t["function"]["name"] for t in tools}
        has_skill_tools = bool(tool_names - {"web_search"})

        now = datetime.now()
        today = now.strftime("%B %d, %Y")
        current_time = now.strftime("%I:%M %p").lstrip("0")

        if has_skill_tools:
            # --- Multi-tool prompt (LLM-centric migration) ---
            # Rules auto-assembled from core/tools/*.py definitions.
            # Only includes rules for tools in the pruned active set.
            rules_text = build_tool_prompt_rules(tool_names)
            system_prompt += (
                f"\n\nToday's date is {today}. Current time: {current_time}."
                "\nFor time or date questions, answer directly from the above — do NOT search.\n\n"
                + rules_text
            )
        else:
            # --- Web-search-only prompt ---
            # Balanced: search for current data, answer knowledge from training.
            system_prompt += (
                f"\n\nToday's date is {today}. Current time: {current_time}.\n\n"
                "You have one tool: web_search. Use it ONLY for current or "
                "real-time information:\n"
                "- Breaking news, live scores, stock prices, current events\n"
                "- Recent product releases, event dates, ticket prices\n"
                "- Travel times, local businesses, recent statistics\n"
                "- Anything that changes frequently or happened recently\n"
                "- Rankings, ratings, reviews, or detailed lists of specific "
                "movies, shows, people, or franchises\n\n"
                "ANSWER DIRECTLY (no search) for:\n"
                "- General knowledge (definitions, how things work, history, "
                "science, concepts)\n"
                "- Follow-ups to your previous answers ('tell me more', "
                "'explain that', 'is that normal?', 'elaborate')\n"
                "- Creative requests (jokes, stories, poems)\n"
                "- Math, coding help, explanations, comparisons\n"
                "- Greetings and small talk\n"
                "- Time or date questions — use the date/time above\n\n"
                "When you DO search, strip conversational filler from the query. "
                "Example: 'Can you find me a good pizza recipe?' "
                "→ query: 'best homemade pizza recipe'.\n"
                "NEVER tell the user to look something up themselves — "
                "either search or answer from your knowledge."
            )

        if memory_context:
            system_prompt += f"\n\n{memory_context}"

            # Prescriptive rule: tell LLM to use user facts for web search queries
            tool_names = {t.get("function", {}).get("name") for t in tools} if tools else set()
            if "web_search" in tool_names:
                system_prompt += (
                    "\n\nIMPORTANT: When building web_search queries, USE the user's "
                    "personal details (location, workplace, interests) from the context "
                    "above to make searches more specific. Example: 'best coffee near me' "
                    "+ user lives in Nashville → search 'best coffee Nashville'."
                )

        messages = [{"role": "system", "content": system_prompt}]

        # Do NOT include conversation history for tool calling.
        # History in the messages array makes Qwen over-eager to search
        # (even general knowledge questions trigger web_search).
        # Follow-up context is handled upstream: the router injects the
        # prior exchange into user_message for follow-up queries.

        if not messages or messages[-1].get("content") != user_message:
            messages.append(self._build_user_message(user_message, image_data))

        # 2-message constraint: enforce structurally, not by convention.
        # History in messages causes "pattern addiction" (JetBrains Koog).
        # Context is injected via XML tags in user_message by the router.
        assert len(messages) == 2, (
            f"Tool-calling messages must be exactly [system, user], got {len(messages)}"
        )

        from core.debug_logger import get_debug_logger
        _dbg = get_debug_logger()
        _dbg.log_llm_messages(messages, tool_count=len(tools),
                               temperature=temp, presence_penalty=pp,
                               label="stream_with_tools")

        # Store messages for continue_after_tool_call()
        self._tool_call_messages = messages
        # Also store tools for continue_after_tool_call() context overflow retry
        self._tool_call_tools = tools

        # Forced web_search: for entertainment listing/ranking queries, skip
        # LLM tool selection and yield a ToolCallRequest directly.  The LLM
        # tends to answer ranking follow-ups from context rather than searching,
        # producing truncated or hallucinated lists.
        if force_web_search and "web_search" in tool_names:
            import re as _re
            # Strip <prior_context>...</prior_context> and "Now the user asks:"
            # to get the clean query for web search.
            _clean = _re.sub(
                r'<prior_context>.*?</prior_context>\s*(?:Now the user asks:\s*)?',
                '', user_message, flags=_re.DOTALL).strip()
            self.logger.info("force_web_search: bypassing LLM tool selection, query='%s'", _clean)
            yield ToolCallRequest(
                name="web_search",
                arguments={"query": _clean},
                call_id="forced_search_0",
            )
            return

        # Let Qwen decide when to use tools via the prescriptive system prompt.
        # tool_choice=auto always — never "required" (causes infinite loops).
        tool_choice = "auto"

        self.logger.info(
            f"stream_with_tools: {len(messages)} msgs, {len(tools)} tools "
            f"({', '.join(tool_names)}), temp={temp}, pp={pp}"
        )
        self.logger.debug("stream_with_tools: image_data=%s%s",
                          "yes" if image_data else "no",
                          f" ({len(image_data)//1024}KB b64)" if image_data else "")

        # Build the request payload
        payload = {
            "messages": messages,
            "temperature": temp,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "max_tokens": max_tokens,
            "stream": True,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        if pp is not None:
            payload["presence_penalty"] = pp

        import json as _json
        _payload_size = len(_json.dumps(payload, default=str))
        self.logger.debug("stream_with_tools: payload %d bytes, %d messages",
                          _payload_size, len(messages))

        model_name = Path(self.local_model_path).stem if self.local_model_path else "unknown"
        start = time.time()
        first_token_time = None
        total_chars = 0
        stream_error = None
        try:
            response = requests.post(
                "http://127.0.0.1:8080/v1/chat/completions",
                json=payload,
                timeout=30,
                stream=True,
            )

            if response.status_code == 400:
                try:
                    err = response.json().get("error", {})
                except Exception:
                    err = {}
                if err.get("type") == "exceed_context_size_error":
                    n_ctx = err.get("n_ctx", "?")
                    n_prompt = err.get("n_prompt_tokens", "?")
                    self.logger.warning(
                        f"Context overflow ({n_prompt}/{n_ctx} tokens) — trimming"
                    )
                    if len(messages) > 7:
                        messages = [messages[0]] + messages[-6:]
                        self._tool_call_messages = messages
                    payload["messages"] = messages
                    response = requests.post(
                        "http://127.0.0.1:8080/v1/chat/completions",
                        json=payload,
                        timeout=30,
                        stream=True,
                    )
                else:
                    self.logger.error(f"LLM server rejected request: {err}")
                    stream_error = "context_overflow"
                    return

            response.raise_for_status()
            self.logger.debug("stream_with_tools: HTTP %d", response.status_code)

            # Accumulate tool call fragments
            tool_call_id = ""
            tool_call_name = ""
            tool_call_args = ""
            is_tool_call = False

            for line in response.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8")
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break

                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0].get("delta", {})
                    finish_reason = chunk["choices"][0].get("finish_reason")

                    # Check for tool call fragments
                    tool_calls = delta.get("tool_calls")
                    if tool_calls:
                        is_tool_call = True
                        if first_token_time is None:
                            first_token_time = time.time()
                        tc = tool_calls[0]
                        if tc.get("id"):
                            tool_call_id = tc["id"]
                        func = tc.get("function", {})
                        if func.get("name"):
                            tool_call_name = func["name"]
                        if func.get("arguments"):
                            tool_call_args += func["arguments"]
                        continue

                    # Regular text token
                    token = delta.get("content", "")
                    if token:
                        if first_token_time is None:
                            first_token_time = time.time()
                        total_chars += len(token)
                        yield token

                    # Finish with tool calls
                    if finish_reason == "tool_calls" and is_tool_call:
                        try:
                            args = json.loads(tool_call_args) if tool_call_args else {}
                        except json.JSONDecodeError:
                            args = {"query": tool_call_args}
                        self.logger.info(
                            f"Tool call: {tool_call_name}({args})"
                        )
                        yield ToolCallRequest(
                            name=tool_call_name,
                            arguments=args,
                            call_id=tool_call_id,
                        )
                        return

                except (json.JSONDecodeError, KeyError, IndexError) as e:
                    self.logger.debug(f"Skipping malformed SSE chunk: {e}")
                    continue

            # If we accumulated tool call fragments but no finish_reason
            if is_tool_call and tool_call_name:
                try:
                    args = json.loads(tool_call_args) if tool_call_args else {}
                except json.JSONDecodeError:
                    args = {"query": tool_call_args}
                self.logger.info(f"Tool call (no finish_reason): {tool_call_name}({args})")
                yield ToolCallRequest(
                    name=tool_call_name,
                    arguments=args,
                    call_id=tool_call_id,
                )

        except Exception as e:
            stream_error = str(e)
            self.logger.error(f"LLM streaming (tool) error: {e}")
        finally:
            self.last_call_info = {
                "provider": "qwen", "method": "stream_with_tools",
                "input_tokens": None, "output_tokens": None,
                "estimated_tokens": total_chars // 4 if total_chars else None,
                "model": model_name,
                "latency_ms": (time.time() - start) * 1000,
                "ttft_ms": ((first_token_time - start) * 1000) if first_token_time else None,
                "quality_gate": False, "is_fallback": False,
                "error": stream_error,
            }

    # ── Domain-specific synthesis rules ──────────────────────────
    # Each block returns the RULES section for a domain.  Common header
    # (date/time/location/chaining) and footer (honorific/no-filler) are
    # applied by continue_after_tool_call(); these are just the middle.

    @staticmethod
    def _get_domain_rules(category: str | None) -> str:
        """Return the domain-specific rules block for synthesis prompts."""
        if category == "entertainment":
            return (
                "DOMAIN: ENTERTAINMENT — Movies, TV, streaming, actors, directors.\n"
                "RULES — follow these EXACTLY:\n"
                "1. ONLY state facts that appear in the search results above. "
                "For each movie, show, or person you mention, it MUST be named in the search results. "
                "If the search results list 4 items and the user asked for 5, give the 4 you have "
                "and say you'd need to search further for the rest.\n"
                "2. NEVER FABRICATE any of the following — if the search results do not contain it, DO NOT STATE IT:\n"
                "- Movie or show titles\n"
                "- Release years or dates\n"
                "- Box office numbers or budget figures\n"
                "- Cast or crew names\n"
                "- Ratings, scores, or review counts\n"
                "- Plot details, character names, or franchise timelines\n"
                "- Award wins or nominations\n"
                "3. When listing movies/shows, include ONLY entries grounded in the search results. "
                "A shorter, accurate list is ALWAYS better than a padded list with invented entries. "
                "If a title sounds familiar from training data but is NOT in the search results, "
                "you MAY include it with an explicit hedge: 'I believe' or 'if I recall correctly'. "
                "Limit hedged entries to at most 2.\n"
                "4. For franchise/sequel questions: state only what is confirmed in the search results. "
                "Do NOT speculate about upcoming releases, rumored projects, or unconfirmed sequels "
                "unless the search results explicitly mention them.\n"
                "5. Present information naturally — DO NOT reference 'search results' or 'based on my search'. "
                "Just state the facts as if you know them.\n"
            )
        if category == "veterinary":
            return (
                "DOMAIN: VETERINARY / PET & ANIMAL HEALTH\n"
                "RULES — follow these EXACTLY:\n"
                "1. NEVER fabricate medication dosages, treatment protocols, or breed-specific "
                "health information for animals. Animal dosages differ dramatically from human "
                "dosages — an incorrect dose can be fatal.\n"
                "2. Only state veterinary information that appears in the search results above. "
                "If the search results are incomplete, say so rather than filling gaps from "
                "training data.\n"
                "3. For medication questions: NEVER state a specific dosage. Instead, say the "
                "medication exists for the condition and recommend consulting their vet for "
                "proper dosing.\n"
                "4. Distinguish between emergency symptoms (bloat, seizures, poisoning, "
                "difficulty breathing) and routine concerns. For anything that sounds like an "
                "emergency, lead with 'If this is happening now, contact an emergency vet "
                "immediately' before providing any other information.\n"
                "5. Present information naturally — DO NOT reference 'search results' or "
                "'based on my search'.\n"
                "6. ALWAYS end your response with: 'This is general information — please "
                "consult your veterinarian for advice specific to your pet.'\n"
            )
        if category == "medical":
            from core import persona
            disclaimer = persona.domain_disclaimer("medical")
            return (
                "DOMAIN: MEDICAL / HEALTH\n"
                "RULES — follow these EXACTLY:\n"
                "1. NEVER fabricate drug names, dosages, interactions, side effects, or "
                "treatment protocols. Only state medical information that appears in the "
                "search results above.\n"
                "2. Distinguish between established medical consensus and emerging or "
                "preliminary research. If a treatment is experimental or controversial, "
                "say so explicitly.\n"
                "3. Never recommend specific dosages or treatment plans. You may relay what "
                "search results say, but frame it as general information rather than personal "
                "recommendation.\n"
                "4. For drug interactions or contraindications: only state what is explicitly "
                "in the search results. If the results don't cover interactions, say "
                "'I'd recommend checking with your pharmacist or doctor about potential "
                "interactions.'\n"
                "5. Present information naturally — DO NOT reference 'search results' or "
                "'based on my search'.\n"
                f"6. ALWAYS end your response with: '{disclaimer}'\n"
            )
        if category == "nutrition":
            return (
                "DOMAIN: NUTRITION / DIETARY INFORMATION\n"
                "RULES — follow these EXACTLY:\n"
                "1. NEVER fabricate calorie counts, macronutrient breakdowns, serving sizes, "
                "or daily recommended values. Only state nutritional data that appears in the "
                "search results above.\n"
                "2. Nutritional values vary by brand, preparation method, and serving size. "
                "If the search results specify a source (USDA, a specific brand), note it. "
                "If not, state that values are approximate.\n"
                "3. NEVER claim a food 'cures', 'prevents', or 'treats' any disease or condition "
                "unless the search results cite established medical consensus or FDA-approved claims.\n"
                "4. For diet plans (keto, paleo, etc.): present factual descriptions of the diet. "
                "Do NOT make health outcome claims beyond what the search results support.\n"
                "5. Present information naturally — DO NOT reference 'search results' or "
                "'based on my search'.\n"
                "6. ALWAYS end your response with: 'Nutritional values are approximate and may "
                "vary. For personalized dietary advice, consider consulting a registered dietitian.'\n"
            )
        if category == "finance":
            return (
                "DOMAIN: FINANCE / MARKETS\n"
                "RULES — follow these EXACTLY:\n"
                "1. NEVER fabricate stock prices, market returns, interest rates, or "
                "financial statistics. Only state financial figures that appear in the "
                "search results above.\n"
                "2. For market data: always note the date or time of the data if available "
                "in search results. Market data becomes stale quickly — flag if the data "
                "might not be current.\n"
                "3. Distinguish between factual reporting (company earnings, index levels) "
                "and speculation or predictions. Never present analyst predictions as "
                "certainties.\n"
                "4. For crypto, penny stocks, or speculative assets: exercise extra caution. "
                "Never characterize any investment as 'safe' or 'guaranteed'.\n"
                "5. Present information naturally — DO NOT reference 'search results' or "
                "'based on my search'.\n"
                "6. ALWAYS end your response with: 'This is general information, not "
                "financial advice. Consider consulting a financial advisor.'\n"
            )
        if category == "legal":
            from core import persona
            disclaimer = persona.domain_disclaimer("legal")
            return (
                "DOMAIN: LEGAL\n"
                "RULES — follow these EXACTLY:\n"
                "1. NEVER fabricate case names, case citations, statute numbers, legal standards, "
                "or court rulings. Legal hallucinations are the MOST dangerous — lawyers have been "
                "sanctioned for citing fabricated cases. Only state legal information that appears "
                "in the search results above.\n"
                "2. Laws vary dramatically by jurisdiction (federal, state, local, international). "
                "ALWAYS note the jurisdiction if available in search results. If the search results "
                "do not specify jurisdiction, say so explicitly.\n"
                "3. Distinguish between current law and proposed legislation. If a bill has been "
                "introduced but not passed, say so. Do NOT present pending legislation as current law.\n"
                "4. For legal rights questions: provide general information about the legal concept "
                "but explicitly state that specific rights depend on jurisdiction and circumstances.\n"
                "5. Present information naturally — DO NOT reference 'search results' or "
                "'based on my search'.\n"
                f"6. ALWAYS end your response with: '{disclaimer}'\n"
            )
        if category == "gaming":
            return (
                "DOMAIN: GAMING — Video games, platforms, reviews, esports.\n"
                "RULES — follow these EXACTLY:\n"
                "1. NEVER fabricate game titles, release dates, review scores, "
                "developer/publisher names, platform availability, or pricing. "
                "Only state what appears in the search results above.\n"
                "2. Distinguish between released games, announced games, and "
                "rumored/leaked games. Check dates against today's date — if a game "
                "hasn't released yet, say so.\n"
                "3. For review scores: specify the source (Metacritic, IGN, etc.) and "
                "distinguish critic scores from user scores when both are available.\n"
                "4. For franchise/sequel questions: state only confirmed entries. "
                "Do NOT speculate about unannounced sequels or DLC.\n"
                "5. Present information naturally — DO NOT reference 'search results' or "
                "'based on my search'.\n"
            )
        if category == "sports":
            return (
                "DOMAIN: SPORTS\n"
                "RULES — follow these EXACTLY:\n"
                "1. NEVER fabricate scores, records, statistics, dates, or player "
                "information. Only state sports facts that appear in the search results "
                "above.\n"
                "2. For historical stats and records: state them confidently if they appear "
                "in search results. If relying on training knowledge for well-known facts "
                "(Super Bowl winners, World Series champions), use slight hedging: "
                "'I believe' or 'if I recall correctly'.\n"
                "3. Distinguish between completed events and upcoming/scheduled events. "
                "Check dates against today's date — never report a future game's score.\n"
                "4. For live or recent games: note that scores are as of the search "
                "results' retrieval time and may not reflect the final outcome if a game "
                "is in progress.\n"
                "5. Present information naturally — DO NOT reference 'search results' or "
                "'based on my search'.\n"
            )
        if category == "automotive":
            return (
                "DOMAIN: AUTOMOTIVE\n"
                "RULES — follow these EXACTLY:\n"
                "1. NEVER fabricate vehicle specifications, prices, model years, "
                "horsepower figures, or safety ratings. Only state automotive facts that "
                "appear in the search results above.\n"
                "2. For pricing: always note that prices vary by trim, options, region, "
                "and dealer. Never present a single price as definitive unless the search "
                "results specify it as MSRP.\n"
                "3. For safety recalls: accuracy is critical. Only relay recall "
                "information that is explicitly in the search results. Include recall "
                "numbers if available.\n"
                "4. Distinguish between confirmed models/specs and rumors or leaks about "
                "upcoming vehicles.\n"
                "5. Present information naturally — DO NOT reference 'search results' or "
                "'based on my search'.\n"
            )
        if category == "real_estate":
            return (
                "DOMAIN: REAL ESTATE / HOUSING\n"
                "RULES — follow these EXACTLY:\n"
                "1. NEVER fabricate property values, median home prices, zoning classifications, "
                "HOA fees, or tax assessments. Real estate data is hyper-local and changes "
                "frequently — only state what appears in the search results above.\n"
                "2. For home values: always note that values are estimates and vary by condition, "
                "lot size, improvements, and market timing. Never present an online estimate as "
                "a definitive appraisal.\n"
                "3. For mortgage calculations: note that actual rates depend on credit score, "
                "down payment, loan type, and lender. Provide search-result figures as examples, "
                "not guarantees.\n"
                "4. For zoning or legal questions: note the jurisdiction and that zoning laws "
                "vary by municipality. Recommend checking with the local planning office for "
                "definitive answers.\n"
                "5. Present information naturally — DO NOT reference 'search results' or "
                "'based on my search'.\n"
            )
        if category == "programming":
            return (
                "DOMAIN: PROGRAMMING / CODING\n"
                "RULES — follow these EXACTLY:\n"
                "1. NEVER fabricate API names, function signatures, library versions, or "
                "configuration options. If you're unsure about exact syntax, say so "
                "rather than guessing.\n"
                "2. When referencing specific library versions or features: note the "
                "version if available in search results. APIs change between versions — "
                "accuracy matters.\n"
                "3. Prefer showing working code examples over describing concepts "
                "abstractly. Keep examples minimal and focused on the question asked.\n"
                "4. If the search results contain code snippets or documentation, use "
                "those as the basis for your answer rather than generating from training "
                "data.\n"
                "5. Present information naturally — DO NOT reference 'search results' or "
                "'based on my search'.\n"
            )
        if category == "science_tech":
            return (
                "DOMAIN: SCIENCE & TECHNOLOGY\n"
                "RULES — follow these EXACTLY:\n"
                "1. Distinguish between established scientific consensus and emerging or "
                "preliminary findings. If citing a study, note whether it's peer-reviewed, "
                "a preprint, or preliminary.\n"
                "2. NEVER fabricate research paper titles, author names, journal names, or "
                "specific statistics. Only cite what appears in the search results above.\n"
                "3. For technology topics: be precise about version numbers, release dates, "
                "and specifications. If the search results contain these, state them. "
                "If not, don't invent them.\n"
                "4. Avoid overstating implications — 'a study found X' is better than "
                "'science proves X'. Single studies do not prove anything; use appropriate "
                "framing.\n"
                "5. Present information naturally — DO NOT reference 'search results' or "
                "'based on my search'.\n"
            )
        if category == "history":
            return (
                "DOMAIN: HISTORY\n"
                "RULES — follow these EXACTLY:\n"
                "1. NEVER fabricate dates, battle casualties, treaty terms, or direct quotes "
                "attributed to historical figures. Only state historical facts that appear in "
                "the search results above or that you are confident about from well-established "
                "historical record.\n"
                "2. For well-known historical facts (major wars, independence dates, famous leaders): "
                "you may state them confidently even without search results. For obscure or disputed "
                "facts, rely on search results and hedge if uncertain.\n"
                "3. Distinguish between historical consensus and contested interpretations. "
                "If historians disagree on causes, motivations, or significance, present multiple "
                "perspectives rather than one as definitive.\n"
                "4. NEVER fabricate direct quotes. If a quote is famous and well-established "
                "('I came, I saw, I conquered'), you may use it. For anything less certain, "
                "either cite the search results or omit it.\n"
                "5. Present information naturally — DO NOT reference 'search results' or "
                "'based on my search'.\n"
            )
        if category == "travel":
            return (
                "DOMAIN: TRAVEL / DESTINATIONS\n"
                "RULES — follow these EXACTLY:\n"
                "1. NEVER fabricate specific business names, addresses, phone numbers, hours of "
                "operation, or prices for hotels, restaurants, or attractions. These change "
                "constantly — only state what appears in the search results above.\n"
                "2. For 'best restaurant/hotel in X' queries: ONLY recommend places that appear "
                "in the search results. If the results list 3 options and the user asked for 5, "
                "give the 3 you have and say you'd need to search further for more.\n"
                "3. For visa and entry requirements: note that these change frequently and vary "
                "by nationality. Always recommend checking the official government or embassy "
                "website for current requirements.\n"
                "4. For flight and hotel pricing: note that prices are as of the search results "
                "and fluctuate. Do NOT present a price as guaranteed.\n"
                "5. Present information naturally — DO NOT reference 'search results' or "
                "'based on my search'.\n"
            )
        # Default / generic rules — covers math, factual, geo, and unclassified
        return (
            "RULES — follow these EXACTLY:\n"
            "1. READBACK DECISION — follow this in order:\n"
            "(a) Check your response. Does it ALREADY list the specific items, steps, prices, names, "
            "or details the user asked for? If YES — STOP. Do not offer a readthrough. "
            "You already provided the information.\n"
            "(b) Does the content require a WALKTHROUGH to be useful — step-by-step cooking instructions, "
            "assembly procedures, installation guides? If YES — give a 1-2 sentence summary "
            "(count of steps, source) and offer a readthrough. Do NOT list any items.\n"
            "(c) For everything else — product comparisons, recommendations, rankings, factual answers, "
            "code, travel, general knowledge — just answer completely and stop. Never offer a readthrough.\n"
            "2. YOU MUST give a direct answer. "
            "YOU MUST include specific details like scores, dates, and numbers when available. "
            "YOU MUST present information as though you simply know it — DO NOT reference "
            "'search results', 'based on the search results', or any variation.\n"
            "3. YOU MUST maintain strict political neutrality — present facts objectively. "
            "DO NOT add editorial bias, emphasis on controversies, or opinionated framing.\n"
            "4. YOU MUST compare any event dates in the results against today's date. "
            "If an event is scheduled for a FUTURE date, YOU MUST clearly state it hasn't "
            "happened yet. DO NOT report predictions, odds, or speculation as fact.\n"
            "5. GROUNDING — For ANY claim involving a specific title, date, name, or number:\n"
            "(a) If the search results above contain it — state it confidently.\n"
            "(b) If the search results do NOT contain it but you have strong training knowledge "
            "— use hedging like 'I believe' or 'from what I recall' to signal uncertainty.\n"
            "(c) If you cannot verify it from search results AND you are unsure — OMIT it entirely. "
            "List only what you can ground. It is ALWAYS better to give a shorter, accurate answer "
            "than a longer one padded with fabricated details.\n"
            "NEVER invent movie titles, release dates, cast members, scores, or statistics. "
            "If you only know some items in a list, give those and say the rest would need "
            "a dedicated search.\n"
        )

    def continue_after_tool_call(self, tool_call: ToolCallRequest,
                                  tool_result: str,
                                  max_tokens: int = 400,
                                  tools: list | None = None,
                                  image_data: str | None = None,
                                  synthesis_temperature: float | None = None,
                                  synthesis_category: str | None = None) -> Iterator[str]:
        """Continue LLM generation after a tool call completes.

        Sends the tool result back to the LLM and streams its synthesized answer.
        If tools are provided and the LLM requests another tool call, yields a
        ToolCallRequest instead of text tokens.

        Args:
            tool_call: The ToolCallRequest that was executed
            tool_result: Formatted string of tool results
            max_tokens: Max tokens for the synthesized response
            tools: Optional tool schemas — if provided, LLM can call another tool
            synthesis_temperature: Override temperature for synthesis (lower = more factual)

        Yields:
            Text tokens of the synthesized answer, or a ToolCallRequest
        """
        self.logger.debug(
            "continue_after_tool_call: tool=%s result_len=%d image=%s%s",
            tool_call.name, len(tool_result) if tool_result else 0,
            "yes" if image_data else "no",
            f" ({len(image_data)//1024}KB b64)" if image_data else "")
        messages = list(getattr(self, '_tool_call_messages', []))

        # Add the assistant's tool call message
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": tool_call.call_id or "call_0",
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": json.dumps(tool_call.arguments),
                }
            }]
        })

        # Add the tool result
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.call_id or "call_0",
            "content": tool_result,
        })

        # Snapshot messages BEFORE the synthesis prompt so that chained
        # tool calls don't carry forward duplicate intermediate prompts.
        # The synthesis user-message is ephemeral — needed only for THIS
        # LLM call, not for subsequent chain steps.
        self._tool_call_messages = list(messages)

        # Synthesis instruction — tell Qwen to give a direct answer.
        # Don't lead with the honorific since the ack phrase already used it.
        # Anti-hallucination is safe HERE (synthesis) — it only suppresses
        # tool calling when placed in the system prompt for stream_with_tools().
        now = datetime.now()
        today = now.strftime("%B %d, %Y")
        current_time = now.strftime("%I:%M %p").lstrip("0")
        h = get_honorific()
        formal = get_formal_address()
        if formal:
            honorific_rule = (
                f"The user is {formal}. If your response is a greeting or farewell, "
                f"YOU MUST use '{formal}'. For mid-conversation replies, YOU MUST use '{h}'. "
                f"YOU MUST check your previous response — if you used '{formal}' last time, use '{h}' this time, and vice versa."
            )
        else:
            honorific_rule = f"YOU MUST address the user as '{h}' naturally in your response."
        # When tools are available, prepend a chaining instruction so the
        # LLM can call the next tool if the user's request needs multiple.
        # This is combined with the domain-specific prompt (not instead of).
        loc_hint = f"The user's home location is {self.home_location}.\n" if self.home_location else ""
        chaining_prefix = ""
        if tools:
            chaining_prefix = (
                "Check the user's ORIGINAL request. If they asked for UNRELATED things "
                "requiring DIFFERENT tools (e.g. weather AND a reminder), call the next tool NOW.\n"
                "If the search results above contain the answer, give a direct answer — do NOT "
                "search again with a rephrased query. Only search again if the results are "
                "completely irrelevant to the question asked.\n"
            )
        # ── Common header / footer ──────────────────────────────────
        synth_header = (
            f"Today's date is {today}. Current time: {current_time}.\n"
            f"{loc_hint}"
            f"{chaining_prefix}"
        )
        synth_footer = (
            f"{honorific_rule}\n"
            "DO NOT start with filler like 'Certainly', 'Of course', 'Absolutely'. "
            "Jump straight into the answer. "
            "DO NOT tell the user to check another website or look elsewhere. "
            "You ARE their source of information."
        )

        # ── Domain-specific rules ─────────────────────────────────
        domain_rules = self._get_domain_rules(synthesis_category)
        has_disclaimer = synthesis_category in ("medical", "legal") if synthesis_category else False
        if has_disclaimer:
            self.logger.debug("continue_after_tool_call: domain_disclaimer injected for %s",
                              synthesis_category)
        synthesis_text = f"{synth_header}{domain_rules}\n{synth_footer}"
        messages.append(self._build_user_message(synthesis_text, image_data))
        self.logger.debug("continue_after_tool_call: %d messages, synthesis_text_len=%d, category=%s",
                          len(messages), len(synthesis_text), synthesis_category)

        from core.debug_logger import get_debug_logger
        _dbg = get_debug_logger()
        _dbg._write("synthesis_rules", {
            "category": synthesis_category,
            "temperature": synthesis_temperature,
            "rules_len": len(domain_rules) if domain_rules else 0,
            "has_disclaimer": has_disclaimer,
            "synthesis_text_len": len(synthesis_text),
        })
        _dbg.log_llm_messages(messages, tool_count=len(tools) if tools else 0,
                               label="continue_after_tool_call",
                               synthesis_category=synthesis_category)

        model_name = Path(self.local_model_path).stem if self.local_model_path else "unknown"
        start = time.time()
        first_token_time = None
        total_chars = 0
        stream_error = None

        _synth_temp = synthesis_temperature if synthesis_temperature is not None else self.temperature
        payload = {
            "messages": messages,
            "temperature": _synth_temp,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        import json as _json
        _payload_size = len(_json.dumps(payload, default=str))
        self.logger.debug("continue_after_tool_call: payload %d bytes, synth_temp=%.1f",
                          _payload_size, _synth_temp)

        # Track tool call fragments (same logic as stream_with_tools)
        is_tool_call = False
        tc_name = ""
        tc_args = ""
        tc_id = None

        # Payload-aware timeout: scale with message content length.
        # Base 30s + 1s per 1000 estimated tokens, capped at 120s.
        # Multi-source queries (3+ web searches) can accumulate 60KB+
        # of context that needs more time to process.
        _est_tokens = sum(len(str(m.get('content', ''))) for m in messages) // 4
        _timeout = min(120, 30 + (_est_tokens // 1000))
        # Multimodal requests (image_data) need longer timeout —
        # mmproj processes the image on CPU before generating tokens.
        if image_data:
            _timeout = max(_timeout, 90)

        try:
            response = requests.post(
                "http://127.0.0.1:8080/v1/chat/completions",
                json=payload,
                timeout=_timeout,
                stream=True,
            )
            response.raise_for_status()
            self.logger.debug("continue_after_tool_call: HTTP %d", response.status_code)

            for line in response.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8")
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    choice = chunk["choices"][0]
                    delta = choice.get("delta", {})
                    finish = choice.get("finish_reason")

                    # Check for tool call fragments
                    if "tool_calls" in delta:
                        is_tool_call = True
                        tc_delta = delta["tool_calls"][0]
                        fn = tc_delta.get("function", {})
                        if fn.get("name"):
                            tc_name = fn["name"]
                        if fn.get("arguments"):
                            tc_args += fn["arguments"]
                        if tc_delta.get("id"):
                            tc_id = tc_delta["id"]

                    if finish == "tool_calls" and tc_name:
                        try:
                            args = json.loads(tc_args) if tc_args else {}
                        except json.JSONDecodeError:
                            args = {"query": tc_args}
                        self.logger.info(
                            f"Chained tool call: {tc_name}({args})")
                        # _tool_call_messages already saved before synthesis prompt
                        yield ToolCallRequest(
                            name=tc_name, arguments=args, call_id=tc_id)
                        return

                    token = delta.get("content", "")
                    if token:
                        if first_token_time is None:
                            first_token_time = time.time()
                        total_chars += len(token)
                        yield token
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

            # Handle accumulated tool call without finish_reason
            if is_tool_call and tc_name:
                try:
                    args = json.loads(tc_args) if tc_args else {}
                except json.JSONDecodeError:
                    args = {"query": tc_args}
                self.logger.info(
                    f"Chained tool call (no finish): {tc_name}({args})")
                # _tool_call_messages already saved before synthesis prompt
                yield ToolCallRequest(
                    name=tc_name, arguments=args, call_id=tc_id)

        except Exception as e:
            stream_error = str(e)
            self.logger.error(f"LLM continue_after_tool_call error: {e}")
        finally:
            elapsed = (time.time() - start) * 1000
            ttft = ((first_token_time - start) * 1000) if first_token_time else None
            self.logger.debug(
                "continue_after_tool_call: done — %d chars, %.0fms%s",
                total_chars, elapsed,
                f", TTFT={ttft:.0f}ms" if ttft else ", TTFT=none (zero tokens)")
            self.last_call_info = {
                "provider": "qwen", "method": "continue_after_tool_call",
                "input_tokens": None, "output_tokens": None,
                "estimated_tokens": total_chars // 4 if total_chars else None,
                "model": model_name,
                "latency_ms": (time.time() - start) * 1000,
                "ttft_ms": ((first_token_time - start) * 1000) if first_token_time else None,
                "quality_gate": False, "is_fallback": False,
                "error": stream_error,
            }


# Convenience function
def get_llm_router(config) -> LLMRouter:
    """Get LLM router instance"""
    return LLMRouter(config)
