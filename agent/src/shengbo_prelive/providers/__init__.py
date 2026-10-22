"""Model providers. Credentials are never model fields or serialized state."""

from .credentials import CredentialManager, CredentialError

__all__ = ["ChatGPTPlanModel", "CredentialManager", "CredentialError", "build_chatgpt_model"]


def __getattr__(name):
    # The voice service reuses the OS lock through credentials.py. It must not
    # import LangChain/Deep Agents merely to read its independent DPAPI secrets.
    if name in {"ChatGPTPlanModel", "build_chatgpt_model"}:
        from . import chatgpt
        value = getattr(chatgpt, name)
        globals()[name] = value
        return value
    raise AttributeError(name)
