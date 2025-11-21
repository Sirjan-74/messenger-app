

(function () {
  'use strict';

  // ----- DOM helpers -----
  function $(sel, ctx=document) { return ctx.querySelector(sel); }
  function $all(sel, ctx=document) { return Array.from(ctx.querySelectorAll(sel)); }
  function escapeHtml(text) {
    const d = document.createElement('div');
    d.textContent = text;
    return d.innerHTML;
  }

  // ----- Core state -----
  const container = document.querySelector('.chat-container');
  if (!container) {
    console.warn('chat.js: chat container not found, aborting');
    return;
  }
  let room = container.dataset.room || '';
  const messagesContainer = document.getElementById('messagesContainer');
  const currentUser = window.CURRENT_USER || null; // optional; server-side session is authoritative
  let socket = null;
  let selectMode = false;
  let selectedIds = new Set();
  let editingMsgId = null;

  // Buttons / controls
  const selectModeBtn = document.getElementById('selectModeBtn');
  const selectAllBtn = document.getElementById('selectAllBtn');
  const deleteSelectedBtn = document.getElementById('deleteSelectedBtn');
  const clearChatBtn = document.getElementById('clearChatBtn');
  const messageInput = document.getElementById('messageInput');
  const sendBtn = document.getElementById('sendBtn');
  const fileInput = document.getElementById('fileInput');
  const voiceBtn = document.getElementById('voiceBtn');
  const editBackdrop = document.getElementById('editModalBackdrop');
  const editInput = document.getElementById('editInput');
  const cancelEditBtn = document.getElementById('cancelEditBtn');
  const saveEditBtn = document.getElementById('saveEditBtn');

  // ----- Socket.IO connection -----
  function initSocket() {
    if (typeof io === 'undefined') {
      console.error('Socket.IO not loaded. Add <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script> before chat.js');
      return;
    }
    socket = io();

    socket.on('connect', () => {
      console.log('socket connected:', socket.id);
      socket.emit('join_room', { room });
      document.getElementById('status') && (document.getElementById('status').textContent = 'Connected');
    });

    socket.on('disconnect', () => {
      document.getElementById('status') && (document.getElementById('status').textContent = 'Reconnecting...');
    });

    socket.on('room_migrated', data => {
      if (data && data.room) {
        console.log('room migrated ->', data.room);
        room = data.room;
        if (container) container.dataset.room = room;
      }
    });

    socket.on('new_message', data => {
      appendMessageToDOM(data);
      if (isAtBottom(messagesContainer, 60)) {
        messagesContainer.scrollTop = messagesContainer.scrollHeight;
      }
    });

    socket.on('message_edited', data => {
      const el = document.querySelector(`.message[data-msg-id="${data.message_id}"] .msg-text`);
      if (el) el.textContent = data.new_text;
    });

    socket.on('message_deleted', data => {
      const node = document.querySelector(`.message[data-msg-id="${data.message_id}"]`);
      if (node) node.remove();
    });

    socket.on('messages_deleted', data => {
      (data.message_ids || []).forEach(id => {
        const node = document.querySelector(`.message[data-msg-id="${id}"]`);
        if (node) node.remove();
      });
    });

    socket.on('chat_cleared', data => {
      $all('.message').forEach(m => m.remove());
    });

    // typing / presence handlers (optional)
    socket.on('user_typing', d => {
      // implement UI indicator if desired
    });
    socket.on('user_stopped_typing', d => {
      // implement UI indicator if desired
    });
  }

  // ----- DOM helpers for messages -----
  function isAtBottom(container, threshold = 40) {
    return container.scrollTop + container.clientHeight >= container.scrollHeight - threshold;
  }

  function formatTime(ts) {
    try {
      const d = ts ? new Date(ts) : new Date();
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch(e) { return ''; }
  }

  function appendMessageToDOM(data) {
    const wrapper = document.createElement('div');
    wrapper.className = 'message ' + (data.author_username === currentUser ? 'own' : '');
    wrapper.dataset.msgId = data._id || ('m' + Date.now());

    const ts = formatTime(data.timestamp);
    let contentHtml = '';
    if (data.message_type === 'file' && data.file_info) {
      // adapt file display
      if (data.file_info.file_type && data.file_info.file_type.startsWith('image')) {
        contentHtml = `<div class="image-message"><img src="${escapeHtml(data.file_info.file_path || data.file_info.file_url)}" style="max-width:100%; border-radius: 8px;" /></div>`;
      } else {
        contentHtml = `<div class="msg-text">${escapeHtml(data.text || 'Shared a file')}</div>`;
      }
    } else if (data.message_type === 'voice' && data.voice_info) {
      contentHtml = `<div class="audio-player" data-audio-id="audio_${Date.now()}">
          <button class="audio-play-btn" onclick="(function(id,src){const a=document.getElementById(id); if(!a){const audio=new Audio(src); audio.id=id; audio.play(); setTimeout(()=>audio.remove(),(audio.duration||5)*1000);} })( 'audio_${Date.now()}','${escapeHtml(data.voice_info.file_path||data.voice_info.file_url)}')"><i class="fas fa-play"></i></button>
          <div style="display:inline-block; margin-left:8px;">${escapeHtml(data.text || 'Voice message')}</div>
        </div>`;
    } else {
      contentHtml = `<div class="msg-text">${escapeHtml(data.text || '')}</div>`;
    }

    wrapper.innerHTML = `
      <div class="message-bubble">
        <div style="display:flex; align-items:flex-start; gap:8px;">
          <div style="flex:1;">
            ${data.author_username && data.author_username !== currentUser ? `<div class="message-author">${escapeHtml(data.author_username)}</div>` : ''}
            ${contentHtml}
            <div class="msg-ts">${escapeHtml(ts)}</div>
          </div>
          <div class="msg-actions">
            <div class="select-checkbox" data-selected="false" title="Select message" style="display:none;"></div>
            <div class="msg-menu-btn"><i class="fas fa-ellipsis-h"></i></div>
          </div>
          <div class="dropdown" role="menu">
            <button data-action="reply"><i class="fas fa-reply"></i> Reply</button>
            <button data-action="edit"><i class="fas fa-edit"></i> Edit</button>
            <button data-action="delete-me"><i class="fas fa-trash"></i> Delete (for me)</button>
            <button data-action="delete-all"><i class="fas fa-user-times"></i> Delete for everyone</button>
            <button data-action="select"><i class="fas fa-check-circle"></i> Select</button>
          </div>
        </div>
      </div>
    `;
    messagesContainer.appendChild(wrapper);
  }

  // ----- Select mode functions -----
  function setSelectMode(on) {
    selectMode = !!on;
    if (selectModeBtn) selectModeBtn.textContent = selectMode ? 'Cancel' : 'Select';
    $all('.select-checkbox').forEach(el => el.style.display = selectMode ? 'inline-flex' : 'none');
    if (!selectMode) {
      selectedIds.clear();
      $all('.select-checkbox').forEach(el => {
        el.classList.remove('checked');
        el.dataset.selected = 'false';
      });
    }
    updateButtonStates();
  }

  function toggleMsgSelect(msgId, checkboxEl) {
    const isSelected = checkboxEl.dataset.selected === 'true';
    if (isSelected) {
      checkboxEl.dataset.selected = 'false';
      checkboxEl.classList.remove('checked');
      selectedIds.delete(msgId);
    } else {
      checkboxEl.dataset.selected = 'true';
      checkboxEl.classList.add('checked');
      selectedIds.add(msgId);
    }
    updateButtonStates();
  }

  function updateButtonStates() {
    if (deleteSelectedBtn) deleteSelectedBtn.disabled = selectedIds.size === 0;
  }

  // ----- Menu actions -----
  function handleMenuAction(action, msgEl) {
    const msgId = msgEl.dataset.msgId;
    const msgText = msgEl.querySelector('.msg-text')?.textContent || '';
    switch(action) {
      case 'reply':
        messageInput.value = `Replying to: "${msgText.substring(0,50)}..."\\n`;
        messageInput.focus();
        break;
      case 'edit':
        openEditModal(msgId, msgText);
        break;
      case 'delete-me':
        deleteForMe(msgId);
        break;
      case 'delete-all':
        deleteEveryone(msgId);
        break;
      case 'select':
        setSelectMode(true);
        const checkbox = msgEl.querySelector('.select-checkbox');
        if (checkbox) toggleMsgSelect(msgId, checkbox);
        break;
    }
  }

  // ----- Edit modal -----
  function openEditModal(msgId, currentText) {
    editingMsgId = msgId;
    if (editInput) editInput.value = currentText || '';
    if (editBackdrop) editBackdrop.classList.add('show');
  }
  function closeEditModal() {
    editingMsgId = null;
    if (editBackdrop) editBackdrop.classList.remove('show');
  }

  // ----- Delete / Clear functions (use fetch to your endpoints) -----
  async function deleteForMe(msgId) {
    if (!confirm('Delete this message for you?')) return;
    try {
      const res = await fetch('/delete_message', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ message_ids: [msgId] })
      });
      const data = await res.json();
      if (data.success) {
        const node = document.querySelector(`.message[data-msg-id="${msgId}"]`);
        if (node) node.remove();
        socket && socket.emit('message_deleted', { room, message_id: msgId });
      } else {
        alert('Delete failed: ' + (data.message || ''));
      }
    } catch(e) {
      console.error(e);
      alert('Delete request failed');
    }
  }

  async function deleteEveryone(msgId) {
    if (!confirm('Delete this message for everyone?')) return;
    try {
      const res = await fetch('/delete_everyone', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ message_id: msgId })
      });
      const data = await res.json();
      if (data.success) {
        const node = document.querySelector(`.message[data-msg-id="${msgId}"]`);
        if (node) node.remove();
        socket && socket.emit('message_deleted_everyone', { room, message_id: msgId });
      } else {
        alert('Delete for everyone failed: ' + (data.message || ''));
      }
    } catch(e) {
      console.error(e);
      alert('Delete everyone request failed');
    }
  }

  deleteSelectedBtn && deleteSelectedBtn.addEventListener('click', async () => {
    if (selectedIds.size === 0) return;
    if (!confirm(`Delete ${selectedIds.size} selected message(s)?`)) return;
    const ids = Array.from(selectedIds);
    try {
      const res = await fetch('/delete_message', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ message_ids: ids })
      });
      const data = await res.json();
      if (data.success) {
        ids.forEach(id => {
          const node = document.querySelector(`.message[data-msg-id="${id}"]`);
          if (node) node.remove();
        });
        selectedIds.clear();
        setSelectMode(false);
        socket && socket.emit('messages_deleted', { room, message_ids: ids });
      } else {
        alert('Delete failed: ' + (data.message || ''));
      }
    } catch(e) {
      console.error(e);
      alert('Delete request failed');
    }
  });

  clearChatBtn && clearChatBtn.addEventListener('click', async () => {
    if (!confirm('Clear entire chat history?')) return;
    try {
      const res = await fetch('/clear_chat', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ room })
      });
      const data = await res.json();
      if (data.success) {
        $all('.message').forEach(m => m.remove());
        socket && socket.emit('chat_cleared', { room });
      } else {
        alert('Clear failed: ' + (data.message || ''));
      }
    } catch(e) {
      console.error(e);
      alert('Clear request failed');
    }
  });

  // ----- File upload -----
  fileInput && fileInput.addEventListener('change', function(e) {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    fd.append('room', room);
    fetch('/upload', { method: 'POST', body: fd })
      .then(r => r.json())
      .then(data => {
        if (!data.success) alert('Upload failed: ' + (data.message || ''));
      })
      .catch(err => {
        console.error(err);
        alert('Upload failed');
      })
      .finally(() => e.target.value = '');
  });

  // ----- Send message -----
  function sendMessage() {
    const text = messageInput.value.trim();
    if (!text) return;
    socket && socket.emit('send_message', { message: text, room });
    messageInput.value = '';
  }
  sendBtn && sendBtn.addEventListener('click', sendMessage);
  messageInput && messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // ----- Dropdown / message actions delegation -----
  messagesContainer && messagesContainer.addEventListener('click', (e) => {
    const menuBtn = e.target.closest('.msg-menu-btn');
    if (menuBtn) {
      e.stopPropagation();
      closeAllDropdowns();
      const dropdown = menuBtn.parentElement.parentElement.querySelector('.dropdown');
      if (dropdown) {
        dropdown.classList.toggle('show');
      }
      return;
    }

    const checkbox = e.target.closest('.select-checkbox');
    if (checkbox) {
      e.stopPropagation();
      const msgId = checkbox.closest('.message').dataset.msgId;
      toggleMsgSelect(msgId, checkbox);
      return;
    }

    const dropdownBtn = e.target.closest('.dropdown button');
    if (dropdownBtn) {
      e.stopPropagation();
      const action = dropdownBtn.dataset.action;
      const msgEl = dropdownBtn.closest('.message');
      handleMenuAction(action, msgEl);
      closeAllDropdowns();
      return;
    }

    // Click outside dropdown closes them
    closeAllDropdowns();
  });

  function closeAllDropdowns() {
    $all('.dropdown').forEach(d => d.classList.remove('show'));
  }

  // Select mode button
  selectModeBtn && selectModeBtn.addEventListener('click', () => {
    setSelectMode(!selectMode);
  });

  // Select All
  selectAllBtn && selectAllBtn.addEventListener('click', () => {
    const checkboxes = Array.from(document.querySelectorAll('.select-checkbox'));
    const allSelected = checkboxes.every(ch => ch.dataset.selected === 'true');
    checkboxes.forEach(ch => {
      const msgId = ch.closest('.message').dataset.msgId;
      if (allSelected) {
        ch.dataset.selected = 'false';
        ch.classList.remove('checked');
        selectedIds.delete(msgId);
      } else {
        ch.dataset.selected = 'true';
        ch.classList.add('checked');
        selectedIds.add(msgId);
      }
    });
    updateButtonStates();
  });

  // Edit modal buttons
  cancelEditBtn && cancelEditBtn.addEventListener('click', closeEditModal);
  saveEditBtn && saveEditBtn.addEventListener('click', async () => {
    const newText = editInput.value.trim();
    if (!editingMsgId || !newText) {
      alert('Message cannot be empty');
      return;
    }
    try {
      const res = await fetch('/edit_message', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ message_id: editingMsgId, new_text: newText })
      });
      const data = await res.json();
      if (data.success) {
        const msgEl = document.querySelector(`.message[data-msg-id="${editingMsgId}"] .msg-text`);
        if (msgEl) msgEl.textContent = newText;
        socket && socket.emit('message_edited', { room, message_id: editingMsgId, new_text: newText });
        closeEditModal();
      } else {
        alert('Edit failed: ' + (data.message || 'unknown'));
      }
    } catch(e) {
      console.error(e);
      alert('Edit request failed');
    }
  });

  // Close dropdowns when clicking elsewhere
  document.addEventListener('click', closeAllDropdowns);

  // ----- Initialization -----
  function init() {
    // hide select checkboxes initially
    $all('.select-checkbox').forEach(el => el.style.display = 'none');
    if (deleteSelectedBtn) deleteSelectedBtn.disabled = true;
    // scroll to bottom
    messagesContainer && (messagesContainer.scrollTop = messagesContainer.scrollHeight);
    initSocket();
  }

  // Start
  init();

  // Expose a tiny API for debugging
  window.__chat = {
    joinRoom: (r) => { room = r; socket && socket.emit('join_room', { room }); },
    leaveRoom: () => { socket && socket.emit('leave_room', { room }); },
    socketId: () => socket && socket.id
  };

})();
