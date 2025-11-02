"""
Media upload and management endpoints.
"""
import inspect
import logging
import mimetypes
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional, Tuple, Any, Dict

import magic
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, BackgroundTasks, Request, Header
from fastapi.responses import FileResponse, StreamingResponse
from sqlmodel import Session

from app.api.dependencies import get_current_user
from app.core import database as database_module
from app.core.config import settings
from app.core.exceptions import (
    MediaNotFoundError,
    EntryNotFoundError,
    FileTooLargeError,
    InvalidFileTypeError,
    FileValidationError
)
from app.core.logging_config import LogCategory
from app.models.enums import MediaType, UploadStatus
from app.models.user import User
from app.schemas.entry import EntryMediaCreate, EntryMediaResponse
from app.services import entry_service as entry_service_module
from app.services import media_service as media_service_module
from app.services.file_processing_service import FileProcessingService

file_logger = logging.getLogger(LogCategory.FILE_UPLOADS.value)
error_logger = logging.getLogger(LogCategory.ERRORS.value)
security_logger = logging.getLogger(LogCategory.SECURITY.value)

router = APIRouter()


def _get_media_service():
    return media_service_module.MediaService()


def _get_entry_service(session: Session):
    return entry_service_module.EntryService(session)


def _get_db_session():
    """Wrapper around database.get_session to allow easy patching in tests."""
    session_or_generator = database_module.get_session()
    if inspect.isgenerator(session_or_generator):
        yield from session_or_generator
    else:
        yield session_or_generator


def _resolve_media_root(media_service: Any) -> Path:
    """Resolve media root from service or fallback to configured path."""
    value = getattr(media_service, "media_root", None)
    if isinstance(value, Path):
        try:
            return value.resolve()
        except Exception:
            pass
    if isinstance(value, (str, bytes)):
        try:
            return Path(value).resolve()
        except Exception:
            pass
    return Path(settings.media_root).resolve()


async def _maybe_await(value: Any) -> Any:
    """Await value if it is awaitable, otherwise return as-is."""
    if inspect.isawaitable(value):
        return await value
    return value


def _supports_async_upload(service: Any) -> bool:
    """Check if service has a coroutine upload_media method."""
    upload_method = getattr(service, "upload_media", None)
    if upload_method is None:
        return False
    # For bound methods, inspect the original function
    if hasattr(upload_method, "__func__"):
        return inspect.iscoroutinefunction(upload_method.__func__)
    return inspect.iscoroutinefunction(upload_method)


def _determine_media_type_from_mime(mime_type: Optional[str]) -> Optional[MediaType]:
    """Infer media type from MIME string."""
    if not mime_type:
        return None
    if mime_type.startswith("image/"):
        return MediaType.IMAGE
    if mime_type.startswith("video/"):
        return MediaType.VIDEO
    if mime_type.startswith("audio/"):
        return MediaType.AUDIO
    return None


