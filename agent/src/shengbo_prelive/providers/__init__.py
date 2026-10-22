"""Model providers. Credentials are never model fields or serialized state."""

from .credentials import CredentialManager, CredentialError

__all__ = ["ChatGPTPlanModel", "CredentialManager", "CredentialError", "build_chatgpt_model", "build_agent_model",
           "DeepSeekAgentModel", "agent_model_settings"]


def build_agent_model(model=None, *, purpose="agent", **kwargs):
    """Agent model factory honouring SHENGBO_AGENT_MODEL_PROVIDER (default deepseek).

    Keeps build_chatgpt_model's keyword hooks. ``purpose='summary'`` returns
    the DeepSeek summary model (thinking disabled); with the ChatGPT plan the
    caller keeps using the main model for summaries.
    """
    from .deepseek import agent_model_settings, build_deepseek_model
    settings = agent_model_settings()
    if settings["provider"] == "deepseek":
        return build_deepseek_model(purpose=purpose, settings=settings, **kwargs)
    if purpose == "summary":
        return None
    from .chatgpt import build_chatgpt_model
    return build_chatgpt_model(model=model or "gpt-6.1-sol", **kwargs)


def __getattr__(name):
    # The voice service reuses the OS lock through credentials.py. It must not
    # import LangChain/Deep Agents merely to read its independent DPAPI secrets.
    if name in {"ChatGPTPlanModel", "build_chatgpt_model"}:
        from . import chatgpt
        value = getattr(chatgpt, name)
        globals()[name] = value
        return value
    if name in {"DeepSeekAgentModel", "agent_model_settings"}:
        from . import deepseek
        value = getattr(deepseek, name)
        globals()[name] = value
        return value
    raise AttributeError(name)
