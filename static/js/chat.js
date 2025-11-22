(function () {
  'use strict';

  function $(sel, ctx = document) { return ctx.querySelector(sel); }
  function $all(sel, ctx = document) { return Array.from(ctx.querySelectorAll(sel)); }
  function escapeHtml(text) {
    const d = document.createElement('div');
    d.textContent = text;
    return d.innerHTML;
  }

  const container = document.querySelector('.chat-container');
  if (!container) return;

  let room = container.dataset.room || '';
  const messagesContainer = $('#messagesContainer');
  const CURRENT_USER = window.CURRENT_USER || null;

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

  function initSocket() {
    if (typeof io === "undefined") return;

    socket = io(window.location.origin, {
      transports: ["websocket", "polling"],
      withCredentials: true
    });

    socket.on("connect", () => {
      socket.emit("join_room", { room });
      $("#status").textContent = "Connected";
    });

    socket.on("disconnect", () => {
      $("#status").textContent = "Reconnecting...";
    });

    socket.on("new_message", (data) => {
      appendMessageToDOM(data);
      scrollToBottom();
    });

    socket.on("message_edited", d => {
      const el = document.querySelector(`.message[data-msg-id="${d.message_id}"] .msg-text`);
      if (el) el.textContent = d.new_text;
    });

    socket.on("message_deleted", d => {
      const node = document.querySelector(`.message[data-msg-id="${d.message_id}"]`);
      if (node) node.remove();
    });

    socket.on("messages_deleted", d => {
      (d.message_ids || []).forEach(id => {
        const node = document.querySelector(`.message[data-msg-id="${id}"]`);
        if (node) node.remove();
      });
    });

    socket.on("chat_cleared", () => {
      $all(".message").forEach(m => m.remove());
    });
  }

  function scrollToBottom() {
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
  }

  function formatTime(ts) {
    try {
      const d = ts ? new Date(ts) : new Date();
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch (e) { return ''; }
  }

  function appendMessageToDOM(data) {
    const id = data._id || "m" + Date.now();
    const wrapper = document.createElement("div");

    wrapper.className = "message " + (data.author_username == CURRENT_USER ? "own" : "");
    wrapper.dataset.msgId = id;

    let contentHtml = "";
    const ts = formatTime(data.timestamp);

    if (data.message_type === "file" && data.file_info) {
      if (data.file_info.file_type.startsWith("image")) {
        contentHtml = `<div class="image-message"><img src="${data.file_info.file_path}" style="max-width:100%;border-radius:8px;"></div>`;
      } else {
        contentHtml = `<div class="msg-text">📎 Shared a file</div>`;
      }
    }
    else if (data.message_type === "voice") {
      contentHtml = `
        <div class="audio-player">
          <button onclick="new Audio('${data.voice_info.file_path}').play()"><i class="fas fa-play"></i></button>
          <span>${escapeHtml(data.text)}</span>
        </div>`;
    }
    else {
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
  }

  function closeAllDropdowns() {
    $all(".dropdown").forEach(d => d.classList.remove("show"));
  }

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
    await fetch("/delete_message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message_ids: [id] }),
    });
    document.querySelector(`.message[data-msg-id="${id}"]`)?.remove();
  }

  async function deleteEveryone(id) {
    await fetch("/delete_everyone", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message_id: id }),
    });
    document.querySelector(`.message[data-msg-id="${id}"]`)?.remove();
  }

  deleteSelectedBtn.addEventListener("click", async () => {
    const ids = [...selectedIds];
    await fetch("/delete_message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message_ids: ids }),
    });
    ids.forEach(id => {
      document.querySelector(`.message[data-msg-id="${id}"]`)?.remove();
    });
    setSelectMode(false);
  });

  clearChatBtn.addEventListener("click", async () => {
    await fetch("/clear_chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ room }),
    });
    $all(".message").forEach(m => m.remove());
  });

  fileInput.addEventListener("change", e => {
    const file = e.target.files[0];
    if (!file) return;

    const fd = new FormData();
    fd.append("file", file);
    fd.append("room", room);

    fetch("/upload", { method: "POST", body: fd })
      .finally(() => e.target.value = "");
  });

  function sendMessage() {
    const txt = messageInput.value.trim();
    if (!txt) return;
    socket.emit("send_message", { message: txt, room });
    messageInput.value = "";
  }

  sendBtn.addEventListener("click", sendMessage);
  messageInput.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  cancelEditBtn.onclick = () => editBackdrop.classList.remove("show");

  saveEditBtn.onclick = async () => {
    const newText = editInput.value.trim();
    await fetch("/edit_message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message_id: editingMsgId, new_text: newText }),
    });

    const el = document.querySelector(`.message[data-msg-id="${editingMsgId}"] .msg-text`);
    if (el) el.textContent = newText;

    editBackdrop.classList.remove("show");
  };

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

  initSocket();
  scrollToBottom();

})();
