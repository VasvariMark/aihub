"""Local registry. External workers only describe a manual destination."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    mode: str
    capability: str = "text"
    roles: tuple = ("researcher", "writer", "coder", "reviewer")

    def public(self):
        return dict(asdict(self), status="Manual handoff available" if self.mode == "manual" else "See Ollama health")


REGISTRY = {
    "ollama": Provider("ollama", "Local Ollama", "local"),
    "manual_codex": Provider("openai_codex", "Codex", "manual"),
    "manual_claude": Provider("anthropic", "Claude", "manual"),
    "manual_gemini": Provider("google", "Gemini", "manual"),
}


def recommendation(role, privacy, preferred=None):
    if privacy == "local_only":
        return "ollama", "Project policy keeps this task local."
    if preferred in REGISTRY:
        return preferred, 'Preferred worker in the saved project profile.'
    choices = {"coder": "manual_codex", "reviewer": "manual_gemini", "writer": "manual_claude"}
    return choices.get(role, "ollama"), "Role preference only. Choose another permitted worker before approval."
