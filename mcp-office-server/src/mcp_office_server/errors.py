"""Unified error semantics for all office document tools.

Every tool returns a structured envelope instead of raising, so the agent always
receives an actionable message rather than a transport-level failure.
"""


class ToolError(Exception):
    """Base class for all document tool failures."""

    code = "tool_error"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class SandboxViolation(ToolError):
    """Raised when a path escapes the workspace root or is otherwise unsafe."""

    code = "sandbox_violation"


class DocumentNotFound(ToolError):
    """Raised when the target file does not exist inside the workspace."""

    code = "document_not_found"


class UnsupportedDocument(ToolError):
    """Raised for files that are neither .xlsx/.xlsm nor .docx."""

    code = "unsupported_document"


class SheetNotFound(ToolError):
    code = "sheet_not_found"


class RangeError(ToolError):
    code = "invalid_range"


class ParagraphNotFound(ToolError):
    code = "paragraph_not_found"


class TableNotFound(ToolError):
    code = "table_not_found"


class CorruptDocument(ToolError):
    """Raised when the underlying library cannot open the file."""

    code = "corrupt_document"


class WriteConflict(ToolError):
    """Raised when the file changed on disk since the caller last read it."""

    code = "write_conflict"


class FileLocked(ToolError):
    """Raised when the OS refuses the write because the file is open elsewhere."""

    code = "file_locked"


class CalculationError(ToolError):
    """Raised when an expression or workbook formula cannot be evaluated."""

    code = "calculation_failed"


def error_payload(exc: ToolError) -> dict:
    """Serialize a ToolError into the standard tool response envelope."""
    return {
        "ok": False,
        "error": {
            "code": exc.code,
            "message": exc.message,
            "detail": exc.detail,
        },
    }
