"""Pydantic models corresponding to pf-client TypeScript types."""

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, RootModel


class PlanStatus(str, Enum):
    """Status of a plan in its lifecycle."""

    DRAFT = "draft"
    NEEDS_CONFIRMATION = "needs_confirmation"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    COMPLETED = "completed"


def to_camel(string: str) -> str:
    """Convert snake_case to camelCase."""
    # pf:ensures:to_camel.first_component_lowercase first component is lowercase
    # pf:ensures:to_camel.subsequent_titlecase subsequent components are title-cased
    components = string.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


def to_camel_dict(obj: dict | list | object) -> dict | list | object:
    """Recursively convert dict keys from snake_case to camelCase."""
    # pf:ensures:to_camel_dict.preserves_structure recursive structure is preserved (dict->dict, list->list)
    # pf:ensures:to_camel_dict.leaves_non_dict non-dict/list values are returned unchanged
    if isinstance(obj, dict):
        return {to_camel(k): to_camel_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_camel_dict(item) for item in obj]
    return obj


class CamelModel(BaseModel):
    """Base model with camelCase JSON support."""

    # pf:invariant:CamelModel.bidirectional accepts both snake_case and camelCase input
    # pf:invariant:CamelModel.output_camel output serialization uses camelCase
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,  # Accept both snake_case and camelCase
    )


# --- sync.ts types ---


class FileChangeType(str, Enum):
    """Type of file change."""

    # pf:invariant:FileChangeType.exhaustive only ADD, MODIFY, DELETE are valid types
    ADD = "add"
    MODIFY = "modify"
    DELETE = "delete"


class FileChange(CamelModel):
    """A single file change in a sync payload."""

    path: str  # workspace-relative
    type: FileChangeType
    hash: str | None = None  # sha-256 hash of new content
    is_binary: bool | None = None
    size_bytes: int | None = None
    content_base64: str | None = None  # raw content, base64-encoded
    encoding: Literal["utf8", "base64"] | None = None
    timestamp: str  # ISO


class SyncPayloadMetadata(CamelModel):
    """Metadata about the client sending the sync."""

    client_version: str | None = None
    node_version: str | None = None
    os: str | None = None


class SyncPayload(CamelModel):
    """Payload sent by client to sync file changes."""

    request_id: str
    session_id: str
    generation: int
    changes: list[FileChange]
    metadata: SyncPayloadMetadata | None = None


class AnalysisPayload(CamelModel):
    """Payload for explicit analysis request from the client."""

    request_id: str  # Mandatory, generated via randomBytes(8).toString('hex')
    file_name: (
        str  # Workspace-relative path (e.g., "src/module.py" or "src/module.py:10-50")
    )


class TarSyncPayload(CamelModel):
    """Tar-based sync payload for efficient full sync."""

    request_id: str
    session_id: str
    generation: int
    project_name: str
    archive_base64: str  # Base64-encoded tar.gz archive
    file_count: int
    original_bytes: int
    compressed_bytes: int
    compression_ratio: float
    metadata: SyncPayloadMetadata | None = None


# --- Server response types ---

ServerStatus = Literal["success", "error", "partial"]


class SyncErrorDetail(CamelModel):
    """Error detail for a specific file."""

    path: str
    error: str


class SyncResponseData(CamelModel):
    """Response data for incremental sync operations."""

    synced_files: list[str]  # Workspace-relative paths successfully synced
    file_count: int  # Total files in the sync operation
    errors: list[SyncErrorDetail]  # Files that failed to sync


class TarSyncResponseData(CamelModel):
    """Response data for tar-based full sync operations.

    Extends SyncResponseData with project name information that's only
    relevant during initial repository setup.
    """

    synced_files: list[str]  # Workspace-relative paths successfully synced
    file_count: int  # Total files in the sync operation
    errors: list[SyncErrorDetail]  # Files that failed to sync
    actual_project_name: str | None = (
        None  # Actual project name used (if different from requested)
    )


class SpecPatch(CamelModel):
    id: str
    patch: str


class AnalysisResponseData(CamelModel):
    """Analysis response data structure."""

    type: Literal["analysis_lite", "analysis_heavy", "analysis_dummy"]
    output: list[dict] | str  # AnnotationResult[] on success, error string on failure
    generation: int
    patches: list[SpecPatch] = []  # Included patches, if any


# --- Question/Answer types ---


class QuestionContext(CamelModel):
    """Context for a question, typically a code selection."""

    file: str
    selection: str | None = None
    start_line: int | None = None
    end_line: int | None = None


