/**
 * Seller Panel JavaScript
 */

document.addEventListener('DOMContentLoaded', function() {
    console.log('Seller panel initialized');
    
    // Initialize seller-specific functionality
    initSellerPanel();
});

function initSellerPanel() {
    // Auto-load chat if URL has query parameters with partner info
    initChatFromUrl();
    
    // Set up dialog click handlers
    initDialogHandlers();
    
    // Set up popstate handler for browser back/forward
    initPopStateHandler();
}

function initChatFromUrl() {
    var params = new URLSearchParams(window.location.search);
    var partnerType = params.get('partner_type');
    var partnerId = params.get('partner_id');
    
    if (partnerType && partnerId) {
        // Find and activate the matching dialog
        var dialogItems = document.querySelectorAll('.dialog-item');
        var targetDialog = document.querySelector('.dialog-item[data-partner-type="' + partnerType + '"][data-partner-id="' + partnerId + '"]');
        if (targetDialog) {
            dialogItems.forEach(function(d) { d.classList.remove('active'); });
            targetDialog.classList.add('active');
        }
        // Load the chat
        loadChat(partnerType, partnerId);
    }
}

function initDialogHandlers() {
    var dialogItems = document.querySelectorAll('.dialog-item');
    var chatContainer = document.getElementById('chatContainer');
    
    dialogItems.forEach(function(item) {
        item.addEventListener('click', function(e) {
            e.preventDefault();
            
            var partnerType = this.dataset.partnerType;
            var partnerId = this.dataset.partnerId;
            var url = '/messages/' + partnerType + '/' + partnerId;
            
            history.pushState({}, '', url);
            loadChat(partnerType, partnerId);
            
            dialogItems.forEach(function(d) { d.classList.remove('active'); });
            this.classList.add('active');
        });
    });
}

function initPopStateHandler() {
    var dialogItems = document.querySelectorAll('.dialog-item');
    var chatContainer = document.getElementById('chatContainer');
    
    window.addEventListener('popstate', function() {
        var path = window.location.pathname;
        var match = path.match(/^\/messages\/(\w+)\/(\d+)$/);
        if (match) {
            var partnerType = match[1];
            var partnerId = match[2];
            loadChat(partnerType, partnerId);
            
            dialogItems.forEach(function(d) {
                if (d.dataset.partnerType === partnerType && d.dataset.partnerId === partnerId) {
                    d.classList.add('active');
                } else {
                    d.classList.remove('active');
                }
            });
        } else {
            stopPolling();
            chatContainer.innerHTML = '<div class="no-dialog-selected"><div class="empty-state"><i class="bi bi-chat-dots" style="font-size: 64px; opacity: 0.3;"></i><h3>Выберите диалог</h3><p>Выберите диалог из списка слева для просмотра сообщений</p></div></div>';
        }
    });
}

// Global CSRF token
var csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';

// Global variables for polling
var currentPartnerType = null;
var currentPartnerId = null;
var lastMessageId = 0;
var pollingInterval = null;

// Attachment handling variables
var pendingImage = null;
var pendingFile = null;
var isUploading = false;

// ============ ATTACHMENT HANDLING ============

function handleFileSelect(file) {
    console.log('[DEBUG] handleFileSelect called with:', file);
    if (!file) {
        console.log('[DEBUG] No file provided');
        return;
    }
    
    console.log('[DEBUG] File details:', file.name, file.type, file.size);
    
    var allowedTypes = ['image/jpeg', 'image/png', 'image/gif', 'image/webp', 'application/pdf'];
    if (allowedTypes.indexOf(file.type) === -1) {
        console.log('[DEBUG] Invalid file type:', file.type);
        alert('Разрешены только изображения (JPEG, PNG, GIF, WebP) и PDF файлы');
        return;
    }
    
    if (file.size > 10 * 1024 * 1024) {
        console.log('[DEBUG] File too large:', file.size);
        alert('Файл слишком большой (максимум 10 МБ)');
        return;
    }
    
    console.log('[DEBUG] Creating FormData and uploading...');
    var formData = new FormData();
    formData.append('file', file);
    
    isUploading = true;
    
    // Get fresh CSRF token from form
    var freshCsrfToken = document.querySelector('input[name="csrf_token"]');
    console.log('[DEBUG] CSRF token found:', freshCsrfToken ? freshCsrfToken.value.substring(0, 20) + '...' : 'NOT FOUND');
    var tokenToUse = freshCsrfToken ? freshCsrfToken.value : csrfToken;
    
    console.log('[DEBUG] Sending fetch to /api/upload-message-file');
    fetch('/api/upload-message-file', {
        method: 'POST',
        body: formData,
        credentials: 'include',
        headers: {
            'X-CSRFToken': tokenToUse
        }
    })
    .then(function(response) {
        console.log('[DEBUG] Response status:', response.status);
        return response.json();
    })
    .then(function(data) {
        console.log('[DEBUG] Response data:', data);
        isUploading = false;
        if (data.success) {
            if (data.is_image) {
                pendingImage = data;
                var imagePathInput = document.getElementById('imagePathInput');
                if (imagePathInput) imagePathInput.value = data.path;
                addAttachmentPreview(data.path, data.filename, 'image');
            } else {
                pendingFile = data;
                var filePathInput = document.getElementById('filePathInput');
                if (filePathInput) filePathInput.value = data.path;
                addAttachmentPreview(data.path, data.filename, 'pdf');
            }
        } else {
            alert(data.error || 'Ошибка загрузки файла');
        }
    })
    .catch(function(error) {
        isUploading = false;
        console.error('Upload error:', error);
        alert('Ошибка загрузки файла');
    });
}

