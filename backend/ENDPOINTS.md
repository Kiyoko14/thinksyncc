# ThinkSync API Endpoints

**Base URL:** `http://localhost:8000`  
**API Prefix:** `/api/v1` (except Health)  
**Status:** ✅ All endpoints operational

---

## 🏥 Health Check

### GET /health
Health check endpoint (no authentication required)
```bash
curl http://localhost:8000/health
```

**Response:**
```json
{
  "status": "ok",
  "service": "ThinkSync API",
  "timestamp": "2026-03-22T12:05:45.123456+00:00"
}
```

---

## 🔐 Authentication

### POST /api/v1/auth/login
Login with email and password
```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"password123"}'
```

**Request Body:**
```json
{
  "email": "user@example.com",
  "password": "password123"
}
```

**Response (200 OK):**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer"
}
```

### GET /api/v1/auth/me
Get current authenticated user info (requires Bearer token)
```bash
curl -H "Authorization: Bearer {token}" http://localhost:8000/api/v1/auth/me
```

**Response (200 OK):**
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "email": "user@example.com"
}
```

### POST /api/v1/auth/logout
Logout current user (requires Bearer token)
```bash
curl -X POST -H "Authorization: Bearer {token}" http://localhost:8000/api/v1/auth/logout
```

**Response (200 OK):**
```json
{
  "message": "Logged out successfully"
}
```

---

## 🖥️ Servers

### GET /api/v1/servers
List all servers for current user (requires auth)
```bash
curl -H "Authorization: Bearer {token}" http://localhost:8000/api/v1/servers
```

**Response (200 OK):**
```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "user_id": "550e8400-e29b-41d4-a716-446655440001",
    "name": "Production Server",
    "host": "server.example.com",
    "port": 22,
    "username": "ubuntu",
    "created_at": "2026-03-22T12:00:00Z"
  }
]
```

---

## 💼 Workspaces

### GET /api/v1/workspaces
List all workspaces (requires auth)
```bash
curl -H "Authorization: Bearer {token}" http://localhost:8000/api/v1/workspaces
```

**Response (200 OK):**
```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "user_id": "550e8400-e29b-41d4-a716-446655440001",
    "server_id": "550e8400-e29b-41d4-a716-446655440002",
    "name": "project-alpha",
    "path": "/home/ubuntu/workspaces/project-alpha",
    "slug": "project-alpha-x9k2",
    "domain": "https://project-alpha-x9k2.app.yoursite.com",
    "created_at": "2026-03-22T12:00:00Z"
  }
]
```

### POST /api/v1/workspaces
Create a new workspace (requires auth)
```bash
curl -X POST -H "Authorization: Bearer {token}" \
  -H "Content-Type: application/json" \
  -d '{"server_id":"550e...","name":"my-project"}' \
  http://localhost:8000/api/v1/workspaces
```

**Request Body:**
```json
{
  "server_id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "my-project"
}
```

**Response (201 Created):**
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440003",
  "user_id": "550e8400-e29b-41d4-a716-446655440001",
  "server_id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "my-project",
  "path": "/home/ubuntu/workspaces/my-project",
  "slug": "my-project-7f3m",
  "domain": "https://my-project-7f3m.app.yoursite.com",
  "created_at": "2026-03-22T12:10:00Z"
}
```

---

## 💬 Chat

### GET /api/v1/chat/{workspace_id}
Get or create chat for workspace
```bash
curl -H "Authorization: Bearer {token}" \
  http://localhost:8000/api/v1/chat/550e8400-e29b-41d4-a716-446655440003
```

**Response (200 OK):**
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440004",
  "workspace_id": "550e8400-e29b-41d4-a716-446655440003",
  "user_id": "550e8400-e29b-41d4-a716-446655440001",
  "created_at": "2026-03-22T12:10:00Z",
  "messages": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440005",
      "role": "user",
      "content": "ls -la",
      "created_at": "2026-03-22T12:10:30Z"
    },
    {
      "id": "550e8400-e29b-41d4-a716-446655440006",
      "role": "assistant",
      "content": "total 48\ndrwxr-xr-x 5 ubuntu ubuntu 4096 Mar 22 12:10 .",
      "created_at": "2026-03-22T12:10:35Z"
    }
  ]
}
```

### POST /api/v1/chat/{workspace_id}/message
Send message to workspace chat
```bash
curl -X POST -H "Authorization: Bearer {token}" \
  -H "Content-Type: application/json" \
  -d '{"message":"ls -la"}' \
  http://localhost:8000/api/v1/chat/550e8400-e29b-41d4-a716-446655440003/message
```

**Request Body:**
```json
{
  "message": "ls -la"
}
```

**Response (200 OK):**
```json
{
  "chat_id": "550e8400-e29b-41d4-a716-446655440004",
  "workspace_id": "550e8400-e29b-41d4-a716-446655440003",
  "response": "total 48\ndrwxr-xr-x  5 ubuntu ubuntu 4096 Mar 22 12:10 ."
}
```

