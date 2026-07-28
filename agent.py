import ollama
from datetime import datetime
from zoneinfo import ZoneInfo


MODEL = "gemma4:e4b"


# ============================================================
# 1. PRAWDZIWE NARZĘDZIE PYTHONA
# ============================================================

def get_current_time():
    """
    Returns the current date and time in Poland.
    """

    now = datetime.now(
        ZoneInfo("Europe/Warsaw")
    )

    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": now.strftime("%A"),
        "timezone": "Europe/Warsaw"
    }


# ============================================================
# 2. REGISTRY - MAPA NAZW NARZĘDZI NA FUNKCJE PYTHONA
# ============================================================

tool_registry = {
    "get_current_time": get_current_time
}


# ============================================================
# 3. OPIS NARZĘDZIA DLA LLM
# ============================================================

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": (
                "Returns the current date, time, "
                "day of the week and timezone."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    }
]


# ============================================================
# 4. AGENT LOOP
# ============================================================

def run_agent(user_message):

    messages = [
        {
            "role": "system",
            "content": """
You are a personal assistant.
You have access to tools.
Do not make up information. 
Do not talk and type in markdown format.

"""
        },
        {
            "role": "user",
            "content": user_message
        }
    ]

    while True:

        # ----------------------------------------------------
        # LLM OTRZYMUJE:
        #
        # 1. historię rozmowy
        # 2. dostępne narzędzia
        #
        # I PODEJMUJE DECYZJĘ
        # ----------------------------------------------------

        response = ollama.chat(
            model=MODEL,
            messages=messages,
            tools=tools
        )

        assistant_message = response["message"]

        # Dodajemy odpowiedź LLM do historii
        messages.append(assistant_message)

        # ----------------------------------------------------
        # JEŚLI LLM CHCE UŻYĆ NARZĘDZIA
        # ----------------------------------------------------

        if "tool_calls" in assistant_message:

            for tool_call in assistant_message["tool_calls"]:

                tool_name = tool_call["function"]["name"]

                arguments = tool_call["function"].get(
                    "arguments",
                    {}
                )

                print(
                    f"\n[Agent wants to use tool: "
                    f"{tool_name}]"
                )

                # ------------------------------------------------
                # SZUKAMY FUNKCJI W REGISTRY
                # ------------------------------------------------

                if tool_name not in tool_registry:

                    raise ValueError(
                        f"Unknown tool: {tool_name}"
                    )

                function = tool_registry[tool_name]

                # ------------------------------------------------
                # WYKONUJEMY PRAWDZIWĄ FUNKCJĘ PYTHONA
                # ------------------------------------------------

                result = function(
                    **arguments
                )

                print(
                    f"[Tool result: {result}]"
                )

                # ------------------------------------------------
                # WYSYŁAMY WYNIK Z POWROTEM DO LLM
                # ------------------------------------------------

                messages.append(
                    {
                        "role": "tool",
                        "content": str(result)
                    }
                )

            # --------------------------------------------
            # LLM DOSTAŁ WYNIK NARZĘDZIA.
            #
            # WRACAMY NA POCZĄTEK PĘTLI.
            # --------------------------------------------

            continue

        # ----------------------------------------------------
        # JEŚLI NIE MA TOOL CALL, LLM ODPOWIEDZIAŁ
        # ----------------------------------------------------

        return assistant_message["content"]


# ============================================================
# 5. URUCHOMIENIE AGENTA
# ============================================================

if __name__ == "__main__":

    user_message = input(
        "You: "
    )

    answer = run_agent(
        user_message
    )

    print(
        f"\nAgent: {answer}"
    )