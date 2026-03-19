"""
Structured JSONL debug logger for full pipeline visibility.

Activated by sentinel file: /tmp/.jarvis_debug_active
The sentinel file contains the output path for the JSONL log.

When the sentinel file does not exist, all methods are no-ops (zero overhead).
"""

import json
import os
import time
import logging

from core.logger import get_logger
logger = get_logger("jarvis.debug_logger")

_SENTINEL_PATH = "/tmp/.jarvis_debug_active"

# Module-level singleton
_instance = None


class ConversationDebugLogger:
    """Structured JSONL logger for full pipeline visibility."""

    def __init__(self, output_path: str):
        self.output_path = output_path
        self._fh = open(output_path, 'w')
        logger.info("Debug logger active: %s", output_path)

    def _write(self, event_type: str, data: dict):
        """Write one JSONL event line."""
        record = {
            "ts": time.time(),
            "type": event_type,
            **data,
        }
        try:
            line = json.dumps(record, default=str)
            self._fh.write(line + "\n")
            self._fh.flush()
        except Exception as e:
            logger.warning("Debug log write failed: %s", e)

    # -- Convenience methods --

    def log_command_received(self, command: str, user_id: str = None,
                              client_type: str = None,
                              in_conversation: bool = False,
                              image_data: bool = False):
        self._write("command_received", {
            "command": command,
            "user_id": user_id,
            "client_type": client_type,
            "in_conversation": in_conversation,
            "has_image": image_data,
        })

    def log_route_decision(self, command: str, result, routing_time_ms: float = 0):
        data = {
            "command": command[:200],
            "handled": result.handled,
            "intent": getattr(result, 'intent', None),
            "skip": getattr(result, 'skip', False),
            "routing_time_ms": round(routing_time_ms, 1),
        }
        mi = result.match_info
        if mi:
            data["match_info"] = {
                "layer": mi.get("layer"),
                "skill_name": mi.get("skill_name"),
                "handler": mi.get("handler"),
                "confidence": mi.get("confidence"),
                "intent_id": mi.get("intent_id"),
            }
        if getattr(result, 'use_tools', None):
            data["use_tools"] = [t["function"]["name"] for t in result.use_tools]
        synth_cat = getattr(result, 'synthesis_category', None)
        if synth_cat:
            data["synthesis_category"] = synth_cat
            data["synthesis_temperature"] = getattr(result, 'synthesis_temperature', None)
        self._write("route_decision", data)

    def log_llm_context(self, system_prompt: str = None,
                         user_message: str = None,
                         memory_context: str = None,
                         history: str = None,
                         context_messages=None,
                         tools=None):
        self._write("llm_context", {
            "system_prompt_len": len(system_prompt) if system_prompt else 0,
            "system_prompt_preview": (system_prompt[:500] + "...") if system_prompt and len(system_prompt) > 500 else system_prompt,
            "user_message_len": len(user_message) if user_message else 0,
            "user_message": user_message,
            "memory_context_len": len(memory_context) if memory_context else 0,
            "memory_context": memory_context,
            "history_len": len(history) if history else 0,
            "history_preview": (history[:500] + "...") if history and len(history) > 500 else history,
            "context_messages_count": len(context_messages) if context_messages else 0,
            "tool_count": len(tools) if tools else 0,
            "tool_names": [t["function"]["name"] for t in tools] if tools else [],
        })

    def log_tool_call(self, tool_name: str, arguments: dict,
                       chain_count: int = 1):
        self._write("tool_call", {
            "tool_name": tool_name,
            "arguments": arguments,
            "chain_count": chain_count,
        })

    def log_tool_result(self, tool_name: str, result_text: str,
                         result_len: int = 0):
        self._write("tool_result", {
            "tool_name": tool_name,
            "result_preview": (result_text[:2000] + "...") if result_text and len(result_text) > 2000 else result_text,
            "result_len": result_len or (len(result_text) if result_text else 0),
        })

    def log_llm_messages(self, messages: list, tool_count: int = 0,
                          temperature: float = None,
                          presence_penalty: float = None,
                          label: str = "stream_with_tools",
                          synthesis_category: str = None):
        """Log the full messages array sent to the LLM."""
        data = {
            "label": label,
            "message_count": len(messages),
            "messages": [
                {
                    "role": m.get("role"),
                    "content_len": len(m.get("content") or "") if isinstance(m.get("content"), str) else None,
                    "content_preview": (
                        (m["content"][:1000] + "...")
                        if isinstance(m.get("content"), str) and len(m["content"]) > 1000
                        else m.get("content")
                    ),
                    "has_tool_calls": bool(m.get("tool_calls")),
                }
                for m in messages
            ],
            "tool_count": tool_count,
            "temperature": temperature,
            "presence_penalty": presence_penalty,
        }
        if synthesis_category:
            data["synthesis_category"] = synthesis_category
        self._write("llm_messages", data)

    def log_response(self, response_text: str, total_ms: float = 0,
                      llm_model: str = None, tokens: int = 0,
                      stats: dict = None):
        self._write("response", {
            "response_len": len(response_text) if response_text else 0,
            "response_preview": (response_text[:500] + "...") if response_text and len(response_text) > 500 else response_text,
            "total_ms": round(total_ms, 1),
            "llm_model": llm_model,
            "tokens": tokens,
            "stats": stats,
        })

    def log_skill_match(self, skill_name: str, layer: str = None,
                         confidence: float = None, intent_id: str = None):
        self._write("skill_match", {
            "skill_name": skill_name,
            "layer": layer,
            "confidence": confidence,
            "intent_id": intent_id,
        })

    # -- Plan execution events --

    def log_plan_generated(self, command: str, plan_json: str,
                            step_count: int, step_details: list,
                            signal: str = None, llm_response: str = None):
        """Log the raw plan output from generate_plan()."""
        self._write("plan_generated", {
            "command": command,
            "signal": signal,
            "step_count": step_count,
            "steps": step_details,  # list of {step_id, skill, input, description}
            "llm_raw_response": llm_response,
            "plan_json": plan_json,
        })

    def log_plan_step_start(self, step_id: int, total_steps: int,
                             skill_name: str, description: str,
                             input_text: str, enriched_text: str = None):
        """Log when a plan step begins execution."""
        self._write("plan_step_start", {
            "step_id": step_id,
            "total_steps": total_steps,
            "skill_name": skill_name,
            "description": description,
            "input_text": input_text,
            "enriched_text_len": len(enriched_text) if enriched_text else 0,
            "enriched_text": enriched_text,
        })

    def log_plan_step_routing(self, step_id: int, skill_name: str,
                               method: str, matched_skill: str = None,
                               matched_intent: str = None,
                               match_score: float = None,
                               match_layer: str = None):
        """Log the routing decision for a plan step."""
        self._write("plan_step_routing", {
            "step_id": step_id,
            "expected_skill": skill_name,
            "routing_method": method,  # "match_intent", "execute_intent", "llm_fallback"
            "matched_skill": matched_skill,
            "matched_intent": matched_intent,
            "match_score": match_score,
            "match_layer": match_layer,
        })

    def log_plan_step_result(self, step_id: int, skill_name: str,
                              status: str, result_text: str = None,
                              elapsed_ms: float = 0, routing_method: str = None):
        """Log the outcome of a plan step."""
        self._write("plan_step_result", {
            "step_id": step_id,
            "skill_name": skill_name,
            "status": status,  # "completed", "failed", "skipped", "cancelled"
            "routing_method": routing_method,
            "result_len": len(result_text) if result_text else 0,
            "result_text": result_text,
            "elapsed_ms": round(elapsed_ms, 1),
        })

    def log_plan_complete(self, step_count: int, completed: int,
                           failed: int, skipped: int,
                           status: str, total_ms: float = 0,
                           final_response: str = None):
        """Log plan completion summary."""
        self._write("plan_complete", {
            "step_count": step_count,
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "status": status,
            "total_ms": round(total_ms, 1),
            "final_response_len": len(final_response) if final_response else 0,
            "final_response": final_response,
        })

    def log_skill_event(self, skill_name: str, event: str, data: dict = None):
        """Generic skill-internal event for pipeline visibility."""
        self._write("skill_event", {
            "skill_name": skill_name,
            "event": event,
            **(data or {}),
        })

    def log_context_window(self, segments_count: int = 0,
                            verbatim_count: int = 0,
                            query: str = None):
        self._write("context_window", {
            "segments_count": segments_count,
            "verbatim_count": verbatim_count,
            "query": query,
        })

    def log_conversation_history(self, history_text: str = None,
                                  message_count: int = 0):
        self._write("conversation_history", {
            "history_len": len(history_text) if history_text else 0,
            "history_preview": (history_text[:1000] + "...") if history_text and len(history_text) > 1000 else history_text,
            "message_count": message_count,
        })

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None
            logger.info("Debug logger closed")


class _NoOpLogger:
    """No-op stub — all methods silently do nothing."""
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


_noop = _NoOpLogger()


def get_debug_logger():
    """Get the active debug logger, or a no-op stub if inactive.

    Checks the sentinel file on each call. This allows activation/deactivation
    without restarting the service.
    """
    global _instance

    if os.path.exists(_SENTINEL_PATH):
        try:
            with open(_SENTINEL_PATH) as f:
                output_path = f.read().strip()
        except OSError:
            return _noop

        if not output_path:
            return _noop

        # Reuse existing instance if same path
        if _instance and _instance.output_path == output_path:
            return _instance

        # Close old instance if path changed
        if _instance:
            _instance.close()

        _instance = ConversationDebugLogger(output_path)
        return _instance
    else:
        # Sentinel removed — deactivate
        if _instance:
            _instance.close()
            _instance = None
        return _noop
