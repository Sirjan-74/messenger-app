// Add this script directly to the bottom of your chat.html template
// Voice recording vars
let mediaRecorder = null;
let audioChunks = [];
let recordingStartTime = 0;
let durationInterval = null;
const maxDuration = 60; // 1 min limit

// DOM elements
const inputField = document.querySelector('.input-field');
const sendBtn = document.querySelector('.send-btn');
const voiceRecordingEl = document.querySelector('.voice-recording');
const durationEl = document.querySelector('.duration');
const cancelBtn = document.querySelector('.cancel');
const sendVoiceBtn = document.querySelector('.send'); // Your Send in voice-controls
const waveformWaves = document.querySelectorAll('.wave'); // For animation

// Start recording on long-press mic or click (adapt to your trigger, e.g., mic icon click)
function startRecording() {
  navigator.mediaDevices.getUserMedia({ audio: true })
    .then(stream => {
      mediaRecorder = new MediaRecorder(stream);
      audioChunks = [];
      recordingStartTime = Date.now();

      mediaRecorder.ondataavailable = e => audioChunks.push(e.data);
      mediaRecorder.onstop = () => {
        const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
        const duration = (Date.now() - recordingStartTime) / 1000;
        sendVoiceMessage(audioBlob, duration);
        stream.getTracks().forEach(track => track.stop()); // Stop mic
      };

      // Animate waveform (simple pulse)
      function animateWave() {
        waveformWaves.forEach((wave, i) => {
          wave.style.height = `${20 + Math.random() * 30}px`; // Random height for effect
        });
      }
      durationInterval = setInterval(() => {
        const elapsed = Math.floor((Date.now() - recordingStartTime) / 1000);
        durationEl.textContent = formatTime(elapsed);
        animateWave();
        if (elapsed >= maxDuration) stopRecording();
      }, 100);

      mediaRecorder.start();
      voiceRecordingEl.style.display = 'block'; // Show recording UI
      inputField.style.display = 'none'; // Hide text input
      sendBtn.disabled = true;
    })
    .catch(err => console.error('Mic access denied:', err));
}

// Format time as MM:SS
function formatTime(seconds) {
  const mins = Math.floor(seconds / 60).toString().padStart(2, '0');
  const secs = (seconds % 60).toString().padStart(2, '0');
  return `${mins}:${secs}`;
}

// Stop on Cancel or Send
cancelBtn.addEventListener('click', () => {
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    stopRecording();
    voiceRecordingEl.style.display = 'none';
    inputField.style.display = 'block';
    sendBtn.disabled = false;
  }
});

sendVoiceBtn.addEventListener('click', () => {
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    stopRecording();
    // Don't hide UI yet—wait for send confirmation
  }
});

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
    clearInterval(durationInterval);
  }
}

// Send voice as form data
function sendVoiceMessage(blob, duration) {
  const formData = new FormData();
  formData.append('voice_blob', blob, 'voice.webm');
  formData.append('message_type', 'voice');
  formData.append('duration', duration);
  formData.append('room', '{{ room }}'); // From template

  fetch('/send_message', { // Your endpoint
    method: 'POST',
    body: formData
  })
  .then(response => response.json())
  .then(data => {
    if (data.success) {
      // Append to messages list (simulate via template reload or WebSocket)
      appendMessage({ message_type: 'voice', voice_info: { duration }, author_username: '{{ user.username }}' });
      voiceRecordingEl.style.display = 'none';
      inputField.style.display = 'block';
      inputField.value = '';
      sendBtn.disabled = false;
    } else {
      alert('Failed to send voice message');
    }
  })
  .catch(err => console.error('Send error:', err));
}

// Trigger start (e.g., on mic icon click)
document.querySelector('.mic-icon').addEventListener('click', startRecording); // Add class="mic-icon" to your mic button

// For text send (existing, but ensure it disables during voice)
sendBtn.addEventListener('click', () => {
  if (inputField.value.trim() && !sendBtn.disabled) {
    // Your existing text send logic
  }
});
document.addEventListener('DOMContentLoaded', function() {
    const socket = io();
    const messageInput = document.getElementById('msg_input');
    const sendButton = document.getElementById('send_btn');
    const messagesContainer = document.getElementById('messages-box');
    const currentRoom = new URLSearchParams(window.location.search).get('room');

    // Join the room when page loads
    if (currentRoom) {
        socket.emit('join_room', { room: currentRoom });
    }

    // Send message function
    function sendMessage() {
        const message = messageInput.value.trim();
        if (message === '' || !currentRoom) return;

        // Send message to server
        socket.emit('send_message', {
            message: message,
            room: currentRoom
        });

        // Clear input
        messageInput.value = '';
        autoResizeTextarea();
    }

    // Event listeners
    sendButton.addEventListener('click', sendMessage);
    
    messageInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    // Auto-resize textarea
    function autoResizeTextarea() {
        messageInput.style.height = 'auto';
        messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + 'px';
    }

    messageInput.addEventListener('input', autoResizeTextarea);

    // Listen for new messages
    socket.on('new_message', function(data) {
        displayMessage(data);
        scrollToBottom();
    });

    // Display message function
    function displayMessage(messageData) {
        const messageDiv = document.createElement('div');
        const isOwnMessage = messageData.author_username === getCurrentUsername();
        messageDiv.className = `message ${isOwnMessage ? 'own' : ''}`;

        const bubbleDiv = document.createElement('div');
        bubbleDiv.className = 'message-bubble';

        let messageHTML = '';
        if (!isOwnMessage) {
            messageHTML += `<strong>${messageData.author_username}</strong>`;
        }
        
        messageHTML += `${messageData.text}`;
        
        // Add timestamp
        const timestamp = new Date(messageData.timestamp || Date.now());
        const timeString = timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        
        messageHTML += `<div class="message-status">
            ${timeString}
            ${isOwnMessage ? '<i class="fas fa-check-double status-icon"></i>' : ''}
        </div>`;

        bubbleDiv.innerHTML = messageHTML;
        messageDiv.appendChild(bubbleDiv);
        messagesContainer.appendChild(messageDiv);
    }

    // Get current username
    function getCurrentUsername() {
        const userElement = document.querySelector('.chat-sidebar p');
        return userElement ? userElement.textContent.trim() : '';
    }

    // Scroll to bottom
    function scrollToBottom() {
        setTimeout(() => {
            messagesContainer.scrollTop = messagesContainer.scrollHeight;
        }, 100);
    }

    // Connection status
    socket.on('connect', function() {
        console.log('Connected to server');
        document.getElementById('connectionStatus').className = 'connection-status connected';
        document.getElementById('connectionStatus').innerHTML = '<i class="fas fa-circle"></i> Connected';
    });

    socket.on('disconnect', function() {
        console.log('Disconnected from server');
        document.getElementById('connectionStatus').className = 'connection-status disconnected';
        document.getElementById('connectionStatus').innerHTML = '<i class="fas fa-circle"></i> Disconnected';
    });

    // Initial scroll to bottom
    scrollToBottom();
});
