from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential

# Import necessary modules for the compare_documents function
from quart import Blueprint, request, jsonify
import uuid
import os
import json
import logging
import asyncio
import numpy as np

# Create Blueprint for document comparison
bp = Blueprint("routes", __name__, static_folder="static", template_folder="static")

# Document Comparison API
@bp.route("/compare-documents", methods=["POST"]) # Decorator 
async def compare_documents(): # Function that is being Decorated
    data = await request.files
    reference_file = data.get("reference")
    candidate_file = data.get("candidate")
    if not reference_file or not candidate_file:
        return jsonify({"error": "Both reference and candidate documents are required."}), 400

    # Save files temporarily
    ref_path = f"/tmp/{uuid.uuid4()}_reference.pdf" #uuid.uuid4() to generate unique Identifier so each request gets its own unique temporary files
    cand_path = f"/tmp/{uuid.uuid4()}_candidate.pdf"
    await reference_file.save(ref_path)
    await candidate_file.save(cand_path)

    # Extract sections using Document Intelligence
    endpoint = app_settings.document_intelligence.endpoint or os.getenv("DOCUMENT_INTELLIGENCE_ENDPOINT")
    key = app_settings.document_intelligence.key or os.getenv("DOCUMENT_INTELLIGENCE_KEY") # Connecting to Azure document reading service with my credentials
    
    if not endpoint or not key:
        return jsonify({"error": "Document Intelligence configuration is missing. Please check DOCUMENT_INTELLIGENCE_ENDPOINT and DOCUMENT_INTELLIGENCE_KEY environment variables."}), 500
    
    # Use async context manager for proper cleanup
    async with DocumentIntelligenceClient(endpoint, AzureKeyCredential(key)) as doc_client:
        async def extract_sections(file_path):
            with open(file_path, "rb") as f:
                poller = await doc_client.begin_analyze_document("prebuilt-read", body=f)
                result = await poller.result()
            # Group by paragraphs 
            sections = []
            for i, paragraph in enumerate(result.paragraphs):
                sections.append({
                    "sectionId": str(i+1),
                    "text": paragraph.content
                })
            return sections
        
        # Extract sections from both docs - applying it
        ref_sections = await extract_sections(ref_path)
        cand_sections = await extract_sections(cand_path)

    # Match sections by order 
    comparison_results = []
    total_score = 0
    
    # Create a single AI client for all comparisons and properly manage it
    ai_foundry_client = await init_ai_foundry_client()
    try:
        # Pre-compute embeddings for all sections (more efficient)
        async def get_embeddings(texts):
            """Get embeddings for semantic similarity"""
            try:
                response = await ai_foundry_client.embeddings.create(
                    model="text-embedding-ada-002",  
                    input=texts
                )
                return [embedding.embedding for embedding in response.data]
            except Exception as e:
                logging.warning(f"Embeddings failed: {e}")
                return None
        
        # Get embeddings for all sections
        ref_texts = [sec['text'] for sec in ref_sections]
        cand_texts = [sec['text'] for sec in cand_sections]
        
        ref_embeddings = await get_embeddings(ref_texts)
        cand_embeddings = await get_embeddings(cand_texts)
        
        def cosine_similarity(vec1, vec2):
            """Calculate cosine similarity between two vectors"""
            try:
                return np.dot(vec1, vec2) / (np.linalg.norm(vec1) * np.linalg.norm(vec2))
            except (ImportError, AttributeError):
                # Fallback to manual calculation if numpy not available
                dot_product = sum(a * b for a, b in zip(vec1, vec2))
                magnitude1 = sum(a * a for a in vec1) ** 0.5
                magnitude2 = sum(b * b for b in vec2) ** 0.5
                return dot_product / (magnitude1 * magnitude2) if magnitude1 * magnitude2 > 0 else 0
        
        async def enhanced_compare_section(i, ref_sec, cand_sec, semantic_score=None):
            """Enhanced comparison with semantic similarity pre-computation"""
            
            # Faster semantic similarity check
            if semantic_score is not None:
                if semantic_score > 0.95:  # Very high similarity
                    return {
                        "sectionId": ref_sec["sectionId"],
                        "matchScore": 1.0,
                        "semanticSimilarity": semantic_score,
                        "status": "✅",
                        "explanation": f"Sections are semantically identical (similarity: {semantic_score:.3f})",
                        "method": "embedding_only"
                    }
                elif semantic_score < 0.3:  # Very low similarity
                    return {
                        "sectionId": ref_sec["sectionId"],
                        "matchScore": 0.0,
                        "semanticSimilarity": semantic_score,
                        "status": "❌",
                        "explanation": f"Sections are semantically different (similarity: {semantic_score:.3f})",
                        "method": "embedding_only"
                    }
            
            # For moderate similarity, use AI for detailed analysis
            similarity_text = f"{semantic_score:.3f}" if semantic_score is not None else "unknown"
            prompt = f"""
            You are an expert document analyst. The sections below have a semantic similarity of {similarity_text}.
            Perform a detailed comparison to determine the exact match quality.
            
            REFERENCE SECTION (Ground Truth):
            {ref_sec['text']}
            
            CANDIDATE SECTION (To be evaluated):
            {cand_sec['text']}
            
            
            Given the semantic similarity score of {similarity_text}, focus on:
            1. **Content Accuracy**: Are the facts and details correct?
            2. **Completeness**: What key information is missing?
            3. **Context Preservation**: Is the meaning preserved?
            4. **Critical Differences**: What are the most important discrepancies?
            
            Respond with a JSON object:
            {{
                "matchScore": <number between 0.0-1.0>,
                "contentAccuracy": <number between 0.0-1.0>,
                "completeness": <number between 0.0-1.0>,
                "explanation": "<detailed analysis>",
                "keyDifferences": ["<list of important differences>"],
                "confidence": <number between 0.0-1.0>
            }}
            """
            
            try:
                response = await ai_foundry_client.chat.completions.create(
                    model=app_settings.azure_openai.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,  # Lower temperature for more consistent analysis
                    max_tokens=512,   # More tokens for detailed analysis
                    timeout=30.0
                )
            except asyncio.CancelledError:
                return {
                    "sectionId": ref_sec["sectionId"],
                    "matchScore": 0,
                    "semanticSimilarity": semantic_score or 0,
                    "status": "❌",
                    "explanation": "Request was cancelled - possibly due to timeout",
                    "method": "ai_cancelled"
                }
            except Exception as e:
                logging.error(f"Error comparing section {i}: {e}")
                return {
                    "sectionId": ref_sec["sectionId"],
                    "matchScore": 0,
                    "semanticSimilarity": semantic_score or 0,
                    "status": "❌",
                    "explanation": f"Comparison failed: {str(e)}",
                    "method": "ai_error"
                }
                
            content = response.choices[0].message.content.strip()
            
            # Parse enhanced response
            try:
                result_json = json.loads(content)
                match_score = float(result_json.get("matchScore", 0))
                explanation = result_json.get("explanation", "")
                
                # Combine AI analysis with semantic similarity
                final_score = (match_score + semantic_score) / 2 if semantic_score else match_score
                
                status = "✅" if final_score >= 0.8 else ("⚠️" if final_score >= 0.5 else "❌")
                
                return {
                    "sectionId": ref_sec["sectionId"],
                    "matchScore": final_score,
                    "semanticSimilarity": semantic_score or 0,
                    "aiAnalysis": match_score,
                    "contentAccuracy": result_json.get("contentAccuracy", 0),
                    "completeness": result_json.get("completeness", 0),
                    "status": status,
                    "explanation": explanation,
                    "keyDifferences": result_json.get("keyDifferences", []),
                    "confidence": result_json.get("confidence", 0.5),
                    "method": "hybrid_ai_embedding"
                }
            except Exception as e:
                logging.warning(f"Failed to parse AI response: {e}")
                fallback_score = semantic_score or 0
                return {
                    "sectionId": ref_sec["sectionId"],
                    "matchScore": fallback_score,
                    "semanticSimilarity": semantic_score or 0,
                    "status": "⚠️",
                    "explanation": f"AI analysis failed, using semantic similarity: {content}",
                    "method": "embedding_fallback"
                }
        
        # Process sections with hybrid approach
        comparison_results = []
        total_score = 0
        
        for i, ref_sec in enumerate(ref_sections):
            cand_sec = cand_sections[i] if i < len(cand_sections) else {"text": ""}
            
            # Calculate semantic similarity if embeddings are available
            semantic_score = None
            if ref_embeddings and cand_embeddings and i < len(cand_embeddings):
                semantic_score = cosine_similarity(ref_embeddings[i], cand_embeddings[i])
            
            result = await enhanced_compare_section(i, ref_sec, cand_sec, semantic_score)
            comparison_results.append(result)
            total_score += result["matchScore"]
        
    finally:
        # Ensure the AI client is properly closed
        if hasattr(ai_foundry_client, 'close'):
            await ai_foundry_client.close()

    compliance_score = int(100 * total_score / max(1, len(ref_sections)))
    result = {
        "complianceScore": compliance_score,
        "sections": comparison_results
    }
    # Clean up temp files
    os.remove(ref_path)
    os.remove(cand_path)
    return jsonify(result), 200
