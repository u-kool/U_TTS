// ---------------------- АВТОМАТИЧЕСКАЯ ОЧИСТКА ДАННЫХ БРАУЗЕРА ----------------------
(function autoCleanBrowserData() {
    // Запускаем асинхронно, чтобы не блокировать отрисовку интерфейса и загрузку голосов
    setTimeout(() => {
        try {
            const keysToKeep = ['twitch_tts_usernames', 'twitch_tts_rewards'];
            
            // Безопасное удаление из localStorage
            const allKeys = Object.keys(localStorage);
            for (let i = 0; i < allKeys.length; i++) {
                const key = allKeys[i];
                if (!keysToKeep.includes(key)) {
                    localStorage.removeItem(key);
                }
            }

            sessionStorage.clear();

            document.cookie.split("; ").forEach(cookie => {
                const eqPos = cookie.indexOf("=");
                const name = eqPos > -1 ? cookie.substr(0, eqPos) : cookie;
                document.cookie = name + "=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
            });

            if ('caches' in window) {
                caches.keys().then(keys => keys.forEach(key => caches.delete(key))).catch(e => console.warn(e));
            }
            
            console.log("🧹 Автоочистка данных браузера выполнена");
        } catch (e) {
            console.warn("⚠️ Ошибка автоочистки: ", e);
        }
    }, 0);
})();

// ---------------------- ПЕРИОДИЧЕСКАЯ ПЕРЕЗАГРУЗКА (КАЖДЫЕ 2 ЧАСА) ----------------------
setTimeout(() => {
    console.log("🔄 Автоматическая перезагрузка страницы (2 часа работы)");
    location.reload();
}, 7200000);

// ---------------------- Глобальные переменные ----------------------
let currentConfig = {};
let logsUpdateInterval = null, statusUpdateInterval = null;
let sseSource = null;
let sseConnected = false;
let emoteMap = {};
let emotesLoaded = false;

// ----- Очередь воспроизведения (HTML5 Audio) -----
let audioQueue = [];
let isPlaying = false;
let currentAudio = null;

let usernamesSet = new Set();
const USERNAMES_STORAGE_KEY = 'twitch_tts_usernames';
let rewardsSet = new Set();
const REWARDS_STORAGE_KEY = 'twitch_tts_rewards';
let availableVoices = [];
let userVoiceMap = {};
let eventsConfig = {};
let saveAudioMode = false;

// DOM элементы
const openSettingsBtn = document.getElementById('open-settings-btn');
const openEventsPanelBtn = document.getElementById('open-events-panel-btn');
const openVoiceMapPanelBtn = document.getElementById('open-voice-map-panel-btn');
const closeSettingsBtn = document.getElementById('close-settings-btn');
const closeEventsBtn = document.getElementById('close-events-btn');
const closeVoiceMapBtn = document.getElementById('close-voice-map-btn');
const settingsPanel = document.getElementById('settings-panel');
const eventsPanel = document.getElementById('events-panel');
const voiceMapPanel = document.getElementById('voice-map-panel');
const settingsOverlay = document.getElementById('settings-overlay');
const eventsOverlay = document.getElementById('events-overlay');
const voiceMapOverlay = document.getElementById('voice-map-overlay');
const logContainer = document.getElementById('log-container');
const channelInput = document.getElementById('channel-input');
const connectBtn = document.getElementById('connect-btn');
const authBtn = document.getElementById('auth-btn');
const statusDot = document.getElementById('status-dot');
const channelHistory = document.getElementById('channel-history');
const headerSaveBtn = document.getElementById('header-save-btn');
const headerTestBtn = document.getElementById('header-test-btn');
const ttsEnabledToggle = document.getElementById('tts-enabled-toggle');
const toggleAdvancedBtn = document.getElementById('toggle-advanced-btn');
const closeAdvancedBtn = document.getElementById('close-advanced-btn');
const advancedDropdown = document.getElementById('advanced-dropdown');

// Утилиты
function showNotification(message, isError = false) {
    const toast = document.getElementById('toast-notification');
    toast.textContent = message;
    toast.style.borderLeftColor = isError ? '#dc3545' : '#28a745';
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 3000);
}

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/[&<>]/g, function(m) {
        if (m === '&') return '&amp;';
        if (m === '<') return '&lt;';
        if (m === '>') return '&gt;';
        return m;
    });
}

function escapeRegex(str) {
    return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function parseEmotes(message, extraEmotes) {
    if (!message) return escapeHtml(message);
    const allEmotes = Object.assign({}, emoteMap, extraEmotes || {});
    if (Object.keys(allEmotes).length === 0) return escapeHtml(message);
    let result = escapeHtml(message);
    for (const [emote, url] of Object.entries(allEmotes)) {
        const re = new RegExp(`(?<=^|\\s)${escapeRegex(emote)}(?=\\s|$)`, 'g');
        result = result.replace(re, `<img src="${url}" alt="${emote}" style="height:1.4em;">`);
    }
    return result;
}

function addSystemMessage(text) {
    const div = document.createElement('div');
    div.className = 'log-entry system';
    div.innerHTML = `<span class="log-time">${new Date().toLocaleTimeString()}</span><span class="log-text">${escapeHtml(text)}</span>`;
    const empty = logContainer.querySelector('.empty-chat');
    if (empty) empty.remove();
    logContainer.appendChild(div);
    logContainer.scrollTop = logContainer.scrollHeight;
    setTimeout(() => { if (div.parentNode) div.remove(); }, 5000);
}

// ---------------------- Кеш наград ----------------------
function loadRewardsCache() {
    const stored = localStorage.getItem(REWARDS_STORAGE_KEY);
    if (stored) try { rewardsSet = new Set(JSON.parse(stored)); } catch(e) {}
}

function saveRewardsCache() {
    localStorage.setItem(REWARDS_STORAGE_KEY, JSON.stringify(Array.from(rewardsSet)));
}

function addRewardToCache(rewardName) {
    if (rewardName && !rewardsSet.has(rewardName)) {
        rewardsSet.add(rewardName);
        saveRewardsCache();
    }
}

// ---------------------- Статус и логи ----------------------
function loadChannelHistory() {
    try { return JSON.parse(localStorage.getItem('twitch_channel_history')) || []; } catch { return []; }
}
function saveChannelHistory(history) {
    localStorage.setItem('twitch_channel_history', JSON.stringify(history));
}
function addChannelToHistory(channel) {
    const h = loadChannelHistory().filter(c => c !== channel);
    h.unshift(channel);
    if (h.length > 10) h.length = 10;
    saveChannelHistory(h);
    renderChannelHistory();
}
function renderChannelHistory() {
    if (!channelHistory) return;
    channelHistory.innerHTML = loadChannelHistory().map(c => `<option value="${c}">`).join('');
}
renderChannelHistory();

async function fetchStatus() {
    try {
        const resp = await fetch('/api/status');
        const data = await resp.json();
        authBtn.style.display = data.has_token ? 'none' : '';
        window.has_token = data.has_token;
        if (data.twitch_running) {
            statusDot.className = 'status-dot dot-online';
            connectBtn.innerHTML = '✕';
            connectBtn.style.cssText = 'position:absolute; top:0; right:2px; background:none; border:none; color:#888; font-size:13px; cursor:pointer; padding:0 2px; line-height:28px; z-index:10;';
            connectBtn.onclick = resetTwitch;
            const ch = (data.login || data.channel || '').replace('#','');
            if (ch) { channelInput.value = ch; addChannelToHistory(ch); }
        } else {
            statusDot.className = 'status-dot dot-offline';
            connectBtn.innerHTML = '▶';
            connectBtn.style.cssText = 'position:absolute; top:0; right:2px; background:none; border:none; color:#888; font-size:13px; cursor:pointer; padding:0 2px; line-height:28px; z-index:10;';
            connectBtn.onclick = connectTwitch;
        }
        const chatInput = document.getElementById('chat-input');
        const chatSendBtn = document.getElementById('chat-send-btn');
        if (chatInput) chatInput.style.display = data.has_token ? '' : 'none';
        if (chatSendBtn) chatSendBtn.style.display = data.has_token ? '' : 'none';
    } catch(e) { console.error('Status error:', e); }
    updateEngineStatus();
}
    } catch(e) { console.error('Status error:', e); }
    updateEngineStatus();
}

