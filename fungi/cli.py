"""Console single-shot mode: `python -m fungi "question"`."""

from fungi.config import load_config
from fungi.events import ConsoleSink
from fungi.trilayer import TriLayer


def run_single_shot(query: str) -> int:
    cfg = load_config()
    if not cfg.configured:
        print("ERROR: API key not configured. Edit config.json (GUI: 设置 page).")
        return 1
    sink = ConsoleSink()
    trilayer = TriLayer(cfg, sink)
    agent = trilayer.build_orchestrator(sink)
    messages = [{"role": "user", "content": query}]
    print()
    agent.run(messages)
    print()
    return 0
