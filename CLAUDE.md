# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Open-LLM-VTuber** is a low-latency, voice-interactive AI companion with Live2D avatar support. The project enables real-time voice conversations, visual perception, and character customization, all capable of running completely offline.

- **Primary Goal**: Achieve end-to-end latency below 500ms (user speaks → AI voice heard)
- **Language**: Python >= 3.10, < 3.13
- **Core Stack**: FastAPI, Pydantic v2, Uvicorn (fully async), WebSockets
- **Package Manager**: `uv` (~= 0.8) - Always use `uv run`, `uv sync`, `uv add`, `uv remove` instead of pip
- **Key Principles**:
  - Offline-ready: Core functionality MUST work without internet
  - Strict frontend-backend separation
  - Performance-critical: avoid blocking operations in async contexts
  - Cross-platform: macOS, Windows, Linux (with CPU and GPU support)

## Development Commands

### Running the Server
```bash
# Standard mode
uv run run_server.py

# Verbose/debug mode
uv run run_server.py --verbose

# With Hugging Face mirror (for Chinese users)
uv run run_server.py --hf_mirror
```

### Code Quality
```bash
# Format all Python code
uv run ruff format

# Lint and check code
uv run ruff check

# Auto-fix linting issues
uv run ruff check --fix

# Run pre-commit hooks
pre-commit run --all-files
```

### Updates
```bash
# Update the project (for versions >= v1.0.0)
uv run upgrade.py
```

### Docker
```bash
# Build and run with Docker Compose
docker-compose up --build

# The server runs on port 12393 by default
```

### Dependency Management
```bash
# Add a new dependency
uv add package-name

# Remove a dependency
uv remove package-name

# Sync dependencies
uv sync

# IMPORTANT: After modifying pyproject.toml, also update requirements.txt
```

## Architecture Overview

### Directory Structure

```
src/open_llm_vtuber/          # Main source code
├── agent/                     # LLM agent implementations
│   ├── agents/                # Concrete agent implementations
│   │   ├── agent_interface.py      # Base interface for all agents
│   │   ├── basic_memory_agent.py   # Default agent with memory
│   │   ├── hume_ai.py              # HumeAI EVI integration
│   │   ├── letta_agent.py          # Letta agent integration
│   │   └── mem0_llm.py             # Mem0 integration
│   ├── stateless_llm/         # Stateless LLM providers
│   ├── agent_factory.py       # Factory for creating agent instances
│   ├── transformers.py        # Input/output transformers
│   └── input_types.py, output_types.py
├── asr/                       # Speech recognition implementations
│   ├── asr_interface.py       # Base ASR interface
│   ├── sherpa_onnx_asr.py     # Sherpa-ONNX (offline)
│   ├── faster_whisper_asr.py  # Faster Whisper
│   ├── fun_asr.py             # FunASR
│   ├── azure_asr.py, groq_whisper_asr.py, etc.
│   └── asr_factory.py
├── tts/                       # Text-to-speech implementations
│   ├── tts_interface.py       # Base TTS interface
│   ├── sherpa_onnx_tts.py     # Sherpa-ONNX (offline)
│   ├── edge_tts.py, azure_tts.py, bark_tts.py, etc.
│   └── tts_factory.py
├── vad/                       # Voice activity detection
├── translate/                 # Translation engines
├── mcpp/                      # Model Context Protocol (MCP) integration
│   ├── server_registry.py     # MCP server management
│   ├── tool_manager.py        # Tool discovery and management
│   ├── mcp_client.py          # MCP client
│   └── tool_executor.py       # Tool execution
├── conversations/             # Conversation handling logic
├── config_manager/            # Pydantic models for configuration
│   └── main.py                # Main Config model (validates conf.yaml)
├── server.py                  # FastAPI server initialization
├── routes.py                  # API route definitions
├── websocket_handler.py       # WebSocket connection management
├── service_context.py         # Service initialization and management
├── chat_group.py              # Multi-client group chat
├── chat_history_manager.py    # Chat history persistence
└── live2d_model.py            # Live2D model management

config_templates/              # Configuration templates
├── conf.default.yaml          # English template
└── conf.ZH.default.yaml       # Chinese template

run_server.py                  # Application entrypoint
conf.yaml                      # User configuration (generated from template)
frontend/                      # React frontend (git submodule)
live2d-models/                 # Live2D character models
prompts/                       # System prompts and character personas
characters/                    # Character configurations
```

### Core Components

#### 1. Server Architecture (FastAPI + WebSockets)
- **Entry Point**: `run_server.py` initializes logging, checks frontend submodule, syncs config, and starts the Uvicorn server
- **WebSocketServer** (`server.py`): Creates FastAPI app, registers routes, serves static files with CORS
- **WebSocketHandler** (`websocket_handler.py`): Manages WebSocket connections, routes messages to handlers
- **Routes** (`routes.py`): Defines endpoints for client WebSocket, web tools, and proxy mode

#### 2. Service Context (`service_context.py`)
The `ServiceContext` class is the central orchestrator that initializes and manages:
- Configuration (system, character, agent, ASR, TTS, VAD)
- Live2D model
- ASR engine instance
- TTS engine instance
- Agent (LLM) engine instance
- VAD engine instance (optional)
- Translation engine (optional)
- MCP components (tool registry, manager, client, executor)
- System prompt (persona + Live2D expressions)

Each connected client gets its own `ServiceContext` (or references a shared cache).

#### 3. Factory Pattern
All major components use factories for instantiation:
- `AgentFactory`: Creates agent instances based on config type
- `ASRFactory`: Creates ASR engines
- `TTSFactory`: Creates TTS engines
- `VADFactory`: Creates VAD engines
- `TranslateFactory`: Creates translation engines