class QuestionPayload(CamelModel):
    """Payload for a question request from the client."""

    request_id: str
    question_id: str
    question: str
    context: QuestionContext | None = None

    def format_context(self) -> str | None:
        """Format the context into a string for the prompt."""
        if self.context is None:
            return None

        ctx = self.context
        parts = [f"File: {ctx.file}"]

        if ctx.start_line is not None and ctx.end_line is not None:
            parts.append(f"Lines: {ctx.start_line}-{ctx.end_line}")
        elif ctx.start_line is not None:
            parts.append(f"Line: {ctx.start_line}")

        if ctx.selection:
            parts.append(f"Selected code:\n{ctx.selection}")

        return "\n".join(parts)


class AnswerStep(CamelModel):
    """A single step in an answer, referencing an annotation."""

    annotation_ref: str  # Annotation ID or new annotation identifier
    is_new: bool
    reasoning: str  # Markdown


class QuestionAnswer(CamelModel):
    """Server response containing the answer to a question."""

    question_id: str
    question: str
    steps: list[AnswerStep]
    synthesis: str
    new_annotation_patches: list[SpecPatch] | None = None


class PatchReviewDecision(CamelModel):
    file_path: str
    accepted_lines: list[str]
    rejected_lines: list[str]


PatchReviewDecisions = list[PatchReviewDecision]


class AnnotationDeletionFeedback(CamelModel):
    file_path: str
    annotation_type: str
    annotation_name: str
    line: int
    description: str | None = None


AnnotationDeletionFeedbacks = list[AnnotationDeletionFeedback]


class ClientFeedback(CamelModel):
    """Client feedback payload types."""

    type: Literal["patch_review", "annotations_deleted"]
    payload: PatchReviewDecisions | AnnotationDeletionFeedbacks


class ClientFeedbackPayload(CamelModel):
    request_id: str
    session_id: str
    generation: int
    origin: Literal["cli", "vscode"] | None = None
    feedback: ClientFeedback
    timestamp: str


class SyncClientRequest(CamelModel):
    type: Literal["sync"]
    payload: SyncPayload


class FeedbackClientRequest(CamelModel):
    type: Literal["feedback"]
    payload: ClientFeedbackPayload


class QuestionClientRequest(CamelModel):
    type: Literal["question"]
    payload: QuestionPayload


class AnalysisClientRequest(CamelModel):
    type: Literal["analysis"]
    payload: AnalysisPayload


# --- Plan types ---


class QuestionOption(CamelModel):
    """An option for an agent question."""

    label: str
    description: str | None = None


class QuestionInfo(CamelModel):
    """A single question from the agent (matches OpenCode format)."""

    question: str  # Complete question text
    header: str  # Short label (max 30 chars)
    options: list[QuestionOption]  # Available choices
    multiple: bool = False  # Allow selecting multiple choices
    custom: bool = True  # Allow typing custom answer


class AgentQuestion(CamelModel):
    """A question from the agent requiring user input."""

    question_id: str
    question: str
    options: list[QuestionOption]


class PlanContext(CamelModel):
    """Context for a plan request."""

    files: list[str] | None = None
    annotations: list[str] | None = None


class PlanRequestPayload(CamelModel):
    """Payload for a plan request from the client."""

    request_id: str  # For response correlation
    plan_id: str  # Unique ID for this planning session
    description: str
    context: PlanContext | None = None


class PlanConfirmationPayload(CamelModel):
    """Payload for confirming/continuing a plan."""

    request_id: str
    plan_id: str
    revision: int  # Must match current plan revision
    confirmed: bool  # True = proceed, False = reject

    # Multi-question support
    answers: list[list[str]] | None = None  # Answers array matching questions array
    feedback: str | None = None  # Optional additional feedback

    # Legacy single-question support (deprecated)
    choice: str | None = None  # Deprecated: use answers instead


class PlanResponseData(CamelModel):
    """Response data for plan operations."""

    plan_id: str
    status: PlanStatus
    content: str
    questions: list[QuestionInfo] | None = None
    revision: int


class PlanRequestClientRequest(CamelModel):
    """Client request to create a new plan."""

    type: Literal["plan_request"]
    payload: PlanRequestPayload


class PlanConfirmationClientRequest(CamelModel):
    """Client request to confirm or continue a plan."""

    type: Literal["plan_confirmation"]
    payload: PlanConfirmationPayload


class ClientRequest(RootModel):
    """Discriminated union of client request types."""

    root: Annotated[
        Union[
            SyncClientRequest,
            FeedbackClientRequest,
            QuestionClientRequest,
            AnalysisClientRequest,
            PlanRequestClientRequest,
            PlanConfirmationClientRequest,
        ],
        Field(discriminator="type"),
    ]


class ServerResponse(CamelModel):
    """Response from server for a sync request."""

    request_id: str | None
    status: ServerStatus
    message: str | None = None
    data: dict | None = (
        None  # SyncResponseData | TarSyncResponseData | AnalysisResponseData as dict
    )
    generation: int | None = None
    timestamp: str  # ISO
