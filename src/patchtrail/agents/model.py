"""LangChain adapter for DeepSeek non-thinking chat tool calls."""

from patchtrail.domain.task import VerificationError


class LangChainModel:
    is_real_model = True

    def __init__(self, settings):
        settings.validate()
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise VerificationError("CONFIG_ERROR", "agent_dependencies_missing",
                                    "Run with the approved project .venv and agent dependencies.") from exc
        self.settings = settings
        self.chat = ChatOpenAI(model=settings.model, base_url=settings.base_url,
                               api_key=settings.api_key, timeout=settings.timeout_seconds,
                               max_retries=0, use_responses_api=False,
                               extra_body={"thinking": {"type": "disabled"},
                                           "max_tokens": settings.max_output_tokens})

    def public(self):
        return self.settings.public()

    def invoke(self, messages, tools):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
        from langsmith import tracing_context

        converted = []
        for item in messages:
            role, content = item["role"], item["content"]
            if role == "system":
                converted.append(SystemMessage(content=content))
            elif role == "user":
                converted.append(HumanMessage(content=content))
            elif role == "assistant":
                converted.append(AIMessage(content=content, tool_calls=item.get("tool_calls", [])))
            elif role == "tool":
                converted.append(ToolMessage(content=content, tool_call_id=item["tool_call_id"]))
        try:
            # Do not inherit a machine-wide tracing setting that uploads source
            # to a second service. Only the configured model receives context.
            with tracing_context(enabled=False):
                response = self.chat.bind_tools(tools).invoke(converted)
        except Exception as exc:
            # Provider exceptions may include request headers or supplied keys.
            raise VerificationError("ENVIRONMENT_ERROR", "model_request_failed",
                                    f"Model request failed ({type(exc).__name__}); no raw credentials logged.") from None
        if response.invalid_tool_calls:
            raise VerificationError("ENVIRONMENT_ERROR", "invalid_tool_response", "Model returned malformed tool arguments.")
        content = response.content
        if not isinstance(content, str):
            content = "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
        return {"content": content, "tool_calls": response.tool_calls,
                "usage": response.usage_metadata,
                "finish_reason": response.response_metadata.get("finish_reason")}