import json
import logging
import os
import uuid
import re
import ast
import requests
import asyncio
import asyncio
import json
import logging
import os
import re
import ast
import uuid
from typing import Dict, Any, AsyncGenerator

from azure.identity.aio import DefaultAzureCredential
from azure.identity import DefaultAzureCredential as DefaultAzureCredentialSync
from quart import (Blueprint, Quart, jsonify, make_response, render_template,
                   request, send_from_directory)

from backend.auth.auth_utils import get_authenticated_user_details
from backend.history.cosmosdbservice import CosmosConversationClient
from backend.settings import (
    MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION, app_settings)
from backend.utils import (ChatType, format_as_ndjson,
                           format_non_streaming_response,
                           format_stream_response)
from event_utils import track_event_if_configured
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from azure.ai.projects.aio import AIProjectClient
from azure.ai.agents.models import (
    MessageRole,
    RunStepToolCallDetails,
    MessageDeltaChunk,
    ThreadRun,
    MessageDeltaTextContent,
    MessageDeltaTextUrlCitationAnnotation
)
from backend.api.agent.section_agent_factory import SectionAgentFactory
from backend.api.agent.browse_agent_factory import BrowseAgentFactory
from backend.api.agent.template_agent_factory import TemplateAgentFactory

# Check if the Application Insights Instrumentation Key is set in the environment variables
instrumentation_key = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
if instrumentation_key:
    # Configure Application Insights if the Instrumentation Key is found
    configure_azure_monitor(connection_string=instrumentation_key)
    logging.info("Application Insights configured with the provided Instrumentation Key")
else:
    # Log a warning if the Instrumentation Key is not found
    logging.warning("No Application Insights Instrumentation Key found. Skipping configuration")

# Configure logging
logging.basicConfig(level=logging.INFO)

# Suppress INFO logs from 'azure.core.pipeline.policies.http_logging_policy'
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(
    logging.WARNING
)
logging.getLogger("azure.identity.aio._internal").setLevel(logging.WARNING)

# Suppress info logs from OpenTelemetry exporter
logging.getLogger("azure.monitor.opentelemetry.exporter.export._base").setLevel(
    logging.WARNING
)


def create_app():
    app = Quart(__name__)
    app.register_blueprint(bp)
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config['PROVIDE_AUTOMATIC_OPTIONS'] = True
    return app


@bp.route("/")
async def index():
    return await render_template(
        "index.html", title=app_settings.ui.title, favicon=app_settings.ui.favicon
    )