function addAttachmentPreview(path, filename, type) {
    var preview = document.getElementById('attachmentPreview');
    if (!preview) return;
    
    var item = document.createElement('div');
    item.className = 'attachment-item';
    item.dataset.path = path;
    item.dataset.type = type;
    
    if (type === 'image') {
        item.innerHTML = '<img src="/static/' + path + '" alt="Preview">' +
            '<span class="file-name">' + escapeHtml(filename) + '</span>' +
            '<button type="button" class="remove-btn" onclick="removeAttachment(this)">×</button>';
    } else {
        item.innerHTML = '<svg class="file-icon" xmlns="http://www.w3.org/2000/svg" width="24" height="24" fill="currentColor" viewBox="0 0 16 16">' +
            '<path d="M4 0a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V2a2 2 0 0 0-2-2h-2.5a.5.5 0 0 1-.5-.5v-.5H4z"/>' +
            '<path d="M4.5 1.5A.5.5 0 0 1 5 1h4.793L10.293 1.5A.5.5 0 0 1 11 2H5a.5.5 0 0 1-.5-.5v-.5a.5.5 0 0 0-.5-.5H5a.5.5 0 0 0-.5.5v.5H4a.5.5 0 0 0-.5.5v-.5z"/></svg>' +
            '<span class="file-name">' + escapeHtml(filename) + '</span>' +
            '<button type="button" class="remove-btn" onclick="removeAttachment(this)">×</button>';
    }
    
    preview.appendChild(item);
}

function removeAttachment(btn) {
    var item = btn.closest('.attachment-item');
    var path = item.dataset.path;
    var type = item.dataset.type;
    
    if (type === 'image') {
        pendingImage = null;
        var imagePathInput = document.getElementById('imagePathInput');
        if (imagePathInput) imagePathInput.value = '';
    } else {
        pendingFile = null;
        var filePathInput = document.getElementById('filePathInput');
        if (filePathInput) filePathInput.value = '';
    }
    
    item.remove();
}

// ============ MESSAGE FORM HANDLING ============

function initMessageForm() {
    var form = document.getElementById('messageForm');
    if (!form) {
        console.log('[DEBUG] messageForm not found');
        return;
    }
    
    form.removeEventListener('submit', handleFormSubmit);
    form.addEventListener('submit', handleFormSubmit);
    console.log('[DEBUG] Form handler attached');
}

