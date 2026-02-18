"""
Configuration Management

Loads and validates system configuration from YAML file.
Handles environment variable expansion and path resolution.
"""

import os
import yaml
from pathlib import Path
from typing import Any, Dict
from dotenv import load_dotenv


class Config:
    """Configuration manager for Jarvis system"""
    
    def __init__(self, config_path: str = None):
        """
        Load configuration from YAML file
        
        Args:
            config_path: Path to config.yaml (default: ~/jarvis/config.yaml)
        """
        # Load environment variables from .env file
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)
        
        # Determine config path
        if config_path is None:
            config_path = Path(__file__).parent.parent / "config.yaml"
        else:
            config_path = Path(config_path)
        
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        
        # Load YAML
        with open(config_path, 'r') as f:
            self._config = yaml.safe_load(f)
        
        # Expand paths and environment variables
        self._expand_paths(self._config)
    
    def _expand_paths(self, obj: Any) -> None:
        """Recursively expand ~ and environment variables in paths"""
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, str):
                    # Expand ~ and env vars
                    expanded = os.path.expanduser(value)
                    expanded = os.path.expandvars(expanded)
                    obj[key] = expanded
                elif isinstance(value, (dict, list)):
                    self._expand_paths(value)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                if isinstance(item, str):
                    expanded = os.path.expanduser(item)
                    expanded = os.path.expandvars(item)
                    obj[i] = expanded
                elif isinstance(item, (dict, list)):
                    self._expand_paths(item)
    
    def get(self, key_path: str, default: Any = None) -> Any:
        """
        Get configuration value using dot notation
        
        Args:
            key_path: Path to config value (e.g., "llm.local.model_path")
            default: Default value if key not found
            
        Returns:
            Configuration value
            
        Example:
            config.get("audio.sample_rate")  # Returns 16000
        """
        keys = key_path.split('.')
        value = self._config
        
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        
        return value
    
    def set(self, key_path: str, value: Any) -> None:
        """
        Set configuration value using dot notation
        
        Args:
            key_path: Path to config value (e.g., "audio.sample_rate")
            value: New value to set
        """
        keys = key_path.split('.')
        obj = self._config
        
        for key in keys[:-1]:
            if key not in obj:
                obj[key] = {}
            obj = obj[key]
        
        obj[keys[-1]] = value
    
    def get_env(self, env_var: str, default: str = None) -> str:
        """
        Get environment variable value
        
        Args:
            env_var: Environment variable name
            default: Default value if not found
            
        Returns:
            Environment variable value or default
        """
        return os.getenv(env_var, default)
    
    def ensure_directories(self) -> None:
        """Create necessary directories if they don't exist"""
        storage_path = Path(self.get("system.storage_path"))
        
        # Create storage structure
        dirs = [
            storage_path / "data" / "conversations",
            storage_path / "data" / "user_profiles" / "voice_embeddings",
            storage_path / "data" / "logs",
            storage_path / "data" / "version_control",
            storage_path / "models" / "llm",
            storage_path / "models" / "whisper",
            storage_path / "models" / "piper",
            storage_path / "skills",
            storage_path / "cache",
            storage_path / "config",
        ]
        
        for directory in dirs:
            directory.mkdir(parents=True, exist_ok=True)
    
    def __getitem__(self, key: str) -> Any:
        """Allow dict-like access: config['audio.sample_rate']"""
        return self.get(key)
    
    def __setitem__(self, key: str, value: Any) -> None:
        """Allow dict-like assignment: config['audio.sample_rate'] = 44100"""
        self.set(key, value)
    
    @property
    def all(self) -> Dict:
        """Get entire configuration dictionary"""
        return self._config


# Global config instance (loaded by main.py)
_config_instance = None


def load_config(config_path: str = None) -> Config:
    """Load or return existing config instance"""
    global _config_instance
    if _config_instance is None:
        _config_instance = Config(config_path)
    return _config_instance


def get_config() -> Config:
    """Get current config instance"""
    if _config_instance is None:
        raise RuntimeError("Config not loaded. Call load_config() first.")
    return _config_instance
