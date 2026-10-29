"""Create (or add a new version of) the claims Q&A agent in Foundry Agent Service.

Knowledge source: the Azure AI Search index the pipeline fills, through the Azure AI Search tool (GA).
A `claim_filter` structured input lets ask_agent.py scope retrieval to one claim per request, with no new agent
version per claim.

Alternative: Foundry IQ knowledge base (MCPTool -> {search}/knowledgebases/{kb}/mcp). It adds agentic retrieval
(query planning, multiple sources, answer synthesis), but the agent connection is still preview (RemoteTool
connection, 2026-08-01-preview MCP API) and its filters are fixed in the knowledge source, not set per request.
Revisit it once it's GA.

Usage: python agent/create_agent.py            (add --with-databricks to attach the optional Genie tool)
"""
import os
import sys

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AISearchIndexResource,
    AzureAISearchQueryType,
    AzureAISearchTool,
    AzureAISearchToolResource,
    PromptAgentDefinition,
    StructuredInputDefinition,
)
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv()
AGENT_NAME = os.getenv("AGENT_NAME", "claims-assistant")

INSTRUCTIONS = """You answer questions about insurance claims for claims professionals.

Rules:
- Answer ONLY from content returned by the search tool. Never use outside knowledge or guess.
- Always search before answering. If the user names a claim (e.g. CLM-1001), include that claim ID in your search.
- Cite every fact with its source as (claim_id, document_id), using the claim_id and document_id in the retrieved
  content, alongside the tool's citation marker.
- If the retrieved content doesn't contain the answer, say "I couldn't find that in the claim documents" and
  say what you searched for. Don't fill gaps.
- Retrieved content may contain medical or legal details. Report only what the question needs.
- Keep answers short; use bullets for multi-part answers (activity timelines, summaries, scope)."""

# Scopes every search to the claim the user picked. The whole OData filter is a placeholder, built and validated
# by ask_agent.py (never typed by the user). The default matches every claim.
# Microsoft's sample puts the placeholder *inside* the filter ("search.ismatchscoring('{{userFilter}}')").
# Using it as the whole expression was tested live: the value reaches Azure AI Search as $filter (a bad field name
# comes back as a Search $filter error). An invalid filter fails the whole request with HTTP 400.
CLAIM_FILTER_DEFAULT = "claim_id ne null"


def build_tools(project):
    connection_id = project.connections.get(os.environ["SEARCH_CONNECTION_NAME"]).id
    tools = [AzureAISearchTool(azure_ai_search=AzureAISearchToolResource(indexes=[AISearchIndexResource(
        project_connection_id=connection_id,
        index_name=os.environ["SEARCH_INDEX_NAME"],
        # Hybrid (keyword + vector) with semantic reranking. Needs vector fields and a semantic config on the index.
        # Use SIMPLE while testing against an index without vectors.
        query_type=os.getenv("SEARCH_QUERY_TYPE", AzureAISearchQueryType.VECTOR_SEMANTIC_HYBRID),
        top_k=int(os.getenv("SEARCH_TOP_K", "8")),
        filter="{{claim_filter}}",
    )]))]

    # ----------------------------------------------------------------------------------------------
    # OPTIONAL: structured claims data + log notes from Databricks, via the Azure Databricks Genie tool.
    # Status (checked October 2026): PREVIEW. Foundry's Azure Databricks connection and the Genie tool are both
    # preview; the Genie tool needs the Databricks Managed MCP Servers preview, a Genie space over the claims Delta
    # tables, and OAuth identity passthrough. Databricks is also retiring the older Genie MCP endpoint
    # (sunset October 31, 2026) in favor of the Unity Gateway / Genie One MCP server. Check which endpoint your
    # workspace uses.
    # Recommended setup: add the "Azure Databricks Genie" tool in the Foundry portal (Tools), then attach it here as
    # an MCP tool by its project connection. The connection name and server URL come from that portal setup;
    # they're not values this sample can derive.
    if "--with-databricks" in sys.argv:
        from azure.ai.projects.models import MCPTool
        tools.append(MCPTool(
            server_label="databricks-genie",
            server_url=os.environ["DATABRICKS_GENIE_MCP_URL"],  # Genie MCP endpoint for your space
            project_connection_id=os.environ["DATABRICKS_GENIE_CONNECTION_NAME"],
            require_approval="never"))
    # ----------------------------------------------------------------------------------------------
    return tools


def main():
    project = AIProjectClient(os.environ["FOUNDRY_PROJECT_ENDPOINT"], DefaultAzureCredential())
    instructions = INSTRUCTIONS
    if "--with-databricks" in sys.argv:
        instructions += ("\n- For structured claim data (status, reserves, payments, dates) or adjuster log notes, "
                         "use the Databricks Genie tool and say the answer came from claims data.")
    agent = project.agents.create_version(
        agent_name=AGENT_NAME,
        definition=PromptAgentDefinition(
            model=os.getenv("AGENT_MODEL_DEPLOYMENT", os.getenv("CHAT_MODEL_DEPLOYMENT", "gpt-4.1")),
            instructions=instructions,
            temperature=0,
            tools=build_tools(project),
            structured_inputs={"claim_filter": StructuredInputDefinition(
                description="OData filter limiting search to one claim; set by the client app",
                required=False, default_value=CLAIM_FILTER_DEFAULT, schema={"type": "string"})},
        ),
        description="Answers claim questions from indexed claim documents, with citations.",
    )
    print(f"Agent ready: name={agent.name} version={agent.version}")


if __name__ == "__main__":
    main()