#### 4. Configuration System
- **Templates**: `config_templates/conf.default.yaml` and `conf.ZH.default.yaml`
- **User Config**: `conf.yaml` (validated on load)
- **Validation**: Pydantic models in `src/open_llm_vtuber/config_manager/main.py`
- **Important**: When modifying config structure, update BOTH templates AND Pydantic models

#### 5. Message Flow (WebSocket)
1. Client connects → `WebSocketHandler.handle_new_connection()`
2. Client sends message → `handle_client_message()` routes to appropriate handler
3. Message types:
   - `mic-audio-data`: Audio data streaming
   - `mic-audio-end`, `text-input`, `ai-speak-signal`: Trigger conversations
   - `interrupt-signal`: Stop current AI speech
   - `add-client-to-group`, `remove-client-from-group`: Group chat
   - `fetch-history-list`, `create-new-history`, etc.: History management
   - `fetch-configs`, `switch-config`: Dynamic configuration switching

#### 6. Conversation Pipeline
1. **Input**: Audio data or text from client
2. **ASR** (if audio): Converts speech to text
3. **Agent**: Processes text through LLM, optionally with MCP tools
4. **TTS**: Converts AI response to speech
5. **Output**: Streams audio back to client, updates Live2D expressions

#### 7. MCP (Model Context Protocol) Integration
- Allows agents to use external tools dynamically
- `ServerRegistry`: Manages available MCP servers
- `ToolAdapter`: Fetches and formats tool definitions
- `ToolManager`: Tracks available tools
- `ToolExecutor`: Executes tool calls
- MCP prompt is dynamically generated and included in agent context

### Key Design Patterns

1. **Interface-Based Design**: All major components (ASR, TTS, Agent, VAD, Translate) implement interfaces for easy extension
2. **Factory Pattern**: Centralized creation of service instances
3. **Async-First**: All I/O operations are async to maximize throughput
4. **Modular**: Easy to add new ASR/TTS/Agent implementations by inheriting interfaces
5. **Separation of Concerns**: Frontend (React) is completely separate, served as static files

## Coding Standards

### Type Hints (CRITICAL)
- **DO**: Use `|` for unions (e.g., `str | None`)
- **DON'T**: Use `Optional` from typing
- **DO**: Use built-in generics (`list[int]`, `dict[str, float]`)
- **DON'T**: Use capitalized types from typing (`List`, `Dict`)
- All function signatures MUST have type hints

### Naming Conventions
- `snake_case`: variables, functions, methods, modules
- `PascalCase`: classes
- Descriptive names (avoid single letters except loop counters)

### Docstrings (CRITICAL)
- **Required**: All public modules, functions, classes, methods
- **Style**: Google Python Style
- **Must Include**:
  - Summary
  - `Args:` section (parameter type and purpose)
  - `Returns:` section (return type and meaning)
  - `Raises:` (optional but encouraged)
- **Language**: English only for docstrings and comments

### Logging
- Use `loguru` for all logging
- Messages in English, clear and informative
- Use emoji when appropriate (project convention)

### Dependencies
1. Try to use Python standard library or existing dependencies first
2. New dependencies must be compatible-licensed and well-maintained
3. Use `uv add`, `uv remove`, `uv run` (NOT pip)
4. After adding to `pyproject.toml`, also update `requirements.txt`

### Cross-Platform Compatibility
- All core logic MUST run on macOS, Windows, Linux
- Platform/hardware-specific features (e.g., CUDA) MUST be optional with graceful fallbacks

## Configuration Files

When modifying configuration:
1. Update both `config_templates/conf.default.yaml` AND `config_templates/conf.ZH.default.yaml`
2. Update Pydantic models in `src/open_llm_vtuber/config_manager/main.py`
3. Run validation tests to ensure config loading works

## Frontend

- Frontend is a **git submodule** (separate repo: `Open-LLM-VTuber-Web`)
- DO NOT manually modify files in `frontend/`
- Submodule auto-initializes on first run via `run_server.py`
- Frontend served as static files by FastAPI

## Important Notes

- **Performance**: This is a latency-sensitive application. Avoid blocking calls in async contexts
- **Offline-First**: Many users run this completely offline. Cloud APIs should be optional
- **Memory**: Long-term memory feature temporarily removed but chat logs persist
- **HTTPS**: Required for remote access (microphone API requires secure context)
- **GPU Support**: Supports NVIDIA GPUs (CUDA), CPU fallback, and macOS GPU acceleration for some components
- **Model Caching**: Models downloaded via ModelScope/HuggingFace are cached in `models/` directory

## Common Patterns

### Adding a New ASR Implementation
1. Create new file in `src/open_llm_vtuber/asr/`
2. Inherit from `ASRInterface` in `asr_interface.py`
3. Implement `transcribe_with_vad()` or `transcribe_np()` methods
4. Add to `ASRFactory` in `asr_factory.py`
5. Update config templates and Pydantic models

### Adding a New Agent Implementation
1. Create new file in `src/open_llm_vtuber/agent/agents/`
2. Inherit from `AgentInterface`
3. Implement `chat()` and `chat_stream()` methods
4. Add to `AgentFactory` in `agent_factory.py`
5. Update config templates and Pydantic models

## Testing

The `graphiti/` directory contains tests for the Graphiti knowledge graph component (integration tests). Currently, no comprehensive test suite exists for the main application. Tests would be valuable contributions.

## Documentation

- Official docs: https://open-llm-vtuber.github.io/docs/
- Documentation repo: `open-llm-vtuber.github.io`
- When creating docs, generate Markdown files in project root (user migrates to docs site)
- Common issues (Chinese): https://docs.qq.com/pdf/DTFZGQXdTUXhIYWRq

## License

MIT License (main project), but Live2D sample models have separate licensing (see LICENSE-Live2D.md)
