from dataclasses import dataclass
from rich.console import Console
import datetime
import ollama
from typing import Literal
from zoneinfo import ZoneInfo

MODEL = "gemma4:e4b"

console = Console()

USER_COLOR = "cyan"
ASSISTANT_COLOR = "green"
THINKING_COLOR = "yellow"
TOOL_COLOR = "magenta"

EventType = Literal["meeting", "habit", "task", "deadline"]


@dataclass
class Event:
    name: str
    type: EventType
    date: datetime.date
    time_start: datetime.time
    time_end: datetime.time
    description: str


@dataclass
class Notes:
    title: str
    date: datetime.date
    content: str


@dataclass
class Reminder:
    name: str
    description: str
    date: datetime.date
    time: datetime.time


# Functions for agent to call

def get_current_time():
    """
    Returns the current date and time in Poland.
    """
    now = datetime.datetime.now(ZoneInfo("Europe/Warsaw"))

    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": now.strftime("%A"),
    }


# Tool registry mapping tool names to Python functions

tool_registry = {
    "get_current_time": get_current_time,
}


# Description of the tools for the LLM

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": (
                "Returns the current date, time, "
                "day of the week."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    }
]

SYSTEM_PROMPT = """
You are a personal assistant.
You manage my daily schedule, including events, notes, and reminders.
You can create, read, update, and delete events, notes, and reminders.
Short answers are preferred.
You have access to tools.
Do NOT make up information.
Do NOT talk and type in markdown format.
Do NOT use emotjs in your responses.
"""


def run_agent(user_message: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    while True:

        with console.status("[bold yellow]Thinking...[/bold yellow]", spinner="dots"):
            response = ollama.chat(
                model=MODEL,
                messages=messages,
                tools=tools,
            )

        assistant_message = response["message"]

        messages.append(assistant_message)

        tool_calls = assistant_message.get("tool_calls")
        if tool_calls:
            for tool_call in tool_calls:
                tool_name = tool_call["function"]["name"]
                arguments = tool_call["function"].get("arguments", {})

                console.print(
                    f"[{TOOL_COLOR}]→ using tool: {tool_name}[/{TOOL_COLOR}]"
                )

                if tool_name not in tool_registry:
                    raise ValueError(f"Unknown tool: {tool_name}")

                function = tool_registry[tool_name]

                with console.status(
                    f"[bold {TOOL_COLOR}]Running {tool_name}...[/bold {TOOL_COLOR}]",
                    spinner="arc",
                ):
                    result = function(**arguments)

                console.print(
                    f"[{TOOL_COLOR}]← result: {result}[/{TOOL_COLOR}]"
                )

                messages.append(
                    {
                        "role": "tool",
                        "content": str(result),
                    }
                )
            # loop back and let the model see the tool result
            continue

        return assistant_message["content"]


def main():
    console.print("[bold]Calendar Agent[/bold] — type 'exit' to quit\n")

    while True:
        user_message = console.input(
            f"[bold {USER_COLOR}]You:[/bold {USER_COLOR}] "
        )

        if user_message.strip().lower() in {"exit", "quit"}:
            break

        answer = run_agent(user_message)

        console.print(
            f"[bold {ASSISTANT_COLOR}]Agent:[/bold {ASSISTANT_COLOR}] {answer}\n"
        )


if __name__ == "__main__":
    main()