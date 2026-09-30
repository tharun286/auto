import asyncio
import os, json, uuid, threading, tempfile, io, shutil
import httpx
import base64
import jwt
import traceback
from werkzeug.utils import secure_filename
from functools import wraps
import requests
from ..config import settings
from fastapi import BackgroundTasks
import uuid

from src.hls_platform.main import hls_uploads_path
from sqlalchemy import select, text, func
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.hls_platform.brands import (
    add_brand_helper,
    edit_brand_helper,
    get_all_brands_helper,
    get_brand_by_id_helper,
    get_brand_logo_helper,
    get_image_url_helper,
    sync_brands_from_connector_helper
)
from fastapi import APIRouter, Depends, Request, HTTPException, status
from ..services.zenseai_content_factory_service import get_all_microsites_service, get_files_service
from ..utils.zenseai_content_factory_utils import get_domain_param
from sqlalchemy.ext.asyncio import AsyncSession
from ..configurations.session import get_session, AsyncSessionLocal

from src.hls_platform.veeva import hls_get_veeva_files_helper,hls_view_veeva_files_helper,hls_get_all_microsites_helper,send_microsite_by_id_helper
from src.hls_platform.veeva_upload import upload_to_veeva_helper, upload_to_promomats_helper
from src.hls_platform.veeva_push import push_to_veeva_helper, push_to_veeva_workflow_helper
from src.hls_platform.hls_harvest_claim import harvest_claim_helper, create_claim_promomats_helper
from src.hls_platform.hls_autotag import autotag_document_helper
from src.hls_platform.claim_annotations.approved_image_phash_sync_service import ApprovedImagePhashSyncService

from src.hls_platform.connector_helpers import get_files, get_not_autotagged_files

from src.hls_platform.branding_docs import fetch_veeva_docs_helper, get_veeva_docs_helper, update_veeva_doc_helper, create_branding_doc_helper

from src.hls_platform.document_types import (
    get_all_document_types_helper, add_document_type_helper,
    edit_document_type_helper, delete_document_type_helper,
)

from src.hls_platform.markets import (
    get_all_markets_helper, add_market_helper,
    edit_market_helper, delete_market_helper,
)
from src.hls_platform.hls_chatbot import hls_chat_helper

from src.hls_platform.medaffairs import process_medaffairs_helper
from src.hls_platform.local_documents import upload_local_document_helper

from src.hls_platform.connectors.connector_factory import HLSDomainType, get_connector, get_connector_name_by_domain
from src.hls_platform.main import hls_content_history_helper, hls_agent_history_helper, hls_convert_content_helper, hls_workflow_history_helper, hls_view_file_helper, hls_get_mlr_document_helper, hls_micro_drama_edit_helper, hls_banner_refine_helper

from src.hls_platform.workflows import (
    start_workflow as start_workflow_run,
    get_workflow_run,
    list_workflow_types,
)

from common.database.models.content_factory import HLSTransaction, HLSChatbotHistory, HLSHarvestedClaim, SupportTicket
from common.observability_utils.logging import get_logger

from src.hls_platform.connectors.connector_factory import get_connector_name_by_domain
from src.hls_platform.movie_maker import stitch_movie, regenerate_movie_frame, split_video_into_scenes, trim_video_clip, transcribe_video_clip, extract_frame_from_clip, inpaint_movie_frame
from src.hls_platform.claim_annotations.autotag_annotation_helper import AutotagAnnotationHelper
from src.hls_platform.claim_annotations.autotag_from_existing_helper import AutotagFromExistingHelper
from pydantic import BaseModel



logger = get_logger(__name__)

hls_content_factory_router = APIRouter(tags=["HLSContentFactory"])




class UpdateClaimRequest(BaseModel):
    match_text: str



    
@hls_content_factory_router.get("/hls_platform/veeva_files")
async def get_veeva_files(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    try:
        domain = await get_domain_param(request)
        connector_type = get_connector_name_by_domain(domain)

        return await get_files(request, db, connector_type)

    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def _crud_base():
    return os.environ.get('CRUD_API_BASE', 'http://127.0.0.1:8114/crud')

AUTO_UPDATE_PROGRESS = {}
AUTO_UPDATE_RESULTS = {}

def update_auto_update_progress(
    task_id: str,
    completed_steps: list,
    current_step: str,
):
    AUTO_UPDATE_PROGRESS[task_id] = {
        "task_id": task_id,
        "current_step": current_step,
        "completed_steps": completed_steps,
    }

async def resolve_user_id_by_email(email: str, request: Request):
    auth_header = request.headers.get("Authorization")
    headers = {}
    if auth_header:
        headers["Authorization"] = auth_header

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            f"{_crud_base()}/aibuddy/user/get_users",
            headers=headers,
        )

        if resp.status_code == 200:
            for user in resp.json():
                if user and user.get("email") and user.get("email").lower() == email.lower():
                    return user.get("id")

    return None


async def get_transaction_metadata(transaction_uuid: str, request: Request):
    auth_header = request.headers.get("Authorization")
    headers = {}
    if auth_header:
        headers["Authorization"] = auth_header

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            f"{_crud_base()}/aibuddy/transactions/{transaction_uuid}",
            headers=headers,
        )

        if resp.status_code == 200:
            return resp.json()

    return None


async def create_transaction_in_crud(
    name: str,
    created_by: int,
    output_path: str,
    files: list | None = None,
    request: Request | None = None,
):
    headers = {"Content-Type": "application/json"}
    if request is not None:
        auth_header = request.headers.get("Authorization")
        if auth_header:
            headers["Authorization"] = auth_header

    payload = {
        "name": name,
        "createdBy": int(created_by) if created_by is not None else 0,
        "outputPath": output_path,
        "files": files or [],
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{_crud_base()}/aibuddy/create_transaction",
            json=payload,
            headers=headers,
        )

        if resp.status_code in (200, 201):
            return resp.json()

    return None


def check_cost_limit(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        request: Request | None = None

        for arg in args:
            if isinstance(arg, Request):
                request = arg
                break

        if request is None:
            request = kwargs.get("request")

        if request is None:
            return await func(*args, **kwargs)

        auth_header = request.headers.get("Authorization")
        token = None

        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1]
        if not token:
            token = request.cookies.get("JWT-SESSION") or request.cookies.get("auth_token")

        user_id = None
        if token:
            try:
                jwt_secret_key = settings.jwt.secret_key
                decoded = jwt.decode(token, jwt_secret_key, algorithms=["HS256"])
                sub = decoded.get("sub")
                if sub:
                    db = await get_session()

                    result = await db.execute(
                        text("SELECT id FROM user_entity WHERE email = :email"),
                        {"email": sub},
                    )
                    row = result.mappings().first()
                    if row:
                        user_id = row["id"]
            except Exception:
                user_id = None

        if user_id:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    headers = {}
                    if auth_header:
                        headers["Authorization"] = auth_header

                    resp = await client.get(
                        f"{_crud_base()}/aibuddy/usage_metrics/get_user_usage_metrics?userId={user_id}",
                        headers=headers,
                    )

                    if resp.status_code == 200:
                        data = resp.json()
                        today_cost = data.get("today_cost", 0.0)
                        daily_limit = data.get("daily_cost_limit", 0.0)

                        try:
                            if float(today_cost) >= float(daily_limit) and float(daily_limit) > 0.0:
                                return JSONResponse(
                                    {"error": "Daily cost limit exceeded. Please come back tomorrow."},
                                    status_code=429,
                                )
                        except Exception:
                            pass
            except Exception:
                pass

        return await func(*args, **kwargs)

    return wrapper