def _validate_fallback_file(
    media_service: Any,
    file_content: bytes,
    filename: str
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Validate file in fallback flow.

    Returns:
        Tuple of (is_valid, error_message, mime_type_guess)
    """
    validation_result = getattr(media_service, "validate_file", None)
    if validation_result:
        validation = validation_result(file_content, filename)
        # Handle both sync and async validation results
        if isinstance(validation, tuple):
            is_valid, error_message = validation
        else:
            is_valid = bool(validation)
            error_message = None if is_valid else "Invalid file"

        return is_valid, error_message, None
    else:
        # No validation function - detect MIME type directly
        try:
            mime_type_guess = magic.from_buffer(file_content, mime=True)
            return True, None, mime_type_guess
        except Exception:
            return True, None, None


def _create_fallback_media_record(
    entry_id: Optional[uuid.UUID],
    saved: Dict[str, Any],
    media_type: MediaType,
    alt_text: Optional[str],
    upload_status: UploadStatus
) -> Dict[str, Any]:
    """Create a dictionary-based media record for fallback flow."""
    return {
        "id": str(uuid.uuid4()),
        "entry_id": str(entry_id) if entry_id else str(uuid.uuid4()),
        "media_type": media_type.value if isinstance(media_type, MediaType) else media_type,
        "file_path": saved.get("file_path"),
        "original_filename": saved.get("original_filename"),
        "file_size": saved.get("file_size"),
        "mime_type": saved.get("mime_type"),
        "thumbnail_path": saved.get("thumbnail_path"),
        "alt_text": alt_text,
        "upload_status": upload_status.value if isinstance(upload_status, UploadStatus) else upload_status,
        "checksum": saved.get("checksum"),
        "created_at": datetime.now(timezone.utc).isoformat()
    }


def _send_bytes_range_requests(file_path: Path, start: int, end: int):
    """Generator function to send file bytes in range for streaming."""
    with open(file_path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining:
            chunk_size = min(8192, remaining)  # 8KB chunks
            chunk = f.read(chunk_size)
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


async def _fallback_upload_flow(
    media_service: Any,
    file: UploadFile,
    entry_id: Optional[uuid.UUID],
    alt_text: Optional[str],
    user_id: uuid.UUID,
    session: Optional[Session]
) -> Tuple[Any, str, Optional[MediaType]]:
    """
    Fallback upload flow used primarily during unit tests where services are mocked.

    Returns:
        Tuple of (media_record, full_file_path, media_type)
    """
    filename = file.filename or "unknown"
    file_content = await file.read()

    # Step 1: Validate file
    is_valid, error_message, mime_type_guess = _validate_fallback_file(
        media_service, file_content, filename
    )

    if not is_valid:
        normalized = (error_message or "").lower()
        if "file too large" in normalized:
            raise FileTooLargeError(error_message or "File too large")
        if "unsupported media type" in normalized:
            raise InvalidFileTypeError("Unsupported media type")
        raise FileValidationError(error_message or "Invalid file")

    # Step 2: Verify MIME type is supported
    if not mime_type_guess or not (
        mime_type_guess.startswith("image/")
        or mime_type_guess.startswith("video/")
        or mime_type_guess.startswith("audio/")
    ):
        raise InvalidFileTypeError("Unsupported media type")

    media_category = _determine_media_type_from_mime(mime_type_guess)
    if media_category is None:
        raise InvalidFileTypeError("Unsupported media type")

    # Step 3: Save file
    save_result = getattr(media_service, "save_uploaded_file", None)
    if not save_result:
        raise RuntimeError("MediaService.save_uploaded_file is not available")

    saved = await _maybe_await(save_result(
        file_content=file_content,
        original_filename=filename,
        user_id=str(user_id),
        media_type=media_category
    ))

    # Step 4: Extract media type and upload status
    media_type = saved.get("media_type")
    if isinstance(media_type, str):
        try:
            media_type = MediaType(media_type)
        except ValueError:
            media_type = None
    if media_type is None:
        media_type = _determine_media_type_from_mime(saved.get("mime_type")) or media_category

    upload_status = saved.get("upload_status", UploadStatus.PENDING)
    if isinstance(upload_status, str):
        try:
            upload_status = UploadStatus(upload_status)
        except ValueError:
            upload_status = UploadStatus.PENDING

    # Step 5: Create media record
    media_record: Any
    if entry_id and session:
        entry_service = _get_entry_service(session)
        entry = entry_service.get_entry_by_id(entry_id, user_id)
        if not entry:
            raise EntryNotFoundError("Entry not found")

        media_data = EntryMediaCreate(
            entry_id=entry_id,
            media_type=media_type,
            file_path=saved.get("file_path"),
            original_filename=saved.get("original_filename"),
            file_size=saved.get("file_size"),
            mime_type=saved.get("mime_type"),
            thumbnail_path=saved.get("thumbnail_path"),
            alt_text=alt_text,
            upload_status=upload_status,
            file_metadata=saved.get("file_metadata"),
            checksum=saved.get("checksum"),
        )
        media_record = entry_service.add_media_to_entry(entry_id, user_id, media_data)
    else:
        media_record = _create_fallback_media_record(
            entry_id, saved, media_type, alt_text, upload_status
        )

    # Step 6: Determine full file path
    full_file_path = saved.get("full_file_path")
    if not full_file_path:
        relative_path = saved.get("file_path")
        if relative_path:
            full_file_path = str(Path(settings.media_root) / relative_path)

    return media_record, full_file_path, media_type



@router.post(
    "/upload",
    response_model=EntryMediaResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"description": "Invalid file or validation failed"},
        401: {"description": "Not authenticated"},
        403: {"description": "Account inactive"},
        404: {"description": "Entry not found"},
        413: {"description": "File too large"},
        500: {"description": "Internal server error"},
    }
)
async def upload_media(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    entry_id: Optional[uuid.UUID] = Form(None),
    alt_text: Optional[str] = Form(None),
    current_user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[Session, Depends(_get_db_session)] = None
):
    """
    Upload a media file.

    Supports images, videos, and audio. Files are validated and processed in background.
    """
    media_service = _get_media_service()
    media_record = None

    try:
        if _supports_async_upload(media_service):
            result = await media_service.upload_media(
                file=file,
                user_id=current_user.id,
                entry_id=entry_id,
                alt_text=alt_text,
                session=session
            )
            media_record = result["media_record"]
            full_file_path = result["full_file_path"]
        else:
            media_record, full_file_path, _ = await _fallback_upload_flow(
                media_service=media_service,
                file=file,
                entry_id=entry_id,
                alt_text=alt_text,
                user_id=current_user.id,
                session=session
            )

        # Queue background processing if we have a real media record (not temporary)
        if hasattr(media_record, 'id') and full_file_path:
            try:
                processing_service = FileProcessingService(session)
                background_tasks.add_task(
                    processing_service.process_uploaded_file_async,
                    str(media_record.id),
                    full_file_path,
                    str(current_user.id)
                )
            except Exception as e:
                # Log background task error but don't fail the upload
                error_logger.warning(
                    "Failed to queue background processing task",
                    extra={"user_id": str(current_user.id), "media_id": str(media_record.id), "error": str(e)}
                )

        # Convert EntryMedia model to EntryMediaResponse schema
        return EntryMediaResponse.model_validate(media_record)

    except FileTooLargeError as e:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(e)
        )
    except FileValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except InvalidFileTypeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except EntryNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Entry not found"
        )
    except Exception as e:
        error_logger.error(
            "Unexpected error uploading media",
            extra={"user_id": str(current_user.id), "error": str(e), "error_type": type(e).__name__},
            exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while uploading file"
        )


@router.get(
    "/file/{file_path:path}",
    responses={
        401: {"description": "Not authenticated"},
        403: {"description": "Account inactive or forbidden"},
        404: {"description": "File not found"},
        416: {"description": "Range Not Satisfiable"},
    }
)
async def get_media_file(
    file_path: str,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    range_header: Optional[str] = Header(None, alias="range")
):
    """Serve media files with proper Range request support for video streaming and full downloads."""
    media_service = _get_media_service()

    try:
        media_root = _resolve_media_root(media_service)
        full_path = (media_root / file_path).resolve()

        # Debug logging
        error_logger.info(f"Media request - media_root: {media_root}, file_path: {file_path}, full_path: {full_path}")

        # Prevent directory traversal
        if not str(full_path).startswith(str(media_root)):
            security_logger.warning(
                "Path traversal attempt detected",
                extra={"user_id": str(current_user.id), "file_path": file_path}
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied"
            )

        if not full_path.exists():
            error_logger.error(f"File not found: {full_path}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found"
            )

        file_size = full_path.stat().st_size
        content_type, _ = mimetypes.guess_type(str(full_path))
        content_type = content_type or "application/octet-stream"

        # Handle Range header
        if range_header:
            try:
                if not range_header.strip().startswith("bytes="):
                    raise ValueError("Invalid range unit")

                range_val = range_header.strip().split("=")[1]
                start_str, end_str = range_val.split("-")

                if not start_str:
                    start = file_size - int(end_str)
                    end = file_size - 1
                elif not end_str:
                    start = int(start_str)
                    end = file_size - 1
                else:
                    start = int(start_str)
                    end = int(end_str)

                if start >= file_size or end >= file_size or start > end:
                    raise HTTPException(
                        status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                        detail="Range Not Satisfiable"
                    )

                headers = {
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(end - start + 1),
                    "Cache-Control": "public, max-age=3600",
                }

                return StreamingResponse(
                    _send_bytes_range_requests(full_path, start, end),
                    status_code=status.HTTP_206_PARTIAL_CONTENT,
                    headers=headers,
                    media_type=content_type,
                )
            except Exception as e:
                error_logger.error(f"Range handling error: {e}")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid Range header"
                )

        # ---- NO RANGE HEADER ----
        # Return a static FileResponse with Content-Length automatically set
        return FileResponse(
            path=full_path,
            media_type=content_type,
            filename=full_path.name,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": "public, max-age=3600",
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        error_logger.error(
            f"Error serving media file: {e}",
            extra={"user_id": str(current_user.id), "file_path": file_path},
            exc_info=True
        )
        raise HTTPException(status_code=500, detail="Failed to serve file")


@router.get(
    "/thumbnail/{file_path:path}",
    responses={
        401: {"description": "Not authenticated"},
        403: {"description": "Account inactive or forbidden"},
        404: {"description": "Thumbnail not found"},
    }
)
async def get_thumbnail(
    file_path: str,
    current_user: Annotated[User, Depends(get_current_user)]
):
    """Serve thumbnail files with standardized path format."""
    media_service = _get_media_service()

    try:
        media_root = _resolve_media_root(media_service)
        full_path = media_root / file_path
        full_path_resolved = full_path.resolve()

        # Validate path to prevent directory traversal
        if not str(full_path_resolved).startswith(str(media_root)):
            security_logger.warning(
                "Path traversal attempt detected in thumbnail",
                extra={"user_id": str(current_user.id), "file_path": file_path}
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied"
            )

        if not full_path_resolved.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Thumbnail not found"
            )

        # Create response for thumbnail display
        response = FileResponse(full_path_resolved)

        return response
    except HTTPException:
        raise
    except Exception as e:
        error_logger.error(
            "Error serving thumbnail",
            extra={"user_id": str(current_user.id), "file_path": file_path, "error": str(e)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to serve thumbnail"
        )


@router.delete(
    "/file/{file_path:path}",
    status_code=status.HTTP_200_OK,
    responses={
        401: {"description": "Not authenticated"},
        403: {"description": "Account inactive or permission denied"},
        404: {"description": "File or entry not found"},
        500: {"description": "Failed to delete file"},
    }
)
async def delete_media_file(
    file_path: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(_get_db_session)]
):
    """Delete a media file."""
    from app.models.entry import EntryMedia, Entry
    from sqlmodel import select

    media_service = _get_media_service()

    try:
        # Validate path to prevent directory traversal
        media_root = _resolve_media_root(media_service)
        full_path = media_root / file_path
        full_path_resolved = full_path.resolve()

        # Ensure the resolved path is within media_root
        if not str(full_path_resolved).startswith(str(media_root)):
            security_logger.warning(
                "Path traversal attempt detected in delete",
                extra={"user_id": str(current_user.id), "file_path": file_path}
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied"
            )

        # Check if file exists
        if not full_path_resolved.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found"
            )

        # Authorization check: verify user owns the file
        # Query media record by file_path
        statement = select(EntryMedia).where(EntryMedia.file_path == file_path)
        media_record = session.exec(statement).first()

        if media_record:
            # Get the entry to check ownership
            entry = session.get(Entry, media_record.entry_id)
            if not entry:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Associated entry not found"
                )

            # Check if entry belongs to a journal owned by the user
            entry_service = _get_entry_service(session)
            try:
                # This will raise an exception if user doesn't own the entry
                entry_service.get_entry_by_id(entry.id, current_user.id)
            except EntryNotFoundError:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You don't have permission to delete this file"
                )
            except Exception as e:
                security_logger.warning(
                    "Error checking entry ownership",
                    extra={"user_id": str(current_user.id), "entry_id": str(entry.id), "error": str(e)}
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You don't have permission to delete this file"
                )

            # Delete the media record from database
            session.delete(media_record)
            session.commit()

        # Delete file from filesystem
        success = await _maybe_await(
            media_service.delete_media_file(str(full_path_resolved))
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to delete file"
            )

        file_logger.info(
            "Media file deleted successfully",
            extra={"user_id": str(current_user.id), "file_path": file_path}
        )
        return {
            "message": "File deleted successfully",
            "file_path": file_path
        }
    except HTTPException:
        raise
    except Exception as e:
        error_logger.error(
            "Unexpected error deleting media file",
            extra={"user_id": str(current_user.id), "file_path": file_path, "error": str(e)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while deleting file"
        )


@router.get(
    "/info/{file_path:path}",
    responses={
        401: {"description": "Not authenticated"},
        403: {"description": "Account inactive or forbidden"},
        404: {"description": "File not found"},
    }
)
async def get_media_info(
    file_path: str,
    current_user: Annotated[User, Depends(get_current_user)]
):
    """Get media file information and metadata."""
    media_service = _get_media_service()

    try:
        # Validate path to prevent directory traversal
        media_root = _resolve_media_root(media_service)
        full_path = media_root / file_path
        full_path_resolved = full_path.resolve()

        # Ensure the resolved path is within media_root
        if not str(full_path_resolved).startswith(str(media_root)):
            security_logger.warning(
                "Path traversal attempt detected in info",
                extra={"user_id": str(current_user.id), "file_path": file_path}
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied"
            )

        info = await _maybe_await(
            media_service.get_media_info(str(full_path_resolved))
        )
        return info
    except MediaNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found"
        )
    except HTTPException:
        raise
    except Exception as e:
        error_logger.error(
            "Error getting media info",
            extra={"user_id": str(current_user.id), "file_path": file_path, "error": str(e)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get media information"
        )


@router.get(
    "/formats",
    responses={
        401: {"description": "Not authenticated"},
        403: {"description": "Account inactive"},
    }
)
async def get_supported_formats(
    current_user: Annotated[User, Depends(get_current_user)]
):
    """
    Get supported file formats.

    Returns lists of supported image, video, and audio formats.
    """
    try:
        media_service = _get_media_service()
        return await _maybe_await(media_service.get_supported_formats())
    except Exception as e:
        error_logger.error(
            "Error getting supported formats",
            extra={"user_id": str(current_user.id), "error": str(e)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get supported formats"
        )


@router.post(
    "/process/{entry_id}",
    responses={
        401: {"description": "Not authenticated"},
        403: {"description": "Account inactive"},
        404: {"description": "Entry not found"},
        500: {"description": "Processing failed"},
    }
)
async def process_entry_media(
    entry_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(_get_db_session)]
):
    """
    Trigger media processing for an entry.

    Generates thumbnails for images and videos that don't have them yet.
    """
    entry_service = _get_entry_service(session)

    try:
        # Verify entry belongs to user
        entry = entry_service.get_entry_by_id(entry_id, current_user.id)
        if not entry:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Entry not found"
            )

        # Get entry media
        media_list = entry_service.get_entry_media(entry_id, current_user.id)

        # Process each media file
        processed_count = 0
        media_service = _get_media_service()
        media_root = _resolve_media_root(media_service)

        for media in media_list:
            if not media.thumbnail_path:
                try:
                    # Validate path to prevent directory traversal
                    full_path = (media_root / media.file_path).resolve()
                    if not str(full_path).startswith(str(media_root)):
                        security_logger.warning(
                            "Path traversal attempt detected in process_entry_media",
                            extra={"user_id": str(current_user.id), "file_path": media.file_path}
                        )
                        continue

                    if not full_path.exists():
                        error_logger.warning(
                            f"Media file not found: {full_path}",
                            extra={"user_id": str(current_user.id), "media_id": str(media.id)}
                        )
                        continue

                    if media.media_type == "image":
                        # Generate image thumbnail
                        with open(full_path, 'rb') as f:
                            file_content = f.read()

                        thumbnail_path = await _maybe_await(
                            media_service._generate_image_thumbnail(
                                file_content, Path(full_path).name
                            )
                        )
                    elif media.media_type == "video":
                        # Generate video thumbnail
                        thumbnail_path = await _maybe_await(
                            media_service._generate_video_thumbnail(
                                str(full_path), Path(full_path).name
                            )
                        )
                    else:
                        # Skip other media types
                        continue

                    if thumbnail_path:
                        # Update media record with thumbnail path
                        media.thumbnail_path = thumbnail_path
                        session.add(media)
                        processed_count += 1

                except Exception as e:
                    error_logger.error(
                        f"Error processing media {media.id}",
                        extra={"user_id": str(current_user.id), "media_id": str(media.id), "error": str(e)},
                        exc_info=True
                    )

        session.commit()

        file_logger.info(
            f"Processed {processed_count} media files for entry",
            extra={"user_id": str(current_user.id), "entry_id": str(entry_id), "processed_count": processed_count}
        )

        return {
            "message": f"Processed {processed_count} media files",
            "entry_id": str(entry_id)
        }
    except HTTPException:
        raise
    except Exception as e:
        error_logger.error(
            "Unexpected error processing entry media",
            extra={"user_id": str(current_user.id), "entry_id": str(entry_id), "error": str(e)},
            exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while processing media"
        )