### GET /api/v1/chat/workspace/{workspace_id}
Get chat for specific workspace (v2 endpoint)
```bash
curl -H "Authorization: Bearer {token}" \
  http://localhost:8000/api/v1/chat/workspace/550e8400-e29b-41d4-a716-446655440003
```

**Response:** Same format as `GET /api/v1/chat/{workspace_id}`

### GET /api/v1/chat/repo/{git_repo_id}
Get chat for specific git repository
```bash
curl -H "Authorization: Bearer {token}" \
  http://localhost:8000/api/v1/chat/repo/550e8400-e29b-41d4-a716-446655440007
```

**Response:** Similar to workspace chat

### POST /api/v1/chat/message
Send message with dual-context support (workspace OR git repo)
```bash
curl -X POST -H "Authorization: Bearer {token}" \
  -H "Content-Type: application/json" \
  -d '{
    "workspace_id": "550e8400-e29b-41d4-a716-446655440003",
    "message": "pwd"
  }' \
  http://localhost:8000/api/v1/chat/message
```

**Request Body (Workspace Context):**
```json
{
  "workspace_id": "550e8400-e29b-41d4-a716-446655440003",
  "message": "pwd"
}
```

**Request Body (Git Repo Context):**
```json
{
  "git_repo_id": "550e8400-e29b-41d4-a716-446655440007",
  "message": "git status"
}
```

**Response (200 OK):**
```json
{
  "chat_id": "550e8400-e29b-41d4-a716-446655440004",
  "workspace_id": "550e8400-e29b-41d4-a716-446655440003",
  "response": "/home/ubuntu/workspaces/my-project"
}
```

---

## 🚀 Deployments

### POST /api/v1/deployments/{workspace_id}
Create deployment for workspace
```bash
curl -X POST -H "Authorization: Bearer {token}" \
  http://localhost:8000/api/v1/deployments/550e8400-e29b-41d4-a716-446655440003
```

**Response (201 Created):**
```json
{
  "workspace_id": "550e8400-e29b-41d4-a716-446655440003",
  "port": 10001,
  "domain": "https://my-project-7f3m.app.yoursite.com",
  "slug": "my-project-7f3m",
  "is_active": true
}
```

### GET /api/v1/deployments/{workspace_id}
Get or auto-create deployment
```bash
curl -H "Authorization: Bearer {token}" \
  http://localhost:8000/api/v1/deployments/550e8400-e29b-41d4-a716-446655440003
```

**Response (200 OK):** Same format as POST response

### DELETE /api/v1/deployments/{workspace_id}
Deactivate deployment
```bash
curl -X DELETE -H "Authorization: Bearer {token}" \
  http://localhost:8000/api/v1/deployments/550e8400-e29b-41d4-a716-446655440003
```

**Response (200 OK):**
```json
{
  "message": "Deployment deactivated",
  "workspace_id": "550e8400-e29b-41d4-a716-446655440003"
}
```

---

## 🔧 Commands

### POST /api/v1/commands/execute
Execute SSH command on server (requires auth)
```bash
curl -X POST -H "Authorization: Bearer {token}" \
  -H "Content-Type: application/json" \
  -d '{
    "server_id": "550e8400-e29b-41d4-a716-446655440000",
    "command": "ls -la"
  }' \
  http://localhost:8000/api/v1/commands/execute
```

**Request Body:**
```json
{
  "server_id": "550e8400-e29b-41d4-a716-446655440000",
  "command": "ls -la"
}
```

**Response (200 OK):**
```json
{
  "stdout": "total 48\ndrwxr-xr-x  5 ubuntu ubuntu 4096 Mar 22 12:10",
  "stderr": "",
  "exit_code": 0,
  "execution_time_ms": 245
}
```

---

## Error Responses

### 400 Bad Request
```json
{
  "detail": "Invalid request data"
}
```

### 401 Unauthorized
```json
{
  "detail": "Invalid credentials"
}
```

### 403 Forbidden
```json
{
  "detail": "Access denied"
}
```

### 404 Not Found
```json
{
  "detail": "Not Found"
}
```

### 409 Conflict
```json
{
  "detail": "Slug or domain collision"
}
```

### 500 Internal Server Error
```json
{
  "detail": "Database operation failed"
}
```

---

## Authentication

All endpoints except `/health` require a Bearer token in the `Authorization` header:

```bash
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

To get a token:
1. Use `POST /api/v1/auth/login` with email and password
2. Extract the `access_token` from the response
3. Include it in subsequent requests

---

## Features Summary

✅ **Health Check** — Service status verification  
✅ **Authentication** — JWT-based user login/logout  
✅ **Workspace Management** — Create, list, and manage workspaces with unique slugs  
✅ **Chat System** — Dual-context support (workspace + git repo)  
✅ **AI Integration** — Process commands and messages with path-safe execution  
✅ **Deployment Tracking** — Auto-allocate ports and map to domains  
✅ **SSH Commands** — Execute commands on remote servers  
✅ **Security** — Path validation, SQL injection prevention, RLS policies  

---

**Last Updated:** March 22, 2026  
**Backend Version:** 1.0.0  
**API Version:** v1
