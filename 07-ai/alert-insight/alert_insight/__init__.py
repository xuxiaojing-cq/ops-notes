"""alert-insight：Alertmanager 告警的聚合与归因工具。

分层：normalize → aggregate → diagnose →（可选）llm → render
设计取舍见各模块文档字符串。
"""

from .aggregate import Incident, aggregate, detect_flapping
from .diagnose import Finding, classify, diagnose
from .llm import LLMClient, LLMResult, enhance
from .normalize import Alert, PayloadError, normalize, normalize_many
from .render import EXIT_CRITICAL, EXIT_OK, EXIT_WARNING, exit_code, render_json, render_markdown

__all__ = [
    "Alert",
    "PayloadError",
    "normalize",
    "normalize_many",
    "Incident",
    "aggregate",
    "detect_flapping",
    "Finding",
    "classify",
    "diagnose",
    "LLMClient",
    "LLMResult",
    "enhance",
    "render_markdown",
    "render_json",
    "exit_code",
    "EXIT_OK",
    "EXIT_WARNING",
    "EXIT_CRITICAL",
]

__version__ = "0.1.0"
