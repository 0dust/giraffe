from __future__ import annotations

from giraffe.models import RunConfig, ToolFixture


def default_tool_fixture() -> ToolFixture:
    return ToolFixture(
        fixture_id="weather",
        messages=[{"role": "user", "content": "Use get_weather to check the weather in Delhi."}],
        tools=[{
            "type": "function",
            "function": {
                "name": "get_weather", "description": "Return the weather for a city.",
                "parameters": {
                    "type": "object", "properties": {"city": {"type": "string"}},
                    "required": ["city"], "additionalProperties": False,
                },
            },
        }],
        expected_calls=[{"name": "get_weather", "arguments": {"city": "Delhi"}}],
    )


def resolved_tool_fixtures(config: RunConfig) -> list[ToolFixture]:
    if config.tool_checks and config.tool_checks.fixtures:
        return config.tool_checks.fixtures
    return [default_tool_fixture()]


def resolved_tool_modes(config: RunConfig) -> list[str]:
    if config.tool_checks and config.tool_checks.modes is not None:
        return config.tool_checks.modes
    return ["complete", "stream"] if config.stream else ["complete"]
