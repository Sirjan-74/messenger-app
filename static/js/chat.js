// -------------------- AUDIO PLAYER FIX --------------------
let currentAudio = null;
let currentButton = null;

function toggleAudio(audioId, fileUrl) {
    try {
        console.log("🎵 Audio clicked:", audioId, fileUrl);
        
        const button = event?.target?.closest('.audio-play-btn');
        
        // If audio is playing, stop it
        if (currentAudio && !currentAudio.paused) {
            currentAudio.pause();
            currentAudio.currentTime = 0;
            if (currentButton) {
                currentButton.innerHTML = '<i class="fas fa-play"></i>';
            }
            currentAudio = null;
            currentButton = null;
            console.log("⏸️ Audio stopped");
            return;
        }

        // Create new audio with crossOrigin
        currentAudio = new Audio();
        currentAudio.crossOrigin = "anonymous";
        currentAudio.src = fileUrl;
        currentButton = button;
        
        console.log("🎵 Audio object created, attempting to play...");
        console.log("🎵 Audio URL:", fileUrl);

        // Change button to pause icon
        if (button) {
            button.innerHTML = '<i class="fas fa-pause"></i>';
        }

        currentAudio.play()
        .then(() => {
            console.log("✅ Audio playing successfully!");
        })
        .catch(e => {
            console.error("❌ Play error:", e);
            if (button) {
                button.innerHTML = '<i class="fas fa-play"></i>';
            }
            alert("Could not play audio: " + e.message);
        });

        currentAudio.onended = () => {
            console.log("🎵 Audio ended naturally");
            if (currentButton) {
                currentButton.innerHTML = '<i class="fas fa-play"></i>';
            }
            currentAudio = null;
            currentButton = null;
        };

        currentAudio.onerror = (e) => {
            console.error("❌ Audio loading error:", e);
            console.error("Audio error details:", currentAudio.error);
            if (currentButton) {
                currentButton.innerHTML = '<i class="fas fa-play"></i>';
            }
            alert("Audio loading failed. Error code: " + (currentAudio.error?.code || 'unknown'));
        };

    } catch (error) {
        console.error("❌ Audio exception:", error);
        alert("Audio error: " + error.message);
    }
}