let userLastMessageTime = {};

function addLogToUI(log) {
    if (log.type === 'system' && log.text.includes('Озвучено')) return;
    if (log.type === 'chat' && log.user) {
        userLastMessageTime[log.user] = log.timestamp || Date.now() / 1000;
    }
    const div = document.createElement('div');
    div.className = `log-entry ${log.type}`;
    
    const timeSpan = document.createElement('span');
    timeSpan.className = 'log-time';
    timeSpan.textContent = new Date(log.timestamp * 1000).toLocaleTimeString();
    div.appendChild(timeSpan);
    
    if (log.user) {
        const userSpan = document.createElement('span');
        userSpan.className = `log-user ${log.type}`;
        userSpan.textContent = log.user + ':';
        div.appendChild(userSpan);
        addUsernameToCache(log.user);
    }
    
    const textSpan = document.createElement('span');
    textSpan.className = 'log-text';
    if (log.type === 'chat' && emotesLoaded) {
        textSpan.innerHTML = parseEmotes(log.text, log.emotes);
    } else {
        textSpan.textContent = escapeHtml(log.text);
    }
    div.appendChild(textSpan);
    
    if (log.type === 'event' && log.text) {
        const match = log.text.match(/использовал награду (.+?)(?: и сказал|$)/);
        if (match && match[1]) {
            addRewardToCache(match[1].trim());
        }
    }

    const empty = logContainer.querySelector('.empty-chat');
    if (empty) empty.remove();
    logContainer.appendChild(div);
    logContainer.scrollTop = logContainer.scrollHeight;
    
    while (logContainer.children.length > 50) logContainer.firstChild.remove();
}

const displayedLogKeys = new Set();
const MAX_LOG_KEYS = 500;

async function fetchLogs() {
    try {
        const resp = await fetch('/api/logs');
        const logs = await resp.json();
        logs.forEach(log => {
            const key = `${log.timestamp}_${log.type}_${log.user}_${log.text}`;
            if (!displayedLogKeys.has(key)) {
                displayedLogKeys.add(key);
                addLogToUI(log);
            }
        });
        if (displayedLogKeys.size > MAX_LOG_KEYS) displayedLogKeys.clear();
    } catch(e) { console.error(e); }
}

// ---------------------- Аудио (HTML5 Audio) ----------------------
function playNextFromQueue() {
    if (isPlaying || audioQueue.length === 0) return;
    
    isPlaying = true;
    const item = audioQueue.shift();
    let url = '';
    
    if (item.url) {
        url = item.url;
    } else if (item.filename) {
        url = `/api/latest?file=${encodeURIComponent(item.filename)}&_=${Date.now()}`;
    } else {
        isPlaying = false;
        playNextFromQueue();
        return;
    }
    
    currentAudio = new Audio(url);
    currentAudio.onended = () => {
        isPlaying = false;
        currentAudio = null;
        playNextFromQueue();
    };
    currentAudio.onerror = (e) => {
        console.error('Ошибка воспроизведения аудио:', e);
        isPlaying = false;
        currentAudio = null;
        playNextFromQueue();
    };
    currentAudio.play().catch(e => {
        console.warn('Не удалось воспроизвести аудио:', e);
        isPlaying = false;
        currentAudio = null;
        playNextFromQueue();
    });
}

function enqueueAudio(filename) {
    audioQueue.push({ filename });
    if (!isPlaying) playNextFromQueue();
}

// ---------------------- SSE ----------------------
function connectSSE() {
    if (sseSource) sseSource.close();
    sseSource = new EventSource('/api/sse');
    
    sseSource.addEventListener('new_audio', e => {
        try {
            const data = JSON.parse(e.data);
            enqueueAudio(data.filename);
        } catch(ex) {}
    });
    
    sseSource.addEventListener('play', e => {
        try {
            const data = JSON.parse(e.data);
            if (data.filename) enqueueAudio(data.filename);
        } catch(ex) {}
    });
    
    sseSource.addEventListener('engine_switched', () => updateEngineStatus());
    
    sseSource.addEventListener('new_emote', e => {
        try {
            const data = JSON.parse(e.data);
            if (data.name && data.url) emoteMap[data.name] = data.url;
        } catch(ex) {}
    });
    
    sseSource.addEventListener('voice_assigned', e => {
        try {
            const data = JSON.parse(e.data);
            const entry = { voice: data.voice };
            if (data.engine === 'xtts') {
                entry.xtts_language = data.xtts_language;
                entry.xtts_temperature = data.xtts_temperature;
            } else {
                entry.rate = data.rate;
                entry.volume = '+0%';
                entry.pitch = data.pitch;
            }
            userVoiceMap[data.user] = entry;
            const isOpen = voiceMapPanel && voiceMapPanel.classList.contains('open');
            if (isOpen) renderUserVoiceMapInPanel();
        } catch(ex) { console.error('voice_assigned error:', ex); }
    });
    
    sseSource.addEventListener('log', e => {
        try {
            const data = JSON.parse(e.data);
            addLogToUI(data);
        } catch(ex) { console.error('log event error:', ex); }
    });
    
    sseSource.onerror = () => {
        sseSource.close();
        setTimeout(connectSSE, 3000);
    };
    
    sseSource.onopen = () => {
        sseConnected = true;
        fetchStatus();
    };
}

// ---------------------- Эмоуты ----------------------
async function loadEmotes() {
    try {
        const resp = await fetch('/api/emotes');
        if (resp.ok) {
            emoteMap = await resp.json();
            emotesLoaded = true;
        }
    } catch(e) { console.error('Emotes load error:', e); }
}

// ---------------------- Кэш имён ----------------------
function loadUsernameCache() {
    const stored = localStorage.getItem(USERNAMES_STORAGE_KEY);
    if (stored) try { usernamesSet = new Set(JSON.parse(stored)); } catch(e) {}
}

function saveUsernameCache() { 
    localStorage.setItem(USERNAMES_STORAGE_KEY, JSON.stringify(Array.from(usernamesSet))); 
}

function addUsernameToCache(username) { 
    if (username && !usernamesSet.has(username)) { 
        usernamesSet.add(username); 
        saveUsernameCache(); 
    } 
}

// ---------------------- Голоса ----------------------
let availableLanguages = [];

let _voicesLoaded = false;
async function loadVoices(force) {
    if (_voicesLoaded && !force) return;
    try {
        const resp = await fetch('/api/voices');
        const data = await resp.json();
        availableVoices = data.voices || [];
        availableLanguages = data.languages || [];
        _voicesLoaded = true;
        
        const select = document.getElementById('voice-select');
        const prevVal = select.value;
        select.innerHTML = '';
        
        const xttsVoices = availableVoices.filter(v => v.engine === 'xtts');
        const edgeVoices = availableVoices.filter(v => v.engine === 'edge-tts');
        
        xttsVoices.forEach(v => {
            const opt = document.createElement('option');
            opt.value = v.name;
            opt.textContent = v.name;
            select.appendChild(opt);
        });
        
        if (xttsVoices.length && edgeVoices.length) {
            const sep = document.createElement('option');
            sep.disabled = true;
            sep.textContent = '──────────';
            select.appendChild(sep);
        }
        
        edgeVoices.forEach(v => {
            const opt = document.createElement('option');
            opt.value = v.name;
            opt.textContent = `${v.name} (${v.gender || ''}${v.locale ? ', '+v.locale : ''})`;
            select.appendChild(opt);
        });
        
        if (prevVal) select.value = prevVal;
        
        const langSelect = document.getElementById('xtts-language-select');
        if (langSelect) {
            langSelect.innerHTML = '';
            availableLanguages.forEach(l => {
                const opt = document.createElement('option');
                opt.value = l.code;
                opt.textContent = l.name;
                langSelect.appendChild(opt);
            });
        }
        
        toggleXttsSettings();
        renderUserVoiceMapInPanel();
        renderEventsPanel(eventsConfig);
    } catch(e) { console.error(e); }
}

