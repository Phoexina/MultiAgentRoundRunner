"""
WebSocket Server - Handle connections and message routing

Reference: clowder-ai SocketManager pattern
"""

import asyncio
import http
import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Optional, Set, Callable, Awaitable
import os

PAGE_SIZE = 20

from websockets.legacy.server import serve, WebSocketServerProtocol
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed

from .db import Database
from .round_controller import RoundController, RoundState, DEFAULT_PROMPT_TEMPLATE
from .process_manager import ProcessManager
from . import session_reader

log = logging.getLogger(__name__)

HOST = '127.0.0.1'
PORT = 8765


class ChatServer:
    """
    WebSocket server for real-time chat and control.
    Handles connection management, message routing, and broadcasting.
    """

    def __init__(
        self,
        db: Database,
        round_controller: RoundController
    ):
        self.db = db
        self.round_controller = round_controller

        # Connected clients
        self.clients: Set[WebSocketServerProtocol] = set()

        # Server reference
        self._server = None

        # Frontend path
        self.frontend_path = Path(__file__).parent.parent / 'frontend'

    async def start(self) -> None:
        """Start the WebSocket server (runs until cancelled)."""
        await self.run(asyncio.Event())  # legacy: never-stopping event

    async def run(self, stop_event: asyncio.Event) -> None:
        """Start the WebSocket server and run until stop_event is set."""
        log.info(f"Starting WebSocket server on {HOST}:{PORT}")

        async with serve(
            self._handle_connection,
            HOST,
            PORT,
            process_request=self._handle_http_request
        ) as server:
            self._server = server
            log.info(f"Server started at ws://{HOST}:{PORT}")
            log.info(f"Web UI available at http://{HOST}:{PORT}/")

            # Wait until stop is requested (Ctrl+C triggers CancelledError which exits the block)
            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                pass  # KeyboardInterrupt on Windows cancels the task — exit cleanly

        log.info("WebSocket server closed.")

    async def _handle_http_request(self, path: str, request_headers: Headers) -> Optional[tuple]:
        """Handle HTTP requests for serving frontend files and API.

        process_request hook: return (status, headers, body) to serve HTTP,
        or None to proceed with WebSocket upgrade.
        """
        # Strip query string
        if '?' in path:
            path = path[:path.index('?')]

        # API endpoints
        if path.startswith('/api/sessions'):
            return await self._handle_api_sessions(path)
        elif path.startswith('/api/conversations/'):
            return await self._handle_api_conversation_detail(path)

        # Static files
        if path in ('/', '/index.html'):
            return await self._serve_file('index.html', 'text/html')
        elif path in ('/logs', '/logs.html'):
            return await self._serve_file('logs.html', 'text/html')
        elif path.startswith('/static/'):
            filename = path[8:]
            if filename.endswith('.css'):
                return await self._serve_file(f'static/{filename}', 'text/css')
            elif filename.endswith('.js'):
                return await self._serve_file(f'static/{filename}', 'application/javascript')

        # All other paths (including /ws) — let WebSocket upgrade proceed
        return None

    async def _handle_api_sessions(self, path: str) -> tuple:
        """Handle /api/sessions endpoints"""
        if path == '/api/sessions':
            # Without a conv_id filter, return empty (requires conversation context)
            return self._json_response({'sessions': []})

        if path.startswith('/api/sessions/'):
            parts = path.split('/')
            # /api/sessions/{id}/messages
            if len(parts) == 5 and parts[4] == 'messages':
                try:
                    session_id = int(parts[3])
                    session = await self.db.get_session(session_id)
                    if session:

                        # session_id (CLI UUID) not yet written — session just started
                        if not session.get('session_id'):
                            return self._json_response({
                                'session': session,
                                'source': 'db_only',
                                'messages': [],
                                'path': None,
                            })

                        result = session_reader.get_session_messages(
                            session['agent_type'],
                            session['session_id'],
                        )
                        return self._json_response({'session': session, **result})
                except (ValueError, IndexError):
                    pass
                return (404, [], b'Not Found')

            # /api/sessions/{id}
            try:
                session_id = int(parts[3])
                session = await self.db.get_session(session_id)
                if session:
                    return self._json_response({'session': session})
            except (ValueError, IndexError):
                pass

        return (404, [], b'Not Found')

    async def _handle_api_conversation_detail(self, path: str) -> tuple:
        """Handle /api/conversations/{id}/pipeline and /api/conversations/{id}/history"""
        parts = path.split('/')
        # /api/conversations/{id}[/pipeline|/history]
        try:
            conv_id = int(parts[3])
        except (IndexError, ValueError):
            return (404, [], b'Not Found')

        sub = parts[4] if len(parts) > 4 else ''

        if sub == 'pipeline':
            steps = await self.db.get_pipeline_steps(conv_id)
            return self._json_response({'steps': steps})
        elif sub == 'history':
            history = await self.db.get_chat_history(conv_id)
            return self._json_response({'messages': history})
        elif sub == 'sessions':
            sessions = await self.db.get_sessions(conv_id)
            return self._json_response({'sessions': sessions})
        else:
            conv = await self.db.get_conversation(conv_id)
            if conv:
                return self._json_response(conv)

        return (404, [], b'Not Found')

    def _json_response(self, data: dict) -> tuple:
        """Create a JSON response"""
        headers = [
            ('Content-Type', 'application/json; charset=utf-8'),
            ('Access-Control-Allow-Origin', '*'),
        ]
        return (200, headers, json.dumps(data, ensure_ascii=False).encode('utf-8'))

    async def _serve_file(self, relative_path: str, content_type: str) -> tuple:
        """Serve a static file"""
        file_path = self.frontend_path / relative_path

        if not file_path.exists():
            return (404, [], b'Not Found')

        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        headers = [
            ('Content-Type', f'{content_type}; charset=utf-8'),
            ('Cache-Control', 'no-cache')
        ]

        return (200, headers, content.encode('utf-8'))

    async def _handle_connection(self, websocket: WebSocketServerProtocol) -> None:
        """Handle a WebSocket connection"""
        client_addr = websocket.remote_address
        log.info(f"Client connected: {client_addr}")

        # Add to clients set
        self.clients.add(websocket)

        try:
            # Send history on connect
            await self._send_history(websocket)

            # Send current state
            await self._send_current_state(websocket)

            # Message loop
            async for raw_message in websocket:
                await self._handle_message(websocket, raw_message)

        except ConnectionClosed:
            log.info(f"Client disconnected: {client_addr}")
        except Exception as e:
            log.exception(f"Error handling connection: {e}")
        finally:
            self.clients.discard(websocket)

    async def _send_history(self, websocket: WebSocketServerProtocol) -> None:
        """Send conversations list and default prompts. Let client decide which to select."""
        # Ensure at least one conversation exists
        conversations = await self.db.get_all_conversations()
        if not conversations:
            await self.db.create_conversation('默认会话')
            conversations = await self.db.get_all_conversations()

        await websocket.send(json.dumps({
            'type': 'conversations_list',
            'conversations': conversations,
            'default_prompt_template': DEFAULT_PROMPT_TEMPLATE,
        }, ensure_ascii=False))

    async def _send_current_state(self, websocket: WebSocketServerProtocol) -> None:
        """Send current state to a client"""
        state = self.round_controller.get_state()
        round_data = self.round_controller.get_current_round()

        payload = {
            'type': 'state',
            'state': state.value,
            'round_number': round_data.round_index,
        }
        # Add step progress info
        if round_data.pipeline_steps:
            payload['step_index'] = round_data.step_index
            payload['total_steps'] = len(round_data.pipeline_steps)
            idx = round_data.step_index - 1   # step_index is 1-based
            if 0 <= idx < len(round_data.pipeline_steps):
                current_step = round_data.pipeline_steps[idx]
                payload['agent'] = current_step.get('agent_type', 'agent')

        await websocket.send(json.dumps(payload, ensure_ascii=False))

    async def _handle_message(self, websocket: WebSocketServerProtocol, raw_message: str) -> None:
        """Handle an incoming message"""
        try:
            data = json.loads(raw_message)
        except json.JSONDecodeError:
            await websocket.send(json.dumps({
                'type': 'error',
                'content': 'Invalid JSON'
            }))
            return

        msg_type = data.get('type')
        log.info(f"TEST: handle_message {msg_type}")

        if msg_type == 'user_message':
            await self._handle_user_message(data)

        elif msg_type == 'control':
            await self._handle_control(data)

        elif msg_type == 'get_sessions':
            conv_id = data.get('conversation_id') if isinstance(data, dict) else None
            page    = data.get('page', 0) if isinstance(data, dict) else 0
            await self._handle_get_sessions(websocket, conversation_id=conv_id, page=page)

        elif msg_type == 'create_conversation':
            await self._handle_create_conversation(data.get('name', '新会话'))

        elif msg_type == 'rename_conversation':
            await self._handle_rename_conversation(data.get('id'), data.get('name'))

        elif msg_type == 'delete_conversation':
            await self._handle_delete_conversation(data.get('id'))

        elif msg_type == 'select_conversation':
            page = data.get('page', 0) if isinstance(data, dict) else 0
            await self._handle_select_conversation(websocket, data.get('id'), page=page)

        elif msg_type == 'save_pipeline':
            await self._handle_save_pipeline(
                data.get('conversation_id'),
                data.get('steps', []),
                data.get('working_dir'),
                data.get('max_rounds'),
                data.get('api_err_retries'),
            )

        elif msg_type == 'delete_session':
            await self._handle_delete_session(data.get('id'))

        elif msg_type == 'resume_session':
            await self._handle_resume_session(data.get('session_id'), data.get('user_input') or data.get('prompt'))

        else:
            log.warning(f"Unknown message type: {msg_type}")

    async def _handle_user_message(self, data: dict) -> None:
        """Handle a user chat message"""
        content = data.get('content', '')
        if not content or not content.strip():
            return

        content = content.strip()

        # conversation_id priority: message > round_data > None
        # When user sends message from UI, it includes conversation_id.
        # During agent intervention, round_data.conversation_id is authoritative.
        round_data = self.round_controller.get_current_round()
        conversation_id = data.get('conversation_id') or round_data.conversation_id

        # Save user message to DB (agent_type='user')
        if conversation_id:
            await self.db.add_user_message(
                conversation_id,
                round_data.round_index,
                round_data.step_index,
                content,
            )

        # Broadcast to all clients
        await self.broadcast({
            'type': 'chat_message',
            'role': 'user',
            'content': content,
            'conversation_id': conversation_id,
            'timestamp': datetime.now().isoformat()
        })

        # Forward to round controller (accepted any time while running)
        if self.round_controller.is_running():
            await self.round_controller.receive_user_message(content)

    async def _handle_control(self, data: dict) -> None:
        """Handle a control command"""
        action = data.get('action') if isinstance(data, dict) else data
        conversation_id = data.get('conversation_id') if isinstance(data, dict) else None

        if action == 'start':
            if not self.round_controller.is_running():
                await self.round_controller.start(conversation_id=conversation_id)
            else:
                await self.broadcast({'type': 'system', 'content': '已经在运行中'})

        elif action == 'stop':
            await self.round_controller.stop()

        elif action == 'stop_after_current':
            await self.round_controller.stop_after_current()

        elif action == 'continue':
            await self.round_controller.continue_next(conversation_id=conversation_id)

        elif action == 'start_round':
            await self.round_controller.start_new_round(conversation_id=conversation_id)

    async def _handle_get_sessions(
        self, websocket: WebSocketServerProtocol,
        conversation_id: Optional[int] = None,
        page: int = 0,
    ) -> None:
        """Return one page of sessions + the corresponding chat history range."""
        if conversation_id:
            sessions = await self.db.get_sessions(conversation_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
            total = await self.db.get_session_count(conversation_id)
            total_pages = max(1, math.ceil(total / PAGE_SIZE))
            if sessions:
                ids = [s['id'] for s in sessions]
                min_id = min(ids)
                max_id = 2 ** 31 if page == 0 else max(ids)
                history = await self.db.get_chat_history_range(conversation_id, min_id, max_id)
            else:
                history = await self.db.get_chat_history(conversation_id)
        else:
            sessions, total_pages, history = [], 1, []

        await websocket.send(json.dumps({
            'type': 'sessions_list',
            'sessions': sessions,
            'history': history,
            'page': page,
            'total_pages': total_pages,
        }, ensure_ascii=False))

    async def _handle_create_conversation(self, name: str) -> None:
        """Create a new conversation"""
        if not name or not name.strip():
            name = '新会话'
        conv_id = await self.db.create_conversation(name.strip())
        conv = await self.db.get_conversation(conv_id)
        await self.broadcast({
            'type': 'conversation_created',
            'conversation': conv
        })

    async def _handle_rename_conversation(self, conv_id, name: str) -> None:
        """Rename a conversation"""
        if not conv_id or not name:
            return
        await self.db.rename_conversation(int(conv_id), name.strip())
        await self.broadcast({
            'type': 'conversation_renamed',
            'id': conv_id,
            'name': name.strip()
        })

    async def _handle_delete_conversation(self, conv_id) -> None:
        """Delete a conversation"""
        if not conv_id:
            return
        await self.db.delete_conversation(int(conv_id))
        await self.broadcast({
            'type': 'conversation_deleted',
            'id': conv_id
        })

    async def _handle_select_conversation(
        self, websocket: WebSocketServerProtocol, conv_id, page: int = 0,
    ) -> None:
        """Send conversation history + pipeline + working_dir + current progress to the requesting client."""
        if not conv_id:
            return
        conv_id = int(conv_id)
        conv    = await self.db.get_conversation(conv_id)
        steps   = await self.db.get_pipeline_steps(conv_id)
        sessions = await self.db.get_sessions(conv_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
        total    = await self.db.get_session_count(conv_id)
        total_pages = max(1, math.ceil(total / PAGE_SIZE))

        if sessions:
            ids = [s['id'] for s in sessions]
            min_id = min(ids)
            max_id = 2 ** 31 if page == 0 else max(ids)
            history = await self.db.get_chat_history_range(conv_id, min_id, max_id)
        else:
            history = await self.db.get_chat_history(conv_id)

        round_index = 0
        step_index  = 0
        total_steps = len(steps) if steps else 2
        latest = await self.db.get_latest_session(conv_id)
        if latest:
            round_index = latest['round_index']
            step_index  = latest['step_index']

        payload = {
            'type': 'conversation_selected',
            'id': conv_id,
            'name':            conv.get('name')            if conv else None,
            'working_dir':     conv.get('working_dir')     if conv else None,
            'max_rounds':      conv.get('max_rounds')      if conv else 0,
            'api_err_retries': conv.get('api_err_retries') if conv else 3,
            'history':     history,
            'pipeline':    steps,
            'sessions':    sessions,
            'page':        page,
            'total_pages': total_pages,
            'default_prompt_template': DEFAULT_PROMPT_TEMPLATE,
            'round_number': round_index,
            'step_index':   step_index,
            'total_steps':  total_steps,
        }

        await websocket.send(json.dumps(payload, ensure_ascii=False))

    async def _handle_save_pipeline(self, conv_id, steps: list, working_dir: Optional[str] = None, max_rounds: Optional[int] = None, api_err_retries: Optional[int] = None) -> None:
        """Save pipeline steps and working_dir for a conversation"""
        if not conv_id:
            return
        await self.db.save_pipeline_steps(int(conv_id), steps, working_dir=working_dir, max_rounds=max_rounds, api_err_retries=api_err_retries)
        await self.broadcast({
            'type': 'pipeline_updated',
            'conversation_id': conv_id,
            'steps': steps,
            'working_dir': working_dir,
            'max_rounds': max_rounds,
            'api_err_retries': api_err_retries,
        })

    async def _handle_delete_session(self, session_id) -> None:
        """Delete a session and its associated data"""
        if not session_id:
            return
        conv_id = await self.db.delete_session(int(session_id))
        if conv_id:
            await self.broadcast({
                'type': 'session_deleted',
                'session_id': session_id,
                'conversation_id': conv_id
            })

    async def _handle_resume_session(self, session_id, user_input: Optional[str] = None) -> None:
        """Resume a specific session with optional new user input"""
        if not session_id:
            return
        session_id = int(session_id)
        user_input = (user_input or '').strip() or None

        # Get the session to check its status
        session = await self.db.get_session(session_id)
        if not session:
            await self.broadcast({
                'type': 'system',
                'content': f'Session {session_id} 不存在'
            })
            return

        if not await self.round_controller.resume_session(session_id, user_input=user_input):
            await self.broadcast({
                'type': 'system',
                'content': f'无法继续 Session {session_id}'
            })

    async def broadcast(self, message: dict) -> None:
        """Broadcast a message to all connected clients"""
        if not self.clients:
            return

        raw = json.dumps(message, ensure_ascii=False)

        # Send to all clients
        await asyncio.gather(
            *[client.send(raw) for client in self.clients],
            return_exceptions=True
        )

    
    async def stop(self) -> None:
        """Stop the server"""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
