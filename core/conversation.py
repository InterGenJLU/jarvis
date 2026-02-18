"""
Conversation Manager

Manages conversation state, memory, context, and follow-up windows.
Tracks conversation history and maintains context across interactions.
"""

import json
import time
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

from core.logger import get_logger


class ConversationManager:
    """Manages conversation state and context"""
    
    def __init__(self, config):
        """
        Initialize conversation manager
        
        Args:
            config: Configuration object
        """
        self.config = config
        self.logger = get_logger(__name__, config)
        
        # Storage paths
        storage_path = Path(config.get("system.storage_path"))
        self.conversations_dir = storage_path / "data" / "conversations"
        self.conversations_dir.mkdir(parents=True, exist_ok=True)
        
        self.chat_history_file = self.conversations_dir / "chat_history.jsonl"
        
        # Configuration
        self.max_history_turns = config.get("conversation.max_history_turns", 16)
        self.max_context_chars = config.get("conversation.max_context_chars", 12000)
        
        # Follow-up window configuration
        self.follow_up_enabled = config.get("conversation.follow_up_window.enabled", True)
        self.follow_up_default_duration = config.get("conversation.follow_up_window.default_duration", 4.0)
        self.follow_up_extended_duration = config.get("conversation.follow_up_window.extended_duration", 7.0)
        
        # Current conversation state
        self.current_user = None
        self.conversation_active = False
        
        # Follow-up window state
        self.follow_up_active = False
        self.follow_up_expires = 0.0

        # Skill-requested follow-up window (skills set this to a duration
        # to request a conversation window after execution; main loop resets it)
        self.request_follow_up: Optional[float] = None
        
        # In-memory history (current session + loaded prior context)
        self.session_history: List[Dict] = []

        # Load recent history from prior sessions for cross-session memory
        self._load_prior_context()

        # Continuation phrases (for detecting follow-ups)
        self.continuation_phrases = [
            "ok", "okay", "so", "then", "awesome", "great", "cool",
            "and", "also", "what about", "how about", "now",
            "can you", "could you", "let's", "please", "yes", "yeah"
        ]

        # Memory system hook (set via set_memory_manager during startup)
        self._memory_manager = None

        # Context window hook (set via set_context_window during startup)
        self._context_window = None

        self.logger.info("Conversation manager initialized")
    
    def _load_prior_context(self):
        """Load recent conversation history from disk for cross-session memory.

        This gives JARVIS awareness of prior conversations so the user can
        reference things discussed in previous sessions.
        """
        prior_messages = self.load_full_history(max_messages=self.max_history_turns * 2)
        if prior_messages:
            self.session_history = prior_messages
            self.logger.info(
                f"Loaded {len(prior_messages)} messages from prior sessions "
                f"(spanning back to {self._format_timestamp(prior_messages[0].get('timestamp', 0))})"
            )
        else:
            self.logger.info("No prior conversation history found")

    @staticmethod
    def _format_timestamp(ts: float) -> str:
        """Format a Unix timestamp for logging"""
        if not ts:
            return "unknown"
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            return "unknown"

    @staticmethod
    def _format_timestamp_for_llm(ts: float) -> str:
        """Format a Unix timestamp for LLM context.

        Uses relative terms for recent messages (today/yesterday)
        and absolute dates for older ones, so the LLM can reason
        about when things were said.
        """
        if not ts:
            return ""
        try:
            msg_time = datetime.fromtimestamp(ts)
            now = datetime.now()
            delta = now - msg_time

            time_part = msg_time.strftime("%-I:%M %p")

            if delta.days == 0 and msg_time.date() == now.date():
                return f"today {time_part}"
            elif delta.days <= 1 and (now.date() - msg_time.date()).days == 1:
                return f"yesterday {time_part}"
            elif delta.days < 7:
                day_name = msg_time.strftime("%A")
                return f"{day_name} {time_part}"
            else:
                return msg_time.strftime("%b %-d, %-I:%M %p")
        except (ValueError, OSError):
            return ""

    def add_message(self, role: str, content: str, user_id: Optional[str] = None):
        """
        Add message to conversation history
        
        Args:
            role: 'user' or 'assistant'
            content: Message content
            user_id: Optional user identifier
        """
        message = {
            "timestamp": time.time(),
            "role": role,
            "content": content,
            "user_id": user_id or self.current_user,
        }
        
        # Add to session history
        self.session_history.append(message)
        
        # Persist to disk
        self._append_to_history_file(message)

        # Memory system hook (non-blocking)
        if self._memory_manager:
            try:
                self._memory_manager.on_message(message)
            except Exception as e:
                self.logger.warning(f"Memory hook failed (non-fatal): {e}")

        # Context window hook (non-blocking)
        if self._context_window:
            try:
                self._context_window.on_message(message)
            except Exception as e:
                self.logger.warning(f"Context window hook failed (non-fatal): {e}")

        self.logger.debug(f"Added {role} message: {content[:50]}...")
    
    def _append_to_history_file(self, message: Dict):
        """Append message to JSONL history file"""
        try:
            with open(self.chat_history_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(message, ensure_ascii=False) + '\n')
        except Exception as e:
            self.logger.error(f"Failed to append to history file: {e}")
    
    def set_memory_manager(self, memory_manager):
        """Wire up memory system hooks. Called during startup."""
        self._memory_manager = memory_manager

    def set_context_window(self, context_window):
        """Wire up context window hooks. Called during startup."""
        self._context_window = context_window

    def get_recent_history(self, max_turns: Optional[int] = None) -> List[Dict]:
        """
        Get recent conversation history
        
        Args:
            max_turns: Maximum number of turns (user+assistant pairs)
            
        Returns:
            List of message dictionaries
        """
        if max_turns is None:
            max_turns = self.max_history_turns
        
        # Get last N turns (each turn = user + assistant message)
        max_messages = max_turns * 2
        recent = self.session_history[-max_messages:] if self.session_history else []
        
        return self._trim_by_characters(recent, self.max_context_chars)
    
    def _trim_by_characters(self, messages: List[Dict], max_chars: int) -> List[Dict]:
        """
        Trim messages to fit within character limit
        
        Args:
            messages: List of messages
            max_chars: Maximum total characters
            
        Returns:
            Trimmed list of messages
        """
        total_chars = 0
        trimmed = []
        
        # Work backwards to keep most recent messages
        for msg in reversed(messages):
            content_len = len(msg.get("content", ""))
            if total_chars + content_len > max_chars:
                break
            total_chars += content_len
            trimmed.insert(0, msg)
        
        return trimmed
    
    def should_open_follow_up_window(self, response_text: str) -> bool:
        """
        Determine if response invites follow-up
        
        Args:
            response_text: Assistant's response
            
        Returns:
            True if follow-up window should open
        """
        if not self.follow_up_enabled:
            return False
        
        # Check for explicit invitations
        invitations = [
            "would you like", "should i", "shall i",
            "want me to", "can show you", "do you want",
            "interested in", "?", "would you", "may i"
        ]
        
        text_lower = response_text.lower()
        return any(invite in text_lower for invite in invitations)
    
    def get_follow_up_duration(self, response_text: str) -> float:
        """
        Get appropriate follow-up window duration
        
        Args:
            response_text: Assistant's response
            
        Returns:
            Duration in seconds
        """
        if self.should_open_follow_up_window(response_text):
            return self.follow_up_extended_duration
        else:
            return self.follow_up_default_duration
    
    def open_follow_up_window(self, duration: Optional[float] = None):
        """
        Open follow-up listening window
        
        Args:
            duration: Window duration in seconds (optional)
        """
        if duration is None:
            duration = self.follow_up_default_duration
        
        self.follow_up_active = True
        self.follow_up_expires = time.time() + duration
        
        self.logger.debug(f"Follow-up window opened for {duration}s")
    
    def close_follow_up_window(self):
        """Close follow-up window"""
        self.follow_up_active = False
        self.follow_up_expires = 0.0
        self.logger.debug("Follow-up window closed")
    
    def is_follow_up_active(self) -> bool:
        """Check if follow-up window is currently active"""
        if not self.follow_up_active:
            return False
        
        if time.time() >= self.follow_up_expires:
            self.close_follow_up_window()
            return False
        
        return True
    
    def is_continuation(self, user_text: str) -> bool:
        """
        Check if user text is a conversation continuation
        
        Args:
            user_text: User's input text
            
        Returns:
            True if text starts with continuation phrase
        """
        if not user_text:
            return False
        
        first_word = user_text.strip().lower().split()[0] if user_text.strip() else ""
        return first_word in self.continuation_phrases
    
    def format_history_for_llm(self, include_system_prompt: bool = True) -> str:
        """
        Format conversation history for LLM input
        
        Args:
            include_system_prompt: Whether to include system prompt
            
        Returns:
            Formatted conversation history
        """
        history = self.get_recent_history()
        
        lines = []
        
        if include_system_prompt:
            lines.append("You are JARVIS, a sophisticated AI assistant inspired by Tony Stark's AI.")
            lines.append("You are helpful, professional, and concise.")
            lines.append("")
        
        for msg in history:
            role = msg["role"].upper()
            content = msg["content"]
            ts = msg.get("timestamp")
            if ts:
                time_str = self._format_timestamp_for_llm(ts)
                lines.append(f"[{time_str}] {role}: {content}")
            else:
                lines.append(f"{role}: {content}")
        
        return "\n".join(lines)
    
    def clear_session_history(self):
        """Clear in-memory session history (not persistent storage)"""
        self.session_history = []
        self.logger.info("Session history cleared")
    
    def load_full_history(self, max_messages: Optional[int] = None) -> List[Dict]:
        """
        Load conversation history from disk
        
        Args:
            max_messages: Maximum messages to load (most recent)
            
        Returns:
            List of message dictionaries
        """
        if not self.chat_history_file.exists():
            return []
        
        messages = []
        
        try:
            with open(self.chat_history_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        messages.append(msg)
                    except json.JSONDecodeError:
                        continue
            
            # Return most recent N messages if specified
            if max_messages and len(messages) > max_messages:
                messages = messages[-max_messages:]
            
            return messages
            
        except Exception as e:
            self.logger.error(f"Failed to load history: {e}")
            return []
    
    def get_conversation_stats(self) -> Dict:
        """
        Get conversation statistics
        
        Returns:
            Dictionary with stats
        """
        total_messages = len(self.load_full_history())
        session_messages = len(self.session_history)
        
        user_messages = sum(1 for msg in self.session_history if msg["role"] == "user")
        assistant_messages = sum(1 for msg in self.session_history if msg["role"] == "assistant")
        
        return {
            "total_messages_all_time": total_messages,
            "session_messages": session_messages,
            "session_user_messages": user_messages,
            "session_assistant_messages": assistant_messages,
            "follow_up_active": self.follow_up_active,
        }


# Convenience function
def get_conversation_manager(config) -> ConversationManager:
    """Get conversation manager instance"""
    return ConversationManager(config)
