import requests
import json
import logging

class LLMServerClient:
    """Client for llama-server REST API"""
    
    def __init__(self, base_url="http://127.0.0.1:8080"):
        self.base_url = base_url
        self.endpoint = f"{base_url}/v1/chat/completions"
        self.logger = logging.getLogger(__name__)
    
    def generate(self, user_message: str, system_prompt: str, 
                 temperature: float = 0.3, max_tokens: int = 100) -> str:
        """Generate response using llama-server"""
        
        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        
        try:
            response = requests.post(
                self.endpoint,
                json=payload,
                timeout=30
            )
            response.raise_for_status()
            
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()
            
        except Exception as e:
            self.logger.error(f"LLM server error: {e}")
            return ""
