"""Minimal CLI for the claims agent: ask a question, print the answer and its citations.

Usage:
  python agent/ask_agent.py                     # search all claims
  python agent/ask_agent.py --claim CLM-1001    # scope every question to one claim
Inside the loop, type `/claim CLM-1001` to switch claims, `/claim` to clear the filter, or `exit` to quit.
"""
import os
import re
import sys

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv()
AGENT_NAME = os.getenv("AGENT_NAME", "claims-assistant")
# Claim IDs go into an OData filter, so allow only safe characters. This blocks filter injection.
CLAIM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def claim_filter(claim_id):
    if claim_id is None:
        return None  # the agent's default filter matches every claim
    if not CLAIM_ID.match(claim_id):
        raise ValueError(f"Invalid claim id: {claim_id!r}")
    return f"claim_id eq '{claim_id}'"


def print_response(response):
    print(f"\n{response.output_text}\n")
    seen = set()
    for item in response.output:
        if item.type != "message":
            continue
        for part in item.content:
            for ann in getattr(part, "annotations", None) or []:
                if ann.type == "url_citation" and ann.url not in seen:
                    seen.add(ann.url)
                    print(f"  [source] {getattr(ann, 'title', '') or ''} {ann.url}")


def main():
    claim = sys.argv[sys.argv.index("--claim") + 1] if "--claim" in sys.argv else None
    project = AIProjectClient(os.environ["FOUNDRY_PROJECT_ENDPOINT"], DefaultAzureCredential())
    openai = project.get_openai_client(agent_name=AGENT_NAME)  # routes responses to the named agent
    conversation = openai.conversations.create()  # keeps follow-up questions in context
    print(f"Claims assistant ({AGENT_NAME}). Claim filter: {claim or 'all claims'}. Type 'exit' to quit.")

    while True:
        question = input("> ").strip()
        if question.lower() in ("exit", "quit"):
            break
        if question.startswith("/claim"):
            claim = question.split(maxsplit=1)[1] if " " in question else None
            print(f"Claim filter: {claim or 'all claims'}")
            continue
        if not question:
            continue
        try:
            inputs = {"claim_filter": claim_filter(claim)} if claim else {}
            response = openai.responses.create(
                conversation=conversation.id, input=question,
                extra_body={"structured_inputs": inputs} if inputs else None)
            print_response(response)
        except Exception as e:  # noqa: BLE001 - keep the loop alive on bad input or transient errors
            print(f"Error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
