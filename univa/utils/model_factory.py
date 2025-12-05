from typing import Any, Dict, Optional

from agno.models.base import Model


def _parse_extra(extra: Optional[str | Dict[str, Any]]) -> Dict[str, Any]:
    if isinstance(extra, dict):
        return extra
    if isinstance(extra, str) and extra.strip():
        try:
            import json
            return json.loads(extra)
        except Exception:
            return {}
    return {}


def create_model(
    provider: str,
    model_id: str,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    extra_params: Optional[str | Dict[str, Any]] = None,
) -> Model:
    p = (provider or "").lower()
    extra = _parse_extra(extra_params)

    if (base_url or "").lower().find("dashscope") != -1 and p in ("openai", "openai_compatible"):
        from agno.models.dashscope import DashScope

        return DashScope(
            id=model_id,
            api_key=api_key,
            base_url=base_url or None,
            request_params=extra or None,
        )

    if p in ("openai", "openai_compatible", "vllm", "sglang", "ollama"):
        from agno.models.openai import OpenAIChat

        role_map = None
        if isinstance(extra, dict) and "role_map" in extra and isinstance(extra["role_map"], dict):
            role_map = extra["role_map"]

        return OpenAIChat(
            id=model_id,
            api_key=api_key,
            base_url=base_url or None,
            request_params=extra or None,
            role_map=role_map,
        )

    if p == "groq":
        from agno.models.groq import Groq

        return Groq(
            id=model_id,
            api_key=api_key,
            base_url=base_url or None,
            request_params=extra or None,
        )

    if p == "anthropic":
        from agno.models.anthropic import Claude

        return Claude(
            id=model_id,
            api_key=api_key,
            request_params=extra or None,
        )

    if p == "dashscope":
        from agno.models.dashscope import DashScope

        return DashScope(
            id=model_id,
            api_key=api_key,
            base_url=base_url or None,
            request_params=extra or None,
        )

    if p == "deepseek":
        from agno.models.deepseek import DeepSeek

        return DeepSeek(
            id=model_id,
            api_key=api_key,
            request_params=extra or None,
            base_url=base_url or None,
        )

    if p == "gemini" or p == "google":
        from agno.models.google.gemini import Gemini

        return Gemini(
            id=model_id,
            api_key=api_key,
            request_params=extra or None,
        )

    if p in ("azure_openai", "azure"):
        try:
            from agno.models.azure.openai_chat import AzureOpenAIChat  # type: ignore

            return AzureOpenAIChat(
                id=model_id,
                api_key=api_key,
                base_url=base_url or None,
                request_params=extra or None,
            )
        except Exception:
            from agno.models.openai import OpenAIChat

            role_map = None
            if isinstance(extra, dict) and "role_map" in extra and isinstance(extra["role_map"], dict):
                role_map = extra["role_map"]

            return OpenAIChat(
                id=model_id,
                api_key=api_key,
                base_url=base_url or None,
                request_params=extra or None,
                role_map=role_map,
            )

    # Fallback to OpenAI-compatible
    from agno.models.openai import OpenAIChat

    role_map = None
    if isinstance(extra, dict) and "role_map" in extra and isinstance(extra["role_map"], dict):
        role_map = extra["role_map"]

    return OpenAIChat(
        id=model_id,
        api_key=api_key,
        base_url=base_url or None,
        request_params=extra or None,
        role_map=role_map,
    )