function toggleXttsSettings() {
    const voice = document.getElementById('voice-select').value;
    const isXtts = voice && voice.startsWith('xtts-');
    document.getElementById('edge-advanced').style.display = isXtts ? 'none' : '';
    document.getElementById('xtts-advanced').style.display = isXtts ? '' : 'none';
}

function toggleEngineSettings(voiceSelect, container) {
    if (!container) container = voiceSelect.closest('.dynamic-list-item') || voiceSelect.closest('.event-block');
    if (!container) return;
    const isXtts = voiceSelect.value && voiceSelect.value.startsWith('xtts-');
    const edge = container.querySelector('.edge-settings');
    const xtts = container.querySelector('.xtts-settings');
    if (edge) edge.style.display = isXtts ? 'none' : '';
    if (xtts) xtts.style.display = isXtts ? '' : 'none';
}

async function updateEngineStatus() {
    const el = document.getElementById('engine-status');
    try {
        const r = await fetch('/api/tts/engine');
        const d = await r.json();
        if (d.engine === 'xtts') {
            el.textContent = d.ready ? '🔊 XTTS' : '⏳ XTTS...';
        } else {
            el.textContent = '🔊 edge-tts';
        }
    } catch(e) { el.textContent = ''; }
}

// ---------- Рендер панели голосов ----------
function renderUserVoiceMapInPanel() {
    const container = document.getElementById('user-voice-list-panel');
    if (!container) return;
    container.innerHTML = '';
    
    const search = (document.getElementById('voice-map-search')?.value || '').toLowerCase();
    const usernamesArray = Array.from(usernamesSet).sort();
    const entries = [];
    
    for (const [user, cfg] of Object.entries(userVoiceMap)) {
        entries.push({user, cfg, source: 'map'});
    }
    for (const u of usernamesArray) {
        if (!userVoiceMap.hasOwnProperty(u)) {
            entries.push({user: u, cfg: null, source: 'suggestion'});
        }
    }
    
    entries.sort((a, b) => {
        const ta = userLastMessageTime[a.user] || 0;
        const tb = userLastMessageTime[b.user] || 0;
        return tb - ta;
    });
    
    const filtered = search ? entries.filter(e => e.user.toLowerCase().includes(search)) : entries;
    
    for (const entry of filtered) {
        const {user, cfg} = entry;
        const isObj = cfg && typeof cfg === 'object';
        const rawVoice = isObj ? cfg.voice : (cfg || '');
        const hasVoice = rawVoice && rawVoice !== 'silent';
        const voice = hasVoice ? rawVoice : '';
        const rate = isObj && hasVoice ? (cfg.rate || '+0%') : '+0%';
        const volume = isObj ? (cfg.volume || '+0%') : '+0%';
        const pitch = isObj ? (cfg.pitch || '+0Hz') : '+0Hz';
        const isXtts = voice && voice.startsWith('xtts-');
        const xttsLanguage = isObj ? (cfg.xtts_language || 'ru') : 'ru';
        
        const div = document.createElement('div');
        div.className = 'dynamic-list-item';
        div.innerHTML = `
            <input type="text" placeholder="Имя пользователя" value="${escapeHtml(user)}" class="map-user" style="flex:1;">
            <select class="map-voice" style="flex:1;">
                <option value="" ${!hasVoice ? 'selected' : ''}>🎤 (не выбран)</option>
                ${availableVoices.map(v => `<option value="${escapeHtml(v.name)}" ${v.name === voice ? 'selected' : ''}>${escapeHtml(v.name)}${v.gender ? `(${v.gender}, ${v.locale})` : ''}</option>`).join('')}
            </select>
            <button class="voice-advanced-btn" style="background:#9146FF;">⚙️</button>
            <button class="remove-item">🗑️</button>
            <div class="advanced-settings" style="width:100%; margin-top:8px;">
                <div class="edge-settings" style="${isXtts ? 'display:none;' : ''}">
                    <div class="slider-row"><label>⚡ Скорость</label><input type="range" class="adv-rate" min="-50" max="100" value="${parseInt(rate)}"><span class="adv-rate-val">${rate}</span></div>
                    <div class="slider-row"><label>🔊 Громкость</label><input type="range" class="adv-volume" min="-50" max="50" value="${parseInt(volume)}"><span class="adv-vol-val">${volume}</span></div>
                    <div class="slider-row"><label>🎵 Тон</label><input type="range" class="adv-pitch" min="-100" max="100" value="${parseInt(pitch)}"><span class="adv-pitch-val">${pitch}</span></div>
                </div>
                <div class="xtts-settings" style="${isXtts ? '' : 'display:none;'}">
                    <div class="slider-row"><label>🌐 Язык</label><select class="adv-language" style="flex:1; background:#2a2a2d; border:1px solid #3a3a3d; color:white; padding:6px; border-radius:4px;">${availableLanguages.map(l => `<option value="${escapeHtml(l.code)}" ${l.code === xttsLanguage ? 'selected' : ''}>${escapeHtml(l.name)}</option>`).join('')}</select></div>
                </div>
            </div>
        `;
        
        const voiceSelect = div.querySelector('.map-voice');
        const advDiv = div.querySelector('.advanced-settings');
        div.querySelector('.voice-advanced-btn').onclick = () => advDiv.classList.toggle('show');
        voiceSelect.addEventListener('change', function() { toggleEngineSettings(this, div); });
        
        const rateSlider = div.querySelector('.adv-rate');
        if (rateSlider) rateSlider.oninput = function() { this.parentNode.querySelector('.adv-rate-val').textContent = this.value + '%'; };
        const volSlider = div.querySelector('.adv-volume');
        if (volSlider) volSlider.oninput = function() { this.parentNode.querySelector('.adv-vol-val').textContent = this.value + '%'; };
        const pitchSlider = div.querySelector('.adv-pitch');
        if (pitchSlider) pitchSlider.oninput = function() { this.parentNode.querySelector('.adv-pitch-val').textContent = this.value + 'Hz'; };
        
        div.querySelector('.remove-item').onclick = async () => {
            const userVal = div.querySelector('.map-user').value.trim();
            if (userVal) {
                try {
                    const r = await fetch(`/api/voice_map/${encodeURIComponent(userVal)}`, { method:'DELETE' });
                    if (r.ok) {
                        delete userVoiceMap[userVal];
                        delete userLastMessageTime[userVal];
                        usernamesSet.delete(userVal);
                        saveUsernameCache();
                        renderUserVoiceMapInPanel();
                    }
                } catch(e) { console.error('Delete error:', e); }
            } else {
                div.remove();
            }
        };
        
        setupUserSuggestions(div.querySelector('.map-user'), usernamesArray);
        container.appendChild(div);
    }
}

function getUserVoiceMapFromPanel() {
    const items = document.querySelectorAll('#user-voice-list-panel .dynamic-list-item');
    const map = {};
    items.forEach(item => {
        const user = item.querySelector('.map-user').value.trim();
        const voice = item.querySelector('.map-voice').value;
        if (!user) return;
        if (!voice) {
            map[user] = {};
            return;
        }
        const adv = item.querySelector('.advanced-settings');
        if (adv && adv.classList.contains('show')) {
            const isXtts = voice && voice.startsWith('xtts-');
            if (isXtts) {
                const langEl = item.querySelector('.adv-language');
                map[user] = { voice, language: langEl ? langEl.value : 'ru' };
            } else {
                const rate = item.querySelector('.adv-rate').value + '%';
                const volume = item.querySelector('.adv-volume').value + '%';
                const pitch = item.querySelector('.adv-pitch').value + 'Hz';
                map[user] = { voice, rate, volume, pitch };
            }
        } else {
            map[user] = voice;
        }
    });
    return map;
}