@bp.route("/favicon.ico")
async def favicon():
    return await bp.send_static_file("favicon.ico")


@bp.route("/assets/<path:path>")
async def assets(path):
    return await send_from_directory("static/assets", path)


# Debug settings
DEBUG = os.environ.get("DEBUG", "false")
if DEBUG.lower() == "true":
    logging.basicConfig(level=logging.DEBUG)

USER_AGENT = "GitHubSampleWebApp/AsyncAzureOpenAI/1.0.0"


# Frontend Settings via Environment Variables
frontend_settings = {
    "auth_enabled": app_settings.base_settings.auth_enabled,
    "feedback_enabled": (
        app_settings.chat_history and app_settings.chat_history.enable_feedback
    ),
    "ui": {
        "title": app_settings.ui.title,
        "logo": app_settings.ui.logo,
        "chat_logo": app_settings.ui.chat_logo or app_settings.ui.logo,
        "chat_title": app_settings.ui.chat_title,
        "chat_description": app_settings.ui.chat_description,
        "show_share_button": app_settings.ui.show_share_button,
    },
    "sanitize_answer": app_settings.base_settings.sanitize_answer,
}


# Enable Microsoft Defender for Cloud Integration
MS_DEFENDER_ENABLED = os.environ.get("MS_DEFENDER_ENABLED", "true").lower() == "true"


# Initialize Azure Foundry SDK client
async def init_ai_foundry_client():
    ai_foundry_client = None
    try:
        track_event_if_configured("AIFoundryClientInitializationStart", {"status": "success"})
        # API version check
        if (
            app_settings.azure_openai.preview_api_version
            < MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION
        ):
            raise ValueError(
                f"The minimum supported Azure OpenAI preview API version is"
                f"'{MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION}'"
            )

        # Project Endpoint check
        if (
            not app_settings.azure_ai.agent_endpoint
        ):
            raise ValueError(
                "AZURE_AI_AGENT_ENDPOINT is required"
            )

        ai_project_client = AIProjectClient(
            endpoint=app_settings.azure_ai.agent_endpoint,
            credential=DefaultAzureCredential()
        )
        track_event_if_configured("AIFoundryAgentEndpointUsed", {
            "endpoint": app_settings.azure_ai.agent_endpoint
        })
        ai_foundry_client = await ai_project_client.inference.get_azure_openai_client(
            api_version=app_settings.azure_openai.preview_api_version,
        )
        return ai_foundry_client
    except Exception as e:
        logging.exception("Exception in AI Foundry initialization: %s", e)
        ai_foundry_client = None
        raise 


def init_cosmosdb_client():
    cosmos_conversation_client = None
    if app_settings.chat_history:
        try:
            cosmos_endpoint = (
                f"https://{app_settings.chat_history.account}.documents.azure.com:443/"
            )

            if not app_settings.chat_history.account_key:
                credential = DefaultAzureCredential()
            else:
                credential = app_settings.chat_history.account_key

            cosmos_conversation_client = CosmosConversationClient(
                cosmosdb_endpoint=cosmos_endpoint,
                credential=credential,
                database_name=app_settings.chat_history.database,
                container_name=app_settings.chat_history.conversations_container,
                enable_message_feedback=app_settings.chat_history.enable_feedback,
            )
        except Exception as e:
            logging.exception("Exception in CosmosDB initialization", e)
            span = trace.get_current_span()
            if span is not None:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
            cosmos_conversation_client = None
            raise e
    else:
        logging.debug("CosmosDB not configured")

    return cosmos_conversation_client


# Conversion of citation markers
def convert_citation_markers(text, doc_mapping):
    def replace_marker(match):
        key = match.group(1)
        if key not in doc_mapping:
            doc_mapping[key] = f"[{len(doc_mapping) + 1}]"
        return doc_mapping[key]

    return re.sub(r'【(\d+:\d+)†source】', replace_marker, text)


# Extract citations from run steps
async def extract_citations_from_run_steps(project_client, thread_id, run_id, answer, streamed_titles=None):
    streamed_titles = streamed_titles or set()

    async for run_step in project_client.agents.run_steps.list(thread_id=thread_id, run_id=run_id):
        if isinstance(run_step.step_details, RunStepToolCallDetails):
            for tool_call in run_step.step_details.tool_calls:
                if "azure_ai_search" in tool_call:
                    output_data = tool_call["azure_ai_search"].get("output")
                    if output_data:
                        tool_output = ast.literal_eval(output_data) if isinstance(output_data, str) else output_data
                        metadata = tool_output.get("metadata", {})
                        urls = metadata.get("get_urls", [])
                        titles = metadata.get("titles", [])

                        for i, title in enumerate(titles):
                            url = urls[i] if i < len(urls) else ""

                            if not streamed_titles or title in streamed_titles:
                                existing = next((c for c in answer["citations"] if c["title"] == title), None)
                                if existing:
                                    existing["url"] = url
                                else:
                                    answer["citations"].append({"title": title, "url": url})