function handleFormSubmit(e) {
    e.preventDefault();
    
    var form = e.target;
    console.log('[DEBUG] handleFormSubmit called, form:', form.id);
    var textarea = form.querySelector('textarea');
    var text = textarea.value.trim();
    var imagePath = document.getElementById('imagePathInput') ? document.getElementById('imagePathInput').value : '';
    var filePath = document.getElementById('filePathInput') ? document.getElementById('filePathInput').value : '';
    
    if (!text && !imagePath && !filePath) {
        alert('Добавьте текст сообщения или вложение');
        return;
    }
    
    if (isUploading) {
        alert('Дождитесь загрузки файла');
        return;
    }
    
    var receiverType = form.querySelector('[name="receiver_type"]').value;
    var receiverId = parseInt(form.querySelector('[name="receiver_id"]').value);
    
    var formData = new FormData();
    formData.append('receiver_type', receiverType);
    formData.append('receiver_id', receiverId);
    formData.append('text', text);
    formData.append('csrf_token', csrfToken);
    if (imagePath) formData.append('image_path', imagePath);
    if (filePath) formData.append('file_path', filePath);
    
    console.log('[DEBUG] Sending to /messages/send with:', {receiverType, receiverId, text, imagePath, filePath});
    
    fetch('/messages/send', {
        method: 'POST',
        body: formData
    })
    .then(function(response) { 
        console.log('[DEBUG] send response status:', response.status);
        return response.json(); 
    })
    .then(function(data) {
        console.log('[DEBUG] send response data:', data);
        if (data.success) {
            console.log('[DEBUG] Success, clearing form and reloading chat...');
            textarea.value = '';
            var preview = document.getElementById('attachmentPreview');
            if (preview) preview.innerHTML = '';
            var imagePathInput = document.getElementById('imagePathInput');
            var filePathInput = document.getElementById('filePathInput');
            if (imagePathInput) imagePathInput.value = '';
            if (filePathInput) filePathInput.value = '';
            pendingImage = null;
            pendingFile = null;
            
            // Reload chat
            console.log('[DEBUG] Calling loadChat for', receiverType, receiverId);
            loadChat(receiverType, receiverId);
        } else {
            alert(data.error || 'Ошибка отправки');
        }
    })
    .catch(function(error) {
        console.error('Error:', error);
        alert('Ошибка отправки');
    });
    
    return false;
}

// ============ UTILITY FUNCTIONS ============