function setupUserSuggestions(input, usernames) {
    const wrap = document.createElement('div');
    wrap.className = 'suggestions-wrap';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    
    const box = document.createElement('div');
    box.className = 'suggestions-box';
    wrap.appendChild(box);
    
    function showSuggestions() {
        const val = input.value.toLowerCase();
        const filtered = usernames.filter(u => u.toLowerCase().includes(val));
        if (filtered.length === 0) { box.classList.remove('show'); return; }
        box.innerHTML = filtered.map(u => `<div data-value="${escapeHtml(u)}">${escapeHtml(u)}</div>`).join('');
        box.classList.add('show');
    }
    function hideSuggestions() { box.classList.remove('show'); }
    
    input.addEventListener('focus', showSuggestions);
    input.addEventListener('input', showSuggestions);
    input.addEventListener('blur', () => setTimeout(hideSuggestions, 200));
    box.addEventListener('mousedown', e => {
        const div = e.target.closest('[data-value]');
        if (div) {
            input.value = div.dataset.value;
            hideSuggestions();
            input.dispatchEvent(new Event('input', { bubbles: true }));
        }
    });
}

// ---------- Рендер панели событий ----------
function renderEventsPanel(events) {
    const container = document.getElementById('events-panel-body');
    if (!container) return;
    const eventTypes = ['follow', 'subscription', 'subscription_gift', 'cheer', 'raid', 'reward'];
    let html = '';
    
    for (const ev of eventTypes) {
        const cfg = events[ev] || {};
        const enabled = cfg.enabled !== false;
        const voice = cfg.voice || currentConfig.voice;
        let format = cfg.format || '';
        const rate = cfg.rate || '+0%';
        const volume = cfg.volume || '+0%';
        const pitch = cfg.pitch || '+0Hz';
        const minViewers = cfg.min_viewers || 0;
        const formatNoMsg = cfg.format_no_msg || '{UserName} использовал награду {RewardName}';
        const formatWithMsg = cfg.format_with_msg || '{UserName} использовал награду {RewardName} и сказал {Message}';
        const useWithMsg = cfg.use_with_msg === true;
        const isXtts = voice && voice.startsWith('xtts-');
        const messages = cfg.messages || [];
        
        html += `
        <div class="event-block" data-event="${ev}">
            <div class="event-header">
                <label class="checkbox-label"><input type="checkbox" class="event-enabled" ${enabled ? 'checked' : ''}><strong>${ev.replace('_',' ').toUpperCase()}</strong></label>
                <select class="event-voice-select">${availableVoices.map(v => `<option value="${v.name}" ${(voice===v.name) ? 'selected' : ''}>${v.name}${v.gender ? `(${v.gender}, ${v.locale})` : ''}</option>`).join('')}</select>
                <button class="action-btn event-advanced-toggle">⚙️</button>
                <button class="action-btn test-event-btn" data-event="${ev}" style="background:#9146FF;">🧪</button>
            </div>
            <div class="event-advanced-settings" style="display:none;">
                <div class="edge-settings" style="${isXtts ? 'display:none;' : ''}">
                    <div class="slider-row"><label>⚡ Скорость</label><input type="range" class="event-rate" min="-50" max="100" value="${parseInt(rate)}"><span class="event-rate-val">${rate}</span></div>
                    <div class="slider-row"><label>🔊 Громкость</label><input type="range" class="event-volume" min="-50" max="50" value="${parseInt(volume)}"><span class="event-vol-val">${volume}</span></div>
                    <div class="slider-row"><label>🎵 Тон</label><input type="range" class="event-pitch" min="-100" max="100" value="${parseInt(pitch)}"><span class="event-pitch-val">${pitch}</span></div>
                </div>
                <div class="xtts-settings" style="${isXtts ? '' : 'display:none;'}">
                    <div class="slider-row"><label>🌐 Язык</label><select class="event-language" style="flex:1; background:#2a2a2d; border:1px solid #3a3a3d; color:white; padding:6px; border-radius:4px;">${availableLanguages.map(l => `<option value="${escapeHtml(l.code)}">${escapeHtml(l.name)}</option>`).join('')}</select></div>
                </div>
            </div>
        `;
        
        if (ev === 'reward') {
            html += `
            <div style="margin-top:8px;"><label class="checkbox-label"><input type="checkbox" class="event-use-with-msg" ${useWithMsg ? 'checked' : ''}> 💬 С сообщением</label></div>
            <div class="reward-format-section" style="${useWithMsg ? '' : 'display:none;'}">
                <div style="margin-top:8px;"><label>Формат с сообщением:</label><input type="text" class="event-format-with-msg" value="${escapeHtml(formatWithMsg)}" style="width:100%; background:#2a2a2d; border:1px solid #3a3a3d; padding:6px; color:white; border-radius:6px;"></div>
            </div>
            <div class="reward-format-section" style="${useWithMsg ? 'display:none;' : ''}">
                <div style="margin-top:8px;"><label>Формат без сообщения:</label><input type="text" class="event-format-no-msg" value="${escapeHtml(formatNoMsg)}" style="width:100%; background:#2a2a2d; border:1px solid #3a3a3d; padding:6px; color:white; border-radius:6px;"></div>
            </div>
            <div style="margin-top:8px;">
                <label>🎭 Сопоставление награды с голосом</label>
                <div id="reward-voice-list-${ev}" class="reward-voice-list"></div>
                <button class="add-reward-voice-btn action-btn" data-event="${ev}">+ Добавить награду</button>
            </div>
            `;
        } else {
            html += `<div style="margin-top:8px;"><label>Формат сообщения:</label><input type="text" class="event-format" placeholder="Формат сообщения" value="${escapeHtml(format)}" style="width:100%; background:#2a2a2d; border:1px solid #3a3a3d; padding:6px; color:white; border-radius:6px;"></div>`;
        }
        
        if (ev === 'raid') {
            const maxViewers = cfg.max_viewers || '';
            html += `<div style="margin-top:8px; display:flex; gap:8px; align-items:center;"><label>👥 Зрители от:</label><input type="number" class="event-min-viewers" value="${minViewers}" style="width:80px; background:#2a2a2d; border:1px solid #3a3a3d; color:white; border-radius:4px;"><label>до:</label><input type="number" class="event-max-viewers" value="${maxViewers}" style="width:80px; background:#2a2a2d; border:1px solid #3a3a3d; color:white; border-radius:4px;"></div>`;
        } else if (ev === 'cheer') {
            const minBits = cfg.min_bits || '';
            const maxBits = cfg.max_bits || '';
            html += `<div style="margin-top:8px; display:flex; gap:8px; align-items:center;"><label>💎 Битсы от:</label><input type="number" class="event-min-bits" value="${minBits}" style="width:80px; background:#2a2a2d; border:1px solid #3a3a3d; color:white; border-radius:4px;"><label>до:</label><input type="number" class="event-max-bits" value="${maxBits}" style="width:80px; background:#2a2a2d; border:1px solid #3a3a3d; color:white; border-radius:4px;"></div>`;
        } else if (ev === 'subscription_gift') {
            const minTotal = cfg.min_total || '';
            const maxTotal = cfg.max_total || '';
            html += `<div style="margin-top:8px; display:flex; gap:8px; align-items:center;"><label>🎁 Подписки от:</label><input type="number" class="event-min-total" value="${minTotal}" style="width:80px; background:#2a2a2d; border:1px solid #3a3a3d; color:white; border-radius:4px;"><label>до:</label><input type="number" class="event-max-total" value="${maxTotal}" style="width:80px; background:#2a2a2d; border:1px solid #3a3a3d; color:white; border-radius:4px;"></div>`;
        }
        
        html += `
            <div class="event-messages-section" style="margin-top:8px;">
                <label>📝 Сообщения (случайный выбор):</label>
                <div class="event-messages-list" data-event="${ev}">
                    ${(messages || []).map(m => {
                        const msgText = typeof m === 'string' ? m : (m.message || '');
                        return `<div class="dynamic-list-item event-msg-row" style="margin-bottom:4px;"><input type="text" class="event-msg-input" value="${escapeHtml(msgText)}" placeholder="Шаблон сообщения..." style="flex:1; background:#1e1e21; border:1px solid #3a3a3d; color:white; padding:6px; border-radius:4px;"><button class="remove-item" style="background:#53535f; border:none; color:white; padding:2px 8px; border-radius:4px; cursor:pointer;">🗑️</button></div>`;
                    }).join('')}
                </div>
                <button class="add-event-msg-btn action-btn" data-event="${ev}" style="margin-top:4px;">+ Добавить сообщение</button>
            </div>
        </div>`;
    }
    
    container.innerHTML = html;
    
    document.querySelectorAll('.test-event-btn').forEach(btn => {
        btn.onclick = async (e) => {
            e.stopPropagation();
            const ev = btn.dataset.event;
            try {
                await fetch('/api/test_event', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({type: ev}) });
            } catch(ex) { console.error('Test event error:', ex); }
        };
    });
    
    document.querySelectorAll('.event-advanced-toggle').forEach(btn => {
        btn.onclick = (e) => {
            const parent = btn.closest('.event-block');
            const adv = parent.querySelector('.event-advanced-settings');
            adv.style.display = adv.style.display === 'none' ? 'block' : 'none';
        };
    });
    
    document.querySelectorAll('.event-block .event-voice-select').forEach(sel => {
        sel.addEventListener('change', function() { toggleEngineSettings(this); });
    });
    
    document.querySelectorAll('.event-use-with-msg').forEach(cb => {
        cb.onchange = function() {
            const block = this.closest('.event-block');
            const sections = block.querySelectorAll('.reward-format-section');
            sections.forEach(s => s.style.display = this.checked ? '' : 'none');
        };
    });
    
    document.querySelectorAll('.add-event-msg-btn').forEach(btn => {
        btn.onclick = function() {
            const list = this.closest('.event-messages-section').querySelector('.event-messages-list');
            const div = document.createElement('div');
            div.className = 'dynamic-list-item event-msg-row';
            div.style.marginBottom = '4px';
            div.innerHTML = '<input type="text" class="event-msg-input" placeholder="Шаблон сообщения..." style="flex:1; background:#1e1e21; border:1px solid #3a3a3d; color:white; padding:6px; border-radius:4px;"><button class="remove-item" style="background:#53535f; border:none; color:white; padding:2px 8px; border-radius:4px; cursor:pointer;">🗑️</button>';
            div.querySelector('.remove-item').onclick = () => div.remove();
            list.appendChild(div);
        };
    });
    
    document.querySelectorAll('.event-messages-list .remove-item').forEach(btn => {
        btn.onclick = function() { this.closest('.event-msg-row').remove(); };
    });
    
    const rewardMap = events.reward?.reward_voice_map || {};
    renderRewardVoiceMapForEvent(rewardMap);
    
    document.querySelectorAll('.add-reward-voice-btn').forEach(btn => {
        btn.onclick = () => addRewardVoiceRow(btn.dataset.event);
    });
}