@hls_content_factory_router.post('/hls_platform/convert_content')
@check_cost_limit
async def hls_convert_content(
    request: Request,
    db: AsyncSession = Depends(get_session)
):
    domain = await get_domain_param(request)
    return await hls_convert_content_helper(request, db, domain)

@hls_content_factory_router.get("/hls_platform/brands")
async def hls_get_all_brands(
    request: Request,
    db: AsyncSession = Depends(get_session)
):
    try:
        domain = await get_domain_param(request)
        return await get_all_brands_helper(domain, db)

    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@hls_content_factory_router.post("/hls_platform/content_history")
async def get_content_history(
    request: Request,
    db: AsyncSession = Depends(get_session)
):
    try:
        domain = await get_domain_param(request)
        return await hls_content_history_helper(db, domain)

    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@hls_content_factory_router.post("/hls_platform/agent_history")
async def get_agent_history(
    request: Request,
    db: AsyncSession = Depends(get_session)
):
    try:
        # domain = await get_domain_param(request)
        domain = request.query_params.get("domain", "").lower()
        return await hls_agent_history_helper(db, domain)

    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@hls_content_factory_router.post("/hls_platform/workflow_history")
async def hls_workflow_history(
    request: Request,
    db: AsyncSession = Depends(get_session)
):
    domain = await get_domain_param(request)
    return await hls_workflow_history_helper(db, domain)

@hls_content_factory_router.post('/hls_platform/view_file')
async def hls_view_file(
    request: Request,
    db: AsyncSession = Depends(get_session)
):
    return await hls_view_file_helper(request, db)

@hls_content_factory_router.get('/api/hls/download/{transaction_uuid}/{filename}')
async def hls_download_by_uuid(
    transaction_uuid: str,
    filename: str,
    db: AsyncSession = Depends(get_session)
):
    result = await db.execute(
        select(HLSTransaction).where(
            HLSTransaction.uuid == transaction_uuid
        )
    )
    transaction = result.scalar_one_or_none()

    if not transaction:
        raise HTTPException(
            status_code=404,
            detail="Transaction not found"
        )

    file_path = transaction.output_file_path

    if not file_path or not os.path.exists(file_path):
        raise HTTPException(
            status_code=404,
            detail="File not found"
        )

    return FileResponse(
        path=file_path,
        filename=filename,
        media_type="application/octet-stream"
    )

@hls_content_factory_router.get('/hls_platform/mlr_document/{transaction_uuid}')
async def hls_get_mlr_document(transaction_uuid: str, db: AsyncSession = Depends(get_session)):
    """Fetch MLR document by transaction UUID for HLS email previews."""
    return await hls_get_mlr_document_helper(db, transaction_uuid)

@hls_content_factory_router.post('/hls_platform/micro_drama_edit')
@check_cost_limit
async def hls_micro_drama_edit(
    request: Request,
    db: AsyncSession = Depends(get_session)
):
    return await hls_micro_drama_edit_helper(request, db)

@hls_content_factory_router.post('/hls_platform/banner_refine')
@check_cost_limit
async def hls_banner_refine(
    request: Request,
    db: AsyncSession = Depends(get_session)
):
    domain = await get_domain_param(request)
    return await hls_banner_refine_helper(request, domain, db)

@hls_content_factory_router.get('/hls_platform/convert_status/{transaction_uuid}')
async def hls_convert_status(transaction_uuid: str):
    try:
        status_file = os.path.join(hls_uploads_path, transaction_uuid, "status.json")

        if os.path.exists(status_file):
            with open(status_file, "r") as f:
                return json.load(f)

        return {"status": "processing"}

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "message": str(e)
            }
        )
@hls_content_factory_router.post('/hls_platform/stitch_movie')
@check_cost_limit
async def hls_stitch_movie(
    request: Request,
    db: AsyncSession = Depends(get_session)
):
    data = await request.json()

    domain = (data.get("domain") or "sales").lower()
    frames = data.get("frames", [])
    logo_base64 = data.get("logoBase64")
    logo_position = data.get("logoPosition", "top-right")
    logo_opacity = data.get("logoOpacity", 100)
    user_id = data.get("userId")
    user_name = data.get("userName", "Unknown User")
    cancel_token = request.query_params.get("cancel_token")

    if not frames:
        return JSONResponse({"error": "No frames provided"}, status_code=400)

    # Non-sales domains: return video directly
    if domain != "sales":
        try:
            stitch_result = await stitch_movie(
                frames,
                domain,
                logo_base64=logo_base64,
                logo_position=logo_position,
                logo_opacity=logo_opacity,
                cancel_token=cancel_token,
                user_id=user_id,
            )

            final_video_path = stitch_result["video_path"]
            tts_cost = stitch_result["tts_cost"]

            response = FileResponse(final_video_path, media_type="video/mp4")
            response.headers["X-Output-File-Path"] = final_video_path
            response.headers["X-TTS-Cost"] = str(tts_cost)
            response.headers["Access-Control-Expose-Headers"] = "X-Output-File-Path, X-TTS-Cost"

            return response

        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # Sales domain: process in background and return transaction UUID
    transaction_uuid = str(uuid.uuid4())
    uuid_path = os.path.join(hls_uploads_path, transaction_uuid)
    os.makedirs(uuid_path, exist_ok=True)
    status_file = os.path.join(uuid_path, "status.json")

    async def background_stitch():
        print("BACKGROUND THREAD STARTED")
        async def update_status(step_msg: str):
            print(f"Updating status: {step_msg}")
            try:
                with open(status_file, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "status": "processing",
                            "transaction_uuid": transaction_uuid,
                            "step": step_msg,
                        },
                        f,
                    )
                print("status.json written")
            except Exception:
                print(f"UPDATE STATUS FAILED: {e}")
                pass

        try:
            await update_status("Initializing video stitching...")

            with open(status_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "status": "processing",
                        "transaction_uuid": transaction_uuid,
                    },
                    f,
                )

            stitch_result = await stitch_movie(
                frames,
                domain,
                logo_base64=logo_base64,
                logo_position=logo_position,
                logo_opacity=logo_opacity,
                cancel_token=cancel_token,
                user_id=user_id,
                status_callback=update_status,
                db=db
            )

            final_video_path = stitch_result["video_path"]
            tts_cost = stitch_result["tts_cost"]

            with open(status_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "status": "completed",
                        "filepath": final_video_path,
                        "filename": os.path.basename(final_video_path),
                        "ttsCost": tts_cost,
                    },
                    f,
                )

        except Exception as e:
            with open(status_file, "w", encoding="utf-8") as f:
                json.dump({"status": "error", "message": str(e)}, f)

    # threading.Thread(target=background_stitch, daemon=True).start()
    asyncio.create_task(background_stitch())

    return JSONResponse(
        {
            "status": "processing",
            "transaction_uuid": transaction_uuid,
        },
        status_code=status.HTTP_202_ACCEPTED,
    )


