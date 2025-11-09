from typing import Type, Literal
from loguru import logger

from .agents.agent_interface import AgentInterface
from .agents.basic_memory_agent import BasicMemoryAgent
from .agents.dual_model_agent import DualModelAgent
from .stateless_llm_factory import LLMFactory as StatelessLLMFactory
from .agents.hume_ai import HumeAIAgent
from .agents.letta_agent import LettaAgent

from ..mcpp.tool_manager import ToolManager
from ..mcpp.tool_executor import ToolExecutor
from typing import Optional


class AgentFactory:
    @staticmethod
    def create_agent(
        conversation_agent_choice: str,
        agent_settings: dict,
        llm_configs: dict,
        system_prompt: str,
        live2d_model=None,
        tts_preprocessor_config=None,
        **kwargs,
    ) -> Type[AgentInterface]:
        """Create an agent based on the configuration.

        Args:
            conversation_agent_choice: The type of agent to create
            agent_settings: Settings for different types of agents
            llm_configs: Pool of LLM configurations
            system_prompt: The system prompt to use
            live2d_model: Live2D model instance for expression extraction
            tts_preprocessor_config: Configuration for TTS preprocessing
            **kwargs: Additional arguments
        """
        logger.info(f"Initializing agent: {conversation_agent_choice}")

        if conversation_agent_choice == "basic_memory_agent":
            # Get the LLM provider choice from agent settings
            basic_memory_settings: dict = agent_settings.get("basic_memory_agent", {})
            llm_provider: str = basic_memory_settings.get("llm_provider")

            if not llm_provider:
                raise ValueError("LLM provider not specified for basic memory agent")

            # Get the LLM config for this provider
            llm_config: dict = llm_configs.get(llm_provider)
            interrupt_method: Literal["system", "user"] = llm_config.pop(
                "interrupt_method", "user"
            )

            if not llm_config:
                raise ValueError(
                    f"Configuration not found for LLM provider: {llm_provider}"
                )

            # Create the stateless LLM
            llm = StatelessLLMFactory.create_llm(
                llm_provider=llm_provider, system_prompt=system_prompt, **llm_config
            )

            tool_prompts = kwargs.get("system_config", {}).get("tool_prompts", {})

            # Extract MCP components/data needed by BasicMemoryAgent from kwargs
            tool_manager: Optional[ToolManager] = kwargs.get("tool_manager")
            tool_executor: Optional[ToolExecutor] = kwargs.get("tool_executor")
            mcp_prompt_string: str = kwargs.get("mcp_prompt_string", "")

            # Create the agent with the LLM and live2d_model
            return BasicMemoryAgent(
                llm=llm,
                system=system_prompt,
                live2d_model=live2d_model,
                tts_preprocessor_config=tts_preprocessor_config,
                faster_first_response=basic_memory_settings.get(
                    "faster_first_response", True
                ),
                segment_method=basic_memory_settings.get("segment_method", "pysbd"),
                use_mcpp=basic_memory_settings.get("use_mcpp", False),
                interrupt_method=interrupt_method,
                tool_prompts=tool_prompts,
                tool_manager=tool_manager,
                tool_executor=tool_executor,
                mcp_prompt_string=mcp_prompt_string,
            )

        elif conversation_agent_choice == "mem0_agent":
            from .agents.mem0_llm import LLM as Mem0LLM

            mem0_settings = agent_settings.get("mem0_agent", {})
            if not mem0_settings:
                raise ValueError("Mem0 agent settings not found")

            # Validate required settings
            required_fields = ["base_url", "model", "mem0_config"]
            for field in required_fields:
                if field not in mem0_settings:
                    raise ValueError(
                        f"Missing required field '{field}' in mem0_agent settings"
                    )

            return Mem0LLM(
                user_id=kwargs.get("user_id", "default"),
                system=system_prompt,
                live2d_model=live2d_model,
                **mem0_settings,
            )

        elif conversation_agent_choice == "hume_ai_agent":
            settings = agent_settings.get("hume_ai_agent", {})
            return HumeAIAgent(
                api_key=settings.get("api_key"),
                host=settings.get("host", "api.hume.ai"),
                config_id=settings.get("config_id"),
                idle_timeout=settings.get("idle_timeout", 15),
            )

        elif conversation_agent_choice == "letta_agent":
            settings = agent_settings.get("letta_agent", {})
            return LettaAgent(
                live2d_model=live2d_model,
                id=settings.get("id"),
                tts_preprocessor_config=tts_preprocessor_config,
                faster_first_response=settings.get("faster_first_response"),
                segment_method=settings.get("segment_method"),
                host=settings.get("host"),
                port=settings.get("port"),
            )

        elif conversation_agent_choice == "dual_model_agent":
            # Get settings for dual model agent
            dual_settings: dict = agent_settings.get("dual_model_agent", {})

            # Get LLM provider choices for both models
            fast_llm_provider: str = dual_settings.get("conversation_llm_provider")
            tool_llm_provider: str = dual_settings.get("tool_llm_provider")
            intent_llm_provider: str | None = dual_settings.get("intent_llm_provider")

            if not fast_llm_provider or not tool_llm_provider:
                raise ValueError(
                    "Both conversation_llm_provider and tool_llm_provider must be specified for dual_model_agent"
                )

            logger.debug(
                f"DualModelAgent: Looking for providers: {fast_llm_provider}, {tool_llm_provider}, intent={intent_llm_provider}"
            )
            logger.debug(
                f"DualModelAgent: Available providers in llm_configs: {list(llm_configs.keys())}"
            )

            # Get configs for both LLMs (make copies to avoid mutation)
            fast_llm_config_raw: dict = llm_configs.get(fast_llm_provider)
            tool_llm_config_raw: dict = llm_configs.get(tool_llm_provider)

            if not fast_llm_config_raw or not tool_llm_config_raw:
                raise ValueError(
                    f"Configuration not found for one or both LLM providers: {fast_llm_provider}, {tool_llm_provider}"
                )

            # Make copies and remove interrupt_method (not needed for instantiation)
            fast_llm_config = fast_llm_config_raw.copy()
            tool_llm_config = tool_llm_config_raw.copy()
            fast_llm_config.pop("interrupt_method", None)
            tool_llm_config.pop("interrupt_method", None)

            # Create both LLMs
            fast_llm = StatelessLLMFactory.create_llm(
                llm_provider=fast_llm_provider,
                system_prompt=system_prompt,
                **fast_llm_config,
            )

            tool_llm = StatelessLLMFactory.create_llm(
                llm_provider=tool_llm_provider,
                system_prompt=system_prompt,
                **tool_llm_config,
            )

            # Create intent LLM if specified (optional)
            intent_llm = None
            if intent_llm_provider:
                logger.debug(
                    f"🔍 Attempting to create intent LLM with provider: {intent_llm_provider}"
                )
                intent_llm_config_raw: dict = llm_configs.get(intent_llm_provider)
                if not intent_llm_config_raw:
                    logger.warning(
                        f"⚠️  Intent LLM provider '{intent_llm_provider}' not found in llm_configs, will use conversation LLM for intent classification"
                    )
                else:
                    logger.debug(f"✅ Found intent LLM config: {intent_llm_config_raw}")
                    intent_llm_config = intent_llm_config_raw.copy()
                    intent_llm_config.pop("interrupt_method", None)
                    try:
                        intent_llm = StatelessLLMFactory.create_llm(
                            llm_provider=intent_llm_provider,
                            system_prompt="",  # Intent router uses its own classification prompt
                            **intent_llm_config,
                        )
                        logger.info(
                            f"✅ Created dedicated intent classification LLM: {intent_llm_provider}"
                        )
                    except Exception as e:
                        logger.error(
                            f"❌ Failed to create intent LLM: {e}", exc_info=True
                        )
                        intent_llm = None
            else:
                logger.debug(
                    "No intent_llm_provider specified, will use conversation LLM for intent classification"
                )

            tool_prompts = kwargs.get("system_config", {}).get("tool_prompts", {})

            # Extract MCP components
            tool_manager: Optional[ToolManager] = kwargs.get("tool_manager")
            tool_executor: Optional[ToolExecutor] = kwargs.get("tool_executor")
            mcp_prompt_string: str = kwargs.get("mcp_prompt_string", "")

            # Get intent detection method (defaults to "llm")
            intent_detection_method = dual_settings.get(
                "intent_detection_method", "llm"
            )

            # Get tool keywords (optional, will use defaults if not provided)
            tool_keywords = dual_settings.get("tool_keywords", None)

            # Get tool acknowledgment setting (defaults to True)
            enable_tool_acknowledgment = dual_settings.get(
                "enable_tool_acknowledgment", True
            )

            # Create dual model agent
            return DualModelAgent(
                fast_llm=fast_llm,
                tool_llm=tool_llm,
                system=system_prompt,
                live2d_model=live2d_model,
                tts_preprocessor_config=tts_preprocessor_config,
                faster_first_response=dual_settings.get("faster_first_response", True),
                segment_method=dual_settings.get("segment_method", "pysbd"),
                use_mcpp=dual_settings.get("use_mcpp", False),
                intent_detection_method=intent_detection_method,
                tool_keywords=tool_keywords,
                intent_llm=intent_llm,
                tool_manager=tool_manager,
                tool_executor=tool_executor,
                mcp_prompt_string=mcp_prompt_string,
                enable_tool_acknowledgment=enable_tool_acknowledgment,
            )

        else:
            raise ValueError(f"Unsupported agent type: {conversation_agent_choice}")
