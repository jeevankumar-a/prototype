from dataclasses import dataclass


@dataclass
class FileSnapshot:
    name: str
    text: str
    digest: str
    traceparent: str


@dataclass
class Diagram:
    source: FileSnapshot
    mermaid: str
