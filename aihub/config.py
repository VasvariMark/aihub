from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    model: str = 'qwen3.5:9b'
    context: int = 4096
    port: int = 8765
    timeout: int = 90

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model.strip() or len(self.model) > 160:
            raise ValueError('Invalid model name.')
        if type(self.context) is not int or not 2048 <= self.context <= 8192:
            raise ValueError('Context must be between 2048 and 8192.')
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ValueError('Port must be between 1024 and 65535.')