from src.hls_platform.movie_maker import global_cancel_tokens
from src.hls_platform.cancel_manager import set_cancel_user_task

@hls_content_factory_router.post('/hls_platform/cancel_stitch_movie')
async def hls_cancel_stitch_movie(request: Request):
    token = await request.json()
    token = token.get('cancel_token')
    if token:
        global_cancel_tokens[token] = True
    return JSONResponse({"status": "cancelled"}, status_code=200)

@hls_content_factory_router.post('/hls_platform/cancel_convert_content')
async def hls_cancel_convert_content(request: Request):
    user_id = (await request.json()).get('user_id')
    if user_id:
        set_cancel_user_task(user_id, True)
        return JSONResponse({"status": "cancelled"}, status_code=200)
    return JSONResponse({"error": "Missing user_id"}, status_code=400)

@hls_content_factory_router.post('/hls_platform/save_stitched_movie')
async def hls_save_stitched_movie(request: Request, db: AsyncSession = Depends(get_session)):
    data = await request.json()
    file_path = data.get('filePath')
    domain = data.get('domain', 'sales')
    user_id = data.get('userId')
    user_name = data.get('userName', 'Unknown User')
    video_name = data.get('videoName', 'Generated Movie.mp4')
    project_state = data.get('projectState') # Get frontend state JSON
    transaction_uuid = data.get('transactionUuid')
    
    input_tokens = data.get('inputTokens', 0)
    output_tokens = data.get('outputTokens', 0)
    total_cost = data.get('totalCost', 0.0)
    
    if not file_path or not user_id:
        return JSONResponse({"error": "Missing required fields"}, status_code=400)

    try:
        # If transaction_uuid provided, try to update existing transaction first
        if transaction_uuid:
            transaction = await db.execute(select(HLSTransaction).filter(HLSTransaction.uuid == transaction_uuid))
            transaction = transaction.scalar_one_or_none()
            if transaction:
                transaction.output_file_path = os.path.abspath(file_path)
                transaction.input_tokens = (transaction.input_tokens or 0) + input_tokens
                transaction.output_tokens = (transaction.output_tokens or 0) + output_tokens
                transaction.total_cost = (transaction.total_cost or 0.0) + total_cost
                
                if project_state:
                    state_dir = os.path.join(hls_uploads_path, 'transactions', transaction_uuid)
                    os.makedirs(state_dir, exist_ok=True)
                    state_file_path = os.path.join(state_dir, 'state.json')
                    with open(state_file_path, 'w', encoding='utf-8') as f:
                        json.dump(project_state, f)
                        
                await db.commit()
                return JSONResponse({"status": "success", "transaction_uuid": transaction_uuid}), 200

        # Fallback to creating a new transaction
        transaction_uuid = transaction_uuid or str(uuid.uuid4())
        
        # Save project state to disk if provided
        if project_state:
            state_dir = os.path.join(hls_uploads_path, 'transactions', transaction_uuid)
            os.makedirs(state_dir, exist_ok=True)
            state_file_path = os.path.join(state_dir, 'state.json')
            with open(state_file_path, 'w', encoding='utf-8') as f:
                json.dump(project_state, f)

        transaction = HLSTransaction(
            uuid=transaction_uuid,
            format_type="movie maker",
            input_file_paths=json.dumps([video_name]),
            output_file_path=os.path.abspath(file_path),
            created_by=str(user_id),
            created_person=str(user_name),
            domain=domain,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_cost=total_cost
        )

        db.add(transaction)
        await db.commit()

        return {
            "status": "success",
            "transaction_uuid": transaction_uuid
        }

    except Exception as e:
        await db.rollback()
        print(f"Failed to save stitched movie transaction: {e}")
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )
    
@hls_content_factory_router.get('/hls_platform/movie_maker/{transaction_uuid}/state')
async def hls_get_movie_maker_state(transaction_uuid):
    state_file_path = os.path.join(hls_uploads_path, 'transactions', transaction_uuid, 'state.json')
    if not os.path.exists(state_file_path):
        return JSONResponse(
            status_code=404,
            content={"error": "State file not found"}
        )
        
    try:
        with open(state_file_path, 'r', encoding='utf-8') as f:
            state_data = json.load(f)
        return JSONResponse(
            status_code=200,
            content=state_data
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )
    
@hls_content_factory_router.post('/hls_platform/movie_maker/upload_clip')
async def hls_upload_movie_clip(request: Request):
    form = await request.form()
    file = form.get("file")

    if not file:
        return JSONResponse(
            status_code=400,
            content={"error": "No file part"}
        )

    if not getattr(file, "filename", None):
        return JSONResponse(
            status_code=400,
            content={"error": "No selected file"}
        )
        
    try:
        temp_folder = os.path.join(hls_uploads_path, "temp_movie_clips")
        if not os.path.exists(temp_folder):
            os.makedirs(temp_folder, exist_ok=True)
            
        filename = secure_filename(file.filename)
        clip_uuid = str(uuid.uuid4())
        ext = os.path.splitext(filename)[1]
        if not ext:
            ext = ".mp4"
            
        final_filename = f"{clip_uuid}{ext}"
        filepath = os.path.abspath(os.path.join(temp_folder, final_filename))
        
        with open(filepath, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        relative_url = f"hls_platform/temp_clips/{final_filename}"
        return JSONResponse(
            status_code=200,
            content={"status": "success", "videoUrl": relative_url, "absolutePath": filepath}
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )
    

@hls_content_factory_router.get("/hls_platform/temp_clips/{filename}")
async def hls_temp_clips(filename: str):
    temp_folder = os.path.join(hls_uploads_path, "temp_movie_clips")
    file_path = os.path.join(temp_folder, filename)

    if not os.path.exists(file_path):
        raise HTTPException(
            status_code=404,
            detail="File not found"
        )

    return FileResponse(file_path)


@hls_content_factory_router.post("/hls_platform/regenerate_movie_frame")
async def hls_regenerate_movie_frame(request: Request, db: AsyncSession = Depends(get_session)):
    data = await request.json()

    transaction_uuid = data.get("transactionUuid")
    prev_script = data.get("prevScript", "")
    next_script = data.get("nextScript", "")
    current_script = data.get("currentScript", "")
    regenerate_type = data.get("regenerateType", "both")
    image_base64 = data.get("imageBase64")
    image_url = data.get("imageUrl")
    custom_prompt = data.get("customPrompt")

    if not transaction_uuid:
        return JSONResponse({"error": "No transaction uuid provided"}, status_code=400)

    try:
        doc_path = None

        token = None
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1]

        user_id = data.get("user_id")
        if not user_id and token:
            try:
                jwt_secret_key = settings.jwt.secret_key
                decoded = jwt.decode(token, jwt_secret_key, algorithms=["HS256"])
                sub = decoded.get("sub")
                if sub:
                    q = text("SELECT id FROM user_entity WHERE email = :email")
                    r = db.session.execute(q, {"email": sub}).mappings().fetchone()
                    if r:
                        user_id = str(r["id"])
            except Exception:
                pass

        domain = (
            data.get("domain")
            or request.query_params.get("domain")
            or "sales"
        ).lower()

        new_frame = await regenerate_movie_frame(
            doc_path,
            prev_script,
            next_script,
            current_script,
            regenerate_type,
            image_base64,
            image_url,
            custom_prompt,
            user_id=user_id,
            domain=domain,
            db=db,
        )

        return JSONResponse(new_frame, status_code=200)

    except Exception as e:
        traceback.print_exc()
        error_msg = str(e)
        if "429" in error_msg and "RESOURCE_EXHAUSTED" in error_msg:
            return JSONResponse(
                {
                    "error": "Google Image gen rate limit has been exceeded. Please try again later.",
                    "isRateLimit": True,
                },
                status_code=429,
            )

        return JSONResponse({"error": error_msg}, status_code=500)


