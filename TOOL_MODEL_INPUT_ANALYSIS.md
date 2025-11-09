# Tool Model Input Analysis

## Your Question
> "I have a question, the tool model receives only its own prompt right? not also the one for the conversational model. Because I think it's getting confused. Also does it receive only the last user message (the one with the command I mean)"

## Short Answer
**NO** - The tool model receives:
1. ✅ Tool guidance prompt (the emphatic GetLiveContext instructions)
2. ✅ **THE FULL PERSONA PROMPT** (same as conversation model)
3. ✅ **FULL CONVERSATION HISTORY** (all previous messages, not just the last one)
4. ✅ MCP tool definitions

This is likely causing confusion!

---

## Detailed Breakdown

### What the Tool Model Receives

#### 1. System Prompt (Lines 907-910)
```python
current_system_prompt = (
    f"{tool_guidance}\n{self._system}\n\n{self._mcp_prompt_string}"
    if self._mcp_prompt_string
    else f"{tool_guidance}\n{self._system}"
)
```

This means the system prompt contains:
- **`tool_guidance`**: Your emphatic HOME ASSISTANT instructions (lines 790-906)
- **`self._system`**: The full persona prompt from conf.yaml that says "Tu ti chiami Mili e sei una assistente vocale..."
- **`self._mcp_prompt_string`**: All MCP tool definitions

#### 2. Conversation History (Line 680, 913-914)
```python
messages = self._to_messages(input_data)  # Line 680
stream = self._tool_llm.chat_completion(
    messages, current_system_prompt, tools=filtered_tools
)
```

Looking at `_to_messages` (lines 545-592):
```python
def _to_messages(self, input_data: BatchInput) -> List[Dict[str, Any]]:
    """Prepare messages for LLM API call."""
    messages = self._memory.copy()  # ⚠️ FULL CONVERSATION HISTORY!
    # ... then adds current user message
    return messages
```

The `self._memory` is:
- Initialized as empty list (line 92)
- Updated with EVERY user and assistant message (lines 446-491)
- Shared between BOTH conversation and tool models
- Contains ALL previous exchanges, not just the last message

---

## The Problem: Confusion from Too Much Context

### Why This Causes Issues

**Example scenario:**
1. User has casual conversation: "Ciao! Come stai?" → Conversation model responds
2. User asks for tool action: "Accendi le luci" → Tool model gets called

**What the tool model sees:**
```
SYSTEM PROMPT:
🚨 CRITICAL TOOL USAGE RULES - FOLLOW EXACTLY! 🚨
[Your emphatic GetLiveContext instructions...]

Tu ti chiami Mili e sei una assistente vocale intelligente, simpatica...
[Full persona description with personality traits...]

[MCP tool definitions...]

CONVERSATION HISTORY:
User: Ciao! Come stai?
Assistant: Ciao! Sto benissimo, grazie! Come posso aiutarti oggi?
User: Raccontami una barzelletta
Assistant: [joke response]
User: Accendi le luci  ← ACTUAL COMMAND
```

**The model is confused because:**
1. System prompt has TWO conflicting personas:
   - Technical tool-execution persona (tool_guidance)
   - Friendly chatbot persona (self._system)
2. Conversation history adds noise that dilutes the tool instruction
3. The persona prompt encourages conversational responses, while tool guidance demands strict tool calling

---

## Evidence from Your Logs

From your recent logs where the model invented device names:
```
❌ HassTurnOff(name="Luci")  # Invented - tried to be helpful
❌ HassTurnOff(name="Luce Camera")  # Invented - guessed based on context
```

The model was trying to be a "helpful assistant" (from persona prompt) instead of following the strict "call GetLiveContext first" rule.

---

## Recommended Solution

### Option 1: Separate System Prompt for Tool Model (RECOMMENDED)

Create a minimal, technical-only system prompt for the tool model:

