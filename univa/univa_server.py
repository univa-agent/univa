import asyncio
import uuid
import os
from typing import Dict, Optional, AsyncGenerator, Any
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import List
import json
import logging
from datetime import datetime
import traceback
import re

def _init_env():
    base = Path(__file__).resolve().parents[1]
    env_file = base / ".env"
    if not env_file.exists():
        raise RuntimeError("Config missing: please copy univa/.env.example to univa/.env and fill your keys.")
    load_dotenv(dotenv_path=str(env_file), override=False)

_init_env()

from univa.univa_agent import PlanActSystem

from univa.config.config import config, auth_service
from univa.auth.middleware import AuthMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

os.environ['UniVA_HTTP_SERVER_MODE'] = 'true'

app = FastAPI(title="UniVA Chat API", version="0.1.0")

# CORS middleware setting
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth middleware
app.add_middleware(
    AuthMiddleware,
    auth_service=auth_service,
    auth_enabled=config.get('auth_enabled', True)
)

global_plan_act_system: Optional[PlanActSystem] = None


async def initialize_global_agents() -> PlanActSystem:
    global global_plan_act_system
    
    if global_plan_act_system:
        return global_plan_act_system
    
    config_path = config.get('mcp_servers_config')
    
    mcp_commands = []
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            mcp_config = json.load(f)
        mcp_servers = mcp_config.get("mcpServers", {})
        logger.info(f"Loaded {len(mcp_servers)} MCP servers from config")
        
        # construct mcp commands
        for server_name, server_config in mcp_servers.items():
            command = server_config.get("command", "")
            args = server_config.get("args", [])
            env = server_config.get("env", {})
            
            full_command = f"{command} {' '.join(args)}"
            
            mcp_commands.append(full_command)
            logger.info(f"Registered MCP server '{server_name}': {full_command}")
        
    except FileNotFoundError:
        logger.warning(f"MCP config file not found: {config_path}, using default")
    except Exception as e:
        logger.error(f"Error loading MCP config: {e}")
    
    global_plan_act_system = PlanActSystem(mcp_command=mcp_commands)
    await global_plan_act_system.__aenter__()
    
    logger.info("Global PlanActSystem initialized")
    
    return global_plan_act_system



class ChatRequest(BaseModel):
    prompt: str
    session_id: Optional[str] = None
    model: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    timestamp: str


async def stream_chat_response(user_id: str, session_id: str, user_prompt: str):
    """
    Stream chat response as SSE
    
    return SSE stream compatible with useCompletion
    """
    try:
        init_message = {
            'type': 'content',
            'content': 'Initializing system, please wait...'
        }
        init_json = json.dumps(init_message, ensure_ascii=False)
        logger.info("Sending system initialization message")
        sse_init = f"data: {init_json}\n\n"
        yield sse_init.encode('utf-8') if isinstance(sse_init, str) else sse_init
        await asyncio.sleep(0.01)
        
        system = await initialize_global_agents()
        
        logger.info(f"Streaming task execution for user {user_id}, session {session_id}")
        
        # calling agent's streaming execution method
        async for event in system.execute_task_stream(session_id, user_prompt):
            if event.get('type') == 'finish':
                event['session_id'] = session_id
            
            json_str = json.dumps(event, ensure_ascii=False)
            logger.info(f"Sending SSE event: {event.get('type', 'unknown')}")
            logger.debug(f"Event details:\n{json_str}")
            
            sse_message = f"data: {json_str}\n\n"
            yield sse_message.encode('utf-8') if isinstance(sse_message, str) else sse_message
            
            await asyncio.sleep(0.01)
        
        logger.info("Stream completed successfully")
        
    except Exception as e:
        logger.error(f"Error in stream_chat_response: {e}")
        logger.error(traceback.format_exc())
        error_message = f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        yield error_message.encode('utf-8') if isinstance(error_message, str) else error_message

