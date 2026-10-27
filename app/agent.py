"""Gemini on Vertex AI as a router: question -> which tool and with which arguments.

Function calling without the automatic loop. The model returns only the call
(name + args), the tool result is not sent back, so we pay only for the question,
the tool declarations and the short answer.
"""

from dataclasses import dataclass
from datetime import date

from google import genai
from google.genai import types

from tools import PROJECT, TOOLS

LOCATION = "global"  # Gemini 3.x is not available in us-central1 (404), only on the global endpoint
MODEL = "gemini-3.5-flash-lite"

# USD per 1M tokens for MODEL. Taken from third-party price lists (2026-10-27),
# verify on https://cloud.google.com/vertex-ai/generative-ai/pricing and update.
# Thinking tokens are billed as output.
PRICE_USD_PER_1M = {"input": 0.30, "output": 2.50}

SYSTEM = (
    "You help users find movies in a movie catalog. "
    "If a tool fits the question, call exactly one tool. "
    "If no tool fits, do not call any tool and briefly say in the user's language what you can search for."
)

_client: genai.Client | None = None


def _ai() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    return _client


def _config(today: date | None = None) -> types.GenerateContentConfig:
    declarations = [
        types.FunctionDeclaration(
            name=name,
            description=decl["description"],
            parameters_json_schema=decl["parameters"],
        )
        for name, (_, decl) in TOOLS.items()
    ]
    # today's date lets the model turn 'next week' etc. into YYYY-MM-DD
    return types.GenerateContentConfig(
        system_instruction=f"{SYSTEM} Today is {today or date.today():%A, %Y-%m-%d}.",
        tools=[types.Tool(function_declarations=declarations)],
        tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="AUTO")),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0,
    )


@dataclass
class Decision:
    tool: str | None       # None = the model did not call any tool
    args: dict
    text: str | None       # model's text answer when no tool is called
    input_tokens: int
    output_tokens: int
    thinking_tokens: int

    @property
    def cost_usd(self) -> float:
        billed_output = self.output_tokens + self.thinking_tokens
        return (self.input_tokens * PRICE_USD_PER_1M["input"]
                + billed_output * PRICE_USD_PER_1M["output"]) / 1_000_000


def decide(question: str, today: date | None = None) -> Decision:
    """today: fixed date for evaluation, so relative dates in the golden set stay valid."""
    resp = _ai().models.generate_content(model=MODEL, contents=question, config=_config(today))
    usage = resp.usage_metadata
    tokens = (usage.prompt_token_count or 0, usage.candidates_token_count or 0, usage.thoughts_token_count or 0)
    calls = resp.function_calls or []
    if calls:
        return Decision(calls[0].name, dict(calls[0].args or {}), None, *tokens)
    return Decision(None, {}, resp.text, *tokens)


def count_input_tokens(question: str) -> int:
    """Input size of one question including tool declarations. Free, no generation."""
    cfg = _config()
    resp = _ai().models.count_tokens(
        model=MODEL,
        contents=question,
        config=types.CountTokensConfig(system_instruction=cfg.system_instruction, tools=cfg.tools),
    )
    return resp.total_tokens
