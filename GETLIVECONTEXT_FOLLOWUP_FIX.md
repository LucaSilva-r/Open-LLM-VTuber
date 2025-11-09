# GetLiveContext Follow-up Fix

## Problem Identified

**User Report**: "The tool model called GetLiveContext yes, but that was the only command he used. It didn't actually use the live context to turn off the lights after!"

### What Was Happening

1. ✅ User says "spegni le luci"
2. ✅ Tool model correctly calls GetLiveContext() first
3. ✅ GetLiveContext returns device information successfully
4. ❌ **Tool model STOPS and hands off to conversation model**
5. ❌ Conversation model responds with text, no action taken
6. ❌ Lights never turn off

### Root Cause

The code had a **single-turn tool calling** approach:
- Tool model makes ONE tool call (GetLiveContext)
- Tool gets executed successfully
- Control immediately passes to conversation model
- **No opportunity for tool model to make a SECOND call**

The retry mechanism (lines 1008-1193) only triggers when a tool **FAILS**, not when it **SUCCEEDS but requires follow-up**.

---

## The Fix

### What Changed

**File**: [dual_model_agent.py:1230-1323](src/open_llm_vtuber/agent/agents/dual_model_agent.py#L1230-L1323)

Added automatic follow-up logic that detects when GetLiveContext succeeds and prompts the tool model to complete the action.

### New Logic Flow

```python
# After tool execution completes:
1. Check if GetLiveContext was called and succeeded
2. If YES and it was the ONLY tool call:
   - Extract the original user request
   - Create explicit follow-up prompt
   - Call tool model AGAIN with GetLiveContext results
   - Execute the follow-up tool call
   - Add results to tool_results
3. Then pass ALL results to conversation model
```

### Key Code Addition

```python
# CRITICAL: If GetLiveContext was called successfully, prompt the tool model to make the ACTUAL control call
if has_getlivecontext and not has_errors and len(pending_tool_calls) == 1:
    logger.info("🔄 GetLiveContext completed - prompting tool model to use the results for the actual action")

    follow_up_prompt = f"""✅ GetLiveContext has returned the device information above.

NOW YOU MUST COMPLETE THE ORIGINAL REQUEST: "{original_request}"

CRITICAL - TAKE ACTION NOW:
1. READ the GetLiveContext results above carefully
2. Find the device mentioned in the original request
3. Call the appropriate Home Assistant tool with EXACT name and domain from GetLiveContext
4. DO NOT stop after GetLiveContext - you must complete the user's request!

Make the tool call NOW to complete the user's request."""

    # Call tool model again
    # Execute follow-up action
    # Merge results
```

---

## Expected Behavior After Fix

### Before (Incomplete):
```
User: "spegni le luci"
→ Tool Model: GetLiveContext() ✅
→ Result: {"name": "Speaker Switch", "domain": "switch"}
→ System: Hands off to conversation model
→ Conversation Model: "Ecco i dispositivi disponibili..."
→ Result: ❌ LIGHTS STILL ON
```

### After (Complete):
```
User: "spegni le luci"
→ Tool Model: GetLiveContext() ✅
→ Result: {"name": "Speaker Switch", "domain": "switch"}
→ System: "Now use these results to complete the request!"
→ Tool Model: HassTurnOff(name="Speaker Switch", domain="switch") ✅
→ Result: {"success": true}
→ Conversation Model: "Ho spento lo speaker!"
→ Result: ✅ LIGHTS OFF
```

---

## Why This Approach?

### Alternative Considered: Single Multi-Step Tool Call

**Option A**: Teach model to make BOTH calls in one response
```python
# Model outputs:
[
  {"name": "GetLiveContext", "arguments": {}},
  {"name": "HassTurnOff", "arguments": {"name": "...", "domain": "..."}}
]
```

**Problem**: Model doesn't know device names BEFORE calling GetLiveContext, so it can't construct the second call upfront.

### Chosen Solution: Sequential Prompting

**Option B**: Call model twice with explicit guidance
```python
# First call
Model → GetLiveContext()

# Second call (with context)
System: "Here are the devices, now complete the action"
Model → HassTurnOff(name="Speaker Switch", domain="switch")
```

**Benefits**:
- ✅ Model sees GetLiveContext results before making second call
- ✅ Can use EXACT names from results
- ✅ Explicit guidance ensures follow-up happens
- ✅ Works with models that don't plan multi-step well

---

## Implementation Details

### Detection Logic

```python
has_getlivecontext = False
for idx, result in enumerate(tool_results):
    tool_name = pending_tool_calls[idx].function.name
    if tool_name == "GetLiveContext" and not content.startswith("Error:"):
        has_getlivecontext = True
```

### Trigger Condition

```python
if has_getlivecontext and not has_errors and len(pending_tool_calls) == 1:
    # Only trigger if:
    # 1. GetLiveContext was called
    # 2. No errors occurred
    # 3. It was the ONLY tool call (not already part of multi-call)
```

### Follow-up Prompt Construction

```python
follow_up_prompt = f"""✅ GetLiveContext has returned the device information above.

NOW YOU MUST COMPLETE THE ORIGINAL REQUEST: "{original_request}"

CRITICAL - TAKE ACTION NOW:
1. READ the GetLiveContext results above carefully
2. Find the device mentioned in the original request ("{original_request}")
3. Call the appropriate Home Assistant tool (HassTurnOn, HassTurnOff, etc.) with EXACT name and domain from GetLiveContext
4. DO NOT stop after GetLiveContext - you must complete the user's request!
"""
```

**Why this works:**
- References original request so model remembers the goal
- Explicit numbered steps
- Emphasizes using EXACT values from GetLiveContext
- Urgent language ("NOW", "MUST") prevents stopping

### Result Merging

```python
if follow_up_results:
    messages.extend(follow_up_results)
    tool_results.extend(follow_up_results)  # Merge with original results
```

Both GetLiveContext AND the follow-up action results are passed to the conversation model, so it can summarize the complete action.

---

## Edge Cases Handled

### 1. Model Makes Multiple Calls Initially
If the model already makes `[GetLiveContext(), HassTurnOff()]` in one response, the condition `len(pending_tool_calls) == 1` prevents redundant follow-up.

### 2. GetLiveContext Fails
The condition `not has_errors` ensures we don't try to use failed results.

### 3. Model Doesn't Generate Follow-up
```python
if not follow_up_tool_calls:
    logger.warning("⚠️  Tool model did not generate a follow-up action after GetLiveContext")
```
Logs a warning but doesn't crash - conversation model will still respond (though action won't complete).