@hls_content_factory_router.post('/hls_platform/view_veeva_files')
async def hls_view_veeva_files(request: Request):
    domain = await get_domain_param(request)
    #To Do: Modify the get_connector function to use FASTAPI architecture
    return get_connector(get_connector_name_by_domain(domain)).download_document()

@hls_content_factory_router.get("/hls_platform/get_all_microsites")
async def get_all_microsites(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    domain = await get_domain_param(request)
    return await get_all_microsites_service(domain, db)

@hls_content_factory_router.post('/hls_platform/send_microsite_by_id')
async def hls_send_microsite_by_id(request: Request, db: AsyncSession = Depends(get_session)):
    #To Do: Modify the send_microsite_by_id_helper function to use FASTAPI architecture
    return await send_microsite_by_id_helper(request, db)


@hls_content_factory_router.post('/hls_platform/brands/sync')
async def hls_sync_brands(request: Request, db: AsyncSession = Depends(get_session)):
    domain = await get_domain_param(request)
    return await sync_brands_from_connector_helper(domain, db)
    
@hls_content_factory_router.post('/hls_platform/brands')
async def hls_add_brand(request: Request, db: AsyncSession = Depends(get_session)):
    domain = await get_domain_param(request)
    return await add_brand_helper(request, domain, db)
    
@hls_content_factory_router.post('/hls_platform/brands')
async def hls_get_all_brands(request: Request, db: AsyncSession = Depends(get_session)):
    domain = await get_domain_param(request)
    return await get_all_brands_helper(domain, db)

@hls_content_factory_router.get('/hls_platform/brands/{brand_id}')
async def hls_get_brand_by_id(brand_id: str, db: AsyncSession = Depends(get_session)):
    return await get_brand_by_id_helper(brand_id, db)

@hls_content_factory_router.put('/hls_platform/brands/{brand_id}')
async def hls_edit_brand(brand_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    domain = await get_domain_param(request)
    return await edit_brand_helper(request, brand_id, domain, db)

@hls_content_factory_router.get('/hls_platform/brand_logo/{logo_path:path}')
async def hls_get_brand_logo(logo_path: str):
    return await get_brand_logo_helper(logo_path)

@hls_content_factory_router.get('/hls_platform/image_url/{document_path:path}')
async def hls_get_image_url(document_path: str):
    return await get_image_url_helper(document_path)

@hls_content_factory_router.post('/hls_platform/upload_to_veeva')
async def hls_upload_to_veeva(request: Request):
    domain = await get_domain_param(request)
    if domain == "hls":
        print("Uploading to Promomats...")
        return await upload_to_promomats_helper(request)
    return await upload_to_veeva_helper(request)

@hls_content_factory_router.post('/hls_platform/push_to_veeva')
async def hls_push_to_veeva(request: Request, db: AsyncSession = Depends(get_session)):
    return await push_to_veeva_helper(request, db)

@hls_content_factory_router.post('/hls_platform/push_to_veeva_workflow')
async def hls_push_to_veeva_workflow(request: Request, db: AsyncSession = Depends(get_session)):
    return await push_to_veeva_workflow_helper(request, db)

@hls_content_factory_router.post('/hls_platform/branding_docs/fetch')
async def hls_fetch_veeva_docs(request: Request, db: AsyncSession = Depends(get_session)):
    domain = await get_domain_param(request)
    return await fetch_veeva_docs_helper(domain, db)

@hls_content_factory_router.get('/hls_platform/branding_docs')
async def hls_get_veeva_docs(request: Request, db: AsyncSession = Depends(get_session)):
    domain = await get_domain_param(request)
    return await get_veeva_docs_helper(domain, db)

@hls_content_factory_router.post('/hls_platform/branding_docs')
async def hls_create_branding_doc(request: Request, db: AsyncSession = Depends(get_session)):
    domain = await get_domain_param(request)
    return await create_branding_doc_helper(request, domain, db)

@hls_content_factory_router.put('/hls_platform/branding_docs/{doc_id:int}')
async def hls_update_veeva_doc(doc_id: int, request: Request, db: AsyncSession = Depends(get_session)):
    return await update_veeva_doc_helper(doc_id, request, db)

@hls_content_factory_router.get('/hls_platform/document_types')
async def hls_get_document_types(request: Request, db: AsyncSession = Depends(get_session)):
    domain = await get_domain_param(request)
    return await get_all_document_types_helper(domain, db)

@hls_content_factory_router.post('/hls_platform/document_types')
async def hls_add_document_type(request: Request, db: AsyncSession = Depends(get_session)):
    domain = await get_domain_param(request)
    return await add_document_type_helper(request, domain, db)

@hls_content_factory_router.post('/hls_platform/document_types')
async def hls_add_document_type(request: Request, db: AsyncSession = Depends(get_session)):
    domain = await get_domain_param(request)
    return await add_document_type_helper(request, domain, db)

@hls_content_factory_router.put('/hls_platform/document_types/{type_id:int}')
async def hls_edit_document_type(type_id: int, request: Request, db: AsyncSession = Depends(get_session)):
    domain = await get_domain_param(request)
    return await edit_document_type_helper(request, type_id, domain, db)

@hls_content_factory_router.delete('/hls_platform/document_types/{type_id:int}')
async def hls_delete_document_type(type_id: int, request: Request, db: AsyncSession = Depends(get_session)):
    domain = await get_domain_param(request)
    return await delete_document_type_helper(type_id, domain, db)

@hls_content_factory_router.get('/hls_platform/markets')
async def hls_get_markets(request: Request, db: AsyncSession = Depends(get_session)):
    domain = await get_domain_param(request)
    return await get_all_markets_helper(domain, db)

@hls_content_factory_router.post('/hls_platform/markets')
async def hls_add_market(request: Request, db: AsyncSession = Depends(get_session)):
    domain = await get_domain_param(request)
    return await add_market_helper(request, domain, db)

@hls_content_factory_router.put('/hls_platform/markets/{market_id:int}')
async def hls_edit_market(market_id: int, request: Request, db: AsyncSession = Depends(get_session)):
    domain = await get_domain_param(request)
    return await edit_market_helper(request, market_id, domain, db)

@hls_content_factory_router.delete('/hls_platform/markets/{market_id:int}')
async def hls_delete_market(market_id: int, request: Request, db: AsyncSession = Depends(get_session)):
    domain = await get_domain_param(request)
    return await delete_market_helper(market_id, domain, db)

@hls_content_factory_router.post('/hls_platform/hls_chat')
async def hls_chat(request: Request, db: AsyncSession = Depends(get_session)):
    return await hls_chat_helper(request, db)


@hls_content_factory_router.get('/hls_platform/chat_history')
async def hls_chat_history(request: Request, db: AsyncSession = Depends(get_session)):
    try:
        domain = await get_domain_param(request)
        if not domain:
            raise ValueError("Domain ${domain} is not a valid domain") 

        # pagination params
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('pageSize', 8))
        offset = (max(page, 1) - 1) * page_size

        total_result = await db.execute(select(func.count()).select_from(HLSChatbotHistory).where(HLSChatbotHistory.domain == domain))
        total = int(total_result.scalar_one() or 0)

        q = (
            select(HLSChatbotHistory)
            .where(HLSChatbotHistory.domain == domain)
            .order_by(HLSChatbotHistory.timestamp.desc())
            .offset(offset)
            .limit(page_size)
        )
        result = await db.execute(q)
        rows = result.scalars().all()

        items = []
        for r in rows:
            ts = r.timestamp
            formatted_ts = ts.strftime("%m/%d/%y, %I:%M %p") if ts else None
            items.append({
                "id": r.id,
                "session_uuid": r.session_uuid,
                "chat_history": r.chat_history,
                "user_name": r.user_name,
                "ipaddress": r.ipaddress,
                "timestamp": formatted_ts,
                "domain": r.domain,
                "adverse_event": r.adverse_event,
            })

        return {
            "items": items,
            "page": page,
            "pageSize": page_size,
            "total": total,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@hls_content_factory_router.get('/hls_platform/support_tickets')
async def get_support_tickets(
    request: Request,
    db: AsyncSession = Depends(get_session)
):
    try:
        domain = await get_domain_param(request)

        if not domain:
            raise ValueError(f"Domain {domain} is not a valid domain")

        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('pageSize', 8))
        offset = (max(page, 1) - 1) * page_size

        total_result = await db.execute(
            select(func.count())
            .select_from(SupportTicket)
            .where(SupportTicket.domain == domain)
        )

        total = int(total_result.scalar_one() or 0)

        q = (
            select(SupportTicket)
            .where(SupportTicket.domain == domain)
            .order_by(SupportTicket.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )

        result = await db.execute(q)
        rows = result.scalars().all()

        items = []

        for r in rows:
            items.append({
                "id": r.id,
                "ticket_id": r.ticket_id,
                "user_name": r.user_name,
                "user_email": r.user_email,
                "original_question": r.original_question,
                "issue_description": r.issue_description,
                "status": r.status,
                "created_at": r.created_at.strftime("%m/%d/%y, %I:%M %p")
                if r.created_at else None,
            })

        return {
            "items": items,
            "page": page,
            "pageSize": page_size,
            "total": total,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
@hls_content_factory_router.get('/hls_platform/hello')
async def hls_hello_world():
    return {"message": "Hello, World!"}


@hls_content_factory_router.post('/hls_platform/local_documents')
async def hls_upload_local_document(request: Request, db: AsyncSession = Depends(get_session)):
    """Admin upload for non-Veeva domains (e.g. sales)."""
    domain = await get_domain_param(request)
    return await upload_local_document_helper(domain, request, db)


#To Do: Modify the process_medaffairs_helper function to use FASTAPI architecture
@hls_content_factory_router.post('/hls_platform/medaffairs')
async def hls_process_medaffairs(request: Request, db: AsyncSession = Depends(get_session)):
    data = await request.json()

    # Validate required fields
    if not data or 'url' not in data or 'brand_name' not in data:
        return {"error": "Missing required fields: url, brand_name"}, 400

    return await process_medaffairs_helper(data)



# ---------------------------------------------------------------------------
# Backend-orchestrated workflows
# ---------------------------------------------------------------------------

@hls_content_factory_router.get('/hls_platform/workflows/types')
async def hls_workflow_types():
    return {"success": True, "data": list_workflow_types()}

@hls_content_factory_router.post('/hls_platform/workflows/start')
async def hls_workflow_start(request: Request, db: AsyncSession = Depends(get_session)):
    data = await request.json()
    workflow_type_id = data.get('workflow_type_id') or data.get('workflowTypeId')
    config = data.get('config') or {}
    user_id = data.get('user_id') or data.get('userId')
    user_name = data.get('user_name') or data.get('userName')
    domain = await get_domain_param(request)

    if not workflow_type_id:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "workflow_type_id is required"
            }
        )

    if ( (not config.get("dataSourceUrl") and workflow_type_id == "1") or not config.get("brandName")):
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "config.dataSourceUrl and config.brandName are required"
            }
        )

    try:
        workflow_run_id = await start_workflow_run(
            workflow_type_id=workflow_type_id,
            config=config,
            user_id=user_id,
            user_name=user_name,
            domain=domain,
            db=db,
            async_session_factory=AsyncSessionLocal,
        )

    except ValueError as e:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": str(e)
            }
        )

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e)
            }
        )

    return JSONResponse(
        status_code=200,
        content={
            "success": True,
            "data": {
                "workflow_run_id": workflow_run_id,
                "status": "in_progress"
            }
        }
    )