function renderRewardVoiceMapForEvent(map) {
    const container = document.querySelector('#reward-voice-list-reward');
    if (!container) return;
    container.innerHTML = '';
    const rewardsArray = Array.from(rewardsSet).sort();
    
    for (const [reward, voiceCfg] of Object.entries(map)) {
        const isObj = typeof voiceCfg === 'object';
        const voice = isObj ? voiceCfg.voice : voiceCfg;
        const rate = isObj ? (voiceCfg.rate || '+0%') : '+0%';
        const volume = isObj ? (voiceCfg.volume || '+0%') : '+0%';
        const pitch = isObj ? (voiceCfg.pitch || '+0Hz') : '+0Hz';
        const isXtts = voice && !voice.startsWith('__') && voice.startsWith('xtts-');
        
        const div = document.createElement('div');
        div.className = 'dynamic-list-item';
        const datalistId = `reward-datalist-${Date.now()}-${Math.random()}`;
        
        div.innerHTML = `
            <input type="text" placeholder="Название награды" value="${escapeHtml(reward)}" class="reward-name" list="${datalistId}" style="flex:1;">
            <datalist id="${datalistId}">${rewardsArray.map(r => `<option value="${escapeHtml(r)}">`).join('')}</datalist>
            <select class="reward-voice-select" style="flex:1;">
                <option value="__silent__" ${voice === '__silent__' ? 'selected' : ''}>🔇 (не озвучивать)</option>
                ${availableVoices.map(v => `<option value="${v.name}" ${v.name===voice ? 'selected' : ''}>${v.name}</option>`).join('')}
            </select>
            <button class="reward-advanced-btn">⚙️</button>
            <button class="remove-item">🗑️</button>
            <div class="advanced-settings" style="width:100%;">
                <div class="edge-settings" style="${isXtts ? 'display:none;' : ''}">
                    <div class="slider-row"><label>⚡ Скорость</label><input type="range" class="reward-rate" min="-50" max="100" value="${parseInt(rate)}"><span class="reward-rate-val">${rate}</span></div>
                    <div class="slider-row"><label>🔊 Громкость</label><input type="range" class="reward-volume" min="-50" max="50" value="${parseInt(volume)}"><span class="reward-vol-val">${volume}</span></div>
                    <div class="slider-row"><label>🎵 Тон</label><input type="range" class="reward-pitch" min="-100" max="100" value="${parseInt(pitch)}"><span class="reward-pitch-val">${pitch}</span></div>
                </div>
                <div class="xtts-settings" style="${isXtts ? '' : 'display:none;'}">
                    <div class="slider-row"><label>🌐 Язык</label><select class="reward-language" style="flex:1; background:#2a2a2d; border:1px solid #3a3a3d; color:white; padding:6px; border-radius:4px;">${availableLanguages.map(l => `<option value="${escapeHtml(l.code)}">${escapeHtml(l.name)}</option>`).join('')}</select></div>
                </div>
            </div>
        `;
        
        const voiceSelect = div.querySelector('.reward-voice-select');
        const advDiv = div.querySelector('.advanced-settings');
        div.querySelector('.reward-advanced-btn').onclick = () => advDiv.classList.toggle('show');
        voiceSelect.addEventListener('change', function() { toggleEngineSettings(this, div); });
        div.querySelector('.remove-item').onclick = () => div.remove();
        
        const rateSlider = div.querySelector('.reward-rate');
        if (rateSlider) rateSlider.oninput = function() { this.parentNode.querySelector('.reward-rate-val').textContent = this.value + '%'; };
        const volSlider = div.querySelector('.reward-volume');
        if (volSlider) volSlider.oninput = function() { this.parentNode.querySelector('.reward-vol-val').textContent = this.value + '%'; };
        const pitchSlider = div.querySelector('.reward-pitch');
        if (pitchSlider) pitchSlider.oninput = function() { this.parentNode.querySelector('.reward-pitch-val').textContent = this.value + 'Hz'; };
        
        container.appendChild(div);
    }
}

