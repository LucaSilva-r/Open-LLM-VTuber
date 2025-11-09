"""
Tool Call Validator - Validates tool calls before execution.

This module provides validation logic for tool calls to catch common errors
before execution, providing better error messages and reducing wasted retries.
"""

import json
from typing import Dict, Any, Tuple
from loguru import logger
from .types import ToolCallObject


class ToolValidator:
    """Validates tool calls before execution to catch common errors early."""

    @staticmethod
    def validate_tool_call(tool_call: ToolCallObject) -> Tuple[bool, str]:
        """
        Validate a tool call before execution.

        Args:
            tool_call: The tool call object to validate

        Returns:
            Tuple of (is_valid: bool, error_message: str)
            If valid, error_message is empty string
        """
        # Extract tool name from the function object
        tool_name = tool_call.function.name if tool_call.function else ""

        # Parse arguments from JSON string
        try:
            tool_params = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
        except json.JSONDecodeError:
            logger.error(f"Failed to parse tool arguments: {tool_call.function.arguments}")
            return False, "Error: Argomenti del tool non validi (JSON malformato)"

        # Home Assistant tools validation
        if tool_name.startswith("Hass"):
            return ToolValidator._validate_home_assistant_tool(tool_name, tool_params)

        # Search tools validation
        if tool_name in ["search", "ddg_search"]:
            return ToolValidator._validate_search_tool(tool_name, tool_params)

        # Time tools validation
        if tool_name in ["get_current_time", "convert_time"]:
            return ToolValidator._validate_time_tool(tool_name, tool_params)

        # Default: allow other tools through
        return True, ""

    @staticmethod
    def _validate_home_assistant_tool(
        tool_name: str, tool_params: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """Validate Home Assistant tool calls."""

        # Most Home Assistant tools require 'name' parameter
        control_tools = [
            "HassTurnOn",
            "HassTurnOff",
            "HassLightSet",
            "HassSetPosition",
            "HassMediaPlay",
            "HassMediaPause",
            "HassMediaNext",
            "HassMediaPrevious",
            "HassVacuumStart",
            "HassVacuumReturnToBase",
        ]

        if tool_name in control_tools:
            # Check for required 'name' parameter
            if "name" not in tool_params or not tool_params["name"]:
                error_msg = f"Error: {tool_name} richiede il parametro 'name' (nome del dispositivo). Usa prima GetLiveContext per vedere i dispositivi disponibili."
                logger.warning(f"❌ Validation failed: {error_msg}")
                return False, error_msg

            # Warn about device_class (often causes errors)
            if "device_class" in tool_params:
                logger.warning(
                    f"⚠️  Tool call includes 'device_class' parameter which often causes errors. "
                    f"Consider removing it and using only 'name' and 'domain'."
                )

            # For control tools, domain is helpful (but not strictly required by all)
            if tool_name in ["HassTurnOn", "HassTurnOff", "HassLightSet"]:
                if "domain" not in tool_params or not tool_params["domain"]:
                    logger.warning(
                        f"⚠️  {tool_name} chiamato senza 'domain'. "
                        f"È consigliabile chiamare prima GetLiveContext per ottenere il domain corretto."
                    )

        # GetLiveContext validation
        elif tool_name == "GetLiveContext":
            # GetLiveContext doesn't require parameters, but 'area' is optional
            pass

        return True, ""

    @staticmethod
    def _validate_search_tool(
        tool_name: str, tool_params: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """Validate search tool calls."""

        # Search tools require 'query' parameter
        if "query" not in tool_params or not tool_params["query"]:
            error_msg = f"Error: {tool_name} richiede il parametro 'query' (testo da cercare)."
            logger.warning(f"❌ Validation failed: {error_msg}")
            return False, error_msg

        # Check if query looks like it should be a time query
        query = str(tool_params["query"]).lower()
        time_indicators = ["che ore", "che ora", "orario", "what time"]
        if any(indicator in query for indicator in time_indicators):
            logger.warning(
                f"⚠️  La query di ricerca '{query}' sembra una richiesta di orario. "
                f"Considera di usare 'get_current_time' invece di '{tool_name}'."
            )

        return True, ""

    @staticmethod
    def _validate_time_tool(
        tool_name: str, tool_params: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """Validate time tool calls."""

        # get_current_time should have timezone
        if tool_name == "get_current_time":
            if "timezone" not in tool_params:
                # Not an error, but log a warning
                logger.warning(
                    f"⚠️  get_current_time chiamato senza 'timezone'. "
                    f"Usa timezone='Europe/Rome' per l'Italia."
                )

        # convert_time requires from_timezone, to_timezone, time
        elif tool_name == "convert_time":
            required = ["from_timezone", "to_timezone", "time"]
            missing = [p for p in required if p not in tool_params]
            if missing:
                error_msg = f"Error: convert_time richiede i parametri: {', '.join(missing)}"
                logger.warning(f"❌ Validation failed: {error_msg}")
                return False, error_msg

        return True, ""

    @staticmethod
    def get_validation_hint(tool_name: str, error: str) -> str:
        """
        Get a helpful hint based on the tool name and error.

        Args:
            tool_name: The name of the tool that failed
            error: The error message

        Returns:
            A helpful hint in Italian for fixing the error
        """
        hints = {
            "HassTurnOn": "Suggerimento: Chiama prima GetLiveContext() per vedere i dispositivi disponibili, poi usa il nome e domain esatti.",
            "HassTurnOff": "Suggerimento: Chiama prima GetLiveContext() per vedere i dispositivi disponibili, poi usa il nome e domain esatti.",
            "HassLightSet": "Suggerimento: Chiama prima GetLiveContext() per vedere le luci disponibili, poi usa il nome e domain esatti.",
            "search": "Suggerimento: Il parametro 'query' deve contenere il testo da cercare (es. 'meteo Roma').",
            "ddg_search": "Suggerimento: Il parametro 'query' deve contenere il testo da cercare (es. 'meteo Roma').",
            "get_current_time": "Suggerimento: Usa timezone='Europe/Rome' per l'orario italiano.",
        }

        return hints.get(tool_name, "Controlla i parametri richiesti dal tool.")