(function () {
  'use strict';

  console.log("🚀 Chat.js initializing...");

  function $(sel, ctx = document) { return ctx.querySelector(sel); }
  function $all(sel, ctx = document) { return Array.from(ctx.querySelectorAll(sel)); }
  function escapeHtml(text) {
    const d = document.createElement('div');
    d.textContent = text;
    return d.innerHTML;
  }

  const container = document.querySelector('.chat-container');
  if (!container) {
    console.error("❌ No .chat-container found!");
    return;
  }

  let room = container.dataset.room || '';
  console.log("📍 Room:", room);
  
  const messagesContainer = $('#messagesContainer');
  const CURRENT_USER = window.CURRENT_USER || null;
  console.log("👤 Current user:", CURRENT_USER);

  let socket = null;
  let selectMode = false;
  let selectedIds = new Set();
  let editingMsgId = null;

  const selectModeBtn = $('#selectModeBtn');
  const selectAllBtn = $('#selectAllBtn');
  const deleteSelectedBtn = $('#deleteSelectedBtn');
  const clearChatBtn = $('#clearChatBtn');
  const messageInput = $('#messageInput');
  const sendBtn = $('#sendBtn');
  const fileInput = $('#fileInput');
  const voiceBtn = $('#voiceBtn');
  const editBackdrop = $('#editModalBackdrop');
  const editInput = $('#editInput');
  const cancelEditBtn = $('#cancelEditBtn');
  const saveEditBtn = $('#saveEditBtn');

  // ==================== SOCKET INITIALIZATION ====================
  function initSocket() {
    if (typeof io === "undefined") {
      console.error("❌ Socket.IO not loaded!");
      return;
    }

    console.log("🔌 Initializing Socket.IO connection...");

    socket = io(window.location.origin, {
      transports: ["websocket", "polling"],
      withCredentials: true
    });

    socket.on("connect", () => {
      console.log("✅ Socket connected! Socket ID:", socket.id);
      socket.emit("join_room", { room });
      console.log("📤 Sent join_room event for:", room);
      
      const statusEl = $("#status");
      if (statusEl) statusEl.textContent = "Connected";
    });

    socket.on("disconnect", () => {
      console.log("❌ Socket disconnected");
      const statusEl = $("#status");
      if (statusEl) statusEl.textContent = "Reconnecting...";
    });

    socket.io.on("reconnect_attempt", () => {
      console.log("🔄 Attempting reconnect...");
      socket.emit("join_room", { room });
    });

    socket.on("new_message", (msg) => {
      console.log("📩 NEW MESSAGE RECEIVED:", JSON.stringify(msg, null, 2));
      appendMessageToDOM(msg);
      scrollToBottom();
    });

    socket.on("message_edited", d => {
      console.log("✏️ Message edited:", d);
      const el = document.querySelector(`.message[data-msg-id="${d.message_id}"] .msg-text`);
      if (el) el.textContent = d.new_text;
    });

    socket.on("message_deleted", d => {
      console.log("🗑️ Message deleted:", d);
      const node = document.querySelector(`.message[data-msg-id="${d.message_id}"]`);
      if (node) node.remove();
    });

    socket.on("messages_deleted", d => {
      console.log("🗑️ Multiple messages deleted:", d);
      (d.message_ids || []).forEach(id => {
        const node = document.querySelector(`.message[data-msg-id="${id}"]`);
        if (node) node.remove();
      });
    });

    socket.on("chat_cleared", () => {
      console.log("🧹 Chat cleared");
      $all(".message").forEach(m => m.remove());
    });

    socket.on("unread_update", data => {
      console.log("📬 Unread update:", data);
    });

    // Log ALL socket events for debugging
    socket.onAny((eventName, ...args) => {
      console.log(`🔔 Socket event: ${eventName}`, args);
    });
  }

  function scrollToBottom() {
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
  }

  function formatTime(ts) {
    try {
      const d = ts ? new Date(ts) : new Date();
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch (e) { 
      console.error("Time format error:", e);
      return ''; 
    }
  }

  function appendMessageToDOM(data) {
    console.log("➕ Appending message to DOM:", data);
    
    const id = data._id || "m" + Date.now();
    const wrapper = document.createElement("div");

    wrapper.className = "message " + (data.author_username == CURRENT_USER ? "own" : "");
    wrapper.dataset.msgId = id;

    let contentHtml = "";
    const ts = formatTime(data.timestamp);

    if (data.message_type === "file" && data.file_info) {
      console.log("📎 Rendering file message");
      if (data.file_info.file_type === "image" || data.file_info.file_type?.startsWith("image")) {
        contentHtml = `<div class="image-message"><img src="${data.file_info.file_path || data.file_info.file_url}" style="max-width:100%;border-radius:8px;"></div>`;
      } else {
        contentHtml = `<div class="msg-text">📎 Shared a file</div>`;
      }
    }
    else if (data.message_type === "voice" && data.voice_info) {
      console.log("🎤 Rendering voice message");
      const voicePath = data.voice_info.file_path || data.voice_info.file_url;
      const duration = data.voice_info.duration || 0;
      contentHtml = `
        <div class="audio-player">
          <button class="audio-play-btn" onclick="toggleAudio('${id}','${voicePath}')" style="width:36px;height:36px;border-radius:50%;background:#667eea;color:white;border:none;cursor:pointer;transition:all 0.2s;">
            <i class="fas fa-play"></i>
          </button>
          <span style="margin-left:8px;color:inherit;">🎤 Voice message — ${duration.toFixed(1)}s</span>
        </div>`;
    }
    else {
      console.log("💬 Rendering text message");
      contentHtml = `<div class="msg-text">${escapeHtml(data.text)}</div>`;
    }

    wrapper.innerHTML = `
      <div class="message-bubble">
        <div style="display:flex;gap:8px;">
          <div style="flex:1;">
            ${data.author_username != CURRENT_USER ?
              `<div class="message-author">${escapeHtml(data.author_username)}</div>` : ""}
            ${contentHtml}
            <div class="msg-ts">${ts}</div>
          </div>

          <div class="msg-actions">
            <div class="select-checkbox" data-selected="false" style="display:none;"></div>
            <div class="msg-menu-btn"><i class="fas fa-ellipsis-h"></i></div>
          </div>

          <div class="dropdown">
            <button data-action="reply"><i class="fas fa-reply"></i> Reply</button>
            <button data-action="edit"><i class="fas fa-edit"></i> Edit</button>
            <button data-action="delete-me"><i class="fas fa-trash"></i> Delete (me)</button>
            <button data-action="delete-all"><i class="fas fa-user-times"></i> Delete all</button>
            <button data-action="select"><i class="fas fa-check-circle"></i> Select</button>
          </div>
        </div>
      </div>`;

    messagesContainer.appendChild(wrapper);
    console.log("✅ Message appended to DOM");
  }

  function closeAllDropdowns() {
    $all(".dropdown").forEach(d => d.classList.remove("show"));
  }

  // ==================== MESSAGE INTERACTIONS ====================
  messagesContainer.addEventListener("click", e => {
    const menuBtn = e.target.closest(".msg-menu-btn");
    if (menuBtn) {
      e.stopPropagation();
      closeAllDropdowns();
      const msgEl = menuBtn.closest(".message");
      const dropdown = msgEl.querySelector(".dropdown");
      dropdown.classList.toggle("show");
      return;
    }

    const checkbox = e.target.closest(".select-checkbox");
    if (checkbox) {
      e.stopPropagation();
      const msgEl = checkbox.closest(".message");
      toggleMsgSelect(msgEl.dataset.msgId, checkbox);
      return;
    }

    const ddBtn = e.target.closest(".dropdown button");
    if (ddBtn) {
      e.stopPropagation();
      const msgEl = ddBtn.closest(".message");
      handleMenuAction(ddBtn.dataset.action, msgEl);
      closeAllDropdowns();
      return;
    }

    closeAllDropdowns();
  });

  function setSelectMode(on) {
    selectMode = on;
    selectModeBtn.textContent = on ? "Cancel" : "Select";
    $all(".select-checkbox").forEach(el => el.style.display = on ? "inline-flex" : "none");

    if (!on) {
      selectedIds.clear();
      $all(".select-checkbox").forEach(ch => {
        ch.classList.remove("checked");
        ch.dataset.selected = "false";
      });
    }
    deleteSelectedBtn.disabled = selectedIds.size === 0;
  }

  function toggleMsgSelect(id, el) {
    if (el.dataset.selected === "true") {
      el.dataset.selected = "false";
      el.classList.remove("checked");
      selectedIds.delete(id);
    } else {
      el.dataset.selected = "true";
      el.classList.add("checked");
      selectedIds.add(id);
    }
    deleteSelectedBtn.disabled = selectedIds.size === 0;
  }

  function handleMenuAction(action, msgEl) {
    const id = msgEl.dataset.msgId;
    const txt = msgEl.querySelector(".msg-text")?.textContent || "";

    if (action === "reply") {
      messageInput.value = `Replying: "${txt.slice(0, 40)}..." \n`;
      messageInput.focus();
      return;
    }

    if (action === "edit") {
      editingMsgId = id;
      editInput.value = txt;
      editBackdrop.classList.add("show");
      return;
    }

    if (action === "delete-me") {
      deleteForMe(id);
      return;
    }

    if (action === "delete-all") {
      deleteEveryone(id);
      return;
    }

    if (action === "select") {
      setSelectMode(true);
      toggleMsgSelect(id, msgEl.querySelector(".select-checkbox"));
    }
  }

  async function deleteForMe(id) {
    try {
      await fetch("/delete_message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message_ids: [id] }),
      });
      document.querySelector(`.message[data-msg-id="${id}"]`)?.remove();
    } catch (e) {
      console.error("Delete error:", e);
    }
  }

  async function deleteEveryone(id) {
    try {
      await fetch("/delete_everyone", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message_id: id }),
      });
      document.querySelector(`.message[data-msg-id="${id}"]`)?.remove();
    } catch (e) {
      console.error("Delete error:", e);
    }
  }

  // ==================== BUTTON HANDLERS ====================
  deleteSelectedBtn.addEventListener("click", async () => {
    const ids = [...selectedIds];
    try {
      await fetch("/delete_message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message_ids: ids }),
      });
      ids.forEach(id => {
        document.querySelector(`.message[data-msg-id="${id}"]`)?.remove();
      });
      setSelectMode(false);
    } catch (e) {
      console.error("Delete selected error:", e);
    }
  });

  clearChatBtn.addEventListener("click", async () => {
    if (!confirm("Clear all messages in this chat?")) return;
    try {
      await fetch("/clear_chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ room }),
      });
      $all(".message").forEach(m => m.remove());
    } catch (e) {
      console.error("Clear chat error:", e);
    }
  });

  fileInput.addEventListener("change", e => {
    const file = e.target.files[0];
    if (!file) return;

    console.log("📤 Uploading file:", file.name);

    const fd = new FormData();
    fd.append("file", file);
    fd.append("room", room);

    fetch("/upload", { method: "POST", body: fd })
      .then(res => res.json())
      .then(data => {
        if (data.success) {
          console.log("✅ File uploaded successfully");
        } else {
          console.error("❌ File upload failed:", data);
        }
      })
      .catch(err => console.error("Upload error:", err))
      .finally(() => e.target.value = "");
  });

  function sendMessage() {
    const txt = messageInput.value.trim();
    if (!txt) {
      console.warn("⚠️ Cannot send empty message");
      return;
    }
    
    if (!socket || !socket.connected) {
      console.error("❌ Socket not connected!");
      alert("Not connected to server. Please refresh the page.");
      return;
    }
    
    console.log("📤 Sending message:", txt);
    console.log("📤 To room:", room);
    console.log("📤 Socket connected:", socket.connected);
    
    socket.emit("send_message", { message: txt, room: room });
    console.log("✅ Message emitted via socket");
    
    messageInput.value = "";
  }

  sendBtn.addEventListener("click", () => {
    console.log("🖱️ Send button clicked");
    sendMessage();
  });
  
  messageInput.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      console.log("⌨️ Enter key pressed");
      sendMessage();
    }
  });

  // ==================== EDIT MODAL ====================
  cancelEditBtn.onclick = () => editBackdrop.classList.remove("show");

  saveEditBtn.onclick = async () => {
    const newText = editInput.value.trim();
    if (!newText) return;
    
    try {
      await fetch("/edit_message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message_id: editingMsgId, new_text: newText }),
      });

      const el = document.querySelector(`.message[data-msg-id="${editingMsgId}"] .msg-text`);
      if (el) el.textContent = newText;

      editBackdrop.classList.remove("show");
    } catch (e) {
      console.error("Edit error:", e);
    }
  };

  // ==================== SELECT MODE ====================
  selectModeBtn.onclick = () => setSelectMode(!selectMode);

  selectAllBtn.onclick = () => {
    const checkboxes = $all(".select-checkbox");
    const allSelected = checkboxes.every(ch => ch.dataset.selected === "true");

    checkboxes.forEach(ch => {
      const id = ch.closest(".message").dataset.msgId;
      if (allSelected) {
        ch.dataset.selected = "false";
        ch.classList.remove("checked");
        selectedIds.delete(id);
      } else {
        ch.dataset.selected = "true";
        ch.classList.add("checked");
        selectedIds.add(id);
      }
    });

    deleteSelectedBtn.disabled = selectedIds.size === 0;
  };

  // ==================== VOICE RECORDING ====================
  let mediaRecorder = null;
  let audioChunks = [];
  let isRecording = false;

  voiceBtn.addEventListener("click", async () => {
    console.log("🎤 Voice button clicked, isRecording:", isRecording);
    
    if (!isRecording) {
      try {
        console.log("🎤 Requesting microphone access...");
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        console.log("✅ Microphone access granted");
        
        mediaRecorder = new MediaRecorder(stream);
        audioChunks = [];

        mediaRecorder.ondataavailable = (e) => {
          console.log("🎤 Audio data available:", e.data.size, "bytes");
          audioChunks.push(e.data);
        };

        mediaRecorder.onstop = async () => {
          console.log("🎤 Recording stopped, chunks:", audioChunks.length);
          const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
          const duration = audioChunks.length * 0.1;
          
          console.log("📤 Uploading voice message, size:", audioBlob.size, "duration:", duration);
          
          const formData = new FormData();
          formData.append('audio', audioBlob, 'voice.webm');
          formData.append('room', room);
          formData.append('duration', duration.toFixed(1));

          try {
            const res = await fetch('/upload_voice', { method: 'POST', body: formData });
            const data = await res.json();
            console.log("📥 Voice upload response:", data);
            if (data.success) {
              console.log("✅ Voice message uploaded successfully");
            } else {
              console.error("❌ Voice upload failed:", data);
            }
          } catch (err) {
            console.error("❌ Voice upload error:", err);
          }

          stream.getTracks().forEach(track => track.stop());
        };

        mediaRecorder.start();
        isRecording = true;
        voiceBtn.innerHTML = '<i class="fas fa-stop"></i>';
        voiceBtn.style.background = '#ff6b6b';
        console.log("🎤 Recording started");
      } catch (err) {
        console.error("❌ Microphone access error:", err);
        alert("Could not access microphone: " + err.message);
      }
    } else {
      console.log("🎤 Stopping recording...");
      mediaRecorder.stop();
      isRecording = false;
      voiceBtn.innerHTML = '<i class="fas fa-microphone"></i>';
      voiceBtn.style.background = '';
      console.log("⏹️ Recording stopped");
    }
  });

  // ==================== THEME SELECTOR ====================
  const themeButtons = $all('.theme-btn');
  themeButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      const theme = btn.dataset.theme;
      applyTheme(theme);
      
      // Mark active theme
      themeButtons.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
    });
  });

  function applyTheme(theme) {
    const container = $('.chat-container');
    if (!container) return;

    container.classList.remove('light', 'dark', 'festive', 'ocean', 'sunset');
    container.classList.add(theme);
    
    console.log(`🎨 Theme applied: ${theme}`);
  }

  // ==================== INITIALIZATION ====================
  console.log("🔧 Starting initialization...");
  initSocket();
  scrollToBottom();

  if (socket) {
    socket.emit('mark_read', { room });
  }

  // Apply default theme
  applyTheme('light');

  console.log("✅ Chat initialized successfully!");
  console.log("📊 Summary:");
  console.log("  - Room:", room);
  console.log("  - User:", CURRENT_USER);
  console.log("  - Socket:", socket ? "Created" : "Failed");
})();
