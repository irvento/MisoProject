// ==========================================================================
// OPENSMS GATEWAY & MULTI-SLOT GSM MANAGEMENT - JAVASCRIPT
// ==========================================================================

let CONTACTS = [];
let GROUPS = [];
let ACTIVE_CONV = null;
let GATEWAY_SLOTS = [];

$(document).ready(function () {
    initApp();

    // Tab Navigation
    $('.nav-tab').click(function () {
        const targetTab = $(this).data('tab');
        $('.nav-tab').removeClass('active');
        $(this).addClass('active');

        $('.tab-pane').removeClass('active');
        $('#' + targetTab).addClass('active');

        if (targetTab === 'tab-outbox') {
            loadOutboxLogs();
        } else if (targetTab === 'tab-gateways') {
            loadGatewaySlots();
        }
    });

    // Auto-refresh timers
    setInterval(loadGatewaySlots, 4000);
    setInterval(loadOutboxLogs, 4000);
    setInterval(refreshMessages, 3000);

    // Messenger handlers
    $('#send-btn').click(sendMessage);
    $('#message-input').keypress(function (e) {
        if (e.which === 13 && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    $('#search-input').on('input', function () {
        renderSidebar($(this).val());
    });
});

function initApp() {
    loadGatewaySlots();
    loadOutboxLogs();
    scanHardwarePorts();

    // Load messenger contacts
    $.get('/api/init_data', function (data) {
        CONTACTS = data.contacts || [];
        GROUPS = data.groups || [];
        renderSidebar();
    });
}

// ==========================================================================
// TAB 1: MODEM SLOTS (GATEWAYS) LOGIC
// ==========================================================================

function loadGatewaySlots() {
    $.get('/api/v1/gateways', function (slots) {
        GATEWAY_SLOTS = slots || [];
        renderSlotsGrid(GATEWAY_SLOTS);
        updateQuickSendSlotDropdown(GATEWAY_SLOTS);
        
        const activeCount = GATEWAY_SLOTS.filter(s => s.is_active).length;
        $('#active-slots-count').text(activeCount);
    }).fail(function () {
        console.warn('Failed to load gateway slots');
    });
}

function renderSlotsGrid(slots) {
    const grid = $('#slots-grid');
    if (!slots || slots.length === 0) {
        grid.html('<div class="empty-cell" style="grid-column: 1/-1;">No modem slots configured. Click "Add Modem Slot" to configure one.</div>');
        return;
    }

    grid.empty();
    slots.forEach(slot => {
        let statusBadge = '';
        if (!slot.is_active) {
            statusBadge = '<span class="slot-status-pill status-disabled">⚪ Inactive</span>';
        } else if (slot.is_mock) {
            statusBadge = '<span class="slot-status-pill status-mock">🟡 Mock Mode</span>';
        } else {
            statusBadge = '<span class="slot-status-pill status-online">🟢 Online</span>';
        }

        const operatorClass = 'carrier-' + (slot.sim_operator || 'auto').toLowerCase();
        
        // Signal bars calculation (0-31 CSQ)
        const csq = slot.signal_csq || 0;
        const b1 = csq >= 5 ? 'active' : '';
        const b2 = csq >= 12 ? 'active' : '';
        const b3 = csq >= 18 ? 'active' : '';
        const b4 = csq >= 24 ? 'active' : '';

        let qualityText = 'No Signal';
        if (csq >= 20) qualityText = `Excellent (${csq}/31)`;
        else if (csq >= 14) qualityText = `Good (${csq}/31)`;
        else if (csq >= 8) qualityText = `Marginal (${csq}/31)`;
        else if (csq > 0) qualityText = `Weak (${csq}/31)`;

        const prefixes = slot.prefix_filter ? `Prefixes: ${slot.prefix_filter}` : 'All Networks (Round Robin)';

        const card = `
            <div class="slot-card">
                <div>
                    <div class="slot-card-header">
                        <div class="slot-title-group">
                            <h3>${escapeHtml(slot.name)}</h3>
                            <span class="slot-port-tag">${escapeHtml(slot.port)} (${slot.baudrate} baud)</span>
                        </div>
                        ${statusBadge}
                    </div>

                    <span class="carrier-tag ${operatorClass}">${escapeHtml(slot.sim_operator || 'Auto')}</span>

                    <div class="signal-container">
                        <span class="signal-label">Signal: ${qualityText}</span>
                        <div class="signal-visual">
                            <div class="signal-bar bar-1 ${b1}"></div>
                            <div class="signal-bar bar-2 ${b2}"></div>
                            <div class="signal-bar bar-3 ${b3}"></div>
                            <div class="signal-bar bar-4 ${b4}"></div>
                        </div>
                    </div>

                    <div class="prefix-list" title="${escapeHtml(prefixes)}">
                        📌 ${escapeHtml(prefixes)}
                    </div>
                </div>

                <div>
                    <div class="slot-metrics">
                        <div class="metric-item">Sent: <strong>${slot.sent_count || 0}</strong></div>
                        <div class="metric-item">Failed: <strong>${slot.failed_count || 0}</strong></div>
                    </div>

                    <div class="slot-actions">
                        <button class="btn btn-small" onclick="toggleSlotStatus(${slot.id})">
                            ${slot.is_active ? '⏸️ Disable' : '▶️ Enable'}
                        </button>
                        <button class="btn btn-small" style="color: #ef4444;" onclick="deleteSlot(${slot.id})">
                            🗑️ Delete
                        </button>
                    </div>
                </div>
            </div>
        `;
        grid.append(card);
    });
}

function updateQuickSendSlotDropdown(slots) {
    const select = $('#quick-slot');
    const currVal = select.val();
    select.empty();
    select.append('<option value="">Auto-Route (Smart Prefix / Round Robin)</option>');
    slots.forEach(s => {
        select.append(`<option value="${escapeHtml(s.name)}">${escapeHtml(s.name)} [${escapeHtml(s.port)}]</option>`);
    });
    if (currVal) select.val(currVal);
}

function triggerAutoDetect() {
    const btn = $('#btn-auto-detect');
    const originalText = btn.html();
    btn.prop('disabled', true).html('<span>⏳</span> Scanning Hardware...');

    $.ajax({
        url: '/api/v1/system/auto-detect',
        type: 'POST',
        success: function (res) {
            btn.prop('disabled', false).html(originalText);
            loadGatewaySlots();
            
            if (res.discovered && res.discovered.length > 0) {
                const list = res.discovered.map(d => `• ${d.name} [${d.port}] (${d.carrier})`).join('\n');
                alert(`✅ Hardware Auto-Detection Complete!\n\nDetected ${res.discovered.length} modem port(s):\n${list}`);
            } else {
                alert(`Hardware scan finished. Scanned ${res.scanned_ports || 0} COM port(s). No new GSM modems responded to AT ping.`);
            }
        },
        error: function (xhr) {
            btn.prop('disabled', false).html(originalText);
            alert('Hardware scan error: ' + (xhr.responseJSON?.error || xhr.statusText));
        }
    });
}

function scanHardwarePorts() {
    $.get('/api/v1/system/ports', function (res) {
        const select = $('#slot-port');
        select.empty();
        const ports = res.ports || [];
        if (ports.length === 0) {
            select.append('<option value="COM19">COM19 (Default)</option>');
            select.append('<option value="COM20">COM20</option>');
            select.append('<option value="COM21">COM21</option>');
            select.append('<option value="COM22">COM22</option>');
        } else {
            ports.forEach(p => {
                select.append(`<option value="${p}">${p}</option>`);
            });
            // Include common multi-slot ports if not listed
            ['COM19', 'COM20', 'COM21', 'COM22'].forEach(p => {
                if (!ports.includes(p)) {
                    select.append(`<option value="${p}">${p}</option>`);
                }
            });
        }
    });
}

function openAddSlotModal() {
    scanHardwarePorts();
    $('#slot-name').val('');
    $('#slot-prefixes').val('');
    $('#add-slot-modal').css('display', 'flex');
}

function closeAddSlotModal() {
    $('#add-slot-modal').hide();
}

function saveNewSlot() {
    const name = $('#slot-name').val().trim();
    const port = $('#slot-port').val();
    const baudrate = $('#slot-baud').val();
    const sim_operator = $('#slot-operator').val();
    const prefix_filter = $('#slot-prefixes').val().trim();

    if (!name || !port) {
        alert('Please provide a Slot Name and select a COM Port.');
        return;
    }

    $.ajax({
        url: '/api/v1/gateways',
        type: 'POST',
        contentType: 'application/json',
        data: JSON.stringify({ name, port, baudrate, sim_operator, prefix_filter }),
        success: function (res) {
            closeAddSlotModal();
            loadGatewaySlots();
        },
        error: function (xhr) {
            alert('Error adding slot: ' + (xhr.responseJSON?.error || xhr.statusText));
        }
    });
}

function toggleSlotStatus(slotId) {
    $.post(`/api/v1/gateways/${slotId}/toggle`, function () {
        loadGatewaySlots();
    });
}

function deleteSlot(slotId) {
    if (!confirm('Are you sure you want to remove this modem slot?')) return;
    $.ajax({
        url: `/api/v1/gateways/${slotId}`,
        type: 'DELETE',
        success: function () {
            loadGatewaySlots();
        }
    });
}

// ==========================================================================
// TAB 3: OUTBOX LOGS LOGIC
// ==========================================================================

function loadOutboxLogs() {
    $.get('/api/v1/outbox', function (logs) {
        const tbody = $('#outbox-tbody');
        tbody.empty();
        $('#queue-count').text(logs.length);

        if (!logs || logs.length === 0) {
            tbody.html('<tr><td colspan="7" class="empty-cell">No message traffic recorded yet.</td></tr>');
            return;
        }

        logs.forEach(log => {
            let statusClass = 'pending';
            const s = (log.status || '').toLowerCase();
            if (s === 'sent') statusClass = 'sent';
            else if (s === 'processing') statusClass = 'processing';
            else if (s === 'failed') statusClass = 'failed';

            const row = `
                <tr>
                    <td>#${log.id}</td>
                    <td>${escapeHtml(log.created_at || 'Just now')}</td>
                    <td><strong>${escapeHtml(log.recipient)}</strong></td>
                    <td style="max-width: 300px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                        ${escapeHtml(log.message)}
                    </td>
                    <td><span class="pill">${escapeHtml(log.connector || 'HTTP')}</span></td>
                    <td>${escapeHtml(log.dispatched_via || 'Pending')}</td>
                    <td><span class="status-badge ${statusClass}">${escapeHtml(log.status)}</span></td>
                </tr>
            `;
            tbody.append(row);
        });
    });
}

// ==========================================================================
// TAB 4: DIRECT MESSENGER LOGIC
// ==========================================================================

function renderSidebar(filter = '') {
    const list = $('#contact-list');
    list.empty();
    filter = filter.toLowerCase();

    // Render Groups
    if (GROUPS.length > 0) {
        list.append(`<div class="sidebar-section-title">Groups</div>`);
        GROUPS.forEach(group => {
            if (group.toLowerCase().includes(filter)) {
                const isActive = (ACTIVE_CONV && ACTIVE_CONV.type === 'group' && ACTIVE_CONV.target === group) ? 'active' : '';
                const html = `
                    <div class="contact-item ${isActive}" onclick="selectConversation('${escapeHtml(group)}', 'group', '${escapeHtml(group)}')">
                        <div class="avatar group-avatar">📢</div>
                        <div class="contact-info">
                            <div class="contact-name">${escapeHtml(group)}</div>
                            <div class="contact-preview">Department Broadcast</div>
                        </div>
                    </div>
                `;
                list.append(html);
            }
        });
    }

    // Render Individuals
    if (CONTACTS.length > 0) {
        list.append(`<div class="sidebar-section-title">Contacts</div>`);
        CONTACTS.forEach(contact => {
            if (contact.name.toLowerCase().includes(filter) || contact.phone.includes(filter)) {
                const isActive = (ACTIVE_CONV && ACTIVE_CONV.type === 'individual' && ACTIVE_CONV.target === contact.phone) ? 'active' : '';
                const initial = contact.name.charAt(0).toUpperCase();
                const html = `
                    <div class="contact-item ${isActive}" onclick="selectConversation('${escapeHtml(contact.phone)}', 'individual', '${escapeHtml(contact.name)}')">
                        <div class="avatar">${initial}</div>
                        <div class="contact-info">
                            <div class="contact-name">${escapeHtml(contact.name)}</div>
                            <div class="contact-preview">${escapeHtml(contact.phone)}</div>
                        </div>
                    </div>
                `;
                list.append(html);
            }
        });
    }
}

function selectConversation(target, type, name) {
    ACTIVE_CONV = { target, type, name };
    renderSidebar();

    $('#chat-placeholder').hide();
    $('#chat-interface').css('display', 'flex');

    $('#chat-name').text(name);
    $('#chat-details').text(type === 'group' ? 'Department Broadcast' : target);

    refreshMessages();
}

function refreshMessages() {
    if (!ACTIVE_CONV) return;

    $.get('/api/messages', { target: ACTIVE_CONV.target, type: ACTIVE_CONV.type }, function (messages) {
        const container = $('#messages-container');
        container.empty();

        messages.forEach(msg => {
            const isOut = msg.direction === 'out' || ACTIVE_CONV.type === 'group';
            const bubble = `
                <div class="message-bubble ${isOut ? 'message-out' : 'message-in'}">
                    <div>${escapeHtml(msg.text)}</div>
                    <div style="font-size: 10px; opacity: 0.7; margin-top: 4px; text-align: right;">${msg.time || ''}</div>
                </div>
            `;
            container.append(bubble);
        });

        container.scrollTop(container[0].scrollHeight);
    });
}

function sendMessage() {
    if (!ACTIVE_CONV) return;
    const input = $('#message-input');
    const text = input.val().trim();
    if (!text) return;

    input.val('');

    $.ajax({
        url: '/api/send',
        type: 'POST',
        contentType: 'application/json',
        data: JSON.stringify({
            target: ACTIVE_CONV.target,
            message: text,
            type: ACTIVE_CONV.type
        }),
        success: function () {
            refreshMessages();
            loadOutboxLogs();
            loadGatewaySlots();
        }
    });
}

function sendDirectMessage() {
    const phone = $('#quick-phone').val().trim();
    const message = $('#quick-message').val().trim();
    const gateway = $('#quick-slot').val();

    if (!phone || !message) {
        alert('Please enter a recipient number and a message.');
        return;
    }

    $.ajax({
        url: '/api/v1/sms/send',
        type: 'POST',
        contentType: 'application/json',
        data: JSON.stringify({ target: phone, message: message, gateway: gateway }),
        success: function (res) {
            alert('Message dispatched via: ' + (res.dispatched_via || 'Gateway'));
            $('#quick-phone').val('');
            $('#quick-message').val('');
            loadOutboxLogs();
            loadGatewaySlots();
        },
        error: function (xhr) {
            alert('Send failed: ' + (xhr.responseJSON?.error || xhr.statusText));
        }
    });
}

// Contacts & Broadcast Modals
function openAddContact() { $('#contact-modal').css('display', 'flex'); }
function closeAddContact() { $('#contact-modal').hide(); }
function openGroupModal() { $('#group-modal').css('display', 'flex'); }
function closeGroupModal() { $('#group-modal').hide(); }

function saveContact() {
    const name = $('#new-name').val().trim();
    const phone_number = $('#new-phone').val().trim();
    const department = $('#new-dept').val();

    if (!name || !phone_number) {
        alert('Please provide name and phone number.');
        return;
    }

    $.ajax({
        url: '/api/contacts',
        type: 'POST',
        contentType: 'application/json',
        data: JSON.stringify({ name, phone_number, department }),
        success: function () {
            closeAddContact();
            initApp();
        }
    });
}

function sendGroup() {
    const dept = $('#group-dept').val();
    const msg = $('#group-msg').val().trim();
    if (!msg) {
        alert('Please enter a message.');
        return;
    }

    $.ajax({
        url: '/api/send',
        type: 'POST',
        contentType: 'application/json',
        data: JSON.stringify({ target: dept, message: msg, type: 'group' }),
        success: function (res) {
            alert(res.info || 'Broadcast sent!');
            closeGroupModal();
            loadOutboxLogs();
            loadGatewaySlots();
        }
    });
}

function escapeHtml(text) {
    if (!text) return '';
    return $('<div>').text(text).html();
}