async def send_chat_request(request_body, request_headers) -> AsyncGenerator[Dict[str, Any], None]:
    filtered_messages = []
    messages = request_body.get("messages", [])
    for message in messages:
        if message.get("role") != "tool":
            filtered_messages.append(message)
    track_event_if_configured("MessagesFiltered", {
        "original_count": len(messages),
        "filtered_count": len(filtered_messages)
    })
    request_body["messages"] = filtered_messages
    try:
        # Use AI Foundry SDK for response
        track_event_if_configured("Foundry_sdk_for_response", {"status": "success"})
        answer: Dict[str, Any] = {"answer": "", "citations": []}
        run_id = None
        streamed_titles = set()
        doc_mapping = {}
        # Browse
        if request_body["chat_type"] == "browse":
            try:
                browse_agent_data = await BrowseAgentFactory.get_browse_agent(system_instruction=app_settings.azure_openai.system_message)
                browse_project_client = browse_agent_data["client"]
                browse_agent = browse_agent_data["agent"]

                thread = await browse_project_client.agents.threads.create()

                for msg in request_body["messages"]:
                    if not msg or "role" not in msg or "content" not in msg:
                        continue
                    if msg["role"] != "tool":
                        await browse_project_client.agents.messages.create(
                            thread_id=thread.id,
                            role=msg["role"],
                            content=msg["content"],
                        )

                if app_settings.azure_openai.stream:
                    async with await browse_project_client.agents.runs.stream(
                        thread_id=thread.id,
                        agent_id=browse_agent.id,
                        tool_choice={"type": "azure_ai_search"}
                    ) as stream:
                        async for event_type, event_data, _ in stream:
                            if isinstance(event_data, ThreadRun):
                                run_id = event_data.id  # Save for post-processing

                            elif isinstance(event_data, MessageDeltaChunk):
                                if event_data.delta.content and isinstance(event_data.delta.content[0], MessageDeltaTextContent):
                                    delta_text = event_data.delta.content[0].text

                                    if delta_text and delta_text.value:
                                        answer["answer"] += delta_text.value
                                        yield {
                                            "answer": convert_citation_markers(delta_text.value, doc_mapping),
                                            "citations": json.dumps(answer["citations"])
                                        }

                                    if delta_text and delta_text.annotations:
                                        for annotation in delta_text.annotations:
                                            if isinstance(annotation, MessageDeltaTextUrlCitationAnnotation):
                                                citation = annotation.url_citation
                                                if citation.url not in [c["url"] for c in answer["citations"]]:
                                                    answer["citations"].append({
                                                        "title": citation.title,
                                                        "url": citation.url
                                                    })
                                                    streamed_titles.add(citation.title)  # Track titles seen in streaming

                    print(f"Streaming completed for thread: {thread.id}", flush=True)

                    # Post-processing citations from run_steps
                    if run_id:
                        await extract_citations_from_run_steps(browse_project_client, thread.id, run_id, answer, streamed_titles)

                    yield {
                        # "answer": answer["answer"],
                        "citations": json.dumps(answer["citations"])
                    }

                else:
                    run = await browse_project_client.agents.runs.create_and_process(
                        thread_id=thread.id,
                        agent_id=browse_agent.id,
                        tool_choice={"type": "azure_ai_search"}
                    )
                    if run.status == "failed":
                        raise Exception(f"Run failed: {run.error_message}")
                    else:
                        await extract_citations_from_run_steps(browse_project_client, thread.id, run.id, answer, streamed_titles)
                        messages = browse_project_client.agents.messages.list(thread_id=thread.id)
                        async for msg in messages:
                            if msg.role == MessageRole.AGENT and msg.text_messages:
                                answer["answer"] = msg.text_messages[-1].text.value
                                answer["answer"] = convert_citation_markers(answer["answer"], doc_mapping)
                                break
                    yield {
                        "answer": answer["answer"],
                        "citations": json.dumps(answer["citations"])
                    }
            finally:
                if thread:
                    print(f"Deleting browse thread: {thread.id}", flush=True)
                    await browse_project_client.agents.threads.delete(thread_id=thread.id)

        # Generate Template
        else:
            try:
                template_agent_data = await TemplateAgentFactory.get_template_agent(system_instruction=app_settings.azure_openai.template_system_message)
                template_project_client = template_agent_data["client"]
                template_agent = template_agent_data["agent"]

                thread = await template_project_client.agents.threads.create()

                for msg in request_body["messages"]:
                    if not msg or "role" not in msg or "content" not in msg:
                        continue
                    if msg["role"] != "tool":
                        await template_project_client.agents.messages.create(
                            thread_id=thread.id,
                            role=msg["role"],
                            content=msg["content"],
                        )

                run = await template_project_client.agents.runs.create_and_process(
                    thread_id=thread.id,
                    agent_id=template_agent.id,
                    tool_choice={"type": "azure_ai_search"}
                )
                if run.status == "failed":
                    raise Exception(f"Run failed: {run.error_message}")
                else:
                    await extract_citations_from_run_steps(template_project_client, thread.id, run.id, answer)
                    messages = template_project_client.agents.messages.list(thread_id=thread.id)
                    async for msg in messages:
                        if msg.role == MessageRole.AGENT and msg.text_messages:
                            answer["answer"] = msg.text_messages[-1].text.value
                            answer["answer"] = convert_citation_markers(answer["answer"], doc_mapping)
                            break
                yield {
                    "answer": answer["answer"],
                    "citations": json.dumps(answer["citations"])
                }
            finally:
                # Clean up the thread after processing
                if thread:
                    print(f"Deleting template thread: {thread.id}", flush=True)
                    await template_project_client.agents.threads.delete(thread_id=thread.id)

    except Exception as e:
        logging.exception("Exception in send_chat_request")
        print(f"Exception in send_chat_request: {e}", flush=True)
        span = trace.get_current_span()
        if span is not None:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
        raise e


async def complete_chat_request(request_body, request_headers):
    # response, apim_request_id = await send_chat_request(request_body, request_headers)
    response = None
    history_metadata = request_body.get("history_metadata", {})

    async for chunk in send_chat_request(request_body, request_headers):
        response = chunk  # Only the last chunk matters for non-streaming

    return format_non_streaming_response(response, history_metadata)


async def stream_chat_request(request_body, request_headers):
    track_event_if_configured("StreamChatRequestStart", {
        "has_history_metadata": "history_metadata" in request_body
    })
    # response, apim_request_id = await send_chat_request(request_body, request_headers)
    history_metadata = request_body.get("history_metadata", {})

    async def generate():
        async for chunk in send_chat_request(request_body, request_headers):
            yield format_stream_response(chunk, history_metadata)

    return generate()