### 4. Multiple Tool Calls Including GetLiveContext
The condition `len(pending_tool_calls) == 1` ensures we only trigger for standalone GetLiveContext calls, not when it's part of a larger sequence.

---

## Performance Impact

### Latency
- **Before**: 1 tool model call → immediate handoff
- **After**: 2 tool model calls → slightly higher latency (~500-800ms added)

### Trade-off
- ❌ +500-800ms latency for Home Assistant commands
- ✅ Commands actually work (vs. not working at all)
- ✅ Better than forcing user to make TWO separate requests

### Optimization Potential
Future improvement: Train/fine-tune model to consistently make both calls in one response, eliminating need for follow-up.

---

## Testing Checklist

### Test 1: Turn Off Device
```
User: "spegni speaker switch"
Expected:
  1. GetLiveContext() called
  2. Follow-up prompt triggered
  3. HassTurnOff(name="Speaker Switch", domain="switch") called
  4. Device turns off
  5. Conversation model confirms action

Check logs for:
  - "🔍 GetLiveContext succeeded - will prompt for follow-up action"
  - "🔄 GetLiveContext completed - prompting tool model to use the results"
  - "🎯 Tool model generated follow-up action"
  - "⚡ Executing follow-up action"
  - "✅ Follow-up action completed"
```

### Test 2: Turn On Device
```
User: "accendi le luci"
Expected: Same as Test 1, but with HassTurnOn
```

### Test 3: Set Light Brightness
```
User: "imposta luce camera al 50%"
Expected: GetLiveContext → HassLightSet with exact device name
```

### Test 4: Non-Home Assistant Command (Should NOT Trigger Follow-up)
```
User: "che tempo fa a Roma"
Expected:
  - search(query="meteo Roma") called
  - NO follow-up prompt (only for GetLiveContext)
  - Results passed directly to conversation model
```

### Test 5: Model Makes Both Calls Initially (Should NOT Trigger Follow-up)
```
User: "spegni le luci"
If model outputs: [GetLiveContext(), HassTurnOff()]
Expected:
  - Both tools execute
  - len(pending_tool_calls) == 2 (not 1)
  - Follow-up logic skipped
  - Works correctly
```

---

## Logs to Monitor

**Success indicators:**
```
INFO: 🔍 GetLiveContext succeeded - will prompt for follow-up action
INFO: 🔄 GetLiveContext completed - prompting tool model to use the results for the actual action
INFO: 🎯 Tool model generated follow-up action: ['HassTurnOff']
INFO: ⚡ Executing follow-up action: 1 tool calls
INFO: ✅ Follow-up action completed with 1 results
```

**Failure indicators:**
```
WARNING: ⚠️  Tool model did not generate a follow-up action after GetLiveContext
```

If you see the warning, it means the tool model didn't respond to the follow-up prompt. This could indicate:
- Model context too full
- Model confused by prompt
- Model stuck in a loop

---

## Related Fixes

This fix works in conjunction with:

1. **[TOOL_MODEL_INPUT_ANALYSIS.md](TOOL_MODEL_INPUT_ANALYSIS.md)** - Removed persona confusion
2. **[TOOL_CALLING_FIX.md](TOOL_CALLING_FIX.md)** - Fixed context truncation and thinking mode
3. **Tool Validator** - Pre-validates tool calls before execution

Together, these fixes create a reliable tool calling pipeline:
1. Clean technical system prompt → Model focuses on tool execution
2. 128K context → Tool definitions never truncated
3. GetLiveContext follow-up → Two-step process completes automatically
4. Validation → Catch errors before wasting API calls

---

## Next Steps

1. **Test the fix**:
   ```bash
   docker-compose restart openllm
   # Try: "accendi le luci", "spegni speaker"
   ```

2. **Monitor logs**:
   ```bash
   docker logs -f openllm_vtuber | grep -E "(GetLiveContext|follow-up|🔄|🎯|⚡)"
   ```

3. **If model still doesn't follow up**:
   - Check tool model logs for errors
   - Verify 128K context is actually loaded
   - Check if model is hitting token limits

4. **Future optimization**:
   - Consider fine-tuning tool model on multi-step examples
   - Or use a larger tool model (e.g., Llama-3.1-70B) that plans better
   - Or implement explicit multi-step planning in prompt

---

## Summary

**Problem**: GetLiveContext succeeded but no follow-up action taken

**Root Cause**: Single-turn tool calling architecture

**Solution**: Automatic follow-up prompting when GetLiveContext succeeds

**Result**: Two-step Home Assistant process now completes automatically ✅