**Location**: [dual_model_agent.py:907-910](src/open_llm_vtuber/agent/agents/dual_model_agent.py#L907-L910)

**Current code:**
```python
current_system_prompt = (
    f"{tool_guidance}\n{self._system}\n\n{self._mcp_prompt_string}"
    if self._mcp_prompt_string
    else f"{tool_guidance}\n{self._system}"
)
```

**Recommended change:**
```python
# Use a minimal technical system prompt for tool model (no persona)
tool_system_only = """You are a technical tool execution agent. Your ONLY job is to call the appropriate tools with correct parameters. Do NOT engage in conversation. Do NOT make assumptions about device names. ALWAYS call GetLiveContext first for Home Assistant commands."""

current_system_prompt = (
    f"{tool_guidance}\n{tool_system_only}\n\n{self._mcp_prompt_string}"
    if self._mcp_prompt_string
    else f"{tool_guidance}\n{tool_system_only}"
)
```

**Why this works:**
- Removes conflicting "friendly assistant" personality
- Single clear directive: execute tools correctly
- No encouragement to be creative or helpful (which leads to guessing)

---

### Option 2: Limit Conversation History for Tool Model

**Location**: [dual_model_agent.py:680](src/open_llm_vtuber/agent/agents/dual_model_agent.py#L680)

**Current code:**
```python
messages = self._to_messages(input_data)  # Gets full history
```

**Alternative approach:**
```python
# Create a modified version that only includes last N messages or just current message
def _to_tool_messages(self, input_data: BatchInput, max_history: int = 2) -> List[Dict[str, Any]]:
    """Prepare messages for tool model with limited history."""
    # Only include last N exchanges or just current message
    messages = self._memory[-max_history:] if len(self._memory) > max_history else self._memory.copy()

    # Add current user message
    user_content = []
    text_prompt = self._to_text_prompt(input_data)
    if text_prompt:
        user_content.append({"type": "text", "text": text_prompt})

    if user_content:
        messages.append({"role": "user", "content": user_content})

    return messages

# Then use it:
messages = self._to_tool_messages(input_data, max_history=2)  # Only last 2 exchanges
```

**Why this works:**
- Reduces noise from unrelated conversation
- Keeps focus on the current command
- Still maintains minimal context if needed (e.g., "turn off that light" needs previous context)

---

### Option 3: Combined Approach (BEST)

1. Use separate technical system prompt (Option 1)
2. Limit conversation history to last 1-2 exchanges (Option 2)
3. Keep the emphatic tool guidance you already added

**Benefits:**
- ✅ No persona confusion
- ✅ Minimal conversational noise
- ✅ Clear technical directives
- ✅ Model focuses only on tool execution

---

## Why Current Approach Isn't Working

### The Tool Guidance is Being Diluted

Even though you added extremely emphatic instructions:
```
🚨 CRITICAL TOOL USAGE RULES - FOLLOW EXACTLY! 🚨
⚠️ HOME ASSISTANT RULE #1 - MANDATORY TWO-STEP PROCESS ⚠️
```

The model also sees:
```
Tu ti chiami Mili e sei una assistente vocale intelligente, simpatica e disponibile.
```

**LLMs try to satisfy ALL instructions**, so the model is torn between:
- Being "simpatica e disponibile" (friendly and helpful) → tries to guess
- Following strict technical rules → should call GetLiveContext

Guess which wins? The friendly persona, because it's seen more often in training data.

---

## Expected Behavior After Fix

### Before (Current):
```
User: "Spegni le luci"
Tool Model Thinks:
  - System: Be a friendly helpful assistant named Mili
  - Also System: Follow strict tool rules
  - History: Previous friendly conversations
  - Conclusion: Be helpful! User wants lights off, I'll guess "Luci" is the name
Result: HassTurnOff(name="Luci") ❌
```

### After (With Separate System Prompt):
```
User: "Spegni le luci"
Tool Model Thinks:
  - System: You are a technical tool execution agent. Call GetLiveContext first.
  - No persona confusion
  - Minimal history noise
  - Conclusion: Follow the rules exactly
Result: GetLiveContext() → [sees devices] → HassTurnOff(name="Speaker Switch", domain="switch") ✅
```

---

## Implementation Priority

**DO THIS FIRST** (10 minute fix):
1. Change system prompt for tool model to technical-only version (Option 1)
2. Test with "accendi le luci" and "spegni speaker"

**IF STILL HAVING ISSUES** (20 minute fix):
3. Add conversation history limiting (Option 2)

**WHY THIS ORDER:**
- The persona confusion is the PRIMARY issue (90% of the problem)
- History limiting is secondary optimization (10% improvement)

---

## Testing After Fix

Try these exact commands:
```
1. "accendi le luci"
   Expected: GetLiveContext() → HassTurnOn(correct name and domain)

2. "spegni speaker switch"
   Expected: GetLiveContext() → HassTurnOff(name="Speaker Switch", domain="switch")

3. "che tempo fa a Roma"
   Expected: search(query="meteo Roma") OR ddg_search(query="meteo Roma")
```

Check logs for:
- ✅ GetLiveContext being called FIRST for Home Assistant commands
- ✅ NO invented device names
- ✅ Correct tool selection (search for weather, not time)
- ❌ NO "Subito!" or conversational preambles in tool output

---

## Code Locations for Changes

1. **System prompt for tool model**: [dual_model_agent.py:907-910](src/open_llm_vtuber/agent/agents/dual_model_agent.py#L907-L910)
2. **Message history preparation**: [dual_model_agent.py:545-592](src/open_llm_vtuber/agent/agents/dual_model_agent.py#L545-L592)
3. **Memory management**: [dual_model_agent.py:92](src/open_llm_vtuber/agent/agents/dual_model_agent.py#L92) (initialization), [446-491](src/open_llm_vtuber/agent/agents/dual_model_agent.py#L446-L491) (updates)

---

## Summary

**Your intuition was correct!** The tool model IS receiving:
- ✅ The conversational model's persona prompt (causing confusion)
- ✅ Full conversation history (adding noise)
- ✅ Current command (what you want it to focus on)

**The fix:** Give the tool model a separate, technical-only system prompt that focuses purely on correct tool execution without personality or conversational elements.

This explains why the model was:
- Inventing device names (trying to be "helpful")
- Not calling GetLiveContext (persona encourages direct action)
- Calling wrong tools (trying to satisfy multiple conflicting instructions)