async def conversation_internal(request_body, request_headers):
    try:
        chat_type = (
            ChatType.BROWSE
            if not (
                request_body["chat_type"] and request_body["chat_type"] == "template"
            )
            else ChatType.TEMPLATE
        )
        track_event_if_configured("ConversationRequestReceived", {
            "chat_type": str(chat_type),
            "streaming_enabled": app_settings.azure_openai.stream
        })
        if app_settings.azure_openai.stream and chat_type == ChatType.BROWSE:
            result = await stream_chat_request(request_body, request_headers)
            response = await make_response(format_as_ndjson(result))
            response.timeout = None
            response.mimetype = "application/json-lines"
            track_event_if_configured("ConversationStreamResponsePrepared", {
                "response": response
            })
            return response
        else:
            result = await complete_chat_request(request_body, request_headers)
            track_event_if_configured("ConversationCompleteResponsePrepared", {
                "result": json.dumps(result)
            })
            return jsonify(result)

    except Exception as ex:
        logging.exception(ex)
        span = trace.get_current_span()
        if span is not None:
            span.record_exception(ex)
            span.set_status(Status(StatusCode.ERROR, str(ex)))

        if hasattr(ex, "status_code"):
            return jsonify({"error": str(ex)}), ex.status_code
        else:
            return jsonify({"error": str(ex)}), 500


@bp.route("/conversation", methods=["POST"])
async def conversation():
    if not request.is_json:
        track_event_if_configured("InvalidRequestFormat", {
            "status_code": 415,
            "detail": "Request must be JSON"
        })
        return jsonify({"error": "request must be json"}), 415
    request_json = await request.get_json()

    return await conversation_internal(request_json, request.headers)


@bp.route("/frontend_settings", methods=["GET"])
def get_frontend_settings():
    try:
        return jsonify(frontend_settings), 200
    except Exception as e:
        logging.exception("Exception in /frontend_settings")
        span = trace.get_current_span()
        if span is not None:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
        return jsonify({"error": str(e)}), 500


# Conversation History API #
@bp.route("/history/generate", methods=["POST"])
async def add_conversation():
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    if not user_id:
        track_event_if_configured("UserIdNotFound", {"status_code": 400, "detail": "no user"})

    # check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        # make sure cosmos is configured
        cosmos_conversation_client = init_cosmosdb_client()
        if not cosmos_conversation_client:
            track_event_if_configured("CosmosNotConfigured", {"error": "CosmosDB is not configured"})
            raise Exception("CosmosDB is not configured or not working")

        # check for the conversation_id, if the conversation is not set, we will create a new one
        history_metadata = {}
        if not conversation_id:
            title = await generate_title(request_json["messages"])
            conversation_dict = await cosmos_conversation_client.create_conversation(
                user_id=user_id, title=title
            )
            conversation_id = conversation_dict["id"]
            history_metadata["title"] = title
            history_metadata["date"] = conversation_dict["createdAt"]

        # Format the incoming message object in the "chat/completions" messages format
        # then write it to the conversation history in cosmos
        messages = request_json["messages"]
        if len(messages) > 0 and messages[-1]["role"] == "user":
            createdMessageValue = await cosmos_conversation_client.create_message(
                uuid=str(uuid.uuid4()),
                conversation_id=conversation_id,
                user_id=user_id,
                input_message=messages[-1],
            )

            track_event_if_configured("MessageCreated", {
                "conversation_id": conversation_id,
                "message_id": json.dumps(messages[-1]),
                "user_id": user_id
            })
            if createdMessageValue == "Conversation not found":
                track_event_if_configured("ConversationNotFound", {"conversation_id": conversation_id})
                raise Exception(
                    "Conversation not found for the given conversation ID: "
                    + conversation_id
                    + "."
                )
        else:
            track_event_if_configured("NoUserMessage", {"status_code": 400, "detail": "No user message found"})
            raise Exception("No user message found")

        await cosmos_conversation_client.cosmosdb_client.close()

        # Submit request to Chat Completions for response
        request_body = await request.get_json()
        history_metadata["conversation_id"] = conversation_id
        request_body["history_metadata"] = history_metadata
        track_event_if_configured("ConversationHistoryGenerated", {"conversation_id": conversation_id})
        return await conversation_internal(request_body, request.headers)

    except Exception as e:
        logging.exception("Exception in /history/generate")
        span = trace.get_current_span()
        if span is not None:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
        return jsonify({"error": str(e)}), 500


@bp.route("/history/update", methods=["POST"])
async def update_conversation():
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    if not user_id:
        track_event_if_configured("UserIdNotFound", {"status_code": 400, "detail": "no user"})

    # check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        # make sure cosmos is configured
        cosmos_conversation_client = init_cosmosdb_client()
        if not cosmos_conversation_client:
            track_event_if_configured("CosmosNotConfigured", {"error": "CosmosDB is not configured"})
            raise Exception("CosmosDB is not configured or not working")

        # check for the conversation_id, if the conversation is not set, we will create a new one
        if not conversation_id:
            track_event_if_configured("MissingConversationId", {"error": "No conversation_id in request"})
            raise Exception("No conversation_id found")

        # Format the incoming message object in the "chat/completions" messages format
        # then write it to the conversation history in cosmos
        messages = request_json["messages"]
        if len(messages) > 0 and messages[-1]["role"] == "assistant":
            if len(messages) > 1 and messages[-2].get("role", None) == "tool":
                # write the tool message first
                await cosmos_conversation_client.create_message(
                    uuid=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    user_id=user_id,
                    input_message=messages[-2],
                )
            # write the assistant message
            await cosmos_conversation_client.create_message(
                uuid=messages[-1]["id"],
                conversation_id=conversation_id,
                user_id=user_id,
                input_message=messages[-1],
            )
        else:
            track_event_if_configured("NoAssistantMessage", {"status_code": 400, "detail": "No bot message found"})
            raise Exception("No bot messages found")

        # Submit request to Chat Completions for response
        await cosmos_conversation_client.cosmosdb_client.close()
        track_event_if_configured("ConversationHistoryUpdated", {"conversation_id": conversation_id})
        response = {"success": True}
        return jsonify(response), 200

    except Exception as e:
        logging.exception("Exception in /history/update")
        span = trace.get_current_span()
        if span is not None:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
        return jsonify({"error": str(e)}), 500


