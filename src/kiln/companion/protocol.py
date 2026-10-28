"""Wire messages between the server and a companion.

Deliberately minimal (docs/local-companion.md): hello, tool_call, tool_result,
ping/pong. Tool inputs are the validated models from kiln.tools.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

TOOLS = ("read_file", "write_file", "run_tests")


class Hello(BaseModel):
    """First message from the companion: claims a pair code."""

    type: Literal["hello"] = "hello"
    code: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    version: str = Field(min_length=1)


class HelloAck(BaseModel):
    type: Literal["hello_ack"] = "hello_ack"
    run_id: str = Field(min_length=1)


class ToolCall(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    input: dict


class ToolResult(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    id: str = Field(min_length=1)
    ok: bool
    output: str = ""
    error: str = ""


class Ping(BaseModel):
    type: Literal["ping"] = "ping"


class Pong(BaseModel):
    type: Literal["pong"] = "pong"


ServerMessage = Annotated[HelloAck | ToolCall | Ping, Field(discriminator="type")]
CompanionMessage = Annotated[Hello | ToolResult | Pong, Field(discriminator="type")]


def decode_companion_message(payload: str) -> Hello | ToolResult | Pong:
    from pydantic import TypeAdapter

    return TypeAdapter(CompanionMessage).validate_json(payload)
