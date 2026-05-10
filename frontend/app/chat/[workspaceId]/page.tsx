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
  type ForgeV2JobResponse,
  type JobRecord,
  type JobStreamEvent,
  type StepResult,
  type StoredChatMessage,
  type Workspace,
} from '@/services/api';
import { getToken, logout } from '@/services/auth';
import { ArrowLeft, Send, Bot, User, Loader2, AlertTriangle, Terminal } from 'lucide-react';

// ===== UTILITY & TYPE DEFINITIONS =====

const POLL_INTERVAL_MS = 2500;

interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  createdAt: string;
  status?: 'running' | 'completed' | 'failed' | 'queued';
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
      setMessages(chat.messages.map(m => ({
        id: m.id,
        role: m.role,
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
  }, [messages, isSending]);


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
    }
  }, []);

  const connectToJob = useCallback((jobId: string, messageId: string) => {
    socketRef.current = new WebSocket(getJobWebSocketUrl(jobId));

    socketRef.current.onmessage = (event) => {
        const data: JobStreamEvent = JSON.parse(event.data);
        if(data.type === 'completed' || data.type === 'failed' || data.type === 'cancelled') {
            // Stop polling and close
            if(timeoutRef.current) clearTimeout(timeoutRef.current);
            socketRef.current?.close();
            setIsRunning(false);
            // Update status
            setMessages(prev => prev.map(msg => 
              msg.id === messageId 
                ? {
                    ...msg,
                    status: data.type as any,
                    content: data.summary || msg.content,
                    isError: data.type === 'failed',
                  }
                : msg
            ));
        } else if(data.type === 'step_result') {
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
    <div className="flex h-screen flex-col bg-gray-900 text-gray-100">
      <Header workspace={workspace} router={useRouter()} />
      {error && <ErrorDisplay message={error} onClose={() => setError(null)} />}
      <main className="flex-1 overflow-y-auto p-4">
        <div className="mx-auto max-w-4xl space-y-6">
          {messages.map(msg => <ChatMessageItem key={msg.id} {...msg} />)}
        </div>
        <div ref={bottomRef} />
      </main>
      <InputArea 
        input={input} 
        setInput={setInput} 
        onSend={handleSend} 
        disabled={isRunning}
      />
    </div>
  );
}

// ===== SUB-COMPONENTS =====

const Header = ({ workspace, router }: { workspace: Workspace | null, router: any }) => (
  <header className="flex-shrink-0 border-b border-gray-700 bg-gray-800 p-4 flex items-center shadow-md">
    <button onClick={() => router.back()} className="p-2 rounded-full hover:bg-gray-700 mr-4">
      <ArrowLeft size={20} />
    </button>
    <h1 className="text-lg font-bold">{workspace?.name || 'Chat'}</h1>
  </header>
);

const ChatMessageItem = (msg: ChatMessage) => (
  <div className={`flex items-start gap-4 ${msg.role === 'user' ? 'justify-end' : ''}`}>
    {msg.role === 'assistant' && <Bot className="w-8 h-8 flex-shrink-0 text-blue-400 mt-1" />}
    <div className={`max-w-2xl rounded-lg px-4 py-2.5 shadow ${msg.role === 'user' ? 'bg-blue-600 text-white rounded-br-none' : msg.isError ? 'bg-red-800/50 border border-red-700 rounded-bl-none' : 'bg-gray-700 rounded-bl-none'}`}>
        <p className="whitespace-pre-wrap">{msg.content}</p>
        {msg.status && msg.status !== 'completed' && (
            <div className="flex items-center gap-2 mt-2 text-xs text-gray-400">
                <Loader2 className="animate-spin" size={14}/>
                <span>{msg.status.charAt(0).toUpperCase() + msg.status.slice(1)}...</span>
            </div>
        )}
        {msg.steps && msg.steps.length > 0 && (
            <div className="mt-4 border-t border-gray-600 pt-3 space-y-3">
                {msg.steps.map(step => <StepCard key={step.step} {...step} />)}
            </div>
        )}
    </div>
    {msg.role === 'user' && <User className="w-8 h-8 flex-shrink-0 bg-gray-600 p-1.5 rounded-full mt-1" />}
  </div>
);

const StepCard = (step: StepResult) => {
    const executedCommand = `${step.tool} ${Object.entries(step.args).map(([k, v]) => `--${k} ${JSON.stringify(v)}`).join(' ')}`.trim();
    const validatorResult = step.success ? 'Passed' : `Failed (exit code: ${step.exit_code})`;
    const retryCount = 0; // Placeholder, as not in data

    return (
        <details className="bg-gray-800/50 rounded-lg p-3 border border-gray-600/70 overflow-hidden">
            <summary className="flex justify-between items-center cursor-pointer text-sm font-semibold">
                <div className="flex items-center gap-2">
                    <Terminal size={16} className="text-gray-400"/>
                    <span>Step {step.step}: {formatToolName(step.tool)}</span>
                </div>
                <span className={`px-2 py-1 rounded-md text-xs font-bold ${step.success ? 'bg-green-500/20 text-green-300' : 'bg-red-500/20 text-red-300'}`}>
                    {step.success ? 'Success' : `Exit ${step.exit_code}`}
                </span>
            </summary>
            <div className="mt-3 pt-3 border-t border-gray-600/50 space-y-3">
                {executedCommand && (
                    <div>
                        <h4 className="text-xs font-semibold text-gray-400 mb-1">Executed Command</h4>
                        <pre className="bg-gray-900 rounded p-2 text-xs text-gray-300 max-h-40 overflow-y-auto whitespace-pre-wrap font-mono">{executedCommand}</pre>
                    </div>
                )}
                {/* Reasoning not available in StepResult */}
                {step.stdout && (
                    <div>
                        <h4 className="text-xs font-semibold text-gray-400 mb-1">STDOUT</h4>
                        <pre className="bg-gray-900 rounded p-2 text-xs text-gray-300 max-h-60 overflow-y-auto whitespace-pre-wrap font-mono">{step.stdout}</pre>
                    </div>
                )}
                {step.stderr && (
                    <div className="mt-2">
                        <h4 className="text-xs font-semibold text-red-400 mb-1">STDERR</h4>
                        <pre className="bg-gray-900 rounded p-2 text-xs text-red-300 max-h-40 overflow-y-auto whitespace-pre-wrap font-mono">{step.stderr}</pre>
                    </div>
                )}
                <div>
                    <h4 className="text-xs font-semibold text-gray-400 mb-1">Validator Result</h4>
                    <p className="text-xs text-gray-300">{validatorResult}</p>
                </div>
                <div>
                    <h4 className="text-xs font-semibold text-gray-400 mb-1">Retry Count</h4>
                    <p className="text-xs text-gray-300">{retryCount}</p>
                </div>
            </div>
        </details>
    );
};

const InputArea = ({ input, setInput, onSend, disabled }: any) => (
  <div className="flex-shrink-0 border-t border-gray-700 bg-gray-800 p-4">
    <div className="flex items-center gap-2 max-w-4xl mx-auto bg-gray-700 rounded-lg p-2">
      <textarea
        value={input}
        onChange={e => setInput(e.target.value)}
        onKeyDown={e => {if(e.key === 'Enter' && !e.shiftKey && !disabled) {e.preventDefault(); onSend();}}}
        placeholder="Ask the agent to do something... (e.g., 'list all running processes')"
        className="flex-1 bg-transparent resize-none outline-none p-2 text-base placeholder:text-gray-400 disabled:opacity-50"
        rows={1}
        disabled={disabled}
      />
      <button onClick={onSend} disabled={!input.trim() || disabled} className="bg-blue-600 p-3 rounded-lg text-white hover:bg-blue-500 disabled:bg-gray-600 disabled:cursor-not-allowed">
        <Send size={20} />
      </button>
    </div>
  </div>
);

const Spinner = () => (
  <div className="flex items-center justify-center h-screen bg-gray-900">
    <Loader2 className="w-10 h-10 text-blue-500 animate-spin" />
  </div>
);

const ErrorDisplay = ({ message, onClose }: { message: string, onClose: () => void }) => (
    <div className="bg-red-800/50 border-b border-red-700 p-3 flex justify-between items-center">
        <div className="flex items-center gap-2">
            <AlertTriangle size={20} className="text-red-300"/>
            <p className="text-sm text-red-200">{message}</p>
        </div>
        <button onClick={onClose} className="text-red-200 hover:text-white">&times;</button>
    </div>
);