@bp.route("/history/message_feedback", methods=["POST"])
async def update_message():
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    cosmos_conversation_client = init_cosmosdb_client()

    # check request for message_id
    request_json = await request.get_json()
    message_id = request_json.get("message_id", None)
    message_feedback = request_json.get("message_feedback", None)
    try:
        if not message_id:
            logging.error("Missing message_id", extra={'request_json': request_json})
            track_event_if_configured("MissingMessageId", {"status_code": 400, "request_json": request_json})
            return jsonify({"error": "message_id is required"}), 400

        if not message_feedback:
            logging.error("Missing message_feedback", extra={'request_id': request_json})
            track_event_if_configured("MissingMessageFeedback", {"status_code": 400, "request_json": request_json})
            return jsonify({"error": "message_feedback is required"}), 400

        # update the message in cosmos
        updated_message = await cosmos_conversation_client.update_message_feedback(
            user_id, message_id, message_feedback
        )
        if updated_message:
            track_event_if_configured("MessageFeedbackUpdated", {
                "message_id": message_id,
                "message_feedback": message_feedback
            })
            logging.info("Message feedback updated", extra={'message_id': message_id})
            return (
                jsonify(
                    {
                        "message": f"Successfully updated message with feedback {message_feedback}",
                        "message_id": message_id,
                    }
                ),
                200,
            )
        else:
            logging.warning("Message not found or access denied", extra={'request_json': request_json})
            track_event_if_configured("MessageNotFoundOrAccessDenied", {
                "status_code": 404,
                "request_json": request_json
            })
            return (
                jsonify(
                    {
                        "error": f"Unable to update message {message_id}. "
                        "It either does not exist or the user does not have access to it."
                    }
                ),
                404,
            )

    except Exception as e:
        logging.exception("Exception in /history/message_feedback")
        span = trace.get_current_span()
        if span is not None:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
        return jsonify({"error": str(e)}), 500


@bp.route("/history/delete", methods=["DELETE"])
async def delete_conversation():
    # get the user id from the request headers
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        if not conversation_id:
            track_event_if_configured("MissingConversationId", {"error": "No conversation_id in request", "request_json": request_json})
            return jsonify({"error": "conversation_id is required"}), 400

        # make sure cosmos is configured
        cosmos_conversation_client = init_cosmosdb_client()
        if not cosmos_conversation_client:
            track_event_if_configured("CosmosDBNotConfigured", {
                "user_id": user_id,
                "conversation_id": conversation_id
            })
            raise Exception("CosmosDB is not configured or not working")

        # delete the conversation messages from cosmos first
        await cosmos_conversation_client.delete_messages(conversation_id, user_id)

        # Now delete the conversation
        await cosmos_conversation_client.delete_conversation(user_id, conversation_id)

        await cosmos_conversation_client.cosmosdb_client.close()

        track_event_if_configured("ConversationDeleted", {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "status": "success"
        })

        return (
            jsonify(
                {
                    "message": "Successfully deleted conversation and messages",
                    "conversation_id": conversation_id,
                }
            ),
            200,
        )
    except Exception as e:
        logging.exception("Exception in /history/delete")
        span = trace.get_current_span()
        if span is not None:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
        return jsonify({"error": str(e)}), 500


@bp.route("/history/list", methods=["GET"])
async def list_conversations():
    offset = request.args.get("offset", 0)
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # make sure cosmos is configured
    cosmos_conversation_client = init_cosmosdb_client()
    if not cosmos_conversation_client:
        track_event_if_configured("CosmosDBNotConfigured", {
            "user_id": user_id,
            "error": "CosmosDB is not configured or not working"
        })
        raise Exception("CosmosDB is not configured or not working")

    # get the conversations from cosmos
    conversations = await cosmos_conversation_client.get_conversations(user_id, offset=offset, limit=25)
    await cosmos_conversation_client.cosmosdb_client.close()
    if not isinstance(conversations, list):
        track_event_if_configured("NoConversationsFound", {
            "user_id": user_id,
            "status": "No conversations found"
        })
        return jsonify({"error": f"No conversations for {user_id} were found"}), 404

    # return the conversation ids
    track_event_if_configured("ConversationsListed", {
        "user_id": user_id,
        "conversation_count": len(conversations),
        "status": "success"
    })
    return jsonify(conversations), 200


@bp.route("/history/read", methods=["POST"])
async def get_conversation():
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    if not conversation_id:
        track_event_if_configured("MissingConversationId", {
            "user_id": user_id,
            "error": "conversation_id is required"
        })
        return jsonify({"error": "conversation_id is required"}), 400

    # make sure cosmos is configured
    cosmos_conversation_client = init_cosmosdb_client()
    if not cosmos_conversation_client:
        track_event_if_configured("CosmosDBNotConfigured", {
            "user_id": user_id,
            "error": "CosmosDB is not configured or not working"
        })
        raise Exception("CosmosDB is not configured or not working")

    # get the conversation object and the related messages from cosmos
    conversation = await cosmos_conversation_client.get_conversation(
        user_id, conversation_id
    )
    # return the conversation id and the messages in the bot frontend format
    if not conversation:
        track_event_if_configured("ConversationNotFound", {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "error": "Conversation not found or access denied"
        })
        return (
            jsonify(
                {
                    "error": (
                        f"Conversation {conversation_id} was not found. "
                        "It either does not exist or the logged in user does not have access to it."
                    )
                }
            ),
            404,
        )

    # get the messages for the conversation from cosmos
    conversation_messages = await cosmos_conversation_client.get_messages(
        user_id, conversation_id
    )

    # format the messages in the bot frontend format
    messages = [
        {
            "id": msg["id"],
            "role": msg["role"],
            "content": msg["content"],
            "createdAt": msg["createdAt"],
            "feedback": msg.get("feedback"),
        }
        for msg in conversation_messages
    ]

    track_event_if_configured("ConversationRead", {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "message_count": len(messages),
        "status": "success"
    })
    await cosmos_conversation_client.cosmosdb_client.close()
    return jsonify({"conversation_id": conversation_id, "messages": messages}), 200


