"""
File processing service.
Handles background file processing with thread pool management.
"""
import atexit
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from sqlmodel import Session

from app.core.exceptions import MediaNotFoundError
from app.core.logging_config import log_info, log_warning, log_error
from app.services.media_service import MediaService

# Thread pool for background processing
_processing_executor: Optional[ThreadPoolExecutor] = None
_is_shutting_down: bool = False


def _get_processing_executor() -> ThreadPoolExecutor:
    """Get or create the processing thread pool executor."""
    global _processing_executor
    if _processing_executor is None and not _is_shutting_down:
        _processing_executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="file-processing"
        )
        log_info("File processing thread pool initialized")

    if _processing_executor is None:
        raise RuntimeError("Thread pool executor is not available (shutting down)")

    return _processing_executor


def _shutdown_executor():
    """Shutdown the thread pool executor on application exit."""
    global _processing_executor, _is_shutting_down
    if _processing_executor is not None and not _is_shutting_down:
        _is_shutting_down = True
        log_info("Shutting down file processing thread pool")
        _processing_executor.shutdown(wait=True)
        _processing_executor = None
        _is_shutting_down = False


# Register shutdown handler
atexit.register(_shutdown_executor)


class FileProcessingService:
    """Service for managing background file processing tasks."""

    def __init__(self, session: Session):
        self.session = session
        self.media_service = MediaService(session)

    def process_uploaded_file_async(self, media_id: str, file_path: str, user_id: str) -> None:
        """
        Submit file processing to background thread pool.

        Args:
            media_id: UUID of the media record
            file_path: Absolute or relative path to the uploaded file
            user_id: ID of the user who uploaded the file
        """
        try:
            # Validate inputs
            if not media_id or not file_path or not user_id:
                raise ValueError("media_id, file_path, and user_id are required")

            # Validate UUID format
            try:
                uuid.UUID(media_id)
                uuid.UUID(user_id)
            except ValueError as e:
                raise ValueError(f"Invalid UUID format: {e}")

            # Validate file path
            if not isinstance(file_path, str) or not file_path.strip():
                raise ValueError("file_path must be a non-empty string")

            # Submit to thread pool
            executor = _get_processing_executor()
            if executor is None:
                raise RuntimeError("Thread pool executor is not available (shutting down)")

            future = executor.submit(
                self._process_uploaded_file_sync,
                media_id,
                file_path,
                user_id
            )

            log_info(f"File processing task submitted for media {media_id}")

        except Exception as e:
            log_error(e)
            raise

    def _process_uploaded_file_sync(self, media_id: str, file_path: str, user_id: str) -> None:
        """
        Synchronous file processing worker.

        This method runs in a background thread and handles the actual processing.
        Each thread creates its own database session to avoid conflicts.
        """
        from app.core.database import engine

        # Create a new session for this background thread
        thread_session = Session(engine)
        try:
            log_info(f"Starting file processing for media {media_id}")

            # Create a new media service instance with thread-local session
            media_service = MediaService(thread_session)
            media_service.process_uploaded_file(media_id, file_path, user_id)

            log_info(f"File processing completed successfully for media {media_id}")

        except MediaNotFoundError as e:
            log_warning(f"Media not found during processing: {e}")
        except Exception as e:
            log_error(e)
            # The media service already handles marking as failed
        finally:
            # Always close the thread-local session
            thread_session.close()

    def get_processing_status(self) -> dict:
        """Get the status of the processing thread pool.

        Note: Uses public ThreadPoolExecutor interface where possible.
        Some attributes may not be available in future Python versions.
        """
        try:
            executor = _get_processing_executor()

            # Use public interface where possible
            # _threads and _work_queue are implementation details
            status = {
                "is_shutting_down": _is_shutting_down,
            }

            # Try to get internal details, but don't fail if unavailable
            try:
                status["max_workers"] = getattr(executor, "_max_workers", None)
                status["active_threads"] = len(getattr(executor, "_threads", []))
                work_queue = getattr(executor, "_work_queue", None)
                status["pending_tasks"] = work_queue.qsize() if work_queue and hasattr(work_queue, 'qsize') else 0
                status["shutdown"] = getattr(executor, "_shutdown", False)
            except (AttributeError, TypeError):
                # Fallback if private attributes are not available
                status["max_workers"] = "unavailable"
                status["active_threads"] = "unavailable"
                status["pending_tasks"] = "unavailable"
                status["shutdown"] = "unavailable"

            return status
        except Exception as e:
            log_warning(f"Failed to get processing status: {e}")
            return {
                "error": str(e),
                "is_shutting_down": _is_shutting_down
            }

    def shutdown_processing(self, wait: bool = True) -> None:
        """Shutdown the processing thread pool."""
        _shutdown_executor()
        if wait:
            log_info("File processing thread pool shutdown completed")
        else:
            log_info("File processing thread pool shutdown initiated")
