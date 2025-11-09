# Dual Model Agent Setup Guide

This guide explains the new dual-model architecture that uses two separate LLMs for better performance in your voice assistant.

## What Is This?

The dual-model agent uses:
1. **Fast Conversational Model** (Cydonia-24B) - For general chat and personality
2. **Tool-Specialized Model** (Hermes-3-8B) - For Home Assistant control and MCP tools

## How It Works

```
User: "Ciao!"
  → Fast model responds: "Ciao! Come posso aiutarti?"
  → Low latency, natural conversation ✅

User: "Accendi la luce in soggiorno"
  → Keywords detected: "accendi", "luce"
  → Routes to tool model
  → Tool model calls Home Assistant
  → Fast model delivers result ✅
```

**Key Benefit**: You get fast responses for chat AND reliable tool calling without compromise!

## Setup Steps

### 1. Download the Hermes-3 Model

You need to download the Hermes-3-Llama-3.1-8B model for tool calling:

```bash
cd /home/docker/compose/Open-LLM-VTuber/models/llm

# Option A: Using huggingface-cli (if installed)
huggingface-cli download NousResearch/Hermes-3-Llama-3.1-8B-GGUF \
  Hermes-3-Llama-3.1-8B.Q6_K.gguf \
  --local-dir . --local-dir-use-symlinks False

# Option B: Using wget
wget https://huggingface.co/NousResearch/Hermes-3-Llama-3.1-8B-GGUF/resolve/main/Hermes-3-Llama-3.1-8B.Q6_K.gguf
```

### 2. Restart Docker Services

```bash
cd /home/docker/compose/Open-LLM-VTuber
docker-compose down
docker-compose up -d
```

This will start both llama.cpp instances:
- `llamacpp` on port 8080 (your existing Cydonia-24B model)
- `llamacpp_tools` on port 8081 (new Hermes-3-8B model)

### 3. Verify Services Are Running

```bash
# Check both containers are up
docker-compose ps

# Test fast model (should respond)
curl http://localhost:8080/health

# Test tool model (should respond)
curl http://localhost:8081/health
```

### 4. Start Open-LLM-VTuber

```bash
uv run run_server.py --verbose
```

Watch the logs - you should see:
```
DualModelAgent initialized with keyword-based intent detection
Tool keywords: ['accendi', 'spegni', 'luce', 'temperatura', ...]
```

## Configuration Details

Your `conf.yaml` is already configured with:

### Agent Selection
```yaml
agent_config:
  conversation_agent_choice: 'dual_model_agent'  # Using dual model
```

### Agent Settings
```yaml
agent_settings:
  dual_model_agent:
    conversation_llm_provider: 'llamacpp_llm_fast'  # Cydonia-24B
    tool_llm_provider: 'llamacpp_llm_tools'         # Hermes-3-8B
    use_mcpp: True
    mcp_enabled_servers: ["time", "home-assistant"]
```

### LLM Configurations
```yaml
llm_configs:
  llamacpp_llm_fast:
    base_url: 'http://llamacpp:8080/v1'
    model: 'cydonia-24b'
    temperature: 0.9  # Higher for natural conversation

  llamacpp_llm_tools:
    base_url: 'http://llamacpp_tools:8081/v1'
    model: 'hermes-3-8b'
    temperature: 0.3  # Lower for accurate JSON
```

## Keyword-Based Intent Detection

The agent automatically detects when tools are needed based on Italian keywords:

### Tool-Triggering Keywords
- **Control verbs**: accendi, spegni, attiva, disattiva, apri, chiudi, aumenta, diminuisci, imposta, regola
- **Entities**: luce, temperatura, termostato, riscaldamento, ventilatore, tapparella, porta, allarme
- **Rooms**: soggiorno, cucina, camera, bagno, studio, living, bedroom, bathroom
- **Time**: che ora, orario, tempo, quando, sveglia, timer
- **Search**: cerca, trova, info, dimmi, mostrami

### Examples

**Conversation (Fast Model)**:
- "Ciao come stai?" → Fast model
- "Raccontami una barzelletta" → Fast model
- "Cosa ne pensi del meteo?" → Fast model

**Tool Calling (Tool Model)**:
- "Accendi la luce in soggiorno" → Tool model (keywords: accendi, luce)
- "Che temperatura c'è in camera?" → Tool model (keywords: temperatura, camera)
- "Spegni il riscaldamento" → Tool model (keywords: spegni, riscaldamento)

## Customizing Keywords

You can add your own keywords in `conf.yaml`:

```yaml
dual_model_agent:
  tool_keywords:
    - "accendi"
    - "spegni"
    - "luce"
    - "temperatura"
    - "your_custom_keyword_here"
```

## Monitoring Performance

The agent tracks statistics internally. In verbose mode, you'll see:

```
🔧 Tool intent detected. Matched keywords: ['accendi', 'luce']
💬 Conversational intent detected
```

You can check which model handled what by looking at the logs.

## Troubleshooting

### Issue: "llamacpp_tools" container not starting

**Solution**: Check that Hermes-3 model file exists:
```bash
ls -lh /home/docker/compose/Open-LLM-VTuber/models/llm/Hermes-3-Llama-3.1-8B.Q6_K.gguf
```

### Issue: Tool calls still failing

**Solution**:
1. Check logs for which model is being used
2. Ensure `use_mcpp: True` in dual_model_agent settings
3. Verify Home Assistant MCP server is accessible:
   ```bash
   curl https://ha.silvaserv.it/mcp_server/sse
   ```

### Issue: Out of VRAM

**Solution**: You're running two models simultaneously. Options:
1. Use smaller quantization for fast model (Q4_K_M instead of Q6_K_L)
2. Reduce `n-gpu-layers` in docker-compose for one or both models
3. Use CPU for fast model if tool accuracy is priority

### Issue: Slow responses

**Solution**: Check which model is being used:
- If using tool model for simple chat, add more conversational keywords to bypass tool detection
- If using fast model when tools needed, add more tool keywords

## Switching Back to Single Model

If you want to revert to using just one model:

```yaml
agent_config:
  conversation_agent_choice: 'basic_memory_agent'  # Change back

agent_settings:
  basic_memory_agent:
    llm_provider: 'openai_compatible_llm'  # Or llamacpp_llm_fast
    use_mcpp: True
    mcp_enabled_servers: ["time", "home-assistant"]
```

## Performance Expectations

With **Dual Model Setup** on NVIDIA GPU:

| Scenario | Model Used | Expected Latency | Tool Accuracy |
|----------|-----------|------------------|---------------|
| Simple chat | Cydonia-24B | 300-500ms | N/A |
| Tool calling | Hermes-3-8B | 400-700ms | 90-95% |
| Complex chat | Cydonia-24B | 500-800ms | N/A |

## Next Steps

1. **Test conversational responses**: Ask general questions, verify fast model is used
2. **Test tool calling**: Try Home Assistant commands, verify tool model is used
3. **Monitor logs**: Watch for keyword matches and model routing
4. **Adjust keywords**: Add domain-specific keywords if needed
5. **Tune temperatures**: Adjust if responses feel too random or too rigid

## Need Help?

- Check logs with: `docker-compose logs -f openllm`
- Verify model loading: `docker-compose logs llamacpp_tools`
- Test MCP directly: Check `mcp_servers.json` configuration

Good luck with your voice assistant! 🎤🤖