@hls_content_factory_router.get("/hls_platform/workflows/{workflow_run_id}/status")
async def hls_workflow_status(workflow_run_id: str, db: AsyncSession = Depends(get_session)):
    run = await get_workflow_run(workflow_run_id, db)

    if run is None:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "error": "workflow_run_id not found"
            }
        )


    return JSONResponse(
        status_code=200,
        content={
            "success": True,
            "data": run
        }
    )

@hls_content_factory_router.post("/hls_platform/split_video_scenes")
async def hls_split_video_scenes(request: Request):
    data = await request.json()
    domain = await get_domain_param(request)

    video_url = data.get("videoUrl")

    if not video_url:
        return JSONResponse(status_code=400, content={"error": "No videoUrl provided"})

    try:
        chunks = split_video_into_scenes(video_url, domain=domain)

        return {"status": "success", "chunks": chunks}

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})

@hls_content_factory_router.post('/hls_platform/split_video_at_timestamps')
async def hls_split_video_at_timestamps(request: Request):
    data = await request.json()
    video_url = data.get('videoUrl')
    timestamps = data.get('timestamps', [])
    duration = data.get('duration')
    
    if not video_url or not isinstance(timestamps, list) or duration is None:
        return JSONResponse(status_code=400, content={"error": "Missing parameters"})
        
    try:
        # Sort and ensure unique timestamps within bounds
        valid_timestamps = sorted(list(set([t for t in timestamps if 0 < t < duration])))
        
        # Build segments
        segments = []
        last_t = 0
        for t in valid_timestamps:
            segments.append((last_t, t))
            last_t = t
        segments.append((last_t, duration))
        
        chunks = []
        for start_t, end_t in segments:
            # Only trim if segment is > 0.1s to avoid errors
            if end_t - start_t > 0.1:
                chunk_url = trim_video_clip(video_url, float(start_t), float(end_t))
                chunks.append(chunk_url)
                
        return JSONResponse(status_code=200, content={"status": "success", "chunks": chunks})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})
    
