from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Union

class ChatMessage(BaseModel):
    model_config = {"extra": "allow"}
    role: str
    content: Optional[Union[str, List[Any]]] = None

class ChatCompletionRequest(BaseModel):
    model_config = {"extra": "allow"}
    model: str
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = None
    n: Optional[int] = None
    stream: Optional[bool] = False
    stop: Optional[Union[str, List[str]]] = None
    max_tokens: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    logit_bias: Optional[Dict[str, float]] = None
    user: Optional[str] = None
    response_format: Optional[Dict[str, Any]] = None
    seed: Optional[int] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None

class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

class ChatChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: Optional[str] = None

class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatChoice]
    usage: Usage


class ProviderResult(BaseModel):
    """Wraps a provider response with metadata for the router."""
    response: ChatCompletionResponse
    provider: str = ""
    key_id: int = 0
    latency_ms: float = 0.0
    rate_limit_headers: Dict[str, str] = {}