@bp.route("/history/rename", methods=["POST"])
async def rename_conversation():
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    if not conversation_id:
        track_event_if_configured("MissingConversationId", {
            "user_id": user_id,
            "error": "conversation_id is required",
            "request_json": request_json
        })
        return jsonify({"error": "conversation_id is required"}), 400

    # make sure cosmos is configured
    cosmos_conversation_client = init_cosmosdb_client()
    if not cosmos_conversation_client:
        track_event_if_configured("CosmosDBNotConfigured", {
            "user_id": user_id,
            "error": "CosmosDB not configured or not working"
        })
        raise Exception("CosmosDB is not configured or not working")

    # get the conversation from cosmos
    conversation = await cosmos_conversation_client.get_conversation(
        user_id, conversation_id
    )
    if not conversation:
        track_event_if_configured("ConversationNotFoundForRename", {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "error": "Conversation not found or access denied"
        })
        return (
            jsonify(
                {
                    "error": (
                        f"Conversation {conversation_id} was not found. "
                        "It either does not exist or the logged in user does not have access to it."
                    )
                }
            ),
            404,
        )

    # update the title
    title = request_json.get("title", None)
    if not title or title.strip() == "":
        track_event_if_configured("MissingTitle", {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "error": "title is required"
        })
        return jsonify({"error": "title is required"}), 400
    conversation["title"] = title
    updated_conversation = await cosmos_conversation_client.upsert_conversation(
        conversation
    )

    await cosmos_conversation_client.cosmosdb_client.close()
    track_event_if_configured("ConversationRenamed", {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "new_title": title
    })
    return jsonify(updated_conversation), 200


@bp.route("/history/delete_all", methods=["DELETE"])
async def delete_all_conversations():
    # get the user id from the request headers
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # get conversations for user
    try:
        # make sure cosmos is configured
        cosmos_conversation_client = init_cosmosdb_client()
        if not cosmos_conversation_client:
            track_event_if_configured("CosmosDBNotConfigured", {
                "user_id": user_id,
                "error": "CosmosDB is not configured or not working"
            })
            raise Exception("CosmosDB is not configured or not working")

        conversations = await cosmos_conversation_client.get_conversations(
            user_id, offset=0, limit=None
        )
        if not conversations:
            track_event_if_configured("NoConversationsToDelete", {
                "user_id": user_id,
                "status": "No conversations found"
            })
            return jsonify({"error": f"No conversations for {user_id} were found"}), 404

        # delete each conversation
        for conversation in conversations:
            # delete the conversation messages from cosmos first
            await cosmos_conversation_client.delete_messages(
                conversation["id"], user_id
            )

            # Now delete the conversation
            await cosmos_conversation_client.delete_conversation(
                user_id, conversation["id"]
            )
        await cosmos_conversation_client.cosmosdb_client.close()
        track_event_if_configured("AllConversationsDeleted", {
            "user_id": user_id,
            "deleted_count": len(conversations)
        })
        return (
            jsonify(
                {
                    "message": f"Successfully deleted conversation and messages for user {user_id}"
                }
            ),
            200,
        )

    except Exception as e:
        logging.exception("Exception in /history/delete_all")
        span = trace.get_current_span()
        if span is not None:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
        return jsonify({"error": str(e)}), 500


@bp.route("/history/clear", methods=["POST"])
async def clear_messages():
    # get the user id from the request headers
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        if not conversation_id:
            return jsonify({"error": "conversation_id is required"}), 400

        # make sure cosmos is configured
        cosmos_conversation_client = init_cosmosdb_client()
        if not cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        # delete the conversation messages from cosmos
        await cosmos_conversation_client.delete_messages(conversation_id, user_id)

        return (
            jsonify(
                {
                    "message": "Successfully deleted messages in conversation",
                    "conversation_id": conversation_id,
                }
            ),
            200,
        )
    except Exception as e:
        logging.exception("Exception in /history/clear_messages")
        span = trace.get_current_span()
        if span is not None:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
        return jsonify({"error": str(e)}), 500


@bp.route("/history/ensure", methods=["GET"])
async def ensure_cosmos():
    if not app_settings.chat_history:
        return jsonify({"error": "CosmosDB is not configured"}), 404

    try:
        cosmos_conversation_client = init_cosmosdb_client()
        success, err = await cosmos_conversation_client.ensure()
        if not cosmos_conversation_client or not success:
            if err:
                track_event_if_configured("CosmosEnsureFailed", err)
                return jsonify({"error": err}), 422
            return jsonify({"error": "CosmosDB is not configured or not working"}), 500

        track_event_if_configured("CosmosEnsureSuccess", {"status": "working"})
        await cosmos_conversation_client.cosmosdb_client.close()
        return jsonify({"message": "CosmosDB is configured and working"}), 200
    except Exception as e:
        logging.exception("Exception in /history/ensure")
        cosmos_exception = str(e)
        if "Invalid credentials" in cosmos_exception:
            return jsonify({"error": cosmos_exception}), 401
        elif "Invalid CosmosDB database name" in cosmos_exception:
            return (
                jsonify(
                    {
                        "error": f"{cosmos_exception} {app_settings.chat_history.database} for account {app_settings.chat_history.account}"
                    }
                ),
                422,
            )
        elif "Invalid CosmosDB container name" in cosmos_exception:
            return (
                jsonify(
                    {
                        "error": f"{cosmos_exception}: {app_settings.chat_history.conversations_container}"
                    }
                ),
                422,
            )
        else:
            return jsonify({"error": "CosmosDB is not working"}), 500