function addRewardVoiceRow(eventType) {
    const container = document.querySelector(`#reward-voice-list-${eventType}`);
    if (!container) return;
    const div = document.createElement('div');
    div.className = 'dynamic-list-item';
    const rewardsArray = Array.from(rewardsSet).sort();
    const datalistId = `reward-datalist-${Date.now()}`;
    
    div.innerHTML = `
        <input type="text" placeholder="Название награды" class="reward-name" list="${datalistId}" style="flex:1;">
        <datalist id="${datalistId}">${rewardsArray.map(r => `<option value="${escapeHtml(r)}">`).join('')}</datalist>
        <select class="reward-voice-select" style="flex:1;">
            <option value="__silent__">🔇 (не озвучивать)</option>
            ${availableVoices.map(v => `<option value="${v.name}">${v.name}</option>`).join('')}
        </select>
        <button class="reward-advanced-btn">⚙️</button>
        <button class="remove-item">🗑️</button>
        <div class="advanced-settings" style="width:100%;">
            <div class="edge-settings">
                <div class="slider-row"><label>⚡ Скорость</label><input type="range" class="reward-rate" min="-50" max="100" value="0"><span class="reward-rate-val">+0%</span></div>
                <div class="slider-row"><label>🔊 Громкость</label><input type="range" class="reward-volume" min="-50" max="50" value="0"><span class="reward-vol-val">+0%</span></div>
                <div class="slider-row"><label>🎵 Тон</label><input type="range" class="reward-pitch" min="-100" max="100" value="0"><span class="reward-pitch-val">+0Hz</span></div>
            </div>
            <div class="xtts-settings" style="display:none;">
                <div class="slider-row"><label>🌐 Язык</label><select class="reward-language" style="flex:1; background:#2a2a2d; border:1px solid #3a3a3d; color:white; padding:6px; border-radius:4px;">${availableLanguages.map(l => `<option value="${escapeHtml(l.code)}">${escapeHtml(l.name)}</option>`).join('')}</select></div>
            </div>
        </div>
    `;
    
    const voiceSelect = div.querySelector('.reward-voice-select');
    const advDiv = div.querySelector('.advanced-settings');
    div.querySelector('.reward-advanced-btn').onclick = () => advDiv.classList.toggle('show');
    voiceSelect.addEventListener('change', function() { toggleEngineSettings(this, div); });
    div.querySelector('.remove-item').onclick = () => div.remove();
    div.querySelector('.reward-rate').oninput = function() { this.parentNode.querySelector('.reward-rate-val').textContent = this.value + '%'; };
    div.querySelector('.reward-volume').oninput = function() { this.parentNode.querySelector('.reward-vol-val').textContent = this.value + '%'; };
    div.querySelector('.reward-pitch').oninput = function() { this.parentNode.querySelector('.reward-pitch-val').textContent = this.value + 'Hz'; };
    
    container.appendChild(div);
}

function collectEventsFromPanel() {
    const eventTypes = ['follow', 'subscription', 'subscription_gift', 'cheer', 'raid', 'reward'];
    const newEvents = {};
    
    for (const ev of eventTypes) {
        const block = document.querySelector(`.event-block[data-event="${ev}"]`);
        if (!block) continue;
        
        const enabled = block.querySelector('.event-enabled').checked;
        const voice = block.querySelector('.event-voice-select').value;
        let rate = '+0%', volume = '+0%', pitch = '+0Hz';
        let language = 'ru';
        
        const adv = block.querySelector('.event-advanced-settings');
        if (adv && adv.style.display !== 'none') {
            const isXtts = voice && voice.startsWith('xtts-');
            if (isXtts) {
                const langSel = adv.querySelector('.event-language');
                if (langSel) language = langSel.value;
            } else {
                const r = adv.querySelector('.event-rate');
                if (r) rate = r.value + '%';
                const v = adv.querySelector('.event-volume');
                if (v) volume = v.value + '%';
                const p = adv.querySelector('.event-pitch');
                if (p) pitch = p.value + 'Hz';
            }
        }
        
        let min_viewers = 0, max_viewers = null, min_bits = null, max_bits = null, min_total = null, max_total = null;
        if (ev === 'raid') {
            const mv = block.querySelector('.event-min-viewers');
            if (mv) min_viewers = parseInt(mv.value) || 0;
            const mxv = block.querySelector('.event-max-viewers');
            if (mxv) max_viewers = mxv.value ? parseInt(mxv.value) : null;
        } else if (ev === 'cheer') {
            const mnb = block.querySelector('.event-min-bits');
            if (mnb) min_bits = mnb.value ? parseInt(mnb.value) : null;
            const mxb = block.querySelector('.event-max-bits');
            if (mxb) max_bits = mxb.value ? parseInt(mxb.value) : null;
        } else if (ev === 'subscription_gift') {
            const mnt = block.querySelector('.event-min-total');
            if (mnt) min_total = mnt.value ? parseInt(mnt.value) : null;
            const mxt = block.querySelector('.event-max-total');
            if (mxt) max_total = mxt.value ? parseInt(mxt.value) : null;
        }
        
        let format = '';
        let format_no_msg = '', format_with_msg = '', reward_voice_map = {};
        let use_with_msg = false;
        
        if (ev === 'reward') {
            const uwm = block.querySelector('.event-use-with-msg');
            if (uwm) use_with_msg = uwm.checked;
            const noMsg = block.querySelector('.event-format-no-msg');
            if (noMsg) format_no_msg = noMsg.value;
            const withMsg = block.querySelector('.event-format-with-msg');
            if (withMsg) format_with_msg = withMsg.value;
            
            const rows = block.querySelectorAll('.reward-voice-list .dynamic-list-item');
            reward_voice_map = {};
            rows.forEach(row => {
                const rewardName = row.querySelector('.reward-name').value.trim();
                const rewardVoice = row.querySelector('.reward-voice-select').value;
                if (!rewardName) return;
                const advDiv = row.querySelector('.advanced-settings');
                if (advDiv && advDiv.classList.contains('show')) {
                    const isXtts = rewardVoice && rewardVoice.startsWith('xtts-');
                    if (isXtts) {
                        const langEl = row.querySelector('.reward-language');
                        reward_voice_map[rewardName] = { voice: rewardVoice, language: langEl ? langEl.value : 'ru' };
                    } else {
                        const rRate = row.querySelector('.reward-rate').value + '%';
                        const rVol = row.querySelector('.reward-volume').value + '%';
                        const rPitch = row.querySelector('.reward-pitch').value + 'Hz';
                        reward_voice_map[rewardName] = { voice: rewardVoice, rate: rRate, volume: rVol, pitch: rPitch };
                    }
                } else {
                    reward_voice_map[rewardName] = rewardVoice;
                }
            });
        } else {
            const formatInput = block.querySelector('.event-format');
            if (formatInput) format = formatInput.value;
        }
        
        const msgList = block.querySelector('.event-messages-list');
        const messages = [];
        if (msgList) {
            msgList.querySelectorAll('.event-msg-input').forEach(inp => {
                const val = inp.value.trim();
                if (val) messages.push({message: val});
            });
        }
        
        newEvents[ev] = { enabled, voice, format, rate, volume, pitch, language, min_viewers, max_viewers, min_bits, max_bits, min_total, max_total, format_no_msg, format_with_msg, reward_voice_map, use_with_msg, messages };
    }
    return newEvents;
}

// ---------- Замена текста ----------
function renderTextReplacements(replacements) {
    const container = document.getElementById('text-replacements-list');
    if (!container) return;
    container.innerHTML = '';
    replacements.forEach(rep => {
        const div = document.createElement('div');
        div.className = 'dynamic-list-item';
        div.innerHTML = `<input type="text" placeholder="Старое" value="${escapeHtml(rep.from)}" class="rep-from" style="flex:1;"><input type="text" placeholder="Новое" value="${escapeHtml(rep.to)}" class="rep-to" style="flex:1;"><button class="remove-item">🗑️</button>`;
        div.querySelector('.remove-item').onclick = () => div.remove();
        container.appendChild(div);
    });
}

function getTextReplacementsFromUI() {
    const items = document.querySelectorAll('#text-replacements-list .dynamic-list-item');
    const reps = [];
    items.forEach(item => {
        const from = item.querySelector('.rep-from').value.trim();
        const to = item.querySelector('.rep-to').value.trim();
        if (from && to) reps.push({ from, to });
    });
    return reps;
}

// ---------- Загрузка конфига ----------
function updateRateLabel(v) { document.getElementById('rate-value').textContent = v >=0 ? `Быстрее (+${v}%)` : `Медленнее (${v}%)`; }
function updateVolumeLabel(v) { document.getElementById('volume-value').textContent = v >=0 ? `Громче (+${v}%)` : `Тише (${v}%)`; }
function updatePitchLabel(v) { document.getElementById('pitch-value').textContent = v >=0 ? `Выше (+${v}Hz)` : `Ниже (${v}Hz)`; }

