"""Loopback-only Ollama adapter; no tools, cloud models, proxies or downloads."""
import json
import logging
import socket
import threading
import urllib.error
import urllib.request

from .models import parse_json, ValidationError
from .context import context_budget

LOG = logging.getLogger(__name__)
BASE_URL = 'http://127.0.0.1:11434'


class OllamaError(RuntimeError):
    pass


class ModelResponseError(ValidationError):
    def __init__(self, message, metrics):
        super().__init__(message)
        self.metrics = metrics


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OllamaError('Ollama redirect refused.')


class OllamaClient:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.blocked = False
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def _request(self, path, body=None, timeout=5):
        request = urllib.request.Request(BASE_URL + path,
            data=None if body is None else json.dumps(body).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                payload = response.read(512_001)
                if len(payload) > 512_000:
                    raise OllamaError('Ollama response exceeds size limit.')
                value = parse_json(payload)
                if type(value) is not dict:
                    raise OllamaError('Unexpected Ollama response.')
                return value
        except (TimeoutError, socket.timeout) as exc:
            if body is not None:
                self.blocked = True
            raise OllamaError('Ollama timed out. Further generation is paused; check Ollama before restarting AIHub.') from exc
        except urllib.error.HTTPError as exc:
            raise OllamaError(f'Ollama returned HTTP {exc.code}.') from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)) and body is not None:
                self.blocked = True
            raise OllamaError('Cannot reach local Ollama at 127.0.0.1:11434.') from exc
        except (ValidationError, UnicodeError) as exc:
            raise OllamaError('Invalid response from local Ollama.') from exc

    def health(self):
        try:
            version = self._request('/api/version').get('version', 'unknown')
            tags = self._request('/api/tags').get('models', [])
            installed = next((m for m in tags if m.get('name') == self.config.model), None)
            # New Ollama versions expose remote model entries through /tags.
            if not installed or installed.get('remote_host') or installed.get('remote_model') or 'cloud' in self.config.model.lower():
                return dict(connected=False, error='Configured model must be installed locally; cloud models are refused.', version=version)
            return dict(connected=not self.blocked, error='Generation paused after timeout; check Ollama and restart AIHub.' if self.blocked else '', version=version)
        except (OllamaError, AttributeError, TypeError) as exc:
            return dict(connected=False, error=str(exc), version=None)

    def chat(self, messages, schema=None, *, role=None):
        with self.lock:
            if self.blocked:
                raise OllamaError('Generation paused after timeout. Check Ollama before restarting AIHub.')
            health = self.health()
            if not health['connected']:
                raise OllamaError(health['error'])
            body = dict(model=self.config.model, messages=messages, stream=False, think=False,
                        keep_alive='2m', options=dict(num_ctx=self.config.context, temperature=0, seed=42,
                                                     num_predict=1400 if schema else 2400 if role in ('researcher', 'writer') else 1000))
            if schema:
                body['format'] = schema
            budget=context_budget(messages,self.config.context,body['options']['num_predict'],schema)
            body['options']['num_predict']=budget['output_budget']
            value = self._request('/api/chat', body, self.config.timeout)
            metrics = {k: value.get(k, 0) for k in ('eval_count', 'eval_duration', 'total_duration', 'load_duration', 'prompt_eval_count')}
            metrics = {k:v if type(v) is int and v>=0 else None for k,v in metrics.items()}
            reason = value.get('done_reason')
            metrics.update(done_reason=reason if reason in ('stop','length') else 'unknown', num_predict=body['options']['num_predict'],
                           num_ctx=self.config.context, think=False, structured=schema is not None)
            metrics['input_upper_bound']=budget['input_upper_bound']
            message = value.get('message', {})
            metrics['content_chars'] = len(message.get('content', '')) if isinstance(message, dict) and isinstance(message.get('content'), str) else 0
            metrics['thinking_chars'] = len(message.get('thinking', '')) if isinstance(message, dict) and isinstance(message.get('thinking', ''), str) else 0
            LOG.info('model_response metrics=%s', metrics)
            if value.get('done_reason') == 'length':
                raise ModelResponseError(f"Response truncated at output limit ({metrics['eval_count']}/{metrics['num_predict']} tokens; thinking chars={metrics['thinking_chars']}). Partial output was not accepted. Split or shorten the task, then review and re-approve it.", metrics)
            if value.get('done') is not True or value.get('done_reason') != 'stop':
                raise ModelResponseError('Incomplete or malformed model response: missing completion or unexpected done_reason. No output accepted.', metrics)
            if type(message) is not dict or message.get('tool_calls') or type(message.get('content')) is not str:
                raise ModelResponseError('Unexpected model response; text content required and tool calls are not accepted.', metrics)
            content = message['content']
            if not content.strip() or len(content) > 40_000:
                raise ModelResponseError('Empty or oversized model output. No output accepted.', metrics)
            from .models import text
            text(content, 'Model output', 40000)
            return content, metrics
