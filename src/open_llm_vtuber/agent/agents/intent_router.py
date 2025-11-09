"""
Intent Router - Uses a small, fast LLM to classify user intent.

This router determines whether user input requires tool calling (Home Assistant, etc.)
or can be handled by the conversational model.
"""

from typing import Literal
from loguru import logger
from ..stateless_llm.stateless_llm_interface import StatelessLLMInterface


IntentType = Literal["conversation", "tool_required"]


class IntentRouter:
    """
    Fast LLM-based intent classification for routing user queries.

    Uses a small model to classify whether the user wants:
    - General conversation
    - Tool usage (Home Assistant control, search, etc.)
    """

    def __init__(self, llm: StatelessLLMInterface):
        """
        Initialize the intent router.

        Args:
            llm: A fast, lightweight LLM for intent classification
        """
        self._llm = llm
        self._classification_prompt = self._build_classification_prompt()
        logger.info("IntentRouter initialized with LLM-based classification")

    @staticmethod
    def _build_classification_prompt() -> str:
        """Build the system prompt for intent classification."""
        return """Classify input as CONVERSATION or TOOL.

CONVERSATION = chat, greetings, questions about me, stories, jokes, general knowledge already known
TOOL = actions requiring external tools:
  - Control smart home devices (lights, switches, temperature)
  - Search web for current information (weather, news, facts)
  - Check current time/date
  - Get live data not in my knowledge

CRITICAL ITALIAN DISAMBIGUATION:

"tempo" has TWO different meanings - pay attention to context!

1. "tempo" = WEATHER (meteo) → TOOL
   - "che tempo fa" = what's the weather → TOOL
   - "che tempo fa a Roma" = weather in Rome → TOOL
   - "come è il tempo" = how's the weather → TOOL
   - "previsioni del tempo" = weather forecast → TOOL

2. "tempo" = TIME CONCEPT (not clock time) → Usually CONVERSATION
   - "quanto tempo ci vuole" = how much time does it take → CONVERSATION
   - "ho tempo libero" = I have free time → CONVERSATION
   - "tempo fa" (without "che") = time ago → CONVERSATION

3. CLOCK TIME queries → TOOL
   - "che ore sono" = what time is it → TOOL
   - "che ora è" = what's the time → TOOL
   - "dimmi l'ora" = tell me the time → TOOL

RULE: If you see "che tempo fa" or "tempo" with weather-related words → TOOL (weather search)
RULE: If you see "che ore" or "che ora" → TOOL (time check)

Examples:

"ciao"
CONVERSATION

"come stai?"
CONVERSATION

"grazie"
CONVERSATION

"raccontami qualcosa"
CONVERSATION

"chi sei?"
CONVERSATION

"cosa sai fare?"
CONVERSATION

"barzelletta"
CONVERSATION

"cos'è Python?" (general knowledge)
CONVERSATION

"quanto tempo ci vuole?" (abstract time concept)
CONVERSATION

"accendi luce"
TOOL

"spegni camera"
TOOL

"che ore sono" (CLOCK time)
TOOL

"che ora è" (CLOCK time)
TOOL

"che giorno è oggi"
TOOL

"dimmi l'ora" (CLOCK time)
TOOL

"temperatura soggiorno"
TOOL

"cerca meteo Roma"
TOOL

"search weather in Milan"
TOOL

"che tempo fa" (WEATHER, not time!)
TOOL

"che tempo fa a Roma" (WEATHER in Rome!)
TOOL

"che tempo fa oggi" (WEATHER today!)
TOOL

"come è il tempo" (WEATHER)
TOOL

"previsioni del tempo" (weather forecast)
TOOL

"cerca notizie"
TOOL

"search for"
TOOL

"find information about"
TOOL

"meteo Milano"
TOOL

"pioggia domani"
TOOL

DEFAULT: If unsure → CONVERSATION"""

    async def classify_intent(self, user_input: str) -> IntentType:
        """
        Classify user input to determine routing.

        Args:
            user_input: The user's text input

        Returns:
            "conversation" or "tool_required"
        """
        logger.debug(f"Classifying intent for: {user_input}")

        # Build classification message
        messages = [
            {"role": "user", "content": user_input}
        ]

        # Get classification from LLM
        response = ""
        async for token in self._llm.chat_completion(
            messages=messages,
            system=self._classification_prompt
        ):
            if isinstance(token, str):
                response += token

        # Parse response
        response_clean = response.strip().upper()

        if "TOOL" in response_clean:
            intent = "tool_required"
            logger.info(f"🔧 Intent classified as TOOL: '{user_input}'")
        else:
            intent = "conversation"
            logger.debug(f"💬 Intent classified as CONVERSATION: '{user_input}'")

        return intent

    async def should_use_tools(self, user_input: str) -> bool:
        """
        Convenience method that returns True if tools should be used.

        Args:
            user_input: The user's text input

        Returns:
            True if tools should be used, False otherwise
        """
        intent = await self.classify_intent(user_input)
        return intent == "tool_required"