async function loadCurrentConfig() {
    try {
        const r = await fetch('/api/config');
        const d = await r.json();
        currentConfig = d;
        
        document.getElementById('event-cooldown-input').value = d.event_cooldown || 5;
        document.getElementById('min-length-input').value = d.min_length || 3;
        document.getElementById('max-length-input').value = d.max_length || 200;
        document.getElementById('user-cooldown-input').value = d.user_cooldown || 10;
        document.getElementById('filter-broadcaster-checkbox').checked = d.filter_broadcaster !== false;
        document.getElementById('save-audio-checkbox').checked = d.save_audio === true;
        saveAudioMode = d.save_audio === true;
        ttsEnabledToggle.checked = d.tts_enabled !== false;
        document.getElementById('read-all-messages-checkbox').checked = d.read_all_messages !== false;
        document.getElementById('read-only-answered-checkbox').checked = d.read_only_answered === true;
        document.getElementById('filter-highlighted-checkbox').checked = d.role_filters?.highlighted === true;
        document.getElementById('filter-sub-checkbox').checked = d.role_filters?.subscription === true;
        document.getElementById('filter-vip-checkbox').checked = d.role_filters?.vip === true;
        document.getElementById('filter-moderator-checkbox').checked = d.role_filters?.moderator === true;
        document.getElementById('filter-links-checkbox').checked = d.filter_links !== false;
        document.getElementById('filter-emotes-checkbox').checked = d.filter_emotes === true;
        document.getElementById('filter-emoji-checkbox').checked = d.filter_emoji !== false;
        document.getElementById('use-keywords-checkbox').checked = d.use_keywords === true;
        document.getElementById('keywords-input').value = (d.keywords || []).join(', ');
        document.getElementById('strip-keywords-checkbox').checked = d.strip_keywords_from_tts === true;
        document.getElementById('ignore-chars-input').value = d.ignore_chars || '';
        document.getElementById('deduplicate-chars-checkbox').checked = d.deduplicate_chars === true;
        document.getElementById('blacklist-input').value = (d.blacklist_users || []).join(', ');
        document.getElementById('whitelist-input').value = (d.whitelist_users || []).join(', ');
        
        userVoiceMap = d.user_voice_map || {};
        eventsConfig = d.events || {};
        
        renderUserVoiceMapInPanel();
        renderEventsPanel(eventsConfig);
        renderTextReplacements(d.text_replacements || []);
        
        const rate = parseInt(d.rate) || 0;
        const vol = parseInt(d.volume) || 0;
        const pitch = parseInt(d.pitch) || 0;
        document.getElementById('rate-slider').value = rate; updateRateLabel(rate);
        document.getElementById('volume-slider').value = vol; updateVolumeLabel(vol);
        document.getElementById('pitch-slider').value = pitch; updatePitchLabel(pitch);
        
        if (d.voice) {
            document.getElementById('voice-select').value = d.voice;
            toggleXttsSettings();
        }
        if (d.xtts_language && document.getElementById('xtts-language-select').querySelector(`option[value="${d.xtts_language}"]`)) {
            document.getElementById('xtts-language-select').value = d.xtts_language;
        }
        if (d.xtts_speed) {
            const speedVal = Math.round(parseFloat(d.xtts_speed) * 10);
            document.getElementById('xtts-speed-slider').value = speedVal;
            document.getElementById('xtts-speed-value').textContent = d.xtts_speed;
        }
        
        document.getElementById('auto-random-voice-checkbox').checked = d.auto_random_voice === true;
        const readAll = document.getElementById('read-all-messages-checkbox').checked;
        document.getElementById('role-filters-section').style.display = readAll ? 'none' : 'block';
        const useKw = document.getElementById('use-keywords-checkbox').checked;
        document.getElementById('keywords-settings').style.display = useKw ? 'block' : 'none';
    } catch(e) { console.error(e); }
}

// ---------- Сохранение ----------
async function saveAllSettings() {
    const newEvents = collectEventsFromPanel();
    const newUserVoiceMap = getUserVoiceMapFromPanel();
    const selectedVoice = document.getElementById('voice-select').value;
    
    const cfg = {
        voice: selectedVoice,
        auto_random_voice: document.getElementById('auto-random-voice-checkbox').checked,
        rate: `${parseInt(document.getElementById('rate-slider').value) >= 0 ? '+' : ''}${parseInt(document.getElementById('rate-slider').value)}%`,
        volume: `${parseInt(document.getElementById('volume-slider').value)}%`,
        pitch: `${parseInt(document.getElementById('pitch-slider').value)}Hz`,
        xtts_language: document.getElementById('xtts-language-select').value || 'ru',
        xtts_speed: parseFloat(document.getElementById('xtts-speed-slider').value) / 10,
        event_cooldown: parseInt(document.getElementById('event-cooldown-input').value),
        min_length: parseInt(document.getElementById('min-length-input').value),
        max_length: parseInt(document.getElementById('max-length-input').value),
        user_cooldown: parseInt(document.getElementById('user-cooldown-input').value),
        filter_broadcaster: document.getElementById('filter-broadcaster-checkbox').checked,
        save_audio: document.getElementById('save-audio-checkbox').checked,
        tts_enabled: ttsEnabledToggle.checked,
        read_all_messages: document.getElementById('read-all-messages-checkbox').checked,
        read_only_answered: document.getElementById('read-only-answered-checkbox').checked,
        role_filters: {
            highlighted: document.getElementById('filter-highlighted-checkbox').checked,
            subscription: document.getElementById('filter-sub-checkbox').checked,
            vip: document.getElementById('filter-vip-checkbox').checked,
            moderator: document.getElementById('filter-moderator-checkbox').checked,
        },
        filter_links: document.getElementById('filter-links-checkbox').checked,
        filter_emotes: document.getElementById('filter-emotes-checkbox').checked,
        filter_emoji: document.getElementById('filter-emoji-checkbox').checked,
        use_keywords: document.getElementById('use-keywords-checkbox').checked,
        keywords: document.getElementById('keywords-input').value.split(',').map(s=>s.trim()).filter(s=>s),
        strip_keywords_from_tts: document.getElementById('strip-keywords-checkbox').checked,
        ignore_chars: document.getElementById('ignore-chars-input').value,
        deduplicate_chars: document.getElementById('deduplicate-chars-checkbox').checked,
        blacklist_users: document.getElementById('blacklist-input').value.split(',').map(s=>s.trim()).filter(s=>s),
        whitelist_users: document.getElementById('whitelist-input').value.split(',').map(s=>s.trim()).filter(s=>s),
        user_voice_map: newUserVoiceMap,
        text_replacements: getTextReplacementsFromUI(),
        events: newEvents,
    };
    
    try {
        const r = await fetch('/api/config', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(cfg) });
        if (!r.ok) throw new Error();
        addSystemMessage('✅ Настройки сохранены');
        closeSettings();
        closeEventsPanel();
        closeVoiceMapPanel();
    } catch(e) { showNotification('Ошибка сохранения', true); }
}

async function saveVoiceMapOnly() {
    const newUserVoiceMap = getUserVoiceMapFromPanel();
    try {
        const r = await fetch('/api/config', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ user_voice_map: newUserVoiceMap, auto_random_voice: document.getElementById('auto-random-voice-checkbox').checked }) });
        if (r.ok) {
            userVoiceMap = newUserVoiceMap;
            showNotification('Сопоставление голосов сохранено');
            closeVoiceMapPanel();
        } else {
            showNotification('Ошибка сохранения', true);
        }
    } catch(e) { showNotification('Ошибка сохранения', true); }
}

async function saveEventsOnly() {
    const newEvents = collectEventsFromPanel();
    try {
        const r = await fetch('/api/config', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ events: newEvents }) });
        if (r.ok) {
            eventsConfig = newEvents;
            showNotification('Настройки событий сохранены');
            closeEventsPanel();
        } else {
            showNotification('Ошибка сохранения событий', true);
        }
    } catch(e) { showNotification('Ошибка сохранения событий', true); }
}

