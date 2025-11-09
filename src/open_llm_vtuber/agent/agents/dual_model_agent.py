"""
Dual Model Agent - Uses two separate LLMs for conversation and tool calling.

This agent routes user input to different models based on intent:
- Fast conversational model for general chat
- Tool-specialized model for Home Assistant and other MCP tools
"""

import asyncio
from typing import AsyncIterator, List, Dict, Any, Callable, Optional, Union
from loguru import logger

from .agent_interface import AgentInterface
from .intent_router import IntentRouter
from ..output_types import SentenceOutput, DisplayText
from ..stateless_llm.stateless_llm_interface import StatelessLLMInterface
from ..stateless_llm.claude_llm import AsyncLLM as ClaudeAsyncLLM
from ..stateless_llm.openai_compatible_llm import AsyncLLM as OpenAICompatibleAsyncLLM
from ...chat_history_manager import get_history
from ..transformers import (
    sentence_divider,
    actions_extractor,
    tts_filter,
    display_processor,
)
from ...config_manager import TTSPreprocessorConfig
from ..input_types import BatchInput, TextSource
from ...mcpp.tool_manager import ToolManager
from ...mcpp.json_detector import StreamJSONDetector
from ...mcpp.types import ToolCallObject
from ...mcpp.tool_executor import ToolExecutor
from ...mcpp.tool_validator import ToolValidator