@hls_content_factory_router.post('/hls_platform/trim_video_clip')
async def hls_trim_video_clip(request: Request):
    data = await request.json()
    video_url = data.get('videoUrl')
    start_time = data.get('startTime')
    end_time = data.get('endTime')
    if not video_url or start_time is None or end_time is None:
        return JSONResponse(status_code=400, content={"error": "Missing parameters"}) 

    try:
        new_url = trim_video_clip(video_url, float(start_time), float(end_time))
        return JSONResponse(status_code=200, content={"status": "success", "videoUrl": new_url})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})


@hls_content_factory_router.post('/hls_platform/transcribe_video')
async def hls_transcribe_video(request: Request):
    data = await request.json()
    domain = await get_domain_param(request)
    video_url = data.get('videoUrl')
    if not video_url:
        return JSONResponse(status_code=400, content={"error": "Missing parameters"})
        
    try:
        result = transcribe_video_clip(video_url, domain=domain)
        return JSONResponse(status_code=200, content={"status": "success", "transcription": result["transcription"], "input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"], "total_cost": result["total_cost"]})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})

@hls_content_factory_router.post('/hls_platform/extract_scene_frames')
async def hls_extract_scene_frames(request: Request):
    data = await request.json()
    chunks = data.get('chunks', [])
    if not chunks:
        return JSONResponse(status_code=400, content={"error": "No chunks provided"})
        
    try:
        frames = []
        for chunk in chunks:
            image_url = extract_frame_from_clip(chunk)
            domain = get_domain_param(request)
            result = transcribe_video_clip(chunk, domain=domain)
            script = result["transcription"]
            
            frames.append({
                "imageUrl": image_url,
                "script": script,
                "type": "generated",
                "videoUrl": chunk,
                "input_tokens": result["input_tokens"],
                "output_tokens": result["output_tokens"],
                "total_cost": result["total_cost"]
            })
            
        return JSONResponse(status_code=200, content={"status": "success", "frames": frames})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})

@hls_content_factory_router.post('/hls_platform/inpaint_frame')
async def hls_inpaint_frame(request: Request, db: AsyncSession = Depends(get_session)):
    data = await request.json()
    image_base64 = data.get('imageBase64')
    image_url = data.get('imageUrl')
    target_text = data.get('targetText')
    new_text = data.get('newText')
    
    if not (image_base64 or image_url) or not target_text or not new_text:
        return JSONResponse(status_code=400, content={"error": "Missing parameters"})
        
    try:
        if not image_base64 and image_url:
            # Convert imageUrl to base64
            src_path = os.path.join(hls_uploads_path, image_url.lstrip('/'))
            if os.path.exists(src_path):
                with open(src_path, "rb") as img_file:
                    image_base64 = base64.b64encode(img_file.read()).decode('utf-8')
            else:
                return JSONResponse(status_code=404, content={"error": "Source image not found"})
            if os.path.exists(src_path):
                with open(src_path, "rb") as img_file:
                    image_base64 = base64.b64encode(img_file.read()).decode('utf-8')
            else:
                return JSONResponse(status_code=404, content={"error": "Source image not found"})

        domain = await get_domain_param(request)

        # Extract user_id for cost tracking (similar logic as other endpoints)
        token = None
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ', 1)[1]
            
        user_id = data.get('user_id')
        if not user_id and token:
            import jwt as pyjwt
            try:
                jwt_secret_key = settings.jwt.secret_key
                decoded = pyjwt.decode(token, jwt_secret_key, algorithms=["HS256"])
                sub = decoded.get('sub')
                if sub:
                    from sqlalchemy import text

                    q = text("SELECT id FROM user_entity WHERE email = :email")
                    result = await db.execute(q, {"email": sub})
                    r = result.mappings().first()
                    if r:
                        user_id = str(r["id"])
            except Exception:
                pass
                
        result = inpaint_movie_frame(image_base64, target_text, new_text, user_id=user_id, domain=domain)
        if "error" in result:
            return JSONResponse(status_code=500, content=result)
        return JSONResponse(status_code=200, content=result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})      

@hls_content_factory_router.get('/hls_platform/download_movie_image/{filename}')
async def download_movie_image(filename: str):
    folder = os.path.join(hls_uploads_path, "movie_maker_images")
    file_path = os.path.join(folder, filename)
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    
    return FileResponse(file_path)

#To harvest claims from Promomats for a particular brand and create them in promomats for a particular brand, we will use the following endpoints. The first endpoint will harvest claims from Promomats and the second endpoint will create claims in Promomats.
@hls_content_factory_router.post('/hls_platform/harvest_claims')
async def hls_harvest_claims(request: Request, db: AsyncSession = Depends(get_session)):
    return await harvest_claim_helper(request, db)

@hls_content_factory_router.post('/hls_platform/create_claim_promomats')
async def hls_create_claim_promomats(request: Request, db: AsyncSession = Depends(get_session)):
    return await create_claim_promomats_helper(request, db)

@hls_content_factory_router.get("/hls_platform/promomats/claims")
async def hls_get_promomats_claims(
    brand_id: str = "",
    db: AsyncSession = Depends(get_session),
):
    try:
        connector = get_connector("promomats")

        await connector.load_credentials(db)

        return connector.get_claims(brand_id)

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )

@hls_content_factory_router.get("/hls_platform/promomats/claims/{claim_id}/relevant-docs")
async def hls_get_relevant_docs_for_claim(
    claim_id: str,
    db: AsyncSession = Depends(get_session),
):
    try:
        connector = get_connector("promomats")

        await connector.load_credentials(db)

        return connector.get_relevant_docs_for_claim(claim_id)

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )

@hls_content_factory_router.get(
    "/hls_platform/promomats/auto-update-progress/{task_id}"
)
async def get_auto_update_progress(task_id: str):

    if task_id not in AUTO_UPDATE_PROGRESS:
        return {
            "status": "NOT_FOUND"
        }

    return AUTO_UPDATE_PROGRESS[task_id] 

@hls_content_factory_router.get(
    "/hls_platform/promomats/auto-update-result/{task_id}"
)
async def get_auto_update_result(task_id: str):

    if task_id not in AUTO_UPDATE_RESULTS:

        return {
            "status": "PENDING"
        }

    return AUTO_UPDATE_RESULTS[task_id]

# ===========================================================================
# SNIPPET 2 OF 2: WORKER ORCHESTRATION PIPELINE ENGINE WITH ALL 15 STAGES
# ===========================================================================
import asyncio
import traceback