// ---------- Управление панелями ----------
function openSettings() {
    settingsPanel.classList.add('open');
    settingsOverlay.classList.add('active');
    loadVoices();
    loadCurrentConfig();
}
function closeSettings() { settingsPanel.classList.remove('open'); settingsOverlay.classList.remove('active'); }
function openEventsPanel() {
    eventsPanel.classList.add('open');
    eventsOverlay.classList.add('active');
    renderEventsPanel(eventsConfig);
}
function closeEventsPanel() { eventsPanel.classList.remove('open'); eventsOverlay.classList.remove('active'); }
async function openVoiceMapPanel() {
    voiceMapPanel.classList.add('open');
    voiceMapOverlay.classList.add('active');
    try {
        const r = await fetch('/api/config');
        const d = await r.json();
        userVoiceMap = d.user_voice_map || {};
    } catch(e) { console.error('Voice map load error:', e); }
    renderUserVoiceMapInPanel();
}
function closeVoiceMapPanel() { voiceMapPanel.classList.remove('open'); voiceMapOverlay.classList.remove('active'); }

// ---------- Тест голоса ----------
async function testVoice() {
    try {
        const voice = document.getElementById('voice-select').value;
        const rate = document.getElementById('rate-slider').value;
        const volume = document.getElementById('volume-slider').value;
        const pitch = document.getElementById('pitch-slider').value;
        const lang = document.getElementById('xtts-language-select').value || 'ru';
        const rep = 20;
        const resp = await fetch('/api/generate', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({text: "Тест голоса, привет мир", voice, rate: `${rate}%`, volume: `${volume}%`, pitch: `${pitch}Hz`, language: lang, repetition_penalty: rep})
        });
        const data = await resp.json();
        if (data.output) enqueueAudio(data.output);
        addSystemMessage('🔊 Тест добавлен в очередь');
    } catch(e) { alert(`Ошибка: ${e.message}`); }
}

// ---------- Инициализация ----------
openSettingsBtn.addEventListener('click', openSettings);
closeSettingsBtn.addEventListener('click', closeSettings);
settingsOverlay.addEventListener('click', closeSettings);
headerSaveBtn.addEventListener('click', saveAllSettings);
headerTestBtn.addEventListener('click', testVoice);
toggleAdvancedBtn.addEventListener('click', () => advancedDropdown.classList.toggle('show'));
closeAdvancedBtn.addEventListener('click', () => advancedDropdown.classList.remove('show'));
document.getElementById('rate-slider').addEventListener('input', e => updateRateLabel(parseInt(e.target.value)));
document.getElementById('volume-slider').addEventListener('input', e => updateVolumeLabel(parseInt(e.target.value)));
document.getElementById('pitch-slider').addEventListener('input', e => updatePitchLabel(parseInt(e.target.value)));
document.getElementById('read-all-messages-checkbox').addEventListener('change', e => document.getElementById('role-filters-section').style.display = e.target.checked ? 'none' : 'block');
document.getElementById('use-keywords-checkbox').addEventListener('change', e => document.getElementById('keywords-settings').style.display = e.target.checked ? 'block' : 'none');
openVoiceMapPanelBtn.addEventListener('click', openVoiceMapPanel);
closeVoiceMapBtn.addEventListener('click', closeVoiceMapPanel);
voiceMapOverlay.addEventListener('click', closeVoiceMapPanel);
document.getElementById('voice-map-save-btn').addEventListener('click', saveVoiceMapOnly);
document.getElementById('voice-map-search').addEventListener('input', () => renderUserVoiceMapInPanel());
openEventsPanelBtn.addEventListener('click', openEventsPanel);
closeEventsBtn.addEventListener('click', closeEventsPanel);
eventsOverlay.addEventListener('click', closeEventsPanel);
document.getElementById('events-save-btn').addEventListener('click', saveEventsOnly);

document.getElementById('add-user-voice-panel-btn').addEventListener('click', () => {
    const container = document.getElementById('user-voice-list-panel');
    const div = document.createElement('div');
    div.className = 'dynamic-list-item';
    div.innerHTML = `
        <input type="text" placeholder="Имя пользователя" class="map-user" style="flex:1;">
        <select class="map-voice" style="flex:1;"><option value="">🎤 (не выбран)</option>${availableVoices.map(v => `<option value="${v.name}">${v.name}</option>`).join('')}</select>
        <button class="voice-advanced-btn">⚙️</button>
        <button class="remove-item">🗑️</button>
        <div class="advanced-settings" style="width:100%;">
            <div class="edge-settings">
                <div class="slider-row"><label>⚡ Скорость</label><input type="range" class="adv-rate" min="-50" max="100" value="0"><span class="adv-rate-val">+0%</span></div>
                <div class="slider-row"><label>🔊 Громкость</label><input type="range" class="adv-volume" min="-50" max="50" value="0"><span class="adv-vol-val">+0%</span></div>
                <div class="slider-row"><label>🎵 Тон</label><input type="range" class="adv-pitch" min="-100" max="100" value="0"><span class="adv-pitch-val">+0Hz</span></div>
            </div>
            <div class="xtts-settings" style="display:none;">
                <div class="slider-row"><label>🌐 Язык</label><select class="adv-language" style="flex:1; background:#2a2a2d; border:1px solid #3a3a3d; color:white; padding:6px; border-radius:4px;">${availableLanguages.map(l => `<option value="${escapeHtml(l.code)}">${escapeHtml(l.name)}</option>`).join('')}</select></div>
            </div>
        </div>
    `;
    const voiceSelect = div.querySelector('.map-voice');
    const advDiv = div.querySelector('.advanced-settings');
    div.querySelector('.voice-advanced-btn').onclick = () => advDiv.classList.toggle('show');
    voiceSelect.addEventListener('change', function() { toggleEngineSettings(this, div); });
    div.querySelector('.remove-item').onclick = async () => {
        const user = div.querySelector('.map-user').value.trim();
        if (user) {
            try {
                const r = await fetch(`/api/voice_map/${encodeURIComponent(user)}`, { method:'DELETE' });
                if (r.ok) {
                    delete userVoiceMap[user];
                    delete userLastMessageTime[user];
                    usernamesSet.delete(user);
                    saveUsernameCache();
                    renderUserVoiceMapInPanel();
                }
            } catch(e) { console.error('Delete error:', e); }
        } else {
            div.remove();
        }
    };
    div.querySelector('.adv-rate').oninput = function() { this.parentNode.querySelector('.adv-rate-val').textContent = this.value + '%'; };
    div.querySelector('.adv-volume').oninput = function() { this.parentNode.querySelector('.adv-vol-val').textContent = this.value + '%'; };
    div.querySelector('.adv-pitch').oninput = function() { this.parentNode.querySelector('.adv-pitch-val').textContent = this.value + 'Hz'; };
    setupUserSuggestions(div.querySelector('.map-user'), Array.from(usernamesSet).sort());
    container.appendChild(div);
});

document.getElementById('add-replacement-btn').addEventListener('click', () => {
    const container = document.getElementById('text-replacements-list');
    const div = document.createElement('div');
    div.className = 'dynamic-list-item';
    div.innerHTML = `<input type="text" placeholder="Старое" class="rep-from" style="flex:1;"><input type="text" placeholder="Новое" class="rep-to" style="flex:1;"><button class="remove-item">🗑️</button>`;
    div.querySelector('.remove-item').onclick = () => div.remove();
    container.appendChild(div);
});

document.getElementById('chat-send-btn').addEventListener('click', sendChatMessage);
document.getElementById('chat-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') sendChatMessage(); });

async function sendChatMessage() {
    const input = document.getElementById('chat-input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    try {
        const r = await fetch('/api/send_chat', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ text }) });
        const data = await r.json();
        if (!data.success) addSystemMessage(`❌ Ошибка: ${data.error || 'неизвестная'}`);
    } catch(e) {
        addSystemMessage(`❌ Ошибка отправки: ${e.message}`);
    }
}

// Старт
loadRewardsCache();
loadUsernameCache();
fetchStatus();
    setTimeout(fetchStatus, 200);
    setTimeout(fetchStatus, 600);
    statusUpdateInterval = setInterval(fetchStatus, 1000);
    logsUpdateInterval = setInterval(fetchLogs, 2000);
    connectSSE();
loadEmotes();
loadCurrentConfig();
loadVoices();