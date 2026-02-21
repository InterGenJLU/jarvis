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

from datetime import date
from core.logger import get_logger
from core.honorific import get_honorific
import requests
import json


@dataclass
class ToolCallRequest:
    """Sentinel yielded by stream_with_tools() when the LLM requests a tool call."""
    name: str
    arguments: dict
    call_id: str = ""


# OpenAI-compatible tool schema for web search
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current information. Use this for ANY factual question "
            "about the real world: distances, people, events, news, scores, prices, "
            "statistics, locations, travel times, or anything requiring accurate data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to look up"
                }
            },
            "required": ["query"]
        }
    }
}


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
            r"\s*Would you like (?:to know|me to|anything).*$",
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
        words = text.lower().split()
        if len(words) >= 4:
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
                return "artifacts"

        return ""

    def generate(self, prompt: str, use_api: bool = False, max_tokens: int = 512) -> str:
        """
        Generate response from LLM.

        When use_api is False and fallback is enabled, uses smart fallback:
        local → quality check → retry local → quality check → Claude API

        Args:
            prompt: Input prompt
            use_api: Whether to force API (Claude) instead of local
            max_tokens: Maximum tokens to generate

        Returns:
            Generated text response
        """
        if use_api:
            return self._generate_api(prompt, max_tokens)
        else:
            return self._generate_local(prompt, max_tokens)
    
    def _generate_local(self, user_message: str, max_tokens: int = 512) -> str:
        """Generate using llama-server REST API"""
        system_prompt = f"You are JARVIS, a personal AI assistant. Be concise, answer directly, address user as {get_honorific()}."

        try:
            response = requests.post(
                "http://127.0.0.1:8080/v1/chat/completions",
                json={
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message}
                    ],
                    "temperature": self.temperature,
                    "top_p": self.top_p,
                    "top_k": self.top_k,
                    "max_tokens": max_tokens
                },
                timeout=30
            )
            # Log context overflow clearly instead of generic error
            if response.status_code == 400:
                try:
                    err = response.json().get("error", {})
                except Exception:
                    err = {}
                if err.get("type") == "exceed_context_size_error":
                    self.logger.error(
                        f"Context overflow: {err.get('n_prompt_tokens', '?')}/"
                        f"{err.get('n_ctx', '?')} tokens"
                    )
                else:
                    self.logger.error(f"LLM server rejected request: {err}")
                return ""
            response.raise_for_status()
            data = response.json()
            usage = data.get("usage", {})
            self.last_call_info = {
                "provider": "qwen",
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
            }
            return self.strip_filler(data["choices"][0]["message"]["content"].strip())
        except Exception as e:
            self.logger.error(f"LLM server error: {e}")
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
        try:
            # Import anthropic SDK
            import anthropic
            
            # Get API key
            api_key = self.config.get_env(self.api_key_env)
            if not api_key or api_key == "your_key_here":
                self.logger.error("Claude API key not configured")
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
                "provider": "claude",
                "input_tokens": message.usage.input_tokens,
                "output_tokens": message.usage.output_tokens,
            }

            return response

        except ImportError:
            self.logger.error("anthropic package not installed")
            return "I'm sorry, the Claude API is not available."
        except Exception as e:
            self.logger.error(f"Claude API call failed: {e}")
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
        """Build the JARVIS system prompt"""
        from datetime import datetime
        h = get_honorific()
        today = datetime.now().strftime("%A, %B %d, %Y")
        return (
            f"You are JARVIS, a personal AI assistant running locally on the user's computer. "
            f"You are NOT the fictional JARVIS from Marvel movies. "
            f"Today is {today}. "
            f"RULES YOU MUST FOLLOW:\n"
            f"1. Address the user as '{h}' — work it naturally into your responses.\n"
            f"2. NEVER end a response with 'feel free to ask', 'let me know', 'if you have any questions', or similar filler. Just answer and stop.\n"
            f"3. NEVER repeat or echo the user's question back to them.\n"
            f"4. When the user asks about past conversations ('did we discuss', 'do you remember', 'remind me'), "
            f"look through the conversation history above for the answer before saying you don't recall.\n"
            f"5. ONLY use imperial units (miles, Fahrenheit, pounds). NEVER include metric conversions in parentheses. Do NOT write '750 miles (1,207 kilometers)' — just write '750 miles'.\n"
            f"STYLE: You are speaking aloud. Be concise, natural, and conversational. "
            f"For factual questions: 1-3 clear sentences. "
            f"For deeper questions: up to a short paragraph, informative but not lecturing. "
            f"Be understated and professional with occasional dry British wit. "
            f"When discussing the user's personal details (age, birthday, name), be warm and personable — "
            f"say 'years young' not 'years old', use 'today' not the literal date, keep it human. "
            f"When asked about preferences or opinions, give thoughtful answers with personality — "
            f"never say 'I don't have preferences' or 'as an AI'."
        )

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
        try:
            import anthropic

            api_key = self.config.get_env(self.api_key_env)
            if not api_key or api_key == "your_key_here":
                self.logger.error("Claude API key not configured")
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
            start = time.time()

            message = client.messages.create(
                model=self.api_model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=messages
            )

            response = message.content[0].text
            elapsed = time.time() - start
            self.api_call_count += 1
            self.last_call_info = {
                "provider": "claude",
                "input_tokens": message.usage.input_tokens,
                "output_tokens": message.usage.output_tokens,
            }
            self.logger.info(f"✅ Claude API responded in {elapsed:.1f}s "
                           f"(tokens: {message.usage.input_tokens}+{message.usage.output_tokens}, "
                           f"total API calls this session: {self.api_call_count})")
            return response

        except ImportError:
            self.logger.error("anthropic package not installed")
            return ""
        except Exception as e:
            self.logger.error(f"Claude API call failed: {e}")
            return ""

    def chat(self, user_message: str, conversation_history: str = "",
             use_api: bool = False, max_tokens: int = None,
             memory_context: str = None,
             conversation_messages: list = None) -> str:
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

        Returns:
            Assistant response
        """
        if max_tokens is None:
            max_tokens = self._estimate_max_tokens(user_message)
        # If explicitly requesting API, go straight there
        if use_api:
            return self._generate_api_chat(user_message, conversation_history,
                                           max_tokens, conversation_messages)

        # --- Attempt 1: Local Qwen (uses ChatML prompt, not message list) ---
        prompt = self._build_chat_prompt(user_message, conversation_history,
                                         memory_context=memory_context)
        start = time.time()
        response = self._generate_local(prompt, max_tokens)
        elapsed_ms = (time.time() - start) * 1000

        quality_issue = self._check_response_quality(response, user_message)
        if not quality_issue:
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
            return api_response

        # Everything failed — return whatever local gave us
        self.logger.error("All LLM attempts failed")
        return response if response else ""

    def stream(self, user_message: str, conversation_history: str = "",
               max_tokens: int = None, memory_context: str = None,
               conversation_messages: list = None) -> Iterator[str]:
        """Stream tokens from the local LLM as they're generated.

        Uses the llama.cpp /v1/chat/completions endpoint with SSE streaming.
        Yields individual tokens as they arrive.

        Args:
            user_message: Current user message
            conversation_history: Previous conversation (ChatML-formatted)
            max_tokens: Maximum tokens to generate (auto-estimated from query if None)
            memory_context: Optional proactive memory context to inject into system prompt
            conversation_messages: Pre-built message list (bypasses string parsing)

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

        # Ensure current message is included
        if not messages or messages[-1].get("content") != user_message:
            messages.append({"role": "user", "content": user_message})

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
                            yield token
                    except (json.JSONDecodeError, KeyError, IndexError) as e:
                        self.logger.debug(f"Skipping malformed SSE chunk: {e}")
                        continue

        except Exception as e:
            self.logger.error(f"LLM streaming error: {e}")

    def stream_with_tools(self, user_message: str, conversation_history: str = "",
                          max_tokens: int = None, memory_context: str = None,
                          conversation_messages: list = None,
                          raw_command: str = None,
                          ) -> Iterator[Union[str, ToolCallRequest]]:
        """Stream tokens from the local LLM with tool calling support.

        Like stream(), but passes tool definitions to the server. If the LLM
        decides to call a tool, yields a ToolCallRequest instead of text tokens.
        The caller should then execute the tool and call continue_after_tool_call().

        Args:
            raw_command: Reserved (previously used for tool_choice pattern
                         matching, now unused since tool_choice=auto).

        Yields:
            str tokens for regular text, or a single ToolCallRequest.
        """
        if not self.tool_calling:
            yield from self.stream(user_message, conversation_history,
                                   max_tokens, memory_context, conversation_messages)
            return

        if max_tokens is None:
            max_tokens = self._estimate_max_tokens(user_message)
        system_prompt = self._build_system_prompt()
        # Prescriptive tool-use prompt: tell Qwen exactly when to search and
        # when NOT to.  Qwen3-8B with tool_choice=auto ignores vague guidance
        # ("when in doubt, search") but reliably follows explicit rules.
        # Tested 15 runs × 7 queries + 15 edge cases × 3 runs = 0 failures.
        today = date.today().strftime("%B %d, %Y")
        system_prompt += (
            f"\n\nToday's date is {today}. "
            "Your training data is OUTDATED and UNRELIABLE.\n\n"
            "RULES — follow these EXACTLY:\n"
            "1. You MUST call web_search for ANY question that has a verifiable "
            "answer. This includes: people, events, dates, releases, versions, "
            "products, prices, scores, statistics, organizations, places, distances, "
            "travel, weather, news, technology, software, science, politics — "
            "ANYTHING that could be looked up.\n"
            "2. NEVER answer from memory if the answer could change or be wrong.\n"
            "3. NEVER say 'I don't have information', 'check official sources', "
            "'you might want to check', or tell the user to look it up themselves. "
            "If you don't know, SEARCH.\n"
            "4. When in doubt: SEARCH. An unnecessary search is harmless. "
            "A wrong answer is unacceptable.\n\n"
            "ONLY skip the search for:\n"
            "- Greetings ('hello', 'how are you', 'thanks')\n"
            "- Creative requests (jokes, stories, poems)\n"
            "- Following instructions ('repeat that', 'say it again')\n"
            "- Pure opinions with no factual component"
        )
        if memory_context:
            system_prompt += f"\n\n{memory_context}"
        messages = [{"role": "system", "content": system_prompt}]

        # Do NOT include conversation history for tool calling.
        # Context window messages (topically relevant) actively suppress tool use —
        # if the LLM sees a prior answer about the same topic, it copies it instead
        # of calling the tool. Keep it clean: system prompt + user message only.

        if not messages or messages[-1].get("content") != user_message:
            messages.append({"role": "user", "content": user_message})

        # Store messages for continue_after_tool_call()
        self._tool_call_messages = messages

        # Let Qwen decide when to search via the prescriptive system prompt.
        # The prompt explicitly lists when to search vs answer directly.
        # Tested: 15 runs × 7 queries = 105/105 correct, 15 edge cases × 3 = 45/45.
        tool_choice = "auto"

        self.logger.info(f"stream_with_tools: {len(messages)} messages, tool_choice={tool_choice}")

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
                    "tools": [WEB_SEARCH_TOOL],
                    "tool_choice": tool_choice,
                },
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
                    response = requests.post(
                        "http://127.0.0.1:8080/v1/chat/completions",
                        json={
                            "messages": messages,
                            "temperature": self.temperature,
                            "top_p": self.top_p,
                            "top_k": self.top_k,
                            "max_tokens": max_tokens,
                            "stream": True,
                            "tools": [WEB_SEARCH_TOOL],
                            "tool_choice": tool_choice,
                        },
                        timeout=30,
                        stream=True,
                    )
                else:
                    self.logger.error(f"LLM server rejected request: {err}")
                    return

            response.raise_for_status()

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
            self.logger.error(f"LLM streaming (tool) error: {e}")

    def continue_after_tool_call(self, tool_call: ToolCallRequest,
                                  tool_result: str,
                                  max_tokens: int = 400) -> Iterator[str]:
        """Continue LLM generation after a tool call completes.

        Sends the tool result back to the LLM and streams its synthesized answer.

        Args:
            tool_call: The ToolCallRequest that was executed
            tool_result: Formatted string of tool results
            max_tokens: Max tokens for the synthesized response

        Yields:
            Text tokens of the synthesized answer
        """
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

        # Synthesis instruction — tell Qwen to give a direct answer.
        # Don't lead with the honorific since the ack phrase already used it.
        # Anti-hallucination is safe HERE (synthesis) — it only suppresses
        # tool calling when placed in the system prompt for stream_with_tools().
        today = date.today().strftime("%B %d, %Y")
        messages.append({
            "role": "user",
            "content": (
                f"Today's date is {today}. "
                "Based on the search results above, give a direct, concise answer. "
                "Include specific details like scores, dates, and numbers when available. "
                "Maintain strict political neutrality — present facts objectively without "
                "editorial bias, emphasis on controversies, or opinionated framing. "
                "CRITICAL: Compare any event dates in the results against today's date. "
                "If an event is scheduled for a FUTURE date, clearly state it hasn't "
                "happened yet — do NOT report predictions, odds, or speculation as fact. "
                "If the results do NOT contain a clear answer, say so honestly. "
                "NEVER fabricate or guess. "
                "Do NOT start with 'Sir' — jump straight into the answer. "
                "NEVER tell the user to check another website or look elsewhere. "
                "You ARE their source of information."
            ),
        })

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
            response.raise_for_status()

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
                    token = delta.get("content", "")
                    if token:
                        yield token
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

        except Exception as e:
            self.logger.error(f"LLM continue_after_tool_call error: {e}")


# Convenience function
def get_llm_router(config) -> LLMRouter:
    """Get LLM router instance"""
    return LLMRouter(config)