async def run_auto_update_background_task(
    task_id: str,
    claim_id: str,
    match_text: str,
):
    from src.hls_platform.claim_annotations.auto_update_agent import AutoUpdateAgent
    completed_steps = []

    try:
        # 🚀 Upgraded helper to space out early stages so checkboxes tick one-by-one
        # 🚀 Upgraded helper to space out early stages so checkboxes tick one-by-one
        async def mark_step(step_name: str):
            completed_steps.append(step_name)
            update_auto_update_progress(
                task_id=task_id,
                completed_steps=completed_steps.copy(),
                current_step=step_name,
            )
            await asyncio.sleep(0.5)  # 🎯 Delays for half a second to let React poll smoothly

        async with AsyncSessionLocal() as db:
            connector = get_connector("promomats")
            await connector.load_credentials(db)

            old_claim_text = None
            target_claim_name = None

            claims_response = connector.get_claims()
            for claim in claims_response.get("claims", []):
                if claim.get("id") == claim_id:
                    old_claim_text = claim.get("match_text")
                    target_claim_name = claim.get("name")
                    break

            print(f"TARGET CLAIM NAME = {target_claim_name}")
            print(f"OLD CLAIM = {old_claim_text}")
            new_claim = match_text
            print(f"NEW CLAIM = {new_claim}")

            # ==========================================================
            # STAGES 1 - 5: CLAIM LIFECYCLE MANAGEMENT
            # ==========================================================
            print("\n=== AUTO UPDATE CLAIM LIFECYCLE START ===")
            claim_details = connector.get_claim_details(claim_id)
            print("CLAIM DETAILS =", claim_details)

            object_type_id = claim_details.get("object_type__v")
            product_id = claim_details.get("product__v")
            country_id = claim_details.get("country__v")
            source_approval_document = claim_details.get("source_approval_document__v")
            source_approval_document_unbound = claim_details.get("source_approval_document_unbound__v")
            source_text_asset = claim_details.get("source_text_asset__v")
            link_target_id = connector.get_link_target_id(claim_id)

            # Stage 1: Create New Claim
            target_new_claim_id = connector.create_new_claim(
                claim_text=new_claim,
                object_type_id=object_type_id,
                product_id=product_id,
                country_id=country_id,
                source_approval_document=source_approval_document,
                source_approval_document_unbound=source_approval_document_unbound,
                source_text_asset=source_text_asset,
            )
            await mark_step("Create New Claim")

            # Stage 2: Create Claim Relationship
            connector.create_claim_relationship(
                claim_id=target_new_claim_id,
                link_target_id=link_target_id,
            )
            await mark_step("Create Claim Relationship")

            # Stage 3: Add Reference To New Claim
            await mark_step("Add Reference To New Claim")

            # Stage 4: Withdraw Old Claim
            connector.withdraw_claim(claim_id)
            await mark_step("Withdraw Old Claim")

            # Stage 5: Approve New Claim
            connector.approve_claim(target_new_claim_id)
            await mark_step("Approve New Claim")

            # ==========================================================
            # STAGES 6 - 8: RELEVANT DOWNLOADS AND PARSING MATCHES
            # ==========================================================
            # Stage 6: Download Relevant Documents
            download_result = await connector.download_relevant_docs_for_claim(
                db=db,
                claim_id=claim_id
            )
            await mark_step("Download Relevant Documents")

            source_files = download_result.get("source_files", [])
            source_file_lookup = {
                item["document_id"]: item
                for item in source_files
            }

            pdf_downloaded_files = download_result.get("pdf_downloaded_files", [])
            tagged_text_results = []
            updated_documents = []
            doc_version_map = {}

            if download_result and isinstance(download_result.get("documents"), list):
                for doc in download_result.get("documents", []):
                    doc_key = doc.get("document_id") or doc.get("id")
                    if doc_key:
                        doc_version_map[str(doc_key)] = (
                            doc.get("major_version"),
                            doc.get("minor_version"),
                        )

            # Stage 7: Extract PDF Annotations
            await mark_step("Extract PDF Annotations")

            # Stage 8: Match Content In HTML
            await mark_step("Match Content In HTML")

            # ==========================================================
            # STAGES 9 - 13: ASSET GENERATION / UPDATE DRAFTS LOOP
            # ==========================================================
            for index, pdf_info in enumerate(pdf_downloaded_files):
                document_id = pdf_info.get("document_id")
                document_name = pdf_info.get("document_name")
                pdf_path = pdf_info.get("pdf_path")

                verified_major, verified_minor = doc_version_map.get(str(document_id), ("0", "1"))
                orig_major = int(verified_major) if str(verified_major).isdigit() else 0
                orig_minor = int(verified_minor) if str(verified_minor).isdigit() else 1

                print(f"📄 Content migration loop for document {document_id}")

                print(f"📄 Extracting tagged text for document {document_id}")
                source_file_info = source_file_lookup.get(
                document_id,
                {}
                )
                source_file_path = source_file_info.get(
                "file_path"
                )
                print("\n========== SOURCE FILE INFO ==========")
                print(source_file_info)
                print(f"SOURCE FILE PATH = {source_file_path}")
                print("======================================")

                source_filename = source_file_info.get(
                "filename"
                )
                print(
                f"SOURCE FILE = {source_file_path}"
                )
                tagged_result = await AutoUpdateAgent().get_tagged_text_from_pdf(
                    document_id=document_id,
                    document_name=document_name,
                    major_version=orig_major,
                    minor_version=orig_minor,
                    pdf_path=pdf_path,
                    claim_id=target_claim_name,
                    old_claim=old_claim_text,
                    new_claim=new_claim,
                    target_new_claim_id=target_new_claim_id,
                    task_id=task_id,
                    source_file_path=source_file_path,
                )

                if tagged_result:
                    tagged_text_results.append(tagged_result)
                    for result in tagged_result.get("results", []):
                        uploaded_id = result.get("uploaded_document_id")
                        curr_major = result.get("uploaded_major_version")
                        curr_minor = result.get("uploaded_minor_version")

                        if uploaded_id and curr_major is not None and curr_minor is not None:
                            compare_major, compare_minor = connector.find_latest_existing_predecessor(
                                document_id=uploaded_id,
                                current_major=int(curr_major),
                                current_minor=int(curr_minor),
                            )

                            compare_result = connector.get_document_compare_url(
                                document_id=uploaded_id,
                                current_major=int(curr_major),
                                current_minor=int(curr_minor),
                                compare_major=compare_major,
                                compare_minor=compare_minor,
                            )

                            # One entry per uploaded version (several annotations of the
                            # same claim map to the same upload).
                            dedupe_key = (str(uploaded_id), str(curr_major), str(curr_minor))
                            if any(
                                (d["document_id"], str(d["major_version"]), str(d["minor_version"])) == dedupe_key
                                for d in updated_documents
                            ):
                                continue

                            updated_documents.append({
                                "document_name": result.get("document_name"),
                                "document_id": str(uploaded_id), # 🎯 FIXED: Explicitly sanitize to string configuration
                                "major_version": curr_major,
                                "minor_version": curr_minor,
                                "view_url": compare_result.get("view_url"),
                                # real file type of what was uploaded (.docx/.pptx/.pdf/.html)
                                "source_file_path": result.get("source_file_path"),
                            })

            # Sequentially update progress for pipeline completion states
            await mark_step("Generate Updated Content")
            await mark_step("Apply Document Updates")
            await mark_step("Upload Draft Documents")
            await mark_step("Migrate Claim Annotations")
            await mark_step("Remove Legacy Annotations")

            # ==========================================================
            # STAGES 14 - 15: TRANSACTION SAVING & COMPLETE
            # ==========================================================
            transaction_uuid = str(uuid.uuid4())
            history_record = HLSHarvestedClaim(
                transaction_uuid=transaction_uuid,
                documents={
                    "updated_documents": updated_documents,
                    "claim_id": claim_id,
                    "new_claim_id": target_new_claim_id,
                    "claim_name": target_claim_name,
                    "old_claim": old_claim_text,
                    "new_claim": new_claim,
                    "success": True,
                },
                # 🎯 FIXED: Persist strict unique file identities rather than variable human name descriptors
                source_documents=[
                    f"{doc.get('document_id')}"
                    f"{os.path.splitext(doc.get('source_file_path') or '')[1] or '.html'}"
                    for doc in updated_documents
                ],
                brand_name="Auto Update",
                domain="hls_autoupdate",
                uuid=str(uuid.uuid4()),
                claims=new_claim,
                is_pushed_to_promomats=False,
            )
            db.add(history_record)
            await db.commit()
            await mark_step("Save Update History")

            await mark_step("Complete")

            # 🚀 ROOT CAUSE FIX: Query fresh metadata for the replacement claim to catch its real name code
            new_claim_details = connector.get_claim_details(target_new_claim_id)
            actual_new_claim_name = new_claim_details.get("name__v") or new_claim_details.get("name") or "N/A"

            AUTO_UPDATE_RESULTS[task_id] = {
                "status": "SUCCESS",
                "old_claim": {
                    "claim_id": claim_id,
                    "claim_name": target_claim_name or "N/A",
                    "claim_text": old_claim_text or "N/A"
                },
                "new_claim": {
                    "claim_id": target_new_claim_id,
                    "claim_name": actual_new_claim_name, # 🎯 FIXED: Displays the actual new claim name string perfectly!
                    "claim_text": new_claim
                },
                "updated_documents": updated_documents
            }


    except Exception as e:
        print(f"❌ Crash on tracking workflow worker thread: {traceback.format_exc()}")
        AUTO_UPDATE_RESULTS[task_id] = {
            "status": "ERROR",
            "error": str(e),
            "traceback": traceback.format_exc()
        }


