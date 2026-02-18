"""
Base Skill Class

Foundation class for all Jarvis skills.
Provides common functionality and interface.
"""

from typing import Optional, Dict, Any, Callable, List
from abc import ABC, abstractmethod
import time

from core.logger import get_logger
from core.honorific import get_honorific, resolve_honorific


class BaseSkill(ABC):
    """Base class for all Jarvis skills"""
    
    def __init__(self, config, conversation, tts, responses):
        """
        Initialize skill
        
        Args:
            config: Configuration object
            conversation: Conversation manager
            tts: Text-to-speech engine
            responses: Response library
        """
        self.config = config
        self.conversation = conversation
        self.tts = tts
        self.responses = responses
        self.logger = get_logger(self.__class__.__name__, config)
        
        # Skill metadata
        self.name = self.__class__.__name__
        self.description = ""
        self.category = "unknown"
        self.enabled = True
        
        # Intent patterns this skill handles
        self.intents: Dict[str, Callable] = {}
        
        # Semantic intents (similarity-based matching)
        self.semantic_intents: Dict[str, Dict] = {}
        
        # Tools this skill provides (for other skills or LLM)
        self.tools: Dict[str, Callable] = {}
        
        # Event handlers
        self.event_handlers: Dict[str, List[Callable]] = {}
        
        self.logger.debug(f"Skill {self.name} initialized")

    @property
    def honorific(self) -> str:
        """Current speaker's honorific (e.g., 'sir', 'mum')."""
        return get_honorific()

    @abstractmethod
    def initialize(self) -> bool:
        """
        Initialize the skill (called after construction)
        
        Returns:
            True if initialization successful
        """
        pass
    
    @abstractmethod
    def handle_intent(self, intent: str, entities: Dict[str, Any]) -> str:
        """
        Handle a matched intent
        
        Args:
            intent: Intent name
            entities: Extracted entities from user input
            
        Returns:
            Response text
        """
        pass
    
    def register_intent(self, pattern: str, handler: Callable, priority: int = 5):
        """
        Register an intent pattern
        
        Args:
            pattern: Intent pattern (can include {variables})
            handler: Function to handle this intent
            priority: Priority (1-10, higher = checked first)
        """
        self.intents[pattern] = {
            "handler": handler,
            "priority": priority,
        }
        self.logger.debug(f"Registered intent: {pattern}")
    
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
        intent_id = f"{self.name}_{handler.__name__}"
        
        self.semantic_intents[intent_id] = {
            "examples": examples,
            "handler": handler,
            "threshold": threshold,
            "priority": priority
        }
        
        self.logger.debug(f"Registered semantic intent: {intent_id} with {len(examples)} examples")

    def register_tool(self, name: str, func: Callable, description: str = ""):
        """
        Register a tool that other skills or LLM can use
        
        Args:
            name: Tool name
            func: Function to execute
            description: Tool description
        """
        self.tools[name] = {
            "func": func,
            "description": description,
        }
        self.logger.debug(f"Registered tool: {name}")
    
    def respond(self, text: str, speak: bool = True) -> str:
        """
        Generate response

        Args:
            text: Response text
            speak: Whether to speak the response

        Returns:
            Response text
        """
        text = resolve_honorific(text)

        if speak:
            self.tts.speak(text)

        return text
    
    def acknowledge(self) -> str:
        """Get random acknowledgment"""
        return self.responses.acknowledgment()
    
    def confirmation(self) -> str:
        """Get random confirmation"""
        return self.responses.confirmation()
    
    def emit_event(self, event_name: str, data: Any = None):
        """
        Emit an event
        
        Args:
            event_name: Event name
            data: Event data
        """
        self.logger.debug(f"Emitting event: {event_name}")
        # Event system will be implemented in skill_manager
    
    def listen_event(self, event_name: str, handler: Callable):
        """
        Listen for an event
        
        Args:
            event_name: Event name
            handler: Function to call when event occurs
        """
        if event_name not in self.event_handlers:
            self.event_handlers[event_name] = []
        self.event_handlers[event_name].append(handler)
        self.logger.debug(f"Listening for event: {event_name}")
    
    def store_data(self, key: str, value: Any):
        """
        Store skill-specific data
        
        Args:
            key: Data key
            value: Data value
        """
        # Data storage will be implemented
        pass
    
    def load_data(self, key: str, default: Any = None) -> Any:
        """
        Load skill-specific data
        
        Args:
            key: Data key
            default: Default value if not found
            
        Returns:
            Stored value or default
        """
        # Data storage will be implemented
        return default
    
    def get_metadata(self) -> Dict[str, Any]:
        """
        Get skill metadata
        
        Returns:
            Metadata dictionary
        """
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "enabled": self.enabled,
            "intents": list(self.intents.keys()),
            "tools": list(self.tools.keys()),
        }


class SkillMetadata:
    """Metadata for a skill (loaded from metadata.yaml)"""
    
    def __init__(self, metadata_dict: Dict[str, Any]):
        """
        Initialize metadata from dictionary
        
        Args:
            metadata_dict: Metadata dictionary from YAML
        """
        self.name = metadata_dict.get("name", "Unknown")
        self.version = metadata_dict.get("version", "1.0.0")
        self.description = metadata_dict.get("description", "")
        self.category = metadata_dict.get("category", "unknown")
        self.author = metadata_dict.get("author", "")
        self.dependencies = metadata_dict.get("dependencies", [])
        self.intents = metadata_dict.get("intents", [])
        self.enabled = metadata_dict.get("enabled", True)
        self.priority = metadata_dict.get("priority", 5)
        self.keywords = metadata_dict.get("keywords", [])  # Add keywords support
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "category": self.category,
            "author": self.author,
            "dependencies": self.dependencies,
            "intents": self.intents,
            "enabled": self.enabled,
            "priority": self.priority,
        }
