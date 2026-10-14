import React, { useState, useEffect, useRef } from 'react';
import { 
  Phone, 
  PhoneOff, 
  Send, 
  Calendar, 
  CheckCircle2, 
  Clock, 
  User, 
  Bot, 
  Activity, 
  Sparkles, 
  RefreshCw,
  Layers,
  Volume2
} from 'lucide-react';

export default function App() {
  const [callActive, setCallActive] = useState(false);
  const [agentSpeaking, setAgentSpeaking] = useState(false);
  const [userSpeaking, setUserSpeaking] = useState(false);
  const [statusMessage, setStatusMessage] = useState("Click 'Start Voice Call' or type a message below");
  const [messages, setMessages] = useState([]);
  const [slots, setSlots] = useState([]);
  const [bookings, setBookings] = useState([]);
  const [dialogueState, setDialogueState] = useState(null);
  const [sessionId, setSessionId] = useState(null);
  const [inputText, setInputText] = useState("");
  const [slotFilter, setSlotFilter] = useState("all");
  const [isRefreshing, setIsRefreshing] = useState(false);

  // Audio & WebSocket refs
  const wsRef = useRef(null);
  const micStreamRef = useRef(null);
  const audioCtxRef = useRef(null);
  const playQueueRef = useRef([]);
  const currentAudioRef = useRef(null);
  const messagesEndRef = useRef(null);

  // Auto scroll messages to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // Initial fetch of slots and bookings
  const loadData = async () => {
    try {
      setIsRefreshing(true);
      const [slotsRes, bookingsRes] = await Promise.all([
        fetch('/slots'),
        fetch('/bookings')
      ]);
      if (slotsRes.ok) {
        const s = await slotsRes.json();
        setSlots(s);
      }
      if (bookingsRes.ok) {
        const b = await bookingsRes.json();
        setBookings(b.filter(item => item.booking_status === 'confirmed'));
      }
    } catch (err) {
      console.error("Failed to load initial data:", err);
    } finally {
      setIsRefreshing(false);
    }
  };

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 10000);
    return () => clearInterval(interval);
  }, []);

  // Audio Playback Manager
  const playNextAudio = () => {
    if (currentAudioRef.current || playQueueRef.current.length === 0) {
      if (!currentAudioRef.current) {
        setAgentSpeaking(false);
        if (wsRef.current?.readyState === WebSocket.OPEN) {
          wsRef.current.send(JSON.stringify({ type: "playback", playing: false }));
        }
      }
      return;
    }

    const audioUrl = playQueueRef.current.shift();
    const audio = new Audio(audioUrl);
    currentAudioRef.current = audio;
    setAgentSpeaking(true);

    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "playback", playing: true }));
    }

    audio.onended = audio.onerror = () => {
      URL.revokeObjectURL(audioUrl);
      currentAudioRef.current = null;
      playNextAudio();
    };

    audio.play().catch(e => {
      console.warn("Audio play prevented:", e);
      currentAudioRef.current = null;
      playNextAudio();
    });
  };

  const enqueueAudioChunk = (b64) => {
    try {
      const byteChars = atob(b64);
      const byteNumbers = new Array(byteChars.length);
      for (let i = 0; i < byteChars.length; i++) {
        byteNumbers[i] = byteChars.charCodeAt(i);
      }
      const byteArray = new Uint8Array(byteNumbers);
      const blob = new Blob([byteArray], { type: "audio/wav" });
      const url = URL.createObjectURL(blob);
      playQueueRef.current.push(url);
      playNextAudio();
    } catch (e) {
      console.error("Error decoding audio chunk", e);
    }
  };

  const stopPlayback = () => {
    playQueueRef.current.forEach(url => URL.revokeObjectURL(url));
    playQueueRef.current = [];
    if (currentAudioRef.current) {
      currentAudioRef.current.pause();
      currentAudioRef.current = null;
    }
    setAgentSpeaking(false);
  };

  // Add message to chat stream
  const addMessage = (role, text, meta) => {
    setMessages(prev => [
      ...prev,
      {
        id: Date.now() + Math.random(),
        role,
        text,
        meta,
        time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
      }
    ]);
  };

  // Handle incoming WebSocket messages
  const handleWebSocketEvent = (event) => {
    switch (event.type) {
      case "session":
        setSessionId(event.session_id);
        setDialogueState(event.state);
        break;

      case "user_speech_start":
        setUserSpeaking(true);
        setStatusMessage("Listening to your voice...");
        break;

      case "barge_in":
        // Caller interrupted agent speech -> stop agent playback immediately
        stopPlayback();
        setStatusMessage("Interrupted! Listening...");
        break;

      case "transcript":
        setUserSpeaking(false);
        addMessage("user", event.text, "Speech-to-Text");
        break;

      case "agent_text":
        addMessage("agent", event.text, `Step: ${event.state.step}`);
        setDialogueState(event.state);
        loadData();
        break;

      case "agent_audio":
        enqueueAudioChunk(event.audio_b64);
        break;

      case "timings":
        if (event.first_audio_ms) {
          setStatusMessage(`Live Voice Ready · First audio ${event.first_audio_ms}ms · STT ${event.stt_ms}ms`);
        }
        break;

      case "ignored":
        setUserSpeaking(false);
        setStatusMessage("Listening...");
        break;

      case "error":
        setStatusMessage(`Error: ${event.message}`);
        break;

      default:
        break;
    }
  };

  // Start Live Audio Call
  const startCall = async () => {
    if (wsRef.current) return;

    try {
      setStatusMessage("Requesting microphone access...");
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1
        }
      });
      micStreamRef.current = stream;

      const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      audioCtxRef.current = audioCtx;

      await audioCtx.audioWorklet.addModule('/pcm-processor.js');
      const workletNode = new AudioWorkletNode(audioCtx, 'pcm16');
      const source = audioCtx.createMediaStreamSource(stream);
      source.connect(workletNode);

      const protocol = window.location.protocol === "https:" ? "wss" : "ws";
      const wsUrl = `${protocol}://${window.location.host}/ws/calls`;
      const ws = new WebSocket(wsUrl);
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.onopen = () => {
        setCallActive(true);
        setStatusMessage("Connected! Start speaking whenever you're ready.");
        addMessage("system", "Voice Call Established. Full-duplex audio stream connected.");
        
        workletNode.port.onmessage = (e) => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(e.data);
          }
        };
      };

      ws.onmessage = (msg) => {
        try {
          const data = JSON.parse(msg.data);
          handleWebSocketEvent(data);
        } catch (e) {
          console.error("WS Parse error", e);
        }
      };

      ws.onclose = () => {
        endCall("Call ended.");
      };

      ws.onerror = (err) => {
        console.error("WebSocket error:", err);
        endCall("Call connection error.");
      };

    } catch (err) {
      console.error("Microphone or WebAudio error:", err);
      setStatusMessage(`Microphone Error: ${err.message}`);
      setCallActive(false);
    }
  };

  // End Call
  const endCall = (message = "Call ended.") => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "hangup" }));
      wsRef.current.close();
    }
    wsRef.current = null;

    stopPlayback();

    if (micStreamRef.current) {
      micStreamRef.current.getTracks().forEach(track => track.stop());
      micStreamRef.current = null;
    }

    if (audioCtxRef.current) {
      audioCtxRef.current.close();
      audioCtxRef.current = null;
    }

    setCallActive(false);
    setUserSpeaking(false);
    setStatusMessage(message);
    addMessage("system", message);
  };

  // Send Typed Message
  const handleSendText = async (customText = null) => {
    const textToSend = customText || inputText.trim();
    if (!textToSend) return;

    if (!customText) setInputText("");
    stopPlayback();
    addMessage("user", textToSend, "Typed");

    try {
      let currentSid = sessionId;
      if (!currentSid) {
        // Create an initial session if not in a call
        const sRes = await fetch('/sessions', { method: 'POST' });
        if (sRes.ok) {
          const sData = await sRes.json();
          currentSid = sData.session_id;
          setSessionId(currentSid);
          setDialogueState(sData.state);
        }
      }

      const res = await fetch(`/sessions/${currentSid}/text`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: textToSend, speak: true })
      });

      if (res.ok) {
        const data = await res.json();
        addMessage("agent", data.reply_text, `Step: ${data.state.step}`);
        setDialogueState(data.state);
        if (data.reply_audio_url) {
          playQueueRef.current.push(data.reply_audio_url);
          playNextAudio();
        }
        loadData();
      }
    } catch (err) {
      console.error("Send text error:", err);
      addMessage("system", "Failed to send message to agent server.");
    }
  };

  // Filter slots
  const filteredSlots = slots.filter(s => {
    if (slotFilter === "all") return true;
    return s.service.toLowerCase().includes(slotFilter);
  });

  return (
    <div className="app-container">
      {/* Header */}
      <header className="app-header glass-panel">
        <div className="brand-section">
          <div className="brand-logo">
            <Bot size={26} />
          </div>
          <div className="brand-text">
            <h1>Voice Appointment AI Agent</h1>
            <p>PS02 Cerevyn Assessment · STT, NLU & Concurrency Protected</p>
          </div>
        </div>

        <div className="header-badges">
          <div className={`status-pill ${callActive ? (agentSpeaking ? 'speaking' : 'active') : ''}`}>
            <span className="pulse-dot"></span>
            {callActive ? (agentSpeaking ? "Agent Speaking" : (userSpeaking ? "User Speaking" : "Call Connected")) : "Agent Idle"}
          </div>

          <div className="status-pill">
            <Clock size={14} />
            <span>IST (UTC+5:30)</span>
          </div>

          <button 
            onClick={loadData} 
            disabled={isRefreshing}
            className="status-pill"
            style={{ cursor: 'pointer', background: 'rgba(255, 255, 255, 0.05)' }}
            title="Refresh database"
          >
            <RefreshCw size={13} className={isRefreshing ? "animate-spin" : ""} />
            <span>Sync</span>
          </button>
        </div>
      </header>

      {/* Main Grid */}
      <div className="main-grid">
        {/* Left Column: Voice Call & Conversation Stream */}
        <section className="glass-panel call-hero-card">
          <div className="call-status-bar">
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <div className="audio-waves">
                {[...Array(6)].map((_, i) => (
                  <div key={i} className={`wave-bar ${(callActive && (agentSpeaking || userSpeaking)) ? 'speaking' : ''}`} />
                ))}
              </div>
              <span style={{ fontSize: '0.85rem', color: '#1e293b', fontWeight: 600 }}>
                {statusMessage}
              </span>
            </div>

            {agentSpeaking && (
              <span style={{ fontSize: '0.75rem', color: '#4f46e5', fontWeight: 600, display: 'flex', alignItems: 'center', gap: 4 }}>
                <Volume2 size={14} /> Barge-in enabled
              </span>
            )}
          </div>

          {/* Action Row */}
          <div className="call-actions-row">
            {!callActive ? (
              <button onClick={startCall} className="btn-primary-call">
                <Phone size={18} />
                <span>Start Voice Call</span>
              </button>
            ) : (
              <button onClick={() => endCall("Call ended by user")} className="btn-danger-hangup">
                <PhoneOff size={18} />
                <span>End Call</span>
              </button>
            )}

            <div className="chips-row">
              <button 
                onClick={() => handleSendText("I want to book an appointment with Dr. Meera Iyer")} 
                className="chip-btn"
              >
                + Dr. Meera Iyer (Dental)
              </button>
              <button 
                onClick={() => handleSendText("October 20 at 10 AM")} 
                className="chip-btn"
              >
                + Oct 20 at 10 AM
              </button>
              <button 
                onClick={() => handleSendText("Yes, confirm")} 
                className="chip-btn"
              >
                + "Yes, confirm"
              </button>
              <button 
                onClick={() => handleSendText("Cancel my booking")} 
                className="chip-btn"
              >
                + Cancel Booking
              </button>
            </div>
          </div>

          {/* Live Transcript Stream */}
          <div className="transcript-box">
            {messages.length === 0 ? (
              <div style={{ margin: 'auto', textAlign: 'center', color: '#64748b' }}>
                <Bot size={40} style={{ margin: '0 auto 10px', opacity: 0.3 }} />
                <p style={{ fontWeight: 600, fontSize: '0.95rem' }}>Conversation is empty</p>
                <p style={{ fontSize: '0.8rem', marginTop: 4 }}>
                  Click <strong>Start Voice Call</strong> to talk live or send a message below.
                </p>
              </div>
            ) : (
              messages.map(msg => (
                <div key={msg.id} className={`message-item ${msg.role}`}>
                  <div>{msg.text}</div>
                  <div className="msg-meta">
                    {msg.meta && <span className="msg-badge">{msg.meta}</span>}
                    <span>{msg.time}</span>
                  </div>
                </div>
              ))
            )}
            <div ref={messagesEndRef} />
          </div>

          {/* Input Bar */}
          <div className="input-bar">
            <input 
              type="text" 
              className="input-field"
              placeholder="Type message or answer agent (e.g. 'Oct 20 at 11 AM')..."
              value={inputText}
              onChange={(e) => setInputText(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSendText()}
            />
            <button 
              onClick={() => handleSendText()} 
              disabled={!inputText.trim()}
              className="btn-send"
            >
              <Send size={16} />
              <span>Send</span>
            </button>
          </div>
        </section>

        {/* Right Column: Operational Dashboard */}
        <aside className="dashboard-col">
          {/* Confirmed Bookings Card */}
          <div className="glass-panel" style={{ borderLeft: '4px solid #10b981' }}>
            <div className="card-header">
              <div className="card-title">
                <CheckCircle2 size={18} color="#10b981" />
                <span>Confirmed Bookings</span>
              </div>
              <span className="count-badge">{bookings.length}</span>
            </div>

            <div className="card-body">
              {bookings.length === 0 ? (
                <p style={{ fontSize: '0.8125rem', color: '#64748b' }}>No confirmed bookings yet in this session.</p>
              ) : (
                <div className="table-wrapper">
                  <table className="custom-table">
                    <thead>
                      <tr>
                        <th>Code</th>
                        <th>Service</th>
                        <th>Date & Time</th>
                        <th>Customer</th>
                      </tr>
                    </thead>
                    <tbody>
                      {bookings.map((b) => {
                        const d = new Date(b.start_at);
                        const formatted = `${d.getDate()} Oct, ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
                        return (
                          <tr key={b.booking_id}>
                            <td className="booking-code">{b.booking_id}</td>
                            <td>{b.service}</td>
                            <td>{formatted}</td>
                            <td>{b.customer_name}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>

          {/* Available Slots Card */}
          <div className="glass-panel">
            <div className="card-header">
              <div className="card-title">
                <Calendar size={18} color="#6366f1" />
                <span>Available Timetable</span>
              </div>
              <div style={{ display: 'flex', gap: 4 }}>
                {['all', 'dental', 'haircut', 'physio'].map((cat) => (
                  <button
                    key={cat}
                    onClick={() => setSlotFilter(cat)}
                    style={{
                      border: 'none',
                      background: slotFilter === cat ? '#eef2ff' : 'transparent',
                      color: slotFilter === cat ? '#4338ca' : '#64748b',
                      borderRadius: 6,
                      padding: '3px 8px',
                      fontSize: '0.75rem',
                      fontWeight: 600,
                      cursor: 'pointer'
                    }}
                  >
                    {cat.toUpperCase()}
                  </button>
                ))}
              </div>
            </div>

            <div className="card-body">
              <div className="table-wrapper">
                <table className="custom-table">
                  <thead>
                    <tr>
                      <th>Service</th>
                      <th>Date</th>
                      <th>Time</th>
                      <th>Practitioner</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredSlots.map((s, idx) => (
                      <tr key={idx}>
                        <td style={{ fontWeight: 600, color: '#0f172a' }}>{s.service}</td>
                        <td>{s.slot_date}</td>
                        <td><span style={{ color: '#0284c7', fontWeight: 600 }}>{s.slot_time}</span></td>
                        <td style={{ color: '#64748b' }}>{s.practitioner}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          {/* Dialogue State Inspector */}
          <div className="glass-panel">
            <div className="card-header">
              <div className="card-title">
                <Layers size={18} color="#f59e0b" />
                <span>Dialogue State Machine</span>
              </div>
              <span style={{ fontSize: '0.75rem', color: '#94a3b8', fontFamily: 'monospace' }}>
                step: {dialogueState?.step || 'idle'}
              </span>
            </div>

            <div className="card-body">
              <div className="state-meta-grid">
                <div className="meta-item">
                  <span className="meta-label">Intent</span>
                  <span className="meta-val">{dialogueState?.intent || 'none'}</span>
                </div>
                <div className="meta-item">
                  <span className="meta-label">Awaiting</span>
                  <span className="meta-val">{dialogueState?.awaiting || 'none'}</span>
                </div>
                <div className="meta-item">
                  <span className="meta-label">Service</span>
                  <span className="meta-val">{dialogueState?.service || '—'}</span>
                </div>
                <div className="meta-item">
                  <span className="meta-label">Customer</span>
                  <span className="meta-val">{dialogueState?.customer_name || '—'}</span>
                </div>
              </div>

              <div className="state-viewer">
                {dialogueState ? (
                  <pre>{JSON.stringify(dialogueState, null, 2)}</pre>
                ) : (
                  <span>No active session. State will update live on first interaction.</span>
                )}
              </div>
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