function escapeHtml(text) {
    var div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ============ POLLING FUNCTIONS ============

function startPolling(partnerType, partnerId) {
    stopPolling();
    
    currentPartnerType = partnerType;
    currentPartnerId = partnerId;
    lastMessageId = 0;
    
    var messagesList = document.getElementById('messagesList');
    if (messagesList) {
        var messageElements = messagesList.querySelectorAll('.message');
        if (messageElements.length > 0) {
            var lastElement = messageElements[messageElements.length - 1];
            lastMessageId = parseInt(lastElement.dataset.messageId) || 0;
        }
    }
    
    pollingInterval = setInterval(checkNewMessages, 3000);
}

function stopPolling() {
    if (pollingInterval) {
        clearInterval(pollingInterval);
        pollingInterval = null;
    }
    currentPartnerType = null;
    currentPartnerId = null;
    lastMessageId = 0;
}

function checkNewMessages() {
    if (!currentPartnerType || !currentPartnerId) return;
    
    fetch('/messages/' + currentPartnerType + '/' + currentPartnerId + '/new?last_id=' + lastMessageId, {
        headers: {
            'X-CSRFToken': csrfToken
        }
    })
    .then(function(response) { return response.json(); })
    .then(function(data) {
        if (data.success && data.messages && data.messages.length > 0) {
            appendNewMessages(data.messages);
        }
    })
    .catch(function(error) {
        console.error('Error checking new messages:', error);
    });
}

function appendNewMessages(messages) {
    var messagesList = document.getElementById('messagesList');
    if (!messagesList) return;
    
    var isAtBottom = messagesList.scrollTop + messagesList.clientHeight >= messagesList.scrollHeight - 50;
    
    messages.forEach(function(msg) {
        if (document.querySelector('[data-message-id="' + msg.id + '"]')) {
            return;
        }
        
        var messageDiv = document.createElement('div');
        var isOutgoing = msg.is_outgoing;
        
        messageDiv.className = 'message ' + (isOutgoing ? 'message-out' : 'message-in');
        messageDiv.dataset.messageId = msg.id;
        
        var time = msg.timestamp ? new Date(msg.timestamp).toLocaleTimeString('ru-RU', {hour: '2-digit', minute:'2-digit'}) : '';
        
        var attachmentsHtml = '';
        if (msg.image_path) {
            attachmentsHtml += '<div class="message-attachment message-image"><a href="/static/' + msg.image_path + '" target="_blank"><img src="/static/' + msg.image_path + '" alt="Изображение"></a></div>';
        }
        if (msg.file_path) {
            attachmentsHtml += '<div class="message-attachment message-file"><a href="/static/' + msg.file_path + '" target="_blank" class="file-link"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" fill="currentColor" viewBox="0 0 16 16"><path d="M4 0a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V2a2 2 0 0 0-2-2h-2.5a.5.5 0 0 1-.5-.5v-.5H4z"/><path d="M4.5 1.5A.5.5 0 0 1 5 1h4.793L10.293 1.5A.5.5 0 0 1 11 2H5a.5.5 0 0 1-.5-.5v-.5a.5.5 0 0 0-.5-.5H5a.5.5 0 0 0-.5.5v.5H4a.5.5 0 0 0-.5.5v-.5z"/></svg><span>PDF документ</span></a></div>';
        }
        
        messageDiv.innerHTML = '<div class="message-content">' + attachmentsHtml + '<p>' + escapeHtml(msg.text || '') + '</p><span class="message-time">' + time + '</span></div>';
        
        messagesList.appendChild(messageDiv);
        
        if (msg.id > lastMessageId) {
            lastMessageId = msg.id;
        }
    });
    
    if (isAtBottom) {
        messagesList.scrollTop = messagesList.scrollHeight;
    }
}

// ============ MAIN LOAD CHAT FUNCTION ============

function loadChat(partnerType, partnerId) {
    var chatContainer = document.getElementById('chatContainer');
    if (!chatContainer) {
        console.error('[DEBUG] chatContainer not found!');
        return;
    }
    
    console.log('[DEBUG] loadChat called for', partnerType, partnerId);
    
    // Stop polling and clear container to prevent duplicate handlers
    stopPolling();
    chatContainer.innerHTML = '';  // Clear first to remove old DOM and handlers
    
    pendingImage = null;
    pendingFile = null;
    
    // Show loading indicator
    chatContainer.innerHTML = '<div class="chat-loading"><div class="loading-spinner"></div><p>Загрузка...</p></div>';
    
    var url = '/messages/' + partnerType + '/' + partnerId + '/content';
    console.log('[DEBUG] Fetching URL:', url);
    
    fetch(url, {
        headers: {
            'X-CSRFToken': csrfToken
        }
    })
    .then(function(response) { 
        console.log('[DEBUG] Response status:', response.status);
        console.log('[DEBUG] Content-Type:', response.headers.get('content-type'));
        if (!response.ok) {
            return response.text().then(function(text) {
                console.error('[DEBUG] Error response:', text);
                throw new Error('Network error: ' + response.status);
            });
        }
        var contentType = response.headers.get('content-type');
        if (contentType && contentType.indexOf('application/json') !== -1) {
            return response.json().then(function(data) {
                console.error('[DEBUG] JSON response (should be HTML!):', data);
                throw new Error(data.error || 'Server error');
            });
        }
        return response.text(); 
    })
    .then(function(html) {
        console.log('[DEBUG] Chat content loaded, length:', html.length);
        chatContainer.innerHTML = html;
        
        // Update CSRF token from loaded content
        var newCsrfInput = document.querySelector('input[name="csrf_token"]');
        if (newCsrfInput) {
            csrfToken = newCsrfInput.value;
            console.log('[DEBUG] CSRF token updated');
        }
        
        // Initialize form handler
        initMessageForm();
        
        // Attach file input handler
        var fileInput = document.getElementById('fileInput');
        if (fileInput) {
            fileInput.addEventListener('change', function(e) {
                console.log('[DEBUG] fileInput change detected');
                handleFileSelect(e.target.files[0]);
                e.target.value = '';
            });
            console.log('[DEBUG] File input handler attached');
        } else {
            console.log('[DEBUG] WARNING: fileInput not found!');
        }
        
        // Scroll to bottom
        var messagesList = document.getElementById('messagesList');
        if (messagesList) {
            messagesList.scrollTop = messagesList.scrollHeight;
        }
        
        startPolling(partnerType, partnerId);
        console.log('[DEBUG] Chat initialized successfully');
    })
    .catch(function(error) {
        console.error('Error loading chat:', error);
        chatContainer.innerHTML = '<div class="no-dialog-selected"><div class="empty-state"><i class="bi bi-exclamation-triangle" style="font-size: 64px; opacity: 0.3;"></i><h3>Ошибка загрузки</h3><p>Не удалось загрузить диалог. Попробуйте ещё раз.</p></div></div>';
    });
}

// Set up global event delegation for file input (as fallback)
document.addEventListener('change', function(e) {
    if (e.target && e.target.id === 'fileInput') {
        console.log('[DEBUG] Global file input change handler triggered');
        handleFileSelect(e.target.files[0]);
        e.target.value = '';
    }
});