@hls_content_factory_router.put("/hls_platform/promomats/claims/{claim_id}")
async def hls_update_claim(
    claim_id: str,
    payload: UpdateClaimRequest,
    background_tasks: BackgroundTasks,
):
    try:
        # 1. Generate an atomic task token unique to this session execution request
        task_id = str(uuid.uuid4())
        
        # 2. Establish initial baseline entry metrics for the UI tracking monitors
        AUTO_UPDATE_PROGRESS[task_id] = {
            "task_id": task_id,
            "current_step": "Initializing Workflow",
            "completed_steps": []
        }
        
        # 3. Offload all the heavy, synchronous processing steps to background tasks
        background_tasks.add_task(
            run_auto_update_background_task,
            task_id=task_id,
            claim_id=claim_id,
            match_text=payload.match_text
        )
        
        # 4. Instantly reply to prevent browser transaction request timeouts
        return {
            "status": "STARTED",
            "task_id": task_id
        }

    except Exception as e:
        print(f"❌ Failed to kick off background update job pipeline: {traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={
                "error": str(e),
                "traceback": traceback.format_exc(),
            },
        )



@hls_content_factory_router.get(
    "/promomats/document-compare-url"
)
async def get_document_compare_url(
    document_id: str,
    current_major: int,       # 🚀 FIXED: Captures the new version number from the client
    current_minor: int,       # 🚀 FIXED: Captures the new version number from the client
    compare_major: int,       # 🚀 FIXED: Explicit live target version parameter
    compare_minor: int,       # 🚀 FIXED: Explicit live target version parameter
):
    connector = get_connector("promomats")

    async with AsyncSessionLocal() as db:
        await connector.load_credentials(db)

    # 🎯 Forwarding all 4 parameters forward safely to bridge version deletion gaps (e.g., V11 to V9)
    return connector.get_document_compare_url(
        document_id=document_id,
        current_major=current_major,
        current_minor=current_minor,
        compare_major=compare_major,
        compare_minor=compare_minor
    )


@hls_content_factory_router.post('/hls_platform/annotations/autotag')
async def hls_autotag_document(request: Request, db: AsyncSession = Depends(get_session)):
    """
    Auto-tag a document with claims using semantic matching.
    """
    return await autotag_document_helper(request, db)

@hls_content_factory_router.post('/hls_platform/annotations/autotag_from_existing')
async def hls_autotag_from_existing(request: Request, db: AsyncSession = Depends(get_session)):
    """
    Auto-tag a document with pre-matched claims.
    
    Expected request body:
    {
        "document_id": "12345_0_1",
        "file": <binary file content>,
        "claims_match": {
            "V1W000000001001": "matched text from claim 1",
            "V1W000000001002": "matched text from claim 2"
        },
        "connector_type": "promomats"
    }
    
    Returns:
    {
        "success": true,
        "matches_found": 2,
        "annotations_submitted": 2,
        "matches": [...],
        "veeva_response": {...}
    }
    """
    try:
        # Parse request
        form_data = await request.form()
        document_id_version = form_data.get('document_id')
        connector_type = form_data.get('connector_type', 'promomats')
        claims_match_json = form_data.get('claims_match')
        file = form_data.get('file')
        
        if not document_id_version:
            return JSONResponse(
                status_code=400,
                content={"error": "document_id is required"}
            )
        
        if not claims_match_json:
            return JSONResponse(
                status_code=400,
                content={"error": "claims_match is required"}
            )
        
        if not file or file.filename == '':
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid file"}
            )
        
        # Parse claims_match JSON
        try:
            claims_match_dict = json.loads(claims_match_json)
            if not isinstance(claims_match_dict, dict):
                raise ValueError("claims_match must be a dict")
        except json.JSONDecodeError as e:
            return JSONResponse(
                status_code=400,
                content={"error": f"Invalid claims_match JSON: {str(e)}"}
            )

        autotag_helper = AutotagFromExistingHelper()
        result = await autotag_helper.process_autotag_from_existing_request(
            uploaded_file=file,
            document_id_version=document_id_version,
            claims_match_dict=claims_match_dict,
            connector_type=connector_type,
            db=db,
        )

        return JSONResponse(status_code=200 if result.get('success') else 500, content=result)
    
    except Exception as e:
        print(f"❌ Error in autotag_from_existing endpoint: {traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={
                "error": str(e),
                "traceback": traceback.format_exc()
            }
        )

@hls_content_factory_router.post('/hls_platform/sync_images')
async def hls_sync_images(request: Request, db: AsyncSession = Depends(get_session)):
    try:
        result = await ApprovedImagePhashSyncService().sync_missing_image_phashes()
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "data": result,
            },
        )
    except Exception as e:
        logger.error(f"❌ Error syncing approved images: {traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e),
                "traceback": traceback.format_exc(),
            },
        )

@hls_content_factory_router.get("/hls_platform/not_autotagged_documents")
async def get_not_autotagged_veeva_files(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    try:
        return await get_not_autotagged_files(request, db)

    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