@bp.route("/section/generate", methods=["POST"])
async def generate_section_content():
    request_json = await request.get_json()
    try:
        # verify that section title and section description are provided
        if "sectionTitle" not in request_json:
            track_event_if_configured("GenerateSectionFailed", {"error": "sectionTitle missing", "request_json": request_json})
            return jsonify({"error": "sectionTitle is required"}), 400

        if "sectionDescription" not in request_json:
            track_event_if_configured("GenerateSectionFailed", {"error": "sectionDescription missing", "request_json": request_json})
            return jsonify({"error": "sectionDescription is required"}), 400

        content = await get_section_content(request_json, request.headers)
        track_event_if_configured("GenerateSectionSuccess", {
            "sectionTitle": request_json["sectionTitle"]
        })
        return jsonify({"section_content": content}), 200
    except Exception as e:
        logging.exception("Exception in /section/generate")
        span = trace.get_current_span()
        if span is not None:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
        return jsonify({"error": str(e)}), 500


# Fetch content from Azure Search API
@bp.route("/fetch-azure-search-content", methods=["POST"])
async def fetch_azure_search_content():
    try:
        request_json = await request.get_json()
        url = request_json.get("url")
        title = request_json.get("title")

        if not url or not title:
            return jsonify({"error": "URL and title are required"}), 400

        # Get Azure AD token
        credential = DefaultAzureCredentialSync()
        token = credential.get_token("https://search.azure.com/.default")
        access_token = token.token

        def fetch_content(fetch_url):
            try:
                response = requests.get(
                    fetch_url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json"
                    },
                    timeout=10
                )
                if response.status_code == 200:
                    data = response.json()
                    content = data.get("content", "")
                    return {"success": True, "content": content}
                else:
                    error_msg = f"Request failed with status code {response.status_code} {response.text}"
                    return {"success": False, "error": error_msg}
            except Exception as e:
                logging.exception("Error fetching content from Azure Search")
                return {"success": False, "error": f"Exception: {str(e)}"}

        result = await asyncio.to_thread(fetch_content, url)

        if result["success"]:
            return jsonify({
                "content": result["content"],
                "title": title
            }), 200
        else:
            return jsonify({"error": result["error"]}), 500

    except Exception as e:
        logging.exception("Exception in /fetch-azure-search-content")
        return jsonify({"error": str(e)}), 500


async def generate_title(conversation_messages):
    # make sure the messages are sorted by _ts descending
    title_prompt = app_settings.azure_openai.title_prompt

    messages = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in conversation_messages
    ]
    messages.append({"role": "user", "content": title_prompt})

    try:
        response = None
        # Use Foundry SDK for title generation
        track_event_if_configured("Foundry_sdk_for_title", {"status": "success"})
        ai_foundry_client = await init_ai_foundry_client()
        response = await ai_foundry_client.chat.completions.create(
            model=app_settings.azure_openai.model,
            messages=messages,
            temperature=app_settings.azure_openai.temperature,
            max_tokens=app_settings.azure_openai.max_tokens,
        )
        raw_content = response.choices[0].message.content
        raw_content = raw_content.strip()
        if raw_content.startswith("{{") and raw_content.endswith("}}"):
            raw_content = raw_content[1:-1]  # Remove one set of braces

        # Extract JSON object
        json_match = re.search(r"\{.*?\}", raw_content, re.DOTALL)
        if not json_match:
            raise ValueError("No JSON object found in response")

        json_str = json_match.group()
        title = json.loads(json_str)["title"]
        track_event_if_configured("TitleGenerated", {"title": title})
        return title
    except Exception as e:
        logging.exception("Exception in generate_title" + str(e))
        return messages[-2]["content"]


async def get_section_content(request_body, request_headers):
    user_prompt = f"""sectionTitle: {request_body['sectionTitle']}
    sectionDescription: {request_body['sectionDescription']}
    """
    messages = []
    messages.append({"role": "user", "content": user_prompt})

    request_body["messages"] = messages

    try:
        # Use Foundry SDK for section content generation
        track_event_if_configured("Foundry_sdk_for_section", {"status": "success"})
        section_agent_data = await SectionAgentFactory.get_sections_agent(system_instruction=app_settings.azure_openai.generate_section_content_prompt)
        section_project_client = section_agent_data["client"]
        section_agent = section_agent_data["agent"]

        thread = await section_project_client.agents.threads.create()

        for msg in request_body["messages"]:
            if not msg or "role" not in msg or "content" not in msg:
                continue  # skip malformed messages

            if msg["role"] != "tool":
                await section_project_client.agents.messages.create(
                    thread_id=thread.id,
                    role=msg["role"],
                    content=msg["content"],
                )

        run = await section_project_client.agents.runs.create_and_process(
            thread_id=thread.id,
            agent_id=section_agent.id,
            tool_choice={"type": "azure_ai_search"}
        )
        if run.status == "failed":
            print(f"Run failed: {run.last_error}", flush=True)
            raise Exception
        else:
            message = (
                await section_project_client.agents.messages.get_last_message_text_by_role(
                    thread_id=thread.id, role=MessageRole.AGENT
                )
            )
            if message:
                response_text = message.text.value
                # Remove markers from section content
                response_text = re.sub(r'【(\d+:\d+)†source】', '', response_text)
        track_event_if_configured("SectionContentGenerated", {
            "sectionTitle": request_body["sectionTitle"]
        })

    except Exception as e:
        logging.exception("Exception in get_section_content")
        print(f"Exception in get_section_content: {e}", flush=True)
        span = trace.get_current_span()
        if span is not None:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
        raise e

    finally:
        if thread:
            print("Deleting section thread", flush=True)
            await section_project_client.agents.threads.delete(thread_id=thread.id)

    return response_text


app = create_app()