class DualModelAgent(AgentInterface):
    """
    Agent that uses two LLMs: one for conversation, one for tool calling.

    Intent detection is done via fast keyword matching to route requests
    to the appropriate model without adding latency.
    """

    _system: str = "You are a helpful assistant."

    def __init__(
        self,
        fast_llm: StatelessLLMInterface,  # For conversation
        tool_llm: StatelessLLMInterface,  # For tool calling
        system: str,
        live2d_model,
        tts_preprocessor_config: TTSPreprocessorConfig = None,
        faster_first_response: bool = True,
        segment_method: str = "pysbd",
        use_mcpp: bool = False,
        intent_detection_method: str = "llm",  # "llm" or "keyword"
        tool_keywords: List[str] = None,
        intent_llm: Optional[
            StatelessLLMInterface
        ] = None,  # Dedicated LLM for intent classification
        tool_manager: Optional[ToolManager] = None,
        tool_executor: Optional[ToolExecutor] = None,
        mcp_prompt_string: str = "",
        enable_tool_acknowledgment: bool = True,
    ):
        """
        Initialize dual model agent.

        Args:
            fast_llm: Fast LLM for general conversation
            tool_llm: Specialized LLM for tool calling
            system: System prompt
            live2d_model: Live2D model for expressions
            tts_preprocessor_config: TTS preprocessing configuration
            faster_first_response: Enable fast first response
            segment_method: Sentence segmentation method
            use_mcpp: Enable MCP tool support
            intent_detection_method: "llm" for LLM-based classification or "keyword" for keyword matching
            tool_keywords: Keywords for fallback keyword-based routing (optional)
            intent_llm: Optional dedicated LLM for intent classification (if None, uses fast_llm)
            tool_manager: MCP tool manager
            tool_executor: MCP tool executor
            mcp_prompt_string: MCP prompt for tool calling
            enable_tool_acknowledgment: Whether to generate acknowledgment message before tool execution
        """
        super().__init__()

        # Models
        self._fast_llm = fast_llm
        self._tool_llm = tool_llm

        # Shared configuration
        self._memory = []
        self._live2d_model = live2d_model
        self._tts_preprocessor_config = tts_preprocessor_config
        self._faster_first_response = faster_first_response
        self._segment_method = segment_method
        self._use_mcpp = use_mcpp
        self._interrupt_handled = False
        self._enable_tool_acknowledgment = enable_tool_acknowledgment

        # Intent detection configuration
        self._intent_detection_method = intent_detection_method
        self._intent_router = None
        if intent_detection_method == "llm":
            # Use dedicated intent LLM if provided, otherwise fall back to fast LLM
            intent_llm_to_use = intent_llm if intent_llm is not None else fast_llm
            self._intent_router = IntentRouter(intent_llm_to_use)
            if intent_llm is not None:
                logger.info("✅ Using dedicated LLM for intent classification")
            else:
                logger.warning(
                    "⚠️  Using conversation LLM for intent classification (no dedicated intent_llm provided)"
                )
        else:
            # Fallback to keyword matching
            self._tool_keywords = tool_keywords or self._default_italian_tool_keywords()
            logger.info("Using keyword-based intent detection")

        # Tool-related configuration
        self._tool_manager = tool_manager
        self._tool_executor = tool_executor
        self._mcp_prompt_string = mcp_prompt_string
        self._json_detector = StreamJSONDetector()

        # Debug: Log tool manager status
        if tool_manager:
            logger.info(f"✅ DualModelAgent has ToolManager with {len(tool_manager.get_formatted_tools('OpenAI') or [])} tools")
        else:
            logger.warning("⚠️  DualModelAgent initialized WITHOUT ToolManager (use_mcpp might be False)")

        # Tool formatting
        self._formatted_tools_openai = []
        self._formatted_tools_claude = []

        # Tools to exclude (you can customize this list)
        excluded_tools = ["HassListAddItem", "HassListCompleteItem", "todo_get_items"]

        if self._tool_manager:
            # Get all tools
            all_tools_openai = self._tool_manager.get_formatted_tools("OpenAI")
            all_tools_claude = self._tool_manager.get_formatted_tools("Claude")

            # Filter out excluded tools
            self._formatted_tools_openai = [
                tool
                for tool in all_tools_openai
                if tool.get("function", {}).get("name") not in excluded_tools
            ]
            self._formatted_tools_claude = [
                tool
                for tool in all_tools_claude
                if tool.get("name") not in excluded_tools
            ]

            # Get tool names for debugging
            openai_tool_names = [t.get("function", {}).get("name", "unknown") for t in self._formatted_tools_openai]

            logger.info(
                f"DualModelAgent received tools - OpenAI: {len(self._formatted_tools_openai)}/{len(all_tools_openai)}, "
                f"Claude: {len(self._formatted_tools_claude)}/{len(all_tools_claude)} "
                f"(excluded: {excluded_tools})"
            )
            logger.info(f"Available tool names: {openai_tool_names}")

        # Set system prompt
        self.set_system(system if system else self._system)

        # Statistics for monitoring
        self._stats = {
            "fast_model_calls": 0,
            "tool_model_calls": 0,
            "intent_detections": [],
        }

        logger.info(
            f"DualModelAgent initialized with {intent_detection_method} intent detection"
        )

    def _get_template_acknowledgment(self, user_input: str) -> str:
        """
        Get a simple template-based acknowledgment (no LLM needed).

        This is fast, reliable, and avoids reasoning issues.

        Args:
            user_input: The user's original request

        Returns:
            A template-based acknowledgment
        """
        import random

        # Italian templates for different actions
        templates = [
            "Perfetto, lo faccio subito!",
            "Va bene, un attimo!",
            "Certo, ci penso io!",
            "Ok, fatto!",
            "Subito!",
            "Ci sto lavorando!",
            "Un momento...",
            "Okay, procedo!",
        ]

        return random.choice(templates)

    def _get_tool_execution_feedback(self, tool_name: str, tool_params: dict[str, Any]) -> str:
        """
        Generate user-friendly feedback about which tool is being executed.

        Args:
            tool_name: Name of the tool being executed
            tool_params: Parameters being passed to the tool

        Returns:
            User-friendly feedback message in Italian
        """
        # Map tool names to user-friendly descriptions
        if tool_name == "search" or tool_name == "ddg_search":
            query = tool_params.get("query", "")
            return f"Sto cercando '{query}'..."

        elif tool_name == "get_current_time":
            return "Controllo l'orario..."

        elif tool_name == "convert_time":
            return "Sto convertendo il fuso orario..."

        elif tool_name == "fetch_content":
            url = tool_params.get("url", "")
            return f"Sto recuperando il contenuto da {url}..."

        elif tool_name == "GetLiveContext":
            area = tool_params.get("area", "")
            if area:
                return f"Controllo i dispositivi in {area}..."
            return "Controllo i dispositivi disponibili..."

        elif tool_name == "HassTurnOn":
            domain = tool_params.get("domain", "")
            name = tool_params.get("name", "")
            if name:
                return f"Accendo {name}..."
            elif domain:
                return f"Accendo il dispositivo..."
            return "Sto accendendo..."

        elif tool_name == "HassTurnOff":
            domain = tool_params.get("domain", "")
            name = tool_params.get("name", "")
            if name:
                return f"Spengo {name}..."
            elif domain:
                return f"Spengo il dispositivo..."
            return "Sto spegnendo..."

        elif tool_name == "HassLightSet":
            name = tool_params.get("name", "")
            if name:
                return f"Regolo {name}..."
            return "Sto regolando la luce..."

        elif tool_name == "HassCancelAllTimers":
            return "Annullo i timer..."

        # Default fallback
        return f"Eseguo {tool_name}..."

    async def _generate_acknowledgment(self, user_input: str) -> str:
        """
        Generate a quick acknowledgment message before tool execution.

        This uses the fast conversational model to generate a natural,
        personality-driven acknowledgment that the user's request is being processed.

        Args:
            user_input: The user's original request

        Returns:
            A short acknowledgment message (e.g., "Let me turn that on for you...")
        """
        # Use template-based acknowledgment by default (fast, reliable, no reasoning)
        # For LLM-generated acknowledgments, use a small non-reasoning model like:
        #   - Qwen2.5-3B-Instruct
        #   - Llama-3.2-3B-Instruct
        #   - Gemma-2-2B-Instruct
        return self._get_template_acknowledgment(user_input)

    @staticmethod
    def _default_italian_tool_keywords() -> List[str]:
        """
        Default Italian keywords that suggest tool usage.

        Returns:
            List of Italian keywords for tool intent detection
        """
        return [
            # Home control verbs
            "accendi",
            "accendere",
            "spegni",
            "spegnere",
            "attiva",
            "attivare",
            "disattiva",
            "disattivare",
            "apri",
            "aprire",
            "chiudi",
            "chiudere",
            "aumenta",
            "aumentare",
            "diminuisci",
            "diminuire",
            "imposta",
            "impostare",
            "regola",
            "regolare",
            "modifica",
            "modificare",
            # Home entities
            "luce",
            "luci",
            "light",
            "lights",
            "lampada",
            "lampadina",
            "temperatura",
            "termostato",
            "riscaldamento",
            "climatizzatore",
            "condizionatore",
            "ventilatore",
            "fan",
            "tapparella",
            "tapparelle",
            "persiana",
            "persiane",
            "tenda",
            "tende",
            "porta",
            "porte",
            "finestra",
            "finestre",
            "garage",
            "allarme",
            "alarm",
            "sicurezza",
            "security",
            "scena",
            "scene",
            "automazione",
            "automation",
            # Rooms (common Italian)
            "soggiorno",
            "salotto",
            "cucina",
            "camera",
            "bagno",
            "studio",
            "living",
            "bedroom",
            "bathroom",
            "kitchen",
            "office",
            # Time queries
            "che ora",
            "ora è",
            "orario",
            "tempo",
            "quando",
            "sveglia",
            "timer",
            "promemoria",
            # Search/info
            "cerca",
            "search",
            "trova",
            "find",
            "info",
            "informazioni",
            "dimmi",
            "mostrami",
            "controllare",
            "verifica",
            # Weather
            "meteo",
            "weather",
            "tempo",
            "previsioni",
            "forecast",
            "temperatura",
            "pioggia",
            "rain",
            "sole",
            "sun",
            "nuvole",
            "clouds",
            # News/current events
            "notizie",
            "news",
            "ultime",
            "latest",
            "oggi",
            "today",
        ]

    def _detect_tool_intent(self, user_input: str) -> tuple[bool, List[str]]:
        """
        Fast keyword-based intent detection using word boundaries.

        Args:
            user_input: User's text input

        Returns:
            Tuple of (needs_tool: bool, matched_keywords: List[str])
        """
        import re

        user_input_lower = user_input.lower()
        matched_keywords = []

        for keyword in self._tool_keywords:
            # Use word boundaries to avoid false positives (e.g., "fan" in "fantastico")
            # \b matches word boundaries
            pattern = r"\b" + re.escape(keyword) + r"\b"
            if re.search(pattern, user_input_lower):
                matched_keywords.append(keyword)

        needs_tool = len(matched_keywords) > 0

        if needs_tool:
            logger.info(
                f"🔧 Tool intent detected. Matched keywords: {matched_keywords}"
            )
        else:
            logger.debug("💬 Conversational intent detected")

        return needs_tool, matched_keywords

    def set_system(self, system: str):
        """Set the system prompt for both models."""
        logger.debug("DualModelAgent: Setting system prompt")
        self._system = system

    def _add_message(
        self,
        message: Union[str, List[Dict[str, Any]]],
        role: str,
        display_text: DisplayText | None = None,
        skip_memory: bool = False,
    ):
        """Add message to shared memory."""
        if skip_memory:
            return

        text_content = ""
        if isinstance(message, list):
            for item in message:
                if item.get("type") == "text":
                    text_content += item["text"] + " "
            text_content = text_content.strip()
        elif isinstance(message, str):
            text_content = message
        else:
            logger.warning(f"Unexpected message type: {type(message)}")
            text_content = str(message)

        if not text_content and role == "assistant":
            return

        message_data = {
            "role": role,
            "content": text_content,
        }

        if display_text:
            if display_text.name:
                message_data["name"] = display_text.name
            if display_text.avatar:
                message_data["avatar"] = display_text.avatar

        # Avoid duplicates
        if (
            self._memory
            and self._memory[-1]["role"] == role
            and self._memory[-1]["content"] == text_content
        ):
            return

        self._memory.append(message_data)

    def set_memory_from_history(self, conf_uid: str, history_uid: str) -> None:
        """Load memory from chat history."""
        messages = get_history(conf_uid, history_uid)

        self._memory = []
        for msg in messages:
            role = "user" if msg["role"] == "human" else "assistant"
            content = msg["content"]
            if isinstance(content, str) and content:
                self._memory.append({"role": role, "content": content})
            else:
                logger.warning(f"Skipping invalid message from history: {msg}")
        logger.info(f"Loaded {len(self._memory)} messages from history.")

    def handle_interrupt(self, heard_response: str) -> None:
        """Handle user interruption."""
        if self._interrupt_handled:
            return

        self._interrupt_handled = True

        if self._memory and self._memory[-1]["role"] == "assistant":
            if not self._memory[-1]["content"].endswith("..."):
                self._memory[-1]["content"] = heard_response + "..."
            else:
                self._memory[-1]["content"] = heard_response + "..."
        else:
            if heard_response:
                self._memory.append(
                    {"role": "assistant", "content": heard_response + "..."}
                )

        self._memory.append({"role": "user", "content": "[Interrupted by user]"})
        logger.info("Handled interrupt.")

    def _to_text_prompt(self, input_data: BatchInput) -> str:
        """Format input data to text prompt."""
        message_parts = []

        for text_data in input_data.texts:
            if text_data.source == TextSource.INPUT:
                message_parts.append(text_data.content)
            elif text_data.source == TextSource.CLIPBOARD:
                message_parts.append(
                    f"[User shared content from clipboard: {text_data.content}]"
                )

        if input_data.images:
            message_parts.append("\n[User has also provided images]")

        return "\n".join(message_parts).strip()

    def _to_messages(self, input_data: BatchInput) -> List[Dict[str, Any]]:
        """Prepare messages for LLM API call."""
        messages = self._memory.copy()
        user_content = []
        text_prompt = self._to_text_prompt(input_data)

        if text_prompt:
            user_content.append({"type": "text", "text": text_prompt})

        if input_data.images:
            image_added = False
            for img_data in input_data.images:
                if isinstance(img_data.data, str) and img_data.data.startswith(
                    "data:image"
                ):
                    user_content.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": img_data.data, "detail": "auto"},
                        }
                    )
                    image_added = True
                else:
                    logger.error(
                        f"Invalid image data format: {type(img_data.data)}. Skipping."
                    )

            if not image_added and not text_prompt:
                logger.warning(
                    "User input contains images but none could be processed."
                )

        if user_content:
            user_message = {"role": "user", "content": user_content}
            messages.append(user_message)

            skip_memory = False
            if input_data.metadata and input_data.metadata.get("skip_memory", False):
                skip_memory = True

            if not skip_memory:
                self._add_message(
                    text_prompt if text_prompt else "[User provided image(s)]", "user"
                )
        else:
            logger.warning("No content generated for user message.")

        return messages

    async def _chat_with_fast_model(
        self, input_data: BatchInput
    ) -> AsyncIterator[Union[str, Dict[str, Any]]]:
        """
        Handle conversation with the fast model (no tools).

        Args:
            input_data: User input

        Yields:
            Text chunks or dict events
        """
        self._stats["fast_model_calls"] += 1
        logger.debug("Using FAST conversational model")

        messages = self._to_messages(input_data)
        token_stream = self._fast_llm.chat_completion(messages, self._system)

        complete_response = ""
        async for event in token_stream:
            text_chunk = ""
            if isinstance(event, dict) and event.get("type") == "text_delta":
                text_chunk = event.get("text", "")
            elif isinstance(event, str):
                text_chunk = event
            else:
                continue

            if text_chunk:
                yield text_chunk
                complete_response += text_chunk

        if complete_response:
            self._add_message(complete_response, "assistant")

    async def _chat_with_tool_model(
        self, input_data: BatchInput
    ) -> AsyncIterator[Union[str, Dict[str, Any]]]:
        """
        Handle conversation with the tool model.

        ARCHITECTURE:
        1. Yield immediate acknowledgment
        2. Tool model executes tools and yields status updates (for UI)
        3. Conversation model generates natural Italian response from results

        Args:
            input_data: User input

        Yields:
            Text chunks, tool events, or dict events
        """
        self._stats["tool_model_calls"] += 1
        logger.debug("Routing to TOOL model")

        # Get user query text
        user_text = self._to_text_prompt(input_data)

        # Yield acknowledgment immediately (if enabled)
        if self._enable_tool_acknowledgment:
            acknowledgment = self._get_template_acknowledgment(user_text)
            if acknowledgment:
                logger.info(f"💬 Yielding acknowledgment: '{acknowledgment}'")
                yield acknowledgment

        # Determine available tools
        tools = None
        if isinstance(self._tool_llm, ClaudeAsyncLLM):
            tools = self._formatted_tools_claude
            # Use Claude interaction loop
            async for output in self._claude_tool_interaction_loop(
                self._to_messages(input_data), tools
            ):
                yield output
            return
        elif isinstance(self._tool_llm, OpenAICompatibleAsyncLLM):
            tools = self._formatted_tools_openai
        else:
            tools = self._formatted_tools_openai  # Default to OpenAI format

        if not tools:
            logger.error("No tools available for tool calling!")
            yield "Mi dispiace, non ho accesso agli strumenti necessari."
            return

        # Prepare messages for tool model
        messages = self._to_messages(input_data)

        # Limit conversation history to prevent confusion from old tool results
        # Keep last 6 messages (3 exchanges) for context like "turn back on the lights"
        # This prevents the model from getting confused by old GetLiveContext results
        if len(messages) > 6:
            truncated_messages = messages[-6:]
            logger.info(f"🔧 Truncated tool model history from {len(messages)} to {len(truncated_messages)} messages to reduce confusion from old context")
            messages = truncated_messages

        # Use OpenAI tool interaction loop (which yields status updates)
        async for output in self._openai_tool_interaction_loop(messages, tools):
            yield output

    async def _claude_tool_interaction_loop(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> AsyncIterator[Union[str, Dict[str, Any]]]:
        """Claude tool interaction (simplified from BasicMemoryAgent)."""
        # For now, delegate to simple prompt mode
        # Full Claude tool support can be added later
        logger.warning(
            "Claude tool mode not fully implemented in DualModelAgent, using prompt mode"
        )
        async for output in self._tool_prompt_mode(messages):
            yield output


    def _filter_tools_by_query(self, user_query: str, all_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Filter tools based on user query to help the model focus on relevant tools.

        Args:
            user_query: The user's query text
            all_tools: All available tools

        Returns:
            Filtered list of relevant tools
        """
        import re
        user_query_lower = user_query.lower()

        # Search-related keywords (Italian and English)
        search_keywords = ["cerca", "search", "meteo", "weather", "notizie", "news", "trova", "find"]

        # Home control keywords
        home_keywords = ["accendi", "spegni", "luce", "light", "switch", "interruttore", "temperatura", "climate", "tapparella", "cover", "apri", "chiudi"]

        # Time keywords - use word boundaries to avoid matching "ora" in "allora"
        time_keywords = ["che ora", "che ore", "orario", "quando"]

        # Helper function for word boundary matching
        def has_keyword(text, keywords):
            for keyword in keywords:
                # Use word boundaries for single words, simple substring for phrases
                if " " in keyword:
                    if keyword in text:
                        return True
                else:
                    pattern = r"\b" + re.escape(keyword) + r"\b"
                    if re.search(pattern, text):
                        return True
            return False

        # Determine intent using word boundary matching
        is_search = has_keyword(user_query_lower, search_keywords)
        is_home = has_keyword(user_query_lower, home_keywords)
        is_time = has_keyword(user_query_lower, time_keywords)

        # If it's clearly a search query, ONLY return search and time tools
        if is_search and not is_home:
            filtered = []
            for tool in all_tools:
                tool_name = tool.get("function", {}).get("name", "").lower()
                # Include search tools, exclude Home Assistant tools
                if "search" in tool_name or "ddg" in tool_name:
                    filtered.append(tool)
                elif "time" in tool_name or "timezone" in tool_name:
                    filtered.append(tool)
                elif tool_name.startswith("hass"):
                    continue  # Skip Home Assistant tools for search queries

            if filtered:
                tool_names = [t.get("function", {}).get("name") for t in filtered]
                logger.info(f"🔍 Filtered {len(filtered)}/{len(all_tools)} tools for search query. Tools: {tool_names}")
                return filtered
            else:
                logger.warning(f"⚠️  No search tools found! Falling back to all tools.")

        # If it's clearly a time query, prioritize time tools
        if is_time and not is_home and not is_search:
            filtered = []
            for tool in all_tools:
                tool_name = tool.get("function", {}).get("name", "").lower()
                if "time" in tool_name or "timezone" in tool_name:
                    filtered.append(tool)

            if filtered:
                tool_names = [t.get("function", {}).get("name") for t in filtered]
                logger.info(f"⏰ Filtered {len(filtered)}/{len(all_tools)} tools for time query. Tools: {tool_names}")
                return filtered

        # Otherwise, return all tools (for home control or ambiguous queries)
        return all_tools

    async def _openai_tool_interaction_loop(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> AsyncIterator[Union[str, Dict[str, Any]]]:
        """OpenAI tool interaction (simplified from BasicMemoryAgent)."""
        # Filter tools based on user query to help the model focus
        user_query = messages[-1].get("content", [])
        if isinstance(user_query, list):
            user_text = " ".join([item.get("text", "") for item in user_query if item.get("type") == "text"])
        else:
            user_text = str(user_query)

        filtered_tools = self._filter_tools_by_query(user_text, tools)

        # Add critical tool usage guidance at the TOP of the system prompt
        tool_guidance = """🚨 CRITICAL TOOL USAGE RULES - FOLLOW EXACTLY! 🚨

═══════════════════════════════════════════════════════════════════════
⚠️ HOME ASSISTANT RULE #1 - MANDATORY TWO-STEP PROCESS ⚠️
═══════════════════════════════════════════════════════════════════════

FOR ANY Home Assistant command (HassTurnOn, HassTurnOff, HassLightSet):

YOU MUST CALL GetLiveContext() FIRST - NO EXCEPTIONS!

Why? Because device names in Home Assistant are EXACT and TECHNICAL.
- "luci" is NOT a device name
- "luce camera" is NOT a device name
- "speaker" is NOT a device name

You MUST get the REAL names from GetLiveContext first!

MANDATORY PROCESS:
1️⃣ User says "spegni le luci" or "accendi speaker"
2️⃣ YOU: Call GetLiveContext() - NO parameters needed
3️⃣ READ the results carefully to find matching devices
4️⃣ YOU: Call HassTurnOn/Off with EXACT name and domain from results

❌ WRONG (will fail): HassTurnOn(name="speaker") - guessed name!
❌ WRONG (will fail): HassTurnOff(name="luci") - guessed name!
❌ WRONG (will fail): HassTurnOff(name="Luce Camera") - guessed name!

✅ RIGHT: GetLiveContext() → Read results → Use EXACT names

═══════════════════════════════════════════════════════════════════════
ITALIAN QUERY DISAMBIGUATION - CRITICAL!
═══════════════════════════════════════════════════════════════════════

⚠️ "tempo" has TWO meanings in Italian - CONTEXT MATTERS:

1. "che tempo fa" / "che tempo fa a [città]" = WEATHER (meteo)
   → Use search tool: search(query="meteo Roma") or search(query="weather Rome")
   → NEVER use get_current_time for weather queries!

2. "che ore sono" / "che ora è" = CLOCK TIME
   → Use get_current_time tool: get_current_time(timezone="Europe/Rome")
   → NEVER use search for time queries!

EXAMPLES - PAY ATTENTION:
❌ WRONG: "che tempo fa a Roma" → get_current_time() [NO! This is weather!]
✅ RIGHT: "che tempo fa a Roma" → search(query="meteo Roma") [YES!]

❌ WRONG: "che ore sono" → search(query="che ore sono") [NO! Use time tool!]
✅ RIGHT: "che ore sono" → get_current_time(timezone="Europe/Rome") [YES!]

═══════════════════════════════════════════════════════════════════════
FOR SEARCH QUERIES (cerca, meteo, weather, notizie, news, trova, find):
═══════════════════════════════════════════════════════════════════════
Use "search" or "ddg_search" tool to search the web for current information.

WEATHER QUERIES - All these are SEARCH, not TIME:
✅ "cerca meteo Roma" → search(query="meteo Roma")
✅ "che tempo fa" → search(query="meteo Italia")
✅ "che tempo fa a Milano" → search(query="meteo Milano")
✅ "previsioni del tempo" → search(query="previsioni meteo")
✅ "pioggia domani" → search(query="previsioni meteo domani")

NEWS QUERIES:
✅ "cerca notizie" → search(query="notizie Italia")
✅ "ultime notizie" → search(query="ultime notizie")

═══════════════════════════════════════════════════════════════════════
FOR TIME QUERIES (che ore sono, che ora è, orario):
═══════════════════════════════════════════════════════════════════════
Use "get_current_time" tool with timezone="Europe/Rome" for Italian users.

EXAMPLES:
✅ "che ore sono" → get_current_time(timezone="Europe/Rome")
✅ "che ora è" → get_current_time(timezone="Europe/Rome")
✅ "dimmi l'ora" → get_current_time(timezone="Europe/Rome")
✅ "orario attuale" → get_current_time(timezone="Europe/Rome")

═══════════════════════════════════════════════════════════════════════
FOR HOME ASSISTANT (accendi, spegni, luci, interruttore, switch):
═══════════════════════════════════════════════════════════════════════

⚠️ CRITICAL STEPS - FOLLOW EXACTLY IN THIS ORDER:

STEP 1: ALWAYS call GetLiveContext FIRST (no parameters needed)
  - This shows you ALL available devices with their correct names and domains
  - NEVER skip this step!

STEP 2: Look at the GetLiveContext results to find:
  - The EXACT device name (e.g., "Speaker Switch", "WLED", "Bedroom Light")
  - The EXACT domain (e.g., "switch", "light", "media_player")
  - Domain is TECHNICAL, not functional (speaker can be "switch", not "media_player"!)

STEP 3: Use HassTurnOn or HassTurnOff with:
  - name: EXACT name from GetLiveContext (copy it precisely!)
  - domain: EXACT domain from GetLiveContext (copy it precisely!)
  - DO NOT use device_class parameter - leave it empty!
  - DO NOT guess or modify names!

CORRECT EXAMPLES:
User: "accendi speaker switch"
1. Call GetLiveContext()
2. See result: {"name": "Speaker Switch", "domain": "switch"}
3. Call HassTurnOn(name="Speaker Switch", domain="switch") ✅

User: "accendi wled"
1. Call GetLiveContext()
2. See result: {"name": "WLED", "domain": "light"}
3. Call HassTurnOn(name="WLED", domain="light") ✅

WRONG EXAMPLES (DO NOT DO THIS):
❌ HassTurnOn(name="speaker", domain="media_player") - WRONG name and domain!
❌ HassTurnOn(name="Speaker Switch") - Missing domain!
❌ HassTurnOn(name="Speaker Switch", domain="switch", device_class="speaker") - Don't use device_class!

⚠️ NEVER GUESS! Always use GetLiveContext first, then use EXACT values from the results.

"""
        # Use a minimal technical system prompt for tool model (not the conversational persona)
        # This prevents confusion between being a "friendly assistant" and executing tools correctly
        tool_system_base = """You are a technical tool execution agent. Your ONLY job is to call the appropriate tools with correct parameters based on the user's request.

CRITICAL RULES:
1. Do NOT engage in conversation or provide text responses
2. Do NOT make assumptions about device names, locations, or parameters
3. ALWAYS call GetLiveContext first for Home Assistant commands
4. Use EXACT names and domains from GetLiveContext results
5. For weather queries ("che tempo fa"), use search tools, NOT time tools
6. For time queries ("che ore sono"), use get_current_time tool
7. Follow the tool guidance instructions exactly as written

Your output should ONLY be tool calls, nothing else."""

        current_system_prompt = (
            f"{tool_guidance}\n{tool_system_base}\n\n{self._mcp_prompt_string}"
            if self._mcp_prompt_string
            else f"{tool_guidance}\n{tool_system_base}"
        )

        stream = self._tool_llm.chat_completion(
            messages, current_system_prompt, tools=filtered_tools
        )
        pending_tool_calls = []
        current_turn_text = ""

        async for event in stream:
            if isinstance(event, str):
                current_turn_text += event
                yield event
            elif isinstance(event, list) and all(
                isinstance(tc, ToolCallObject) for tc in event
            ):
                # Tool calls detected
                pending_tool_calls = event
                break
            elif event == "__API_NOT_SUPPORT_TOOLS__":
                logger.warning(
                    "Tool LLM doesn't support native tools, switching to prompt mode"
                )
                async for output in self._tool_prompt_mode(messages):
                    yield output
                return

        # Execute tools if detected
        if pending_tool_calls and self._tool_executor:
            # Pre-validate tool calls before execution
            validation_failed = False
            validation_errors = []

            for tool_call in pending_tool_calls:
                is_valid, error_msg = ToolValidator.validate_tool_call(tool_call)
                if not is_valid:
                    validation_failed = True
                    validation_errors.append(error_msg)
                    tool_name = tool_call.name if hasattr(tool_call, "name") else tool_call.get("name", "unknown")
                    hint = ToolValidator.get_validation_hint(tool_name, error_msg)
                    logger.warning(f"❌ Pre-validation failed for {tool_name}: {error_msg}")
                    logger.info(f"💡 {hint}")

            # If validation failed, provide immediate feedback without executing
            if validation_failed:
                logger.error(f"⚠️  Tool validation failed. Providing feedback to model.")
                combined_error = "\n".join(validation_errors)

                # Create error message for the model
                validation_error_response = f"""Error di validazione prima dell'esecuzione:

{combined_error}

IMPORTANTE: Questi errori sono stati rilevati PRIMA dell'esecuzione. Correggi i parametri e riprova.

Suggerimenti:
1. Per Home Assistant: Chiama sempre GetLiveContext() prima per vedere i dispositivi disponibili
2. Per ricerche: Assicurati che il parametro 'query' sia presente
3. Per orari: Usa get_current_time con timezone='Europe/Rome'
4. NON usare il parametro 'device_class' per Home Assistant

Riprova con i parametri corretti."""

                # Add validation error to messages as tool response
                messages.append({
                    "role": "user",
                    "content": validation_error_response
                })

                # Let conversation model respond to the validation error
                final_stream = self._fast_llm.chat_completion(
                    messages, self._system
                )
                final_response = ""
                async for event in final_stream:
                    if isinstance(event, str):
                        final_response += event
                        yield event

                if final_response:
                    self._add_message(final_response, "assistant")
                return

            max_retries = 5
            retry_count = 0
            tool_results = []

            while retry_count <= max_retries:
                if retry_count > 0:
                    logger.warning(
                        f"🔄 Retry attempt {retry_count}/{max_retries} - asking tool model to adjust approach"
                    )
                else:
                    logger.info(f"Executing {len(pending_tool_calls)} tool calls")

                tool_executor_iterator = self._tool_executor.execute_tools(
                    tool_calls=pending_tool_calls,
                    caller_mode="OpenAI",
                )

                tool_results = []
                try:
                    while True:
                        update = await anext(tool_executor_iterator)
                        if update.get("type") == "final_tool_results":
                            tool_results = update.get("results", [])
                            break
                        else:
                            yield update
                except StopAsyncIteration:
                    logger.warning("Tool executor finished without final results")

                # Check if all tools succeeded
                all_succeeded = True
                if tool_results:
                    for result in tool_results:
                        content = result.get("content", "")
                        if isinstance(content, str) and content.startswith("Error:"):
                            all_succeeded = False
                            break

                # If all succeeded, break out of retry loop
                if all_succeeded and tool_results:
                    if retry_count > 0:
                        logger.info(
                            f"✅ Tool execution succeeded after {retry_count} retries"
                        )
                    break

                # If we haven't reached max retries, ask the tool model to try again with a different approach
                if retry_count < max_retries and not all_succeeded:
                    retry_count += 1
                    logger.warning(
                        f"⚠️  Tool execution failed, asking tool model to adjust approach ({retry_count}/{max_retries})"
                    )

                    # Add a small delay before retrying
                    delay = min(2 ** (retry_count - 1), 5)  # Max 5 seconds
                    await asyncio.sleep(delay)

                    # Add the error results to messages so the tool model can see what went wrong
                    messages.extend(tool_results)

                    # Check if the failed tool was a Home Assistant tool
                    failed_hass_tool = False
                    hass_tool_names = ["HassTurnOn", "HassTurnOff", "HassLightSet", "HassSetPosition",
                                       "HassMediaPlay", "HassMediaPause", "HassMediaNext", "HassMediaPrevious",
                                       "HassVacuumStart", "HassVacuumReturnToBase"]

                    for tool_call in pending_tool_calls:
                        tool_name = tool_call.name if hasattr(tool_call, 'name') else tool_call.get("name", "")
                        if tool_name in hass_tool_names:
                            # Check if this specific call failed
                            for result in tool_results:
                                result_tool_id = result.get("tool_call_id", "")
                                result_content = result.get("content", "")
                                call_id = tool_call.id if hasattr(tool_call, 'id') else tool_call.get("id", "")
                                if result_tool_id == call_id and isinstance(result_content, str) and result_content.startswith("Error:"):
                                    failed_hass_tool = True
                                    logger.warning(f"🏠 Home Assistant tool '{tool_name}' failed, will fetch device context")
                                    break
                        if failed_hass_tool:
                            break

                    # If a Home Assistant tool failed, automatically call GetLiveContext first
                    if failed_hass_tool and self._tool_executor:
                        logger.info("🔄 Automatically calling GetLiveContext to get fresh device information")

                        # Create a GetLiveContext tool call
                        context_tool_call = ToolCallObject(
                            id="auto_context_call",
                            name="GetLiveContext",
                            arguments={}
                        )

                        # Execute GetLiveContext
                        try:
                            context_executor = self._tool_executor.execute_tools(
                                tool_calls=[context_tool_call],
                                caller_mode="OpenAI"
                            )

                            context_results = []
                            async for update in context_executor:
                                if update.get("type") == "final_tool_results":
                                    context_results = update.get("results", [])
                                    break

                            # Add GetLiveContext results to messages
                            if context_results:
                                messages.extend(context_results)
                                context_content = context_results[0].get("content", "") if context_results else ""
                                logger.info(f"✅ GetLiveContext returned device information: {context_content[:200]}...")

                                # Add specific retry guidance for Home Assistant with fresh context
                                retry_guidance = f"""The previous Home Assistant tool call failed. Error details are above.

I have now called GetLiveContext for you - the results are in the tool response above showing ALL available devices.

CRITICAL RETRY INSTRUCTIONS (Attempt {retry_count}/{max_retries}):
1. READ the GetLiveContext results carefully - they show the EXACT device names and domains available
2. Find the device the user wants to control in the GetLiveContext results
3. Use the EXACT name and domain from GetLiveContext (copy them precisely!)
4. DO NOT use device_class parameter
5. DO NOT guess or make up device names

Example:
- If GetLiveContext shows: {{"name": "Speaker Switch", "domain": "switch"}}
- Use: HassTurnOn(name="Speaker Switch", domain="switch")

Now retry the Home Assistant command using the EXACT information from GetLiveContext above."""
                            else:
                                logger.warning("⚠️  GetLiveContext returned no results")
                                retry_guidance = f"""The previous tool call failed. Error details are in the tool results above.
Attempt {retry_count}/{max_retries}: Please analyze the error and try again with a different approach."""
                        except Exception as e:
                            logger.error(f"❌ Failed to execute GetLiveContext: {e}")
                            retry_guidance = f"""The previous tool call failed. Error details are in the tool results above.
Attempt {retry_count}/{max_retries}: Please analyze the error and try again with a different approach."""
                    else:
                        # Generic retry guidance for non-Home Assistant tools
                        retry_guidance = f"""The previous tool call failed. Error details are in the tool results above.
Attempt {retry_count}/{max_retries}: Please analyze the error and try again with:
- Different parameter values if the error suggests wrong input
- A different tool if this one isn't working
- A modified approach based on the error message
Be creative and adjust your strategy based on what failed."""

                    messages.append({"role": "user", "content": retry_guidance})

                    # Let the tool model generate a new tool call based on the error
                    current_system_prompt = (
                        f"{self._system}\n\n{self._mcp_prompt_string}"
                        if self._mcp_prompt_string
                        else self._system
                    )

                    # Get available tools from the tool manager (use filtered tools for consistency)
                    available_tools = filtered_tools

                    stream = self._tool_llm.chat_completion(
                        messages, current_system_prompt, tools=available_tools
                    )

                    pending_tool_calls = []
                    current_turn_text = ""

                    async for event in stream:
                        if isinstance(event, str):
                            current_turn_text += event
                        elif isinstance(event, list):
                            # Tool calls detected (list of ToolCallObject)
                            pending_tool_calls = event
                            logger.info(
                                f"🔧 Tool model generated new tool call strategy for retry {retry_count}"
                            )
                            break

                    if not pending_tool_calls:
                        logger.error(
                            "Tool model couldn't generate a new approach, giving up"
                        )
                        break
                else:
                    if retry_count == max_retries:
                        logger.error(
                            f"❌ Tool execution failed after {max_retries} retries, giving up"
                        )
                    break

            # Add tool results to memory and hand off to conversation model for natural response
            if tool_results:
                # Log tool results for debugging
                logger.debug(f"📋 Received {len(tool_results)} tool results")
                has_errors = False
                has_getlivecontext = False
                for idx, result in enumerate(tool_results):
                    content = result.get("content", "")
                    tool_name = pending_tool_calls[idx].function.name if idx < len(pending_tool_calls) else ""

                    if tool_name == "GetLiveContext" and not content.startswith("Error:"):
                        has_getlivecontext = True
                        logger.info("🔍 GetLiveContext succeeded - will prompt for follow-up action")

                    if isinstance(content, str) and content.startswith("Error:"):
                        logger.warning(
                            f"  ❌ Result {idx} (FAILED): {content[:200]}..."
                            if len(content) > 200
                            else f"  ❌ Result {idx} (FAILED): {content}"
                        )
                        has_errors = True
                    else:
                        logger.debug(
                            f"  ✅ Result {idx}: {content[:200]}..."
                            if len(content) > 200
                            else f"  ✅ Result {idx}: {content}"
                        )

                if has_errors:
                    logger.warning(
                        "⚠️  Some tool executions failed - conversation model will be informed"
                    )

                messages.extend(tool_results)

                # CRITICAL: If GetLiveContext was called successfully, prompt the tool model to make the ACTUAL control call
                if has_getlivecontext and not has_errors and len(pending_tool_calls) == 1:
                    logger.info("🔄 GetLiveContext completed - prompting tool model to use the results for the actual action")

                    # Get the original user request text
                    original_request = ""
                    for msg in reversed(messages[:-len(tool_results)]):  # Skip the tool results we just added
                        if msg.get("role") == "user":
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                original_request = " ".join([item.get("text", "") for item in content if item.get("type") == "text"])
                            else:
                                original_request = str(content)
                            break

                    follow_up_prompt = f"""✅ GetLiveContext has returned the device information above.

NOW YOU MUST COMPLETE THE ORIGINAL REQUEST: "{original_request}"

CRITICAL - TAKE ACTION NOW:
1. READ the GetLiveContext results above carefully
2. Find the device mentioned in the original request ("{original_request}")
3. Call the appropriate Home Assistant tool (HassTurnOn, HassTurnOff, etc.) with EXACT name and domain from GetLiveContext
4. DO NOT stop after GetLiveContext - you must complete the user's request!

Example:
- User said: "spegni le luci"
- GetLiveContext showed: {{"name": "Speaker Switch", "domain": "switch"}}
- YOU MUST NOW CALL: HassTurnOff(name="Speaker Switch", domain="switch")

Make the tool call NOW to complete the user's request."""

                    messages.append({"role": "user", "content": follow_up_prompt})

                    # Use the same system prompt with tool guidance
                    tool_system_base = """You are a technical tool execution agent. Your ONLY job is to call the appropriate tools with correct parameters based on the user's request.

CRITICAL RULES:
1. Do NOT engage in conversation or provide text responses
2. Do NOT make assumptions about device names, locations, or parameters
3. ALWAYS call GetLiveContext first for Home Assistant commands
4. Use EXACT names and domains from GetLiveContext results
5. For weather queries ("che tempo fa"), use search tools, NOT time tools
6. For time queries ("che ore sono"), use get_current_time tool
7. Follow the tool guidance instructions exactly as written

Your output should ONLY be tool calls, nothing else."""

                    follow_up_system_prompt = (
                        f"{tool_system_base}\n\n{self._mcp_prompt_string}"
                        if self._mcp_prompt_string
                        else tool_system_base
                    )

                    # Call the tool model again to make the follow-up action
                    follow_up_stream = self._tool_llm.chat_completion(
                        messages, follow_up_system_prompt, tools=filtered_tools
                    )

                    follow_up_tool_calls = []
                    async for event in follow_up_stream:
                        if isinstance(event, list) and all(isinstance(tc, ToolCallObject) for tc in event):
                            follow_up_tool_calls = event
                            logger.info(f"🎯 Tool model generated follow-up action: {[tc.function.name for tc in event]}")
                            break

                    # Execute the follow-up tool calls
                    if follow_up_tool_calls and self._tool_executor:
                        logger.info(f"⚡ Executing follow-up action: {len(follow_up_tool_calls)} tool calls")

                        follow_up_executor = self._tool_executor.execute_tools(
                            tool_calls=follow_up_tool_calls,
                            caller_mode="OpenAI"
                        )

                        follow_up_results = []
                        try:
                            while True:
                                update = await anext(follow_up_executor)
                                if update.get("type") == "final_tool_results":
                                    follow_up_results = update.get("results", [])
                                    break
                                else:
                                    yield update
                        except StopAsyncIteration:
                            logger.warning("Follow-up tool executor finished without final results")

                        # Add follow-up results to messages and update tool_results
                        if follow_up_results:
                            messages.extend(follow_up_results)
                            tool_results.extend(follow_up_results)
                            logger.info(f"✅ Follow-up action completed with {len(follow_up_results)} results")
                        else:
                            logger.warning("⚠️  Follow-up tool executor returned no results")
                    else:
                        logger.warning("⚠️  Tool model did not generate a follow-up action after GetLiveContext")

                # IMPORTANT: After follow-up completes, NOW we can pass everything to conversation model
                # Add guidance for the conversation model to check tool results
                retry_info = (
                    f" (after {retry_count} retry attempts)" if retry_count > 0 else ""
                )

                # If GetLiveContext follow-up occurred, provide specific guidance
                if has_getlivecontext and len(tool_results) > 1:
                    response_guidance = f"""IMPORTANT: Check ALL tool execution results carefully.

Multiple tools were executed:
1. GetLiveContext - returned device information
2. The ACTUAL action (HassTurnOn/HassTurnOff/etc.) - this is what the user requested

YOU MUST respond based on the FINAL action result (the last tool result), NOT the GetLiveContext result!

Look at the LAST tool result in the conversation above:
- If it starts with "Error:", the action FAILED{retry_info} - inform the user about the failure
- If it doesn't contain errors, the action SUCCEEDED - acknowledge what was done (e.g., "Ho spento le luci!")
- DO NOT mention GetLiveContext in your response - the user doesn't care about that internal step
- Focus ONLY on confirming whether their request (turn on/off lights, etc.) was completed

Be honest and clear about what actually happened with the user's requested action."""
                else:
                    response_guidance = f"""IMPORTANT: Check the tool execution results carefully.
Look at the tool results in the conversation above (role: "tool").
- If the result content starts with "Error:", the tool FAILED{retry_info} - inform the user about the failure
- If the result doesn't contain errors, the tool succeeded - acknowledge what was done
- NEVER say something succeeded when the tool result shows "Error:"
- Be honest and clear about what actually happened"""

                # IMPORTANT: Use the CONVERSATION model (fast_llm) to generate the response
                # This ensures the response has the full personality and conversational style
                # The tool model (tool_llm) was only for executing tools accurately
                conversation_system = f"{self._system}\n\n{response_guidance}"

                # Debug: Log what we're sending to conversation model
                logger.debug(f"📨 Sending {len(messages)} messages to conversation model for final response")
                logger.debug(f"Last 3 messages: {messages[-3:] if len(messages) >= 3 else messages}")

                final_stream = self._fast_llm.chat_completion(
                    messages, conversation_system
                )
                final_response = ""
                async for event in final_stream:
                    if isinstance(event, str):
                        final_response += event
                        yield event

                if final_response:
                    self._add_message(final_response, "assistant")
        else:
            if current_turn_text:
                self._add_message(current_turn_text, "assistant")

    async def _tool_prompt_mode(
        self, messages: List[Dict[str, Any]]
    ) -> AsyncIterator[Union[str, Dict[str, Any]]]:
        """Tool calling via prompt mode with JSON detection."""
        current_system_prompt = (
            f"{self._system}\n\n{self._mcp_prompt_string}"
            if self._mcp_prompt_string
            else self._system
        )

        stream = self._tool_llm.chat_completion(
            messages, current_system_prompt, tools=None
        )
        current_turn_text = ""

        if self._json_detector:
            self._json_detector.reset()

        async for event in stream:
            if isinstance(event, str):
                current_turn_text += event

                # Check for JSON tool calls in the stream
                if self._json_detector:
                    potential_json = self._json_detector.process_chunk(event)
                    if potential_json and self._tool_executor:
                        # Found a tool call!
                        logger.info("Detected tool call in prompt mode")
                        self._add_message(current_turn_text, "assistant")

                        parsed_tools = (
                            self._tool_executor.process_tool_from_prompt_json(
                                potential_json
                            )
                        )
                        if parsed_tools:
                            max_retries = 5
                            retry_count = 0
                            tool_results = []

                            while retry_count <= max_retries:
                                if retry_count > 0:
                                    logger.warning(
                                        f"🔄 Retry attempt {retry_count}/{max_retries} for failed tool execution (prompt mode)"
                                    )

                                tool_executor_iterator = (
                                    self._tool_executor.execute_tools(
                                        tool_calls=parsed_tools,
                                        caller_mode="Prompt",
                                    )
                                )

                                tool_results = []
                                try:
                                    while True:
                                        update = await anext(tool_executor_iterator)
                                        if update.get("type") == "final_tool_results":
                                            tool_results = update.get("results", [])
                                            break
                                        else:
                                            yield update
                                except StopAsyncIteration:
                                    pass

                                # Check if all tools succeeded
                                all_succeeded = True
                                if tool_results:
                                    for result in tool_results:
                                        content = result.get("content", "")
                                        if (
                                            isinstance(content, str)
                                            and "Error:" in content
                                        ):
                                            all_succeeded = False
                                            break

                                # If all succeeded, break out of retry loop
                                if all_succeeded and tool_results:
                                    if retry_count > 0:
                                        logger.info(
                                            f"✅ Tool execution succeeded after {retry_count} retries (prompt mode)"
                                        )
                                    break

                                # If we haven't reached max retries, try again
                                if retry_count < max_retries and not all_succeeded:
                                    retry_count += 1
                                    logger.warning(
                                        f"⚠️  Tool execution failed, retrying... ({retry_count}/{max_retries})"
                                    )
                                    # Add a small delay before retrying (exponential backoff)
                                    delay = min(2**retry_count, 10)  # Max 10 seconds
                                    logger.debug(f"⏱️  Waiting {delay}s before retry")
                                    await asyncio.sleep(delay)
                                else:
                                    if retry_count == max_retries:
                                        logger.error(
                                            f"❌ Tool execution failed after {max_retries} retries, giving up (prompt mode)"
                                        )
                                    break

                            # Continue conversation with tool results
                            if tool_results:
                                result_strings = [
                                    res.get("content", "") for res in tool_results
                                ]
                                combined_results = "\n".join(result_strings)
                                messages.append(
                                    {"role": "user", "content": combined_results}
                                )

                                # Check for errors in tool results
                                has_errors = any(
                                    "Error:" in str(res.get("content", ""))
                                    for res in tool_results
                                )
                                if has_errors:
                                    logger.warning(
                                        "⚠️  Tool execution failed in prompt mode"
                                    )

                                # Add error handling guidance for conversation model
                                retry_info = (
                                    f" (after {retry_count} retry attempts)"
                                    if retry_count > 0
                                    else ""
                                )
                                response_guidance = f"""IMPORTANT: Check the tool execution results carefully.
- If the result contains "Error:", the tool FAILED{retry_info} - inform the user about the failure
- If no errors, the tool succeeded - acknowledge what was done
- Be honest about failures"""

                                # Get final response from conversation model
                                conversation_system = (
                                    f"{self._system}\n\n{response_guidance}"
                                )

                                # Debug: Log what we're sending to conversation model
                                logger.debug(f"📨 [Prompt Mode] Sending {len(messages)} messages to conversation model")
                                logger.debug(f"Last 3 messages: {messages[-3:] if len(messages) >= 3 else messages}")

                                final_stream = self._fast_llm.chat_completion(
                                    messages, conversation_system
                                )
                                final_response = ""
                                async for final_event in final_stream:
                                    if isinstance(final_event, str):
                                        final_response += final_event
                                        yield final_event

                                if final_response:
                                    self._add_message(final_response, "assistant")
                            return

                yield event

        if current_turn_text:
            self._add_message(current_turn_text, "assistant")

    def _create_chat_pipeline(self) -> Callable:
        """Create the chat pipeline with decorators."""

        @tts_filter(self._tts_preprocessor_config)
        @display_processor()
        @actions_extractor(self._live2d_model)
        @sentence_divider(
            faster_first_response=self._faster_first_response,
            segment_method=self._segment_method,
            valid_tags=[],
        )
        async def chat_with_routing(
            input_data: BatchInput,
        ) -> AsyncIterator[Union[str, Dict[str, Any]]]:
            """Route to appropriate model based on intent detection."""
            self.reset_interrupt()

            # Get user input text
            user_text = self._to_text_prompt(input_data)

            # Detect intent
            if self._intent_detection_method == "llm" and self._intent_router:
                # Use LLM-based intent classification
                logger.debug(
                    f"🔍 Using LLM-based intent classification for: '{user_text[:50]}...'"
                )
                needs_tool = await self._intent_router.should_use_tools(user_text)
                self._stats["intent_detections"].append(
                    {"text": user_text, "needs_tool": needs_tool, "method": "llm"}
                )
            else:
                # Use keyword-based detection
                logger.debug(
                    f"🔍 Using keyword-based intent detection for: '{user_text[:50]}...'"
                )
                needs_tool, matched_keywords = self._detect_tool_intent(user_text)
                if matched_keywords:
                    self._stats["intent_detections"].append(
                        {
                            "text": user_text,
                            "needs_tool": needs_tool,
                            "keywords": matched_keywords,
                            "method": "keyword",
                        }
                    )

            # Route to appropriate model
            if needs_tool and self._use_mcpp:
                logger.info(f"🔧 Routing to TOOL model (use_mcpp={self._use_mcpp})")
                async for output in self._chat_with_tool_model(input_data):
                    yield output
            else:
                logger.info(
                    f"💬 Routing to CONVERSATION model (needs_tool={needs_tool}, use_mcpp={self._use_mcpp})"
                )
                async for output in self._chat_with_fast_model(input_data):
                    yield output

        return chat_with_routing

    async def chat(
        self, input_data: BatchInput
    ) -> AsyncIterator[Union[SentenceOutput, Dict[str, Any]]]:
        """Main chat interface."""
        chat_pipeline = self._create_chat_pipeline()
        async for output in chat_pipeline(input_data):
            yield output

    def reset_interrupt(self) -> None:
        """Reset interrupt flag."""
        self._interrupt_handled = False

    def get_stats(self) -> Dict[str, Any]:
        """Get routing statistics for debugging."""
        return self._stats.copy()