@app.post("/chat/stream")
async def chat(request: ChatRequest, req: Request):
    """
    Unified chat request handling endpoint - using PlanActSystem (streaming)

    Access code is passed via X-Access-Code header (handled by auth middleware)
    Returns a streaming response compatible with Vercel AI SDK
    """
    try:
        # get user_id and access_code from request state (injected by auth middleware)
        user_id = getattr(req.state, 'user_id', 'anonymous')
        access_code = getattr(req.state, 'access_code', None)
        
        # check conversation limit
        if access_code and config.get('auth_enabled', True):
            if not auth_service.check_conversation_limit(access_code):
                code_info = auth_service.get_access_code_info(access_code)
                if code_info:
                    raise HTTPException(
                        status_code=429,
                        detail=f"Conversation limit reached ({code_info['conversation_count']}/{code_info['max_conversations']}). Please contact administrator."
                    )
                else:
                    raise HTTPException(status_code=429, detail="Conversation limit reached")
            
            # increment conversation count
            auth_service.increment_conversation_count(access_code)
        
        # generate session_id if not provided
        session_id = request.session_id or str(uuid.uuid4())
        
        logger.info(f"POST /chat/stream - user: {user_id}, session: {session_id}, prompt: {request.prompt[:50]}...")
        
        return StreamingResponse(
            stream_chat_response(user_id, session_id, request.prompt),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing chat request: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/chat/stream")
async def chat_get(
    prompt: str,
    session_id: Optional[str] = None,
    accessCode: Optional[str] = None,
    request: Request = None
):
    """
    Get method chat endpoint for streaming responses.
    Note: Access code is passed via URL parameter due to EventSource limitations.
    Returns a streaming response compatible with Vercel AI SDK.
    """
    try:
        # If accessCode is provided, validate it
        if accessCode and config.get('auth_enabled', True):
            user_id = auth_service.validate_access_code(accessCode)
            if not user_id:
                raise HTTPException(status_code=401, detail="Invalid access code")
            
            # Check conversation limit
            if not auth_service.check_conversation_limit(accessCode):
                code_info = auth_service.get_access_code_info(accessCode)
                if code_info:
                    raise HTTPException(
                        status_code=429,
                        detail=f"Conversation limit reached ({code_info['conversation_count']}/{code_info['max_conversations']}). Please contact administrator."
                    )
                else:
                    raise HTTPException(status_code=429, detail="Conversation limit reached")
            
            auth_service.increment_conversation_count(accessCode)
            
            # Inject user_id into request state for consistency
            request.state.user_id = user_id
        else:
            # get user_id from request state (injected by auth middleware)
            user_id = getattr(request.state, 'user_id', 'anonymous')
        
        sid = session_id or str(uuid.uuid4())
        
        return StreamingResponse(
            stream_chat_response(user_id, sid, prompt),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing chat request: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now().isoformat()
    )

@app.get("/")
async def root():
    return {"message": "UniVA Chat API is running"}


def verify_admin(req: Request):
    """verify admin access code from request"""
    admin_code = config.get('admin_access_code')
    logger.info(f"verify_admin: admin_code from config = {admin_code[:8] if admin_code else 'None'}...")
    
    if not admin_code:
        logger.error("Admin access code not configured in config")
        raise HTTPException(status_code=500, detail="Admin access code not configured")
    
    access_code = getattr(req.state, 'access_code', None)
    logger.info(f"verify_admin: access_code from request = {access_code[:8] if access_code else 'None'}...")
    
    if not access_code:
        logger.warning("Access code not found in request state")
        raise HTTPException(status_code=401, detail="Access code required")
    
    if access_code != admin_code:
        logger.warning(f"Access code mismatch: provided={access_code[:8]}..., expected={admin_code[:8]}...")
        raise HTTPException(status_code=403, detail="Admin access required")
    
    logger.info("Admin verification successful")
    return True


class CreateAccessCodeRequest(BaseModel):
    user_id: str
    description: str = ""
    max_conversations: Optional[int] = None


class BatchCreateAccessCodesRequest(BaseModel):
    count: int
    user_id_prefix: str = "user"
    description: str = ""
    max_conversations: Optional[int] = None


class UpdateAccessCodeRequest(BaseModel):
    description: Optional[str] = None
    enabled: Optional[bool] = None
    max_conversations: Optional[int] = None


class BatchDeleteRequest(BaseModel):
    access_codes: List[str]


class ImportCodesRequest(BaseModel):
    codes: List[Dict[str, Any]]
    overwrite: bool = False


@app.post("/admin/access-codes")
async def create_access_code(
    request: CreateAccessCodeRequest,
    req: Request = None,
    _: None = Depends(verify_admin)
):
    """create a new access code (admin only)"""
    try:
        access_code = auth_service.add_access_code(
            request.user_id,
            request.description,
            max_conversations=request.max_conversations
        )
        logger.info(f"Admin created access code for user {request.user_id}")
        return {
            "access_code": access_code,
            "user_id": request.user_id,
            "max_conversations": request.max_conversations
        }
    except Exception as e:
        logger.error(f"Error creating access code: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/admin/access-codes/batch")
async def batch_create_access_codes(
    request: BatchCreateAccessCodesRequest,
    req: Request = None,
    _: None = Depends(verify_admin)
):
    """create multiple access codes in batch (admin only)"""
    try:
        if request.count <= 0 or request.count > 100:
            raise HTTPException(status_code=400, detail="Count must be between 1 and 100")
        
        created_codes = []
        for i in range(request.count):
            user_id = f"{request.user_id_prefix}_{i+1}"
            access_code = auth_service.add_access_code(
                user_id,
                request.description,
                max_conversations=request.max_conversations
            )
            created_codes.append({
                "access_code": access_code,
                "user_id": user_id,
                "max_conversations": request.max_conversations
            })
        
        logger.info(f"Admin batch created {len(created_codes)} access codes")
        return {"codes": created_codes, "count": len(created_codes)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error batch creating access codes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/access-codes/stats")
async def get_access_codes_stats(
    req: Request = None,
    _: None = Depends(verify_admin)
):
    """get statistics about access codes (admin only)"""
    try:
        codes = auth_service.list_access_codes()
        
        total_codes = len(codes)
        enabled_codes = sum(1 for c in codes if c['enabled'])
        disabled_codes = total_codes - enabled_codes
        
        total_usage = sum(c['usage_count'] for c in codes)
        total_conversations = sum(c['conversation_count'] for c in codes)
        
        # calculate limited vs unlimited codes
        limited_codes = sum(1 for c in codes if c['max_conversations'] is not None)
        unlimited_codes = total_codes - limited_codes
        
        # calculate exhausted codes
        exhausted_codes = sum(
            1 for c in codes
            if c['max_conversations'] is not None
            and c['conversation_count'] >= c['max_conversations']
        )
        
        # recently used codes
        recent_used = sorted(
            [c for c in codes if c['last_used']],
            key=lambda x: x['last_used'],
            reverse=True
        )[:10]
        
        stats = {
            "total_codes": total_codes,
            "enabled_codes": enabled_codes,
            "disabled_codes": disabled_codes,
            "limited_codes": limited_codes,
            "unlimited_codes": unlimited_codes,
            "exhausted_codes": exhausted_codes,
            "total_usage": total_usage,
            "total_conversations": total_conversations,
            "recent_used": recent_used
        }
        
        logger.info("Admin retrieved access codes statistics")
        return stats
    except Exception as e:
        logger.error(f"Error getting access codes stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/access-codes/export/json")
async def export_access_codes(
    req: Request = None,
    _: None = Depends(verify_admin)
):
    """export all access codes as JSON (admin only)"""
    try:
        codes = auth_service.list_access_codes()
        logger.info(f"Admin exported {len(codes)} access codes")
        return JSONResponse(
            content={"codes": codes, "exported_at": datetime.now().isoformat()},
            headers={
                "Content-Disposition": f"attachment; filename=access_codes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            }
        )
    except Exception as e:
        logger.error(f"Error exporting access codes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/access-codes")
async def list_access_codes(
    search: Optional[str] = None,
    enabled: Optional[bool] = None,
    skip: int = 0,
    limit: int = 50,
    req: Request = None,
    _: None = Depends(verify_admin)
):
    """list access codes with optional filtering and pagination (admin only)"""
    try:
        codes = auth_service.list_access_codes()
        
        # filtering
        if search:
            search_lower = search.lower()
            codes = [
                c for c in codes
                if search_lower in c['user_id'].lower()
                or search_lower in c['description'].lower()
                or search_lower in c['access_code'].lower()
            ]
        
        if enabled is not None:
            codes = [c for c in codes if c['enabled'] == enabled]
        
        total = len(codes)
        
        # pagination
        codes = codes[skip:skip + limit]
        
        logger.info(f"Admin listed {len(codes)} access codes (total: {total})")
        return {
            "codes": codes,
            "total": total,
            "skip": skip,
            "limit": limit
        }
    except Exception as e:
        logger.error(f"Error listing access codes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/access-codes/{access_code}")
async def get_access_code(
    access_code: str,
    req: Request = None,
    _: None = Depends(verify_admin)
):
    """get details of a specific access code (admin only)"""
    try:
        code_info = auth_service.get_access_code_info(access_code)
        if not code_info:
            raise HTTPException(status_code=404, detail="Access code not found")
        return code_info
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting access code: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/admin/access-codes/{access_code}")
async def update_access_code(
    access_code: str,
    request: UpdateAccessCodeRequest,
    req: Request = None,
    _: None = Depends(verify_admin)
):
    """update an existing access code (admin only)"""
    try:
        code_info = auth_service.get_access_code_info(access_code)
        if not code_info:
            raise HTTPException(status_code=404, detail="Access code not found")
        
        # update fields
        if request.enabled is not None:
            auth_service.enable_access_code(access_code, request.enabled)
        
        if request.max_conversations is not None:
            auth_service.set_conversation_limit(access_code, request.max_conversations)
        
        if request.description is not None:
            # update description
            if access_code in auth_service.config.access_codes:
                auth_service.config.access_codes[access_code].description = request.description
                auth_service._save_config()
        
        logger.info(f"Admin updated access code: {access_code[:8]}...")
        return {"message": "Access code updated", "access_code": access_code}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating access code: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/admin/access-codes/{access_code}")
async def delete_access_code(
    access_code: str,
    req: Request = None,
    _: None = Depends(verify_admin)
):
    """delete an access code (admin only)"""
    try:
        if auth_service.remove_access_code(access_code):
            logger.info(f"Admin deleted access code: {access_code[:8]}...")
            return {"message": "Access code deleted"}
        raise HTTPException(status_code=404, detail="Access code not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting access code: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/access-codes/batch-delete")
async def batch_delete_access_codes(
    request: BatchDeleteRequest,
    req: Request = None,
    _: None = Depends(verify_admin)
):
    """delete multiple access codes in batch (admin only)"""
    try:
        deleted_count = 0
        errors = []
        
        for code in request.access_codes:
            try:
                if auth_service.remove_access_code(code):
                    deleted_count += 1
                else:
                    errors.append({"code": code, "error": "Not found"})
            except Exception as e:
                errors.append({"code": code, "error": str(e)})
        
        logger.info(f"Admin batch deleted {deleted_count} access codes")
        return {
            "deleted_count": deleted_count,
            "errors": errors
        }
    except Exception as e:
        logger.error(f"Error batch deleting access codes: {e}")
        raise HTTPException(status_code=500, detail=str(e))




@app.post("/admin/access-codes/import/json")
async def import_access_codes(
    request: ImportCodesRequest,
    req: Request = None,
    _: None = Depends(verify_admin)
):
    """from JSON import access codes (admin only)"""
    try:
        imported_count = 0
        skipped_count = 0
        errors = []
        
        for code_data in request.codes:
            try:
                access_code = code_data.get('access_code')
                user_id = code_data.get('user_id')
                
                if not access_code or not user_id:
                    errors.append({"code": access_code, "error": "Missing required fields"})
                    continue
                
                if access_code in auth_service.config.access_codes:
                    if not request.overwrite:
                        skipped_count += 1
                        continue
                    else:
                        auth_service.remove_access_code(access_code)
                
                auth_service.add_access_code(
                    user_id=user_id,
                    description=code_data.get('description', ''),
                    access_code=access_code,
                    max_conversations=code_data.get('max_conversations')
                )
                
                if access_code in auth_service.config.access_codes:
                    code_info = auth_service.config.access_codes[access_code]
                    code_info.enabled = code_data.get('enabled', True)
                    code_info.usage_count = code_data.get('usage_count', 0)
                    code_info.conversation_count = code_data.get('conversation_count', 0)
                    if code_data.get('last_used'):
                        try:
                            code_info.last_used = datetime.fromisoformat(code_data['last_used'].replace('Z', '+00:00'))
                        except:
                            pass
                    auth_service._save_config()
                
                imported_count += 1
            except Exception as e:
                errors.append({"code": code_data.get('access_code'), "error": str(e)})
        
        logger.info(f"Admin imported {imported_count} access codes (skipped: {skipped_count})")
        return {
            "imported_count": imported_count,
            "skipped_count": skipped_count,
            "errors": errors
        }
    except Exception as e:
        logger.error(f"Error importing access codes: {e}")
        raise HTTPException(status_code=500, detail=str(e))




@app.put("/admin/access-codes/{access_code}/conversation-limit")
async def set_conversation_limit(
    access_code: str,
    max_conversations: Optional[int] = None,
    req: Request = None,
    _: None = Depends(verify_admin)
):
    """set the conversation limit for an access code (admin)
    
    Args:
        access_code: access code
        max_conversations: maximum number of conversations (None for unlimited)
    """
    try:
        if auth_service.set_conversation_limit(access_code, max_conversations):
            logger.info(f"Admin set conversation limit for {access_code[:8]}... to {max_conversations or 'unlimited'}")
            return {
                "message": f"Conversation limit set to {max_conversations or 'unlimited'}",
                "access_code": access_code,
                "max_conversations": max_conversations
            }
        raise HTTPException(status_code=404, detail="Access code not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting conversation limit: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/access-codes/{access_code}/reset-conversation-count")
async def reset_conversation_count(
    access_code: str,
    req: Request = None,
    _: None = Depends(verify_admin)
):
    """reset the conversation count for an access code (admin)
    
    Args:
        access_code: access code
    """
    try:
        if auth_service.reset_conversation_count(access_code):
            logger.info(f"Admin reset conversation count for {access_code[:8]}...")
            code_info = auth_service.get_access_code_info(access_code)
            return {
                "message": "Conversation count reset successfully",
                "access_code": access_code,
                "conversation_count": code_info['conversation_count'] if code_info else 0
            }
        raise HTTPException(status_code=404, detail="Access code not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resetting conversation count: {e}")
        raise HTTPException(status_code=500, detail=str(e))



@app.get("/access-code/status")
async def get_access_code_status(req: Request):
    """
    get access code status info (user)
    returns the status of the current access code, including:
    - enabled: whether the code is enabled
    - usage_count: total usage count
    - conversation_count: number of conversations used
    - max_conversations: maximum conversation limit (None means unlimited)
    - remaining_conversations: remaining conversations (None means unlimited)
    - last_used: last used timestamp
    - created_at: creation timestamp
    """
    try:
        # get access_code from request state (injected by auth middleware)
        access_code = getattr(req.state, 'access_code', None)
        
        if not access_code:
            raise HTTPException(status_code=401, detail="Access code not provided")
        
        code_info = auth_service.get_access_code_info(access_code)
        
        if not code_info:
            raise HTTPException(status_code=404, detail="Access code not found")
        
        remaining_conversations = None
        if code_info['max_conversations'] is not None:
            remaining_conversations = code_info['max_conversations'] - code_info['conversation_count']
            remaining_conversations = max(0, remaining_conversations)
        
        return {
            "enabled": code_info['enabled'],
            "usage_count": code_info['usage_count'],
            "conversation_count": code_info['conversation_count'],
            "max_conversations": code_info['max_conversations'],
            "remaining_conversations": remaining_conversations,
            "last_used": code_info['last_used'],
            "created_at": code_info['created_at']
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting access code status: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    

    


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
