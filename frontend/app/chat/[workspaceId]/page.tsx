'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import { useParams, useRouter } from 'next/navigation';
import {
  ApiError,
  getForgeV2JobStatus,
  getJobWebSocketUrl,
  getWorkspace,
  getWorkspaceChat,
  getWorkspaceJobs,
  runForgeV2,
  type AgentJobStatus,
  type ForgeV2JobResponse,
  type JobRecord,
  type JobStreamEvent,
  type StepResult,
  type StoredChatMessage,
  type ChatRole,
  type Workspace,
} from '@/services/api';
import { getToken, logout } from '@/services/auth';
import { ArrowLeft, Send, Bot, User, Loader2, AlertTriangle, Terminal, Clock, CheckCircle, XCircle, AlertCircle, Play, Pause } from 'lucide-react';

// ===== UTILITY & TYPE DEFINITIONS =====

const POLL_INTERVAL_MS = 2500;

interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  createdAt: string;
  status?: AgentJobStatus;
  steps?: StepResult[];
  isError?: boolean;
}

function createId(): string {
  return `${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
}

function formatToolName(tool: string): string {
  return tool
    .replace(/_/g, ' ')
    .split(' ')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

// ===== MAIN PAGE COMPONENT =====

export default function ChatPage() {
  const router = useRouter();
  const params = useParams();
  const workspaceId = params.workspaceId as string;

  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(true);
  const [isRunning, setIsRunning] = useState(false);
  const [jobStartTime, setJobStartTime] = useState<Date | null>(null);
  const [elapsedTime, setElapsedTime] = useState(0);
  const [error, setError] = useState<string | null>(null);
  
  const socketRef = useRef<WebSocket | null>(null);
  const timeoutRef = useRef<NodeJS.Timeout>();
  const bottomRef = useRef<HTMLDivElement>(null);

  // ===== DATA FETCHING & INITIALIZATION =====

  const loadChat = useCallback(async (id: string) => {
    if (!getToken()) {
      router.replace('/login');
      return;
    }
    setIsLoading(true);
    setError(null);
    try {
      const [ws, chat, jobs] = await Promise.all([
        getWorkspace(id),
        getWorkspaceChat(id),
        getWorkspaceJobs(id),
      ]);
      setWorkspace(ws);
      setMessages(chat.messages.filter(m => m.role !== 'system').map(m => ({
        id: m.id,
        role: m.role as 'user' | 'assistant',
        content: m.content,
        createdAt: m.created_at,
        status: 'completed',
      })));
      const hasActiveJob = jobs.some(job => job.status === 'running' || job.status === 'queued' || job.status === 'waiting_for_llm');
      setIsRunning(hasActiveJob);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        logout();
        router.replace('/login');
      } else {
        setError('Failed to load chat history.');
      }
    } finally {
      setIsLoading(false);
    }
  }, [router]);

  useEffect(() => {
    loadChat(workspaceId);
    return () => {
      socketRef.current?.close();
      if(timeoutRef.current) clearTimeout(timeoutRef.current);
    };
  }, [workspaceId, loadChat]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isRunning]);

  useEffect(() => {
    let interval: NodeJS.Timeout;
    if (isRunning && jobStartTime) {
      interval = setInterval(() => {
        setElapsedTime(Math.floor((Date.now() - jobStartTime.getTime()) / 1000));
      }, 1000);
    }
    return () => clearInterval(interval);
  }, [isRunning, jobStartTime]);


  // ===== REAL-TIME JOB HANDLING =====

  const handleJobUpdate = (job: ForgeV2JobResponse, messageId: string) => {
    setMessages(prev => prev.map(msg => 
      msg.id === messageId 
        ? {
            ...msg,
            content: job.run?.summary || msg.content,
            status: job.status,
            steps: job.run?.results ? [...(msg.steps || []), ...job.run?.results.filter(s => !(msg.steps || []).some(existing => existing.step === s.step))] : msg.steps,
            isError: job.status === 'failed',
          }
        : msg
    ));

    if (job.status === 'completed' || job.status === 'failed') {
      socketRef.current?.close();
      if(timeoutRef.current) clearTimeout(timeoutRef.current);
      setIsRunning(false);
      setJobStartTime(null);
    }
  };

  const pollJobStatus = useCallback(async (jobId: string, messageId: string) => {
    try {
      const job = await getForgeV2JobStatus(jobId);
      handleJobUpdate(job, messageId);
      if (job.status !== 'completed' && job.status !== 'failed') {
        timeoutRef.current = setTimeout(() => pollJobStatus(jobId, messageId), POLL_INTERVAL_MS);
      } 
    } catch {
        setError("Connection lost. Could not update agent status.");
        setIsRunning(false);
        setJobStartTime(null);
    }
  }, []);

  const connectToJob = useCallback((jobId: string, messageId: string) => {
    socketRef.current = new WebSocket(getJobWebSocketUrl(jobId));

    socketRef.current.onmessage = (event) => {
        const data: JobStreamEvent = JSON.parse(event.data);
        if(data.type === 'completed' || data.type === 'step_result') {
             // To get the full summary, we still need to poll
             pollJobStatus(jobId, messageId);
        }
    };

    socketRef.current.onerror = () => pollJobStatus(jobId, messageId);
    socketRef.current.onclose = () => pollJobStatus(jobId, messageId);

  }, [pollJobStatus]);

  // ===== UI ACTIONS =====

  const handleSend = async () => {
    if (!input.trim() || !workspace || isRunning) return;

    if (isRunning) {
      setError("Agent is already running in this workspace.");
      return;
    }

    const userMessage: ChatMessage = {
      id: createId(),
      role: 'user',
      content: input,
      createdAt: new Date().toISOString(),
    };
    
    const assistantId = createId();
    const assistantMessage: ChatMessage = {
      id: assistantId,
      role: 'assistant',
      content: 'Agent is starting...',
      createdAt: new Date().toISOString(),
      status: 'queued',
      steps: [],
    };

    setMessages(prev => [...prev, userMessage, assistantMessage]);
    setInput('');
    setIsRunning(true);
    setJobStartTime(new Date());
    setError(null);

    try {
      const job = await runForgeV2({
        workspace_id: workspace.id,
        server_id: workspace.server_id,
        objective: input,
        max_steps: 10,
        allow_write: true,
      });
      handleJobUpdate(job, assistantId);
      connectToJob(job.job_id, assistantId);
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to start the agent.';
      setMessages(prev => prev.map(m => m.id === assistantId ? {...m, content: errorMessage, status: 'failed', isError: true } : m));
      setError(errorMessage);
      setIsRunning(false);
    }
  };

  // ===== RENDER =====

  if (isLoading) return <Spinner />;

  return (
    <div className="min-h-screen bg-white text-gray-900">
      <WorkspaceHeader workspace={workspace} router={useRouter()} />
      {isRunning && <RunningBanner elapsedTime={elapsedTime} />}
      {error && <ErrorDisplay message={error} onClose={() => setError(null)} />}
      <div className="max-w-4xl mx-auto px-4 py-6 pb-24">
        <div className="space-y-6">
          {messages.map(msg => <ChatMessageItem key={msg.id} {...msg} />)}
        </div>
        <div ref={bottomRef} />
      </div>
      <StickyInputArea 
        input={input} 
        setInput={setInput} 
        onSend={handleSend} 
        disabled={isRunning || !input.trim()}
      />
    </div>
  );
}

// ===== SUB-COMPONENTS =====

const WorkspaceSidebar = ({ workspace }: { workspace: Workspace | null }) => (
  <div className="space-y-6">
    <div>
      <h2 className="text-lg font-semibold mb-4">Workspace</h2>
      <div className="space-y-3 text-sm">
        <div>
          <span className="text-gray-400">Name:</span>
          <p className="font-medium">{workspace?.name || 'Loading...'}</p>
        </div>
        <div>
          <span className="text-gray-400">Subdomain:</span>
          <p className="font-medium">{workspace?.domain || 'N/A'}</p>
        </div>
        <div>
          <span className="text-gray-400">Path:</span>
          <p className="font-medium text-xs">{workspace?.path || 'N/A'}</p>
        </div>
        <div>
          <span className="text-gray-400">Status:</span>
          <p className="font-medium text-green-400">Active</p>
        </div>
      </div>
    </div>
  </div>
);

const RunningBanner = ({ elapsedTime }: { elapsedTime: number }) => (
  <div className="bg-green-50 border-b border-green-200 px-4 py-3">
    <div className="flex items-center gap-3">
      <Loader2 className="animate-spin text-green-600" size={16} />
      <span className="text-sm font-medium text-green-800">Agent is actively operating</span>
      <div className="flex items-center gap-1 text-xs text-green-700 ml-auto">
        <Clock size={12} />
        <span>{Math.floor(elapsedTime / 60)}:{(elapsedTime % 60).toString().padStart(2, '0')}</span>
      </div>
    </div>
  </div>
);

const InlineStepCard = (step: StepResult) => {
  const getStepIcon = () => {
    if (step.success) return <CheckCircle size={16} className="text-green-600" />;
    return <XCircle size={16} className="text-red-500" />;
  };

  const getStepTitle = () => formatToolName(step.tool);

  const getStepStatus = () => {
    if (step.success) return 'Completed';
    return `Failed (exit ${step.exit_code})`;
  };

  const formatDuration = (ms: number) => `${(ms / 1000).toFixed(1)}s`;

  const command = `${step.tool} ${Object.entries(step.args).map(([k, v]) => `--${k} ${v}`).join(' ')}`.trim();

  return (
    <details className="bg-white border border-gray-200 rounded-lg overflow-hidden">
      <summary className="flex items-center justify-between p-3 cursor-pointer hover:bg-gray-50 transition-colors">
        <div className="flex items-center gap-3">
          {getStepIcon()}
          <div>
            <h4 className="text-sm font-medium text-gray-900">{getStepTitle()}</h4>
            <p className="text-xs text-gray-500">{getStepStatus()} • {formatDuration(step.duration_ms)}</p>
          </div>
        </div>
        <div className="text-xs text-gray-400">
          Step {step.step}
        </div>
      </summary>
      <div className="px-3 pb-3 space-y-3">
        {command && (
          <div>
            <h5 className="text-xs font-semibold text-gray-700 mb-1">Command</h5>
            <pre className="bg-gray-50 rounded p-2 text-xs text-gray-800 font-mono overflow-x-auto">{command}</pre>
          </div>
        )}
        {step.stdout && (
          <div>
            <h5 className="text-xs font-semibold text-gray-700 mb-1">Output</h5>
            <pre className="bg-gray-50 rounded p-2 text-xs text-gray-800 max-h-32 overflow-y-auto whitespace-pre-wrap font-mono">{step.stdout}</pre>
          </div>
        )}
        {step.stderr && (
          <div>
            <h5 className="text-xs font-semibold text-red-700 mb-1">Errors</h5>
            <pre className="bg-red-50 rounded p-2 text-xs text-red-800 max-h-32 overflow-y-auto whitespace-pre-wrap font-mono">{step.stderr}</pre>
          </div>
        )}
      </div>
    </details>
  );
};

const WorkspaceHeader = ({ workspace, router }: { workspace: Workspace | null, router: any }) => (
  <header className="bg-white border-b border-gray-200 px-4 py-3 flex items-center justify-between shadow-sm">
    <div className="flex items-center gap-3">
      <button onClick={() => router.back()} className="p-2 rounded-lg hover:bg-gray-100 transition-colors">
        <ArrowLeft size={20} className="text-gray-600" />
      </button>
      <h1 className="text-xl font-semibold text-gray-900">{workspace?.name || 'Chat'}</h1>
    </div>
    <div className="text-sm text-gray-500">
      {workspace?.domain && <span className="px-2 py-1 bg-gray-100 rounded-md">{workspace.domain}</span>}
    </div>
  </header>
);

const ChatMessageItem = (msg: ChatMessage) => (
  <div className={`flex gap-3 ${msg.role === 'user' ? 'justify-end' : ''}`}>
    {msg.role === 'assistant' && <Bot className="w-8 h-8 flex-shrink-0 text-green-600 mt-1" />}
    <div className={`max-w-2xl ${msg.role === 'user' ? 'order-first' : ''}`}>
      <div className={`rounded-2xl px-4 py-3 shadow-sm border ${
        msg.role === 'user' 
          ? 'bg-green-600 text-white rounded-br-md' 
          : msg.isError 
            ? 'bg-red-50 border-red-200 rounded-bl-md' 
            : 'bg-gray-50 border-gray-200 rounded-bl-md'
      }`}>
        <p className="text-sm leading-relaxed whitespace-pre-wrap">{msg.content}</p>
        {msg.status && msg.status !== 'completed' && (
          <div className="flex items-center gap-2 mt-3 text-xs text-gray-500">
            <Loader2 className="animate-spin" size={14}/>
            <span>{msg.status.charAt(0).toUpperCase() + msg.status.slice(1)}...</span>
          </div>
        )}
      </div>
      {msg.steps && msg.steps.length > 0 && (
        <div className="mt-4 space-y-2">
          {msg.steps.map(step => <InlineStepCard key={step.step} {...step} />)}
        </div>
      )}
    </div>
    {msg.role === 'user' && <User className="w-8 h-8 flex-shrink-0 bg-gray-300 p-1.5 rounded-full mt-1" />}
  </div>
);

const StickyInputArea = ({ input, setInput, onSend, disabled }: any) => (
  <div className="fixed bottom-0 left-0 right-0 bg-white border-t border-gray-200 p-4">
    <div className="max-w-4xl mx-auto">
      <div className="flex items-end gap-3 bg-gray-50 rounded-2xl p-3 border border-gray-200">
        <textarea
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => {if(e.key === 'Enter' && !e.shiftKey && !disabled) {e.preventDefault(); onSend();}}}
          placeholder="Ask the agent to do something... (e.g., 'list all running processes')"
          className="flex-1 bg-transparent resize-none outline-none text-base placeholder:text-gray-400 disabled:opacity-50 min-h-[20px] max-h-32"
          rows={1}
          disabled={disabled}
        />
        <button 
          onClick={onSend} 
          disabled={!input.trim() || disabled} 
          className="bg-green-600 hover:bg-green-700 disabled:bg-gray-300 disabled:cursor-not-allowed p-3 rounded-xl text-white transition-colors"
        >
          <Send size={20} />
        </button>
      </div>
    </div>
  </div>
);

const Spinner = () => (
  <div className="flex items-center justify-center min-h-screen bg-white">
    <Loader2 className="w-10 h-10 text-green-600 animate-spin" />
  </div>
);

const ErrorDisplay = ({ message, onClose }: { message: string, onClose: () => void }) => (
  <div className="bg-red-50 border-b border-red-200 px-4 py-4">
    <div className="flex justify-between items-start">
      <div className="flex items-start gap-3">
        <AlertTriangle size={20} className="text-red-500 mt-0.5" />
        <div>
          <h3 className="text-sm font-semibold text-red-800 mb-1">Execution Failed</h3>
          <p className="text-sm text-red-700 mb-2">{message}</p>
          <div className="text-xs text-red-600">
            <p><strong>Root Cause:</strong> {message}</p>
            <p><strong>Suggested Recovery:</strong> Check agent logs and retry with modified objective</p>
          </div>
        </div>
      </div>
      <button onClick={onClose} className="text-red-500 hover:text-red-700 p-1">
        <span className="text-lg">&times;</span>
      </button>
    </div>
  </div>
);
