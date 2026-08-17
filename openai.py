"""Small OpenAI-compatible client used by Tab Assistant.

This intentionally uses requests instead of the official OpenAI SDK. The official
SDK pulls in Rust/PyO3 wheels (notably jiter) that are awkward on some Termux
Android ABIs, including armv8l. This module implements only the API surface app.py
needs, keeping the core app pure Python and friendly to Python 3.14 on Termux.
"""
import json
import requests


class APIError(Exception):
    def __init__(self, message="", status_code=None, response=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class APIConnectionError(APIError):
    pass


class APIStatusError(APIError):
    pass


class AuthenticationError(APIStatusError):
    pass


class RateLimitError(APIStatusError):
    pass


class _Delta:
    def __init__(self, content=""):
        self.content = content


class _Choice:
    def __init__(self, content=None, delta=None):
        self.message = type("Message", (), {"content": content})()
        self.delta = delta or _Delta()


class _Response:
    def __init__(self, content):
        self.choices = [_Choice(content=content)]


def _error(response):
    try:
        data = response.json()
        message = data.get("error", {}).get("message", data)
    except Exception:
        message = response.text or response.reason
    message = str(message)
    if response.status_code == 401:
        return AuthenticationError(message, response.status_code, response)
    if response.status_code == 429:
        return RateLimitError(message, response.status_code, response)
    return APIStatusError(message, response.status_code, response)


class _Completions:
    def __init__(self, client):
        self.client = client

    def create(self, model, messages, stream=False, **kwargs):
        payload = {"model": model, "messages": messages, "stream": stream}
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        url = self.client.base_url.rstrip("/") + "/chat/completions"
        try:
            response = self.client.session.post(
                url, json=payload, timeout=self.client.timeout, stream=stream
            )
        except requests.RequestException as exc:
            raise APIConnectionError(str(exc)) from exc
        if not response.ok:
            raise _error(response)
        if not stream:
            try:
                data = response.json()
                return _Response(data["choices"][0]["message"].get("content") or "")
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise APIStatusError("Invalid response from provider", response.status_code, response) from exc
        return self._stream(response)

    @staticmethod
    def _stream(response):
        try:
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        content = obj.get("choices", [{}])[0].get("delta", {}).get("content") or ""
                    except (ValueError, IndexError, TypeError):
                        continue
                    yield type("Chunk", (), {"choices": [_Choice(delta=_Delta(content))]})()
        finally:
            response.close()


class _Chat:
    def __init__(self, client):
        self.completions = _Completions(client)


class OpenAI:
    def __init__(self, api_key, base_url=None, timeout=30, **kwargs):
        if not api_key:
            raise AuthenticationError("API key is empty", 401)
        self.base_url = base_url or "https://api.openai.com/v1"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "User-Agent": "tab-assistant-termux/1.0",
        })
        self.chat = _Chat(self)
