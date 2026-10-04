"""兼容 OpenAI Chat Completions 的异步客户端，不打印响应体或 API Key。"""

from urllib.parse import urlsplit

import aiohttp

from qqbot.config import Settings


class LLMError(Exception):
    pass


class ChatCompletionsClient:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession):
        self.settings = settings
        self.session = session

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        body = {"model": self.settings.llm_model, "messages": messages, "max_tokens": 800}
        if tools:
            body.update(tools=tools, tool_choice="auto")
        if urlsplit(self.settings.llm_base_url).hostname == "api.deepseek.com":
            # 电费查询无需长推理；DeepSeek 默认开启思考，可能耗尽短输出预算。
            body["thinking"] = {"type": "disabled"}
        try:
            async with self.session.post(
                f"{self.settings.llm_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                json=body,
                timeout=aiohttp.ClientTimeout(total=self.settings.llm_timeout),
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise LLMError(f"模型服务调用失败（HTTP {response.status}）")
                try:
                    data = await response.json(content_type=None)
                    message = data["choices"][0]["message"]
                except (ValueError, UnicodeError, KeyError, IndexError, TypeError):
                    raise LLMError("模型服务返回格式异常") from None
        except (aiohttp.ClientError, TimeoutError):
            raise LLMError("模型服务连接失败或超时") from None
        if not isinstance(message, dict):
            raise LLMError("模型消息格式异常")
        result = {"role": "assistant", "content": message.get("content")}
        if result["content"] is not None and not isinstance(result["content"], str):
            raise LLMError("模型文本格式异常")
        calls = message.get("tool_calls")
        if calls is not None:
            if not isinstance(calls, list) or len(calls) > 1:
                raise LLMError("模型工具调用格式异常或数量过多")
            ids = set()
            for call in calls:
                if (
                    not isinstance(call, dict)
                    or call.get("type") != "function"
                    or not isinstance(call.get("id"), str)
                    or not 0 < len(call["id"]) <= 128
                    or call["id"] in ids
                    or not isinstance(call.get("function"), dict)
                    or not isinstance(call["function"].get("name"), str)
                    or not isinstance(call["function"].get("arguments"), str)
                ):
                    raise LLMError("模型工具调用格式异常")
                ids.add(call["id"])
            result["tool_calls"] = calls
        # 一些兼容服务在工具调用后要求回传该字段。
        if isinstance(message.get("reasoning_content"), str):
            result["reasoning_content"] = message["reasoning_content"]
        return result
