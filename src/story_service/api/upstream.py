"""The connection point for modules 1 to 5 (S0 to S5).

Section 3: POST /v1/stories takes a lesson topic and returns 202; modules 1 to 5 turn it into the story's intake,
script and cast, and module 6 builds from there. Those modules are written by the rest of the team, so module 6 only
defines the one door they come in through:

    class MyModules:
        def run(self, request: dict) -> dict:
            # request: the body of POST /v1/stories (external, lesson_text, topic, metadata, …)
            ...                                   # S0 intake, S1 concepts, S2 idea, S3–S4 script, S5 cast
            return {"intake": {...}, "script": {...}, "cast": {...}}   # the shapes of 05_cast_setting / module 6 input
            # optional "plans": {"s01": {...}} skips the director (S7) for those scenes

A refusal the plan names (INPUT_TOO_SHORT, TOPIC_NOT_FOUND, LANGUAGE_MISMATCH, …) is raised as
`UpstreamError(code, message)` and the version ends `failed` with that code (Section 12.1).

Until the modules exist, POST /v1/stories answers 501 NOT_CONNECTED. To connect them later, either pass the object to
`create_app(upstream=...)`, or set the environment variable STORY_UPSTREAM to "package.module:name" (an object with
`run`, or a class that is created with no arguments). Plain Python, no FastAPI."""

from __future__ import annotations

import importlib
from typing import Protocol


class Upstream(Protocol):
    def run(self, request: dict) -> dict: ...


class UpstreamError(Exception):
    """Modules 1 to 5 refused the request with one of the plan's codes."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def load_upstream(spec: str | None) -> Upstream | None:
    """The object STORY_UPSTREAM names ("package.module:name"), or None when it is not set."""
    if not spec:
        return None
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError("STORY_UPSTREAM must look like package.module:name")
    obj = getattr(importlib.import_module(module_name), attr)
    obj = obj() if isinstance(obj, type) else obj
    if not callable(getattr(obj, "run", None)):
        raise ValueError(f"STORY_UPSTREAM {spec} has no run(request) method")
    return obj
